"""The prospective Gate 2 lockbox protocol (evaluation/lockbox.py, LOCKBOX_PROTOCOL.md, maintainer 2026-10-25): eligibility,
duplicates, moved kickoffs, outcomes captured once and revisions, scoring states, the tuning cutoff, and the scorer."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from prop_alpha.evaluation import lockbox
from prop_alpha.evaluation.lockbox import PROTOCOL, StoredMatch
from prop_alpha.ingestion.sportmonks import SCAN_LEAGUES
from prop_alpha.modeling import model_freeze

ROOT = Path(__file__).resolve().parents[1]
KICKOFF = PROTOCOL.start + timedelta(days=4, hours=15)


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_script", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "logged_at": (KICKOFF - timedelta(hours=2)).isoformat(), "window_start": (KICKOFF - timedelta(hours=3)).isoformat(),
        "kickoff_at": KICKOFF.isoformat(), "fixture_id": 5001, "player_id": 7, "market": "tackles", "line": 1.5,
        "p_win": 0.36, "p_lose": 0.54, "p_void": 0.10, "lineup_basis": "estimated", "digest": PROTOCOL.pricing_digest,
        "data_vendor": "sportmonks",
    }
    return {**base, **changes}


# ---------------- the protocol itself ----------------
def test_the_protocol_matches_the_freeze_and_starts_after_every_examined_match() -> None:
    # A re-freeze (new FROZEN_DIGEST) or a moved LOCKBOX_START fails here until a new protocol with a later start is written.
    assert PROTOCOL.pricing_digest == model_freeze.FROZEN_DIGEST
    assert PROTOCOL.start == model_freeze.LOCKBOX_START and PROTOCOL.start.date() > model_freeze.FROZEN_ON
    assert dict(PROTOCOL.lines) == dict(model_freeze.GATE2_LINES)
    assert set(PROTOCOL.leagues) == set(SCAN_LEAGUES.values())
    assert PROTOCOL.start == datetime(2026, 10, 27, tzinfo=timezone.utc) and PROTOCOL.decided_on == date(2026, 10, 25)
    assert len(PROTOCOL.examined_fixtures) == 24 and {19727666, 19727677, 19728218, 19728229} <= PROTOCOL.examined_fixtures
    assert PROTOCOL.start > PROTOCOL.last_examined_kickoff == datetime(2026, 10, 10, 14, 15, tzinfo=timezone.utc)


def test_a_protocol_starting_before_an_examined_match_or_on_its_decision_day_is_refused() -> None:
    with pytest.raises(ValueError, match="after every examined match"):
        replace(PROTOCOL, start=PROTOCOL.last_examined_kickoff)
    with pytest.raises(ValueError, match="after the day the protocol was decided"):
        replace(PROTOCOL, start=datetime(2026, 10, 25, 12, tzinfo=timezone.utc))


# ---------------- eligibility ----------------
def test_each_rule_excludes_a_line_with_its_reason() -> None:
    assert lockbox.record_reason(_record()) is None
    assert lockbox.record_reason(_record(fixture_id=19728228)) == lockbox.EXAMINED
    early = PROTOCOL.start - timedelta(hours=1)
    assert lockbox.record_reason(_record(kickoff_at=early.isoformat(),
                                         logged_at=(early - timedelta(hours=1)).isoformat())) == lockbox.BEFORE_START
    assert lockbox.record_reason(_record(digest="0" * 64)) == lockbox.OTHER_DIGEST
    assert lockbox.record_reason({k: v for k, v in _record().items() if k != "data_vendor"}) == lockbox.OTHER_VENDOR
    assert lockbox.record_reason(_record(data_vendor="thestatsapi")) == lockbox.OTHER_VENDOR
    assert lockbox.record_reason(_record(logged_at=KICKOFF.isoformat())) == lockbox.NOT_BEFORE_KICKOFF
    assert lockbox.record_reason(_record(line=3.5)) == lockbox.OTHER_LINE
    assert lockbox.record_reason(_record(market="cards", line=0.5)) == lockbox.OTHER_LINE


def test_of_duplicate_lines_only_the_earliest_counts() -> None:
    first = _record()
    later = _record(logged_at=(KICKOFF - timedelta(minutes=30)).isoformat(), p_win=0.5, p_lose=0.4)
    selection = lockbox.eligible([later, first], {5001: KICKOFF})
    assert selection.eligible == (first,) and selection.excluded == {lockbox.DUPLICATE: 1}


def test_a_line_for_a_kickoff_that_moved_is_excluded_and_the_new_kickoffs_line_counts() -> None:
    moved_to = KICKOFF + timedelta(days=3)
    old = _record()
    new = _record(kickoff_at=moved_to.isoformat(), logged_at=(moved_to - timedelta(hours=2)).isoformat())
    selection = lockbox.eligible([old, new], {5001: moved_to})
    assert selection.eligible == (new,) and selection.excluded == {lockbox.KICKOFF_MOVED: 1}
    # Within the tolerance (a 10-minute slip in the listing) the line still counts; a match not yet stored is not judged.
    slipped = lockbox.eligible([old], {5001: KICKOFF + timedelta(minutes=10)})
    assert slipped.eligible == (old,)
    assert lockbox.eligible([old], {}).eligible == (old,)


# ---------------- outcomes: captured once, revisions reported ----------------
def _match(final: bool = True, tackles: int = 2, minutes: int = 90) -> StoredMatch:
    return StoredMatch(KICKOFF, final, {7: (minutes, {"tackles": tackles, "fouls": 1})})


def test_an_outcome_is_captured_once_after_the_revision_grace_and_only_for_a_final_match() -> None:
    records = [_record(), _record(line=0.5)]
    before = KICKOFF + PROTOCOL.revision_grace - timedelta(minutes=1)
    after = KICKOFF + PROTOCOL.revision_grace
    assert lockbox.outcomes_to_capture(records, {}, {5001: _match()}, before) == []
    assert lockbox.outcomes_to_capture(records, {}, {5001: _match(final=False)}, after) == []
    [captured] = lockbox.outcomes_to_capture(records, {}, {5001: _match()}, after)  # one per player, both lines
    assert (captured["fixture_id"], captured["player_id"], captured["minutes"], captured["tackles"]) == (5001, 7, 90, 2)
    assert lockbox.outcomes_to_capture(records, {(5001, 7): captured}, {5001: _match(tackles=4)}, after) == []


def test_a_player_without_a_stored_row_is_captured_as_not_playing() -> None:
    [captured] = lockbox.outcomes_to_capture([_record(player_id=8)], {}, {5001: _match()}, KICKOFF + timedelta(days=4))
    assert captured["minutes"] == 0 and captured["tackles"] is None


def test_a_revision_after_capture_is_reported_and_never_changes_the_scored_outcome() -> None:
    later = KICKOFF + timedelta(days=4)
    [captured] = lockbox.outcomes_to_capture([_record()], {}, {5001: _match(tackles=2)}, later)
    store = {(5001, 7): captured}
    assert lockbox.revised(store, {5001: _match(tackles=2)}) == []
    assert lockbox.revised(store, {5001: _match(tackles=1)}) == [(5001, 7)]
    scoring = lockbox.scoring_rows([_record()], store, later)
    assert scoring.rows[("tackles", 1.5)] == ((5001, 7, pytest.approx(0.4), 1),)  # 2 > 1.5 as captured, not 1


# ---------------- scoring ----------------
def test_scoring_uses_the_logged_probability_given_he_plays_and_counts_each_state() -> None:
    later = KICKOFF + timedelta(days=4)
    played: dict[str, Any] = {"fixture_id": 5001, "player_id": 7, "minutes": 80, "tackles": 1, "fouls": 0}
    benched: dict[str, Any] = {"fixture_id": 5001, "player_id": 9, "minutes": 0, "tackles": None, "fouls": None}
    records = [_record(), _record(player_id=9), _record(fixture_id=5002), _record(fixture_id=5003)]
    captured = {(5001, 7): played, (5001, 9): benched}
    scoring = lockbox.scoring_rows(records, captured, later)
    assert scoring.rows[("tackles", 1.5)] == ((5001, 7, pytest.approx(0.36 / 0.90), 0),)
    assert scoring.states == {"scored": 1, lockbox.VOID: 1, lockbox.PENDING: 2}
    late = lockbox.scoring_rows(records, captured, KICKOFF + PROTOCOL.missing_after)
    assert late.states[lockbox.MISSING] == 2
    assert not lockbox.ready_to_score(scoring)
    full = lockbox.ScoringRows({key: tuple((n, 1, 0.5, n % 2) for n in range(PROTOCOL.min_scored_per_line))
                                for key in scoring.rows}, {})
    assert lockbox.ready_to_score(full)


def test_tuning_never_reaches_the_lockbox_for_league_one_and_two_only() -> None:
    assert lockbox.tuning_cutoff(None) == PROTOCOL.start
    assert lockbox.tuning_cutoff("League Two") == PROTOCOL.start
    assert lockbox.tuning_cutoff("Brazil Serie A") is None


# ---------------- the scripts ----------------
def test_the_retired_repricing_scorer_refuses_to_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script("gate2_lockbox").main() == 2
    assert "retired" in capsys.readouterr().out


def test_the_scorer_never_passes_without_the_benchmark_and_the_card_market(tmp_path: Path) -> None:
    scorer = _script("gate2_prospective")
    rows = {key: tuple((f, p, 0.3 + 0.4 * (p % 2), (f + p) % 2) for f in range(12) for p in range(10))
            for key in [(m, line) for m, lines in PROTOCOL.lines.items() for line in lines]}
    result = scorer.score(lockbox.ScoringRows(rows, {}), {})
    assert result["passed"] is False
    assert any("market benchmark: 0 predictions" in b for b in result["blockers"])
    assert any(b.startswith("yellow_card") for b in result["blockers"])
    assert all("bins" in entry and "criteria" in entry for market in result["markets"].values()
               for key, entry in market.items() if key != "market_benchmark")


def test_the_first_captured_outcome_of_a_player_is_the_one_kept(tmp_path: Path) -> None:
    scorer = _script("gate2_prospective")
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps({"fixture_id": 1, "player_id": 2, "minutes": m, "tackles": t, "fouls": 0})
                              for m, t in ((90, 3), (90, 1))) + "\n", encoding="utf-8")
    assert scorer.load_outcomes(path)[(1, 2)]["tackles"] == 3
    path.write_text(json.dumps({"fixture_id": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not an outcome record"):
        scorer.load_outcomes(path)


def test_prospective_result_is_written_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scorer = _script("gate2_prospective")
    result_file = tmp_path / "result.json"
    monkeypatch.setattr(scorer, "RESULT_FILE", result_file)
    monkeypatch.setattr(scorer, "RESULT_LOCK_FILE", tmp_path / "result.lock")
    first = {"scored_at": "first", "passed": False, "blockers": ["first result"]}
    later = {"scored_at": "later", "passed": True, "blockers": []}

    assert scorer.write_result_once(first) is None
    assert scorer.write_result_once(later) == first
    assert json.loads(result_file.read_text(encoding="utf-8")) == first


# ---------------- coverage (closing-price review, 2026-10-25) ----------------
def test_coverage_counts_lockbox_matches_their_pre_kickoff_predictions_and_closing_captures() -> None:
    other = KICKOFF + timedelta(hours=3)
    kicked_off = {5001: KICKOFF, 5002: KICKOFF, 5003: other, 19728228: KICKOFF,
                  4000: PROTOCOL.start - timedelta(days=1)}  # an examined match and one before the start are not counted
    records = [_record(), _record(fixture_id=5003, kickoff_at=other.isoformat(), logged_at=other.isoformat())]  # 5003: late
    result = lockbox.coverage(kicked_off, records, closing_tried={5001, 5002, 4000}, closing_priced={5001})
    assert result == lockbox.Coverage(matches=3, predicted=1, closing_tried=2, closing_priced=1)
    line = lockbox.coverage_line(result)
    assert line.startswith("Lockbox coverage (kickoff from 27 Oct): 3 matches kicked off; 1 with a prediction")
    assert "WARNING: 2 missed: machine off, or the scan failed" in line and "closing capture ran for 2, prices for 1" in line
    assert lockbox.coverage_line(lockbox.coverage({}, [], set(), set())).endswith("no lockbox match has kicked off yet")
    assert "WARNING" not in lockbox.coverage_line(lockbox.Coverage(2, 2, 0, 0))
