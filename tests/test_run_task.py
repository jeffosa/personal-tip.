"""The portable scheduled tasks (maintainer decision 4, 2026-10-11): scripts/run_task.py runs the same steps as the
Windows .cmd scripts, deploy/systemd/ schedules them on Ubuntu, scripts/backup.py finds the PostgreSQL tools on any
host and records row counts, and scripts/restore_backup.py restores a backup without overwriting anything."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

import pytest

from prop_alpha.alerts import task_health

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
NOW = datetime(2026, 10, 11, 2, 30, tzinfo=timezone.utc)
CMD_SCRIPTS = {"telegram_status.cmd": "status", "ledger_poll.cmd": "ledger", "backup.cmd": "backup"}


def _script(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------- scripts/run_task.py ----------------
def _cmd_lines(script: str) -> list[str]:
    text = (SCRIPTS / script).read_text(encoding="ascii")
    return [line for line in text.splitlines() if line.strip() and not line.lower().startswith("rem ")]


@pytest.mark.parametrize("script", sorted(CMD_SCRIPTS))
def test_each_task_runs_the_same_commands_in_the_same_order_as_its_cmd_script(script: str) -> None:
    run_task = _script("run_task")
    commands = [line[len('"%PY%" '):line.index(' >> "%LOG%"')].replace("\\", "/").split()
                for line in _cmd_lines(script) if line.startswith('"%PY%" ') and "task_health" not in line]
    assert commands == [list(step.argv) for step in run_task.TASKS[CMD_SCRIPTS[script]]]


@pytest.mark.parametrize("script", sorted(CMD_SCRIPTS))
def test_each_task_reports_the_same_failed_step_names_as_its_cmd_script(script: str) -> None:
    run_task = _script("run_task")
    recorded = sorted(part.split('"', 1)[0] for line in _cmd_lines(script)
                      for part in line.split('set "FAILED=%FAILED% ')[1:])
    steps = run_task.TASKS[CMD_SCRIPTS[script]]
    assert recorded == sorted([step.name for step in steps] + [name for step in steps for _, name in step.named_exits])


def test_the_named_exit_codes_match_the_scans_and_the_tasks_match_task_health() -> None:
    from prop_alpha.alerts.ladder_scan import STALE_EXIT
    from prop_alpha.alerts.scan import ODDS_UNAVAILABLE_EXIT

    run_task = _script("run_task")
    assert (run_task.ODDS_UNAVAILABLE_EXIT, run_task.STALE_EXIT) == (ODDS_UNAVAILABLE_EXIT, STALE_EXIT)
    assert set(run_task.TASKS) == set(task_health.TASKS)
    with pytest.raises(ValueError, match="one word"):
        run_task.Step(("-m", "x"), "two words")


def test_every_step_runs_failures_are_named_and_reported_last() -> None:
    run_task = _script("run_task")
    calls: list[tuple[str, ...]] = []
    notes: list[str] = []
    codes = {"prop_alpha.ingestion.results": 1, "prop_alpha.alerts.scan": 3, "prop_alpha.alerts.ladder_scan": 4}

    def run(argv: Sequence[str]) -> int:
        calls.append(tuple(argv))
        return codes.get(argv[1], 0) if argv[1:2] and "--pilot" not in argv else 0

    assert run_task.execute("status", run, notes.append) == 1
    assert len(calls) == len(run_task.TASKS["status"]) + 1  # every step ran despite the failures
    assert calls[-1] == ("-m", "prop_alpha.alerts.task_health", "--task", "status", "--failed",
                         "results scan-no-odds pilot-scan-stale")
    assert notes == ["STEP FAILED: results (exit 1)", "STEP FAILED: scan-no-odds (exit 3)",
                     "STEP FAILED: pilot-scan-stale (exit 4)"]


def test_a_clean_run_exits_0_and_a_failed_report_still_fails_the_run() -> None:
    run_task = _script("run_task")
    reported: list[tuple[str, ...]] = []

    def record(argv: Sequence[str]) -> int:
        reported.append(tuple(argv))
        return 0

    assert run_task.execute("backup", record, lambda _: None) == 0
    assert reported[-1][-2:] == ("--failed", "")
    notes: list[str] = []
    assert run_task.execute("ledger", lambda argv: 1 if "task_health" in " ".join(argv) else 0, notes.append) == 1
    assert notes == ["STEP FAILED: task health"]
    with pytest.raises(ValueError, match="unknown task"):
        run_task.execute("nightly", lambda _: 0, lambda _: None)


# ---------------- deploy/systemd ----------------
SCHEDULES = {"status": "*-*-* 00/4:00:00 UTC", "ledger": "*-*-* *:00/10:00 UTC", "backup": "*-*-* 02:30:00 UTC"}


@pytest.mark.parametrize("task", sorted(SCHEDULES))
def test_each_task_has_a_systemd_service_and_timer_on_its_utc_schedule(task: str) -> None:
    service = (ROOT / "deploy" / "systemd" / f"prop-alpha-{task}.service").read_bytes()
    timer = (ROOT / "deploy" / "systemd" / f"prop-alpha-{task}.timer").read_bytes()
    assert b"\r" not in service and b"\r" not in timer  # systemd files are LF only
    lines = service.decode().splitlines()
    assert f"ExecStart=/opt/prop_alpha/.venv/bin/python scripts/run_task.py {task}" in lines
    assert "Type=oneshot" in lines and "User=propalpha" in lines and "WorkingDirectory=/opt/prop_alpha" in lines
    assert ("Restart=on-failure" in lines) == (task == "backup")  # status and ledger re-runs would re-send messages
    assert f"OnCalendar={SCHEDULES[task]}" in timer.decode().splitlines() and b"Persistent=true" in timer


# ---------------- scripts/backup.py on any host ----------------
COUNTS = {"fixtures": 3, "player_match_stats": 70, "players": 40, "teams": 6, "leagues": 2, "raw_payloads": 3,
          "incomplete_team_matches": 0}
LISTING = "\n".join(f"1; 0 1 TABLE DATA prop_alpha {table} postgres" for table in COUNTS)


def _text(counts: dict[str, int]) -> str:
    return "".join(f"{table}|{count}\n" for table, count in counts.items())


def test_the_postgresql_tools_come_from_the_setting_the_windows_folder_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _script("backup")
    assert backup.pg_tool("pg_dump", {"PROP_ALPHA_PG_BIN": str(tmp_path)}) == str(tmp_path / "pg_dump")
    with pytest.raises(ValueError, match="not an existing folder"):
        backup.pg_tool("pg_dump", {"PROP_ALPHA_PG_BIN": str(tmp_path / "missing")})
    monkeypatch.setattr(backup, "WINDOWS_PG_BIN", tmp_path / "no-windows-install")
    assert backup.pg_tool("pg_dump", {}, lambda name: f"/usr/bin/{name}") == "/usr/bin/pg_dump"
    with pytest.raises(ValueError, match="pg_dump not found"):
        backup.pg_tool("pg_dump", {}, lambda name: None)


def _backup_runner(counts: list[dict[str, int]]) -> Any:
    """Counts come from `counts` in turn (the last one repeats); dumps are written as fake files."""
    def runner(command: Sequence[str]) -> str:
        if "-c" in command:
            if "pg_database" in command[-1]:
                return ""
            return _text(counts.pop(0) if len(counts) > 1 else counts[0])
        if "--list" in command:
            return LISTING
        Path(command[command.index("-f") + 1]).write_bytes(b"PGDMP " + command[command.index("-d") + 1].encode())
        return ""
    return runner


def test_a_database_written_to_during_the_dump_fails_the_backup(tmp_path: Path) -> None:
    backup = _script("backup")
    (tmp_path / "state").mkdir()
    changed = dict(COUNTS, player_match_stats=72)
    with pytest.raises(ValueError, match=r"prop_alpha changed during the dump \(player_match_stats\)"):
        backup.backup(tmp_path / "backups", tmp_path / "state", NOW, runner=_backup_runner([COUNTS, changed]))
    assert [path.name for path in (tmp_path / "backups").iterdir()] == ["prop_alpha_20261011T023000Z.incomplete"]


# ---------------- scripts/restore_backup.py ----------------
def _made_backup(tmp_path: Path) -> Path:
    backup = _script("backup")
    state = tmp_path / "laptop_state"
    (state / "odds_archive").mkdir(parents=True)
    (state / "placed_bets.jsonl").write_text('{"bet_id": 1}\n', encoding="utf-8")
    (state / "odds_archive" / "a.json.gz").write_bytes(b"\x1f\x8b raw")
    return Path(backup.backup(tmp_path / "backups", state, NOW, runner=_backup_runner([COUNTS]), pilot_db="prop_alpha_pilot"))


def _restore_runner(calls: list[list[str]], existing: frozenset[str] = frozenset(), counts: dict[str, int] = COUNTS) -> Any:
    def runner(command: Sequence[str]) -> str:
        calls.append(list(command))
        if "-c" in command:
            if "pg_database" in command[-1]:
                return "1\n" if any(f"'{name}'" in command[-1] for name in existing) else ""
            return _text(counts)
        return ""
    return runner


def _backup_with_pilot(tmp_path: Path) -> Path:
    backup = _script("backup")
    state = tmp_path / "laptop_state"
    (state / "odds_archive").mkdir(parents=True)
    (state / "placed_bets.jsonl").write_text('{"bet_id": 1}\n', encoding="utf-8")
    (state / "odds_archive" / "a.json.gz").write_bytes(b"\x1f\x8b raw")

    def runner(command: Sequence[str]) -> str:
        if "-c" in command and "pg_database" in command[-1]:
            return "1\n"  # the laptop has the pilot database
        return _backup_runner([COUNTS])(command)
    return Path(backup.backup(tmp_path / "backups", state, NOW, runner=runner, pilot_db="prop_alpha_pilot"))


def test_a_restore_verifies_creates_restores_checks_counts_and_copies_the_state(tmp_path: Path) -> None:
    restore = _script("restore_backup")
    folder = _backup_with_pilot(tmp_path)
    calls: list[list[str]] = []
    lines = restore.restore(folder, "prop_alpha", "prop_alpha_pilot", tmp_path / "server_state", _restore_runner(calls))
    created = [call[-1] for call in calls if Path(call[0]).name.startswith("createdb")]
    assert created == ["prop_alpha", "prop_alpha_pilot"]
    restored = [call for call in calls if Path(call[0]).name.startswith("pg_restore")]
    assert all("--exit-on-error" in call and "--no-owner" in call for call in restored) and len(restored) == 2
    assert lines[0].startswith("prop_alpha: fixtures 3, player_match_stats 70") and "match the backup" in lines[0]
    assert (tmp_path / "server_state" / "placed_bets.jsonl").read_text(encoding="utf-8") == '{"bet_id": 1}\n'
    assert (tmp_path / "server_state" / "odds_archive" / "a.json.gz").read_bytes() == b"\x1f\x8b raw"
    assert not any("password" in " ".join(call).lower() for call in calls)


def test_a_restore_refuses_before_writing_anything(tmp_path: Path) -> None:
    restore = _script("restore_backup")
    folder = _backup_with_pilot(tmp_path)
    for existing in (frozenset({"prop_alpha"}), frozenset({"prop_alpha_pilot"})):  # never over a database
        calls: list[list[str]] = []
        with pytest.raises(ValueError, match="already exists"):
            restore.restore(folder, "prop_alpha", "prop_alpha_pilot", None, _restore_runner(calls, existing))
        assert not any(Path(call[0]).name.startswith(("createdb", "pg_restore")) for call in calls)
    server = tmp_path / "server_state"
    server.mkdir()
    (server / "placed_bets.jsonl").write_text("the server's own ledger\n", encoding="utf-8")
    calls = []
    with pytest.raises(ValueError, match="never overwrites state files"):  # never over a ledger
        restore.restore(folder, "prop_alpha", "prop_alpha_pilot", server, _restore_runner(calls))
    assert not any(Path(call[0]).name.startswith("createdb") for call in calls)
    assert (server / "placed_bets.jsonl").read_text(encoding="utf-8") == "the server's own ledger\n"
    with pytest.raises(ValueError, match="different names"):
        restore.restore(folder, "same", "same", None, _restore_runner([]))


def test_a_tampered_backup_or_different_counts_fail_the_restore(tmp_path: Path) -> None:
    restore = _script("restore_backup")
    folder = _backup_with_pilot(tmp_path)
    calls: list[list[str]] = []
    with pytest.raises(ValueError, match="restored prop_alpha does not match the backup: player_match_stats 69"):
        restore.restore(folder, "prop_alpha", "prop_alpha_pilot", None,
                        _restore_runner(calls, counts=dict(COUNTS, player_match_stats=69)))
    (folder / "state" / "placed_bets.jsonl").write_text('{"bet_id": 2}\n', encoding="utf-8")
    calls = []
    with pytest.raises(ValueError, match="does not match the manifest"):
        restore.restore(folder, "prop_alpha_2", "prop_alpha_pilot_2", None, _restore_runner(calls))
    assert calls == []  # verified before any database call
    (folder / "manifest.json").unlink()
    with pytest.raises(ValueError, match="not a complete backup"):
        restore.verify(folder)


def test_an_older_backup_without_counts_restores_and_says_its_counts_are_unchecked(tmp_path: Path) -> None:
    restore = _script("restore_backup")
    folder = _made_backup(tmp_path)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    del manifest["dump"]["counts"]
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = restore.restore(folder, "prop_alpha_restore_test", "prop_alpha_pilot_restore_test", None, _restore_runner([]))
    assert lines == [f"prop_alpha_restore_test: {', '.join(f'{t} {c:,}' for t, c in COUNTS.items())} "
                     "(no counts in this backup to check)"]
