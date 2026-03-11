"""
StockTwits sentiment collector for Graphene Intel.

Fetches the public symbol stream for each primary ticker (HGRAF, BSWGF) from
the StockTwits API (no authentication required for public streams) and computes
an aggregate sentiment score in the range [-1.0, +1.0]:

    score = (bullish_count - bearish_count) / total_count

Messages without a sentiment label do not affect the score but are counted in
the volume.  If fewer than two messages are found the score defaults to 0.0.

Rate limits (HTTP 429) are caught and logged; an empty list is returned for the
affected ticker so the run continues cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import yaml

from src.db.store import SentimentScore, Store
from src.utils.http import fetch_json

logger = logging.getLogger(__name__)

_SOURCES_PATH = "/opt/grafene/config/sources.yaml"
_BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol"

# Primary tickers to collect — also read from sources.yaml below
_DEFAULT_TICKERS = ["HGRAF", "BSWGF"]


def _load_primary_tickers() -> list[str]:
    """Return StockTwits tickers from sources.yaml sentiment section."""
    try:
        with open(_SOURCES_PATH) as fh:
            cfg: dict[str, Any] = yaml.safe_load(fh)

        for source in cfg.get("sentiment", []):
            if source.get("name") == "stocktwits":
                return source.get("tickers", _DEFAULT_TICKERS)

    except Exception as exc:
        logger.warning(
            "Could not load sources.yaml, falling back to defaults: %s", exc
        )

    return _DEFAULT_TICKERS


def _compute_score(messages: list[dict[str, Any]]) -> tuple[float, int, dict[str, int]]:
    """Compute sentiment score from a list of StockTwits message objects.

    Returns:
        (score, total_count, counts_dict)
        score          — float in [-1.0, +1.0], or 0.0 when no labels present
        total_count    — total number of messages (labelled + unlabelled)
        counts_dict    — {'bullish': N, 'bearish': N, 'none': N}
    """
    bullish = 0
    bearish = 0
    no_label = 0

    for msg in messages:
        sentiment_block = msg.get("entities", {}).get("sentiment") or msg.get("sentiment")
        if isinstance(sentiment_block, dict):
            basic = sentiment_block.get("basic", "")
        elif isinstance(sentiment_block, str):
            basic = sentiment_block
        else:
            basic = ""

        if basic == "Bullish":
            bullish += 1
        elif basic == "Bearish":
            bearish += 1
        else:
            no_label += 1

    total = bullish + bearish + no_label
    labelled = bullish + bearish

    if labelled > 0:
        score = (bullish - bearish) / labelled
    else:
        score = 0.0

    return score, total, {"bullish": bullish, "bearish": bearish, "none": no_label}


async def _fetch_ticker_sentiment(ticker: str) -> SentimentScore | None:
    """Fetch StockTwits stream for *ticker* and return a SentimentScore.

    Returns None on rate-limit (429) or any other error.
    """
    url = f"{_BASE_URL}/{ticker}.json"
    try:
        data: dict[str, Any] = await fetch_json(url)
    except Exception as exc:
        # Detect rate-limit specifically for cleaner log messaging
        exc_str = str(exc)
        if "429" in exc_str:
            logger.warning(
                "StockTwits rate limit (429) for %s — skipping this ticker", ticker
            )
        else:
            logger.error("StockTwits fetch failed for %s: %s", ticker, exc)
        return None

    messages: list[dict[str, Any]] = data.get("messages", [])
    if not messages:
        logger.info("StockTwits: no messages returned for %s", ticker)
        # Still return a zero-volume score rather than None
        return SentimentScore(
            ticker=ticker,
            source="stocktwits",
            score=0.0,
            volume=0,
            raw_data={"messages_count": 0},
        )

    score, total, counts = _compute_score(messages)

    logger.info(
        "StockTwits %s: %d messages — bullish=%d bearish=%d none=%d → score=%.3f",
        ticker,
        total,
        counts["bullish"],
        counts["bearish"],
        counts["none"],
        score,
    )

    return SentimentScore(
        ticker=ticker,
        source="stocktwits",
        score=score,
        volume=total,
        raw_data={
            "messages_count": total,
            "bullish": counts["bullish"],
            "bearish": counts["bearish"],
            "no_label": counts["none"],
        },
    )


async def _get_volume_trend(store: Store, ticker: str) -> dict[str, float | None]:
    """Compare current volume against 7-day average from DB.

    Returns a dict with keys: avg_7d, trend_pct (None when insufficient data).
    """
    rows = await store.get_latest_sentiment(ticker, hours=7 * 24)
    st_rows = [r for r in rows if r["source"] == "stocktwits" and r["volume"] is not None]
    if not st_rows:
        return {"avg_7d": None, "trend_pct": None}
    avg_7d = sum(r["volume"] for r in st_rows) / len(st_rows)
    return {"avg_7d": round(avg_7d, 1), "trend_pct": None}  # trend_pct filled by caller


async def collect_stocktwits_sentiment(store: Store) -> list[SentimentScore]:
    """Collect StockTwits sentiment for all primary tickers and persist to *store*.

    Extends each score with week-over-week volume trend analysis.
    Tickers are fetched sequentially to respect the 3 s public-stream rate limit.

    Returns:
        List of SentimentScore dataclasses for tickers that returned data.
    """
    results: list[SentimentScore] = []

    try:
        tickers = _load_primary_tickers()
    except Exception as exc:
        logger.error("StockTwits collector: failed to load ticker list: %s", exc)
        return results

    for ticker in tickers:
        try:
            score_obj = await _fetch_ticker_sentiment(ticker)
            if score_obj is None:
                continue

            # Augment raw_data with volume trend vs last 7 days
            try:
                trend = await _get_volume_trend(store, ticker)
                avg_7d = trend["avg_7d"]
                if avg_7d is not None and avg_7d > 0:
                    trend_pct = round((score_obj.volume - avg_7d) / avg_7d * 100, 1)
                else:
                    trend_pct = None
                score_obj.raw_data["volume_avg_7d"] = avg_7d
                score_obj.raw_data["volume_trend_pct"] = trend_pct
                if trend_pct is not None:
                    logger.info(
                        "StockTwits %s volume trend: %.0f vs 7d avg %.0f (%+.0f%%)",
                        ticker, score_obj.volume, avg_7d, trend_pct,
                    )
            except Exception as exc:
                logger.warning("StockTwits trend calc failed for %s: %s", ticker, exc)

            try:
                await store.insert_sentiment(score_obj)
            except Exception as exc:
                logger.error(
                    "Failed to persist StockTwits sentiment for %s: %s", ticker, exc
                )

            results.append(score_obj)

        except Exception as exc:
            logger.error(
                "Unexpected error in StockTwits collector for %s: %s", ticker, exc
            )

    logger.info(
        "StockTwits collector complete: %d/%d tickers yielded scores",
        len(results),
        len(tickers),
    )
    return results
