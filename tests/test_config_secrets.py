"""Secrets in Settings stay out of validation errors, reprs and dumps."""

import pytest
from pydantic import ValidationError

from successfactors_toolkit.config import Settings

SECRET_FIELDS = ["admin_api_key", "api_key"]


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_startup_validation_error_does_not_echo_a_secret(monkeypatch, tmp_path, field):
    # The vault/results check is a model-level error, so pydantic reports the
    # whole input dict as input_value; that is where a secret would leak.
    secret = "leaked-synthetic-secret-0123456789"
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            **{field: secret},
            results_dir=tmp_path / "results",
            pii_vault_dir=tmp_path / "results" / "vault",
        )
    message = str(exc.value)
    assert "PII_VAULT_DIR must not be inside RESULTS_DIR" in message
    assert "input_value" not in message
    assert "leaked" not in message


def test_secrets_are_masked_in_repr_and_dump():
    settings = Settings(
        _env_file=None,
        admin_api_key="admin-secret",
        api_key="api-secret",
    )
    shown = repr(settings) + str(settings.model_dump())
    for secret in ("admin-secret", "api-secret"):
        assert secret not in shown


@pytest.mark.parametrize(
    ("env", "field"),
    [
        ("ADMIN_API_KEY", "admin_api_key"),
        ("API_KEY", "api_key"),
    ],
)
def test_env_names_and_values_are_unchanged(monkeypatch, env, field):
    monkeypatch.setenv(env, "synthetic-value")
    assert getattr(Settings(_env_file=None), field).get_secret_value() == "synthetic-value"


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_unset_secret_is_empty(field):
    assert getattr(Settings(_env_file=None), field).get_secret_value() == ""
