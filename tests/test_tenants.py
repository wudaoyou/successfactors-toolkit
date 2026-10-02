import base64
import errno
import fcntl
import json
import os
import shutil
import stat
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from successfactors_toolkit import mcp_server
from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.main import app
from successfactors_toolkit.services import tenant_store
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.credentials import load_key_pem
from successfactors_toolkit.services.tenant_store import (
    CertNotYetValid,
    InvalidCompanyId,
    InvalidKeyOrCert,
    KeyCertMismatch,
    TenantAlreadyExists,
    TenantStore,
)


def _pem_pair(key=None, not_before=timedelta(minutes=-1)):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-test-user")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + not_before)
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


@pytest.fixture
def keypair():
    return _pem_pair()


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


def test_production_flag_round_trip(tmp_path):
    store = TenantStore(str(tmp_path / "tenants"))
    assert store.production("example-a") is None
    store.set_production("example-a", True)
    assert store.production("example-a") is True
    store.set_production("example-a", False)
    assert store.production("example-a") is False
    flag = store.tenant_dir("example-a") / "example-a.json"
    assert json.loads(flag.read_text()) == {"production": False}
    assert stat.S_IMODE(flag.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "content", ["", "{bad", "[]", "{}", '{"production": "true"}', '{"production": 1}']
)
def test_production_flag_unset_forms_read_as_none(tmp_path, content):
    store = TenantStore(str(tmp_path / "tenants"))
    store.tenant_dir("example-a").mkdir(parents=True)
    (store.tenant_dir("example-a") / "example-a.json").write_text(content)
    assert store.production("example-a") is None


def test_production_flag_ignores_ids_that_are_not_a_path_segment(tmp_path):
    store = TenantStore(str(tmp_path / "tenants"))
    for flag in (tmp_path / ".json", tmp_path / "tenants" / ".json"):
        flag.parent.mkdir(exist_ok=True)
        flag.write_text('{"production": true}')
    assert store.production("../") is None and store.production("") is None
    with pytest.raises(InvalidCompanyId):
        store.set_production("../x", True)


def test_key_rotation_keeps_the_production_flag(tmp_path, keypair):
    store = TenantStore(str(tmp_path / "tenants"))
    store.set_production("example-a", True)
    store.install("example-a", *keypair)
    assert store.production("example-a") is True
    store.install("example-a", *keypair, force=True)
    assert store.production("example-a") is True
    assert store.get("example-a").production is True


def test_a_dir_holding_only_the_flag_is_not_a_tenant(tmp_path):
    store = TenantStore(str(tmp_path / "tenants"))
    store.set_production("example-a", False)
    assert store.list_tenants() == []
    assert mcp_server.list_tenants()["tenants"] == []


def test_mcp_list_tenants_reports_environment_tier_and_unset_warning(
    monkeypatch, tmp_path, keypair
):
    monkeypatch.setenv("SF_COMPANY_ID", "example-c")
    monkeypatch.setenv("PII_FILTER_TIER", "2")
    get_settings.cache_clear()
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    store.install("example-b", *keypair)
    store.set_production("example-a", True)
    result = mcp_server.list_tenants()
    a, b = result["tenants"]
    assert (a["production"], a["pii_filter_tier"]) == (True, 3)
    assert (b["production"], b["pii_filter_tier"]) == (None, None)
    assert (result["default"]["production"], result["default"]["pii_filter_tier"]) == (None, None)
    [warning] = result["warnings"]
    assert "example-b, example-c" in warning and "example-a" not in warning

    store.set_production("example-b", False)
    store.set_production("example-c", False)
    result = mcp_server.list_tenants()
    assert result["tenants"][1]["pii_filter_tier"] == 2
    assert (result["default"]["production"], result["default"]["pii_filter_tier"]) == (False, 2)
    assert "warnings" not in result


