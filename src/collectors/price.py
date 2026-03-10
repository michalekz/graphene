"""
Price collector for Graphene Intel.

Downloads OHLCV data for all watched tickers via yfinance and calculates
derived metrics (MA20, MA50, volume ratio, change_pct). Stores results via
Store.upsert_price().

Ticker universe: primary + competitors + sector_context + commodities from
tickers.yaml. OTC tickers may return empty DataFrames — those are skipped
with a warning.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import yaml
import yfinance as yf

from src.db.store import PriceSnapshot, Store

logger = logging.getLogger(__name__)

_TICKERS_PATH = "/opt/grafene/config/tickers.yaml"


def _load_all_tickers() -> list[str]:
    """Load and return the full ticker universe from tickers.yaml.

    Includes primary, competitors, sector_context and commodities sections.
    Commodity symbols that have no direct yfinance feed (e.g. 'graphite_spot')
    are included so that yfinance can attempt a download; empty results are
    handled gracefully in the caller.
    """
    with open(_TICKERS_PATH) as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)

    tickers: list[str] = []

    for entry in cfg.get("primary", []):
        if ticker := entry.get("ticker"):
            tickers.append(ticker)

    for entry in cfg.get("competitors", []):
        if ticker := entry.get("ticker"):
            tickers.append(ticker)

    for entry in cfg.get("sector_context", []):
        if ticker := entry.get("ticker"):
            tickers.append(ticker)

    for entry in cfg.get("commodities", []):
        # commodities use 'symbol' instead of 'ticker'
        if symbol := entry.get("symbol"):
            tickers.append(symbol)

    return tickers


def _safe_float(value: Any) -> Optional[float]:
    """Convert a value to float, returning None on failure."""
    try:
        f = float(value)
        return None if (f != f) else f  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    """Convert a value to int, returning None on failure."""
    try:
        f = float(value)
        if f != f:  # NaN
            return None
        return int(f)
    except (TypeError, ValueError):
        return None


def _build_snapshots_for_ticker(ticker: str, df: "Any") -> list[PriceSnapshot]:
    """Build PriceSnapshot objects for every row in *df* for a single ticker.

    *df* is a single-ticker slice with columns Open/High/Low/Close/Volume
    (all as floats after auto_adjust=True).  Returns an empty list if *df*
    is empty or missing required columns.
    """
    import pandas as pd  # local import — only needed inside thread

    if df is None or df.empty:
        return []

    # Normalise column names to lower-case so we're robust to yfinance changes
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(df.columns)):
        logger.warning(
            "Ticker %s: missing expected columns (got %s)", ticker, list(df.columns)
        )
        return []

    close_series: pd.Series = df["close"]

    # Rolling moving averages over the whole history slice
    ma_20_series = close_series.rolling(window=20, min_periods=1).mean()
    ma_50_series = close_series.rolling(window=50, min_periods=1).mean()

    # 20-day average volume
    avg_vol_20_series = df["volume"].rolling(window=20, min_periods=1).mean()

    snapshots: list[PriceSnapshot] = []

    for i, (idx, row) in enumerate(df.iterrows()):
        # Timestamp — yfinance index is a DatetimeIndex
        if hasattr(idx, "to_pydatetime"):
            ts: datetime = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        close = _safe_float(row.get("close"))
        prev_close: Optional[float] = None
        change_pct: Optional[float] = None

        if i > 0:
            prev_row = df.iloc[i - 1]
            prev_close = _safe_float(prev_row.get("close"))
            if prev_close and prev_close != 0.0 and close is not None:
                change_pct = (close - prev_close) / prev_close * 100.0

        volume = _safe_int(row.get("volume"))
        avg_volume_20d = _safe_float(avg_vol_20_series.iloc[i])
        volume_ratio: Optional[float] = None
        if avg_volume_20d and avg_volume_20d > 0 and volume is not None:
            volume_ratio = volume / avg_volume_20d

        snapshots.append(
            PriceSnapshot(
                ticker=ticker,
                timestamp=ts,
                open=_safe_float(row.get("open")),
                high=_safe_float(row.get("high")),
                low=_safe_float(row.get("low")),
                close=close,
                volume=volume,
                prev_close=prev_close,
                change_pct=change_pct,
                avg_volume_20d=avg_volume_20d,
                volume_ratio=volume_ratio,
                ma_20=_safe_float(ma_20_series.iloc[i]),
                ma_50=_safe_float(ma_50_series.iloc[i]),
            )
        )

    return snapshots


def _download_prices_sync(tickers: list[str]) -> dict[str, Any]:
    """Synchronous yfinance download — must be called via asyncio.to_thread().

    Returns a dict mapping ticker -> DataFrame (possibly empty).
    """
    import pandas as pd

    if not tickers:
        return {}

    logger.info("Downloading price data for %d tickers via yfinance", len(tickers))

    # Download all tickers in one request; group_by='ticker' gives a
    # MultiIndex DataFrame with the ticker as the top-level column.
    raw = yf.download(
        tickers,
        period="60d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )

    result: dict[str, pd.DataFrame] = {}

    if raw.empty:
        logger.warning("yfinance returned an empty DataFrame for the full batch")
        return result

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                # Single-ticker download returns a flat DataFrame
                df: pd.DataFrame = raw
            else:
                # Multi-ticker: top-level columns are tickers
                if ticker not in raw.columns.get_level_values(0):
                    logger.warning("Ticker %s not present in yfinance response", ticker)
                    result[ticker] = pd.DataFrame()
                    continue
                df = raw[ticker].copy()

            if df.empty:
                logger.warning("Ticker %s: yfinance returned empty DataFrame — OTC/no data", ticker)
            result[ticker] = df

        except Exception as exc:
            logger.warning("Ticker %s: error slicing yfinance data: %s", ticker, exc)
            result[ticker] = pd.DataFrame()

    return result


async def collect_prices(store: Store) -> list[PriceSnapshot]:
    """Collect price/volume data for all tickers and persist to *store*.

    Downloads 60 days of daily history from yfinance in a background thread,
    computes MA20, MA50, avg_volume_20d, volume_ratio and change_pct for
    every bar, stores each snapshot via store.upsert_price(), and returns
    the full list of snapshots (all tickers, all bars).

    OTC tickers with no data are skipped with a warning.  All exceptions are
    caught so that a single bad ticker never aborts the entire run.

    Args:
        store: Open Store instance to persist results.

    Returns:
        List of PriceSnapshot dataclasses (may be empty on total failure).
    """
    all_snapshots: list[PriceSnapshot] = []

    try:
        tickers = _load_all_tickers()
        logger.info("Price collector: ticker universe = %s", tickers)
    except Exception as exc:
        logger.error("Failed to load tickers.yaml: %s", exc)
        return all_snapshots

    try:
        ticker_dfs: dict[str, Any] = await asyncio.to_thread(
            _download_prices_sync, tickers
        )
    except Exception as exc:
        logger.error("yfinance download failed: %s", exc)
        return all_snapshots

    for ticker, df in ticker_dfs.items():
        try:
            snapshots = _build_snapshots_for_ticker(ticker, df)
            if not snapshots:
                continue

            for snap in snapshots:
                try:
                    await store.upsert_price(snap)
                except Exception as exc:
                    logger.error(
                        "Failed to upsert price for %s @ %s: %s",
                        ticker, snap.timestamp, exc,
                    )

            all_snapshots.extend(snapshots)
            logger.info(
                "Ticker %s: stored %d price bars (latest close=%.4f)",
                ticker,
                len(snapshots),
                snapshots[-1].close if snapshots[-1].close is not None else float("nan"),
            )

        except Exception as exc:
            logger.error("Unexpected error processing ticker %s: %s", ticker, exc)

    logger.info(
        "Price collector complete: %d snapshots across %d tickers",
        len(all_snapshots),
        len(ticker_dfs),
    )
    return all_snapshots
