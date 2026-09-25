# Changelog

Notable changes are recorded here. Version identifiers follow Semantic Versioning.

## [Unreleased]

### Changed (breaking)

- `odata_query` and `ce_query` refuse a tenant that has not declared whether
  it is production, returning `tenant_environment_unset` before anything is
  sent to SuccessFactors. Declare it in
  `{TENANT_KEYS_DIR}/{company_id}/tenant.json` as `{"production": true}` or
  `{"production": false}`; the default tenant reads `SF_COMPANY_ID`'s file.
- Production tenants are always PII tier 3. `PII_FILTER_TIER` now sets the
  tier for test tenants only (default still 1).
- `PII_VAULT_DIR` inside `RESULTS_DIR` is rejected at `PII_FILTER_TIER=0` too.
- The MCP server always keeps httpx request logging at WARNING.

### Added

- `PUT /api/tenants/{company_id}/environment` with `{"production": bool}`
  sets the flag for a registered tenant. The tenant list and get responses
  carry `production`, and replacing a key with `?force=true` keeps it.
- `list_tenants` reports `production` and the effective `pii_filter_tier`
  for each tenant and the default, and warns about tenants without a flag.
- Plugins: `pii_request` takes `company_id=""`. Pass the tool's `company_id`
  so the right tenant's flag applies; without it the default tenant's is used.

## [0.3.2] - 2026-09-25

### Fixed

- `odata_query` no longer adds `paging=snapshot` to a multi-page read that
  has `$top` or `$skip`. SuccessFactors rejects the combination
  (`COE_SNAPSHOT_BAD_REQUEST`), so such a query failed with HTTP 400.

### Security

- Login names are now tier 2 PII: `User.username` in OData and
  `person.logon_user_name` in Compound Employee. Tenants often set them to
  the work email.
- The Docker guide explains that a `pii_vault_unavailable` "owned by another
  user" error means the vault is a mounted host folder, and points to the
  named volume.

## [0.3.1] - 2026-09-24

### Changed

- README split into topic pages under `docs/`.
- `successfactors-pii-reveal` writes to stdout unless `-o FILE` is given (it
  used to write `<name>.revealed<ext>` next to the input).

### Security

- Released `docker-compose.mcp.yml` and the user guide now reference the
  release's own tag (a repository check enforces this); the image digest is
  added after publishing.
- Plugin registration is rolled back in full when a plugin fails, so a
  plugin that removed or replaced a core tool and then raised no longer
  leaves the core tool missing or replaced.
- `successfactors-pii-reveal` refuses a symlinked or non-regular output
  file.
- The PII vault refuses a key, database or vault directory it doesn't own,
  refuses a key or database that isn't a regular file, and tightens loose
  permissions on a vault directory it does own.
- Retokenizing an error body also catches percent-encoded and HTML-escaped
  echoes of PII plaintext, not just the raw and doubled-quote forms.
- Nested `$expand` `__next` pagination links are dropped from tokenized
  records, since they can carry key values.

## [0.3.0] - 2026-09-24

### Added

- MCP plugin interface: entry-point group `successfactors_toolkit.plugins`,
  the `plugin_api` import surface, and a `plugins` block in `list_tenants`.

### Security

- OData paths whose segment is `..` or `.` followed by `;parameters` (e.g.
  `..;/`) are now rejected, since some servers drop `;parameters` before
  resolving dot segments.

## [0.2.3] - 2026-09-24

### Security

- PII tokenization for MCP tool results. Before `odata_query` and `ce_query`
  output reaches the AI, whether as a saved file, an inline preview or an
  error body, PII fields are replaced by stable tokens such as
  `[PII-T1-3f9a1c2b7d10e4a5]`. The same value always gets the same token, so
  the model can still compare, group and count. Plaintext stays in a local
  vault (`PII_VAULT_DIR`, default `./pii_vault`), which must not be inside
  `RESULTS_DIR`.
- **Behavior change:** tokenization is on by default at tier 1. National IDs,
  work-permit and personal-document numbers, bank account numbers and IBANs,
  and passwords come back as tokens. Attachment and document content comes
  back as `[PII-T1-REDACTED]`. Set `PII_FILTER_TIER=0` to get the previous
  plaintext results.
- `PII_FILTER_TIER` (0–3) sets how far tokenization goes. Tier 2 adds birth
  dates, home address, contact data on Per* entities (all emails and
  phones), nationality, ethnicity, disability and veteran status. Tier 3
  adds names, gender, marital status, photos and User-entity contact fields
  (email, business phone, cell phone). `PII_EXTRA_FIELDS` maps
  tenant-specific fields to a tier, e.g. `{"PerPersonal": {"customString6": 2}}`.
