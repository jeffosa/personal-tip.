"""Failure alerts for the scheduled tasks (maintainer task 4, 2026-10-05). Sends text only; it never places a bet.

Each scheduled script (scripts/telegram_status.cmd, scripts/ledger_poll.cmd) ends by reporting its run here, with the
steps that failed (none when it succeeded):

    python -m prop_alpha.alerts.task_health --task status --failed "results scan"
    python -m prop_alpha.alerts.task_health --task ledger --failed ""

Until now a failed step only wrote "STEP FAILED" into a log nobody reads. This sends a Telegram message when a task has
failed ALERT_AFTER_FAILURES runs in a row (the status task retries a failed run after 5 minutes, so one transient
failure is not paged), again at most every REALERT_AFTER while it keeps failing or when the failing steps change, and
once when it recovers. Each run also checks the OTHER task: one that has not reported for longer than its OVERDUE
limit is flagged ("has not run since ..."), so a task that stops running altogether is noticed while the other runs.
Neither check can notice that both tasks stopped (the machine is off, asleep or logged out); that needs a monitor
outside this machine.

State: .prop_alpha_state/task_health.json, written only after the Telegram send succeeded, so an alert that could not be
sent (often: no network after sleep) is sent by the next run. A failed send exits non-zero. A corrupt state file
raises: guessing could silence an alert.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from prop_alpha.alerts.telegram import DEFAULT_STATE_FILE, TelegramNotifier, load_config

logger = logging.getLogger("prop_alpha.task_health")

DEFAULT_HEALTH_FILE: Final[Path] = DEFAULT_STATE_FILE.parent / "task_health.json"
# Task key -> (scheduler name, log file, longest normal gap between two runs before it counts as overdue). The status
# task runs every 4 hours (and catches up after sleep); the ledger poll every 10 minutes.
TASKS: Final[Mapping[str, tuple[str, str, timedelta]]] = {
    "status": ("prop_alpha\\Telegram status", ".prop_alpha_state\\status.log", timedelta(hours=5)),
    "ledger": ("prop_alpha\\Bet ledger poll", ".prop_alpha_state\\ledger.log", timedelta(minutes=40)),
}
ALERT_AFTER_FAILURES: Final[int] = 2
REALERT_AFTER: Final[timedelta] = timedelta(hours=6)


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


def evaluate(states: Mapping[str, TaskState], task: str, failed: Sequence[str],
             now: datetime) -> tuple[dict[str, TaskState], list[str]]:
    """Pure: the new states after `task` reported a run whose `failed` steps failed (empty: success), and the alert
    messages to send."""
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


def save_states(states: Mapping[str, TaskState], path: Path = DEFAULT_HEALTH_FILE) -> None:
    def encode(state: TaskState) -> dict[str, Any]:
        record = asdict(state)
        return {key: value.isoformat() if isinstance(value, datetime) else list(value) if isinstance(value, tuple)
                else value for key, value in record.items()}

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({task: encode(state) for task, state in sorted(states.items())}, indent=1),
                         encoding="utf-8")
    temporary.replace(path)


def report_run(notifier: TelegramNotifier | None, task: str, failed: Sequence[str], now: datetime,
               path: Path = DEFAULT_HEALTH_FILE) -> list[str]:
    """Evaluate the run, send its alerts, then save the state (only after every send succeeded). Returns the alerts."""
    updated, messages = evaluate(load_states(path), task, failed, now)
    for message in messages:
        if notifier is None:
            raise ValueError("an alert is due but no Telegram notifier was given")
        notifier.send_message(message)
        logger.warning("task health alert sent: %s", message)
    save_states(updated, path)
    return messages


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Report a scheduled task's run; alert on repeated failure.")
    parser.add_argument("--task", choices=sorted(TASKS), required=True)
    parser.add_argument("--failed", default="", help="space-separated names of the steps that failed (empty: success)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    failed = args.failed.split()
    # The token is read only when an alert is due, so a healthy run never depends on the Telegram configuration.
    _, due = evaluate(load_states(), args.task, failed, now)
    notifier = TelegramNotifier(load_config()) if due else None
    sent = report_run(notifier, args.task, failed, now)
    print(f"{args.task}: {'failed: ' + ' '.join(failed) if failed else 'ok'}; {len(sent)} alert(s) sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
