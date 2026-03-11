# Graphene Intel

Automated intelligence platform for graphene sector stock monitoring. Continuously collects news, prices, social sentiment, insider trades, and patents — scores them with AI, and delivers prioritized alerts via Telegram. Primary focus is on **HGRAF** (HydroGraph Clean Power) and **BSWGF** (Black Swan Graphene), with coverage of 9 competitor tickers across the graphene sector.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      CRON SCHEDULER                      │
│  collect (30m) │ evaluate (35m) │ price (15m) │ reports  │
└───────┬────────┴───────┬─────────┴──────┬──────┴──────────┘
        │                │                │
        ▼                │                ▼
┌──────────────┐         │       ┌────────────────┐
│  COLLECTORS  │         │       │ PRICE COLLECTOR│
│  TickerTick  │         │       │ yfinance OHLCV │
│  RSS / GNews │         │       │ MA20/MA50/vol  │
│  StockTwits  │         │       └───────┬────────┘
│  Reddit      │         │               │
│  SEC EDGAR   │         │               ▼
│  Google Trend│         │       ┌────────────────┐
│  Patents     │         │       │ ANOMALY DETECT │
└──────┬───────┘         │       │ vol/price/gap  │
       │                 │       └───────┬────────┘
       ▼                 │               │
┌──────────────┐         │               │
│  SQLite DB   │◄────────┴───────────────┘
│  (WAL mode)  │
└──────┬───────┘
       │
       ▼
┌──────────────────┐       ┌─────────────────────┐
│  EVALUATOR       │──────►│  TELEGRAM NOTIFIER  │
│  Groq / Claude   │       │  instant alerts     │
│  score 1-10      │       │  anomaly alerts     │
│  Tier 1/2/3      │       │  daily summary      │
└──────────────────┘       │  weekly report      │
                           └─────────────────────┘
```

**Pipeline summary:**

| Schedule | Script | Action |
|---|---|---|
| Every 30 min (06:00–23:30 CET) | `collect.py` | Fetch headlines from all sources |
| Every 35 min (06:00–23:30 CET) | `evaluate.py` | Score headlines, send alerts (score >= 7) |
| Every 15 min, Mon–Fri (US market hours) | `price_check.py` | Check prices, detect anomalies |
| Daily 20:00 CET | `daily_summary.py` | AI-generated daily digest via Claude Sonnet |
| Sunday 18:00 CET | `weekly_report.py` | Deep weekly analysis via Claude Sonnet |

---

## Requirements

- **Python 3.12+**
- **Linux** (developed and tested on RHEL 10.1; Ubuntu-compatible)
- **[uv](https://github.com/astral-sh/uv)** for dependency management
- **API keys** (see [Configuration](#configuration) below):
  - Groq API key (headline scoring — free tier sufficient)
  - Anthropic API key (daily/weekly reports)
  - Telegram Bot Token + Chat ID
  - Reddit OAuth credentials (optional, for sentiment)
  - USPTO/PatentsView API key (optional, for patent tracking)

---

## Installation

```bash
# 1. Clone the repository
git clone <repo-url> /opt/grafene
cd /opt/grafene

# 2. Copy and fill in environment variables
cp .env.example .env
nano .env

# 3. Run the setup script (installs uv, venv, dependencies, log dirs)
bash deploy/setup.sh

# 4. Test Telegram connectivity
.venv/bin/python scripts/setup_telegram.py

# 5. Initialize DB and run first collection
.venv/bin/python scripts/collect.py

# 6. Run first evaluation (scores headlines, sends alerts)
.venv/bin/python scripts/evaluate.py

# 7. Install crontab
crontab deploy/crontab
```

---

## Configuration

All configuration lives in `.env` (never commit this file). Copy `.env.example` as a starting point.

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key — used for daily/weekly reports (Claude Sonnet) |
| `GROQ_API_KEY` | Yes* | Groq API key — used for headline scoring (llama-3.3-70b) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat/channel ID to receive alerts |
| `SCORER_BACKEND` | No | `groq` (default) or `anthropic` — which LLM backend to use for scoring |
| `SCORER_MODEL` | No | Override the scoring model name (e.g. `llama-3.1-8b-instant`) |
| `DEDUP_USE_LLM` | No | Enable LLM-assisted semantic deduplication for borderline cases |
| `DEDUP_JACCARD_THRESHOLD` | No | Jaccard similarity threshold for dedup (default: `0.55`) |
| `DASHBOARD_USER` | No | Web dashboard login username |
| `DASHBOARD_PASSWORD` | No | Web dashboard login password |
| `DASHBOARD_SECRET` | No | Flask session secret key — generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `USPTO_API_KEY` | No | USPTO/PatentsView API key for patent tracking |
| `REDDIT_CLIENT_ID` | No | Reddit app client ID (for sentiment collection) |
| `REDDIT_CLIENT_SECRET` | No | Reddit app client secret |

*Either `GROQ_API_KEY` or `ANTHROPIC_API_KEY` must be set depending on `SCORER_BACKEND`.

Additional settings (with defaults):

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/opt/grafene/data/graphene.db` | SQLite database path |
| `LOG_DIR` | `/var/log/graphene-intel` | Log file directory |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |
| `ALERT_THRESHOLD` | `7` | Minimum score (1–10) to trigger an instant Telegram alert |

