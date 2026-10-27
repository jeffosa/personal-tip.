"""Late lockbox captures (2026-10-27, LOCKBOX_PROTOCOL.md section 5). The protocol captures each outcome "once, 72 hours
after kickoff"; the capture step runs only when the 4-hourly task does, and it copies whatever the database holds then.
A capture of the stored statistics as they are can therefore hold data fetched after the intended capture time. That is
recorded on every capture and reported (never applied). Since the capture amendment of 2026-10-27 (section 6) the scorer
re-fetches at the grace or rebuilds from data before it; these tests cover the records and the timing report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from prop_alpha.evaluation import lockbox
from prop_alpha.evaluation.lockbox import PROTOCOL, StoredMatch

KICKOFF = PROTOCOL.start + timedelta(days=4, hours=19, minutes=45)
DUE = KICKOFF + PROTOCOL.revision_grace


def _record(fixture_id: int = 5001, player_id: int = 7, **changes: Any) -> dict[str, Any]:
    return {"logged_at": (KICKOFF - timedelta(hours=3)).isoformat(), "window_start": (KICKOFF - timedelta(hours=4)).isoformat(),
            "kickoff_at": KICKOFF.isoformat(), "fixture_id": fixture_id, "player_id": player_id, "market": "tackles",
            "line": 1.5, "p_win": 0.36, "p_lose": 0.54, "p_void": 0.10, "lineup_basis": "estimated",
            "digest": PROTOCOL.pricing_digest, "data_vendor": "sportmonks", **changes}


def _match(tackles: int, first: datetime | None, last: datetime | None) -> StoredMatch:
    return StoredMatch(KICKOFF, True, {7: (90, {"tackles": tackles, "fouls": 1})}, first, last)


def _captured(records: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    return {(int(r["fixture_id"]), int(r["player_id"])): r for r in records}


def test_an_on_time_capture_records_when_it_was_due_and_when_its_data_was_fetched() -> None:
    fetched = KICKOFF + timedelta(hours=3)  # the results job ingested it three hours after kickoff
    [outcome] = lockbox.outcomes_to_capture([_record()], {}, {5001: _match(2, fetched, fetched)}, DUE + timedelta(hours=1))
    assert outcome["tackles"] == 2 and outcome["due_at"] == DUE.isoformat()
    assert outcome["data_first_fetched_at"] == outcome["data_last_fetched_at"] == fetched.isoformat()
    timing = lockbox.capture_timing(_captured([outcome]))
    assert (timing.matches, timing.late, timing.first_fetched_after_due, timing.revised_after_due) == (1, 0, 0, 0)
    assert timing.latest_hours == pytest.approx(1.0)


def test_inside_the_grace_nothing_is_captured() -> None:
    assert lockbox.outcomes_to_capture([_record()], {}, {5001: _match(2, KICKOFF, KICKOFF)}, DUE - timedelta(seconds=1)) == []


def test_a_late_capture_holds_a_revision_made_after_the_intended_time_and_says_so() -> None:
    """The laptop was off at the 72 h mark; meanwhile the match was re-ingested with a revised payload (3 tackles, not 2).
    The late capture copies the revised value: it is reported, not dropped or replaced (the protocol authorises reporting)."""
    first, revised_at = KICKOFF + timedelta(hours=3), DUE + timedelta(hours=20)
    late = DUE + timedelta(days=2)
    [outcome] = lockbox.outcomes_to_capture([_record()], {}, {5001: _match(3, first, revised_at)}, late)
    assert outcome["tackles"] == 3  # the post-grace revision is in the captured outcome
    timing = lockbox.capture_timing(_captured([outcome]))
    assert (timing.late, timing.revised_after_due, timing.first_fetched_after_due) == (1, 1, 0)
    assert timing.latest_hours == pytest.approx(48.0)
    assert "1 more than 4 h after the 72 h grace (latest 48.0 h after it)" in lockbox.capture_timing_line(timing)
    scoring = lockbox.scoring_rows([_record()], _captured([outcome]), late)
    assert scoring.rows[("tackles", 1.5)] == ((5001, 7, pytest.approx(0.4), 1),)  # scored as captured, unchanged rules


def test_a_match_first_ingested_after_the_grace_is_reported_as_fetched_after_the_intended_time() -> None:
    fetched = DUE + timedelta(hours=30)  # the results job could not run until then
    [outcome] = lockbox.outcomes_to_capture([_record()], {}, {5001: _match(2, fetched, fetched)}, fetched + timedelta(hours=1))
    timing = lockbox.capture_timing(_captured([outcome]))
    assert (timing.late, timing.first_fetched_after_due, timing.revised_after_due) == (1, 1, 0)


def test_a_repeated_capture_run_never_captures_again_and_a_later_revision_is_only_reported() -> None:
    fetched = KICKOFF + timedelta(hours=3)
    [first] = lockbox.outcomes_to_capture([_record()], {}, {5001: _match(2, fetched, fetched)}, DUE + timedelta(hours=1))
    store = _captured([first])
    revised = {5001: _match(4, fetched, DUE + timedelta(days=1))}
    assert lockbox.outcomes_to_capture([_record()], store, revised, DUE + timedelta(days=2)) == []  # captured once
    assert lockbox.revised(store, revised) == [(5001, 7)]  # the revision after capture is reported...
    assert store[(5001, 7)]["tackles"] == 2  # ...never applied
    assert lockbox.capture_timing(store).revised_after_due == 0  # judged by the data held when it was captured


def test_records_captured_before_the_timing_fields_existed_are_counted_as_unknown() -> None:
    old = {"fixture_id": 5001, "player_id": 7, "minutes": 90, "tackles": 2, "fouls": 1,
           "captured_at": (DUE + timedelta(hours=1)).isoformat(), "source": "sportmonks:player_match_stats"}
    timing = lockbox.capture_timing(_captured([old]))
    assert (timing.matches, timing.unknown, timing.late) == (1, 1, 0)
    assert "1 captured without timing records" in lockbox.capture_timing_line(timing)


# The scorer end to end (re-fetch, rebuild, exclusion, repeated and dry runs): tests/test_lockbox_capture_policy.py.
