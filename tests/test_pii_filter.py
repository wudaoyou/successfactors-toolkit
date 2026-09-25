"""pii_filter: vault, tokenization of OData JSON and CE XML, request
detokenization and reveal. No network; every vault lives in tmp_path."""

import html
import json
import os
import re
import sqlite3
import stat
from urllib.parse import quote, quote_plus

import pytest
from lxml import etree
from pydantic import ValidationError

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.pii_filter import (
    PiiFilter,
    PiiUnknownTokenError,
    PiiVaultError,
    TenantEnvironmentUnset,
    Vault,
    detokenize,
    for_tenant,
    retokenize,
    reveal,
)
from successfactors_toolkit.services.tenant_store import TenantStore

_TOKEN = re.compile(r"\[PII-T([1-3])-([0-9a-f]{16})\]")


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_vault_creates_owner_only_key_and_database(tmp_path):
    Vault(tmp_path / "vault")
    assert _mode(tmp_path / "vault") == 0o700
    assert _mode(tmp_path / "vault" / "key") == 0o600
    assert _mode(tmp_path / "vault" / "vault.sqlite") == 0o600
    assert len((tmp_path / "vault" / "key").read_bytes()) == 32


def test_vault_digest_is_stable_across_reopen_and_depends_on_the_key(tmp_path):
    first = Vault(tmp_path / "a").digest("123-45-6789")
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert Vault(tmp_path / "a").digest("123-45-6789") == first
    assert Vault(tmp_path / "b").digest("123-45-6789") != first


def test_vault_save_and_load_round_trip_and_first_write_wins(tmp_path):
    vault = Vault(tmp_path / "vault")
    vault.save({"aaaaaaaaaaaaaaaa": "one"})
    vault.save({"aaaaaaaaaaaaaaaa": "other", "bbbbbbbbbbbbbbbb": "two"})
    assert vault.load(["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"]) == {
        "aaaaaaaaaaaaaaaa": "one",
        "bbbbbbbbbbbbbbbb": "two",
    }


def test_vault_load_handles_more_hexes_than_one_sqlite_statement_allows(tmp_path):
    vault = Vault(tmp_path / "vault")
    pairs = {f"{i:016x}": str(i) for i in range(2500)}
    vault.save(pairs)
    assert vault.load(pairs) == pairs


def test_vault_rejects_a_key_of_the_wrong_size(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "key").write_bytes(b"short")
    with pytest.raises(PiiVaultError):
        Vault(tmp_path / "vault")


def test_vault_on_a_path_that_is_a_file_raises_vault_error(tmp_path):
    (tmp_path / "vault").write_text("not a directory")
    with pytest.raises(PiiVaultError):
        Vault(tmp_path / "vault")


def test_vault_tightens_loose_permissions_on_an_existing_vault(tmp_path, capsys):
    vault_dir = tmp_path / "vault"
    Vault(vault_dir)
    os.chmod(vault_dir, 0o777)
    os.chmod(vault_dir / "key", 0o644)
    os.chmod(vault_dir / "vault.sqlite", 0o644)

    Vault(vault_dir)

    assert _mode(vault_dir) == 0o700
    assert _mode(vault_dir / "key") == 0o600
    assert _mode(vault_dir / "vault.sqlite") == 0o600
    err = capsys.readouterr().err
    assert str(vault_dir) in err and str(vault_dir / "key") in err
    assert "vault.sqlite" in err
    # No secret contents (the key bytes or any plaintext) in the warning.
    assert (vault_dir / "key").read_bytes().hex() not in err


def test_vault_refuses_a_symlinked_key(tmp_path):
    vault_dir = tmp_path / "vault"
    Vault(vault_dir)
    real_key = (vault_dir / "key").read_bytes()
    (vault_dir / "key").unlink()
    decoy = tmp_path / "decoy-key"
    decoy.write_bytes(real_key)
    (vault_dir / "key").symlink_to(decoy)

    with pytest.raises(PiiVaultError):
        Vault(vault_dir)


def test_vault_refuses_a_symlinked_database(tmp_path):
    vault_dir = tmp_path / "vault"
    Vault(vault_dir)
    (vault_dir / "vault.sqlite").unlink()
    decoy = tmp_path / "decoy.sqlite"
    decoy.touch()
    (vault_dir / "vault.sqlite").symlink_to(decoy)

    with pytest.raises(PiiVaultError):
        Vault(vault_dir)


