"""Private-key resolution shared by the SFAPI and OData clients."""

from pathlib import Path

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.connection_policy import (
    ConnectionPolicyError,
    check_key_path,
    deny,
)
from successfactors_toolkit.services.system_store import KEY_FILE, NAME_RE, exact_case_path


def load_key_pem(path_override: str | None, settings: Settings, system: str) -> bytes:
    """The system's private key: SYSTEMS_DIR/<system>/private-key.pem, or a
    per-request path override confined to that system's directory."""
    if not NAME_RE.fullmatch(system):
        raise ConnectionPolicyError("Invalid system name for credential resolution.")
    base = Path(settings.systems_dir)
    if path_override:
        # Per-request overrides are caller input on the REST path: the server
        # may only read the directory of the system the request names.
        key = check_key_path(path_override, settings, system)
        system_dir = (base / system).resolve()
        if (
            exact_case_path(base, system) is None
            or exact_case_path(system_dir, *key.relative_to(system_dir).parts) is None
        ):
            raise deny(
                "private_key_path",
                f"private_key_path {path_override!r} must be inside the directory of system {system!r}.",
                system,
            )
        return key.read_bytes()
    key = exact_case_path(base, system, KEY_FILE)
    if key is None:
        raise ConnectionPolicyError(f"System {system!r} has no {KEY_FILE}.")
    return key.read_bytes()
