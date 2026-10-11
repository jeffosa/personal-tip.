"""Score the pilot scan's logged predictions against what happened (audit 2026-10-11, finding 3).

The pilot scan (alerts/ladder_scan.py) logs each fixture's Over probabilities to `.prop_alpha_state/pilot_predictions.jsonl`
before it sends anything, tagged with the digest of the pilot's pricing settings. This reads that log, finds the final
matches in the pilot database `prop_alpha_pilot` (read-only) and scores each league, market and line the way
scripts/gate2_lockbox.py scores the frozen model (evaluation/gate2.py: Brier skill against climatology with a
fixture-clustered 90% interval, calibration slope, worst bin). It is DIAGNOSTIC: the pilot has no frozen code and no lockbox,
so nothing here passes or fails a PRD gate, and a league with few matches says little. A prediction is made once per
fixture, player, market and line (the earliest logged is used) and counts only if the player played.

    .venv\\Scripts\\python.exe scripts\\pilot_score.py

Writes .prop_alpha_state/pilot_score_<date>.json.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.ladder_scan import PILOT_PREDICTIONS_FILE  # noqa: E402
from prop_alpha.alerts.predictions import PredictionLog  # noqa: E402
from prop_alpha.config import DEFAULT_PILOT_DSN, PILOT_DSN_ENV  # noqa: E402
from prop_alpha.evaluation.gate2 import score_line  # noqa: E402

SEED: Final[int] = 20261011
MIN_PREDICTIONS: Final[int] = 50
OUTCOME_COLUMNS: Final[dict[str, int]] = {"tackles": 2, "fouls": 3}
OUTCOMES_SQL: Final[str] = """
    SELECT l.name, s.player_id, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.leagues l ON l.league_id = f.league_id
    WHERE f.vendor_fixture_uid = %s AND f.status = 'final'
"""


def earliest_per_prediction(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per (fixture, player, market, line): the earliest logged, so a re-run never double counts a match."""
    best: dict[tuple[int, int, str, float], dict[str, Any]] = {}
    for record in records:
        key = (int(record["fixture_id"]), int(record["player_id"]), str(record["market"]), float(record["line"]))
        if key not in best or str(record["logged_at"]) < str(best[key]["logged_at"]):
            best[key] = record
    return list(best.values())


def main() -> int:
    import psycopg2

    records = earliest_per_prediction(PredictionLog(PILOT_PREDICTIONS_FILE).load())
    if not records:
        print("no pilot predictions logged yet")
        return 0
    digests = sorted({str(r["digest"]) for r in records})
    by_fixture: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_fixture[int(record["fixture_id"])].append(record)
    scored: dict[tuple[str, str, float], list[tuple[int, float, int]]] = defaultdict(list)
    waiting = 0
    conn = psycopg2.connect(os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN),
                            options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            for fixture_id, rows in sorted(by_fixture.items()):
                cur.execute(OUTCOMES_SQL, (str(fixture_id),))
                played = {int(r[1]): r for r in cur.fetchall() if int(r[2]) > 0}
                if not played:
                    waiting += 1  # not final in the pilot database yet
                    continue
                league = str(next(iter(played.values()))[0])
                for record in rows:
                    row = played.get(int(record["player_id"]))
                    total = float(record["p_win"]) + float(record["p_lose"])
                    if row is None or total <= 0.0:
                        continue
                    count = int(row[OUTCOME_COLUMNS[str(record["market"])]])
                    scored[(league, str(record["market"]), float(record["line"]))].append(
                        (fixture_id, float(record["p_win"]) / total, int(count > float(record["line"]))))
    finally:
        conn.close()
    rng = np.random.default_rng(SEED)
    result: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "digests": digests,
                              "fixtures_logged": len(by_fixture), "fixtures_waiting": waiting, "lines": {}}
    print(f"{len(by_fixture)} fixtures logged ({waiting} not final yet); settings digests: {', '.join(digests)}")
    print("DIAGNOSTIC ONLY: the pilot has no frozen model or lockbox; no PRD gate is judged here.")
    for (league, market, line), points in sorted(scored.items()):
        label = f"{league} {market} over {line:g}"
        if len(points) < MIN_PREDICTIONS or len({y for _, _, y in points}) < 2:
            print(f"  {label}: {len(points)} predictions - too few to score (needs {MIN_PREDICTIONS} and both outcomes)")
            result["lines"][label] = {"n": len(points), "scored": False}
            continue
        fixtures_of, p, y = zip(*points)
        score = score_line(fixtures_of, p, y, rng)
        result["lines"][label] = {**asdict(score), "criteria": score.criteria}
        print(f"  {label}: n={score.n} ({score.n_fixtures} matches) skill {score.skill:+.3f} "
              f"[{score.skill_low:+.3f}, {score.skill_high:+.3f}] slope {score.calibration_slope:.2f} "
              f"worst bin {score.worst_bin_gap:.3f}")
    out = ROOT / ".prop_alpha_state" / f"pilot_score_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
