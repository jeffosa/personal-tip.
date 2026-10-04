"""Manual bet ledger: records the bets the maintainer places by hand, from Telegram replies to alert and board
messages. It never places a bet.

    python -m prop_alpha.alerts.ledger --poll    # read new replies, record bets, answer each in the chat
    python -m prop_alpha.alerts.ledger --list    # print the open (unsettled) recorded bets

Replies are parsed by prop_alpha.alerts.replies (pure). Only messages from the configured TELEGRAM_CHAT_ID are
read; anyone else who messages the bot is ignored. A `placed` reply must answer the alert or board message it
refers to, which MessageIndex maps to that message's numbered bets.

I/O: Telegram (getUpdates, sendMessage) and three gitignored files under .prop_alpha_state/: placed_bets.jsonl
(append-only placed/undo events), alert_messages.jsonl (written by the senders) and telegram_offset.json (the
next update to read). Each recorded event carries its Telegram update id, so an update delivered twice is
recorded once.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from prop_alpha.alerts.replies import (
    AlertItem,
    PlaceCommand,
    PlacedBet,
    ReplyError,
    UndoCommand,
    confirmation,
    parse_reply,
    resolve_bet,
)
from prop_alpha.alerts.telegram import (
    DEFAULT_INDEX_FILE,
    OPEN_POSITION_WINDOW,
    MessageIndex,
    SentAlertStore,
    TelegramNotifier,
    load_config,
)
from prop_alpha.scanner.market_scanner import ExposureBook, ProjectionKey

logger = logging.getLogger("prop_alpha.ledger")

STATE_DIR: Final[Path] = DEFAULT_INDEX_FILE.parent
DEFAULT_BETS_FILE: Final[Path] = STATE_DIR / "placed_bets.jsonl"
DEFAULT_OFFSET_FILE: Final[Path] = STATE_DIR / "telegram_offset.json"
COMMAND_WORDS: Final[tuple[str, ...]] = ("placed", "undo")
NOT_A_REPLY: Final[str] = (
    "Reply to the alert or board message itself (long-press or swipe it, then Reply), so I know which bet you mean."
)


def _bet_from_record(raw: Mapping[str, Any]) -> PlacedBet:
    fields = {key: raw[key] for key in PlacedBet.__dataclass_fields__ if key != "placed_at"}
    return PlacedBet(**fields, placed_at=datetime.fromisoformat(str(raw["placed_at"])))


class PlacedBetStore:
    """Append-only JSON-lines record of placed and undone bets. A corrupt line raises: a guessed ledger could
    under-count exposure."""

    def __init__(self, path: Path = DEFAULT_BETS_FILE) -> None:
        self.path = path

    def _append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> tuple[dict[int, PlacedBet], set[int], set[int]]:
        """(bets by id, cancelled bet ids, Telegram update ids already recorded)."""
        bets: dict[int, PlacedBet] = {}
        cancelled: set[int] = set()
        seen: set[int] = set()
        if not self.path.is_file():
            return bets, cancelled, seen
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record: Any = json.loads(line)
            event = record.get("event") if isinstance(record, dict) else None
            if event == "placed":
                bet = _bet_from_record(record)
                bets[bet.bet_id] = bet
                seen.add(bet.update_id)
            elif event == "undo" and isinstance(record.get("bet_id"), int) and isinstance(record.get("update_id"), int):
                cancelled.add(int(record["bet_id"]))
                seen.add(int(record["update_id"]))
            else:
                raise ValueError(f"{self.path.name} line {number} is not a placed or undo record")
        return bets, cancelled, seen

    def record_bet(self, bet: PlacedBet) -> None:
        self._append({"event": "placed", **asdict(bet), "placed_at": bet.placed_at.isoformat()})

    def record_undo(self, bet_id: int, update_id: int, at: datetime) -> None:
        self._append({"event": "undo", "bet_id": bet_id, "update_id": update_id, "at": at.isoformat()})

    def open_bets(self, now: datetime, window: timedelta = OPEN_POSITION_WINDOW) -> list[PlacedBet]:
        """Bets placed within `window` before `now` and not undone (settlement is not recorded yet)."""
        bets, cancelled, _ = self.load()
        return sorted((b for b in bets.values() if b.bet_id not in cancelled and b.placed_at > now - window),
                      key=lambda b: b.bet_id)

    def exposure(self, now: datetime, window: timedelta = OPEN_POSITION_WINDOW) -> ExposureBook:
        totals: dict[ProjectionKey, float] = defaultdict(float)
        for bet in self.open_bets(now, window):
            totals[(bet.fixture_id, bet.player_id, bet.market)] += bet.stake
        return ExposureBook(dict(totals))


def combined_exposure(alerts: SentAlertStore, bets: PlacedBetStore, now: datetime) -> ExposureBook:
    """Per market, the larger of the stakes alerted and the stakes placed: a placed bet that followed an alert
    is not counted twice, and a bet taken from a board (no alerted stake) is still counted."""
    alerted, placed = alerts.exposure(now).by_market, bets.exposure(now).by_market
    return ExposureBook({key: max(alerted.get(key, 0.0), placed.get(key, 0.0)) for key in {*alerted, *placed}})


def _message_time(message: Mapping[str, Any], fallback: datetime) -> datetime:
    date = message.get("date")
    return datetime.fromtimestamp(date, timezone.utc) if isinstance(date, int) and not isinstance(date, bool) else fallback


def process_updates(
    updates: Sequence[Any],
    chat_id: str,
    messages: Mapping[int, Mapping[int, AlertItem]],
    store: PlacedBetStore,
    now: datetime,
) -> tuple[list[tuple[int, str]], int | None]:
    """Record the bets in `updates` and return (replies to send as (message id to answer, text), next offset).
    Messages from other chats, non-text messages and chat that is not a command are skipped."""
    replies: list[tuple[int, str]] = []
    next_offset: int | None = None
    bets, cancelled, seen = store.load()
    next_id = max(bets, default=0) + 1
    for update in updates:
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            continue
        next_offset = max(next_offset or 0, update_id + 1)
        message = update.get("message")
        if not isinstance(message, dict) or update_id in seen:
            continue
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or str(chat.get("id")) != chat_id:
            logger.warning("ignored update %d from a chat other than TELEGRAM_CHAT_ID", update_id)
            continue
        if not isinstance(text, str) or not text.strip().lower().startswith(COMMAND_WORDS):
            continue
        own_id = message.get("message_id")
        if not isinstance(own_id, int):
            continue
        command = parse_reply(text)
        if isinstance(command, ReplyError):
            replies.append((own_id, command.message))
        elif isinstance(command, UndoCommand):
            if command.bet_id not in bets or command.bet_id in cancelled:
                replies.append((own_id, f"No open recorded bet #{command.bet_id} to cancel."))
            else:
                store.record_undo(command.bet_id, update_id, _message_time(message, now))
                cancelled.add(command.bet_id)
                replies.append((own_id, f"Cancelled bet #{command.bet_id}."))
        else:
            answer, recorded = _record_place(command, message, messages, store, update_id, next_id, now)
            replies.append((own_id, answer))
            next_id += 1 if recorded else 0
        seen.add(update_id)
    return replies, next_offset


def _record_place(
    command: PlaceCommand, message: Mapping[str, Any], messages: Mapping[int, Mapping[int, AlertItem]],
    store: PlacedBetStore, update_id: int, bet_id: int, now: datetime,
) -> tuple[str, bool]:
    """(answer for the chat, whether a bet was recorded)."""
    replied: Any = message.get("reply_to_message")
    replied_id = replied.get("message_id") if isinstance(replied, dict) else None
    if not isinstance(replied_id, int):
        return NOT_A_REPLY, False
    items = messages.get(replied_id)
    if items is None:
        return "That message is not an alert or board I sent, so no bet was recorded. " + NOT_A_REPLY, False
    result = resolve_bet(command, items, bet_id=bet_id, update_id=update_id, message_id=replied_id,
                         placed_at=_message_time(message, now))
    if isinstance(result, ReplyError):
        return result.message, False
    store.record_bet(result)
    logger.info("recorded bet #%d: %s %s %s %g @ %.2f stake %g", result.bet_id, result.player, result.market,
                result.side, result.line, result.odds, result.stake)
    return confirmation(result), True


def poll(
    notifier: TelegramNotifier, index: MessageIndex, store: PlacedBetStore, offset_file: Path, now: datetime,
) -> int:
    """Read new replies, record bets, answer each in the chat. Returns the number of replies sent."""
    if notifier.chat_id is None:
        raise ValueError("TELEGRAM_CHAT_ID is not set; run python -m prop_alpha.alerts.telegram --get-chat-id")
    offset: int | None = None
    if offset_file.is_file():
        raw: Any = json.loads(offset_file.read_text(encoding="utf-8"))
        if not (isinstance(raw, dict) and isinstance(raw.get("offset"), int)):
            raise ValueError(f"{offset_file.name} is not an offset record")
        offset = int(raw["offset"])
    replies, next_offset = process_updates(notifier.get_updates(offset), notifier.chat_id, index.load(), store, now)
    if next_offset is not None:
        # Saved before answering: the bets are already recorded, and a failed answer must not re-read them.
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        offset_file.write_text(json.dumps({"offset": next_offset}), encoding="utf-8")
    for reply_to, text in replies:
        notifier.send_message(text, reply_to=reply_to)
    return len(replies)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Record manually placed bets from Telegram replies.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--poll", action="store_true", help="read new replies, record bets, answer in the chat")
    action.add_argument("--list", action="store_true", help="print the open recorded bets")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    store = PlacedBetStore()
    if args.list:
        open_bets = store.open_bets(now)
        for bet in open_bets:
            flag = "  BELOW MINIMUM" if bet.below_minimum else ""
            print(f"#{bet.bet_id} {bet.placed_at:%d %b %H:%M} {bet.match} | {bet.player} | {bet.side.upper()} "
                  f"{bet.line:g} {bet.market} @ {bet.odds:.2f} stake {bet.stake:g} ({bet.book}){flag}")
        print(f"{len(open_bets)} open bet(s), total stake {sum(b.stake for b in open_bets):g}")
        return 0
    sent = poll(TelegramNotifier(load_config()), MessageIndex(), store, DEFAULT_OFFSET_FILE, now)
    print(f"answered {sent} message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
