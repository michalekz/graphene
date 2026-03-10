#!/usr/bin/env python3
"""
Helper: Setup and test Telegram bot connection.
Usage: python scripts/setup_telegram.py
"""

import asyncio
import sys
import os

sys.path.insert(0, "/opt/grafene")

from dotenv import load_dotenv
load_dotenv("/opt/grafene/.env")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    print("=== Telegram Bot Setup ===\n")

    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set in .env")
        print("   1. Open Telegram, search for @BotFather")
        print("   2. Send /newbot, follow instructions")
        print("   3. Copy the token to .env")
        return

    if not chat_id:
        print("⚠️  TELEGRAM_CHAT_ID not set in .env")
        print("   To get your chat_id:")
        print("   1. Send any message to your bot")
        print(f"   2. Open: https://api.telegram.org/bot{token}/getUpdates")
        print('   3. Find "chat":{"id":XXXXXXXX} in the response')
        print("   4. Copy that ID to .env as TELEGRAM_CHAT_ID")
        return

    print(f"✓ Token: {token[:10]}...{token[-4:]}")
    print(f"✓ Chat ID: {chat_id}")

    from src.notifier.telegram import TelegramNotifier
    notifier = TelegramNotifier()
    ok = await notifier.test_connection()

    if ok:
        print("\n✅ Telegram connection working! Test message sent.")
    else:
        print("\n❌ Telegram test failed. Check token and chat_id in .env")


if __name__ == "__main__":
    asyncio.run(main())