def test_vault_dir_as_a_symlink_to_a_tight_directory_we_own_is_silent(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o700)
    link = tmp_path / "link"
    link.symlink_to(real)

    Vault(link)

    assert _mode(real) == 0o700
    assert "tightened" not in capsys.readouterr().err

    # Second start over the same symlink stays silent too.
    Vault(link)
    assert "tightened" not in capsys.readouterr().err


def test_vault_dir_as_a_symlink_to_a_loose_directory_tightens_the_target_once(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    Vault(link)
    capsys.readouterr()
    os.chmod(real, 0o777)

    Vault(link)
    err = capsys.readouterr().err
    assert "tightened" in err
    assert _mode(real) == 0o700

    Vault(link)
    assert "tightened" not in capsys.readouterr().err


def test_vault_key_as_a_fifo_is_refused_without_hanging(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    os.chmod(vault_dir, 0o700)
    os.mkfifo(vault_dir / "key")

    with pytest.raises(PiiVaultError):
        Vault(vault_dir)


def test_vault_database_as_a_fifo_is_refused(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    os.chmod(vault_dir, 0o700)
    os.mkfifo(vault_dir / "vault.sqlite")

    with pytest.raises(PiiVaultError):
        Vault(vault_dir)


def test_vault_owned_by_another_user_raises(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    Vault(vault_dir)
    real_euid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)

    with pytest.raises(PiiVaultError):
        Vault(vault_dir)


def test_vault_a_fresh_vault_still_works_after_the_ownership_checks(tmp_path):
    vault = Vault(tmp_path / "vault")
    vault.save({"aaaaaaaaaaaaaaaa": "value"})
    assert vault.load(["aaaaaaaaaaaaaaaa"]) == {"aaaaaaaaaaaaaaaa": "value"}


def test_settings_default_to_tier_one(monkeypatch):
    settings = Settings()
    assert settings.pii_filter_tier == 1
    assert settings.pii_extra_fields == {}


@pytest.mark.parametrize("tier", ["-1", "4"])
def test_settings_reject_out_of_range_tier(monkeypatch, tier):
    monkeypatch.setenv("PII_FILTER_TIER", tier)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_parse_extra_fields_json(monkeypatch):
    monkeypatch.setenv("PII_EXTRA_FIELDS", '{"PerPersonal": {"customString6": 2}}')
    assert Settings().pii_extra_fields == {"PerPersonal": {"customString6": 2}}


def test_settings_reject_extra_field_tier_outside_one_to_three(monkeypatch):
    monkeypatch.setenv("PII_EXTRA_FIELDS", '{"PerPersonal": {"customString6": 0}}')
    with pytest.raises(ValidationError):
        Settings()


def test_settings_reject_vault_inside_results_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "results" / "vault"))
    with pytest.raises(ValidationError, match="PII_VAULT_DIR"):
        Settings()


def test_settings_reject_vault_inside_results_dir_even_at_tier_zero(monkeypatch, tmp_path):
    # Production tenants are tier 3 whatever PII_FILTER_TIER says.
    monkeypatch.setenv("PII_FILTER_TIER", "0")
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "results" / "vault"))
    with pytest.raises(ValidationError, match="PII_VAULT_DIR"):
        Settings()


def _filter(tmp_path, tier=1, extra=None):
    return PiiFilter(tier, extra or {}, Vault(tmp_path / "vault"))


def _meta(entity):
    return {"__metadata": {"uri": "x", "type": f"SFOData.{entity}"}}


def _stored_rows(tmp_path):
    with sqlite3.connect(tmp_path / "vault" / "vault.sqlite") as db:
        return db.execute("SELECT COUNT(*) FROM token").fetchone()[0]


def test_national_id_is_tokenized_and_join_keys_and_look_alikes_are_kept(tmp_path):
    pii = _filter(tmp_path)
    record = {
        **_meta("PerNationalId"),
        "personIdExternal": "p1",
        "cardType": "SSN",
        "country": "USA",
        "nationalId": "123-45-6789",
    }
    [out], count = pii.tokenize_records([record])
    assert count == 1
    assert out["personIdExternal"] == "p1" and out["cardType"] == "SSN" and out["country"] == "USA"
    tier, hex_ = _TOKEN.fullmatch(out["nationalId"]).groups()
    assert tier == "1"
    assert pii.vault.load([hex_]) == {hex_: "123-45-6789"}
    assert record["nationalId"] == "123-45-6789", "input must not be mutated"


