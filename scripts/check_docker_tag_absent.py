#!/usr/bin/env python3
"""Refuse Docker Hub publication unless an authenticated tag lookup is absent."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

TOKEN_URL = "https://auth.docker.io/token"
REGISTRY_URL = "https://registry-1.docker.io/v2"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class DockerHubError(RuntimeError):
    """Docker Hub did not prove that the requested tag is absent."""


RequestFn = Callable[[str, dict[str, str]], tuple[int, bytes]]


def request(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """Return HTTP responses and fail closed for transport failures."""
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:  # noqa: S310
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except OSError as error:
        raise DockerHubError(f"Docker Hub request failed: {error}") from error


def ensure_tag_absent(
    repository: str,
    tag: str,
    username: str,
    token: str,
    transport: RequestFn = request,
) -> None:
    """Return only for Docker's authenticated MANIFEST_UNKNOWN response."""
    scope = f"repository:{repository}:pull"
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    token_status, token_body = transport(
        f"{TOKEN_URL}?{urlencode({'service': 'registry.docker.io', 'scope': scope})}",
        {"Authorization": f"Basic {auth}"},
    )
    if token_status != 200:
        raise DockerHubError(f"Docker Hub token request returned HTTP {token_status}")

    try:
        bearer_token = json.loads(token_body)["token"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DockerHubError("Docker Hub token response was invalid") from error
    if not isinstance(bearer_token, str) or not bearer_token:
        raise DockerHubError("Docker Hub token response was invalid")

    manifest_url = f"{REGISTRY_URL}/{quote(repository, safe='/')}/manifests/{quote(tag, safe='')}"
    status, body = transport(
        manifest_url,
        {"Accept": MANIFEST_ACCEPT, "Authorization": f"Bearer {bearer_token}"},
    )
    if status == 200:
        raise DockerHubError(f"Docker Hub tag already exists: {repository}:{tag}")
    if status == 404 and _is_manifest_unknown(body):
        return
    raise DockerHubError(f"Docker Hub manifest lookup returned HTTP {status}")


def _is_manifest_unknown(body: bytes) -> bool:
    try:
        errors = json.loads(body)["errors"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(errors, list)
        and len(errors) == 1
        and isinstance(errors[0], dict)
        and errors[0].get("code") == "MANIFEST_UNKNOWN"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("tag")
    args = parser.parse_args(argv)

    username = os.environ.get("DOCKERHUB_USERNAME")
    token = os.environ.get("DOCKERHUB_TOKEN")
    if not username or not token:
        print("Refusing to publish: Docker Hub credentials are unavailable.", file=sys.stderr)
        return 2

    try:
        ensure_tag_absent(args.repository, args.tag, username, token)
    except DockerHubError as error:
        print(f"Refusing to publish: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
