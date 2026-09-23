"""OData client using OAuth2 SAML Bearer Assertion.

Per the SAP HCM OData API Developer Guide §2.3 (2H 2025), /oauth/token only
documents `urn:ietf:params:oauth:grant-type:saml2-bearer` — the same flow used
by SFAPI. OData uses the shared `SF_*` settings and per-request connection overrides.
The REST API version is configured separately through `SF_ODATA_VERSION`.

Token caching mirrors `SFAPIClient`: keyed by (host, company_id, user_id,
client_key), with a lock to coalesce concurrent first-call token storms.

Retry policy (§12.7, p.228-229):
  * 401 once  — refresh token, retry
  * 429 up to MAX_RETRIES — honor Retry-After header (capped at 300s; SAP rate
    limiting has been observed returning a 300s Retry-After)
  * 5xx up to MAX_RETRIES — exponential backoff; ONLY for idempotent methods
    (GET/HEAD) since POST/PATCH/PUT/DELETE may have already mutated state.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlparse, urlsplit

import httpx

from successfactors_toolkit.config import Settings
from successfactors_toolkit.models.common import ODataConnectionConfig
from successfactors_toolkit.services import saml_bearer
from successfactors_toolkit.services.connection_policy import ConnectionPolicyError, check_host
from successfactors_toolkit.services.credentials import load_key_pem

_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}
# Caller-supplied headers may tune the request, but not these two: httpx sends a
# supplied Host verbatim (so the allowlisted hostname would stop being the host
# the request addresses), and Authorization carries the tenant's access token.
_RESERVED_HEADERS = {"host", "authorization"}
_IDEMPOTENT_FOR_5XX_RETRY = {"GET", "HEAD"}
_TOKEN_EXPIRY_SLACK_SECONDS = 60
_MAX_RETRIES = 3
_MAX_RETRY_AFTER_SECONDS = 300.0
_ODATA_VERSIONS = {"v2", "v4"}


def _eff(override: str | None, default: str) -> str:
    return override if override is not None else default


def _parse_retry_after(value: str | None) -> float:
    """Parse Retry-After header (seconds or HTTP-date). Bounded to [0, 300]."""
    if not value:
        return 1.0
    try:
        return min(max(float(value), 0.0), _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return min(max(delta, 0.0), _MAX_RETRY_AFTER_SECONDS)
        except (TypeError, ValueError):
            return 1.0


def split_path_query(path: str) -> tuple[str, dict[str, str]]:
    """Split any ``?query`` off an OData path into (clean_path, params).

    Query options embedded in ``path`` (e.g. ``"EmpJob?$filter=...&$select=..."``)
    must be pulled out before the request URL is built: httpx *replaces* rather
    than merges a URL's existing query string when ``params=`` is also passed to
    ``Client.request()``, so leaving them baked into the path silently drops
    them. Exposed (not module-private) so callers that need to know what a path
    already asks for — e.g. detecting a caller-supplied ``$orderby`` — can reuse
    the same parsing instead of re-deriving it.
    """
    base, sep, query = path.partition("?")
    return (base, dict(parse_qsl(query, keep_blank_values=True))) if sep else (path, {})


# Conservative URL-length budget for a chunked $filter: KBA 2576271 cites a
# ~2KB GET URL limit; 1800 leaves room for the rest of the query string
# (path, $select, $orderby, paging, ...) that shares the same URL.
_MAX_FILTER_ENCODED_LEN = 1800


def _in_clause(column: str, values: list[str]) -> str:
    """Build ``column in 'v1','v2',...`` — confirmed working on EmpJob,
    BenefitEnrollment and nav paths without the parenthesised `in (...)` form
    the dev guide's grammar suggests (that form 400s). OData literal escaping
    doubles an embedded single quote.
    """
    escaped = (v.replace("'", "''") for v in values)
    return f"{column} in " + ",".join(f"'{v}'" for v in escaped)


def _combined_filter(base_filter: str, column: str, values: list[str]) -> str:
    in_clause = _in_clause(column, values)
    return f"({base_filter}) and ({in_clause})" if base_filter else in_clause


def _encoded_len(filter_value: str) -> int:
    """Estimate the URL-encoded length of a $filter value."""
    return len(quote(filter_value, safe=""))


def _extract_skiptoken(next_url: str) -> str | None:
    """Pull $skiptoken value out of a __next link."""
    if not next_url:
        return None
    qs = parse_qs(urlparse(next_url).query)
    vals = qs.get("$skiptoken")
    return vals[0] if vals else None


def _odata_url(host: str, version: str, path: str) -> str:
    """Build an OData URL without letting caller input escape its API root."""
    if version not in _ODATA_VERSIONS:
        raise ConnectionPolicyError("OData version must be 'v2' or 'v4'.")
    if "\\" in path or any(ord(char) < 32 for char in path):
        raise ConnectionPolicyError("OData path contains invalid characters.")
    try:
        parts = urlsplit(path)
    except ValueError as exc:
        raise ConnectionPolicyError("OData path is not valid.") from exc
    if parts.scheme or parts.netloc:
        raise ConnectionPolicyError("OData path must be relative.")

    decoded_path = parts.path
    for _ in range(4):
        if "\\" in decoded_path or any(ord(char) < 32 for char in decoded_path):
            raise ConnectionPolicyError("OData path contains invalid characters.")
        if any(segment in {".", ".."} for segment in decoded_path.split("/")):
            raise ConnectionPolicyError("OData path must stay inside the configured API root.")
        unquoted = unquote(decoded_path)
        if unquoted == decoded_path:
            break
        decoded_path = unquoted
    else:
        raise ConnectionPolicyError("OData path has too many encoding layers.")

    prefix = f"/odata/{version}/"
    url = httpx.URL(f"https://{host}{prefix}{path.lstrip('/')}")
    if not url.path.startswith(prefix):
        raise ConnectionPolicyError("OData path must stay inside the configured API root.")
    return str(url)


class ODataClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        # Token cache: (host, company_id, user_id, client_key) -> (token, exp_epoch)
        self._tokens: dict[tuple[str, str, str, str], tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()

    def _resolve(self, conn: ODataConnectionConfig | None) -> dict[str, Any]:
        # OData shares OAuth2 client + tenant key with SFAPI — both use the
        # same /oauth/token endpoint with the same SAML assertion. Per-request
        # overrides via ODataConnectionConfig take precedence; otherwise we
        # fall back to the SF_* settings (NOT SF_ODATA_* — those don't exist
        # anymore, only SF_ODATA_VERSION is OData-specific).
        s = self._settings
        c = conn or ODataConnectionConfig()
        company_id = _eff(c.company_id, s.sf_company_id)
        return {
            # Overrides are attacker-controlled on the REST path: only hosts the
            # policy allows may end up in the request base_url.
            "host": check_host(_eff(c.host, s.sf_host), s),
            "version": _eff(c.odata_version, s.sf_odata_version),
            "client_key": _eff(c.client_key, s.sf_client_key),
            "user_id": _eff(c.user_id, s.sf_user_id),
            "company_id": company_id,
            "token_url": _eff(c.token_url, s.sf_token_url),
            "csrf_protected": c.csrf_protected,
            "private_key_pem": load_key_pem(c.private_key_path, s, company_id),
        }

    @staticmethod
    def _token_key(r: dict[str, Any]) -> tuple[str, str, str, str]:
        return (r["host"], r["company_id"], r["user_id"], r["client_key"])

    async def _fetch_new_token(self, r: dict[str, Any]) -> tuple[str, float]:
        """Mint a fresh access token via SAML Bearer; returns (token, exp_epoch).

        saml_bearer.fetch_token returns only the token string; SAP tokens default
        to 24h, so we cache for 23h to leave a safety margin and absorb skew.
        """
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
        exp = time.monotonic() + (23 * 3600)
        return token, exp

    async def _get_token(self, r: dict[str, Any], force_refresh: bool = False) -> str:
        key = self._token_key(r)
        if not force_refresh:
            cached = self._tokens.get(key)
            if cached and cached[1] - _TOKEN_EXPIRY_SLACK_SECONDS > time.monotonic():
                return cached[0]
        async with self._token_lock:
            cached = self._tokens.get(key)
            if (
                not force_refresh
                and cached
                and cached[1] - _TOKEN_EXPIRY_SLACK_SECONDS > time.monotonic()
            ):
                return cached[0]
            token, exp = await self._fetch_new_token(r)
            self._tokens[key] = (token, exp)
            return token

    async def _fetch_csrf_token(self, base_url: str, auth_header: str) -> str:
        resp = await self._client.get(
            f"{base_url}?$top=0",
            headers={"Authorization": auth_header, "X-CSRF-Token": "Fetch"},
            timeout=self._settings.request_timeout,
        )
        return resp.headers.get("x-csrf-token", "")

    async def request(
        self,
        method: str,
        path: str,
        conn: ODataConnectionConfig | None = None,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # Query options embedded in the path (e.g. "EmpJob?$select=userId") are
        # pulled out here so they reach the request instead of being silently
        # dropped when `params` is also merged in below (see split_path_query).
        path, path_params = split_path_query(path)
        r = self._resolve(conn)
        base_url = f"https://{r['host']}/odata/{r['version']}"
        url = _odata_url(r["host"], r["version"], path)
        m = method.upper()

        # $metadata is served as EDMX XML only (§5.5.2.2): asking for JSON gets
        # an HTTP 406 back, which is how you lose an afternoon looking up an
        # entity name. Everything else defaults to $format=JSON, since per
        # §5.5.2.6 the server otherwise returns Atom XML.
        # The document may be asked for whole ('$metadata') or scoped to a
        # single entity set ('EmpJob/$metadata'); both are EDMX.
        is_metadata = path.rstrip("/").endswith("$metadata")
        # Explicit `params` win over whatever the path already asked for.
        effective_params: dict[str, Any] = {**path_params, **(params or {})}
        if not is_metadata and "$format" not in effective_params:
            effective_params["$format"] = "JSON"

        async def _do(token: str) -> httpx.Response:
            auth_header = f"Bearer {token}"
            headers: dict[str, str] = {
                "Accept": "application/xml" if is_metadata else "application/json",
                "Content-Type": "application/json",
                **{
                    k: v
                    for k, v in (extra_headers or {}).items()
                    if k.lower() not in _RESERVED_HEADERS
                },
                "Authorization": auth_header,
            }
            if m in _MUTATING and r["csrf_protected"]:
                csrf = await self._fetch_csrf_token(base_url, auth_header)
                if csrf:
                    headers["X-CSRF-Token"] = csrf
            return await self._client.request(
                method=m,
                url=url,
                headers=headers,
                params=effective_params,
                json=body,
                timeout=self._settings.request_timeout,
            )

        token = await self._get_token(r)
        resp: httpx.Response | None = None
        token_refreshed = False

        for attempt in range(_MAX_RETRIES + 1):
            resp = await _do(token)
            sc = resp.status_code

            # 401: refresh token once, then retry. Don't count toward retry budget.
            if sc == 401 and not token_refreshed:
                token = await self._get_token(r, force_refresh=True)
                token_refreshed = True
                continue

            # 429: always retryable (server didn't process the request)
            if sc == 429 and attempt < _MAX_RETRIES:
                wait = _parse_retry_after(resp.headers.get("retry-after"))
                # Add small jitter to avoid thundering herd on shared retry-after.
                await asyncio.sleep(wait + random.uniform(0, 0.5))
                continue

            # 5xx: retry only idempotent methods (mutating may have committed).
            if 500 <= sc < 600 and m in _IDEMPOTENT_FOR_5XX_RETRY and attempt < _MAX_RETRIES:
                backoff = (2**attempt) * 0.5 + random.uniform(0, 0.5)
                await asyncio.sleep(min(backoff, _MAX_RETRY_AFTER_SECONDS))
                continue

            break

        assert resp is not None
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text,
        }

    async def extract_all(
        self,
        path: str,
        conn: ODataConnectionConfig | None = None,
        params: dict[str, Any] | None = None,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        """Auto-follow `__next` links and aggregate `d.results` across pages.

        Designed for bulk extraction of entity sets (especially EmpJob,
        which supports cursor pagination per §5.5.3.2.1). Pass
        ``paging=cursor`` or ``paging=snapshot`` in params for server-side
        pagination; otherwise client-side `$skip`+`$top` is used and the
        ``__next`` link follows the same offset semantics.

        Returns:
            {
              "pages_fetched": int,
              "total_records": int,
              "results": list[dict],            # flattened d.results
              "next_skiptoken": str | None,     # set if max_pages was hit
                                                # mid-stream — pass back in
                                                # params to resume.
              "last_status_code": int,
              "last_headers": dict,
              "last_page_size": int,            # records on the final page
                                                # fetched — lets a caller spot
                                                # a page that filled exactly
                                                # to $top with no __next.
              "stopped_reason": str,            # 'exhausted'|'max_pages'|
                                                # 'http_error'|'parse_error'
            }
        """
        all_results: list[dict[str, Any]] = []
        current_params = dict(params or {})
        last_status = 0
        last_headers: dict[str, str] = {}
        pages = 0
        last_page_size = 0
        next_skiptoken: str | None = None
        stopped = "exhausted"

        while pages < max_pages:
            resp = await self.request("GET", path, conn=conn, params=current_params)
            last_status = resp["status_code"]
            last_headers = resp["headers"]

            if last_status >= 400:
                stopped = "http_error"
                break

            try:
                body = json.loads(resp["body"])
            except json.JSONDecodeError:
                stopped = "parse_error"
                break

            d = body.get("d", body)  # tolerant: some endpoints don't wrap in 'd'
            results = d.get("results", []) if isinstance(d, dict) else []
            all_results.extend(results)
            last_page_size = len(results)
            pages += 1

            next_link = d.get("__next") if isinstance(d, dict) else None
            if not next_link:
                stopped = "exhausted"
                break

            skip = _extract_skiptoken(next_link)
            if not skip:
                stopped = "exhausted"  # __next without skiptoken → server says we're done
                break

            if pages >= max_pages:
                next_skiptoken = skip
                stopped = "max_pages"
                break
            current_params["$skiptoken"] = skip

        return {
            "pages_fetched": pages,
            "total_records": len(all_results),
            "results": all_results,
            "next_skiptoken": next_skiptoken,
            "last_status_code": last_status,
            "last_headers": last_headers,
            "last_page_size": last_page_size,
            "stopped_reason": stopped,
        }

    async def extract_by_filter_in(
        self,
        path: str,
        column: str,
        values: list[str],
        conn: ODataConnectionConfig | None = None,
        params: dict[str, Any] | None = None,
        chunk_size: int = 1000,
        max_pages_per_chunk: int = 100,
    ) -> dict[str, Any]:
        """Extract records where ``column in (values...)``, chunking the IN
        list to stay under SF's 1000-value-per-$filter limit (§12.7, p.228)
        and under a conservative URL-length budget (`_MAX_FILTER_ENCODED_LEN`;
        KBA 2576271 cites a ~2KB GET URL limit).

        Typical use case: enrich CompoundEmployee codes — collect distinct
        jobCode / locationCode / etc. from a CE payload, then fetch the
        corresponding FO* records' name/description in one call here.

        Combines existing ``$filter`` from params with an AND clause.
        """
        if chunk_size > 1000:
            raise ValueError("chunk_size must be <= 1000 (SF $filter 'in' limit)")
        if not values:
            return {
                "chunks_processed": 0,
                "total_pages_fetched": 0,
                "total_records": 0,
                "results": [],
                "chunk_diagnostics": [],
            }

        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped = [v for v in values if not (v in seen or seen.add(v))]

        base_params = dict(params or {})
        base_filter = base_params.pop("$filter", "")

        chunks: list[list[str]] = []
        current: list[str] = []
        for value in deduped:
            candidate = current + [value]
            over_count = len(candidate) > chunk_size
            over_length = (
                current
                and _encoded_len(_combined_filter(base_filter, column, candidate))
                > _MAX_FILTER_ENCODED_LEN
            )
            if current and (over_count or over_length):
                chunks.append(current)
                current = [value]
            else:
                current = candidate
        if current:
            chunks.append(current)

        all_results: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        total_pages = 0

        for index, chunk in enumerate(chunks):
            chunk_params = {**base_params, "$filter": _combined_filter(base_filter, column, chunk)}
            result = await self.extract_all(
                path=path,
                conn=conn,
                params=chunk_params,
                max_pages=max_pages_per_chunk,
            )
            all_results.extend(result["results"])
            total_pages += result["pages_fetched"]
            diagnostics.append(
                {
                    "chunk_index": index,
                    "chunk_size": len(chunk),
                    "records_fetched": len(result["results"]),
                    "pages_fetched": result["pages_fetched"],
                    "stopped_reason": result["stopped_reason"],
                }
            )

        return {
            "chunks_processed": len(diagnostics),
            "total_pages_fetched": total_pages,
            "total_records": len(all_results),
            "results": all_results,
            "chunk_diagnostics": diagnostics,
        }
