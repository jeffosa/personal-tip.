"""Run one scheduled task on any operating system (maintainer decision 4, 2026-10-11: move the tasks to an always-on
Ubuntu host, after making the Windows-only scheduling portable).

    python scripts/run_task.py status     # every 4 hours: results, pilot results, exchange rate, status report, both scans
    python scripts/run_task.py ledger     # every 10 minutes: reply poll, closing prices, team sheets, settlement, pilot
    python scripts/run_task.py backup     # daily: verified backup, then the push to the private GitHub repository

The same steps, in the same order and with the same failed-step names, as scripts/telegram_status.cmd,
scripts/ledger_poll.cmd and scripts/backup.cmd, which the Windows Task Scheduler runs (tests/test_run_task.py fails if
the two drift apart). Every step runs with this interpreter from the repository root, even if an earlier one failed;
its output is appended to `.prop_alpha_state/<task>.log`. A step that exits non-zero adds its name to the failed steps
(the scans' "no real odds" and "stale league" exits have names of their own), and the run is reported last to
prop_alpha.alerts.task_health, which alerts by Telegram after 2 failing runs in a row. The exit code is 1 if any step
or the report failed, so systemd (deploy/systemd/) records the run as failed.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable, Final, Mapping, Sequence

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
STATE_DIR: Final[Path] = ROOT / ".prop_alpha_state"
# Exit codes with a failed-step name of their own (prop_alpha.alerts.scan.ODDS_UNAVAILABLE_EXIT and
# prop_alpha.alerts.ladder_scan.STALE_EXIT; tests/test_run_task.py checks they still agree).
ODDS_UNAVAILABLE_EXIT: Final[int] = 3
STALE_EXIT: Final[int] = 4


@dataclass(frozen=True)
class Step:
    """One command (the arguments after the interpreter) and the failed-step name it reports on a non-zero exit, or the
    name for a specific exit code in `named_exits`."""

    argv: tuple[str, ...]
    name: str
    named_exits: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a step needs a command")
        for name in (self.name, *(named for _, named in self.named_exits)):
            if not name or " " in name:
                raise ValueError(f"step name {name!r} must be one word (task_health splits the failed steps on spaces)")

    def failed_name(self, returncode: int) -> str:
        return dict(self.named_exits).get(returncode, self.name)


TASKS: Final[Mapping[str, tuple[Step, ...]]] = {
    "status": (
        Step(("-m", "prop_alpha.ingestion.results"), "results"),
        Step(("-m", "prop_alpha.ingestion.results", "--pilot"), "pilot-results"),
        Step(("-m", "prop_alpha.alerts.fx", "--refresh"), "fx-refresh"),
        Step(("-m", "prop_alpha.alerts.status", "--send"), "status-report"),
        Step(("-m", "prop_alpha.alerts.scan", "--odds", "oddspapi", "--send"), "scan",
             ((ODDS_UNAVAILABLE_EXIT, "scan-no-odds"),)),
        Step(("-m", "prop_alpha.alerts.ladder_scan", "--odds", "oddspapi", "--send"), "pilot-scan",
             ((ODDS_UNAVAILABLE_EXIT, "pilot-scan-no-odds"), (STALE_EXIT, "pilot-scan-stale"))),
    ),
    "ledger": (
        Step(("-m", "prop_alpha.alerts.ledger", "--poll"), "poll"),
        Step(("-m", "prop_alpha.alerts.closing"), "closing-prices"),
        Step(("-m", "prop_alpha.alerts.lineup_capture"), "lineups"),
        Step(("-m", "prop_alpha.alerts.ledger", "--settle", "--send"), "settle"),
        Step(("-m", "prop_alpha.alerts.pilot_closing"), "pilot-closing"),
        Step(("-m", "prop_alpha.alerts.pilot_ledger", "--settle", "--send"), "pilot-settle"),
    ),
    "backup": (
        Step(("scripts/backup.py", "--keep", "14"), "backup"),
        Step(("scripts/backup_push.py", "--keep", "14"), "github-push"),
    ),
}
Run = Callable[[Sequence[str]], int]


def execute(task: str, run: Run, note: Callable[[str], None]) -> int:
    """Run every step of `task` (each through `run`, which returns the exit code), then report the failed steps to
    task_health; returns 1 if any step or the report failed, else 0."""
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; known: {', '.join(sorted(TASKS))}")
    failed: list[str] = []
    for step in TASKS[task]:
        returncode = run(step.argv)
        if returncode != 0:
            failed.append(step.failed_name(returncode))
            note(f"STEP FAILED: {failed[-1]} (exit {returncode})")
    report = run(("-m", "prop_alpha.alerts.task_health", "--task", task, "--failed", " ".join(failed)))
    if report != 0:
        note("STEP FAILED: task health")
    return 1 if failed or report != 0 else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in TASKS:
        print(f"usage: run_task.py {{{','.join(sorted(TASKS))}}}", file=sys.stderr)
        return 2
    task = args[0]
    STATE_DIR.mkdir(exist_ok=True)
    with (STATE_DIR / f"{task}.log").open("ab") as log:
        def note(text: str) -> None:
            _write(log, f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {text}\n")

        def run(command: Sequence[str]) -> int:
            log.flush()
            return subprocess.run([sys.executable, *command], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT).returncode

        note(f"run_task {task}: start")
        code = execute(task, run, note)
        note(f"run_task {task}: exit {code}")
    return code


def _write(log: IO[bytes], text: str) -> None:
    log.write(text.encode("utf-8"))
    log.flush()


if __name__ == "__main__":
    sys.exit(main())
