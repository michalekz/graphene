"""
Weekly deep-analysis report generator for Graphene Intel.

Aggregates 7 days of price performance, all scored headlines, insider trades,
recent patents, Google Trends data and pending catalysts, then calls Claude
Sonnet to produce a multi-section Telegram-ready report.

Falls back to a structured plain-text report if the Claude API is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import yaml

from src.db.store import Store
from src.evaluator.prompts import WEEKLY_REPORT_SYSTEM, WEEKLY_REPORT_USER

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000  # weekly report is longer; Telegram splits it
TICKERS_CONFIG = "/opt/grafene/config/tickers.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Ticker helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_tickers_config() -> dict:
    """Load and return the full tickers.yaml config dict."""
    try:
        with open(TICKERS_CONFIG) as f:
            return yaml.safe_load(f)
    except Exception as exc:
        logger.error("Failed to load tickers config: %s", exc)
        return {}


def _all_equity_tickers(cfg: dict) -> list[str]:
    """Return every equity ticker (no commodity proxies)."""
    tickers: list[str] = []
    for item in cfg.get("primary", []):
        tickers.append(item["ticker"])
    for item in cfg.get("competitors", []):
        tickers.append(item["ticker"])
    for item in cfg.get("sector_context", []):
        t = item.get("ticker") or item.get("symbol")
        if t and not t.startswith("graphite"):
            tickers.append(t)
    return tickers


# ─────────────────────────────────────────────────────────────────────────────
# Weekly performance calculation
# ─────────────────────────────────────────────────────────────────────────────

async def _calculate_weekly_performance(store: Store) -> dict[str, float]:
    """
    Calculate percentage price change over the last 7 days for every tracked
    equity ticker.

    Strategy: fetch price history for 8 days, compare the oldest close found
    within the window to the most recent close.

    Returns:
        Mapping of ticker -> week_change_pct (e.g. {"HGRAF": 5.2, "BSWGF": -3.1}).
        Missing tickers are excluded from the result.
    """
    cfg = _load_tickers_config()
    tickers = _all_equity_tickers(cfg)
    perf: dict[str, float] = {}

    for ticker in tickers:
        try:
            history = await store.get_price_history(ticker, days=8)
            if len(history) < 2:
                continue
            # sort ascending by timestamp — get_price_history already does this
            earliest_close = history[0].get("close")
            latest_close = history[-1].get("close")
            if earliest_close and latest_close and earliest_close > 0:
                pct = (latest_close - earliest_close) / earliest_close * 100
                perf[ticker] = round(pct, 2)
        except Exception as exc:
            logger.warning("Weekly perf calc failed for %s: %s", ticker, exc)

    return perf


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_weekly_prices(perf: dict[str, float], prices: list[dict]) -> str:
    """
    Table: Ticker | Week% | Price | VolRatio
    Ordered by absolute week performance (biggest movers first).
    """
    if not prices and not perf:
        return "No weekly price data available."

    price_map = {p["ticker"]: p for p in prices}

    # Build rows for every ticker we have any data for
    rows_data: list[tuple[str, Optional[float], Optional[float], Optional[float]]] = []
    seen: set[str] = set()

    for p in prices:
        ticker = p.get("ticker", "")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        rows_data.append((
            ticker,
            perf.get(ticker),
            p.get("close"),
            p.get("volume_ratio"),
        ))

    # Add tickers that only appear in perf (no price snapshot)
    for ticker, pct in perf.items():
        if ticker not in seen:
            rows_data.append((ticker, pct, None, None))

    # Sort: biggest absolute mover first
    rows_data.sort(key=lambda r: abs(r[1]) if r[1] is not None else 0, reverse=True)

    header = f"{'Ticker':<8} {'Week%':>7} {'Price':>9} {'VolRatio':>9}"
    sep = "-" * len(header)
    lines = [header, sep]

    for ticker, week_pct, close, vol_ratio in rows_data:
        week_str = f"{week_pct:+.1f}%" if week_pct is not None else "N/A"
        price_str = f"${close:.4f}" if close is not None else "N/A"
        vol_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "N/A"
        lines.append(f"{ticker:<8} {week_str:>7} {price_str:>9} {vol_str:>9}")

    return "\n".join(lines)


def _format_insider_trades(trades: list[dict]) -> str:
    """
    Formatted insider trade list, most recent first.
    Returns placeholder if no trades.
    """
    if not trades:
        return "No insider trades reported in the last 30 days."

    lines: list[str] = []
    for t in trades:
        ticker = t.get("ticker", "?")
        name = t.get("insider_name") or "Unknown"
        title = t.get("title") or ""
        tx_type = (t.get("transaction_type") or "?").upper()
        shares = t.get("shares")
        price = t.get("price")
        value = t.get("value_usd")
        date = t.get("date") or "?"

        shares_str = f"{shares:,}" if shares else "?"
        price_str = f"${price:.4f}" if price else "?"
        value_str = f"${value:,.0f}" if value else ""

        line = (
            f"• {date} | {ticker} | {name} ({title}) | {tx_type} "
            f"{shares_str} shares @ {price_str}"
        )
        if value_str:
            line += f" = {value_str}"
        lines.append(line)

    return "\n".join(lines)


async def _format_patents(store: Store) -> str:
    """
    Fetch recent patent filings from DB (last 30 days) and format them.
    Queries patent_filings table directly via the underlying DB connection.
    """
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        # Access the internal connection — store._db is aiosqlite.Connection
        async with store._db.execute(  # type: ignore[attr-defined]
            """
            SELECT patent_id, title, assignee, filing_date, publication_date,
                   relevance_score, abstract
            FROM patent_filings
            WHERE filing_date >= ? OR publication_date >= ?
            ORDER BY COALESCE(publication_date, filing_date) DESC
            LIMIT 10
            """,
            (since, since),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return "No new patent filings tracked in the last 30 days."

        lines: list[str] = []
        for row in rows:
            row_d = dict(row)
            title = row_d.get("title") or "(untitled)"
            assignee = row_d.get("assignee") or "Unknown"
            pub_date = row_d.get("publication_date") or row_d.get("filing_date") or "?"
            relevance = row_d.get("relevance_score")
            abstract = (row_d.get("abstract") or "")[:120]
            rel_str = f" [relevance {relevance}/10]" if relevance else ""
            lines.append(f"• {pub_date} | {assignee}{rel_str} — {title}")
            if abstract:
                lines.append(f"  {abstract}{'...' if len(abstract) == 120 else ''}")

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Patent fetch failed: %s", exc)
        return "Patent data unavailable."


async def _format_google_trends(store: Store) -> str:
    """
    Retrieve Google Trends data from the sentiment_scores table
    (source = 'google_trends') for the last 7 days and summarise it.
    """
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        async with store._db.execute(  # type: ignore[attr-defined]
            """
            SELECT ticker, score, volume, raw_data, timestamp
            FROM sentiment_scores
            WHERE source = 'google_trends' AND timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (since,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return "No Google Trends data available for the past 7 days."

        # Group by ticker, pick the most recent entry per ticker
        latest: dict[str, dict] = {}
        for row in rows:
            row_d = dict(row)
            ticker = row_d.get("ticker", "?")
            if ticker not in latest:
                latest[ticker] = row_d

        lines: list[str] = []
        for ticker, row_d in latest.items():
            score = row_d.get("score")
            volume = row_d.get("volume")
            raw = row_d.get("raw_data") or "{}"
            try:
                raw_dict = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                raw_dict = {}

            # raw_data may contain {"interest": 45, "change_pct": 12.3, "keyword": "HydroGraph"}
            interest = raw_dict.get("interest") or raw_dict.get("value")
            change = raw_dict.get("change_pct") or raw_dict.get("change")
            keyword = raw_dict.get("keyword") or ticker

            score_str = f"{score:+.2f}" if score is not None else "N/A"
            interest_str = f" interest={interest}" if interest is not None else ""
            change_str = f" ({change:+.1f}% WoW)" if change is not None else ""
            vol_str = f" {volume} mentions" if volume else ""

            lines.append(
                f"• {keyword}: score={score_str}{interest_str}{change_str}{vol_str}"
            )

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Google Trends fetch failed: %s", exc)
        return "Google Trends data unavailable."


def _format_cash_runway() -> str:
    """
    Return hardcoded cash runway estimates for primary tickers.

    These values should be updated whenever new quarterly filings are published.
    Last updated: March 2026 (based on Q4 2025 / Q1 2026 filings).
    """
    return """\
HGRAF (HydroGraph Clean Power):
  Last known cash position: ~C$30M (March 2026 offering proceeds, estimated net ~C$28M after costs)
  Estimated quarterly burn rate: ~C$1.5–2.5M (pre-revenue stage, R&D + G&A)
  Estimated runway: ~10–18 months from March 2026 (i.e., through Q1–Q3 2027)
  Key variable: Texas facility capex will accelerate burn. Watch Q1 2026 filing (expected May 2026).
  NOTE: Figures are estimates. Update from SEDAR+ MD&A when Q1 2026 results filed.

BSWGF (Black Swan Graphene):
  Last known cash position: ~C$3–5M (Q3 2025 estimate, not recently updated)
  Estimated quarterly burn rate: ~C$0.8–1.2M
  Estimated runway: ~3–6 months (short; watch for financing announcements)
  Key variable: UK capacity expansion capex. Named customer revenue needed urgently.
  NOTE: Figures are estimates. Verify against latest SEDAR+ filing."""


def _format_all_headlines(headlines: list[dict]) -> str:
    """
    Full week headline digest: score + title + source + impact_summary.
    Cap at 30 to keep prompt size manageable.
    """
    if not headlines:
        return "No significant headlines this week."

    lines: list[str] = []
    for i, h in enumerate(headlines[:30], start=1):
        score = h.get("score", "?")
        title = h.get("title") or "(no title)"
        source = h.get("source") or ""
        summary = h.get("impact_summary") or ""
        sentiment = h.get("sentiment") or "neutral"
        pub = h.get("published_at") or ""
        pub_short = pub[:10] if pub else ""

        display = summary if summary else title
        flags = ""
        if h.get("is_red_flag"):
            flags += " [RED FLAG]"
        if h.get("is_pump_suspect"):
            flags += " [PUMP SUSPECT]"

        lines.append(
            f"{i}. [{pub_short}][Score {score}][{sentiment}]{flags} "
            f"{display} ({source})"
        )

    if len(headlines) > 30:
        lines.append(f"... and {len(headlines) - 30} more headlines (lower score).")

    return "\n".join(lines)


def _fallback_weekly_report(
    perf: dict[str, float],
    prices: list[dict],
    headlines: list[dict],
    trades: list[dict],
    catalysts: list[dict],
    patents_str: str,
    trends_str: str,
    date: str,
) -> str:
    """
    Plain-text fallback weekly report when Claude API is unavailable.
    """
    sections: list[str] = [
        f"GRAPHENE INTEL — WEEKLY REPORT — Week ending {date}",
        "(Fallback mode — Claude API unavailable)",
        "",
        "=== WEEKLY PRICE PERFORMANCE ===",
        _format_weekly_prices(perf, prices),
        "",
        "=== ALL HEADLINES THIS WEEK ===",
        _format_all_headlines(headlines),
        "",
        "=== INSIDER TRADES (last 30 days) ===",
        _format_insider_trades(trades),
        "",
        "=== RECENT PATENTS ===",
        patents_str,
        "",
        "=== GOOGLE TRENDS ===",
        trends_str,
        "",
        "=== CASH RUNWAY ANALYSIS ===",
        _format_cash_runway(),
        "",
        "=== PENDING CATALYSTS ===",
    ]

    if catalysts:
        for c in catalysts:
            ticker = c.get("ticker", "?")
            desc = c.get("description", "")
            date_c = c.get("expected_date") or "TBD"
            status = c.get("status") or "pending"
            sections.append(f"• {date_c} | {ticker} | {status.upper()} | {desc}")
    else:
        sections.append("No pending catalysts tracked.")

    sections.append("")
    sections.append("NOTE: This is an automated plain-text fallback. Not investment advice.")

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

async def generate_weekly_report(store: Store) -> str:
    """
    Generate a weekly deep-analysis report using Claude Sonnet.

    Steps:
    1. Fetch from DB: headlines (7 days, score >= 3), latest prices for all
       tracked tickers, insider trades (30 days), pending catalysts.
    2. Calculate per-ticker week performance (latest_close vs 7 days ago).
    3. Fetch patents and Google Trends data from the DB.
    4. Assemble all sections into a structured prompt.
    5. Call Claude Sonnet (claude-sonnet-4-6) with WEEKLY_REPORT_SYSTEM +
       WEEKLY_REPORT_USER and return the model text.

    Falls back to a structured plain-text report if the Claude API fails.

    Args:
        store: An open Store instance (async context manager already entered).

    Returns:
        A Telegram-ready string. May be up to ~4000 characters — the caller is
        responsible for splitting into multiple Telegram messages if needed.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logger.info("Generating weekly report for week ending %s", today)

    cfg = _load_tickers_config()
    all_tickers = _all_equity_tickers(cfg)

    # ── 1. Fetch DB data in parallel where safe ──────────────────────────────
    try:
        headlines, prices, trades, catalysts = await asyncio.gather(
            store.get_headlines_for_weekly_report(min_score=3, days=7),
            store.get_latest_prices(all_tickers),
            store.get_recent_insider_trades(days=30),
            store.get_pending_catalysts(),
        )
    except Exception as exc:
        logger.error("DB fetch failed in generate_weekly_report: %s", exc, exc_info=True)
        return f"Weekly report unavailable — DB error: {exc}"

    logger.info(
        "Fetched %d headlines, %d prices, %d trades, %d catalysts",
        len(headlines), len(prices), len(trades), len(catalysts),
    )

    # ── 2. Weekly performance calculation ────────────────────────────────────
    try:
        perf = await _calculate_weekly_performance(store)
        logger.info("Calculated weekly performance for %d tickers", len(perf))
    except Exception as exc:
        logger.warning("Weekly performance calculation failed: %s", exc)
        perf = {}

    # ── 3. Patents and Google Trends ─────────────────────────────────────────
    patents_str = await _format_patents(store)
    trends_str = await _format_google_trends(store)

    # ── 4. Sentiment summary for the week (all primary tickers) ──────────────
    primary_tickers = [t["ticker"] for t in cfg.get("primary", [])] or ["HGRAF", "BSWGF"]
    sentiment_parts: list[str] = []
    for ticker in primary_tickers:
        try:
            rows = await store.get_latest_sentiment(ticker, hours=168)  # 7 days
            if rows:
                for row in rows[:5]:
                    source = row.get("source", "?")
                    score = row.get("score")
                    vol = row.get("volume", 0)
                    if score is not None:
                        label = "bullish" if score > 0.1 else ("bearish" if score < -0.1 else "neutral")
                        sentiment_parts.append(
                            f"{ticker} / {source}: {label} ({score:+.2f}, {vol} mentions)"
                        )
        except Exception as exc:
            logger.warning("Sentiment fetch failed for %s: %s", ticker, exc)

    sentiment_data = "\n".join(sentiment_parts) if sentiment_parts else "No sentiment data available."

    # ── 5. Format prompt sections ────────────────────────────────────────────
    weekly_prices_str = _format_weekly_prices(perf, prices)
    all_headlines_str = _format_all_headlines(headlines)
    insider_trades_str = _format_insider_trades(trades)
    cash_runway_str = _format_cash_runway()

    catalysts_str_parts: list[str] = []
    for c in catalysts:
        ticker = c.get("ticker", "?")
        desc = c.get("description", "")
        date_c = c.get("expected_date") or "TBD"
        status = c.get("status") or "pending"
        notes = (c.get("notes") or "")[:100]
        note_part = f" | {notes}" if notes else ""
        catalysts_str_parts.append(f"• {date_c} | {ticker} | {status} | {desc}{note_part}")

    catalysts_str = "\n".join(catalysts_str_parts) if catalysts_str_parts else "No pending catalysts."

    user_prompt = WEEKLY_REPORT_USER.format(
        date=today,
        weekly_prices=weekly_prices_str,
        all_headlines=all_headlines_str,
        insider_trades=insider_trades_str,
        patents=patents_str,
        google_trends=trends_str,
        sentiment_data=sentiment_data,
        cash_runway_notes=cash_runway_str,
        catalysts=catalysts_str,
    )

    # ── 6. Call Claude Sonnet ────────────────────────────────────────────────
    try:
        client = _get_async_client()
        logger.info("Calling Claude Sonnet for weekly report (model=%s)", SONNET_MODEL)
        message = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=MAX_TOKENS,
            system=WEEKLY_REPORT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        result_text: str = message.content[0].text
        logger.info(
            "Weekly report generated by Claude: %d chars, stop_reason=%s",
            len(result_text),
            message.stop_reason,
        )
        return result_text

    except anthropic.APIError as exc:
        logger.error("Claude API error generating weekly report: %s", exc)
    except RuntimeError as exc:
        logger.error("Runtime error (likely missing API key): %s", exc)
    except Exception as exc:
        logger.error("Unexpected error calling Claude for weekly report: %s", exc, exc_info=True)

    # ── 7. Fallback ──────────────────────────────────────────────────────────
    logger.warning("Falling back to plain-text weekly report")
    return _fallback_weekly_report(
        perf, prices, headlines, trades, catalysts,
        patents_str, trends_str, today,
    )
