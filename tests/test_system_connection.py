"""SF connections come from SYSTEMS_DIR/<system>/<system>.json only."""

import httpx
import pytest

from successfactors_toolkit.config import get_settings
from successfactors_toolkit.models.common import ODataConnectionConfig, SFAPIConnectionConfig
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError
from successfactors_toolkit.services.credentials import load_key_pem
from successfactors_toolkit.services.odata_client import ODataClient
from successfactors_toolkit.services.sfapi_client import SFAPIClient
from successfactors_toolkit.services.system_store import SystemUnavailable
from tests.systems import write_system


def _odata():
    return ODataClient(get_settings(), httpx.AsyncClient())


def _with_key(name, **fields):
    directory = write_system(get_settings().systems_dir, name, **fields)
    (directory / "private-key.pem").write_bytes(f"key-{name}".encode())


def test_resolve_reads_everything_from_the_system_file():
    _with_key("tc2", company_id="TC2", user_id="u2", client_key="k2", odata_version="v4")
    r = _odata()._resolve(ODataConnectionConfig(system="tc2"))
    assert (r["system"], r["company_id"], r["user_id"], r["client_key"], r["version"]) == (
        "tc2",
        "TC2",
        "u2",
        "k2",
        "v4",
    )
    assert r["private_key_pem"] == b"key-tc2"
    s = SFAPIClient(get_settings(), httpx.AsyncClient())._resolve(
        SFAPIConnectionConfig(system="tc2")
    )
    assert (s["system"], s["company_id"], s["user_id"]) == ("tc2", "TC2", "u2")


def test_no_system_means_the_only_successfactors_system():
    assert _odata()._resolve(None)["system"] == "example-a"


def test_two_systems_and_no_name_is_refused():
    _with_key("example-b")
    with pytest.raises(SystemUnavailable) as info:
        _odata()._resolve(None)
    assert info.value.code == "system_required"


def test_a_file_host_outside_the_policy_is_refused():
    _with_key("evil", host="evil.example.com", token_url="https://evil.example.com/oauth/token")
    with pytest.raises(ConnectionPolicyError):
        _odata()._resolve(ODataConnectionConfig(system="evil"))
    with pytest.raises(ConnectionPolicyError):
        SFAPIClient(get_settings(), httpx.AsyncClient())._resolve(
            SFAPIConnectionConfig(system="evil")
        )


@pytest.mark.parametrize(
    ("client_class", "config"),
    [(ODataClient, ODataConnectionConfig), (SFAPIClient, SFAPIConnectionConfig)],
)
def test_a_request_may_repeat_but_not_replace_the_files_identity(client_class, config):
    client = client_class(get_settings(), httpx.AsyncClient())
    same = config(client_key="synthetic-client-key", user_id="synthetic-user")
    assert client._resolve(same)["user_id"] == "synthetic-user"
    for override in ({"client_key": "request-key"}, {"user_id": "OTHER"}):
        with pytest.raises(ConnectionPolicyError, match="cannot be overridden"):
            client._resolve(config(**override))


def test_a_request_host_override_keeps_the_files_identity():
    _with_key(
        "tc2",
        host="api4preview.sapsf.com",
        token_url="https://api4preview.sapsf.com/oauth/token",
        client_key="k2",
        user_id="u2",
    )
    r = _odata()._resolve(ODataConnectionConfig(system="tc2"))
    assert (r["host"], r["token_url"]) == (
        "api4preview.sapsf.com",
        "https://api4preview.sapsf.com/oauth/token",
    )
    r = _odata()._resolve(ODataConnectionConfig(system="tc2", host="api.example.invalid"))
    assert (r["host"], r["client_key"], r["user_id"]) == ("api.example.invalid", "k2", "u2")


def test_token_cache_key_leads_with_the_system():
    _with_key("a", company_id="acme")
    _with_key("b", company_id="acme")
    client = _odata()
    key_a = client._token_key(client._resolve(ODataConnectionConfig(system="a")))
    key_b = client._token_key(client._resolve(ODataConnectionConfig(system="b")))
    assert key_a[0] == "a" and key_b[0] == "b" and key_a != key_b


def test_key_comes_only_from_the_system_directory():
    settings = get_settings()
    (settings.systems_dir / "example-a" / "private-key.pem").unlink()
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(None, settings, "example-a")


def test_key_lookup_refuses_a_name_in_another_case():
    with pytest.raises(ConnectionPolicyError):
        load_key_pem(None, get_settings(), "EXAMPLE-A")


def test_key_lookup_refuses_a_directory_spelled_in_another_case():
    # A case-insensitive filesystem opens Demo/ for "demo"; the lookup must not.
    settings = get_settings()
    write_system(settings.systems_dir, "Demo")
    (settings.systems_dir / "Demo" / "private-key.pem").write_bytes(b"key-Demo")
    with pytest.raises(ConnectionPolicyError, match="has no private-key.pem"):
        load_key_pem(None, settings, "demo")
    # Selection refuses the wrong spelling before any key lookup.
    with pytest.raises(ConnectionPolicyError, match="No system 'demo'"):
        _odata()._resolve(ODataConnectionConfig(system="demo"))
