"""Failure alerts for the scheduled tasks (maintainer task 4, 2026-10-05). Sends text only; it never places a bet.

Each scheduled script (scripts/telegram_status.cmd, scripts/ledger_poll.cmd, scripts/backup.cmd) ends by reporting its
run here, with the steps that failed (none when it succeeded); step names are single words separated by spaces:

    python -m prop_alpha.alerts.task_health --task status --failed "results scan"
    python -m prop_alpha.alerts.task_health --task ledger --failed ""

Until now a failed step only wrote "STEP FAILED" into a log nobody reads. This sends a Telegram message when a task has
failed ALERT_AFTER_FAILURES runs in a row (the status task retries a failed run after 5 minutes, so one transient
failure is not paged), again at most every REALERT_AFTER while it keeps failing or when the failing steps change, and
once when it recovers. Each run also checks the OTHER tasks: one that has not reported for longer than its OVERDUE
limit is flagged ("has not run since ..."), so a task that stops running altogether is noticed while another runs.

Silence of the whole machine (reliability review, 2026-10-26: from 16 to 26 Oct the 4-hourly run completed on 1 to 5 of
its 6 daily slots, because the laptop was on battery, asleep, off, offline or logged out). Nothing on the machine can
alert while every task is stopped, so two checks run on the first report after it:
- SCHEDULER WAS SILENT: when no task has reported for longer than SILENCE_LIMIT (the poll's own overdue limit), the gap
  is alerted once, with the scheduled runs it swallowed (4-hourly scan windows, polls, backups), and appended to
  .prop_alpha_state/scheduler_gaps.jsonl for the status report. The per-task "not running" flags it explains are not
  sent separately.
- ON BATTERY: every run reads the power source (Windows only); on battery it alerts once per battery spell (again after
  REALERT_AFTER), because on battery this laptop sleeps after 3 minutes idle with wake timers off (powercfg, checked
  2026-10-26), which stops every task.
While the machine is silent, the only signal is the absence of the 4-hourly status report, which says when the next
one is due (alerts/status.py). A monitor outside this machine would be needed to page during the silence.

State: .prop_alpha_state/task_health.json (per task, plus "machine" for the battery alert), written only after the
Telegram sends succeeded, so an alert that could not be sent (often: no network after sleep) is sent by the next run. A
failed send exits non-zero. A corrupt state file raises: guessing could silence an alert. Runs that report at the same
moment take turns under an OS lock (task_health.lock).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Final, Mapping, Sequence

from prop_alpha.alerts.ledger import ledger_lock
from prop_alpha.alerts.telegram import DEFAULT_STATE_FILE, TelegramNotifier, load_config

logger = logging.getLogger("prop_alpha.task_health")

DEFAULT_HEALTH_FILE: Final[Path] = DEFAULT_STATE_FILE.parent / "task_health.json"
DEFAULT_GAP_FILE: Final[Path] = DEFAULT_STATE_FILE.parent / "scheduler_gaps.jsonl"
MACHINE_KEY: Final[str] = "machine"
# Task key -> (scheduler name, log file, longest normal gap between two runs before it counts as overdue). The status
# task runs every 4 hours (and catches up after sleep); the ledger poll every 10 minutes; the backup once a day.
TASKS: Final[Mapping[str, tuple[str, str, timedelta]]] = {
    "status": ("prop_alpha\\Telegram status", ".prop_alpha_state\\status.log", timedelta(hours=5)),
    "ledger": ("prop_alpha\\Bet ledger poll", ".prop_alpha_state\\ledger.log", timedelta(minutes=40)),
    "backup": ("prop_alpha\\Daily backup", ".prop_alpha_state\\backup.log", timedelta(hours=26)),
}
ALERT_AFTER_FAILURES: Final[int] = 2
REALERT_AFTER: Final[timedelta] = timedelta(hours=6)
# No task reporting for longer than this means the whole machine was silent: the poll's own overdue limit (it runs every
# 10 minutes), so a slow poll or two is not reported.
SILENCE_LIMIT: Final[timedelta] = TASKS["ledger"][2]
# The schedules (Windows tasks and deploy/systemd timers, both in UTC; tests/test_scheduler_silence.py checks the timers):
# the status task at every 4-hour boundary from 00:00, the poll every 10 minutes, the backup daily at 02:30.
STATUS_EVERY: Final[timedelta] = timedelta(hours=4)
POLL_EVERY: Final[timedelta] = timedelta(minutes=10)
BACKUP_AT: Final[time] = time(2, 30)


@dataclass(frozen=True)
class TaskState:
    """What is known about one task. `alerted_at`: when its current failure streak was last paged (None: not paged).
    `overdue_alerted_at`: when another task last flagged it as overdue (None: not flagged since it last ran)."""

    last_run_at: datetime
    consecutive_failures: int = 0
    failed_steps: tuple[str, ...] = ()
    failing_since: datetime | None = None
    alerted_at: datetime | None = None
    overdue_alerted_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("last_run_at", "failing_since", "alerted_at", "overdue_alerted_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
                raise ValueError(f"{name} must be a timezone-aware UTC datetime")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must not be negative")
        if (self.consecutive_failures == 0) != (self.failing_since is None) or \
                (self.consecutive_failures == 0) != (not self.failed_steps):
            raise ValueError("a failure streak needs its start and its failed steps, and only a streak has them")


def _require_utc(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class PowerStatus:
    """The power source when a run reported: `on_mains` None when the system does not know (a desktop, or Windows
    reporting it as unknown); `battery_percent` None when there is no battery reading."""

    on_mains: bool | None
    battery_percent: int | None = None

    def __post_init__(self) -> None:
        if self.battery_percent is not None and not 0 <= self.battery_percent <= 100:
            raise ValueError("battery_percent must lie in [0, 100]")

    def describe(self) -> str:
        source = "unknown" if self.on_mains is None else "mains" if self.on_mains else "battery"
        return source + ("" if self.battery_percent is None else f" ({self.battery_percent}%)")


@dataclass(frozen=True)
class MachineState:
    """Machine-wide alert state: when the current battery spell was last alerted (None: not alerted, or on mains)."""

    battery_alerted_at: datetime | None = None

    def __post_init__(self) -> None:
        value = self.battery_alerted_at
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("battery_alerted_at must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class SilentGap:
    """A stretch with no report from any task: from the last report before it to the run that noticed it, and the
    scheduled runs inside it (none of which can be made up: a 4-hourly scan prices only its own window)."""

    start: datetime
    end: datetime
    missed_status_runs: int
    missed_polls: int
    missed_backups: int

    def __post_init__(self) -> None:
        for value in (self.start, self.end):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("a gap's times must be timezone-aware UTC datetimes")
        if self.end <= self.start or min(self.missed_status_runs, self.missed_polls, self.missed_backups) < 0:
            raise ValueError("a gap ends after it starts and misses no negative number of runs")

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def _occurrences(start: datetime, end: datetime, first: datetime, every: timedelta) -> int:
    """How many of first, first + every, ... lie strictly between start and end."""
    if end <= start:
        return 0
    k_low = math.floor((start - first) / every) + 1
    k_high = math.ceil((end - first) / every) - 1
    return max(0, k_high - k_low + 1)


def silent_gap(states: Mapping[str, TaskState], now: datetime) -> SilentGap | None:
    """Pure: the gap since the last report of ANY task, when it is longer than SILENCE_LIMIT; None otherwise (or before
    any task has reported)."""
    _require_utc(now)
    if not states:
        return None
    start = max(state.last_run_at for state in states.values())
    if now - start <= SILENCE_LIMIT:
        return None
    midnight = datetime.combine(start.date(), time(0), tzinfo=timezone.utc)
    return SilentGap(
        start, now,
        missed_status_runs=_occurrences(start, now, midnight, STATUS_EVERY),
        missed_polls=max(0, math.floor((now - start) / POLL_EVERY) - 1),
        missed_backups=_occurrences(start, now, datetime.combine(start.date(), BACKUP_AT, tzinfo=timezone.utc),
                                    timedelta(days=1)),
    )


def gap_message(gap: SilentGap, power: PowerStatus | None) -> str:
    """Pure: the alert for a silent gap."""
    return (f"SCHEDULER WAS SILENT: no prop_alpha task ran from {gap.start:%a %d %b %H:%M} to {gap.end:%a %d %b %H:%M} "
            f"UTC ({gap.hours:.1f} hours): the laptop was off, asleep, offline or logged out. Missed: "
            f"{gap.missed_status_runs} four-hourly run(s) (their League One/Two scan windows, lockbox predictions and "
            f"status reports), about {gap.missed_polls} ten-minute poll(s) (closing prices, T-60 team sheets, bet "
            f"replies) and {gap.missed_backups} daily backup(s). Matches in those windows can never get a lockbox "
            "prediction." + ("" if power is None else f" Power now: {power.describe()}."))


def evaluate_power(machine: MachineState, power: PowerStatus | None,
                   now: datetime) -> tuple[MachineState, list[str]]:
    """Pure: the battery alert, once per battery spell (again after REALERT_AFTER); mains clears it."""
    _require_utc(now)
    if power is None or power.on_mains is None:
        return machine, []
    if power.on_mains:
        return MachineState(), []
    alerted = machine.battery_alerted_at
    if alerted is not None and now - alerted < REALERT_AFTER:
        return machine, []
    return MachineState(now), [
        f"ON BATTERY{'' if power.battery_percent is None else f' ({power.battery_percent}%)'}: the scheduled scans, "
        "the 10-minute poll and the lockbox capture stop when the laptop sleeps. On battery this laptop sleeps after 3 "
        "minutes idle and its wake timers are off (power plan checked 2026-10-26). Plug it in and keep it awake."]


def evaluate(states: Mapping[str, TaskState], task: str, failed: Sequence[str],
             now: datetime, explained: Collection[str] = ()) -> tuple[dict[str, TaskState], list[str]]:
    """Pure: the new states after `task` reported a run whose `failed` steps failed (empty: success), and the alert
    messages to send. A task in `explained` (silent because the whole machine was: see silent_gap) is flagged as
    overdue without a message of its own."""
    _require_utc(now)
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; known: {', '.join(sorted(TASKS))}")
    steps = tuple(sorted({step.strip() for step in failed if step.strip()}))
    name, log, _ = TASKS[task]
    previous = states.get(task)
    updated = dict(states)
    messages: list[str] = []
    if steps:
        streak = (previous.consecutive_failures if previous is not None else 0) + 1
        since = previous.failing_since if previous is not None and previous.failing_since is not None else now
        alerted = previous.alerted_at if previous is not None else None
        changed = previous is not None and previous.failed_steps != steps and alerted is not None
        due = alerted is None or now - alerted >= REALERT_AFTER or changed
        if streak >= ALERT_AFTER_FAILURES and due:
            messages.append(f"TASK FAILING: {name} failed {streak} runs in a row since {since:%a %d %b %H:%M} UTC. "
                            f"Failed step(s): {', '.join(steps)}. See {log}. Alerts and bet recording may be "
                            "affected until it recovers.")
            alerted = now
        updated[task] = TaskState(now, streak, steps, since, alerted)
    else:
        if previous is not None and previous.alerted_at is not None and previous.failing_since is not None:
            messages.append(f"TASK RECOVERED: {name} succeeded again after {previous.consecutive_failures} failed "
                            f"run(s) since {previous.failing_since:%a %d %b %H:%M} UTC.")
        updated[task] = TaskState(now)
    for other, (other_name, _, overdue) in TASKS.items():
        state = updated.get(other)
        if other == task or state is None or now - state.last_run_at <= overdue:
            continue
        flagged = state.overdue_alerted_at
        if flagged is None or now - flagged >= REALERT_AFTER:
            if other not in explained:
                messages.append(f"TASK NOT RUNNING: {other_name} has not run since {state.last_run_at:%a %d %b %H:%M} "
                                f"UTC (expected at least every {_duration(overdue)}). Check Task Scheduler and "
                                "that the user is logged in.")
            updated[other] = replace(state, overdue_alerted_at=now)
    return updated, messages


def _duration(value: timedelta) -> str:
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60} hours" if minutes % 60 == 0 else f"{minutes} minutes"


def _parse_time(raw: Any, where: str) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{where}: not a timestamp")
    return datetime.fromisoformat(raw)


def load_states(path: Path = DEFAULT_HEALTH_FILE) -> dict[str, TaskState]:
    if not path.is_file():
        return {}
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} is not an object")
    states: dict[str, TaskState] = {}
    for task, entry in raw.items():
        if task == MACHINE_KEY:
            continue
        if task not in TASKS or not isinstance(entry, dict) or not isinstance(entry.get("failed_steps"), list):
            raise ValueError(f"{path.name}: malformed entry for {task!r}")
        last_run = _parse_time(entry.get("last_run_at"), f"{path.name} {task}.last_run_at")
        if last_run is None or not isinstance(entry.get("consecutive_failures"), int):
            raise ValueError(f"{path.name}: {task} needs last_run_at and consecutive_failures")
        states[task] = TaskState(
            last_run, int(entry["consecutive_failures"]), tuple(str(step) for step in entry["failed_steps"]),
            *(_parse_time(entry.get(key), f"{path.name} {task}.{key}")
              for key in ("failing_since", "alerted_at", "overdue_alerted_at")),
        )
    return states


def load_machine(path: Path = DEFAULT_HEALTH_FILE) -> MachineState:
    """The machine-wide alert state ("machine" in the health file; a file written before 2026-10-26 has none)."""
    if not path.is_file():
        return MachineState()
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    entry = raw.get(MACHINE_KEY) if isinstance(raw, dict) else None
    if entry is None:
        return MachineState()
    if not isinstance(entry, dict):
        raise ValueError(f"{path.name}: malformed {MACHINE_KEY!r} entry")
    return MachineState(_parse_time(entry.get("battery_alerted_at"), f"{path.name} machine.battery_alerted_at"))


def save_states(states: Mapping[str, TaskState], path: Path = DEFAULT_HEALTH_FILE,
                machine: MachineState | None = None) -> None:
    """Write the task states and, when given, the machine state (otherwise the stored one is kept)."""
    def encode(state: TaskState) -> dict[str, Any]:
        record = asdict(state)
        return {key: value.isoformat() if isinstance(value, datetime) else list(value) if isinstance(value, tuple)
                else value for key, value in record.items()}

    kept = load_machine(path) if machine is None else machine
    alerted = kept.battery_alerted_at
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**{task: encode(state) for task, state in sorted(states.items())},
                                     MACHINE_KEY: {"battery_alerted_at": None if alerted is None else alerted.isoformat()}},
                                    indent=1), encoding="utf-8")
    temporary.replace(path)


def append_gap(gap: SilentGap, task: str, power: PowerStatus | None, path: Path = DEFAULT_GAP_FILE) -> None:
    """Append one silent gap to the gap log (the status report reads it; a gap noticed again, because the save after the
    send failed, is appended again with a later end, and the reader keeps the longest record per start)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"start": gap.start.isoformat(), "end": gap.end.isoformat(), "hours": round(gap.hours, 3),
              "missed_status_runs": gap.missed_status_runs, "missed_polls": gap.missed_polls,
              "missed_backups": gap.missed_backups, "noticed_by": task,
              "power": None if power is None else power.describe()}
    # A write cut short by a power loss leaves a partial last line (no newline). It is not a record: drop it before
    # appending, so it never becomes a complete, unreadable line.
    if path.is_file():
        content = path.read_bytes()
        if content and not content.endswith(b"\n"):
            path.write_bytes(content[:content.rfind(b"\n") + 1])
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record) + "\n")


