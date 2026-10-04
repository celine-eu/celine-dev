#!/usr/bin/env python3
"""A second, prod-like instance of one service, beside the dev one, and its teardown.

Dev is permissive and everything else is hardened, from one signal
(`celine.sdk.posture`: only `CELINE_ENV=dev` relaxes). A hardened code path is
proved by running it, and this is the run:

  up <svc>        a temporary least-privilege Postgres role (DML only, no DDL,
                  not the owner), then the service from its checkout's `.venv`
                  on its dev port + 10000 with `CELINE_ENV=staging`, explicit
                  OIDC and that role. Waits until it answers, or prints the
                  posture refusal and tears down. `--client svc-x` adds a copy
                  of that Keycloak client with a random secret, for services
                  that hold a client identity (a secret equal to the client id
                  is refused outside dev).
  user <svc> <n>  a temporary realm user `prodlike-<n>` (organisation member,
                  organisation groups, realm roles); `token` mints its real
                  access token (password grant).
  token <svc> <n> prints that token.
  down <svc>      stops the instance, drops the role, deletes the users and the
                  client. Idempotent; safe after a failed `up`.
  run <svc> ... -- <cmd>
                  up, `<cmd>` with PRODLIKE_URL / PRODLIKE_SVC set, down — the
                  teardown runs whatever the command did.

The dev unit on the dev port is never touched. Local hosts only: the harness
refuses a Postgres or Keycloak that is not on this machine. State lives in
`tmp/prodlike/<svc>.json` (gitignored); nothing is written anywhere else.

    task prodlike -- up dataset-api --schema dataset_api --schema ds_dev_gold --read-only
    task prodlike -- user dataset-api orgadmin --org example-rec --org-group admins
    task prodlike -- token dataset-api orgadmin
    task prodlike -- down dataset-api
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOS = ROOT / "repositories"
STATE = ROOT / "tmp" / "prodlike"

KC = os.environ.get("PRODLIKE_KC_URL", "http://keycloak.celine.localhost")
REALM = os.environ.get("PRODLIKE_REALM", "celine")
KC_ADMIN = os.environ.get("PRODLIKE_KC_ADMIN", "admin")
KC_ADMIN_PASSWORD = os.environ.get("PRODLIKE_KC_ADMIN_PASSWORD", "admin")
# The local realm's login client; its secret is its id on this stack only.
TOKEN_CLIENT = os.environ.get("PRODLIKE_TOKEN_CLIENT", "oauth2_proxy")
TOKEN_CLIENT_SECRET = os.environ.get("PRODLIKE_TOKEN_CLIENT_SECRET", TOKEN_CLIENT)
PG_ADMIN = os.environ.get(
    "PRODLIKE_PG_ADMIN_URL", "postgresql://postgres:securepassword123@127.0.0.1:15432/postgres"
)
# The address the service reaches Postgres at; the dev stack's own defaults use it.
PG_SERVICE_HOST = os.environ.get("PRODLIKE_PG_SERVICE_HOST", "172.17.0.1:15432")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "172.17.0.1", "host.docker.internal")

# repo, uvicorn target (+ --factory), cwd inside the repo, dev port, database,
# driver, the settings that carry the database URL, the path polled for "up".
SERVICES: dict[str, dict] = {
    "dataset-api": dict(app="celine.dataset.main:create_app", factory=True, port=8001,
                        db="datasets", driver="postgresql+psycopg",
                        db_vars=["DATABASE_URL", "DATASETS_DATABASE_URL"]),
    "digital-twin": dict(app="celine.dt.main:create_app", factory=True, port=8002, db=None),
    "rec-registry": dict(app="celine.rec_registry.main:create_app", factory=True, port=8004,
                         db="celine_rec_registry", driver="postgresql+asyncpg"),
    "celine-ai-assistant": dict(app="celine.assistant.main:create_app", factory=True, port=8012,
                                db="ai_assistant", driver="postgresql+asyncpg"),
    "celine-webapp": dict(app="celine.webapp.main:app", factory=False, port=8014,
                          db="celine_webapp", driver="postgresql+asyncpg"),
    "celine-grid": dict(app="celine.grid.main:app", factory=False, port=8015,
                        db="grid", driver="postgresql+asyncpg"),
    "nudging-tool": dict(app="celine.nudging.main:create_app", factory=True, port=8016,
                         db="nudging", driver="postgresql+asyncpg"),
    "flexibility-api": dict(app="celine.flexibility.main:create_app", factory=True, port=8017,
                            db="flexibility", driver="postgresql+asyncpg"),
    "celine-roi": dict(app="celine.roi.api.entrypoint:main", factory=False, port=8018,
                       db="roi", driver="postgresql", health="/docs"),
    "celine-community": dict(app="celine.community.main:app", factory=False, port=8019,
                             db="community", driver="postgresql+asyncpg"),
    "onboarding": dict(app="celine.onboarding.main:app", factory=False, port=8040, cwd="src",
                       venv="../.venv", db="rec_onboarding", driver="postgresql+asyncpg",
                       health="/api/health"),
}


def die(msg: str) -> None:
    print(f"prodlike: {msg}", file=sys.stderr)
    sys.exit(1)


def require_local(url: str, what: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in LOCAL_HOSTS and not host.endswith(".localhost"):
        die(f"{what} {host!r} is not on this machine — the harness runs locally only")


# ── state ────────────────────────────────────────────────────────────────────


def state_path(svc: str) -> Path:
    return STATE / f"{svc}.json"


def load(svc: str) -> dict:
    p = state_path(svc)
    return json.loads(p.read_text()) if p.exists() else {"svc": svc, "users": {}}


def save(st: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    p = state_path(st["svc"])
    p.write_text(json.dumps(st, indent=2))
    p.chmod(0o600)


# ── Postgres ─────────────────────────────────────────────────────────────────


def psql(db: str, sql: str) -> str:
    """Run SQL as the local admin. Host psql when present, else the image's."""
    u = urllib.parse.urlsplit(PG_ADMIN)
    env = dict(os.environ, PGPASSWORD=urllib.parse.unquote(u.password or ""))
    args = ["-h", u.hostname or "127.0.0.1", "-p", str(u.port or 5432), "-U", u.username or "postgres",
            "-d", db, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", sql]
    if shutil.which("psql"):
        cmd = ["psql", *args]
    else:
        cmd = ["docker", "run", "--rm", "--network", "host", "-e", f"PGPASSWORD={env['PGPASSWORD']}",
               "postgres:17", "psql", *args]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def create_role(st: dict, db: str, schemas: list[str], read_only: bool) -> str:
    role = f"prodlike_{st['svc'].replace('-', '_')}_{secrets.token_hex(3)}"
    pw = secrets.token_hex(16)
    if not schemas:
        schemas = psql(db, "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
                           "AND nspname <> 'information_schema'").split()
    quoted = ", ".join(f'"{s}"' for s in schemas)
    dml = "SELECT" if read_only else "SELECT, INSERT, UPDATE, DELETE"
    psql(db, f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOCREATEDB NOCREATEROLE")
    st["role"], st["role_db"] = role, db
    save(st)
    psql(db, f'GRANT CONNECT ON DATABASE "{db}" TO {role};'
             f"GRANT USAGE ON SCHEMA {quoted} TO {role};"
             f"GRANT {dml} ON ALL TABLES IN SCHEMA {quoted} TO {role};"
             + ("" if read_only else f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {quoted} TO {role};"))
    print(f"  role     {role} on {db}: {dml.lower()} on {', '.join(schemas)}")
    return pw


def drop_role(st: dict) -> None:
    role, db = st.get("role"), st.get("role_db")
    if not role:
        return
    try:
        psql(db, f"REASSIGN OWNED BY {role} TO CURRENT_USER; DROP OWNED BY {role}; DROP ROLE {role}")
        print(f"  role     {role} dropped")
    except RuntimeError as exc:
        if "does not exist" not in str(exc):
            raise
    st.pop("role", None)


# ── Keycloak ─────────────────────────────────────────────────────────────────


def http(method: str, url: str, token: str | None = None, body=None, form=None):
    data, headers = None, {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except ValueError:
            return e.code, raw.decode(errors="replace"), e.headers


def admin_token() -> str:
    require_local(KC, "Keycloak")
    code, body, _ = http("POST", f"{KC}/realms/master/protocol/openid-connect/token",
                         form=dict(grant_type="password", client_id="admin-cli",
                                   username=KC_ADMIN, password=KC_ADMIN_PASSWORD))
    if code != 200:
        die(f"Keycloak admin login failed: {code} {body}")
    return body["access_token"]


def kc(method: str, path: str, at: str, body=None):
    return http(method, f"{KC}/admin/realms/{REALM}{path}", at, body=body)


def one(items, what: str) -> dict:
    if not items:
        die(f"{what} not found in realm {REALM}")
    return items[0]


def copy_client(st: dict, at: str, source: str) -> tuple[str, str]:
    """A copy of `source` — mappers, scopes, service-account realm roles — under
    a new id and a random secret. The original, which dev units use, is untouched."""
    src = one(kc("GET", f"/clients?clientId={urllib.parse.quote(source)}", at)[1], f"client {source}")
    rep = {k: v for k, v in src.items() if k not in ("id", "secret", "registeredNodes")}
    new_id, secret = f"prodlike-{source}-{secrets.token_hex(3)}", secrets.token_hex(24)
    rep.update(clientId=new_id, secret=secret, publicClient=False)
    for m in rep.get("protocolMappers", []):
        m.pop("id", None)
    code, body, hdr = kc("POST", "/clients", at, rep)
    if code != 201:
        die(f"creating client copy failed: {code} {body}")
    new_uuid = hdr["Location"].rsplit("/", 1)[-1]
    st["client"] = new_uuid
    save(st)
    if src.get("serviceAccountsEnabled"):
        sa_src = kc("GET", f"/clients/{src['id']}/service-account-user", at)[1]
        sa_new = kc("GET", f"/clients/{new_uuid}/service-account-user", at)[1]
        roles = kc("GET", f"/users/{sa_src['id']}/role-mappings/realm", at)[1] or []
        if roles:
            kc("POST", f"/users/{sa_new['id']}/role-mappings/realm", at, roles)
    print(f"  client   {new_id} (copy of {source}, random secret)")
    return new_id, secret


def create_user(st: dict, at: str, name: str, org: str | None, org_groups: list[str],
                realm_roles: list[str]) -> None:
    username = f"prodlike-{name}"
    pw = secrets.token_hex(12)
    code, body, hdr = kc("POST", "/users", at, dict(
        username=username, email=f"{username}@rec.example.org", firstName="Prodlike",
        lastName=name, enabled=True, emailVerified=True,
        credentials=[dict(type="password", value=pw, temporary=False)]))
    if code != 201:
        die(f"creating {username} failed: {code} {body}")
    uid = hdr["Location"].rsplit("/", 1)[-1]
    st["users"][name] = dict(id=uid, username=username, password=pw)
    save(st)
    if org:
        o = one([x for x in kc("GET", "/organizations?briefRepresentation=true&max=500", at)[1]
                 if x.get("alias") == org], f"organisation {org}")
        code, body, _ = kc("POST", f"/organizations/{o['id']}/members", at, uid)
        if code not in (201, 204):
            die(f"adding {username} to {org} failed: {code} {body}")
        groups = kc("GET", f"/organizations/{o['id']}/groups?max=100", at)[1] or []
        for g in org_groups:
            gid = one([x for x in groups if x.get("name") == g], f"group {g} of {org}")["id"]
            code, body, _ = kc("PUT", f"/organizations/{o['id']}/groups/{gid}/members/{uid}", at)
            if code not in (201, 204):
                die(f"adding {username} to {org}/{g} failed: {code} {body}")
    if realm_roles:
        roles = [kc("GET", f"/roles/{urllib.parse.quote(r)}", at)[1] for r in realm_roles]
        kc("POST", f"/users/{uid}/role-mappings/realm", at, roles)
    print(f"  user     {username}: org={org or '-'} groups={','.join(org_groups) or '-'} "
          f"roles={','.join(realm_roles) or '-'}")


def user_token(st: dict, name: str) -> str:
    u = st["users"].get(name) or die(f"no user {name!r} — create it with `user`")
    code, body, _ = http("POST", f"{KC}/realms/{REALM}/protocol/openid-connect/token",
                         form=dict(grant_type="password", client_id=TOKEN_CLIENT,
                                   client_secret=TOKEN_CLIENT_SECRET, username=u["username"],
                                   password=u["password"], scope="openid"))
    if code != 200:
        die(f"token for {u['username']} failed: {code} {body}")
    return body["access_token"]


# ── the instance ─────────────────────────────────────────────────────────────


def answers(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def up(a) -> None:
    spec = SERVICES.get(a.svc) or die(f"unknown service {a.svc!r}; known: {', '.join(SERVICES)}")
    st = load(a.svc)
    if st.get("pid"):
        die(f"{a.svc} already has an instance (pid {st['pid']}); run `down` first")
    require_local(PG_ADMIN, "Postgres")
    repo = REPOS / a.svc
    cwd = repo / spec.get("cwd", ".")
    uvicorn = (cwd / spec.get("venv", ".venv") / "bin" / "uvicorn").resolve()
    if not uvicorn.exists():
        die(f"{uvicorn} missing — prepare the checkout's venv first")
    port = a.port or spec["port"] + 10000
    if answers(f"http://127.0.0.1:{port}/"):
        die(f"port {port} is taken")

    env = {k: v for k, v in os.environ.items() if k not in ("CELINE_ENV", "ENVIRONMENT", "ENV")}
    if a.env_file:
        for line in Path(a.env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    env.pop("CELINE_ENV", None)  # the signal comes from here, never from a file
    issuer = a.issuer or f"{KC}/realms/{REALM}"
    require_local(issuer, "issuer")
    env.update(CELINE_OIDC_BASE_URL=issuer,
               CELINE_OIDC_JWKS_URI=f"{issuer}/protocol/openid-connect/certs")
    if a.env != "unset":
        env["CELINE_ENV"] = a.env

    st.update(svc=a.svc, port=port, env=a.env)
    save(st)
    try:
        if spec.get("db") and not a.no_db:
            pw = create_role(st, spec["db"], a.schema, a.read_only)
            url = f"{spec['driver']}://{st['role']}:{pw}@{PG_SERVICE_HOST}/{spec['db']}"
            for var in spec.get("db_vars", ["DATABASE_URL"]):
                env[var] = url
        if a.client:
            at = admin_token()
            cid, secret = copy_client(st, at, a.client)
            env.update(CELINE_OIDC_CLIENT_ID=cid, CELINE_OIDC_CLIENT_SECRET=secret)
        for kv in a.set:
            k, v = kv.split("=", 1)
            env[k] = v

        log = STATE / f"{a.svc}.log"
        cmd = [str(uvicorn), spec["app"], *(["--factory"] if spec["factory"] else []),
               "--host", "127.0.0.1", "--port", str(port)]
        with open(log, "w") as fh:
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        st["pid"] = p.pid
        save(st)
        print(f"  instance {a.svc} pid {p.pid} on :{port}, CELINE_ENV={env.get('CELINE_ENV', '(unset)')}, log {log}")
        health = f"http://127.0.0.1:{port}{spec.get('health', '/health')}"
        for _ in range(int(a.wait * 2)):
            if p.poll() is not None:
                break
            if answers(health):
                print(f"  up       {health}")
                return
            time.sleep(0.5)
        text = log.read_text()
        lines = [ln for ln in text.splitlines()
                 if "refusing" in ln or ln.lstrip().startswith("- ") or "Error" in ln]
        print("\n".join(lines[-25:]) or text[-3000:], file=sys.stderr)
        down_state(st)
        die(f"{a.svc} did not come up (exit {p.poll()}); torn down")
    except BaseException:
        if st.get("pid") is None:
            down_state(st)
        raise


def down_state(st: dict) -> None:
    pid = st.pop("pid", None)
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(20):
                os.killpg(pid, 0)
                time.sleep(0.25)
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        print(f"  instance pid {pid} stopped")
    drop_role(st)
    if st.get("users") or st.get("client"):
        at = admin_token()
        for name, u in list(st["users"].items()):
            code, _, _ = kc("DELETE", f"/users/{u['id']}", at)
            print(f"  user     {u['username']} deleted ({code})")
            del st["users"][name]
        if st.get("client"):
            code, _, _ = kc("DELETE", f"/clients/{st.pop('client')}", at)
            print(f"  client   deleted ({code})")
    state_path(st["svc"]).unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def instance_args(p):
        p.add_argument("svc")
        p.add_argument("--env", default="staging", help="CELINE_ENV for the instance; 'unset' leaves it out")
        p.add_argument("--port", type=int)
        p.add_argument("--env-file", help="base environment, e.g. a deployment's binding for the service")
        p.add_argument("--schema", action="append", default=[], help="schemas to grant (default: all)")
        p.add_argument("--read-only", action="store_true", help="SELECT only")
        p.add_argument("--no-db", action="store_true", help="no temporary role")
        p.add_argument("--client", help="copy this Keycloak client with a random secret")
        p.add_argument("--issuer", help="OIDC issuer (default: the local realm)")
        p.add_argument("--set", action="append", default=[], metavar="K=V")
        p.add_argument("--wait", type=float, default=40, help="seconds to wait for it to answer")

    instance_args(sub.add_parser("up"))
    instance_args(sub.add_parser("run"))
    u = sub.add_parser("user")
    u.add_argument("svc")
    u.add_argument("name")
    u.add_argument("--org")
    u.add_argument("--org-group", action="append", default=[])
    u.add_argument("--realm-role", action="append", default=[])
    t = sub.add_parser("token")
    t.add_argument("svc")
    t.add_argument("name")
    sub.add_parser("down").add_argument("svc")
    sub.add_parser("status")
    # `run`'s command follows `--`; split it off here, because argparse's
    # REMAINDER would also swallow the options in front of it.
    argv = sys.argv[1:]
    command = argv[argv.index("--") + 1:] if "--" in argv else []
    a = ap.parse_args(argv[: argv.index("--")] if "--" in argv else argv)

    if a.cmd == "up":
        up(a)
    elif a.cmd == "run":
        if not command:
            die("run needs a command after --")
        up(a)
        st = load(a.svc)
        rc = 1
        try:
            rc = subprocess.call(command, env=dict(
                os.environ, PRODLIKE_SVC=a.svc, PRODLIKE_URL=f"http://127.0.0.1:{st['port']}"))
        finally:
            down_state(load(a.svc))
        sys.exit(rc)
    elif a.cmd == "user":
        st = load(a.svc)
        if a.name in st["users"]:
            die(f"user {a.name!r} exists")
        create_user(st, admin_token(), a.name, a.org, a.org_group, a.realm_role)
    elif a.cmd == "token":
        print(user_token(load(a.svc), a.name))
    elif a.cmd == "down":
        down_state(load(a.svc))
    elif a.cmd == "status":
        for p in sorted(STATE.glob("*.json")) if STATE.exists() else []:
            st = json.loads(p.read_text())
            print(f"{st['svc']}: pid={st.get('pid')} port={st.get('port')} role={st.get('role')} "
                  f"users={','.join(st['users']) or '-'} client={'yes' if st.get('client') else '-'}")


if __name__ == "__main__":
    main()
