"""
SEDI (Canadian insider trading) collector for Graphene Intel.

Canadian public companies (HGRAF/HG.CN, BSWGF/SWAN.V) report insider
transactions through SEDI (System for Electronic Disclosure by Insiders)
at sedi.ca — a Government of Canada / CSA system.

Access methods:
  - SEDI.ca: Protected by ShieldSquare bot-protection (needs headless browser)
  - SEDAR+:  Also bot-protected
  - canadianinsider.com: Behind Cloudflare
  - TMX Money GraphQL: Returns no data for these microcaps

CURRENT STATUS: Uses TMX Money GraphQL API as primary source.
For full SEDI coverage, headless browser (Playwright) integration is needed.
Tracked in: https://github.com/michalekz/graphene/issues

Fallback: SEC EDGAR Form 4 (already in sec_edgar.py) covers US-listed companies.
This module adds Canadian-specific monitoring where accessible.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.db.store import Headline, InsiderTrade, Store

logger = logging.getLogger(__name__)

_TMX_GRAPHQL = "https://app-money.tmx.com/graphql"
_TMX_HEADERS = {
    "Content-Type": "application/json",
    "x-tmxmoney-platform": "web",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://money.tmx.com/",
}

# Map OTC ticker → TSX/TSXV symbol
_OTC_TO_TSXV: dict[str, str] = {
    "HGRAF": "HG",
    "BSWGF": "SWAN",
    "NNXPF": "NXE",
}

_HEADLINE_MIN_VALUE_CAD = 25_000  # Only generate alerts for trades >= C$25k


async def _fetch_tmx_insiders(tsxv_symbol: str) -> list[dict[str, Any]]:
    """Fetch insider transactions via TMX Money GraphQL API."""
    query = f'{{ getInsiderTransactions(symbol:"{tsxv_symbol}") }}'
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                _TMX_GRAPHQL,
                json={"query": query},
                headers=_TMX_HEADERS,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("data", {}).get("getInsiderTransactions", [])
            return result if isinstance(result, list) else []
    except Exception as exc:
        logger.error("[sedi] TMX GraphQL error for %s: %s", tsxv_symbol, exc)
        return []


def _make_insider_trade(ticker: str, row: dict[str, Any]) -> InsiderTrade | None:
    """Map TMX GraphQL row to InsiderTrade dataclass."""
    try:
        insider_name = row.get("insiderName", "Unknown")
        trans_type_raw = row.get("transType", "")
        shares = int(row.get("volume", 0) or 0)
        price = float(row.get("price", 0) or 0)
        value_cad = float(row.get("valueCAD", 0) or 0)
        report_date = row.get("reportDate", "")

        if not insider_name or not report_date:
            return None

        # Normalize transaction type
        trans_map = {
            "10+": "buy", "10-": "sell",
            "acquisition": "buy", "disposition": "sell",
            "exercise": "exercise", "grant": "grant",
        }
        trans_type = trans_map.get(trans_type_raw.lower(), trans_type_raw.lower() or "unknown")

        # Use TMX URL as filing reference
        accession = f"tmx_{ticker}_{report_date}_{insider_name.replace(' ', '_')}"
        filing_url = f"https://money.tmx.com/en/quote/{ticker}?section=insiders"

        return InsiderTrade(
            ticker=ticker,
            insider_name=insider_name,
            title=row.get("insiderTitle", "Insider"),
            transaction_type=trans_type,
            shares=shares,
            price=price,
            value_usd=value_cad,  # CAD amount stored in value_usd field
            date=report_date[:10] if report_date else "",
            source="sedi_tmx",
            filing_url=filing_url,
            filing_accession=accession,
        )
    except Exception as exc:
        logger.warning("[sedi] Failed to parse insider trade row: %s — %s", row, exc)
        return None


async def collect_sedi_insider_trades(store: Store) -> tuple[list[InsiderTrade], list[Headline]]:
    """Collect Canadian insider trades via TMX Money GraphQL API.

    Returns:
        (trades, alert_headlines)
        Headlines generated only for trades >= _HEADLINE_MIN_VALUE_CAD.

    Note: SEDI.ca direct access requires headless browser (see module docstring).
    """
    trades: list[InsiderTrade] = []
    headlines: list[Headline] = []

    for otc_ticker, tsxv_symbol in _OTC_TO_TSXV.items():
        rows = await _fetch_tmx_insiders(tsxv_symbol)
        if not rows:
            logger.debug("[sedi] No insider data from TMX for %s (%s)", otc_ticker, tsxv_symbol)
            continue

        for row in rows:
            trade = _make_insider_trade(otc_ticker, row)
            if not trade:
                continue

            trade_id = await store.insert_insider_trade(trade)
            if trade_id is None:
                continue  # duplicate

            trades.append(trade)
            logger.info(
                "[sedi] %s insider: %s %s %s shares @ C$%.2f (C$%s)",
                otc_ticker, trade.insider_name, trade.transaction_type,
                f"{trade.shares:,}", trade.price,
                f"{trade.value_usd:,.0f}" if trade.value_usd else "?",
            )

            # Generate headline for significant trades
            value_cad = trade.value_usd or 0
            if value_cad >= _HEADLINE_MIN_VALUE_CAD:
                direction = "🟢 NÁKUP" if trade.transaction_type == "buy" else "🔴 PRODEJ"
                title = (
                    f"{otc_ticker} insider {direction}: {trade.insider_name} "
                    f"— C${value_cad:,.0f} ({trade.date})"
                )
                body = (
                    f"Insider: {trade.insider_name} ({trade.title})\n"
                    f"Transakce: {trade.transaction_type.upper()}\n"
                    f"Množství: {trade.shares:,} akcií @ C${trade.price:.3f}\n"
                    f"Hodnota: C${value_cad:,.0f}\n"
                    f"Datum: {trade.date}\n"
                    f"Zdroj: SEDI (přes TMX Money)"
                )
                hl = Headline(
                    url=trade.filing_url,
                    title=title,
                    source="sedi_insider",
                    published_at=datetime.strptime(trade.date, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    ) if trade.date else datetime.now(timezone.utc),
                    tickers=[otc_ticker],
                    category="filing",
                    raw_content=body,
                )
                hl_id = await store.insert_headline(hl)
                if hl_id:
                    headlines.append(hl)

    logger.info(
        "[sedi] Complete: %d trades collected, %d alerts generated",
        len(trades), len(headlines),
    )
    return trades, headlines
