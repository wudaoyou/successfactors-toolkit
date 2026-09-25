# OData API

[← README](../README.md)

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

## `execute` — one request

```bash
curl -X POST http://127.0.0.1:8000/api/odata/execute \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"method": "GET", "path": "User", "params": {"$top": 5, "$select": "userId,username,email"}}'
```

## `extract` — bulk pages

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

## `extract-by-filter-in` — N-code lookup

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

## Per-request connection override

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

## Known SuccessFactors API footguns

- **OData bulk extraction can look complete while being truncated.** See the
  `extract` and `extract-by-filter-in` footguns documented above
  (`stopped_reason` and the ~8 KB request-line limit).

See [Response format](REST_API.md#response-format) for the shape of these responses.
