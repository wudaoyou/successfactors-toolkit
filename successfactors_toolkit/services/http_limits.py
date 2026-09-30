"""Bounds on what one call may read, keep in memory, or spend time on.

Every limit ends in `LimitExceededError` with a message naming the limit,
never in a silently shortened result.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from fastapi import HTTPException

from successfactors_toolkit.config import Settings

# Wall-clock ceiling for one upstream request including its retries and waits.
# httpx timeouts apply per phase, so a slow trickle never trips them.
MAX_REQUEST_SECONDS = 600.0
# Ceiling for the time one request spends sleeping on 429 Retry-After waits.
MAX_RETRY_SLEEP_SECONDS = 300.0

_T = TypeVar("_T")


class LimitExceededError(HTTPException):
    """A size or time limit stopped the call. An HTTPException so REST callers
    get the status code (413 / 504); MCP callers get the message."""

    def __str__(self) -> str:
        return str(self.detail)


@dataclass
class CappedResponse:
    status_code: int
    headers: httpx.Headers
    text: str


async def send_capped(
    client: httpx.AsyncClient, method: str, url: str, max_bytes: int, **kwargs: Any
) -> CappedResponse:
    """`client.request` that stops reading once the decoded body passes `max_bytes`."""
    async with client.stream(method, url, **kwargs) as resp:
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise LimitExceededError(
                    413,
                    f"Response from SuccessFactors exceeded {max_bytes} bytes; "
                    "narrow the query ($filter, $select, smaller $top) or raise MAX_RESPONSE_BYTES.",
                )
        try:
            text = body.decode(resp.encoding or "utf-8", errors="replace")
        except LookupError:  # a charset Python has no codec for
            text = body.decode("utf-8", errors="replace")
        return CappedResponse(resp.status_code, resp.headers, text)


async def within_request_limit(work: Awaitable[_T]) -> _T:
    """Await `work` (one request with its retries) for at most MAX_REQUEST_SECONDS."""
    limit = asyncio.timeout(MAX_REQUEST_SECONDS)
    try:
        async with limit:
            return await work
    except TimeoutError:
        if not limit.expired():
            raise
        raise LimitExceededError(
            504, f"Request to SuccessFactors exceeded {MAX_REQUEST_SECONDS:.0f}s including retries."
        ) from None


class ExtractBudget:
    """Time and size allowance shared by every request of one multi-page extract."""

    def __init__(self, settings: Settings) -> None:
        self._seconds = settings.max_extract_seconds
        self._deadline = time.monotonic() + self._seconds
        self._bytes_left = settings.max_extract_bytes
        self._bytes = settings.max_extract_bytes

    def check_time(self) -> None:
        if time.monotonic() > self._deadline:
            raise LimitExceededError(
                504,
                f"Extract exceeded {self._seconds}s; narrow the query, fetch fewer pages, "
                "or raise MAX_EXTRACT_SECONDS.",
            )

    def spend(self, nbytes: int) -> None:
        self._bytes_left -= nbytes
        if self._bytes_left < 0:
            raise LimitExceededError(
                413,
                f"Extract exceeded {self._bytes} bytes of response data; narrow the query, "
                "fetch fewer pages, or raise MAX_EXTRACT_BYTES.",
            )
