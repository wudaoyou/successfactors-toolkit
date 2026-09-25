"""Tiered PII tokenization for everything the MCP server hands the model.

Values of mapped fields become deterministic tokens, ``[PII-T<tier>-<hex>]``;
the plaintext goes to a local vault (``PII_VAULT_DIR``) that only the
deployer controls. The same value always yields the same token, so the model
can still compare, group and join, and can send a token back in a query.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
import sqlite3
import stat
import sys
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from lxml import etree

from successfactors_toolkit.services.tenant_store import TenantStore

_KEY_BYTES = 32
_HEX_LEN = 16
# Under SQLite's default limit of 999 host parameters per statement.
_LOOKUP_CHUNK = 900


class PiiVaultError(Exception):
    """The vault's key or database can't be read or written."""


class PiiUnknownTokenError(Exception):
    """A request carried tokens the vault has never issued."""

    def __init__(self, tokens: list[str]):
        super().__init__(f"Unknown PII token(s): {', '.join(tokens)}")
        self.tokens = tokens


class TenantEnvironmentUnset(PiiVaultError):
    """The tenant has not declared production or test, so it has no PII tier.

    A PiiVaultError subclass: every `except PiiVaultError` site that answers
    with pii_error, plugins included, refuses the call without changes."""

    def __init__(self, company_id: str):
        super().__init__(
            f"Declare whether this tenant is production: create {company_id}/tenant.json "
            'under TENANT_KEYS_DIR containing {"production": true} or {"production": false}, '
            f"or PUT that body to /api/tenants/{company_id}/environment."
        )
        self.company_id = company_id


def _secure(path: Path, mode: int, *, regular_file: bool) -> None:
    """Before reusing an existing vault path: refuse one we don't own outright,
    and tighten one we own but that has stray group/other bits.

    For the key and vault.sqlite (regular_file=True), checked with lstat and
    refused outright unless it's a plain regular file — a symlink, FIFO, dir
    or device is judged (and refused) on its own terms, never a target's.
    For the vault directory (regular_file=False), checked with stat, so a
    directory reached through a symlink is judged — and, if loose, tightened
    — on the real directory, not the (always world-permissive) link. Never
    touches anything above `path` itself."""
    st = path.lstat() if regular_file else path.stat()
    if regular_file and not stat.S_ISREG(st.st_mode):
        raise PiiVaultError(f"PII vault path {path} is not a regular file; refusing to use it")
    if st.st_uid != os.geteuid():
        raise PiiVaultError(f"PII vault path {path} is owned by another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, mode)
        print(f"pii vault: tightened permissions on {path}", file=sys.stderr)


def _load_or_create_key(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _secure(path, 0o600, regular_file=True)
        # O_NOFOLLOW + an fstat check close the gap between that check and
        # this read: nothing can swap `path` for a symlink or FIFO in between.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise PiiVaultError(f"PII vault path {path} is not a regular file; refusing to use it")
        with os.fdopen(fd, "rb") as handle:
            key = handle.read()
    else:
        key = secrets.token_bytes(_KEY_BYTES)
        with os.fdopen(descriptor, "wb") as output:
            output.write(key)
    if len(key) != _KEY_BYTES:
        raise PiiVaultError(f"{path} must hold exactly {_KEY_BYTES} bytes")
    return key


class Vault:
    """HMAC key plus a hex -> plaintext table, both owner-only on disk."""

    def __init__(self, directory: Path):
        self._db_path = directory / "vault.sqlite"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _secure(directory, 0o700, regular_file=False)
            self._key = _load_or_create_key(directory / "key")
            try:
                # Create the file owner-only before sqlite opens it under the umask.
                os.close(os.open(self._db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                _secure(self._db_path, 0o600, regular_file=True)
            self._run(
                lambda db: db.execute(
                    "CREATE TABLE IF NOT EXISTS token (hex TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
            )
        except OSError as exc:
            raise PiiVaultError(f"PII vault at {directory} is unavailable: {exc}") from exc

    def _run(self, action):
        try:
            with closing(sqlite3.connect(self._db_path)) as db, db:
                return action(db)
        except sqlite3.Error as exc:
            raise PiiVaultError(f"PII vault database {self._db_path} failed: {exc}") from exc

    def digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:_HEX_LEN]

    def save(self, pairs: dict[str, str]) -> None:
        if pairs:
            self._run(
                lambda db: db.executemany(
                    "INSERT OR IGNORE INTO token (hex, value) VALUES (?, ?)", pairs.items()
                )
            )

    def load(self, hexes: Iterable[str]) -> dict[str, str]:
        wanted = sorted(set(hexes))
        found: dict[str, str] = {}
        for start in range(0, len(wanted), _LOOKUP_CHUNK):
            chunk = wanted[start : start + _LOOKUP_CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = self._run(
                lambda db: db.execute(
                    f"SELECT hex, value FROM token WHERE hex IN ({marks})", chunk
                ).fetchall()
            )
            found.update(rows)
        return found


def _fields(tier: int, *names: str) -> dict[str, int]:
    return dict.fromkeys(names, tier)


# {entity: {field: tier}}. Entity is the OData EntityType (from __metadata.type)
# or a Compound Employee segment; "*" applies in every entity. Tiers: 1 = DHS
# stand-alone sensitive PII, 2 = DHS in-combination + CCPA sensitive, 3 = other
# directly identifying PII. OData names from tctrain $metadata (2026-09-24).
# CE names checked against a tctrain payload (2026-09-24); names the test person
# had no data for follow the same convention. CE payment segments reuse the OData
# names (PaymentInformationV3 > PaymentInformationDetailV3), so one entry covers both.
_BUILTIN_MAP: dict[str, dict[str, int]] = {
    "*": {
        **_fields(1, "nationalId", "iban", "password"),
        **_fields(2, "dateOfBirth"),
        **_fields(3, "firstName", "lastName", "middleName"),
    },
    "PerNationalId": _fields(1, "nationalId"),
    "EmpWorkPermit": _fields(1, "documentNumber", "attachment"),
    "PaymentInformationDetailV3": {
        **_fields(1, "accountNumber", "iban"),
        **_fields(3, "accountOwner"),
    },
    "Attachment": _fields(1, "fileContent"),
    "User": {
        **_fields(1, "password"),
        **_fields(
            2,
            "addressLine1",
            "addressLine2",
            "addressLine3",
            "city",
            "state",
            "zipCode",
            "ethnicity",
            "Disability",
            # Often the work email.
            "username",
        ),
        **_fields(
            3,
            "mi",
            "nickname",
            "displayName",
            "defaultFullName",
            "gender",
            "email",
            "businessPhone",
            "cellPhone",
        ),
    },
    "PerPerson": _fields(2, "dateOfBirth", "placeOfBirth", "countryOfBirth", "dateOfDeath"),
    "PerPersonal": {
        **_fields(2, "nationality", "secondNationality"),
        **_fields(
            3,
            "preferredName",
            "formalName",
            "salutation",
            "suffix",
            "secondLastName",
            "gender",
            "maritalStatus",
        ),
    },
    "PerEmail": _fields(2, "emailAddress"),
    "PerPhone": _fields(2, "phoneNumber"),
    "PerAddressDEFLT": _fields(
        2, *(f"address{i}" for i in range(1, 14)), "city", "state", "province", "county", "zipCode"
    ),
    # SAP's standard US model: genericString1 = ethnic group, genericNumber* = veteran flags.
    "PerGlobalInfoUSA": _fields(
        2, "genericString1", *(f"genericNumber{i}" for i in (1, 2, 4, 5, 6, 7, 8))
    ),
    "PerEmergencyContacts": {
        **_fields(
            2,
            "phone",
            "secondPhone",
            "email",
            *(f"addressAddress{i}" for i in range(1, 6)),
            "addressCity",
            "addressState",
            "addressProvince",
            "addressZipCode",
        ),
        **_fields(3, "name"),
    },
    "PerPersonRelationship": _fields(3, "firstName", "lastName"),
    "Photo": _fields(3, "photo"),
    # Compound Employee segments.
    "national_id_card": _fields(1, "national_id"),
    "personal_documents_information": _fields(1, "document_number"),
    "person": _fields(
        2,
        "date_of_birth",
        "place_of_birth",
        "country_of_birth",
        "date_of_death",
        # Often the work email.
        "logon_user_name",
    ),
    "personal_information": {
        **_fields(2, "nationality", "second_nationality"),
        **_fields(
            3,
            "first_name",
            "last_name",
            "middle_name",
            "preferred_name",
            "formal_name",
            "salutation",
            "suffix",
            "second_last_name",
            "gender",
            "marital_status",
        ),
    },
    "email_information": _fields(2, "email_address"),
    "phone_information": _fields(2, "phone_number"),
    "address_information": _fields(
        2, *(f"address{i}" for i in range(1, 14)), "city", "state", "province", "county", "zip_code"
    ),
    "emergency_contact_primary": {
        **_fields(
            2, "phone", "email", *(f"address{i}" for i in range(1, 6)), "city", "state", "zip_code"
        ),
        **_fields(3, "name"),
    },
    "dependent_information": _fields(3, "first_name", "last_name"),
}
# Binary content: redacted, never stored — the model has no use for it and
# the original stays in SuccessFactors.
_BINARY_FIELDS = frozenset({"attachment", "fileContent", "photo"})


_URI_KEYS = ("uri", "media_src", "edit_media")


def _entity_of(record: dict[str, Any]) -> str:
    """ "SFOData.PerNationalId" -> "PerNationalId"; "*" without __metadata."""
    meta = record.get("__metadata")
    type_ = meta.get("type") if isinstance(meta, dict) else None
    return type_.rsplit(".", 1)[-1] if isinstance(type_, str) else "*"


class PiiFilter:
    def __init__(self, tier: int, extra: dict[str, dict[str, int]], vault: Vault):
        self.tier = tier
        self.vault = vault
        self._map = {entity: dict(fields) for entity, fields in _BUILTIN_MAP.items()}
        for entity, fields in extra.items():
            self._map.setdefault(entity, {}).update(fields)

    def _tier(self, entity: str, field: str) -> int | None:
        """The field's tier if it is tokenized at this filter's level."""
        tier = self._map.get(entity, {}).get(field)
        if tier is None:
            tier = self._map["*"].get(field)
        return tier if tier is not None and tier <= self.tier else None

    def _token(self, tier: int, field: str, value: Any, pending: dict[str, str]) -> str:
        if field in _BINARY_FIELDS:
            return f"[PII-T{tier}-REDACTED]"
        text = str(value)
        hex_ = self.vault.digest(text)
        pending[hex_] = text
        return f"[PII-T{tier}-{hex_}]"

    def tokenize_records(self, records: Any) -> tuple[Any, int]:
        """OData JSON (a list of records, nested $expand included) with
        mapped values tokenized. Returns a new structure and the count."""
        pending: dict[str, str] = {}
        count = 0

        def visit(node: Any) -> Any:
            nonlocal count
            if isinstance(node, list):
                return [visit(item) for item in node]
            if not isinstance(node, dict):
                return node
            entity = _entity_of(node)
            out = {}
            for key, value in node.items():
                if key == "__next":
                    # Top-level response paging is consumed by the paging
                    # client before a record ever reaches here; a "__next"
                    # key this deep is an $expand-ed collection's own paging
                    # link, and it can embed key values (PII) in its URL.
                    continue
                if key in ("__metadata", "__deferred") and isinstance(value, dict):
                    # SF puts the record's key predicate in these URIs, and a
                    # key can be PII (EmpWorkPermit documentNumber). Joins use
                    # key fields, never URIs. Media links carry it too.
                    value = {k: v for k, v in value.items() if k not in _URI_KEYS}
                tier = None if isinstance(value, (dict, list)) else self._tier(entity, key)
                if tier is None or value is None or value == "":
                    out[key] = visit(value)
                else:
                    out[key] = self._token(tier, key, value, pending)
                    count += 1
            # Property-bag records: {Name, Value} pairs keyed by Name.
            name, value = out.get("Name"), out.get("Value")
            if (
                isinstance(name, str)
                and value not in (None, "")
                and not isinstance(value, (dict, list))
            ):
                tier = self._tier("*", name)
                if tier is not None:
                    out["Value"] = self._token(tier, name, value, pending)
                    count += 1
            return out

        result = visit(records)
        self.vault.save(pending)
        return result, count

    def tokenize_xml(self, xml: str) -> tuple[str, int]:
        """A Compound Employee page with mapped leaf values tokenized."""
        # Same hardened settings as mcp_server._edmx_root / ce_response.parse_page.
        root = etree.fromstring(
            xml.encode("utf-8"), parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
        if root.getroottree().docinfo.doctype:
            raise etree.XMLSyntaxError("DOCTYPE is not supported", 0, 0, 0)
        pending: dict[str, str] = {}
        count = 0
        for element in root.iter():
            if not isinstance(element.tag, str) or len(element) or not element.text:
                continue
            segment = next(
                (
                    etree.QName(ancestor).localname
                    for ancestor in element.iterancestors()
                    if etree.QName(ancestor).localname in self._map
                ),
                "*",
            )
            field = etree.QName(element).localname
            tier = self._tier(segment, field)
            if tier is not None:
                element.text = self._token(tier, field, element.text, pending)
                count += 1
        self.vault.save(pending)
        return etree.tostring(root, encoding="unicode"), count


def tier_for(settings, production: bool | None) -> int | None:
    """Production is tier 3 whatever PII_FILTER_TIER says; test is
    PII_FILTER_TIER; unset (None) has no tier."""
    if production is None:
        return None
    return 3 if production else settings.pii_filter_tier


def for_tenant(settings, company_id: str) -> PiiFilter | None:
    """The filter for a tenant ("" = SF_COMPANY_ID), or None for a test tenant
    at tier 0. Raises TenantEnvironmentUnset when the tenant has not declared
    production or test."""
    company_id = company_id or settings.sf_company_id
    tier = tier_for(settings, TenantStore(settings.tenant_keys_dir).production(company_id))
    if tier is None:
        raise TenantEnvironmentUnset(company_id)
    if not tier:
        return None
    return PiiFilter(tier, settings.pii_extra_fields, Vault(settings.pii_vault_dir))


# A token as the model may write it: literal brackets or percent-encoded.
_TOKEN = re.compile(r"(\[|%5[Bb])PII-T[1-3]-([0-9a-f]{16})(\]|%5[Dd])")


def _substitute(text: str, vault: Vault, *, request: bool, encode: bool = False):
    """(text, {inserted: token}, replaced, unknown) with known tokens swapped
    for plaintext. request=True escapes for OData literals and URLs;
    encode=True percent-encodes every value (for a raw URL query string)."""
    hexes = {match.group(2) for match in _TOKEN.finditer(text)}
    if not hexes:
        return text, {}, 0, []
    known = vault.load(hexes)
    substitutions: dict[str, str] = {}
    unknown: list[str] = []
    replaced = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal replaced
        raw = known.get(match.group(2))
        if raw is None:
            unknown.append(match.group(0))
            return match.group(0)
        value = raw
        if request:
            # SF may echo the value back as sent, as the unencoded literal, or
            # as the raw plaintext — map every form so retokenize catches it.
            before = text[max(0, match.start() - 3) : match.start()]
            if before.endswith("'") or before.lower() == "%27":
                value = value.replace("'", "''")
                substitutions[value] = match.group(0)
            if encode or match.group(1) != "[":
                value = quote(value, safe="")
            substitutions[value] = match.group(0)
            substitutions[raw] = match.group(0)
            # A server that echoes the request URL back in an error body may
            # encode it differently than we did on the way out (a different
            # quote() `safe`, quote_plus's '+' for spaces, HTML entities with
            # or without quotes escaped, numeric vs. hex entity, or lowercase
            # percent-hex) — cover those too, or retokenize won't find the
            # exact substring. Not covered: a value double-encoded by SF
            # itself (e.g. percent-encoded twice) — rare enough to skip.
            for variant in (
                quote(raw, safe=""),
                quote(raw),
                quote_plus(raw),
                html.escape(raw),
                html.escape(raw, quote=False),
                html.escape(raw).replace("&#x27;", "&#39;"),
                re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), quote(raw, safe="")),
            ):
                if variant != raw:
                    substitutions[variant] = match.group(0)
        replaced += 1
        return value

    return _TOKEN.sub(swap, text), substitutions, replaced, unknown


def detokenize(text: str, vault: Vault, *, encode: bool = False) -> tuple[str, dict[str, str]]:
    """Tokens in an outgoing request -> plaintext. Unknown tokens abort the
    request rather than reaching SuccessFactors as literal text. encode=True
    for a raw query string, so a value's +, & or = survive URL parsing."""
    out, substitutions, _, unknown = _substitute(text, vault, request=True, encode=encode)
    if unknown:
        raise PiiUnknownTokenError(unknown)
    return out, substitutions


def retokenize(text: str, substitutions: dict[str, str]) -> str:
    """Put tokens back over any plaintext SF echoed (e.g. in an error body).
    Longest first, so a value that contains another is replaced whole."""
    for plain in sorted(substitutions, key=len, reverse=True):
        if plain:
            text = text.replace(plain, substitutions[plain])
    return text


def reveal(text: str, vault: Vault) -> tuple[str, int, list[str]]:
    """Tokens in a local file -> plaintext, for the reveal CLI only."""
    out, _, replaced, unknown = _substitute(text, vault, request=False)
    return out, replaced, unknown
