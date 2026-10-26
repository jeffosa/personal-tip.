"""Operational protections for the prospective lockbox (operational audit, 2026-10-26): the League One/Two scan prices and
logs nothing on history not known to be current; the results job records its last clean run; lockbox outcomes are captured
by the schedule without ever being scored by it; the status report counts the outcomes still to capture."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from prop_alpha.alerts import scan
from prop_alpha.alerts.predictions import PredictionLog
from prop_alpha.evaluation import lockbox
from prop_alpha.ingestion import results
from prop_alpha.scanner.market_scanner import AllocationReport
from prop_alpha.scanner.slate import OddsCoverage

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


# ---------------- the results heartbeat and its freshness rule ----------------
def test_history_is_current_only_after_a_recent_clean_run_that_listed_matches() -> None:
    fresh = {"at": (NOW - timedelta(hours=4)).isoformat(), "listed": 24}
    assert results.results_staleness(fresh, NOW) is None
    assert results.results_staleness({"at": (NOW - timedelta(hours=8, minutes=59)).isoformat(), "listed": 3}, NOW) is None
    assert "no clean League One/Two results run on record" in str(results.results_staleness(None, NOW))
    old = results.results_staleness({"at": (NOW - timedelta(hours=10)).isoformat(), "listed": 24}, NOW)
    assert old is not None and "over 9:00:00 ago" in old
    nothing = results.results_staleness({"at": NOW.isoformat(), "listed": 0}, NOW)
    assert nothing is not None and "listed no finished" in nothing  # e.g. the data plan stopped covering the leagues
    with pytest.raises(ValueError, match="UTC"):
        results.results_staleness(fresh, NOW.replace(tzinfo=None))


def test_the_heartbeat_is_written_only_by_a_clean_real_league_one_two_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from prop_alpha.ingestion import pipeline

    class _Client:
        def close(self) -> None:
            return None

    outcome = {"report": results.ResultsReport(NOW.date(), NOW.date(), listed=24, ingested=2)}
    monkeypatch.setattr(pipeline, "build_vendor_client", lambda environ: _Client())
    monkeypatch.setattr(results, "run", lambda now, client, dsn, **kwargs: outcome["report"])
    monkeypatch.setattr(results, "pilot_leagues", lambda client: ({648: "Brazil Serie A"}, []))
    assert results.main(["--dry-run"]) == 0 and results.load_results_heartbeat() is None  # a dry run proves nothing
    assert results.main(["--pilot"]) == 0 and results.load_results_heartbeat() is None    # the pilot is not League One/Two
    outcome["report"] = results.ResultsReport(NOW.date(), NOW.date(), listed=24, failed={"19700001": "timeout"})
    assert results.main([]) == 1 and results.load_results_heartbeat() is None             # a failed run is not clean
    outcome["report"] = results.ResultsReport(NOW.date(), NOW.date(), listed=24, ingested=2)
    assert results.main([]) == 0
    record = results.load_results_heartbeat()
    assert record is not None and (record["listed"], record["ingested"]) == (24, 2)
    results.RESULTS_OK_FILE.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a results heartbeat"):
        results.load_results_heartbeat()


# ---------------- the League One/Two scan refuses stale history ----------------
class _CountingNotifier:
    def __init__(self) -> None:
        self.sent = 0

    def notify_digest(self, report: AllocationReport, *args: Any, **kwargs: Any) -> tuple[int, list[str]]:
        self.sent += len(report.directives)
        return len(report.directives), []


def _scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heartbeat: dict[str, Any] | None) -> tuple[Any, list[str]]:
    from test_scan import _patch_database, _StubSportmonks

    _patch_database(monkeypatch)
    priced: list[str] = []

    def price(*args: Any, **kwargs: Any) -> tuple[dict[Any, Any], AllocationReport, list[Any]]:
        priced.append("fixture")
        return {}, AllocationReport(), []

    monkeypatch.setattr(scan, "scan_fixture", price)
    monkeypatch.setattr(scan, "collect_odds", lambda *args, **kwargs: (OddsCoverage((), 0, 0, (), ()), [], True))
    summary = scan.run_scan(datetime(2026, 10, 10, 12, tzinfo=timezone.utc), _StubSportmonks(), "postgresql://unused",
                            cast(Any, _CountingNotifier()), scan.SentAlertStore(tmp_path / "sent.jsonl"), 1000.0, "",
                            currency="GBP", odds="oddspapi", odds_client=object(), books=("bet365",), state_dir=tmp_path,
                            predictions=PredictionLog(tmp_path / "predictions.jsonl", data_vendor="sportmonks"),
                            results_heartbeat=heartbeat)
    return summary, priced


def test_the_scan_prices_and_logs_nothing_when_the_history_is_not_known_to_be_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary, priced = _scan(tmp_path, monkeypatch, None)
    assert priced == [] and summary.stale_leagues == [scan.STALE_LEAGUE_LABEL]
    assert not (tmp_path / "predictions.jsonl").exists()  # no lockbox prediction on unverified history
    assert "not priced and no prediction logged, because no clean League One/Two results run on record" in summary.text("")
    stale = {"at": "2026-10-09T20:00:00+00:00", "listed": 24}  # 16 hours before the run
    assert _scan(tmp_path, monkeypatch, stale)[1] == []
    fresh = {"at": "2026-10-10T08:00:00+00:00", "listed": 24}
    summary, priced = _scan(tmp_path, monkeypatch, fresh)
    assert priced == ["fixture"] and summary.stale_leagues == []


def test_the_scheduled_scan_exit_for_stale_history_has_its_own_step_name() -> None:
    from prop_alpha.alerts.ladder_scan import STALE_EXIT

    sys.path.insert(0, str(ROOT / "scripts"))
    run_task = _script("run_task")
    assert scan.STALE_EXIT == STALE_EXIT == run_task.STALE_EXIT
    [step] = [s for s in run_task.TASKS["status"] if s.name == "scan"]
    assert step.failed_name(scan.STALE_EXIT) == "scan-stale" and step.failed_name(scan.ODDS_UNAVAILABLE_EXIT) == "scan-no-odds"
    names = [s.name for s in run_task.TASKS["status"]]
    # The lockbox capture runs after both results jobs (it reads the history they load) and before the scans.
    assert names.index("lockbox-capture") == names.index("pilot-results") + 1 < names.index("scan")


# ---------------- the scheduled lockbox capture never scores ----------------
def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_ops", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_capture_only_captures_and_never_scores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    scorer = _script("gate2_prospective")
    monkeypatch.setattr(scorer, "STATE", tmp_path)
    monkeypatch.setattr(scorer, "OUTCOMES_FILE", tmp_path / "lockbox_outcomes.jsonl")
    monkeypatch.setattr(scorer, "RESULT_FILE", tmp_path / "gate2_prospective_result.json")

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

    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    assert scorer.main(["--capture-only"]) == 0
    out = capsys.readouterr().out
    assert "Capture only: outcomes captured, nothing scored" in out and not (tmp_path / "gate2_prospective_result.json").exists()
    assert scorer.main([]) == 1  # without the flag the same state reports NOT SCORED (exit 1), as before
    with pytest.raises(SystemExit):
        scorer.main(["--capture-only", "--dry-run"])


# ---------------- the coverage line counts outcomes still to capture ----------------
def test_coverage_counts_the_outcomes_due_and_captured() -> None:
    kickoff = lockbox.PROTOCOL.start + timedelta(days=1, hours=15)
    record = {"logged_at": (kickoff - timedelta(hours=2)).isoformat(), "kickoff_at": kickoff.isoformat(), "fixture_id": 5001,
              "player_id": 7, "market": "tackles", "line": 1.5, "p_win": 0.4, "p_lose": 0.5,
              "digest": lockbox.PROTOCOL.pricing_digest, "data_vendor": "sportmonks"}
    other = {**record, "fixture_id": 5002}
    kicked_off = {5001: kickoff, 5002: kickoff}
    early = lockbox.coverage(kicked_off, [record, other], set(), set(), now=kickoff + timedelta(hours=71))
    assert (early.outcomes_due, early.outcomes_captured) == (0, 0)
    late = lockbox.coverage(kicked_off, [record, other], set(), set(), captured={5001}, now=kickoff + timedelta(hours=73))
    assert (late.outcomes_due, late.outcomes_captured) == (2, 1)
    line = lockbox.coverage_line(late)
    assert "outcomes captured for 1 of 2 due (WARNING: 1 not captured: run scripts/gate2_prospective.py --capture-only)" in line
    done = lockbox.coverage(kicked_off, [record, other], set(), set(), captured={5001, 5002}, now=kickoff + timedelta(days=4))
    assert "WARNING: 1" not in lockbox.coverage_line(done) and "outcomes captured for 2 of 2 due" in lockbox.coverage_line(done)
    assert json.dumps(lockbox.coverage_line(early))  # plain text
