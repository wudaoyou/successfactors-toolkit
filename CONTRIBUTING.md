# Contributing

## Branches and pull requests

Use two long-lived branches: `develop` for integration and `main` for releases.
Keep both branches passing CI and runnable. The repository default branch is
`develop`.

Create short-lived branches from current `develop`:

```sh
git switch develop
git pull --ff-only
git switch -c feature/short-description
```

Use `feature/`, `fix/`, or `docs/` for branch names. Open a draft PR early for
work in progress. Do not push normal development directly to `main` or
`develop`.

PRs must pass the required `repository-checks` status and resolve review
conversations before merging. Contributions from outside the maintainer team are
reviewed by a maintainer; new commits dismiss stale approval. `main` and `develop`
cannot be pushed to directly, force-pushed, or deleted.

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

Before opening a PR, run:

```sh
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python3 scripts/check_repository.py
```

Each implementation PR must add or adapt focused tests for the behavior it
introduces. Tests must run without live tenant credentials or network access
to SuccessFactors.

Keep changes focused. Include the problem, resulting behavior, verification,
and any compatibility implications in the PR description.

## Versions and releases

`VERSION` is the single version source, using SemVer with `v`-prefixed Git
tags. During `0.x`, a preparation PR to `develop` bumps it and moves the
relevant `CHANGELOG.md` entries from Unreleased before a release PR goes to
`main`. See [docs/RELEASING.md](docs/RELEASING.md) for the full versioning
rules and release procedure.

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
