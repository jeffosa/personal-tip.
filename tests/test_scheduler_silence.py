"""Silence of the whole machine (reliability review, 2026-10-26): a gap with no scheduled task is alerted once when the
laptop comes back, with the runs it swallowed; running on battery is alerted before the laptop sleeps; and the status
report shows recent gaps, the power source and when the next report is due (the only signal while it is silent)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

from prop_alpha.alerts import status, task_health
from prop_alpha.alerts.task_health import (
    MachineState,
    PowerStatus,
    SilentGap,
    TaskState,
    evaluate_power,
    silent_gap,
)

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 22, 18, 1, tzinfo=timezone.utc)
BATTERY = PowerStatus(False, 29)
MAINS = PowerStatus(True, 100)


class _Notifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    def send_message(self, text: str, reply_to: int | None = None) -> int:
        if self.fail:
            raise RuntimeError("no network")
        self.sent.append(text)
        return len(self.sent)


# ---------------- the gap ----------------
def test_no_gap_within_the_poll_limit_or_before_any_task_reported() -> None:
    assert silent_gap({}, T0) is None
    states = {"ledger": TaskState(T0), "status": TaskState(T0 - timedelta(hours=3))}
    assert silent_gap(states, T0 + task_health.SILENCE_LIMIT) is None  # the latest report of any task counts
    assert silent_gap(states, T0 + task_health.SILENCE_LIMIT + timedelta(seconds=1)) is not None


def test_a_silent_stretch_counts_the_scheduled_runs_it_swallowed() -> None:
    # The laptop was silent from Thu 22 Oct 18:01 to Sun 25 Oct 03:18 UTC (status.log, 2026-10-25).
    gap = silent_gap({"status": TaskState(T0)}, datetime(2026, 10, 25, 3, 18, tzinfo=timezone.utc))
    assert gap is not None
    assert (gap.missed_status_runs, gap.missed_backups, gap.missed_polls) == (14, 3, 342)
    assert gap.hours == pytest.approx(57.283, abs=0.001)
    # Runs exactly at the gap's ends are the ones that did happen.
    on_boundaries = silent_gap({"status": TaskState(datetime(2026, 10, 26, 16, tzinfo=timezone.utc))},
                               datetime(2026, 10, 27, 0, tzinfo=timezone.utc))
    assert on_boundaries is not None and on_boundaries.missed_status_runs == 1  # 20:00 only
    with pytest.raises(ValueError):
        SilentGap(T0, T0, 0, 0, 0)


def test_a_gap_is_alerted_once_logged_and_explains_the_tasks_it_silenced(tmp_path: Path) -> None:
    health, gaps = tmp_path / "task_health.json", tmp_path / "scheduler_gaps.jsonl"
    up = _Notifier()
    task_health.report_run(None, "status", [], T0, health, gap_log=gaps)
    task_health.report_run(None, "ledger", [], T0 + timedelta(minutes=5), health, gap_log=gaps)
    back = T0 + timedelta(hours=10)
    sent = task_health.report_run(up, "ledger", [], back, health, power=BATTERY, gap_log=gaps)  # type: ignore[arg-type]
    assert [m.split(":")[0] for m in sent] == ["SCHEDULER WAS SILENT", "ON BATTERY (29%)"]
    assert "from Thu 22 Oct 18:06 to Fri 23 Oct 04:01 UTC (9.9 hours)" in sent[0]
    assert "Missed: 3 four-hourly run(s)" in sent[0] and "1 daily backup(s)" in sent[0]
    assert "Power now: battery (29%)" in sent[0]
    assert not any(m.startswith("TASK NOT RUNNING") for m in sent)  # the status task's silence is the machine's
    [record] = [json.loads(line) for line in gaps.read_text(encoding="utf-8").splitlines()]
    assert record["missed_status_runs"] == 3 and record["noticed_by"] == "ledger" and record["power"] == "battery (29%)"
    # The status task catches up minutes later: no second gap alert, no second record, no repeat of the battery alert.
    assert task_health.report_run(up, "status", [], back + timedelta(minutes=3), health, power=BATTERY,  # type: ignore[arg-type]
                                  gap_log=gaps) == []
    assert len(gaps.read_text(encoding="utf-8").splitlines()) == 1


def test_a_gap_whose_alert_could_not_be_sent_is_alerted_by_the_next_run(tmp_path: Path) -> None:
    health, gaps = tmp_path / "task_health.json", tmp_path / "scheduler_gaps.jsonl"
    task_health.report_run(None, "ledger", [], T0, health, gap_log=gaps)
    with pytest.raises(RuntimeError):
        task_health.report_run(_Notifier(fail=True), "ledger", [], T0 + timedelta(hours=2), health,  # type: ignore[arg-type]
                               gap_log=gaps)
    assert not gaps.exists() and task_health.load_states(health)["ledger"].last_run_at == T0
    up = _Notifier()
    [alert] = task_health.report_run(up, "ledger", [], T0 + timedelta(hours=2, minutes=10), health,  # type: ignore[arg-type]
                                     gap_log=gaps)
    assert "(2.2 hours)" in alert and len(gaps.read_text(encoding="utf-8").splitlines()) == 1


# ---------------- battery ----------------
def test_battery_is_alerted_once_per_spell_and_mains_clears_it() -> None:
    machine, [alert] = evaluate_power(MachineState(), BATTERY, T0)
    assert alert.startswith("ON BATTERY (29%)") and "sleeps after 3 minutes idle" in alert
    assert evaluate_power(machine, BATTERY, T0 + timedelta(hours=1)) == (machine, [])
    assert evaluate_power(machine, BATTERY, T0 + task_health.REALERT_AFTER)[1]  # still on battery 6 hours later
    cleared, quiet = evaluate_power(machine, MAINS, T0 + timedelta(hours=2))
    assert cleared == MachineState() and quiet == []
    assert evaluate_power(cleared, BATTERY, T0 + timedelta(hours=3))[1]  # a new battery spell
    for unknown in (None, PowerStatus(None)):
        assert evaluate_power(MachineState(), unknown, T0) == (MachineState(), [])
    with pytest.raises(ValueError):
        PowerStatus(False, 101)


def test_a_health_file_from_before_the_machine_state_loads_and_the_battery_flag_survives(tmp_path: Path) -> None:
    health = tmp_path / "task_health.json"
    health.write_text(json.dumps({"ledger": {"last_run_at": T0.isoformat(), "consecutive_failures": 0,
                                             "failed_steps": []}}), encoding="utf-8")
    assert task_health.load_machine(health) == MachineState()
    up = _Notifier()
    task_health.report_run(up, "ledger", [], T0 + timedelta(minutes=10), health, power=BATTERY)  # type: ignore[arg-type]
    assert task_health.load_machine(health) == MachineState(T0 + timedelta(minutes=10))
    assert set(task_health.load_states(health)) == {"ledger"}
    health.write_text(json.dumps({"machine": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        task_health.load_machine(health)


# ---------------- the gap log ----------------
def test_the_gap_log_keeps_the_longest_record_per_start_and_survives_a_write_cut_short(tmp_path: Path) -> None:
    gaps = tmp_path / "scheduler_gaps.jsonl"
    first = SilentGap(T0, T0 + timedelta(hours=2), 0, 11, 0)
    task_health.append_gap(first, "ledger", None, gaps)
    task_health.append_gap(SilentGap(T0, T0 + timedelta(hours=3), 1, 17, 0), "status", BATTERY, gaps)
    with gaps.open("a", encoding="utf-8") as handle:
        handle.write('{"start": "2026-10-23')  # a power loss in the middle of a write
    loaded, unfinished = task_health.load_gaps(gaps)
    assert [g.end for g in loaded] == [T0 + timedelta(hours=3)] and unfinished == 1
    later = SilentGap(T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1), 0, 5, 0)
    task_health.append_gap(later, "ledger", None, gaps)  # drops the fragment first: it never becomes a complete line
    loaded, unfinished = task_health.load_gaps(gaps)
    assert [g.start for g in loaded] == [T0, T0 + timedelta(days=1)] and unfinished == 0
    assert len(gaps.read_text(encoding="utf-8").splitlines()) == 3
    with gaps.open("a", encoding="utf-8") as handle:
        handle.write('["not", "a", "gap"]' + chr(10))  # a complete line that is not a record is reported, never guessed
    with pytest.raises(ValueError, match="not a gap record"):
        task_health.load_gaps(gaps)


# ---------------- the status report ----------------
def test_the_status_report_shows_recent_gaps_the_power_source_and_when_the_next_report_is_due(tmp_path: Path) -> None:
    gaps = tmp_path / "scheduler_gaps.jsonl"
    now = datetime(2026, 10, 26, 20, 51, tzinfo=timezone.utc)
    lines = status.scheduler_section(gaps, now, None)
    assert lines == ["Scheduler silent periods in the last 7 days: none recorded",
                     "Next status report due by Tue 27 Oct 00:15 UTC: if none has arrived by then, the laptop or its "
                     "scheduled tasks are down."]
    task_health.append_gap(SilentGap(datetime(2026, 10, 18, tzinfo=timezone.utc),
                                     datetime(2026, 10, 18, 6, tzinfo=timezone.utc), 1, 35, 1), "ledger", None, gaps)
    task_health.append_gap(SilentGap(datetime(2026, 10, 22, 18, 1, tzinfo=timezone.utc),
                                     datetime(2026, 10, 25, 3, 18, tzinfo=timezone.utc), 14, 342, 3), "status", None, gaps)
    task_health.append_gap(SilentGap(datetime(2026, 10, 26, 13, tzinfo=timezone.utc),
                                     datetime(2026, 10, 26, 16, tzinfo=timezone.utc), 0, 17, 0), "ledger", None, gaps)
    headline, power, due = status.scheduler_section(gaps, now, BATTERY)
    assert headline.startswith("WARNING: Scheduler silent periods in the last 7 days: 2, 60.3 hours in all")
    assert "longest Thu 22 Oct 18:01 to Sun 25 Oct 03:18 UTC; missed 14 four-hourly run(s)" in headline
    assert power.startswith("WARNING: on battery (29%): the laptop sleeps") and due.startswith("Next status report due by")
    assert status.scheduler_section(gaps, now + timedelta(days=2), MAINS)[1] == "Power: mains (100%)"
    assert status.next_report_due(datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)) == \
        datetime(2026, 10, 27, 4, 15, tzinfo=timezone.utc)


# ---------------- the schedules and the backup ----------------
def test_the_schedule_constants_match_the_systemd_timers() -> None:
    timers = {name: re.search(r"OnCalendar=(.+)", (ROOT / "deploy" / "systemd" / f"prop-alpha-{name}.timer")
                              .read_text(encoding="utf-8")).group(1).strip()  # type: ignore[union-attr]
              for name in ("status", "ledger", "backup")}
    assert timers == {"status": "*-*-* 00/4:00:00 UTC", "ledger": "*-*-* *:00/10:00 UTC",
                      "backup": "*-*-* 02:30:00 UTC"}
    assert task_health.STATUS_EVERY == timedelta(hours=4) and task_health.POLL_EVERY == timedelta(minutes=10)
    assert (task_health.BACKUP_AT.hour, task_health.BACKUP_AT.minute) == (2, 30)


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_silence", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_gap_log_is_in_the_daily_backup() -> None:
    assert task_health.DEFAULT_GAP_FILE.name in _script("backup").STATE_FILES


def test_the_power_source_is_read_on_windows_only() -> None:
    power = task_health.read_power_status()
    assert isinstance(power, PowerStatus) if sys.platform == "win32" else power is None
