"""
IBKR Flex Query collector for Graphene Intel.

Fetches open portfolio positions via Interactive Brokers Flex Web Service API
(no TWS/IB Gateway required — just a token + query ID).

Two-step protocol:
  1. POST SendRequest → get ReferenceCode
  2. GET GetStatement?q={ReferenceCode} → XML with positions

Requires in .env:
  IBKR_FLEX_TOKEN     — from Client Portal → Settings → Flex Web Service
  IBKR_FLEX_QUERY_ID  — from Client Portal → Reports → Flex Queries
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from src.db.store import PortfolioPosition, Store

logger = logging.getLogger(__name__)

_SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
_GET_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"


def _safe_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


async def _request_report(token: str, query_id: str) -> str | None:
    """Step 1: request report generation, return ReferenceCode."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _SEND_URL,
                params={"t": token, "q": query_id, "v": "3"},
            )
            resp.raise_for_status()
        root = ET.fromstring(resp.text)
        status = root.findtext("Status")
        if status != "Success":
            logger.error("[ibkr_flex] SendRequest failed: %s", resp.text[:200])
            return None
        return root.findtext("ReferenceCode")
    except Exception as exc:
        logger.error("[ibkr_flex] SendRequest error: %s", exc)
        return None


async def _fetch_statement(token: str, ref_code: str) -> str | None:
    """Step 2: download XML statement by ReferenceCode."""
    import asyncio
    await asyncio.sleep(2)  # IBKR needs a moment to generate the report
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                _GET_URL,
                params={"t": token, "q": ref_code, "v": "3"},
            )
            resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.error("[ibkr_flex] GetStatement error: %s", exc)
        return None


def _parse_positions(xml_text: str) -> list[PortfolioPosition]:
    """Parse Flex Query XML into PortfolioPosition list."""
    positions: list[PortfolioPosition] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("[ibkr_flex] XML parse error: %s", exc)
        return positions

    for stmt in root.iter("FlexStatement"):
        report_date_raw = stmt.get("toDate", "")
        # Convert yyyyMMdd → yyyy-MM-dd
        if len(report_date_raw) == 8:
            report_date = f"{report_date_raw[:4]}-{report_date_raw[4:6]}-{report_date_raw[6:]}"
        else:
            report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for pos in stmt.iter("OpenPosition"):
            ticker = pos.get("symbol", "").strip()
            if not ticker:
                continue

            positions.append(PortfolioPosition(
                ticker=ticker,
                report_date=report_date,
                quantity=_safe_float(pos.get("position")),
                mark_price=_safe_float(pos.get("markPrice")),
                position_value=_safe_float(pos.get("positionValue")),
                cost_basis_price=_safe_float(pos.get("costBasisPrice")),
                cost_basis_money=_safe_float(pos.get("costBasisMoney")),
                unrealized_pnl=_safe_float(pos.get("fifoPnlUnrealized")),
                side=pos.get("side"),
                currency=pos.get("currency", "USD"),
                description=pos.get("description"),
                asset_category=pos.get("assetCategory"),
                listing_exchange=pos.get("listingExchange"),
            ))

    return positions


async def collect_ibkr_positions(store: Store) -> list[PortfolioPosition]:
    """Fetch and persist current IBKR portfolio positions.

    Returns list of PortfolioPosition objects (empty on error or missing config).
    """
    token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.getenv("IBKR_FLEX_QUERY_ID", "").strip()

    if not token or not query_id:
        logger.info("[ibkr_flex] IBKR_FLEX_TOKEN or IBKR_FLEX_QUERY_ID not set — skipping")
        return []

    ref_code = await _request_report(token, query_id)
    if not ref_code:
        return []

    xml_text = await _fetch_statement(token, ref_code)
    if not xml_text:
        return []

    positions = _parse_positions(xml_text)
    if not positions:
        logger.warning("[ibkr_flex] No positions parsed from Flex Query response")
        return []

    for pos in positions:
        try:
            await store.upsert_portfolio_position(pos)
        except Exception as exc:
            logger.error("[ibkr_flex] Failed to upsert position %s: %s", pos.ticker, exc)

    logger.info(
        "[ibkr_flex] Portfolio synced: %d positions (report_date=%s)",
        len(positions),
        positions[0].report_date if positions else "?",
    )
    for pos in positions:
        pnl_str = f"{pos.unrealized_pnl:+,.0f}" if pos.unrealized_pnl is not None else "?"
        pct_str = f"{pos.pnl_pct:+.1f}%" if pos.pnl_pct is not None else ""
        logger.info(
            "  %s: %.0f ks @ $%.4f | hodnota $%.0f | P&L $%s %s",
            pos.ticker,
            pos.quantity or 0,
            pos.cost_basis_price or 0,
            pos.position_value or 0,
            pnl_str,
            pct_str,
        )

    return positions
