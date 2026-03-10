"""
Message formatter for Telegram notifications.

All output uses standard Markdown (parse_mode="Markdown"), which supports:
  *bold*, _italic_, `code`, [text](url)

Dynamic content that may contain special Markdown characters is escaped
before embedding into formatted strings.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.evaluator.anomaly import PriceAnomaly

logger = logging.getLogger(__name__)

# ── Telegram limits ────────────────────────────────────────────────────────────

TELEGRAM_MAX_CHARS = 4096
SPLIT_THRESHOLD = 4000  # conservative split boundary


# ── Emoji constants ────────────────────────────────────────────────────────────

EMOJI_CRITICAL = "🚨"
EMOJI_WARNING = "⚠️"
EMOJI_ANNOUNCE = "📢"
EMOJI_NEWS = "📰"
EMOJI_BULLISH = "🟢"
EMOJI_BEARISH = "🔴"
EMOJI_NEUTRAL = "⬜"
EMOJI_ANOMALY = "⚡"
EMOJI_INSIGHT = "💡"
EMOJI_CATALYST = "🎯"
EMOJI_PRICE = "📊"
EMOJI_LINK = "🔗"
EMOJI_FLAG = "⚠️"

# Anomaly-type labels used in format_anomaly_alert
_ANOMALY_LABELS: dict[str, str] = {
    "volume_spike": "Volume Spike",
    "price_spike": "Price Spike",
    "price_drop": "Price Drop",
    "gap_up": "Gap Up",
    "gap_down": "Gap Down",
    "ma_breach": "MA Breach",
    "sector_signal": "Sector Signal",
    "commodity_spike": "Commodity Spike",
}

# Contextual insight strings for anomaly types
_ANOMALY_INSIGHTS: dict[str, str] = {
    "volume_spike": "Unusual activity without press release — watch for news",
    "price_spike": "Sharp intraday move — check for catalyst or halt",
    "price_drop": "Sharp intraday decline — monitor for further weakness",
    "gap_up": "Significant gap up at open — confirm with volume",
    "gap_down": "Significant gap down at open — potential sell-off",
    "ma_breach": "Price crossed below moving average — bearish technical signal",
    "sector_signal": "Sector leader weakness — review all holdings for risk",
    "commodity_spike": "Commodity price spike may affect production costs",
}


# ── Escaping helpers ───────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """
    Escape characters that have special meaning in Telegram's standard Markdown.

    Standard Markdown (parse_mode="Markdown") only requires escaping of:
      `  *  _  [
    within non-formatted regions.  We also escape ] for symmetry.
    """
    if not text:
        return text
    # Order matters: escape backslash first to avoid double-escaping
    for ch in ("\\", "`", "*", "_", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ── Score helpers ──────────────────────────────────────────────────────────────

def _score_emoji(score: int) -> str:
    """Return alert-level emoji for a given score."""
    if score >= 9:
        return EMOJI_CRITICAL
    if score == 8:
        return EMOJI_WARNING
    if score == 7:
        return EMOJI_ANNOUNCE
    return EMOJI_NEWS


def _sentiment_emoji(sentiment: str) -> str:
    """Map sentiment string to display emoji."""
    s = (sentiment or "").lower()
    if s == "bullish":
        return EMOJI_BULLISH
    if s == "bearish":
        return EMOJI_BEARISH
    return EMOJI_NEUTRAL


def _sentiment_label(sentiment: str) -> str:
    """Return uppercase sentiment label."""
    return (sentiment or "neutral").upper()


# ── Public formatters ──────────────────────────────────────────────────────────

