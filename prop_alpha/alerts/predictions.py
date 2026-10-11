"""Prediction log: what the frozen model priced, written down before kickoff (PRD Gate 2 prospective evidence).

    python -m prop_alpha.alerts.predictions     # progress towards the lockbox, from the log only

The opportunity scan (alerts/scan.py) calls `PredictionLog.record` for every fixture it prices, before any alert is
sent. One line per fixture, player, market and line goes to `.prop_alpha_state/predictions.jsonl` (append-only,
gitignored, covered by the daily backup): the time it was made, the kickoff, the model's probability that the count goes
over the line (win, lose and void shares of the over side, void if the player does not appear), the lineup basis
("estimated": the scan prices before team sheets exist) and the digest of the pricing code that made it. A line counts
towards the lockbox only if the fixture kicked off on or after LOCKBOX_START and its digest equals FROZEN_DIGEST
(modeling/model_freeze.py), so a price made by code that has since changed is never counted by accident.

Every fixture is written once per pricing digest, whatever the scan window (audit 2026-10-11: a retried run, a late catch-up
run or overlapping windows used to log the same fixture again, double counting it towards the lockbox). A failed
write raises, so the scan stops before it alerts: no alert is sent on a price that was not recorded.

I/O: the log file, and the repository's pricing source files through `model_freeze.pricing_digest`. It records the
model's output only; it never changes a price.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from prop_alpha.modeling.engine import FactorProjection, VoidRule
from prop_alpha.modeling.model_freeze import FROZEN_DIGEST, GATE2_LINES, LOCKBOX_START, pricing_digest
from prop_alpha.scanner.slate import UpcomingFixture

PREDICTIONS_NAME: Final[str] = "predictions.jsonl"
LINEUP_BASIS_ESTIMATED: Final[str] = "estimated"
REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "logged_at", "window_start", "kickoff_at", "fixture_id", "player_id", "market", "line", "p_win", "p_lose", "p_void",
    "lineup_basis", "digest",
)
STATE_DIR: Final[Path] = Path(".prop_alpha_state")
ProjectionKey = tuple[int, int, str]


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def prediction_records(
    fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection], logged_at: datetime,
    window_start: datetime, digest: str,
) -> list[dict[str, Any]]:
    """Pure: one record per projection key and Gate 2 line. Raises if a logged time is not before kickoff, since a
    prediction made after the match began is not a prediction."""
    _require_utc(logged_at, "logged_at")
    _require_utc(window_start, "window_start")
    if logged_at >= fixture.kickoff_at:
        raise ValueError(f"fixture {fixture.fixture_id}: logged at {logged_at.isoformat()}, not before kickoff "
                         f"{fixture.kickoff_at.isoformat()}")
    records: list[dict[str, Any]] = []
    for key in sorted(projections):
        fixture_id, player_id, market = key
        if fixture_id != fixture.fixture_id:
            raise ValueError(f"projection {key} does not belong to fixture {fixture.fixture_id}")
        if market not in GATE2_LINES:
            continue
        for line in GATE2_LINES[market]:
            over = projections[key].side_probabilities(line, "over", VoidRule.VOID_IF_NO_APPEARANCE)
            records.append({
                "logged_at": logged_at.isoformat(), "window_start": window_start.isoformat(),
                "kickoff_at": fixture.kickoff_at.isoformat(), "fixture_id": fixture_id, "player_id": player_id,
                "market": market, "line": line, "p_win": over.p_win, "p_lose": over.p_lose, "p_void": over.p_void,
                "lineup_basis": LINEUP_BASIS_ESTIMATED, "digest": digest,
            })
    return records


def counts_for_lockbox(record: Mapping[str, Any]) -> bool:
    """Pure: made by the frozen code, for a match from LOCKBOX_START on, before that match began."""
    kickoff = datetime.fromisoformat(str(record["kickoff_at"]))
    logged = datetime.fromisoformat(str(record["logged_at"]))
    return record["digest"] == FROZEN_DIGEST and kickoff >= LOCKBOX_START and logged < kickoff


class PredictionLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else STATE_DIR / PREDICTIONS_NAME

    def load(self) -> list[dict[str, Any]]:
        """Every logged prediction. A damaged or incomplete line raises: a log that cannot be trusted is not scored."""
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record: Any = json.loads(line)
            if not isinstance(record, dict) or any(field not in record for field in REQUIRED_FIELDS):
                raise ValueError(f"{self.path.name} line {number} is not a prediction record")
            records.append(record)
        return records

    def record(
        self, fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection], logged_at: datetime,
        window_start: datetime, digest: str | None = None,
    ) -> int:
        """Append the fixture's predictions unless it is already logged under the same digest (the earliest prediction
        stands; a new digest after a re-freeze is a different model and is logged); returns lines written."""
        made_by = pricing_digest() if digest is None else digest
        if any(r["fixture_id"] == fixture.fixture_id and r["digest"] == made_by for r in self.load()):
            return 0
        records = prediction_records(fixture, projections, logged_at, window_start, made_by)
        if not records:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return len(records)


def lockbox_progress(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure: counts of logged predictions that count towards the lockbox, per market, against the 500 Gate 2 needs.
    A prediction is one (fixture, player, market, line); a starter priced on three lines counts three times, as the
    gate scores each line."""
    counted = [r for r in records if counts_for_lockbox(r)]
    per_line: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
    fixtures: set[int] = set()
    for r in counted:
        per_line[str(r["market"])][float(r["line"])] += 1
        fixtures.add(int(r["fixture_id"]))
    return {"logged": len(records), "counted": len(counted), "fixtures": len(fixtures),
            "not_counted": len(records) - len(counted),
            "per_line": {market: dict(lines) for market, lines in per_line.items()}}


def main(argv: Sequence[str] | None = None) -> int:
    log = PredictionLog()
    progress = lockbox_progress(log.load())
    print(f"prediction log {log.path}: {progress['logged']} lines, {progress['counted']} count towards the lockbox "
          f"(frozen code, kickoff from {LOCKBOX_START:%Y-%m-%d}), {progress['fixtures']} fixtures, "
          f"{progress['not_counted']} do not")
    for market, lines in sorted(progress["per_line"].items()):
        print(f"  {market}: " + ", ".join(f"over {line:g}: {n}" for line, n in sorted(lines.items())) + " (Gate 2 needs 500 per market)")
    print(f"pricing digest now {pricing_digest()[:8]}, frozen {FROZEN_DIGEST[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
