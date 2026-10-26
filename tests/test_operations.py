"""Scheduled-task failure alerts (alerts/task_health.py) and the daily backup (scripts/backup.py): maintainer task 4,
2026-10-05. Before this, a failed scheduled step only wrote "STEP FAILED" into a log nobody reads, a task that stopped
running went unnoticed, and placed_bets.jsonl, the only record of the bets placed by hand, had no backup."""

from __future__ import annotations

import hashlib
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
        evaluate({}, "nightly", [], NOW)
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


def test_a_daily_backup_that_stops_running_is_flagged_after_26_hours() -> None:
    states: dict[str, TaskState] = {}
    _run(states, "backup", [], NOW)
    assert _run(states, "ledger", [], NOW + timedelta(hours=26)) == []  # a day plus a catch-up margin
    [overdue] = _run(states, "ledger", [], NOW + timedelta(hours=26, minutes=10))
    assert overdue.startswith("TASK NOT RUNNING: prop_alpha\\Daily backup has not run since Tue 06 Oct 08:00 UTC")
    assert "expected at least every 26 hours" in overdue
    _run(states, "backup", ["backup"], NOW + timedelta(hours=27))
    [failing] = _run(states, "backup", ["backup"], NOW + timedelta(hours=27, minutes=30))  # the scheduler's retry
    assert failing.startswith("TASK FAILING: prop_alpha\\Daily backup failed 2 runs in a row")
    assert ".prop_alpha_state\\backup.log" in failing


# ---------------- the scheduled .cmd scripts report every run to task_health ----------------
# Script -> (task_health task key, the failed-step names it can report). 2026-10-05: task_health was built but no
# script called it, so a failing scheduled step still only wrote "STEP FAILED" into a log nobody reads.
SCHEDULED_SCRIPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "telegram_status.cmd": ("status", ("results", "pilot-results", "lockbox-capture", "fx-refresh", "status-report",
                                       "scan-no-odds", "scan-stale", "scan", "pilot-scan-no-odds", "pilot-scan-stale",
                                       "pilot-scan")),
    "ledger_poll.cmd": ("ledger", ("poll", "closing-prices", "lineups", "settle", "pilot-closing", "pilot-settle")),
    "backup.cmd": ("backup", ("backup", "github-push")),
}


def _commands(script: str) -> list[str]:
    raw = (ROOT / "scripts" / script).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{script} must keep CRLF line endings"
    return [line for line in raw.decode("ascii").splitlines() if line.strip() and not line.lower().startswith("rem ")]


@pytest.mark.parametrize("script", sorted(SCHEDULED_SCRIPTS))
def test_each_scheduled_script_reports_its_failed_steps_to_task_health_last(script: str) -> None:
    task, steps = SCHEDULED_SCRIPTS[script]
    assert task in task_health.TASKS
    lines = _commands(script)
    assert lines[-1] == "exit /b %RC%"
    report = lines[-2]
    assert report.startswith('"%PY%" -m prop_alpha.alerts.task_health ')
    assert f'--task {task} --failed "%FAILED%"' in report
    assert "set RC=1" in report and "STEP FAILED: task health" in report  # a failed report still fails the run
    assert 'set "FAILED="' in lines  # starts empty: a successful run reports no failed step
    recorded = [part.split('"', 1)[0] for line in lines for part in line.split('set "FAILED=%FAILED% ')[1:]]
    assert sorted(recorded) == sorted(steps)
    assert all(step and " " not in step for step in recorded)  # task_health splits --failed on spaces
    # Every step that can fail adds its name: no "STEP FAILED" line without one, except the report itself.
    for line in lines:
        for segment in line.split(" else "):
            if "STEP FAILED" in segment and "task health" not in segment:
                assert 'set "FAILED=%FAILED% ' in segment, segment


def test_the_status_script_reports_a_scan_without_real_odds_as_its_own_step() -> None:
    from prop_alpha.alerts.scan import ODDS_UNAVAILABLE_EXIT

    lines = _commands("telegram_status.cmd")
    scan_at = next(i for i, line in enumerate(lines) if "-m prop_alpha.alerts.scan " in line)
    assert lines[scan_at + 1] == "set SCAN_RC=%ERRORLEVEL%"
    branch = lines[scan_at + 2]
    assert branch.startswith(f'if "%SCAN_RC%"=="{ODDS_UNAVAILABLE_EXIT}" (set RC=1& set "FAILED=%FAILED% scan-no-odds"')
    assert 'else if not "%SCAN_RC%"=="0" (set RC=1& set "FAILED=%FAILED% scan"' in branch


