# Changelog

Notable changes are recorded here. Version identifiers follow Semantic Versioning.

## [Unreleased]

### Security

- Restrict authenticated OData requests to the configured `/odata/v2` or
  `/odata/v4` API root.
- Reject request bodies larger than 10 MiB before FastAPI parses JSON or
  multipart content.

## [0.1.0-rc.1] - 2026-09-17

This release candidate contains the first migration of the REST API, MCP
server, tenant management, and scripts. It has been verified against mocked
SuccessFactors responses, not a live tenant.

### Added

- Packaging metadata: the project now installs as `successfactors-toolkit`
  via setuptools, with its version read from `VERSION` and a `dev` extra for
  running tests and lint checks.
- A `successfactors-mcp` console script that starts the MCP stdio server.
- A `RESULTS_DIR` setting (default `results`) controlling where downloaded
  payloads are written.
- An `SF_ALLOWED_HOSTS` setting: a list of additional hosts a per-request
  connection override may target, alongside the default host and the SAP
  datacenter domains.
- Connection policy rejections now return a clear 400 response with the
  reason instead of an unhandled error.
- `SECURITY.md` with a support policy, private vulnerability-reporting
  instructions, and deployment guidance (localhost, TLS, dual-key setups).
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- GitHub issue templates for bug reports and feature requests, with blank
  issues disabled and a link to the security policy.
- Release documentation covering the full version/tag/release procedure.
- CI now also builds the Docker image and smoke-tests that the container
  starts and its health check responds successfully.
- Releases now build and attach source/wheel distributions to the GitHub
  Release.
- Dependency updates now also track pip packages weekly, alongside GitHub
  Actions.

### Changed

- Renamed the application package from `app` to `successfactors_toolkit`.
- A connection override's private-key path must now point inside the
  configured tenant key store; symlinks that escape it are rejected.
- Rewrote the README into full user documentation covering the
  authentication model, environment variables, tenant management, API usage
  examples, and MCP client setup.
- Updated the contributor guide's verification steps and moved the release
  procedure into its own document.
- The project notice now names the origin project directly.
- Required CI checks now install the package and run lint, format, and test
  checks before the existing repository hygiene checks.

### Fixed

- MCP tools no longer crash when writing results; results are now created
  safely with restrictive file permissions.
- MCP query tools now surface the actual SuccessFactors error body (SOAP
  fault, OData error, metadata parser message) instead of a generic error or
  a bare status code.
- Login failures now include the start of the server's response to help
  diagnose the cause.
- A Compound Employee request with a missing timezone offset now returns a
  clear 400 error instead of crashing.
- The release workflow's version check now references the correct
  application entry point after the package rename.

### Security

- Per-request connection overrides (host, token URL, private key path) are
  now validated against an allowlist before use, closing an authenticated
  SSRF and local-file-read vulnerability.
- All tenant management endpoints, including reads, now require admin
  authentication (previously only writes did).

### Removed

- The MCP server no longer changes its working directory at startup; the
  output directory is now resolved from configuration instead of the
  installed file layout.
- Migration-era planning documents that are no longer needed now that the
  migration is complete.

[Unreleased]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.1.0-rc.1...HEAD
[0.1.0-rc.1]: https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.1.0-rc.1
