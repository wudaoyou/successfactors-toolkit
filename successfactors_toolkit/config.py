from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SAP SuccessFactors host, e.g. "api4preview.sapsf.com"
    sf_host: str = "example.invalid"
    # Extra hosts a per-request connection override may point at, on top of
    # sf_host and the SAP datacenter domains. See services/connection_policy.py.
    sf_allowed_hosts: list[str] = []

    # ── EC SFAPI (SOAP) — OAuth2 SAML Bearer Assertion ───────────────────────
    sf_client_key: str = ""  # API key registered in SF Admin
    sf_user_id: str = ""  # Technical user (CN from key pair certificate)
    sf_company_id: str = ""
    sf_token_url: str = ""  # https://{host}/oauth/token
    # Private key — set exactly one of the two options below:
    sf_private_key_path: str = ""  # Path to PEM file (Docker Secret: /run/secrets/sf_private_key)
    sf_private_key_pem: str = ""  # Base64-encoded PEM content (for CI/CD env vars)

    # ── OData ─────────────────────────────────────────────────────────────────
    # OData and SFAPI share the same OAuth2 SAML Bearer flow against the same
    # /oauth/token endpoint (Dev Guide §2.3). They use the SAME OAuth2 client
    # (sf_client_key / sf_user_id / sf_company_id / sf_token_url / tenant key).
    # Only the OData REST version is OData-specific.
    sf_odata_version: str = "v2"

    # ── Tenant management API ─────────────────────────────────────────────────
    # Directory where per-tenant key+cert subdirectories live. Each subdirectory
    # is named by company_id and contains exactly:
    #   sf_private_key_<company_id>.pem    (mode 600)
    #   sf_saml_signing_<company_id>.crt   (mode 644)
    # Populated via POST /api/tenants/{company_id}/keypair.
    tenant_keys_dir: str = "./tenants"

    # API key required to call POST/DELETE on /api/tenants/*. Set to a strong
    # random value in production. Endpoints reject the request with 401 if the
    # X-Admin-Key header does not match. If left empty, admin endpoints refuse
    # all requests (safe default).
    admin_api_key: str = ""
    api_key: str = ""
    cors_origins: list[str] = []

    # ── PII tokenization (MCP only) ───────────────────────────────────────────
    # Values of mapped fields reach the model as [PII-T<tier>-<hex>] tokens;
    # the plaintext stays in the vault. See services/pii_filter.py.
    # 0 = off; N = tokenize every field whose tier <= N.
    pii_filter_tier: int = Field(default=1, ge=0, le=3)
    # Tenant-specific additions, e.g. {"PerPersonal": {"customString6": 2}}.
    pii_extra_fields: dict[str, dict[str, Annotated[int, Field(ge=1, le=3)]]] = {}
    # HMAC key + token vault. Must persist, and must stay out of RESULTS_DIR
    # (the model reads that directory).
    pii_vault_dir: Path = Path("pii_vault")

    # ── General ───────────────────────────────────────────────────────────────
    request_timeout: int = 120

    # Directory for payloads written to disk instead of returned in-band: the
    # MCP tools write under {results_dir}/mcp/. Relative paths resolve against
    # the working directory, so an MCP host that starts the server elsewhere
    # should set RESULTS_DIR to an absolute path.
    results_dir: Path = Path("results")

    @model_validator(mode="after")
    def _vault_outside_results(self) -> "Settings":
        if self.pii_filter_tier and self.pii_vault_dir.resolve().is_relative_to(
            self.results_dir.resolve()
        ):
            raise ValueError("PII_VAULT_DIR must not be inside RESULTS_DIR")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
