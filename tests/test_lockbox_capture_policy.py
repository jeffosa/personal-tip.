"""The capture amendment of 2026-10-27 (maintainer: option D, LOCKBOX_PROTOCOL.md section 6). A match's outcome is captured
from data re-fetched at its first capture run within 4 hours of the 72-hour grace; a later capture is rebuilt from the last
payload fetched at or before the grace, and excluded (never scored) when there is none. Captured outcomes are never
changed, the start and the scoring rules are unchanged."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest

from prop_alpha.evaluation import lockbox
from prop_alpha.evaluation.lockbox import PROTOCOL, StoredMatch

ROOT = Path(__file__).resolve().parents[1]
KICKOFF = PROTOCOL.start + timedelta(days=4, hours=19, minutes=45)
DUE = KICKOFF + PROTOCOL.revision_grace
EARLY = KICKOFF + timedelta(hours=3)  # the results job's first ingestion, three hours after kickoff


def _record(fixture_id: int = 5001, player_id: int = 7) -> dict[str, Any]:
    return {"logged_at": (KICKOFF - timedelta(hours=3)).isoformat(), "window_start": (KICKOFF - timedelta(hours=4)).isoformat(),
            "kickoff_at": KICKOFF.isoformat(), "fixture_id": fixture_id, "player_id": player_id, "market": "tackles",
            "line": 1.5, "p_win": 0.36, "p_lose": 0.54, "p_void": 0.10, "lineup_basis": "estimated",
            "digest": PROTOCOL.pricing_digest, "data_vendor": "sportmonks"}


# ---------------- the protocol and the plan ----------------
def test_the_capture_amendment_is_recorded_before_any_capture_could_be_due() -> None:
    assert PROTOCOL.refetch_window == timedelta(hours=4)
    assert PROTOCOL.start < PROTOCOL.capture_policy_decided_at < PROTOCOL.start + PROTOCOL.revision_grace
    with pytest.raises(ValueError, match="before any capture can be due"):
        replace(PROTOCOL, capture_policy_decided_at=PROTOCOL.start + PROTOCOL.revision_grace)
    with pytest.raises(ValueError, match="re-fetch window"):
        replace(PROTOCOL, refetch_window=timedelta(0))
    assert lockbox.CAPTURE_RUN_INTERVAL == PROTOCOL.refetch_window


def test_the_plan_re_fetches_within_four_hours_of_the_grace_and_rebuilds_after() -> None:
    match = {5001: StoredMatch(KICKOFF, True, {}, EARLY, EARLY)}
    plan = lambda now, captured=None, matches=match: lockbox.capture_plan([_record()], captured or {}, matches, now)  # noqa: E731
    assert plan(DUE - timedelta(seconds=1)) == {}  # inside the grace
    assert plan(DUE) == plan(DUE + PROTOCOL.refetch_window) == {5001: lockbox.REFETCH}
    assert plan(DUE + PROTOCOL.refetch_window + timedelta(seconds=1)) == {5001: lockbox.REBUILD}
    assert plan(DUE + timedelta(hours=1), {(5001, 7): {"captured_at": DUE.isoformat()}}) == {}  # captured once
    assert plan(DUE + timedelta(hours=1), None, {5001: StoredMatch(KICKOFF, False, {})}) == {}  # not final yet


# ---------------- the scorer, end to end ----------------
def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_policy", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Database:
    """What the fake database holds: the match as stored now (kickoff, final, first and last payload fetch, the player's
    current tackles) and the last payload fetched at or before the grace (None: there is none)."""

    def __init__(self, stored_tackles: int, last_fetched: datetime, early_payload: dict[str, Any] | None) -> None:
        self.stored_tackles, self.last_fetched, self.early_payload = stored_tackles, last_fetched, early_payload
        self.queries: list[str] = []

    def connect(self, *args: Any, **kwargs: Any) -> "_Conn":
        return _Conn(self)


class _Cursor:
    def __init__(self, db: _Database) -> None:
        self.db = db

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.db.queries.append(sql)

    def fetchone(self) -> Any:
        return None if self.db.early_payload is None else (self.db.early_payload, EARLY)

    def fetchall(self) -> list[Any]:
        sql = self.db.queries[-1]
        if "prop_alpha.players" in sql:
            return [("5001-h0", 7)]
        if "player_match_stats" in sql:
            return [("5001", 7, 90, self.db.stored_tackles, 1)]
        return [("5001", KICKOFF, True, EARLY, self.db.last_fetched)]


class _Conn(_Cursor):
    def cursor(self) -> _Cursor:
        return _Cursor(self.db)

    def close(self) -> None:
        return None


def _scorer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: _Database, at: datetime,
            refetch: Callable[[str, list[int]], set[int]] | None = None) -> ModuleType:
    scorer = _script("gate2_prospective")
    monkeypatch.setattr(scorer, "STATE", tmp_path)
    monkeypatch.setattr(scorer, "OUTCOMES_FILE", tmp_path / "lockbox_outcomes.jsonl")
    monkeypatch.setattr(scorer, "RESULT_FILE", tmp_path / "gate2_prospective_result.json")
    (tmp_path / "predictions.jsonl").write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", db.connect)

    class _Now(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_Now":
            return cls.fromtimestamp(at.timestamp(), timezone.utc)

    monkeypatch.setattr(scorer, "datetime", _Now)
    monkeypatch.setattr(scorer, "refetch", refetch or (lambda dsn, ids: pytest.fail("no re-fetch expected")))
    return scorer


def _outcomes(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "lockbox_outcomes.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_an_on_time_capture_re_fetches_the_match_then_captures_the_fresh_data(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    asked: list[list[int]] = []

    def refetch(dsn: str, ids: list[int]) -> set[int]:
        asked.append(list(ids))
        return set(ids)

    db = _Database(stored_tackles=3, last_fetched=DUE + timedelta(minutes=30), early_payload=None)  # the fresh fetch
    scorer = _scorer(tmp_path, monkeypatch, db, DUE + timedelta(hours=1), refetch)
    assert scorer.main(["--capture-only"]) == 0
    assert asked == [[5001]]
    [outcome] = _outcomes(tmp_path)
    assert (outcome["capture_basis"], outcome["tackles"]) == (lockbox.REFETCHED, 3)
    out = capsys.readouterr().out
    assert "1 match(es) to re-fetch at the grace (1 re-fetched)" in out and "(1 re-fetched at the grace" in out
    assert "0 first fetched then, 0 revised then" in out  # the fresh fetch is the intended data
    assert scorer.main(["--capture-only"]) == 0 and len(_outcomes(tmp_path)) == 1  # a repeated run captures nothing


def test_a_failed_re_fetch_captures_nothing_and_the_next_run_rebuilds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_payload: Callable[..., dict[str, Any]]) -> None:
    db = _Database(stored_tackles=9, last_fetched=EARLY, early_payload=fixture_payload("5001"))
    scorer = _scorer(tmp_path, monkeypatch, db, DUE + timedelta(hours=1), lambda dsn, ids: set())
    assert scorer.main(["--capture-only"]) == 0 and _outcomes(tmp_path) == []  # nothing captured on stale data
    later = _scorer(tmp_path, monkeypatch, db, DUE + timedelta(hours=5))  # the next run: past the window
    assert later.main(["--capture-only"]) == 0
    [outcome] = _outcomes(tmp_path)
    assert (outcome["capture_basis"], outcome["tackles"]) == (lockbox.REBUILT, 4)  # from the payload, not the stored 9


def test_a_late_capture_is_rebuilt_from_the_last_payload_before_the_grace_never_from_later_data(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        fixture_payload: Callable[..., dict[str, Any]]) -> None:
    # Stored now: 9 tackles from a revision fetched a day after the grace; the payload before the grace had 4.
    db = _Database(stored_tackles=9, last_fetched=DUE + timedelta(days=1), early_payload=fixture_payload("5001"))
    scorer = _scorer(tmp_path, monkeypatch, db, DUE + timedelta(days=2))
    assert scorer.main(["--capture-only"]) == 0
    [outcome] = _outcomes(tmp_path)
    assert (outcome["capture_basis"], outcome["tackles"], outcome["fouls"], outcome["minutes"]) == (lockbox.REBUILT, 4, 2, 90)
    assert outcome["data_first_fetched_at"] == outcome["data_last_fetched_at"] == EARLY.isoformat()
    assert any("fetched_at <= %s" in q for q in db.queries)
    out = capsys.readouterr().out
    assert "1 to rebuild from data before the grace" in out and "1 rebuilt from data before it" in out
    assert "revised at the source since capture (not applied): 1" in out  # the stored 9 is reported, never applied


def test_a_late_capture_with_no_payload_before_the_grace_is_excluded_and_never_scored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    db = _Database(stored_tackles=2, last_fetched=DUE + timedelta(days=1), early_payload=None)  # first fetched late
    scorer = _scorer(tmp_path, monkeypatch, db, DUE + timedelta(days=2))
    assert scorer.main(["--capture-only"]) == 0
    [outcome] = _outcomes(tmp_path)
    assert outcome["excluded"] is True and outcome["capture_basis"] == lockbox.NO_EARLY_DATA
    out = capsys.readouterr().out
    assert f"eligible, {lockbox.EXCLUDED_LATE}: 1" in out and "1 excluded: no data before it" in out
    assert scorer.main(["--capture-only"]) == 0 and len(_outcomes(tmp_path)) == 1  # recorded once, never retried
    store = {(5001, 7): outcome}
    assert lockbox.scoring_rows([_record()], store, DUE + timedelta(days=2)).states == {lockbox.EXCLUDED_LATE: 1}
    assert lockbox.revised(store, {5001: StoredMatch(KICKOFF, True, {7: (90, {"tackles": 5, "fouls": 0})})}) == []


def test_a_dry_run_fetches_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    db = _Database(stored_tackles=2, last_fetched=EARLY, early_payload=None)
    scorer = _scorer(tmp_path, monkeypatch, db, DUE + timedelta(hours=1))
    monkeypatch.setattr(scorer, "rebuild", lambda *args: pytest.fail("a dry run rebuilds nothing"))
    assert scorer.main(["--dry-run"]) == 1  # not scored: counts only
    assert _outcomes(tmp_path) == [] and "dry run: nothing fetched or written" in capsys.readouterr().out


def test_a_payload_of_a_match_not_final_rebuilds_nothing_and_unknown_players_are_left_out(
        fixture_payload: Callable[..., dict[str, Any]]) -> None:
    scorer = _script("gate2_prospective")
    payload = fixture_payload("5001")
    rebuilt = scorer.match_from_payload(payload, EARLY, KICKOFF, {"5001-h0": 7})
    assert rebuilt is not None and set(rebuilt.players) == {7} and rebuilt.players[7] == (90, {"tackles": 4, "fouls": 2})
    payload["fixture"]["status"] = "live"
    assert scorer.match_from_payload(payload, EARLY, KICKOFF, {"5001-h0": 7}) is None
