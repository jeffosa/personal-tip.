"""Pure parsing of the Telegram replies that record a manually placed bet (no I/O, no clock).

The maintainer places every bet by hand, then replies to the alert or fair-price board message it came from:

    placed 3 @1.95 5                  bet 3 of that message at decimal odds 1.95, stake 5
    placed 2 under @2.10 10 skybet    board rows list both sides, so the side is required there; the book is optional
    undo 12                           cancel recorded bet 12
    settled 12 lost                   record bet 12's result (won, lost or void); overrides an automatic result

`resolve_bet` checks the reply against the numbered items of the message it answers and returns a PlacedBet, or a
ReplyError whose message is sent back to the chat. Odds below the item's minimum are recorded (the bet happened)
and flagged in the confirmation.

`grade` settles a bet from the stored match statistics (automatic settlement, run by `ledger --settle`). The
statistic and void rule are the assumed market contract of ingestion/oddspapi.py (tackles = TACKLES, fouls =
FOULS, void if the player does not play), not yet checked against a bookmaker's settlement: every automatic
result says so, and a `settled` reply overrides it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Mapping, Sequence

# A tip message numbers its matches and letters the bets under each ("1a", "2c"). The letters leave out o and u,
# which a reply may use as the over/under shorthand right after the number ("placed 3 u @1.9 5").
BET_LETTERS: Final[str] = "abcdefghijklmnpqrstvwxyz"
PLACED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*placed\s+#?(?P<number>\d{1,3}[a-npq-tv-z]?)\s*(?P<side>over|under|o|u)?\s*@\s*(?P<odds>\d{1,4}(?:\.\d{1,3})?)\s+"
    r"(?P<symbol>[£$€])?\s*(?P<stake>\d{1,7}(?:\.\d{1,2})?)\s*(?P<book>[A-Za-z0-9][A-Za-z0-9 ._-]{0,39})?\s*$",
    re.IGNORECASE,
)
UNDO_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*undo\s+#?(?P<bet_id>\d{1,7})\s*$", re.IGNORECASE)
SETTLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*settled?\s+#?(?P<bet_id>\d{1,7})\s+(?P<result>won|win|lost|lose|void)\s*$", re.IGNORECASE
)
# The bookmaker's settled result for a pilot PAPER tip (2026-10-25): "result 3 won bet365". It records evidence about the
# settlement rules only (alerts/pilot_ledger --reconcile); pilot tips stay paper and are never recorded as placed bets. The tip
# number is the one /pilot lists; the book is optional (a result read off another bookmaker's slip names that book).
RESULT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*result\s+#?(?P<bet_id>\d{1,7})\s+(?P<result>won|win|lost|lose|void)"
    r"(?:\s+(?P<book>[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}?))?\s*$",
    re.IGNORECASE,
)
USAGE: Final[str] = (
    "To record a bet, reply to the tip or board message with: placed <number> [over|under] @<odds> <stake> [book]"
    " - e.g. placed 1a @1.95 5 (the number is the one shown beside the bet). To cancel one: undo <bet id>. "
    "To record a result: settled <bet id> won|lost|void. For a pilot paper tip, the bookmaker's result: "
    "result <tip number> won|lost|void [book] (tip numbers: /pilot)."
)
REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{1,3}[a-npq-tv-z]?$")
RESULTS: Final[tuple[str, ...]] = ("won", "lost", "void")
SETTLEMENT_SOURCES: Final[frozenset[str]] = frozenset({"auto", "manual"})
# The stored count each market settles on (ingestion/oddspapi.py market contract, assumed until checked).
SETTLEMENT_COLUMNS: Final[Mapping[str, str]] = {"tackles": "tackles_attempted", "fouls": "fouls_committed"}
# The data the automatic settlement reads: Sportmonks again since 2026-10-21 (TheStatsAPI from 2026-10-15 to 2026-10-20).
AUTO_BASIS: Final[str] = "Sportmonks stats, assumed market rules (not yet checked against the bookmaker)"
UNKNOWN_BOOK: Final[str] = "unknown"
ITEM_KINDS: Final[frozenset[str]] = frozenset({"alert", "board"})
SIDES: Final[tuple[str, ...]] = ("over", "under")


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def _valid_line(line: float) -> bool:
    return math.isfinite(line) and line >= 0.0 and float(line * 2.0).is_integer()


@dataclass(frozen=True)
class AlertItem:
    """One numbered bet of a sent alert or board message. `min_odds` holds (side, minimum acceptable odds) for
    each side offered: one side on an alert, one or two on a board row. `book` is the alert's bookmaker; a board
    names none. `number` is unique within its message; `label` is the reference a reply uses when the message shows
    one ("1a": match 1, bet a), otherwise the reply uses the number."""

    number: int
    kind: str
    fixture_id: int
    player_id: int
    market: str
    line: float
    min_odds: tuple[tuple[str, float], ...]
    match: str
    player: str
    book: str | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"item kind must be one of {sorted(ITEM_KINDS)}")
        if self.number < 1:
            raise ValueError("item number must be at least 1")
        if self.label and (REF_PATTERN.match(self.label) is None or self.label[-1].isdigit()):
            raise ValueError(f"item label {self.label!r} must be a match number and a bet letter, e.g. 1a")
        if not self.market or not _valid_line(self.line):
            raise ValueError("item needs a market and a whole or half line")
        sides = [side for side, _ in self.min_odds]
        if not sides or len(set(sides)) != len(sides) or any(side not in SIDES for side in sides):
            raise ValueError("min_odds must list 'over' and/or 'under' once each")
        if any(not (math.isfinite(odds) and odds > 1.0) for _, odds in self.min_odds):
            raise ValueError("minimum odds must be finite and above 1")

    @property
    def sides(self) -> dict[str, float]:
        return dict(self.min_odds)

    @property
    def ref(self) -> str:
        """What a reply calls this bet: its label ("1a") or, without one, its number ("3")."""
        return self.label or str(self.number)


@dataclass(frozen=True)
class PlaceCommand:
    """`number` is the reference typed after `placed` ("3" or "1a", lower-case). `stake_symbol` is the currency
    sign typed before the stake (£, $ or €), or None: then the stake is in the bankroll currency. The ledger
    converts a book-currency stake (£ with a USD bankroll) at the cached rate."""

    number: str
    side: str | None
    odds: float
    stake: float
    book: str | None
    stake_symbol: str | None = None


@dataclass(frozen=True)
class UndoCommand:
    bet_id: int


@dataclass(frozen=True)
class SettleCommand:
    bet_id: int
    result: str


@dataclass(frozen=True)
class ResultCommand:
    """The bookmaker's result for pilot paper tip `bet_id`, and the bookmaker it was read from (None: not given)."""

    bet_id: int
    result: str
    book: str | None


