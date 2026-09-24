"""Shared async HTTP client.

One pooled ``httpx.AsyncClient`` is reused by every provider (keep-alive connections
remove a TLS handshake from nearly every call - the single biggest latency win over
creating a client per request).  All transport / status errors are mapped onto the
``ProviderError`` hierarchy so nothing from httpx ever leaks upward.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import AuthError, NoDataError, ProviderError, ProviderTimeout, RateLimited

log = logging.getLogger(__name__)


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class HttpClient:
    def __init__(self, timeout: float = 8.0, connect_timeout: float = 4.0, user_agent: str = "ABGTerminal/3.0",
                 max_connections: int = 32, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            headers={"User-Agent": user_agent, "Accept": "application/json, text/csv, */*"},
            follow_redirects=True,
            transport=transport,
        )

    @property
    def raw(self) -> httpx.AsyncClient:
        return self._client

    async def request(self, method: str, url: str, *, provider: str, params: dict | None = None,
                      headers: dict | None = None, json: Any = None, timeout: float | None = None) -> httpx.Response:
        try:
            resp = await self._client.request(method, url, params=params, headers=headers, json=json,
                                              timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(f"timeout: {type(e).__name__}", provider=provider) from None
        except httpx.HTTPError as e:
            raise ProviderError(f"network error: {type(e).__name__}: {e}", provider=provider) from None
        s = resp.status_code
        if s < 400:
            return resp
        body = resp.text[:200].replace("\n", " ")
        if s == 429:
            raise RateLimited(f"HTTP 429 {body}", provider=provider, retry_after=_retry_after(resp))
        if s in (401, 402, 403):
            raise AuthError(f"HTTP {s} (check API key / plan) {body}", provider=provider)
        if s == 404:
            raise NoDataError(f"HTTP 404 {body}", provider=provider)
        if s >= 500:
            raise ProviderError(f"HTTP {s} {body}", provider=provider)
        err = ProviderError(f"HTTP {s} {body}", provider=provider)
        err.retryable = False
        raise err

    async def get_json(self, url: str, *, provider: str, params: dict | None = None,
                       headers: dict | None = None, timeout: float | None = None) -> Any:
        resp = await self.request("GET", url, provider=provider, params=params, headers=headers, timeout=timeout)
        try:
            return resp.json()
        except ValueError:
            raise ProviderError(f"non-JSON response: {resp.text[:120]!r}", provider=provider) from None

    async def get_text(self, url: str, *, provider: str, params: dict | None = None,
                       headers: dict | None = None, timeout: float | None = None) -> str:
        resp = await self.request("GET", url, provider=provider, params=params, headers=headers, timeout=timeout)
        return resp.text

    async def post_json(self, url: str, *, provider: str, json: Any, headers: dict | None = None,
                        timeout: float | None = None) -> Any:
        resp = await self.request("POST", url, provider=provider, json=json, headers=headers, timeout=timeout)
        try:
            return resp.json()
        except ValueError:
            raise ProviderError(f"non-JSON response: {resp.text[:120]!r}", provider=provider) from None

    async def aclose(self) -> None:
        await self._client.aclose()
