"""The Gate 2 calibration amendment of 2026-10-26 (maintainer: Option 3 of GATE2_CALIBRATION_DECISION_MEMO.md): the bin
criterion is the count-weighted mean gap of bins with >= 50 predictions, at most 0.03; the one lockbox look waits for 500
scored predictions from 320 matches on every line; only kickoffs before 2027-06-01 count; and a lockbox that closes short
of that is NOT PASSED and never scored."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from prop_alpha.evaluation import gate2, lockbox
from prop_alpha.evaluation.lockbox import PROTOCOL

ROOT = Path(__file__).resolve().parents[1]
BIN_CRITERION = "weighted mean gap of bins of >= 50 within 0.03"


def _two_bins(low_ones: int, high_ones: int) -> tuple[list[int], list[float], list[int]]:
    """400 predictions at 0.25 with `low_ones` hits and 100 at 0.55 with `high_ones` hits, spread over 50 fixtures."""
    p = [0.25] * 400 + [0.55] * 100
    y = [1] * low_ones + [0] * (400 - low_ones) + [1] * high_ones + [0] * (100 - high_ones)
    return [n % 50 for n in range(500)], p, y


# ---------------- the bin criterion ----------------
def test_one_bin_off_by_more_than_the_tolerance_no_longer_fails_when_the_weighted_mean_gap_is_within_it() -> None:
    fixtures, p, y = _two_bins(100, 60)  # gaps 0.00 (400 predictions) and 0.05 (100): weighted mean 0.01
    score = gate2.score_line(fixtures, p, y, np.random.default_rng(1), resamples=200)
    assert score.worst_bin_gap == pytest.approx(0.05) and score.worst_bin_gap > gate2.MAX_BIN_GAP
    assert score.mean_bin_gap == pytest.approx(0.01) == gate2.mean_bin_gap(p, y)
    assert score.criteria[BIN_CRITERION]


def test_a_weighted_mean_gap_above_the_tolerance_fails() -> None:
    fixtures, p, y = _two_bins(120, 60)  # gaps 0.05 and 0.05
    score = gate2.score_line(fixtures, p, y, np.random.default_rng(1), resamples=200)
    assert score.mean_bin_gap == pytest.approx(0.05)
    assert not score.criteria[BIN_CRITERION] and not score.passed


def test_without_a_bin_of_50_the_bin_criterion_fails() -> None:
    p, y = [0.25] * 40, [1] * 10 + [0] * 30
    assert math.isnan(gate2.mean_bin_gap(p, y))
    score = gate2.score_line(list(range(40)), p, y, np.random.default_rng(1), resamples=200)
    assert not score.criteria[BIN_CRITERION] and not score.passed


# ---------------- the lockbox amendment ----------------
def test_the_amendment_is_recorded_in_the_protocol_before_the_start() -> None:
    assert PROTOCOL.min_scored_per_line == 500 and PROTOCOL.min_scored_fixtures_per_line == 320
    assert PROTOCOL.end == datetime(2027, 6, 1, tzinfo=timezone.utc)
    assert PROTOCOL.amended_on == date(2026, 10, 26) < PROTOCOL.start.date()
    with pytest.raises(ValueError, match="end after it starts"):
        replace(PROTOCOL, end=PROTOCOL.start)
    with pytest.raises(ValueError, match="before the lockbox starts"):
        replace(PROTOCOL, amended_on=PROTOCOL.start.date())


def test_a_kickoff_from_the_lockbox_end_on_is_not_eligible() -> None:
    def record(kickoff: datetime) -> dict[str, Any]:
        return {"logged_at": (kickoff - timedelta(hours=2)).isoformat(), "kickoff_at": kickoff.isoformat(),
                "fixture_id": 5001, "player_id": 7, "market": "tackles", "line": 1.5, "p_win": 0.3, "p_lose": 0.6,
                "p_void": 0.1, "digest": PROTOCOL.pricing_digest, "data_vendor": "sportmonks"}

    assert lockbox.record_reason(record(PROTOCOL.end - timedelta(minutes=1))) is None
    assert lockbox.record_reason(record(PROTOCOL.end)) == lockbox.AFTER_END
    coverage = lockbox.coverage({1: PROTOCOL.end - timedelta(hours=1), 2: PROTOCOL.end}, [], (), ())
    assert coverage.matches == 1


def _rows(per_line: int, fixtures: int) -> lockbox.ScoringRows:
    keys = [(market, line) for market, lines in PROTOCOL.lines.items() for line in lines]
    return lockbox.ScoringRows({key: tuple((n % fixtures, n, 0.5, n % 2) for n in range(per_line)) for key in keys}, {})


def test_the_one_look_waits_for_320_matches_on_every_line_as_well_as_500_predictions() -> None:
    assert not lockbox.ready_to_score(_rows(per_line=600, fixtures=100))
    assert not lockbox.ready_to_score(_rows(per_line=499, fixtures=320))
    assert lockbox.ready_to_score(_rows(per_line=500, fixtures=320))


def test_a_lockbox_that_closes_short_of_the_threshold_has_expired() -> None:
    closes = PROTOCOL.end + PROTOCOL.missing_after
    short, full = _rows(per_line=600, fixtures=100), _rows(per_line=500, fixtures=320)
    assert not lockbox.closed(closes - timedelta(seconds=1)) and lockbox.closed(closes)
    assert not lockbox.expired(short, closes - timedelta(seconds=1))
    assert lockbox.expired(short, closes) and not lockbox.expired(full, closes)


# ---------------- the scorer ----------------
def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_option3", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


class _Conn(_Cursor):
    def cursor(self) -> _Cursor:
        return _Cursor()

    def close(self) -> None:
        return None


def test_a_closed_lockbox_short_of_the_threshold_is_recorded_not_passed_once_and_never_scored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    scorer = _script("gate2_prospective")
    result_file = tmp_path / "gate2_prospective_result.json"
    monkeypatch.setattr(scorer, "STATE", tmp_path)
    monkeypatch.setattr(scorer, "OUTCOMES_FILE", tmp_path / "lockbox_outcomes.jsonl")
    monkeypatch.setattr(scorer, "RESULT_FILE", result_file)
    monkeypatch.setattr(scorer, "RESULT_LOCK_FILE", tmp_path / "gate2_prospective_result.lock")
    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    later = PROTOCOL.end + PROTOCOL.missing_after + timedelta(days=1)

    class _Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_Later":
            return cls.fromtimestamp(later.timestamp(), timezone.utc)

    monkeypatch.setattr(scorer, "datetime", _Later)
    monkeypatch.setattr(scorer, "score", lambda *args, **kwargs: pytest.fail("an expired lockbox must never be scored"))

    assert scorer.main(["--capture-only"]) == 0 and not result_file.exists()
    assert scorer.main(["--dry-run"]) == 1 and not result_file.exists()
    assert scorer.main([]) == 1
    stored = json.loads(result_file.read_text(encoding="utf-8"))
    assert stored["expired"] is True and stored["passed"] is False and "never scored" in stored["blockers"][0]
    assert "markets" not in stored
    capsys.readouterr()
    assert scorer.main([]) == 0
    assert "closed unscored" in capsys.readouterr().out
    assert json.loads(result_file.read_text(encoding="utf-8")) == stored