@dataclass(frozen=True)
class ReplyError:
    message: str


def parse_reply(text: str) -> PlaceCommand | UndoCommand | SettleCommand | ResultCommand | ReplyError:
    """A `placed`, `undo`, `settled` or `result` command, or a ReplyError explaining the expected form."""
    placed = PLACED_PATTERN.match(text)
    if placed is not None:
        side = placed.group("side")
        odds, stake = float(placed.group("odds")), float(placed.group("stake"))
        if odds <= 1.0:
            return ReplyError(f"Odds must be decimal odds above 1, got {placed.group('odds')}. {USAGE}")
        if stake <= 0.0:
            return ReplyError(f"Stake must be positive. {USAGE}")
        book = (placed.group("book") or "").strip() or None
        return PlaceCommand(
            placed.group("number").lower(),
            None if side is None else ("over" if side.lower().startswith("o") else "under"),
            odds, stake, book, placed.group("symbol"),
        )
    undo = UNDO_PATTERN.match(text)
    if undo is not None:
        return UndoCommand(int(undo.group("bet_id")))
    settle = SETTLE_PATTERN.match(text)
    if settle is not None:
        word = settle.group("result").lower()
        return SettleCommand(int(settle.group("bet_id")), {"win": "won", "lose": "lost"}.get(word, word))
    found = RESULT_PATTERN.match(text)
    if found is not None:
        word = found.group("result").lower()
        return ResultCommand(int(found.group("bet_id")), {"win": "won", "lose": "lost"}.get(word, word),
                             (found.group("book") or "").strip() or None)
    return ReplyError(f"Not understood. {USAGE}")


