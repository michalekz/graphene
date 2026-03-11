# Graphene Intel — Technická dokumentace

> Stav k: **11. března 2026**
> Verze: `main @ eb6c1da`
> Server: `/opt/grafene` · RHEL 10.1 · Python 3.12

---

## Obsah

1. [Přehled projektu](#1-přehled-projektu)
2. [Architektura](#2-architektura)
3. [Struktura projektu](#3-struktura-projektu)
4. [Datové zdroje](#4-datové-zdroje)
5. [Databáze](#5-databáze)
6. [Hodnocení zpráv (scorer)](#6-hodnocení-zpráv-scorer)
7. [Anomaly detection](#7-anomaly-detection)
8. [Telegram notifikace](#8-telegram-notifikace)
9. [Denní a týdenní reporty](#9-denní-a-týdenní-reporty)
10. [Konfigurace](#10-konfigurace)
11. [Deployment & provoz](#11-deployment--provoz)
12. [Stav systému (11. 3. 2026)](#12-stav-systému-11-3-2026)
13. [Známá omezení a plánovaná vylepšení](#13-známá-omezení-a-plánovaná-vylepšení)

---

## 1. Přehled projektu

**Graphene Intel** je automatizovaná zpravodajsko-analytická platforma pro sledování akcií v sektoru grafenu. Kontinuálně sbírá zprávy, ceny, sociální sentiment, insider trades a patenty, hodnotí je pomocí AI a doručuje prioritní upozornění přes Telegram.

### Primárně sledované akcie

| Ticker | Společnost | Burza | Popis |
|--------|------------|-------|-------|
| **HGRAF** | HydroGraph Clean Power Inc. | OTC (US) | Výroba grafenu detonační syntézou |
| **HG.CN** | HydroGraph Clean Power Inc. | CSE (CA) | Kanadský symbol |
| **BSWGF** | Black Swan Graphene Inc. | OTC (US) | CVD/PECVD proces, UK továrna |
| **SWAN.V** | Black Swan Graphene Inc. | TSXV (CA) | Kanadský symbol |

### Konkurenti a sektor (9 tickerů)

`NNXPF` (NanoXplore), `GMGMF`, `ZTEK`, `ARLSF`, `CVV`, `FGPHF`, `DTPKF` + `DMAT` (ETF proxy) + `NG=F` (natural gas futures)

### Co systém dělá

```
Každých 30 min:  collect.py    → sbírá zprávy (TickerTick, RSS, Google News)
Každých 35 min:  evaluate.py   → hodnotí Groq/Claude (1-10), posílá alerty skóre ≥ 7
Každých 15 min:  price_check.py → ceny yfinance, anomalie (US market hours)
20:00 CET:       daily_summary.py → denní souhrn Claude Sonnet
18:00 CET (ned): weekly_report.py → týdenní analýza Claude Sonnet
```

---

## 2. Architektura

### Přehled

```
┌─────────────────────────────────────────────────────────────────┐
│                         CRON SCHEDULER                          │
│   collect(30m) │ evaluate(35m) │ price_check(15m) │ reports     │
└───────┬────────┴──────┬────────┴────────┬──────────┴─────────────┘
        │               │                 │
        ▼               │                 ▼
┌───────────────┐       │        ┌────────────────┐
│  COLLECTORS   │       │        │  PRICE COLLECT │
│  - TickerTick │       │        │  - yfinance    │
│  - RSS/Google │       │        │  - 60d history │
│  - StockTwits │       │        │  - MA20/MA50   │
│  - Reddit     │       │        │  - vol_ratio   │
│  - SEC EDGAR  │       │        └───────┬────────┘
│  - Patents    │       │                │
│  - G.Trends   │       │                ▼
└───────┬───────┘       │        ┌────────────────┐
        │               │        │ ANOMALY DETECT │
        ▼               │        │ - vol_spike 3× │
┌───────────────┐       │        │ - intraday ±10%│
│  SQLite (WAL) │◄──────┘        │ - gap ±5%      │
│  DB           │                │ - MA breach    │
│  - headlines  │◄───────────────┘
│  - prices     │
│  - sentiment  │        ┌────────────────────────┐
│  - trades     │───────►│  EVALUATOR (Groq/LLM)  │
│  - patents    │        │  - score 1-10 per batch│
│  - catalysts  │        │  - sentiment           │
│  - alerts     │        │  - is_red_flag         │
└───────────────┘        └───────────┬────────────┘
                                     │
                                     ▼
                          ┌─────────────────────┐
                          │   TELEGRAM NOTIFIER │
                          │   - instant alerts  │
                          │   - anomaly alerts  │
                          │   - daily summary   │
                          │   - weekly report   │
                          └─────────────────────┘
```

### Klíčové designové rozhodnutí

| Rozhodnutí | Důvod |
|------------|-------|
| **Cron, ne daemon** | Jednodušší provoz, izolované chyby, RHEL-native |
| **SQLite + WAL** | Bez externího DB serveru; WAL umožňuje concurrent čtení při zápisu z cronu |
| **Async I/O** | httpx, aiosqlite — všechny sítě neblokují; price collector a RSS jsou I/O-bound |
| **Groq pro scoring** | llama-3.3-70b je výrazně levnější než Claude Haiku pro rutinní 1-10 hodnocení |
| **Claude Sonnet pro reporty** | Komplexní analýza, kde záleží na kvalitě výstupu |
| **Fallback bez AI** | Všechny AI-závislé funkce mají plain-text fallback při výpadku API |

---

## 3. Struktura projektu

```
/opt/grafene/
├── .env                        # API klíče a konfigurace (nikdy nekomitnout)
├── .env.example                # Šablona pro nové deploymenty
├── .gitignore
├── pyproject.toml              # Projektové metadata a dependencies
├── docs/
│   ├── DOCS.md                 # Tento dokument
│   ├── graphene-intel-plan.md  # Původní implementační plán
│   └── readme-context.md       # Konverzační kontext projektu
│
├── config/
│   ├── tickers.yaml            # Sledované akcie, keywords, katalyzátory
│   ├── sources.yaml            # Datové zdroje (APIs, RSS, sentiment)
│   └── alerts.yaml             # Prahy pro skórování a anomalie
│
├── src/
│   ├── collectors/             # Sběrači dat
│   │   ├── base.py             # ABC BaseCollector + collect_and_store()
│   │   ├── tickertick.py       # TickerTick news API (10k zdrojů, free)
│   │   ├── rss.py              # RSS feeds + Google News RSS
│   │   ├── google_news.py      # Dedikovaný Google News collector
│   │   ├── stocktwits.py       # StockTwits sentiment (bez API klíče)
│   │   ├── reddit.py           # Reddit PRAW (REDDIT_CLIENT_ID/SECRET)
│   │   ├── price.py            # yfinance OHLCV (60 dní history)
│   │   ├── sec_edgar.py        # SEC Form 4 insider trades
│   │   ├── google_trends.py    # Google Trends (pytrends, týdenně)
│   │   └── patents.py          # PatentsView API (týdenně)
│   │
│   ├── db/
│   │   ├── models.py           # SQL schema + seed data (5 katalyzátorů)
│   │   └── store.py            # Store class — veškeré DB operace
│   │
│   ├── evaluator/
│   │   ├── prompts.py          # Claude/Groq prompty (scoring, summary, weekly)
│   │   ├── context.py          # Sestavení kontextu pro LLM (ceny, sentiment)
│   │   ├── scorer.py           # LLM hodnocení (Groq/Anthropic, konfig. backend)
│   │   └── anomaly.py          # Detekce cenových anomálií (rule-based)
│   │
│   ├── notifier/
│   │   ├── formatter.py        # Formátování zpráv pro Telegram (HTML — parse_mode="HTML")
│   │   └── telegram.py         # TelegramNotifier — odesílání + dedup
│   │
│   ├── analysis/
│   │   ├── daily_summary.py    # Denní souhrn přes Claude Sonnet
│   │   └── weekly_report.py    # Týdenní report přes Claude Sonnet
│   │
│   └── utils/
│       ├── http.py             # fetch_json/text + RateLimiter per domain
│       ├── logging.py          # JSON structured logging (file v cronu, tty+file interaktivně)
│       └── dedup.py            # Jaccard title similarity — semantic dedup alertů
│
├── scripts/                    # Cron entry pointy
│   ├── collect.py
│   ├── evaluate.py
│   ├── price_check.py
│   ├── daily_summary.py
│   ├── weekly_report.py
│   └── setup_telegram.py       # Testovací skript pro Telegram
│
├── web/                        # Flask web dashboard
│   ├── app.py                  # Flask aplikace (Bootstrap 5, HTTPS-ready)
│   ├── templates/              # Jinja2 šablony
│   └── static/                 # CSS/JS assets
│
├── deploy/
│   ├── setup.sh                # VPS setup (RHEL/Ubuntu)
│   └── crontab                 # Cron konfigurace
│
└── data/
    └── graphene.db             # SQLite databáze (gitignore)
```

---

## 4. Datové zdroje

### 4.1 Zpravodajské zdroje

| Zdroj | Typ | Rate limit | Pokrytí |
|-------|-----|-----------|---------|
| **TickerTick** | REST API | 10 req/min (12s gap) | ~10 000 zdrojů, per-ticker dotaz |
| **GlobeNewswire** | RSS | — | Press releases (Mining + Technology) |
| **Graphene-Info** | RSS | — | Průmyslový portál #1 pro grafen |
| **phys.org** | RSS | — | Výzkumné publikace o grafenu |
| **Google News** | RSS | 2s gap | 8 dotazů (tickery, company names, sektor) |

**Poznámka k TickerTick**: OR-syntax (`tt:HGRAF OR tt:BSWGF`) vrací 400 Bad Request. Každý ticker se dotazuje zvlášť (sequential). V produkci (30min cron interval) nejsou 429 chyby problémem.

### 4.2 Cenová data

| Zdroj | Knihovna | Co se stahuje |
|-------|----------|---------------|
| **Yahoo Finance** | `yfinance` | OHLCV, 60 dní, 1d interval |

Výpočty nad daty (v `price.py`):
- `ma_20`, `ma_50` — klouzavé průměry
- `avg_volume_20d` — průměrný objem 20 dní
- `volume_ratio` = current_volume / avg_volume_20d
- `change_pct` = (close − prev_close) / prev_close × 100

### 4.3 Sentiment

| Zdroj | Auth | Co vrací |
|-------|------|---------|
| **StockTwits** | Bez klíče (public stream) | bullish/bearish/none na zprávu, score -1.0..+1.0 |
| **Reddit** | PRAW (REDDIT_CLIENT_ID + SECRET) | Skóre ze subredditů + high-upvote posty jako headlines |
| **Google Trends** | pytrends (unofficial) | Interest 0-100, spike detection >200% tydeň/tyden |

Reddit zatím **není nakonfigurován** (chybí REDDIT_CLIENT_ID v `.env`).

### 4.4 Regulatorní a speciální zdroje

| Zdroj | Collector | Jak často | Poznámka |
|-------|-----------|-----------|---------|
| **SEC EDGAR** | `sec_edgar.py` | 2× denně (08:00, 20:00 UTC) | Form 4 insider trades; Canadian companies mohou být na SEDAR místo SEC |
| **PatentsView** | `patents.py` | Týdenně (ned) | Grafen patenty, lookback 90 dní; nový POST API (`/api/v1/patents`), vyžaduje `PATENTSVIEW_API_KEY` |
| **Google Trends** | `google_trends.py` | Týdenně (ned) | 5 klíčových slov, timeframe 90 dní; vyžaduje `urllib3<2` (pin v `pyproject.toml`) |

---

## 5. Databáze

**Typ:** SQLite s WAL journal mode
**Cesta:** `/opt/grafene/data/graphene.db`
**Přístup:** Async přes `aiosqlite`; veškeré operace přes `Store` class

### Tabulky

#### `headlines`
Každá sebraná zpráva. Deduplikace přes `url_hash` (SHA-256 URL).

```sql
id              INTEGER PK
url_hash        TEXT UNIQUE         -- SHA-256(url), primární dedup klíč
url             TEXT
title           TEXT
source          TEXT                -- "tickertick", "globenewswire", "rss", ...
published_at    TIMESTAMP
collected_at    TIMESTAMP DEFAULT now
tickers         TEXT                -- JSON pole: ["HGRAF","BSWGF"]
category        TEXT                -- press_release|analysis|research|social|filing
raw_content     TEXT                -- tělo článku (pokud staženo)
-- Claude/Groq výsledky:
score           INTEGER             -- 1-10
sentiment       TEXT                -- bullish|bearish|neutral
impact_summary  TEXT                -- 1-řádkové vysvětlení
affected_tickers TEXT               -- JSON pole
evaluated_at    TIMESTAMP
is_red_flag     INTEGER DEFAULT 0
is_pump_suspect INTEGER DEFAULT 0
```

#### `prices`
OHLCV snapshot + odvozené metriky. Deduplikace přes `(ticker, timestamp) UNIQUE`.

```sql
id, ticker, timestamp, open, high, low, close, volume,
prev_close, change_pct, avg_volume_20d, volume_ratio, ma_20, ma_50
```

#### `sentiment_scores`
Agregovaný sentiment ze sociálních sítí.

```sql
id, ticker, source (stocktwits|reddit|google_trends),
timestamp, score REAL [-1..+1], volume INTEGER, raw_data TEXT
```

#### `alerts_sent`
Log odeslaných Telegram zpráv pro dedup.

```sql
id, headline_id, sent_at, alert_type (instant|daily_summary|weekly_report|anomaly),
telegram_message_id, content_hash  -- SHA-256 textu zprávy
```

#### `insider_trades`
SEC Form 4 / SEDI transakce. Deduplikace přes `filing_accession UNIQUE`.

```sql
id, ticker, insider_name, title (CEO|CFO|...), transaction_type (buy|sell|exercise|...),
shares, price, value_usd, date, source, filing_url, filing_accession
```

#### `patent_filings`
Grafen patenty z PatentsView. Deduplikace přes `patent_id UNIQUE`.

```sql
id, patent_id, title, assignee, filing_date, publication_date,
relevance_score, keywords_matched, collected_at
```

#### `catalysts`
Manuálně zadané nadcházející události.

```sql
id, ticker, description, expected_date, status (pending|confirmed|passed)
```

**Seed data (k 11. 3. 2026):**
- HGRAF: NASDAQ listing (do června 2026), Texas HQ opening, 2× Hyperion reaktory
- BSWGF: UK továrna na 140t/yr (do června 2026)
- HGRAF: Q1 2026 financial results (15. 5. 2026)

---

## 6. Hodnocení zpráv (scorer)

### LLM Backend

Konfigurace přes env proměnné:

```env
SCORER_BACKEND=groq          # "groq" (default) nebo "anthropic"
SCORER_MODEL=llama-3.3-70b-versatile  # přepis modelu
```

| Backend | Model | Výhody | Nevýhody |
|---------|-------|--------|---------|
| **Groq** (default) | `llama-3.3-70b-versatile` | Rychlý, výrazně levnější | Mírně horší JSON konzistence |
| **Groq** (fast) | `llama-3.1-8b-instant` | Nejrychlejší, nejlevnější | Nižší kvalita analýzy |
| **Anthropic** | `claude-haiku-4-5-20251001` | Nejlepší JSON, konzistentní | Dražší |

### Skórovací schéma (1-10) — kalibrovaný prompt s Tier systémem

**Tier 1** — Přímé zprávy HGRAF / BSWGF (plné skóre):

| Skóre | Kategorie | Příklady |
|-------|-----------|---------|
| **9-10** | Kritické | NASDAQ podání, named customer, going concern, reverse split, SEC vyšetřování, insider >$50K |
| **8-9** | Velmi důležité | Strategický partner, revenue milestone, regulatorní schválení, insider >$25K |
| **7-8** | Důležité | Výrobní milestone, vládní grant, analyst coverage, volume spike |
| **4-6** | Sledovat | Rutinní press release, obecný výzkum |
| **1-3** | Ignorovat | Duplicitní, nesouvisející, obecný komentář |

**Tier 2** — Zprávy konkurentů (NNXPF, GMGMF, ZTEK, ...): skóre se snižuje o 2 body oproti Tier 1 ekvivalentu.

**Tier 3** — Obecný výzkum grafénu, ETF pohyby, komodity: výchozí skóre 2–4.

### LLM Response formát (JSON)

```json
{
  "score": 8,
  "sentiment": "bullish",
  "impact_summary": "HydroGraph delivers first commercial batch to Tesla Energy",
  "affected_tickers": ["HGRAF"],
  "is_red_flag": false,
  "is_pump_suspect": false,
  "reasoning": "Named customer + commercial milestone = strong catalyst"
}
```

### Dávkování a výkon

- Batch: 20 headlines za run (každých 35 min)
- Při 377 headlines + 10 scorovacích rundách: ~377 API volání celkem (1× per headline)
- Cena Groq llama-3.3-70b: ~$0.001 / 1K input tokenů → celá databáze ≈ $0.10

---

## 7. Anomaly Detection

Rule-based detekce v `src/evaluator/anomaly.py`. Prahy konfigurovatelné v `config/alerts.yaml`.

| Typ | Podmínka | Výchozí práh |
|-----|----------|-------------|
| `volume_spike` | `volume_ratio ≥ N` | 3.0× průměr |
| `price_spike` | `change_pct ≥ +N` | +10% intraday |
| `price_drop` | `change_pct ≤ -N` | -10% intraday |
| `gap_up` | Otevření >+N% nad předchozím close | +5% |
| `gap_down` | Otevření >-N% pod předchozím close | -5% |
| `ma_breach` | Close klesne pod MA20 nebo MA50 | — |
| `sector_signal` | NNXPF (sektor leader) klesne >5% | — |
| `commodity_spike` | NG=F (natural gas) spike | — |

Severity: `high` (okamžitý alert), `medium` (agreguje do denního souhrnu).

---

## 8. Telegram notifikace

**Bot:** @ZMGrafenBot
**Token:** viz `.env`
**Chat ID:** viz `.env`

### Typy zpráv

#### Instant alert (score ≥ 7)
```
🚨 HGRAF [9/10] 🟢 BULLISH
📰 HydroGraph Signs Agreement with Major Battery Maker

💡 Named customer + commercial milestone — strong catalyst for NASDAQ uplisting bid.

⚠️ Red Flag | 🎯 NASDAQ catalyst watch
📎 Source: businesswire.com
```

#### Anomaly alert
```
⚡ DTPKF — Significant price drop

📊 Price: -12.4% today
📊 Volume: 4.2× average

💡 Notable volume with double-digit drop — check for news catalyst or sector rotation.
```

### Formátování zpráv

Všechny zprávy používají `parse_mode="HTML"`. Dynamický obsah je HTML-escapován (`&`, `<`, `>`). HTML je spolehlivější než Markdown — nevznikají problémy s nepárovými `*` nebo `_` znaky.

### Deduplication (dvě vrstvy)

1. **Content hash** (`alerts_sent.content_hash`): Identická zpráva se neopakuje 24h.
2. **Semantic dedup** (`src/utils/dedup.py`): Dvoustupňový proces — nejprve Jaccard similarity (threshold `DEDUP_JACCARD_THRESHOLD=0.55`), pak LLM fallback přes Groq `llama-3.1-8b-instant` pro hraniční případy.

```
DEDUP_JACCARD_THRESHOLD — Jaccard koeficient (Stage 1)
  0.0 = žádný dedup
  0.55 = default (55% shodných signifikantních slov)
  0.8 = pouze téměř identické nadpisy
```

**Jak dvoustupňový dedup funguje:**
- Stage 1 — Jaccard: Z titulků se odstraní stopwords (a, the, is, corp, stock, ...). Zbývající tokeny → Jaccard = |průnik| / |sjednocení|. Stejná zpráva z TickerTick + Google News typicky dosahuje 0.65–0.80 → potlačena.
- Stage 2 — LLM (Groq `llama-3.1-8b-instant`, ~$0.0001/call): Pro hraniční případy (Jaccard těsně pod prahem) rozhodne LLM, zda jde o duplicitu.

### Rate limiting

- Telegram API: 1 zpráva/sec (python-telegram-bot enforces)
- Retry: 3 pokusy s exponential backoff (2s, 4s)

---

## 9. Denní a týdenní reporty

### Daily Summary (20:00 CET)

**Model:** `claude-sonnet-4-6`
**Max tokens:** 1 500
**Script:** `scripts/daily_summary.py`

Vstupní data:
- Headlines posledních 24h se skóre ≥ 4
- Aktuální ceny pro všechny tickery
- Sentiment posledních 24h (StockTwits, Reddit)
- Detekované anomálie
- Nadcházející katalyzátory

Výstupní sekce (v Telegram zprávě):
```
📊 Denní souhrn grafenového sektoru — 11. 3. 2026

CENY
HGRAF  $8.23  +18.42%  Vol: 2.1×
BSWGF  $0.67  -10.67%  Vol: 1.4×

TOP ZPRÁVY (skóre ≥ 7)
[1] 9/10 🟢 HydroGraph signs battery partner
[2] 8/10 🟢 Black Swan capacity expansion confirmed

ANOMÁLIE
⚡ DTPKF: price_drop -12.4% (high)

SENTIMENT
HGRAF: 0.875 bullish (StockTwits, 30 zpráv)
BSWGF: 1.000 bullish (StockTwits, 30 zpráv)

KATALYZÁTORY
📅 15. 5. 2026 — HGRAF Q1 financial results
📅 jun 2026 — HGRAF NASDAQ listing application
```

### Weekly Report (neděle 18:00 CET)

**Model:** `claude-sonnet-4-6`
**Max tokens:** 8 000 (navýšeno kvůli truncation — stop_reason=`end_turn` ověřen v logu)
**Script:** `scripts/weekly_report.py`
**Odesílá se:** 2–3 Telegram zprávy (split na 4000 char)

Sekce:
- Performance Summary (týdenní % pohyb tickerů)
- Key Stories (top headlines týdne)
- Insider Activity (SEC Form 4 transakce)
- Competitor Landscape
- Patent Activity (PatentsView)
- Cash Runway (HGRAF: ~10-18m, BSWGF: ~3-6m)
- Google Trends
- Upcoming Catalysts
- Red Flags
- Bottom Line / Investment View

---

## 10. Konfigurace

### `.env` — přehled klíčů

```env
# LLM
GROQ_API_KEY=...          # Pro SCORER_BACKEND=groq
ANTHROPIC_API_KEY=...     # Pro SCORER_BACKEND=anthropic, daily/weekly summary

# Graphene Intel
DB_PATH=/opt/grafene/data/graphene.db
LOG_LEVEL=INFO            # DEBUG|INFO|WARNING|ERROR
LOG_DIR=/var/log/graphene-intel
ALERT_THRESHOLD=7         # Minimální skóre pro Telegram alert (1-10)

# LLM backend
SCORER_BACKEND=groq       # "groq" nebo "anthropic"
SCORER_MODEL=llama-3.3-70b-versatile  # přepis modelu

# Dedup
DEDUP_JACCARD_THRESHOLD=0.55

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Reddit (volitelné)
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=graphene-intel/0.1 by u/michalekz

# PatentsView (volitelné — bez klíče se collector přeskočí)
PATENTSVIEW_API_KEY=

# Web dashboard (volitelné)
DASHBOARD_SECRET=...   # Flask secret key pro sessions
```

### `config/alerts.yaml` — hlavní prahy

```yaml
scoring:
  instant_alert: 7      # ≥ 7 → okamžitý Telegram alert
  daily_summary: 4      # ≥ 4 → zahrne do denního souhrnu
  ignore: 3             # < 3 → přeskočí

anomaly:
  volume_spike_threshold: 3.0     # násobek průměrného objemu
  intraday_change_threshold: 10.0 # % intraday pohyb
  gap_threshold: 5.0              # % gap na otevření
```

### `config/tickers.yaml` — sledovací schéma

Každý ticker může mít:
- `keywords`: list slov pro keyword matching z RSS headlines
- `newswire_sources`: specifické PR newswiry
- `catalysts`: nadcházející události s `expected_date` a `description`

---

## 11. Deployment & provoz

### Instalace (na čistém serveru)

```bash
# 1. Stáhnout projekt
git clone <repo> /opt/grafene
cd /opt/grafene

# 2. Vyplnit API klíče
cp .env.example .env
nano .env

# 3. Spustit setup skript
bash deploy/setup.sh
# Instaluje: python3.12, uv, venv, dependencies, log dirs, crontab

# 4. Otestovat Telegram
.venv/bin/python scripts/setup_telegram.py

# 5. První ruční collection
.venv/bin/python scripts/collect.py

# 6. První evaluace
.venv/bin/python scripts/evaluate.py
```

### Crontab (aktivní)

```cron
MAILTO=""
PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin

# Sběr zpráv (každých 30 min, 6:00-23:30 CET)
*/30 5-22 * * * cd /opt/grafene && .venv/bin/python scripts/collect.py >> /var/log/graphene-intel/collect.log 2>&1

# Hodnocení + instant alerty (každých 35 min)
*/35 5-22 * * * cd /opt/grafene && .venv/bin/python scripts/evaluate.py >> /var/log/graphene-intel/evaluate.log 2>&1

# Cenová kontrola (každých 15 min, US obchodní hodiny, Po-Pá)
*/15 14-21 * * 1-5 cd /opt/grafene && .venv/bin/python scripts/price_check.py >> /var/log/graphene-intel/price.log 2>&1

# Denní souhrn (20:00 CET = 19:00 UTC)
0 19 * * * cd /opt/grafene && .venv/bin/python scripts/daily_summary.py >> /var/log/graphene-intel/daily.log 2>&1

# Týdenní report (neděle 18:00 CET = 17:00 UTC)
0 17 * * 0 cd /opt/grafene && .venv/bin/python scripts/weekly_report.py >> /var/log/graphene-intel/weekly.log 2>&1

# Rotace logů (mazat soubory starší 30 dní, denně 03:00 CET)
0 2 * * * find /var/log/graphene-intel -name "*.log" -mtime +30 -delete
```

**Poznámka k logování:** `setup_logging()` v non-tty módu (cron) zapisuje **pouze do FileHandler** → žádná duplikace. V interaktivním terminálu přidává také stdout.

### Monitorování

```bash
# Sledování živých logů
tail -f /var/log/graphene-intel/collect.log
tail -f /var/log/graphene-intel/evaluate.log

# Stav databáze
.venv/bin/python -c "
import sqlite3; conn = sqlite3.connect('data/graphene.db')
c = conn.cursor()
for t in ['headlines','prices','alerts_sent','sentiment_scores']:
    c.execute(f'SELECT COUNT(*) FROM {t}'); print(t, c.fetchone()[0])
conn.close()"

# Ruční spuštění scriptů
.venv/bin/python scripts/collect.py
.venv/bin/python scripts/evaluate.py
.venv/bin/python scripts/daily_summary.py
```

### Git workflow

```bash
git log --oneline   # commit history
git diff            # nestagované změny
git add src/ && git commit -m "feat: ..."
```

Historie commitů:
- `ccf2e27` — feat: initial implementation (46 files, 9347 lines)
- `b91a24b` — fix: TickerTick OR query → per-ticker requests
- `baf6d8f` — feat: semantic dedup + Groq backend + logging fix
- `69cfc99` — feat: Czech UI, semantic dedup, scoring calibration
- `c4189b1` — fix: calibrate scorer prompt + staleness-aware scoring
- `5ea1ef7` — feat: web dashboard, report fixes, header styling
- `fb73870` — fix: switch to HTML parse_mode, fix Google Trends urllib3
- `eb6c1da` — fix: PatentsView migration to PatentSearch API v2

---

## 12. Stav systému (11. 3. 2026)

### Databáze

| Tabulka | Záznamy | Poznámka |
|---------|---------|---------|
| `headlines` | **377** | 377 scored (0 unscored), 126 high-score (≥7) |
| `prices` | **660** | 60 dní × 11 tickerů |
| `sentiment_scores` | **2** | StockTwits HGRAF (0.875), BSWGF (1.000) |
| `alerts_sent` | **31** | 27 instant, 4 anomálie |
| `catalysts` | **5** | HGRAF (4×), BSWGF (1×) |

### Doručené alerty

Od spuštění (11. 3. 2026 ~01:00 CET) bylo odesláno:
- 19 instant alertů (skóre 7-9) při první evaluaci
- 4 anomaly alertů (DTPKF price_drop, gap_down + 2 opakování)
- 8 dalších instant alertů po background scoringu

### Funkční stav

| Komponenta | Stav |
|-----------|------|
| TickerTick collector | ✅ Funguje (per-ticker mode, fix b91a24b) |
| RSS collector | ✅ Funguje (4 feeds + 8 Google News) |
| Google News collector | ✅ Funguje |
| StockTwits sentiment | ✅ Funguje |
| Reddit sentiment | ⚠️ Nakonfigurováno, ale chybí REDDIT_CLIENT_ID |
| yfinance prices | ✅ Funguje (11/12 tickerů má data) |
| Groq scoring | ✅ Funguje (llama-3.3-70b-versatile, kalibrovaný Tier 1/2/3 prompt) |
| Anomaly detection | ✅ Funguje (DTPKF detekován) |
| Telegram alerts | ✅ Funguje (bot @ZMGrafenBot, chat_id 66087522, parse_mode HTML) |
| Cron pipeline | ✅ Nainstalován |
| Semantic dedup | ✅ Aktivní (Jaccard 0.55 + Groq llama-3.1-8b-instant fallback) |
| SEC EDGAR | ⚠️ Implementováno, Canadian-only tickery nemusí být na SEC |
| Google Trends | ✅ Opraveno (urllib3<2 pin), spouští se týdenně |
| Patents | ✅ Migrováno na PatentSearch API v2 (POST, vyžaduje PATENTSVIEW_API_KEY) |
| Daily summary | ✅ Cron nainstalován (20:00 CET), neproběhl live test |
| Weekly report | ✅ Cron nainstalován (neděle 18:00 CET), MAX_TOKENS=8000, stop_reason ověřen |
| Web dashboard | ✅ Implementováno (Flask, Bootstrap 5, HTTPS-ready) — `web/app.py` |

---

## 13. Známá omezení a plánovaná vylepšení

### Aktuální omezení

| Omezení | Popis | Workaround |
|---------|-------|-----------|
| **Reddit sentiment** | Chybí OAuth credentialy | Doplnit REDDIT_CLIENT_ID do .env |
| **SEDAR (kanadský EDGAR)** | HydroGraph/BSWGF jsou primárně na SEDAR+, ne SEC | Manuální monitoring nebo SEDAR API (není free) |
| **TickerTick coverage** | OTC tickers mají méně zpráv než NYSE/NASDAQ | Pokryto RSS + Google News |
| **yfinance OTC data** | Některé OTC tickery mají neúplná data | Graceful fallback (empty DataFrame přeskočen) |
| **Intraday price** | yfinance free tier = jen EOD data | Pro intraday by bylo potřeba placené API |
| **Telegram flood** | Při prvním spuštění bylo 27 alertů naráz | Semantic dedup + content hash to od teď brání |
| **PatentsView API** | Původní GET API deprecated (2024) | Migrováno na nový POST API v2, vyžaduje `PATENTSVIEW_API_KEY` |
| **Google Trends** | pytrends nekompatibilní s urllib3 2.x | Opraveno pinováním `urllib3<2` v `pyproject.toml` |
| **Price před otevřením** | `close=None` způsobovalo chybu při výpočtu `change_pct` | Opraveno fallbackem na `prev_close` |
| **Weekly report truncation** | Zpráva se zkracovala při nízkém `max_tokens` | Navýšeno na 8000, stop_reason=`end_turn` ověřen |

### Doporučená další vylepšení

1. **Reddit credentialy** — Doplnit do `.env`, přidá sentiment z r/pennystocks, r/smallcaps
2. **Daily summary live test** — Spustit `.venv/bin/python scripts/daily_summary.py` manuálně před prvním live runem
3. **Weekly report live test** — Spustit `.venv/bin/python scripts/weekly_report.py` manuálně
4. **Alert threshold tuning** — Po 1-2 týdnech provozu zhodnotit, zda `ALERT_THRESHOLD=7` dává správnou granularitu
5. **Dedup threshold tuning** — Pokud dedup potlačuje relevantní zprávy, snížit `DEDUP_JACCARD_THRESHOLD` na 0.6
6. **Backtesting scorer** — Porovnat hodnocení Groq llama vs. Claude Haiku na stejné sadě headlines
7. **Catalyst notifications** — Přidat alert den před každým katalyzátorem (`expected_date - 1d`)
8. **DB archivace** — Po 3+ měsících uvážit archivaci starých headlines (SQLite zůstane malý)

---

*Dokumentace aktuální k commitu `eb6c1da` · aktualizováno 11. 3. 2026*
