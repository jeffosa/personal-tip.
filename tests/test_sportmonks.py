"""Sportmonks adapter (C1) on synthetic responses shaped like fixture 19727702 (no vendor data is committed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from prop_alpha.config import credential
from prop_alpha.ingestion import sportmonks
from prop_alpha.ingestion.pipeline import TokenBucketRateLimiter, VendorClient, build_vendor_client
from prop_alpha.ingestion.transform import parse_fixture_payload, parse_lineup_payload

HOME, AWAY = 76, 348


def _detail(name: str, value: int) -> dict[str, Any]:
    return {"type": {"developer_name": name}, "data": {"value": value}}


def _player(player_id: int, team: int, kind: int, **details: int) -> dict[str, Any]:
    return {
        "player_id": player_id, "player_name": f"Player\xa0{player_id}", "team_id": team, "position_id": 26,
        "type_id": kind, "details": [_detail(name, value) for name, value in details.items()],
    }


def _event(name: str, team: int, minute: int, player: int | None = None, related: int | None = None) -> dict[str, Any]:
    return {"type": {"developer_name": name}, "participant_id": team, "minute": minute,
            "player_id": player, "related_player_id": related}


def _fixture(**overrides: Any) -> dict[str, Any]:
    lineups = [
        _player(100 + i, HOME, sportmonks.STARTING_XI, MINUTES_PLAYED=90) for i in range(10)
    ] + [
        _player(110, HOME, sportmonks.STARTING_XI, MINUTES_PLAYED=70, TACKLES=6, TACKLES_WON=4, FOULS=2, YELLOWCARDS=1),
        _player(111, HOME, sportmonks.BENCH, MINUTES_PLAYED=20, FOULS_DRAWN=1),
        _player(112, HOME, sportmonks.BENCH),                                   # unused substitute
        _player(113, HOME, sportmonks.BENCH, YELLOWCARDS=1),                     # booked on the bench
    ] + [
        _player(200 + i, AWAY, sportmonks.STARTING_XI, MINUTES_PLAYED=90) for i in range(10)
    ] + [
        _player(210, AWAY, sportmonks.STARTING_XI, MINUTES_PLAYED=55, YELLOWCARDS=1, YELLOWREDCARDS=1),
    ]
    data: dict[str, Any] = {
        "id": 19727702,
        "starting_at_timestamp": 1789817400,
        "state": {"developer_name": "FT"},
        "participants": [{"id": AWAY, "meta": {"location": "away"}}, {"id": HOME, "meta": {"location": "home"}}],
        "scores": [
            {"description": "CURRENT", "participant_id": HOME, "score": {"goals": 2, "participant": "home"}},
            {"description": "CURRENT", "participant_id": AWAY, "score": {"goals": 0, "participant": "away"}},
        ],
        "events": [
            _event("GOAL", HOME, 45), _event("GOAL", HOME, 89),
            _event("SUBSTITUTION", HOME, 70, player=111, related=110),
        ],
        "referees": [{"referee_id": 19057, "type_id": 6}, {"referee_id": 75604, "type_id": 7}],
        "statistics": [
            {"type": {"developer_name": "BALL_POSSESSION"}, "participant_id": HOME, "data": {"value": 65}},
            {"type": {"developer_name": "BALL_POSSESSION"}, "participant_id": AWAY, "data": {"value": 35}},
        ],
        "lineups": lineups,
    }
    data.update(overrides)
    return {"data": data}


def _by_uid(body: dict[str, Any]) -> dict[str, Any]:
    return {p.player_uid: p for p in parse_fixture_payload(sportmonks.fixture_payload(body)).players}


def test_fixture_maps_to_a_payload_transform_accepts() -> None:
    parsed = parse_fixture_payload(sportmonks.fixture_payload(_fixture()))
    fx = parsed.fixture
    assert (fx.vendor_uid, fx.home_team_uid, fx.away_team_uid) == ("19727702", "76", "348")
    assert (fx.home_score, fx.away_score, fx.status, fx.kickoff_at) == (2, 0, "final", "2026-09-19T11:30:00+00:00")
    assert parsed.referee is not None and parsed.referee.vendor_uid == "19057"
    assert len(parsed.players) == 25


def test_absent_details_are_zero_and_present_ones_map_exactly() -> None:
    players = _by_uid(_fixture())
    worker = players["110"]
    assert (worker.tackles_attempted, worker.tackles_won, worker.fouls_committed, worker.yellow_cards) == (6, 4, 2, 1)
    assert (players["100"].tackles_attempted, players["100"].fouls_committed) == (0, 0)
    assert players["101"].name == "Player 101"                       # non-breaking space normalised
    assert players["113"].minutes_played == 0 and players["113"].yellow_cards == 1   # bench booking kept
    sent_off = players["210"]
    assert (sent_off.yellow_cards, sent_off.red_cards) == (2, 1)      # second yellow


def test_substitution_minutes_come_from_events() -> None:
    players = _by_uid(_fixture())
    assert (players["110"].minute_on, players["110"].minute_off) == (0, 70)
    assert (players["111"].minute_on, players["111"].minute_off, players["111"].started) == (70, 90, False)
    assert players["112"].minutes_played == 0


def test_finished_fixture_without_coverage_fails_instead_of_zero_filling() -> None:
    body = _fixture()
    for row in body["data"]["lineups"]:
        row["details"] = [d for d in row["details"] if d["type"]["developer_name"] != "TACKLES"]
    with pytest.raises(ValueError, match="TACKLES"):
        sportmonks.fixture_payload(body)
    body = _fixture()
    body["data"]["lineups"][0]["details"] = []
    with pytest.raises(ValueError, match="no MINUTES_PLAYED"):
        sportmonks.fixture_payload(body)


def test_own_goal_attribution_must_reproduce_the_score() -> None:
    events = [_event("GOAL", HOME, 45), _event("OWNGOAL", AWAY, 89)]   # Sportmonks tags the conceding side
    payload = sportmonks.fixture_payload(_fixture(events=events))
    assert sorted(g["team_uid"] for g in payload["goals"]) == ["76", "76"]
    events = [_event("GOAL", HOME, 45), _event("OWNGOAL", HOME, 89)]   # or the benefiting side
    assert sorted(g["team_uid"] for g in sportmonks.fixture_payload(_fixture(events=events))["goals"]) == ["76", "76"]
    with pytest.raises(ValueError, match="reproduce the score"):
        sportmonks.fixture_payload(_fixture(events=[_event("GOAL", HOME, 45)]))


def test_unknown_states_and_lineup_types_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported Sportmonks state"):
        sportmonks.fixture_payload(_fixture(state={"developer_name": "ABANDONED"}))
    body = _fixture()
    body["data"]["lineups"][0]["type_id"] = 99
    with pytest.raises(ValueError, match="lineup type_id"):
        sportmonks.fixture_payload(body)
    with pytest.raises(ValueError, match="no 'data'"):
        sportmonks.fixture_payload({"message": "Unauthenticated"})


def test_scheduled_fixture_needs_no_stats() -> None:
    body = _fixture(state={"developer_name": "NS"}, scores=[], events=[])
    for row in body["data"]["lineups"]:
        row["details"] = []
    parsed = parse_fixture_payload(sportmonks.fixture_payload(body))
    assert parsed.fixture.status == "scheduled" and parsed.fixture.home_score is None


def test_lineup_payload_maps_starting_eleven_and_bench() -> None:
    entries = parse_lineup_payload(sportmonks.lineup_payload(_fixture()), 0.35)
    assert sum(e.p_start == 1.0 for e in entries) == 22
    assert {e.vendor_player_uid for e in entries if e.p_sub_appearance == 0.35} == {"111", "112", "113"}


def test_vendor_client_sends_sportmonks_auth_and_adapts_responses(tmp_path: Path) -> None:
    client = VendorClient(
        sportmonks.BASE_URL, "secret-token", TokenBucketRateLimiter(100.0, 10), auth_prefix="",
        stats_path=sportmonks.STATS_PATH, lineups_path=sportmonks.LINEUPS_PATH,
        stats_adapter=sportmonks.fixture_payload, lineups_adapter=sportmonks.lineup_payload,
    )
    seen: list[str] = []

    class Session:
        def get(self, url: str, timeout: float) -> requests.Response:
            seen.append(url)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(_fixture()).encode("utf-8")
            return response

    client._local.session = Session()
    payload = client.get_fixture_feed("19727702")
    assert seen == [f"{sportmonks.BASE_URL}/fixtures/19727702?include={sportmonks.STATS_INCLUDE}"]
    assert payload["fixture"]["uid"] == "19727702"
    real = VendorClient(sportmonks.BASE_URL, "secret-token", TokenBucketRateLimiter(100.0, 10), auth_prefix="")
    assert real._session().headers["Authorization"] == "secret-token"
    real.close()


def test_build_vendor_client_defaults_to_sportmonks_and_reads_the_token(tmp_path: Path) -> None:
    client = build_vendor_client({"SPORTMONKS_API_TOKEN": "tok", "DATA_VENDOR_MAX_REQUESTS": "40"})
    assert client.base_url == sportmonks.BASE_URL and client.max_requests == 40
    assert client.stats_adapter is sportmonks.fixture_payload
    with pytest.raises(ValueError, match="DATA_VENDOR"):
        build_vendor_client({"DATA_VENDOR": "api-football"})
    env_file = tmp_path / ".env"
    env_file.write_text("SPORTMONKS_API_TOKEN=from-file\n", encoding="utf-8")
    assert credential("SPORTMONKS_API_TOKEN", {}, env_file) == "from-file"
    assert credential("SPORTMONKS_API_TOKEN", {"SPORTMONKS_API_TOKEN": "from-env"}, env_file) == "from-env"
    with pytest.raises(ValueError, match="not set"):
        credential("SPORTMONKS_API_TOKEN", {}, tmp_path / "missing.env")
