"""
Patent filings collector for graphene-intel.

Monitors recent graphene-related patent filings via the PatentsView API
(https://search.patentsview.org). Scores patents by relevance to watched companies,
persists them to the patent_filings table, and surfaces high-relevance patents
as Headline objects for AI evaluation.

Relevance scoring:
  - 9: Patent assigned to HydroGraph or Black Swan Graphene
  - 7: Patent assigned to a tracked competitor company
  - 4: General graphene patent (no watched assignee match)

Run frequency: weekly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from src.db.store import Headline, Store
from src.utils.http import fetch_json

logger = logging.getLogger(__name__)

# ── PatentsView API configuration ─────────────────────────────────────────────

PATENTSVIEW_BASE = "https://search.patentsview.org/api/v1/patent/"

# Fields to retrieve per patent
PATENT_FIELDS = [
    "patent_id",
    "patent_title",
    "patent_date",
    "patent_abstract",
    "assignees.assignee_organization",
]

PATENTS_PER_PAGE = 25
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

# Search queries — ordered by expected relevance
PATENT_QUERIES: list[dict[str, Any]] = [
    # Exact phrase in title: high precision
    {
        "label": "title:graphene",
        "q": {"_text_phrase": {"patent_title": "graphene"}},
    },
    # Abstract mention: broader coverage
    {
        "label": "abstract:graphene",
        "q": {"_text_phrase": {"patent_abstract": "graphene"}},
    },
    # Detonation synthesis — unique to HydroGraph's process
    {
        "label": "title:detonation synthesis",
        "q": {"_text_phrase": {"patent_title": "detonation synthesis"}},
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
) -> list[dict[str, Any]]:
    """
    Fetch patents from PatentsView API matching the given query.

    Adds a date filter to restrict results to patents from the last 90 days.
    Returns a list of raw patent dicts from the API response.

    PatentsView query format uses a nested JSON DSL; date ranges use _gte/_lte
    combined with _and to merge with the content query.
    """
    # Combine the content query with a date range filter
    combined_q = {
        "_and": [
            query["q"],
            {"_gte": {"patent_date": since_date}},
        ]
    }

    params: dict[str, Any] = {
        "q": json.dumps(combined_q),
        "f": json.dumps(PATENT_FIELDS),
        "s": json.dumps([{"patent_date": "desc"}]),
        "o": json.dumps({"per_page": PATENTS_PER_PAGE}),
    }

    logger.info(
        "[patents] Fetching PatentsView query '%s' since %s",
        query["label"],
        since_date,
    )

    try:
        data = await fetch_json(PATENTSVIEW_BASE, params=params)
    except Exception as exc:
        logger.error(
            "[patents] PatentsView API request failed for '%s': %s",
            query["label"],
            exc,
        )
        return []

    patents: list[dict[str, Any]] = data.get("patents", []) or []
    total = data.get("total_patent_count", len(patents))
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

    PatentsView nests assignees as a list; we take the first entry.
    """
    assignees: list[dict[str, Any]] = patent.get("assignees", []) or []
    if not assignees:
        return ""
    return assignees[0].get("assignee_organization", "") or ""


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
    headlines: list[Headline] = []
    seen_patent_ids: set[str] = set()
    since_date = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    total_fetched = 0
    total_inserted = 0

    for query in PATENT_QUERIES:
        raw_patents = await _fetch_patents_for_query(query, since_date)

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
