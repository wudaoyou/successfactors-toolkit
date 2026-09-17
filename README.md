# SuccessFactors Toolkit

[![CI](https://github.com/wudaoyou/successfactors-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/wudaoyou/successfactors-toolkit/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

A self-hosted toolkit for SAP SuccessFactors API troubleshooting, payload
extraction, and integration development, exposed as a REST API and as an MCP
(Model Context Protocol) server.

| API | Protocol | Endpoint prefix |
|-----|----------|-----------------|
| EC SFAPI — Compound Employee | SOAP 1.1 | `/api/sfapi/ce/` |
| OData | REST (v2) | `/api/odata/` |

This is an independent project, not affiliated with or endorsed by SAP SE.
SAP and SuccessFactors are trademarks of SAP SE.

## Before you start: it's fail-closed

The REST API refuses every `/api/*` request with `503` until you set
`API_KEY`, and refuses every `/api/tenants/*` request with `503` until you
also set `ADMIN_API_KEY`. There is no "works out of the box, insecure"
mode — set both before you can call anything:

```sh
cp .env.example .env
# edit .env: set API_KEY and ADMIN_API_KEY to independent random values
```

Requests then authenticate with an `X-API-Key` header (all `/api/*` routes)
and, for `/api/tenants/*`, an additional `X-Admin-Key` header. `CORS_ORIGINS`
is a JSON list of allowed browser origins and defaults to `[]` (closed).

The project ships no SuccessFactors credentials. See
[Connect to SuccessFactors](#connect-to-successfactors) below to generate
your own key pair and register it with your tenant.

## Install

Requires Python 3.12+.

```sh
pip install .
# or, for development:
pip install -e ".[dev]"
```

### Run the REST API

```sh
uvicorn successfactors_toolkit.main:app --host 127.0.0.1 --port 8000
```

Interactive docs (Swagger UI): `http://127.0.0.1:8000/docs`.

### Run with Docker

```sh
docker compose up --build
```

Binds to `127.0.0.1:8000` by default (see `docker-compose.yml`). Tenant keys
are stored in a named volume mounted at `TENANT_KEYS_DIR=/data/tenants`
inside the container.

### Run the MCP server

```sh
successfactors-mcp
# or: python -m successfactors_toolkit.mcp_server
```

Speaks MCP over stdio — see [MCP server](#mcp-server-claude-desktop-claude-code)
below for client configuration.

### Verify it's running

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.0-rc.1"}
```

## Cheat sheet

```bash
# Register a tenant's key+cert (one-time per company)
curl -X POST http://127.0.0.1:8000/api/tenants/demo/keypair \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -F "private_key=@secrets/keypair_demo/private_key.pem" \
  -F "certificate=@secrets/keypair_demo/certificate.crt"

# Single-employee lookup by PERSON_ID_EXTERNAL
curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query-by-person-id \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"person_id_external": ["EMP001"]}'

# Delta extract + auto-pagination
curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query-all \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"last_modified_on": "2026-04-01T00:00:00+0000", "max_rows": 800}'

# OData: pull EmpJob with code -> description in one shot via $expand
curl -X POST http://127.0.0.1:8000/api/odata/execute \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"path": "EmpJob", "params": {
        "$top": 5,
        "$expand": "jobCodeNav,locationNav",
        "$select": "userId,jobCode,jobCodeNav/name,location,locationNav/name"
      }}'

# OData: bulk-extract an entity set across all pages
curl -X POST http://127.0.0.1:8000/api/odata/extract \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"path": "EmpJob", "params": {"paging": "cursor", "$top": 1000,
        "fromDate": "1900-01-01", "toDate": "9999-12-31"}}'

# List registered tenants (with cert expiry warnings)
curl http://127.0.0.1:8000/api/tenants \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"
```

## Connect to SuccessFactors

Authentication is OAuth2 SAML Bearer Assertion, which requires an RSA key
pair registered as an X.509 certificate in SuccessFactors. The project never
ships or generates credentials for you — you provide your own tenant.

### 1. Generate a key pair

```sh
./scripts/generate-keypair.sh <company_id> [technical_user_CN] [validity_days]
# e.g.
./scripts/generate-keypair.sh demo APIUSER 730
```

This writes `secrets/keypair_<company_id>/private_key.pem` (mode 600) and
`secrets/keypair_<company_id>/certificate.crt`, both gitignored. Upload
`certificate.crt` to **SF Admin Center → Manage OAuth2 Client Applications →
Register a Client Application**, using an X.509 certificate.

Already have a PKCS#12 key pair? Convert it first:

```sh
openssl pkcs12 -in your_keypair.p12 -nocerts -nodes -out private_key.pem
```

### 2. Configure the environment

```sh
cp .env.example .env
```

At minimum set `API_KEY`, `ADMIN_API_KEY`, `SF_HOST`, `SF_CLIENT_KEY`,
`SF_USER_ID` (must equal the certificate's CN), `SF_COMPANY_ID`, and
`SF_TOKEN_URL` (`https://{SF_HOST}/oauth/token`).