def format_instant_alert(
    headline: dict,
    anomaly: Optional[PriceAnomaly] = None,
) -> str:
    """
    Format a single high-score headline for an instant Telegram alert.

    Example output:
        🚨 *HGRAF* — Score: 9/10 🔴
        📰 HydroGraph files NASDAQ application
        💡 Major catalyst: listing confirmed, expect significant volume spike
        🔗 [Source: GlobeNewsWire](url)
        ⚠️ RED FLAG | 🎯 BULLISH

    Args:
        headline: Dict with keys: tickers (str|list), score, title,
                  impact_summary, url, source, sentiment,
                  is_red_flag, is_pump_suspect.
        anomaly:  Optional PriceAnomaly to append anomaly context.

    Returns:
        Formatted message string suitable for Telegram Markdown.
    """
    score: int = int(headline.get("score") or 0)

    # Tickers may be stored as a JSON list string or actual list
    tickers_raw = headline.get("tickers") or []
    if isinstance(tickers_raw, str):
        import json
        try:
            tickers_raw = json.loads(tickers_raw)
        except (ValueError, TypeError):
            tickers_raw = [tickers_raw] if tickers_raw else []
    ticker_str = ", ".join(tickers_raw) if tickers_raw else "—"

    title = _escape_md(str(headline.get("title") or ""))
    impact = _escape_md(str(headline.get("impact_summary") or ""))
    url = str(headline.get("url") or "")
    source = _escape_md(str(headline.get("source") or "Unknown"))
    sentiment = str(headline.get("sentiment") or "neutral")
    is_red_flag: bool = bool(headline.get("is_red_flag"))
    is_pump_suspect: bool = bool(headline.get("is_pump_suspect"))

    alert_emoji = _score_emoji(score)
    sent_emoji = _sentiment_emoji(sentiment)

    lines: list[str] = []

    # Header line: alert emoji + tickers in bold + score + sentiment colour
    lines.append(f"{alert_emoji} *{_escape_md(ticker_str)}* — Score: {score}/10 {sent_emoji}")

    # Headline title
    lines.append(f"{EMOJI_NEWS} {title}")

    # Impact summary (insight)
    if impact:
        lines.append(f"{EMOJI_INSIGHT} {impact}")

    # Source link
    if url:
        lines.append(f"{EMOJI_LINK} [Source: {source}]({url})")
    else:
        lines.append(f"{EMOJI_LINK} Source: {source}")

    # Flags / sentiment footer
    footer_parts: list[str] = []
    if is_red_flag:
        footer_parts.append(f"{EMOJI_FLAG} RED FLAG")
    if is_pump_suspect:
        footer_parts.append("🚩 PUMP SUSPECT")
    footer_parts.append(f"{EMOJI_CATALYST} {_sentiment_label(sentiment)}")
    lines.append(" | ".join(footer_parts))

    # Optional anomaly context
    if anomaly is not None:
        label = _ANOMALY_LABELS.get(anomaly.anomaly_type, anomaly.anomaly_type.replace("_", " ").title())
        lines.append(f"{EMOJI_ANOMALY} *{label}*: {_escape_md(anomaly.details)}")

    message = "\n".join(lines)

    # Enforce instant alert character budget (trim impact if needed)
    if len(message) > 500:
        # Re-build without impact line to stay compact
        short_lines = [lines[0], lines[1]]
        short_lines.extend(lines[2:])  # keep; trimming only if still too long
        message = "\n".join(short_lines)
        if len(message) > 500:
            message = message[:497] + "…"

    return message


def format_anomaly_alert(anomaly: PriceAnomaly) -> str:
    """
    Format a price/volume anomaly for an instant Telegram alert.

    Example output:
        ⚡ *Volume Spike* — HGRAF
        📊 Volume 4.2× 20-day average
        💡 Unusual activity without press release — watch for news

    Args:
        anomaly: PriceAnomaly dataclass instance.

    Returns:
        Formatted message string suitable for Telegram Markdown.
    """
    label = _ANOMALY_LABELS.get(
        anomaly.anomaly_type,
        anomaly.anomaly_type.replace("_", " ").title(),
    )
    ticker = _escape_md(anomaly.ticker)
    details = _escape_md(anomaly.details)
    insight = _escape_md(
        _ANOMALY_INSIGHTS.get(anomaly.anomaly_type, "Review position and recent news")
    )

    lines: list[str] = [
        f"{EMOJI_ANOMALY} *{_escape_md(label)}* — {ticker}",
        f"{EMOJI_PRICE} {details}",
        f"{EMOJI_INSIGHT} {insight}",
    ]

    return "\n".join(lines)


