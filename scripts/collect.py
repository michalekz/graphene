#!/usr/bin/env python3
"""
Entry point: Run all collectors and store new headlines/prices.

Cron: every 30 minutes (news) + every 15 minutes (prices)
Usage: python scripts/collect.py [--prices-only] [--news-only]
"""

import argparse
import asyncio
import sys
import os

sys.path.insert(0, "/opt/grafene")

from dotenv import load_dotenv
load_dotenv("/opt/grafene/.env")

from src.utils.logging import setup_logging
from src.db.store import Store

logger = setup_logging("collect")


async def run_news_collectors(store: Store) -> int:
    """Run all news/text collectors. Returns total new headlines inserted."""
    from src.collectors.tickertick import TickerTickCollector
    from src.collectors.rss import RSSCollector
    from src.collectors.google_news import GoogleNewsCollector

    collectors = [
        TickerTickCollector(),
        RSSCollector(),
        GoogleNewsCollector(),
    ]

    total = 0
    for collector in collectors:
        try:
            count = await collector.collect_and_store(store)
            logger.info("Collector done", extra={"source": collector.name, "new": count})
            total += count
        except Exception as e:
            logger.error("Collector failed", extra={"source": collector.name, "error": str(e)})

    return total


async def run_sentiment_collectors(store: Store) -> None:
    """Run StockTwits + Reddit sentiment."""
    from src.collectors.stocktwits import collect_stocktwits_sentiment
    from src.collectors.reddit import collect_reddit_sentiment

    try:
        await collect_stocktwits_sentiment(store)
        logger.info("StockTwits sentiment collected")
    except Exception as e:
        logger.error("StockTwits failed: %s", e)

    try:
        await collect_reddit_sentiment(store)
        logger.info("Reddit sentiment collected")
    except Exception as e:
        logger.error("Reddit failed: %s", e)


async def run_price_collector(store: Store) -> None:
    """Run yfinance price collector."""
    from src.collectors.price import collect_prices

    try:
        snapshots = await collect_prices(store)
        logger.info("Price collection done", extra={"count": len(snapshots)})
    except Exception as e:
        logger.error("Price collector failed: %s", e)


async def run_filing_collectors(store: Store) -> None:
    """Run SEC EDGAR + SEDI insider trades (daily)."""
    from src.collectors.sec_edgar import collect_insider_trades
    from src.collectors.sedi import collect_sedi_insider_trades

    try:
        trades, headlines = await collect_insider_trades(store)
        logger.info("EDGAR done", extra={"trades": len(trades), "headlines": len(headlines)})
    except Exception as e:
        logger.error("SEC EDGAR collector failed: %s", e)

    try:
        trades, headlines = await collect_sedi_insider_trades(store)
        logger.info("SEDI done", extra={"trades": len(trades), "headlines": len(headlines)})
    except Exception as e:
        logger.error("SEDI collector failed: %s", e)


async def run_company_news_collectors(store: Store) -> int:
    """Run direct company IR / RSS collectors."""
    from src.collectors.company_news import collect_company_news

    try:
        count = await collect_company_news(store)
        logger.info("Company news done", extra={"new": count})
        return count
    except Exception as e:
        logger.error("Company news collector failed: %s", e)
        return 0


async def run_portfolio_collector(store: Store) -> None:
    """Sync IBKR portfolio positions once daily."""
    from src.collectors.ibkr_flex import collect_ibkr_positions

    try:
        positions = await collect_ibkr_positions(store)
        logger.info("IBKR portfolio synced", extra={"positions": len(positions)})
    except Exception as e:
        logger.error("IBKR portfolio collector failed: %s", e)


async def run_market_data_collectors(store: Store) -> None:
    """Run FINRA short interest + OTC tier monitoring (hourly)."""
    from src.collectors.finra_short import collect_short_interest
    from src.collectors.otc_markets import collect_otc_status

    try:
        records, headlines = await collect_short_interest(store)
        logger.info("FINRA short interest done", extra={"records": len(records), "alerts": len(headlines)})
    except Exception as e:
        logger.error("FINRA short interest collector failed: %s", e)

    try:
        scores, headlines = await collect_otc_status(store)
        logger.info("OTC tier monitoring done", extra={"tickers": len(scores), "alerts": len(headlines)})
    except Exception as e:
        logger.error("OTC Markets collector failed: %s", e)


async def main(prices_only: bool = False, news_only: bool = False) -> None:
    logger.info("Starting collection run", extra={"prices_only": prices_only, "news_only": news_only})

    async with Store.connect() as store:
        if not news_only:
            await run_price_collector(store)

        if not prices_only:
            from datetime import datetime
            hour = datetime.now().hour

            total_news = await run_news_collectors(store)
            total_news += await run_company_news_collectors(store)
            await run_sentiment_collectors(store)

            # Run filing collectors twice daily (8:00 and 20:00 UTC)
            if hour in (8, 20):
                await run_filing_collectors(store)

            # Run market data collectors every 4 hours
            if hour % 4 == 0:
                await run_market_data_collectors(store)

            # Sync IBKR portfolio once daily at 20:00 UTC
            if hour == 20:
                await run_portfolio_collector(store)

            logger.info("Collection complete", extra={"total_new_headlines": total_news})

        stats = await store.get_db_stats()
        logger.info("DB stats", extra=stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Graphene Intel collector")
    parser.add_argument("--prices-only", action="store_true", help="Only collect price data")
    parser.add_argument("--news-only", action="store_true", help="Only collect news")
    args = parser.parse_args()
    asyncio.run(main(prices_only=args.prices_only, news_only=args.news_only))
