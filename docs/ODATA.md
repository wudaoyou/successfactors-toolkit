# OData API

[← README](../README.md)

| Endpoint | Use case |
|---|---|
| `POST /api/odata/execute` | One arbitrary OData call (`GET`/`POST`/`PATCH`/`PUT`/`DELETE`). |
| `POST /api/odata/extract` | Bulk-extract an entity set, auto-following next links (`__next`, v4 `@odata.nextLink`). |
| `POST /api/odata/extract-by-filter-in` | Resolve N codes to records, auto-chunked under SF's `$filter` IN-list limit. |

`$format=JSON` is auto-injected unless the path is `$metadata` (served as
EDMX XML only), the caller already set `$format`, or the system uses OData v4
(see [OData v4](#odata-v4)). All three endpoints
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

`max_pages` is 1–1000.

Response includes `pages_fetched`, `total_records`, `results` (flattened
`d.results`, or v4 `value`), `stopped_reason` (`exhausted` | `max_pages` |
`http_error` | `parse_error`), and, if `max_pages` was hit mid-stream,
`next_skiptoken` to resume via `params["$skiptoken"]`.

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

`column` is a property path (letters, digits, `_` and `/`). One call accepts at
most `MAX_FILTER_VALUES` distinct values (default 10000); `max_pages_per_chunk`
is 1–1000.

**Footgun:** each chunk is sent as `col eq 'a' or col eq 'b' or ...` on the
URL query string, not a native `in()` (SF OData v2 doesn't accept it despite
some docs listing it). A large `chunk_size` can push the request line past
SuccessFactors' ~8 KB limit, returning `HTTP 414`. If you see `414`, lower
`chunk_size`.

## OData v4

With `odata_version` `v4` (the system file, or a
per-request `connection`), requests go to `https://{host}/odatav4/{path}`.
SuccessFactors serves each v4 API as its own service, so `path` starts at
the service root, e.g.
`talent/calibration/CalSession.svc/v1/CalibrationSession?$top=5`. The root
ends at the `.svc` segment and its version, or, for services without one
(`talent/continuousfeedback/v1`), at the first version segment; a path with
neither is rejected with `400`. `$metadata` exists per service only
(`talent/calibration/CalSession.svc/v1/$metadata`). The service roots are on
each API's SAP Business Accelerator Hub page. Employee Central entities
(`EmpJob`, `PerPerson`, ...) and Onboarding data are v2 only: the v4
Onboarding and Succession services offer actions, not entity sets.

- JSON is requested with `Accept: application/json;odata.metadata=full`
  instead of `$format`, so every record carries `@odata.type`.
- `extract` reads `value` and follows `@odata.nextLink`, taking only its
  `$skiptoken`, `$skip` and `$top`: host, path and other options stay the
  request's own. After `max_pages`, `next_skiptoken` is set for a
  `$skiptoken` link; for a `$skip` link it is `null` — resume with
  `$skip` = records fetched so far.
- `extract-by-filter-in` sends `col in ('a','b')`.
- `paging=cursor`/`snapshot` are v2 options.

## Per-request connection override

```json
{
  "connection": {
    "host": "example.invalid",
    "odata_version": "v2",
    "system": "demo",
    "private_key_path": "/absolute/path/to/systems/demo/private-key.pem",
    "csrf_protected": false
  },
  "method": "GET",
  "path": "EmpJob",
  "params": {"$top": 5}
}
```

`system` names the directory under `SYSTEMS_DIR` whose
`<system>.json` and key the request uses; an empty `system` means the only
`successfactors` system. A system that is unknown, ambiguous or invalid is
rejected with `400`. `host` and `token_url` overrides are only accepted for a
host listed in `SF_ALLOWED_HOSTS` or a SAP SuccessFactors datacenter domain;
anything else is rejected with `400`. `private_key_path` must resolve inside
`SYSTEMS_DIR/<system>/`, the directory of the system the request names, or the
request is rejected with `400`. `user_id` and `client_key` are the system
file's configured identity: a request may repeat them, but a different value
is rejected with `400`. `csrf_protected` defaults to `false` (CSRF is a
session-cookie defense, not needed under Bearer auth) — set it per request
for systems that enforce it.

## Known SuccessFactors API footguns

- **OData bulk extraction can look complete while being truncated.** See the
  `extract` and `extract-by-filter-in` footguns documented above
  (`stopped_reason` and the ~8 KB request-line limit).

See [Response format](REST_API.md#response-format) for the shape of these responses.
