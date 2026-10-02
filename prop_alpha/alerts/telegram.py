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

One alert is sent per market, line, side and book for the life of the process, whatever the price does
afterwards, so a person following the alerts is not told to stack the same position on every price tick.
The sent set is in memory only: persisting it across restarts needs the bet ledger ([N]).

Usage:
    python -m prop_alpha.alerts.telegram --check        # getMe + one test message
    python -m prop_alpha.alerts.telegram --get-chat-id  # list chats that have messaged the bot
    python -m prop_alpha.alerts.telegram --demo         # synthetic directives, screened, labelled
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

import numpy as np
import requests

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
DEFAULT_ENV_FILE: Final[Path] = Path(__file__).resolve().parents[2] / ".env"
PAPER_LABEL: Final[str] = "PAPER ONLY - model not validated, no PRD go-live gate passed. Not betting advice."
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
_CHAT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,})$")

AlertKey = tuple[int, int, str, float, str, str]
SeedKey = tuple[int, int, str, float, str, str, float]
DEFAULT_CRITERIA: Final[HighValueCriteria] = HighValueCriteria(loss_floor=MODEL_RISK_LOSS_FLOOR)


class TelegramError(RuntimeError):
    """A Telegram call failed. The message never contains the bot token."""


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments ignored; optional `export` prefix and matching quotes."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f".env line {number} is not KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


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
        """Process environment first, then the .env file."""
        file_values = parse_env_file(env_file_text) if env_file_text is not None else {}
        return cls(
            bot_token=environ.get(TOKEN_ENV) or file_values.get(TOKEN_ENV, ""),
            chat_id=environ.get(CHAT_ID_ENV) or file_values.get(CHAT_ID_ENV) or None,
        )


def load_config(env_file: Path = DEFAULT_ENV_FILE) -> TelegramConfig:
    text = env_file.read_text(encoding="utf-8") if env_file.is_file() else None
    return TelegramConfig.from_sources(os.environ, text)


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
    """Sends one message per new directive. Remembers what it sent for its own lifetime only."""

    def __init__(self, config: TelegramConfig, session: Any | None = None, timeout_s: float = 10.0) -> None:
        if not timeout_s > 0.0:
            raise ValueError("timeout_s must be positive")
        self._config = config
        self._session = session if session is not None else requests.Session()
        self._timeout_s = timeout_s
        self._sent: set[AlertKey] = set()

    def _call(self, method: str, payload: Mapping[str, Any]) -> Any:
        url = f"{TELEGRAM_API_BASE}/bot{self._config.bot_token}/{method}"
        try:
            response = self._session.post(url, json=dict(payload), timeout=self._timeout_s)
            body = response.json()
        except requests.RequestException as exc:
            raise TelegramError(f"{method} failed: {type(exc).__name__} (details withheld: they contain the token)") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            description = body.get("description", "no description") if isinstance(body, dict) else "malformed reply"
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
            message_ids.append(self.send_message(format_directive(directive, assessment, bankroll_currency)))
            self._sent.add(key)
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
