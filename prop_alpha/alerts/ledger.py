"""Manual bet ledger: records the bets the maintainer places by hand, from Telegram replies to alert and board
messages. It never places a bet.

    python -m prop_alpha.alerts.ledger --poll    # read new replies, record bets, answer each in the chat
    python -m prop_alpha.alerts.ledger --list    # print the open (unsettled) bets and the settled results
    python -m prop_alpha.alerts.ledger --settle [--send]   # settle open bets whose match is final in the database
    python -m prop_alpha.alerts.ledger --reconcile [--send]   # your settled replies v automatic settlement

Replies are parsed by prop_alpha.alerts.replies (pure). Only messages from the configured TELEGRAM_CHAT_ID are
read; anyone else who messages the bot is ignored. A `placed` reply must answer the alert or board message it
refers to, which MessageIndex maps to that message's numbered bets.

I/O: Telegram (getUpdates, sendMessage), one short read-only database transaction for --settle, and three
gitignored files under .prop_alpha_state/: placed_bets.jsonl (append-only placed, undo and settled events),
alert_messages.jsonl (written by the senders) and telegram_offset.json (the next update to read). Each recorded
reply carries its Telegram update id, so an update delivered twice is recorded once.

A placed bet counts as open exposure until it is settled or undone, however old it is (audit 2026-10-04: expiring
it after OPEN_POSITION_WINDOW under-counted a postponed or unsettled bet). Automatic settlement (`grade`) reads the
match from the database, which scripts/telegram_status.cmd refreshes first (prop_alpha.ingestion.results); a bet
still open STALE_AFTER after it was placed is flagged by --list and the status report for settling by hand.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterator, Mapping, Sequence

from prop_alpha.alerts.closing import load_closing_quotes
from prop_alpha.alerts.fx import BOOK_CURRENCY, CURRENCY_SYMBOLS, ExchangeRate, bankroll_currency, cached_rate, is_fresh
from prop_alpha.alerts.replies import (
    RECONCILE_FAILURES,
    RESULTS,
    SETTLEMENT_COLUMNS,
    SETTLEMENT_SOURCES,
    AlertItem,
    ClosingValue,
    PlaceCommand,
    PlacedBet,
    Reconciliation,
    ReplyError,
    ResultCommand,
    SettleCommand,
    Settlement,
    UndoCommand,
    book_from_detail,
    confirmation,
    grade,
    parse_reply,
    profit,
    reconcile,
    reconciliation_summary,
    reconciliation_text,
    resolve_bet,
    settlement_text,
)
from prop_alpha.alerts.telegram import (
    DEFAULT_INDEX_FILE,
    DeliveryLog,
    MessageIndex,
    SentAlertStore,
    TelegramNotifier,
    load_config,
)
from prop_alpha.config import DEFAULT_DSN, DSN_ENV
from prop_alpha.evaluation.backtest import closing_line_value
from prop_alpha.scanner.market_scanner import ExposureBook, ProjectionKey

logger = logging.getLogger("prop_alpha.ledger")

STATE_DIR: Final[Path] = DEFAULT_INDEX_FILE.parent
DEFAULT_BETS_FILE: Final[Path] = STATE_DIR / "placed_bets.jsonl"
DEFAULT_OFFSET_FILE: Final[Path] = STATE_DIR / "telegram_offset.json"
# The bot's answers to bet replies, queued before they are sent (AnswerOutbox), and the time of the last poll that
# read Telegram (status report: Telegram keeps an unread reply for 24 hours only).
DEFAULT_ANSWERS_FILE: Final[Path] = STATE_DIR / "ledger_answers.jsonl"
DEFAULT_HEARTBEAT_FILE: Final[Path] = STATE_DIR / "ledger_last_poll.json"
# Held by every run that writes the ledger (ledger_lock).
DEFAULT_LOCK_FILE: Final[Path] = STATE_DIR / "ledger.lock"
# An answer that failed this many sends is left for the status report instead of being retried for ever.
MAX_ANSWER_ATTEMPTS: Final[int] = 3
DELAYED_PREFIX: Final[str] = "(Delayed answer - this was not delivered when you sent your reply.) "
# A queued notice that answers no message (the BETS SETTLED message of --settle --send), re-sent by the next poll.
DELAYED_NOTICE_PREFIX: Final[str] = "(Delayed - this message failed to send earlier.) "
SETTLED_HEADER: Final[str] = "BETS SETTLED (reply settled <bet id> won|lost|void to correct one)"
EDIT_NOTICE: Final[str] = (
    "Edits are not applied. If the original reply recorded a bet, send undo <bet id>, then send the corrected reply "
    "as a new reply to the alert."
)
COMMAND_WORDS: Final[tuple[str, ...]] = ("placed", "undo", "settle", "result")
# A bot command ("/status", "/bets@my_bot ..."): read-only questions the poll answers (alerts/bot_commands.py, 2026-10-15).
BOT_COMMAND_PREFIX: Final[str] = "/"
# A bet still open this long after it was placed is flagged for settling by hand (postponed, abandoned, or a
# player row missing from the stored match).
STALE_AFTER: Final[timedelta] = timedelta(days=7)
# Fixture status and the bet player's minutes and counts; no fixture row means the match was never ingested.
SETTLEMENT_SQL: Final[str] = """
    SELECT f.status, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.fixtures f
    LEFT JOIN prop_alpha.player_match_stats s ON s.fixture_id = f.fixture_id AND s.player_id = %s
    WHERE f.vendor_fixture_uid = %s
