import pytest
from pydantic import ValidationError

from successfactors_toolkit.config import Settings


def test_startup_needs_an_existing_systems_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("SYSTEMS_DIR")
    with pytest.raises(ValidationError, match="SYSTEMS_DIR"):
        Settings(_env_file=None)
    monkeypatch.setenv("SYSTEMS_DIR", str(tmp_path / "missing"))
    with pytest.raises(ValidationError, match="SYSTEMS_DIR"):
        Settings(_env_file=None)


@pytest.mark.parametrize("name", ["SF_HOST", "SF_COMPANY_ID", "TENANT_KEYS_DIR", "PII_FILTER_TIER"])
def test_old_settings_are_gone(name):
    assert name.lower() not in Settings.model_fields
