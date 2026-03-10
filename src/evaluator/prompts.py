"""
Prompt templates for Claude evaluations.

Haiku  → headline scoring (fast, cheap, runs every 30 min)
Sonnet → daily summary + weekly deep report (richer analysis)
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Headline scoring prompt (Claude Haiku)
# ─────────────────────────────────────────────────────────────────────────────

HEADLINE_SCORING_SYSTEM = """You are a graphene sector equity analyst monitoring small-cap stocks.
Your job: evaluate news headlines for significance to these watched companies and score them 1-10.

Respond ONLY with valid JSON, no markdown, no explanation outside the JSON object."""

HEADLINE_SCORING_USER = """WATCHED TICKERS:
{tickers_context}

SECTOR CONTEXT (latest prices & sentiment):
{sector_context}

UPCOMING CATALYSTS:
{catalysts_context}

---
Evaluate this headline:
Title: {title}
Source: {source}
Published: {published_at}
Content snippet: {content_snippet}

Score 1-10 where:
1-3: Routine, irrelevant, or duplicate noise — do not alert
4-6: Mildly interesting, not immediately actionable
7-8: Significant — material news that could move price in next 1-3 days
9-10: Critical — major catalyst or major red flag

SCORE 9-10 for:
- NASDAQ listing application filed or confirmed
- Named customer / named revenue contract announced
- Major insider selling (>$50K value)
- Going concern warning in filing
- Management departure (CEO/CFO)
- Reverse split announcement
- SEC/OSC regulatory enforcement action
- Emergency fundraising (>30% dilution signal)
- Paid stock promotion detected

SCORE 8-9 for:
- Named strategic partner announced (with verifiable company name)
- Revenue milestone announced
- Regulatory approval for new application (EPA, FDA, REACH)
- Major patent granted (to watched company)
- NASDAQ listing process update
- Significant insider buying (>$25K)
- Competitor going concern or major negative (sector sentiment impact)

SCORE 7-8 for:
- Positive production/capacity milestone
- Conference presentation with new data
- Government grant awarded
- Meaningful analyst coverage initiated
- Volume/price anomaly with news catalyst

SCORE 4-6 for:
- Routine press release (participation in event, minor update)
- General graphene research paper (no direct commercial application)
- Competitor minor news with indirect sector implications

SCORE 1-3 for:
- Already evaluated similar story (different source, same facts)
- Unrelated company with "graphene" in name
- Generic market commentary with no company-specific info
- Social media speculation without factual basis

Respond with ONLY this JSON (no markdown, no extra text):
{{
  "score": <integer 1-10>,
  "sentiment": "<bullish|bearish|neutral>",
  "impact_summary": "<1 concise sentence explaining why this score, max 120 chars>",
  "affected_tickers": ["<ticker1>", ...],
  "is_red_flag": <true|false>,
  "is_pump_suspect": <true|false>,
  "reasoning": "<2-3 sentences of analysis, not shown to user but used for calibration>"
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