"""
NOT_A_REPLY: Final[str] = (
    "Reply to the alert or board message itself (long-press or swipe it, then Reply), so I know which bet you mean."
)


OPTIONAL_BET_FIELDS: Final[frozenset[str]] = frozenset({"entered_stake", "entered_currency"})


def _bet_from_record(raw: Mapping[str, Any]) -> PlacedBet:
    """Records written before a field existed lack it; only OPTIONAL_BET_FIELDS may be missing."""
    fields = {key: raw[key] for key in PlacedBet.__dataclass_fields__
              if key != "placed_at" and (key not in OPTIONAL_BET_FIELDS or key in raw)}
    return PlacedBet(**fields, placed_at=datetime.fromisoformat(str(raw["placed_at"])))


def stake_in_bankroll(command: PlaceCommand, currency: str,
                      book_rate: ExchangeRate | None) -> tuple[float, tuple[float, str] | None] | ReplyError:
    """(stake in the bankroll currency, (amount, currency) as entered when converted), or why not: a stake with no
    sign, or the bankroll's own sign, is in the bankroll currency; the books' sign is converted at `book_rate`."""
    entered = None if command.stake_symbol is None else CURRENCY_SYMBOLS[command.stake_symbol]
    if entered is None or entered == currency:
        return command.stake, None
    if book_rate is not None and not book_rate.identity and (book_rate.base, book_rate.quote) == (entered, currency):
        return round(book_rate.convert(command.stake), 2), (command.stake, entered)
    return ReplyError(f"Stakes are recorded in {currency or 'the bankroll currency'}, and no current {entered} rate "
                      f"is available: give the stake without a currency sign, e.g. placed {command.number} "
                      f"@{command.odds:g} {command.stake:g}")


def _optional_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{where}: settled record has a non-integer count, minutes or update_id")
    return value


def _settlement_from_record(raw: Mapping[str, Any], where: str) -> Settlement:
    if (not isinstance(raw.get("bet_id"), int) or raw.get("result") not in RESULTS
            or raw.get("source") not in SETTLEMENT_SOURCES or not isinstance(raw.get("at"), str)):
        raise ValueError(f"{where}: settled record needs bet_id, result, source and at")
    return Settlement(
        bet_id=int(raw["bet_id"]), result=str(raw["result"]), source=str(raw["source"]),
        detail=str(raw.get("detail", "")), settled_at=datetime.fromisoformat(str(raw["at"])),
        count=_optional_int(raw.get("count"), where), minutes=_optional_int(raw.get("minutes"), where),
        update_id=_optional_int(raw.get("update_id"), where),
        # Records written before settlements carried a bookmaker name it in their detail (audit 2026-10-25).
        book=(None if raw.get("source") != "manual" else
              str(raw["book"]) if raw.get("book") is not None else book_from_detail(str(raw.get("detail", "")))),
    )


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
        bets, cancelled, seen, _ = self.load_all()
        return bets, cancelled, seen

    def load_all(self) -> tuple[dict[int, PlacedBet], set[int], set[int], dict[int, Settlement]]:
        """As load, plus the settlement of each settled bet: a manual result overrides an automatic one whatever
        their order in the file, and a later manual result overrides an earlier one."""
        bets: dict[int, PlacedBet] = {}
        cancelled: set[int] = set()
        seen: set[int] = set()
        settlements: dict[int, Settlement] = {}
        if not self.path.is_file():
            return bets, cancelled, seen, settlements
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
            elif event == "settled":
                settlement = _settlement_from_record(record, f"{self.path.name} line {number}")
                if settlement.update_id is not None:
                    seen.add(settlement.update_id)
                earlier = settlements.get(settlement.bet_id)
                if earlier is None or settlement.source == "manual" or earlier.source == "auto":
                    settlements[settlement.bet_id] = settlement
            else:
                raise ValueError(f"{self.path.name} line {number} is not a placed, undo or settled record")
        return bets, cancelled, seen, settlements

    def settlement_update_ids(self) -> set[int]:
        """The Telegram update of every settlement recorded from a reply (a re-delivered result is recorded once). Kept
        apart from load()'s seen set: a paper bet's update_id is its own number, not a Telegram update."""
        if not self.path.is_file():
            return set()
        found: set[int] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            record: Any = json.loads(line) if line.strip() else None
            if isinstance(record, dict) and record.get("event") == "settled" and isinstance(record.get("update_id"), int):
                found.add(int(record["update_id"]))
        return found

    def record_bet(self, bet: PlacedBet) -> None:
        self._append({"event": "placed", **asdict(bet), "placed_at": bet.placed_at.isoformat()})

    def record_undo(self, bet_id: int, update_id: int, at: datetime) -> None:
        self._append({"event": "undo", "bet_id": bet_id, "update_id": update_id, "at": at.isoformat()})

    def record_settlement(self, settlement: Settlement) -> None:
        self._append({
            "event": "settled", "bet_id": settlement.bet_id, "result": settlement.result,
            "source": settlement.source, "detail": settlement.detail, "at": settlement.settled_at.isoformat(),
            "count": settlement.count, "minutes": settlement.minutes, "update_id": settlement.update_id,
            "book": settlement.book,
        })

    def open_bets(self, now: datetime) -> list[PlacedBet]:
        """Bets neither undone nor settled, however old. `now` is checked to be UTC (stale_bets ages from it)."""
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be a timezone-aware UTC datetime")
        bets, cancelled, _, settlements = self.load_all()
        return sorted((b for b in bets.values() if b.bet_id not in cancelled and b.bet_id not in settlements),
                      key=lambda b: b.bet_id)

    def stale_bets(self, now: datetime, after: timedelta = STALE_AFTER) -> list[PlacedBet]:
        """Open bets placed more than `after` before `now`: settle them by hand."""
        return [bet for bet in self.open_bets(now) if bet.placed_at < now - after]

    def automatic_settlements(self) -> dict[int, Settlement]:
        """The latest automatic settlement recorded for each bet, even one a `settled` reply later overrode (the
        effective settlement hides it): settlement reconciliation counts such overrides."""
        automatic: dict[int, Settlement] = {}
        if not self.path.is_file():
            return automatic
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            record: Any = json.loads(line) if line.strip() else None
            if isinstance(record, dict) and record.get("event") == "settled" and record.get("source") == "auto":
                settlement = _settlement_from_record(record, f"{self.path.name} line {number}")
                automatic[settlement.bet_id] = settlement
        return automatic

    def settled_bets(self) -> list[tuple[PlacedBet, Settlement]]:
        """Every settled bet that was not undone, with its effective settlement, by bet id."""
        bets, cancelled, _, settlements = self.load_all()
        return [(bets[bet_id], settlements[bet_id]) for bet_id in sorted(settlements)
                if bet_id in bets and bet_id not in cancelled]

    def exposure(self, now: datetime) -> ExposureBook:
        totals: dict[ProjectionKey, float] = defaultdict(float)
        for bet in self.open_bets(now):
            totals[(bet.fixture_id, bet.player_id, bet.market)] += bet.stake
        return ExposureBook(dict(totals))


def combined_exposure(alerts: SentAlertStore, bets: PlacedBetStore, now: datetime) -> ExposureBook:
    """Per market, the larger of the stakes alerted (within OPEN_POSITION_WINDOW) and the stakes placed and not yet
    settled: a placed bet that followed an alert is not counted twice, and a bet taken from a board (no alerted
    stake) is still counted."""
    alerted, placed = alerts.exposure(now).by_market, bets.exposure(now).by_market
    return ExposureBook({key: max(alerted.get(key, 0.0), placed.get(key, 0.0)) for key in {*alerted, *placed}})


class AnswerOutbox:
    """Append-only record of the bot's answers to bet replies (delivery audit 2026-10-05). The offset is saved before
    answers are sent, so a reply is never recorded twice; an answer that then failed to send used to be lost, and a
    maintainer who saw no "Recorded bet #N" could send the reply again and record the bet twice. Each answer is now
    queued ("pending", with its text) before the offset is saved, each send attempt is logged, and a sent answer is
    marked "sent"; the next poll re-sends what is still unsent, up to MAX_ANSWER_ATTEMPTS. A corrupt line raises."""

    def __init__(self, path: Path = DEFAULT_ANSWERS_FILE) -> None:
        self.path = path

    def _append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record: Any = json.loads(line)
            if not (isinstance(record, dict) and isinstance(record.get("answer_id"), int)
                    and record.get("event") in ("pending", "attempt", "sent")):
                raise ValueError(f"{self.path.name} line {number} is not an answer event")
            reply_to: Any = record.get("reply_to")
            if record["event"] == "pending" and not ((reply_to is None or isinstance(reply_to, int))
                                                     and isinstance(record.get("text"), str)):
                raise ValueError(f"{self.path.name} line {number}: a pending answer needs reply_to and text")
            events.append(record)
        return events

    def queue(self, reply_to: int | None, text: str, at: datetime) -> int:
        """Queue one answer (reply_to None: a notice that answers no message, such as BETS SETTLED); returns its id."""
        answer_id = max((event["answer_id"] for event in self._events()), default=0) + 1
        self._append({"answer_id": answer_id, "event": "pending", "reply_to": reply_to, "text": text,
                      "at": at.isoformat()})
        return answer_id

    def attempt(self, answer_id: int, at: datetime) -> None:
        self._append({"answer_id": answer_id, "event": "attempt", "at": at.isoformat()})

    def mark_sent(self, answer_id: int, message_id: int, at: datetime) -> None:
        self._append({"answer_id": answer_id, "event": "sent", "message_id": message_id, "at": at.isoformat()})

    def unsent(self) -> list[tuple[int, int | None, str, int]]:
        """(answer id, message it answers or None, text, send attempts so far) of every answer not yet sent, oldest
        first."""
        pending: dict[int, tuple[int | None, str]] = {}
        attempts: dict[int, int] = defaultdict(int)
        sent: set[int] = set()
        for event in self._events():
            if event["event"] == "pending":
                raw_reply_to: Any = event.get("reply_to")
                pending[event["answer_id"]] = (None if raw_reply_to is None else int(raw_reply_to), str(event["text"]))
            elif event["event"] == "attempt":
                attempts[event["answer_id"]] += 1
            else:
                sent.add(event["answer_id"])
        return [(answer_id, reply_to, text, attempts[answer_id])
                for answer_id, (reply_to, text) in sorted(pending.items()) if answer_id not in sent]

    def abandoned(self) -> list[tuple[int, int | None, str, int]]:
        """Unsent answers that reached MAX_ANSWER_ATTEMPTS: reported, no longer retried."""
        return [entry for entry in self.unsent() if entry[3] >= MAX_ANSWER_ATTEMPTS]


def last_poll(heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE) -> datetime | None:
    """When a poll last read Telegram's updates, or None if never (or not since this was added)."""
    if not heartbeat_file.is_file():
        return None
    raw: Any = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    if not (isinstance(raw, dict) and isinstance(raw.get("at"), str)):
        raise ValueError(f"{heartbeat_file.name} is not a poll heartbeat")
    return datetime.fromisoformat(raw["at"])


