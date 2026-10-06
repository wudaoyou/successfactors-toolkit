import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Settings that moved into SYSTEMS_DIR/<name>/<name>.json. A removed variable
# that is still set would be silently ignored (PII_FILTER_TIER=3 would drop a
# system to tier 1), so startup refuses instead.
_REMOVED_VARIABLES = frozenset(
    {
        "PII_FILTER_TIER",
        "PII_EXTRA_FIELDS",
        "TENANT_KEYS_DIR",
        "SF_COMPANY_ID",
        "SF_HOST",
        "SF_TOKEN_URL",
        "SF_CLIENT_KEY",
        "SF_USER_ID",
        "SF_ODATA_VERSION",
    }
)


class Settings(BaseSettings):
    # hide_input_in_errors: a startup ValidationError (MCP prints it to stderr,
    # where the AI host can read it) must not echo the input values.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        # SYSTEMS_DIR= (empty) is unset, not Path(".").
        env_ignore_empty=True,
    )

    # Extra hosts a system file's "host" / "token_url" may name, on top of the
    # SAP datacenter domains. See services/connection_policy.py.
    sf_allowed_hosts: list[str] = []

    # One directory per system: SYSTEMS_DIR/<name>/<name>.json ("type",
    # "production", ...) beside that system's secret files.
    systems_dir: Path | None = None

    # API key required to call POST/DELETE on /api/systems/*. Set to a strong
    # random value in production. Endpoints reject the request with 401 if the
    # X-Admin-Key header does not match. If left empty, admin endpoints refuse
    # all requests (safe default).
    admin_api_key: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    cors_origins: list[str] = []

    # ── PII tokenization (MCP only) ───────────────────────────────────────────
    # Values of mapped fields reach the model as [PII-T<tier>-<hex>] tokens;
    # the plaintext stays in the vault. The tier and extra fields are per
    # system, in <name>.json. See services/pii_filter.py.
    # HMAC key + token vault. Must persist, and must stay out of RESULTS_DIR
    # (the model reads that directory).
    pii_vault_dir: Path = Path("pii_vault")

    # ── General ───────────────────────────────────────────────────────────────
    request_timeout: int = 120
    # Bounds on memory and wall-clock per call; a call that passes one fails
    # with an error rather than returning a shortened result. See
    # services/http_limits.py.
    # One upstream response.
    max_response_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    # Response data gathered across all pages of one extract.
    max_extract_bytes: int = Field(default=500 * 1024 * 1024, ge=1)
    # Duration of one multi-page extract.
    max_extract_seconds: int = Field(default=1800, ge=1)
    # Distinct values in one extract-by-filter-in call.
    max_filter_values: int = Field(default=10_000, ge=1)

    # Directory for payloads written to disk instead of returned in-band: the
    # MCP tools write under {results_dir}/mcp/. Relative paths resolve against
    # the working directory, so an MCP host that starts the server elsewhere
    # should set RESULTS_DIR to an absolute path.
    results_dir: Path = Path("results")
    # Files under {results_dir}/mcp/ older than this many days are deleted
    # when the next one is written. 0 keeps them forever.
    results_retention_days: int = Field(default=7, ge=0)

    @model_validator(mode="after")
    def _vault_outside_results(self) -> "Settings":
        # Production systems always use the vault.
        if self.pii_vault_dir.resolve().is_relative_to(self.results_dir.resolve()):
            raise ValueError("PII_VAULT_DIR must not be inside RESULTS_DIR")
        return self

    @model_validator(mode="after")
    def _no_removed_variables(self) -> "Settings":
        removed = sorted(
            name
            for name in map(str.upper, os.environ)
            if name in _REMOVED_VARIABLES or name.startswith("SF_PRIVATE_KEY_")
        )
        if removed:
            raise ValueError(
                f"{', '.join(removed)} no longer {'has' if len(removed) == 1 else 'have'} an "
                "effect: the setting now lives in SYSTEMS_DIR/<name>/<name>.json "
                "(docs/CONNECT.md). Unset the variable."
            )
        return self

    @model_validator(mode="after")
    def _systems_dir_exists(self) -> "Settings":
        if self.systems_dir is None or not self.systems_dir.is_dir():
            raise ValueError(
                "SYSTEMS_DIR must name an existing directory with one subdirectory per "
                'system: <name>/<name>.json holding "type" and "production", beside '
                "that system's key files (docs/MCP_SERVER.md)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
