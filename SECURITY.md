# Security Policy

## Supported Versions

Only the latest `0.1.x` release receives security fixes while the project is
pre-1.0.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |
| < 0.1   | No        |

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
- The tenant key/certificate store and per-request connection overrides.
- The included `Dockerfile` and `docker-compose.yml`.

Out of scope:

- SAP SuccessFactors itself, or any tenant's configuration of it.
- Issues that require an already-compromised `API_KEY`, `ADMIN_API_KEY`, or
  host environment.

## Deployment Guidance

This service brokers OAuth2 credentials and returns HR data (employee
records via Compound Employee and OData). Treat it accordingly:

- **Prefer local deployment for sensitive HR data.** Use local Docker and a
  local AI agent rather than online AI platforms or third-party hosted MCP
  services. A local agent may still call a cloud model: prompts, tool responses,
  previews, and files it supplies can leave the machine. Use a locally hosted
  model and local file-processing tools when HR data must remain within your
  controlled environment. The toolkit still contacts your configured SF tenant.
- **Bind to localhost or a private network.** The default Docker Compose
  file binds `127.0.0.1:8000`; don't expose the container port more widely
  without a reverse proxy in front of it.
- **Set REST access keys when using the REST API.** The REST API is
  fail-closed: every `/api/*` route returns `503` while `API_KEY` is unset,
  and all tenant-management routes (including list and get) return
  `503` while `ADMIN_API_KEY` is unset. Leaving either unset is not a safe
  default to rely on in production — set strong, independent random values.
  These access keys do not apply to local stdio MCP; it uses your SF credentials
  directly.
- **Run behind TLS.** `X-API-Key`, `X-Admin-Key`, and any per-request
  `connection` credentials travel in plain headers/JSON; terminate TLS in
  front of the service (reverse proxy or load balancer) rather than serving
  plaintext HTTP beyond localhost.
- **Keep private keys out of the image and out of Git.** Use the tenant
  keypair API or the `SF_PRIVATE_KEY_PEM*` / `SF_PRIVATE_KEY_PATH` env vars
  described in the README; never bake a key into a committed file.
- **Restrict `TENANT_KEYS_DIR` and `RESULTS_DIR`** to storage only the
  service account can read — the former holds private keys, the latter can
  hold extracted employee payloads.
</content>