### 3. Register the key with the toolkit

Either point `SF_PRIVATE_KEY_PATH` at the PEM file directly, or register it
through the tenant management API so the toolkit stores and validates it for
you — see [Tenant management](#tenant-management) below.

## Environment variables

See `.env.example` for a filled-in starting point and
`successfactors_toolkit/config.py` for the authoritative field list.

| Variable | Description |
|---|---|
| `API_KEY` | Required for any `/api/*` call. Sent as `X-API-Key`. Empty = all `/api/*` routes return 503. |
| `ADMIN_API_KEY` | Required for any `/api/tenants/*` call. Sent as `X-Admin-Key`. Empty = those routes return 503. |
| `CORS_ORIGINS` | JSON list of allowed browser origins. Default `[]` (closed). |
| `SF_HOST` | SuccessFactors host, e.g. `example.invalid`. |
| `SF_ALLOWED_HOSTS` | JSON list of extra hosts a per-request `connection.host`/`token_url` override may target. `SF_HOST` and hosts under SAP's `*.successfactors.{com,eu,cn}` / `*.sapsf.{com,eu,cn}` domains are always allowed; anything else is rejected with `400`. |
| `SF_CLIENT_KEY` | OAuth2 client API key from SF Admin Center. |
| `SF_USER_ID` | Technical user; must equal the certificate's CN. |
| `SF_COMPANY_ID` | Default tenant/company ID. |
| `SF_TOKEN_URL` | `https://{SF_HOST}/oauth/token`. |
| `SF_ODATA_VERSION` | OData REST version, default `v2`. |
| `REQUEST_TIMEOUT` | HTTP timeout in seconds, default `30`. |
| `TENANT_KEYS_DIR` | Where per-tenant key+cert pairs are stored (see below). Default `./tenants`. |
| `RESULTS_DIR` | Where MCP tools and `scripts/download_employee.py` write payload files. Default `./results`. |

### Private key resolution order

For each request, the toolkit resolves the RSA private key in this order:

1. Per-request `connection.private_key_path` — must resolve to a path inside `TENANT_KEYS_DIR`, or the request is rejected with `400`.
2. `{TENANT_KEYS_DIR}/{company_id}/sf_private_key_{company_id}.pem` — populated via the tenant management API.
3. `SF_PRIVATE_KEY_PEM_<COMPANY_ID>` env var (base64-encoded PEM, per company — for CI/CD).
4. `SF_PRIVATE_KEY_PEM` env var (base64-encoded PEM, single-tenant fallback).
5. `SF_PRIVATE_KEY_PATH`, a path template with a `{company_id}` placeholder.

## Tenant management

Per-tenant private keys and certificates are stored on disk under
`{TENANT_KEYS_DIR}/{company_id}/`, one key+cert pair per company. All
`/api/tenants/*` routes — including the read-only list and get — require the
`X-Admin-Key` header in addition to `X-API-Key`.

```bash
# Register (or replace with ?force=true)
curl -X POST http://127.0.0.1:8000/api/tenants/demo/keypair \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -F "private_key=@secrets/keypair_demo/private_key.pem" \
  -F "certificate=@secrets/keypair_demo/certificate.crt"

# List / inspect
curl http://127.0.0.1:8000/api/tenants -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"
curl http://127.0.0.1:8000/api/tenants/demo -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"

# Delete
curl -X DELETE http://127.0.0.1:8000/api/tenants/demo \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"
```

The keypair endpoint validates the key and certificate cryptographically
(matching public key, not expired) before writing anything, returns `409` if
the tenant already exists (bypass with `?force=true`), and returns
certificate metadata including a `days_until_expiry` warning once a cert has
under 90 days left. Installing or deleting a tenant's key invalidates any
cached SFAPI session or OData token for that `company_id`.

## EC SFAPI — Compound Employee (SOAP)

### Single-employee lookup

```bash
curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query-by-person-id \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"person_id_external": ["EMP001", "EMP002"], "include_contingent_workers": true}'

curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query-by-user-id \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": ["jsmith"]}'
```

These default to `COMMON_SEGMENTS` (11 broadly-supported segments); override
with `select_segments` if your tenant returns `INVALID_SFQL` for a
module-gated one.

### Structured filter query

```bash
curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "last_modified_on": "2026-04-01T00:00:00+0000",
    "company": "COMP1,COMP2",
    "employee_class": "FT",
    "max_rows": 200
  }'
```

`person_id_external` and `user_id` take precedence over the date/org/job
filters when set (in that order); `include_contingent_workers` may combine
with either. `last_modified_on` **must carry a timezone offset** (e.g.
`2026-04-01T00:00:00+0000`) — a bare timestamp is rejected with `400`. SAP
caps look-back on this filter at 3 months.

`select_segments` overrides the default `DEFAULT_SEGMENTS` (22 segments,
`SELECT *` is not supported by this API). `max_rows` is 1–800, sent as the
`maxRows` SOAP parameter.

### Pagination

```bash
# Manual, one page at a time
curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query-more \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"query_session": "<querySessionId from the previous response>"}'

# Automatic, all pages in one call
curl -X POST http://127.0.0.1:8000/api/sfapi/ce/query-all \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"last_modified_on": "2026-04-01T00:00:00+0000", "max_rows": 800, "max_pages": 50}'
```

`query-all` returns `page_count`, `total_records`, `truncated`,
`stopped_reason` (`exhausted`, `max_pages`, or a parse/fault error), and the
list of raw pages.

## OData API

| Endpoint | Use case |
|---|---|
| `POST /api/odata/execute` | One arbitrary OData call (`GET`/`POST`/`PATCH`/`PUT`/`DELETE`). |
| `POST /api/odata/extract` | Bulk-extract an entity set, auto-following `__next` links. |
| `POST /api/odata/extract-by-filter-in` | Resolve N codes to records, auto-chunked under SF's `$filter` IN-list limit. |

`$format=JSON` is auto-injected unless the path is `$metadata` (served as
EDMX XML only) or the caller already set `$format`. All three endpoints
accept an optional `connection` override (host, OData version, credentials,
`csrf_protected`) — see [Per-request connection override](#per-request-connection-override).

> **Effective-dated entities** — `EmpJob`, `Position`, all `FO*`, and MDF
> generic objects return only **today's** time slice unless you pass
> `asOfDate`, or `fromDate` + `toDate`. For full history use
> `fromDate=1900-01-01&toDate=9999-12-31`.

### `execute` — one request

```bash
curl -X POST http://127.0.0.1:8000/api/odata/execute \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"method": "GET", "path": "User", "params": {"$top": 5, "$select": "userId,username,email"}}'
```

### `extract` — bulk pages

```bash
curl -X POST http://127.0.0.1:8000/api/odata/extract \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "path": "EmpJob",
    "params": {"$top": 1000, "paging": "cursor",
               "fromDate": "1900-01-01", "toDate": "9999-12-31"},
    "max_pages": 100
  }'
```

Response includes `pages_fetched`, `total_records`, `results` (flattened
`d.results`), `stopped_reason` (`exhausted` | `max_pages` | `http_error` |
`parse_error`), and, if `max_pages` was hit mid-stream, `next_skiptoken` to
resume via `params["$skiptoken"]`.

**Footgun:** `extract` stops at `max_pages` and reports `stopped_reason`,
but a truncated response can otherwise look identical to a complete one at a
glance for an entity set without a `__next` link on its last page — check
`stopped_reason`, not just the HTTP status.

### `extract-by-filter-in` — N-code lookup

```bash
curl -X POST http://127.0.0.1:8000/api/odata/extract-by-filter-in \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "path": "FOJobCode",
    "column": "externalCode",
    "values": ["500001", "500002", "500003"],
    "chunk_size": 1000,
    "max_pages_per_chunk": 100
  }'
```

**Footgun:** each chunk is sent as `col eq 'a' or col eq 'b' or ...` on the
URL query string, not a native `in()` (SF OData v2 doesn't accept it despite
some docs listing it). A large `chunk_size` can push the request line past
SuccessFactors' ~8 KB limit, returning `HTTP 414`. If you see `414`, lower
`chunk_size`.

### Per-request connection override

```json
{
  "connection": {
    "host": "example.invalid",
    "odata_version": "v2",
    "company_id": "demo",
    "private_key_path": "/absolute/path/to/tenants/demo/sf_private_key_demo.pem",
    "csrf_protected": false
  },
  "method": "GET",
  "path": "EmpJob",
  "params": {"$top": 5}
}
```

`host` and `token_url` overrides are only accepted for the configured
`SF_HOST`, a host listed in `SF_ALLOWED_HOSTS`, or a SAP SuccessFactors
datacenter domain; anything else is rejected with `400`.
`private_key_path` must resolve inside `TENANT_KEYS_DIR`, or the request is
rejected with `400`. `csrf_protected` defaults to `false` (CSRF is a
session-cookie defense, not needed under Bearer auth) — set it per request
for tenants that enforce it.

## Response format

All `/api/sfapi/*` and `/api/odata/execute` calls return the same shape:

```json
{
  "status_code": 200,
  "headers": { "content-type": "application/xml" },
  "body": "<raw response body as a string — XML for SFAPI, JSON for OData>"
}
```

## MCP server (Claude Desktop, Claude Code)

`successfactors-mcp` exposes five tools over stdio, reusing the same OAuth2
SAML Bearer flow, tenant key store, and pagination logic as the REST API:

| Tool | Arguments | Returns |
|---|---|---|
| `list_tenants` | — | Registered tenants (cert expiry) plus the `.env` default. |
| `odata_metadata` | `company_id`, `entity` | `{entity: {field: attributes}}` map; inlined when small, always written to file. |
| `compare_metadata` | `company_a`, `company_b`, `entity` | `in_sync`, a summary, and the per-entity drift, diffed server-side. |
| `odata_query` | `path`, `company_id`, `params`, `max_pages`, `preview` | Counts, field names, file path; `preview>0` also inlines that many records. |
| `ce_query` | `company_id`, `person_id_external`, `user_id`, `last_modified_on`, `include_contingent_workers`, `select_segments`, `max_rows`, `max_pages` | Counts and one XML file path per page. |

### Payloads stay on disk

Records are written under `{RESULTS_DIR}/mcp/` (mode `0600`) and the tool
returns the file path plus counts, never the records themselves. This is
deliberate, not a limitation: a single Compound Employee payload runs about
80 KB, it is HR data with no business being echoed into a chat transcript,
and comparing two OData metadata documents in-context would burn tens of
thousands of tokens doing a diff a few lines of Python does instantly.

### Install

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "successfactors": {
      "command": "successfactors-mcp",
      "env": {
        "SF_HOST": "example.invalid",
        "SF_CLIENT_KEY": "...",
        "SF_USER_ID": "APIUSER",
        "SF_COMPANY_ID": "demo",
        "SF_TOKEN_URL": "https://example.invalid/oauth/token",
        "TENANT_KEYS_DIR": "/absolute/path/to/tenants",
        "RESULTS_DIR": "/absolute/path/to/results"
      }
    }
  }
}
```

Claude Code:

```sh
claude mcp add successfactors \
  -e SF_HOST=example.invalid \
  -e SF_CLIENT_KEY=... \
  -e SF_USER_ID=APIUSER \
  -e SF_COMPANY_ID=demo \
  -e SF_TOKEN_URL=https://example.invalid/oauth/token \
  -e TENANT_KEYS_DIR=/absolute/path/to/tenants \
  -e RESULTS_DIR=/absolute/path/to/results \
  -- successfactors-mcp
