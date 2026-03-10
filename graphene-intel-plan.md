# Graphene Stock Intelligence Agent — Implementation Plan

## Context

Build an automated intelligence platform for monitoring graphene sector stocks (primarily HGRAF/HydroGraph and BSWGF/Black Swan Graphene), their competitors, and the graphene industry. The system aggregates news from multiple sources, correlates with price/volume data, evaluates significance via Claude AI, and delivers only high-priority alerts via Telegram. Runs on a Hetzner VPS (Ubuntu/Intel).

**Core principle:** Agents do 95% of the work. User receives only significant alerts (score ≥ 7/10) and a daily structured summary.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                     Hetzner VPS (CX32)                       │
│                Ubuntu 24.04, 4 vCPU, 8 GB RAM                │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐ │
│  │  Collector   │  │  Collector   │  │   Price Collector    │ │
│  │  (News)      │  │  (Sentiment) │  │   (yfinance)         │ │
│  │  cron 30min  │  │  cron 30min  │  │   cron 15min         │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘ │
│         │                 │                      │            │
│         ▼                 ▼                      ▼            │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │                   SQLite Database                         │ │
│  │  headlines │ prices │ sentiment │ alerts │ config         │ │
│  └──────────────────────┬───────────────────────────────────┘ │
│                         │                                     │
│                         ▼                                     │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              Evaluator (cron 30min)                        │ │
│  │  • Deduplicate by URL hash                                │ │
│  │  • Claude Haiku: score 1-10, sentiment, impact            │ │
│  │  • Anomaly detection (volume, price spikes)               │ │
│  │  • Cross-reference with sector context                    │ │
│  └──────────────────────┬───────────────────────────────────┘ │
│                         │                                     │
│                    score ≥ 7                                   │
│                         │                                     │
│                         ▼                                     │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              Telegram Notifier                            │ │
│  │  • Instant alerts for score ≥ 7                           │ │
│  │  • Daily summary at 20:00 CET                             │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │           Weekly Deep Analysis (Sunday)                   │ │
│  │  • Claude Sonnet: sector report                           │ │
│  │  • Competitor comparison, patent check                    │ │
│  │  • Cash runway update, insider trades                     │ │
│  │  • Google Trends analysis                                 │ │
│  └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

---

## VPS Requirements

**Recommended: Hetzner CX32**
- 4 vCPU (Intel)
- 8 GB RAM
- 80 GB NVMe SSD
- ~€8/měsíc
- Ubuntu 24.04 LTS

**Why CX32 and not less:**
- Python + SQLite + cron jobs + occasional Claude API calls = ~1-2 GB RAM baseline
- yfinance, feedparser, aiohttp, pytrends etc. can spike RAM during parallel fetches
- Google Trends + Reddit + multiple RSS parses simultaneously = comfortable headroom at 8 GB
- 80 GB disk covers SQLite growth (years of data) + logs + Python venvs
- 4 vCPU lets collectors run in parallel without contention

**Monthly cost estimate:**
- VPS: ~€8
- Claude API (Haiku for screening, Sonnet for weekly report): ~$5-15
- Telegram: free
- All data APIs: free
- **Total: ~€15-25/měsíc**

---

## Project Structure

