"""Pure adapters from TheStatsAPI responses to the payload shapes transform.py parses (no I/O).

The data vendor since 2026-10-13 (maintainer: "stick with TheStatsAPI"; Sportmonks' plan had shrunk to its free leagues).
Every number lands in databases built from TheStatsAPI alone (scripts/tsa_backfill.py), never mixed with the Sportmonks ones:
on Santos v Flamengo (2026-10-08) its tackles differed from Sportmonks/Opta for 4 of 28 players and its interceptions for 5,
while challenges lost and fouls agreed for all (MARKET_CONTRACT.md 2f).

A finished match takes two replies: the match row of the listing (`GET /football/matches`: teams, kickoff, status, score)
and `GET /football/matches/{id}/player-stats` (one row per squad player, numbers grouped under passing, shooting, duels,
defending, goalkeeping and general). Field mapping, checked on mt_986401287 (Santos v Flamengo):

    tackles_attempted  defending.tackles        (the count the tackles market prices; Opta-style "Tackles")
    interceptions      defending.interceptions
    dribbled_past      duels.challenge_lost
    duels_total        duels.duel_won + duels.duel_lost
    duels_won          duels.duel_won
    fouls_committed    general.fouls
    fouls_drawn        general.was_fouled
    yellow/red cards   general.yellow_cards / general.red_cards

Not provided, and stored as stated (none is a pricing input): **tackles_won** has no TheStatsAPI field and is stored as 0
(TACKLES_WON_NOT_PROVIDED); goal and substitution minutes are not fetched (the timeline is a request of its own), so
`goals` is empty, on/off minutes come from minutes played, and every minute a player was on counts as "level" in the
match-state columns; possession and the referee are not fetched.

A player who played must carry the tackles and fouls numbers as integers, or the match is refused: a renamed or missing
field must fail the match, never parse as 0 (transform.py's guard, applied here as TheStatsAPI sends null for numbers it
lacks). A player who did not play has every count 0.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

logger = logging.getLogger("prop_alpha.ingestion.thestatsapi")

BASE_URL: Final[str] = "https://api.thestatsapi.com/api"
KEY_NAME: Final[str] = "THESTATSAPI_KEY"
MATCHES_PATH: Final[str] = "football/matches"
PLAYER_STATS_PATH: Final[str] = "football/matches/{uid}/player-stats"
LINEUPS_PATH: Final[str] = "football/matches/{uid}/lineups"
# TheStatsAPI sends a per-minute budget as X-RateLimit-Remaining and the monthly one as X-Monthly-Quota-Remaining; only the
# monthly one is a quota (the per-minute one is at most 12 on a trial and would trip any reserve at once).
QUOTA_HEADERS: Final[tuple[str, ...]] = ("x-monthly-quota-remaining",)
MAX_PER_PAGE: Final[int] = 100
# TheStatsAPI competition id -> prop_alpha.leagues.name (the names the frozen scan's queries filter on).
COMPETITIONS: Final[Mapping[str, str]] = {
    "comp_0196": "League One",
    "comp_4023": "League Two",
    "comp_4795": "Brazil Serie A",
    "comp_6387": "Scottish Premiership",
    "comp_6236": "Scottish Championship",
}
# Which database each league lives in: the working one (League One/Two, the frozen model) or the pilot one.
WORKING_LEAGUES: Final[frozenset[str]] = frozenset({"League One", "League Two"})
# The opportunity scan's competitions and the pilot scan's (competition id -> prop_alpha.leagues.name).
SCAN_COMPETITIONS: Final[Mapping[str, str]] = {c: n for c, n in COMPETITIONS.items() if n in WORKING_LEAGUES}
PILOT_COMPETITIONS: Final[Mapping[str, str]] = {c: n for c, n in COMPETITIONS.items() if n not in WORKING_LEAGUES}
# Every listed match id is "mt_" and nine digits (2,408 of 2,408 on 2026-10-13; 235 start with a zero). The frozen pricing
# code keys a fixture by an integer (slate.UpcomingFixture.fixture_id), so the nine digits are that integer and are padded
# back to nine to rebuild the id; any other shape is refused rather than guessed.
MATCH_ID: Final[re.Pattern[str]] = re.compile(r"mt_(\d{9})")
POSITIONS: Final[Mapping[str, str]] = {"G": "GK", "D": "DEF", "M": "MID", "F": "ATT"}
STATUSES: Final[Mapping[str, str]] = {"finished": "final", "scheduled": "scheduled", "live": "live", "postponed": "postponed"}
TACKLES_WON_NOT_PROVIDED: Final[int] = 0
# Numbers a player who played must carry (as integers); the others default to 0 when null.
REQUIRED: Final[tuple[tuple[str, str], ...]] = (("defending", "tackles"), ("general", "fouls"))


def fixture_number(match_id: str) -> int:
    """'mt_010487045' -> 10487045 (the integer the frozen pricing code keys a fixture by)."""
    found = MATCH_ID.fullmatch(match_id)
    if found is None:
        raise ValueError(f"match id {match_id!r} is not 'mt_' and nine digits")
    return int(found.group(1))


def match_uid(fixture_number_: int) -> str:
    """10487045 -> 'mt_010487045' (the vendor uid the databases store)."""
    if isinstance(fixture_number_, bool) or not isinstance(fixture_number_, int) or not 0 <= fixture_number_ < 10 ** 9:
        raise ValueError(f"{fixture_number_!r} is not a TheStatsAPI match number")
    return f"mt_{fixture_number_:09d}"


def upcoming_fixture(match: Mapping[str, Any], competitions: Mapping[str, str]) -> dict[str, Any] | None:
    """A scheduled match of `competitions` as slate.UpcomingFixture's fields; None for any other status or competition."""
    league = competitions.get(str(match.get("competition_id")))
    if league is None or match.get("status") != "scheduled":
        return None
    home, home_name = _team(match, "home")
    away, away_name = _team(match, "away")
    return {"fixture_id": fixture_number(str(match.get("id"))), "league": league, "home_team_uid": home,
            "away_team_uid": away, "home_name": home_name, "away_name": away_name, "kickoff_at": kickoff(match)}