def test_same_value_gives_same_token_across_records_and_nested_expands(tmp_path):
    pii = _filter(tmp_path)
    records = [
        {**_meta("PerNationalId"), "nationalId": "123-45-6789"},
        {
            **_meta("PerPersonRelationship"),
            "relNationalIdNav": {
                "results": [{**_meta("PerNationalId"), "nationalId": "123-45-6789"}]
            },
        },
        {
            **_meta("PerPersonRelationship"),
            "single": {**_meta("PerNationalId"), "nationalId": "123-45-6789"},
        },
    ]
    out, count = pii.tokenize_records(records)
    tokens = {
        out[0]["nationalId"],
        out[1]["relNationalIdNav"]["results"][0]["nationalId"],
        out[2]["single"]["nationalId"],
    }
    assert count == 3 and len(tokens) == 1


def test_null_and_empty_values_are_left_alone(tmp_path):
    out, count = _filter(tmp_path).tokenize_records(
        [
            {**_meta("PerNationalId"), "nationalId": None},
            {**_meta("PerNationalId"), "nationalId": ""},
        ]
    )
    assert count == 0
    assert out[0]["nationalId"] is None and out[1]["nationalId"] == ""


def test_map_is_entity_scoped(tmp_path):
    records = [
        {**_meta("FOLocation"), "city": "Plant City"},
        {**_meta("PerAddressDEFLT"), "city": "Home Town"},
    ]
    out, _ = _filter(tmp_path, tier=2).tokenize_records(records)
    assert out[0]["city"] == "Plant City"
    assert _TOKEN.fullmatch(out[1]["city"]).group(1) == "2"


def test_star_entries_apply_to_unknown_entities_and_records_without_metadata(tmp_path):
    out, count = _filter(tmp_path).tokenize_records(
        [{**_meta("cust_Anything"), "nationalId": "A1"}, {"nationalId": "B2"}]
    )
    assert count == 2
    assert all(_TOKEN.fullmatch(record["nationalId"]) for record in out)


def test_tier_cutoff(tmp_path):
    record = {**_meta("PerPerson"), "dateOfBirth": "/Date(315532800000)/"}
    [kept], count = _filter(tmp_path, tier=1).tokenize_records([record])
    assert count == 0 and kept["dateOfBirth"] == "/Date(315532800000)/"
    [masked], _ = _filter(tmp_path, tier=2).tokenize_records([record])
    assert _TOKEN.fullmatch(masked["dateOfBirth"]).group(1) == "2"


def test_extra_fields_add_and_override(tmp_path):
    pii = _filter(
        tmp_path,
        tier=1,
        extra={"PerPersonal": {"customString6": 1}, "PerPerson": {"dateOfBirth": 1}},
    )
    out, count = pii.tokenize_records(
        [
            {**_meta("PerPersonal"), "customString6": "Race X"},
            {**_meta("PerPerson"), "dateOfBirth": "1980-01-01"},
        ]
    )
    assert count == 2
    assert _TOKEN.fullmatch(out[1]["dateOfBirth"]).group(1) == "1"


def test_binary_fields_are_redacted_and_not_stored(tmp_path):
    out, count = _filter(tmp_path, tier=3).tokenize_records(
        [{**_meta("Photo"), "photo": "iVBORw0KGgo="}]
    )
    assert count == 1
    assert out[0]["photo"] == "[PII-T3-REDACTED]"
    assert _stored_rows(tmp_path) == 0


def test_name_value_pairs_tokenize_the_value_by_name(tmp_path):
    type_ = {"__metadata": {"type": "Example.PropertyBagItem"}}
    out, count = _filter(tmp_path).tokenize_records(
        [
            {**type_, "Name": "nationalId", "Value": "123-45-6789"},
            {**type_, "Name": "Region", "Value": "EMEA"},
        ]
    )
    assert count == 1
    assert out[0]["Name"] == "nationalId" and _TOKEN.fullmatch(out[0]["Value"])
    assert out[1]["Value"] == "EMEA"


