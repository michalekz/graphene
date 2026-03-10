"""
Daily summary generator for Graphene Intel.

Fetches the last 24 hours of data from the DB, runs anomaly detection,
then calls Claude Sonnet to produce a Telegram-ready markdown briefing.
Falls back to a structured plain-text summary if the Claude API is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic
import yaml

from src.db.store import Store
from src.evaluator.anomaly import PriceAnomaly, detect_anomalies
from src.evaluator.prompts import DAILY_SUMMARY_SYSTEM, DAILY_SUMMARY_USER

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500
TICKERS_CONFIG = "/opt/grafene/config/tickers.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Ticker helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_all_tickers() -> list[str]:
    """Return every tracked ticker symbol from tickers.yaml."""
    try:
        with open(TICKERS_CONFIG) as f:
            cfg = yaml.safe_load(f)
        tickers: list[str] = []
        for item in cfg.get("primary", []):
            tickers.append(item["ticker"])
        for item in cfg.get("competitors", []):
            tickers.append(item["ticker"])
        for item in cfg.get("sector_context", []):
            t = item.get("ticker") or item.get("symbol")
            if t:
                tickers.append(t)
        # Exclude commodity placeholder symbols
        tickers = [t for t in tickers if t and not t.startswith("graphite")]
        return tickers
    except Exception as exc:
        logger.error("Failed to load tickers config: %s", exc)
        return ["HGRAF", "BSWGF", "NNXPF", "GMGMF", "ZTEK", "ARLSF", "CVV", "FGPHF", "DTPKF", "DMAT"]


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_prices_table(prices: list[dict]) -> str:
    """
    Build a markdown-style table:
        Ticker | Price   | Change%  | VolRatio
        HGRAF  | $0.1234 | +5.2%    | 2.3x
    Returns placeholder string if prices list is empty.
    """
    if not prices:
        return "No price data available."

    header = f"{'Ticker':<8} {'Price':>9} {'Chg%':>7} {'VolRatio':>9}"
    sep = "-" * len(header)
    rows = [header, sep]

    for p in prices:
        ticker = p.get("ticker", "?")
        close = p.get("close")
        change = p.get("change_pct")
        vol_ratio = p.get("volume_ratio")

        price_str = f"${close:.4f}" if close is not None else "N/A"
        change_str = f"{change:+.1f}%" if change is not None else "N/A"
        vol_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "N/A"

        rows.append(f"{ticker:<8} {price_str:>9} {change_str:>7} {vol_str:>9}")

    return "\n".join(rows)


def _format_headlines_list(headlines: list[dict]) -> str:
    """
    Numbered list: score + title + source (one per line).
    Returns placeholder if no headlines.
    """
    if not headlines:
        return "No significant headlines in the last 24 hours."

    lines: list[str] = []
    for i, h in enumerate(headlines[:15], start=1):
        score = h.get("score", "?")
        title = h.get("title", "(no title)")
        source = h.get("source", "")
        summary = h.get("impact_summary") or ""
        tickers_raw = h.get("tickers") or h.get("affected_tickers") or "[]"
        try:
            tickers = json.loads(tickers_raw) if isinstance(tickers_raw, str) else tickers_raw
            ticker_str = f" [{', '.join(tickers)}]" if tickers else ""
        except (json.JSONDecodeError, TypeError):
            ticker_str = ""

        display = summary if summary else title
        lines.append(f"{i}. [Score {score}] {display}{ticker_str} ({source})")

    return "\n".join(lines)


def _format_anomalies_list(anomalies: list[PriceAnomaly]) -> str:
    """
    Human-readable anomaly list.
    Returns empty string if no anomalies (caller omits section).
    """
    if not anomalies:
        return "No anomalies detected."

    lines: list[str] = []
    for a in anomalies:
        sev_tag = f"[{a.severity.upper()}]"
        lines.append(f"• {a.ticker} {sev_tag} {a.anomaly_type}: {a.details}")

    return "\n".join(lines)


def _format_sentiment_summary(sentiment: dict[str, list[dict]]) -> str:
    """
    Compact sentiment overview per ticker.
    sentiment = {ticker: [rows from sentiment_scores table]}
    """
    if not sentiment:
        return "No social sentiment data available."

    lines: list[str] = []
    for ticker, rows in sentiment.items():
        if not rows:
            continue
        parts: list[str] = []
        for row in rows[:4]:
            source = row.get("source", "?")
            score = row.get("score")
            vol = row.get("volume", 0)
            if score is not None:
                label = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
                parts.append(f"{source}: {label} ({score:+.2f}, {vol} mentions)")
        if parts:
            lines.append(f"{ticker} — {' | '.join(parts)}")

    return "\n".join(lines) if lines else "No social sentiment data available."


def _format_catalysts_list(catalysts: list[dict]) -> str:
    """
    Bullet list: expected_date | ticker | description.
    Returns placeholder if empty.
    """
    if not catalysts:
        return "No pending catalysts tracked."

    lines: list[str] = []
    for c in catalysts:
        ticker = c.get("ticker", "?")
        desc = c.get("description", "(no description)")
        date = c.get("expected_date") or "TBD"
        notes = c.get("notes") or ""
        note_str = f" — {notes[:80]}" if notes else ""
        lines.append(f"• {date} | {ticker} | {desc}{note_str}")

    return "\n".join(lines)


def _fallback_summary(
    prices: list[dict],
    headlines: list[dict],
    anomalies: list[PriceAnomaly],
    catalysts: list[dict],
    date: str,
) -> str:
    """
    Plain-text fallback summary used when Claude API call fails.
    Assembles all data sections without LLM formatting.
    """
    sections: list[str] = [
        f"GRAPHENE INTEL — DAILY SUMMARY {date}",
        "(Fallback mode — Claude API unavailable)",
        "",
        "=== PRICE OVERVIEW ===",
        _format_prices_table(prices),
        "",
        "=== TOP HEADLINES (last 24h) ===",
        _format_headlines_list(headlines),
    ]

    if anomalies:
        sections += [
            "",
            "=== PRICE/VOLUME ANOMALIES ===",
            _format_anomalies_list(anomalies),
        ]

    sections += [
        "",
        "=== UPCOMING CATALYSTS ===",
        _format_catalysts_list(catalysts),
        "",
        "NOTE: This is an automated plain-text fallback. Not investment advice.",
    ]

    return "\n".join(sections)


# ─────────────────────────────────────────────────────────────────────────────
# Claude client
# ─────────────────────────────────────────────────────────────────────────────

def _get_async_client() -> anthropic.AsyncAnthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    return anthropic.AsyncAnthropic(api_key=api_key)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def generate_daily_summary(store: Store) -> str:
    """
    Generate a daily summary using Claude Sonnet.

    Steps:
    1. Fetch from DB: headlines (last 24h, score >= 4), latest prices for all
       tickers, latest sentiment (24h), pending catalysts.
    2. Run anomaly detection against the fresh price snapshots.
    3. Format all data into compact prompt strings.
    4. Call Claude Sonnet (claude-sonnet-4-6) with DAILY_SUMMARY_SYSTEM +
       DAILY_SUMMARY_USER and return the model's Telegram-ready markdown text.

    Falls back to a structured plain-text summary assembled directly from DB
    data if the Claude API call fails for any reason.

    Args:
        store: An open Store instance (async context manager already entered).

    Returns:
        A Telegram-ready string (markdown or plain-text fallback).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logger.info("Generating daily summary for %s", today)

    # ── 1. Fetch data ────────────────────────────────────────────────────────
    all_tickers = _load_all_tickers()
    try:
        headlines, prices, catalysts = await asyncio.gather(
            store.get_headlines_for_daily_summary(min_score=4, hours=24),
            store.get_latest_prices(all_tickers),
            store.get_pending_catalysts(),
        )
    except Exception as exc:
        logger.error("DB fetch failed in generate_daily_summary: %s", exc, exc_info=True)
        return f"Daily summary unavailable — DB error: {exc}"

    logger.info(
        "Fetched %d headlines, %d prices, %d catalysts",
        len(headlines), len(prices), len(catalysts),
    )

    # Fetch sentiment for each primary ticker individually (sequential to
    # avoid SQLite contention on a shared connection)
    primary_tickers = ["HGRAF", "BSWGF"]
    sentiment: dict[str, list[dict]] = {}
    for ticker in primary_tickers:
        try:
            sentiment[ticker] = await store.get_latest_sentiment(ticker, hours=24)
        except Exception as exc:
            logger.warning("Sentiment fetch failed for %s: %s", ticker, exc)
            sentiment[ticker] = []

    # ── 2. Anomaly detection ─────────────────────────────────────────────────
    try:
        anomalies = detect_anomalies(prices)
        logger.info("Detected %d anomalies", len(anomalies))
    except Exception as exc:
        logger.warning("Anomaly detection failed: %s", exc)
        anomalies = []

    # ── 3. Format prompt sections ────────────────────────────────────────────
    prices_table = _format_prices_table(prices)
    headlines_list = _format_headlines_list(headlines)
    anomalies_list = _format_anomalies_list(anomalies)
    sentiment_summary = _format_sentiment_summary(sentiment)
    catalysts_list = _format_catalysts_list(catalysts)

    user_prompt = DAILY_SUMMARY_USER.format(
        date=today,
        prices_table=prices_table,
        headlines_list=headlines_list,
        anomalies_list=anomalies_list,
        sentiment_summary=sentiment_summary,
        catalysts_list=catalysts_list,
    )

    # ── 4. Call Claude Sonnet ────────────────────────────────────────────────
    try:
        client = _get_async_client()
        logger.info("Calling Claude Sonnet for daily summary (model=%s)", SONNET_MODEL)
        message = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=MAX_TOKENS,
            system=DAILY_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        result_text: str = message.content[0].text
        logger.info(
            "Daily summary generated by Claude: %d chars, stop_reason=%s",
            len(result_text),
            message.stop_reason,
        )
        return result_text

    except anthropic.APIError as exc:
        logger.error("Claude API error generating daily summary: %s", exc)
    except RuntimeError as exc:
        logger.error("Runtime error (likely missing API key): %s", exc)
    except Exception as exc:
        logger.error("Unexpected error calling Claude: %s", exc, exc_info=True)

    # ── 5. Fallback ──────────────────────────────────────────────────────────
    logger.warning("Falling back to plain-text daily summary")
    return _fallback_summary(prices, headlines, anomalies, catalysts, today)
