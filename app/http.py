"""Shared outbound HTTP plumbing.

One httpx.AsyncClient for the whole app, concurrency capped by a global
asyncio.Semaphore(5). Every external call goes through request() which adds
one retry on transient failures. Never logs secrets.
"""
import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("jellylook.http")

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
LLM_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
MAX_CONCURRENCY = 5

_client: httpx.AsyncClient | None = None
_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


async def startup() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        log.info("shared http client started (max concurrency %d)", MAX_CONCURRENCY)


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        log.info("shared http client closed")


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("http client not started — call http.startup() first")
    return _client


async def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: httpx.Timeout | None = None,
    retries: int = 1,
) -> httpx.Response:
    """Semaphore-capped request with one retry on transient failure.

    Raises httpx.HTTPError on final failure; callers decide how to degrade.
    Does not raise for HTTP status — callers check response.status_code.
    """
    client = get_client()
    attempt = 0
    while True:
        try:
            async with _semaphore:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json,
                    timeout=timeout or DEFAULT_TIMEOUT,
                )
            if resp.status_code in TRANSIENT_STATUS and attempt < retries:
                attempt += 1
                log.warning("transient %s from %s — retry %d", resp.status_code,
                            _safe_url(url), attempt)
                await asyncio.sleep(1.5 * attempt)
                continue
            return resp
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt < retries:
                attempt += 1
                log.warning("transport error on %s (%s) — retry %d",
                            _safe_url(url), type(exc).__name__, attempt)
                await asyncio.sleep(1.5 * attempt)
                continue
            raise


def _safe_url(url: str) -> str:
    """Strip query strings so API keys never reach the logs."""
    return url.split("?", 1)[0]
