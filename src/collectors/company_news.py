"""
Direct company IR / press-release collector for Graphene Intel.

Scrapes investor-relations news from company websites using two strategies:

  1. RSS/Atom feed  — for companies that publish a public feed
  2. WordPress REST API — for companies that run WordPress IR sites

Both strategies produce Headline objects with category="press_release" so the
scorer treats them as higher-quality primary sources.

New companies are added via the COMPANY_SOURCES list below.  No external API
keys are required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from src.db.store import Headline, Store

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Company source definitions
# ─────────────────────────────────────────────────────────────────────────────

COMPANY_SOURCES: list[dict[str, Any]] = [
    # ── Primary ────────────────────────────────────────────────────────────────
    {
        "ticker": "HGRAF",
        "name": "HydroGraph Clean Power",
        "type": "rss",
        "url": "https://hydrograph.com/feed/",
        # Note: hydrograph.co (old domain) unreachable; hydrograph.com works
    },
    {
        "ticker": "BSWGF",
        "name": "Black Swan Graphene",
        "type": "rss",
        "url": "https://blackswangraphene.com/feed/",
    },
    # ── Competitors ────────────────────────────────────────────────────────────
    {
        "ticker": "NNXPF",
        "name": "NanoXplore",
        "type": "wp_rest",
        "url": "https://nanoxplore.com/wp-json/wp/v2/posts",
        "params": {"per_page": 10, "_fields": "id,date,title,link,excerpt"},
        # RSS feed exists but returns 0 items; WP REST works
    },
    {
        "ticker": "GMGMF",
        "name": "Graphene Manufacturing Group",
        "type": "rss",
        "url": "https://www.graphenemg.com/feed/",
    },
    {
        "ticker": "ARLSF",
        "name": "Argo Graphene",
        "type": "rss",
        "url": "https://argographene.com/feed/",
    },
    {
        "ticker": "CVV",
        "name": "CVD Equipment Corporation",
        "type": "rss",
        "url": "https://cvdequipment.com/feed/",
    },
    {
        "ticker": "FGPHF",
        "name": "First Graphene",
        "type": "rss",
        "url": "https://firstgraphene.net/feed/",
    },
    {
        "ticker": "DTPKF",
        "name": "Directa Plus",
        "type": "wp_rest",
        "url": "https://www.directa-plus.com/wp-json/wp/v2/posts",
        "params": {"per_page": 10, "_fields": "id,date,title,link,excerpt"},
    },
    # ZTEK (Zentek): zentek.com unreachable — covered by Google News + TickerTick
]

_MAX_AGE_DAYS = 90  # ignore articles older than this (company IR RSS often lags)


def _parse_rss_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return None


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text).strip()


async def _collect_rss(source: dict[str, Any]) -> list[Headline]:
    """Fetch and parse an RSS/Atom feed, return list of Headlines."""
    ticker = source["ticker"]
    url = source["url"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)
    headlines: list[Headline] = []

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "GrapheneIntel/0.1 research-bot"},
            )
            resp.raise_for_status()
            content = resp.text
    except Exception as exc:
        logger.error("[company_news] RSS fetch failed for %s (%s): %s", ticker, url, exc)
        return []

    feed = feedparser.parse(content)
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        pub_str = entry.get("published") or entry.get("updated")
        pub_dt = _parse_rss_date(pub_str)
        if pub_dt and pub_dt < cutoff:
            continue

        raw = _strip_html(entry.get("summary", ""))

        headlines.append(
            Headline(
                url=link,
                title=title,
                source=f"company_rss_{ticker.lower()}",
                published_at=pub_dt,
                tickers=[ticker],
                category="press_release",
                raw_content=raw[:2000] if raw else None,
            )
        )

    logger.info("[company_news] RSS %s (%s): %d items", ticker, url, len(headlines))
    return headlines


async def _collect_wp_rest(source: dict[str, Any]) -> list[Headline]:
    """Fetch posts via WordPress REST API, return list of Headlines."""
    ticker = source["ticker"]
    url = source["url"]
    params = source.get("params", {"per_page": 10})
    cutoff = datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)
    headlines: list[Headline] = []

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"User-Agent": "GrapheneIntel/0.1 research-bot"},
            )
            resp.raise_for_status()
            posts: list[dict[str, Any]] = resp.json()
    except Exception as exc:
        logger.error("[company_news] WP REST failed for %s (%s): %s", ticker, url, exc)
        return []

    if not isinstance(posts, list):
        logger.warning("[company_news] WP REST unexpected response type for %s", ticker)
        return []

    for post in posts:
        title_raw = post.get("title", {})
        title = _strip_html(
            title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)
        ).strip()
        link = post.get("link", "").strip()
        if not title or not link:
            continue

        date_str = post.get("date", "")
        try:
            pub_dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except Exception:
            pub_dt = None

        if pub_dt and pub_dt < cutoff:
            continue

        excerpt_raw = post.get("excerpt", {})
        excerpt = _strip_html(
            excerpt_raw.get("rendered", "") if isinstance(excerpt_raw, dict) else ""
        )

        headlines.append(
            Headline(
                url=link,
                title=title,
                source=f"company_wp_{ticker.lower()}",
                published_at=pub_dt,
                tickers=[ticker],
                category="press_release",
                raw_content=excerpt[:2000] if excerpt else None,
            )
        )

    logger.info("[company_news] WP REST %s (%s): %d items", ticker, url, len(posts))
    return headlines


async def collect_company_news(store: Store) -> int:
    """Collect IR news from all company sources.

    Returns:
        Number of newly inserted headlines.
    """
    total = 0
    for source in COMPANY_SOURCES:
        try:
            if source["type"] == "rss":
                headlines = await _collect_rss(source)
            elif source["type"] == "wp_rest":
                headlines = await _collect_wp_rest(source)
            else:
                logger.warning("[company_news] Unknown source type: %s", source["type"])
                continue

            for hl in headlines:
                row_id = await store.insert_headline(hl)
                if row_id is not None:
                    total += 1

        except Exception as exc:
            logger.error(
                "[company_news] Unexpected error for %s: %s",
                source.get("ticker", "?"), exc,
            )

    logger.info("[company_news] Done — %d new headlines inserted", total)
    return total