def test_non_string_values_are_tokenized_as_text(tmp_path):
    pii = _filter(tmp_path)
    [out], _ = pii.tokenize_records([{**_meta("PerNationalId"), "nationalId": 123456789}])
    hex_ = _TOKEN.fullmatch(out["nationalId"]).group(2)
    assert pii.vault.load([hex_]) == {hex_: "123456789"}


def _tenant_settings(monkeypatch, tmp_path, production, tier=None):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    if tier is not None:
        monkeypatch.setenv("PII_FILTER_TIER", tier)
    settings = Settings()
    if production is not None:
        TenantStore(settings.tenant_keys_dir).set_production("example-a", production)
    return settings


@pytest.mark.parametrize("tier", [None, "0", "1"])
def test_for_tenant_production_is_always_tier_three(monkeypatch, tmp_path, tier):
    pii = for_tenant(_tenant_settings(monkeypatch, tmp_path, True, tier), "example-a")
    assert pii.tier == 3 and (tmp_path / "vault" / "key").exists()


@pytest.mark.parametrize(("tier", "expected"), [(None, 1), ("2", 2), ("3", 3)])
def test_for_tenant_test_uses_pii_filter_tier_or_one(monkeypatch, tmp_path, tier, expected):
    pii = for_tenant(_tenant_settings(monkeypatch, tmp_path, False, tier), "example-a")
    assert pii.tier == expected


def test_for_tenant_test_at_tier_zero_is_none(monkeypatch, tmp_path):
    assert for_tenant(_tenant_settings(monkeypatch, tmp_path, False, "0"), "example-a") is None
    assert not (tmp_path / "vault").exists()


@pytest.mark.parametrize("tier", ["0", "3"])
def test_for_tenant_unset_raises_before_touching_the_vault(monkeypatch, tmp_path, tier):
    settings = _tenant_settings(monkeypatch, tmp_path, None, tier)
    with pytest.raises(TenantEnvironmentUnset) as caught:
        for_tenant(settings, "example-a")
    assert caught.value.company_id == "example-a"
    assert "example-a/tenant.json" in str(caught.value)
    assert not (tmp_path / "vault").exists()


def test_for_tenant_empty_company_id_reads_the_default_tenant(monkeypatch, tmp_path):
    monkeypatch.setenv("SF_COMPANY_ID", "example-a")
    assert for_tenant(_tenant_settings(monkeypatch, tmp_path, True), "").tier == 3
    monkeypatch.delenv("SF_COMPANY_ID")
    with pytest.raises(TenantEnvironmentUnset):
        for_tenant(Settings(), "")


def _ce(inner):
    return (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        "<queryResponse><numResults>1</numResults><hasMore>false</hasMore>"
        f"<person><person_id_external>p1</person_id_external>{inner}</person>"
        "</queryResponse></SOAP-ENV:Envelope>"
    )


def test_ce_national_id_is_tokenized_and_names_follow_the_tier(tmp_path):
    xml = _ce(
        "<national_id_card><card_type>SSN</card_type><national_id>123-45-6789</national_id></national_id_card>"
        "<personal_information><first_name>Ann</first_name></personal_information>"
    )
    out, count = _filter(tmp_path, tier=1).tokenize_xml(xml)
    assert count == 1
    assert "123-45-6789" not in out and "<card_type>SSN</card_type>" in out
    assert "<first_name>Ann</first_name>" in out
    assert "<person_id_external>p1</person_id_external>" in out
    assert "<numResults>1</numResults>" in out
    out3, _ = _filter(tmp_path, tier=3).tokenize_xml(xml)
    assert "Ann" not in out3


def test_ce_uses_nearest_mapped_ancestor_segment(tmp_path):
    xml = _ce(
        "<PaymentInformationV3><PaymentInformationDetailV3>"
        "<accountNumber>000123</accountNumber><accountOwner>Ann Owner</accountOwner>"
        "</PaymentInformationDetailV3></PaymentInformationV3>"
        "<dependent_information><person><personal_information>"
        "<first_name>Kid</first_name></personal_information></person></dependent_information>"
        "<job_information><city>Plant City</city></job_information>"
        "<address_information><city>Home Town</city></address_information>"
    )
    out, _ = _filter(tmp_path, tier=3).tokenize_xml(xml)
    assert "000123" not in out and "Kid" not in out and "Home Town" not in out
    assert "Ann Owner" not in out
    assert "<city>Plant City</city>" in out


