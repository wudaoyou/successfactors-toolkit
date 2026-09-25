"""successfactors-pii-reveal: local, stdout-by-default, never creates a vault."""

import os
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


def test_reveal_defaults_to_stdout_and_writes_no_file(monkeypatch, tmp_path, capsys):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(f"Name: {token}\n", encoding="utf-8")
    assert pii_reveal.main([str(report)]) == 0
    assert capsys.readouterr().out == "Name: O'Brien\n"
    assert not (tmp_path / "report.revealed.md").exists()


def test_reveal_to_stdout(monkeypatch, tmp_path, capsys):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(token, encoding="utf-8")
    assert pii_reveal.main([str(report), "-o", "-"]) == 0
    assert capsys.readouterr().out == "O'Brien"


def test_reveal_to_explicit_output_file(monkeypatch, tmp_path):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(f"Name: {token}\n", encoding="utf-8")
    out = tmp_path / "private" / "out.md"
    out.parent.mkdir()
    assert pii_reveal.main([str(report), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "Name: O'Brien\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_reveal_refuses_a_symlink_output_and_leaves_target_untouched(monkeypatch, tmp_path):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(token, encoding="utf-8")
    target = tmp_path / "vault-key"
    target.write_text("secret-key-material", encoding="utf-8")
    link = tmp_path / "out.md"
    link.symlink_to(target)
    assert pii_reveal.main([str(report), "-o", str(link)]) == 2
    assert target.read_text(encoding="utf-8") == "secret-key-material"
    assert link.is_symlink()


def test_reveal_refuses_a_directory_output(monkeypatch, tmp_path):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(token, encoding="utf-8")
    out = tmp_path / "adir"
    out.mkdir()
    assert pii_reveal.main([str(report), "-o", str(out)]) == 2
    assert out.is_dir()


def test_reveal_refuses_a_fifo_output(monkeypatch, tmp_path):
    token = _vault(monkeypatch, tmp_path)
    report = tmp_path / "report.md"
    report.write_text(token, encoding="utf-8")
    out = tmp_path / "afifo"
    os.mkfifo(out)
    assert pii_reveal.main([str(report), "-o", str(out)]) == 2
    assert stat.S_ISFIFO(out.stat().st_mode)


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