```
graphene-intel/
├── README.md
├── pyproject.toml              # Dependencies (uv/pip)
├── .env.example                # Template for secrets
├── .env                        # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
│
├── config/
│   ├── tickers.yaml            # Watched tickers + metadata (method, sector, newswire)
│   ├── sources.yaml            # All news sources with URLs, type (rss/api/scrape), priority
│   └── alerts.yaml             # Scoring thresholds, notification rules
│
├── src/
│   ├── __init__.py
│   │
│   ├── collectors/             # Data ingestion modules
│   │   ├── __init__.py
│   │   ├── base.py             # Abstract BaseCollector class
│   │   ├── tickertick.py       # TickerTick API collector
│   │   ├── rss.py              # Generic RSS/Atom collector (GlobeNewsWire, Newsfile, Graphene-Info, etc.)
│   │   ├── google_news.py      # Google News RSS collector
│   │   ├── stocktwits.py       # StockTwits API sentiment
│   │   ├── reddit.py           # Reddit/PRAW collector
│   │   ├── price.py            # yfinance price/volume collector
│   │   ├── sec_edgar.py        # SEC EDGAR filings (Form 4, 10-K, 10-Q)
│   │   ├── google_trends.py    # pytrends search interest
│   │   └── patents.py          # PatentsView API for graphene patents
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py           # SQLite schema (headlines, prices, sentiment, alerts, etc.)
│   │   └── store.py            # Database operations (insert, dedup, query)
│   │
│   ├── evaluator/
│   │   ├── __init__.py
│   │   ├── scorer.py           # Claude Haiku: score headlines 1-10
│   │   ├── anomaly.py          # Volume/price spike detection
│   │   ├── context.py          # Sector context builder (peer prices, macro signals)
│   │   └── prompts.py          # Prompt templates for Claude evaluation
│   │
│   ├── notifier/
│   │   ├── __init__.py
│   │   ├── telegram.py         # Telegram Bot API sender
│   │   └── formatter.py        # Message formatting (alerts, daily summary, weekly report)
│   │
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── daily_summary.py    # Daily summary generator (Claude Sonnet)
│   │   ├── weekly_report.py    # Weekly deep analysis (Claude Sonnet)
│   │   └── backtest.py         # Historical backtest utility
│   │
│   └── utils/
│       ├── __init__.py
│       ├── http.py             # Shared HTTP client with retries, rate limiting
│       └── logging.py          # Structured logging setup
│
├── scripts/
│   ├── collect.py              # Entry point: run all collectors
│   ├── evaluate.py             # Entry point: evaluate new headlines
│   ├── daily_summary.py        # Entry point: generate & send daily summary
│   ├── weekly_report.py        # Entry point: generate & send weekly report
│   ├── price_check.py          # Entry point: price/volume check + anomaly alerts
│   ├── setup_telegram.py       # Helper: setup & test Telegram bot
│   └── backtest.py             # Helper: run historical backtest
│
├── deploy/
│   ├── setup.sh                # VPS initial setup script (apt, uv, venv, cron)
│   ├── crontab                 # Crontab configuration
│   └── graphene-intel.service  # Optional systemd service for watchdog
│
└── tests/
    ├── test_collectors.py
    ├── test_evaluator.py
    ├── test_notifier.py
    └── test_db.py
```

---

## Implementation Areas

### Area 1: Infrastructure & Setup (`deploy/`)

**setup.sh** provisions VPS:
```bash
# System packages
apt update && apt install -y python3.12 python3.12-venv git curl

# Install uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Project setup
git clone <repo> /opt/graphene-intel
cd /opt/graphene-intel
uv venv && uv pip install -e .

# Copy .env, set secrets
cp .env.example .env
# User fills in: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Install crontab
crontab deploy/crontab
```

**crontab:**
```cron
# News collection + evaluation (every 30 min, 6am-midnight CET)
*/30 6-23 * * * cd /opt/graphene-intel && /opt/graphene-intel/.venv/bin/python scripts/collect.py >> /var/log/graphene-intel/collect.log 2>&1
*/35 6-23 * * * cd /opt/graphene-intel && /opt/graphene-intel/.venv/bin/python scripts/evaluate.py >> /var/log/graphene-intel/evaluate.log 2>&1

# Price/volume check (every 15 min during US market hours: 15:30-22:00 CET)
*/15 15-22 * * 1-5 cd /opt/graphene-intel && /opt/graphene-intel/.venv/bin/python scripts/price_check.py >> /var/log/graphene-intel/price.log 2>&1

# Daily summary (20:00 CET)
0 20 * * * cd /opt/graphene-intel && /opt/graphene-intel/.venv/bin/python scripts/daily_summary.py >> /var/log/graphene-intel/daily.log 2>&1

# Weekly deep report (Sunday 18:00 CET)
0 18 * * 0 cd /opt/graphene-intel && /opt/graphene-intel/.venv/bin/python scripts/weekly_report.py >> /var/log/graphene-intel/weekly.log 2>&1
```

### Area 2: Configuration (`config/`)

**tickers.yaml:**
```yaml
primary:
  - ticker: HGRAF
    name: HydroGraph Clean Power
    canadian: HG.CN
    exchange: OTCQB
    production_method: detonation_synthesis
    feedstock: methane
    newswire: globenewswire

  - ticker: BSWGF
    name: Black Swan Graphene
    canadian: SWAN.V
    exchange: OTCQX
    production_method: exfoliation
    feedstock: graphite
    newswire: newsfile

competitors:
  - ticker: NNXPF
    name: NanoXplore
    canadian: GRA
    exchange: OTCQX
    role: sector_leader

  - ticker: GMGMF
    name: Graphene Manufacturing Group
    canadian: GMG
    exchange: OTCQX

  - ticker: ZTEK
    name: Zentek
    exchange: NASDAQ

  - ticker: ARLSF
    name: Argo Graphene
    canadian: ARGO
    exchange: OTCQB

  - ticker: CVV
    name: CVD Equipment
    exchange: NASDAQ
    role: picks_and_shovels

sector_context:
  - ticker: DMAT
    name: Global X Disruptive Materials ETF
    role: sector_proxy

commodities:
  - symbol: NG=F
    name: Natural Gas (Henry Hub)
    relevance: HGRAF feedstock cost

  - symbol: "graphite_spot"
    name: Graphite Spot Price
    relevance: BSWGF/NanoXplore feedstock cost
```