def lineup_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """A line-ups reply (`GET /football/matches/{id}/lineups`) as the payload parse_lineup_payload expects, with
    `fixture.confirmed` from the reply: TheStatsAPI also serves predicted line-ups before the clubs announce theirs, and
    only a confirmed sheet is a team sheet. A starter without an id refuses the sheet; a substitute without one is left out."""
    data = body.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("malformed TheStatsAPI line-ups reply (expected a data object)")
    uid = str(data.get("match_id"))
    sheets: dict[str, dict[str, list[str]]] = {}
    sides: list[str] = []
    for side in ("home", "away"):
        team = data.get(side)
        if not isinstance(team, Mapping) or not isinstance(team.get("id"), str):
            raise ValueError(f"match {uid}: line-ups have no {side} team")
        starters: list[str] = []
        bench: list[str] = []
        for group, target in (("starting_xi", starters), ("substitutes", bench)):
            for player in team.get(group) or []:
                pid = player.get("id") if isinstance(player, Mapping) else None
                if not isinstance(pid, str):
                    if group == "starting_xi":
                        raise ValueError(f"match {uid}: {side} starting XI lists a player without an id")
                    continue
                target.append(pid)
        sheets[str(team["id"])] = {"starters": starters, "bench": bench}
        sides.append(str(team["id"]))
    return {"fixture": {"uid": uid, "home_team_uid": sides[0], "away_team_uid": sides[1],
                        "confirmed": data.get("confirmed") is True},
            "lineups": sheets}


def matches_path(competition_id: str, date_from: str, date_to: str, page: int, status: str | None = None) -> str:
    """One page of a competition's matches between two dates (YYYY-MM-DD, inclusive), soonest first."""
    if competition_id not in COMPETITIONS:
        raise ValueError(f"{competition_id!r} is not a competition this project uses")
    query = (f"{MATCHES_PATH}?competition_id={competition_id}&date_from={date_from}&date_to={date_to}"
             f"&per_page={MAX_PER_PAGE}&page={page}&sort=utc_date")
    return query + (f"&status={status}" if status else "")


