# Releasing

`VERSION` is the single version source. Use SemVer, and prefix Git tags with
`v`.

- During `0.x`, use a minor bump for new capabilities or breaking API
  changes, and a patch bump for compatible fixes. Document breaking changes
  explicitly.
- From `1.0.0`, breaking public API changes require a major bump; compatible
  features use a minor bump; compatible fixes use a patch bump.
- REST contracts, MCP tool names/arguments/results, and supported
  configuration are part of the public API.
- Use `-rc.1`, `-rc.2`, etc. for release candidates.
- Do not bump a version per commit or per branch. A preparation PR to
  `develop` updates `VERSION` and moves the relevant `CHANGELOG.md` entries
  from Unreleased to a dated version.
- Never move or overwrite a published tag. Fix a release with a new version.

## Procedure

1. Open a preparation PR against `develop` that updates `VERSION` and moves
   the relevant `CHANGELOG.md` entries from `Unreleased` to a new dated
   section.
2. After that PR merges, open a release PR from `develop` to `main`.
3. After its checks and review pass, merge it with a merge commit.
4. A maintainer then tags the approved commit on `main`:

   ```sh
   git switch main
   git pull --ff-only
   python3 scripts/check_repository.py
   version="$(cat VERSION)"
   git tag -a "v$version" -m "Release $version"
   git push origin "v$version"
   ```

5. The release workflow verifies version/tag agreement, that the tag is on
   `main`, repository checks, and that application code exists, then creates
   a GitHub Release. Versions with a hyphen (e.g. `-rc.1`) are marked
   prerelease.
6. After a release or hotfix, merge `main` back into `develop` before
   starting the next development cycle.

Runtime package and container publication are deferred until their builds
are implemented.
</content>
