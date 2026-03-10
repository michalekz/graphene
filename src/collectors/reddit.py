"""
Reddit sentiment collector for Graphene Intel.

Uses PRAW (Python Reddit API Wrapper) to search targeted subreddits for posts
and comments mentioning our primary tickers or related keywords.  For each
match we:

  1. Detect which tickers are mentioned in the post/comment text.
  2. Compute a simple sentiment score from upvotes/downvotes and keyword
     heuristics on the title/body.
  3. Aggregate per-ticker SentimentScore objects.
  4. Emit Headline objects (category="social") for high-upvote posts (>10)
     that mention our tickers.

PRAW is a synchronous library — all calls are wrapped in asyncio.to_thread().

Credentials are read from environment variables:
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT

If any credential is missing the collector logs an info message and returns
empty lists without raising.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import yaml

from src.db.store import Headline, SentimentScore, Store

logger = logging.getLogger(__name__)

_SOURCES_PATH = "/opt/grafene/config/sources.yaml"

# ── Keyword / sentiment heuristics ────────────────────────────────────────────

_BULLISH_WORDS = frozenset(
    [
        "buy", "bull", "bullish", "moon", "rocket", "long", "upside", "breakout",
        "undervalued", "opportunity", "surge", "rally", "accumulate", "growth",
        "potential", "promising", "positive", "strong", "beat", "record",
    ]
)

_BEARISH_WORDS = frozenset(
    [
        "sell", "bear", "bearish", "short", "dump", "overvalued", "crash",
        "avoid", "risk", "warning", "red flag", "dilution", "concern",
        "negative", "weak", "miss", "decline", "fall", "drop",
    ]
)


# ── Configuration loaders ─────────────────────────────────────────────────────

def _load_reddit_config() -> dict[str, Any]:
    """Return the reddit subsection from sources.yaml."""
    try:
        with open(_SOURCES_PATH) as fh:
            cfg: dict[str, Any] = yaml.safe_load(fh)
        for source in cfg.get("sentiment", []):
            if source.get("name") == "reddit":
                return source
    except Exception as exc:
        logger.warning("Could not read sources.yaml: %s", exc)
    return {}


def _primary_tickers() -> list[str]:
    """Return OTC tickers for our primary companies."""
    return ["HGRAF", "BSWGF"]


# ── Text analysis helpers ──────────────────────────────────────────────────────

def _extract_tickers(text: str, known_tickers: list[str]) -> list[str]:
    """Return list of known tickers mentioned in *text*."""
    upper = text.upper()
    found = []
    for ticker in known_tickers:
        # Match whole-word occurrences to avoid false positives
        if re.search(r"\b" + re.escape(ticker) + r"\b", upper):
            found.append(ticker)
    return found


def _keyword_sentiment(text: str) -> float:
    """Return a naive keyword-based sentiment score in [-1.0, +1.0].

    Counts bullish and bearish word hits (case-insensitive) and normalises by
    the total hit count.  Returns 0.0 when no sentiment words are found.
    """
    lower = text.lower()
    words = re.findall(r"\b\w+\b", lower)
    bull = sum(1 for w in words if w in _BULLISH_WORDS)
    bear = sum(1 for w in words if w in _BEARISH_WORDS)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


def _post_sentiment(score: int, ratio: float, text: str) -> float:
    """Blend upvote signal with keyword signal into a single [-1.0, +1.0] score.

    *score*  — Reddit post score (upvotes minus downvotes)
    *ratio*  — upvote_ratio (0.0–1.0)
    *text*   — concatenated title + body for keyword analysis
    """
    # Map upvote_ratio to [-1, +1]: ratio 0.5 → 0.0, ratio 1.0 → 1.0
    vote_signal = (ratio - 0.5) * 2.0

    kw_signal = _keyword_sentiment(text)

    # Weight: 60% keyword-based (more reliable for micro-cap), 40% vote signal
    blended = 0.6 * kw_signal + 0.4 * vote_signal
    # Clamp
    return max(-1.0, min(1.0, blended))


# ── PRAW synchronous operations (all run inside to_thread) ────────────────────

def _create_reddit_client() -> Any | None:
    """Create and return a PRAW Reddit read-only client, or None if credentials missing."""
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    user_agent = os.getenv(
        "REDDIT_USER_AGENT",
        "GrapheneIntel/0.1 (research-bot; contact: zdenek.michalek@gmail.com)",
    ).strip()

    if not client_id or not client_secret:
        logger.info(
            "Reddit collector: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set — skipping"
        )
        return None

    try:
        import praw  # type: ignore[import]
    except ImportError:
        logger.info("Reddit collector: praw library not installed — skipping")
        return None

    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        read_only=True,
    )


def _search_subreddits_sync(
    reddit: Any,
    subreddits: list[str],
    keywords: list[str],
    primary_tickers: list[str],
    limit: int = 100,
) -> tuple[dict[str, list[float]], dict[str, list[dict[str, Any]]]]:
    """Search *subreddits* for *keywords* and collect sentiment signals.

    This is a pure synchronous function — call via asyncio.to_thread().

    Returns:
        ticker_scores   — {ticker: [score1, score2, ...]} for aggregation
        high_upvote_posts — {ticker: [post_dict, ...]} for Headline creation
    """
    ticker_scores: dict[str, list[float]] = defaultdict(list)
    high_upvote_posts: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for subreddit_name in subreddits:
        try:
            subreddit = reddit.subreddit(subreddit_name)
        except Exception as exc:
            logger.warning("Cannot access r/%s: %s", subreddit_name, exc)
            continue

        for keyword in keywords:
            try:
                posts = list(subreddit.search(keyword, limit=limit, sort="new"))
            except Exception as exc:
                logger.warning(
                    "Search failed in r/%s for '%s': %s", subreddit_name, keyword, exc
                )
                continue

            for post in posts:
                try:
                    title: str = getattr(post, "title", "") or ""
                    body: str = getattr(post, "selftext", "") or ""
                    full_text = f"{title} {body}"

                    mentioned = _extract_tickers(full_text, primary_tickers)
                    if not mentioned:
                        # Only record posts that clearly mention our tickers
                        continue

                    post_score: int = getattr(post, "score", 0) or 0
                    upvote_ratio: float = getattr(post, "upvote_ratio", 0.5) or 0.5
                    url: str = f"https://www.reddit.com{getattr(post, 'permalink', '')}"
                    created_utc: float = getattr(post, "created_utc", 0.0)
                    published_at = datetime.fromtimestamp(created_utc, tz=timezone.utc)

                    sentiment = _post_sentiment(post_score, upvote_ratio, full_text)

                    for ticker in mentioned:
                        ticker_scores[ticker].append(sentiment)

                        if post_score > 10:
                            high_upvote_posts[ticker].append(
                                {
                                    "url": url,
                                    "title": title,
                                    "score": post_score,
                                    "sentiment": sentiment,
                                    "published_at": published_at,
                                    "tickers": mentioned,
                                    "subreddit": subreddit_name,
                                }
                            )

                    # Also check top-level comments for the most discussed posts
                    if post_score > 5:
                        try:
                            post.comments.replace_more(limit=0)
                            for comment in post.comments.list()[:20]:
                                comment_body: str = getattr(comment, "body", "") or ""
                                if not _extract_tickers(comment_body, primary_tickers):
                                    continue
                                c_score: int = getattr(comment, "score", 0) or 0
                                c_sentiment = _keyword_sentiment(comment_body)
                                for ticker in mentioned:
                                    ticker_scores[ticker].append(c_sentiment)
                                    _ = c_score  # available for future use
                        except Exception as exc:
                            logger.debug("Error reading comments for post %s: %s", url, exc)

                except Exception as exc:
                    logger.debug("Error processing post: %s", exc)

    return dict(ticker_scores), dict(high_upvote_posts)


# ── Public async entry point ───────────────────────────────────────────────────

async def collect_reddit_sentiment(
    store: Store,
) -> tuple[list[SentimentScore], list[Headline]]:
    """Collect Reddit sentiment and high-upvote social headlines.

    Searches configured subreddits for keywords related to HGRAF and BSWGF,
    computes per-ticker aggregate sentiment scores and extracts notable posts
    (score > 10) as Headline objects with category='social'.

    All PRAW calls run in a background thread via asyncio.to_thread().
    Missing credentials result in an immediate empty return without error.

    Args:
        store: Open Store instance used to persist scores and headlines.

    Returns:
        Tuple of (list[SentimentScore], list[Headline]).
        Both lists may be empty on credential absence or total failure.
    """
    sentiment_results: list[SentimentScore] = []
    headline_results: list[Headline] = []

    # ── Build Reddit client (sync, cheap) ─────────────────────────────────
    try:
        reddit = await asyncio.to_thread(_create_reddit_client)
    except Exception as exc:
        logger.error("Failed to create Reddit client: %s", exc)
        return sentiment_results, headline_results

    if reddit is None:
        return sentiment_results, headline_results

    # ── Load config ────────────────────────────────────────────────────────
    try:
        cfg = _load_reddit_config()
        subreddits: list[str] = cfg.get(
            "subreddits", ["pennystocks", "smallcaps", "graphene", "nanotechnology"]
        )
        keywords: list[str] = cfg.get(
            "keywords", ["HGRAF", "BSWGF", "HydroGraph", "Black Swan Graphene", "graphene"]
        )
    except Exception as exc:
        logger.error("Reddit collector: config load error: %s", exc)
        return sentiment_results, headline_results

    primary_tickers = _primary_tickers()

    # ── Run PRAW search in thread ──────────────────────────────────────────
    try:
        ticker_scores, high_upvote_posts = await asyncio.to_thread(
            _search_subreddits_sync,
            reddit,
            subreddits,
            keywords,
            primary_tickers,
        )
    except Exception as exc:
        logger.error("Reddit search failed: %s", exc)
        return sentiment_results, headline_results

    # ── Build and persist SentimentScore objects ───────────────────────────
    for ticker, scores in ticker_scores.items():
        if not scores:
            continue

        avg_score = sum(scores) / len(scores)
        # Clamp to [-1.0, +1.0] for safety
        avg_score = max(-1.0, min(1.0, avg_score))

        score_obj = SentimentScore(
            ticker=ticker,
            source="reddit",
            score=avg_score,
            volume=len(scores),
            raw_data={
                "mention_count": len(scores),
                "avg_score": avg_score,
                "subreddits": subreddits,
                "keywords_searched": keywords,
            },
        )

        try:
            await store.insert_sentiment(score_obj)
        except Exception as exc:
            logger.error("Failed to persist Reddit sentiment for %s: %s", ticker, exc)

        sentiment_results.append(score_obj)
        logger.info(
            "Reddit %s: %d mentions → avg sentiment=%.3f",
            ticker,
            len(scores),
            avg_score,
        )

    # ── Build and persist Headline objects ────────────────────────────────
    seen_urls: set[str] = set()

    for ticker, posts in high_upvote_posts.items():
        for post in posts:
            url: str = post["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            headline = Headline(
                url=url,
                title=post["title"],
                source=f"reddit/r/{post['subreddit']}",
                published_at=post["published_at"],
                tickers=post["tickers"],
                category="social",
                raw_content=None,
            )

            try:
                row_id = await store.insert_headline(headline)
                if row_id is not None:
                    logger.info(
                        "Reddit headline stored (id=%d): %s", row_id, headline.title[:60]
                    )
            except Exception as exc:
                logger.error("Failed to store Reddit headline '%s': %s", headline.title[:60], exc)

            headline_results.append(headline)

    logger.info(
        "Reddit collector complete: %d sentiment scores, %d headlines",
        len(sentiment_results),
        len(headline_results),
    )
    return sentiment_results, headline_results