def load_gaps(path: Path = DEFAULT_GAP_FILE) -> tuple[list[SilentGap], int]:
    """The recorded gaps, the longest record per start, oldest first, and how many unfinished last lines (a write cut
    short) were left out. A complete line that is not a gap record raises."""
    if not path.is_file():
        return [], 0
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    unfinished = 0 if text.endswith("\n") or not text else 1
    longest: dict[datetime, SilentGap] = {}
    for line in lines[:-1]:
        if not line.strip():
            continue
        raw: Any = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name}: not a gap record")
        gap = SilentGap(datetime.fromisoformat(raw["start"]), datetime.fromisoformat(raw["end"]),
                        int(raw["missed_status_runs"]), int(raw["missed_polls"]), int(raw["missed_backups"]))
        if gap.start not in longest or gap.end > longest[gap.start].end:
            longest[gap.start] = gap
    return [longest[start] for start in sorted(longest)], unfinished


def read_power_status() -> PowerStatus | None:
    """The power source now (Windows GetSystemPowerStatus); None on other systems. A failed call raises OSError."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _SystemPowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    status = _SystemPowerStatus()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(), "GetSystemPowerStatus failed")
    line = {0: False, 1: True}.get(int(status.ACLineStatus))
    percent = int(status.BatteryLifePercent)
    return PowerStatus(line, percent if percent <= 100 else None)


def due_alerts(states: Mapping[str, TaskState], machine: MachineState, task: str, failed: Sequence[str],
               now: datetime, power: PowerStatus | None = None,
               ) -> tuple[dict[str, TaskState], MachineState, SilentGap | None, list[str]]:
    """Pure: every alert a run owes (a silent gap first, then the task checks, then the battery), with the new states."""
    gap = silent_gap(states, now)
    explained = {name for name, state in states.items() if gap is not None and state.last_run_at <= gap.start}
    updated, messages = evaluate(states, task, failed, now, explained)
    new_machine, power_messages = evaluate_power(machine, power, now)
    return updated, new_machine, gap, ([gap_message(gap, power)] if gap is not None else []) + messages + power_messages


def report_run(notifier: TelegramNotifier | None, task: str, failed: Sequence[str], now: datetime,
               path: Path = DEFAULT_HEALTH_FILE, *, power: PowerStatus | None = None,
               gap_log: Path | None = None) -> list[str]:
    """Evaluate the run, send its alerts, append a silent gap to `gap_log` (when given), then save the state (only after
    every send succeeded), all under an OS lock beside the state file (task_health.lock), so two runs reporting at once
    do not both alert the same gap. Returns the alerts."""
    with ledger_lock(path.with_suffix(".lock"), wait=True):
        updated, machine, gap, messages = due_alerts(load_states(path), load_machine(path), task, failed, now, power)
        for message in messages:
            if notifier is None:
                raise ValueError("an alert is due but no Telegram notifier was given")
            notifier.send_message(message)
            logger.warning("task health alert sent: %s", message)
        if gap is not None and gap_log is not None:
            append_gap(gap, task, power, gap_log)
        save_states(updated, path, machine)
    return messages


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Report a scheduled task's run; alert on repeated failure.")
    parser.add_argument("--task", choices=sorted(TASKS), required=True)
    parser.add_argument("--failed", default="", help="space-separated names of the steps that failed (empty: success)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    failed = args.failed.split()
    power = read_power_status()
    # The token is read only when an alert is due, so a healthy run never depends on the Telegram configuration.
    *_, due = due_alerts(load_states(), load_machine(), args.task, failed, now, power)
    notifier = TelegramNotifier(load_config()) if due else None
    sent = report_run(notifier, args.task, failed, now, power=power, gap_log=DEFAULT_GAP_FILE)
    print(f"{args.task}: {'failed: ' + ' '.join(failed) if failed else 'ok'}; {len(sent)} alert(s) sent; "
          f"power {power.describe() if power is not None else 'not read on this system'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
