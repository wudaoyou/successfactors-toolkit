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
import json
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

from successfactors_toolkit.services.system_store import (
    SystemBase,
    SystemStore,
    SystemUnavailable,
)

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


class SystemRefused(PiiVaultError):
    """The system can't be served (see SystemUnavailable). A PiiVaultError
    subclass: every `except PiiVaultError` site that answers with pii_error,
    plugins included, refuses the call without changes."""

    def __init__(self, exc: SystemUnavailable):
        super().__init__(exc.detail)
        self.code = exc.code
        self.system = exc.system
        self.detail = exc.detail


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
    """HMAC key plus a hex -> plaintext table, both owner-only on disk.

    A vault opened for a system issues and resolves only that system's tokens:
    the system name is mixed into the digest and stored with each row (column
    "tenant"). Rows written before tokens were bound have none and resolve only
    in a vault opened without one, which is reveal-only (the reveal CLI).
    Tier is deliberately not part of the binding: it is system-wide, and a
    resolved value only goes back to the system it came from."""

    def __init__(self, directory: Path, tenant: str | None = None):
        self._tenant = tenant
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
            self._run(self._create_table)
        except OSError as exc:
            raise PiiVaultError(f"PII vault at {directory} is unavailable: {exc}") from exc

    @staticmethod
    def _create_table(db) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS token "
            "(hex TEXT PRIMARY KEY, value TEXT NOT NULL, tenant TEXT)"
        )

        def has_tenant_column() -> bool:
            return any(row[1] == "tenant" for row in db.execute("PRAGMA table_info(token)"))

        if not has_tenant_column():
            try:
                db.execute("ALTER TABLE token ADD COLUMN tenant TEXT")
            except sqlite3.OperationalError:
                # A vault is opened per request; another opener may have added it first.
                if not has_tenant_column():
                    raise

    def _run(self, action):
        try:
            with closing(sqlite3.connect(self._db_path)) as db, db:
                return action(db)
        except sqlite3.Error as exc:
            raise PiiVaultError(f"PII vault database {self._db_path} failed: {exc}") from exc

    def _issuer(self) -> str:
        if self._tenant is None:
            raise PiiVaultError("This PII vault handle has no tenant and cannot issue tokens")
        return self._tenant

    def digest(self, value: str) -> str:
        message = f"{self._issuer()}\0{value}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:_HEX_LEN]

    def save(self, pairs: dict[str, str]) -> None:
        tenant = self._issuer()
        if pairs:
            self._run(
                lambda db: db.executemany(
                    "INSERT OR IGNORE INTO token (hex, value, tenant) VALUES (?, ?, ?)",
                    [(hex_, value, tenant) for hex_, value in pairs.items()],
                )
            )

    def load(self, hexes: Iterable[str]) -> dict[str, str]:
        wanted = sorted(set(hexes))
        found: dict[str, str] = {}
        for start in range(0, len(wanted), _LOOKUP_CHUNK):
            chunk = wanted[start : start + _LOOKUP_CHUNK]
            marks = ",".join("?" * len(chunk))
            # A tenant sees only its own rows; no tenant (reveal) sees them all.
            scope, args = (
                ("", chunk) if self._tenant is None else (" AND tenant = ?", [*chunk, self._tenant])
            )
            rows = self._run(
                lambda db: db.execute(
                    f"SELECT hex, value FROM token WHERE hex IN ({marks}){scope}", args
                ).fetchall()
            )
            found.update(rows)
        return found


def _fields(tier: int, *names: str) -> dict[str, int]:
    return dict.fromkeys(names, tier)


# Entities and CE segments reviewed as holding no PII beyond the "*" rules: known
# to the map with no fields of their own, so fail-closed tokenization leaves
# them alone. Anything not here, in _BUILTIN_MAP or in the extra fields is unknown.
_REVIEWED_ENTITIES = (
    # OData v2 (the first payment name also covers the CE payment header)
    "EmpJob EmpEmployment EmpEmploymentTermination EmpCompensation EmpPayCompRecurring "
    "EmpPayCompNonRecurring EmpJobRelationships Position BenefitEnrollment PaymentInformationV3 "
    "FOCompany FOBusinessUnit FODivision FODepartment FOLocation FOLocationGroup FOCostCenter "
    "FOJobCode FOJobFunction FOPayGrade FOPayRange FOPayComponent FOPayComponentGroup "
    "FOPayGroup FOFrequency FOEventReason FOGeozone FOCorporateAddressDEFLT "
    "Picklist PicklistOption PicklistLabel PickListV2 PickListValueV2 "
    # Compound Employee
    "CompoundEmployee employment_information job_information compensation_information "
    "paycompensation_recurring paycompensation_non_recurring global_assignment_information "
    "alternative_cost_distribution deduction_recurring deduction_non_recurring payment_information"
).split()

