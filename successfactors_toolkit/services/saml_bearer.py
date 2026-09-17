"""
OAuth2 SAML Bearer Assertion flow for SAP SuccessFactors.

Builds a signed SAML 2.0 assertion and exchanges it for an access token
at the SF token endpoint. Mirrors the SAP CPI "OAuth2 SAML Bearer Assertion"
credential type.
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.hazmat.primitives import serialization
from lxml import etree
from signxml import XMLSigner, methods

from successfactors_toolkit.config import Settings
from successfactors_toolkit.services.connection_policy import check_token_url

_SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
_AUDIENCE = "www.successfactors.com"
_AUTHN_CTX = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
_NAMEID_FMT = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
_BEARER_METHOD = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:saml2-bearer"


def _dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sub(parent: etree._Element, tag: str, **attribs: str) -> etree._Element:
    el = etree.SubElement(parent, f"{{{_SAML_NS}}}{tag}")
    for k, v in attribs.items():
        el.set(k, v)
    return el


def _build_assertion(
    client_key: str,
    user_id: str,
    token_url: str,
    assertion_id: str,
    now: datetime,
    expire: datetime,
) -> etree._Element:
    nsmap = {"saml2": _SAML_NS}
    root = etree.Element(f"{{{_SAML_NS}}}Assertion", nsmap=nsmap)
    root.set("ID", assertion_id)
    root.set("IssueInstant", _dt(now))
    root.set("Version", "2.0")

    _sub(root, "Issuer").text = client_key

    subject = _sub(root, "Subject")
    _sub(subject, "NameID", Format=_NAMEID_FMT).text = user_id
    sc = _sub(subject, "SubjectConfirmation", Method=_BEARER_METHOD)
    _sub(sc, "SubjectConfirmationData", NotOnOrAfter=_dt(expire), Recipient=token_url)

    cond = _sub(root, "Conditions", NotBefore=_dt(now), NotOnOrAfter=_dt(expire))
    _sub(_sub(cond, "AudienceRestriction"), "Audience").text = _AUDIENCE

    authn = _sub(root, "AuthnStatement", AuthnInstant=_dt(now))
    _sub(_sub(authn, "AuthnContext"), "AuthnContextClassRef").text = _AUTHN_CTX

    attr_stmt = _sub(root, "AttributeStatement")
    attr = _sub(attr_stmt, "Attribute", Name="Use")
    _sub(attr, "AttributeValue").text = "api"

    return root


async def fetch_token(
    http_client: httpx.AsyncClient,
    client_key: str,
    user_id: str,
    company_id: str,
    token_url: str,
    private_key_pem: bytes,
    settings: Settings,
    timeout: int = 30,
) -> str:
    # The signed assertion is a bearer credential: never POST it to a host the
    # connection policy does not allow. The policy has to come from the caller's
    # own Settings — reading the process-global ones here would check an
    # override against a configuration this client never agreed to.
    check_token_url(token_url, settings)
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)

    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=10)
    assertion_id = f"_{uuid.uuid4().hex}"

    assertion_el = _build_assertion(client_key, user_id, token_url, assertion_id, now, expire)

    signer = XMLSigner(
        method=methods.enveloped,
        digest_algorithm="sha256",
        signature_algorithm="rsa-sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    signed = signer.sign(assertion_el, key=private_key, reference_uri=f"#{assertion_id}")

    assertion_b64 = base64.b64encode(etree.tostring(signed)).decode("ascii")

    resp = await http_client.post(
        token_url,
        data={
            "grant_type": _GRANT_TYPE,
            "client_id": client_key,
            "company_id": company_id,
            "assertion": assertion_b64,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]