def _message_time(message: Mapping[str, Any], fallback: datetime) -> datetime:
    date = message.get("date")
    return datetime.fromtimestamp(date, timezone.utc) if isinstance(date, int) and not isinstance(date, bool) else fallback


def process_updates(
    updates: Sequence[Any],
    chat_id: str,
    messages: Mapping[int, Mapping[int, AlertItem]],
    store: PlacedBetStore,
    now: datetime,
    currency: str = "",
    book_rate: ExchangeRate | None = None,
    bot_commands: list[tuple[int, str]] | None = None,
    pilot_store: PlacedBetStore | None = None,
    pilot_lock: Path | None = None,
) -> tuple[list[tuple[int, str]], int | None]:
    """Record the bets in `updates` and return (replies to send as (message id to answer, text), next offset).
    Messages from other chats, non-text messages and chat that is not a command are skipped. Stakes are recorded
    in `currency` (the bankroll's); one typed with the books' sign is converted at `book_rate`. With `bot_commands`, each
    "/command" from the chat is appended to it as (message id, command name) for the caller to answer; without, it is
    skipped as before. With `pilot_store` (the pilot's paper-bet store) a "result <tip> won|lost|void [book]" message
    records the bookmaker's result for that paper tip, under `pilot_lock` when given (held only for the write)."""
    replies: list[tuple[int, str]] = []
    pilot_bets: dict[int, PlacedBet] | None = None
    pilot_seen: set[int] = set()
    next_offset: int | None = None
    bets, cancelled, seen = store.load()
    next_id = max(bets, default=0) + 1
    for update in updates:
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            continue
        next_offset = max(next_offset or 0, update_id + 1)
        edited = update.get("edited_message")
        if isinstance(edited, dict) and update_id not in seen:
            # Until 2026-10-05 edits were not even fetched: a corrected stake or price was silently ignored.
            edited_chat: Any = edited.get("chat")
            edited_text: Any = edited.get("text")
            if (isinstance(edited_chat, dict) and str(edited_chat.get("id")) == chat_id
                    and isinstance(edited_text, str) and edited_text.strip().lower().startswith(COMMAND_WORDS)
                    and isinstance(edited.get("message_id"), int)):
                replies.append((int(edited["message_id"]), EDIT_NOTICE))
            continue
        message = update.get("message")
        if not isinstance(message, dict) or update_id in seen:
            continue
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or str(chat.get("id")) != chat_id:
            logger.warning("ignored update %d from a chat other than TELEGRAM_CHAT_ID", update_id)
            continue
        if bot_commands is not None and isinstance(text, str) and text.strip().startswith(BOT_COMMAND_PREFIX)                 and isinstance(message.get("message_id"), int):
            bot_commands.append((int(message["message_id"]), bot_command_name(text)))
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
        elif isinstance(command, ResultCommand):
            if pilot_store is None:
                replies.append((own_id, "Pilot results are not recorded by this poll."))
            else:
                if pilot_bets is None:
                    pilot_bets = pilot_store.load()[0]
                    pilot_seen = pilot_store.settlement_update_ids()
                if update_id in pilot_seen:
                    continue
                if command.bet_id not in pilot_bets:
                    replies.append((own_id, f"No pilot paper tip #{command.bet_id} (tip numbers: /pilot)."))
                else:
                    tip = pilot_bets[command.bet_id]
                    book = command.book or "book not given"
                    settlement = Settlement(command.bet_id, command.result, "manual",
                                            f"bookmaker result ({book}) recorded from Telegram",
                                            _message_time(message, now), update_id=update_id, book=command.book)
                    if pilot_lock is None:
                        pilot_store.record_settlement(settlement)
                    else:
                        with ledger_lock(pilot_lock):
                            pilot_store.record_settlement(settlement)
                    pilot_seen.add(update_id)
                    other = (f" Note: the tip was {tip.book}'s price; this result is {command.book}'s."
                             if command.book and command.book.lower() != tip.book.lower() else "")
                    replies.append((own_id, f"Recorded the bookmaker's result for pilot paper tip #{command.bet_id}: "
                                            f"{command.result.upper()} ({book}). {tip.player}, {tip.side.upper()} "
                                            f"{tip.line:g} {tip.market}, {tip.match}.{other} It stays a paper tip; "
                                            "pilot_ledger --reconcile compares it with the automatic result."))
        elif isinstance(command, SettleCommand):
            if command.bet_id not in bets or command.bet_id in cancelled:
                replies.append((own_id, f"No recorded bet #{command.bet_id} to settle."))
            else:
                settlement = Settlement(command.bet_id, command.result, "manual", "settled by hand",
                                        _message_time(message, now), update_id=update_id)
                store.record_settlement(settlement)
                replies.append((own_id, "Recorded " + settlement_text(bets[command.bet_id], settlement)))
        else:
            answer, recorded = _record_place(command, message, messages, store, update_id, next_id, now, currency,
                                             book_rate, open_bets=[b for b in bets.values() if b.bet_id not in cancelled])
            replies.append((own_id, answer))
            if recorded is not None:
                bets[recorded.bet_id] = recorded
                next_id += 1
        seen.add(update_id)
    return replies, next_offset


