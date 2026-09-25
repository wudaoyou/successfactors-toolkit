# Connect to SuccessFactors

[← README](../README.md)

Authentication is OAuth2 SAML Bearer Assertion, which requires an RSA key
pair registered as an X.509 certificate in SuccessFactors. The project ships
no credentials. You provide your tenant and can use the helper below to
generate a key pair, then register the certificate in SuccessFactors.

For Docker MCP, follow the [business user guide](DOCKER_MCP_GUIDE.md)'s `credentials/sf.env` and read-only tenant
folder setup. The `.env` and REST registration examples below are an
alternative setup for local Python or the REST API.

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

## 2. Configure the environment

```sh
cp .env.example .env
```

At minimum set `SF_HOST`, `SF_CLIENT_KEY`,
`SF_USER_ID` (must equal the certificate's CN), `SF_COMPANY_ID`, and
`SF_TOKEN_URL` (`https://{SF_HOST}/oauth/token`).
For REST calls also set `API_KEY`; tenant-management endpoints additionally
require `ADMIN_API_KEY`.

## 3. Register the key with the toolkit

Either point `SF_PRIVATE_KEY_PATH` at the PEM file directly, or register it
through the tenant management API so the toolkit stores and validates it for
you — see [Tenant management](#tenant-management) below.

## Environment variables

See `.env.example` for a filled-in starting point and
`successfactors_toolkit/config.py` for the authoritative field list. A
tenant's `{company_id}.json` can override the connection and PII values per
tenant; see [Per-tenant settings](#per-tenant-settings).

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
| `REQUEST_TIMEOUT` | HTTP timeout in seconds, default `120` (long-running queries can take minutes per SAP KBA 2735876). |
| `TENANT_KEYS_DIR` | Where per-tenant key+cert pairs are stored (see below). Default `./tenants`. |
| `RESULTS_DIR` | Payload output root. Program default: `./results`; MCP adds `/mcp/`. The Docker MCP guide sets `/data` and mounts a host `data` folder there. |
| `PII_FILTER_TIER` | MCP PII tokenization level for **test** tenants: `0` off, `1` (default) stand-alone sensitive PII such as national IDs and bank accounts, `2` adds birth dates, home contact data and protected characteristics, `3` adds names and other identifying data. Production tenants are always tier 3 (see [Production or test](#production-or-test)). |
| `PII_EXTRA_FIELDS` | JSON map of tenant-specific fields to tokenize, e.g. `{"PerPersonal": {"customString6": 2}}`. |
| `PII_VAULT_DIR` | Where the token key and vault live. Default `./pii_vault`. Must persist and must not be inside `RESULTS_DIR`. |

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

### Production or test

Every tenant the MCP `odata_query` and `ce_query` tools read must say whether
it is production, in `{TENANT_KEYS_DIR}/{company_id}/{company_id}.json`
(for example `tenants/demo/demo.json`):

```json
{"production": true}
```

`true` makes the tenant's PII tier 3, which nothing can lower; `false` makes
it the file's `pii_filter_tier`, else `PII_FILTER_TIER` (default 1). The value
must be a JSON boolean. Without the file, or without a boolean `production`,
those tools refuse the tenant with `tenant_environment_unset` before anything
is sent to SuccessFactors. The host name is never used to guess. Replacing a
key with `?force=true` keeps the file; deleting the tenant removes it. For
the default tenant keyed from `SF_PRIVATE_KEY_PEM`, create the
`{SF_COMPANY_ID}` folder holding only `{SF_COMPANY_ID}.json`; it is not listed
as a registered tenant.

Version 0.3.3 named this file `tenant.json`. That name is no longer read:
rename it to `{company_id}.json`. Until you do, the refusal's `detail` says so.

For a registered tenant, set or change it through the API (`404` for an
unknown tenant). The list and get responses carry `production` too (`null` =
not declared):

```bash
curl -X PUT http://127.0.0.1:8000/api/tenants/demo/environment \
  -H "X-API-Key: $API_KEY" -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"production": false}'
```

The `PUT` changes only `production`; the file's other keys and its file mode
are kept. The REST data routes do not tokenize and do not check the flag.

### Per-tenant settings

The same file can hold the rest of a tenant's settings, so one server serves
several tenants that differ in OAuth client, technical user, host or PII tier.
Only `production` is required; every other key falls back to the environment:

| Key | Falls back to | Notes |
|---|---|---|
| `production` | — | Required, `true` or `false`. |
| `pii_filter_tier` | `PII_FILTER_TIER` | `0`–`3`, test tenants only. With `"production": true` it must be `3` or absent. |
| `pii_extra_fields` | `PII_EXTRA_FIELDS` | Merged on top of the environment value, per entity and field; the file wins on the same field. |
| `host` | `SF_HOST` | Set together with `token_url`. |
| `token_url` | `SF_TOKEN_URL` | Set together with `host`. |
| `client_key` | `SF_CLIENT_KEY` | |
| `user_id` | `SF_USER_ID` | |
| `odata_version` | `SF_ODATA_VERSION` | `v2` or `v4`. |

```json
{
  "production": false,
  "pii_filter_tier": 2,
  "pii_extra_fields": {"PerPersonal": {"customString6": 2}},
  "host": "api4preview.sapsf.com",
  "token_url": "https://api4preview.sapsf.com/oauth/token",
  "client_key": "<API key of this tenant's OAuth client>",
  "user_id": "APIUSER",
  "odata_version": "v2"
}
```

- Precedence: a per-request connection override on the REST API, then the
  file, then the environment. The MCP tools and the REST data routes all pick
  the file up for the `company_id` they call; an empty `company_id` reads
  `SF_COMPANY_ID`'s file.
- The file is read on every call, so edits take effect without a restart.
  Edit it by hand; the API only sets `production`.
- `host` and `token_url` pass the same allowlist as request overrides
  (`SF_HOST`, `SF_ALLOWED_HOSTS` or a SAP datacenter host).
- An unknown key (a typo such as `pii_tier`) or a wrong value makes the file
  invalid. `odata_query` and `ce_query` then refuse the tenant with
  `{"error": "tenant_config_invalid", "company_id": ..., "detail": ...}`;
  `detail` names the field. `list_tenants` shows the tenant with
  `config_error` and a warning.
- Calls that do not tokenize (`odata_metadata`, `compare_metadata`, the REST
  data routes) still use the connection keys of a file that is otherwise
  invalid or has no `production`. They fail only when a connection key itself
  is invalid, such as a `host` without `token_url` or a host outside the
  allowlist, instead of falling back to another tenant's environment values.
- The private key stays in `sf_private_key_<company_id>.pem`; the file holds
  no secrets.

Example: two tenants with their own OAuth clients and tiers, one server:

```text
tenants/
├── demo/
│   ├── sf_private_key_demo.pem
│   ├── sf_saml_signing_demo.crt
│   └── demo.json          {"production": false, "client_key": "<demo client key>"}
└── demo2/
    ├── sf_private_key_demo2.pem
    ├── sf_saml_signing_demo2.crt
    └── demo2.json         {"production": false, "client_key": "<demo2 client key>", "pii_filter_tier": 2}
```

With `SF_COMPANY_ID=demo` and the shared `SF_HOST`, `SF_TOKEN_URL` and
`SF_USER_ID` in the environment, `company_id=""` or `"demo"` uses demo's
client key at the default tier, and `company_id="demo2"` uses demo2's client
key at tier 2.
