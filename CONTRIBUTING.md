# Contributing

## Branches and pull requests

Use two long-lived branches: `develop` for integration and `main` for releases.
Keep both branches passing CI; after application migration, both must also remain
runnable. The repository default branch is `develop`.

Create short-lived branches from current `develop`:

```sh
git switch develop
git pull --ff-only
git switch -c feature/short-description
```

Use `feature/`, `fix/`, or `docs/` for human-created branches. Automated development
uses `codex/`. Open a draft PR early for work in progress. Do not push normal
development directly to `main` or `develop`.

PRs must pass the required `repository-checks` status, resolve review conversations,
and receive one independent approving review before merging. New commits dismiss
stale approval. Repository administrators retain an explicit bootstrap or recovery
bypass; using it is exceptional and must be explained in the PR. Authors cannot
satisfy their own approval requirement.

| Source | Target | Merge method |
| --- | --- | --- |
| Short-lived feature, fix, or docs branch | `develop` | Squash |
| `develop` release PR | `main` | Merge commit |
| `hotfix/description` from `main` | `main` | Squash |
| `main` after a release or hotfix | `develop` | Merge commit |

Merge commits between the long-lived branches preserve their shared history.
Delete merged short-lived branches manually; never delete `main` or `develop`.
Automatic branch deletion is disabled because release PRs use `develop` as their
source. Do not force-push either long-lived branch. After release, merge `main`
back into `develop` before starting the next development cycle.

## Commit and PR titles

Use Conventional Commits for PR titles and squash commits:

- `feat: add an OData query endpoint`
- `fix(mcp): handle pagination errors`
- `docs: explain tenant setup`
- `feat!: change the connection request schema`

Other accepted types: `build`, `chore`, `ci`, `perf`, `refactor`, `revert`,
`style`, and `test`. Describe breaking changes in the PR body and changelog.
Avoid employee identifiers, tenant names, and credentials in commits or PRs.

## Verification

Run `python3 scripts/check_repository.py` before opening a PR. CI currently
validates repository hygiene, version syntax, and PR title conventions.
It does not yet validate application behavior because application code is not
present.

Each implementation PR must add or adapt focused tests for the behavior it
introduces. Before the first application merge, add reproducible dependencies,
Python lint/format checks, REST/MCP tests, and container smoke checks to the
required CI job. Tests must run without live tenant credentials or network access
to SuccessFactors.

Keep changes focused. Include the problem, resulting behavior, verification,
and any compatibility implications in the PR description.

## Versions and releases

`VERSION` is the single version source. Use SemVer, and prefix Git tags with `v`.
The current `0.1.0-dev.0` marks unreleased bootstrap work; it is not a release tag.

- During `0.x`, use a minor bump for new capabilities or breaking API changes,
  and a patch bump for compatible fixes. Document breaking changes explicitly.
- From `1.0.0`, breaking public API changes require a major bump; compatible
  features use a minor bump; compatible fixes use a patch bump.
- REST contracts, MCP tool names/arguments/results, and supported configuration
  are part of the public API.
- Use `-rc.1`, `-rc.2`, etc. for release candidates.
- Do not bump a version per commit or per branch. A preparation PR to `develop` updates `VERSION`
  and moves relevant `CHANGELOG.md` entries from Unreleased to a dated version.
- Never move or overwrite a published tag. Fix a release with a new version.

The first intended application release is `0.1.0`, after migration acceptance.

Open a release PR from `develop` to `main` after the version and changelog
preparation is merged. After its checks and review pass, merge with a merge commit.
A maintainer then releases that approved commit on `main`:

```sh
git switch main
git pull --ff-only
python3 scripts/check_repository.py
version="$(cat VERSION)"
git tag -a "v$version" -m "Release $version"
git push origin "v$version"
```

The release workflow verifies version/tag agreement, membership in `main`,
repository checks, and that application code exists before creating a GitHub
Release. Versions with a hyphen are marked prerelease. Runtime package and
container publication are deferred until their builds are implemented.

## Licensing and privacy

Submit only code you have authority to contribute. Contributions are licensed
under Apache-2.0 unless an applicable existing license requires otherwise.
Retain upstream copyright and license notices for migrated or third-party code.

Use synthetic test fixtures. Never commit real employee data, credentials,
certificates, environment files, customer exports, or local assistant settings.

## References

- [GitHub pull requests](https://docs.github.com/en/pull-requests)
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
- [Semantic Versioning](https://semver.org/)