# {entity: {field: tier}}. Entity is the OData EntityType (from v2
# __metadata.type or v4 @odata.type) or a Compound Employee segment; "*"
# applies in every entity. Tiers: 1 = DHS stand-alone sensitive PII, 2 = DHS
# in-combination + CCPA sensitive, 3 = other
# directly identifying PII. OData names from tctrain $metadata (2026-09-24).
# CE names checked against a tctrain payload (2026-09-24); names the test person
# had no data for follow the same convention. CE payment segments reuse the OData
# names (PaymentInformationV3 > PaymentInformationDetailV3), so one entry covers both.
_BUILTIN_MAP: dict[str, dict[str, int]] = {
    "*": {
        **_fields(1, "nationalId", "iban", "password", "national_id"),
        **_fields(2, "dateOfBirth", "date_of_birth"),
        **_fields(
            3, "firstName", "lastName", "middleName", "first_name", "last_name", "middle_name"
        ),
    },
    **{entity: {} for entity in _REVIEWED_ENTITIES},
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
    # OData v4 (entity = the @odata.type's last segment). Names from tctrain
    # $metadata (2026-09-28): talent/continuousfeedback/v1.
    "feedback": _fields(3, "senderDisplayName", "subjectDisplayName"),
    "feedbackRequests": _fields(
        3, "requesterDisplayName", "recipientDisplayName", "subjectDisplayName"
    ),
}
# Binary content: redacted, never stored — the model has no use for it and
# the original stays in SuccessFactors.
_BINARY_FIELDS = frozenset({"attachment", "fileContent", "photo"})


# What survives of these control dicts: SF puts the record's key predicate in
# their URIs, and a key can be PII (EmpWorkPermit documentNumber). Joins use
# key fields, never URIs.
_URI_KEYS = {"__metadata": ("type",), "__deferred": ()}
# v4 control information holding URLs, which embed key predicates (PII) like
# v2's __metadata URIs do: "@odata.id", "empInfo@odata.nextLink",
# "photo@odata.mediaReadLink", ... The bare 4.01 forms ("@id") too.
_V4_LINK_KEY = re.compile(
    r".*@(?:odata\.)?(?:context|id|editLink|readLink|navigationLink|associationLink"
    r"|mediaReadLink|mediaEditLink|nextLink|deltaLink)"
)


def _entity_of(record: dict[str, Any], v4: bool = False) -> str | None:
    """ "SFOData.PerNationalId" (v2 __metadata.type) or "#SFOData.PerNationalId"
    (v4 @odata.type) -> "PerNationalId". Untyped: "*" for v2; None for v4,
    where it means the type is unknown (see PiiFilter._tier)."""
    type_ = record.get("@odata.type", record.get("@type"))
    if not isinstance(type_, str):
        meta = record.get("__metadata")
        type_ = meta.get("type") if isinstance(meta, dict) else None
    if isinstance(type_, str):
        return type_.lstrip("#").rsplit(".", 1)[-1]
    return None if v4 else "*"