**sources.yaml:**
```yaml
news_apis:
  - name: tickertick
    type: api
    base_url: https://api.tickertick.com/feed
    rate_limit: 10/min
    priority: 1

  - name: google_news
    type: rss
    url_template: "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    queries:
      - HGRAF
      - BSWGF
      - '"HydroGraph Clean Power"'
      - '"Black Swan Graphene"'
      - "graphene stocks"
      - "graphene production"
    priority: 1

rss_feeds:
  - name: graphene_info
    type: rss
    url: https://www.graphene-info.com/rss.xml
    priority: 1
    category: industry

  - name: globenewswire_mining
    type: rss
    url: "https://www.globenewswire.com/RssFeed/subjectcode/05-Mining/feedTitle/GlobeNewswire"
    priority: 1
    category: press_releases

  - name: newsfile_mining
    type: rss
    url: https://www.newsfilecorp.com/feed/mining-metals
    priority: 1
    category: press_releases

  - name: nanowerk
    type: rss
    url: https://www.nanowerk.com/nwfeedcomplete.xml
    priority: 2
    category: industry

  - name: phys_org_graphene
    type: rss
    url: https://phys.org/rss-feed/tags/graphene/
    priority: 2
    category: research

  - name: sec_edgar_hgraf
    type: rss
    url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=hydrograph&type=&dateb=&owner=include&count=20&search_text=&action=getcompany&output=atom"
    priority: 1
    category: filings

sentiment:
  - name: stocktwits
    type: api
    priority: 1
    tickers: [HGRAF, BSWGF]

  - name: reddit
    type: api
    subreddits: [pennystocks, graphene, smallcaps]
    keywords: [HGRAF, BSWGF, HydroGraph, "Black Swan Graphene", graphene]
    priority: 2
```

### Area 3: Database Schema (`src/db/`)

```sql
-- headlines: all collected news items
CREATE TABLE headlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash TEXT UNIQUE NOT NULL,        -- SHA256 of URL for dedup
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,                  -- e.g. "tickertick", "globenewswire"
    published_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tickers TEXT,                          -- JSON array of related tickers
    category TEXT,                         -- press_release, analysis, research, social
    raw_content TEXT,                      -- article body if available

    -- Evaluation fields (filled by evaluator)
    score INTEGER,                         -- 1-10 significance
    sentiment TEXT,                        -- bullish / bearish / neutral
    impact_summary TEXT,                   -- Claude's 1-line summary
    evaluated_at TIMESTAMP
);

-- prices: OHLCV snapshots
CREATE TABLE prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    prev_close REAL,
    change_pct REAL,
    avg_volume_20d REAL,
    volume_ratio REAL,                     -- current / avg_20d
    UNIQUE(ticker, timestamp)
);

-- sentiment_scores: social sentiment snapshots
CREATE TABLE sentiment_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,                   -- stocktwits, reddit, google_trends
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score REAL,                            -- normalized -1.0 to +1.0
    volume INTEGER,                        -- message count / mentions
    raw_data TEXT                           -- JSON blob
);

-- alerts_sent: log of sent notifications (prevent duplicates)
CREATE TABLE alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    headline_id INTEGER REFERENCES headlines(id),
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    alert_type TEXT,                        -- instant, daily_summary, weekly_report
    telegram_message_id INTEGER
);

-- insider_trades: tracked insider transactions
CREATE TABLE insider_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    insider_name TEXT,
    title TEXT,                             -- CEO, CFO, Director
    transaction_type TEXT,                  -- buy, sell, exercise
    shares INTEGER,
    price REAL,
    date DATE,
    source TEXT,                            -- sec_form4, sedi
    filing_url TEXT,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- patent_filings: graphene patent tracking
CREATE TABLE patent_filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patent_id TEXT UNIQUE,
    title TEXT,
    assignee TEXT,
    filing_date DATE,
    publication_date DATE,
    abstract TEXT,
    url TEXT,
    relevance_score INTEGER,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Area 4: Collectors (`src/collectors/`)

Each collector implements `BaseCollector`:

```python
class BaseCollector(ABC):
    @abstractmethod
    async def collect(self) -> list[Headline]:
        """Fetch new items from source. Returns list of Headline objects."""

    def deduplicate(self, headlines: list[Headline]) -> list[Headline]:
        """Filter out already-seen URLs via url_hash lookup in DB."""
