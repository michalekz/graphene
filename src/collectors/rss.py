"""
Generic RSS/Atom feed collector for graphene-intel.

Reads all feeds from the ``rss_feeds`` section of sources.yaml and also
processes the ``google_news`` section (Google News RSS queries).

Ticker inference:
    Since RSS feeds do not carry structured ticker metadata, each article's
    title and summary are matched against the ``keywords`` lists defined in
    tickers.yaml.  Any ticker whose keyword appears (case-insensitive) in the
    text is included in the headline's ``tickers`` list.

Category mapping:
    - globenewswire / newsfile sources → "press_release"
    - phys.org / nanowerk sources      → "research"
    - Everything else                  → "news"

feedparser is a synchronous library; all parse calls are wrapped with
``asyncio.to_thread()`` to avoid blocking the event loop.
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

# Source-name substrings that determine category override
_PRESS_RELEASE_SOURCES = ("globenewswire", "newsfile")
_RESEARCH_SOURCES = ("phys.org", "nanowerk", "phys_org")


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_sources() -> dict[str, Any]:
    with open(_SOURCES_PATH) as fh:
        return yaml.safe_load(fh) or {}


def _load_tickers() -> dict[str, Any]:
    with open(_TICKERS_PATH) as fh:
        return yaml.safe_load(fh) or {}


def _build_keyword_map(tickers_config: dict[str, Any]) -> dict[str, list[str]]:
    """Return {ticker_symbol: [keyword, ...]} for all configured tickers.

    Covers the ``primary`` and ``competitors`` sections of tickers.yaml.
    """
    keyword_map: dict[str, list[str]] = {}
    for section in ("primary", "competitors"):
        for entry in tickers_config.get(section, []):
            ticker = entry.get("ticker", "").upper()
            if not ticker:
                continue
            keywords = [kw for kw in entry.get("keywords", []) if kw]
            # Always include the ticker symbol itself as a fallback keyword
            if ticker not in keywords:
                keywords.append(ticker)
            # Include canonical name as a keyword too
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

    feedparser may provide ``published_parsed`` (a time.struct_time in UTC)
    or a raw ``published`` RFC-2822 string.  We try both.
    """
    # Preferred: already-parsed struct_time (UTC)
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed is not None:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    # Fallback: raw RFC-2822 string
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    return None


def _infer_tickers(
    text: str,
    keyword_map: dict[str, list[str]],
) -> list[str]:
    """Return tickers whose keywords appear in ``text`` (case-insensitive).

    Args:
        text: Combined title + summary to search.
        keyword_map: {ticker: [keyword, ...]} from tickers.yaml.

    Returns:
        Sorted list of matching ticker symbols.
    """
    text_lower = text.lower()
    matched: list[str] = []
    for ticker, keywords in keyword_map.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                matched.append(ticker)
                break  # one match per ticker is enough
    return sorted(matched)


def _category_for_source(source_name: str, source_url: str) -> str:
    """Determine article category based on source name or URL."""
    combined = (source_name + source_url).lower()
    for marker in _PRESS_RELEASE_SOURCES:
        if marker in combined:
            return "press_release"
    for marker in _RESEARCH_SOURCES:
        if marker in combined:
            return "research"
    return "news"


def _parse_feed_sync(raw_text: str) -> Any:
    """Synchronous feedparser call — run inside asyncio.to_thread()."""
    return feedparser.parse(raw_text)


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────