class PiiFilter:
    def __init__(self, tier: int, extra: dict[str, dict[str, int]], vault: Vault):
        self.tier = tier
        self.vault = vault
        self._map = {entity: dict(fields) for entity, fields in _BUILTIN_MAP.items()}
        for entity, fields in extra.items():
            self._map.setdefault(entity, {}).update(fields)
        # Every mapped field at its most sensitive tier in any entity, for a
        # v4 record whose type is unknown.
        self._any: dict[str, int] = {}
        for fields in self._map.values():
            for field, tier in fields.items():
                self._any[field] = min(tier, self._any.get(field, tier))

    def _tier(self, entity: str | None, field: str) -> int | None:
        """The field's tier if it is tokenized at this filter's level.
        entity=None (a v4 record without @odata.type) fails safe: the record
        could be any entity, so every entity's rules apply to it."""
        if entity is None:
            tier = self._any.get(field)
        else:
            tier = self._map.get(entity, {}).get(field)
            if tier is None:
                tier = self._map["*"].get(field)
        return tier if tier is not None and tier <= self.tier else None

    def protects(self, entity: str | None, field: str) -> bool:
        """Whether a query reference to `field` touches a value tokenized at
        this filter's tier. entity=None (reached through a navigation path,
        target unknown) is true if any entity tokenizes it; an entity the map
        doesn't know is true (fail closed). Field names match case-insensitively,
        in case SuccessFactors resolves them that way."""
        if entity is not None and entity not in self._map:
            return True
        fields = self._any if entity is None else {**self._map["*"], **self._map[entity]}
        tier = min((t for f, t in fields.items() if f.lower() == field.lower()), default=None)
        return tier is not None and tier <= self.tier

    def _token(self, tier: int, field: str, value: Any, pending: dict[str, str]) -> str:
        if field in _BINARY_FIELDS:
            return f"[PII-T{tier}-REDACTED]"
        text = str(value)
        hex_ = self.vault.digest(text)
        pending[hex_] = text
        return f"[PII-T{tier}-{hex_}]"

    def tokenize_value(self, value: str) -> str:
        """One opaque value (a paging $skiptoken, which a server may build
        from key values) as a tier-1 token that detokenize resolves back."""
        pending: dict[str, str] = {}
        token = self._token(1, "", value, pending)
        self.vault.save(pending)
        return token

    def tokenize_records(
        self,
        records: Any,
        v4: bool = False,
        *,
        entity: str | None = None,
        fail_closed: bool = False,
    ) -> tuple[Any, int]:
        """OData JSON (a list of records, nested $expand included) with
        mapped values tokenized. Returns a new structure and the count.
        v4=True for OData v4 records: one without a known @odata.type gets every
        entity's rules (see _tier) instead of only the "*" ones.
        fail_closed=True: a record whose entity (its type, or `entity` for an
        untyped top-level record) the map doesn't know has every string value
        tokenized at tier 1, and v4 no longer falls back to every entity's rules."""
        pending: dict[str, str] = {}
        count = 0

        def visit(node: Any, top_entity: str | None = None) -> Any:
            nonlocal count
            if isinstance(node, list):
                return [visit(item, top_entity) for item in node]
            if not isinstance(node, dict):
                return node
            unknown = False
            if fail_closed:
                record = _entity_of(node, True) or top_entity
                unknown = record not in self._map or record == "*"
                if unknown:
                    record = "*"
            else:
                record = _entity_of(node, v4)
                if v4 and record not in self._map:
                    record = None  # a v4 type the map doesn't know: same fail-safe as untyped
            out = {}
            for key, value in node.items():
                if key == "__next" or _V4_LINK_KEY.fullmatch(key):
                    # Top-level response paging is consumed by the paging
                    # client before a record ever reaches here; a "__next"
                    # key this deep is an $expand-ed collection's own paging
                    # link, and it can embed key values (PII) in its URL.
                    # v4 links (_V4_LINK_KEY) carry them the same way.
                    continue
                if key in _URI_KEYS and isinstance(value, dict):
                    out[key] = {k: v for k, v in value.items() if k in _URI_KEYS[key]}
                    continue
                if unknown and not key.startswith("__") and "@" not in key:
                    # An entity nobody reviewed: any string may be PII.
                    if isinstance(value, str) and value:
                        out[key] = self._token(1, key, value, pending)
                        count += 1
                        continue
                    if isinstance(value, list):
                        items = []
                        for item in value:
                            if isinstance(item, str) and item:
                                items.append(self._token(1, key, item, pending))
                                count += 1
                            else:
                                items.append(visit(item))
                        out[key] = items
                        continue
                tier = None if isinstance(value, (dict, list)) else self._tier(record, key)
                if tier is None or value is None or value == "":
                    out[key] = visit(value)
                else:
                    out[key] = self._token(tier, key, value, pending)
                    count += 1
            # Property-bag records: {Name, Value} pairs keyed by Name. An unknown
            # record already had every string tokenized above.
            name, value = out.get("Name"), out.get("Value")
            if (
                not unknown
                and isinstance(name, str)
                and value not in (None, "")
                and not isinstance(value, (dict, list))
            ):
                tier = self._tier("*", name)
                if tier is not None:
                    out["Value"] = self._token(tier, name, value, pending)
                    count += 1
            return out

        result = visit(records, entity)
        self.vault.save(pending)
        return result, count

    def tokenize_xml(self, xml: str) -> tuple[str, int]:
        """A Compound Employee page with mapped leaf values tokenized. A leaf is
        judged by its immediate parent element as the segment; one inside a
        CompoundEmployee whose segment isn't in the map is tokenized at tier 1."""
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
            field = etree.QName(element).localname
            parent = element.getparent()
            segment = etree.QName(parent).localname if parent is not None else "*"
            if segment in self._map:
                tier = self._tier(segment, field)
            elif any(
                etree.QName(a).localname == "CompoundEmployee" for a in element.iterancestors()
            ):
                tier = 1  # a segment nobody reviewed: fail closed
            else:
                tier = self._tier("*", field)  # SOAP envelope, paging, faults
            if tier is not None:
                element.text = self._token(tier, field, element.text, pending)
                count += 1
        self.vault.save(pending)
        return etree.tostring(root, encoding="unicode"), count


