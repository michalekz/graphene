"""
SEC EDGAR Form 4 (insider trading) collector for graphene-intel.

Monitors insider transactions for HGRAF (HydroGraph) and BSWGF (Black Swan Graphene)
via the SEC EDGAR full-text search and submissions API. Companies primarily listed in
Canada (SEDAR) that are not registered with SEC are handled gracefully.

Run frequency: daily (not every 30 minutes).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.db.store import Headline, InsiderTrade, Store
from src.utils.http import fetch_json, fetch_text

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

EDGAR_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
EDGAR_EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FILING_BASE = "https://www.sec.gov/Archives/edgar/data"
EDGAR_CIK_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_COMPANY_SEARCH = "https://www.sec.gov/cgi-bin/browse-edgar"

# Company search terms → ticker mapping
COMPANY_SEARCH_TARGETS: list[dict[str, str]] = [
    {
        "search_term": "HydroGraph",
        "ticker": "HGRAF",
        "alt_terms": ["Hydrograph Clean Power", "HydroGraph Clean Power"],
    },
    {
        "search_term": "Black Swan Graphene",
        "ticker": "BSWGF",
        "alt_terms": ["Black Swan Graphene Inc"],
    },
]

# Transaction type mapping from SEC codes
TRANSACTION_TYPE_MAP: dict[str, str] = {
    "P": "buy",
    "S": "sell",
    "A": "exercise",   # Award
    "D": "exercise",   # Disposition to issuer
    "F": "exercise",   # Tax withholding
    "G": "gift",
    "I": "buy",        # Discretionary transaction (treat as buy)
    "J": "exercise",   # Other acquisition
    "K": "exercise",   # Equity swap
    "L": "exercise",   # Small acquisition under rule 16a-6
    "M": "exercise",   # Option exercise
    "U": "sell",       # Disposition pursuant to contract
    "W": "exercise",   # Will / inheritance
    "X": "exercise",   # Option exercise (in-the-money)
    "Z": "exercise",   # Deposit/withdrawal from voting trust
}

# Minimum value threshold for generating a news headline
HEADLINE_THRESHOLD_USD = 10_000.0


# ── CIK lookup ────────────────────────────────────────────────────────────────

async def _lookup_cik(company_name: str) -> Optional[str]:
    """
    Search SEC EDGAR company search for a CIK number.

    Returns the CIK string (zero-padded to 10 digits) or None if not found.
    The SEC EDGAR company JSON endpoint is the canonical lookup path.
    """
    try:
        # Try the company_tickers JSON file first — fast, complete
        url = "https://www.sec.gov/files/company_tickers.json"
        data = await fetch_json(url)
        # data is {index: {cik_str, ticker, title}, ...}
        name_lower = company_name.lower()
        for entry in data.values():
            title: str = entry.get("title", "")
            if name_lower in title.lower():
                cik_raw = str(entry["cik_str"])
                return cik_raw.zfill(10)
        return None
    except Exception as exc:
        logger.warning("CIK lookup failed for '%s': %s", company_name, exc)
        return None


# ── Filing parser helpers ─────────────────────────────────────────────────────

def _parse_transaction_type(code: str) -> str:
    """Map a raw SEC transaction code to a normalized type string."""
    return TRANSACTION_TYPE_MAP.get(code.upper(), "exercise")


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce a value to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int, returning default on failure."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _build_filing_url(cik: str, accession: str) -> str:
    """Construct a direct URL to the Form 4 filing on EDGAR."""
    acc_clean = accession.replace("-", "")
    return f"{EDGAR_FILING_BASE}/{cik.lstrip('0')}/{acc_clean}/{accession}-index.htm"


# ── Form 4 fetch via CIK submissions ─────────────────────────────────────────

async def _fetch_form4_via_submissions(
    cik: str,
    ticker: str,
    lookback_days: int = 30,
) -> list[InsiderTrade]:
    """
    Fetch Form 4 filings from the EDGAR submissions JSON for a given CIK.

    The /submissions/CIK{cik}.json endpoint contains a complete filing history
    including form type, accession number, filing date, and a link to the
    primary document. We then fetch each Form 4 document to parse the XML.
    """
    trades: list[InsiderTrade] = []
    url = f"{EDGAR_SUBMISSIONS_BASE}/CIK{cik}.json"
    since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()

    try:
        submissions = await fetch_json(url)
    except Exception as exc:
        logger.error("Failed to fetch submissions for CIK %s: %s", cik, exc)
        return trades

    # filings is split into "recent" and "files" (paginated older)
    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        logger.info("No recent filings found for CIK %s", cik)
        return trades

    forms: list[str] = recent.get("form", [])
    accessions: list[str] = recent.get("accessionNumber", [])
    filing_dates: list[str] = recent.get("filingDate", [])
    primary_docs: list[str] = recent.get("primaryDocument", [])

    for i, form_type in enumerate(forms):
        if form_type != "4":
            continue

        filing_date_str = filing_dates[i] if i < len(filing_dates) else ""
        try:
            filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        if filing_date < since_date:
            # Filings are roughly chronological newest-first; once we're past
            # our window we can stop for the recent block
            break

        accession = accessions[i] if i < len(accessions) else ""
        if not accession:
            continue

        doc_name = primary_docs[i] if i < len(primary_docs) else ""
        filing_url = _build_filing_url(cik, accession)

        # Fetch the raw Form 4 XML document
        acc_clean = accession.replace("-", "")
        xml_url = (
            f"{EDGAR_FILING_BASE}/{cik.lstrip('0')}/{acc_clean}/{doc_name}"
        )
        parsed = await _parse_form4_xml(
            xml_url=xml_url,
            accession=accession,
            filing_url=filing_url,
            ticker=ticker,
        )
        trades.extend(parsed)

    return trades


async def _parse_form4_xml(
    xml_url: str,
    accession: str,
    filing_url: str,
    ticker: str,
) -> list[InsiderTrade]:
    """
    Download and parse a Form 4 XML file from EDGAR.

    Form 4 XML structure (simplified):
      <ownershipDocument>
        <reportingOwner>
          <reportingOwnerId><rptOwnerName>, <rptOwnerCik>
          <reportingOwnerRelationship><officerTitle>, <isDirector>, <isOfficer>
        <nonDerivativeTable>
          <nonDerivativeTransaction>
            <securityTitle>, <transactionDate>, <transactionAmounts>
              <transactionShares>, <transactionPricePerShare>,
              <transactionAcquiredDisposedCode>
        <derivativeTable> (stock options etc.)
    """
    trades: list[InsiderTrade] = []
    try:
        xml_text = await fetch_text(xml_url, timeout=20.0)
    except Exception as exc:
        logger.warning("Could not fetch Form 4 XML from %s: %s", xml_url, exc)
        return trades

    def _extract(tag: str, text: str) -> str:
        """Simple regex tag extractor — avoids xml.etree dependency for speed."""
        m = re.search(rf"<{tag}[^>]*>\s*(.*?)\s*</{tag}>", text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _extract_all(tag: str, text: str) -> list[str]:
        return re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)

    # ── Insider identity ──────────────────────────────────────────────────────
    owner_name = _extract("rptOwnerName", xml_text)
    officer_title = _extract("officerTitle", xml_text)
    is_director = _extract("isDirector", xml_text) == "1"
    is_officer = _extract("isOfficer", xml_text) == "1"

    if not officer_title:
        if is_director:
            officer_title = "Director"
        elif is_officer:
            officer_title = "Officer"
        else:
            officer_title = "Insider"

    # ── Non-derivative transactions (common stock purchases/sales) ────────────
    non_deriv_blocks = re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
        xml_text,
        re.DOTALL | re.IGNORECASE,
    )
    for block in non_deriv_blocks:
        trade = _parse_transaction_block(
            block=block,
            owner_name=owner_name,
            officer_title=officer_title,
            ticker=ticker,
            accession=accession,
            filing_url=filing_url,
            source="sec_form4",
        )
        if trade:
            trades.append(trade)

    # ── Derivative transactions (options, warrants) ───────────────────────────
    deriv_blocks = re.findall(
        r"<derivativeTransaction>(.*?)</derivativeTransaction>",
        xml_text,
        re.DOTALL | re.IGNORECASE,
    )
    for block in deriv_blocks:
        trade = _parse_transaction_block(
            block=block,
            owner_name=owner_name,
            officer_title=officer_title,
            ticker=ticker,
            accession=accession,
            filing_url=filing_url,
            source="sec_form4",
            is_derivative=True,
        )
        if trade:
            trades.append(trade)

    return trades


def _parse_transaction_block(
    block: str,
    owner_name: str,
    officer_title: str,
    ticker: str,
    accession: str,
    filing_url: str,
    source: str,
    is_derivative: bool = False,
) -> Optional[InsiderTrade]:
    """Parse a single non-derivative or derivative transaction XML block."""

    def _extract(tag: str) -> str:
        m = re.search(rf"<{tag}[^>]*>\s*(.*?)\s*</{tag}>", block, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    # Transaction date
    tx_date = _extract("transactionDate")
    if not tx_date:
        # Try <periodOfReport> as fallback
        tx_date_m = re.search(r"<value>\s*(\d{4}-\d{2}-\d{2})\s*</value>", block)
        tx_date = tx_date_m.group(1) if tx_date_m else ""
    # Normalize date to ISO format
    tx_date = tx_date[:10] if tx_date else ""

    # Transaction type code
    if is_derivative:
        tx_code = _extract("transactionAcquiredDisposedCode")
        if not tx_code:
            tx_code = _extract("transactionCode")
    else:
        tx_code = _extract("transactionAcquiredDisposedCode")
        if not tx_code:
            tx_code = _extract("transactionCode")
    # Sometimes it's nested inside <value> tags
    if len(tx_code) > 1:
        m = re.search(r"<value>\s*([A-Za-z])\s*</value>", tx_code)
        tx_code = m.group(1) if m else tx_code[0]

    tx_type = _parse_transaction_type(tx_code)

    # Shares
    shares_str = _extract("transactionShares")
    if not shares_str:
        shares_str = _extract("sharesOwnedFollowingTransaction")
    # Strip nested XML tags if any
    shares_str = re.sub(r"<[^>]+>", "", shares_str)
    shares = _safe_int(shares_str.replace(",", ""))
    if shares <= 0:
        return None

    # Price per share
    price_str = _extract("transactionPricePerShare")
    price_str = re.sub(r"<[^>]+>", "", price_str)
    price = _safe_float(price_str.replace(",", ""))

    # For options/warrants the exercise price may be separate from market value;
    # we record the exercise price if available, otherwise 0.
    if is_derivative and price == 0.0:
        exercise_price_str = _extract("exerciseDate")  # sometimes price is here
        price = _safe_float(exercise_price_str.replace(",", ""))

    value_usd = shares * price if price > 0 else None

    if not tx_date or not owner_name:
        return None

    # Build a unique accession per transaction when a single Form 4 may
    # have multiple transaction rows (use block content hash suffix).
    import hashlib
    block_hash = hashlib.sha256(block.encode()).hexdigest()[:8]
    unique_accession = f"{accession}_{block_hash}"

    return InsiderTrade(
        ticker=ticker,
        insider_name=owner_name,
        title=officer_title,
        transaction_type=tx_type,
        shares=shares,
        price=price,
        date=tx_date,
        source=source,
        filing_url=filing_url,
        filing_accession=unique_accession,
        value_usd=value_usd,
    )


# ── Full-text search fallback ─────────────────────────────────────────────────

async def _fetch_form4_via_fulltext(
    company_name: str,
    ticker: str,
    lookback_days: int = 30,
) -> list[InsiderTrade]:
    """
    Use SEC EDGAR full-text search (EFTS) to find Form 4 filings by company name.

    This is the fallback when the company's CIK cannot be resolved via the
    company_tickers.json file (e.g., company registered under a holding entity name).
    """
    trades: list[InsiderTrade] = []
    since_dt = (datetime.now(timezone.utc) - timedelta(days=lookback_days))
    start_dt = since_dt.strftime("%Y-%m-%d")

    try:
        data = await fetch_json(
            EDGAR_EFTS_BASE,
            params={
                "q": f'"{company_name}"',
                "dateRange": "custom",
                "startdt": start_dt,
                "forms": "4",
                "_source": "file_date,period_of_report,entity_name,file_num,accession_no",
            },
        )
    except Exception as exc:
        logger.error("EFTS full-text search failed for '%s': %s", company_name, exc)
        return trades

    hits = data.get("hits", {}).get("hits", [])
    logger.info(
        "EFTS search for '%s' returned %d Form 4 hits", company_name, len(hits)
    )

    for hit in hits:
        src = hit.get("_source", {})
        accession = src.get("accession_no", "").replace(":", "-")
        entity_name = src.get("entity_name", "")
        period = src.get("period_of_report", "")[:10]

        if not accession:
            continue

        # CIK embedded in accession: first 10 digits
        cik_from_acc = accession.split("-")[0].lstrip("0").zfill(10)
        filing_url = _build_filing_url(cik_from_acc, accession)
        # We don't have a specific XML doc name here; try the standard pattern
        acc_clean = accession.replace("-", "")
        # Attempt to get index page to find primary document
        index_url = (
            f"{EDGAR_FILING_BASE}/{cik_from_acc.lstrip('0')}"
            f"/{acc_clean}/{accession}-index.json"
        )
        try:
            index_data = await fetch_json(index_url)
            docs = index_data.get("documents", [])
            primary = next(
                (d["name"] for d in docs if d.get("type") == "4"), None
            )
            if not primary:
                primary = next(
                    (d["name"] for d in docs if d.get("name", "").endswith(".xml")),
                    None,
                )
            if primary:
                xml_url = (
                    f"{EDGAR_FILING_BASE}/{cik_from_acc.lstrip('0')}"
                    f"/{acc_clean}/{primary}"
                )
                parsed = await _parse_form4_xml(
                    xml_url=xml_url,
                    accession=accession,
                    filing_url=filing_url,
                    ticker=ticker,
                )
                trades.extend(parsed)
        except Exception as exc:
            logger.warning(
                "Could not parse Form 4 index for accession %s: %s", accession, exc
            )

    return trades


# ── Headline generation ───────────────────────────────────────────────────────

def _trade_to_headline(trade: InsiderTrade) -> Headline:
    """Convert a significant insider trade to a Headline for AI evaluation."""
    action = trade.transaction_type.upper()
    value_str = f"${trade.value_usd:,.0f}" if trade.value_usd else "unknown value"
    price_str = f"@ ${trade.price:.4f}" if trade.price else ""
    title = (
        f"INSIDER {action}: {trade.insider_name} ({trade.title}) "
        f"— {trade.shares:,} shares {price_str} ({value_str}) [{trade.ticker}]"
    )
    url = trade.filing_url or (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&company={trade.ticker}&type=4&dateb=&owner=include&count=20"
    )
    sentiment_hint = (
        "Insider BUY signal" if trade.transaction_type == "buy"
        else "Insider SELL signal" if trade.transaction_type == "sell"
        else "Insider stock transaction"
    )
    raw = (
        f"{sentiment_hint}.\n"
        f"Insider: {trade.insider_name} ({trade.title})\n"
        f"Action: {action} {trade.shares:,} shares {price_str}\n"
        f"Total value: {value_str}\n"
        f"Date: {trade.date}\n"
        f"Source: SEC Form 4 filing\n"
        f"Filing URL: {trade.filing_url}"
    )
    return Headline(
        url=url + f"#acc={trade.filing_accession}",
        title=title,
        source="sec_edgar_form4",
        published_at=datetime.strptime(trade.date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        ) if trade.date else None,
        tickers=[trade.ticker],
        category="filing",
        raw_content=raw,
    )


# ── Public collector interface ────────────────────────────────────────────────

async def collect_insider_trades(
    store: Store,
    lookback_days: int = 30,
) -> tuple[list[InsiderTrade], list[Headline]]:
    """
    Collect SEC EDGAR Form 4 insider trades for watched graphene tickers.

    Strategy:
    1. Look up each company's CIK via company_tickers.json.
    2. If found, pull Form 4 filings from /submissions/CIK{n}.json.
    3. If not found (Canadian-only company not SEC-registered), fall back to
       EFTS full-text search by company name.
    4. Parse each Form 4 XML for transaction details.
    5. Persist each trade via store.insert_insider_trade().
    6. Generate Headline objects for transactions exceeding $10,000.

    Args:
        store: An open Store instance.
        lookback_days: How many days back to search for filings.

    Returns:
        Tuple of (all trades found, high-value headlines for AI evaluation).
    """
    all_trades: list[InsiderTrade] = []
    headlines: list[Headline] = []

    for target in COMPANY_SEARCH_TARGETS:
        company_name = target["search_term"]
        ticker = target["ticker"]
        alt_terms: list[str] = target.get("alt_terms", [])

        logger.info("[sec_edgar] Processing %s (%s)", company_name, ticker)

        trades: list[InsiderTrade] = []

        # Step 1: Try CIK lookup
        cik: Optional[str] = None
        for search_name in [company_name] + alt_terms:
            cik = await _lookup_cik(search_name)
            if cik:
                logger.info(
                    "[sec_edgar] Found CIK %s for '%s'", cik, search_name
                )
                break

        if cik:
            trades = await _fetch_form4_via_submissions(
                cik=cik, ticker=ticker, lookback_days=lookback_days
            )
        else:
            # Step 2: Fallback to full-text EFTS search
            logger.info(
                "[sec_edgar] No CIK for '%s' — trying EFTS full-text search",
                company_name,
            )
            trades = await _fetch_form4_via_fulltext(
                company_name=company_name,
                ticker=ticker,
                lookback_days=lookback_days,
            )
            if not trades:
                logger.info(
                    "[sec_edgar] '%s' appears not SEC-registered (likely SEDAR only) "
                    "— returning empty for this company",
                    company_name,
                )

        # Step 3: Persist and build headlines
        for trade in trades:
            try:
                row_id = await store.insert_insider_trade(trade)
                if row_id is not None:
                    logger.info(
                        "[sec_edgar] Inserted trade id=%d: %s %s %s shares",
                        row_id,
                        trade.ticker,
                        trade.transaction_type,
                        trade.shares,
                    )
                    all_trades.append(trade)

                    # Generate headline for large transactions
                    value = trade.value_usd or 0.0
                    if value >= HEADLINE_THRESHOLD_USD:
                        headlines.append(_trade_to_headline(trade))
                else:
                    logger.debug(
                        "[sec_edgar] Skipping duplicate accession: %s",
                        trade.filing_accession,
                    )
            except Exception as exc:
                logger.error(
                    "[sec_edgar] Failed to insert trade %s: %s",
                    trade.filing_accession,
                    exc,
                )

    logger.info(
        "[sec_edgar] Done. %d trades collected, %d high-value headlines generated.",
        len(all_trades),
        len(headlines),
    )
    return all_trades, headlines
