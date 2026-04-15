## Introduction

CELINE is an open source, modular, and federated digital ecosystem for Local Energy Communities and related stakeholders. Its purpose is to transform heterogeneous cross-sector data into trusted digital services, decision support, and community-facing tools through a platform that prioritises interoperability, data sovereignty, traceability, and openness.

The `celine-dev` repository is the development and integration workspace for that ecosystem. It assembles the main CELINE repositories as submodules under `repositories/**`, providing a single place to coordinate cross-repository development, integration testing, interface alignment, and local platform composition. This repository should be treated as the operational view of the full system rather than the implementation home of one isolated component.

Across the CELINE platform, the core technical objective is consistent: ingest and govern distributed data, refine it into reusable data products, expose it through stable interfaces, enrich it through semantic and analytical layers, and deliver it safely to digital services such as dashboards, Digital Twins, AI assistants, registries, and nudging applications. Each submodule implements one bounded part of that system, but all components are expected to remain compatible with the shared architectural principles, API contracts, governance rules, and semantic models defined for the wider CELINE ecosystem.

The CELINE stack combines data engineering, governed APIs, identity and policy enforcement, semantic interoperability, AI-assisted knowledge access, and open-source operational practices. Technologies explicitly referenced across the architecture include Prefect, Meltano, dbt, PostgreSQL, Parquet, OpenLineage, Marquez, Keycloak, OAuth2 Proxy, OPA, OpenAPI, AsyncAPI, RDF/JSON-LD, SHACL, Qdrant, LlamaIndex, and LLM-based assistant services. Repository-specific AGENTS files define the local responsibilities and constraints for each submodule.

## Submodule model

This repository vendors CELINE component repositories as submodules in `repositories/**`. Agents working here must treat each submodule as the source of truth for its own implementation and local conventions. Cross-repository changes must preserve interface compatibility, shared governance metadata, ontology alignment, and authentication/authorization expectations across the platform.

When changing code from this workspace:
- prefer making changes in the owning submodule rather than adding ad hoc integration logic in `celine-dev`
- verify whether the change affects API contracts, data schemas, governance metadata, ontology mappings, or identity/policy behaviour
- keep documentation and integration configuration aligned with the submodule state

### Python packages

- always use `uv` as package manager, with hatching tool for building and `python-semantic-release` as dev deps.
- use `src/celine/**` for cross package compatibility
- alembic with models in repo root, default to pgsql and assume an instance is available at port `:15432` with credentials `postgres:securepassword123`
- pydantic models for settings management, with defaults set to work in the local dev enironment. Use `host.docker.internal` for cross-service references, which works across services running locally / docker.
- taskfile.yaml for local dev management.  Ensure `run`, `debug` `almebic:*`, `release` cmds are available, see `digital-twin/taskfile.yaml` for reference.

API:  `celine-ai-assistant`, `celine-grid`, `flexibility-api`, `celine-roi`, `celine-webapp`, `dataset-api`, `digital-twin`, `nudging-tool`, `rec-registry`, 
pypi packages: `celine-utils`, `celine-sdk`
tooling / CLIs: `celine-policies`, `ontologies`


## Documenting

Each repository should have a `README.md` with package/repository capabilities and functional details. Details are stored in `docs/*.md` for developers / technical audience where to explain the architecture, design decisions, rationale, features.
These documents are collected and combined in `repositories/celine-eu.github.io`.