def bot_command_name(text: str) -> str:
    """"/Status@prop_alpha_bot now" -> "status": the command word, lower case, without the bot's name."""
    word = text.strip()[len(BOT_COMMAND_PREFIX):].split(maxsplit=1)
    return word[0].split("@", 1)[0].lower() if word else ""


def _record_place(
    command: PlaceCommand, message: Mapping[str, Any], messages: Mapping[int, Mapping[int, AlertItem]],
    store: PlacedBetStore, update_id: int, bet_id: int, now: datetime, currency: str = "",
    book_rate: ExchangeRate | None = None, open_bets: Sequence[PlacedBet] = (),
) -> tuple[str, PlacedBet | None]:
    """(answer for the chat, the bet recorded or None). A bet identical to an open one (same message, number,
    side, odds and stake) is still recorded, since two identical bets can be real, but the answer says so: a
    repeat sent because an answer never arrived would otherwise double the position unnoticed."""
    replied: Any = message.get("reply_to_message")
    replied_id = replied.get("message_id") if isinstance(replied, dict) else None
    if not isinstance(replied_id, int):
        return NOT_A_REPLY, None
    items = messages.get(replied_id)
    if items is None:
        return "That message is not an alert or board I sent, so no bet was recorded. " + NOT_A_REPLY, None
    stake = stake_in_bankroll(command, currency, book_rate)
    if isinstance(stake, ReplyError):
        return stake.message, None
    result = resolve_bet(command, items, bet_id=bet_id, update_id=update_id, message_id=replied_id,
                         placed_at=_message_time(message, now), stake=stake[0], entered=stake[1])
    if isinstance(result, ReplyError):
        return result.message, None
    twins = [bet.bet_id for bet in open_bets
             if (bet.message_id, bet.number, bet.side, bet.odds, bet.stake)
             == (result.message_id, result.number, result.side, result.odds, result.stake)]
    store.record_bet(result)
    logger.info("recorded bet #%d: %s %s %s %g @ %.2f stake %g", result.bet_id, result.player, result.market,
                result.side, result.line, result.odds, result.stake)
    warning = ("" if not twins else
               f"\nPOSSIBLE DUPLICATE of open bet #{twins[0]} (same bet, odds and stake). If you placed it only once, "
               f"reply undo {result.bet_id}.")
    return confirmation(result) + warning, result


