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
from typing import Any, Final, Mapping

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.ladder_scan import PILOT_PREDICTIONS_FILE  # noqa: E402
from prop_alpha.alerts.predictions import PredictionLog  # noqa: E402
from prop_alpha.config import DEFAULT_PILOT_DSN, PILOT_DSN_ENV  # noqa: E402
from prop_alpha.evaluation.gate2 import score_line  # noqa: E402

SEED: Final[int] = 20261011
MIN_PREDICTIONS: Final[int] = 50
# The outcome each market is scored on. Audit 2026-10-11 (second pass): this used to index the query's row by position and
# read minutes as tackles and tackles as fouls; outcomes are now named fields of `Outcome`.
OUTCOMES_SQL: Final[str] = """
    SELECT l.name, s.player_id, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.leagues l ON l.league_id = f.league_id
    WHERE f.vendor_fixture_uid = %s AND f.status = 'final'
"""
Outcome = tuple[str, int, int, int]  # (league, minutes, tackles_attempted, fouls_committed)
LineKey = tuple[str, str, str, float]  # (settings digest, league, market, line)


def outcome_count(outcome: Outcome, market: str) -> int:
    """The settled count of `market` in a player's match: tackles_attempted or fouls_committed, never minutes."""
    _, _, tackles, fouls = outcome
    if market == "tackles":
        return tackles
    if market == "fouls":
        return fouls
    raise ValueError(f"no outcome for market {market!r}")


def earliest_per_prediction(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per (digest, fixture, player, market, line): the earliest logged, so a re-run never double counts a match."""
    best: dict[tuple[str, int, int, str, float], dict[str, Any]] = {}
    for record in records:
        key = (str(record["digest"]), int(record["fixture_id"]), int(record["player_id"]), str(record["market"]),
               float(record["line"]))
        if key not in best or str(record["logged_at"]) < str(best[key]["logged_at"]):
            best[key] = record
    return list(best.values())


def score_points(
    records: list[dict[str, Any]], outcomes: Mapping[int, Mapping[int, Outcome]],
) -> dict[LineKey, list[tuple[int, float, int]]]:
    """Pure: (fixture, P(over | plays), over happened) per settings digest, league, market and line. `outcomes` maps a
    fixture to its players' (league, minutes, tackles, fouls); a player who did not play, or a fixture with no outcomes yet,
    is left out. Each digest is scored on its own: two pricing settings are two models."""
    points: dict[LineKey, list[tuple[int, float, int]]] = defaultdict(list)
    for record in earliest_per_prediction(records):
        fixture_id, player_id = int(record["fixture_id"]), int(record["player_id"])
        outcome = outcomes.get(fixture_id, {}).get(player_id)
        total = float(record["p_win"]) + float(record["p_lose"])
        if outcome is None or outcome[1] <= 0 or total <= 0.0:
            continue
        market, line = str(record["market"]), float(record["line"])
        points[(str(record["digest"]), outcome[0], market, line)].append(
            (fixture_id, float(record["p_win"]) / total, int(outcome_count(outcome, market) > line)))
    return points


def main() -> int:
    import psycopg2

    records = PredictionLog(PILOT_PREDICTIONS_FILE).load()
    if not records:
        print("no pilot predictions logged yet")
        return 0
    fixture_ids = sorted({int(r["fixture_id"]) for r in records})
    outcomes: dict[int, dict[int, Outcome]] = {}
    conn = psycopg2.connect(os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN),
                            options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            for fixture_id in fixture_ids:
                cur.execute(OUTCOMES_SQL, (str(fixture_id),))
                rows = cur.fetchall()
                if rows:
                    outcomes[fixture_id] = {int(r[1]): (str(r[0]), int(r[2]), int(r[3]), int(r[4])) for r in rows}
    finally:
        conn.close()
    scored = score_points(records, outcomes)
    waiting = len(fixture_ids) - len(outcomes)
    rng = np.random.default_rng(SEED)
    digests = sorted({str(r["digest"]) for r in records})
    result: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "digests": digests,
                              "fixtures_logged": len(fixture_ids), "fixtures_waiting": waiting, "lines": {}}
    print(f"{len(fixture_ids)} fixtures logged ({waiting} not final yet); settings digests: {', '.join(digests)}")
    print("DIAGNOSTIC ONLY: the pilot has no frozen model or lockbox; no PRD gate is judged here.")
    for (digest, league, market, line), points in sorted(scored.items()):
        label = f"[{digest}] {league} {market} over {line:g}"
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
