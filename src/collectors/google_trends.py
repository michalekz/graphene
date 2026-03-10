"""
Google Trends weekly interest collector for graphene-intel.

Tracks relative search interest for graphene stock keywords over the past 90 days
using the pytrends library (unofficial Google Trends API). Detects anomalous spikes
in search volume that may signal retail investor attention.

IMPORTANT: Google Trends is heavily rate-limited. This collector:
  - Runs weekly (not every 30 minutes)
  - Uses asyncio.to_thread() for all synchronous pytrends calls
  - Sleeps 5–10 seconds between keyword batch requests
  - Catches TooManyRequestsError and returns empty gracefully

Run frequency: weekly.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from src.db.store import Headline, SentimentScore, Store

logger = logging.getLogger(__name__)

# ── Keywords and ticker mappings ──────────────────────────────────────────────

# Keywords to track, grouped into batches of ≤5 (pytrends limit per request)
KEYWORD_BATCHES: list[list[str]] = [
    ["HGRAF", "HydroGraph", "BSWGF"],
    ["graphene stocks", "graphene investment"],
]

# Keyword → ticker mapping for SentimentScore storage
KEYWORD_TICKER_MAP: dict[str, str] = {
    "HGRAF": "HGRAF",
    "HydroGraph": "HGRAF",
    "BSWGF": "BSWGF",
    "graphene stocks": "HGRAF",    # stored against primary ticker
    "graphene investment": "HGRAF",
}

# Spike detection: week-over-week change ratio that triggers a headline
SPIKE_THRESHOLD_PCT = 200.0   # >200% week-over-week increase

# Lookback window for trends data
TRENDS_TIMEFRAME = "today 3-m"   # past 90 days, weekly granularity


# ── Synchronous pytrends helpers (run in thread) ──────────────────────────────

def _build_pytrends_client() -> "pytrends.request.TrendReq":
    """Create a TrendReq instance with sensible timeouts."""
    from pytrends.request import TrendReq  # type: ignore[import]
    return TrendReq(
        hl="en-US",
        tz=0,
        timeout=(10, 25),
        retries=1,
        backoff_factor=2.0,
    )


def _fetch_interest_batch(
    keywords: list[str],
    timeframe: str,
) -> dict[str, list[tuple[str, int]]]:
    """
    Fetch weekly interest-over-time for a batch of keywords.

    Returns a dict mapping keyword → list of (date_str, value) tuples.
    Runs synchronously; call via asyncio.to_thread().
    """
    from pytrends.request import TrendReq  # type: ignore[import]
    import pandas as pd  # noqa: F401 — already a yfinance/pytrends dependency

    pt = _build_pytrends_client()
    pt.build_payload(keywords, cat=0, timeframe=timeframe, geo="", gprop="")
    df = pt.interest_over_time()

    result: dict[str, list[tuple[str, int]]] = {}
    if df is None or df.empty:
        return result

    for kw in keywords:
        if kw in df.columns:
            series = df[kw].dropna()
            result[kw] = [
                (str(idx.date()), int(val))
                for idx, val in series.items()
            ]
    return result


def _detect_spike(
    series: list[tuple[str, int]],
    threshold_pct: float = SPIKE_THRESHOLD_PCT,
) -> Optional[tuple[str, int, float]]:
    """
    Scan a time series for a week-over-week spike above the threshold.

    Returns (date_str, value, change_pct) for the most recent spike found,
    or None if no spike detected.
    """
    if len(series) < 2:
        return None

    # Check most recent week vs prior week
    recent_date, recent_val = series[-1]
    _, prior_val = series[-2]

    if prior_val <= 0:
        # Can't compute meaningful percentage from zero baseline
        return None

    change_pct = ((recent_val - prior_val) / prior_val) * 100.0
    if change_pct >= threshold_pct:
        return (recent_date, recent_val, change_pct)
    return None


# ── Async collection logic ────────────────────────────────────────────────────

async def _collect_batch(
    keywords: list[str],
    timeframe: str,
) -> dict[str, list[tuple[str, int]]]:
    """
    Fetch a keyword batch via asyncio.to_thread, with error handling.

    Returns empty dict on rate-limit or any other error.
    """
    try:
        result = await asyncio.to_thread(_fetch_interest_batch, keywords, timeframe)
        logger.info(
            "[google_trends] Fetched interest data for keywords: %s",
            ", ".join(keywords),
        )
        return result
    except Exception as exc:
        exc_name = type(exc).__name__
        if "TooManyRequests" in exc_name or "429" in str(exc):
            logger.warning(
                "[google_trends] Rate limited by Google Trends for %s — skipping batch",
                keywords,
            )
        else:
            logger.error(
                "[google_trends] Error fetching trends for %s: %s", keywords, exc
            )
        return {}


def _normalize_score(value: int, max_value: int = 100) -> float:
    """
    Normalize a Google Trends interest value (0–100) to a [-1.0, +1.0] score.

    We map 0→−1.0, 50→0.0, 100→+1.0 to align with SentimentScore convention.
    Above 75 is considered bullish territory.
    """
    if max_value <= 0:
        return 0.0
    normalized = value / max_value  # 0.0 – 1.0
    return round(normalized * 2.0 - 1.0, 4)  # -1.0 – +1.0


def _build_spike_headline(
    keyword: str,
    ticker: str,
    spike_date: str,
    spike_value: int,
    change_pct: float,
    series: list[tuple[str, int]],
) -> Headline:
    """Construct a Headline for an anomalous Google Trends spike."""
    title = (
        f"Google Trends SPIKE: '{keyword}' search interest +{change_pct:.0f}% "
        f"week-over-week on {spike_date} (value: {spike_value}/100)"
    )
    raw = (
        f"Unusual search interest spike detected for '{keyword}'.\n"
        f"Week-over-week change: +{change_pct:.1f}%\n"
        f"Interest value: {spike_value}/100 (Google scale)\n"
        f"Date: {spike_date}\n"
        f"This may indicate increased retail investor attention.\n"
        f"Recent weekly values: {series[-4:]}"
    )
    # Canonical URL for Google Trends search
    from urllib.parse import quote
    url = (
        f"https://trends.google.com/trends/explore?q={quote(keyword)}"
        f"&date=today+3-m&geo=US"
    )
    return Headline(
        url=url,
        title=title,
        source="google_trends",
        published_at=datetime.now(timezone.utc),
        tickers=[ticker],
        category="analysis",
        raw_content=raw,
    )


# ── Public collector interface ────────────────────────────────────────────────

async def collect_google_trends(
    store: Store,
    timeframe: str = TRENDS_TIMEFRAME,
) -> tuple[list[SentimentScore], list[Headline]]:
    """
    Collect Google Trends weekly search interest for graphene stock keywords.

    Fetches interest-over-time for keyword batches, stores the most recent
    weekly value as a SentimentScore, and generates Headline objects for any
    keyword that shows a spike of >200% week-over-week.

    Batches are separated by a 5–10 second random sleep to respect Google's
    unofficial rate limits. All errors are caught and logged; on
    TooManyRequestsError the collector returns whatever it has collected so far.

    Args:
        store: An open Store instance.
        timeframe: pytrends-format timeframe string (default: "today 3-m").

    Returns:
        Tuple of (sentiment scores stored, spike headlines for AI evaluation).
    """
    all_scores: list[SentimentScore] = []
    headlines: list[Headline] = []

    for batch_idx, keywords in enumerate(KEYWORD_BATCHES):
        if batch_idx > 0:
            # Polite delay between batches to avoid rate limiting
            sleep_secs = random.uniform(5.0, 10.0)
            logger.debug(
                "[google_trends] Sleeping %.1fs before next batch", sleep_secs
            )
            await asyncio.sleep(sleep_secs)

        interest_data = await _collect_batch(keywords, timeframe)
        if not interest_data:
            logger.info(
                "[google_trends] No data returned for batch %d: %s",
                batch_idx,
                keywords,
            )
            continue

        for keyword, series in interest_data.items():
            if not series:
                logger.debug("[google_trends] Empty series for keyword '%s'", keyword)
                continue

            ticker = KEYWORD_TICKER_MAP.get(keyword, "HGRAF")

            # ── Store most recent week's value as SentimentScore ──────────────
            latest_date, latest_val = series[-1]
            normalized = _normalize_score(latest_val)

            score = SentimentScore(
                ticker=ticker,
                source="google_trends",
                score=normalized,
                volume=latest_val,  # raw interest value 0-100 as "volume"
                raw_data={
                    "keyword": keyword,
                    "timeframe": timeframe,
                    "latest_date": latest_date,
                    "latest_value": latest_val,
                    "series_length": len(series),
                    "recent_values": series[-8:],  # last 8 weeks for context
                },
            )

            try:
                await store.insert_sentiment(score)
                all_scores.append(score)
                logger.info(
                    "[google_trends] Stored sentiment for '%s' (%s): "
                    "score=%.3f, value=%d/100",
                    keyword, ticker, normalized, latest_val,
                )
            except Exception as exc:
                logger.error(
                    "[google_trends] Failed to store sentiment for '%s': %s",
                    keyword, exc,
                )

            # ── Spike detection ───────────────────────────────────────────────
            spike = _detect_spike(series, threshold_pct=SPIKE_THRESHOLD_PCT)
            if spike:
                spike_date, spike_val, change_pct = spike
                logger.warning(
                    "[google_trends] SPIKE detected for '%s': "
                    "+%.1f%% week-over-week on %s",
                    keyword, change_pct, spike_date,
                )
                headline = _build_spike_headline(
                    keyword=keyword,
                    ticker=ticker,
                    spike_date=spike_date,
                    spike_value=spike_val,
                    change_pct=change_pct,
                    series=series,
                )
                try:
                    row_id = await store.insert_headline(headline)
                    if row_id is not None:
                        headlines.append(headline)
                        logger.info(
                            "[google_trends] Inserted spike headline id=%d for '%s'",
                            row_id, keyword,
                        )
                    else:
                        logger.debug(
                            "[google_trends] Spike headline already exists for '%s'",
                            keyword,
                        )
                except Exception as exc:
                    logger.error(
                        "[google_trends] Failed to insert spike headline for '%s': %s",
                        keyword, exc,
                    )

    logger.info(
        "[google_trends] Done. %d sentiment scores stored, %d spike headlines.",
        len(all_scores),
        len(headlines),
    )
    return all_scores, headlines
