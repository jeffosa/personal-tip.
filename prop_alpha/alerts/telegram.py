"""Telegram alerts for staking directives. Sends messages only; it never places a bet.

Credentials come from the environment or from a gitignored `.env` file at the repository root:

    TELEGRAM_BOT_TOKEN=<token from @BotFather>
    TELEGRAM_CHAT_ID=<your chat id>

Telegram puts the token in the request URL, and `requests` error messages quote that URL, so every
transport error is re-raised as TelegramError without the URL (sanctioned handler H6). The token never
appears in a repr, log line or exception message.

Only high-value bets are sent (prop_alpha.scanner.value_screen): mispriced by the book, at least 4%
expected profit per unit staked, and an edge that still returns at least 4% in more than 60% of 1,000
simulated match conditions, all after the model-risk loss floor (MODEL_RISK_LOSS_FLOOR).

One alert is sent per market, line, side and book, whatever the price does afterwards, so a person
following the alerts is not told to stack the same position on every price tick. With a SentAlertStore
(default `.prop_alpha_state/sent_alerts.jsonl`, gitignored) this holds across runs, and
SentAlertStore.exposure() gives the allocator the stakes already suggested, so a second run sizes net of
the first. Every recent alert counts as an open position; prop_alpha.alerts.ledger adds the bets the maintainer
records by replying, taking the larger of alerted and placed stake per market. Each sent alert message is indexed
(MessageIndex) so that a reply resolves to one numbered bet.

Usage:
    python -m prop_alpha.alerts.telegram --check        # getMe + one test message
    python -m prop_alpha.alerts.telegram --get-chat-id  # list chats that have messaged the bot
    python -m prop_alpha.alerts.telegram --demo         # synthetic directives, screened, labelled
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

import numpy as np
import requests

from prop_alpha.alerts.replies import USAGE, AlertItem
from prop_alpha.config import DEFAULT_ENV_FILE, parse_env_file
from prop_alpha.modeling.engine import FactorProjection, LineupExpectation, QuantEngine, RefereeProfile, VoidRule
from prop_alpha.scanner.market_scanner import (
    MODEL_RISK_LOSS_FLOOR,
    AllocationReport,
    ExposureBook,
    InstitutionalAssetManager,
    LiveOddsQuote,
    ProjectionKey,
    StakingDirective,
    price_for_ev,
)
from prop_alpha.scanner.value_screen import HighValueAssessment, HighValueCriteria, assess_high_value

logger = logging.getLogger("prop_alpha.alerts")

TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
TOKEN_ENV: Final[str] = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV: Final[str] = "TELEGRAM_CHAT_ID"
DEFAULT_STATE_FILE: Final[Path] = Path(__file__).resolve().parents[2] / ".prop_alpha_state" / "sent_alerts.jsonl"
# Telegram message id -> the numbered bets it lists, so a reply ("placed 3 @1.95 5") resolves to one bet.
DEFAULT_INDEX_FILE: Final[Path] = DEFAULT_STATE_FILE.parent / "alert_messages.jsonl"
# How long an alerted stake counts as open exposure. Player props settle within a day or two of kickoff and
# alerts go out before kickoff; 4 days over-counts rather than under-counts until the ledger records fills.
OPEN_POSITION_WINDOW: Final[timedelta] = timedelta(days=4)
# Telegram allows about one message per second per chat and answers 429 with parameters.retry_after.
MIN_SEND_INTERVAL_S: Final[float] = 1.1
MAX_TELEGRAM_RETRY_AFTER_S: Final[float] = 30.0
PAPER_LABEL: Final[str] = "PAPER ONLY - model not validated, no PRD go-live gate passed. Not betting advice."
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
_CHAT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,})$")

AlertKey = tuple[int, int, str, float, str, str]
# High-value bets per digest message (10 bets arrive as two messages of 5).
DIGEST_SIZE: Final[int] = 5
SeedKey = tuple[int, int, str, float, str, str, float]
DEFAULT_CRITERIA: Final[HighValueCriteria] = HighValueCriteria(loss_floor=MODEL_RISK_LOSS_FLOOR)


class TelegramError(RuntimeError):
    """A Telegram call failed. The message never contains the bot token."""


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str | None = None

    def __post_init__(self) -> None:
        if not _TOKEN_PATTERN.match(self.bot_token):
            raise ValueError(f"{TOKEN_ENV} is missing or not a bot token (expected <digits>:<secret>)")
        if self.chat_id is not None and not _CHAT_ID_PATTERN.match(self.chat_id):
            raise ValueError(f"{CHAT_ID_ENV} is not a chat id (a number, or @channelname)")

    @classmethod
    def from_sources(cls, environ: Mapping[str, str], env_file_text: str | None) -> TelegramConfig:
        """Process environment first, then the .env file. The file is parsed only when the environment lacks
        a value, so a malformed line elsewhere in .env cannot stop alerts whose credentials are set."""
        complete = bool(environ.get(TOKEN_ENV)) and bool(environ.get(CHAT_ID_ENV))
        file_values = parse_env_file(env_file_text) if env_file_text is not None and not complete else {}
        return cls(
            bot_token=environ.get(TOKEN_ENV) or file_values.get(TOKEN_ENV, ""),
            chat_id=environ.get(CHAT_ID_ENV) or file_values.get(CHAT_ID_ENV) or None,
        )


def load_config(env_file: Path = DEFAULT_ENV_FILE) -> TelegramConfig:
    text = env_file.read_text(encoding="utf-8") if env_file.is_file() else None
    return TelegramConfig.from_sources(os.environ, text)


@dataclass(frozen=True)
class SentAlert:
    key: AlertKey
    stake: float
    sent_at: datetime

    def __post_init__(self) -> None:
        _, _, market_type, line_value, side, sportsbook = self.key
        if not market_type or not sportsbook or side not in ("over", "under") or not math.isfinite(line_value):
            raise ValueError(f"malformed alert key {self.key!r}")
        if not (math.isfinite(self.stake) and self.stake >= 0.0):
            raise ValueError("alert stake must be finite and non-negative")
        if self.sent_at.tzinfo is None or self.sent_at.utcoffset() != timedelta(0):
            raise ValueError("sent_at must be a timezone-aware UTC datetime")


class SentAlertStore:
    """Append-only JSON-lines record of every alert, read at start-up. A corrupt line raises: guessing what
    was sent could double a position."""

    def __init__(self, path: Path = DEFAULT_STATE_FILE) -> None:
        self.path = path

    def load(self) -> list[SentAlert]:
        if not self.path.is_file():
            return []
        alerts: list[SentAlert] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record: Any = json.loads(line)
            key: Any = record.get("key") if isinstance(record, dict) else None
            if not (
                isinstance(key, list) and len(key) == 6
                and all(isinstance(part, int) and not isinstance(part, bool) for part in key[:2])
                and all(isinstance(part, str) for part in (key[2], key[4], key[5]))
                and isinstance(key[3], (int, float)) and not isinstance(key[3], bool)
                and isinstance(record.get("stake"), (int, float)) and isinstance(record.get("sent_at"), str)
            ):
                raise ValueError(f"{self.path.name} line {number} is not a sent-alert record")
            alerts.append(SentAlert(
                key=(int(key[0]), int(key[1]), str(key[2]), float(key[3]), str(key[4]), str(key[5])),
                stake=float(record["stake"]),
                sent_at=datetime.fromisoformat(str(record["sent_at"])),
            ))
        return alerts

    def append(self, alert: SentAlert) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"key": list(alert.key), "stake": alert.stake, "sent_at": alert.sent_at.isoformat()})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def exposure(self, now: datetime, window: timedelta = OPEN_POSITION_WINDOW) -> ExposureBook:
        """Stakes alerted within `window` before `now`, per market: pass this to allocate() as `exposure`."""
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be a timezone-aware UTC datetime")
        totals: dict[ProjectionKey, float] = defaultdict(float)
        for alert in self.load():
            if alert.sent_at > now - window:
                totals[(alert.key[0], alert.key[1], alert.key[2])] += alert.stake
        return ExposureBook(dict(totals))


def _item_record(item: AlertItem) -> dict[str, Any]:
    return {
        "number": item.number, "kind": item.kind, "fixture_id": item.fixture_id, "player_id": item.player_id,
        "market": item.market, "line": item.line, "min_odds": [list(pair) for pair in item.min_odds],
        "match": item.match, "player": item.player, "book": item.book,
    }


def _item_from_record(raw: Any) -> AlertItem:
    if not isinstance(raw, dict):
        raise ValueError("indexed item is not an object")
    return AlertItem(
        number=int(raw["number"]), kind=str(raw["kind"]), fixture_id=int(raw["fixture_id"]),
        player_id=int(raw["player_id"]), market=str(raw["market"]), line=float(raw["line"]),
        min_odds=tuple((str(side), float(odds)) for side, odds in raw["min_odds"]),
        match=str(raw["match"]), player=str(raw["player"]),
        book=None if raw.get("book") is None else str(raw["book"]),
    )


class MessageIndex:
    """Append-only JSON-lines map from each sent alert or board message to the numbered bets it lists."""

    def __init__(self, path: Path = DEFAULT_INDEX_FILE) -> None:
        self.path = path

    def record(self, message_id: int, items: Sequence[AlertItem], sent_at: datetime) -> None:
        if sent_at.tzinfo is None or sent_at.utcoffset() != timedelta(0):
            raise ValueError("sent_at must be a timezone-aware UTC datetime")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"message_id": message_id, "sent_at": sent_at.isoformat(),
                           "items": [_item_record(item) for item in items]})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> dict[int, dict[int, AlertItem]]:
        """message id -> {item number -> item}. A corrupt line raises: a guessed mapping could record the wrong bet."""
        if not self.path.is_file():
            return {}
        messages: dict[int, dict[int, AlertItem]] = {}
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record: Any = json.loads(line)
            if not (isinstance(record, dict) and isinstance(record.get("message_id"), int)
                    and isinstance(record.get("items"), list)):
                raise ValueError(f"{self.path.name} line {number} is not a message-index record")
            items = [_item_from_record(raw) for raw in record["items"]]
            messages[int(record["message_id"])] = {item.number: item for item in items}
        return messages


def item_from_directive(number: int, directive: StakingDirective, context: AlertContext | None) -> AlertItem:
    return AlertItem(
        number=number, kind="alert", fixture_id=directive.fixture_id, player_id=directive.player_id,
        market=directive.market_type, line=directive.line_value,
        min_odds=((directive.side, directive.min_acceptable_odds),),
        match=context.match if context else f"Fixture {directive.fixture_id}",
        player=context.player if context else f"player {directive.player_id}",
        book=directive.sportsbook,
    )


def _flood_wait_seconds(body: Any) -> float | None:
    """Seconds Telegram asks to wait on a 429 (parameters.retry_after), else None."""
    if not isinstance(body, dict) or body.get("error_code") != 429:
        return None
    parameters: Any = body.get("parameters")
    retry_after: Any = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)) or retry_after < 0:
        return None
    return float(retry_after)


def alert_key(directive: StakingDirective) -> AlertKey:
    """One alert per market/line/side/book per process, whatever the price does afterwards."""
    return (
        directive.fixture_id, directive.player_id, directive.market_type,
        directive.line_value, directive.side, directive.sportsbook,
    )


def simulation_seed(directive: StakingDirective) -> int:
    """Stable seed per directive and price (sha256, not hash(), which is salted per process), so re-runs agree."""
    key: SeedKey = (*alert_key(directive), directive.decimal_odds)
    return int.from_bytes(hashlib.sha256(repr(key).encode("utf-8")).digest()[:8], "big")


@dataclass(frozen=True)
class AlertContext:
    """Readable names for one projection key: the match ("Walsall v Port Vale, Sat 10 Oct 14:00 UTC") and the
    player with his club ("Alex Hunt (York City)")."""

    match: str
    player: str


def format_digest(
    entries: Sequence[tuple[StakingDirective, HighValueAssessment, AlertContext | None]],
    first_number: int,
    total: int,
    part: int,
    parts: int,
    label: str = "",
    bankroll_currency: str = "",
) -> str:
    """One plain-text message listing several high-value bets, numbered from `first_number` of `total`."""
    currency = f"{bankroll_currency} " if bankroll_currency else ""
    last = first_number + len(entries) - 1
    lines = [
        *((label,) if label else ()),
        PAPER_LABEL,
        f"HIGH-VALUE BETS {first_number}-{last} of {total}" + (f" (message {part} of {parts})" if parts > 1 else ""),
    ]
    for number, (directive, assessment, context) in enumerate(entries, first_number):
        lines += [
            "",
            f"{number}. {context.match if context else f'Fixture {directive.fixture_id}'}",
            f"   Player: {context.player if context else f'player {directive.player_id}'}",
            f"   Bet: {directive.side.upper()} {directive.line_value:g} {directive.market_type} "
            f"@ {directive.decimal_odds:.2f} ({directive.sportsbook})",
            f"   Take it only at {directive.min_acceptable_odds:.2f} or better, until {directive.expires_at:%H:%M} UTC",
            f"   Edge: {assessment.ev_per_unit:+.1%} expected profit | model {assessment.model_win_probability:.0%} "
            f"v book {assessment.book_win_probability:.0%} | held in {assessment.survival_rate:.0%} of "
            f"{assessment.n_simulations:,} simulations",
            f"   Stake: {currency}{directive.stake:.2f} ({directive.fraction:.1%} of bankroll)",
        ]
    lines += ["", USAGE]
    return "\n".join(lines)


def format_directive(
    directive: StakingDirective,
    assessment: HighValueAssessment,
    bankroll_currency: str = "",
    label: str = "",
    context: AlertContext | None = None,
) -> str:
    """Plain-text alert (no parse_mode, so nothing needs escaping). `label` (e.g. MOCK ODDS) heads the message;
    `context` (match and player names) replaces the bare ids."""
    currency = f"{bankroll_currency} " if bankroll_currency else ""
    return "\n".join((
        *((label,) if label else ()),
        PAPER_LABEL,
        "HIGH-VALUE BET",
        f"{context.match} | {context.player} | {directive.market_type}" if context
        else f"Fixture {directive.fixture_id} | player {directive.player_id} | {directive.market_type}",
        f"{directive.side.upper()} {directive.line_value:g} @ {directive.decimal_odds:.3f} "
        f"({directive.sportsbook}, {directive.void_rule})",
        f"Do not bet below {directive.min_acceptable_odds:.3f}; valid until {directive.expires_at:%H:%M:%S} UTC",
        f"Mispriced: model win chance {assessment.model_win_probability:.1%} vs book "
        f"{assessment.book_win_probability:.1%}",
        f"Expected profit {assessment.ev_per_unit:+.2%} per 1 staked",
        f"Edge held in {assessment.surviving_simulations} of {assessment.n_simulations} simulated match "
        f"conditions ({assessment.survival_rate:.1%})",
        f"Suggested stake {currency}{directive.stake:.2f} ({directive.fraction:.3%} of bankroll)",
    ))


class TelegramNotifier:
    """Sends one message per new directive. Without a store it remembers what it sent for its own lifetime
    only (the synthetic demo); with one, across runs."""

    def __init__(
        self,
        config: TelegramConfig,
        session: Any | None = None,
        timeout_s: float = 10.0,
        store: SentAlertStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        index: MessageIndex | None = None,
    ) -> None:
        if not timeout_s > 0.0:
            raise ValueError("timeout_s must be positive")
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        self._last_send: float | None = None
        self._store = store
        self._sent: set[AlertKey] = {alert.key for alert in store.load()} if store is not None else set()
        # Where sent alert and board messages are indexed for bet-recording replies; None records nothing.
        self.index = index

    def _call(self, method: str, payload: Mapping[str, Any], *, retry_on_flood: bool = True) -> Any:
        url = f"{TELEGRAM_API_BASE}/bot{self._config.bot_token}/{method}"
        try:
            response = self._session.post(url, json=dict(payload), timeout=self._timeout_s)
            body = response.json()
        except requests.RequestException as exc:
            raise TelegramError(f"{method} failed: {type(exc).__name__} (details withheld: they contain the token)") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            description = body.get("description", "no description") if isinstance(body, dict) else "malformed reply"
            retry_after = _flood_wait_seconds(body)
            if retry_on_flood and retry_after is not None and retry_after <= MAX_TELEGRAM_RETRY_AFTER_S:
                logger.warning("Telegram rate limit on %s; waiting %.0fs and retrying once", method, retry_after)
                self._sleep(retry_after)
                return self._call(method, payload, retry_on_flood=False)
            raise TelegramError(f"{method} rejected by Telegram (HTTP {response.status_code}): {description}")
        return body["result"]

    def check_connection(self) -> str:
        """Bot username from getMe; raises TelegramError if the token is wrong or Telegram is unreachable."""
        result = self._call("getMe", {})
        return str(result["username"])

    def recent_chats(self) -> list[tuple[str, str]]:
        """(chat id, title or name) for chats that have recently messaged the bot."""
        chats: dict[str, str] = {}
        for update in self._call("getUpdates", {"limit": 100}):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat")
            if chat:
                name = chat.get("title") or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
                chats[str(chat["id"])] = name or str(chat.get("username", ""))
        return sorted(chats.items())

    def send_message(self, text: str, reply_to: int | None = None) -> int:
        """Send `text` to the configured chat, as a reply to message `reply_to` when given."""
        if not text.strip():
            raise ValueError("message text must not be empty")
        if self._config.chat_id is None:
            raise ValueError(f"{CHAT_ID_ENV} is not set; run with --get-chat-id to find it")
        if self._last_send is not None:
            wait = MIN_SEND_INTERVAL_S - (self._clock() - self._last_send)
            if wait > 0.0:
                self._sleep(wait)
        self._last_send = self._clock()
        payload: dict[str, Any] = {"chat_id": self._config.chat_id, "text": text[:4096], "disable_web_page_preview": True}
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def get_updates(self, offset: int | None) -> list[Any]:
        """New messages to the bot from `offset` on (Telegram drops updates below the offset). Never blocks."""
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramError("getUpdates returned no update list")
        return result

    @property
    def chat_id(self) -> str | None:
        return self._config.chat_id

    def notify(
        self,
        report: AllocationReport,
        projections: Mapping[ProjectionKey, FactorProjection],
        criteria: HighValueCriteria = DEFAULT_CRITERIA,
        bankroll_currency: str = "",
        label: str = "",
        descriptions: Mapping[ProjectionKey, AlertContext] | None = None,
        max_alerts: int | None = None,
    ) -> list[int]:
        """Send each directive in report.directives whose market/line/side/book has not been alerted yet
        and that passes the high-value screen.

        Sub-minimum stakes (report.skipped_directives) and skipped markets are never screened or sent.
        A directive without its projection cannot be screened: it is logged at ERROR and skipped, and
        the rest of the report is still screened (maintainer decision 5, 2026-10-01). A failed send
        raises TelegramError; directives sent before it stay marked as sent.

        With a store, each alert is recorded before it is sent: if the send then fails, the alert is lost
        rather than repeated on the next run, because a repeat could double a position.
        """
        message_ids: list[int] = []
        for directive, assessment in self._high_value(report, projections, criteria, max_alerts):
            context = (descriptions or {}).get((directive.fixture_id, directive.player_id, directive.market_type))
            text = format_directive(directive, assessment, bankroll_currency, label, context)
            self._record(directive)
            message_ids.append(self.send_message(text))
            logger.info("alert sent for %s", alert_key(directive))
        return message_ids

    def notify_digest(
        self,
        report: AllocationReport,
        projections: Mapping[ProjectionKey, FactorProjection],
        criteria: HighValueCriteria = DEFAULT_CRITERIA,
        bankroll_currency: str = "",
        label: str = "",
        descriptions: Mapping[ProjectionKey, AlertContext] | None = None,
        max_alerts: int | None = None,
        per_message: int = DIGEST_SIZE,
    ) -> tuple[int, list[int]]:
        """As notify, but the high-value bets go out as numbered lists of at most `per_message` per message
        (10 bets: two messages of 5), in report order. Returns (bets alerted, message ids). Every bet of a
        message is recorded before that message is sent."""
        if per_message < 1:
            raise ValueError("per_message must be at least 1")
        passing = list(self._high_value(report, projections, criteria, max_alerts))
        parts = -(-len(passing) // per_message)
        message_ids: list[int] = []
        for part in range(parts):
            chunk = passing[part * per_message:(part + 1) * per_message]
            entries = [
                (d, a, (descriptions or {}).get((d.fixture_id, d.player_id, d.market_type))) for d, a in chunk
            ]
            text = format_digest(entries, part * per_message + 1, len(passing), part + 1, parts, label, bankroll_currency)
            for directive, _ in chunk:
                self._record(directive)
            message_id = self.send_message(text)
            message_ids.append(message_id)
            if self.index is not None:
                self.index.record(message_id, [
                    item_from_directive(number, directive, context)
                    for number, (directive, _, context) in enumerate(entries, part * per_message + 1)
                ], datetime.now(timezone.utc))
            logger.info("digest message %d/%d sent with %d bets", part + 1, parts, len(chunk))
        return len(passing), message_ids

    def _record(self, directive: StakingDirective) -> None:
        if self._config.chat_id is None:
            raise ValueError(f"{CHAT_ID_ENV} is not set; run with --get-chat-id to find it")
        if self._store is not None:
            self._store.append(SentAlert(alert_key(directive), directive.stake, datetime.now(timezone.utc)))
        self._sent.add(alert_key(directive))

    def _high_value(
        self,
        report: AllocationReport,
        projections: Mapping[ProjectionKey, FactorProjection],
        criteria: HighValueCriteria,
        max_alerts: int | None,
    ) -> list[tuple[StakingDirective, HighValueAssessment]]:
        """Directives not alerted before that pass the high-value screen, at most `max_alerts`, in report order."""
        if max_alerts is not None and max_alerts < 0:
            raise ValueError("max_alerts must be non-negative")
        passing: list[tuple[StakingDirective, HighValueAssessment]] = []
        for directive in report.directives:
            if max_alerts is not None and len(passing) >= max_alerts:
                logger.info("alert cap of %d reached; remaining directives not screened", max_alerts)
                break
            key = alert_key(directive)
            if key in self._sent or any(alert_key(d) == key for d, _ in passing):
                continue
            projection = projections.get((directive.fixture_id, directive.player_id, directive.market_type))
            if projection is None:
                logger.error("no projection for directive %s; it cannot be screened and was not sent", key)
                continue
            assessment = assess_high_value(
                directive, projection, criteria, np.random.default_rng(simulation_seed(directive))
            )
            if not assessment.is_high_value:
                logger.info(
                    "not high value %s: mispriced=%s ev=%.4f survived %d/%d",
                    key, assessment.mispriced, assessment.ev_per_unit,
                    assessment.surviving_simulations, assessment.n_simulations,
                )
                continue
            passing.append((directive, assessment))
        return passing


def reference_projection() -> FactorProjection:
    """The documented reference projection (ARCHITECTURE 4.1): tackles, fixture 1001, player 55."""
    return QuantEngine(game_state_k=6.0).factor_count_market(
        fixture_id=1001, player_id=55, market_type="tackles",
        counts=[3, 2, 4, 3, 1, 3], minutes=[90.0, 90.0, 90.0, 85.0, 90.0, 90.0],
        league_rate_per90=2.2, referee=RefereeProfile(1.0, 0.13, 0.004, 0.0), is_home=True,
        lineup=LineupExpectation(0.92, 0.05, 86.0, 20.0),
    )


def reference_report(now: datetime) -> AllocationReport:
    """The documented reference allocation (ARCHITECTURE 4.2) on synthetic quotes."""
    projection = reference_projection()
    quotes = [
        LiveOddsQuote(
            1001, 55, "tackles", line,
            over_price=price_for_ev(projection.side_probabilities(line, "over"), ev),
            under_price=price_for_ev(projection.side_probabilities(line, "under"), -0.10),
            max_stake=500.0, sportsbook=book, captured_at=now, void_rule=VoidRule.VOID_IF_NO_APPEARANCE,
        )
        for line, ev, book in ((1.5, 0.060, "SyntheticBookA"), (2.5, 0.062, "SyntheticBookB"))
    ]
    return _reference_manager().allocate(
        {(1001, 55, "tackles"): projection}, quotes, now=now, exposure=ExposureBook({})
    )


def _reference_manager() -> InstitutionalAssetManager:
    return InstitutionalAssetManager(bankroll=10_000.0, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)


def demo_case(now: datetime) -> tuple[dict[ProjectionKey, FactorProjection], AllocationReport]:
    """Reference quotes plus a synthetic Under 4.5 priced at +8% EV, which passes the screen."""
    projection = reference_projection()
    quotes = [
        LiveOddsQuote(
            1001, 55, "tackles", line,
            over_price=price_for_ev(projection.side_probabilities(line, "over"), over_ev),
            under_price=price_for_ev(projection.side_probabilities(line, "under"), under_ev),
            max_stake=500.0, sportsbook=book, captured_at=now, void_rule=VoidRule.VOID_IF_NO_APPEARANCE,
        )
        for line, over_ev, under_ev, book in (
            (1.5, 0.060, -0.10, "SyntheticBookA"),
            (2.5, 0.062, -0.10, "SyntheticBookB"),
            (4.5, -0.10, 0.080, "SyntheticBookC"),
        )
    ]
    projections = {(1001, 55, "tackles"): projection}
    return projections, _reference_manager().allocate(projections, quotes, now=now, exposure=ExposureBook({}))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Telegram alerts for prop_alpha staking directives.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="verify the token and send one test message")
    action.add_argument("--get-chat-id", action="store_true", help="list chats that have messaged the bot")
    action.add_argument("--demo", action="store_true", help="send the synthetic reference-case directives")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args(argv)

    notifier = TelegramNotifier(load_config(args.env_file))
    username = notifier.check_connection()
    print(f"connected: token accepted for bot @{username}")
    if args.get_chat_id:
        chats = notifier.recent_chats()
        if not chats:
            print("no recent chats: send /start to the bot in Telegram, then run this again")
        for chat_id, name in chats:
            print(f"chat id {chat_id}  ({name})")
        return 0
    if args.check:
        message_id = notifier.send_message(
            f"prop_alpha connection test at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
            "Alerts will arrive in this chat. " + PAPER_LABEL
        )
        print(f"test message delivered (message_id {message_id})")
        return 0
    projections, report = demo_case(datetime.now(timezone.utc))
    criteria = DEFAULT_CRITERIA
    for directive in report.directives:
        assessment = assess_high_value(
            directive, projections[(directive.fixture_id, directive.player_id, directive.market_type)],
            criteria, np.random.default_rng(simulation_seed(directive)),
        )
        print(
            f"{directive.side} {directive.line_value:g} @ {directive.decimal_odds:.3f}: "
            f"mispriced={assessment.mispriced} ev={assessment.ev_per_unit:+.2%} "
            f"survived {assessment.surviving_simulations}/{assessment.n_simulations} "
            f"-> {'HIGH VALUE' if assessment.is_high_value else 'not high value'}"
        )
    notifier.send_message("SYNTHETIC DEMO: the alert below uses made-up quotes on the documented reference case.")
    sent = notifier.notify(report, projections, criteria)
    print(f"demo: {len(sent)} of {len(report.directives)} synthetic directives passed the screen and were sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