def confirm_quoted_deliveries(
    updates: Sequence[Any], chat_id: str, messages: Mapping[int, Mapping[int, AlertItem]], deliveries: DeliveryLog,
    index: MessageIndex, now: datetime,
) -> dict[int, dict[int, AlertItem]]:
    """Message id -> numbered bets for replies to an alert or board whose send was never confirmed (delivery audit
    2026-10-05). A send that timed out after Telegram had delivered it left the message unindexed, so a "placed"
    reply to it was refused as "not an alert I sent". Telegram quotes the replied-to text: when it equals an
    unconfirmed delivery's text, the reply proves the message arrived, so the message is indexed and the delivery
    confirmed ("confirmed_by_reply"). Only replies from `chat_id` count."""
    unconfirmed = deliveries.unconfirmed_by_text(now)
    found: dict[int, dict[int, AlertItem]] = {}
    for update in updates:
        message: Any = update.get("message") if isinstance(update, dict) else None
        chat: Any = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict) or str(chat.get("id")) != chat_id:
            continue
        replied: Any = message.get("reply_to_message")
        replied_id = replied.get("message_id") if isinstance(replied, dict) else None
        quoted = replied.get("text") if isinstance(replied, dict) else None
        if not isinstance(replied_id, int) or replied_id in messages or replied_id in found \
                or not isinstance(quoted, str):
            continue
        delivery = unconfirmed.get(quoted.strip())
        if delivery is None or not delivery.items:
            continue
        index.record(replied_id, delivery.items, now)
        deliveries.resolve(delivery.delivery_id, "confirmed_by_reply", now, replied_id)
        found[replied_id] = {item.number: item for item in delivery.items}
        logger.warning("delivery #%d confirmed by a reply to message %d", delivery.delivery_id, replied_id)
    return found