@dataclass(frozen=True)
class PlacedBet:
    """A bet the maintainer reports having placed. `update_id` is the Telegram update that recorded it, so a
    re-delivered update is never recorded twice."""

    bet_id: int
    update_id: int
    message_id: int
    number: int
    kind: str
    fixture_id: int
    player_id: int
    market: str
    line: float
    side: str
    odds: float
    stake: float
    book: str
    min_odds: float
    match: str
    player: str
    placed_at: datetime
    # The stake as typed, when it was in the books' currency and `stake` is its bankroll-currency conversion.
    entered_stake: float | None = None
    entered_currency: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.placed_at, "placed_at")
        if (self.entered_stake is None) != (self.entered_currency is None):
            raise ValueError("entered_stake and entered_currency go together")
        if self.bet_id < 1 or self.kind not in ITEM_KINDS or self.side not in SIDES:
            raise ValueError("bet needs a positive id, a known kind and a side")
        if not self.market or not self.book or not _valid_line(self.line):
            raise ValueError("bet needs a market, a book and a whole or half line")
        for name, value in (("odds", self.odds), ("min_odds", self.min_odds)):
            if not (math.isfinite(value) and value > 1.0):
                raise ValueError(f"{name} must be finite and above 1")
        if not (math.isfinite(self.stake) and self.stake > 0.0):
            raise ValueError("stake must be finite and positive")

    @property
    def below_minimum(self) -> bool:
        return self.odds < self.min_odds


def resolve_bet(
    command: PlaceCommand,
    items: Mapping[int, AlertItem],
    *,
    bet_id: int,
    update_id: int,
    message_id: int,
    placed_at: datetime,
    stake: float | None = None,
    entered: tuple[float, str] | None = None,
) -> PlacedBet | ReplyError:
    """The bet `command` describes on the message whose numbered items are `items`, or why it cannot be recorded.
    `stake` replaces command.stake when the ledger converted it from `entered` (amount, currency)."""
    by_ref = {item.ref: item for item in items.values()}
    item = by_ref.get(command.number)
    if item is None:
        listed = ", ".join(i.ref for i in sorted(items.values(), key=lambda i: i.number)) or "none"
        return ReplyError(f"That message has no bet {command.number} (it lists: {listed}).")
    sides = item.sides
    if command.side is None:
        if len(sides) != 1:
            return ReplyError(f"Bet {command.number} lists both sides: say over or under, e.g. "
                              f"placed {command.number} over @{command.odds:g} {command.stake:g}")
        side = next(iter(sides))
    elif command.side not in sides:
        return ReplyError(f"Bet {command.number} offers only {' and '.join(s.upper() for s in sides)}, "
                          f"not {command.side.upper()}.")
    else:
        side = command.side
    return PlacedBet(
        bet_id=bet_id, update_id=update_id, message_id=message_id, number=item.number, kind=item.kind,
        fixture_id=item.fixture_id, player_id=item.player_id, market=item.market, line=item.line, side=side,
        odds=command.odds, stake=command.stake if stake is None else stake,
        book=command.book or item.book or UNKNOWN_BOOK, min_odds=sides[side], match=item.match, player=item.player,
        placed_at=placed_at, entered_stake=None if entered is None else entered[0],
        entered_currency=None if entered is None else entered[1],
    )


