"""
Sector context builder for Claude evaluations.

Fetches fresh data from DB to provide Claude with:
- Current prices and volume for all tracked tickers
- Recent sentiment scores
- Pending catalysts
- Cash runway estimate
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import yaml

from src.db.store import Store

logger = logging.getLogger(__name__)

TICKERS_CONFIG = "/opt/grafene/config/tickers.yaml"


def _load_tickers() -> dict:
    with open(TICKERS_CONFIG) as f:
        return yaml.safe_load(f)


def _format_price_row(p: dict) -> str:
    ticker = p.get("ticker", "?")
    close = p.get("close")
    change = p.get("change_pct")
    vol_ratio = p.get("volume_ratio")
    ma20 = p.get("ma_20")

    price_str = f"${close:.4f}" if close else "N/A"
    change_str = f"{change:+.1f}%" if change is not None else "N/A"
    vol_str = f"{vol_ratio:.1f}x avg" if vol_ratio else "N/A"
    ma_str = f"MA20=${ma20:.4f}" if ma20 else ""

    return f"  {ticker}: {price_str} ({change_str}) vol={vol_str} {ma_str}".strip()


def _format_sentiment_row(ticker: str, scores: list[dict]) -> str:
    if not scores:
        return f"  {ticker}: no sentiment data"
    parts = []
    for s in scores[:3]:
        source = s.get("source", "?")
        score = s.get("score")
        vol = s.get("volume", 0)
        if score is not None:
            label = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
            parts.append(f"{source}={label}({score:+.2f}, {vol} msgs)")
    return f"  {ticker}: {', '.join(parts)}"


async def build_tickers_context(store: Store) -> str:
    """Build watched tickers context string for Claude prompt."""
    cfg = _load_tickers()
    all_tickers = []
    for item in cfg.get("primary", []):
        all_tickers.append((item["ticker"], item.get("name", ""), "PRIMARY"))
    for item in cfg.get("competitors", []):
        all_tickers.append((item["ticker"], item.get("name", ""), "COMPETITOR"))

    ticker_symbols = [t[0] for t in all_tickers]
    prices = await store.get_latest_prices(ticker_symbols)
    price_map = {p["ticker"]: p for p in prices}

    lines = []
    for ticker, name, role in all_tickers:
        p = price_map.get(ticker)
        if p:
            close = p.get("close")
            change = p.get("change_pct")
            vol_ratio = p.get("volume_ratio")
            price_s = f"${close:.4f}" if close else "N/A"
            change_s = f"{change:+.1f}%" if change is not None else "N/A"
            vol_s = f"{vol_ratio:.1f}x avg vol" if vol_ratio else ""
            lines.append(f"- {ticker} ({name}) [{role}]: {price_s} {change_s} {vol_s}")
        else:
            lines.append(f"- {ticker} ({name}) [{role}]: no price data")

    return "\n".join(lines) if lines else "No price data available"


async def build_sector_context(store: Store) -> str:
    """Build sector context string: prices + sentiment summary."""
    cfg = _load_tickers()

    primary_tickers = [t["ticker"] for t in cfg.get("primary", [])]
    competitor_tickers = [t["ticker"] for t in cfg.get("competitors", [])]
    all_tickers = primary_tickers + competitor_tickers[:4]  # limit context size

    prices = await store.get_latest_prices(all_tickers)
    price_map = {p["ticker"]: p for p in prices}

    lines = ["=== Price Snapshot ==="]
    for t in all_tickers:
        p = price_map.get(t)
        if p:
            lines.append(_format_price_row(p))

    lines.append("\n=== Social Sentiment (24h) ===")
    for ticker in primary_tickers:
        scores = await store.get_latest_sentiment(ticker, hours=24)
        lines.append(_format_sentiment_row(ticker, scores))

    return "\n".join(lines)


async def build_catalysts_context(store: Store) -> str:
    """Return pending catalysts as a prompt-friendly string."""
    catalysts = await store.get_pending_catalysts()
    if not catalysts:
        return "No pending catalysts tracked."

    lines = []
    for c in catalysts:
        ticker = c.get("ticker", "?")
        desc = c.get("description", "")
        date = c.get("expected_date", "TBD")
        lines.append(f"- {ticker} | {date} | {desc}")

    return "\n".join(lines)


async def build_full_context(store: Store) -> dict[str, str]:
    """Return all context strings in one call (parallel-friendly)."""
    import asyncio

    tickers_ctx, sector_ctx, catalysts_ctx = await asyncio.gather(
        build_tickers_context(store),
        build_sector_context(store),
        build_catalysts_context(store),
    )
    return {
        "tickers_context": tickers_ctx,
        "sector_context": sector_ctx,
        "catalysts_context": catalysts_ctx,
    }
