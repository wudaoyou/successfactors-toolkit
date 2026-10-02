"""Both Compose files run the container locked down, with every write path mounted."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).parents[1]
# service name -> paths its settings say the app writes to (see config.py)
WRITE_VARS = {
    "docker-compose.yml": ("api", ["TENANT_KEYS_DIR"]),
    "docker-compose.mcp.yml": ("mcp", ["RESULTS_DIR", "PII_VAULT_DIR"]),
}


def load(name: str) -> dict:
    services = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]
    return services[WRITE_VARS[name][0]]


def mounts(service: dict) -> list[tuple[str, bool]]:
    """(container path, writable) for every volume and tmpfs of the service."""
    found = [(path, True) for path in service.get("tmpfs", [])]
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            _, target, *mode = volume.split(":")
            found.append((target, mode != ["ro"]))
        else:
            found.append((volume["target"], not volume.get("read_only", False)))
    return found


def writable(service: dict, path: str) -> bool:
    best = max(
        (m for m in mounts(service) if PurePosixPath(path).is_relative_to(m[0])),
        key=lambda m: len(m[0]),
        default=None,
    )
    return best is not None and best[1]


@pytest.mark.parametrize("name", WRITE_VARS)
def test_container_is_locked_down(name: str) -> None:
    service = load(name)
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert isinstance(service["pids_limit"], int) and service["pids_limit"] > 0
    # /tmp takes upload spooling and sqlite temp files.
    assert writable(service, "/tmp")


@pytest.mark.parametrize("name", WRITE_VARS)
def test_every_write_path_stays_writable(name: str) -> None:
    service = load(name)
    for var in WRITE_VARS[name][1]:
        assert writable(service, service["environment"][var]), var


def test_mcp_tenant_keys_stay_read_only() -> None:
    service = load("docker-compose.mcp.yml")
    assert not writable(service, service["environment"]["TENANT_KEYS_DIR"])


def test_image_writes_no_bytecode() -> None:
    # Imports would otherwise try to write __pycache__ under the read-only /app.
    assert "PYTHONDONTWRITEBYTECODE=1" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
