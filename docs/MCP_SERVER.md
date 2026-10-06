# MCP server for AI agents

[← README](../README.md)

`successfactors-mcp` exposes five tools over stdio, reusing the same OAuth2
SAML Bearer flow, system store, and pagination logic as the REST API. Each
SuccessFactors instance is a system under `SYSTEMS_DIR` (see
[Connect to SuccessFactors](CONNECT.md#2-create-the-system)); every tool except
`list_systems` takes a `system` argument, the system's directory name. An
empty `system` means the only `successfactors` system; with several, the call
is refused with `system_required`.

| Tool | Arguments | Returns |
|---|---|---|
| `list_systems` | — | Every system under `SYSTEMS_DIR` with its `type`, `production`, `pii_filter_tier` and, for SuccessFactors systems, certificate expiry; a system whose file is refused carries `error` and `detail`. Also `plugins`. |
| `odata_metadata` | `system`, `entity` | `{entity: {field: attributes}}` map; inlined when small, always written to file. When `entity` is given, its navigation properties (name, target entity, filterable) are included too — resolved from the full service `$metadata` (cached per system) since entity-scoped `$metadata` omits them. On a v4 system `entity` is a service root (`talent/cdp/Learning.svc/v1`) or root plus entity set; both read that service's `$metadata`. v4 CSDL has no `sap:` attributes: navigations report `filterable` `"true"` and keys count as sortable. |
| `compare_metadata` | `system_a`, `system_b`, `entity` | `in_sync`, a summary, and the per-entity drift, diffed server-side. |
| `odata_query` | `path`, `system`, `params`, `max_pages`, `preview` | Counts, field names, file path; `preview` accepts 0-20 and values above zero inline only when at most 16 KiB. Query options may be passed in `path` (`"EmpJob?$select=..."`) or `params` — both are merged, `params` wins on conflict. When paging and no `$orderby` is given, one is added automatically from the entity's key properties (reported as `orderby_added`); `duplicate_records` and `warnings` (missing keys, suspected silent truncation) are surfaced when relevant. On a v4 system `path` starts at the service root (`talent/cdp/Learning.svc/v1/Items`) and `paging=snapshot` is never added. `max_pages` is 1-1000; responses and extracts are bounded, see [Limits](CONNECT.md#limits). |
| `ce_query` | `system`, `person_id_external`, `user_id`, `last_modified_on`, `include_contingent_workers`, `select_segments`, `max_rows`, `max_pages` | Counts and one XML file path per page. `select_segments` takes only documented segment names; ID filters take letters, digits, space and `_ . @ : / + -`. |

## Payloads stay on disk

Records are written under `{RESULTS_DIR}/mcp/` (mode `0600`) and the tool
returns the file path plus counts by default. OData `preview > 0` explicitly
includes up to 20 records only when their serialized UTF-8 size is at most
16 KiB; an oversized byte payload returns `preview_error` directing you to
inspect the saved file locally. Counts outside 0–20 are rejected before querying. Small metadata maps and comparison results can also be returned
inline. Keep employee payloads on disk unless their values are needed in the
conversation.

Files in `{RESULTS_DIR}/mcp/` older than `RESULTS_RETENTION_DAYS` (default 7)
are deleted when the next payload is written; `0` keeps them. Copy anything you
need longer to another folder.

## Export formats and folders

- **JSON:** `odata_query` writes records to JSON; metadata tools also write JSON.
- **XML:** `ce_query` preserves one raw XML response per page.
- **CSV and other formats:** ask an AI agent with local file read/write and
  conversion tools to transform the saved source file. Connecting this MCP
  alone does not grant those capabilities. Specify columns and how nested
  records should become rows; do not reconstruct exports from chat summaries.
- **Default folder in the Docker guide:** `/data/mcp/` inside the container
  maps to `~/sf-toolkit/data/mcp/` on the host. Give the host path to client-side
  file tools; a returned container path is not automatically a host path.
- **A folder requested in chat:** the AI agent can save a converted file or
  copy to an authorized host folder. To change where MCP writes future raw
  files, change the output bind mount's host source and restart MCP, or change
  `RESULTS_DIR` to a path on another writable, persisted mount (the container's
  root filesystem is read-only). MCP always adds the `mcp/` subdirectory and
  has no per-query destination argument.

Before reporting a complete export, check that OData `stopped_reason` is
`exhausted`, or that Compound Employee has no error and `truncated` is false.
An existing file is not proof that all pages were retrieved.

## PII tokenization

PII values in MCP results — files and previews from `odata_query` and
`ce_query` — are replaced with tokens such as `[PII-T1-3f9a1c2b7d10e4a5]`
before anything is written. The plaintext stays in a local vault under
`PII_VAULT_DIR`.

How far tokenization goes depends on whether the system is production, which
you declare per system with `production` in its
`SYSTEMS_DIR/<name>/<name>.json` (see
[Production or test](CONNECT.md#production-or-test)):

| `<name>.json` | Tier |
|---|---|
| `"production": true` | 3, always. `pii_filter_tier` must be `3` or absent. |
| `"production": false` | The file's `pii_filter_tier` (`0`-`3`; `0` = off), default `1`. |

A file with no boolean `production` (or no `type`, or any other invalid
value) is refused before anything is sent to SuccessFactors:

```json
{"error": "system_invalid", "system": "demo", "detail": "production: Field required"}
```

The codes are `system_unknown` (no such system), `system_required` (several
`successfactors` systems and no `system` given), `system_unsupported` (a
`type` no installed plugin handles) and `system_invalid` (the file is not
valid; `detail` names the field). `list_systems` reports each system's
`type`, `production` and effective `pii_filter_tier`, and its `error` when the
file is refused. `odata_metadata` and `compare_metadata` return schema only
and do not tokenize.

The same file can also set the system's connection (`company_id`, `host`,
`token_url`, `client_key`, `user_id`, `odata_version`) and `pii_extra_fields`,
so one MCP server serves several systems: see
[Connect to SuccessFactors](CONNECT.md#2-create-the-system).

- Within a system the same value always gets the same token, so the model can
  still compare, group, count and join. Tokens are bound to the system name:
  the same value gets a different token on another system, and a token only
  resolves in queries to the system whose results it came from
  (otherwise `pii_unknown_token`). Tokens issued by earlier versions no
  longer resolve in queries; re-run the query. `successfactors-pii-reveal` reveals
  old and new tokens from the same vault.
- Where tokenization is on (a production system, or a test system at tier 1
  or above), the model can pass a token back only as `field eq|ne '<token>'`,
  `field eq|ne null` or `field in '<token>',...` in `$filter`; the server
  resolves it before calling SuccessFactors. `odata_query` refuses anything
  else with `pii_query_refused`, before sending: any other `$filter` use of a
  tokenized field, any `$orderby`, `$apply` or `$compute` reference to one,
  and any `$search`, including inside nested `$expand` options. Navigation
  paths (`xxxNav/field`) are checked against every entity's tokenized fields,
  and bare fields of an entity the map doesn't cover count as tokenized. The
  automatic `$orderby` is skipped, with a warning, when the entity's key
  properties are tokenized. At tier 0 none of this applies.
- The map fails closed. Every text value in records of an entity the map
  doesn't cover (MDF and custom objects, unlisted modules, untyped records)
  and in Compound Employee segments it doesn't cover (e.g. `direct_deposit`,
  `person_relation`, `job_relation`) is tokenized as tier 1, so at every
  tier but 0. Reviewed Employee Central entities with no PII beyond the
  map's cross-entity fields keep plaintext: `EmpJob`, `EmpEmployment`,
  `EmpEmploymentTermination`, `EmpCompensation`, `EmpPayCompRecurring`,
  `EmpPayCompNonRecurring`, `EmpJobRelationships`, `Position`,
  `BenefitEnrollment`, `PaymentInformationV3`, the `FO*` foundation objects
  and picklists, and the Compound Employee employment, job, compensation,
  pay, deduction, global assignment, cost distribution and payment segments.
  To keep an entity or segment you have reviewed in plaintext, list it in
  the system file's `pii_extra_fields` with the
  fields that should still be tokenized, or `{}` for none, e.g.
  `{"cust_Badge": {}}`. Custom fields on known entities (`customString*`,
  `cust_*`, User `custom01`-`custom15`) stay plaintext unless listed there.
- Binary content (photos, document scans) becomes `[PII-T<n>-REDACTED]`.
- Entity-specific fields are found by the record's type: `__metadata.type`
  (v2) or `@odata.type` (v4, requested with full metadata). A top-level
  record without a type is taken to be the queried entity set; any other
  untyped record, or one with a type the PII map doesn't know, fails closed
  as above. URIs that can embed key values (`__metadata` URIs,
  `__deferred`, nested `__next`, v4 `@odata.id`/`...Link`/`@odata.context`
  annotations) are dropped; `__metadata` keeps only `type`. `odata_query`'s
  `next_skiptoken`, which a server may build from key values, is a tier-1
  token; pass it back in `params["$skiptoken"]` to resume.
- Tiers are cumulative: tier 1 covers national IDs, passports, work permits,
  bank accounts and credentials. Tier 2 adds birth dates, home address,
  contact data on Per* entities (all emails and phones), login names,
  nationality, race/ethnicity, disability and veteran status. Tier 3 adds names, gender,
  marital status, photos and User-entity contact fields (email, business
  phone, cell phone). The full map is in
  `successfactors_toolkit/services/pii_filter.py`. Relabeled custom fields go
  in `pii_extra_fields`.
- To restore plaintext in a report the model wrote, run it locally:
  `successfactors-pii-reveal report.md`. With no `-o`, it prints to stdout,
  so redirect it to a path the AI can't read, e.g.
  `successfactors-pii-reveal report.md > ~/private/report.md`. Pass
  `-o FILE` to write a file directly instead.
  `PII_VAULT_DIR` must resolve to the same vault the server used to write the
  tokens; its default is relative to the current working directory, so run
  the command from the same directory as the server, or set `PII_VAULT_DIR`
  explicitly.

This keeps PII out of the model's context in the normal tool flow and refuses
the query forms it can recognize as probing. It is not an access-control
boundary: a model can still learn which records share a value and whether a
value is empty, and, through navigation into entities the map doesn't cover or
custom fields nobody listed, values it wasn't meant to see. Don't give a model
access to a system whose PII it must not be able to infer. Nor is it a sandbox
against an agent that deliberately reads the vault or runs the reveal
command. With a local (non-Docker) server, deny both in Claude Code, e.g. in
`.claude/settings.json`:

```json
{
  "permissions": {
    "deny": [
      "Read(//absolute/path/to/pii_vault/**)",
      "Bash(successfactors-pii-reveal:*)"
    ]
  }
}
```

The Docker setup keeps the vault in a named volume that is not mounted on the
host, which is the stronger option.

## Alternative: configure a locally installed MCP server

The primary Docker setup is in the [business user guide](DOCKER_MCP_GUIDE.md). The following examples
require `pip install .` and use host filesystem paths directly.

In an AI agent that supports the `mcpServers` JSON configuration format, add
the following server entry. Configuration filenames and locations vary by
agent; use its MCP settings or configuration file:

```json
{
  "mcpServers": {
    "successfactors": {
      "command": "successfactors-mcp",
      "env": {
        "SYSTEMS_DIR": "/absolute/path/to/systems",
        "RESULTS_DIR": "/absolute/path/to/data",
        "PII_VAULT_DIR": "/absolute/path/to/pii_vault"
      }
    }
  }
}
```

For agents with a different configuration format or a setup interface, use
these equivalent settings:

| Setting | Value |
|---|---|
| Server name | `successfactors` |
| Transport | `stdio` (local process) |
| Command | `successfactors-mcp`, or its absolute path if it is not on the agent's PATH |
| Arguments | None |
| Environment | The variables shown in the JSON `env` block above |

The agent must support launching a local stdio MCP process; an HTTP-only MCP
connector cannot use this configuration directly. After reloading the agent's
MCP configuration, verify discovery of the five tools and call `list_systems`.

`Settings` reads `.env` from the current working directory, which an MCP
host does not reliably set to the repo root — pass everything needed as an
explicit `env` block (as above), or set the host's working directory to a
folder containing your `.env`, rather than relying on ambient state.

Debug tool calls with the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```sh
npx @modelcontextprotocol/inspector successfactors-mcp
```

## Extending with plugins

The MCP server loads installed packages that declare an entry point in the
group `successfactors_toolkit.plugins`:

```toml
[project.entry-points."successfactors_toolkit.plugins"]
example = "example_plugin:register"
```

`register(mcp)` runs once at startup and adds tools with `@mcp.tool()`.
Import only from `successfactors_toolkit.plugin_api`, which holds the
supported helpers: settings, the shared HTTP pool, URL building that
caller input can't escape, result-file writing, previews, and PII
tokenization. Call `set_status("<entry point name>", fn)`
to report a status block in `list_systems`. A plugin can register its own
system type with `register_system_type`, so `<name>.json` files of that
`type` validate against its model and live in `SYSTEMS_DIR` beside the
SuccessFactors systems; `select_system`, `system_config` and `system_dir`
read them. Keep secrets in separate files in the system's directory, never in
`<name>.json`: `list_systems` shows a plugin's file in full. A plugin that fails to load is
skipped: it is logged to stderr and listed as `loaded: false`. Plugins run
with the server's full privileges, so install only packages you trust.
