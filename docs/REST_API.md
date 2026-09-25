# REST API

[← README](../README.md)

## Setup: it's fail-closed

The REST API refuses every `/api/*` request with `503` until you set
`API_KEY`, and refuses every `/api/tenants/*` request with `503` until you
also set `ADMIN_API_KEY`. There is no "works out of the box, insecure"
mode — set `API_KEY` for REST calls and both keys for tenant-management calls:

```sh
cp .env.example .env
# edit .env: set API_KEY and ADMIN_API_KEY to independent random values
```

Requests then authenticate with an `X-API-Key` header (all `/api/*` routes)
and, for `/api/tenants/*`, an additional `X-Admin-Key` header. `CORS_ORIGINS`
is a JSON list of allowed browser origins and defaults to `[]` (closed).

These two access keys apply to REST endpoints. They are not required by the
stdio MCP workflow in the [README](../README.md), which connects directly to SuccessFactors using
your SF credentials.

The project ships no SuccessFactors credentials. See
[Connect to SuccessFactors](CONNECT.md#connect-to-successfactors) to generate
your own key pair and register it with your tenant.

## Install

Local Python installation requires Python 3.12+. The Docker workflow in the [README](../README.md)
does not require Python on the host.

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

### Run the REST API with Docker (local development)

```sh
docker compose up --build
```

Binds to `127.0.0.1:8000` by default (see `docker-compose.yml`). Tenant keys
are stored in a named volume mounted at `TENANT_KEYS_DIR=/data/tenants`
inside the container. This Compose service runs the REST API, not the MCP
server. For Docker MCP, use the client configuration in the [business user guide](DOCKER_MCP_GUIDE.md).

### Run the MCP server with a local Python installation

```sh
successfactors-mcp
# or: python -m successfactors_toolkit.mcp_server
```

Speaks MCP over stdio — see [MCP server](MCP_SERVER.md#mcp-server-for-ai-agents)
for client configuration.

### Verify the REST API is running

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.2"}
```

For MCP, verify initialization and discovery of the five tools in your AI
client, then call `list_tenants`. A small metadata query verifies SF access;
`list_tenants` alone only reads local configuration and keys.

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

## Response format

All `/api/sfapi/*` and `/api/odata/execute` calls return the same shape:

```json
{
  "status_code": 200,
  "headers": { "content-type": "application/xml" },
  "body": "<raw response body as a string — XML for SFAPI, JSON for OData>"
}
```
