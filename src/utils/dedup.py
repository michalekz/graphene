"""
Headline deduplication utilities for Graphene Intel.

Two-stage approach:
  1. Fast Jaccard similarity on normalised title tokens — catches obvious
     same-story duplicates from different sources (Google News + TickerTick
     + graphene-info all publishing the same press release).
  2. Optional LLM clustering via Claude Haiku for harder cases where simple
     word overlap fails (paraphrased titles, different angles of same event).

Thresholds (tunable via DEDUP_JACCARD_THRESHOLD env var, default 0.55):
  ≥ threshold → skip alert (duplicate)
  < threshold → send alert (unique enough)
"""

from __future__ import annotations

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
