# Migration manifest

## Source and boundaries

Source revision: `ab949b54b0e3fb073f550234635f7079e229c6db`. The snapshot contains **111 tracked files**.
Exactly **30 source files** are allowlisted below; the other **81** are not copied.
Existing bootstrap files are maintained independently. No uncommitted source inputs
are included. Read the pinned Git objects rather than copying the working tree.

Source working-tree status SHA-256 at inventory and completion:
`4e8498b38362068708663b98e5cf4ca22194202d4e2d4b9969769fca2676be1b`.

## File allowlist

| Source | Target | Action |
| --- | --- | --- |
| `.dockerignore` | `.dockerignore` | Sanitize and adapt; preserve generic behavior |
| `.env.example` | `.env.example` | Sanitize and adapt; preserve generic behavior |
| `Dockerfile` | `Dockerfile` | Sanitize and adapt; preserve generic behavior |
| `LICENSE` | `licenses/legacy-MIT.txt` | Retain license notice |
| `analysis.py` | `scripts/download_employee.py` | Sanitize and adapt; preserve generic behavior |
| `app/__init__.py` | `app/__init__.py` | Retain empty package marker |
| `app/config.py` | `app/config.py` | Sanitize and adapt; preserve generic behavior |
| `app/main.py` | `app/main.py` | Sanitize and adapt; preserve generic behavior |
| `app/mcp_server.py` | `app/mcp_server.py` | Sanitize and adapt; preserve generic behavior |
| `app/models/__init__.py` | `app/models/__init__.py` | Retain empty package marker |
| `app/models/common.py` | `app/models/common.py` | Sanitize and adapt; preserve generic behavior |
| `app/models/odata.py` | `app/models/odata.py` | Sanitize and adapt; preserve generic behavior |
| `app/models/sfapi.py` | `app/models/sfapi.py` | Sanitize and adapt; preserve generic behavior |
| `app/routers/__init__.py` | `app/routers/__init__.py` | Retain empty package marker |
| `app/routers/odata.py` | `app/routers/odata.py` | Sanitize and adapt; preserve generic behavior |
| `app/routers/sfapi.py` | `app/routers/sfapi.py` | Sanitize and adapt; preserve generic behavior |
| `app/routers/tenants.py` | `app/routers/tenants.py` | Sanitize and adapt; preserve generic behavior |
| `app/services/__init__.py` | `app/services/__init__.py` | Retain empty package marker |
| `app/services/ce_query_builder.py` | `app/services/ce_query_builder.py` | Sanitize and adapt; preserve generic behavior |
| `app/services/odata_client.py` | `app/services/odata_client.py` | Sanitize and adapt; preserve generic behavior |
| `app/services/saml_bearer.py` | `app/services/saml_bearer.py` | Sanitize and adapt; preserve generic behavior |
| `app/services/sfapi_client.py` | `app/services/sfapi_client.py` | Sanitize and adapt; preserve generic behavior |
| `app/services/tenant_store.py` | `app/services/tenant_store.py` | Sanitize and adapt; preserve generic behavior |
| `docker-compose.yml` | `docker-compose.yml` | Sanitize and adapt; preserve generic behavior |
| `requirements.txt` | `requirements.txt` | Sanitize and adapt; preserve generic behavior |
| `scripts/generate-keypair.sh` | `scripts/generate-keypair.sh` | Sanitize and adapt; preserve generic behavior |
| `tests/__init__.py` | `tests/__init__.py` | Retain empty package marker |
| `tests/test_ce_contingent_workers.py` | `tests/test_ce_contingent_workers.py` | Sanitize and adapt; preserve generic behavior |
| `tests/test_mcp_server.py` | `tests/test_mcp_server.py` | Sanitize and adapt; preserve generic behavior |
| `tests/test_odata_metadata.py` | `tests/test_odata_metadata.py` | Sanitize and adapt; preserve generic behavior |

## Exclusions

| Source category | Count | Disposition |
| --- | ---: | --- |
| `CPI/` | 48 | Exclude customer integrations, schemas, mappings, and tests |
| `results/` | 11 | Exclude generated data; do not read or copy contents |
| `certs/` | 2 | Exclude tenant certificates and associated documentation |
| `docs/` | 5 | Exclude internal project and assistant work records |
| Other scripts | 3 | Exclude customer-specific probes and exports |
| Other tests | 2 | Exclude customer integration test harnesses |
| Other root/workflow files | 10 | Do not copy old history, release metadata, README, rules, or helper; keep new bootstrap equivalents |

Ignored/untracked data and assistant directories are outside the snapshot and
remain excluded, including environment files, secret stores, payloads, archives,
local assistant configuration, and in-progress customer integration work.

## License inventory

The new distribution uses Apache-2.0. Generic code copied from the source retains
its original MIT notice in `licenses/legacy-MIT.txt`; `NOTICE` identifies its scope.
No third-party source is being vendored. The following direct runtime dependency
licenses were read from the installed distributions matching the pinned versions.
This inventory does not establish employer ownership or replace review of the
eventual locked dependency set and distribution artifacts.

| Dependency | Version | Declared license |
| --- | --- | --- |
| fastapi | 0.136.1 | MIT |
| uvicorn | 0.47.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| pydantic-settings | 2.14.2 | MIT |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| lxml | 6.1.1 | BSD-3-Clause |
| signxml | 4.4.0 | Apache Software License classifier |
| cryptography | 48.0.1 | Apache-2.0 OR BSD-3-Clause |
| python-multipart | 0.0.31 | Apache-2.0 |
| mcp | 2.2.0 | MIT |

## Verification

Inventory generated from `git ls-tree` at the pinned revision; 30 allowlisted +
81 excluded = 111 tracked files. No sensitive files were opened. The source
working-tree status hash still matches the baseline. Runtime verification remains
part of subsequent migration steps.
