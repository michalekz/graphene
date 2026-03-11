"""
Telegram Bot API sender for Graphene Intel notifications.

Uses the python-telegram-bot library (v20+ async API).

Configuration is read from environment variables:
    TELEGRAM_BOT_TOKEN  — bot token from @BotFather
    TELEGRAM_CHAT_ID    — target chat/channel ID

All public methods are safe to call even when the bot is not configured:
they log a warning and return a falsy result rather than raising.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Optional

from src.db.store import Store
from src.evaluator.anomaly import PriceAnomaly
from src.notifier import formatter
from src.utils.dedup import is_duplicate

logger = logging.getLogger(__name__)

# ── Retry / rate-limit constants ───────────────────────────────────────────────

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
_RATE_LIMIT_SECONDS = 1.0  # Telegram allows ~1 message/second per bot


# ── Internal helpers ───────────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    """SHA-256 of the message text, used as a deduplication key."""
    return hashlib.sha256(text.encode()).hexdigest()


# ── Main class ─────────────────────────────────────────────────────────────────

class TelegramNotifier:
    """
    Async Telegram notification sender.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment on
    instantiation.  All send methods return False / None gracefully when the
    bot is not configured or an unrecoverable error occurs.

    Usage::

        notifier = TelegramNotifier()
        async with Store.connect() as store:
            await notifier.send_alert(headline, store)
    """

    def __init__(self) -> None:
        self._token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
        self._chat_id: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")

        if not self._token:
            logger.warning(
                "TELEGRAM_BOT_TOKEN is not set — Telegram notifications disabled"
            )
        if not self._chat_id:
            logger.warning(
                "TELEGRAM_CHAT_ID is not set — Telegram notifications disabled"
            )

        # Lazy-initialised; created on first use so the constructor stays fast
        self._bot: Optional[object] = None
        self._last_send_time: float = 0.0

    def _is_configured(self) -> bool:
        """Return True only when both token and chat ID are present."""
        return bool(self._token and self._chat_id)

    def _get_bot(self):
        """Return (or lazily create) the telegram.Bot instance."""
        if self._bot is None:
            from telegram import Bot  # type: ignore[import-untyped]
            self._bot = Bot(token=self._token)  # type: ignore[arg-type]
        return self._bot

    async def _enforce_rate_limit(self) -> None:
        """Sleep if needed to respect the 1 message/second Telegram limit."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_send_time
        if elapsed < _RATE_LIMIT_SECONDS:
            await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
        self._last_send_time = asyncio.get_event_loop().time()

    # ── Core send primitive ────────────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
    ) -> Optional[int]:
        """
        Send a single Telegram message with up to *_MAX_RETRIES* attempts.

        Args:
            text:       Message body (Markdown-formatted).
            parse_mode: Telegram parse mode (default "Markdown").

        Returns:
            Telegram message_id on success, None on failure.
        """
        if not self._is_configured():
            logger.debug("send_message skipped — bot not configured")
            return None

        if not text or not text.strip():
            logger.debug("send_message skipped — empty text")
            return None

        bot = self._get_bot()
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                await self._enforce_rate_limit()
                msg = await bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                logger.debug(
                    "Telegram message sent: id=%s attempt=%d", msg.message_id, attempt
                )
                return int(msg.message_id)

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "Telegram send attempt %d/%d failed: %s",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)

        logger.error(
            "Failed to send Telegram message after %d attempts: %s",
            _MAX_RETRIES,
            last_exc,
        )
        return None

    # ── High-level send methods ────────────────────────────────────────────────

    async def send_alert(self, headline: dict, store: Store) -> bool:
        """
        Send an instant alert for a single high-score headline.

        Steps:
            1. Compute a content hash and check whether this alert has already
               been sent within the cooldown window (store.alert_already_sent).
            2. Format the message via formatter.format_instant_alert.
            3. Send via Telegram.
            4. Record the sent alert in store.log_alert.

        Args:
            headline: Headline dict as returned by Store.get_unsent_high_score_headlines.
            store:    Active Store instance for deduplication and logging.

        Returns:
            True if the message was sent successfully, False otherwise.
        """
        try:
            text = formatter.format_instant_alert(headline)
            chash = _content_hash(text)

            if await store.alert_already_sent(chash):
                logger.debug(
                    "Alert already sent for headline id=%s — skipping",
                    headline.get("id"),
                )
                return False

            # Semantic deduplication: skip if a similar story was already alerted
            title = headline.get("title", "")
            if title:
                recent_titles = await store.get_recent_alert_titles(hours=6)
                if is_duplicate(title, recent_titles):
                    logger.info(
                        "Duplicate story suppressed: headline_id=%s title=%r",
                        headline.get("id"),
                        title[:80],
                    )
                    return False

            msg_id = await self.send_message(text)
            if msg_id is None:
                return False

            await store.log_alert(
                alert_type="instant",
                headline_id=headline.get("id"),
                telegram_message_id=msg_id,
                content_hash=chash,
            )
            logger.info(
                "Instant alert sent: headline_id=%s msg_id=%s",
                headline.get("id"),
                msg_id,
            )
            return True

        except Exception as exc:  # noqa: BLE001
            logger.error("send_alert failed: %s", exc, exc_info=True)
            return False

    async def send_anomaly_alert(self, anomaly: PriceAnomaly, store: Store) -> bool:
        """
        Send a price/volume anomaly alert.

        The content hash is derived from the formatted message text, ensuring
        duplicate anomaly alerts (same ticker + type + details) are suppressed
        within the cooldown window.

        Args:
            anomaly: PriceAnomaly dataclass instance.
            store:   Active Store instance for deduplication and logging.

        Returns:
            True if the message was sent successfully, False otherwise.
        """
        try:
            text = formatter.format_anomaly_alert(anomaly)
            chash = _content_hash(text)

            if await store.alert_already_sent(chash):
                logger.debug(
                    "Anomaly alert already sent for %s/%s — skipping",
                    anomaly.ticker,
                    anomaly.anomaly_type,
                )
                return False

            msg_id = await self.send_message(text)
            if msg_id is None:
                return False

            await store.log_alert(
                alert_type="anomaly",
                telegram_message_id=msg_id,
                content_hash=chash,
            )
            logger.info(
                "Anomaly alert sent: ticker=%s type=%s msg_id=%s",
                anomaly.ticker,
                anomaly.anomaly_type,
                msg_id,
            )
            return True

        except Exception as exc:  # noqa: BLE001
            logger.error("send_anomaly_alert failed: %s", exc, exc_info=True)
            return False

    async def send_daily_summary(
        self,
        prices: list[dict],
        headlines: list[dict],
        anomalies: list[PriceAnomaly],
        sentiment: dict,
        catalysts: list[dict],
        ai_summary: str,
        store: Store,
    ) -> bool:
        """
        Format and send the daily summary (possibly as multiple messages).

        Each chunk produced by formatter.format_daily_summary is sent
        sequentially.  If any chunk fails the method still attempts the
        remaining chunks and returns True only when all chunks succeed.

        Args:
            prices:     Latest price-snapshot dicts.
            headlines:  Scored headline dicts for the day.
            anomalies:  Detected anomaly instances.
            sentiment:  Ticker -> sentiment-score mapping.
            catalysts:  Pending catalyst dicts.
            ai_summary: Optional pre-formatted Claude Sonnet prose summary.
            store:      Active Store instance for deduplication and logging.

        Returns:
            True if all chunks were sent successfully, False otherwise.
        """
        try:
            chunks = formatter.format_daily_summary(
                prices=prices,
                headlines=headlines,
                anomalies=anomalies,
                sentiment=sentiment,
                catalysts=catalysts,
                ai_summary=ai_summary,
            )

            if not chunks:
                logger.warning("format_daily_summary returned empty result — nothing to send")
                return False

            all_ok = True
            for i, chunk in enumerate(chunks, start=1):
                chash = _content_hash(chunk)
                if await store.alert_already_sent(chash):
                    logger.debug("Daily summary chunk %d already sent — skipping", i)
                    continue

                msg_id = await self.send_message(chunk)
                if msg_id is None:
                    logger.error("Failed to send daily summary chunk %d/%d", i, len(chunks))
                    all_ok = False
                    continue

                await store.log_alert(
                    alert_type="daily_summary",
                    telegram_message_id=msg_id,
                    content_hash=chash,
                )
                logger.info("Daily summary chunk %d/%d sent: msg_id=%s", i, len(chunks), msg_id)

            return all_ok

        except Exception as exc:  # noqa: BLE001
            logger.error("send_daily_summary failed: %s", exc, exc_info=True)
            return False

    async def send_weekly_report(self, ai_report: str, store: Store) -> bool:
        """
        Format and send the weekly report (possibly as multiple messages).

        The report text from Claude Sonnet is split on section boundaries by
        formatter.format_weekly_report before sending.

        Args:
            ai_report: Full report prose produced by Claude Sonnet.
            store:     Active Store instance for deduplication and logging.

        Returns:
            True if all chunks were sent successfully, False otherwise.
        """
        try:
            chunks = formatter.format_weekly_report(ai_report)

            if not chunks:
                logger.warning("format_weekly_report returned empty result — nothing to send")
                return False

            all_ok = True
            for i, chunk in enumerate(chunks, start=1):
                chash = _content_hash(chunk)
                if await store.alert_already_sent(chash):
                    logger.debug("Weekly report chunk %d already sent — skipping", i)
                    continue

                msg_id = await self.send_message(chunk)
                if msg_id is None:
                    logger.error("Failed to send weekly report chunk %d/%d", i, len(chunks))
                    all_ok = False
                    continue

                await store.log_alert(
                    alert_type="weekly_report",
                    telegram_message_id=msg_id,
                    content_hash=chash,
                )
                logger.info("Weekly report chunk %d/%d sent: msg_id=%s", i, len(chunks), msg_id)

            return all_ok

        except Exception as exc:  # noqa: BLE001
            logger.error("send_weekly_report failed: %s", exc, exc_info=True)
            return False

    async def test_connection(self) -> bool:
        """
        Send a test message to verify the bot token and chat ID are correct.

        Returns:
            True if the test message was delivered, False otherwise.
        """
        if not self._is_configured():
            logger.error(
                "Cannot test connection: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set"
            )
            return False

        text = (
            "✅ *Graphene Intel* — connection test\n"
            "Bot is configured and reachable\\."
        )
        try:
            msg_id = await self.send_message(text)
            if msg_id is not None:
                logger.info("Telegram test message sent successfully: msg_id=%s", msg_id)
                return True
            logger.error("Telegram test message send returned None (no message_id)")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("test_connection failed: %s", exc, exc_info=True)
            return False
