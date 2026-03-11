"""
Shared async HTTP client with:
- Automatic retries (exponential backoff)
- Rate limiting per domain
- User-Agent rotation
- Timeout handling
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0  # seconds, doubles each retry

# Rate limiting: minimum seconds between requests to the same domain
DOMAIN_RATE_LIMITS: dict[str, float] = {
    "api.tickertick.com": 12.0,     # ~5 req/min (conservative for free tier)
    "news.google.com": 2.0,
    "www.globenewswire.com": 2.0,
    "www.newsfilecorp.com": 2.0,
    "www.graphene-info.com": 5.0,
    "api.stocktwits.com": 3.0,
    "data.sec.gov": 0.1,             # SEC allows 10 req/s
    "search.patentsview.org": 1.0,
    "trends.google.com": 60.0,       # pytrends is very rate-limited
    "api.finra.org": 2.0,            # FINRA public API — be polite
    "app-money.tmx.com": 3.0,        # TMX Money GraphQL (SEDI insider data)
    "blackswangraphene.com": 5.0,    # Company website RSS
    "www.directa-plus.com": 5.0,     # Company website WP REST API
    "www.hydrograph.co": 5.0,        # Company website
    "www.sec.gov": 0.5,              # SEC general website (halts page)
    "default": 1.0,
}

HEADERS = {
    "User-Agent": (
        "GrapheneIntel/0.1 (+https://github.com/michalekz/graphene-intel; "
        "research-bot; contact: zdenek.michalek@gmail.com)"
    ),
    "Accept": "application/json, text/html, application/rss+xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# SEC EDGAR requires specific User-Agent format "AppName email" — no browser UA
SEC_EDGAR_HEADERS = {
    "User-Agent": "GrapheneIntel zdenek.michalek@gmail.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, application/atom+xml, */*",
}

def get_headers(url: str) -> dict[str, str]:
    """Return appropriate headers for the given URL."""
    if "sec.gov" in url or "edgar" in url.lower():
        return SEC_EDGAR_HEADERS
    return HEADERS


class RateLimiter:
    """Per-domain rate limiter using token bucket (simplified)."""

    def __init__(self) -> None:
        self._last_call: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _min_interval(self, domain: str) -> float:
        return DOMAIN_RATE_LIMITS.get(domain, DOMAIN_RATE_LIMITS["default"])

    async def acquire(self, domain: str) -> None:
        async with self._locks[domain]:
            elapsed = time.monotonic() - self._last_call[domain]
            wait = self._min_interval(domain) - elapsed
            if wait > 0:
                logger.debug("Rate limiting %s — waiting %.1fs", domain, wait)
                await asyncio.sleep(wait)
            self._last_call[domain] = time.monotonic()


_rate_limiter = RateLimiter()


def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return "unknown"


async def fetch_json(
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """Fetch URL, parse JSON. Retries on transient errors."""
    domain = _extract_domain(url)
    merged_headers = {**HEADERS, **(headers or {})}

    for attempt in range(1, retries + 1):
        await _rate_limiter.acquire(domain)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, params=params, headers=merged_headers)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 503) and attempt < retries:
                wait = DEFAULT_BACKOFF ** attempt
                logger.warning(
                    "HTTP %d from %s, retry %d/%d in %.0fs",
                    e.response.status_code, domain, attempt, retries, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error("HTTP error fetching %s: %s", url, e)
                raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < retries:
                wait = DEFAULT_BACKOFF ** attempt
                logger.warning("Network error %s, retry %d/%d in %.0fs: %s", domain, attempt, retries, wait, e)
                await asyncio.sleep(wait)
            else:
                logger.error("Failed to fetch %s after %d attempts: %s", url, retries, e)
                raise

    raise RuntimeError(f"fetch_json: all {retries} attempts failed for {url}")


async def fetch_text(
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Fetch URL, return raw text (for RSS parsing)."""
    domain = _extract_domain(url)
    merged_headers = {**HEADERS, **(headers or {}), "Accept": "text/html, application/rss+xml, */*"}

    for attempt in range(1, retries + 1):
        await _rate_limiter.acquire(domain)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, params=params, headers=merged_headers)
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 503) and attempt < retries:
                wait = DEFAULT_BACKOFF ** attempt
                await asyncio.sleep(wait)
            else:
                logger.error("HTTP error fetching %s: %s", url, e)
                raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < retries:
                await asyncio.sleep(DEFAULT_BACKOFF ** attempt)
            else:
                logger.error("Failed to fetch text %s: %s", url, e)
                raise

    raise RuntimeError(f"fetch_text: all {retries} attempts failed for {url}")