def tier_for(config: SystemBase) -> int:
    """Production is tier 3; a test system's pii_filter_tier, default 1."""
    if config.production:
        return 3
    return 1 if config.pii_filter_tier is None else config.pii_filter_tier


def for_system(settings, system: str) -> PiiFilter | None:
    """The filter for a system (a name SystemStore.select returned), or None
    for a test system at tier 0. Raises SystemRefused."""
    try:
        config = SystemStore(settings.systems_dir).config(system)
    except SystemUnavailable as exc:
        raise SystemRefused(exc) from None
    tier = tier_for(config)
    if not tier:
        return None
    extra = {entity: dict(fields) for entity, fields in config.pii_extra_fields.items()}
    return PiiFilter(tier, extra, Vault(settings.pii_vault_dir, system))


# A token as the model may write it: literal brackets or percent-encoded.
_TOKEN = re.compile(r"(\[|%5[Bb])PII-T[1-3]-([0-9a-f]{16})(\]|%5[Dd])")
# An OData string-literal quote, as sent or percent-encoded. Counting them
# tells whether a token sits inside a literal: '' (an escaped quote) counts
# twice, so it leaves that unchanged.
_QUOTE = re.compile(r"'|%27", re.IGNORECASE)


def _json_escapes(text: str) -> set[str]:
    """`text` as a JSON string body may carry it: \\uXXXX for non-ASCII in
    either hex case, or not, and "/" escaped as "\\/" or not."""
    out = {text}
    for ascii_ in (True, False):
        body = json.dumps(text, ensure_ascii=ascii_)[1:-1]
        upper = re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m[1].upper(), body)
        for form in (body, upper):
            out.update((form, form.replace("/", "\\/")))
    return out


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
    scanned = 0
    in_literal = False

    def swap(match: re.Match[str]) -> str:
        nonlocal replaced, scanned, in_literal
        # Quotes between the previous token and this one; tokens hold none.
        if len(_QUOTE.findall(text, scanned, match.start())) % 2:
            in_literal = not in_literal
        scanned = match.end()
        raw = known.get(match.group(2))
        if raw is None:
            unknown.append(match.group(0))
            return match.group(0)
        value = raw
        if request:
            # SF may echo the value back as sent, as the unencoded literal, or
            # as the raw plaintext — map every form so retokenize catches it.
            forms = {raw}
            if in_literal:
                # Anywhere inside a literal, not just right after its quote:
                # a quote in the value must not end the literal early.
                value = value.replace("'", "''")
                forms.add(value)
            if encode or match.group(1) != "[":
                value = quote(value, safe="")
            forms.add(value)
            # A server that echoes the request URL back in an error body may
            # encode it differently than we did on the way out (a different
            # quote() `safe`, quote_plus's '+' for spaces, HTML entities with
            # or without quotes escaped, numeric vs. hex entity, or lowercase
            # percent-hex) — cover those too, or retokenize won't find the
            # exact substring. Each form may also sit JSON-escaped in a JSON
            # error body. Not covered: a value double-encoded by SF itself
            # (e.g. percent-encoded twice) — rare enough to skip.
            forms.update(
                (
                    quote(raw, safe=""),
                    quote(raw),
                    quote_plus(raw),
                    html.escape(raw),
                    html.escape(raw, quote=False),
                    html.escape(raw).replace("&#x27;", "&#39;"),
                    re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), quote(raw, safe="")),
                )
            )
            for form in forms:
                for variant in _json_escapes(form):
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
