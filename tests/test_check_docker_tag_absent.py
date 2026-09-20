"""Fail-closed unit checks for the Docker Hub release preflight."""

from __future__ import annotations

import runpy
from collections.abc import Iterable
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(Path(__file__).parents[1] / "scripts" / "check_docker_tag_absent.py")
ensure_tag_absent = SCRIPT["ensure_tag_absent"]
DockerHubError = SCRIPT["DockerHubError"]

TOKEN = b'{"token": "test-token"}'
MANIFEST_UNKNOWN = b'{"errors": [{"code": "MANIFEST_UNKNOWN"}]}'


def transport(responses: Iterable[tuple[int, bytes]]):
    response_iter = iter(responses)

    def send(_url: str, _headers: dict[str, str]) -> tuple[int, bytes]:
        return next(response_iter)

    return send


def test_existing_tag_refuses_publication() -> None:
    with pytest.raises(DockerHubError, match="tag already exists"):
        ensure_tag_absent(
            "wudaoyou/successfactors-toolkit",
            "v1.2.3",
            "user",
            "token",
            transport([(200, TOKEN), (200, b"{}")]),
        )


def test_only_manifest_unknown_allows_publication() -> None:
    ensure_tag_absent(
        "wudaoyou/successfactors-toolkit",
        "v1.2.3",
        "user",
        "token",
        transport([(200, TOKEN), (404, MANIFEST_UNKNOWN)]),
    )


@pytest.mark.parametrize(
    "responses",
    (
        ((401, b"{}"),),
        ((200, TOKEN), (401, b"{}")),
        ((200, TOKEN), (404, b'{"errors": [1]}')),
        ((200, TOKEN), (404, b'{"errors": [{"code": "NAME_UNKNOWN"}]}')),
        ((200, TOKEN), (500, b"{}")),
    ),
)
def test_indeterminate_registry_results_refuse_publication(
    responses: tuple[tuple[int, bytes], ...],
) -> None:
    with pytest.raises(DockerHubError):
        ensure_tag_absent(
            "wudaoyou/successfactors-toolkit", "v1.2.3", "user", "token", transport(responses)
        )