def test_the_status_script_runs_the_pilot_results_and_scan_with_their_own_steps() -> None:
    from prop_alpha.alerts.ladder_scan import STALE_EXIT
    from prop_alpha.alerts.scan import ODDS_UNAVAILABLE_EXIT

    lines = _commands("telegram_status.cmd")
    results_at = next(i for i, line in enumerate(lines) if "-m prop_alpha.ingestion.results --pilot" in line)
    scan_at = next(i for i, line in enumerate(lines) if "-m prop_alpha.alerts.ladder_scan --odds oddspapi --send" in line)
    assert results_at < scan_at  # the pilot database is refreshed before it is scanned
    assert lines[scan_at + 1] == "set PILOT_RC=%ERRORLEVEL%"
    branch = lines[scan_at + 2]
    assert f'if "%PILOT_RC%"=="{ODDS_UNAVAILABLE_EXIT}" (set RC=1& set "FAILED=%FAILED% pilot-scan-no-odds"' in branch
    assert f'else if "%PILOT_RC%"=="{STALE_EXIT}" (set RC=1& set "FAILED=%FAILED% pilot-scan-stale"' in branch
    assert 'else if not "%PILOT_RC%"=="0" (set RC=1& set "FAILED=%FAILED% pilot-scan"' in branch


def test_the_backup_script_runs_the_backup_into_its_own_log() -> None:
    lines = _commands("backup.cmd")
    assert 'set "LOG=.prop_alpha_state\\backup.log"' in lines
    assert task_health.TASKS["backup"][1] == ".prop_alpha_state\\backup.log"
    assert any(line.startswith('"%PY%" scripts\\backup.py --keep 14 >> "%LOG%" 2>&1 ||') for line in lines)


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


COUNTS = {"fixtures": 3, "player_match_stats": 70, "players": 40, "teams": 6, "leagues": 2, "raw_payloads": 3,
          "incomplete_team_matches": 0}
COUNTS_TEXT = "".join(f"{table}|{count}\n" for table, count in COUNTS.items())


def _fake_runner(listing: str, calls: list[list[str]]) -> Any:
    def runner(command: Sequence[str]) -> str:
        calls.append(list(command))
        if "-c" in command:
            return COUNTS_TEXT
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
    (state / "analysis").mkdir()
    (state / "analysis" / "league_one_two_2026-10-17.csv").write_text("player,over_1.5\n", encoding="utf-8")
    (state / "status.log").write_text("logs are not state", encoding="utf-8")
    return state


def test_backup_dumps_copies_and_verifies_the_ledger_and_writes_a_manifest(tmp_path: Path) -> None:
    backup = _backup_module()
    calls: list[list[str]] = []
    folder = backup.backup(tmp_path / "backups", _state(tmp_path), NOW, runner=_fake_runner(LISTING, calls))
    assert folder.name == "prop_alpha_20261006T080000Z"
    dump_at = next(i for i, call in enumerate(calls) if "-Fc" in call)
    assert calls[dump_at][1:3] == ["-w", "-h"] and calls[dump_at + 1][1] == "--list"
    assert "-c" in calls[0] and "-c" in calls[dump_at + 2]  # counted before and after the dump
    assert not any("password" in part.lower() or "@" in part for call in calls for part in call)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["state"]) == {"placed_bets.jsonl", "sent_alerts.jsonl",
                                      "odds_archive/20261010T120000Z_bet365.json.gz",
                                      "analysis/league_one_two_2026-10-17.csv"}  # the stored analyses (2026-10-15)
    assert (folder / "state" / "placed_bets.jsonl").read_text(encoding="utf-8") == '{"event": "placed", "bet_id": 1}\n'
    assert manifest["dump"]["bytes"] == len(b"PGDMP fake dump") and manifest["dump"]["counts"] == COUNTS


