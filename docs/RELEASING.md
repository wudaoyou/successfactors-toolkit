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
   `main`, repository checks, and that application code exists. It builds Python
   packages, tests and publishes the Docker image, then creates a GitHub Release
   only after image publication succeeds. Versions with a hyphen (e.g. `-rc.1`)
   are marked prerelease.
6. After a release or hotfix, merge `main` back into `develop` before
   starting the next development cycle.

## Docker Hub publication

The release job publishes
`wudaoyou/successfactors-toolkit:<release-tag>` (including the `v` prefix)
for `linux/amd64` and `linux/arm64`. It builds and loads both platforms once,
then checks MCP initialization, tool discovery, and a local tenant-list call
on each platform using the existing stdio test. The arm64 check uses QEMU.
These smoke tests make no live SuccessFactors requests. The workflow pushes
the same tested images without rebuilding, verifies both published platforms,
and only then creates the GitHub Release with the Python distributions.

The containerd image store enables loading both platforms, following
[Docker's multi-platform GitHub Actions guidance](https://docs.docker.com/build/ci/github-actions/multi-platform/).

Before the first image release:

1. Create a **public** Docker Hub repository named
   `wudaoyou/successfactors-toolkit` so users can pull it without signing in.
2. Create a Docker Hub access token with the read/write access needed to push
   that repository. In GitHub repository **Settings → Secrets and variables →
   Actions**, save it as `DOCKERHUB_TOKEN`. Never put the token in chat or Git.
3. Merge the workflow through the normal development and release process,
   then publish a new approved release tag. Do not move an existing Git tag.

Only the exact release tag is published; release candidates do not become
`latest`. Python packages remain attached to the GitHub Release; publication
to a Python package registry is still deferred. A Docker failure prevents the
GitHub Release from being created. Docker Hub and GitHub are not an atomic
transaction: if GitHub Release creation fails after the push, recover only
the missing release from the approved commit and already built Python
artifacts; do not rebuild and overwrite the published image. If those Python
artifacts cannot be recovered, prepare a new version.

After a successful push, verify the published tag:

```sh
# Replace vX.Y.Z with the exact tag that completed publication.
docker buildx imagetools inspect wudaoyou/successfactors-toolkit:vX.Y.Z
docker pull wudaoyou/successfactors-toolkit:vX.Y.Z
```

Confirm that both architectures are present and exercise the documented MCP
configuration against the pulled image. Update the user guide with the
verified tag only after this succeeds.
Never rebuild and overwrite an already published version tag; use a new
version for corrections. If publication fails, inspect Docker Hub before
retrying to determine whether the tag was already pushed.
