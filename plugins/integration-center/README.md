# successfactors-toolkit-integration-center

A plugin for the successfactors-toolkit MCP server that reads SAP
SuccessFactors Integration Center definitions. It is read-only: it never
creates, changes, runs or deletes an integration.

## Install

Install it into the same environment (or image) as the server, which loads it
through the `successfactors_toolkit.plugins` entry point at startup:

```bash
pip install ./plugins/integration-center
```

The public Docker image does not include it.

It needs a successfactors-toolkit release whose `plugin_api` has
`odata_get`, and an API user with Integration Center access.

## Tools

### `ic_integrations(system="", ran_within_days=None, preview=0)`

Lists the integrations under Integration Center's "My Integrations"
(`IntegrationProcessDefinition` with `definitionType eq 'SAVEDCONTENT'`),
read from the latest version of each and its destination operation and
connection. Rows go to a JSON file under `RESULTS_DIR`; `preview` (0–20)
inlines them when they fit in 16 KiB.

| Field | From |
| --- | --- |
| `id`, `name`, `active` | definition `externalCode`, `externalName`, `isActive` |
| `trigger_type`, `last_attempted_run`, `last_successful_run` | latest version `triggerType`, `lastAttemptedRunTime`, `lastSuccessfulRunTime` (ISO 8601 UTC) |
| `downstream[]`: `operation_type`, `method`, `operation_directory`, `file_name` | destination operation `type`, `method`, `directory`, `fileName` |
| `downstream[]`: `connection_type`, `connection_name`, `dependency_name`, `endpoint`, `port`, `connection_directory` | connection `type`, `externalName`, `dependencyName`, `uri`, `port`, `directory` |

`ran_within_days` (1–3650) keeps integrations whose last attempted run,
successful or not, falls within that many days. Types are SAP's own values,
passed on unchanged; on a REST connection `port` can hold a leftover value
and says nothing about the type.

Errors: `system_*` (see the server docs), `connection_policy`, and
`http_error`/`parse_error` with `status_code` only — never SF's error body.

## What it never returns

Only the properties in the table are requested (`$select`) and only they
are copied into a row, on every PII tier including tier 0; a value of an
unexpected type is dropped. Connection tokens, usernames and keys, operation
encryption and signing keys, the `Keypairs`, `X509Keypairs`, `OAuthSettings`
and `KnownHostKeys` entities, and anything SAP adds later are never returned.
`endpoint` is the URI's scheme, host, port and path: everything up to its
last `@`, and its query and fragment, are dropped, since they can carry
credentials; a value that isn't a plain URI gives `null`. A date outside the
representable range gives `null` too. Person references (`createdBy`, `lastModifiedBy`) are not
returned either. The tool takes no path, filter or expand from the caller and
only ever sends that one GET.

## Known limits

- `_IntegrationCenterRestrictedService` is an internal SAP service that SAP
  does not support for customers (KBAs 2749468, 2402891); it may change.
- A REST connection set to "REST Server Settings from Security Center" may
  have its effective URL in Security Center, not on the connection.
- `last_attempted_run` is the version's own run time; the UI's "Last Run"
  column can differ.
