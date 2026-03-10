#!/usr/bin/env python3
"""
Entry point: Generate and send weekly deep analysis report via Telegram.
Cron: 0 18 * * 0 (Sunday 18:00 CET)
Usage: python scripts/weekly_report.py
"""

import asyncio
import sys

sys.path.insert(0, "/opt/grafene")

from dotenv import load_dotenv
load_dotenv("/opt/grafene/.env")

from src.utils.logging import setup_logging
from src.db.store import Store

logger = setup_logging("weekly_report")


async def main() -> None:
    logger.info("Starting weekly report generation")

    # Also run weekly collectors before generating report
    async with Store.connect() as store:
        # Weekly: Google Trends + Patents
        try:
            from src.collectors.google_trends import collect_google_trends
            scores, headlines = await collect_google_trends(store)
            logger.info("Google Trends collected", extra={"scores": len(scores), "headlines": len(headlines)})
        except Exception as e:
            logger.error("Google Trends collection failed: %s", e)

        try:
            from src.collectors.patents import collect_patents
            patent_headlines = await collect_patents(store)
            logger.info("Patents collected", extra={"headlines": len(patent_headlines)})
        except Exception as e:
            logger.error("Patents collection failed: %s", e)

        # Generate weekly report with Claude Sonnet
        from src.analysis.weekly_report import generate_weekly_report
        report_text = await generate_weekly_report(store)

        # Send via Telegram
        from src.notifier.telegram import TelegramNotifier
        from src.notifier.formatter import format_weekly_report

        notifier = TelegramNotifier()
        messages = format_weekly_report(report_text)

        for i, msg in enumerate(messages, 1):
            sent = await notifier.send_message(msg)
            if sent:
                await store.log_alert(alert_type="weekly_report")
                logger.info("Weekly report part %d/%d sent", i, len(messages))
            else:
                logger.error("Failed to send weekly report part %d", i)

    logger.info("Weekly report run complete")


if __name__ == "__main__":
    asyncio.run(main())
