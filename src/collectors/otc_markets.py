"""
OTC Markets tier & status monitor for Graphene Intel.

Detects two types of signals without paid API access:

  1. Market tier changes  — OTCQX → OTCQB → Pink is a red-flag downgrade;
                            Pink → OTCQB / OTCQB → OTCQX is positive progression.

  2. SEC trading halts   — scrapes the SEC litigation/suspension bulletin list
                            for any of our tracked OTC tickers.

Tier data is sourced from yfinance (exchange field) which maps to:
  OQX  = OTCQX  (highest voluntary OTC tier)
  OQB  = OTCQB  (early-stage companies)
  PNK  = Pink / Pink Open Market (lowest tier, often no reporting)
  PINX = Pink (some variants)

Previous tiers are stored in the sentiment_scores table (source="otc_tier")
to allow change detection on the next run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
import yfinance as yf

from src.db.store import Headline, SentimentScore, Store

logger = logging.getLogger(__name__)

# OTC tickers to monitor for tier changes
_OTC_TICKERS = ["HGRAF", "BSWGF", "NNXPF", "GMGMF", "DTPKF", "ZTEK", "ARLSF", "CVV", "FGPHF"]

_SEC_HALTS_URL = "https://www.sec.gov/litigation/suspensions.shtml"

_TIER_LABELS: dict[str, str] = {
    "OQX": "OTCQX",
    "OQB": "OTCQB",
    "PNK": "Pink",
    "PINX": "Pink",
    "OTC": "OTC",
}

# Score to assign when storing tier as a signal (used to compare with previous)
_TIER_SCORES: dict[str, float] = {
    "OQX": 1.0,   # best
    "OQB": 0.0,   # mid
    "PNK": -1.0,  # worst
    "PINX": -1.0,
    "OTC": -0.5,
}


def _tier_label(code: str) -> str:
    return _TIER_LABELS.get(code, code)


def _tier_score(code: str) -> float:
    return _TIER_SCORES.get(code, 0.0)


async def _get_current_tier(ticker: str) -> str | None:
    """Return yfinance exchange code for the ticker (e.g. 'OQX', 'PNK')."""
    try:
        info: dict[str, Any] = await _run_sync(lambda: yf.Ticker(ticker).info)
        exchange = info.get("exchange", "")
        return exchange if exchange else None
    except Exception as exc:
        logger.warning("[otc_markets] Could not fetch tier for %s: %s", ticker, exc)
        return None


async def _run_sync(fn):
    """Run a sync function in an async context (avoids blocking the event loop)."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn)


async def _get_previous_tier(store: Store, ticker: str) -> str | None:
    """Return the most recently stored tier code for ticker from sentiment_scores."""
    rows = await store.get_latest_sentiment(ticker, hours=30 * 24)
    for row in rows:
        if row["source"] == "otc_tier":
            import json
            raw = json.loads(row["raw_data"]) if row["raw_data"] else {}
            return raw.get("exchange_code")
    return None


async def _check_sec_halts(tickers: list[str]) -> list[Headline]:
    """Scrape SEC litigation/suspensions page for our tickers."""
    headlines: list[Headline] = []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                _SEC_HALTS_URL,
                headers={"User-Agent": "GrapheneIntel zdenek.michalek@gmail.com"},
            )
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.error("[otc_markets] SEC halts page fetch failed: %s", exc)
        return []

    # Each suspension entry is a paragraph or list item on the page
    for ticker in tickers:
        # Look for ticker symbol mentions in proximity to "suspend" or "halt"
        pattern = rf'\b{re.escape(ticker)}\b.{{0,300}}(?:suspend|halt|order|violat)'
        matches = re.findall(pattern, html, re.I | re.DOTALL)
        if matches:
            excerpt = re.sub(r'<[^>]+>', ' ', matches[0]).strip()[:300]
            logger.warning("[otc_markets] SEC suspension match for %s: %s", ticker, excerpt[:80])
            headlines.append(Headline(
                url=_SEC_HALTS_URL,
                title=f"⚠️ SEC SUSPENSION/HALT mention: {ticker}",
                source="sec_halt",
                published_at=datetime.now(timezone.utc),
                tickers=[ticker],
                category="filing",
                raw_content=excerpt,
            ))

    return headlines


async def collect_otc_status(store: Store) -> tuple[list[SentimentScore], list[Headline]]:
    """Monitor OTC tier changes and SEC halts.

    Returns:
        (tier_scores, alert_headlines)
    """
    scores: list[SentimentScore] = []
    headlines: list[Headline] = []

    for ticker in _OTC_TICKERS:
        current_code = await _get_current_tier(ticker)
        if not current_code:
            continue

        current_label = _tier_label(current_code)
        tier_score = _tier_score(current_code)

        # Store tier as sentiment score for history/trend
        score_obj = SentimentScore(
            ticker=ticker,
            source="otc_tier",
            score=tier_score,
            volume=1,
            raw_data={"exchange_code": current_code, "tier_label": current_label},
        )
        try:
            await store.insert_sentiment(score_obj)
        except Exception as exc:
            logger.error("[otc_markets] Failed to store tier for %s: %s", ticker, exc)

        scores.append(score_obj)

        # Compare with previous tier
        previous_code = await _get_previous_tier(store, ticker)
        if previous_code and previous_code != current_code:
            prev_label = _tier_label(previous_code)
            prev_score = _tier_score(previous_code)

            if tier_score < prev_score:
                direction = "⬇️ DOWNGRADE"
                emoji = "🔴"
            else:
                direction = "⬆️ UPGRADE"
                emoji = "🟢"

            title = (
                f"{emoji} {ticker} OTC tier {direction}: "
                f"{prev_label} → {current_label}"
            )
            body = (
                f"Ticker: {ticker}\n"
                f"Předchozí tier: {prev_label} ({previous_code})\n"
                f"Nový tier: {current_label} ({current_code})\n"
                f"Zdroj: yfinance / OTC Markets"
            )
            hl = Headline(
                url=f"https://www.otcmarkets.com/stock/{ticker}/company-info",
                title=title,
                source="otc_tier_change",
                published_at=datetime.now(timezone.utc),
                tickers=[ticker],
                category="filing",
                raw_content=body,
            )
            hl_id = await store.insert_headline(hl)
            if hl_id:
                headlines.append(hl)
                logger.warning(
                    "[otc_markets] Tier change for %s: %s → %s",
                    ticker, prev_label, current_label,
                )

        logger.info("[otc_markets] %s: %s (%s)", ticker, current_label, current_code)

    # Check SEC halts for all tickers
    halt_headlines = await _check_sec_halts(_OTC_TICKERS)
    for hl in halt_headlines:
        hl_id = await store.insert_headline(hl)
        if hl_id:
            headlines.append(hl)

    logger.info(
        "[otc_markets] Done: %d tickers monitored, %d alerts generated",
        len(scores), len(headlines),
    )
    return scores, headlines
