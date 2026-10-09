"""One-off matchday check, sent to Telegram: did the evidence collection run? (2026-10-10, first lockbox matchday)

    .venv\\Scripts\\python.exe scripts\\matchday_report.py [--no-send] [--keep]

Read-only. It looks at the state files the scheduled tasks write, with no vendor or database request, and reports:
the last scan (from status.log), the prediction log (lines, fixtures, how many count towards the lockbox, the pricing
digest), the T-60 team sheets, the saved scan fixtures and closing quotes, the last bet-reply poll and the task-health
record. It sends one message through the project's Telegram notifier; a failed send exits 1.

The Windows task "prop_alpha\\Matchday report" runs it at 12:15 and 13:35 UTC on 2026-10-10. From 13:30 UTC on 2026-10-10, a
successful run also deletes that task (`--keep` leaves it). `--no-send` prints the message instead.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts import lineup_capture  # noqa: E402
from prop_alpha.alerts.closing import load_closing_quotes, load_scan_fixtures  # noqa: E402
from prop_alpha.alerts.predictions import PredictionLog, lockbox_progress  # noqa: E402
from prop_alpha.alerts.telegram import PAPER_LABEL, TelegramNotifier, load_config  # noqa: E402
from prop_alpha.modeling.model_freeze import FROZEN_DIGEST  # noqa: E402

STATE: Final[Path] = ROOT / ".prop_alpha_state"
TASK_NAME: Final[str] = "prop_alpha\\Matchday report"
DELETE_FROM: Final[datetime] = datetime(2026, 10, 10, 13, 30, tzinfo=timezone.utc)


def last_scan(now: datetime) -> list[str]:
    """The last scan block in status.log: its header, the matches line and any odds line."""
    path = STATE / "status.log"
    if not path.is_file():
        return ["scan: status.log not found"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("prop_alpha scan ")]
    if not starts:
        return ["scan: no scan in status.log"]
    block = lines[starts[-1]:starts[-1] + 14]
    keep = [line.strip() for line in block if line.startswith(("prop_alpha scan", "Matches kicking off", "Odds", "Players priced",
                                                              "Odds unavailable", "Bets alerted", "Fair-price"))]
    return ["scan: " + keep[0].removeprefix("prop_alpha scan ")] + [f"  {line[:150]}" for line in keep[1:5]]


def prediction_lines() -> list[str]:
    records = PredictionLog().load()
    if not records:
        return ["predictions: NONE logged yet (a real --send scan with fixtures writes them)"]
    progress = lockbox_progress(records)
    digests = Counter(str(r["digest"])[:8] for r in records)
    latest = max(str(r["logged_at"]) for r in records)
    per_line = "; ".join(f"{market}: " + ", ".join(f"over {line:g} {n}" for line, n in sorted(lines.items()))
                         for market, lines in sorted(progress["per_line"].items()))
    frozen = "frozen digest" if set(digests) == {FROZEN_DIGEST[:8]} else f"DIGESTS {dict(digests)} (frozen {FROZEN_DIGEST[:8]})"
    return [f"predictions: {progress['logged']} lines, {progress['fixtures']} fixtures, {progress['counted']} count for the lockbox",
            f"  {per_line}", f"  {frozen}; latest logged {latest[:16]}Z; basis {sorted({str(r['lineup_basis']) for r in records})}"]


def lineup_lines() -> list[str]:
    records = lineup_capture.load_records(STATE)
    if not records:
        return ["T-60 team sheets: none captured yet (due 30-65 min before each kickoff)"]
    status = Counter(str(r["status"]) for r in records)
    minutes = sorted(round(float(r["minutes_to_kickoff"])) for r in records)
    return [f"T-60 team sheets: {len(records)} captured {dict(status)}; minutes before kickoff {minutes}"]


def other_lines(now: datetime) -> list[str]:
    out: list[str] = []
    fixtures = load_scan_fixtures(STATE)
    out.append(f"scan fixtures saved for closing capture: {len(fixtures)}")
    quotes = load_closing_quotes(STATE)
    out.append(f"closing quotes captured: {len(quotes)}" + ("" if quotes else " (none yet; no bookmaker has quoted these markets for League One/Two)"))
    poll = STATE / "ledger_last_poll.json"
    if poll.is_file():
        at = datetime.fromisoformat(str(json.loads(poll.read_text(encoding="utf-8"))["at"]))
        out.append(f"last bet-reply poll: {(now - at).total_seconds() / 60:.0f} min ago")
    health = STATE / "task_health.json"
    if health.is_file():
        data: dict[str, Any] = json.loads(health.read_text(encoding="utf-8"))
        parts = []
        for name, record in sorted(data.items()):
            age = (now - datetime.fromisoformat(str(record["last_run_at"]))).total_seconds() / 3600
            parts.append(f"{name} {age:.1f}h ago, {record['consecutive_failures']} failing")
        out.append("tasks: " + "; ".join(parts))
    return out


def build(now: datetime) -> str:
    lines = [f"MATCHDAY CHECK {now:%a %d %b %H:%M} UTC", *last_scan(now), *prediction_lines(), *lineup_lines(), *other_lines(now), "", PAPER_LABEL]
    return "\n".join(lines)


def main() -> int:
    flags = set(sys.argv[1:])
    unknown = flags - {"--no-send", "--keep"}
    if unknown:
        print(__doc__)
        return 2
    now = datetime.now(timezone.utc)
    text = build(now)
    if "--no-send" in flags:
        print(text)
        return 0
    notifier = TelegramNotifier(load_config(ROOT / ".env"))
    message_id = notifier.send_message(text)
    print(f"{now.isoformat()} sent (message_id {message_id})")
    if "--keep" not in flags and now >= DELETE_FROM:
        done = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True, check=False)
        print(f"task {TASK_NAME} deleted" if done.returncode == 0 else f"could not delete {TASK_NAME}: {done.stdout.strip()} {done.stderr.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