- A token passed back in an `odata_query` `$filter` or path is resolved to
  its plaintext before the request is sent, so filtering on a national ID
  still works. If SuccessFactors echoes that value in an error, it is
  replaced by the token again. An unknown token is refused with
  `pii_unknown_token` and nothing is sent.
- New results and errors:
  - Tool results carry `pii_filter_tier`, `pii_tokenized` and `pii_note`.
  - `pii_vault_unavailable` means the vault can't be opened.
  - `pii_tokenize_failed` means a page couldn't be tokenized, so it is
    withheld instead of being returned in plaintext.
- New local command `successfactors-pii-reveal <file> -o <path>` restores
  plaintext in a report the AI wrote. It is not an MCP tool. Write its output
  outside the AI's workspace.
- Docker: the image creates `/vault`, and `docker-compose.mcp.yml` sets
  `PII_VAULT_DIR=/vault/store` on a named volume `sf-toolkit-pii-vault`. If
  you run your own compose file, add the same volume. Otherwise `odata_query`
  and `ce_query` return `pii_vault_unavailable` at the default tier.
- With tokenization on, OData results no longer include `__metadata.uri`,
  `__deferred.uri` or media links, because those URIs repeat key values such
  as work-permit numbers. HTTP request-URL logging is also turned down so
  resolved plaintext doesn't reach the server log.
- This protects the normal tool flow. It does not stop an agent that can read
  the vault directly. The README lists deny rules for local installs.

## [0.2.2] - 2026-09-23

### Fixed

- MCP server `instructions` now say that "active" employees usually include
  paid and unpaid leave, not only status A: resolve the `emplStatus` picklist
  and state which statuses were counted. Without this, runs of the same
  question disagreed on whether employees on leave were in scope. The
  `params`/`$select` reminder was dropped (the `odata_query` description
  already covers it) to stay under the 2048-character limit.

## [0.2.1] - 2026-09-23

### Added

- `odata_metadata` now lists an entity's navigation properties (name, target
  entity type, filterable) alongside its fields. Entity-scoped `$metadata`
  doesn't carry `NavigationProperty` elements, so these are resolved from the
  full service `$metadata` instead, fetched once per `company_id` per process
  and cached; a lookup failure is reported as a warning and never blocks the
  existing field output. The written JSON file now has the shape
  `{"fields": ..., "navigation": [...]}`; the inline result caps the
  navigation list at 50 entries with a note when more exist.
- `odata_query` now adds `paging=snapshot` automatically whenever `max_pages`
  > 1 and the caller hasn't passed `paging` (in `params` or `path`), reported
  as `paging_added`. Server-side snapshot paging pages 1000 rows at a time
  via `$skiptoken` with no duplicate keys, and is SAP's recommended
  alternative to client-side `$skip` paging. If SF answers with an HTTP 400
  whose body says paging/pagination isn't supported for that entity (e.g.
  `PerPersonRelationship`), the query retries once without it and adds a
  warning — mirroring the existing auto-`$orderby` fallback.

### Changed

- MCP server `instructions` rewritten as an ordered query workflow: decide
  the population filter (usually on EmpJob, with employment status explicit),
  list the needed entities, push the filter into each via navigation in
  `$filter` (standard paths listed), and only pull an entity in full when no
  path exists. Also: resolve codes in bulk, and check national IDs by
  `cardType`/`country` without selecting the ID values. Two more `Also:`
  bullets cover multi-value filters (`field in 'a','b'`, no parentheses;
  chunk long lists) and that `User` returns active users only by default.
- `extract_by_filter_in` now builds `column in 'v1','v2',...` (no
  parentheses) instead of an `eq A or eq B` chain — confirmed working on
  EmpJob, BenefitEnrollment and nav paths; the old comment claiming `in`
  isn't accepted was only true of the parenthesised `in (...)` form. Chunking
  now also splits before the URL-encoded `$filter` would exceed ~1800 chars,
  on top of the existing 1000-value-per-chunk cap (SAP KBA 2576271 cites a
  ~2KB GET URL limit).
- SAP rate limiting has been observed returning a 300s `Retry-After`, so the
  cap on honoring it (`_MAX_RETRY_AFTER_SECONDS`) is raised from 60s to 300s.
  Long-running queries can take minutes (SAP KBA 2735876), so the default
  `REQUEST_TIMEOUT` is raised from 30s to 120s.

### Fixed

- Server instructions and the `odata_query` description are now under 2048
  characters. Claude Code truncates at that length, so the query guidance
  was partly cut off before reaching the model. A test enforces the limit.

## [0.2.0] - 2026-09-23

### Fixed

- OData query options embedded in `odata_query`'s `path` (e.g.
  `"EmpJob?$filter=...&$select=..."`) were silently dropped whenever `params`
  was also passed. They are now parsed out of `path` and merged into the
  request, with explicit `params` winning on conflicts — fixed at the root in
  `ODataClient.request()`, so every caller benefits.