def match_rows(body: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], int]:
    """The match rows of a listing reply and its total number of pages."""
    data = body.get("data")
    meta = body.get("meta")
    if not isinstance(data, list) or not isinstance(meta, Mapping) or not isinstance(meta.get("total_pages"), int):
        raise ValueError("malformed TheStatsAPI match listing (expected data list and meta.total_pages)")
    return [row for row in data if isinstance(row, Mapping)], int(meta["total_pages"])


def kickoff(match: Mapping[str, Any]) -> datetime:
    raw = match.get("utc_date")
    if not isinstance(raw, str):
        raise ValueError(f"match {match.get('id')}: no utc_date")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"match {match.get('id')}: utc_date has no time zone")
    return parsed.astimezone(timezone.utc)


def status(match: Mapping[str, Any]) -> str:
    raw = match.get("status")
    if raw not in STATUSES:
        raise ValueError(f"match {match.get('id')}: status {raw!r} cannot be stored (cancelled or unknown)")
    return STATUSES[str(raw)]


def _team(match: Mapping[str, Any], side: str) -> tuple[str, str]:
    team = match.get(f"{side}_team")
    if not isinstance(team, Mapping) or not isinstance(team.get("id"), str):
        raise ValueError(f"match {match.get('id')}: no {side} team")
    return str(team["id"]), str(team.get("name") or team["id"])


