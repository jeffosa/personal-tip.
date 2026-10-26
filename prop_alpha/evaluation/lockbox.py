"""The prospective Gate 2 lockbox protocol (LOCKBOX_PROTOCOL.md, maintainer 2026-10-25): which logged predictions count,
which outcome each is scored against, and when scoring may happen.

Pure: no I/O, no clock (the caller passes `now`), no randomness. The protocol is written down here as literals, so a
pricing change (a new FROZEN_DIGEST in modeling/model_freeze.py) or a new data source cannot slip into the lockbox:
tests/test_lockbox.py fails until a new protocol with a later start replaces this one.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Collection, Final, Mapping, Sequence

# The 24 League One/Two matches of 2026-10-10, scored by scripts/gate2_lockbox.py on 2026-10-11 and inside the
# 2026-10-15 backtest windows: examined before the restart, so never evidence.
EXAMINED_FIXTURES: Final[frozenset[int]] = frozenset({
    *range(19727666, 19727678), *range(19728218, 19728230),
})

# Exclusion reasons, in the order they are checked.
EXAMINED: Final[str] = "match examined before the restart"
BEFORE_START: Final[str] = "kickoff before the lockbox start"
AFTER_END: Final[str] = "kickoff after the lockbox end"
OTHER_DIGEST: Final[str] = "made by other pricing code"
OTHER_VENDOR: Final[str] = "data source not recorded or not the protocol's"
NOT_BEFORE_KICKOFF: Final[str] = "not logged before kickoff"
OTHER_LINE: Final[str] = "market or line not in the protocol"
KICKOFF_MOVED: Final[str] = "kickoff moved (postponed or rescheduled)"
DUPLICATE: Final[str] = "duplicate (a later log of the same prediction)"
# Scoring states of an eligible prediction.
PENDING: Final[str] = "outcome pending (not final, or inside the revision grace)"
MISSING: Final[str] = "outcome missing (not final 14 days after kickoff)"
VOID: Final[str] = "void (the player did not play)"


@dataclass(frozen=True)
class LockboxProtocol:
    start: datetime
    decided_on: date
    pricing_digest: str
    data_vendor: str
    leagues: tuple[str, ...]
    lines: Mapping[str, tuple[float, ...]]
    outcome_columns: Mapping[str, str]
    examined_fixtures: frozenset[int]
    last_examined_kickoff: datetime
    kickoff_tolerance: timedelta
    revision_grace: timedelta
    missing_after: timedelta
    min_scored_per_line: int
    seed: int
    # Amendment of 2026-10-26 (maintainer, before the start; GATE2_CALIBRATION_DECISION_MEMO.md Option 3): the one look waits
    # until every line also has `min_scored_fixtures_per_line` distinct matches, and only kickoffs before `end` count. A
    # lockbox still short of that once it has closed (`end` plus `missing_after`) is NOT PASSED and never scored.
    end: datetime
    min_scored_fixtures_per_line: int
    amended_on: date

    def __post_init__(self) -> None:
        for name in ("start", "last_examined_kickoff", "end"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be a UTC datetime")
        if self.start <= self.last_examined_kickoff:
            raise ValueError("the lockbox must start after every examined match")
        if self.start.date() <= self.decided_on:
            raise ValueError("the lockbox must start after the day the protocol was decided")
        if len(self.pricing_digest) != 64 or not self.data_vendor:
            raise ValueError("the protocol must name a full pricing digest and a data source")
        if set(self.lines) != set(self.outcome_columns):
            raise ValueError("every protocol market needs an outcome column")
        if self.min_scored_per_line < 1 or self.revision_grace < timedelta(0) or self.missing_after <= self.revision_grace:
            raise ValueError("invalid scoring thresholds")
        if self.end <= self.start or self.min_scored_fixtures_per_line < 1:
            raise ValueError("the lockbox must end after it starts and need at least one match per line")
        if self.amended_on >= self.start.date():
            raise ValueError("an amendment to the scoring rules must be decided before the lockbox starts")


PROTOCOL: Final[LockboxProtocol] = LockboxProtocol(
    start=datetime(2026, 10, 27, tzinfo=timezone.utc),
    decided_on=date(2026, 10, 25),
    pricing_digest="839e97ce504eaecd29a7b435fea9980e0a3d3401e648d4cee70a39b1ca63275f",
    data_vendor="sportmonks",
    leagues=("League One", "League Two"),
    lines={"tackles": (0.5, 1.5, 2.5), "fouls": (0.5, 1.5)},
    outcome_columns={"tackles": "tackles_attempted", "fouls": "fouls_committed"},
    examined_fixtures=EXAMINED_FIXTURES,
    last_examined_kickoff=datetime(2026, 10, 10, 14, 15, tzinfo=timezone.utc),
    kickoff_tolerance=timedelta(minutes=15),
    revision_grace=timedelta(hours=72),
    missing_after=timedelta(days=14),
    min_scored_per_line=500,
    seed=20261027,
    end=datetime(2027, 6, 1, tzinfo=timezone.utc),
    min_scored_fixtures_per_line=320,
    amended_on=date(2026, 10, 26),
)

PredictionKey = tuple[int, int, str, float]


@dataclass(frozen=True)
class Eligibility:
    eligible: tuple[Mapping[str, Any], ...]
    excluded: Mapping[str, int]


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"time {value!r} has no timezone")
    return parsed.astimezone(timezone.utc)


def record_reason(record: Mapping[str, Any], protocol: LockboxProtocol = PROTOCOL) -> str | None:
    """Pure: why one logged line is not lockbox evidence on its own, or None (duplicates and moved kickoffs need the whole
    log and the stored matches: see `eligible`)."""
    kickoff, logged = _time(record["kickoff_at"]), _time(record["logged_at"])
    if int(record["fixture_id"]) in protocol.examined_fixtures:
        return EXAMINED
    if kickoff < protocol.start:
        return BEFORE_START
    if kickoff >= protocol.end:
        return AFTER_END
    if record.get("digest") != protocol.pricing_digest:
        return OTHER_DIGEST
    if record.get("data_vendor") != protocol.data_vendor:
        return OTHER_VENDOR
    if logged >= kickoff:
        return NOT_BEFORE_KICKOFF
    if float(record["line"]) not in protocol.lines.get(str(record["market"]), ()):
        return OTHER_LINE
    return None


def eligible(records: Sequence[Mapping[str, Any]], stored_kickoffs: Mapping[int, datetime],
             protocol: LockboxProtocol = PROTOCOL) -> Eligibility:
    """Pure: the lockbox predictions among `records` and a count of the rest by reason. `stored_kickoffs` is each match's
    kickoff as stored now (vendor id -> UTC time); a line logged for a kickoff more than the tolerance away from it was a
    prediction for a postponed or rescheduled date. Of several lines for one (match, player, market, line), only the
    earliest logged counts."""
    excluded: Counter[str] = Counter()
    candidates: list[Mapping[str, Any]] = []
    for record in records:
        reason = record_reason(record, protocol)
        if reason is None:
            stored = stored_kickoffs.get(int(record["fixture_id"]))
            if stored is not None and abs(stored - _time(record["kickoff_at"])) > protocol.kickoff_tolerance:
                reason = KICKOFF_MOVED
        if reason is None:
            candidates.append(record)
        else:
            excluded[reason] += 1
    first: dict[PredictionKey, Mapping[str, Any]] = {}
    for record in sorted(candidates, key=lambda r: (_time(r["logged_at"]), str(r["kickoff_at"]))):
        key = (int(record["fixture_id"]), int(record["player_id"]), str(record["market"]), float(record["line"]))
        if key in first:
            excluded[DUPLICATE] += 1
        else:
            first[key] = record
    return Eligibility(tuple(first.values()), dict(excluded))


@dataclass(frozen=True)
class StoredMatch:
    """A match as the outcome source holds it now: its kickoff, whether it is final, and each player's minutes and
    protocol counts (player id -> (minutes, {market: count}))."""

    kickoff_at: datetime
    final: bool
    players: Mapping[int, tuple[int, Mapping[str, int]]]


def outcomes_to_capture(predictions: Sequence[Mapping[str, Any]], captured: Mapping[tuple[int, int], Mapping[str, Any]],
                        matches: Mapping[int, StoredMatch], now: datetime,
                        protocol: LockboxProtocol = PROTOCOL) -> list[dict[str, Any]]:
    """Pure: the outcome records to append, once per (match, player) of an eligible prediction: only for a final match
    whose kickoff is at least the revision grace before `now`, and never again once captured (a later vendor revision
    is reported by `revised`, never applied). A player with no stored row did not play."""
    new: dict[tuple[int, int], dict[str, Any]] = {}
    for record in predictions:
        key = (int(record["fixture_id"]), int(record["player_id"]))
        match = matches.get(key[0])
        if key in captured or key in new or match is None or not match.final or now < match.kickoff_at + protocol.revision_grace:
            continue
        minutes, counts = match.players.get(key[1], (0, {}))
        new[key] = {"fixture_id": key[0], "player_id": key[1], "minutes": int(minutes),
                    **{market: (None if counts.get(market) is None else int(counts[market])) for market in protocol.lines},
                    "captured_at": now.isoformat(), "source": f"{protocol.data_vendor}:player_match_stats"}
    return list(new.values())


def revised(captured: Mapping[tuple[int, int], Mapping[str, Any]], matches: Mapping[int, StoredMatch],
            protocol: LockboxProtocol = PROTOCOL) -> list[tuple[int, int]]:
    """Pure: captured outcomes the source now holds differently (reported; the captured outcome stays the scored one)."""
    changed = []
    for (fixture_id, player_id), outcome in sorted(captured.items()):
        match = matches.get(fixture_id)
        if match is None:
            continue
        minutes, counts = match.players.get(player_id, (0, {}))
        if int(minutes) != int(outcome["minutes"]) or any(
                (counts.get(m) if counts.get(m) is None else int(counts[m])) != outcome.get(m) for m in protocol.lines):
            changed.append((fixture_id, player_id))
    return changed


@dataclass(frozen=True)
class ScoringRows:
    # (market, line) -> (match, player, logged probability given he plays, 1 if the count went over the line)
    rows: Mapping[tuple[str, float], tuple[tuple[int, int, float, int], ...]]
    states: Mapping[str, int]


def scoring_rows(predictions: Sequence[Mapping[str, Any]], captured: Mapping[tuple[int, int], Mapping[str, Any]],
                 now: datetime, protocol: LockboxProtocol = PROTOCOL) -> ScoringRows:
    """Pure: (match, player, logged probability given the player plays, 1 if the captured count went over the line) per
    market and line, and how many eligible predictions are pending, missing or void. The logged probability is scored, never a
    re-price."""
    rows: dict[tuple[str, float], list[tuple[int, int, float, int]]] = {
        (market, line): [] for market, lines in protocol.lines.items() for line in lines}
    states: Counter[str] = Counter()
    for record in predictions:
        fixture_id, market, line = int(record["fixture_id"]), str(record["market"]), float(record["line"])
        outcome = captured.get((fixture_id, int(record["player_id"])))
        if outcome is None:
            late = now >= _time(record["kickoff_at"]) + protocol.missing_after
            states[MISSING if late else PENDING] += 1
            continue
        count = outcome.get(market)
        if int(outcome["minutes"]) <= 0 or count is None:
            states[VOID] += 1
            continue
        settled = float(record["p_win"]) + float(record["p_lose"])
        p = float(record["p_win"]) / settled if settled > 0.0 else math.nan
        if not 0.0 < p < 1.0:
            states[VOID] += 1
            continue
        rows[(market, line)].append((fixture_id, int(record["player_id"]), p, int(int(count) > line)))
        states["scored"] += 1
    return ScoringRows({key: tuple(value) for key, value in rows.items()}, dict(states))


def ready_to_score(scoring: ScoringRows, protocol: LockboxProtocol = PROTOCOL) -> bool:
    """Pure: every protocol line has at least the required scored predictions, from at least the required distinct matches
    (the lockbox is scored once, then)."""
    return all(len(rows) >= protocol.min_scored_per_line
               and len({row[0] for row in rows}) >= protocol.min_scored_fixtures_per_line
               for rows in (scoring.rows.get((m, line), ()) for m, lines in protocol.lines.items() for line in lines))


def closed(now: datetime, protocol: LockboxProtocol = PROTOCOL) -> bool:
    """Pure: no more evidence can arrive: the last eligible kickoff is past and its outcomes are captured or missing."""
    return now >= protocol.end + protocol.missing_after


def expired(scoring: ScoringRows, now: datetime, protocol: LockboxProtocol = PROTOCOL) -> bool:
    """Pure: the lockbox closed short of the threshold, so Gate 2 is NOT PASSED and the lockbox is never scored."""
    return closed(now, protocol) and not ready_to_score(scoring, protocol)


def tuning_cutoff(league: str | None, protocol: LockboxProtocol = PROTOCOL) -> datetime | None:
    """Pure: the kickoff from which a tuning or backtest run on `league` (None: every league in the database, the League
    One/Two default) must not score a match, so the lockbox never informs a setting; None for a league outside it."""
    return protocol.start if league is None or league in protocol.leagues else None


@dataclass(frozen=True)
class Coverage:
    """How much of the lockbox the evidence trails caught (closing-price review, 2026-10-25): of the protocol matches that
    have kicked off, how many have an eligible prediction logged before kickoff, a closing-price capture attempt, and a
    captured closing price. A match missed while the machine was off is otherwise invisible."""

    matches: int
    predicted: int
    closing_tried: int
    closing_priced: int
    outcomes_due: int = 0
    outcomes_captured: int = 0


def coverage(kicked_off: Mapping[int, datetime], records: Sequence[Mapping[str, Any]], closing_tried: Collection[int],
             closing_priced: Collection[int], protocol: LockboxProtocol = PROTOCOL, *,
             captured: Collection[int] = (), now: datetime | None = None) -> Coverage:
    """Pure: `kicked_off` is each protocol-league match that has kicked off, as stored (vendor id -> UTC kickoff); `records`
    the prediction log; `closing_tried` the matches the closing capture ran for, `closing_priced` those it found prices for.
    Matches before the start, from the end on, or examined before the restart are not lockbox matches and are left out.
    With `now`, the predicted matches whose kickoff is at least the revision grace before it are due an outcome capture, and `captured`
    (the matches with a captured outcome) says how many have one."""
    matches = {fixture_id for fixture_id, kickoff in kicked_off.items()
               if protocol.start <= kickoff < protocol.end and fixture_id not in protocol.examined_fixtures}
    predicted = {int(r["fixture_id"]) for r in eligible(records, kicked_off, protocol).eligible} & matches
    due = set() if now is None else {f for f in predicted if kicked_off[f] + protocol.revision_grace <= now}
    return Coverage(len(matches), len(predicted), len(matches & set(closing_tried)), len(matches & set(closing_priced)),
                    len(due), len(due & set(captured)))


def coverage_line(result: Coverage, protocol: LockboxProtocol = PROTOCOL) -> str:
    """Pure: the status report's line for `result`."""
    head = f"Lockbox coverage (kickoff from {protocol.start:%d %b}): "
    if result.matches == 0:
        return head + "no lockbox match has kicked off yet"
    missed = result.matches - result.predicted
    return (head + f"{result.matches} matches kicked off; {result.predicted} with a prediction logged before kickoff"
            + (f" (WARNING: {missed} missed: machine off, or the scan failed)" if missed else "")
            + f"; closing capture ran for {result.closing_tried}, prices for {result.closing_priced}"
            + (f"; outcomes captured for {result.outcomes_captured} of {result.outcomes_due} due"
               + (f" (WARNING: {result.outcomes_due - result.outcomes_captured} not captured: run "
                  "scripts/gate2_prospective.py --capture-only)" if result.outcomes_captured < result.outcomes_due else "")
               if result.outcomes_due else ""))
