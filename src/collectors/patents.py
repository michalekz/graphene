"""
Patent filings collector for graphene-intel.

Monitors recent graphene-related patent filings via the PatentsView PatentSearch API
(https://search.patentsview.org/api/v1/patents — POST, requires X-Api-Key).

If PATENTSVIEW_API_KEY is not set, the collector skips gracefully.
Register for a free key at: https://search.patentsview.org/
NOTE: As of early 2026, new key registrations are temporarily suspended.

Relevance scoring:
  - 9: Patent assigned to HydroGraph or Black Swan Graphene
  - 7: Patent assigned to a tracked competitor company
  - 4: General graphene patent (no watched assignee match)

Run frequency: weekly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from src.db.store import Headline, Store

logger = logging.getLogger(__name__)

# ── PatentsView PatentSearch API configuration ────────────────────────────────

# New PatentSearch API (March 2024+) — POST, requires X-Api-Key header
PATENTSVIEW_BASE = "https://search.patentsview.org/api/v1/patents"

# Fields to retrieve per patent
PATENT_FIELDS = [
    "patent_id",
    "patent_title",
    "patent_date",
    "patent_abstract",
    "assignee",
]

PATENTS_PER_PAGE = 100
LOOKBACK_DAYS = 90

# ── Company name patterns for relevance scoring ───────────────────────────────

# Primary companies → relevance 9
PRIMARY_ASSIGNEE_PATTERNS: list[tuple[str, str]] = [
    ("hydrograph", "HGRAF"),
    ("hydro graph", "HGRAF"),
    ("black swan graphene", "BSWGF"),
    ("black swan", "BSWGF"),
]

# Competitor companies → relevance 7
COMPETITOR_ASSIGNEE_PATTERNS: list[tuple[str, str]] = [
    ("nanoxplore", "NNXPF"),
    ("nanoexplore", "NNXPF"),
    ("graphene manufacturing", "GMGMF"),
    ("zentek", "ZTEK"),
    ("argo graphene", "ARLSF"),
    ("first graphene", "FGPHF"),
    ("directa plus", "DTPKF"),
    ("cvd equipment", "CVV"),
    ("thomas swan", "BSWGF"),   # Thomas Swan distributes Black Swan's graphene
]

# Single combined query covering title + abstract (new API supports _or)
# Using _text_all (all words must appear) for precision
PATENT_QUERIES: list[dict[str, Any]] = [
    {
        "label": "graphene (title or abstract)",
        "q": {
            "_or": [
                {"_text_all": {"patent_title": "graphene"}},
                {"_text_all": {"patent_abstract": "graphene"}},
            ]
        },
    },
    {
        "label": "detonation synthesis (HydroGraph process)",
        "q": {"_text_all": {"patent_title": "detonation synthesis"}},
    },
]

# Minimum relevance score to generate a Headline
HEADLINE_MIN_RELEVANCE = 7


# ── Relevance scoring ─────────────────────────────────────────────────────────

def _score_patent(
    assignee: str,
    title: str,
    abstract: str,
) -> tuple[int, list[str], list[str]]:
    """
    Compute a relevance score for a patent based on assignee and content.

    Returns:
        (relevance_score, matched_tickers, matched_keywords)
    """
    assignee_lower = assignee.lower()
    title_lower = title.lower()
    abstract_lower = abstract.lower()
    content = title_lower + " " + abstract_lower

    tickers: list[str] = []
    keywords: list[str] = []

    # Check primary companies first (highest score)
    for pattern, ticker in PRIMARY_ASSIGNEE_PATTERNS:
        if pattern in assignee_lower:
            return 9, [ticker], [pattern]

    # Check competitor companies
    for pattern, ticker in COMPETITOR_ASSIGNEE_PATTERNS:
        if pattern in assignee_lower:
            tickers.append(ticker)
            keywords.append(pattern)

    if tickers:
        return 7, tickers, keywords

    # General graphene keyword matching
    graphene_terms = [
        "graphene", "few-layer graphene", "single-layer graphene",
        "graphene oxide", "reduced graphene", "graphene nanoplatelet",
        "detonation graphene", "graphene synthesis", "graphene production",
    ]
    for term in graphene_terms:
        if term in content:
            keywords.append(term)

    if keywords:
        return 4, [], keywords

    # Fallback — shouldn't normally reach here given our search queries
    return 1, [], []


# ── PatentsView API fetcher ───────────────────────────────────────────────────

async def _fetch_patents_for_query(
    query: dict[str, Any],
    since_date: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """
    Fetch patents from PatentsView PatentSearch API (POST) matching the given query.

    Uses the new v2 API: POST to /api/v1/patents with JSON body and X-Api-Key header.
    Returns a list of raw patent dicts from the API response.
    """
    body: dict[str, Any] = {
        "q": {
            "_and": [
                query["q"],
                {"_gte": {"patent_date": since_date}},
            ]
        },
        "f": PATENT_FIELDS,
        "s": [{"patent_date": "desc"}],
        "o": {"size": PATENTS_PER_PAGE},
    }

    logger.info(
        "[patents] Fetching PatentSearch query '%s' since %s",
        query["label"],
        since_date,
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                PATENTSVIEW_BASE,
                json=body,
                headers={
                    "X-Api-Key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error(
            "[patents] PatentSearch API request failed for '%s': %s",
            query["label"],
            exc,
        )
        return []

    patents: list[dict[str, Any]] = data.get("patents", []) or []
    total = data.get("total_hits", len(patents))
    logger.info(
        "[patents] Query '%s': %d patents returned (total matching: %s)",
        query["label"],
        len(patents),
        total,
    )
    return patents


def _extract_assignee(patent: dict[str, Any]) -> str:
    """
    Extract the primary assignee organization name from a patent record.

    New PatentSearch API returns 'assignee' as a dict (or list); we handle both.
    """
    raw = patent.get("assignee")
    if not raw:
        return ""
    # New API: dict with assignee_organization key
    if isinstance(raw, dict):
        return raw.get("assignee_organization", "") or ""
    # Fallback: list of dicts (legacy format)
    if isinstance(raw, list) and raw:
        return raw[0].get("assignee_organization", "") or ""
    return ""


def _patent_url(patent_id: str) -> str:
    """Construct the PatentsView detail page URL for a patent."""
    return f"https://search.patentsview.org/patent/{patent_id}"


# ── DB insert helper ──────────────────────────────────────────────────────────

async def _insert_patent(
    store: Store,
    patent_id: str,
    title: str,
    assignee: str,
    patent_date: str,
    abstract: str,
    relevance_score: int,
    keywords_matched: list[str],
) -> bool:
    """
    Insert a patent record into the patent_filings table.

    Uses INSERT OR IGNORE semantics via the UNIQUE constraint on patent_id.
    Returns True if the record was newly inserted, False if it was a duplicate.
    """
    try:
        async with store._db.execute(
            """
            INSERT OR IGNORE INTO patent_filings
                (patent_id, title, assignee, publication_date, abstract,
                 url, relevance_score, keywords_matched)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patent_id,
                title,
                assignee,
                patent_date,
                abstract,
                _patent_url(patent_id),
                relevance_score,
                json.dumps(keywords_matched),
            ),
        ) as cur:
            inserted = cur.rowcount > 0
        await store._db.commit()
        return inserted
    except Exception as exc:
        logger.error(
            "[patents] DB insert failed for patent_id=%s: %s", patent_id, exc
        )
        return False


