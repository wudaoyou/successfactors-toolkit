# SuccessFactors Toolkit

A self-hosted toolkit for SAP SuccessFactors API troubleshooting, payload extraction,
and integration development, with REST and Model Context Protocol (MCP) interfaces.

**Status: repository bootstrap.** The development workflow is configured; the
application has not been migrated yet. There is no installable release.

## Planned capabilities

- Compound Employee queries, employee lookup, and paginated extraction.
- OData queries, extraction, and explicit create, update, and delete operations.
- OData metadata inspection and comparison.
- Multi-tenant connections using OAuth2 SAML Bearer authentication.
- MCP tools backed by the same services as the REST API.

MCP is a core product interface. Local coding-assistant configuration and internal
agent instructions are excluded from the distributed project.

## Architecture

REST routes and MCP tools will call shared services for authentication,
SFAPI, OData, metadata, and tenant management. Runtime code will be introduced in
reviewable migration PRs; this repository currently contains documentation and
repository checks only.

See the [migration plan](docs/migration-plan.md) for scope and acceptance criteria.

## Development

Git and Python 3.12 or newer are sufficient for the current repository checks:

```sh
git clone https://github.com/wudaoyou/successfactors-toolkit.git
cd successfactors-toolkit
python3 scripts/check_repository.py
```

Contribute through a short-lived branch and pull request to `develop`.
`main` holds release-ready commits; `develop` is the integration branch.
See [CONTRIBUTING.md](CONTRIBUTING.md) for branch, review, commit, and version rules.

Application installation, tenant setup, REST examples, and MCP client setup will
be documented alongside the implementation. Do not treat the planned capabilities
above as already available.

## Data handling

Use synthetic examples and test fixtures. Credentials, certificates, tenant
exports, employee payloads, and generated results do not belong in Git.
Repository hygiene checks are a basic guardrail, not a complete secret or
personal-data scanner.

## License

[Apache License 2.0](LICENSE). Copyright 2026 Justin Gong.

This is an independent project, not an official SAP product. Migration of existing
code must preserve applicable copyright and third-party license notices.