def test_login_name_is_tier_two_in_ce_and_odata(tmp_path):
    # Tenants often set the login name to the work email.
    xml = _ce("<logon_user_name>ann@example.com</logon_user_name>")
    user = {**_meta("User"), "userId": "u1", "username": "ann@example.com"}
    out1, count1 = _filter(tmp_path, tier=1).tokenize_xml(xml)
    [rec1], _ = _filter(tmp_path, tier=1).tokenize_records([user])
    assert count1 == 0 and "ann@example.com" in out1
    assert rec1["username"] == "ann@example.com"
    out2, count2 = _filter(tmp_path, tier=2).tokenize_xml(xml)
    [rec2], _ = _filter(tmp_path, tier=2).tokenize_records([user])
    assert count2 == 1 and "ann@example.com" not in out2
    assert _TOKEN.fullmatch(rec2["username"]).group(1) == "2"
    assert rec2["userId"] == "u1"


def test_ce_empty_elements_are_left_alone(tmp_path):
    out, count = _filter(tmp_path).tokenize_xml(
        _ce("<national_id_card><national_id/></national_id_card>")
    )
    assert count == 0 and "<national_id/>" in out


def test_ce_doctype_is_rejected(tmp_path):
    with pytest.raises(etree.XMLSyntaxError):
        _filter(tmp_path).tokenize_xml('<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')


def _seed(tmp_path, value, tier=1):
    vault = Vault(tmp_path / "vault")
    hex_ = vault.digest(value)
    vault.save({hex_: value})
    return vault, f"[PII-T{tier}-{hex_}]"


def test_detokenize_substitutes_plaintext_and_reports_it(tmp_path):
    vault, token = _seed(tmp_path, "123-45-6789")
    text, subs = detokenize(f"nationalId eq '{token}'", vault)
    assert text == "nationalId eq '123-45-6789'"
    assert subs == {"123-45-6789": token}


def test_detokenize_doubles_quotes_inside_an_odata_string_literal(tmp_path):
    vault, token = _seed(tmp_path, "O'Brien", tier=3)
    text, subs = detokenize(f"lastName eq '{token}'", vault)
    assert text == "lastName eq 'O''Brien'"
    # The doubled-quote literal form, the raw plaintext, and its percent- and
    # HTML-encoded variants are all recorded, since SF may echo any of them
    # back in an error body.
    assert subs == {
        "O''Brien": token,
        "O'Brien": token,
        "O%27Brien": token,
        "O&#x27;Brien": token,
        "O&#39;Brien": token,
    }


def test_retokenize_hides_the_raw_plaintext_sf_echoes_back(tmp_path):
    # SF's error body echoes the raw value, not the doubled-quote OData
    # literal form that was actually sent on the wire.
    vault, token = _seed(tmp_path, "O'Brien", tier=3)
    _, subs = detokenize(f"lastName eq '{token}'", vault)
    body = "Invalid filter: lastName eq O'Brien"
    assert retokenize(body, subs) == f"Invalid filter: lastName eq {token}"


def test_retokenize_hides_encoded_echoes_of_the_plaintext(tmp_path):
    # SF may echo the request URL back in an error body encoded differently
    # than we sent it: a different quote() `safe`, quote_plus's '+' for
    # spaces, or HTML entities (with or without quotes escaped, numeric or
    # hex, lowercase percent-hex). Each form must still come back as the
    # token. (A value SF itself double-encodes is out of scope.)
    raw = "a/b c&d's"
    vault, token = _seed(tmp_path, raw, tier=3)
    _, subs = detokenize(f"lastName eq '{token}'", vault)

    variants = (
        quote(raw, safe=""),
        quote(raw),
        quote_plus(raw),
        html.escape(raw),
        html.escape(raw, quote=False),
        html.escape(raw).replace("&#x27;", "&#39;"),
        re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), quote(raw, safe="")),
    )
    for encoded in variants:
        assert encoded != raw  # otherwise this variant tests nothing
        body = f"Invalid filter: lastName eq {encoded}"
        out = retokenize(body, subs)
        assert out == f"Invalid filter: lastName eq {token}"
        assert raw not in out and encoded not in out


def test_detokenize_percent_encoded_token_yields_encoded_plaintext(tmp_path):
    vault, token = _seed(tmp_path, "A B/1")
    encoded = token.replace("[", "%5B").replace("]", "%5D")
    text, _ = detokenize(f"PerNationalId?$filter=nationalId eq '{encoded}'", vault)
    assert text == "PerNationalId?$filter=nationalId eq 'A%20B%2F1'"


