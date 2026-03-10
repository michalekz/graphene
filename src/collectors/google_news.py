"""
Google News RSS collector for graphene-intel.

A focused collector that only handles the ``google_news`` section of
sources.yaml, as a standalone alternative to the full RSSCollector.

For each configured query string, it builds a Google News RSS URL:

    https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en

…fetches the feed, parses it with feedparser (via asyncio.to_thread),
infers ticker associations from title text, and returns Headline objects.

This collector can be used independently (e.g. on a different schedule) or
alongside RSSCollector with deduplication handled by Store.insert_headline().
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import feedparser
import yaml

from src.collectors.base import BaseCollector
from src.db.store import Headline
from src.utils.http import fetch_text

logger = logging.getLogger(__name__)

_SOURCES_PATH = "/opt/grafene/config/sources.yaml"
_TICKERS_PATH = "/opt/grafene/config/tickers.yaml"

_GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_google_news_config() -> dict[str, Any]:
    """Return the ``google_news`` section of sources.yaml."""
    with open(_SOURCES_PATH) as fh:
        config = yaml.safe_load(fh) or {}
    return config.get("google_news", {})


def _load_keyword_map() -> dict[str, list[str]]:
    """Build {ticker: [keyword, ...]} from tickers.yaml for ticker inference."""
    with open(_TICKERS_PATH) as fh:
        tickers_config = yaml.safe_load(fh) or {}

    keyword_map: dict[str, list[str]] = {}
    for section in ("primary", "competitors"):
        for entry in tickers_config.get(section, []):
            ticker = entry.get("ticker", "").upper()
            if not ticker:
                continue
            keywords: list[str] = [kw for kw in entry.get("keywords", []) if kw]
            if ticker not in keywords:
                keywords.append(ticker)
            name = entry.get("name", "")
            if name and name not in keywords:
                keywords.append(name)
            keyword_map[ticker] = keywords
    return keyword_map


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_entry_datetime(entry: Any) -> datetime | None:
    """Extract a UTC-aware datetime from a feedparser entry.

    Tries ``published_parsed`` (time.struct_time in UTC) first, then falls
    back to parsing the raw ``published`` RFC-2822 string.
    """
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed is not None:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except Exception:
            pass

    return None


def _infer_tickers(title: str, keyword_map: dict[str, list[str]]) -> list[str]:
    """Return tickers whose keywords are found in ``title`` (case-insensitive).

    Args:
        title: Article title text to search.
        keyword_map: {ticker: [keyword, ...]} mapping.

    Returns:
        Sorted list of matching ticker symbols.
    """
    title_lower = title.lower()
    matched: list[str] = []
    for ticker, keywords in keyword_map.items():
        for kw in keywords:
            if kw.lower() in title_lower:
                matched.append(ticker)
                break
    return sorted(matched)


def _parse_feed_sync(raw_text: str) -> Any:
    """Thin synchronous wrapper around feedparser.parse() for use with to_thread."""
    return feedparser.parse(raw_text)


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────


class GoogleNewsCollector(BaseCollector):
    """Collects news headlines from Google News RSS feeds.

    Reads the ``google_news`` section from sources.yaml to obtain the list of
    search queries and URL parameters.  For each query it builds a Google News
    RSS URL, fetches the raw XML, and parses it with feedparser.

    Ticker associations are inferred by matching each headline's title against
    the keyword lists in tickers.yaml.
    """

    name = "google_news"

    def __init__(self) -> None:
        self._config = _load_google_news_config()
        self._keyword_map = _load_keyword_map()
        params = self._config.get("params", {})
        self._hl: str = params.get("hl", "en-US")
        self._gl: str = params.get("gl", "US")
        self._ceid: str = params.get("ceid", "US:en")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_url(self, query: str) -> str:
        """Construct the Google News RSS URL for a search query.

        Args:
            query: Raw search query string (e.g. ``'"HydroGraph Clean Power"'``).

        Returns:
            Fully-formed URL ready for fetching.
        """
        encoded = quote_plus(query)
        return (
            f"{_GOOGLE_NEWS_BASE}"
            f"?q={encoded}"
            f"&hl={self._hl}"
            f"&gl={self._gl}"
            f"&ceid={self._ceid}"
        )

    async def _fetch_query(self, query: str) -> list[Headline]:
        """Fetch and parse a single Google News RSS query.

        Args:
            query: Search query string.

        Returns:
            List of Headline objects parsed from the feed.  Returns an empty
            list on any error (fetch failure, parse error, etc.).
        """
        url = self._build_url(query)
        source_label = f"google_news:{query}"

        try:
            raw = await fetch_text(url)
        except Exception:
            logger.exception(
                "[google_news] Failed to fetch query=%r url=%s", query, url
            )
            return []

        try:
            feed = await asyncio.to_thread(_parse_feed_sync, raw)
        except Exception:
            logger.exception(
                "[google_news] feedparser error for query=%r url=%s", query, url
            )
            return []

        if feed.get("bozo") and not feed.entries:
            logger.warning(
                "[google_news] Bozo (malformed) feed for query=%r — skipping", query
            )
            return []

        headlines: list[Headline] = []
        for entry in feed.entries:
            entry_url = getattr(entry, "link", "").strip()
            entry_title = getattr(entry, "title", "").strip()
            if not entry_url or not entry_title:
                continue

            summary = getattr(entry, "summary", "") or ""
            published_at = _parse_entry_datetime(entry)
            tickers = _infer_tickers(entry_title, self._keyword_map)

            headlines.append(
                Headline(
                    url=entry_url,
                    title=entry_title,
                    source=source_label,
                    published_at=published_at,
                    tickers=tickers,
                    category="news",
                    raw_content=summary if summary else None,
                )
            )

        logger.debug(
            "[google_news] query=%r → %d entries parsed", query, len(headlines)
        )
        return headlines

    # ── Public API ────────────────────────────────────────────────────────────

    async def collect(self) -> list[Headline]:
        """Fetch all configured Google News RSS queries.

        Queries are processed sequentially so the shared RateLimiter for
        ``news.google.com`` (2 s between requests) is respected without extra
        coordination.

        Returns:
            Deduplicated list of Headline objects across all queries.
            An empty list is returned if every query fails.
        """
        queries: list[str] = [
            str(q).strip()
            for q in self._config.get("queries", [])
            if str(q).strip()
        ]

        if not queries:
            logger.warning("[google_news] No queries configured in sources.yaml")
            return []

        all_headlines: list[Headline] = []
        seen_urls: set[str] = set()

        for query in queries:
            try:
                batch = await self._fetch_query(query)
            except Exception:
                logger.exception(
                    "[google_news] Unexpected error for query=%r", query
                )
                batch = []

            for h in batch:
                if h.url not in seen_urls:
                    seen_urls.add(h.url)
                    all_headlines.append(h)

        logger.info(
            "[google_news] Collected %d unique headlines from %d queries",
            len(all_headlines),
            len(queries),
        )
        return all_headlines
