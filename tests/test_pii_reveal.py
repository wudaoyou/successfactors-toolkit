"""successfactors-pii-reveal: local, file-to-file, never creates a vault."""

import stat

import pytest

from successfactors_toolkit import pii_reveal
from successfactors_toolkit.config import get_settings
from successfactors_toolkit.services.pii_filter import Vault


def _vault(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "vault"))
    get_settings.cache_clear()
    vault = Vault(tmp_path / "vault")
    hex_ = vault.digest("O'Brien")
    vault.save({hex_: "O'Brien"})
    return f"[PII-T3-{hex_}]"


def test_reveal_writes_an_owner_only_sibling_file(monkeypatch, tmp_path):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(f"Name: {token}\n", encoding="utf-8")
    assert pii_reveal.main([str(report)]) == 0
    revealed = tmp_path / "report.revealed.md"
    assert revealed.read_text(encoding="utf-8") == "Name: O'Brien\n"
    assert stat.S_IMODE(revealed.stat().st_mode) == 0o600


def test_reveal_to_stdout(monkeypatch, tmp_path, capsys):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(token, encoding="utf-8")
    assert pii_reveal.main([str(report), "-o", "-"]) == 0
    assert capsys.readouterr().out == "O'Brien"


def test_reveal_exits_1_when_unknown_tokens_remain(monkeypatch, tmp_path):
    _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text("[PII-T1-0123456789abcdef]", encoding="utf-8")
    assert pii_reveal.main([str(report)]) == 1


def test_reveal_rejects_non_utf8_input(monkeypatch, tmp_path):
    _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_bytes(b"\xff\xfe not utf-8")
    assert pii_reveal.main([str(report)]) == 2


def test_reveal_without_a_vault_fails_and_creates_none(monkeypatch, tmp_path):
    monkeypatch.setenv("PII_VAULT_DIR", str(tmp_path / "missing"))
    get_settings.cache_clear()
    report = tmp_path / "report.md"
    report.write_text("x", encoding="utf-8")
    assert pii_reveal.main([str(report)]) == 2
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    ("name", "value"), [("PII_FILTER_TIER", "9"), ("PII_EXTRA_FIELDS", "{bad")]
)
def test_reveal_exits_2_on_invalid_settings(monkeypatch, tmp_path, capsys, name, value):
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    report = tmp_path / "report.md"
    report.write_text("x", encoding="utf-8")
    assert pii_reveal.main([str(report)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1  # one-line error, no traceback
    assert "error:" in err
