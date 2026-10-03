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
the first. Until the bet ledger ([N]) records real fills, every recent alert counts as an open position.

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
from typing import Any, Callable, Final, Mapping

import numpy as np
import requests

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


def format_directive(
    directive: StakingDirective, assessment: HighValueAssessment, bankroll_currency: str = ""
) -> str:
    """Plain-text alert (no parse_mode, so nothing needs escaping)."""
    currency = f"{bankroll_currency} " if bankroll_currency else ""
    return "\n".join((
        PAPER_LABEL,
        "HIGH-VALUE BET",
        f"Fixture {directive.fixture_id} | player {directive.player_id} | {directive.market_type}",
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

    def send_message(self, text: str) -> int:
        if not text.strip():
            raise ValueError("message text must not be empty")
        if self._config.chat_id is None:
            raise ValueError(f"{CHAT_ID_ENV} is not set; run with --get-chat-id to find it")
        if self._last_send is not None:
            wait = MIN_SEND_INTERVAL_S - (self._clock() - self._last_send)
            if wait > 0.0:
                self._sleep(wait)
        self._last_send = self._clock()
        result = self._call(
            "sendMessage", {"chat_id": self._config.chat_id, "text": text[:4096], "disable_web_page_preview": True}
        )
        return int(result["message_id"])

    def notify(
        self,
        report: AllocationReport,
        projections: Mapping[ProjectionKey, FactorProjection],
        criteria: HighValueCriteria = DEFAULT_CRITERIA,
        bankroll_currency: str = "",
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
        for directive in report.directives:
            key = alert_key(directive)
            if key in self._sent:
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
            text = format_directive(directive, assessment, bankroll_currency)
            if self._config.chat_id is None:
                raise ValueError(f"{CHAT_ID_ENV} is not set; run with --get-chat-id to find it")
            if self._store is not None:
                self._store.append(SentAlert(key, directive.stake, datetime.now(timezone.utc)))
            self._sent.add(key)
            message_ids.append(self.send_message(text))
            logger.info("alert sent for %s", key)
        return message_ids


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
