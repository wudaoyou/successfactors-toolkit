import base64
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.main import app
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.credentials import load_key_pem
from successfactors_toolkit.services.tenant_store import (
    InvalidCompanyId,
    KeyCertMismatch,
    TenantStore,
)


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-test-user")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=7))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def test_registered_tenants_are_listed_without_loading_connection_attribute(
    monkeypatch, tmp_path, keypair
):
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    result = mcp_server.list_tenants()
    assert [tenant["company_id"] for tenant in result["tenants"]] == ["example-a"]
    assert "synthetic-test-user" not in str(result)
    assert stat.S_IMODE(store.key_path("example-a").stat().st_mode) == 0o600


def test_failed_replacement_keeps_previous_keypair(tmp_path, keypair):
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    with pytest.raises(KeyCertMismatch):
        store.install("example-a", different_key, keypair[1], force=True)
    assert store.key_path("example-a").read_bytes() == keypair[0]
    assert store.cert_path("example-a").read_bytes() == keypair[1]


def test_credential_resolution_precedence_and_traversal(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        tenant_keys_dir=str(tmp_path / "tenants"),
        sf_private_key_pem=base64.b64encode(b"global").decode(),
    )
    monkeypatch.setenv("SF_PRIVATE_KEY_PEM_EXAMPLE", base64.b64encode(b"company").decode())
    assert load_key_pem(None, settings, "example") == b"company"
    tenant_key = tmp_path / "tenants" / "example" / "sf_private_key_example.pem"
    tenant_key.parent.mkdir(parents=True)
    tenant_key.write_bytes(b"tenant")
    assert load_key_pem(None, settings, "example") == b"tenant"
    override = tmp_path / "tenants" / "example" / "override.pem"
    override.write_bytes(b"override")
    assert load_key_pem(str(override), settings, "example") == b"override"
    # A path override may not escape the tenant store (connection_policy).
    outside = tmp_path / "outside.pem"
    outside.write_bytes(b"outside")
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(str(outside), settings, "example")
    with pytest.raises((InvalidCompanyId, ValueError)):
        load_key_pem(None, settings, "../../escape")


def test_tenant_routes_require_the_admin_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        api_only = {"X-API-Key": "test-api-key"}
        assert client.get("/api/tenants", headers=api_only).status_code == 401
        assert client.get("/api/tenants/example", headers=api_only).status_code == 401
        admin = {**api_only, "X-Admin-Key": "test-admin-key"}
        assert client.get("/api/tenants", headers=admin).status_code == 200
