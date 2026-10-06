# Security Policy

## Supported Versions

Only the latest release receives security fixes. While the project is
pre-1.0, that's the latest `0.x` minor version; older minors and any
pre-release before it are not patched — upgrade to get a fix.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately using
[GitHub private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
(Security tab → Report a vulnerability) on this repository. Do not open a
public issue for a security report.

Include, where possible:

- The affected version or commit.
- Deployment mode (REST, MCP, or Docker).
- Steps to reproduce, and the impact you believe it has.

You should receive an initial response within a few days. We'll keep you
updated as the report is triaged and fixed, and credit you in the advisory
unless you ask otherwise.

## Scope

In scope:

- The REST API (`successfactors_toolkit.main:app`) and its routers, services,
  and models.
- The MCP server (`successfactors-mcp`).
- The systems store (`SYSTEMS_DIR`: system files, keys and certificates) and per-request connection overrides.
- The included `Dockerfile` and `docker-compose.yml`.

Out of scope:

- SAP SuccessFactors itself, or any instance's configuration of it.
- Issues that require an already-compromised `API_KEY`, `ADMIN_API_KEY`, or
  host environment.

## What `API_KEY` grants

`API_KEY` is a server-wide credential, not a per-system one. A caller that holds
it can query every system under `SYSTEMS_DIR` through the REST data routes
(`/api/odata/*`, `/api/sfapi/*`) by naming a `system`, and sees whatever
the system's configured SF technical user can see. The per-request `connection`
object is bounded as follows:

- `private_key_path` must resolve inside the directory of the system the
  request names (`SYSTEMS_DIR/<system>/`); a key of another system, or
  any other file, is rejected.
- `user_id` and `client_key` must equal what the operator configured in that
  system's `<system>.json`. A request can supply them only where nothing is
  configured.
- `host` and `token_url` must pass the host allowlist: SAP datacenter domains
  plus `SF_ALLOWED_HOSTS`. The same check applies to the values in the system
  file.

To keep a system's data away from holders of `API_KEY`, do not put it on a
shared server. `API_KEY` does not apply to the MCP server, which accepts only a
`system` name. The system-management routes (`/api/systems/*`) need
`ADMIN_API_KEY` as well.

## Audit log

Security-relevant events are written to stderr (never stdout, which carries
the MCP protocol) as one `key=value` line each, from the logger
`successfactors_toolkit.audit`, e.g.
`2026-10-01T09:30:00Z WARNING audit event=auth outcome=denied scope=admin reason=invalid`.
`docker logs` shows them. To route them elsewhere, give that logger its own
handler in your logging config (for uvicorn, `--log-config`).

| `event` | When |
| --- | --- |
| `key_install`, `key_delete` | A system's keypair is installed/replaced (`force`) or deleted |
| `production_flag` | A system's `production` flag is set (`previous` is `unset`, `true` or `false`) |
| `connection_override` | A request's `connection` values are accepted (`fields` names them) or rejected by the policy (`field`), with the `system` the request targeted |
| `connection_config` | A `host` or `token_url` from the system's `<name>.json` fails the policy (`field`), so every request to that system is refused; fix the configuration. MCP mode has no per-request overrides, so it only logs this |
| `auth` | `X-API-Key` or `X-Admin-Key` is missing, wrong or the API is disabled (`scope`, `reason`) |
| `sf_token` | SuccessFactors refuses the OAuth token request (`status`) |

`outcome` is `ok`, `denied` or `failed`; only `ok` lines are `INFO`. Lines carry
identifiers and outcomes only: a system name, field names, error codes and
status codes — never keys, certificates, tokens, `connection` values, request
bodies or employee data. A request's source address is not recorded; use the
uvicorn access log, which shares timestamps with these lines. Edits to
`<name>.json` made on disk are not audited.

## Deployment Guidance

This service brokers OAuth2 credentials and returns HR data (employee
records via Compound Employee and OData). Treat it accordingly:

- **Prefer local deployment for sensitive HR data.** Use local Docker and a
  local AI agent rather than online AI platforms or third-party hosted MCP
  services. A local agent may still call a cloud model: prompts, tool responses,
  previews, and files it supplies can leave the machine. Use a locally hosted
  model and local file-processing tools when HR data must remain within your
  controlled environment. The toolkit still contacts your configured SuccessFactors instance.
- **Bind to localhost or a private network.** The default Docker Compose
  file binds `127.0.0.1:8000`; don't expose the container port more widely
  without a reverse proxy in front of it.
- **Set REST access keys when using the REST API.** The REST API is
  fail-closed: every `/api/*` route returns `503` while `API_KEY` is unset,
  and all system-management routes (including list and get) return
  `503` while `ADMIN_API_KEY` is unset. Leaving either unset is not a safe
  default to rely on in production — set strong, independent random values.
  These access keys do not apply to local stdio MCP; it uses your SF credentials
  directly.
- **Run behind TLS.** `X-API-Key`, `X-Admin-Key`, and any per-request
  `connection` credentials travel in plain headers/JSON; terminate TLS in
  front of the service (reverse proxy or load balancer) rather than serving
  plaintext HTTP beyond localhost.
- **Keep private keys out of the image and out of Git.** Put each key in its
  system's directory (`private-key.pem`, mode 600) or install it with the
  keypair API, as described in [Connect to SuccessFactors](docs/CONNECT.md#2-create-the-system);
  never bake a key into a committed file. Under Docker, mount `SYSTEMS_DIR`
  read-only for MCP.
- **Restrict `SYSTEMS_DIR` and `RESULTS_DIR`** to storage only the
  service account can read — the former holds private keys and OAuth client
  keys, the latter can hold extracted employee payloads.
- **Production systems are tier 3.** Declare `"production": true` in a
  production system's file; its PII tokenization then cannot be lowered. Use
  `SF_ALLOWED_HOSTS` only for hosts you control.
</content>
