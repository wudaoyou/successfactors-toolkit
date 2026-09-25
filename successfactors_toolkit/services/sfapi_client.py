"""SFAPI SOAP client using OAuth2 SAML Bearer Assertion.

SAP SuccessFactors SFAPI requires a three-step authentication flow when called
directly (outside SAP CPI):

  1. Obtain an OAuth2 access token via SAML Bearer Assertion (POST /oauth/token).
  2. Call SFAPI login() with the access token in the HTTP Authorization header
     and EMPTY companyId/username/password fields in the SOAP body. The response
     contains a <sessionId>.
  3. Use that sessionId as a JSESSIONID cookie on all subsequent SOAP calls.

Reference: SAP SuccessFactors HCM Suite SFAPI Developer Guide, section
"Authentication Using OAuth 2.0".

WSSE BinarySecurityToken (the OData pattern) does NOT work for SFAPI — the
server responds with INVALID_SESSION. The login() + JSESSIONID flow is the
only documented way.
"""

import asyncio
import re
from xml.sax.saxutils import escape

import httpx

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.common import SFAPIConnectionConfig
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.connection_policy import check_host
from successfactors_toolkit.services.credentials import load_key_pem
from successfactors_toolkit.services.tenant_store import TenantStore

_SOAP_ENVELOPE = """\
<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:urn="urn:sfobject.sfapi.successfactors.com">
  <soapenv:Header/>
  <soapenv:Body>
{body}
  </soapenv:Body>
</soapenv:Envelope>"""

# Per SAP docs: the body is required but all three fields MUST be empty when
# authenticating via OAuth Bearer header.
_LOGIN_BODY = """\
    <urn:login>
      <urn:credential>
        <urn:companyId></urn:companyId>
        <urn:username></urn:username>
        <urn:password></urn:password>
      </urn:credential>
    </urn:login>"""

_SESSION_ID_RE = re.compile(r"<sessionId>([^<]+)</sessionId>")
_INVALID_SESSION_MARKER = "INVALID_SESSION"


def _eff(override: str | None, default: str) -> str:
    return override if override is not None else default


class SFAPIClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        # JSESSIONID cache keyed by (host, company_id, user_id, client_key).
        # SFAPI sessions are long-lived (typically 60 min) but expire eventually;
        # _post invalidates on INVALID_SESSION and retries once.
        self._sessions: dict[tuple[str, str, str, str], str] = {}
        self._login_lock = asyncio.Lock()

    def _resolve(self, conn: SFAPIConnectionConfig | None) -> dict:
        s = self._settings
        c = conn or SFAPIConnectionConfig()
        company_id = _eff(c.company_id, s.sf_company_id)
        # Per-request override, then the tenant's {company_id}.json, then SF_*.
        t = TenantStore(s.tenant_keys_dir).connection(company_id)
        return {
            # Overrides are attacker-controlled on the REST path: only hosts the
            # policy allows may end up in _endpoint()'s URL.
            "host": check_host(_eff(c.host, t.get("host", s.sf_host)), s),
            "client_key": _eff(c.client_key, t.get("client_key", s.sf_client_key)),
            "user_id": _eff(c.user_id, t.get("user_id", s.sf_user_id)),
            "company_id": company_id,
            "token_url": _eff(c.token_url, t.get("token_url", s.sf_token_url)),
            "private_key_pem": load_key_pem(c.private_key_path, s, company_id),
        }

    @staticmethod
    def _session_key(r: dict) -> tuple[str, str, str, str]:
        return (r["host"], r["company_id"], r["user_id"], r["client_key"])

    @staticmethod
    def _endpoint(r: dict) -> str:
        return f"https://{r['host']}/sfapi/v1/soap"

    async def _login(self, r: dict) -> str:
        """Exchange OAuth2 token + SFAPI login() for a JSESSIONID."""
        token = await saml_bearer.fetch_token(
            http_client=self._client,
            client_key=r["client_key"],
            user_id=r["user_id"],
            company_id=r["company_id"],
            token_url=r["token_url"],
            private_key_pem=r["private_key_pem"],
            settings=self._settings,
            timeout=self._settings.request_timeout,
        )
        envelope = _SOAP_ENVELOPE.format(body=_LOGIN_BODY)
        resp = await self._client.post(
            self._endpoint(r),
            content=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": "login",
                "Authorization": f"Bearer {token}",
            },
            timeout=self._settings.request_timeout,
        )
        m = _SESSION_ID_RE.search(resp.text)
        if not m:
            raise RuntimeError(f"SFAPI login failed (HTTP {resp.status_code}): {resp.text[:500]}")
        return m.group(1)

    async def _ensure_session(self, r: dict) -> str:
        key = self._session_key(r)
        if key in self._sessions:
            return self._sessions[key]
        async with self._login_lock:
            if key not in self._sessions:  # double-check
                self._sessions[key] = await self._login(r)
            return self._sessions[key]

    async def _post(
        self,
        soap_action: str,
        body: str,
        conn: SFAPIConnectionConfig | None,
    ) -> dict[str, str | int]:
        r = self._resolve(conn)
        envelope = _SOAP_ENVELOPE.format(body=body).encode("utf-8")
        endpoint = self._endpoint(r)
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": soap_action}
        key = self._session_key(r)

        async def _do_request(session_id: str) -> httpx.Response:
            return await self._client.post(
                endpoint,
                content=envelope,
                headers={**headers, "Cookie": f"JSESSIONID={session_id}"},
                timeout=self._settings.request_timeout,
            )

        session_id = await self._ensure_session(r)
        resp = await _do_request(session_id)

        # Session may have expired server-side; invalidate and retry once.
        if resp.status_code >= 400 and _INVALID_SESSION_MARKER in resp.text:
            self._sessions.pop(key, None)
            session_id = await self._ensure_session(r)
            resp = await _do_request(session_id)

        return {"status_code": resp.status_code, "headers": dict(resp.headers), "body": resp.text}

    @staticmethod
    def _render_params(params: list[tuple[str, str]] | None) -> str:
        """Render zero-or-more <urn:param> elements for the query body."""
        if not params:
            return ""
        return "".join(
            f"      <urn:param>\n"
            f"        <urn:name>{escape(name)}</urn:name>\n"
            f"        <urn:value>{escape(value)}</urn:value>\n"
            f"      </urn:param>\n"
            for name, value in params
        )

    async def query(
        self,
        query_string: str,
        conn: SFAPIConnectionConfig | None = None,
        params: list[tuple[str, str]] | None = None,
    ) -> dict[str, str | int]:
        """Send a <urn:query>.

        ``params`` is a list of (name, value) tuples rendered as ``<urn:param>``
        children of the query body — e.g. ``[("maxRows", "100"), ("resultOptions",
        "xsd")]``. See the SAP CompoundEmployee API guide for the full parameter
        list.
        """
        body = (
            "    <urn:query>\n"
            f"      <urn:queryString>{escape(query_string)}</urn:queryString>\n"
            f"{self._render_params(params)}"
            "    </urn:query>"
        )
        return await self._post("query", body, conn)

    async def query_more(
        self,
        query_session_id: str,
        conn: SFAPIConnectionConfig | None = None,
    ) -> dict[str, str | int]:
        """Fetch the next page using the querySessionId from a previous response.

        Per the SAP SFAPI WSDL the operation parameter is ``querySessionId``
        (an earlier revision of this client used ``querySession``, which SF
        silently rejected with INVALID_SFQL / no session).
        """
        body = (
            "    <urn:queryMore>\n"
            f"      <urn:querySessionId>{escape(query_session_id)}</urn:querySessionId>\n"
            "    </urn:queryMore>"
        )
        return await self._post("queryMore", body, conn)
