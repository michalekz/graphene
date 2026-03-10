#!/usr/bin/env python3
"""
Entry point: Generate and send daily summary via Telegram.
Cron: 0 20 * * * (20:00 CET)
Usage: python scripts/daily_summary.py
"""

import asyncio
import sys

sys.path.insert(0, "/opt/grafene")

from dotenv import load_dotenv
load_dotenv("/opt/grafene/.env")

from src.utils.logging import setup_logging
from src.db.store import Store

logger = setup_logging("daily_summary")


async def main() -> None:
    logger.info("Starting daily summary generation")

    async with Store.connect() as store:
        # Generate summary with Claude Sonnet
        from src.analysis.daily_summary import generate_daily_summary
        summary_text = await generate_daily_summary(store)

        # Fetch data for structured formatter (prices, anomalies, catalysts)
        from src.evaluator.anomaly import detect_and_report
        anomalies = await detect_and_report(store)

        # Send via Telegram
        from src.notifier.telegram import TelegramNotifier
        from src.notifier.formatter import format_daily_summary

        notifier = TelegramNotifier()
        # Daily summary uses the AI-generated text directly
        messages = format_daily_summary(
            prices=[],
            headlines=[],
            anomalies=anomalies,
            sentiment={},
            catalysts=[],
            ai_summary=summary_text,
        )

        all_sent = True
        for msg in messages:
            sent = await notifier.send_message(msg)
            if sent is None:
                all_sent = False
                logger.error("Failed to send daily summary chunk")
            else:
                await store.log_alert(
                    alert_type="daily_summary",
                    content_hash=None,
                )

        logger.info("Daily summary %s", "sent" if all_sent else "PARTIALLY FAILED")


if __name__ == "__main__":
    asyncio.run(main())
