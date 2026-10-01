"""Pure parsing and feature transforms for vendor fixture payloads (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping, Sequence

from prop_alpha.modeling.engine import MAX_MATCH_MINUTES

VALID_STATUSES: Final[frozenset[str]] = frozenset({"scheduled", "live", "final", "postponed"})

# Standard linear possession adjustment until fitted values (fit_possession_kappa) are loaded.
DEFAULT_KAPPAS: Final[Mapping[str, float]] = MappingProxyType({"tackles": 1.0, "interceptions": 1.0, "fouls": 1.0})

STATS_COLUMNS: Final[tuple[str, ...]] = (
    "fixture_id", "player_id", "team_id", "is_home", "started",
    "minute_on", "minute_off", "minutes_played",
    "match_state_leading_min", "match_state_level_min", "match_state_trailing_min",
    "tackles_attempted", "tackles_won", "interceptions", "dribbled_past",
    "duels_total", "duels_won",
    "fouls_committed", "fouls_drawn", "yellow_cards", "red_cards",
    "zone14_entries", "halfspace_entries", "long_balls_targeted",
    "team_possession_pct",
    "padj_tackles", "padj_interceptions", "padj_fouls", "dribbled_past_rate",
)


def padj_scale(raw_count: float, team_possession_pct: float | None, kappa: float) -> float:
    """raw * (50 / opponent_possession)^kappa; deflates actions made while the opponent dominates the ball."""
    if team_possession_pct is None:
        return float(raw_count)
    opponent_possession = 100.0 - min(max(float(team_possession_pct), 1.0), 99.0)
    return float(raw_count) * (50.0 / opponent_possession) ** kappa


def dribbled_past_rate(dribbled_past: int, tackles_attempted: int) -> float | None:
    denominator = dribbled_past + tackles_attempted
    if denominator == 0:
        return None
    return dribbled_past / denominator


@dataclass(frozen=True)
class GoalEvent:
    minute: int
    team_uid: str


def match_state_minutes(
    goals: Sequence[GoalEvent], team_uid: str, minute_on: int, minute_off: int
) -> tuple[int, int, int]:
    """Minutes spent (leading, level, trailing) by `team_uid` while the player was on the pitch."""
    if minute_off <= minute_on:
        return 0, 0, 0
    cuts = sorted({g.minute for g in goals if minute_on < g.minute < minute_off})
    edges = [minute_on, *cuts, minute_off]
    leading = level = trailing = 0
    for start, end in zip(edges[:-1], edges[1:]):
        diff = sum(1 if g.team_uid == team_uid else -1 for g in goals if g.minute <= start)
        span = end - start
        if diff > 0:
            leading += span
        elif diff < 0:
            trailing += span
        else:
            level += span
    return leading, level, trailing


@dataclass(frozen=True)
class FixtureRecord:
    vendor_uid: str
    home_team_uid: str
    away_team_uid: str
    kickoff_at: str
    status: str
    home_score: int | None
    away_score: int | None
    matchday: int | None


@dataclass(frozen=True)
class RefereeRecord:
    vendor_uid: str
    name: str


@dataclass(frozen=True)
class PlayerMatchRecord:
    player_uid: str
    name: str
    position: str
    team_uid: str
    is_home: bool
    started: bool
    minute_on: int
    minute_off: int
    minutes_played: int
    state_leading_min: int
    state_level_min: int
    state_trailing_min: int
    tackles_attempted: int
    tackles_won: int
    interceptions: int
    dribbled_past: int
    duels_total: int
    duels_won: int
    fouls_committed: int
    fouls_drawn: int
    yellow_cards: int
    red_cards: int
    zone14_entries: int
    halfspace_entries: int
    long_balls_targeted: int
    team_possession_pct: float | None

    def __post_init__(self) -> None:
        counts = (
            self.minute_on, self.minute_off, self.minutes_played, self.tackles_attempted, self.tackles_won,
            self.interceptions, self.dribbled_past, self.duels_total, self.duels_won, self.fouls_committed,
            self.fouls_drawn, self.zone14_entries, self.halfspace_entries, self.long_balls_targeted,
        )
        if any(c < 0 for c in counts):
            raise ValueError(f"player {self.player_uid}: negative count in vendor stats")
        if self.minute_off < self.minute_on:
            raise ValueError(f"player {self.player_uid}: minute_off precedes minute_on")
        if self.minutes_played > MAX_MATCH_MINUTES:
            raise ValueError(f"player {self.player_uid}: minutes_played exceeds {MAX_MATCH_MINUTES}")
        if self.tackles_won > self.tackles_attempted:
            raise ValueError(f"player {self.player_uid}: tackles_won exceeds tackles_attempted")
        if self.duels_won > self.duels_total:
            raise ValueError(f"player {self.player_uid}: duels_won exceeds duels_total")
        if not 0 <= self.yellow_cards <= 2 or not 0 <= self.red_cards <= 1:
            raise ValueError(f"player {self.player_uid}: card counts out of range")
        if self.team_possession_pct is not None and not 0.0 <= self.team_possession_pct <= 100.0:
            raise ValueError(f"player {self.player_uid}: possession out of range")

    def derived_features(self, kappas: Mapping[str, float]) -> tuple[float, float, float, float | None]:
        return (
            padj_scale(self.tackles_attempted, self.team_possession_pct, kappas["tackles"]),
            padj_scale(self.interceptions, self.team_possession_pct, kappas["interceptions"]),
            padj_scale(self.fouls_committed, self.team_possession_pct, kappas["fouls"]),
            dribbled_past_rate(self.dribbled_past, self.tackles_attempted),
        )


@dataclass(frozen=True)
class ParsedFixture:
    fixture: FixtureRecord
    referee: RefereeRecord | None
    players: tuple[PlayerMatchRecord, ...]


def _count(stats: Mapping[str, Any], key: str) -> int:
    value = stats.get(key)
    return int(value) if value is not None else 0


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def parse_fixture_payload(payload: Mapping[str, Any], match_length: int = 90) -> ParsedFixture:
    """
    Expected shape:
    {
      "fixture": {"uid", "home_team_uid", "away_team_uid", "kickoff_at", "status",
                  "home_score"?, "away_score"?, "matchday"?},
      "referee": {"uid", "name"} | null,
      "teams": {"<team_uid>": {"possession_pct": 47.3}},
      "goals": [{"minute": 23, "team_uid": "..."}],          # team_uid = side credited with the goal
      "players": [{"uid", "name", "position", "team_uid", "started",
                   "minute_on"?, "minute_off"?, "match_state_minutes"?: {"leading","level","trailing"},
                   "stats": {"minutes", "tackles_attempted" | "tackles", "tackles_won", "interceptions",
                             "dribbled_past", "duels_total", "duels_won", "fouls_committed",
                             "fouls_drawn", "yellow_cards", "red_cards", "zone14_entries",
                             "halfspace_entries", "long_balls_targeted"}}]
    }
    """
    fx = payload["fixture"]
    status = str(fx["status"]).lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown fixture status: {status!r}")

    fixture = FixtureRecord(
        vendor_uid=str(fx["uid"]),
        home_team_uid=str(fx["home_team_uid"]),
        away_team_uid=str(fx["away_team_uid"]),
        kickoff_at=str(fx["kickoff_at"]),
        status=status,
        home_score=_optional_int(fx.get("home_score")),
        away_score=_optional_int(fx.get("away_score")),
        matchday=_optional_int(fx.get("matchday")),
    )
    if fixture.home_team_uid == fixture.away_team_uid:
        raise ValueError("home and away team uids are identical")

    referee_raw = payload.get("referee")
    referee = RefereeRecord(str(referee_raw["uid"]), str(referee_raw["name"])) if referee_raw else None

    goals = [GoalEvent(int(g["minute"]), str(g["team_uid"])) for g in payload.get("goals", [])]
    possession = {
        str(uid): float(team["possession_pct"])
        for uid, team in payload.get("teams", {}).items()
        if team.get("possession_pct") is not None
    }

    seen: set[str] = set()
    players: list[PlayerMatchRecord] = []
    for p in payload.get("players", []):
        uid = str(p["uid"])
        if uid in seen:
            raise ValueError(f"duplicate player uid in payload: {uid}")
        seen.add(uid)

        team_uid = str(p["team_uid"])
        if team_uid not in (fixture.home_team_uid, fixture.away_team_uid):
            raise ValueError(f"player {uid} belongs to neither side: {team_uid}")

        stats = p.get("stats", {})
        minutes = _count(stats, "minutes")
        started = bool(p.get("started", False))
        default_on = 0 if started else max(match_length - minutes, 0)
        minute_on = int(p.get("minute_on", default_on))
        minute_off = int(p.get("minute_off", minute_on + minutes))

        explicit_states = p.get("match_state_minutes")
        if explicit_states:
            states = (
                int(explicit_states["leading"]),
                int(explicit_states["level"]),
                int(explicit_states["trailing"]),
            )
        elif minutes > 0:
            states = match_state_minutes(goals, team_uid, minute_on, minute_off)
        else:
            states = (0, 0, 0)

        tackles_attempted = _count(stats, "tackles_attempted") if "tackles_attempted" in stats else _count(stats, "tackles")

        players.append(PlayerMatchRecord(
            player_uid=uid,
            name=str(p["name"]),
            position=str(p["position"]),
            team_uid=team_uid,
            is_home=team_uid == fixture.home_team_uid,
            started=started,
            minute_on=minute_on,
            minute_off=minute_off,
            minutes_played=minutes,
            state_leading_min=states[0],
            state_level_min=states[1],
            state_trailing_min=states[2],
            tackles_attempted=tackles_attempted,
            tackles_won=_count(stats, "tackles_won"),
            interceptions=_count(stats, "interceptions"),
            dribbled_past=_count(stats, "dribbled_past"),
            duels_total=_count(stats, "duels_total"),
            duels_won=_count(stats, "duels_won"),
            fouls_committed=_count(stats, "fouls_committed"),
            fouls_drawn=_count(stats, "fouls_drawn"),
            yellow_cards=_count(stats, "yellow_cards"),
            red_cards=_count(stats, "red_cards"),
            zone14_entries=_count(stats, "zone14_entries"),
            halfspace_entries=_count(stats, "halfspace_entries"),
            long_balls_targeted=_count(stats, "long_balls_targeted"),
            team_possession_pct=possession.get(team_uid),
        ))

    return ParsedFixture(fixture=fixture, referee=referee, players=tuple(players))


def build_stats_row(
    record: PlayerMatchRecord, fixture_id: int, player_id: int, team_id: int, kappas: Mapping[str, float]
) -> tuple:
    """Row ordered exactly as STATS_COLUMNS."""
    padj_tackles, padj_interceptions, padj_fouls, dpr = record.derived_features(kappas)
    return (
        fixture_id, player_id, team_id, record.is_home, record.started,
        record.minute_on, record.minute_off, record.minutes_played,
        record.state_leading_min, record.state_level_min, record.state_trailing_min,
        record.tackles_attempted, record.tackles_won, record.interceptions, record.dribbled_past,
        record.duels_total, record.duels_won,
        record.fouls_committed, record.fouls_drawn, record.yellow_cards, record.red_cards,
        record.zone14_entries, record.halfspace_entries, record.long_balls_targeted,
        record.team_possession_pct,
        padj_tackles, padj_interceptions, padj_fouls, dpr,
    )


# ---------------- staging rows (append-only landing table) ----------------
STAGING_COLUMNS: Final[tuple[str, ...]] = (
    "batch_id", "vendor_fixture_uid", "home_team_uid", "away_team_uid", "kickoff_at", "fixture_status",
    "home_score", "away_score", "matchday", "referee_uid", "referee_name",
    "vendor_player_uid", "player_name", "player_position", "team_uid", "is_home", "started",
    "minute_on", "minute_off", "minutes_played",
    "match_state_leading_min", "match_state_level_min", "match_state_trailing_min",
    "tackles_attempted", "tackles_won", "interceptions", "dribbled_past", "duels_total", "duels_won",
    "fouls_committed", "fouls_drawn", "yellow_cards", "red_cards",
    "zone14_entries", "halfspace_entries", "long_balls_targeted", "team_possession_pct",
    "padj_tackles", "padj_interceptions", "padj_fouls", "dribbled_past_rate",
)
_FIXTURE_PREFIX_WIDTH: Final[int] = 11
_PLAYER_WIDTH: Final[int] = len(STAGING_COLUMNS) - _FIXTURE_PREFIX_WIDTH


def build_staging_rows(parsed: ParsedFixture, kappas: Mapping[str, float], batch_id: str) -> list[tuple]:
    """One fixture header row (player columns NULL) plus one row per player, ordered as STAGING_COLUMNS."""
    fx = parsed.fixture
    prefix = (
        batch_id, fx.vendor_uid, fx.home_team_uid, fx.away_team_uid, fx.kickoff_at, fx.status,
        fx.home_score, fx.away_score, fx.matchday,
        parsed.referee.vendor_uid if parsed.referee else None,
        parsed.referee.name if parsed.referee else None,
    )
    rows: list[tuple] = [prefix + (None,) * _PLAYER_WIDTH]
    for rec in parsed.players:
        padj_tackles, padj_interceptions, padj_fouls, dpr = rec.derived_features(kappas)
        rows.append(prefix + (
            rec.player_uid, rec.name, rec.position, rec.team_uid, rec.is_home, rec.started,
            rec.minute_on, rec.minute_off, rec.minutes_played,
            rec.state_leading_min, rec.state_level_min, rec.state_trailing_min,
            rec.tackles_attempted, rec.tackles_won, rec.interceptions, rec.dribbled_past,
            rec.duels_total, rec.duels_won,
            rec.fouls_committed, rec.fouls_drawn, rec.yellow_cards, rec.red_cards,
            rec.zone14_entries, rec.halfspace_entries, rec.long_balls_targeted, rec.team_possession_pct,
            padj_tackles, padj_interceptions, padj_fouls, dpr,
        ))
    return rows


# ---------------- confirmed team sheets ----------------
@dataclass(frozen=True)
class LineupEntry:
    vendor_fixture_uid: str
    vendor_player_uid: str
    p_start: float
    p_sub_appearance: float


def parse_lineup_payload(payload: Mapping[str, Any], bench_sub_probability: float) -> list[LineupEntry]:
    """
    Expected shape:
    {"fixture": {"uid", "home_team_uid", "away_team_uid"},
     "lineups": {"<team_uid>": {"starters": [player_uid x 11], "bench": [player_uid, ...]}}}
    Both sides are required so that players absent from the sheet can be safely zeroed.
    """
    if not 0.0 <= bench_sub_probability <= 1.0:
        raise ValueError("bench_sub_probability must lie in [0, 1]")
    fx = payload["fixture"]
    fixture_uid = str(fx["uid"])
    sides = (str(fx["home_team_uid"]), str(fx["away_team_uid"]))
    lineups = payload["lineups"]
    missing = [side for side in sides if side not in lineups]
    if missing:
        raise ValueError(f"team sheet incomplete for fixture {fixture_uid}; missing sides {missing}")

    seen: set[str] = set()
    entries: list[LineupEntry] = []
    for side in sides:
        starters = [str(u) for u in lineups[side].get("starters", [])]
        bench = [str(u) for u in lineups[side].get("bench", [])]
        if len(starters) != 11:
            raise ValueError(f"fixture {fixture_uid} side {side}: expected 11 starters, got {len(starters)}")
        for uid, p_start, p_sub in [(u, 1.0, 0.0) for u in starters] + [(u, 0.0, bench_sub_probability) for u in bench]:
            if uid in seen:
                raise ValueError(f"fixture {fixture_uid}: player {uid} listed twice")
            seen.add(uid)
            entries.append(LineupEntry(fixture_uid, uid, p_start, p_sub))
    return entries
