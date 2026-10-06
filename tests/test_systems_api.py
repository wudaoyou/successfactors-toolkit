import errno
import fcntl
import json
import os
import shutil
import stat
import threading
import time
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from successfactors_toolkit.config import Settings, get_settings
from successfactors_toolkit.main import app as _app
from successfactors_toolkit.services import system_store
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.credentials import load_key_pem
from successfactors_toolkit.services.system_store import (
    CertNotYetValid,
    InvalidKeyOrCert,
    InvalidSystemName,
    KeyCertMismatch,
    KeypairAlreadyExists,
    SystemNotFound,
    SystemStore,
    SystemStoreError,
    WrongSystemType,
)
from tests.systems import write_system
from tests.test_mcp_systems import _pem_pair

ADMIN = {"X-API-Key": "test-api-key", "X-Admin-Key": "test-admin-key"}


@pytest.fixture
def keypair():
    return _pem_pair()


@pytest.fixture
def key_pem(keypair):
    return keypair[0]


@pytest.fixture
def cert_pem(keypair):
    return keypair[1]


@pytest.fixture
def app():
    return _app


@pytest.fixture
def client(monkeypatch, app):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def settings(client):
    return get_settings()


def _files(key_pem, cert_pem):
    return {"private_key": ("k.pem", key_pem), "certificate": ("c.pem", cert_pem)}


@pytest.fixture
def store(tmp_path):
    """A store with one SF system, example-a, and no keypair yet."""
    write_system(tmp_path / "store", "example-a")
    return SystemStore(tmp_path / "store")


def _key_path(store, name="example-a"):
    return store.system_dir(name) / "private-key.pem"


def _cert_path(store, name="example-a"):
    return store.system_dir(name) / "signing-cert.crt"


# ── REST: admin auth and routes ───────────────────────────────────────────


def test_system_routes_require_the_admin_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    with TestClient(_app) as client:
        api_only = {"X-API-Key": "test-api-key"}
        assert client.get("/api/systems", headers=api_only).status_code == 401
        assert client.get("/api/systems/example-a", headers=api_only).status_code == 401
        assert client.get("/api/systems", headers=ADMIN).status_code == 200
        assert client.get("/api/tenants", headers=ADMIN).status_code == 404


def test_keypair_install_needs_an_existing_successfactors_system(
    client, settings, key_pem, cert_pem
):
    r = client.post("/api/systems/newco/keypair", files=_files(key_pem, cert_pem), headers=ADMIN)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "system_not_found"
    write_system(settings.systems_dir, "widget", {"type": "widget", "production": False})
    r = client.post("/api/systems/widget/keypair", files=_files(key_pem, cert_pem), headers=ADMIN)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "wrong_system_type"


def test_keypair_rotation_keeps_the_config_and_needs_force(client, settings, key_pem, cert_pem):
    write_system(settings.systems_dir, "acme")
    assert (
        client.post(
            "/api/systems/acme/keypair", files=_files(key_pem, cert_pem), headers=ADMIN
        ).status_code
        == 201
    )
    directory = settings.systems_dir / "acme"
    assert {p.name for p in directory.iterdir()} >= {
        "acme.json",
        "private-key.pem",
        "signing-cert.crt",
    }
    assert oct(os.stat(directory / "private-key.pem").st_mode & 0o777) == "0o600"
    r = client.post("/api/systems/acme/keypair", files=_files(key_pem, cert_pem), headers=ADMIN)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "keypair_already_exists"
    r = client.post(
        "/api/systems/acme/keypair?force=true", files=_files(key_pem, cert_pem), headers=ADMIN
    )
    assert r.status_code == 201
    assert json.loads((directory / "acme.json").read_text())["type"] == "successfactors"


def test_list_shows_every_system_and_keypairs_only_for_successfactors(client, settings):
    write_system(settings.systems_dir, "widget", {"type": "widget", "production": True})
    rows = {row["name"]: row for row in client.get("/api/systems", headers=ADMIN).json()}
    assert rows["widget"]["type"] == "widget" and rows["widget"]["keypair"] is None
    assert rows["widget"]["error"] == "system_unsupported"
    assert rows["example-a"]["type"] == "successfactors"


def test_get_returns_one_system_with_its_keypair_metadata(client, settings, key_pem, cert_pem):
    client.post("/api/systems/example-a/keypair", files=_files(key_pem, cert_pem), headers=ADMIN)
    body = client.get("/api/systems/example-a", headers=ADMIN).json()
    assert body["name"] == "example-a" and body["production"] is False and body["error"] is None
    assert body["keypair"]["private_key"] == {"algorithm": "RSA", "size_bits": 2048}
    assert body["keypair"]["certificate"]["cn"] == "synthetic-test-user"
    assert body["keypair"]["certificate"]["warning"]  # a 7-day certificate
    r = client.get("/api/systems/nope", headers=ADMIN)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "system_not_found"
    assert client.get("/api/systems/UPPER", headers=ADMIN).status_code == 422
    assert client.get("/api/systems/x.y", headers=ADMIN).status_code == 422