def confirmation(bet: PlacedBet) -> str:
    lines = [
        f"Recorded bet #{bet.bet_id}: {bet.player}, {bet.side.upper()} {bet.line:g} {bet.market} "
        f"@ {bet.odds:.2f}, stake {bet.stake:g} ({bet.book})"
        + ("" if bet.entered_stake is None else f" = {bet.entered_currency} {bet.entered_stake:g} as entered"),
        bet.match,
    ]
    if bet.below_minimum:
        lines.append(f"WARNING: {bet.odds:.2f} is below the minimum {bet.min_odds:.2f}; below it the model "
                     "expects no edge.")
    lines.append(f"To cancel: undo {bet.bet_id}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Settlement:
    """The result of one recorded bet. `source` "auto" comes from the stored statistics (`grade`); "manual" from a
    `settled` reply, which overrides any automatic result. `count` and `minutes` are the statistics an automatic
    result used (None for a manual one); `update_id` is the Telegram update of a manual one."""

    bet_id: int
    result: str
    source: str
    detail: str
    settled_at: datetime
    count: int | None = None
    minutes: int | None = None
    update_id: int | None = None

    def __post_init__(self) -> None:
        _require_utc(self.settled_at, "settled_at")
        if self.bet_id < 1 or self.result not in RESULTS or self.source not in SETTLEMENT_SOURCES:
            raise ValueError("settlement needs a positive bet id, a known result and a known source")
        if self.source == "manual" and self.update_id is None:
            raise ValueError("a manual settlement carries the Telegram update that recorded it")
        for name, value in (("count", self.count), ("minutes", self.minutes)):
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")


def profit(bet: PlacedBet, settlement: Settlement) -> float:
    """Profit in stake units: won = stake x (odds - 1), lost = -stake, void (a push or no appearance) = 0."""
    if settlement.bet_id != bet.bet_id:
        raise ValueError("settlement is for another bet")
    if settlement.result == "won":
        return bet.stake * (bet.odds - 1.0)
    return -bet.stake if settlement.result == "lost" else 0.0


def grade(bet: PlacedBet, fixture_status: str, minutes: int | None, count: int | None,
          settled_at: datetime) -> Settlement | None:
    """The automatic result of `bet`, or None while it cannot be graded safely: the match is not final, or the
    player has no stored row (Sportmonks may have listed him without an identity; settle it by hand). A player
    with a row and 0 minutes did not play: void. A count equal to a whole line is a push: void."""
    if bet.market not in SETTLEMENT_COLUMNS:
        raise ValueError(f"no settlement statistic for market {bet.market!r}")
    if fixture_status != "final" or minutes is None:
        return None
    if minutes == 0:
        return Settlement(bet.bet_id, "void", "auto", f"did not play; {AUTO_BASIS}", settled_at, count, minutes)
    if count is None:
        raise ValueError(f"bet {bet.bet_id}: player played {minutes} minutes but has no {bet.market} count")
    if count == bet.line:
        result = "void"
    else:
        result = "won" if (count > bet.line) == (bet.side == "over") else "lost"
    detail = f"{count} {bet.market} in {minutes} min v {bet.side} {bet.line:g}; {AUTO_BASIS}"
    return Settlement(bet.bet_id, result, "auto", detail, settled_at, count, minutes)


@dataclass(frozen=True)
class ClosingValue:
    """Closing-line value of one bet (PRD Gate 3): CLV = odds taken x devigged closing probability of the side - 1,
    from `book`'s two-way closing price, which may differ from the book the bet was placed with."""

    clv: float
    closing_price: float
    book: str


def settlement_text(bet: PlacedBet, settlement: Settlement, closing: ClosingValue | None = None) -> str:
    """One line for the chat or the ledger listing."""
    # Automatic results rest on assumed market rules until a bookmaker's settlement confirms them (audit 2026-10-04).
    outcome = settlement.result.upper() + (" (provisional)" if settlement.source == "auto" else "")
    clv = ("" if closing is None else
           f" | close {closing.closing_price:.2f} at {closing.book}, CLV {closing.clv:+.1%}")
    return (f"#{bet.bet_id} {outcome} {profit(bet, settlement):+.2f}: {bet.player}, {bet.side.upper()} {bet.line:g} "
            f"{bet.market} @ {bet.odds:.2f}, stake {bet.stake:g} ({bet.match}){clv} - {settlement.detail}")


# Settlement reconciliation (maintainer task 6, 2026-10-05): every bookmaker result the maintainer reports with a
# `settled` reply is compared with what automatic settlement (`grade`, the assumed market contract) gives from the
# stored statistics. Only "agree" is a pass. PRD Gate 3 criterion 4 needs every paper bet to reconcile.
RECONCILE_OUTCOMES: Final[tuple[str, ...]] = ("agree", "discrepancy", "unverifiable", "unconfirmed")


@dataclass(frozen=True)
class Reconciliation:
    """One settled bet. `bookmaker_result` is the maintainer's `settled` reply (None when the bet has only an automatic
    result); `automatic_result` is `grade` on the statistics stored now (None when it cannot grade). Outcomes:
    "agree" (both present and equal), "discrepancy" (both present and different), "unverifiable" (a bookmaker result,
    but no gradable statistics: match not final or not ingested, or no row for the player), "unconfirmed" (an
    automatic result that no bookmaker result has confirmed yet). `overridden` is True when a recorded automatic
    result was changed by the reply."""

    bet_id: int
    outcome: str
    bookmaker_result: str | None
    automatic_result: str | None
    detail: str
    overridden: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in RECONCILE_OUTCOMES:
            raise ValueError(f"unknown reconciliation outcome {self.outcome!r}")
        for name, value in (("bookmaker_result", self.bookmaker_result), ("automatic_result", self.automatic_result)):
            if value is not None and value not in RESULTS:
                raise ValueError(f"{name} {value!r} is not a result")
        if (self.outcome in ("agree", "discrepancy")) != (self.bookmaker_result is not None
                                                          and self.automatic_result is not None):
            raise ValueError(f"outcome {self.outcome!r} does not fit the results it compares")
        if self.outcome == "agree" and self.bookmaker_result != self.automatic_result:
            raise ValueError("an agreeing reconciliation needs equal results")
        if self.outcome == "discrepancy" and self.bookmaker_result == self.automatic_result:
            raise ValueError("a discrepancy needs different results")


def reconcile(bet: PlacedBet, settlement: Settlement, row: tuple[str, int | None, int | None, int | None] | None,
              recorded_automatic: Settlement | None, now: datetime) -> Reconciliation:
    """Pure: compare a settled bet's effective result with `grade` on `row` (fixture status, the player's minutes,
    tackles and fouls, as ledger.read_settlement_rows returns it; None when the match was never ingested).
    `recorded_automatic` is the automatic settlement the ledger recorded earlier, if any."""
    _require_utc(now, "now")
    if settlement.bet_id != bet.bet_id:
        raise ValueError(f"settlement {settlement.bet_id} is not for bet {bet.bet_id}")
    if settlement.source == "auto":
        return Reconciliation(bet.bet_id, "unconfirmed", None, settlement.result,
                              "automatic result, not yet confirmed by a bookmaker result (reply settled <id> ...)")
    overridden = recorded_automatic is not None and recorded_automatic.result != settlement.result
    if row is None:
        return Reconciliation(bet.bet_id, "unverifiable", settlement.result, None,
                              "match not in the database", overridden)
    status, minutes, tackles, fouls = row
    count = {"tackles": tackles, "fouls": fouls}.get(bet.market)
    automatic = grade(bet, status, minutes, count, now)
    if automatic is None:
        reason = f"match status {status}" if status != "final" else "no stored row for the player in this match"
        return Reconciliation(bet.bet_id, "unverifiable", settlement.result, None, reason, overridden)
    outcome = "agree" if automatic.result == settlement.result else "discrepancy"
    return Reconciliation(bet.bet_id, outcome, settlement.result, automatic.result, automatic.detail, overridden)


def reconciliation_text(bet: PlacedBet, item: Reconciliation) -> str:
    """One line per bet for the reconciliation report."""
    label = {"agree": "AGREE", "discrepancy": "DISCREPANCY", "unverifiable": "NOT CHECKED",
             "unconfirmed": "UNCONFIRMED"}[item.outcome]
    results = (f"bookmaker {item.bookmaker_result or '-'} v automatic {item.automatic_result or '-'}"
               + (" (your reply overrode the recorded automatic result)" if item.overridden else ""))
    return (f"#{bet.bet_id} {label}: {bet.player}, {bet.side.upper()} {bet.line:g} {bet.market} ({bet.match}, "
            f"{bet.book}): {results} - {item.detail}")


def reconciliation_summary(items: Sequence[Reconciliation]) -> str:
    """Counts per outcome. A discrepancy is a failure of the assumed market contract, never a pass."""
    counts = {outcome: sum(1 for item in items if item.outcome == outcome) for outcome in RECONCILE_OUTCOMES}
    checked = counts["agree"] + counts["discrepancy"] + counts["unverifiable"]
    verdict = ("FAIL: the assumed market rules disagree with a bookmaker's settlement" if counts["discrepancy"]
               else "no discrepancy so far" if counts["agree"] else "nothing verified yet")
    return (f"Settlement check: {checked} bookmaker result(s): {counts['agree']} agree, {counts['discrepancy']} "
            f"DISCREPANCY, {counts['unverifiable']} could not be checked; {counts['unconfirmed']} automatic result(s) "
            f"not yet confirmed by a bookmaker; {sum(1 for item in items if item.overridden)} override(s). {verdict}. "
            "PRD Gate 3 needs every bet to reconcile.")