```

`Settings` reads `.env` from the current working directory, which an MCP
host does not reliably set to the repo root — pass everything needed as an
explicit `env` block (as above), or set the host's working directory to a
folder containing your `.env`, rather than relying on ambient state.

Debug tool calls with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```sh
npx @modelcontextprotocol/inspector successfactors-mcp
```

## Known SuccessFactors API footguns

- **Compound Employee `periodDelta` mode and `isNotFirstQuery`.** When
  driving Compound Employee in period-delta mode across repeated calls, the
  `isNotFirstQuery` flag must be embedded as one of the `<urn:param>`
  entries inside the query's `resultOptions` parameter, not passed as a
  standalone parameter — SAP's SOAP API silently ignores it in the wrong
  place rather than erroring.
- **OData bulk extraction can look complete while being truncated.** See the
  `extract` and `extract-by-filter-in` footguns documented above
  (`stopped_reason` and the ~8 KB request-line limit).

## Development

```sh
git clone https://github.com/wudaoyou/successfactors-toolkit.git
cd successfactors-toolkit
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python3 scripts/check_repository.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch, review, commit, and
version rules, and [docs/RELEASING.md](docs/RELEASING.md) for the release
procedure.

## Data handling

Use synthetic examples and test fixtures. Credentials, certificates, tenant
exports, employee payloads, and generated results do not belong in Git —
`.gitignore` and `scripts/check_repository.py` are a basic guardrail, not a
complete secret or personal-data scanner. See [SECURITY.md](SECURITY.md) for
deployment guidance and how to report a vulnerability.

## License

[Apache License 2.0](LICENSE). Copyright 2026 Justin Gong. See
[NOTICE](NOTICE) for third-party and migrated-code attribution.
</content>