def _number(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{where} is not a whole number ({value!r})")
    return int(value)


def _group(row: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    group = row.get(name)
    return group if isinstance(group, Mapping) else {}


STARTING_XI: Final[int] = 11


def _sides(uid: str, home: str, away: str, rows: Sequence[Any]) -> dict[str, str | None]:
    """Player id -> the side he played for, or None for a substitute who cannot be placed. A row's team_id is used when it is
    one of the two sides, else its club_team_id when that is. TheStatsAPI files some loan players under another club's id
    (e.g. Jid Okeke, Notts County v Walsall, 2025-12-20: team and club both other clubs', 90 minutes for Walsall), so starters
    still unplaced are given to the one side that is short of a full starting XI, when they exactly fill it; any other case
    refuses the match, since a starting XI must be kept whole."""
    sides: dict[str, str | None] = {}
    unplaced_starters: list[str] = []
    starters = {home: 0, away: 0}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("player_id"), str):
            raise ValueError(f"match {uid}: a player-stats row has no player_id")
        pid = str(row["player_id"])
        if not isinstance(row.get("started"), bool):
            raise ValueError(f"match {uid}, player {pid}: 'started' is not a boolean")
        team = next((str(t) for t in (row.get("team_id"), row.get("club_team_id")) if str(t) in (home, away)), None)
        sides[pid] = team
        if row["started"]:
            if team is None:
                unplaced_starters.append(pid)
            else:
                starters[team] += 1
    if unplaced_starters:
        short = [side for side, count in starters.items() if count < STARTING_XI]
        if len(short) != 1 or STARTING_XI - starters[short[0]] != len(unplaced_starters):
            raise ValueError(f"match {uid}, player {unplaced_starters[0]}: starter's team is neither side and the starting "
                             f"XIs ({starters[home]} and {starters[away]}) do not say which, so a starting XI cannot be kept whole")
        for pid in unplaced_starters:
            sides[pid] = short[0]
    return sides


def fixture_payload(match: Mapping[str, Any], player_stats: Mapping[str, Any]) -> dict[str, Any]:
    """A listed match and its player-stats reply as the payload parse_fixture_payload expects."""
    uid = str(match.get("id"))
    home, _ = _team(match, "home")
    away, _ = _team(match, "away")
    state = status(match)
    raw_score = match.get("score")
    score: Mapping[str, Any] = raw_score if isinstance(raw_score, Mapping) else {}
    rows = player_stats.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"match {uid}: malformed player-stats reply (expected a data list)")
    players: list[dict[str, Any]] = []
    unplaced: list[str] = []
    sides = _sides(uid, home, away, rows)
    for row in rows:
        pid = str(row["player_id"])
        where = f"match {uid}, player {pid}"
        team = sides[pid]
        if team is None:
            # Seen on about 1 match in 25 of 2025/26 League One/Two (e.g. Kevin Berkoe, Salford v Crawley, 2025-11-29, 1 minute):
            # a substitute whose team_id and club_team_id are both other clubs'. As for Sportmonks' unidentified substitutes
            # (maintainer decision 2026-10-04) he is left out and logged, and the match is loaded; his raw row stays in
            # vendor_response.
            unplaced.append(f"{row.get('player_name') or pid} ({row.get('minutes_played') or 0} min, team {row.get('team_id')})")
            continue
        minutes = _number(row.get("minutes_played"), f"{where}: minutes_played") or 0
        played = bool(row.get("played")) or minutes > 0
        if state == "final" and row["started"] and minutes <= 0:
            raise ValueError(f"{where}: started but has no minutes_played")
        duels, defending, general = _group(row, "duels"), _group(row, "defending"), _group(row, "general")
        if played:
            for group, field in REQUIRED:
                if _number(_group(row, group).get(field), f"{where}: {group}.{field}") is None:
                    raise ValueError(f"{where}: played {minutes} minutes but {group}.{field} is missing")

        def count(group: Mapping[str, Any], field: str) -> int:
            return (_number(group.get(field), f"{where}: {field}") or 0) if played else 0

        won, lost = count(duels, "duel_won"), count(duels, "duel_lost")
        stats = {
            "minutes": minutes, "tackles_attempted": count(defending, "tackles"), "tackles_won": TACKLES_WON_NOT_PROVIDED,
            "interceptions": count(defending, "interceptions"), "dribbled_past": count(duels, "challenge_lost"),
            "duels_total": won + lost, "duels_won": won, "fouls_committed": count(general, "fouls"),
            "fouls_drawn": count(general, "was_fouled"), "yellow_cards": count(general, "yellow_cards"),
            "red_cards": count(general, "red_cards"),
        }
        # An empty position means none was given (mt_732019591, 2026-10-07): stored as no position, like a missing one.
        position = row.get("position") or None
        if position is not None and position not in POSITIONS:
            raise ValueError(f"{where}: unknown position {position!r}")
        # general.player_subbed_on / player_subbed_off name the OTHER player of a substitution (an id, not a minute), so
        # no minute_on/minute_off is sent: transform.py derives them from minutes_played (starters from 0, substitutes up
        # to 90). They feed only the match-state columns, which no price reads.
        players.append({"uid": pid, "name": " ".join(str(row.get("player_name") or pid).split()),
                        "position": None if position is None else POSITIONS[str(position)],
                        "team_uid": team, "started": row["started"], "stats": stats})
    if unplaced:
        logger.warning("match %s: substitutes left out, their team is neither side: %s", uid, ", ".join(unplaced))
    if state == "final" and not any(p["stats"]["minutes"] > 0 for p in players):
        raise ValueError(f"match {uid}: finished but no player has minutes (coverage gap)")
    return {
        "fixture": {"uid": uid, "home_team_uid": home, "away_team_uid": away, "kickoff_at": kickoff(match).isoformat(),
                    "status": state, "home_score": _number(score.get("home"), f"match {uid}: home score"),
                    "away_score": _number(score.get("away"), f"match {uid}: away score"),
                    "matchday": _number(match.get("matchday"), f"match {uid}: matchday")},
        "referee": None,
        "teams": {},
        "goals": [],
        "players": players,
        # Both untouched replies ride along so raw_payloads keeps every TheStatsAPI number (parse_fixture_payload ignores it).
        "vendor_response": {"match": dict(match), "player_stats": dict(player_stats)},
    }


def clubs(matches: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str, datetime]]:
    """Club id -> (name, competition id, kickoff) of its most recent listed match: the league a club is filed under."""
    latest: dict[str, tuple[str, str, datetime]] = {}
    for match in matches:
        at = kickoff(match)
        for side in ("home", "away"):
            team, name = _team(match, side)
            if team not in latest or at >= latest[team][2]:
                latest[team] = (name, str(match.get("competition_id")), at)
    return latest