def poll(
    notifier: TelegramNotifier, index: MessageIndex, store: PlacedBetStore, offset_file: Path, now: datetime,
    currency: str = "", book_rate: ExchangeRate | None = None, *, outbox: AnswerOutbox | None = None,
    deliveries: DeliveryLog | None = None, heartbeat_file: Path | None = None,
    answer_command: Callable[[str, datetime], str] | None = None,
    pilot_store: PlacedBetStore | None = None, pilot_lock: Path | None = None,
) -> int:
    """Read new replies, record bets, answer each in the chat. Returns the number of answers sent.

    With an `outbox`, every answer is queued before the offset is saved and an unsent one is re-sent by the next
    poll; with `deliveries`, a reply to an alert whose send was never confirmed still records its bet
    (confirm_quoted_deliveries); with a `heartbeat_file`, the time Telegram was read is saved for the status report;
    with `answer_command` (command name, now) -> text, each "/command" is answered. Command answers are built only after
    the offset is saved and the bet answers are queued, so one that fails cannot make the poll re-read the same messages
    on every run (the run fails and task_health reports it; the queued bet answers go out with the next poll).
    New answers go out before re-sent ones, so one answer Telegram keeps refusing cannot hold back the rest."""
    if notifier.chat_id is None:
        raise ValueError("TELEGRAM_CHAT_ID is not set; run python -m prop_alpha.alerts.telegram --get-chat-id")
    offset: int | None = None
    if offset_file.is_file():
        raw: Any = json.loads(offset_file.read_text(encoding="utf-8"))
        if not (isinstance(raw, dict) and isinstance(raw.get("offset"), int)):
            raise ValueError(f"{offset_file.name} is not an offset record")
        offset = int(raw["offset"])
    earlier = outbox.unsent() if outbox is not None else []
    updates = notifier.get_updates(offset)
    messages: dict[int, Mapping[int, AlertItem]] = dict(index.load())
    if deliveries is not None:
        messages.update(confirm_quoted_deliveries(updates, notifier.chat_id, messages, deliveries, index, now))
    commands: list[tuple[int, str]] = []
    replies, next_offset = process_updates(updates, notifier.chat_id, messages, store, now, currency, book_rate,
                                           bot_commands=commands if answer_command is not None else None,
                                           pilot_store=pilot_store, pilot_lock=pilot_lock)
    queued = [(outbox.queue(reply_to, text, now) if outbox is not None else 0, reply_to, text)
              for reply_to, text in replies]
    if next_offset is not None:
        # Saved before answering: the bets are already recorded, and a failed answer must not re-read them.
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        offset_file.write_text(json.dumps({"offset": next_offset}), encoding="utf-8")
    if heartbeat_file is not None:
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_file.write_text(json.dumps({"at": now.isoformat()}), encoding="utf-8")
    if answer_command is not None:
        for command_message, name in commands:
            text = answer_command(name, now)
            queued.append((outbox.queue(command_message, text, now) if outbox is not None else 0, command_message, text))
    retries = [(answer_id, reply_to, (DELAYED_PREFIX if reply_to is not None else DELAYED_NOTICE_PREFIX) + text)
               for answer_id, reply_to, text, attempts in earlier if attempts < MAX_ANSWER_ATTEMPTS]
    for answer_id, reply_to, text in [*queued, *retries]:
        if outbox is not None:
            outbox.attempt(answer_id, now)
        message_id = notifier.send_message(text, reply_to=reply_to)
        if outbox is not None:
            outbox.mark_sent(answer_id, message_id, now)
    return len(queued) + len(retries)


def send_queued_notice(notifier: TelegramNotifier, outbox: AnswerOutbox, text: str, at: datetime) -> int:
    """Queue a notice that answers no message, then send it; returns Telegram's message id. Delivery audit 2026-10-05
    (third pass): --settle --send recorded each settlement and only then sent BETS SETTLED. A failed send was never
    repeated, because the next run finds nothing new to settle, so the maintainer never saw those automatic results
    to check against the bookmaker. Queued first, a failed notice is re-sent by the next poll (DELAYED_NOTICE_PREFIX)
    up to MAX_ANSWER_ATTEMPTS, then reported by the status report and --list."""
    answer_id = outbox.queue(None, text, at)
    outbox.attempt(answer_id, at)
    message_id = notifier.send_message(text)
    outbox.mark_sent(answer_id, message_id, at)
    return message_id


def settle_open_bets(
    open_bets: Sequence[PlacedBet],
    rows: Mapping[tuple[int, int], tuple[str, int | None, int | None, int | None] | None],
    now: datetime,
) -> list[tuple[PlacedBet, Settlement]]:
    """Pure: grade each open bet from its database row, keyed by (fixture id, player id) and holding (fixture
    status, minutes, tackles, fouls), or None when the match was never ingested. Ungradable bets stay open."""
    settled: list[tuple[PlacedBet, Settlement]] = []
    columns = tuple(SETTLEMENT_COLUMNS)
    for bet in open_bets:
        row = rows.get((bet.fixture_id, bet.player_id))
        if row is None:
            continue
        status, minutes, *counts = row
        count = counts[columns.index(bet.market)] if bet.market in columns else None
        settlement = grade(bet, status, minutes, count, now)
        if settlement is not None:
            settled.append((bet, settlement))
    return settled


