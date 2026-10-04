"""Tune the fouls market's game-state k and prior strength with the scan's own pricing (2026-10-04).

On the selection window the frozen fouls model was overconfident (Gate 2 calibration slope 0.79 and 0.84). Two
settings set how far fouls probabilities spread: the game-state dispersion k (1000 = none) and the prior strength
(how many 90-minute matches the position's league rate is worth against the player's own history). This scores a
grid of both with slate.build_projections, exactly as the scan prices (as-of-kickoff positions, league levels,
level weights, estimated lineups), on every League One/Two match in the tune window, and chooses the setting
with the lowest mean log loss over the fouls lines in model_freeze.GATE2_LINES, each Over probability given that
the player plays. Stage 2 then chooses the fouls rate multiplier (level recalibration; the earlier fit, 0.935,
was rejected on selection-window results, which is the contamination the 2026-10-04 audit flagged) from
MULTIPLIER_GRID, again by tune log loss alone. The selection window is then scored once, current v chosen, as a
check; it does not choose. --stage2 K PRIOR skips the grid and runs stage 2 at that setting.

    tune      2026-01-01 .. 2026-09-12
    selection 2026-09-12 .. 2026-10-05 (the lockbox starts 2026-10-05 and is never read here)

Read-only on the working database. Run from the repository root:
    .venv\\Scripts\\python.exe -u scripts\\tune_fouls.py
Writes .prop_alpha_state/tune_fouls_<date>.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.scan import load_slate_inputs  # noqa: E402
from prop_alpha.config import DEFAULT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.evaluation.backtest import log_loss  # noqa: E402
from prop_alpha.evaluation.gate2 import calibration_fit, worst_bin_gap  # noqa: E402
from prop_alpha.ingestion.sportmonks import SCAN_LEAGUES  # noqa: E402
from prop_alpha.modeling.engine import QuantEngine  # noqa: E402
from prop_alpha.modeling.model_freeze import GATE2_LINES, LOCKBOX_START  # noqa: E402
from prop_alpha.scanner.slate import (  # noqa: E402
    SCAN_GAME_STATE_K,
    SCAN_GAME_STATE_K_BY_MARKET,
    SCAN_PRIOR_STRENGTH_BY_MARKET,
    SCAN_RATE_MULTIPLIER_BY_MARKET,
    UpcomingFixture,
    build_projections,
)

TUNE_START: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
SELECTION_START: Final[datetime] = datetime(2026, 9, 12, tzinfo=timezone.utc)
K_GRID: Final[tuple[float, ...]] = (4.0, 6.0, 10.0, 20.0, 1000.0)
PRIOR_GRID: Final[tuple[float, ...]] = (6.0, 12.0, 24.0, 48.0)
MULTIPLIER_GRID: Final[tuple[float, ...]] = (0.92, 0.94, 0.96, 0.98, 1.0)
MARKET: Final[str] = "fouls"
FIXTURES_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, l.name, ht.vendor_team_uid, at.vendor_team_uid, ht.name, at.name, f.kickoff_at
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.leagues l USING (league_id)
    JOIN prop_alpha.teams ht ON ht.team_id = f.home_team_id
    JOIN prop_alpha.teams at ON at.team_id = f.away_team_id
    WHERE f.status = 'final' AND f.kickoff_at >= %s AND f.kickoff_at < %s AND l.name = ANY(%s)
    ORDER BY f.kickoff_at, f.vendor_fixture_uid
"""
OUTCOMES_SQL: Final[str] = """
    SELECT s.player_id, s.minutes_played, s.fouls_committed
    FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f USING (fixture_id)
    WHERE f.vendor_fixture_uid = %s
"""


