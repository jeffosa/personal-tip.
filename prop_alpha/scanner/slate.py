"""Pure slate preparation for the 4-hourly opportunity scan (no I/O, no clock, randomness only via `rng`).

A scan run at `now` covers fixtures kicking off in [now, next 4-hour boundary), so with runs at 00:00, 04:00, ...
every fixture is scanned once, in the last 4 hours before kickoff (when player markets are listed).

Projections come from each player's own history. The lineup is an estimate from the team's last
RECENT_TEAM_MATCHES fixtures (share started, share used as a substitute), not a confirmed team sheet. The referee
is neutral (NEUTRAL_REFEREE): the scan does not yet load referee coefficients.

`quotes_from_prices` turns real OddsPapi prices into quotes on our fixture and player ids. `mock_quotes` is the
test stand-in when no odds are fetched. It prices each line from the model's own
probability plus random noise and a bookmaker margin, so it exercises the pipeline end to end; it says nothing
about whether real bookmakers misprice anything. Every mock quote carries MOCK_SPORTSBOOK.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Mapping, Sequence

import numpy as np

from prop_alpha.ingestion.oddspapi import UNKNOWN_LIMIT_STAKE, OddsFixture, PropPrice, normalise, team_tokens
from prop_alpha.modeling.engine import (
    MAX_MATCH_MINUTES,
    NEUTRAL_REFEREE,
    FactorProjection,
    LineupExpectation,
    QuantEngine,
    VoidRule,
)
from prop_alpha.scanner.market_scanner import LiveOddsQuote, ProjectionKey

SCAN_INTERVAL: Final[timedelta] = timedelta(hours=4)
# Market -> the Appearance field it counts. Card markets need referee propensities and are not scanned yet.
SCAN_MARKETS: Final[tuple[str, ...]] = ("tackles", "fouls")
RECENT_TEAM_MATCHES: Final[int] = 5
HISTORY_MATCHES: Final[int] = 20
# Books list props for regular starters; a player who started under 40% of recent matches is not priced.
MIN_P_START: Final[float] = 0.4
# Not fitted (PRD Gate 2): the documented reference value until fit_game_state_dispersion runs on the backfill.
SCAN_GAME_STATE_K: Final[float] = 6.0

MOCK_SPORTSBOOK: Final[str] = "MOCK"
MOCK_LINES: Final[tuple[float, ...]] = (0.5, 1.5, 2.5, 3.5)
MOCK_BOOK_MARGIN: Final[float] = 1.06
# Logit-scale error of the mock book. 0.35 produced edges above +100% and 74 alerts in one window; 0.2 keeps
# mock prices in the range of a real, if soft, bookmaker.
MOCK_MISPRICING_SD: Final[float] = 0.2
MOCK_MAX_STAKE: Final[float] = 500.0
_MOCK_PROBABILITY_RANGE: Final[tuple[float, float]] = (0.05, 0.95)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def scan_window(now: datetime) -> tuple[datetime, datetime]:
    """[now, next 4-hour boundary): fixtures kicking off before the next scheduled run."""
    _require_utc(now, "now")
    interval_hours = int(SCAN_INTERVAL.total_seconds() // 3600)
    boundary = now.replace(hour=now.hour - now.hour % interval_hours, minute=0, second=0, microsecond=0)
    return now, boundary + SCAN_INTERVAL


@dataclass(frozen=True)
class UpcomingFixture:
    fixture_id: int
    league: str
    home_team_uid: str
    away_team_uid: str
    home_name: str
    away_name: str
    kickoff_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.kickoff_at, "kickoff_at")
        if self.home_team_uid == self.away_team_uid:
            raise ValueError(f"fixture {self.fixture_id}: home and away teams are identical")

    @property
    def name(self) -> str:
        return f"{self.home_name} v {self.away_name}"


@dataclass(frozen=True)
class SquadPlayer:
    player_id: int
    name: str
    position: str
    team_uid: str


@dataclass(frozen=True)
class Appearance:
    """One past match of one player. `fixture_id` is the database id, `team_uid` the side he played for."""

    player_id: int
    fixture_id: int
    team_uid: str
    kickoff_at: datetime
    started: bool
    minutes: float
    tackles: int
    fouls: int

    def __post_init__(self) -> None:
        _require_utc(self.kickoff_at, "kickoff_at")
        if not (math.isfinite(self.minutes) and 0.0 <= self.minutes <= MAX_MATCH_MINUTES):
            raise ValueError(f"player {self.player_id}: minutes {self.minutes!r} out of range")
        if self.tackles < 0 or self.fouls < 0:
            raise ValueError(f"player {self.player_id}: negative count")

    def count(self, market: str) -> int:
        if market == "tackles":
            return self.tackles
        if market == "fouls":
            return self.fouls
        raise ValueError(f"unsupported scan market {market!r}")


def lineup_estimate(
    team_recent_fixtures: Sequence[int], history: Sequence[Appearance]
) -> LineupExpectation | None:
    """Share of the team's recent fixtures the player started / came on in; minutes from his own history."""
    recent = set(team_recent_fixtures)
    if not recent:
        return None
    in_recent = [a for a in history if a.fixture_id in recent]
    starts = sum(1 for a in in_recent if a.started)
    subs = sum(1 for a in in_recent if not a.started and a.minutes > 0.0)
    start_minutes = [a.minutes for a in history if a.started and a.minutes > 0.0]
    sub_minutes = [a.minutes for a in history if not a.started and a.minutes > 0.0]
    return LineupExpectation(
        p_start=starts / len(recent),
        p_sub_appearance=subs / len(recent),
        exp_minutes_start=float(np.mean(start_minutes)) if start_minutes else 85.0,
        exp_minutes_sub=float(np.mean(sub_minutes)) if sub_minutes else 20.0,
    )