class RSSCollector(BaseCollector):
    """Collects headlines from all RSS/Atom feeds listed in sources.yaml.

    Covers both the ``rss_feeds`` static list and the ``google_news`` query
    section (which produces Google News RSS URLs at runtime).
    """

    name = "rss"

    def __init__(self) -> None:
        sources = _load_sources()
        self._rss_feeds: list[dict[str, Any]] = sources.get("rss_feeds", [])
        self._google_news_config: dict[str, Any] = sources.get("google_news", {})
        self._keyword_map = _build_keyword_map(_load_tickers())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _google_news_url(self, query: str) -> str:
        """Build a Google News RSS URL for the given query string."""
        config = self._google_news_config
        base = config.get("base_url", "https://news.google.com/rss/search")
        params = config.get("params", {})
        hl = params.get("hl", "en-US")
        gl = params.get("gl", "US")
        ceid = params.get("ceid", "US:en")
        encoded_query = quote_plus(query)
        return f"{base}?q={encoded_query}&hl={hl}&gl={gl}&ceid={ceid}"

    async def _fetch_and_parse(
        self,
        url: str,
        source_name: str,
        category: str,
    ) -> list[Headline]:
        """Fetch ``url``, parse it with feedparser, and return Headline objects.

        Args:
            url: Feed URL to fetch.
            source_name: Human-readable name for logging and Headline.source.
            category: Default category string (may be overridden per entry).

        Returns:
            Parsed headlines.  Never raises — errors are logged and an empty
            list is returned.
        """
        try:
            raw = await fetch_text(url)
        except Exception:
            logger.exception("[rss] Failed to fetch feed name=%s url=%s", source_name, url)
            return []

        try:
            feed = await asyncio.to_thread(_parse_feed_sync, raw)
        except Exception:
            logger.exception("[rss] feedparser error for name=%s url=%s", source_name, url)
            return []

        if feed.get("bozo") and not feed.entries:
            # bozo=True with no entries usually means a fatal parse error
            logger.warning(
                "[rss] bozo feed (parse error) name=%s url=%s — skipping",
                source_name,
                url,
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
            search_text = f"{entry_title} {summary}"
            tickers = _infer_tickers(search_text, self._keyword_map)

            # Allow per-entry category override based on the actual source URL
            effective_category = _category_for_source(source_name, entry_url)
            if effective_category == "news" and category != "news":
                # Prefer the feed-level category if more specific
                effective_category = category

            headlines.append(
                Headline(
                    url=entry_url,
                    title=entry_title,
                    source=source_name,
                    published_at=published_at,
                    tickers=tickers,
                    category=effective_category,
                    raw_content=summary if summary else None,
                )
            )

        logger.debug(
            "[rss] name=%s → %d entries parsed", source_name, len(headlines)
        )
        return headlines

    # ── Public API ────────────────────────────────────────────────────────────

    async def collect(self) -> list[Headline]:
        """Fetch all configured RSS feeds and Google News RSS queries.

        Processes static ``rss_feeds`` entries first, then each query from the
        ``google_news`` section.  All requests run sequentially (Google News
        has a 2 s inter-request rate limit already encoded in the shared
        RateLimiter).

        Returns:
            Deduplicated list of Headline objects.  An empty list is returned
            if every feed fails.
        """
        all_headlines: list[Headline] = []
        seen_urls: set[str] = set()

        # 1. Static RSS/Atom feeds
        for feed_cfg in self._rss_feeds:
            url = feed_cfg.get("url", "").strip()
            if not url:
                continue
            source_name = feed_cfg.get("name", url)
            raw_category = feed_cfg.get("category", "news")
            # Normalise category names from sources.yaml to our internal values
            if raw_category in ("press_releases", "filings"):
                category = "press_release"
            elif raw_category == "research":
                category = "research"
            else:
                category = "news"

            try:
                batch = await self._fetch_and_parse(url, source_name, category)
            except Exception:
                logger.exception("[rss] Unexpected error for feed name=%s", source_name)
                batch = []

            for h in batch:
                if h.url not in seen_urls:
                    seen_urls.add(h.url)
                    all_headlines.append(h)

        # 2. Google News RSS queries
        for query in self._google_news_config.get("queries", []):
            query_str = str(query).strip()
            if not query_str:
                continue
            gn_url = self._google_news_url(query_str)
            source_name = f"google_news:{query_str}"

            try:
                batch = await self._fetch_and_parse(gn_url, source_name, "news")
            except Exception:
                logger.exception("[rss] Unexpected error for google_news query=%s", query_str)
                batch = []

            for h in batch:
                if h.url not in seen_urls:
                    seen_urls.add(h.url)
                    all_headlines.append(h)

        logger.info(
            "[rss] Collected %d unique headlines from %d static feeds + %d Google News queries",
            len(all_headlines),
            len(self._rss_feeds),
            len(self._google_news_config.get("queries", [])),
        )
        return all_headlines
