"""
Database schema for Graphene Intel.
SQLite via aiosqlite — async-first.

Tables:
  headlines       — all collected news items + evaluation results
  prices          — OHLCV snapshots per ticker
  sentiment_scores — StockTwits / Reddit / Google Trends
  alerts_sent     — dedup log for sent Telegram notifications
  insider_trades  — SEC Form 4 / SEDI insider transactions
  patent_filings  — graphene patent tracking
  catalysts       — manually tracked upcoming events / milestones
"""

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ─────────────────────────────────────────────
-- headlines: collected news items + AI scoring
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS headlines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash        TEXT    UNIQUE NOT NULL,    -- SHA-256(url) for dedup
    url             TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    source          TEXT    NOT NULL,           -- e.g. "tickertick", "globenewswire"
    published_at    TIMESTAMP,
    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tickers         TEXT,                       -- JSON array, e.g. ["HGRAF","BSWGF"]
    category        TEXT,                       -- press_release|analysis|research|social|filing
    raw_content     TEXT,                       -- article body if fetched
    -- Claude evaluation (filled by evaluator)
    score           INTEGER,                    -- 1-10 significance
    sentiment       TEXT,                       -- bullish|bearish|neutral
    impact_summary  TEXT,                       -- 1-line explanation from Claude
    affected_tickers TEXT,                      -- JSON array from Claude
    evaluated_at    TIMESTAMP,
    -- Flags
    is_red_flag     INTEGER DEFAULT 0,          -- 1 if red flag detected
    is_pump_suspect INTEGER DEFAULT 0           -- 1 if pump & dump pattern
);

CREATE INDEX IF NOT EXISTS idx_headlines_collected ON headlines(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_headlines_score ON headlines(score DESC);
CREATE INDEX IF NOT EXISTS idx_headlines_source ON headlines(source);
CREATE INDEX IF NOT EXISTS idx_headlines_tickers ON headlines(tickers);

-- ─────────────────────────────────────────────
-- prices: OHLCV snapshots
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    timestamp       TIMESTAMP NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume          INTEGER,
    prev_close      REAL,
    change_pct      REAL,                       -- (close - prev_close) / prev_close * 100
    avg_volume_20d  REAL,                       -- rolling 20-day average volume
    volume_ratio    REAL,                       -- volume / avg_volume_20d
    ma_20           REAL,                       -- 20-day moving average
    ma_50           REAL,                       -- 50-day moving average
    UNIQUE(ticker, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_prices_ticker_ts ON prices(ticker, timestamp DESC);

-- ─────────────────────────────────────────────
-- sentiment_scores: social sentiment snapshots
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sentiment_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    source          TEXT    NOT NULL,           -- stocktwits|reddit|google_trends
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score           REAL,                       -- normalized -1.0 to +1.0
    volume          INTEGER,                    -- message count / mentions
    raw_data        TEXT                        -- JSON blob
);

CREATE INDEX IF NOT EXISTS idx_sentiment_ticker_ts ON sentiment_scores(ticker, timestamp DESC);

-- ─────────────────────────────────────────────
-- alerts_sent: dedup log for Telegram notifications
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts_sent (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    headline_id         INTEGER REFERENCES headlines(id) ON DELETE SET NULL,
    sent_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    alert_type          TEXT    NOT NULL,       -- instant|daily_summary|weekly_report|anomaly
    telegram_message_id INTEGER,
    content_hash        TEXT                    -- SHA-256 of message body for dedup
);

CREATE INDEX IF NOT EXISTS idx_alerts_sent_at ON alerts_sent(sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_headline ON alerts_sent(headline_id);

-- ─────────────────────────────────────────────
-- insider_trades: SEC Form 4 / SEDI
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insider_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    insider_name        TEXT,
    title               TEXT,                   -- CEO|CFO|Director|Officer
    transaction_type    TEXT,                   -- buy|sell|exercise|gift
    shares              INTEGER,
    price               REAL,
    value_usd           REAL,                   -- shares * price
    date                DATE,
    source              TEXT,                   -- sec_form4|sedi
    filing_url          TEXT,
    filing_accession    TEXT    UNIQUE,         -- SEC accession number (dedup)
    collected_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_insider_ticker ON insider_trades(ticker, date DESC);

-- ─────────────────────────────────────────────
-- patent_filings: graphene patent tracking
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patent_filings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    patent_id           TEXT    UNIQUE,
    title               TEXT,
    assignee            TEXT,
    filing_date         DATE,
    publication_date    DATE,
    abstract            TEXT,
    url                 TEXT,
    relevance_score     INTEGER,                -- 1-10 relevance to watched companies
    keywords_matched    TEXT,                   -- JSON array
    collected_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_patent_date ON patent_filings(filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_patent_assignee ON patent_filings(assignee);

-- ─────────────────────────────────────────────
-- catalysts: manually tracked milestones
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS catalysts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    expected_date       DATE,
    status              TEXT    DEFAULT 'pending',  -- pending|completed|missed|delayed
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Seed catalysts — known upcoming events as of March 2026
SEED_CATALYSTS = [
    {
        "ticker": "HGRAF",
        "description": "NASDAQ listing application filed",
        "expected_date": "2026-06-30",
        "status": "pending",
        "notes": "CEO stated 'mid-2026' in Oct 2025 media interview. Not yet formally filed."
    },
    {
        "ticker": "HGRAF",
        "description": "Texas HQ / second production facility opens (Austin TX)",
        "expected_date": "2026-06-01",
        "status": "pending",
        "notes": "C$30M offering (March 2026) funds this expansion."
    },
    {
        "ticker": "HGRAF",
        "description": "Two new Hyperion reactors commissioned",
        "expected_date": "2026-09-30",
        "status": "pending",
        "notes": "Part of Texas expansion plan."
    },
    {
        "ticker": "BSWGF",
        "description": "UK facility capacity expansion to 140t/yr (GEA Ariete 3160)",
        "expected_date": "2026-06-30",
        "status": "pending",
        "notes": "New unit ordered, up from 40t/yr current capacity."
    },
    {
        "ticker": "HGRAF",
        "description": "Q1 2026 financial results / filing",
        "expected_date": "2026-05-15",
        "status": "pending",
        "notes": "Watch for cash burn rate update and runway calculation."
    },
]
