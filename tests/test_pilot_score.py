"""scripts/pilot_score.py: the pilot's logged predictions scored against what happened (audit 2026-10-11, second pass:
the outcome columns were read by position and swapped minutes for tackles and tackles for fouls)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prop_alpha_pilot_score_script", ROOT / "scripts" / "pilot_score.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record(market: str, line: float, digest: str = "pilot:a", player: int = 7, logged: str = "2026-10-11T10:00:00+00:00",
            p_win: float = 0.6) -> dict[str, Any]:
    return {"logged_at": logged, "window_start": logged, "kickoff_at": "2026-10-11T20:30:00+00:00", "fixture_id": 500,
            "player_id": player, "market": market, "line": line, "p_win": p_win, "p_lose": 1.0 - p_win - 0.1, "p_void": 0.1,
            "lineup_basis": "estimated", "digest": digest}


def test_each_market_is_scored_on_its_own_count_never_on_minutes() -> None:
    score = _module()
    # Deliberately different: 90 minutes, 1 tackle, 4 fouls. Under the old mapping tackles read 90 (over 1.5: yes) and fouls
    # read 1 (over 1.5: no); the right answers are the other way round.
    outcomes = {500: {7: ("Brazil Serie A", 90, 1, 4)}}
    points = score.score_points([_record("tackles", 1.5), _record("fouls", 1.5)], outcomes)
    assert points[("pilot:a", "Brazil Serie A", "tackles", 1.5)] == [(500, pytest.approx(0.6 / 0.9), 0)]
    assert points[("pilot:a", "Brazil Serie A", "fouls", 1.5)] == [(500, pytest.approx(0.6 / 0.9), 1)]
    assert score.outcome_count(("x", 90, 1, 4), "tackles") == 1 and score.outcome_count(("x", 90, 1, 4), "fouls") == 4
    with pytest.raises(ValueError, match="no outcome"):
        score.outcome_count(("x", 90, 1, 4), "minutes")


def test_each_settings_digest_is_scored_separately_and_the_earliest_prediction_counts_once() -> None:
    score = _module()
    outcomes = {500: {7: ("Brazil Serie A", 90, 3, 0)}}
    records = [_record("tackles", 2.5, "pilot:a", logged="2026-10-11T12:00:00+00:00", p_win=0.2),
               _record("tackles", 2.5, "pilot:a", logged="2026-10-11T10:00:00+00:00", p_win=0.5),  # earlier: this one counts
               _record("tackles", 2.5, "pilot:b", p_win=0.7)]
    points = score.score_points(records, outcomes)
    assert points[("pilot:a", "Brazil Serie A", "tackles", 2.5)] == [(500, pytest.approx(0.5 / 0.9), 1)]
    assert points[("pilot:b", "Brazil Serie A", "tackles", 2.5)] == [(500, pytest.approx(0.7 / 0.9), 1)]


def test_a_player_who_did_not_play_or_a_match_not_final_is_left_out() -> None:
    score = _module()
    records = [_record("tackles", 0.5, player=7), _record("tackles", 0.5, player=8)]
    assert score.score_points(records, {500: {7: ("Brazil Serie A", 0, 0, 0)}}) == {}  # 0 minutes; 8 has no row
    assert score.score_points(records, {}) == {}  # not final yet
