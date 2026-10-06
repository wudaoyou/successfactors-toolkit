from pydantic import BaseModel, Field


class SFAPIConnectionConfig(BaseModel):
    """Per-request connection override for EC SFAPI (SOAP + OAuth2 SAML Bearer)."""

    host: str | None = Field(
        default=None,
        description="SF host, e.g. api4preview.sapsf.com. Must be a SAP datacenter host or "
        "listed in SF_ALLOWED_HOSTS.",
    )
    client_key: str | None = Field(
        default=None,
        description="API key from SF OAuth2 Client Applications. Must match the system's "
        "configured client key; it can only be supplied where none is configured.",
    )
    user_id: str | None = Field(
        default=None,
        description="Technical user (CN of the key pair certificate). Must match the "
        "system's configured user; it can only be supplied where none is configured.",
    )
    system: str | None = Field(
        default=None,
        description="System name under SYSTEMS_DIR; empty = the only successfactors system.",
    )
    token_url: str | None = Field(
        default=None, description="https:// URL on an allowed host (see `host`)."
    )
    private_key_path: str | None = Field(
        default=None,
        description="Path to an RSA private key PEM inside SYSTEMS_DIR/<system>/",
    )


class ODataConnectionConfig(BaseModel):
    """Per-request connection override for OData API (OAuth2 SAML Bearer)."""

    host: str | None = Field(
        default=None,
        description="SF host, e.g. api4preview.sapsf.com. Must be a SAP datacenter host or "
        "listed in SF_ALLOWED_HOSTS.",
    )
    odata_version: str | None = Field(default=None, description="'v2' or 'v4'")
    client_key: str | None = Field(
        default=None,
        description="API key from SF OAuth2 Client Applications. Must match the system's "
        "configured client key; it can only be supplied where none is configured.",
    )
    user_id: str | None = Field(
        default=None,
        description="Technical user (CN of the key pair certificate). Must match the "
        "system's configured user; it can only be supplied where none is configured.",
    )
    system: str | None = Field(
        default=None,
        description="System name under SYSTEMS_DIR; empty = the only successfactors system.",
    )
    token_url: str | None = Field(
        default=None, description="https:// URL on an allowed host (see `host`)."
    )
    private_key_path: str | None = Field(
        default=None,
        description="Path to an RSA private key PEM inside SYSTEMS_DIR/<system>/",
    )
    # OAuth Bearer tokens are not vulnerable to CSRF; SAP CSRF tokens apply to
    # session-cookie auth. Default off; flip on for tenants that enforce it.
    csrf_protected: bool = Field(
        default=False, description="Fetch X-CSRF-Token before mutating requests"
    )
