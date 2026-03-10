#!/usr/bin/env python3
"""
Entry point: Price/volume check during US market hours + anomaly alerts.
Cron: */15 15-22 * * 1-5 (every 15 min, Mon-Fri, 15:00-22:00 UTC = 16:00-23:00 CET)
Usage: python scripts/price_check.py
"""

import asyncio
import sys

sys.path.insert(0, "/opt/grafene")

from dotenv import load_dotenv
load_dotenv("/opt/grafene/.env")

from src.utils.logging import setup_logging
from src.db.store import Store

logger = setup_logging("price_check")


async def main() -> None:
    logger.info("Starting price check")

    async with Store.connect() as store:
        # Collect fresh prices
        from src.collectors.price import collect_prices
        snapshots = await collect_prices(store)
        logger.info("Prices updated", extra={"count": len(snapshots)})

        # Detect anomalies
        from src.evaluator.anomaly import detect_and_report
        anomalies = await detect_and_report(store)

        if anomalies:
            logger.info("Anomalies detected", extra={"count": len(anomalies)})
            from src.notifier.telegram import TelegramNotifier
            notifier = TelegramNotifier()

            for anomaly in anomalies:
                # Alert immediately for high severity, queue for daily summary for medium
                if anomaly.severity in ("high", "medium"):
                    await notifier.send_anomaly_alert(anomaly, store)
        else:
            logger.info("No anomalies detected")

    logger.info("Price check complete")


if __name__ == "__main__":
    asyncio.run(main())
