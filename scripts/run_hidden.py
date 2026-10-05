"""Run a scheduled .cmd script with no visible console window (maintainer decision 1, 2026-10-05).

Task Scheduler starts this with the console-less interpreter, so nothing appears on screen:

    .venv\\Scripts\\pythonw.exe scripts\\run_hidden.py scripts\\ledger_poll.cmd

Why: the tasks ran `cmd.exe /c <script>.cmd` as interactive tasks, so every run opened a console window that took
the keyboard focus. A Ctrl+C typed into it (copying text in another window, say), or closing it, killed the run before
it logged a line: exit code 0xC000013A, and `^C` marks in ledger.log. From about 10:51 to 16:19 BST on 2026-10-05
every ledger poll and 4-hourly run died this way, so no bet reply was recorded and no scan was sent, silently.

The script runs in a console created with CREATE_NO_WINDOW: it exists (python.exe and the redirections in the .cmd
need one) but is never shown, so it cannot take focus or be closed. The script's exit code is this process's exit
code, so the task's Last Run Result still reports a failed step. Only a .cmd file inside this repository is run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Final, Sequence

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
# Win32 process-creation flag (winbase.h); subprocess.CREATE_NO_WINDOW exists only on Windows builds of Python.
CREATE_NO_WINDOW: Final[int] = 0x08000000

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def resolve_script(raw: str, root: Path = REPOSITORY_ROOT) -> Path:
    """The .cmd script to run: an existing .cmd file inside `root`. Anything else raises ValueError."""
    script = Path(raw)
    if not script.is_absolute():
        script = root / script
    script = script.resolve()
    if script.suffix.lower() != ".cmd" or not script.is_file():
        raise ValueError(f"{raw!r} is not an existing .cmd script")
    if not script.is_relative_to(root.resolve()):
        raise ValueError(f"{raw!r} is outside the repository {root}")
    return script


def main(argv: Sequence[str] | None = None, run: Runner = subprocess.run, root: Path = REPOSITORY_ROOT) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise ValueError("usage: pythonw.exe scripts\\run_hidden.py <script.cmd>")
    script = resolve_script(args[0], root)
    completed = run(["cmd.exe", "/d", "/c", str(script)], cwd=str(root), creationflags=CREATE_NO_WINDOW,
                    check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())
