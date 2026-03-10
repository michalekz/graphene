#!/usr/bin/env python3
"""
Entry point: Evaluate new headlines with Claude Haiku + detect anomalies.
Sends instant Telegram alerts for score >= 7.

Cron: every 35 minutes (runs 5 min after collect.py to ensure fresh headlines)
Usage: python scripts/evaluate.py
"""

import asyncio
import sys
import os

sys.path.insert(0, "/opt/grafene")

from dotenv import load_dotenv
load_dotenv("/opt/grafene/.env")

from src.utils.logging import setup_logging
from src.db.store import Store

logger = setup_logging("evaluate")


async def main() -> None:
    logger.info("Starting evaluation run")

    async with Store.connect() as store:
        # 1. Score new headlines with Claude Haiku
        from src.evaluator.scorer import score_headlines
        high_priority = await score_headlines(store)
        logger.info("Scoring done", extra={"high_priority": len(high_priority)})

        # 2. Detect price/volume anomalies
        from src.evaluator.anomaly import detect_and_report
        anomalies = await detect_and_report(store)
        logger.info("Anomaly detection done", extra={"anomalies": len(anomalies)})

        # 3. Send Telegram alerts
        from src.notifier.telegram import TelegramNotifier
        notifier = TelegramNotifier()

        # Send high-score headline alerts
        for headline in high_priority:
            sent = await notifier.send_alert(headline, store)
            if sent:
                logger.info(
                    "Alert sent",
                    extra={"score": headline.get("score"), "title": headline.get("title", "")[:60]},
                )

        # Send anomaly alerts (high severity only)
        for anomaly in anomalies:
            if anomaly.severity == "high":
                await notifier.send_anomaly_alert(anomaly, store)

    logger.info("Evaluation run complete")


if __name__ == "__main__":
    asyncio.run(main())
