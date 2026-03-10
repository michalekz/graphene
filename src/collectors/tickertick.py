"""
TickerTick API collector for graphene-intel.

Queries the free TickerTick news aggregation API for each ticker in sources.yaml
(news_apis → tickertick section). No API key is required.

API format:
    GET https://api.tickertick.com/feed?q=tt:TICKER&n=50
    Response: {"feed": [{"id", "tt", "title", "url", "time", "tags", ...}, ...]}

Rate limit is 10 req/min (6 s between requests to api.tickertick.com), which is
already enforced by the shared RateLimiter in src/utils/http.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import yaml

from src.collectors.base import BaseCollector
from src.db.store import Headline
from src.utils.http import fetch_json

logger = logging.getLogger(__name__)

_SOURCES_PATH = "/opt/grafene/config/sources.yaml"
_API_BASE = "https://api.tickertick.com/feed"


def _load_tickertick_config() -> dict[str, Any]:
    """Load the tickertick section from sources.yaml."""
    with open(_SOURCES_PATH) as fh:
        config = yaml.safe_load(fh)
    for entry in config.get("news_apis", []):
        if entry.get("name") == "tickertick":
            return entry
    return {}


def _parse_timestamp(ms: int | None) -> datetime | None:
    """Convert a Unix millisecond timestamp to a UTC-aware datetime."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _extract_tickers_from_tags(tags: list[str], query_ticker: str) -> list[str]:
    """Build the tickers list for a headline.

    TickerTick tags are strings like ``"tt:AAPL"`` or plain strings.
    We extract the ticker symbol after the colon.  The query ticker is
    always included so there is at least one association.

    Args:
        tags: Raw tag list from the API response item.
        query_ticker: The ticker symbol used for this query.

    Returns:
        Deduplicated list of uppercase ticker symbols.
    """
    tickers: set[str] = {query_ticker.upper()}
    for tag in tags or []:
        if isinstance(tag, str) and ":" in tag:
            symbol = tag.split(":", 1)[1].strip().upper()
            if symbol:
                tickers.add(symbol)
    return sorted(tickers)


class TickerTickCollector(BaseCollector):
    """Collects news from the free TickerTick aggregation API.

    Iterates over all tickers listed in the ``news_apis → tickertick`` section
    of sources.yaml and fetches the most recent items for each one.
    """

    name = "tickertick"

    def __init__(self) -> None:
        self._config = _load_tickertick_config()
        self._tickers: list[str] = self._config.get("tickers", [])
        self._items_per_ticker: int = self._config.get("items_per_ticker", 50)

    async def _fetch_ticker(self, ticker: str) -> list[Headline]:
        """Fetch headlines for a single ticker symbol.

        Args:
            ticker: Uppercase ticker symbol, e.g. ``"HGRAF"``.

        Returns:
            List of Headline objects parsed from the API response.
            Returns an empty list on any error.
        """
        url = _API_BASE
        params = {"q": f"tt:{ticker}", "n": self._items_per_ticker}

        try:
            data = await fetch_json(url, params=params)
        except Exception:
            logger.exception("[tickertick] Failed to fetch ticker=%s", ticker)
            return []

        if not isinstance(data, dict):
            logger.warning("[tickertick] Unexpected response type for ticker=%s", ticker)
            return []

        items = data.get("feed", [])
        if not isinstance(items, list):
            logger.warning("[tickertick] 'feed' is not a list for ticker=%s", ticker)
            return []

        headlines: list[Headline] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url_val = item.get("url", "").strip()
            title_val = item.get("title", "").strip()
            if not url_val or not title_val:
                continue

            # 'tt' field is the source site domain / publisher
            source = item.get("tt", "tickertick").strip() or "tickertick"
            published_at = _parse_timestamp(item.get("time"))
            tags = item.get("tags") or []
            tickers = _extract_tickers_from_tags(tags, ticker)

            headlines.append(
                Headline(
                    url=url_val,
                    title=title_val,
                    source=source,
                    published_at=published_at,
                    tickers=tickers,
                    category="news",
                    raw_content=None,
                )
            )

        logger.debug(
            "[tickertick] ticker=%s → %d items parsed", ticker, len(headlines)
        )
        return headlines

    async def collect(self, primary_only: bool = False) -> list[Headline]:
        """Fetch headlines for configured tickers (one API request per ticker).

        OR-query syntax is not supported by TickerTick API (returns 400).
        Sequential per-ticker requests with 12s rate limit between them stay
        well within the 10 req/min API limit in production (30-min cron runs).

        Args:
            primary_only: If True, only query HGRAF + BSWGF (first 2 tickers).
                         Useful for more frequent lightweight runs.

        Returns:
            Deduplicated list of Headline objects.
        """
        if not self._tickers:
            logger.warning("[tickertick] No tickers configured in sources.yaml")
            return []

        tickers = self._tickers[:2] if primary_only else self._tickers
        all_headlines: list[Headline] = []
        seen_urls: set[str] = set()

        for ticker in tickers:
            try:
                batch = await self._fetch_ticker(ticker)
            except Exception:
                logger.exception("[tickertick] Unexpected error for ticker=%s", ticker)
                batch = []

            for h in batch:
                if h.url not in seen_urls:
                    seen_urls.add(h.url)
                    all_headlines.append(h)

        logger.info(
            "[tickertick] Collected %d unique headlines across %d tickers",
            len(all_headlines),
            len(tickers),
        )
        return all_headlines
