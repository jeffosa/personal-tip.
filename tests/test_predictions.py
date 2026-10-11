from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from prop_alpha.alerts import predictions
from prop_alpha.alerts.predictions import PredictionLog, counts_for_lockbox, lockbox_progress, prediction_records
from prop_alpha.modeling.engine import QuantEngine
from prop_alpha.modeling.model_freeze import FROZEN_DIGEST, GATE2_LINES, LOCKBOX_START
from prop_alpha.scanner.slate import Appearance, SquadPlayer, UpcomingFixture, build_projections

KICKOFF = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
FIXTURE = UpcomingFixture(9001, "League Two", "76", "348", "Walsall", "Port Vale", KICKOFF)
NOW = KICKOFF - timedelta(hours=2)
WINDOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
RATES = {(position, market): rate for position in ("DEF", "MID") for market, rate in (("tackles", 1.8), ("fouls", 1.2))}


def _projections() -> Any:
    rows = [Appearance(1, 100 + week, "76", KICKOFF - timedelta(days=7 * (10 - week)), True, 90.0, 2, 1)
            for week in range(10)]
    projections, _ = build_projections(FIXTURE, [SquadPlayer(1, "Regular Starter", "MID", "76")], {1: rows},
                                       {"76": [105, 106, 107, 108, 109], "348": [105, 106, 107, 108, 109]}, RATES,
                                       QuantEngine(game_state_k=6.0))
    return projections


def test_a_record_is_written_for_every_gate2_line_with_the_digest_and_estimated_basis() -> None:
    records = prediction_records(FIXTURE, _projections(), NOW, WINDOW, "abc")
    expected = {(market, line) for market, lines in GATE2_LINES.items() for line in lines}
    assert {(r["market"], r["line"]) for r in records} == expected
    for r in records:
        assert r["fixture_id"] == 9001 and r["player_id"] == 1 and r["digest"] == "abc"
        assert r["lineup_basis"] == "estimated" and r["kickoff_at"] == KICKOFF.isoformat()
        assert 0.0 < r["p_win"] < 1.0 and abs(r["p_win"] + r["p_lose"] + r["p_void"] - 1.0) < 1e-9


def test_a_prediction_made_at_or_after_kickoff_is_refused() -> None:
    with pytest.raises(ValueError, match="not before kickoff"):
        prediction_records(FIXTURE, _projections(), KICKOFF, WINDOW, "abc")


def test_a_projection_for_another_fixture_is_refused() -> None:
    other = UpcomingFixture(9002, "League Two", "76", "348", "Walsall", "Port Vale", KICKOFF)
    with pytest.raises(ValueError, match="does not belong"):
        prediction_records(other, _projections(), NOW, WINDOW, "abc")


def test_the_log_is_append_only_and_a_fixture_is_logged_once_per_digest_whatever_the_window(tmp_path: Path) -> None:
    """Audit 2026-10-11: a retry, a late catch-up run or an overlapping window must not log the fixture again."""
    log = PredictionLog(tmp_path / "p.jsonl")
    first = log.record(FIXTURE, _projections(), NOW, WINDOW, "abc")
    assert first > 0 and len(log.load()) == first
    assert log.record(FIXTURE, _projections(), NOW + timedelta(minutes=5), WINDOW, "abc") == 0  # a retried run
    assert log.record(FIXTURE, _projections(), NOW, WINDOW + timedelta(hours=1), "abc") == 0  # another window
    assert len(log.load()) == first and {r["logged_at"] for r in log.load()} == {NOW.isoformat()}  # the earliest stands
    assert log.record(FIXTURE, _projections(), NOW, WINDOW + timedelta(hours=1), "def") == first  # a re-frozen model
    assert len(log.load()) == 2 * first


def test_a_damaged_log_line_raises_rather_than_being_skipped(tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"fixture_id": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a prediction record"):
        PredictionLog(path).load()
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        PredictionLog(path).load()


def _record(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "logged_at": (LOCKBOX_START + timedelta(days=4, hours=-2)).isoformat(), "window_start": WINDOW.isoformat(),
        "kickoff_at": (LOCKBOX_START + timedelta(days=4)).isoformat(), "fixture_id": 1, "player_id": 1,
        "market": "tackles", "line": 1.5, "p_win": 0.4, "p_lose": 0.6, "p_void": 0.0,
        "lineup_basis": "estimated", "digest": FROZEN_DIGEST,
    }
    return {**base, **changes}


def test_only_frozen_code_before_kickoff_from_the_lockbox_start_counts() -> None:
    assert counts_for_lockbox(_record())
    assert not counts_for_lockbox(_record(digest="0" * 64))                      # the code has since changed
    early = (LOCKBOX_START - timedelta(hours=1)).isoformat()
    assert not counts_for_lockbox(_record(kickoff_at=early, logged_at=(LOCKBOX_START - timedelta(hours=3)).isoformat()))
    kickoff = datetime.fromisoformat(str(_record()["kickoff_at"]))
    assert not counts_for_lockbox(_record(logged_at=(kickoff + timedelta(minutes=1)).isoformat()))  # after kickoff


def test_lockbox_progress_counts_per_market_and_line_and_what_does_not_count() -> None:
    records = [_record(), _record(line=0.5), _record(market="fouls"), _record(digest="0" * 64)]
    progress = lockbox_progress(records)
    assert progress["logged"] == 4 and progress["counted"] == 3 and progress["not_counted"] == 1
    assert progress["per_line"] == {"tackles": {1.5: 1, 0.5: 1}, "fouls": {1.5: 1}}


def test_predictions_module_keeps_to_the_documented_file_name() -> None:
    assert predictions.PREDICTIONS_NAME == "predictions.jsonl"
