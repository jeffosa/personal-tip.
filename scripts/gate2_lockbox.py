"""PRD Gate 2 on the lockbox: score the frozen scan model on League One/Two matches from LOCKBOX_START on.

Every final lockbox fixture is priced exactly as the scan prices it (alerts/scan.py load_slate_inputs and
price_fixture, with data strictly before kickoff), and each regular starter's Over probability, given that he
plays (the void-if-no-appearance contract the scan bets on), is scored against what he did, per market and line
(model_freeze.GATE2_LINES). Criteria per line (evaluation/gate2.py): >= 500 predictions, Brier skill against
climatology with a fixture-clustered bootstrap 90% lower bound > 0, calibration slope in [0.9, 1.1], every bin of
>= 50 within 0.03. A market passes when all its lines pass.

Not judged here: the market benchmark (criterion 4) needs devigged closing prices, which are not captured; card
markets are not priced by the scan. Deviation from the PRD protocol: lineups are the scan's estimates, not team
sheets. Positions are as of kickoff (player_match_stats.match_position, migration 007), so a lockbox match never
informs its own price.

Refuses to run when the pricing code differs from the freeze (model_freeze.FROZEN_DIGEST). Read-only on the working
database. Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\gate2_lockbox.py
Writes .prop_alpha_state/gate2_lockbox_<date>.json.
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
from prop_alpha.alerts.scan import load_slate_inputs, price_fixture, scan_engines  # noqa: E402
from prop_alpha.config import DEFAULT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.evaluation.gate2 import score_line  # noqa: E402
from prop_alpha.ingestion.sportmonks import SCAN_LEAGUES  # noqa: E402
from prop_alpha.modeling.model_freeze import FROZEN_DIGEST, GATE2_LINES, LOCKBOX_START, pricing_digest  # noqa: E402
from prop_alpha.scanner.slate import UpcomingFixture  # noqa: E402

SEED: Final[int] = 20261005
OUTCOME_COLUMNS: Final[dict[str, int]] = {"tackles": 1, "fouls": 2}
FIXTURES_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, l.name, ht.vendor_team_uid, at.vendor_team_uid, ht.name, at.name, f.kickoff_at
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.leagues l USING (league_id)
    JOIN prop_alpha.teams ht ON ht.team_id = f.home_team_id
    JOIN prop_alpha.teams at ON at.team_id = f.away_team_id
    WHERE f.status = 'final' AND f.kickoff_at >= %s AND l.name = ANY(%s)
    ORDER BY f.kickoff_at, f.vendor_fixture_uid
"""
OUTCOMES_SQL: Final[str] = """
    SELECT s.player_id, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    WHERE f.vendor_fixture_uid = %s
"""


def main() -> int:
    import psycopg2

    digest = pricing_digest()
    if digest != FROZEN_DIGEST:
        print(f"REFUSED: the pricing code is not the frozen model (digest {digest[:12]} v frozen {FROZEN_DIGEST[:12]}).")
        return 2
    engine, market_engines = scan_engines()
    predictions: dict[tuple[str, float], list[tuple[int, float, int]]] = defaultdict(list)
    failures: list[str] = []
    conn = psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN),
                            options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(FIXTURES_SQL, (LOCKBOX_START, sorted(SCAN_LEAGUES.values())))
            fixtures = [UpcomingFixture(int(uid), league, str(home), str(away), home_name, away_name,
                                        kickoff.astimezone(timezone.utc))
                        for uid, league, home, away, home_name, away_name, kickoff in cur.fetchall()]
            for fixture in fixtures:
                inputs = load_slate_inputs(cur, fixture)
                try:
                    projections, _ = price_fixture(fixture, inputs, engine, market_engines)
                except ValueError as exc:  # ProjectionError is a ValueError
                    failures.append(f"{fixture.fixture_id} {fixture.name}: {exc}")
                    continue
                cur.execute(OUTCOMES_SQL, (str(fixture.fixture_id),))
                played = {int(row[0]): row for row in cur.fetchall() if int(row[1]) > 0}
                for (fixture_id, player_id, market), projection in projections.items():
                    if player_id not in played or market not in GATE2_LINES:
                        continue
                    count = int(played[player_id][1 + OUTCOME_COLUMNS[market]])
                    for line in GATE2_LINES[market]:
                        side = projection.side_probabilities(line, "over")
                        predictions[(market, line)].append(
                            (fixture_id, side.p_win / (side.p_win + side.p_lose), int(count > line)))
    finally:
        conn.close()

    rng = np.random.default_rng(SEED)
    result: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "lockbox_start": LOCKBOX_START.isoformat(),
                              "frozen_digest": FROZEN_DIGEST, "fixtures": len(fixtures), "unpriced": failures,
                              "seed": SEED, "markets": {}}
    print(f"Lockbox from {LOCKBOX_START:%Y-%m-%d}: {len(fixtures)} final matches, {len(failures)} could not be priced")
    for market, lines in GATE2_LINES.items():
        verdicts: list[bool] = []
        result["markets"][market] = {}
        for line in lines:
            rows = predictions.get((market, line), [])
            if len({y for _, _, y in rows}) < 2:
                print(f"  {market} over {line}: {len(rows)} predictions - not enough to score")
                result["markets"][market][str(line)] = {"n": len(rows), "scored": False}
                verdicts.append(False)
                continue
            fixtures_of, p, y = zip(*rows)
            try:
                score = score_line(fixtures_of, p, y, rng)
            except ValueError as exc:  # recorded as a failed line; the other lines are still scored
                print(f"  {market} over {line}: {len(rows)} predictions - NOT SCORABLE: {exc}")
                result["markets"][market][str(line)] = {"n": len(rows), "scored": False, "error": str(exc)}
                verdicts.append(False)
                continue
            verdicts.append(score.passed)
            result["markets"][market][str(line)] = {**asdict(score), "criteria": score.criteria, "passed": score.passed}
            failed = [name for name, ok in score.criteria.items() if not ok]
            print(f"  {market} over {line}: n={score.n} ({score.n_fixtures} matches) skill {score.skill:+.3f} "
                  f"[{score.skill_low:+.3f}, {score.skill_high:+.3f}] slope {score.calibration_slope:.2f} "
                  f"worst bin {score.worst_bin_gap:.3f} -> {'PASS' if score.passed else 'FAIL: ' + '; '.join(failed)}"
                  + (f" ({score.calibration_note})" if score.calibration_note else ""))
        result["markets"][market]["passed"] = all(verdicts)
        print(f"{market}: {'PASS' if all(verdicts) else 'not passed'}")
    print("Market benchmark (criterion 4): not judged - closing prices are not captured. Card markets: not priced.")
    out = ROOT / ".prop_alpha_state" / f"gate2_lockbox_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
