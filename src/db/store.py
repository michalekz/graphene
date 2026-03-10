"""
Database operations for Graphene Intel.
All async, using aiosqlite.

Usage:
    async with Store.connect(db_path) as store:
        await store.insert_headline(...)
        headlines = await store.get_unscored_headlines()
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Optional

import aiosqlite

from .models import SCHEMA_SQL, SEED_CATALYSTS

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/opt/grafene/data/graphene.db")


# ─────────────────────────────────────────────────────────────────────────────
# Data classes (lightweight, no ORM overhead)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Headline:
    url: str
    title: str
    source: str
    published_at: Optional[datetime] = None
    tickers: list[str] = field(default_factory=list)
    category: str = "news"
    raw_content: Optional[str] = None

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()


@dataclass
class EvaluationResult:
    url_hash: str
    score: int
    sentiment: str  # bullish|bearish|neutral
    impact_summary: str
    affected_tickers: list[str]
    is_red_flag: bool = False
    is_pump_suspect: bool = False


@dataclass
class PriceSnapshot:
    ticker: str
    timestamp: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[int]
    prev_close: Optional[float]
    change_pct: Optional[float]
    avg_volume_20d: Optional[float]
    volume_ratio: Optional[float]
    ma_20: Optional[float] = None
    ma_50: Optional[float] = None


@dataclass
class SentimentScore:
    ticker: str
    source: str
    score: float  # -1.0 to +1.0
    volume: int
    raw_data: dict


@dataclass
class InsiderTrade:
    ticker: str
    insider_name: str
    title: str
    transaction_type: str  # buy|sell|exercise
    shares: int
    price: float
    date: str  # ISO date string
    source: str
    filing_url: str
    filing_accession: str
    value_usd: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────────────

class Store:
    """Async SQLite store. Use via async context manager."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    @asynccontextmanager
    async def connect(cls, db_path: str = DB_PATH) -> AsyncGenerator[Store, None]:
        """Open DB, initialize schema, yield store instance."""
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(SCHEMA_SQL)
            await db.commit()
            store = cls(db)
            await store._seed_catalysts_if_empty()
            yield store

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _seed_catalysts_if_empty(self) -> None:
        async with self._db.execute("SELECT COUNT(*) FROM catalysts") as cur:
            row = await cur.fetchone()
            if row[0] > 0:
                return
        for cat in SEED_CATALYSTS:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO catalysts (ticker, description, expected_date, status, notes)
                VALUES (:ticker, :description, :expected_date, :status, :notes)
                """,
                cat,
            )
        await self._db.commit()
        logger.info("Seeded %d catalysts", len(SEED_CATALYSTS))

    # ── Headlines ────────────────────────────────────────────────────────────

    async def headline_exists(self, url_hash: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM headlines WHERE url_hash = ?", (url_hash,)
        ) as cur:
            return await cur.fetchone() is not None

    async def insert_headline(self, h: Headline) -> Optional[int]:
        """Insert new headline. Returns row id, or None if duplicate."""
        if await self.headline_exists(h.url_hash):
            return None
        async with self._db.execute(
            """
            INSERT INTO headlines (url_hash, url, title, source, published_at, tickers, category, raw_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                h.url_hash,
                h.url,
                h.title,
                h.source,
                h.published_at.isoformat() if h.published_at else None,
                json.dumps(h.tickers),
                h.category,
                h.raw_content,
            ),
        ) as cur:
            row_id = cur.lastrowid
        await self._db.commit()
        return row_id

    async def get_unscored_headlines(self, limit: int = 100) -> list[dict]:
        """Return headlines that haven't been evaluated yet."""
        async with self._db.execute(
            """
            SELECT id, url_hash, url, title, source, published_at, tickers, category, raw_content
            FROM headlines
            WHERE score IS NULL
            ORDER BY collected_at ASC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_evaluation(self, result: EvaluationResult) -> None:
        """Write Claude evaluation results back to headline."""
        await self._db.execute(
            """
            UPDATE headlines
            SET score = ?, sentiment = ?, impact_summary = ?, affected_tickers = ?,
                is_red_flag = ?, is_pump_suspect = ?, evaluated_at = CURRENT_TIMESTAMP
            WHERE url_hash = ?
            """,
            (
                result.score,
                result.sentiment,
                result.impact_summary,
                json.dumps(result.affected_tickers),
                int(result.is_red_flag),
                int(result.is_pump_suspect),
                result.url_hash,
            ),
        )
        await self._db.commit()

    async def get_headlines_for_daily_summary(
        self, min_score: int = 4, hours: int = 24
    ) -> list[dict]:
        """Headlines from last N hours with score >= min_score."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._db.execute(
            """
            SELECT id, url, title, source, published_at, tickers, score, sentiment, impact_summary
            FROM headlines
            WHERE score >= ? AND collected_at >= ?
            ORDER BY score DESC, collected_at DESC
            """,
            (min_score, since),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_headlines_for_weekly_report(
        self, min_score: int = 3, days: int = 7
    ) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            """
            SELECT id, url, title, source, published_at, tickers, score, sentiment, impact_summary
            FROM headlines
            WHERE score >= ? AND collected_at >= ?
            ORDER BY score DESC, collected_at DESC
            """,
            (min_score, since),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_unsent_high_score_headlines(self, threshold: int = 7) -> list[dict]:
        """Headlines with score >= threshold not yet sent as instant alert."""
        async with self._db.execute(
            """
            SELECT h.id, h.url, h.title, h.source, h.published_at,
                   h.tickers, h.score, h.sentiment, h.impact_summary,
                   h.is_red_flag, h.is_pump_suspect
            FROM headlines h
            LEFT JOIN alerts_sent a ON a.headline_id = h.id AND a.alert_type = 'instant'
            WHERE h.score >= ? AND a.id IS NULL AND h.evaluated_at IS NOT NULL
            ORDER BY h.score DESC, h.collected_at DESC
            """,
            (threshold,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Prices ───────────────────────────────────────────────────────────────

    async def upsert_price(self, p: PriceSnapshot) -> None:
        await self._db.execute(
            """
            INSERT INTO prices (ticker, timestamp, open, high, low, close, volume,
                                prev_close, change_pct, avg_volume_20d, volume_ratio, ma_20, ma_50)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, timestamp) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume,
                prev_close = excluded.prev_close, change_pct = excluded.change_pct,
                avg_volume_20d = excluded.avg_volume_20d, volume_ratio = excluded.volume_ratio,
                ma_20 = excluded.ma_20, ma_50 = excluded.ma_50
            """,
            (
                p.ticker, p.timestamp.isoformat(),
                p.open, p.high, p.low, p.close, p.volume,
                p.prev_close, p.change_pct, p.avg_volume_20d, p.volume_ratio,
                p.ma_20, p.ma_50,
            ),
        )
        await self._db.commit()

    async def get_latest_prices(self, tickers: list[str]) -> list[dict]:
        """Most recent price snapshot for each ticker."""
        placeholders = ",".join("?" * len(tickers))
        async with self._db.execute(
            f"""
            SELECT p.*
            FROM prices p
            INNER JOIN (
                SELECT ticker, MAX(timestamp) AS max_ts
                FROM prices WHERE ticker IN ({placeholders})
                GROUP BY ticker
            ) latest ON p.ticker = latest.ticker AND p.timestamp = latest.max_ts
            ORDER BY p.ticker
            """,
            tickers,
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_price_history(
        self, ticker: str, days: int = 30
    ) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            """
            SELECT * FROM prices
            WHERE ticker = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (ticker, since),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Sentiment ────────────────────────────────────────────────────────────

    async def insert_sentiment(self, s: SentimentScore) -> None:
        await self._db.execute(
            """
            INSERT INTO sentiment_scores (ticker, source, score, volume, raw_data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (s.ticker, s.source, s.score, s.volume, json.dumps(s.raw_data)),
        )
        await self._db.commit()

    async def get_latest_sentiment(self, ticker: str, hours: int = 24) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._db.execute(
            """
            SELECT * FROM sentiment_scores
            WHERE ticker = ? AND timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (ticker, since),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def log_alert(
        self,
        alert_type: str,
        headline_id: Optional[int] = None,
        telegram_message_id: Optional[int] = None,
        content_hash: Optional[str] = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO alerts_sent (headline_id, alert_type, telegram_message_id, content_hash)
            VALUES (?, ?, ?, ?)
            """,
            (headline_id, alert_type, telegram_message_id, content_hash),
        )
        await self._db.commit()

    async def alert_already_sent(self, content_hash: str, hours: int = 24) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._db.execute(
            "SELECT 1 FROM alerts_sent WHERE content_hash = ? AND sent_at >= ?",
            (content_hash, since),
        ) as cur:
            return await cur.fetchone() is not None

    # ── Insider Trades ───────────────────────────────────────────────────────

    async def insert_insider_trade(self, t: InsiderTrade) -> Optional[int]:
        try:
            async with self._db.execute(
                """
                INSERT INTO insider_trades (ticker, insider_name, title, transaction_type,
                    shares, price, value_usd, date, source, filing_url, filing_accession)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.ticker, t.insider_name, t.title, t.transaction_type,
                    t.shares, t.price, t.value_usd, t.date,
                    t.source, t.filing_url, t.filing_accession,
                ),
            ) as cur:
                row_id = cur.lastrowid
            await self._db.commit()
            return row_id
        except aiosqlite.IntegrityError:
            return None  # duplicate accession

    async def get_recent_insider_trades(self, days: int = 30) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            "SELECT * FROM insider_trades WHERE date >= ? ORDER BY date DESC",
            (since[:10],),  # date only
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Catalysts ────────────────────────────────────────────────────────────

    async def get_pending_catalysts(self) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM catalysts WHERE status = 'pending' ORDER BY expected_date ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_catalyst_status(
        self, catalyst_id: int, status: str, notes: Optional[str] = None
    ) -> None:
        await self._db.execute(
            """
            UPDATE catalysts SET status = ?, notes = COALESCE(?, notes),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, notes, catalyst_id),
        )
        await self._db.commit()

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_db_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        for table in ["headlines", "prices", "sentiment_scores", "alerts_sent",
                      "insider_trades", "patent_filings", "catalysts"]:
            async with self._db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                row = await cur.fetchone()
                stats[table] = row[0]
        return stats