- `odata_query` paged pulls without an explicit `$orderby` could return
  duplicated and skipped rows. When a pull may span multiple pages and no
  `$orderby` is given, one is now derived automatically from the entity's
  `$metadata` key properties (cached per entity in-process, skipping any key
  marked `sap:sortable="false"` since SF rejects `$orderby` on those) and
  reported as `orderby_added`; if the key can't be determined or used, the
  query still runs but a warning is added. If SF rejects the auto-added
  `$orderby` outright (e.g. stale metadata), the query is retried once
  without it and a warning notes ordering isn't guaranteed. Duplicate records
  are counted and surfaced as `duplicate_records` with a warning, but only
  when every key property is actually present in the returned records — an
  incomplete `$select` (or unusable key) skips duplicate counting entirely
  rather than risk false positives.
- `odata_query` now warns when a page comes back sized exactly to `$top` with
  no `__next` link — some MDF/custom entities have been observed to stop
  paging silently even though more data exists. The warning suggests a manual
  `$skip` resume with an explicit `$orderby`; behavior is unchanged.

### Added

- MCP server `instructions` and the `odata_query`/`ce_query` tool docstrings
  now include general SuccessFactors querying guidance: how `path`/`params`
  combine, effective-dating, resolving picklist/foreign-key codes via
  `$expand` or `PicklistOption`/`PickListValueV2` instead of N+1 calls, common
  EC join keys (`userId`, `personIdExternal`, `PerPersonRelationship`,
  `PerNationalId`), the CompoundEmployee SFQL single-condition limit on
  `last_modified_on`, and `isNotFirstQuery` delta-filter semantics.

## [0.1.2] - 2026-09-20

### Changed

- Reframe onboarding for functional consultants and business key users, with
  business prompts, clear success checks, troubleshooting, and expandable
  administrator setup instructions.

- Make Docker Compose the MCP onboarding path in the README and bilingual
  HTML guide, with a pinned image, read-only credentials, and local exports.
  Remove standalone image download and local-build options from the guide.
- Remove GitHub CLI login and manual provenance verification from user
  onboarding; retain pinned image digests and release-time verification.

## [0.1.1] - 2026-09-20

### Fixed

- Allow extra MCP initialization time during emulated multi-platform release
  checks while preserving the native test timeout and all assertions.

### Added

- Docker Hub release workflow for versioned `wudaoyou/successfactors-toolkit`
  images on amd64 and arm64, with MCP startup checks before publication.
- Docker MCP setup guide and offline HTML with English/Chinese language selection.

### Changed

- Promote the release-candidate series to the first stable release.
- Make versioned Docker Hub downloads the user setup path; retain local image
  builds as a developer fallback.
- Use generic AI agent configuration in the README, clarify REST versus MCP
  startup, and document credential folders, local data exports, format
  conversion, and custom output folders.
- Recommend local deployment for sensitive HR data and explain that local AI
  agents can still send content to cloud models.

### Security

- Limit explicitly requested inline MCP OData previews to 20 records and 16 KiB
  serialized UTF-8; reject larger record counts before querying and return a
  `preview_error` that directs callers to the saved file when the byte limit is
  exceeded. The default remains zero.
- Protect versioned Docker images against overwrite, publish their fixed
  digest and signed build provenance, and document verification before
  mounting credentials.

## [0.1.0] - 2026-09-20

The release tag was created, but publication stopped at the multi-platform
MCP initialization timeout before any image was pushed or GitHub Release
created. The tag is retained unchanged; 0.1.1 supersedes this attempt.

## [0.1.0-rc.2] - 2026-09-17

### Security

- Restrict authenticated OData requests to the configured `/odata/v2` or
  `/odata/v4` API root.
- Reject request bodies larger than 10 MiB before FastAPI parses JSON or
  multipart content.

### Changed

- Dependency updates: cryptography 50.0.1 (fixes a high-severity advisory
  affecting 44.x–49.x), signxml 5.1.0, fastapi 0.141.1, uvicorn 0.53.0,
  pydantic-settings 2.15.0, lxml 6.1.3, python-dotenv 1.2.3,
  python-multipart 0.0.32, ruff 0.16.7.
- Dependabot now groups GitHub Actions bumps and pip minor/patch bumps into
  single weekly pull requests.

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

[Unreleased]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/wudaoyou/successfactors-toolkit/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.2.0
[0.1.2]: https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.1.2
[0.1.1]: https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.1.1
[0.1.0]: https://github.com/wudaoyou/successfactors-toolkit/tree/v0.1.0
[0.1.0-rc.2]: https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.1.0-rc.2
[0.1.0-rc.1]: https://github.com/wudaoyou/successfactors-toolkit/releases/tag/v0.1.0-rc.1