def predictions(cur: Any, start: datetime, end: datetime, k: float, prior: float,
                cache: dict[int, Any], multiplier: float | None = None) -> dict[float, tuple[list[float], list[int]]]:
    """Over probability given an appearance, and the outcome, per fouls line, for every match in [start, end)."""
    cur.execute(FIXTURES_SQL, (start, end, sorted(SCAN_LEAGUES.values())))
    fixtures = [UpcomingFixture(int(uid), league, str(home), str(away), hn, an, kickoff.astimezone(timezone.utc))
                for uid, league, home, away, hn, an, kickoff in cur.fetchall()]
    out: dict[float, tuple[list[float], list[int]]] = {line: ([], []) for line in GATE2_LINES[MARKET]}
    engine = QuantEngine(game_state_k=SCAN_GAME_STATE_K)
    for fixture in fixtures:
        if fixture.fixture_id not in cache:
            inputs = load_slate_inputs(cur, fixture)
            cur.execute(OUTCOMES_SQL, (str(fixture.fixture_id),))
            played = {int(pid): int(fouls) for pid, minutes, fouls in cur.fetchall() if int(minutes) > 0}
            cache[fixture.fixture_id] = (inputs, played)
        inputs, played = cache[fixture.fixture_id]
        projections, _ = build_projections(
            fixture, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures, inputs.league_rates, engine,
            season_levels=inputs.season_levels or None,
            market_engines={**{m: QuantEngine(game_state_k=v) for m, v in SCAN_GAME_STATE_K_BY_MARKET.items()},
                            MARKET: QuantEngine(game_state_k=k)},
            rate_multipliers={**SCAN_RATE_MULTIPLIER_BY_MARKET,
                              **({} if multiplier is None else {MARKET: multiplier})},
            prior_strengths={**SCAN_PRIOR_STRENGTH_BY_MARKET, MARKET: prior},
        )
        for (_, player_id, market), projection in projections.items():
            if market != MARKET or player_id not in played:
                continue
            for line in GATE2_LINES[MARKET]:
                side = projection.side_probabilities(line, "over")
                out[line][0].append(side.p_win / (side.p_win + side.p_lose))
                out[line][1].append(int(played[player_id] > line))
    return out


def summary(preds: dict[float, tuple[list[float], list[int]]]) -> dict[str, Any]:
    lines = {}
    for line, (p, y) in preds.items():
        fit = calibration_fit(p, y)
        lines[str(line)] = {"n": len(p), "log_loss": log_loss(np.array(p), np.array(y)), "slope": fit.slope,
                            "worst_bin": worst_bin_gap(p, y)}
    return {"mean_log_loss": float(np.mean([v["log_loss"] for v in lines.values()])), "lines": lines}


def main() -> int:
    import psycopg2

    if SELECTION_START >= LOCKBOX_START:
        raise ValueError("the selection window must end before the lockbox")
    stage2_only = sys.argv[1:2] == ["--stage2"]
    conn = psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN),
                            options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    result: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "tune": {}, "selection": {}}

    def report(label: str, s: dict[str, Any]) -> None:
        print(f"{label}: mean log loss {s['mean_log_loss']:.5f} | " + " | ".join(
            f"over {line}: slope {v['slope']:.2f} worst bin {v['worst_bin']:.3f}" for line, v in s["lines"].items()),
            flush=True)

    try:
        with conn, conn.cursor() as cur:
            cache: dict[int, Any] = {}
            if stage2_only:
                k_best, prior_best = float(sys.argv[2]), float(sys.argv[3])
            else:
                for k in K_GRID:
                    for prior in PRIOR_GRID:
                        s = summary(predictions(cur, TUNE_START, SELECTION_START, k, prior, cache))
                        result["tune"][f"k={k:g} prior={prior:g}"] = s
                        report(f"tune k={k:g} prior={prior:g}", s)
                best = min(result["tune"], key=lambda name: result["tune"][name]["mean_log_loss"])
                k_best, prior_best = (float(part.split("=")[1]) for part in best.split())
            print(f"chosen on the tune window: k={k_best:g} prior={prior_best:g}", flush=True)
            stage2: dict[str, Any] = {}
            for multiplier in MULTIPLIER_GRID:
                s = summary(predictions(cur, TUNE_START, SELECTION_START, k_best, prior_best, cache, multiplier))
                stage2[f"{multiplier:g}"] = s
                report(f"tune multiplier={multiplier:g}", s)
            result["tune_multiplier"] = stage2
            m_best = float(min(stage2, key=lambda name: stage2[name]["mean_log_loss"]))
            print(f"chosen multiplier on the tune window: {m_best:g}", flush=True)
            current = (SCAN_GAME_STATE_K_BY_MARKET[MARKET], SCAN_PRIOR_STRENGTH_BY_MARKET[MARKET],
                       SCAN_RATE_MULTIPLIER_BY_MARKET[MARKET])
            result["chosen"] = {"k": k_best, "prior": prior_best, "multiplier": m_best}
            selection_cache: dict[int, Any] = {}
            for name, (k, prior, multiplier) in (("current", current), ("chosen", (k_best, prior_best, m_best))):
                s = summary(predictions(cur, SELECTION_START, LOCKBOX_START, k, prior, selection_cache, multiplier))
                result["selection"][name] = {"k": k, "prior": prior, "multiplier": multiplier, **s}
                report(f"selection {name} k={k:g} prior={prior:g} multiplier={multiplier:g}", s)
    finally:
        conn.close()
    out = ROOT / ".prop_alpha_state" / f"tune_fouls_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