---

## Running Scripts Manually

```bash
# Collect headlines from all sources
.venv/bin/python scripts/collect.py

# Score unscored headlines and send alerts
.venv/bin/python scripts/evaluate.py

# Check prices and detect anomalies
.venv/bin/python scripts/price_check.py

# Generate and send daily summary
.venv/bin/python scripts/daily_summary.py

# Generate and send weekly report
.venv/bin/python scripts/weekly_report.py
```

**Install crontab** (runs all scripts automatically):

```bash
crontab deploy/crontab
```

**Monitor logs:**

```bash
tail -f /var/log/graphene-intel/collect.log
tail -f /var/log/graphene-intel/evaluate.log
```

---

## Web Dashboard

A Flask-based web dashboard ([web/app.py](web/app.py)) provides a browser interface for reviewing headlines, scores, prices, and alerts. Uses Bootstrap 5 dark theme with session-based login.

**Rychlé spuštění (bez nginx):**

```bash
# Nainstaluj a spusť systemd service
cp deploy/dashboard.service /etc/systemd/system/graphene-dashboard.service
systemctl daemon-reload
systemctl enable --now graphene-dashboard
```

Dashboard poběží na **`http://<IP>:5001`** — přihlášení přes `DASHBOARD_USER` / `DASHBOARD_PASSWORD` z `.env`.

**Se správcem reverzní proxy (nginx + HTTPS):**

```bash
# 1. Vygeneruj self-signed TLS certifikát
bash deploy/gen_cert.sh

# 2. Nasaď nginx konfiguraci
cp deploy/nginx-dashboard.conf /etc/nginx/conf.d/graphene-dashboard.conf
nginx -t && systemctl reload nginx
```

Nginx přesměruje HTTP→HTTPS a proxuje provoz na `127.0.0.1:5001`. V tomto případě uprav `dashboard.service` tak, aby gunicorn poslouchal pouze na `127.0.0.1:5001` (ne na `0.0.0.0`).

**Správa služby:**

```bash
systemctl status graphene-dashboard      # stav
systemctl restart graphene-dashboard     # restart
journalctl -u graphene-dashboard -f      # logy v reálném čase
tail -f /var/log/graphene-intel/dashboard-access.log
```

---

## Data Sources

| Source | Type | Coverage |
|---|---|---|
| **TickerTick** | REST API | ~10,000 financial news sources, per-ticker queries |
| **GlobeNewswire** | RSS | Press releases (Mining + Technology feeds) |
| **Graphene-Info** | RSS | Industry portal, graphene-specific news |
| **phys.org** | RSS | Academic/research publications |
| **Google News** | RSS | 8 keyword queries (tickers, company names, sector) |
| **yfinance** | Library | OHLCV prices, 60-day history, MA20/MA50/volume ratios |
| **StockTwits** | Public API | Social sentiment, bullish/bearish signals |
| **Reddit** | PRAW (OAuth) | Sentiment from r/pennystocks, r/smallcaps |
| **SEC EDGAR** | edgartools | Form 4 insider trades |
| **Google Trends** | pytrends | Search interest trends (weekly, 5 keywords) |
| **USPTO PatentsView** | REST API | Graphene patent filings (90-day lookback, weekly) |

---

## Scoring System

Headlines are scored 1–10 by an LLM (Groq llama-3.3-70b by default, with Anthropic Claude as an optional backend) using a three-tier calibrated prompt:

| Tier | Scope | Score range |
|---|---|---|
| **Tier 1** | Direct HGRAF / BSWGF news | Full score (1–10) |
| **Tier 2** | Competitor news (NNXPF, GMGMF, ZTEK, ...) | Reduced by ~2 points |
| **Tier 3** | General graphene research, ETF moves, commodities | 2–4 |

**Score thresholds:**

| Score | Action |
|---|---|
| >= 7 | Instant Telegram alert |
| >= 4 | Included in daily summary |
| < 3 | Ignored |

Each scored headline includes: `score`, `sentiment` (bullish/bearish/neutral), `impact_summary`, `affected_tickers`, `is_red_flag`, `is_pump_suspect`.

**Deduplication** uses a two-stage pipeline: Jaccard title similarity (threshold 0.55) followed by LLM fallback for borderline cases.

---

## Documentation

- Full technical documentation (architecture, DB schema, collectors, scoring, deployment): [`docs/DOCS.md`](docs/DOCS.md)
- Telegram HTML formatting guide: [`docs/telegram-html-guide.md`](docs/telegram-html-guide.md)
