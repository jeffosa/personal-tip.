"""Pure adapters from Sportmonks Football API v3 responses to the payload shapes transform.py parses (no I/O).

Sportmonks lists a player statistic only when it is non-zero, so an absent TACKLES row means 0 tackles. That
defeats transform.py's per-player "required field" guard against vendor schema drift, so the guard moves here
to the fixture: a finished fixture whose lineups carry no MINUTES_PLAYED, TACKLES or FOULS rows at all, or a
player who started without MINUTES_PLAYED, raises instead of manufacturing zeros.

Field mapping verified on fixture 19727702 (League Two, 2026-09-19): SUBSTITUTION events carry the incoming
player as player_id and the outgoing one as related_player_id; GOAL events credit participant_id; lineup
type_id 11 is the starting XI and 12 the bench.

TACKLES is the tackle count the tackles market prices. TACKLES_WON comes from a different count: in the
2025/26 League One backfill it exceeded TACKLES in about a third of fixtures, and TACKLES_WON_PERCENTAGE
disagrees with both (e.g. 4 tackles, 4 won, 80%). By maintainer decision (2026-10-03) tackles_won is stored
as min(TACKLES_WON, TACKLES), each capped player is logged, and the original figure stays in
`vendor_response`. tackles_won is not a pricing input.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

logger = logging.getLogger("prop_alpha.ingestion.sportmonks")

BASE_URL: Final[str] = "https://api.sportmonks.com/v3/football"
STATS_INCLUDE: Final[str] = "participants;lineups.details.type;events.type;referees;state;scores;statistics.type"
LINEUPS_INCLUDE: Final[str] = "participants;lineups"
STATS_PATH: Final[str] = "fixtures/{uid}?include=" + STATS_INCLUDE
LINEUPS_PATH: Final[str] = "fixtures/{uid}?include=" + LINEUPS_INCLUDE

STARTING_XI: Final[int] = 11
BENCH: Final[int] = 12
POSITIONS: Final[Mapping[int, str]] = {24: "GK", 25: "DEF", 26: "MID", 27: "ATT"}

FINAL_STATES: Final[frozenset[str]] = frozenset({"FT", "AET", "FT_PEN"})
SCHEDULED_STATES: Final[frozenset[str]] = frozenset({"NS", "TBA"})
LIVE_STATES: Final[frozenset[str]] = frozenset({
    "INPLAY_1ST_HALF", "HT", "INPLAY_2ND_HALF", "BREAK", "INPLAY_ET", "EXTRA_TIME_BREAK", "INPLAY_PENALTIES", "PEN_BREAK",
})
POSTPONED_STATES: Final[frozenset[str]] = frozenset({"POSTPONED", "DELAYED"})
GOAL_EVENTS: Final[frozenset[str]] = frozenset({"GOAL", "PENALTY"})
OWN_GOAL_EVENT: Final[str] = "OWNGOAL"

# Sportmonks player detail -> transform.py stats key. Absent detail = 0 (Sportmonks omits zeros).
DETAIL_FIELDS: Final[Mapping[str, str]] = {
    "TACKLES": "tackles_attempted",
    "TACKLES_WON": "tackles_won",
    "INTERCEPTIONS": "interceptions",
    "DRIBBLED_PAST": "dribbled_past",
    "TOTAL_DUELS": "duels_total",
    "DUELS_WON": "duels_won",
    "FOULS": "fouls_committed",
    "FOULS_DRAWN": "fouls_drawn",
}
COVERAGE_DETAILS: Final[tuple[str, ...]] = ("MINUTES_PLAYED", "TACKLES", "FOULS")


def _data(body: Mapping[str, Any]) -> Mapping[str, Any]:
    data = body.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("Sportmonks response has no 'data' object")
    return data


def _type_name(item: Mapping[str, Any]) -> str:
    kind = item.get("type")
    return str(kind.get("developer_name")) if isinstance(kind, Mapping) else ""


def _status(fixture: Mapping[str, Any]) -> str:
    state = fixture.get("state")
    name = str(state.get("developer_name")) if isinstance(state, Mapping) else ""
    if name in FINAL_STATES:
        return "final"
    if name in SCHEDULED_STATES:
        return "scheduled"
    if name in LIVE_STATES:
        return "live"
    if name in POSTPONED_STATES:
        return "postponed"
    raise ValueError(f"fixture {fixture.get('id')}: unsupported Sportmonks state {name!r}")


def _sides(fixture: Mapping[str, Any]) -> tuple[str, str]:
    by_location: dict[str, str] = {}
    for participant in fixture.get("participants") or []:
        meta = participant.get("meta") or {}
        by_location[str(meta.get("location"))] = str(participant["id"])
    if "home" not in by_location or "away" not in by_location:
        raise ValueError(f"fixture {fixture.get('id')}: participants lack a home and an away side")
    return by_location["home"], by_location["away"]


def _kickoff(fixture: Mapping[str, Any]) -> str:
    timestamp = fixture.get("starting_at_timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError(f"fixture {fixture.get('id')}: starting_at_timestamp missing")
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _current_score(fixture: Mapping[str, Any], home: str, away: str) -> tuple[int | None, int | None]:
    goals: dict[str, int] = {}
    for score in fixture.get("scores") or []:
        if score.get("description") == "CURRENT":
            goals[str(score["participant_id"])] = int(score["score"]["goals"])
    return goals.get(home), goals.get(away)


def _goals(fixture: Mapping[str, Any], home: str, away: str, final_score: tuple[int | None, int | None]) -> list[dict[str, Any]]:
    """Goal events credited to the scoring side. Own-goal attribution is not documented, so both readings are
    tried and the one that reproduces the final score is used; if neither or both do, the fixture fails."""
    regular: list[dict[str, Any]] = []
    own: list[tuple[int, str]] = []
    for event in fixture.get("events") or []:
        name = _type_name(event)
        if name in GOAL_EVENTS:
            regular.append({"minute": int(event["minute"]), "team_uid": str(event["participant_id"])})
        elif name == OWN_GOAL_EVENT:
            own.append((int(event["minute"]), str(event["participant_id"])))
    if final_score[0] is None or final_score[1] is None:
        if own:
            raise ValueError(f"fixture {fixture.get('id')}: own goal without a final score to attribute it")
        return regular
    other = {home: away, away: home}
    readings = [regular] if not own else [
        regular + [{"minute": minute, "team_uid": team} for minute, team in own],
        regular + [{"minute": minute, "team_uid": other[team]} for minute, team in own],
    ]
    matching = [
        reading for reading in readings
        if (sum(g["team_uid"] == home for g in reading), sum(g["team_uid"] == away for g in reading)) == final_score
    ]
    if len(matching) != 1:
        raise ValueError(f"fixture {fixture.get('id')}: goal events do not uniquely reproduce the score {final_score}")
    return matching[0]


def _possession(fixture: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    teams: dict[str, dict[str, float]] = {}
    for statistic in fixture.get("statistics") or []:
        if _type_name(statistic) == "BALL_POSSESSION":
            teams[str(statistic["participant_id"])] = {"possession_pct": float(statistic["data"]["value"])}
    return teams


def _substitution_minutes(fixture: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """(minute_on by incoming player, minute_off by outgoing player) from SUBSTITUTION events."""
    on: dict[str, int] = {}
    off: dict[str, int] = {}
    for event in fixture.get("events") or []:
        if _type_name(event) == "SUBSTITUTION":
            minute = int(event["minute"])
            if event.get("player_id") is not None:
                on[str(event["player_id"])] = minute
            if event.get("related_player_id") is not None:
                off[str(event["related_player_id"])] = minute
    return on, off


def _details(row: Mapping[str, Any]) -> dict[str, int]:
    values: dict[str, int] = {}
    for detail in row.get("details") or []:
        name = _type_name(detail)
        value = (detail.get("data") or {}).get("value")
        if name and isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not value.is_integer() and (name in DETAIL_FIELDS or name == "MINUTES_PLAYED"):
                raise ValueError(f"player {row.get('player_id')}: {name} is not a whole number ({value!r})")
            values[name] = int(value)
    return values


def fixture_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """A Sportmonks fixture (fetched with STATS_INCLUDE) as the payload parse_fixture_payload expects."""
    fixture = _data(body)
    status = _status(fixture)
    home, away = _sides(fixture)
    final_score = _current_score(fixture, home, away)
    referee = next(
        (r for r in fixture.get("referees") or [] if r.get("type_id") == 6 or _type_name(r) == "REFEREE"), None
    )
    lineups = fixture.get("lineups") or []
    on_events, off_events = _substitution_minutes(fixture)

    players: list[dict[str, Any]] = []
    seen_details: set[str] = set()
    capped: list[str] = []
    unidentified: list[str] = []
    for row in lineups:
        kind = row.get("type_id")
        if kind not in (STARTING_XI, BENCH):
            raise ValueError(f"fixture {fixture.get('id')}: unknown lineup type_id {kind!r}")
        details = _details(row)
        if row.get("player_id") is None:
            # Seen on ~9% of 2025/26 League One fixtures: a named player, usually a substitute, with no identity.
            # The row cannot be keyed, so it is left out of the stored players (the raw row stays in
            # vendor_response) and logged with its role and minutes. Maintainer decision 2026-10-04: load the match.
            role = "starter" if kind == STARTING_XI else "sub"
            unidentified.append(f"{row.get('player_name') or 'unnamed'} ({role}, {details.get('MINUTES_PLAYED', 0)} min)")
            continue
        uid = str(row["player_id"])
        seen_details.update(details)
        started = kind == STARTING_XI
        if status == "final" and started and "MINUTES_PLAYED" not in details:
            raise ValueError(f"fixture {fixture.get('id')}: starter {uid} has no MINUTES_PLAYED")
        minutes = details.get("MINUTES_PLAYED", 0)
        stats: dict[str, int] = {"minutes": minutes}
        for source, target in DETAIL_FIELDS.items():
            stats[target] = details.get(source, 0)
        if stats["tackles_won"] > stats["tackles_attempted"]:
            capped.append(f"{uid} ({stats['tackles_won']} won > {stats['tackles_attempted']} tackles)")
            stats["tackles_won"] = stats["tackles_attempted"]
        stats["yellow_cards"] = details.get("YELLOWCARDS", 0) + details.get("YELLOWREDCARDS", 0)
        stats["red_cards"] = details.get("REDCARDS", 0) + details.get("YELLOWREDCARDS", 0)
        player: dict[str, Any] = {
            "uid": uid,
            "name": " ".join(str(row.get("player_name") or uid).split()),
            "position": _position(fixture.get("id"), uid, row.get("position_id")),
            "team_uid": str(row["team_id"]),
            "started": started,
            "stats": stats,
        }
        if minutes > 0:
            minute_on = 0 if started else on_events.get(uid)
            if minute_on is not None:
                player["minute_on"] = minute_on
                minute_off = off_events.get(uid)
                player["minute_off"] = max(minute_off, minute_on) if minute_off is not None else minute_on + minutes
        players.append(player)

    if unidentified:
        logger.warning("fixture %s: skipped players without a player_id: %s", fixture.get("id"),
                       ", ".join(unidentified))
    if capped:
        logger.warning("fixture %s: TACKLES_WON capped at TACKLES for %s", fixture.get("id"), ", ".join(capped))
    if status == "final":
        missing = [name for name in COVERAGE_DETAILS if name not in seen_details]
        if missing:
            raise ValueError(f"fixture {fixture.get('id')}: no {missing} in any lineup (coverage gap or schema drift)")

    return {
        "fixture": {
            "uid": str(fixture["id"]),
            "home_team_uid": home,
            "away_team_uid": away,
            "kickoff_at": _kickoff(fixture),
            "status": status,
            "home_score": final_score[0],
            "away_score": final_score[1],
        },
        "referee": {"uid": str(referee["referee_id"]), "name": f"Sportmonks referee {referee['referee_id']}"}
        if referee is not None else None,
        "teams": _possession(fixture),
        "goals": _goals(fixture, home, away, final_score) if status in ("final", "live") else [],
        "players": players,
        # The untouched vendor object rides along so raw_payloads keeps every Sportmonks statistic, not only
        # the mapped ones: a paid backfill can then be re-derived later without re-fetching.
        # parse_fixture_payload ignores this key.
        "vendor_response": dict(fixture),
    }


def _position(fixture_id: Any, uid: str, position_id: Any) -> str | None:
    """The player's position code, or None when Sportmonks gives no position_id. Until 2026-10-05 a missing id became
    the code "PNone" (and an unknown one "P<id>"), which pricing treated as a position of its own (14 player-matches
    in fixture 19727778). An unknown id is refused: it is schema drift, not a position."""
    if position_id is None:
        return None
    if position_id not in POSITIONS:
        raise ValueError(f"fixture {fixture_id}: player {uid} has an unknown position_id {position_id!r}")
    return POSITIONS[position_id]


def lineup_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """A Sportmonks fixture (fetched with LINEUPS_INCLUDE) as the payload parse_lineup_payload expects.

    A sheet row without a player_id (Sportmonks lists some named players without an identity) used to become the
    player "None", which matched the bogus players row 954 (vendor_player_uid 'None', created before the stats
    adapter learned to skip such rows on 2026-10-04): every unidentified player was filed as that one player
    (found by Gate 1 criterion 7, 2026-10-05). Now an unidentified starter refuses the sheet, since its starting XI
    cannot be keyed, and an unidentified bench player is left out and logged, as the stats adapter does."""
    fixture = _data(body)
    home, away = _sides(fixture)
    sheets: dict[str, dict[str, list[str]]] = {home: {"starters": [], "bench": []}, away: {"starters": [], "bench": []}}
    unidentified: list[str] = []
    for row in fixture.get("lineups") or []:
        team = str(row["team_id"])
        if team not in sheets:
            raise ValueError(f"fixture {fixture.get('id')}: lineup row for unknown team {team}")
        kind = row.get("type_id")
        if kind not in (STARTING_XI, BENCH):
            raise ValueError(f"fixture {fixture.get('id')}: unknown lineup type_id {kind!r}")
        if row.get("player_id") is None:
            name = row.get("player_name") or "unnamed"
            if kind == STARTING_XI:
                raise ValueError(f"fixture {fixture.get('id')}: team {team}'s starting XI lists {name} without a "
                                 "player_id, so the sheet cannot be keyed")
            unidentified.append(str(name))
            continue
        sheets[team]["starters" if kind == STARTING_XI else "bench"].append(str(row["player_id"]))
    if unidentified:
        logger.warning("fixture %s: bench players without a player_id left off the sheet: %s", fixture.get("id"),
                       ", ".join(unidentified))
    return {"fixture": {"uid": str(fixture["id"]), "home_team_uid": home, "away_team_uid": away}, "lineups": sheets}


# Leagues the opportunity scan covers (Sportmonks league id -> prop_alpha.leagues.name).
SCAN_LEAGUES: Final[Mapping[int, str]] = {12: "League One", 14: "League Two"}
# The pilot scan's leagues (alerts/ladder_scan.py), kept in the pilot database `prop_alpha_pilot` and chosen by the
# maintainer on 2026-10-11 (the plan's five leagues: League One and League Two, which the frozen scan covers, and these
# three): (country, Sportmonks league name) -> the league's name in that database (unique there). Sportmonks league ids are read
# from the plan's own league list (`resolve_leagues`) instead of being written down: a league the plan does not
# include is reported, never guessed.
# Since 2026-10-21 (the Starter plan's five leagues, maintainer): the English Championship, where bookmakers quote player
# tackles and fouls, in place of the Scottish Championship, whose per-player tackles no source here holds.
PILOT_LEAGUES_WANTED: Final[Mapping[tuple[str, str], str]] = {
    ("Scotland", "Premiership"): "Scottish Premiership",
    ("England", "Championship"): "Championship",
    ("Brazil", "Serie A"): "Brazil Serie A",
}
LEAGUES_PATH: Final[str] = "leagues?include=country&per_page=50"


def resolve_leagues(
    rows: Sequence[Mapping[str, Any]], wanted: Mapping[tuple[str, str], str] = PILOT_LEAGUES_WANTED
) -> tuple[dict[int, str], list[str]]:
    """(Sportmonks league id -> database name for each wanted league the plan lists, the wanted names it does not)
    from the rows of the leagues listing (`LEAGUES_PATH`, which includes the country), all pages together."""
    found: dict[int, str] = {}
    for league in rows:
        country = (league.get("country") or {}).get("name")
        name = wanted.get((str(country), str(league.get("name"))))
        if name is not None:
            found[int(league["id"])] = name
    return found, sorted(name for name in wanted.values() if name not in found.values())


def league_rows(body: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    """(the league rows of one `LEAGUES_PATH` page, whether another page follows)."""
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("Sportmonks leagues listing has no 'data' list")
    pagination = body.get("pagination")
    return data, bool(isinstance(pagination, Mapping) and pagination.get("has_more"))
UPCOMING_INCLUDE: Final[str] = "participants;state"


def upcoming_path(start: datetime, end: datetime, page: int, leagues: Mapping[int, str] = SCAN_LEAGUES) -> str:
    """Listing of `leagues` (default SCAN_LEAGUES) fixtures between two UTC dates (inclusive), one page of 50."""
    league_ids = ",".join(str(league) for league in sorted(leagues))
    return (
        f"fixtures/between/{start:%Y-%m-%d}/{end:%Y-%m-%d}?filters=fixtureLeagues:{league_ids}"
        f"&include={UPCOMING_INCLUDE}&per_page=50&page={page}"
    )


def upcoming_fixtures(
    body: Mapping[str, Any], leagues: Mapping[int, str] = SCAN_LEAGUES
) -> tuple[list[dict[str, Any]], bool]:
    """(not-started fixtures in `leagues` (default SCAN_LEAGUES), whether another page follows) from one listing page."""
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("Sportmonks listing has no 'data' list")
    fixtures: list[dict[str, Any]] = []
    for fixture in data:
        league = leagues.get(fixture.get("league_id"))
        state = fixture.get("state")
        if league is None or not isinstance(state, Mapping) or state.get("developer_name") not in SCHEDULED_STATES:
            continue
        names = {
            (participant.get("meta") or {}).get("location"): str(participant.get("name"))
            for participant in fixture.get("participants") or []
        }
        home, away = _sides(fixture)
        fixtures.append({
            "fixture_id": int(fixture["id"]),
            "league": league,
            "home_team_uid": home,
            "away_team_uid": away,
            "home_name": names.get("home", home),
            "away_name": names.get("away", away),
            "kickoff_at": datetime.fromisoformat(_kickoff(fixture)),
        })
    pagination = body.get("pagination")
    return fixtures, bool(isinstance(pagination, Mapping) and pagination.get("has_more"))


def finished_with_leagues(
    body: Mapping[str, Any], leagues: Mapping[int, str]
) -> tuple[list[tuple[str, datetime, str]], bool]:
    """As finished_fixtures, each fixture with its league's name (`leagues`: Sportmonks id -> name): the pilot scan checks
    each league's results separately."""
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("Sportmonks listing has no 'data' list")
    finished: list[tuple[str, datetime, str]] = []
    for fixture in data:
        state = fixture.get("state")
        league = leagues.get(fixture.get("league_id"))
        if league is not None and isinstance(state, Mapping) and state.get("developer_name") in FINAL_STATES:
            finished.append((str(int(fixture["id"])), datetime.fromisoformat(_kickoff(fixture)), league))
    pagination = body.get("pagination")
    return finished, bool(isinstance(pagination, Mapping) and pagination.get("has_more"))


def finished_fixtures(
    body: Mapping[str, Any], leagues: Mapping[int, str] = SCAN_LEAGUES
) -> tuple[list[tuple[str, datetime]], bool]:
    """(finished `leagues` (default SCAN_LEAGUES) fixtures as (fixture id, kickoff), whether another page follows) from
    one page of the upcoming_path listing: the results job ingests these."""
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("Sportmonks listing has no 'data' list")
    finished: list[tuple[str, datetime]] = []
    for fixture in data:
        state = fixture.get("state")
        if (fixture.get("league_id") in leagues and isinstance(state, Mapping)
                and state.get("developer_name") in FINAL_STATES):
            finished.append((str(int(fixture["id"])), datetime.fromisoformat(_kickoff(fixture))))
    pagination = body.get("pagination")
    return finished, bool(isinstance(pagination, Mapping) and pagination.get("has_more"))
