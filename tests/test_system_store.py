"""SYSTEMS_DIR/<name>/<name>.json: validation, type registry, selection."""

from typing import Literal

import pytest

from successfactors_toolkit.services.system_store import (
    SF_TYPE,
    SuccessFactorsSystem,
    SystemBase,
    SystemStore,
    SystemUnavailable,
    register_type,
)
from tests.systems import Widget, write_system


@pytest.fixture
def store(tmp_path):
    (tmp_path / "store").mkdir()
    return SystemStore(tmp_path / "store")


def _code(fn) -> str:
    with pytest.raises(SystemUnavailable) as info:
        fn()
    return info.value.code


def test_an_sf_file_validates_with_defaults(store):
    write_system(store.base, "tctrain")
    config = store.config("tctrain")
    assert isinstance(config, SuccessFactorsSystem)
    assert (config.company_id, config.odata_version, config.production) == ("tctrain", "v2", False)


@pytest.mark.parametrize(
    "config, code",
    [
        ({"production": False}, "system_invalid"),
        ({"type": 1, "production": False}, "system_invalid"),
        ({"type": "successfactors"}, "system_invalid"),
        ({"type": "widget", "production": False}, "system_unsupported"),
    ],
)
def test_bad_files_are_refused(store, config, code):
    write_system(store.base, "x", config)
    assert _code(lambda: store.config("x")) == code


def test_sf_schema_rejects_unknown_and_missing_keys(store):
    write_system(store.base, "alpha", surprise=1)
    assert _code(lambda: store.config("alpha")) == "system_invalid"
    write_system(store.base, "beta", {"type": SF_TYPE, "production": False, "company_id": "beta"})
    with pytest.raises(SystemUnavailable) as info:
        store.config("beta")
    assert "host" in info.value.detail and "client_key" in info.value.detail


def test_production_locks_tier_three(store):
    write_system(store.base, "alpha", production=True, pii_filter_tier=1)
    assert _code(lambda: store.config("alpha")) == "system_invalid"
    write_system(store.base, "beta", production=True, pii_filter_tier=3)
    assert store.config("beta").pii_filter_tier == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"pii_filter_tier": "2"},
        {"pii_filter_tier": 4},
        {"pii_filter_tier": True},
        {"pii_extra_fields": {"PerPersonal": {"customString6": 4}}},
        {"pii_extra_fields": ["PerPersonal"]},
        {"host": 1},
        {"client_key": 123},
        {"user_id": ["APIUSER"]},
        {"odata_version": "v3"},
    ],
)
def test_each_bad_type_is_invalid(store, bad):
    write_system(store.base, "alpha", **bad)
    with pytest.raises(SystemUnavailable) as info:
        store.config("alpha")
    assert info.value.code == "system_invalid" and next(iter(bad)) in info.value.detail


@pytest.mark.parametrize("text", ["{not json", "[]", '"x"'])
def test_unreadable_or_non_object_json_is_invalid(store, text):
    (store.base / "x").mkdir()
    (store.base / "x" / "x.json").write_text(text)
    assert _code(lambda: store.config("x")) == "system_invalid"


@pytest.mark.parametrize("name", ["nope", "../etc", "", "A", "alpha\n"])
def test_unknown_or_malformed_names_are_unknown(store, name):
    write_system(store.base, "alpha")
    assert _code(lambda: store.config(name)) == "system_unknown"


def test_name_case_must_match_exactly(store):
    write_system(store.base, "alpha")
    assert _code(lambda: store.config("ALPHA")) == "system_unknown"
    # On a case-insensitive filesystem demo/demo.json opens Demo/Demo.json.
    write_system(store.base, "Demo")
    assert _code(lambda: store.config("demo")) == "system_unknown"
    assert _code(lambda: store.config("Demo")) == "system_unknown"
    assert store.names() == ["alpha"]


def test_invalid_detail_never_echoes_values(store):
    write_system(store.base, "x", client_key=987654321)
    with pytest.raises(SystemUnavailable) as info:
        store.config("x")
    assert "987654321" not in info.value.detail and "client_key" in info.value.detail


def test_a_registered_type_validates_with_its_model(store, widget_type):
    write_system(store.base, "w", {"type": "widget", "production": True})
    config = store.config("w")
    assert isinstance(config, Widget) and config.colour == "blue"


def test_register_type_is_idempotent_but_never_replaces(widget_type):
    register_type("widget", Widget)

    class _Other(SystemBase):
        type: Literal["widget"]

    with pytest.raises(ValueError):
        register_type("widget", _Other)
    with pytest.raises(ValueError):
        register_type(SF_TYPE, Widget)
    with pytest.raises(ValueError):
        register_type("Bad Name", Widget)


def test_select_picks_the_only_system_of_a_type(store, widget_type):
    write_system(store.base, "tctrain")
    write_system(store.base, "w", {"type": "widget", "production": False})
    assert store.select(SF_TYPE) == "tctrain"
    assert store.select("widget") == "w"


def test_select_needs_a_name_when_several(store):
    write_system(store.base, "alpha")
    write_system(store.base, "beta")
    with pytest.raises(SystemUnavailable) as info:
        store.select(SF_TYPE)
    assert info.value.code == "system_required"
    assert "alpha" in info.value.detail and "beta" in info.value.detail
    assert store.select(SF_TYPE, "beta") == "beta"


def test_select_refuses_a_system_of_another_type(store, widget_type):
    write_system(store.base, "w", {"type": "widget", "production": False})
    assert _code(lambda: store.select(SF_TYPE, "w")) == "system_unknown"


def test_select_with_nothing_configured_is_unknown(store):
    assert _code(lambda: store.select(SF_TYPE)) == "system_unknown"


def test_select_refuses_the_only_match_when_it_is_invalid(store):
    write_system(store.base, "alpha", surprise=1)
    assert _code(lambda: store.select(SF_TYPE)) == "system_invalid"


def test_default_selection_is_refused_while_a_file_is_unreadable(store):
    write_system(store.base, "good")
    (store.base / "broken").mkdir()
    (store.base / "broken" / "broken.json").write_text("{oops")
    with pytest.raises(SystemUnavailable) as info:
        store.select(SF_TYPE)
    assert info.value.code == "system_required" and "broken" in info.value.detail
    assert store.select(SF_TYPE, "good") == "good"


def test_names_lists_only_directories_with_their_file(store):
    write_system(store.base, "alpha")
    (store.base / "stray").mkdir()
    (store.base / "Upper").mkdir()
    (store.base / "Upper" / "Upper.json").write_text("{}")
    (store.base / "loose.json").write_text("{}")
    assert store.names() == ["alpha"]


def test_info_reports_errors_without_raising(store):
    write_system(store.base, "ok")
    write_system(store.base, "bad", {"production": False})
    infos = {name: store.info(name) for name in store.names()}
    assert infos["ok"].error is None and infos["ok"].type == SF_TYPE
    assert infos["ok"].production is False
    assert infos["bad"].error == "system_invalid" and infos["bad"].type is None


def test_keypair_is_none_until_both_files_exist(store):
    write_system(store.base, "alpha")
    assert store.keypair("alpha") is None
