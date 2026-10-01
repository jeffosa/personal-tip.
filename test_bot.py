"""Temporary Telegram bot check. Not part of the test suite; delete when done.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env (or the environment). Never prints the token.

Run from the repository root:
    .venv\\Scripts\\python.exe test_bot.py
"""

from __future__ import annotations

import sys

from prop_alpha.alerts.telegram import TelegramError, TelegramNotifier, load_config


def run() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        print(f"config problem: {exc}")
        print("put the new token in .env (copy .env.example to .env first)")
        return 1
    notifier = TelegramNotifier(config)
    try:
        print(f"token accepted for bot @{notifier.check_connection()}")
        if config.chat_id is None:
            chats = notifier.recent_chats()
            print("TELEGRAM_CHAT_ID is not set. Chats that have messaged the bot:")
            for chat_id, name in chats or [("(none)", "send /start to the bot in Telegram, then run again")]:
                print(f"  {chat_id}  {name}")
            return 1
        message_id = notifier.send_message("prop_alpha test_bot.py: connection works.")
        print(f"test message delivered to chat {config.chat_id} (message_id {message_id})")
    except TelegramError as exc:
        print(f"telegram error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
