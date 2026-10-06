import pytest
from pydantic import ValidationError

from successfactors_toolkit.config import Settings


def test_startup_needs_an_existing_systems_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("SYSTEMS_DIR")
    with pytest.raises(ValidationError, match="SYSTEMS_DIR"):
        Settings(_env_file=None)
    monkeypatch.setenv("SYSTEMS_DIR", "")
    with pytest.raises(ValidationError, match="SYSTEMS_DIR"):
        Settings(_env_file=None)
    monkeypatch.setenv("SYSTEMS_DIR", str(tmp_path / "missing"))
    with pytest.raises(ValidationError, match="SYSTEMS_DIR"):
        Settings(_env_file=None)


@pytest.mark.parametrize("name", ["PII_FILTER_TIER", "SF_HOST", "TENANT_KEYS_DIR"])
def test_startup_refuses_a_removed_variable(monkeypatch, tmp_path, name):
    monkeypatch.setenv("SYSTEMS_DIR", str(tmp_path))
    monkeypatch.setenv(name, "3")
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None)


def test_a_kept_variable_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEMS_DIR", str(tmp_path))
    monkeypatch.setenv("SF_ALLOWED_HOSTS", '["sf.example.invalid"]')
    assert Settings(_env_file=None).sf_allowed_hosts == ["sf.example.invalid"]


def test_startup_refuses_a_removed_variable_in_the_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEMS_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PII_FILTER_TIER=3\n")
    with pytest.raises(ValidationError, match="PII_FILTER_TIER"):
        Settings()