def build_projections(
    fixture: UpcomingFixture,
    squad: Sequence[SquadPlayer],
    history_by_player: Mapping[int, Sequence[Appearance]],
    team_recent_fixtures: Mapping[str, Sequence[int]],
    league_rates: Mapping[tuple[str, str], float],
    engine: QuantEngine,
) -> tuple[dict[ProjectionKey, FactorProjection], list[str]]:
    """Projections for every regular starter and scan market, plus the reasons players were left out.

    `league_rates` maps (position, market) to the league's trailing rate per 90; only history strictly before
    kickoff is used.
    """
    projections: dict[ProjectionKey, FactorProjection] = {}
    skipped: list[str] = []
    for player in sorted(squad, key=lambda p: p.player_id):
        history = sorted(
            (a for a in history_by_player.get(player.player_id, ()) if a.kickoff_at < fixture.kickoff_at),
            key=lambda a: (a.kickoff_at, a.fixture_id),
        )
        lineup = lineup_estimate(team_recent_fixtures.get(player.team_uid, ()), history)
        if lineup is None or lineup.p_start < MIN_P_START:
            continue
        on_pitch = [a for a in history if a.minutes > 0.0][-HISTORY_MATCHES:]
        for market in SCAN_MARKETS:
            rate = league_rates.get((player.position, market))
            if rate is None or not (math.isfinite(rate) and rate > 0.0):
                skipped.append(f"{player.name}: no league {market} rate for position {player.position}")
                continue
            projections[(fixture.fixture_id, player.player_id, market)] = engine.factor_count_market(
                fixture_id=fixture.fixture_id,
                player_id=player.player_id,
                market_type=market,
                counts=[a.count(market) for a in on_pitch],
                minutes=[a.minutes for a in on_pitch],
                league_rate_per90=rate,
                referee=NEUTRAL_REFEREE,
                is_home=player.team_uid == fixture.home_team_uid,
                lineup=lineup,
            )
    return projections, skipped


def mock_quotes(
    projections: Mapping[ProjectionKey, FactorProjection], captured_at: datetime, rng: np.random.Generator
) -> list[LiveOddsQuote]:
    """Two-way MOCK quotes: the model's settled-over probability, moved by N(0, MOCK_MISPRICING_SD) on the logit
    scale (the mock book's error), with MOCK_BOOK_MARGIN applied to both sides."""
    _require_utc(captured_at, "captured_at")
    low, high = _MOCK_PROBABILITY_RANGE
    quotes: list[LiveOddsQuote] = []
    for key in sorted(projections):
        projection = projections[key]
        for line in MOCK_LINES:
            over = projection.side_probabilities(line, "over", VoidRule.VOID_IF_NO_APPEARANCE)
            settled = over.p_win + over.p_lose
            if settled <= 0.0:
                continue
            p_over = over.p_win / settled
            if not low < p_over < high:
                continue
            book_logit = math.log(p_over / (1.0 - p_over)) + float(rng.normal(0.0, MOCK_MISPRICING_SD))
            book_over = 1.0 / (1.0 + math.exp(-book_logit))
            quotes.append(LiveOddsQuote(
                key[0], key[1], key[2], line,
                over_price=round(1.0 / (book_over * MOCK_BOOK_MARGIN), 3),
                under_price=round(1.0 / ((1.0 - book_over) * MOCK_BOOK_MARGIN), 3),
                max_stake=MOCK_MAX_STAKE,
                sportsbook=MOCK_SPORTSBOOK,
                captured_at=captured_at,
                void_rule=VoidRule.VOID_IF_NO_APPEARANCE,
            ))
    return quotes