def test_the_backup_covers_the_prediction_log_and_the_t60_team_sheets(tmp_path: Path) -> None:
    """Audit 2026-10-10: predictions.py and lineup_capture.py say their files are in the daily backup, but the file
    list omitted them, so a successful backup did not protect the Gate 2 lockbox evidence."""
    from prop_alpha.alerts import lineup_capture, predictions

    backup = _backup_module()
    names = (predictions.PREDICTIONS_NAME, lineup_capture.LINEUPS_NAME, lineup_capture.HANDLED_NAME)
    assert set(names) <= set(backup.STATE_FILES)
    state = _state(tmp_path)
    for name in names:
        (state / name).write_text(f"evidence {name}\n", encoding="utf-8")
    folder = backup.backup(tmp_path / "backups", state, NOW, runner=_fake_runner(LISTING, []))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert set(names) <= set(manifest["state"])
    assert all((folder / "state" / name).read_text(encoding="utf-8") == f"evidence {name}\n" for name in names)
    # The manifest's hashes and sizes are those of the source bytes, and the copies match them.
    for name in names:
        data = (state / name).read_bytes()  # not re-encoded: write_text translates line endings on Windows
        assert manifest["state"][name] == {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        assert hashlib.sha256((folder / "state" / name).read_bytes()).hexdigest() == manifest["state"][name]["sha256"]


def test_the_backup_covers_every_pilot_evidence_file() -> None:
    """Audit 2026-10-11: the pilot scan's alerts, paper bets, tips, predictions, closing prices and refusals are evidence
    (and the only record of what was tipped), so each file name its module writes must be in the backup list."""
    from prop_alpha.alerts import ladder_scan, pilot_closing, pilot_ledger
    from prop_alpha.ingestion import results

    backup = _backup_module()
    written = {ladder_scan.PILOT_STATE_FILE.name, ladder_scan.PILOT_INDEX_FILE.name, ladder_scan.PILOT_DELIVERY_FILE.name,
               ladder_scan.PILOT_PREDICTIONS_FILE.name, pilot_ledger.PILOT_BETS_FILE.name, pilot_ledger.PILOT_TIPS_FILE.name,
               pilot_ledger.PILOT_CLOSING_FILE.name, pilot_closing.HANDLED_FILE.name, results.PILOT_REFUSED_FILE.name,
               results.REFUSED_FILE.name,
               ladder_scan.PILOT_HEARTBEAT_FILE.name}
    assert written <= set(backup.STATE_FILES), sorted(written - set(backup.STATE_FILES))


def test_absent_prediction_and_lineup_files_are_skipped_safely(tmp_path: Path) -> None:
    """Before the first scan or T-60 capture the files do not exist: that is not an error and not in the manifest.
    A later backup picks each up as soon as it exists."""
    from prop_alpha.alerts import lineup_capture, predictions

    backup = _backup_module()
    state = _state(tmp_path)
    optional = {predictions.PREDICTIONS_NAME, lineup_capture.LINEUPS_NAME, lineup_capture.HANDLED_NAME}
    first = backup.backup(tmp_path / "backups", state, NOW, runner=_fake_runner(LISTING, []))
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert not optional & set(manifest["state"])
    for name in optional:
        (state / name).write_text(f"restored evidence {name}\n", encoding="utf-8")
    second = backup.backup(tmp_path / "backups", state, NOW + timedelta(days=1), runner=_fake_runner(LISTING, []))
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert optional <= set(manifest["state"])


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


# ---------------- scripts/run_hidden.py: scheduled scripts without a console window ----------------
def _launcher_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prop_alpha_run_hidden", ROOT / "scripts" / "run_hidden.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeRun:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append((command, kwargs))
        return type("Completed", (), {"returncode": self.returncode})()


@pytest.mark.parametrize("returncode", [0, 1, 3])
def test_launcher_runs_the_script_in_a_hidden_console_and_passes_its_exit_code_on(returncode: int) -> None:
    """2026-10-05: each run opened a visible console; a Ctrl+C or close killed it (0xC000013A) before it logged."""
    launcher = _launcher_module()
    fake = _FakeRun(returncode)
    assert launcher.main(["scripts/ledger_poll.cmd"], run=fake) == returncode
    [(command, kwargs)] = fake.calls
    assert command == ["cmd.exe", "/d", "/c", str((ROOT / "scripts" / "ledger_poll.cmd").resolve())]
    assert kwargs["creationflags"] == launcher.CREATE_NO_WINDOW == 0x08000000
    assert kwargs["cwd"] == str(ROOT) and kwargs["check"] is False


def test_launcher_runs_only_an_existing_cmd_script_inside_the_repository(tmp_path: Path) -> None:
    launcher = _launcher_module()
    fake = _FakeRun(0)
    outside = tmp_path / "evil.cmd"
    outside.write_text("@echo off\n", encoding="utf-8")
    for bad in ([r"scripts\backup.py"], [r"scripts\missing.cmd"], [str(outside)], [r"..\x.cmd"], [],
                [r"scripts\ledger_poll.cmd", r"scripts\telegram_status.cmd"]):
        with pytest.raises(ValueError):
            launcher.main(bad, run=fake)
    assert fake.calls == []
    for script in ("ledger_poll.cmd", "telegram_status.cmd"):
        assert launcher.resolve_script(f"scripts/{script}") == (ROOT / "scripts" / script).resolve()


def test_the_backup_folder_comes_from_the_environment_then_env_and_must_exist(tmp_path: Path) -> None:
    """2026-10-05: the folder was read from the process environment only, so a setting in .env (where every other
    setting lives, and what a scheduled task sees) was ignored and backups stayed on this disk."""
    backup = _backup_module()
    env_file = tmp_path / ".env"
    assert backup.backup_dir_setting({}, env_file) == backup.DEFAULT_BACKUP_DIR  # no .env at all
    synced = tmp_path / "OneDrive" / "prop_alpha_backups"
    synced.mkdir(parents=True)
    env_file.write_text(f"TELEGRAM_CHAT_ID=1\nPROP_ALPHA_BACKUP_DIR={synced}\n", encoding="utf-8")
    assert backup.backup_dir_setting({}, env_file) == synced
    other = tmp_path / "other"
    other.mkdir()
    assert backup.backup_dir_setting({"PROP_ALPHA_BACKUP_DIR": str(other)}, env_file) == other  # environment first
    env_file.write_text("PROP_ALPHA_BACKUP_DIR=\n", encoding="utf-8")
    assert backup.backup_dir_setting({}, env_file) == backup.DEFAULT_BACKUP_DIR
    # A typo or an unmounted drive must not quietly create a local folder that never leaves the machine.
    env_file.write_text(f"PROP_ALPHA_BACKUP_DIR={tmp_path / 'OneDrive' / 'typo'}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not an existing folder"):
        backup.backup_dir_setting({}, env_file)
    assert not (tmp_path / "OneDrive" / "typo").exists()


def _pilot_runner(exists: bool, calls: list[list[str]], pilot_listing: str = LISTING) -> Any:
    def runner(command: Sequence[str]) -> str:
        calls.append(list(command))
        if "-c" in command:
            return ("1\n" if exists else "") if "pg_database" in command[-1] else COUNTS_TEXT
        if "--list" in command:
            return pilot_listing if "prop_alpha_pilot.dump" in command[-1] else LISTING
        Path(command[command.index("-f") + 1]).write_bytes(b"PGDMP " + command[command.index("-d") + 1].encode())
        return ""
    return runner


def test_the_pilot_database_is_dumped_checked_and_listed_in_the_manifest(tmp_path: Path) -> None:
    """Audit 2026-10-11, finding 5: prop_alpha_pilot was in no backup."""
    backup = _backup_module()
    calls: list[list[str]] = []
    folder = backup.backup(tmp_path / "backups", _state(tmp_path), NOW, runner=_pilot_runner(True, calls),
                           pilot_db=backup.PILOT_DB)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    dump = folder / backup.PILOT_DUMP_NAME
    assert dump.read_bytes() == f"PGDMP {backup.PILOT_DB}".encode()
    assert manifest["pilot_dump"] == {"database": backup.PILOT_DB, "bytes": dump.stat().st_size,
                                      "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "counts": COUNTS}
    assert (folder / "prop_alpha.dump").read_bytes() == f"PGDMP {backup.SOURCE_DB}".encode()  # the working dump is unchanged
    assert sum(1 for call in calls if "-Fc" in call) == 2 and not any("password" in " ".join(c).lower() for c in calls)


def test_a_server_without_the_pilot_database_still_backs_up_and_a_bad_pilot_dump_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = _backup_module()
    folder = backup.backup(tmp_path / "backups", _state(tmp_path), NOW, runner=_pilot_runner(False, []),
                           pilot_db=backup.PILOT_DB)
    assert "pilot_dump" not in json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert f"no {backup.PILOT_DB} database" in capsys.readouterr().out
    partial = "\n".join(line for line in LISTING.splitlines() if "player_match_stats" not in line)
    with pytest.raises(ValueError, match="missing table data for: player_match_stats"):
        backup.backup(tmp_path / "backups2", _state(tmp_path / "s2"), NOW, runner=_pilot_runner(True, [], partial),
                      pilot_db=backup.PILOT_DB)
    with pytest.raises(ValueError, match="plain database name"):
        backup.database_exists("x'; DROP DATABASE prop_alpha; --", _pilot_runner(True, []))


def test_the_backup_covers_the_shadow_picks() -> None:
    """2026-10-25: the withheld picks kept for their closing-line value are evidence, and their only record."""
    from prop_alpha.alerts import shadow_picks

    backup = _backup_module()
    assert {shadow_picks.SHADOW_FILE.name, shadow_picks.PILOT_SHADOW_FILE.name} <= set(backup.STATE_FILES)
