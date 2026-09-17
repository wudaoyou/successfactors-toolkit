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

## Checklist and completion rules

Work through the numbered steps in order. After completing a step, update its
checkbox in this file in the same work branch and record the verification
command/result and relevant commit or PR. Do not wait until the end to update
progress. Check a step only after its stated acceptance conditions pass.

A checked implementation step means it is implemented, verified, and pushed on
its work branch. It does not mean merged, released, or publicly available:
steps 8, 9, and 10 track those outcomes separately. If verification fails, leave
the step unchecked, record the failure, and resolve it before dependent work.

Routine steps do not require a new approval between each one. Follow the existing
PR review requirement for merges; keep live tenant access, live mutations, and
public publication separate from offline implementation and testing.

**Current state:** step 1 is complete. Application migration has not started.
**Next action:** step 2, migration inventory and file allowlist.

### 1. Repository foundation

- [x] Create the local repository and synchronize it to the remote.
- [x] Establish `main` and `develop`, with `develop` as the default branch.
- [x] Configure PR review, required CI, and force-push/deletion protection.
- [x] Establish SemVer, Conventional Commit titles, and protected release tags.
- [x] Add Apache-2.0, a bootstrap README, and contribution guidance.

Evidence: bootstrap commit `007f69e21ddad76a4f4c586966bf8744673f7f00` is on both
branches. Repository hygiene checks and both GitHub CI runs passed:
[develop CI](https://github.com/wudaoyou/successfactors-toolkit/actions/runs/35180613225),
[main CI](https://github.com/wudaoyou/successfactors-toolkit/actions/runs/35180650796).
No application code or application release is included in this milestone.

### 2. Migration inventory and file allowlist

- [ ] Record the source revision and identify any intended uncommitted inputs.
- [ ] Classify generic runtime code, tests, scripts, dependencies, and Docker files.
- [ ] Write `docs/migration-manifest.md` with source-to-target paths and actions:
  retain, sanitize, or exclude. Record counts using the corrected MCP scope.
- [ ] Inventory notices needed for existing MIT code and third-party components.

Acceptance: every proposed migrated file has a disposition; real payloads,
customer integrations, local assistant configuration, and Git history are
excluded. Do not include sensitive contents or identifiers in the manifest.
Confirm that the source working tree has not changed as a result of this work.

### 3. Runnable core application

- [ ] Migrate `app/`, including models, authentication, clients, REST routes,
  tenant management, and the MCP server.
- [ ] Migrate applicable tests and dependencies, including the MCP dependency.
- [ ] Provide a neutral `.env.example` and runnable Docker configuration.
- [ ] Add reproducible dependency setup, lint/format checks, basic runtime tests,
  and container smoke checks to required CI before the first application merge.
- [ ] Replace customer defaults and examples with synthetic values.

Acceptance: install in a clean environment; REST startup, `/health`, OpenAPI
loading, and the migrated regression tests pass without real credentials.
The work branch visibly contains `app/`, `tests/`, dependency declarations,
`Dockerfile`, and `docker-compose.yml`. Preserve existing contracts in this step.

### 4. MCP functionality

- [ ] Verify stdio startup, MCP initialization, and tool discovery.
- [ ] Verify tenant listing, OData metadata/query, metadata comparison, and CE query.
- [ ] Verify file output, paging, and error behavior with synthetic responses.
- [ ] Confirm the server starts without local coding-assistant settings and
  that stdout remains valid MCP transport output.

Acceptance: an MCP client can initialize, list tools, and call representative
operations against mocked SuccessFactors responses. Keep MCP connection examples
in the product documentation. Do not claim new MCP write/delete tools unless
implemented and tested; existing OData mutation support remains available in REST.

### 5. Shared services and API boundaries

- [ ] Extract shared credential or metadata helpers only where current coupling
  requires it; preserve REST and MCP behavior.
- [ ] Make local binding, CORS, API access, and tenant-management permissions explicit.
- [ ] Verify CE queries, SOAP faults, pagination termination, and partial results.
- [ ] Verify OData reads, writes, deletes, pagination, and retry behavior.
- [ ] Verify credential resolution, tenant isolation, and unauthorized access rejection.

Acceptance: focused tests cover invalid input and failures, including preventing
unsafe mutation replays. Document intentional contract changes. Use mocks and
synthetic keys; do not issue real tenant writes or deletes as a test.

### 6. Scripts and user documentation

- [ ] Convert the employee downloader to accept arguments rather than a fixed ID.
- [ ] Generalize key-generation and output handling; ignore all generated artifacts.
- [ ] Replace the bootstrap README with verified installation and quick-start steps.
- [ ] Document authentication, tenant setup, REST and MCP usage, explicit OData
  write/delete examples, pagination, effective dates, limitations, and troubleshooting.
- [ ] Update the changelog and clearly state what is implemented and unverified.

Acceptance: follow the README from a fresh checkout; local and Docker startup
work, MCP setup works, and examples match actual interfaces. Use synthetic data
and document live SAP prerequisites separately. CLI tests cover invalid arguments,
SOAP faults, and output behavior without exposing payload contents in logs.

### 7. Candidate verification and publication review

- [ ] Run required lint, format, unit/integration, REST/MCP, and Docker checks.
- [ ] Inspect all tracked files and candidate distribution contents for customer
  data, credentials, certificates, assistant configuration, and stale references.
- [ ] Inspect dependencies and confirm applicable upstream notices are retained.
- [ ] Review the complete migration diff and resolve actionable findings.
- [ ] Record remaining live-SAP checks; do not represent mocks as live verification.

Acceptance: required CI is green on the candidate commit; the review record links
actual commands/results and identifies residual limitations. Runtime evidence,
privacy review, and license provenance must each be addressed; a keyword scan
alone is insufficient. Keep the repository private at this step.

### 8. Merge into develop and verify remote contents

- [ ] Open or update the migration PR targeting `develop` with evidence.
- [ ] Obtain the required independent approval and resolve review conversations.
- [ ] Merge after required CI passes, then synchronize local `develop`.
- [ ] Verify remote `develop` contains `app/`, tests, runtime dependencies, Docker
  files, and the completed README; record the merged commit and CI result.

Acceptance: the default branch shows the application files. Local and remote
`develop` agree and the working tree is clean. A pushed feature branch or an open
PR does not satisfy this step. Do not bypass branch protection for routine work.

### 9. First application release

- [ ] Prepare `VERSION` and dated changelog entries for `0.1.0` on `develop`.
- [ ] Merge an approved release PR from `develop` to `main` using a merge commit.
- [ ] Tag the verified release commit as `v0.1.0`; verify the release workflow.
- [ ] Verify GitHub Release contents and synchronize `main` back into `develop`.

Acceptance: tag, version file, changelog, and release commit agree; release CI
passes. Do not move a published tag. Do not claim package or container publication
unless those artifacts have actually been built and published.

### 10. Public open-source publication

- [ ] Confirm authority to publish the migrated code and complete release-content review.
- [ ] Obtain the owner's go-ahead for the concrete reviewed public-release candidate.
- [ ] Change repository visibility to public and verify anonymous access.
- [ ] Verify public README links, license recognition, release, and cloneability.

Acceptance: the reviewed repository and release are publicly accessible, and a
fresh clone can follow the documented setup. A private repository or private
GitHub Release does not satisfy this step.

## Evidence log

Add a concise entry whenever a numbered step completes:

| Step | Commit / PR | Verification and result | Merge / release state |
| --- | --- | --- | --- |
| 1 | `007f69e` | Repository checks passed; main/develop CI passed; remote refs verified | Bootstrap on both branches; private; no release |

For incomplete work, record the failed or pending check and the next concrete
action. Never check a box merely because a file was copied, a command was planned,
or a PR was opened.
