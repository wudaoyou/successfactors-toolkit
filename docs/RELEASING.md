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

1. Open a preparation PR against `develop` that updates `VERSION`, moves
   the relevant `CHANGELOG.md` entries from `Unreleased` to a new dated
   section, and sets every `docker.io/wudaoyou/successfactors-toolkit` image
   reference (`docker-compose.mcp.yml`, `docs/DOCKER_MCP_GUIDE.md`,
   `docs/DOCKER_MCP_GUIDE.html`) to `:v<new version>` with no digest — the
   digest isn't known until the image for this tag is published, and since
   release tags are immutable (see below) the tag alone already points at
   the right image once it exists.
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
   packages, tests and publishes the Docker image, signs and verifies its
   provenance, then creates a GitHub Release only after those steps succeed.
   Versions with a hyphen (e.g. `-rc.1`) are marked prerelease.
6. After a release or hotfix, merge `main` back into `develop` before
   starting the next development cycle. As part of that sync, add the
   `@sha256:...` digest from the release's `image-reference.txt` asset to
   the same three image references so `develop` pins the exact published
   image.

## Docker Hub publication

The release job publishes
`wudaoyou/successfactors-toolkit:<release-tag>` (including the `v` prefix)
for `linux/amd64` and `linux/arm64`. It builds and loads both platforms once,
then checks MCP initialization, tool discovery, and a local tenant-list call
on each platform using the existing stdio test. The arm64 check uses QEMU.
These smoke tests make no live SuccessFactors requests. The workflow pushes
the same tested images without rebuilding, verifies both published platforms,
captures the published manifest digest, and only then creates the GitHub
Release with the Python distributions.

The containerd image store enables loading both platforms, following
[Docker's multi-platform GitHub Actions guidance](https://docs.docker.com/build/ci/github-actions/multi-platform/).

Before the first image release:

1. Create a **public** Docker Hub repository named
   `wudaoyou/successfactors-toolkit` so users can pull it without signing in.
2. Create a Docker Hub access token with the read/write access needed to push
   that repository. In GitHub repository **Settings → Secrets and variables →
   Actions**, save it as `DOCKERHUB_TOKEN`. Never put the token in chat or Git.
3. In Docker Hub **Repository Settings**, select **Specific tags immutable**
   with the expression `^v.*$`. This repository is configured that way, so all
   release tags are immutable. Keep the setting in place before publishing.
4. Merge the workflow through the normal development and release process,
   then publish a new approved release tag. Do not move an existing Git tag.

Only the exact release tag is published; release candidates do not become
`latest`. Before the push, the workflow authenticates to Docker Hub and looks
up the tag manifest. Only Docker's authenticated `MANIFEST_UNKNOWN` response
allows the push; an existing tag, missing repository, authentication failure,
or network/error response stops the job. This is not a tag reservation, so the
Docker Hub immutability setting remains the final overwrite protection.

After the push, the workflow records the actual multi-platform manifest digest
as `docker.io/wudaoyou/successfactors-toolkit@sha256:...` in the
`image-reference.txt` release asset and in the generated release notes. It then
creates a signed GitHub SLSA provenance attestation with the official pinned
[`actions/attest`](https://github.com/actions/attest) action and verifies it
against this repository and `.github/workflows/release.yml` before creating the
GitHub Release.

Python packages remain attached to the GitHub Release; publication to a Python
package registry is still deferred. Docker Hub and GitHub are not an atomic
transaction. If the push succeeds but digest capture, signing, or signature
verification fails, the image is a partial publication and no GitHub Release is
created. Do not rerun or overwrite that tag: recover the missing release only
from the approved commit and verified existing image, or prepare a new version.

After a successful push, verify the published tag:

```sh
# Copy the exact digest-pinned reference from the image-reference.txt release asset.
image="docker.io/wudaoyou/successfactors-toolkit@sha256:..."
docker login docker.io
gh attestation verify "oci://$image" --repo wudaoyou/successfactors-toolkit \
  --signer-workflow wudaoyou/successfactors-toolkit/.github/workflows/release.yml \
  --source-ref refs/tags/vX.Y.Z
docker buildx imagetools inspect "$image"
docker pull "$image"
```

Confirm that both architectures are present and exercise the documented MCP
configuration against the pulled image. Update the user guide with the
verified tag only after this succeeds.
Never rebuild and overwrite an already published version tag; use a new
version for corrections. If publication fails, inspect Docker Hub before any
recovery action to determine whether the tag was already pushed.
