"""Pure parsing of the Telegram replies that record a manually placed bet (no I/O, no clock).

The maintainer places every bet by hand, then replies to the alert or fair-price board message it came from:

    placed 3 @1.95 5                  bet 3 of that message at decimal odds 1.95, stake 5
    placed 2 under @2.10 10 skybet    board rows list both sides, so the side is required there; the book is optional
    undo 12                           cancel recorded bet 12

`resolve_bet` checks the reply against the numbered items of the message it answers and returns a PlacedBet, or a
ReplyError whose message is sent back to the chat. Odds below the item's minimum are recorded (the bet happened)
and flagged in the confirmation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Mapping

PLACED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*placed\s+#?(?P<number>\d{1,3})\s*(?P<side>over|under|o|u)?\s*@\s*(?P<odds>\d{1,4}(?:\.\d{1,3})?)\s+"
    r"[£$€]?\s*(?P<stake>\d{1,7}(?:\.\d{1,2})?)\s*(?P<book>[A-Za-z0-9][A-Za-z0-9 ._-]{0,39})?\s*$",
    re.IGNORECASE,
)
UNDO_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*undo\s+#?(?P<bet_id>\d{1,7})\s*$", re.IGNORECASE)
USAGE: Final[str] = (
    "To record a bet, reply to the alert or board message with: placed <number> [over|under] @<odds> <stake> [book]"
    " - e.g. placed 3 @1.95 5. To cancel one: undo <bet id>."
)
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
    names none."""

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

    def __post_init__(self) -> None:
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"item kind must be one of {sorted(ITEM_KINDS)}")
        if self.number < 1:
            raise ValueError("item number must be at least 1")
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


@dataclass(frozen=True)
class PlaceCommand:
    number: int
    side: str | None
    odds: float
    stake: float
    book: str | None


@dataclass(frozen=True)
class UndoCommand:
    bet_id: int


@dataclass(frozen=True)
class ReplyError:
    message: str


def parse_reply(text: str) -> PlaceCommand | UndoCommand | ReplyError:
    """A `placed` or `undo` command, or a ReplyError explaining the expected form."""
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
            int(placed.group("number")),
            None if side is None else ("over" if side.lower().startswith("o") else "under"),
            odds, stake, book,
        )
    undo = UNDO_PATTERN.match(text)
    if undo is not None:
        return UndoCommand(int(undo.group("bet_id")))
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

    def __post_init__(self) -> None:
        _require_utc(self.placed_at, "placed_at")
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
) -> PlacedBet | ReplyError:
    """The bet `command` describes on the message whose numbered items are `items`, or why it cannot be recorded."""
    item = items.get(command.number)
    if item is None:
        listed = ", ".join(str(n) for n in sorted(items)) or "none"
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
        odds=command.odds, stake=command.stake, book=command.book or item.book or UNKNOWN_BOOK,
        min_odds=sides[side], match=item.match, player=item.player, placed_at=placed_at,
    )


def confirmation(bet: PlacedBet) -> str:
    lines = [
        f"Recorded bet #{bet.bet_id}: {bet.player}, {bet.side.upper()} {bet.line:g} {bet.market} "
        f"@ {bet.odds:.2f}, stake {bet.stake:g} ({bet.book})",
        bet.match,
    ]
    if bet.below_minimum:
        lines.append(f"WARNING: {bet.odds:.2f} is below the minimum {bet.min_odds:.2f}; below it the model "
                     "expects no edge.")
    lines.append(f"To cancel: undo {bet.bet_id}")
    return "\n".join(lines)
