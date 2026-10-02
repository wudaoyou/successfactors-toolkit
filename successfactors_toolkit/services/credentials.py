"""Private-key resolution shared by the SFAPI and OData clients."""

import base64
import os
import re
from pathlib import Path

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError, check_key_path
from successfactors_toolkit.services.tenant_store import exact_case_path


def load_key_pem(path_override: str | None, settings: Settings, company_id: str) -> bytes:
    """Resolve private key PEM bytes for the given company.

    Priority (highest first):
    1. Per-request path override (SFAPIConnectionConfig.private_key_path) —
       restricted to files inside {tenant_keys_dir}/{company_id}/, see connection_policy.
    2. Tenant store dir: {tenant_keys_dir}/{company_id}/sf_private_key_{company_id}.pem
       — populated via POST /api/tenants/{company_id}/keypair.
    3. SF_PRIVATE_KEY_PEM_<COMPANY_ID> env var  (per-company base64 PEM, for CI/CD)
    4. SF_PRIVATE_KEY_PEM env var               (fallback base64 PEM, single-tenant)
    5. SF_PRIVATE_KEY_PATH with {company_id} placeholder (Docker Secrets file path)
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", company_id):
        raise ConnectionPolicyError("Invalid company_id for credential resolution.")
    # Tenant files match company_id case exactly (see exact_case_path): SF
    # company IDs are sent as given, so "DEMO" never reads tenants/demo/.
    if path_override:
        # Per-request overrides are caller input on the REST path: the server
        # may only read the key directory of the tenant the request names.
        key = check_key_path(path_override, settings, company_id)
        base = Path(settings.tenant_keys_dir)
        tenant_dir = (base / company_id).resolve()
        if (
            exact_case_path(base, company_id) is None
            or exact_case_path(tenant_dir, *key.relative_to(tenant_dir).parts) is None
        ):
            raise ConnectionPolicyError(
                f"private_key_path {path_override!r} must be inside the key directory of tenant {company_id!r}."
            )
        return key.read_bytes()
    tenant_key = exact_case_path(
        Path(settings.tenant_keys_dir), company_id, f"sf_private_key_{company_id}.pem"
    )
    if tenant_key is not None:
        return tenant_key.read_bytes()
    company_pem = os.environ.get(f"SF_PRIVATE_KEY_PEM_{company_id.upper()}")
    if company_pem:
        return base64.b64decode(company_pem)
    pem = settings.sf_private_key_pem.get_secret_value()
    if pem:
        return base64.b64decode(pem)
    path = settings.sf_private_key_path.format(company_id=company_id.lower())
    if not path:
        raise ValueError("No private key configured for this tenant.")
    return Path(path).read_bytes()
