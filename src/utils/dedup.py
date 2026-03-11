"""
Headline deduplication utilities for Graphene Intel.

Two-stage approach:
  1. Fast Jaccard similarity on normalised title tokens — catches obvious
     same-story duplicates from different sources (Google News + TickerTick
     + graphene-info all publishing the same press release).
  2. LLM clustering via Groq — for borderline cases (0.25–0.55 Jaccard) where
     two headlines might describe the same event with different wording.
     A single batch call clusters N recent alert titles, cost ≈ $0.001.

Environment:
  DEDUP_JACCARD_THRESHOLD   — hard skip threshold (default 0.55)
  DEDUP_LLM_SOFT_THRESHOLD  — below this, ask LLM (default 0.25)
  DEDUP_USE_LLM             — "true" (default) / "false" to disable LLM stage
"""

from __future__ import annotations

import json
import os
import re
import logging
from typing import Sequence

logger = logging.getLogger(__name__)

# Tune this if you get too many false-positives or false-negatives
_JACCARD_THRESHOLD: float = float(os.getenv("DEDUP_JACCARD_THRESHOLD", "0.55"))

# English stopwords + common financial filler words to ignore
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "has", "have", "had", "will", "would", "could", "should", "may", "might",
        "its", "it", "this", "that", "these", "those", "as", "up", "out",
        "new", "now", "more", "also", "after", "over", "into", "about",
        # financial noise
        "inc", "corp", "ltd", "llc", "plc", "co", "group", "holdings",
        "stock", "shares", "market", "trading", "price",
    }
)


def _normalise(title: str) -> frozenset[str]:
    """Lowercase, strip punctuation, remove stopwords → frozenset of tokens."""
    text = title.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = {t for t in text.split() if t and t not in _STOPWORDS and len(t) > 2}
    return frozenset(tokens)


def jaccard(a: str, b: str) -> float:
    """Jaccard similarity between two headline titles (0.0 – 1.0)."""
    sa = _normalise(a)
    sb = _normalise(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_duplicate(title: str, recent_titles: Sequence[str], threshold: float | None = None) -> bool:
    """
    Return True if *title* is a duplicate of any title in *recent_titles*.

    Args:
        title:         The candidate headline title.
        recent_titles: Titles of headlines that were already alerted recently.
        threshold:     Override the default Jaccard threshold.

    Returns:
        True  → skip this headline (duplicate)
        False → send alert (sufficiently unique)
    """
    thr = threshold if threshold is not None else _JACCARD_THRESHOLD
    for other in recent_titles:
        sim = jaccard(title, other)
        if sim >= thr:
            logger.debug(
                "Duplicate detected (jaccard=%.2f >= %.2f): %r ≈ %r",
                sim,
                thr,
                title[:60],
                other[:60],
            )
            return True
    return False


_LLM_SOFT_THRESHOLD: float = float(os.getenv("DEDUP_LLM_SOFT_THRESHOLD", "0.25"))
_USE_LLM: bool = os.getenv("DEDUP_USE_LLM", "true").lower() != "false"


def _llm_is_same_story(candidate: str, recent_titles: Sequence[str]) -> bool:
    """Ask Groq whether *candidate* covers the same story as any of *recent_titles*.

    Called only when Jaccard is in the soft zone (0.25–0.55) — i.e. the titles
    share some words but not enough to be certain.  One API call per candidate,
    very cheap on Groq.

    Returns True if LLM says it's a duplicate, False otherwise.
    Falls back to False (send the alert) on any API error.
    """
    if not recent_titles:
        return False
    try:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            return False
        client = Groq(api_key=api_key)

        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(recent_titles[:15]))
        system = (
            "You are a financial news deduplication assistant. "
            "Respond ONLY with valid JSON."
        )
        user = (
            f"NEW HEADLINE:\n{candidate}\n\n"
            f"RECENTLY ALERTED HEADLINES:\n{numbered}\n\n"
            "Does the NEW HEADLINE report on the same underlying event or story "
            "as ANY of the recently alerted headlines?\n"
            "Same story = same company announcement, same deal, same data release "
            "(even if worded differently or from a different source).\n"
            "Different story = different event, different data point, different angle.\n\n"
            'Respond ONLY with: {"is_duplicate": true} or {"is_duplicate": false}'
        )
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",   # fastest/cheapest for binary decision
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=20,
            temperature=0.0,
        )
        text = resp.choices[0].message.content.strip()
        data = json.loads(text)
        result = bool(data.get("is_duplicate", False))
        if result:
            logger.info("LLM dedup: duplicate detected for %r", candidate[:60])
        return result
    except Exception as exc:
        logger.debug("LLM dedup API call failed (falling back to send): %s", exc)
        return False


def is_duplicate(title: str, recent_titles: Sequence[str], threshold: float | None = None) -> bool:
    """
    Two-stage duplicate detection:

    Stage 1 — Jaccard:
      ≥ hard threshold (0.55) → immediate duplicate, no LLM call needed
      < soft threshold (0.25) → clearly different story, skip LLM

    Stage 2 — LLM (Groq llama-3.1-8b-instant, ~$0.0001/call):
      In the soft zone (0.25–0.55): ask LLM if it's the same underlying event.

    Returns True → skip alert (duplicate)
    Returns False → send alert (unique)
    """
    thr = threshold if threshold is not None else _JACCARD_THRESHOLD

    max_sim = 0.0
    for other in recent_titles:
        sim = jaccard(title, other)
        if sim >= thr:
            logger.debug(
                "Jaccard duplicate (%.2f >= %.2f): %r ≈ %r",
                sim, thr, title[:60], other[:60],
            )
            return True
        if sim > max_sim:
            max_sim = sim

    # Soft zone: ask LLM
    if _USE_LLM and max_sim >= _LLM_SOFT_THRESHOLD:
        return _llm_is_same_story(title, recent_titles)

    return False


def cluster_by_similarity(
    titles: list[str],
    threshold: float | None = None,
) -> list[int]:
    """
    Greedy single-linkage clustering of headline titles.

    Returns a list of cluster IDs (same length as *titles*).
    Headlines in the same cluster are considered the same story.
    Cluster 0 is assigned to the first headline, etc.

    Usage in scorer: only score/alert the first headline per cluster
    (index == min(indices_in_cluster)).
    """
    thr = threshold if threshold is not None else _JACCARD_THRESHOLD
    cluster_ids: list[int] = []
    canonical_titles: list[str] = []  # one per cluster

    for title in titles:
        assigned = -1
        for cid, ctitle in enumerate(canonical_titles):
            if jaccard(title, ctitle) >= thr:
                assigned = cid
                break
        if assigned == -1:
            assigned = len(canonical_titles)
            canonical_titles.append(title)
        cluster_ids.append(assigned)

    return cluster_ids