def test_detokenize_unknown_token_raises_with_the_token(tmp_path):
    vault = Vault(tmp_path / "vault")
    with pytest.raises(PiiUnknownTokenError) as caught:
        detokenize("x eq '[PII-T1-0123456789abcdef]'", vault)
    assert caught.value.tokens == ["[PII-T1-0123456789abcdef]"]


def test_detokenize_without_tokens_is_a_no_op(tmp_path):
    assert detokenize("EmpJob?$select=userId", Vault(tmp_path / "vault")) == (
        "EmpJob?$select=userId",
        {},
    )


def test_retokenize_hides_echoed_plaintext_longest_first(tmp_path):
    body = "Invalid filter: nationalId eq '123-45-6789' or x eq '123-45'"
    out = retokenize(
        body, {"123-45": "[PII-T1-bbbbbbbbbbbbbbbb]", "123-45-6789": "[PII-T1-aaaaaaaaaaaaaaaa]"}
    )
    assert out == (
        "Invalid filter: nationalId eq '[PII-T1-aaaaaaaaaaaaaaaa]' or x eq '[PII-T1-bbbbbbbbbbbbbbbb]'"
    )
    assert retokenize(body, {}) == body


def test_reveal_counts_known_and_keeps_unknown_tokens(tmp_path):
    vault, token = _seed(tmp_path, "O'Brien", tier=3)
    text, replaced, unknown = reveal(f"Name: {token}; other: [PII-T1-0123456789abcdef]", vault)
    assert text == "Name: O'Brien; other: [PII-T1-0123456789abcdef]"
    assert replaced == 1 and unknown == ["[PII-T1-0123456789abcdef]"]


def test_detokenize_percent_encoded_quote_before_a_token_doubles_quotes(tmp_path):
    vault, token = _seed(tmp_path, "O'Brien", tier=3)
    encoded = token.replace("[", "%5B").replace("]", "%5D")
    text, subs = detokenize(f"lastName eq %27{encoded}%27", vault)
    assert text == "lastName eq %27O%27%27Brien%27"
    assert subs["O''Brien"] == subs["O'Brien"] == encoded


def test_detokenize_encode_percent_encodes_a_bracketed_token(tmp_path):
    vault, token = _seed(tmp_path, "a+b&c's@x.com", tier=2)
    text, subs = detokenize(f"$filter=emailAddress eq '{token}'", vault, encode=True)
    assert text == "$filter=emailAddress eq 'a%2Bb%26c%27%27s%40x.com'"
    assert subs["a+b&c's@x.com"] == subs["a+b&c''s@x.com"] == token


def test_uris_are_dropped_so_key_predicates_do_not_leak(tmp_path):
    uri = "https://api/odata/v2/EmpWorkPermit(country='USA',documentNumber='A1234567',userId='u1')"
    record = {
        "__metadata": {"uri": uri, "type": "SFOData.EmpWorkPermit", "media_src": uri + "/$value"},
        "userId": "u1",
        "documentNumber": "A1234567",
        "userNav": {"__deferred": {"uri": uri + "/userNav"}},
    }
    [out], _ = _filter(tmp_path).tokenize_records([record])
    assert "A1234567" not in json.dumps(out)
    assert out["__metadata"] == {"type": "SFOData.EmpWorkPermit"}
    assert out["userNav"] == {"__deferred": {}}
    assert record["__metadata"]["uri"] == uri, "input must not be mutated"


def test_expanded_collections_next_link_is_dropped(tmp_path):
    # An $expand-ed nav property paginates on its own and can carry a
    # "__next" URL embedding key values (PII) — unlike top-level paging,
    # nothing upstream of tokenize_records strips this one.
    record = {
        "__metadata": {"type": "SFOData.User"},
        "userId": "u1",
        "permissionRoleNav": {
            "results": [
                {"__metadata": {"type": "SFOData.PermissionRole"}, "id": "1"},
            ],
            "__next": "https://api/odata/v2/User('u1')/permissionRoleNav?$skip=1",
        },
    }
    [out], _ = _filter(tmp_path).tokenize_records([record])
    assert "__next" not in out["permissionRoleNav"]
    assert out["permissionRoleNav"]["results"][0]["id"] == "1"
