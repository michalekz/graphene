"""
Headline scorer supporting both Anthropic (Claude) and Groq backends.

Backend selection via environment variables:
    SCORER_BACKEND   = "anthropic" (default) | "groq"
    SCORER_MODEL     = model name override
                       Anthropic default: claude-haiku-4-5-20251001
                       Groq default:      llama-3.3-70b-versatile

Groq is significantly cheaper and faster for simple scoring tasks.
Claude Haiku gives marginally better JSON reliability.

For each unscored headline:
1. Build sector context (prices, sentiment, catalysts)
2. Call the LLM backend with the scoring prompt
3. Parse JSON response
4. Write EvaluationResult back to DB
5. Return high-score items for immediate alerting
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from datetime import datetime, timezone

from src.db.store import EvaluationResult, Store
from src.evaluator.context import build_full_context
from src.evaluator.prompts import (
    HEADLINE_SCORING_SYSTEM,
    HEADLINE_SCORING_USER,
)

logger = logging.getLogger(__name__)

# ── Backend configuration ────────────────────────────────────────────────────

_BACKEND = os.getenv("SCORER_BACKEND", "anthropic").lower()  # "anthropic" | "groq"

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "groq": "llama-3.3-70b-versatile",   # fast + cheap; alternatives: llama-3.1-8b-instant
}
_SCORER_MODEL = os.getenv("SCORER_MODEL", _DEFAULT_MODELS.get(_BACKEND, "claude-haiku-4-5-20251001"))

MAX_CONTENT_SNIPPET = 500   # chars — keep prompts cheap
BATCH_SIZE = 20             # headlines per evaluator run


# ── Client factories ─────────────────────────────────────────────────────────

def _get_anthropic_client():
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    return anthropic.Anthropic(api_key=api_key)


def _get_groq_client():
    from groq import Groq
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set in environment")
    return Groq(api_key=api_key)


def _call_llm(system: str, user: str) -> str:
    """Call the configured LLM backend. Returns raw text response."""
    if _BACKEND == "groq":
        client = _get_groq_client()
        response = client.chat.completions.create(
            model=_SCORER_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=512,
            temperature=0.1,
        )
        return response.choices[0].message.content or ""
    else:
        # Default: Anthropic
        import anthropic
        client = _get_anthropic_client()
        message = client.messages.create(
            model=_SCORER_MODEL,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _build_prompt(headline: dict, context: dict[str, str], today: str) -> str:
    snippet = (headline.get("raw_content") or "")[:MAX_CONTENT_SNIPPET]
    return HEADLINE_SCORING_USER.format(
        tickers_context=context["tickers_context"],
        sector_context=context["sector_context"],
        catalysts_context=context["catalysts_context"],
        today=today,
        title=headline["title"],
        source=headline["source"],
        published_at=headline.get("published_at") or "unknown",
        content_snippet=snippet or "(no content available)",
    )


def _parse_response(text: str) -> Optional[dict]:
    """Extract JSON from LLM response. Handles markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    logger.warning("Failed to parse LLM JSON response: %s", text[:200])
    return None


def _validate_result(data: dict, url_hash: str) -> Optional[EvaluationResult]:
    """Validate and construct EvaluationResult from parsed JSON."""
    try:
        score = int(data.get("score", 0))
        if not 1 <= score <= 10:
            logger.warning("Score out of range: %d for %s", score, url_hash)
            score = max(1, min(10, score))

        sentiment = data.get("sentiment", "neutral")
        if sentiment not in ("bullish", "bearish", "neutral"):
            sentiment = "neutral"

        return EvaluationResult(
            url_hash=url_hash,
            score=score,
            sentiment=sentiment,
            impact_summary=str(data.get("impact_summary", ""))[:200],
            affected_tickers=data.get("affected_tickers", []),
            is_red_flag=bool(data.get("is_red_flag", False)),
            is_pump_suspect=bool(data.get("is_pump_suspect", False)),
        )
    except (TypeError, ValueError) as e:
        logger.error("Validation error for %s: %s — data=%s", url_hash, e, data)
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

async def score_headlines(store: Store, batch_size: int = BATCH_SIZE) -> list[dict]:
    """
    Score unscored headlines from DB using the configured LLM backend.

    Returns list of high-score headlines (score >= ALERT_THRESHOLD) that should
    be sent as instant Telegram alerts.
    """
    headlines = await store.get_unscored_headlines(limit=batch_size)
    if not headlines:
        logger.info("No unscored headlines to evaluate")
        return []

    logger.info(
        "Scoring %d headlines via %s/%s",
        len(headlines),
        _BACKEND,
        _SCORER_MODEL,
    )

    # Build context once, share across all headlines in this batch
    try:
        context = await build_full_context(store)
    except Exception as e:
        logger.error("Failed to build sector context: %s", e)
        context = {
            "tickers_context": "Context unavailable",
            "sector_context": "Context unavailable",
            "catalysts_context": "Context unavailable",
        }

    alert_threshold = int(os.getenv("ALERT_THRESHOLD", "7"))
    high_score_items: list[dict] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for headline in headlines:
        url_hash = headline["url_hash"]
        try:
            prompt = _build_prompt(headline, context, today)
            response_text = _call_llm(HEADLINE_SCORING_SYSTEM, prompt)
            data = _parse_response(response_text)
            if not data:
                await store.update_evaluation(
                    EvaluationResult(
                        url_hash=url_hash,
                        score=1,
                        sentiment="neutral",
                        impact_summary="Parse error — manual review needed",
                        affected_tickers=[],
                    )
                )
                continue

            result = _validate_result(data, url_hash)
            if result:
                await store.update_evaluation(result)
                logger.info(
                    "Scored headline",
                    extra={
                        "url_hash": url_hash[:12],
                        "score": result.score,
                        "sentiment": result.sentiment,
                        "source": headline.get("source"),
                    },
                )
                if result.score >= alert_threshold:
                    headline["score"] = result.score
                    headline["sentiment"] = result.sentiment
                    headline["impact_summary"] = result.impact_summary
                    headline["is_red_flag"] = result.is_red_flag
                    headline["is_pump_suspect"] = result.is_pump_suspect
                    high_score_items.append(headline)

        except Exception as e:
            logger.error("LLM error scoring %s: %s", url_hash[:12], e, exc_info=True)

    logger.info(
        "Scoring complete: %d scored, %d high-priority",
        len(headlines),
        len(high_score_items),
    )
    return high_score_items