def format_daily_summary(
    prices: list[dict],
    headlines: list[dict],
    anomalies: list[PriceAnomaly],
    sentiment: dict[str, list[dict]],
    catalysts: list[dict],
    ai_summary: str,
) -> list[str]:
    """
    Format the complete daily summary.

    If *ai_summary* is non-empty (produced by Claude Sonnet), it is used
    directly after a brief header and split to fit Telegram's limit.
    Otherwise the function assembles a structured summary from the raw data.

    Args:
        prices:     List of latest price-snapshot dicts.
        headlines:  List of scored headline dicts (score >= daily threshold).
        anomalies:  List of detected PriceAnomaly instances.
        sentiment:  Mapping of ticker -> list of sentiment-score dicts.
        catalysts:  List of pending catalyst dicts.
        ai_summary: Pre-formatted prose from Claude Sonnet (may be empty).

    Returns:
        List of message strings, each within SPLIT_THRESHOLD characters.
    """
    from datetime import datetime, timezone

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = f"{EMOJI_PRICE} *Daily Summary — {date_str}*\n"

    if ai_summary and ai_summary.strip():
        full_text = header + ai_summary.strip()
        return split_message(full_text, max_len=SPLIT_THRESHOLD)

    # ── Structured fallback ──────────────────────────────────────────────────
    sections: list[str] = [header]

    # Price overview
    if prices:
        price_lines: list[str] = [f"\n{EMOJI_PRICE} *Prices*"]
        for p in prices[:15]:  # cap at 15 rows
            ticker = _escape_md(str(p.get("ticker") or ""))
            close = p.get("close")
            chg = p.get("change_pct")
            vol_ratio = p.get("volume_ratio")
            if close is None:
                continue
            line = f"  • *{ticker}* ${close:.4f}"
            if chg is not None:
                sent_em = EMOJI_BULLISH if chg >= 0 else EMOJI_BEARISH
                line += f" ({chg:+.1f}%) {sent_em}"
            if vol_ratio is not None and vol_ratio >= 2.0:
                line += f"  vol {vol_ratio:.1f}×"
            price_lines.append(line)
        sections.append("\n".join(price_lines))

    # Top headlines
    if headlines:
        hl_lines: list[str] = [f"\n{EMOJI_NEWS} *Top Headlines*"]
        for h in headlines[:10]:
            score = int(h.get("score") or 0)
            title = _escape_md(str(h.get("title") or ""))
            url = str(h.get("url") or "")
            sent_em = _sentiment_emoji(str(h.get("sentiment") or "neutral"))
            em = _score_emoji(score)
            if url:
                hl_lines.append(f"  {em} [{title}]({url}) {sent_em} ({score}/10)")
            else:
                hl_lines.append(f"  {em} {title} {sent_em} ({score}/10)")
        sections.append("\n".join(hl_lines))

    # Anomalies
    if anomalies:
        an_lines: list[str] = [f"\n{EMOJI_ANOMALY} *Anomalies*"]
        for a in anomalies:
            label = _ANOMALY_LABELS.get(a.anomaly_type, a.anomaly_type)
            an_lines.append(
                f"  {EMOJI_ANOMALY} *{_escape_md(a.ticker)}* — {_escape_md(label)}: "
                f"{_escape_md(a.details)}"
            )
        sections.append("\n".join(an_lines))

    # Sentiment snapshot
    if sentiment:
        sent_lines: list[str] = [f"\n{EMOJI_INSIGHT} *Sentiment*"]
        for ticker, scores in sentiment.items():
            if not scores:
                continue
            avg = sum(s.get("score", 0) for s in scores) / len(scores)
            em = EMOJI_BULLISH if avg > 0.1 else (EMOJI_BEARISH if avg < -0.1 else EMOJI_NEUTRAL)
            sent_lines.append(f"  • *{_escape_md(ticker)}* {avg:+.2f} {em}")
        sections.append("\n".join(sent_lines))

    # Pending catalysts
    if catalysts:
        cat_lines: list[str] = [f"\n{EMOJI_CATALYST} *Pending Catalysts*"]
        for c in catalysts[:5]:
            ticker = _escape_md(str(c.get("ticker") or ""))
            desc = _escape_md(str(c.get("description") or ""))
            exp = str(c.get("expected_date") or "TBD")
            cat_lines.append(f"  {EMOJI_CATALYST} *{ticker}* — {desc} (by {_escape_md(exp)})")
        sections.append("\n".join(cat_lines))

    full_text = "\n".join(sections).strip()
    return split_message(full_text, max_len=SPLIT_THRESHOLD)


def format_weekly_report(ai_report: str) -> list[str]:
    """
    Split a Claude Sonnet weekly report into Telegram-sized chunks.

    Splits prefer section boundaries (lines starting with '## ') so that
    each chunk is a coherent section.  Falls back to split_message if the
    report contains no headings or a single section is still too long.

    Args:
        ai_report: Full report text produced by Claude Sonnet.

    Returns:
        List of message strings, each <= SPLIT_THRESHOLD characters.
    """
    if not ai_report or not ai_report.strip():
        return []

    header = f"{EMOJI_PRICE} *Weekly Report*\n\n"
    full_text = header + ai_report.strip()

    # Attempt to split on ## section headings
    import re
    parts = re.split(r"(?=\n## )", full_text)

    chunks: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        if len(current) + len(part) <= SPLIT_THRESHOLD:
            current += part
        else:
            if current:
                chunks.append(current.strip())
            # If single part is oversized, sub-split it
            if len(part) > SPLIT_THRESHOLD:
                chunks.extend(split_message(part, max_len=SPLIT_THRESHOLD))
                current = ""
            else:
                current = part

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else split_message(full_text, max_len=SPLIT_THRESHOLD)


def split_message(text: str, max_len: int = SPLIT_THRESHOLD) -> list[str]:
    """
    Split *text* into a list of strings each no longer than *max_len* chars.

    The split prefers line boundaries (\\n) over hard character cuts so that
    the resulting chunks remain human-readable.

    Args:
        text:    The full text to split.
        max_len: Maximum length of each chunk (default: SPLIT_THRESHOLD).

    Returns:
        List of string chunks.  Guaranteed non-empty when text is non-empty.
    """
    if not text:
        return []

    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    lines = text.splitlines(keepends=True)
    current = ""

    for line in lines:
        # Single line itself exceeds limit — hard-split it
        if len(line) > max_len:
            # Flush current buffer first
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            # Hard-split the oversized line
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            current = line
            continue

        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current.strip():
        chunks.append(current.rstrip("\n"))

    return chunks
