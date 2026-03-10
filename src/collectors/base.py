"""
Abstract base class for all graphene-intel news collectors.

Each collector is responsible for fetching a specific source of headlines and
returning them as Headline objects. The base class provides the common
collect_and_store() workflow: fetch -> deduplicate -> persist.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from src.db.store import Headline, Store

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Abstract base for all news collectors.

    Subclasses must set the ``name`` class attribute and implement ``collect()``.
    """

    name: str = "base"

    @abstractmethod
    async def collect(self) -> list[Headline]:
        """Fetch new items from the source.

        Returns a list of Headline objects that have *not* been deduplicated
        against the database — that step is handled by ``collect_and_store()``.

        Implementations must catch all exceptions internally, log them, and
        return an empty list rather than propagating errors to the caller.
        """

    async def collect_and_store(self, store: Store) -> int:
        """Run the full collect → deduplicate → store pipeline.

        Calls ``collect()`` to fetch raw headlines, then inserts only those
        that do not already exist in the database (keyed by url_hash).

        Args:
            store: An open Store instance (async context manager already entered).

        Returns:
            The number of *new* headlines inserted into the database.
        """
        logger.info("[%s] Starting collection", self.name)

        try:
            headlines = await self.collect()
        except Exception:
            logger.exception("[%s] Unhandled error in collect()", self.name)
            return 0

        if not headlines:
            logger.info("[%s] No headlines returned", self.name)
            return 0

        inserted = 0
        for headline in headlines:
            try:
                row_id = await store.insert_headline(headline)
                if row_id is not None:
                    inserted += 1
                    logger.debug(
                        "[%s] Inserted headline id=%d: %s",
                        self.name,
                        row_id,
                        headline.title[:80],
                    )
            except Exception:
                logger.exception(
                    "[%s] Failed to insert headline url=%s", self.name, headline.url
                )

        logger.info(
            "[%s] Done. Collected %d, inserted %d new.",
            self.name,
            len(headlines),
            inserted,
        )
        return inserted
