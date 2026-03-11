"""
FINRA Consolidated Short Interest collector for Graphene Intel.

Fetches bi-monthly OTC short interest data from the FINRA Open Data API
(no authentication required).  Significant changes (>= ALERT_THRESHOLD_PCT)
generate a Headline for downstream scoring.

API: POST https://api.finra.org/data/group/otcmarket/name/consolidatedShortInterest
Docs: https://developer.finra.org/docs#operation/getDatasetPost
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.db.store import Headline, ShortInterest, Store

logger = logging.getLogger(__name__)

_API_URL = "https://api.finra.org/data/group/otcmarket/name/consolidatedShortInterest"
_FIELDS = [
    "symbolCode", "issueName", "settlementDate", "marketClassCode",
    "currentShortPositionQuantity", "previousShortPositionQuantity",
    "changePercent", "daysToCoverQuantity", "averageDailyVolumeQuantity",
]

# Generate a headline alert when short interest change exceeds this threshold
ALERT_THRESHOLD_PCT = 20.0

# Tickers to monitor
_DEFAULT_TICKERS = ["HGRAF", "BSWGF", "NNXPF", "GMGMF", "DTPKF", "ZTEK"]


async def _fetch_short_interest(ticker: str) -> dict[str, Any] | None:
    """Fetch the most recent short interest record for *ticker* from FINRA."""
    payload = {
        "compareFilters": [
            {"compareType": "equal", "fieldName": "symbolCode", "fieldValue": ticker}
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _API_URL,
                params={"limit": 1, "sortFields": "-settlementDate"},
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
    except httpx.HTTPStatusError as exc:
        logger.error("FINRA API HTTP error for %s: %s", ticker, exc)
    except Exception as exc:
        logger.error("FINRA API error for %s: %s", ticker, exc)
    return None


def _make_headline(si: ShortInterest, raw: dict[str, Any]) -> Headline:
    """Build a Headline describing a notable short interest change."""
    direction = "↑ vzestup" if (si.change_pct or 0) > 0 else "↓ pokles"
    abs_pct = abs(si.change_pct or 0)
    current_m = si.current_si / 1_000_000 if si.current_si else 0
    title = (
        f"{si.ticker} — short interest {direction} {abs_pct:.0f}% "
        f"na {current_m:.2f}M akcií ({si.settlement_date})"
    )
    body = (
        f"Ticker: {si.ticker}\n"
        f"Datum vyrovnání: {si.settlement_date}\n"
        f"Současný short interest: {si.current_si:,} akcií\n"
        f"Předchozí period: {si.previous_si:,} akcií\n"
        f"Změna: {si.change_pct:+.1f}%\n"
        f"Days to cover: {si.days_to_cover:.2f}\n"
        f"Průměrný denní objem: {si.avg_daily_vol:,}\n"
        f"Zdroj: FINRA OTC Short Interest"
    )
    url = f"https://finra-markets.morningstar.com/MarketData/EquityI/default.jsp?type=Stock&Symbol={si.ticker}"
    return Headline(
        url=url + f"&date={si.settlement_date}",
        title=title,
        source="finra_short_interest",
        published_at=datetime.now(timezone.utc),
        tickers=[si.ticker],
        category="filing",
        raw_content=body,
    )


async def collect_short_interest(store: Store) -> tuple[list[ShortInterest], list[Headline]]:
    """Collect FINRA short interest data for all tracked OTC tickers.

    Returns:
        (short_interest_records, alert_headlines)
        Headlines are generated only for changes >= ALERT_THRESHOLD_PCT.
    """
    records: list[ShortInterest] = []
    headlines: list[Headline] = []

    for ticker in _DEFAULT_TICKERS:
        raw = await _fetch_short_interest(ticker)
        if not raw:
            logger.debug("FINRA: no data for %s", ticker)
            continue

        si = ShortInterest(
            ticker=ticker,
            settlement_date=raw.get("settlementDate", ""),
            current_si=raw.get("currentShortPositionQuantity") or 0,
            previous_si=raw.get("previousShortPositionQuantity"),
            change_pct=raw.get("changePercent"),
            days_to_cover=raw.get("daysToCoverQuantity"),
            avg_daily_vol=raw.get("averageDailyVolumeQuantity"),
        )

        if not si.settlement_date:
            continue

        try:
            await store.upsert_short_interest(si)
        except Exception as exc:
            logger.error("Failed to persist short interest for %s: %s", ticker, exc)
            continue

        records.append(si)

        logger.info(
            "FINRA %s: SI=%s (%+.1f%%), DTC=%.2f — %s",
            ticker,
            f"{si.current_si:,}" if si.current_si else "?",
            si.change_pct or 0,
            si.days_to_cover or 0,
            si.settlement_date,
        )

        # Alert on significant change
        if si.change_pct is not None and abs(si.change_pct) >= ALERT_THRESHOLD_PCT:
            hl = _make_headline(si, raw)
            inserted_id = await store.insert_headline(hl)
            if inserted_id:
                headlines.append(hl)
                logger.info(
                    "FINRA alert generated for %s: %+.1f%% short interest change",
                    ticker, si.change_pct,
                )

    logger.info(
        "FINRA short interest: %d tickers collected, %d alerts generated",
        len(records), len(headlines),
    )
    return records, headlines
