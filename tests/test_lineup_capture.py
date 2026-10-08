from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from prop_alpha.alerts import lineup_capture
from prop_alpha.scanner.slate import UpcomingFixture

KICKOFF = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)


def _fixture(fixture_id: int = 9001, kickoff: datetime = KICKOFF) -> UpcomingFixture:
    return UpcomingFixture(fixture_id, "League Two", "76", "348", "Walsall", "Port Vale", kickoff)


def _payload(home: int = 11, away: int = 11, overlap: bool = False) -> dict[str, Any]:
    home_ids = [f"h{i}" for i in range(home)]
    away_ids = [("h0" if overlap and i == 0 else f"a{i}") for i in range(away)]
    return {"fixture": {"uid": "9001", "home_team_uid": "76", "away_team_uid": "348"},
            "lineups": {"76": {"starters": home_ids, "bench": ["hb1"]}, "348": {"starters": away_ids, "bench": ["ab1"]}}}


def test_a_fixture_is_due_from_65_to_30_minutes_before_kickoff_until_handled() -> None:
    now = KICKOFF - timedelta(minutes=60)
    fixtures = [_fixture(1), _fixture(2, KICKOFF + timedelta(minutes=30)), _fixture(3, KICKOFF - timedelta(minutes=40))]
    assert [f.fixture_id for f in lineup_capture.due_fixtures(fixtures, frozenset(), now)] == [1]
    assert lineup_capture.due_fixtures(fixtures, frozenset({1}), now) == []
    assert lineup_capture.due_fixtures([_fixture()], frozenset(), KICKOFF - timedelta(minutes=66)) == []
    assert lineup_capture.due_fixtures([_fixture()], frozenset(), KICKOFF - timedelta(minutes=65)) != []
    assert lineup_capture.due_fixtures([_fixture()], frozenset(), KICKOFF - timedelta(minutes=30)) == []  # at the limit
    with pytest.raises(ValueError, match="UTC"):
        lineup_capture.due_fixtures([_fixture()], frozenset(), datetime(2026, 10, 10, 14))


def test_a_sheet_is_confirmed_only_with_eleven_distinct_starters_a_side() -> None:
    assert lineup_capture.sheet_status(_payload()) == "confirmed"
    assert lineup_capture.sheet_status(_payload(home=10)) == "incomplete"
    assert lineup_capture.sheet_status(_payload(away=0)) == "incomplete"
    assert lineup_capture.sheet_status(_payload(overlap=True)) == "incomplete"
    with pytest.raises(ValueError, match="two sides"):
        lineup_capture.sheet_status({"lineups": {"76": {"starters": [], "bench": []}}})


def test_the_last_poll_before_the_cut_off_is_the_last_chance() -> None:
    assert not lineup_capture.is_last_chance(_fixture(), KICKOFF - timedelta(minutes=60))
    assert not lineup_capture.is_last_chance(_fixture(), KICKOFF - timedelta(minutes=45))
    assert lineup_capture.is_last_chance(_fixture(), KICKOFF - timedelta(minutes=40))
    assert lineup_capture.is_last_chance(_fixture(), KICKOFF - timedelta(minutes=35))


class _Client:
    def __init__(self, sheets: list[dict[str, Any]]) -> None:
        self.sheets, self.listings, self.fetched = sheets, 0, 0

    def get_json(self, path: str) -> dict[str, Any]:
        self.listings += 1
        row = {"id": 9001, "league_id": 14, "state": {"developer_name": "NS"},
               "starting_at_timestamp": int(KICKOFF.timestamp()),
               "participants": [{"id": 76, "name": "Walsall", "meta": {"location": "home"}},
                                {"id": 348, "name": "Port Vale", "meta": {"location": "away"}}]}
        return {"data": [row], "pagination": {"has_more": False}}

    def get_lineups(self, uid: str) -> dict[str, Any]:
        assert uid == "9001"
        self.fetched += 1
        return self.sheets.pop(0)


def test_a_confirmed_sheet_is_recorded_once_and_not_fetched_again(tmp_path: Path) -> None:
    client = _Client([_payload()])
    now = KICKOFF - timedelta(minutes=60)
    assert lineup_capture.capture(now, client, tmp_path)[:2] == (1, 1)
    assert lineup_capture.capture(now + timedelta(minutes=10), client, tmp_path)[:2] == (0, 0)
    assert client.fetched == 1
    [record] = lineup_capture.load_records(tmp_path)
    assert record["status"] == "confirmed" and record["minutes_to_kickoff"] == 60.0
    assert len(record["starters"]["76"]) == 11 and record["bench"]["348"] == ["ab1"]


def test_an_incomplete_sheet_is_retried_and_recorded_as_incomplete_on_the_last_poll(tmp_path: Path) -> None:
    client = _Client([_payload(home=0), _payload(home=0), _payload(home=0)])
    assert lineup_capture.capture(KICKOFF - timedelta(minutes=60), client, tmp_path)[:2] == (0, 0)
    assert lineup_capture.load_records(tmp_path) == []
    assert lineup_capture.capture(KICKOFF - timedelta(minutes=50), client, tmp_path)[:2] == (0, 0)
    recorded, confirmed, notes = lineup_capture.capture(KICKOFF - timedelta(minutes=40), client, tmp_path)
    assert (recorded, confirmed) == (1, 0)
    [record] = lineup_capture.load_records(tmp_path)
    assert record["status"] == "incomplete" and "incomplete" in notes[0]
    assert json.loads((tmp_path / lineup_capture.HANDLED_NAME).read_text(encoding="utf-8")) == [9001]


def test_no_request_is_made_outside_the_listing_hours(tmp_path: Path) -> None:
    late = datetime(2026, 10, 10, 22, 30, tzinfo=timezone.utc)
    client = _Client([])
    assert lineup_capture.capture(late, client, tmp_path) == (0, 0, [])
    assert client.listings == 0


def test_a_vendor_failure_is_not_swallowed_and_records_nothing(tmp_path: Path) -> None:
    class _Broken(_Client):
        def get_lineups(self, uid: str) -> dict[str, Any]:
            raise ValueError("starting XI lists X without a player_id")

    with pytest.raises(ValueError, match="without a player_id"):
        lineup_capture.capture(KICKOFF - timedelta(minutes=60), _Broken([]), tmp_path)
    assert lineup_capture.load_records(tmp_path) == []
    assert not (tmp_path / lineup_capture.HANDLED_NAME).exists()
