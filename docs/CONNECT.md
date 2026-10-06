# Connect to SuccessFactors

[← README](../README.md)

Authentication is OAuth2 SAML Bearer Assertion, which requires an RSA key
pair registered as an X.509 certificate in SuccessFactors. The project ships
no credentials. You provide your SuccessFactors instance and can use the
helper below to generate a key pair, then register the certificate in
SuccessFactors.

Every instance you connect to is a **system**: one directory under
`SYSTEMS_DIR` holding a JSON file and the key files. For Docker MCP, follow
the [business user guide](DOCKER_MCP_GUIDE.md)'s read-only
`credentials/systems` folder setup. The REST registration examples below are
an alternative for local Python or the REST API.

## 1. Generate a key pair

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

## 2. Create the system

```text
systems/
└── demo/
    ├── demo.json           (mode 644)
    ├── private-key.pem     (mode 600)
    └── signing-cert.crt    (mode 644)
```

Name the directory after the system: lowercase letters, digits, `_` and `-`,
at most 63 characters, starting with a letter or digit. The JSON file inside
has the same name, spelled exactly the same (`DEMO` and `demo` are different
systems, also on case-insensitive filesystems). Copy the generated key and
certificate in as `private-key.pem` and `signing-cert.crt`, or install them
through the REST API (see [System management](#system-management)). Point
`SYSTEMS_DIR` at the parent folder; the server will not start without it.

`demo.json`:

```json
{
  "type": "successfactors",
  "production": false,
  "company_id": "demo",
  "host": "api4preview.sapsf.com",
  "token_url": "https://api4preview.sapsf.com/oauth/token",
  "client_key": "<API key of the OAuth client>",
  "user_id": "APIUSER"
}
```

| Key | Required | Notes |
|---|---|---|
| `type` | yes | `successfactors` for the systems on this page. Plugins can add system types; their files live in the same directory. |
| `production` | yes | `true` or `false` (a JSON boolean). See [Production or test](#production-or-test). |
| `company_id` | yes | The SuccessFactors company ID. |
| `host` | yes | SuccessFactors API host, e.g. `api4preview.sapsf.com`. |
| `token_url` | yes | `https://<host>/oauth/token`. |
| `client_key` | yes | OAuth2 client API key from SF Admin Center. |
| `user_id` | yes | Technical user; must equal the certificate's CN. |
| `odata_version` | no | `v2` (default) or `v4`. `v4` sends paths to `/odatav4/` and expects them to start at a service root; see [OData v4](ODATA.md#odata-v4). |
| `pii_filter_tier` | no | `0`–`3` for a test system; `3` or absent for production. See [Production or test](#production-or-test). |
| `pii_extra_fields` | no | Fields beyond the built-in list to tokenize, per entity, e.g. `{"PerPersonal": {"customString6": 2}}`; `{}` for an entity marks it reviewed, so it isn't tokenized whole (see [PII tokenization](MCP_SERVER.md#pii-tokenization)). |

A file without a valid `type` or `production`, with an unknown key (a typo
such as `pii_tier`) or with a wrong value is refused. The tools answer with
`system_invalid` and a `detail` that names the field; `list_systems` shows the
system with its `error`. The file holds no private key (`client_key` is
its only credential) and is read on every call, so edits take effect without a restart.

`host` and `token_url` must be a SAP datacenter host (`*.successfactors.{com,eu,cn}`,
`*.sapsf.{com,eu,cn}`) or listed in `SF_ALLOWED_HOSTS`; any other value makes
every call to that system fail.

Several systems differ in OAuth client, technical user, host or PII tier
simply by having their own directory:

```text
systems/
├── demo/
│   ├── demo.json           {"type": "successfactors", "production": false, ...}
│   ├── private-key.pem
│   └── signing-cert.crt
└── demo2/
    ├── demo2.json          {"type": "successfactors", "production": false, "pii_filter_tier": 2, ...}
    ├── private-key.pem
    └── signing-cert.crt
```

## Environment variables

See `.env.example` for a filled-in starting point and
`successfactors_toolkit/config.py` for the authoritative field list.
Connection settings live in the system files, not in the environment: the
server does not start while a removed variable (`SF_HOST`, `SF_COMPANY_ID`,
`SF_CLIENT_KEY`, `SF_USER_ID`, `SF_TOKEN_URL`, `SF_ODATA_VERSION`,
`SF_PRIVATE_KEY_*`, `TENANT_KEYS_DIR`, `PII_FILTER_TIER`, `PII_EXTRA_FIELDS`)
is set.

| Variable | Description |
|---|---|
| `SYSTEMS_DIR` | **Required.** The folder holding one directory per system. The server does not start if it is unset or not an existing directory. |
| `API_KEY` | Required for any `/api/*` call. Sent as `X-API-Key`. Empty = all `/api/*` routes return 503. It grants the REST data routes for every system on the server, each under the system's own key and configured identity (see [Security](../SECURITY.md#what-api_key-grants)). |
| `ADMIN_API_KEY` | Required for any `/api/systems/*` call. Sent as `X-Admin-Key`. Empty = those routes return 503. |
| `CORS_ORIGINS` | JSON list of allowed browser origins. Default `[]` (closed). |
| `SF_ALLOWED_HOSTS` | JSON list of extra hosts a system file's `host`/`token_url`, or a per-request `connection.host`/`token_url` override, may name. Hosts under SAP's `*.successfactors.{com,eu,cn}` / `*.sapsf.{com,eu,cn}` domains are always allowed; anything else is rejected (`400` on REST). |
| `REQUEST_TIMEOUT` | HTTP timeout in seconds, default `120` (long-running queries can take minutes per SAP KBA 2735876). |
| `MAX_RESPONSE_BYTES`, `MAX_EXTRACT_BYTES`, `MAX_EXTRACT_SECONDS`, `MAX_FILTER_VALUES` | Per-call limits, see [Limits](#limits). |
| `RESULTS_DIR` | Payload output root. Program default: `./results`; MCP adds `/mcp/`. The Docker MCP guide sets `/data` and mounts a host `data` folder there. |
| `RESULTS_RETENTION_DAYS` | Files in `{RESULTS_DIR}/mcp/` older than this are deleted when the next payload is written. Default `7`; `0` keeps them. |
| `PII_VAULT_DIR` | Where the token key and vault live. Default `./pii_vault`. Must persist and must not be inside `RESULTS_DIR`. |

Under Docker Compose, `docker-compose.mcp.yml` has no `env_file`: the
settings above reach the MCP container only through the service's
`environment:` block. Add `SF_ALLOWED_HOSTS`, `REQUEST_TIMEOUT`,
`RESULTS_RETENTION_DAYS` or the `MAX_*` limits there.

### Limits

Each call is bounded in memory and time. A call that passes a limit fails with
an error (`413` or `504` on the REST API) and never returns a shortened result
as if it were complete.

| Limit | Default | Applies to |
|---|---|---|
| `MAX_RESPONSE_BYTES` | 50 MiB | One SuccessFactors response, measured after decompression. |
| `MAX_EXTRACT_BYTES` | 500 MiB | Response data gathered across the pages of one OData `extract` / `extract-by-filter-in` or Compound Employee `query-all` call. |
| `MAX_EXTRACT_SECONDS` | 1800 | Duration of one multi-page extract, including the MCP `ce_query`; checked between pages. |
| `MAX_FILTER_VALUES` | 10000 | Distinct `values` in one `extract-by-filter-in` call. |

Fixed: one request, including its retries, takes at most 600 s, and 429
`Retry-After` waits add up to at most 300 s (the 429 is returned after that).
`max_pages` is at most 1000 for OData and 500 for Compound Employee.

### Private key

For each request, the toolkit reads `{SYSTEMS_DIR}/{system}/private-key.pem`.
A per-request `connection.private_key_path` may name another file, but it must
resolve to a path inside that system's directory, or the request is rejected
with `400`. Directory and file names must match the system name exactly,
including case, on case-insensitive filesystems too.

## System management

The REST API installs keys, sets the environment and deletes systems. It never
creates a system: `{name}/{name}.json` must exist first. All `/api/systems/*`
routes, including the read-only list and get, require the `X-Admin-Key`
header in addition to `X-API-Key`.

```bash
# Install a key and certificate (replace with ?force=true)
curl -X POST http://127.0.0.1:8000/api/systems/demo/keypair \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -F "private_key=@secrets/keypair_demo/private_key.pem" \
  -F "certificate=@secrets/keypair_demo/certificate.crt"

# List / inspect
curl http://127.0.0.1:8000/api/systems -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"
curl http://127.0.0.1:8000/api/systems/demo -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"

# Delete the system's whole directory
curl -X DELETE http://127.0.0.1:8000/api/systems/demo \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY"
```

The keypair endpoint validates the key and certificate cryptographically
(RSA key of at least 2048 bits, matching public key, certificate currently
valid) before writing anything, and stores them as `private-key.pem` and
`signing-cert.crt`, leaving the directory's other files alone. It returns
`404` (`system_not_found`) when `{name}/{name}.json` does not exist, `400`
(`system_invalid`) when the file fails validation, `400`
(`wrong_system_type`) for a system that is not `successfactors`, `409`
(`keypair_already_exists`) if the system already has a keypair (bypass with
`?force=true`), and certificate metadata including a `days_until_expiry`
warning once a cert has under 90 days left. Installing or deleting a system's
key invalidates any cached SFAPI session or OData token for that system.

List and get return each system's `name`, `type`, `production` (`null` = not
declared), `directory`, key and certificate metadata, and, for a file that is
refused, `error` (`system_invalid` or `system_unsupported`) with its `detail`.
The REST service loads no plugins, so a system of a type added by a plugin is
listed with `system_unsupported`.

### Production or test

Every system the MCP `odata_query` and `ce_query` tools read says whether it
is production, with `production` in its `<name>.json`. The value must be a JSON
boolean; the host name is never used to guess.

`true` makes the system's PII tier 3, which nothing can lower; `pii_filter_tier`
must then be `3` or absent. `false` makes it the file's `pii_filter_tier`,
default `1`:

| Tier | Tokenized |
|---|---|
| `0` | Nothing (test systems only). |
| `1` | Stand-alone sensitive PII such as national IDs and bank accounts. |
| `2` | Adds birth dates, home contact data and protected characteristics. |
| `3` | Adds names and other identifying data. |

For an existing system, set or change `production` through the API (`404` for
an unknown system):

```bash
curl -X PUT http://127.0.0.1:8000/api/systems/demo/environment \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"production": false}'
```

The `PUT` changes only `production`; the file's other keys and its file mode
are kept. The REST data routes do not tokenize and do not check the flag.

### Choosing the system on a call

The MCP tools take a `system` argument (the directory name; call
`list_systems` for the names). An empty `system` means the only
`successfactors` system; with several, the call is refused with
`system_required`. The REST data routes take `connection.system` the same
way. Per-request `connection` values (`host`, `token_url`, `private_key_path`)
take precedence over the file and pass the same host allowlist. `user_id` and
`client_key` are the exception: a request may repeat the configured value, but
a different value is rejected with `400`.

Tool errors have the form `{"error": code, "system": name, "detail": text}`:

| Code | Meaning |
|---|---|
| `system_unknown` | No such system, or it is of another type. |
| `system_required` | Several systems fit, or a file has no readable `type`; pass `system`. |
| `system_unsupported` | The file's `type` has no installed handler. |
| `system_invalid` | The file can't be read or fails validation; `detail` names the field. |

`detail` carries paths, field names and validation messages, never file
contents or secret values.
