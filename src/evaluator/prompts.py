"""
Prompt templates for Claude evaluations.

Haiku  → headline scoring (fast, cheap, runs every 30 min)
Sonnet → daily summary + weekly deep report (richer analysis)
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Headline scoring prompt (Claude Haiku)
# ─────────────────────────────────────────────────────────────────────────────

HEADLINE_SCORING_SYSTEM = """You are a strict graphene sector equity analyst. You score headlines 1-10 for alert-worthiness.

CALIBRATION RULE: Most headlines are noise. Expected distribution:
  ~50% → score 1-4  (noise, old news, generic research)
  ~30% → score 5-6  (interesting context, not actionable today)
  ~15% → score 7-8  (significant — alert worthy)
  ~5%  → score 9-10 (critical — rare, only genuine game-changers)

If you are tempted to give a 7+, ask yourself: "Would a fund manager act on this TODAY?"
If no → score 5-6 at most.

Respond ONLY with valid JSON. No markdown, no text outside the JSON object."""

HEADLINE_SCORING_USER = """PRIMARY TICKERS (HGRAF = HydroGraph, BSWGF = Black Swan Graphene):
{tickers_context}

SECTOR CONTEXT:
{sector_context}

UPCOMING CATALYSTS:
{catalysts_context}

TODAY'S DATE: {today}
---
Evaluate this headline:
Title: {title}
Source: {source}
Published: {published_at}
Content snippet: {content_snippet}

SCORING RULES — apply strictly:

STALENESS PENALTY: If the headline was published more than 14 days before today ({today}) → cap score at 5 maximum.
(Old news is already priced in and not actionable.)

TIER 1 — PRIMARY TICKERS (HGRAF, BSWGF) direct news:
  9-10: NASDAQ filing confirmed | Named revenue-generating customer (verifiable company, not vague "partner") | Going concern warning | CEO/CFO departure | Reverse split | SEC/OSC enforcement | Paid promotion detected | Emergency dilution >25%
  8:    Production capacity milestone with specific numbers | Capital raise >C$5M | Major patent granted | Significant insider buy/sell >$25K | Named partner with commercial contract details
  7:    CFO/board appointment | Minor capacity or R&D update | Named application in new industry | Analyst coverage initiated with target price

TIER 2 — COMPETITOR news (NNXPF, GMGMF, ZTEK, etc.) — always score 2 points LOWER than Tier 1 equivalent:
  7:    Competitor going concern, reverse split, or major negative (sector contagion risk)
  5-6:  Competitor partnership, capacity expansion, capital raise (sector signal only)
  3-4:  Competitor routine update

TIER 3 — General graphene research, ETF moves, commodity prices:
  4-5:  Directly relevant to commercialization (e.g., new battery application breakthrough)
  1-3:  Academic paper, general industry trend, no ticker impact

AUTOMATIC SCORE 1-2 for:
  - Same story already covered from a different source
  - No specific ticker information, pure generic commentary
  - Social media speculation without cited facts

Respond with ONLY this JSON (no markdown, no extra text):
{{
  "score": <integer 1-10>,
  "sentiment": "<bullish|bearish|neutral>",
  "impact_summary": "<1 concise sentence explaining score, max 120 chars>",
  "affected_tickers": ["<ticker1>", ...],
  "is_red_flag": <true|false>,
  "is_pump_suspect": <true|false>,
  "reasoning": "<why this tier and score, max 2 sentences>"
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# Daily summary prompt (Claude Sonnet)
# ─────────────────────────────────────────────────────────────────────────────

DAILY_SUMMARY_SYSTEM = """You are a senior graphene sector analyst providing a concise daily briefing.
Format your response as a structured Telegram message using Markdown (Bold, italic, bullet points).
Keep it factual, actionable, and under 3000 characters total.
Focus on what matters for investment decisions. No fluff."""

DAILY_SUMMARY_USER = """Generate a daily briefing for {date} based on this data:

PRICE OVERVIEW:
{prices_table}

TOP HEADLINES (last 24h, scored by AI):
{headlines_list}

PRICE/VOLUME ANOMALIES:
{anomalies_list}

SOCIAL SENTIMENT:
{sentiment_summary}

UPCOMING CATALYSTS:
{catalysts_list}

---
Structure your response EXACTLY as:

📊 *Daily Graphene Intel — {date}*

*Price Overview*
[table: ticker | price | change% | vol ratio]

*Top Stories*
[bullets: score + 1-line summary + source]

*Anomalies & Signals*
[only if any; else omit section]

*Social Sentiment*
[1-2 lines on StockTwits/Reddit buzz]

*Upcoming Catalysts*
[bullets: date | ticker | event]

*Valuation Context*
HGRAF mkt cap ~$Xm on $YK revenue = Xk× revenue multiple. NanoXplore (sector leader with real revenue): ~2.5× revenue.

Keep total length under 3000 characters."""


# ─────────────────────────────────────────────────────────────────────────────
# Weekly deep report prompt (Claude Sonnet)
# ─────────────────────────────────────────────────────────────────────────────

WEEKLY_REPORT_SYSTEM = """You are a senior graphene sector analyst writing a comprehensive weekly report.
Format for Telegram: use Markdown (bold, italic, bullets, tables where space allows).
Be analytical and honest — highlight both risks and opportunities.
Total length: split into multiple messages if needed (each under 4000 chars)."""

WEEKLY_REPORT_USER = """Generate a weekly deep report for week ending {date}:

WEEKLY PRICE PERFORMANCE:
{weekly_prices}

ALL HEADLINES THIS WEEK (scored):
{all_headlines}

INSIDER TRADES (if any):
{insider_trades}

RECENT PATENTS:
{patents}

SOCIAL TRENDS:
{google_trends}
{sentiment_data}

CASH RUNWAY ANALYSIS:
{cash_runway_notes}

PENDING CATALYSTS:
{catalysts}

---
Structure as multiple sections (split into 2-3 messages if needed):

MESSAGE 1:
📈 *Weekly Graphene Intel — Week of {date}*

*Performance Summary*
[table: ticker | week% | mkt cap | vol change]

*Key Stories This Week*
[top 5-8 scored headlines with brief analysis]

*Insider Activity*
[trades if any, otherwise "No insider trades reported"]

MESSAGE 2:
*Competitor Landscape*
[NanoXplore, GMG, Zentek, Argo — what moved and why]

*Patent Activity*
[new patents if any]

*Cash Runway Estimate*
[HGRAF: last known cash + estimated burn rate = X months runway]
[BSWGF: same]

*Google Trends*
[search interest changes for HGRAF, HydroGraph, graphene]

MESSAGE 3:
*Catalyst Tracker*
[table: ticker | expected date | catalyst | status | risk level]

*Red Flags (if any)*
[anything concerning this week]

*Valuation Context*
[market cap vs revenue comparison table for all tracked companies]

*Bottom Line*
[2-3 sentence honest assessment of where we are]

⚠️ _This is automated AI analysis, not investment advice._"""


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly alert prompt (Claude Haiku, short)
# ─────────────────────────────────────────────────────────────────────────────

ANOMALY_ALERT_SYSTEM = """You are a graphene sector analyst. Explain a price/volume anomaly in 1-2 sentences.
Respond with ONLY JSON."""

ANOMALY_ALERT_USER = """Ticker: {ticker}
Anomaly type: {anomaly_type}
Details: {details}
Recent news context: {news_context}

Respond with ONLY this JSON:
{{
  "interpretation": "<1-2 sentence interpretation of what this anomaly might mean>",
  "urgency": "<high|medium|low>",
  "suggested_action": "<watch|investigate|alert>"
}}"""
