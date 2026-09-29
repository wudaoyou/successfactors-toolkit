"""Query-option guard for tenants where PII is tokenized.

The model writes $filter, $orderby, $search and $apply itself, and the server
resolves tokens in them to plaintext. Counts and record order would let it
learn a tokenized field's value without ever seeing it, so on those tenants a
tokenized field may only be compared by token equality.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from successfactors_toolkit.services.pii_filter import _TOKEN, PiiVaultError


class PiiQueryRefused(PiiVaultError):
    """The query could compare or order tokenized PII other than by token equality.

    A PiiVaultError subclass: every `except PiiVaultError` site that answers
    with pii_error refuses the call without changes."""


# One scanner for filter-like expressions. Order matters: string and typed
# literals first so nothing inside quotes is read as an identifier. Only real
# OData literal prefixes make a typed literal; any other name followed by a
# quote is an identifier and a string.
_SCAN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>'(?:[^']|'')*')
  | (?P<lit>(?i:datetimeoffset|datetime|time|date|guid|binary|duration|geography\w*|geometry\w*|X)
        '(?:[^']|'')*'
      | [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}
      | \d{4}-\d\d-\d\d(?:T[\d:.]+(?:Z|[+-]\d\d:\d\d)?)?
      | \d+(?:\.\d+)?(?:[eE][+-]?\d+)?[A-Za-z]?)
  | (?P<id>[A-Za-z_$][\w.$]*(?:/[A-Za-z_$][\w.$]*)*)
  | (?P<punct>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# Operators and literals (case-insensitive); the aggregation words only mean
# something inside $apply/$compute, elsewhere they can be property names.
_OPS = frozenset(
    "and or not eq ne gt ge lt le has in add sub mul div mod true false null asc desc "
    "$count $it $root".split()
)
_AGG = frozenset("with as from sum min max average countdistinct".split())

_NESTED = ("filter", "orderby", "apply", "compute", "search")


def _scan(text: str) -> list[tuple[str, str, int, int]]:
    """(kind, text, start, end) per token; kind is str, lit, id or punct."""
    return [
        (m.lastgroup, m.group(), m.start(), m.end())
        for m in _SCAN.finditer(text)
        if m.lastgroup != "ws"
    ]


def _refs(tokens: list[tuple[str, str, int, int]], keywords: frozenset[str]):
    """(index, path) of every property reference: not a keyword, function
    name or lambda variable declaration."""
    for i, (kind, text, _, _) in enumerate(tokens):
        if kind != "id" or text.rsplit("/", 1)[-1].lower() in keywords:
            continue
        if i + 1 < len(tokens) and tokens[i + 1][0] == "punct" and tokens[i + 1][1] in ("(", ":"):
            continue
        yield i, text


def _is_token(token: tuple[str, str, int, int]) -> bool:
    return token[0] == "str" and _TOKEN.fullmatch(token[1][1:-1]) is not None


def _equality_only(tokens: list[tuple[str, str, int, int]], i: int) -> bool:
    """The reference at i is followed by eq/ne + token or null, or in + tokens."""
    n = len(tokens)
    op = tokens[i + 1][1].lower() if i + 1 < n and tokens[i + 1][0] == "id" else ""
    j = i + 2
    if op in ("eq", "ne"):
        return j < n and (_is_token(tokens[j]) or tokens[j][1].lower() == "null")
    if op != "in":
        return False
    paren = j < n and tokens[j][1] == "("
    j += paren
    while j < n and _is_token(tokens[j]):
        j += 1
        if j < n and tokens[j][1] == ",":
            j += 1
            continue
        return not paren or (j < n and tokens[j][1] == ")")
    return False


def _refuse(field: str, option: str) -> PiiQueryRefused:
    if option == "filter":
        return PiiQueryRefused(
            f"{field} holds tokenized PII on this tenant: in $filter compare it only with eq, "
            "ne or in against tokens from earlier results (or null); it can't be sorted, "
            "searched, aggregated or passed to functions. Filter on a non-PII field instead."
        )
    return PiiQueryRefused(
        f"{field} holds tokenized PII on this tenant and can't be used in ${option}: it can't "
        "be sorted, searched, aggregated or passed to functions, and in $filter it can only be "
        "compared with eq, ne or in against tokens from earlier results (or null)."
    )


def _refuse_search() -> PiiQueryRefused:
    return PiiQueryRefused("Full-text search is not available where PII is tokenized.")


def _check_expression(
    option: str,
    value: str,
    entity: str | None,
    protects: Callable[[str | None, str], bool],
) -> None:
    tokens = _scan(value)
    keywords = _OPS | _AGG if option in ("apply", "compute") else _OPS
    for i, path in _refs(tokens, keywords):
        # A path is checked by its last segment with no entity context.
        bare = "/" not in path
        if not protects(entity if bare else None, path.rsplit("/", 1)[-1]):
            continue
        if option != "filter" or not _equality_only(tokens, i):
            raise _refuse(path, option)


def _check_expand(value: str, protects: Callable[[str | None, str], bool]) -> None:
    """Nested options such as nav($filter=...;$orderby=...); every identifier
    counts as a navigation property. A plain $expand=a,bNav/c has none."""
    tokens = _scan(value)
    for i, (kind, text, _, end) in enumerate(tokens[:-1]):
        name = text.lower().removeprefix("$")
        if kind != "id" or name not in _NESTED or tokens[i + 1][1] != "=":
            continue
        start = tokens[i + 1][3]
        stop, depth = len(value), 0
        for _, tok, tok_start, _ in tokens[i + 2 :]:
            if tok == "(":
                depth += 1
            elif depth == 0 and tok in (")", ";"):
                stop = tok_start
                break
            elif tok == ")":
                depth -= 1
        nested = value[start:stop]
        if name == "search":
            if nested.strip():
                raise _refuse_search()
        else:
            _check_expression(name, nested, None, protects)


def check_query(
    options: Iterable[tuple[str, str]],
    entity: str,
    protects: Callable[[str | None, str], bool],
) -> None:
    """Raise PiiQueryRefused if a query option could compare or order a
    tokenized field other than by token equality. options are every URL-decoded
    (name, value) the request sends, before token resolution; entity names bare
    properties ("" when unknown); protects(entity_or_None, field) says whether
    a field is tokenized."""
    for raw_name, value in options:
        name = raw_name.lower().removeprefix("$")
        if name == "search":
            if value.strip():
                raise _refuse_search()
        elif name in ("filter", "orderby", "apply", "compute"):
            _check_expression(name, value, entity, protects)
        elif name == "expand":
            _check_expand(value, protects)