def test_environment_put_keeps_the_other_keys(client, settings):
    r = client.put("/api/systems/example-a/environment", json={"production": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["production"] is True
    data = json.loads((settings.systems_dir / "example-a" / "example-a.json").read_text())
    assert data["production"] is True and data["company_id"] == "example-a"


def test_environment_put_flips_the_flag_and_list_follows(client):
    url = "/api/systems/example-a/environment"
    assert client.get("/api/systems/example-a", headers=ADMIN).json()["production"] is False
    assert client.put(url, headers=ADMIN, json={"production": True}).json()["production"] is True
    assert client.put(url, headers=ADMIN, json={"production": False}).json()["production"] is False
    assert client.get("/api/systems", headers=ADMIN).json()[0]["production"] is False


def test_environment_put_rejects_unknown_system_bad_body_and_missing_key(client, settings):
    url = "/api/systems/nope/environment"
    api_only = {"X-API-Key": ADMIN["X-API-Key"]}
    assert client.put(url, headers=ADMIN, json={"production": True}).status_code == 404
    assert client.put(url, headers=api_only, json={"production": True}).status_code == 401
    ok = "/api/systems/example-a/environment"
    assert client.put(ok, headers=ADMIN, json={"production": "yes"}).status_code == 422
    assert not (settings.systems_dir / "nope").exists()


def test_environment_put_keeps_the_file_mode(client, settings):
    path = settings.systems_dir / "example-a" / "example-a.json"
    path.chmod(0o600)
    r = client.put("/api/systems/example-a/environment", json={"production": True}, headers=ADMIN)
    assert r.status_code == 200
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_environment_put_refuses_a_broken_file_and_leaves_it_alone(client, settings):
    path = settings.systems_dir / "example-a" / "example-a.json"
    path.write_text("{bad")
    r = client.put("/api/systems/example-a/environment", json={"production": True}, headers=ADMIN)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "system_invalid"
    assert path.read_text() == "{bad"


def test_delete_removes_the_system_directory_and_its_cached_sessions(client, settings, app):
    app.state.odata_client._tokens[("example-a", "h", "u", "k")] = ("t", 0.0)
    assert client.delete("/api/systems/example-a", headers=ADMIN).status_code == 204
    assert not (settings.systems_dir / "example-a").exists()
    assert ("example-a", "h", "u", "k") not in app.state.odata_client._tokens
    r = client.delete("/api/systems/example-a", headers=ADMIN)
    assert r.status_code == 404 and r.json()["detail"]["error"] == "system_not_found"


def test_delete_removes_a_broken_system_too(client, settings):
    (settings.systems_dir / "example-a" / "example-a.json").write_text("{bad")
    assert client.delete("/api/systems/example-a", headers=ADMIN).status_code == 204
    assert not (settings.systems_dir / "example-a").exists()


@pytest.mark.parametrize(
    "key, error",
    [
        (b"not a key", "invalid_private_key"),
        (ec.generate_private_key(ec.SECP256R1()), "invalid_private_key"),
    ],
    ids=["garbage", "ec-p256"],
)
def test_keypair_upload_validation_errors_are_400(client, cert_pem, key, error):
    if not isinstance(key, bytes):
        key = _pem_pair(key)[0]
    r = client.post("/api/systems/example-a/keypair", files=_files(key, cert_pem), headers=ADMIN)
    assert r.status_code == 400 and r.json()["detail"]["error"] == error


def test_keypair_upload_of_a_mismatched_pair_is_400_and_nothing_is_written(
    client, settings, cert_pem
):
    other = _pem_pair()[0]
    r = client.post("/api/systems/example-a/keypair", files=_files(other, cert_pem), headers=ADMIN)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "key_cert_mismatch"
    assert not (settings.systems_dir / "example-a" / "signing-cert.crt").exists()


# ── store: install ────────────────────────────────────────────────────────


def test_install_writes_a_pair_with_the_documented_modes(store, keypair):
    info = store.install("example-a", *keypair)
    assert stat.S_IMODE(_key_path(store).stat().st_mode) == 0o600
    assert stat.S_IMODE(_cert_path(store).stat().st_mode) == 0o644
    assert info == store.keypair("example-a")
    assert info.certificate.cn == "synthetic-test-user"


def test_failed_replacement_keeps_previous_keypair(store, keypair):
    store.install("example-a", *keypair)
    different_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    with pytest.raises(KeyCertMismatch):
        store.install("example-a", different_key, keypair[1], force=True)
    assert _key_path(store).read_bytes() == keypair[0]
    assert _cert_path(store).read_bytes() == keypair[1]


@pytest.mark.parametrize(
    "key",
    [
        ec.generate_private_key(ec.SECP256R1()),
        rsa.generate_private_key(public_exponent=65537, key_size=1024),
    ],
    ids=["ec-p256", "rsa-1024"],
)
def test_install_rejects_keys_other_than_rsa_2048_or_more(store, key):
    with pytest.raises(InvalidKeyOrCert, match="RSA") as exc:
        store.install("example-a", *_pem_pair(key))
    assert exc.value.code == "invalid_private_key"
    assert store.keypair("example-a") is None


def test_install_rejects_a_certificate_not_yet_valid_beyond_clock_skew(store):
    with pytest.raises(CertNotYetValid) as exc:
        store.install("example-a", *_pem_pair(not_before=timedelta(days=1)))
    assert exc.value.code == "certificate_not_yet_valid"
    store.install("example-a", *_pem_pair(not_before=timedelta(minutes=1)))


def test_install_rejects_an_expired_certificate(store):
    with pytest.raises(system_store.CertExpired) as exc:
        store.install(
            "example-a", *_pem_pair(not_before=timedelta(days=-30), valid_for=timedelta(days=-1))
        )
    assert exc.value.code == "certificate_expired"


def test_install_needs_force_to_replace_a_keypair(store, keypair):
    store.install("example-a", *keypair)
    with pytest.raises(KeypairAlreadyExists) as exc:
        store.install("example-a", *keypair)
    assert exc.value.code == "keypair_already_exists"
    store.install("example-a", *keypair, force=True)


def test_install_checks_the_system_before_the_upload(store, keypair):
    with pytest.raises(SystemNotFound):
        store.install("newco", *keypair)
    write_system(store.base, "widget", {"type": "widget", "production": False})
    with pytest.raises(WrongSystemType):
        store.install("widget", *keypair)
    assert not (store.base / "widget" / "private-key.pem").exists()
    assert not (store.base / "newco").exists()


def test_key_rotation_keeps_the_system_file_and_other_files(store, keypair):
    (store.system_dir("example-a") / "notes.txt").write_text("keep")
    store.set_production("example-a", True)
    store.install("example-a", *keypair)
    store.install("example-a", *keypair, force=True)
    assert store.info("example-a").production is True
    assert (store.system_dir("example-a") / "notes.txt").read_text() == "keep"


def test_install_keeps_subdirectories_and_the_directory_mode(store, keypair):
    # A read-only system directory (0o555) installs and keeps its mode.
    system_dir = store.system_dir("example-a")
    (system_dir / "extra").mkdir()
    (system_dir / "extra" / "note.txt").write_text("keep")
    (system_dir / "extra" / "private-key.pem").write_text("old copy")
    system_dir.chmod(0o555)
    try:
        store.install("example-a", *keypair)
        assert stat.S_IMODE(system_dir.stat().st_mode) == 0o555
    finally:
        system_dir.chmod(0o755)
    assert (system_dir / "extra" / "note.txt").read_text() == "keep"
    assert (system_dir / "extra" / "private-key.pem").read_text() == "old copy"
    assert (system_dir / "private-key.pem").read_text() != "old copy"


def test_concurrent_installs_do_not_both_succeed(monkeypatch, store, keypair):
    check_pair = system_store._check_pair
    monkeypatch.setattr(system_store, "_check_pair", lambda *a: (time.sleep(0.2), check_pair(*a)))
    start, results = threading.Barrier(2), []

    def install():
        start.wait()
        try:
            store.install("example-a", *keypair)
            results.append("ok")
        except KeypairAlreadyExists:
            results.append("exists")

    threads = [threading.Thread(target=install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["exists", "ok"]


def test_install_waits_for_the_lock_file_another_process_holds(store, keypair):
    store.set_production("example-a", False)  # creates the lock file
    fd = os.open(store.base / ".lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    worker = threading.Thread(target=store.install, args=("example-a", *keypair))
    worker.start()
    time.sleep(0.2)
    assert worker.is_alive() and store.keypair("example-a") is None
    os.close(fd)
    worker.join()
    assert store.keypair("example-a") is not None


@pytest.mark.parametrize("exchange", [True, False], ids=["exchange", "file-by-file"])
def test_forced_reinstall_never_removes_the_system_directory(monkeypatch, store, keypair, exchange):
    store.install("example-a", *keypair)
    store.set_production("example-a", True)
    if not exchange:

        def unsupported(*_):
            raise OSError(errno.EINVAL, "not supported")

        monkeypatch.setattr(system_store, "_exchange", unsupported)
    rmtree = shutil.rmtree

    def checked_rmtree(path, *args, **kwargs):
        rmtree(path, *args, **kwargs)
        assert _key_path(store).exists(), "system key missing mid-install"

    monkeypatch.setattr(shutil, "rmtree", checked_rmtree)
    new = _pem_pair()
    store.install("example-a", *new, force=True)
    assert _key_path(store).read_bytes() == new[0]
    assert _cert_path(store).read_bytes() == new[1]
    assert store.info("example-a").production is True
    assert sorted(p.name for p in store.base.iterdir()) == [".lock", "example-a"]


def test_store_root_is_private(tmp_path, store, keypair):
    fresh = SystemStore(tmp_path / "fresh" / "systems")
    with pytest.raises(SystemNotFound):
        fresh.install("example-a", *keypair)  # no system there, but the root is made private
    assert stat.S_IMODE(fresh.base.stat().st_mode) == 0o700


# ── store: environment flag, names, deletion ──────────────────────────────


def test_set_production_round_trip_keeps_the_file_contents_and_mode(store):
    path = store.config_path("example-a")
    before = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    store.set_production("example-a", True)
    assert store.info("example-a").production is True
    store.set_production("example-a", False)
    assert store.info("example-a").production is False
    assert json.loads(path.read_text()) == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize("content", ["", "{bad", "[]"])
def test_set_production_never_rewrites_a_file_it_cannot_read(store, content):
    path = store.config_path("example-a")
    path.write_text(content)
    with pytest.raises(SystemStoreError) as exc:
        store.set_production("example-a", True)
    assert exc.value.code == "system_invalid"
    assert path.read_text() == content
    assert [p.name for p in store.system_dir("example-a").iterdir()] == ["example-a.json"]


@pytest.mark.parametrize("name", ["../x", "", "A", "a/b", "x\n", "-a", "a" * 64])
def test_every_mutation_rejects_names_that_are_not_a_path_segment(store, keypair, name):
    for call in (
        lambda: store.set_production(name, True),
        lambda: store.install(name, *keypair),
        lambda: store.delete(name),
    ):
        with pytest.raises(InvalidSystemName) as exc:
            call()
        assert exc.value.code == "invalid_system_name"
    assert system_store.NAME_RE.fullmatch("x\n") is None


def test_set_production_and_delete_need_an_existing_system(store):
    for call in (lambda: store.set_production("nope", True), lambda: store.delete("nope")):
        with pytest.raises(SystemNotFound):
            call()
    assert not (store.base / "nope").exists()


def test_delete_removes_only_that_system(store):
    write_system(store.base, "example-b")
    store.delete("example-a")
    assert store.names() == ["example-b"]


def test_system_names_do_not_share_files_across_case(tmp_path, keypair):
    # On a case-insensitive filesystem (macOS, Docker Desktop) systems/demo/
    # also opens as systems/DEMO/; "DEMO" must still not read "demo"'s files.
    settings = Settings(_env_file=None, systems_dir=tmp_path / "systems")
    store = SystemStore(settings.systems_dir)
    write_system(store.base, "demo")
    store.install("demo", *keypair)
    assert store.raw("DEMO") == {} and store.keypair("DEMO") is None
    with pytest.raises(InvalidSystemName):
        store.install("DEMO", *keypair)
    system_dir = settings.systems_dir / "demo"
    assert load_key_pem(None, settings, "demo") == keypair[0]
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(None, settings, "DEMO")
    variant = settings.systems_dir / "DEMO" / "private-key.pem"
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(str(variant), settings, "DEMO")
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(str(system_dir / "PRIVATE-KEY.PEM"), settings, "demo")


def test_a_directory_differing_only_in_case_is_not_the_system(tmp_path, keypair):
    store = SystemStore(tmp_path / "systems")
    (tmp_path / "systems" / "DEMO").mkdir(parents=True)
    (tmp_path / "systems" / "DEMO" / "DEMO.json").write_text('{"type": "x", "production": true}')
    if not store.system_dir("demo").exists():
        pytest.skip("case-sensitive filesystem")
    with pytest.raises(SystemNotFound):
        store.install("demo", *keypair, force=True)
    with pytest.raises(SystemNotFound):
        store.delete("demo")
    assert (tmp_path / "systems" / "DEMO" / "DEMO.json").is_file()


def test_credential_resolution_and_traversal(tmp_path):
    settings = Settings(_env_file=None)
    system_key = settings.systems_dir / "example" / "private-key.pem"
    system_key.parent.mkdir(parents=True)
    system_key.write_bytes(b"system")
    assert load_key_pem(None, settings, "example") == b"system"
    override = settings.systems_dir / "example" / "override.pem"
    override.write_bytes(b"override")
    assert load_key_pem(str(override), settings, "example") == b"override"
    # A path override may not escape the system's directory (connection_policy).
    outside = tmp_path / "outside.pem"
    outside.write_bytes(b"outside")
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(str(outside), settings, "example")
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(None, settings, "../../escape")
