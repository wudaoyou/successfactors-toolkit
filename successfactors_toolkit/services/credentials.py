"""Private-key resolution shared by the SFAPI and OData clients."""

import base64
import os
import re
from pathlib import Path

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.connection_policy import check_key_path


def load_key_pem(path_override: str | None, settings: Settings, company_id: str) -> bytes:
    """Resolve private key PEM bytes for the given company.

    Priority (highest first):
    1. Per-request path override (SFAPIConnectionConfig.private_key_path) —
       restricted to files inside {tenant_keys_dir}, see connection_policy.
    2. Tenant store dir: {tenant_keys_dir}/{company_id}/sf_private_key_{company_id}.pem
       — populated via POST /api/tenants/{company_id}/keypair.
    3. SF_PRIVATE_KEY_PEM_<COMPANY_ID> env var  (per-company base64 PEM, for CI/CD)
    4. SF_PRIVATE_KEY_PEM env var               (fallback base64 PEM, single-tenant)
    5. SF_PRIVATE_KEY_PATH with {company_id} placeholder (Docker Secrets file path)
    """
    if company_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", company_id):
        raise ValueError("Invalid company_id for credential resolution.")
    if path_override:
        # Per-request overrides are attacker-controlled on the REST path: the
        # server may only read keys out of the tenant store.
        return check_key_path(path_override, settings).read_bytes()
    tenant_key = Path(settings.tenant_keys_dir) / company_id / f"sf_private_key_{company_id}.pem"
    if tenant_key.exists():
        return tenant_key.read_bytes()
    company_pem = os.environ.get(f"SF_PRIVATE_KEY_PEM_{company_id.upper()}")
    if company_pem:
        return base64.b64decode(company_pem)
    if settings.sf_private_key_pem:
        return base64.b64decode(settings.sf_private_key_pem)
    path = settings.sf_private_key_path.format(company_id=company_id.lower())
    if not path:
        raise ValueError("No private key configured for this tenant.")
    return Path(path).read_bytes()
