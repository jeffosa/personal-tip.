"""Scheduled-task failure alerts (alerts/task_health.py) and the daily backup (scripts/backup.py): maintainer task 4,
2026-10-05. Before this, a failed scheduled step only wrote "STEP FAILED" into a log nobody reads, a task that stopped
running went unnoticed, and placed_bets.jsonl, the only record of the bets placed by hand, had no backup."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

import pytest

from prop_alpha.alerts import task_health
from prop_alpha.alerts.task_health import ALERT_AFTER_FAILURES, REALERT_AFTER, TaskState, evaluate

NOW = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _run(states: dict[str, TaskState], task: str, failed: Sequence[str], at: datetime) -> list[str]:
    updated, messages = evaluate(states, task, failed, at)
    states.clear()
    states.update(updated)
    return messages


def test_one_transient_failure_is_not_paged_but_a_second_in_a_row_is() -> None:
    states: dict[str, TaskState] = {}
    assert _run(states, "status", ["scan"], NOW) == []  # the scheduler retries after 5 minutes
    assert _run(states, "status", [], NOW + timedelta(minutes=5)) == []  # recovered before anyone was paged
    assert _run(states, "status", ["results"], NOW + timedelta(hours=4)) == []
    [alert] = _run(states, "status", ["results"], NOW + timedelta(hours=4, minutes=5))
    assert alert.startswith("TASK FAILING: prop_alpha\\Telegram status failed 2 runs in a row")
    assert "Failed step(s): results" in alert and ".prop_alpha_state\\status.log" in alert
    assert ALERT_AFTER_FAILURES == 2


def test_a_continuing_failure_is_re_paged_only_after_the_interval_or_when_the_steps_change() -> None:
    states: dict[str, TaskState] = {}
    start = NOW
    for minutes in (0, 10):
        _run(states, "ledger", ["poll"], start + timedelta(minutes=minutes))
    quiet = [_run(states, "ledger", ["poll"], start + timedelta(minutes=10 * n)) for n in range(2, 30)]
    assert all(messages == [] for messages in quiet)  # every 10 minutes for 4.7 hours: silent
    [changed] = _run(states, "ledger", ["poll", "settle"], start + timedelta(minutes=300))
    assert "Failed step(s): poll, settle" in changed
    assert _run(states, "ledger", ["poll", "settle"], start + timedelta(minutes=310)) == []
    [again] = _run(states, "ledger", ["poll", "settle"], start + timedelta(minutes=300) + REALERT_AFTER)
    assert again.startswith("TASK FAILING") and "since" in again


def test_recovery_is_announced_once_and_only_after_an_alert() -> None:
    states: dict[str, TaskState] = {}
    for minutes in (0, 10):
        _run(states, "ledger", ["settle"], NOW + timedelta(minutes=minutes))
    [recovered] = _run(states, "ledger", [], NOW + timedelta(minutes=20))
    assert recovered.startswith("TASK RECOVERED: prop_alpha\\Bet ledger poll succeeded again after 2 failed run(s)")
    assert _run(states, "ledger", [], NOW + timedelta(minutes=30)) == []
    assert states["ledger"] == TaskState(NOW + timedelta(minutes=30))


def test_a_task_that_stops_running_is_flagged_by_the_other_one() -> None:
    states: dict[str, TaskState] = {}
    _run(states, "status", [], NOW)
    assert _run(states, "ledger", [], NOW + timedelta(hours=5)) == []  # 5 hours is still within the limit
    [overdue] = _run(states, "ledger", [], NOW + timedelta(hours=5, minutes=10))
    assert overdue.startswith("TASK NOT RUNNING: prop_alpha\\Telegram status has not run since Tue 06 Oct 08:00 UTC")
    assert "expected at least every 5 hours" in overdue
    assert _run(states, "ledger", [], NOW + timedelta(hours=6)) == []  # flagged once per REALERT_AFTER
    assert _run(states, "ledger", [], NOW + timedelta(hours=11, minutes=10))[0].startswith("TASK NOT RUNNING")
    # The status task runs again (its flag clears); by then the ledger poll, last run at 19:10, is 50 minutes silent.
    [silent_poll] = _run(states, "status", [], NOW + timedelta(hours=12))
    assert states["status"].overdue_alerted_at is None
    assert "Bet ledger poll has not run since Tue 06 Oct 19:10 UTC" in silent_poll and "every 40 minutes" in silent_poll
    assert _run(states, "status", [], NOW + timedelta(hours=16)) == []  # flagged once per REALERT_AFTER


def test_inputs_and_states_are_validated() -> None:
    with pytest.raises(ValueError, match="unknown task"):
        evaluate({}, "backup", [], NOW)
    with pytest.raises(ValueError, match="UTC"):
        evaluate({}, "status", [], NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        TaskState(NOW, consecutive_failures=1)  # a streak needs its start and its steps
    with pytest.raises(ValueError):
        TaskState(NOW, failed_steps=("scan",))
    with pytest.raises(ValueError):
        TaskState(NOW, consecutive_failures=-1)


class _Notifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    def send_message(self, text: str, reply_to: int | None = None) -> int:
        if self.fail:
            raise RuntimeError("no network")
        self.sent.append(text)
        return len(self.sent)


def test_state_is_saved_only_after_the_alert_was_sent_so_the_next_run_retries(tmp_path: Path) -> None:
    path = tmp_path / "task_health.json"
    task_health.report_run(None, "status", ["scan"], NOW, path)  # first failure: no alert, no notifier needed
    down = _Notifier(fail=True)
    with pytest.raises(RuntimeError):
        task_health.report_run(down, "status", ["scan"], NOW + timedelta(minutes=5), path)  # type: ignore[arg-type]
    assert task_health.load_states(path)["status"].consecutive_failures == 1  # unchanged: the alert is still owed
    up = _Notifier()
    sent = task_health.report_run(up, "status", ["scan"], NOW + timedelta(minutes=10), path)  # type: ignore[arg-type]
    assert up.sent == sent and sent[0].startswith("TASK FAILING")
    saved = task_health.load_states(path)["status"]
    assert (saved.consecutive_failures, saved.alerted_at) == (2, NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="no Telegram notifier"):
        task_health.report_run(None, "status", [], NOW + timedelta(minutes=15), path)  # the recovery is due


def test_a_corrupt_health_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "task_health.json"
    for bad in ("[]", json.dumps({"other": {}}), json.dumps({"status": {"failed_steps": [], "last_run_at": None,
                                                                        "consecutive_failures": 0}}), "not json"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            task_health.load_states(path)


# ---------------- scripts/backup.py ----------------
def _backup_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prop_alpha_backup_script", ROOT / "scripts" / "backup.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LISTING = "\n".join(f"1; 0 1 TABLE DATA prop_alpha {table} postgres" for table in (
    "fixtures", "player_match_stats", "players", "teams", "leagues", "raw_payloads", "incomplete_team_matches"))


def _fake_runner(listing: str, calls: list[list[str]]) -> Any:
    def runner(command: Sequence[str]) -> str:
        calls.append(list(command))
        if "--list" in command:
            return listing
        Path(command[command.index("-f") + 1]).write_bytes(b"PGDMP fake dump")
        return ""
    return runner


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "odds_archive").mkdir(parents=True)
    (state / "placed_bets.jsonl").write_text('{"event": "placed", "bet_id": 1}\n', encoding="utf-8")
    (state / "sent_alerts.jsonl").write_text("{}\n", encoding="utf-8")
    (state / "odds_archive" / "20261010T120000Z_bet365.json.gz").write_bytes(b"\x1f\x8b raw")
    (state / "status.log").write_text("logs are not state", encoding="utf-8")
    return state


def test_backup_dumps_copies_and_verifies_the_ledger_and_writes_a_manifest(tmp_path: Path) -> None:
    backup = _backup_module()
    calls: list[list[str]] = []
    folder = backup.backup(tmp_path / "backups", _state(tmp_path), NOW, runner=_fake_runner(LISTING, calls))
    assert folder.name == "prop_alpha_20261006T080000Z"
    assert calls[0][1:3] == ["-w", "-h"] and "-Fc" in calls[0] and calls[1][1] == "--list"
    assert not any("password" in part.lower() or "@" in part for call in calls for part in call)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["state"]) == {"placed_bets.jsonl", "sent_alerts.jsonl",
                                      "odds_archive/20261010T120000Z_bet365.json.gz"}
    assert (folder / "state" / "placed_bets.jsonl").read_text(encoding="utf-8") == '{"event": "placed", "bet_id": 1}\n'
    assert manifest["dump"]["bytes"] == len(b"PGDMP fake dump")


def test_a_dump_missing_table_data_fails_and_is_left_incomplete(tmp_path: Path) -> None:
    backup = _backup_module()
    partial = "\n".join(line for line in LISTING.splitlines() if "player_match_stats" not in line)
    with pytest.raises(ValueError, match="missing table data for: player_match_stats"):
        backup.backup(tmp_path / "backups", _state(tmp_path), NOW, runner=_fake_runner(partial, []))
    assert [path.name for path in (tmp_path / "backups").iterdir()] == ["prop_alpha_20261006T080000Z.incomplete"]


def test_prune_keeps_the_newest_complete_backups_and_touches_nothing_else(tmp_path: Path) -> None:
    backup = _backup_module()
    state = _state(tmp_path)
    folders = [backup.backup(tmp_path / "backups", state, NOW + timedelta(days=day), keep=3,
                             runner=_fake_runner(LISTING, [])) for day in range(5)]
    (tmp_path / "backups" / "notes.txt").write_text("keep me", encoding="utf-8")
    (tmp_path / "backups" / "prop_alpha_20261001T000000Z.incomplete").mkdir()
    remaining = sorted(path.name for path in (tmp_path / "backups").iterdir())
    assert remaining == sorted([*(f.name for f in folders[2:]), "notes.txt", "prop_alpha_20261001T000000Z.incomplete"])
    with pytest.raises(ValueError, match="at least 1"):
        backup.prune(tmp_path / "backups", 0, folders[-1])