```

**Key collectors:**

1. **TickerTick** — `GET api.tickertick.com/feed?q=tt:{ticker}&n=50` for each primary + competitor ticker
2. **RSS** — Generic feedparser-based collector. Iterates over all feeds in sources.yaml
3. **Google News** — RSS with URL-encoded search queries for company names + "graphene"
4. **Price** — yfinance download for all tickers + commodities. Calculates change_pct, volume_ratio
5. **StockTwits** — `GET api.stocktwits.com/api/2/streams/symbol/{ticker}.json`
6. **Reddit** — PRAW subreddit search for keywords
7. **SEC EDGAR** — RSS feed for company filings + `edgartools` for Form 4 parsing
8. **Google Trends** — `pytrends` weekly interest for ticker names (rate-limited, weekly only)
9. **Patents** — PatentsView API search for graphene patents, filtered by recent filing date

### Area 5: Evaluator (`src/evaluator/`)

**scorer.py** — Core intelligence:

```python
async def score_headline(headline: Headline, context: SectorContext) -> EvaluationResult:
    """
    Uses Claude Haiku to evaluate a headline.

    Context includes:
    - Current prices & volume for all tracked tickers
    - Recent sentiment scores
    - Known upcoming catalysts (NASDAQ listing, conferences)
    - Cash runway estimate

    Returns:
    - score: 1-10 (7+ triggers instant alert)
    - sentiment: bullish/bearish/neutral
    - impact_summary: 1-line explanation
    - affected_tickers: which tickers this impacts
    """
```

**Prompt template (prompts.py):**
```
You are a graphene sector analyst monitoring small-cap stocks.

WATCHED TICKERS: {tickers_with_context}
SECTOR CONTEXT: {sector_context}

Evaluate this headline:
Title: {title}
Source: {source}
Date: {date}

Score 1-10 where:
1-3: Routine/irrelevant (skip)
4-6: Mildly interesting but not actionable
7-8: Significant — material news that could move price
9-10: Critical — major catalyst (NASDAQ listing confirmed, major contract, regulatory action, insider selling)

RED FLAGS to score 9-10:
- Management departure
- Going concern warning
- Missed deadlines (e.g. NASDAQ listing postponed)
- Large insider selling
- Cash running out / emergency fundraising
- Paid stock promotion detected

BULLISH CATALYSTS to score 8-10:
- Named customer/partner announced
- Revenue milestone
- Regulatory approval for new application
- NASDAQ listing filing confirmed
- Major patent granted

Respond in JSON: {"score": N, "sentiment": "...", "impact_summary": "...", "affected_tickers": [...]}
```

**anomaly.py** — Price/volume detection:
```python
def detect_anomalies(ticker: str) -> list[Alert]:
    """
    Checks:
    - Volume > 3x 20-day average
    - Price change > ±10% intraday
    - Price crosses below 20-day or 50-day MA
    - Price gap up/down > 5% from previous close
    - NanoXplore (sector leader) drops > 5% (sector risk signal)
    - Natural gas or graphite price spike > 10% (feedstock cost signal)
    """
```

### Area 6: Notifier (`src/notifier/`)

**telegram.py:**
```python
async def send_alert(alert: Alert):
    """Send instant alert for score >= 7"""
    # Format: emoji + ticker + score + 1-line summary + link

async def send_daily_summary(summary: DailySummary):
    """Send at 20:00 CET"""
    # Sections:
    # 📊 Price Overview (table: ticker, price, change%, volume ratio)
    # 📰 Top Headlines (score 5+, max 10)
    # ⚠️ Anomalies (if any)
    # 💬 Social Sentiment (StockTwits/Reddit buzz)
    # 📅 Upcoming (conferences, expected filings)

async def send_weekly_report(report: WeeklyReport):
    """Send Sunday 18:00 CET"""
    # Sections:
    # 📈 Week in Review (price performance table)
    # 🏭 Competitor Update
    # 🔬 Patent Activity
    # 👤 Insider Trades
    # 💰 Cash Runway Estimate
    # 📊 Valuation Context (market cap / revenue ratios)
    # 🔍 Google Trends
    # 🎯 Key Catalysts Tracker (NASDAQ status, Texas facility, etc.)
    # ⚡ Red Flags (if any)