def _admin_headers(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    return {"X-API-Key": "test-api-key", "X-Admin-Key": "test-admin-key"}


def test_environment_route_sets_and_flips_the_flag(monkeypatch, tmp_path, keypair):
    admin = _admin_headers(monkeypatch)
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    with TestClient(app) as client:
        assert client.get("/api/tenants/example-a", headers=admin).json()["production"] is None
        response = client.put(
            "/api/tenants/example-a/environment", headers=admin, json={"production": True}
        )
        assert response.status_code == 200 and response.json()["production"] is True
        response = client.put(
            "/api/tenants/example-a/environment", headers=admin, json={"production": False}
        )
        assert response.json()["production"] is False
        assert client.get("/api/tenants", headers=admin).json()[0]["production"] is False
    assert store.production("example-a") is False


def test_environment_route_rejects_unknown_tenant_bad_body_and_missing_key(monkeypatch, tmp_path):
    admin = _admin_headers(monkeypatch)
    api_only = {"X-API-Key": admin["X-API-Key"]}
    url = "/api/tenants/example-a/environment"
    with TestClient(app) as client:
        assert client.put(url, headers=admin, json={"production": True}).status_code == 404
        assert client.put(url, headers=api_only, json={"production": True}).status_code == 401
        assert client.put(url, headers=admin, json={"production": "yes"}).status_code == 422
    assert not (tmp_path / "tenants" / "example-a").exists()


def test_environment_route_keeps_other_keys_and_the_file_mode(monkeypatch, tmp_path, keypair):
    admin = _admin_headers(monkeypatch)
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    flag = store.tenant_dir("example-a") / "example-a.json"
    flag.write_text('{"production": false, "client_key": "key-a", "pii_filter_tier": 3}')
    flag.chmod(0o600)
    with TestClient(app) as client:
        response = client.put(
            "/api/tenants/example-a/environment", headers=admin, json={"production": True}
        )
    assert response.status_code == 200 and response.json()["production"] is True
    assert json.loads(flag.read_text()) == {
        "production": True,
        "client_key": "key-a",
        "pii_filter_tier": 3,
    }
    assert stat.S_IMODE(flag.stat().st_mode) == 0o600


def test_mcp_list_tenants_reports_effective_settings_and_config_errors(
    monkeypatch, tmp_path, keypair
):
    monkeypatch.setenv("SF_USER_ID", "ENVUSER")
    get_settings.cache_clear()
    store = TenantStore(str(tmp_path / "tenants"))
    for company_id in ("example-a", "example-b", "example-c"):
        store.install(company_id, *keypair)
    (store.tenant_dir("example-a") / "example-a.json").write_text(
        json.dumps(
            {
                "production": False,
                "pii_filter_tier": 2,
                "host": "api4preview.sapsf.com",
                "token_url": "https://api4preview.sapsf.com/oauth/token",
                "user_id": "FILEUSER",
            }
        )
    )
    store.set_production("example-b", True)
    (store.tenant_dir("example-c") / "example-c.json").write_text(
        '{"production": false, "host": "api4preview.sapsf.com"}'
    )
    result = mcp_server.list_tenants()
    a, b, c = result["tenants"]
    assert (a["host"], a["technical_user"], a["production"], a["pii_filter_tier"]) == (
        "api4preview.sapsf.com",
        "FILEUSER",
        False,
        2,
    )
    assert (b["host"], b["technical_user"], b["pii_filter_tier"]) == (
        "api.example.invalid",
        "ENVUSER",
        3,
    )
    assert "config_error" not in a and "config_error" not in b
    assert "token_url" in c["config_error"] and c["pii_filter_tier"] is None
    assert any("example-c" in w and "token_url" in w for w in result["warnings"])


@pytest.mark.parametrize(
    "key",
    [
        ec.generate_private_key(ec.SECP256R1()),
        rsa.generate_private_key(public_exponent=65537, key_size=1024),
    ],
    ids=["ec-p256", "rsa-1024"],
)
def test_install_rejects_keys_other_than_rsa_2048_or_more(tmp_path, key):
    store = TenantStore(str(tmp_path / "tenants"))
    with pytest.raises(InvalidKeyOrCert, match="RSA") as exc:
        store.install("example-a", *_pem_pair(key))
    assert exc.value.code == "invalid_private_key"
    assert not store.exists("example-a")


def test_install_rejects_a_certificate_not_yet_valid_beyond_clock_skew(tmp_path):
    store = TenantStore(str(tmp_path / "tenants"))
    with pytest.raises(CertNotYetValid) as exc:
        store.install("example-a", *_pem_pair(not_before=timedelta(days=1)))
    assert exc.value.code == "certificate_not_yet_valid"
    store.install("example-a", *_pem_pair(not_before=timedelta(minutes=1)))


def test_concurrent_installs_do_not_both_succeed(monkeypatch, tmp_path, keypair):
    store = TenantStore(str(tmp_path / "tenants"))
    check_pair = tenant_store._check_pair
    monkeypatch.setattr(tenant_store, "_check_pair", lambda *a: (time.sleep(0.2), check_pair(*a)))
    start, results = threading.Barrier(2), []

    def install():
        start.wait()
        try:
            store.install("example-a", *keypair)
            results.append("ok")
        except TenantAlreadyExists:
            results.append("exists")

    threads = [threading.Thread(target=install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["exists", "ok"]


def test_install_waits_for_the_lock_file_another_process_holds(tmp_path, keypair):
    store = TenantStore(str(tmp_path / "tenants"))
    store.set_production("example-a", False)  # creates the root and its .lock
    fd = os.open(tmp_path / "tenants" / ".lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    worker = threading.Thread(target=store.install, args=("example-a", *keypair))
    worker.start()
    time.sleep(0.2)
    assert worker.is_alive() and not store.exists("example-a")
    os.close(fd)
    worker.join()
    assert store.exists("example-a")


@pytest.mark.parametrize("exchange", [True, False], ids=["exchange", "file-by-file"])
def test_forced_reinstall_never_removes_the_tenant_directory(
    monkeypatch, tmp_path, keypair, exchange
):
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    store.set_production("example-a", True)
    if not exchange:

        def unsupported(*_):
            raise OSError(errno.EINVAL, "not supported")

        monkeypatch.setattr(tenant_store, "_exchange", unsupported)
    rmtree = shutil.rmtree

    def checked_rmtree(path, *args, **kwargs):
        rmtree(path, *args, **kwargs)
        assert store.key_path("example-a").exists(), "tenant key missing mid-install"

    monkeypatch.setattr(shutil, "rmtree", checked_rmtree)
    new = _pem_pair()
    store.install("example-a", *new, force=True)
    assert store.key_path("example-a").read_bytes() == new[0]
    assert store.cert_path("example-a").read_bytes() == new[1]
    assert store.production("example-a") is True
    assert sorted(p.name for p in (tmp_path / "tenants").iterdir()) == [".lock", "example-a"]


def test_tenant_store_directories_are_private(tmp_path, keypair):
    store = TenantStore(str(tmp_path / "tenants"))
    store.install("example-a", *keypair)
    store.set_production("example-b", False)
    for path in (
        tmp_path / "tenants",
        store.tenant_dir("example-a"),
        store.tenant_dir("example-b"),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