# ---------------- real odds (OddsPapi) -> LiveOddsQuote ----------------
FIXTURE_KICKOFF_TOLERANCE: Final[timedelta] = timedelta(minutes=15)


def match_fixture(
    odds_fixture: OddsFixture, names: Mapping[int, str], fixtures: Sequence[UpcomingFixture]
) -> UpcomingFixture | None:
    """The one scanned fixture with the same kickoff (±15 min) whose home and away clubs share a distinctive
    name word with OddsPapi's; None when there is no such fixture or more than one."""
    home, away = team_tokens(names.get(odds_fixture.home_id, "")), team_tokens(names.get(odds_fixture.away_id, ""))
    hits = [
        fixture for fixture in fixtures
        if abs(fixture.kickoff_at - odds_fixture.kickoff_at) <= FIXTURE_KICKOFF_TOLERANCE
        and team_tokens(fixture.home_name) & home and team_tokens(fixture.away_name) & away
    ]
    return hits[0] if len(hits) == 1 else None


def match_player(name: str, squad: Sequence[SquadPlayer]) -> SquadPlayer | None:
    """Exact normalised full name, else a unique surname plus first initial; None when absent or ambiguous."""
    wanted = normalise(name)
    if not wanted:
        return None
    exact = [player for player in squad if normalise(player.name) == wanted]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    words = wanted.split()
    loose = [
        player for player in squad
        if normalise(player.name).split()[-1:] == words[-1:] and normalise(player.name)[:1] == words[0][:1]
    ]
    return loose[0] if len(loose) == 1 else None


@dataclass(frozen=True)
class OddsCoverage:
    quotes: tuple[LiveOddsQuote, ...]
    prices: int
    matched_prices: int
    unmatched_fixtures: tuple[str, ...]
    unmatched_players: tuple[str, ...]


def quotes_from_prices(
    prices: Sequence[PropPrice],
    names: Mapping[int, str],
    fixtures: Sequence[UpcomingFixture],
    squads: Mapping[int, Sequence[SquadPlayer]],
    captured_at: datetime,
) -> OddsCoverage:
    """Pair Over and Under of each bookmaker's player line into LiveOddsQuotes on our fixture and player ids.

    The quote's max_stake is the smaller published limit of its sides, or UNKNOWN_LIMIT_STAKE when none is
    published. captured_at is when the odds were fetched, not when the price last changed.
    """
    _require_utc(captured_at, "captured_at")
    sides: dict[tuple[int, int, str, float, str], dict[str, PropPrice]] = {}
    unmatched_fixtures: set[str] = set()
    unmatched_players: set[str] = set()
    fixture_cache: dict[str, UpcomingFixture | None] = {}
    matched = 0
    for price in prices:
        odds_id = price.fixture.odds_fixture_id
        if odds_id not in fixture_cache:
            fixture_cache[odds_id] = match_fixture(price.fixture, names, fixtures)
        fixture = fixture_cache[odds_id]
        if fixture is None:
            unmatched_fixtures.add(f"{names.get(price.fixture.home_id, price.fixture.home_id)} v "
                                   f"{names.get(price.fixture.away_id, price.fixture.away_id)}")
            continue
        player = match_player(price.player_name, squads.get(fixture.fixture_id, ()))
        if player is None:
            unmatched_players.add(f"{price.player_name} ({fixture.name})")
            continue
        matched += 1
        key = (fixture.fixture_id, player.player_id, price.market, price.line, price.bookmaker)
        sides.setdefault(key, {})[price.side] = price
    quotes: list[LiveOddsQuote] = []
    for (fixture_id, player_id, market, line, bookmaker), pair in sorted(sides.items()):
        limits = [p.limit for p in pair.values() if p.limit is not None]
        quotes.append(LiveOddsQuote(
            fixture_id, player_id, market, line,
            over_price=pair["over"].price if "over" in pair else None,
            under_price=pair["under"].price if "under" in pair else None,
            max_stake=min(limits) if limits else UNKNOWN_LIMIT_STAKE,
            sportsbook=bookmaker,
            captured_at=captured_at,
            void_rule=VoidRule.VOID_IF_NO_APPEARANCE,
        ))
    return OddsCoverage(tuple(quotes), len(prices), matched, tuple(sorted(unmatched_fixtures)),
                        tuple(sorted(unmatched_players)))
