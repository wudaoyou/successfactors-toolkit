# MCP server for AI agents

[← README](../README.md)

`successfactors-mcp` exposes five tools over stdio, reusing the same OAuth2
SAML Bearer flow, tenant key store, and pagination logic as the REST API:

| Tool | Arguments | Returns |
|---|---|---|
| `list_tenants` | — | Registered tenants (cert expiry) plus the `.env` default. |
| `odata_metadata` | `company_id`, `entity` | `{entity: {field: attributes}}` map; inlined when small, always written to file. When `entity` is given, its navigation properties (name, target entity, filterable) are included too — resolved from the full service `$metadata` (cached per `company_id`) since entity-scoped `$metadata` omits them. |
| `compare_metadata` | `company_a`, `company_b`, `entity` | `in_sync`, a summary, and the per-entity drift, diffed server-side. |
| `odata_query` | `path`, `company_id`, `params`, `max_pages`, `preview` | Counts, field names, file path; `preview` accepts 0-20 and values above zero inline only when at most 16 KiB. Query options may be passed in `path` (`"EmpJob?$select=..."`) or `params` — both are merged, `params` wins on conflict. When paging and no `$orderby` is given, one is added automatically from the entity's key properties (reported as `orderby_added`); `duplicate_records` and `warnings` (missing keys, suspected silent truncation) are surfaced when relevant. |
| `ce_query` | `company_id`, `person_id_external`, `user_id`, `last_modified_on`, `include_contingent_workers`, `select_segments`, `max_rows`, `max_pages` | Counts and one XML file path per page. |

## Payloads stay on disk

Records are written under `{RESULTS_DIR}/mcp/` (mode `0600`) and the tool
returns the file path plus counts by default. OData `preview > 0` explicitly
includes up to 20 records only when their serialized UTF-8 size is at most
16 KiB; an oversized byte payload returns `preview_error` directing you to
inspect the saved file locally. Counts outside 0–20 are rejected before querying. Small metadata maps and comparison results can also be returned
inline. Keep employee payloads on disk unless their values are needed in the
conversation.

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
  `RESULTS_DIR` to another writable, persisted container path. MCP always adds
  the `mcp/` subdirectory and has no per-query destination argument.

Before reporting a complete export, check that OData `stopped_reason` is
`exhausted`, or that Compound Employee has no error and `truncated` is false.
An existing file is not proof that all pages were retrieved.

## PII tokenization

PII values in MCP results — files and previews from `odata_query` and
`ce_query` — are replaced with tokens such as `[PII-T1-3f9a1c2b7d10e4a5]`
before anything is written. The plaintext stays in a local vault under
`PII_VAULT_DIR`.

How far tokenization goes depends on whether the tenant is production, which
you declare per tenant in `{TENANT_KEYS_DIR}/{company_id}/tenant.json` (see
[Production or test](CONNECT.md#production-or-test)):

| `tenant.json` | Tier |
|---|---|
| `{"production": true}` | 3, always. `PII_FILTER_TIER` cannot lower it. |
| `{"production": false}` | `PII_FILTER_TIER` (`0`–`3`, default `1`; `0` = off). |
| missing, or anything else | The call is refused. |

A refused call returns `{"error": "tenant_environment_unset", "company_id":
..., "detail": ...}` before anything is sent to SuccessFactors; `detail` says
where to put the file. An empty `company_id` means `SF_COMPANY_ID`, whose
flag is read the same way. `list_tenants` reports `production` and the
effective `pii_filter_tier` for each tenant and the default, and warns about
tenants without a flag. `odata_metadata` and `compare_metadata` return schema
only and do not check it.

- The same value always gets the same token, so the model can still compare,
  group, count and join. It can also pass a token back in `$filter`; the
  server resolves it before calling SuccessFactors.
- Binary content (photos, document scans) becomes `[PII-T<n>-REDACTED]`.
- Tiers are cumulative: tier 1 covers national IDs, passports, work permits,
  bank accounts and credentials. Tier 2 adds birth dates, home address,
  contact data on Per* entities (all emails and phones), login names,
  nationality, race/ethnicity, disability and veteran status. Tier 3 adds names, gender,
  marital status, photos and User-entity contact fields (email, business
  phone, cell phone). The full map is in
  `successfactors_toolkit/services/pii_filter.py`. Relabeled custom fields go
  in `PII_EXTRA_FIELDS`.
- To restore plaintext in a report the model wrote, run it locally:
  `successfactors-pii-reveal report.md`. With no `-o`, it prints to stdout,
  so redirect it to a path the AI can't read, e.g.
  `successfactors-pii-reveal report.md > ~/private/report.md`. Pass
  `-o FILE` to write a file directly instead.
  `PII_VAULT_DIR` must resolve to the same vault the server used to write the
  tokens; its default is relative to the current working directory, so run
  the command from the same directory as the server, or set `PII_VAULT_DIR`
  explicitly.

This keeps PII out of the model's context in the normal tool flow. It is not a
sandbox against an agent that deliberately reads the vault or runs the reveal
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
        "SF_HOST": "example.invalid",
        "SF_CLIENT_KEY": "...",
        "SF_USER_ID": "APIUSER",
        "SF_COMPANY_ID": "demo",
        "SF_TOKEN_URL": "https://example.invalid/oauth/token",
        "TENANT_KEYS_DIR": "/absolute/path/to/tenants",
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
MCP configuration, verify discovery of the five tools and call `list_tenants`.

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
to report a status block in `list_tenants`. A plugin that fails to load is
skipped: it is logged to stderr and listed as `loaded: false`. Plugins run
with the server's full privileges, so install only packages you trust.
