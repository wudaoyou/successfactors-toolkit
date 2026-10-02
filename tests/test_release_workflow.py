"""The release workflow keeps OIDC use and release writes in separate jobs."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = yaml.safe_load(
    (Path(__file__).parents[1] / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
)
JOBS = WORKFLOW["jobs"]


def writers(permission: str) -> list[str]:
    return [
        name for name, job in JOBS.items() if job.get("permissions", {}).get(permission) == "write"
    ]


def test_workflow_default_is_read_only() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_only_the_signing_and_registry_jobs_can_mint_an_oidc_token() -> None:
    assert writers("id-token") == ["attest", "registry"]
    # The signing job signs and nothing else: no shell steps run with the token.
    assert all("run" not in step for step in JOBS["attest"]["steps"])
    # The registry job holds no other write permission and runs a pinned binary.
    assert JOBS["registry"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert len(JOBS["registry"]["env"]["PUBLISHER_SHA256"]) == 64
    assert "sha256sum -c" in JOBS["registry"]["steps"][1]["run"]


def test_only_the_release_job_can_write_contents() -> None:
    assert writers("contents") == ["release"]


def test_jobs_run_in_order() -> None:
    assert JOBS["attest"]["needs"] == "publish"
    assert JOBS["publish"]["environment"] == "dockerhub"
    assert set(JOBS["release"]["needs"]) == {"publish", "attest"}
    # Listed only after the GitHub Release exists, and never for a release candidate.
    assert JOBS["registry"]["needs"] == "release"
    assert "contains(github.ref_name, '-')" in JOBS["registry"]["if"]