```

### Area 7: Analysis (`src/analysis/`)

**daily_summary.py** — Uses Claude Sonnet:
- Aggregates all headlines, prices, sentiment from past 24h
- Generates structured summary with actionable insights
- Highlights anything unusual

**weekly_report.py** — Uses Claude Sonnet:
- Deep analysis across all dimensions
- Updates cash runway estimate from latest filings
- Checks insider trading (SEC Form 4 / SEDI)
- Patent landscape update
- Google Trends comparison
- Competitor performance comparison table
- Catalyst tracker (NASDAQ listing status, facility construction, etc.)

**backtest.py** — Historical validation:
- Download 12-month history for HGRAF
- Replay news headlines (from TickerTick historical)
- Check if scoring system would have caught major moves
- Calibrate thresholds

---

## Dependencies

```toml
[project]
name = "graphene-intel"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.40",           # Claude API
    "httpx>=0.27",               # Async HTTP client
    "feedparser>=6.0",           # RSS/Atom parsing
    "yfinance>=0.2",             # Stock price data
    "praw>=7.7",                 # Reddit API
    "pytrends>=4.9",             # Google Trends
    "python-telegram-bot>=21",   # Telegram Bot API
    "pyyaml>=6.0",               # Config parsing
    "python-dotenv>=1.0",        # .env file loading
    "aiosqlite>=0.20",           # Async SQLite
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "ruff>=0.8"]
```

---

## Claude Code Execution Prompt

The implementation will be executed via a single Claude Code prompt that orchestrates subagents:

```markdown
# Task: Implement Graphene Stock Intelligence Agent

Read the plan at `tasks/graphene-intel-plan.md` and implement the full project.

## Execution Strategy

Use subagents for parallel implementation of independent areas:

### Phase 1: Foundation (sequential)
1. Create project structure, pyproject.toml, .env.example
2. Implement config/ YAML files
3. Implement src/db/ (schema + store operations)
4. Implement src/utils/ (HTTP client, logging)

### Phase 2: Collectors (parallel subagents)
Launch subagents in parallel:
- **Subagent A**: Implement tickertick.py, google_news.py, rss.py (news APIs)
- **Subagent B**: Implement price.py, stocktwits.py, reddit.py (price + sentiment)
- **Subagent C**: Implement sec_edgar.py, patents.py, google_trends.py (filings + research)
Each subagent: implement collector + write tests

### Phase 3: Intelligence (sequential)
1. Implement evaluator/prompts.py (prompt templates)
2. Implement evaluator/scorer.py (Claude Haiku integration)
3. Implement evaluator/anomaly.py (price/volume detection)
4. Implement evaluator/context.py (sector context builder)

### Phase 4: Output (parallel subagents)
- **Subagent D**: Implement notifier/ (Telegram bot, message formatting)
- **Subagent E**: Implement analysis/ (daily summary, weekly report generators)

### Phase 5: Entry points & deployment
1. Implement all scripts/ entry points
2. Create deploy/setup.sh and deploy/crontab
3. Write README.md with setup instructions

### Phase 6: Testing & validation
1. Run all tests
2. Test each collector individually with real API calls
3. Test Telegram notification delivery
4. Run backtest on 3-month HGRAF history

## Important notes
- Use async/await throughout (httpx + aiosqlite)
- All API calls must have retry logic + rate limiting
- Structured logging (JSON format) for all operations
- Every collector must handle failures gracefully (log + continue)
- Telegram messages must respect 4096 char limit (split if needed)
- Use Haiku (claude-haiku-4-5-20251001) for headline scoring
- Use Sonnet (claude-sonnet-4-6) for daily/weekly reports
```

---

## Verification Plan

1. **Unit tests**: Each collector, evaluator, notifier module has tests
2. **Integration test**: `scripts/collect.py` → check DB has new records
3. **Evaluator test**: Feed known headlines → verify scores are reasonable
4. **Telegram test**: `scripts/setup_telegram.py` sends test message
5. **End-to-end**: Run full collect → evaluate → notify pipeline manually
6. **Backtest**: Validate scoring on HGRAF's $0.15→$8.32 run
7. **Cron validation**: After deploy, verify logs show successful runs for 24h

---

## Pre-deployment Checklist (user must complete)

1. [ ] Create Telegram bot via BotFather, get token
2. [ ] Get Telegram chat_id (send message to bot, query getUpdates)
3. [ ] Have Anthropic API key ready
4. [ ] Provision Hetzner CX32 VPS
5. [ ] SSH access configured
6. [ ] (Optional) Register StockTwits developer account
7. [ ] (Optional) Create Reddit app for PRAW credentials
