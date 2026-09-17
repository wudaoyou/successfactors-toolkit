"""Tests run with synthetic configuration and no external sockets."""

import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    # Never read the developer's working-directory .env or inherited SF secrets.
    monkeypatch.chdir(tmp_path)
    import os

    for name in list(os.environ):
        if name.startswith("SF_") or name in {"API_KEY", "ADMIN_API_KEY", "TENANT_KEYS_DIR"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("SF_HOST", "api.example.invalid")
    monkeypatch.setenv("TENANT_KEYS_DIR", str(tmp_path / "tenants"))
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
