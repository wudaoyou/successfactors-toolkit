"""Tests run with synthetic configuration and no external sockets."""

import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    # Never read the developer's working-directory .env or inherited SF secrets.
    monkeypatch.chdir(tmp_path)
    import os

    for name in list(os.environ):
        if name.startswith(("SF_", "PII_")) or name in {
            "API_KEY",
            "ADMIN_API_KEY",
            "TENANT_KEYS_DIR",
            "SYSTEMS_DIR",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("SF_HOST", "api.example.invalid")
    monkeypatch.setenv("TENANT_KEYS_DIR", str(tmp_path / "tenants"))
    from tests.systems import write_system

    monkeypatch.setenv("SYSTEMS_DIR", str(tmp_path / "systems"))
    monkeypatch.setenv("SF_ALLOWED_HOSTS", '["api.example.invalid"]')
    (write_system(tmp_path / "systems", "example-a") / "private-key.pem").write_bytes(
        b"synthetic-key"
    )
    from successfactors_toolkit.config import get_settings

    get_settings.cache_clear()

    def deny_network(*args, **kwargs):
        raise AssertionError("Live network access is forbidden in tests; use httpx.MockTransport")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    original_connect = socket.socket.connect

    def local_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            deny_network()
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    yield
    get_settings.cache_clear()


@pytest.fixture
def widget_type(monkeypatch):
    """tests.systems.Widget registered as "widget" for one test; the type
    registry is a copy, so nothing a test registers outlives it."""
    from successfactors_toolkit.services import system_store
    from tests.systems import Widget

    monkeypatch.setattr(system_store, "_TYPES", dict(system_store._TYPES))
    system_store.register_type("widget", Widget)
    return Widget
