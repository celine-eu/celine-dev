# celine-dev

The development and integration environment for CELINE. It vendors every component under
`repositories/` and composes them into one running platform.

## Setup, once

**1. Hostnames.** Everything is served by the Caddy in `config/caddy/Caddyfile` on `:80`,
routed by hostname, so the names have to resolve. Add to `/etc/hosts`:

```
172.17.0.1  host.docker.internal
172.17.0.1  keycloak.celine.localhost sso.celine.localhost mqtt.celine.localhost
172.17.0.1  api.celine.localhost
172.17.0.1  webapp.celine.localhost assistant.celine.localhost grid.celine.localhost roi.celine.localhost community.celine.localhost mailpit.celine.localhost
172.17.0.1  onboarding.celine.localhost
172.17.0.1  superset.celine.localhost jupyter.celine.localhost
172.17.0.1  marquez.celine.localhost prefect.celine.localhost mlflow.celine.localhost
```

**2. Dependencies**, for running services from source: `task dev:setup`. This installs each
component's dependencies and then links `celine-sdk` and `celine-utils` from their local
checkouts, so a change in either is visible to the services that consume it. **`uv sync`
undoes that linking** — re-run `task dev:link` after any `task setup` in a component.

Most components carry their dev defaults in config and need no `.env`. Where one is wanted,
copy the component's `.env.example` — or run **`task local:pull`**, which fetches every
`.env`, `taskfile.local.*` and other uncommittable file this workspace needs from
`labs/celine-dev`. It never overwrites a file you have changed; `task local:status` says
what differs and `task local:push` publishes yours. The set it carries is the local-file
table in `taskfile.yaml`.

**3. Check it.** `task dev:doctor` reports missing tools, invalid compose files, missing
virtualenvs and already-taken ports before anything is started. It is also the first step
of `task dev:start`.

## Running the platform

Two modes. Both are driven by the same composition tables at the top of `taskfile.yaml`,
and both are idempotent — re-running a start converges rather than erroring.

### Containers

```
task docker:start          # every stack, in dependency order
task docker:stop           # reverse order
task docker:restart
task docker:ps
```

This is the mode that exercises the Dockerfiles and the compose `environment:` blocks, so
it is the one that validates a change to either.

The core stack is the platform: Caddy, the BFFs (webapp, grid, assistant, roi, community), the APIs
(dataset-api, digital-twin, rec-registry, nudging, flexibility), the five frontend apps,
onboarding — plus the two things none of them run without, Postgres (celine-pipelines'
`datasets-db`) and identity (celine-policies).

Opt-in, started with `STACKS=all` or by name: marquez and prefect, celine-dashboards
(Superset, Jupyter), celine-forecasting (MLflow). `STACKS` also scopes to named
repositories — `task docker:restart STACKS="digital-twin dataset-api"`. `BUILD=true`
rebuilds images.

Nothing here removes volumes. Every service's database lives in `datasets-db`, so that is
`docker volume rm`, by hand, when you mean it.

### From source, with hot reload

```
task dev:start             # containers up, then services from source in tmux
task dev:stop
task dev:restart
task dev:attach            # attach to the tmux session
task dev:status            # what is answering, and where each service runs
```

`dev:start` brings the whole container stack up first — so ordering, health conditions and
the one-shot migration jobs really run — then stops the application containers and starts
each service from its own repository under hot reload, one per tmux window. Python and
TypeScript services both reload on change.

Inside the session, `Ctrl+PageUp` / `Ctrl+PageDown` move between windows and the mouse
works. `Ctrl+B D` detaches without stopping anything.

Per-service, without disturbing the rest:

```
task dev:logs -- grid-api
task dev:svc:restart -- grid-api
task dev:link                # re-link celine-sdk/celine-utils after a uv sync
```

A service that crashes leaves its window and its traceback on screen; the next
`task dev:start` restarts just that one.

**What dev mode does not check.** The services it runs from source read their component's
`.env`, never the compose `environment:` block, and never the Dockerfile. A change to
either is verified by `task docker:restart`, not by this mode.

### For end-to-end runs

```
task dev:start ATTACH=false
task dev:wait TIMEOUT=180    # blocks until every service answers; non-zero if not
```

## Adding a service

Add a row to `DOCKER_STACKS` — or `DOCKER_STACKS_EXTRA` if it is opt-in — and, if it should
run from source, to `DEV_SERVICES` in `taskfile.yaml`. Nothing else needs to change. Read
`.agents/knowledge/stack-composition-traps.md` first: it states what the port column means
and which services cannot be lifted out of Docker at all.

## Working across the repositories

```
task repo:pull             # git pull in every component
task repo:push -- "msg"    # commit and push all of them
task repo:upgrade-sdk      # bump celine-sdk everywhere it is a dependency
task vscode:workspace      # regenerate the multi-root workspace
```

## If nothing answers on :80

Read `docker-compose.override.yaml`. It is generated rather than committed, and it explains
itself: another project on this machine may own `:80` and reverse-proxy this stack behind
it.
