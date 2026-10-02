"""Audit trail: one `key=value` line per security-relevant event.

Lines go to the "successfactors_toolkit.audit" logger, which writes to stderr
only: in MCP stdio mode stdout carries the protocol, so a stray byte there
would corrupt it. Callers pass identifiers and outcomes (company_id, field
names, status codes), never secrets, key material, tokens, request bodies or
personal data. Values are quoted when they hold anything but plain characters,
so a caller-supplied string cannot forge a second line.

Events: key_install, key_delete, production_flag, connection_override,
connection_config, auth, sf_token. `outcome` is "ok", "denied" (rejected by
policy or authentication) or "failed" (an operation error); anything but "ok"
logs at WARNING.
"""

from __future__ import annotations

import json
import logging
import re
import time

logger = logging.getLogger("successfactors_toolkit.audit")

_PLAIN = re.compile(r"[\w.,:@/+-]+")

if not logger.handlers:
    _handler = logging.StreamHandler()  # stderr
    _formatter = logging.Formatter(
        "%(asctime)s %(levelname)s audit %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    _formatter.converter = time.gmtime
    _handler.setFormatter(_formatter)
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # not into a root handler that may format or route elsewhere


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return text if _PLAIN.fullmatch(text) else json.dumps(text[:200])


def audit(event: str, outcome: str, **fields: object) -> None:
    """Record one event. Fields set to None are left out."""
    parts = [f"event={event}", f"outcome={outcome}"]
    parts += [f"{k}={_value(v)}" for k, v in fields.items() if v is not None]
    logger.log(logging.INFO if outcome == "ok" else logging.WARNING, " ".join(parts))
