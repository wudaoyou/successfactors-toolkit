"""The release workflow keeps OIDC signing and release writes in separate jobs."""

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


def test_only_the_signing_job_can_mint_an_oidc_token() -> None:
    assert writers("id-token") == ["attest"]
    # It signs and nothing else: no shell steps run with the token.
    assert all("run" not in step for step in JOBS["attest"]["steps"])


def test_only_the_release_job_can_write_contents() -> None:
    assert writers("contents") == ["release"]


def test_jobs_run_in_order() -> None:
    assert JOBS["attest"]["needs"] == "publish"
    assert set(JOBS["release"]["needs"]) == {"publish", "attest"}
