"""Write SYSTEMS_DIR/<name>/<name>.json files for tests."""

import json
from pathlib import Path
from typing import Literal

from successfactors_toolkit.services.system_store import SystemBase

SF_DEFAULTS = {
    "type": "successfactors",
    "production": False,
    "host": "api.example.invalid",
    "token_url": "https://api.example.invalid/oauth/token",
    "client_key": "synthetic-client-key",
    "user_id": "synthetic-user",
}


class Widget(SystemBase):
    """A plugin-style system type; the `widget_type` fixture registers it."""

    type: Literal["widget"]
    colour: str = "blue"


def write_system(systems_dir, name: str, config: dict | None = None, **fields) -> Path:
    """An SF system (company_id = name) unless `config` is given; `fields` override keys."""
    data = {**SF_DEFAULTS, "company_id": name} if config is None else dict(config)
    data.update(fields)
    directory = Path(systems_dir) / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return directory
