"""Prediction log: what the frozen model priced, written down before kickoff (PRD Gate 2 prospective evidence).

    python -m prop_alpha.alerts.predictions     # progress towards the lockbox, from the log only

The opportunity scan (alerts/scan.py) calls `PredictionLog.record` for every fixture it prices, before any alert is
sent. One line per fixture, player, market and line goes to `.prop_alpha_state/predictions.jsonl` (append-only,
gitignored, covered by the daily backup): the time it was made, the kickoff, the model's probability that the count goes
over the line (win, lose and void shares of the over side, void if the player does not appear), the lineup basis
("estimated": the scan prices before team sheets exist), the digest of the pricing code that made it and, since
2026-10-25, the data source it was priced from (`data_vendor`). Whether a line counts towards the lockbox is decided by
the prospective protocol (evaluation/lockbox.py, LOCKBOX_PROTOCOL.md): the frozen digest, the protocol's data source, a
kickoff from the lockbox start and a time logged before kickoff, among other rules.

Every fixture is written once per pricing digest and kickoff, whatever the scan window (audit 2026-10-11: a retried run, a
late catch-up run or overlapping windows used to log the same fixture again, double counting it towards the lockbox). A
match moved to a new kickoff (postponed or rescheduled) is logged again when it is next scanned (since 2026-10-25: until
then it never got a second prediction); the protocol scores only the line logged for the kickoff the match was played at. A failed
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

from prop_alpha.evaluation.lockbox import record_reason
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
    window_start: datetime, digest: str, data_vendor: str | None = None,
) -> list[dict[str, Any]]:
    """Pure: one record per projection key and Gate 2 line, with `data_vendor` when given. Raises if a logged time is not
    before kickoff, since a prediction made after the match began is not a prediction."""
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
                **({} if data_vendor is None else {"data_vendor": data_vendor}),
            })
    return records


def counts_for_lockbox(record: Mapping[str, Any]) -> bool:
    """Pure: the line meets the prospective protocol on its own (evaluation/lockbox.record_reason): the frozen digest and
    data source, a kickoff from the lockbox start, not an examined match, logged before kickoff, a protocol line. The
    scorer (scripts/gate2_prospective.py) also drops duplicates and lines for a kickoff that moved."""
    return record_reason(record) is None


class PredictionLog:
    def __init__(self, path: Path | None = None, data_vendor: str | None = None) -> None:
        """`data_vendor` (e.g. "sportmonks") is written on every line this log records."""
        self.path = path if path is not None else STATE_DIR / PREDICTIONS_NAME
        self.data_vendor = data_vendor

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
        """Append the fixture's predictions unless it is already logged under the same digest for the same kickoff (the
        earliest prediction stands; a new digest after a re-freeze is a different model, and a new kickoff a postponed or
        rescheduled match, and both are logged); returns lines written."""
        made_by = pricing_digest() if digest is None else digest
        kickoff = fixture.kickoff_at.isoformat()
        if any(r["fixture_id"] == fixture.fixture_id and r["digest"] == made_by and r["kickoff_at"] == kickoff
               for r in self.load()):
            return 0
        records = prediction_records(fixture, projections, logged_at, window_start, made_by, self.data_vendor)
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
