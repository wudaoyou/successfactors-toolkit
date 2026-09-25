# EC SFAPI — Compound Employee (SOAP)

[← README](../README.md)

## Single-employee lookup

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

## Structured filter query

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

## Pagination

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

## Known footguns

- **Compound Employee `periodDelta` mode and `isNotFirstQuery`.** When
  driving Compound Employee in period-delta mode across repeated calls, the
  `isNotFirstQuery` flag must be embedded as one of the `<urn:param>`
  entries inside the query's `resultOptions` parameter, not passed as a
  standalone parameter — SAP's SOAP API silently ignores it in the wrong
  place rather than erroring.

See [Response format](REST_API.md#response-format) for the shape of these responses.
