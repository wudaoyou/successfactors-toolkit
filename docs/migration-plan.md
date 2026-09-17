# Migration plan

## Scope

Build a general-purpose SuccessFactors toolkit under `wudaoyou/successfactors-toolkit`.
Preserve REST functionality, including OData writes and deletes, and preserve MCP
as a core product interface. "Remove AI configuration" means excluding local
coding-assistant settings, internal instructions, and agent work records; it does
not mean removing MCP, its dependency, tests, or user-facing connection examples.

Keep the repository private during preparation. Public visibility is a separate
publication step after the candidate contents and publishing rights are checked.

## Migration method

Start from an explicit file allowlist and a new Git history. Keep the source
repository and its uncommitted work intact. Do not copy its Git directory,
environment files, key stores, certificates, generated payloads, customer-specific
integration packages, business mappings, internal documents, or release history.

Generic runtime code and tests require content review even when their paths are
allowlisted. Replace customer-specific constants, employee identifiers, endpoints,
and example values with synthetic equivalents.

Keep existing MIT notices for applicable migrated code. Review dependency and
source provenance before applying Apache-2.0 to the new distribution.

## Implementation sequence

1. **Repository foundation (this bootstrap).** Establish main, develop, feature branches,
   PR checks, version conventions, licensing, and this plan.
2. **Core migration.** Move generic models, authentication, SFAPI/OData clients,
   REST routes, MCP server, and applicable tests without changing contracts.
   Make configuration and helper scripts tenant-neutral.
3. **Shared services and access boundaries.** Isolate shared credential resolution
   and metadata logic where needed. Preserve both entry points. Make network
   binding, authentication, tenant isolation, and mutating operations explicit.
4. **Developer experience.** Add reproducible dependency management, linting,
   formatting, automated tests, container startup verification, and synthetic
   examples. Choose one dependency workflow and document it.
5. **Documentation and release.** Replace the bootstrap README with verified local
   and Docker setup, authentication, REST and MCP usage, write/delete examples,
   pagination/error semantics, limitations, and troubleshooting. Prepare 0.1.0
   only after acceptance.

## Acceptance

- REST and MCP import and start without any local coding-assistant configuration.
- MCP tools retain their documented functionality and use the shared clients.
- CE queries and pagination handle SOAP faults and incomplete results correctly.
- OData extraction and mutation tests cover request shape, errors, pagination,
  and retry behavior without replaying unsafe writes.
- Tenant operations, credentials, and exposed interfaces have tested boundaries.
- A fresh checkout can follow the README using only synthetic test data.
- Required CI includes runtime tests and container verification before an
  application release.
- Tracked files and candidate distribution artifacts contain no real customer
  data, credentials, tenant certificates, or internal assistant configuration.
- Public API changes and remaining live-SAP verification are documented.
- Applicable upstream license notices and publishing authority are confirmed.

Repository checks in this bootstrap are not a completed migration, security
audit, or proof of live SuccessFactors interoperability.
