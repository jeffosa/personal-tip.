"""T-60 team sheets: the starting elevens as published about an hour before kickoff (PRD Gate 1 criterion 7, Gate 2
protocol "lineups are fixed at team-sheet time").

    python -m prop_alpha.alerts.lineup_capture     # run every 10 minutes by scripts\\ledger_poll.cmd

The opportunity scan prices up to four hours before kickoff, from estimated line-ups. This job records what the clubs
then announced, so a later check can say how good the estimate was and whether a price rested on a player who was not in
the team. It does not change any price.

Each run, between LISTING_START_HOUR and LISTING_END_HOUR UTC, lists the League One/Two fixtures kicking off in the
next CAPTURE_EARLIEST minutes with one Sportmonks request (the scan's own `fetch_upcoming`; the saved scan fixtures are
not used, because the scan runs only once the match is within four hours and would miss a 12:30 kickoff). For each
fixture between CAPTURE_LATEST and CAPTURE_EARLIEST minutes from kickoff that has no outcome yet, it fetches the sheet
(`VendorClient.get_lineups`, adapter `sportmonks.lineup_payload`). A sheet with exactly 11 starters per side is
"confirmed". An incomplete one is retried on the next poll; on the last poll before CAPTURE_LATEST it is recorded as
"incomplete", so a miss is a record, not silence. Records go to `.prop_alpha_state/lineups_t60.jsonl` (append-only,
gitignored, in the daily backup); handled fixtures to `lineups_t60_handled.json`.

A vendor failure or an unidentified starter (the adapter refuses that sheet) raises, so the scheduled step fails and
`task_health` reports it; nothing is recorded for that attempt.

I/O: one Sportmonks listing per run in hours, and one request per fixture due; the two state files.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

import prop_alpha.scanner.slate

logger = logging.getLogger("prop_alpha.lineup_capture")

CAPTURE_EARLIEST: Final[timedelta] = timedelta(minutes=65)
CAPTURE_LATEST: Final[timedelta] = timedelta(minutes=30)
POLL_INTERVAL: Final[timedelta] = timedelta(minutes=10)
LISTING_START_HOUR: Final[int] = 8
LISTING_END_HOUR: Final[int] = 21
STARTERS_PER_SIDE: Final[int] = 11
LINEUPS_NAME: Final[str] = "lineups_t60.jsonl"
HANDLED_NAME: Final[str] = "lineups_t60_handled.json"
STATE_DIR: Final[Path] = Path(".prop_alpha_state")
STATUS_CONFIRMED: Final[str] = "confirmed"
STATUS_INCOMPLETE: Final[str] = "incomplete"


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def due_fixtures(fixtures: Sequence[prop_alpha.scanner.slate.UpcomingFixture], handled: frozenset[int], now: datetime) -> list[prop_alpha.scanner.slate.UpcomingFixture]:
    """Pure: fixtures between CAPTURE_LATEST and CAPTURE_EARLIEST minutes from kickoff with no outcome yet, earliest
    kickoff first."""
    _require_utc(now, "now")
    due = [f for f in fixtures if CAPTURE_LATEST < f.kickoff_at - now <= CAPTURE_EARLIEST and f.fixture_id not in handled]
    return sorted(due, key=lambda f: (f.kickoff_at, f.fixture_id))


def sheet_status(payload: Mapping[str, Any]) -> str:
    """Pure: "confirmed" when both sides list exactly STARTERS_PER_SIDE distinct starters and no player starts for
    both, otherwise "incomplete"."""
    sheets = payload.get("lineups")
    if not isinstance(sheets, Mapping) or len(sheets) != 2:
        raise ValueError("lineup payload does not hold exactly two sides")
    everyone: list[str] = []
    for team, sheet in sheets.items():
        starters = sheet.get("starters") if isinstance(sheet, Mapping) else None
        if not isinstance(starters, list):
            raise ValueError(f"lineup payload has no starters list for team {team}")
        if len(starters) != STARTERS_PER_SIDE or len(set(starters)) != STARTERS_PER_SIDE:
            return STATUS_INCOMPLETE
        everyone += [str(uid) for uid in starters]
    return STATUS_CONFIRMED if len(set(everyone)) == len(everyone) else STATUS_INCOMPLETE


def is_last_chance(fixture: prop_alpha.scanner.slate.UpcomingFixture, now: datetime) -> bool:
    """Pure: the next poll would be at or past CAPTURE_LATEST."""
    return fixture.kickoff_at - (now + POLL_INTERVAL) <= CAPTURE_LATEST


def record_for(fixture: prop_alpha.scanner.slate.UpcomingFixture, payload: Mapping[str, Any], status: str, captured_at: datetime) -> dict[str, Any]:
    """Pure: the stored line for one captured sheet."""
    _require_utc(captured_at, "captured_at")
    return {
        "fixture_id": fixture.fixture_id, "league": fixture.league, "kickoff_at": fixture.kickoff_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "minutes_to_kickoff": round((fixture.kickoff_at - captured_at).total_seconds() / 60.0, 1),
        "status": status, "home_team_uid": fixture.home_team_uid, "away_team_uid": fixture.away_team_uid,
        "starters": {str(team): list(sheet["starters"]) for team, sheet in payload["lineups"].items()},
        "bench": {str(team): list(sheet["bench"]) for team, sheet in payload["lineups"].items()},
    }


def load_records(state_dir: Path = STATE_DIR) -> list[dict[str, Any]]:
    """Every captured sheet. A damaged line raises."""
    path = state_dir / LINEUPS_NAME
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record: Any = json.loads(line)
        if not isinstance(record, dict) or any(k not in record for k in ("fixture_id", "status", "starters", "captured_at")):
            raise ValueError(f"{path.name} line {number} is not a captured team sheet")
        records.append(record)
    return records


def _handled(state_dir: Path) -> frozenset[int]:
    path = state_dir / HANDLED_NAME
    return frozenset(int(f) for f in json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else frozenset()


def listing_hours(now: datetime) -> bool:
    return LISTING_START_HOUR <= now.hour < LISTING_END_HOUR


def capture(now: datetime, client: Any, state_dir: Path = STATE_DIR) -> tuple[int, int, list[str]]:
    """(sheets recorded, of which confirmed, notes). Makes no request outside the listing hours."""
    from prop_alpha.alerts.scan import fetch_upcoming

    _require_utc(now, "now")
    if not listing_hours(now):
        return 0, 0, []
    fixtures = fetch_upcoming(client, now + CAPTURE_LATEST, now + CAPTURE_EARLIEST + timedelta(seconds=1))
    handled = _handled(state_dir)
    due = due_fixtures(fixtures, handled, now)
    recorded, confirmed = 0, 0
    notes: list[str] = []
    path = state_dir / LINEUPS_NAME
    for fixture in due:
        payload = client.get_lineups(str(fixture.fixture_id))
        status = sheet_status(payload)
        if status != STATUS_CONFIRMED and not is_last_chance(fixture, now):
            notes.append(f"{fixture.name}: team sheet not complete yet, will retry")
            continue
        state_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record_for(fixture, payload, status, now)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        handled = handled | {fixture.fixture_id}
        (state_dir / HANDLED_NAME).write_text(json.dumps(sorted(handled)), encoding="utf-8")
        recorded += 1
        confirmed += status == STATUS_CONFIRMED
        notes.append(f"{fixture.name}: team sheet {status} {round((fixture.kickoff_at - now).total_seconds() / 60)} "
                     "minutes before kickoff")
    return recorded, confirmed, notes


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from prop_alpha.ingestion.pipeline import build_vendor_client

    now = datetime.now(timezone.utc)
    if not listing_hours(now):
        return 0
    client = build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "8", "DATA_VENDOR_QUOTA_RESERVE": "100"})
    try:
        recorded, confirmed, notes = capture(now, client)
    finally:
        client.close()
    if recorded or notes:
        print(f"T-60 team sheets: {recorded} recorded, {confirmed} confirmed")
        for note in notes:
            print(f"  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