def _nullable_int(value: Any) -> int | None:
    return None if value is None else int(value)


def read_settlement_rows(
    dsn: str, bets: Sequence[PlacedBet], settlement_sql: str = SETTLEMENT_SQL,
) -> dict[tuple[int, int], tuple[str, int | None, int | None, int | None] | None]:
    """One read-only transaction: each bet's fixture status and its player's stored minutes and counts
    (`settlement_sql`: SETTLEMENT_SQL, or the pilot's, which counts tackles its own way)."""
    import psycopg2

    rows: dict[tuple[int, int], tuple[str, int | None, int | None, int | None] | None] = {}
    if not bets:
        return rows
    conn = psycopg2.connect(dsn, options="-c statement_timeout=10000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                for bet in bets:
                    # The stored vendor uid of the bet's match: the Sportmonks fixture id (again since 2026-10-21; a bet
                    # made on TheStatsAPI data, 2026-10-15 to 2026-10-20, finds no row and stays open, flagged after
                    # STALE_AFTER: none was placed then).
                    cur.execute(settlement_sql, (bet.player_id, str(bet.fixture_id)))
                    found = cur.fetchone()
                    rows[(bet.fixture_id, bet.player_id)] = None if found is None else (
                        str(found[0]), _nullable_int(found[1]), _nullable_int(found[2]), _nullable_int(found[3])
                    )
    finally:
        conn.close()
    return rows


def settle(store: PlacedBetStore, dsn: str, now: datetime,
           settlement_sql: str = SETTLEMENT_SQL) -> list[tuple[PlacedBet, Settlement]]:
    """Record an automatic result for every open bet whose match is final in the database."""
    open_bets = store.open_bets(now)
    settled = settle_open_bets(open_bets, read_settlement_rows(dsn, open_bets, settlement_sql), now)
    for _, settlement in settled:
        store.record_settlement(settlement)
    return settled


def reconcile_settlements(store: PlacedBetStore, dsn: str, now: datetime,
                          settlement_sql: str = SETTLEMENT_SQL) -> list[tuple[PlacedBet, Reconciliation]]:
    """Every settled bet compared with automatic settlement on the statistics stored now (replies.reconcile), from one
    read-only transaction. Bets settled only automatically need no query."""
    settled = store.settled_bets()
    confirmed = [bet for bet, settlement in settled if settlement.source == "manual"]
    rows = read_settlement_rows(dsn, confirmed, settlement_sql)
    automatic = store.automatic_settlements()
    return [(bet, reconcile(bet, settlement, rows.get((bet.fixture_id, bet.player_id)), automatic.get(bet.bet_id), now))
            for bet, settlement in settled]


def closing_values(settled: Sequence[tuple[PlacedBet, Settlement]],
                   quotes: Sequence[Mapping[str, Any]]) -> dict[int, ClosingValue]:
    values = {bet.bet_id: closing_value(bet, quotes) for bet, _ in settled}
    return {bet_id: value for bet_id, value in values.items() if value is not None}


def closing_value(bet: PlacedBet, quotes: Sequence[Mapping[str, Any]]) -> ClosingValue | None:
    """Pure: the bet's closing-line value from the captured closing quotes (alerts/closing.py) for its fixture,
    player, market and line, preferring the book it was placed with, then the latest capture. None when no two-way
    closing price was captured (CLV needs both sides to devig)."""
    candidates = [q for q in quotes
                  if (q["fixture_id"], q["player_id"], q["market"], float(q["line"]))
                  == (bet.fixture_id, bet.player_id, bet.market, bet.line)
                  and q["over"] is not None and q["under"] is not None]
    if not candidates:
        return None
    quote = max(candidates, key=lambda q: (str(q["book"]).lower() == bet.book.lower(), str(q["captured_at"])))
    taken, other = (quote["over"], quote["under"]) if bet.side == "over" else (quote["under"], quote["over"])
    return ClosingValue(closing_line_value(bet.odds, float(taken), float(other)), float(taken), str(quote["book"]))


def results_summary(settled: Sequence[tuple[PlacedBet, Settlement]],
                    closing: Mapping[int, ClosingValue] | None = None) -> str:
    """Count, stakes, profit and return on stake of the settled bets, and their mean closing-line value."""
    staked = sum(bet.stake for bet, _ in settled)
    total = sum(profit(bet, settlement) for bet, settlement in settled)
    by_result = {result: sum(1 for _, s in settled if s.result == result) for result in RESULTS}
    roi = f", return on stake {total / staked:+.1%}" if staked > 0 else ""
    provisional = sum(1 for _, s in settled if s.source == "auto")
    # Automatic results rest on the assumed market contract until bookmakers' settlements confirm it (readiness item
    # 2): they are kept out of a separate confirmed-only total, which is the one a performance claim may quote.
    confirmed = [(bet, s) for bet, s in settled if s.source != "auto"]
    caveat = (f"; {provisional} provisional (automatic, assumed market rules; not validated results); confirmed "
              f"only: {len(confirmed)} bet(s), staked {sum(bet.stake for bet, _ in confirmed):.2f}, profit "
              f"{sum(profit(bet, s) for bet, s in confirmed):+.2f}" if provisional else "")
    values = [closing[bet.bet_id].clv for bet, _ in settled if closing and bet.bet_id in closing]
    clv = (f"; mean CLV {sum(values) / len(values):+.1%} over {len(values)} bet(s) with a closing price" if values
           else "; no closing price captured yet")
    return (f"{len(settled)} settled bet(s) ({by_result['won']} won, {by_result['lost']} lost, {by_result['void']} "
            f"void): staked {staked:.2f}, profit {total:+.2f}{roi}{caveat}{clv}")


@contextmanager
def ledger_lock(path: Path = DEFAULT_LOCK_FILE) -> Iterator[None]:
    """Hold an exclusive OS lock for a run that writes the ledger (--poll, --settle). Delivery audit 2026-10-05: the
    scheduled task runs one instance at a time, but a manual --poll during it could read the same Telegram update in
    both runs before either recorded it, recording one bet twice. The lock is released by the OS if the process dies,
    so a crash never leaves it stuck. A second writer fails at once (OSError from the lock call), recorded by the
    scheduled script as a failed step, rather than waiting or writing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("ledger lock taken (%s)", path.name)
        yield
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Record manually placed bets from Telegram replies.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--poll", action="store_true", help="read new replies, record bets, answer in the chat")
    action.add_argument("--list", action="store_true", help="print the open bets and the settled results")
    action.add_argument("--settle", action="store_true", help="settle open bets whose match is final")
    action.add_argument("--reconcile", action="store_true",
                        help="compare your settled replies with automatic settlement; exits 1 on any discrepancy")
    parser.add_argument("--send", action="store_true",
                        help="with --settle: send the new results; with --reconcile: send the report")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    store = PlacedBetStore()
    if args.list:
        open_bets = store.open_bets(now)
        stale = {bet.bet_id for bet in store.stale_bets(now)}
        for bet in open_bets:
            flag = ("  BELOW MINIMUM" if bet.below_minimum else "") + ("  STALE: settle by hand" if bet.bet_id in stale else "")
            print(f"#{bet.bet_id} {bet.placed_at:%d %b %H:%M} {bet.match} | {bet.player} | {bet.side.upper()} "
                  f"{bet.line:g} {bet.market} @ {bet.odds:.2f} stake {bet.stake:g} ({bet.book}){flag}")
        print(f"{len(open_bets)} open bet(s), total stake {sum(b.stake for b in open_bets):g}")
        settled = store.settled_bets()
        closing = closing_values(settled, load_closing_quotes(STATE_DIR))
        for bet, settlement in settled:
            print(settlement_text(bet, settlement, closing.get(bet.bet_id)))
        print(results_summary(settled, closing))
        for answer_id, reply_to, text, attempts in AnswerOutbox().abandoned():
            target = f"to your message {reply_to}" if reply_to is not None else "(a notice)"
            print(f"UNDELIVERED answer #{answer_id} {target} ({attempts} failed sends): {text}")
        return 0
    if args.settle:
        with ledger_lock():
            settled = settle(store, os.environ.get(DSN_ENV, DEFAULT_DSN), now)
            closing = closing_values(settled, load_closing_quotes(STATE_DIR))
            lines = [settlement_text(bet, settlement, closing.get(bet.bet_id)) for bet, settlement in settled]
            print("\n".join(lines) if lines else "no open bet has a final match in the database")
            if args.send and lines:
                notice = "\n".join(
                    [SETTLED_HEADER, *lines,
                     results_summary(store.settled_bets(), closing_values(store.settled_bets(),
                                                                         load_closing_quotes(STATE_DIR)))])
                send_queued_notice(TelegramNotifier(load_config()), AnswerOutbox(), notice, now)
        return 0
    if args.reconcile:
        items = reconcile_settlements(store, os.environ.get(DSN_ENV, DEFAULT_DSN), now)
        lines = [reconciliation_text(bet, item) for bet, item in items if item.outcome != "unconfirmed"]
        report = "\n".join([*lines, reconciliation_summary([item for _, item in items])])
        print(report)
        if args.send:
            TelegramNotifier(load_config()).send_message("SETTLEMENT CHECK\n" + report)
        return 1 if any(item.outcome in RECONCILE_FAILURES for _, item in items) else 0
    currency = bankroll_currency()
    rate = None if currency == BOOK_CURRENCY else cached_rate()
    fresh = rate if rate is not None and is_fresh(rate, now.date()) else None
    # Imported here: bot_commands builds the status report, and alerts/status.py imports this module.
    from prop_alpha.alerts.bot_commands import answer
    from prop_alpha.alerts.pilot_ledger import PILOT_BETS_FILE, lock_for

    pilot_store = PlacedBetStore(PILOT_BETS_FILE)

    with ledger_lock():
        sent = poll(TelegramNotifier(load_config()), MessageIndex(), store, DEFAULT_OFFSET_FILE, now, currency, fresh,
                    outbox=AnswerOutbox(), deliveries=DeliveryLog(), heartbeat_file=DEFAULT_HEARTBEAT_FILE,
                    answer_command=answer, pilot_store=pilot_store, pilot_lock=lock_for(pilot_store))
    print(f"answered {sent} message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
