"""
Price and volume anomaly detection.

Checks each tickers's latest price snapshot against thresholds from alerts.yaml.
Generates Alert objects for:
  - Volume spikes (> 3× 20-day average)
  - Intraday price change > ±10%
  - Gap from previous close > ±5%
  - Price crosses below MA20 or MA50
  - Sector leader (NanoXplore) drops > 5% (sector risk signal)
  - Commodity spikes (natural gas > 10%)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import yaml

from src.db.store import Store

logger = logging.getLogger(__name__)

ALERTS_CONFIG = "/opt/grafene/config/alerts.yaml"
TICKERS_CONFIG = "/opt/grafene/config/tickers.yaml"


def _load_config() -> dict:
    with open(ALERTS_CONFIG) as f:
        return yaml.safe_load(f)


def _load_tickers() -> dict:
    with open(TICKERS_CONFIG) as f:
        return yaml.safe_load(f)


@dataclass
class PriceAnomaly:
    ticker: str
    anomaly_type: str  # volume_spike|price_spike|price_drop|gap_up|gap_down|ma_breach|sector_signal|commodity_spike
    severity: str      # high|medium|low
    details: str       # human-readable description
    change_pct: Optional[float] = None
    volume_ratio: Optional[float] = None


def _check_volume_spike(price: dict, threshold: float) -> Optional[PriceAnomaly]:
    vol_ratio = price.get("volume_ratio")
    if vol_ratio and vol_ratio >= threshold:
        severity = "high" if vol_ratio >= threshold * 2 else "medium"
        return PriceAnomaly(
            ticker=price["ticker"],
            anomaly_type="volume_spike",
            severity=severity,
            details=f"Volume {vol_ratio:.1f}× 20-day average (threshold: {threshold}×)",
            volume_ratio=vol_ratio,
        )
    return None


def _check_intraday_change(price: dict, threshold: float) -> Optional[PriceAnomaly]:
    change = price.get("change_pct")
    if change is not None and abs(change) >= threshold:
        anomaly_type = "price_spike" if change > 0 else "price_drop"
        severity = "high" if abs(change) >= threshold * 2 else "medium"
        return PriceAnomaly(
            ticker=price["ticker"],
            anomaly_type=anomaly_type,
            severity=severity,
            details=f"Intraday change: {change:+.1f}% (threshold: ±{threshold}%)",
            change_pct=change,
        )
    return None


def _check_gap(price: dict, threshold: float) -> Optional[PriceAnomaly]:
    open_price = price.get("open")
    prev_close = price.get("prev_close")
    if open_price and prev_close and prev_close > 0:
        gap_pct = (open_price - prev_close) / prev_close * 100
        if abs(gap_pct) >= threshold:
            anomaly_type = "gap_up" if gap_pct > 0 else "gap_down"
            return PriceAnomaly(
                ticker=price["ticker"],
                anomaly_type=anomaly_type,
                severity="high",
                details=f"Gap {gap_pct:+.1f}% at open vs prev close (threshold: ±{threshold}%)",
                change_pct=gap_pct,
            )
    return None


def _check_ma_breach(price: dict, periods: list[int]) -> list[PriceAnomaly]:
    """Check if price closed below moving average(s)."""
    anomalies = []
    close = price.get("close")
    if not close:
        return anomalies

    for period in periods:
        ma = price.get(f"ma_{period}")
        if ma and close < ma:
            prev_close = price.get("prev_close")
            # Only alert on fresh breach (prev_close was above MA)
            if prev_close and prev_close >= ma:
                anomalies.append(PriceAnomaly(
                    ticker=price["ticker"],
                    anomaly_type="ma_breach",
                    severity="medium",
                    details=f"Price ${close:.4f} crossed below MA{period} ${ma:.4f}",
                    change_pct=price.get("change_pct"),
                ))
    return anomalies


def detect_anomalies(prices: list[dict], config: Optional[dict] = None) -> list[PriceAnomaly]:
    """
    Run all anomaly checks against a list of price snapshots.
    Returns list of detected anomalies.
    """
    if config is None:
        config = _load_config()

    pa_cfg = config.get("price_anomalies", {})
    volume_threshold = pa_cfg.get("volume_spike_ratio", 3.0)
    intraday_threshold = pa_cfg.get("intraday_change_pct", 10.0)
    gap_threshold = pa_cfg.get("gap_pct", 5.0)
    ma_periods = pa_cfg.get("ma_breach", {}).get("periods", [20, 50])
    sector_leader = pa_cfg.get("sector_leader_ticker", "NNXPF")
    sector_leader_drop = pa_cfg.get("sector_leader_drop_pct", 5.0)
    commodity_spike = pa_cfg.get("commodity_spike_pct", 10.0)

    tickers_cfg = _load_tickers()
    commodity_symbols = {c["symbol"] for c in tickers_cfg.get("commodities", [])}

    anomalies: list[PriceAnomaly] = []

    for price in prices:
        ticker = price.get("ticker", "")
        if not ticker:
            continue

        # Commodity special handling
        if ticker in commodity_symbols:
            change = price.get("change_pct")
            if change is not None and abs(change) >= commodity_spike:
                commodity_name = ticker
                tickers_cfg_commodities = tickers_cfg.get("commodities", [])
                for c in tickers_cfg_commodities:
                    if c["symbol"] == ticker:
                        commodity_name = c.get("name", ticker)
                        break
                anomalies.append(PriceAnomaly(
                    ticker=ticker,
                    anomaly_type="commodity_spike",
                    severity="medium",
                    details=f"{commodity_name}: {change:+.1f}% change (threshold: ±{commodity_spike}%)",
                    change_pct=change,
                ))
            continue

        # Sector leader signal
        if ticker == sector_leader:
            change = price.get("change_pct")
            if change is not None and change <= -sector_leader_drop:
                anomalies.append(PriceAnomaly(
                    ticker=ticker,
                    anomaly_type="sector_signal",
                    severity="high",
                    details=f"Sector leader {ticker} dropped {change:.1f}% — potential sector risk",
                    change_pct=change,
                ))
            continue

        # Standard checks for all tickers
        anom = _check_volume_spike(price, volume_threshold)
        if anom:
            anomalies.append(anom)

        anom = _check_intraday_change(price, intraday_threshold)
        if anom:
            anomalies.append(anom)

        anom = _check_gap(price, gap_threshold)
        if anom:
            anomalies.append(anom)

        anomalies.extend(_check_ma_breach(price, ma_periods))

    if anomalies:
        logger.info("Detected %d anomalies", len(anomalies))
        for a in anomalies:
            logger.info(
                "Anomaly",
                extra={"ticker": a.ticker, "type": a.anomaly_type, "severity": a.severity},
            )

    return anomalies


async def detect_and_report(store: Store) -> list[PriceAnomaly]:
    """
    Fetch latest prices from DB and run anomaly detection.
    High-severity anomalies should be sent as Telegram alerts.
    """
    tickers_cfg = _load_tickers()
    all_tickers = (
        [t["ticker"] for t in tickers_cfg.get("primary", [])]
        + [t["ticker"] for t in tickers_cfg.get("competitors", [])]
        + [t.get("ticker", t.get("symbol", "")) for t in tickers_cfg.get("sector_context", [])]
        + [c["symbol"] for c in tickers_cfg.get("commodities", []) if "symbol" in c]
    )
    all_tickers = [t for t in all_tickers if t and not t.startswith("graphite")]

    prices = await store.get_latest_prices(all_tickers)
    return detect_anomalies(prices)