# ── Headline generation ───────────────────────────────────────────────────────

def _patent_to_headline(
    patent_id: str,
    title: str,
    assignee: str,
    patent_date: str,
    abstract: str,
    relevance_score: int,
    matched_tickers: list[str],
    keywords_matched: list[str],
) -> Headline:
    """Build a Headline for a high-relevance patent filing."""
    assignee_str = f" by {assignee}" if assignee else ""
    relevance_label = (
        "PRIMARY COMPANY" if relevance_score >= 9
        else "COMPETITOR" if relevance_score >= 7
        else "SECTOR"
    )
    headline_title = (
        f"New Graphene Patent [{relevance_label}]{assignee_str}: {title}"
    )
    raw = (
        f"Patent filing detected with relevance score {relevance_score}/10.\n"
        f"Title: {title}\n"
        f"Assignee: {assignee or 'Unknown'}\n"
        f"Publication date: {patent_date}\n"
        f"Relevance: {relevance_label}\n"
        f"Matched tickers: {', '.join(matched_tickers) if matched_tickers else 'sector'}\n"
        f"Keywords matched: {', '.join(keywords_matched)}\n\n"
        f"Abstract:\n{abstract[:800] if abstract else 'N/A'}"
    )
    pub_dt: Optional[datetime] = None
    try:
        pub_dt = datetime.strptime(patent_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        pass

    return Headline(
        url=_patent_url(patent_id),
        title=headline_title,
        source="patentsview",
        published_at=pub_dt,
        tickers=matched_tickers or ["HGRAF", "BSWGF"],
        category="research",
        raw_content=raw,
    )


# ── Public collector interface ────────────────────────────────────────────────

async def collect_patents(
    store: Store,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[Headline]:
    """
    Collect recent graphene patents from the PatentsView API.

    Executes multiple search queries (title phrase, abstract phrase, process
    terms), deduplicates by patent_id, scores each patent for relevance to
    watched companies, persists all results to patent_filings, and returns
    Headline objects for high-relevance patents (relevance >= 7) for AI
    evaluation.

    Args:
        store: An open Store instance.
        lookback_days: How many days back to look for newly published patents.

    Returns:
        List of high-relevance patent headlines for AI evaluation.
    """
    api_key = os.getenv("PATENTSVIEW_API_KEY", "")
    if not api_key:
        logger.info(
            "[patents] PATENTSVIEW_API_KEY not set — skipping patent collection. "
            "Register at https://search.patentsview.org/ to enable."
        )
        return []

    headlines: list[Headline] = []
    seen_patent_ids: set[str] = set()
    since_date = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    total_fetched = 0
    total_inserted = 0

    for query in PATENT_QUERIES:
        raw_patents = await _fetch_patents_for_query(query, since_date, api_key)

        for patent in raw_patents:
            patent_id: str = patent.get("patent_id", "")
            if not patent_id or patent_id in seen_patent_ids:
                continue
            seen_patent_ids.add(patent_id)

            title: str = patent.get("patent_title", "") or ""
            patent_date: str = patent.get("patent_date", "") or ""
            abstract: str = patent.get("patent_abstract", "") or ""
            assignee: str = _extract_assignee(patent)

            if not title:
                logger.debug("[patents] Skipping patent with no title: id=%s", patent_id)
                continue

            total_fetched += 1

            # ── Score for relevance ───────────────────────────────────────────
            relevance_score, matched_tickers, keywords_matched = _score_patent(
                assignee=assignee,
                title=title,
                abstract=abstract,
            )

            logger.debug(
                "[patents] Patent %s ('%s', assignee='%s') → relevance=%d",
                patent_id,
                title[:60],
                assignee[:40],
                relevance_score,
            )

            # ── Persist to DB ─────────────────────────────────────────────────
            was_inserted = await _insert_patent(
                store=store,
                patent_id=patent_id,
                title=title,
                assignee=assignee,
                patent_date=patent_date,
                abstract=abstract,
                relevance_score=relevance_score,
                keywords_matched=keywords_matched,
            )

            if was_inserted:
                total_inserted += 1
                log_level = logging.INFO if relevance_score >= 7 else logging.DEBUG
                logger.log(
                    log_level,
                    "[patents] Inserted patent id=%s, relevance=%d, assignee='%s'",
                    patent_id,
                    relevance_score,
                    assignee or "unknown",
                )

            # ── Generate headline for high-relevance patents ──────────────────
            if relevance_score >= HEADLINE_MIN_RELEVANCE:
                headline = _patent_to_headline(
                    patent_id=patent_id,
                    title=title,
                    assignee=assignee,
                    patent_date=patent_date,
                    abstract=abstract,
                    relevance_score=relevance_score,
                    matched_tickers=matched_tickers,
                    keywords_matched=keywords_matched,
                )
                try:
                    row_id = await store.insert_headline(headline)
                    if row_id is not None:
                        headlines.append(headline)
                        logger.info(
                            "[patents] Inserted high-relevance headline id=%d "
                            "for patent '%s' (score=%d)",
                            row_id,
                            title[:60],
                            relevance_score,
                        )
                    else:
                        logger.debug(
                            "[patents] Headline already exists for patent %s",
                            patent_id,
                        )
                except Exception as exc:
                    logger.error(
                        "[patents] Failed to insert headline for patent %s: %s",
                        patent_id,
                        exc,
                    )

    logger.info(
        "[patents] Done. Fetched %d unique patents, %d newly inserted, "
        "%d high-relevance headlines generated.",
        total_fetched,
        total_inserted,
        len(headlines),
    )
    return headlines
