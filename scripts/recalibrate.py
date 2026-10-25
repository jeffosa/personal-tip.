"""Fit a per-market rate multiplier on the tune window and test it once (PRD Gate 2 calibration).

The quick and tuning backtests left tackles over-predicted by about 2.5 points on every line with calibration
slopes near 1: a level bias. Recalibrating each line's probability separately would make the lines disagree with
each other and with the allocator, which prices from the whole count distribution. This recalibrates the
distribution instead: the predictive rate is multiplied by c_m = (sum of actual counts) / (sum of predicted mean
counts) over the tune window, per market. Every price derived from it moves consistently.

Windows and settings as scripts/tune_backtest.py (prior strength 6, season blend SEASON_PRIOR_MINUTES, k 6 for
tackles and 1000 for fouls). The test window (2026-09-12 on) is scored once, with and without the multiplier,
against the PRD Gate 2 criteria. Its result decided which multipliers to keep, so it is a selection window, not a
holdout: Gate 2 evidence comes only from the lockbox in prop_alpha/modeling/model_freeze.py. Read-only on the working database. Run from the repository root:
    .venv\\Scripts\\python.exe -u scripts\\recalibrate.py
Writes .prop_alpha_state/recalibration_<date>.json, which holds the fitted multipliers.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np
from scipy.stats import nbinom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import quick_backtest as qb  # noqa: E402
import tune_backtest as tb  # noqa: E402
from prop_alpha.evaluation.backtest import PlayerMatchObservation, brier_score, calibration_table, log_loss  # noqa: E402
from prop_alpha.modeling.engine import NEUTRAL_REFEREE, LineupExpectation, QuantEngine  # noqa: E402
from prop_alpha.modeling.league_level import SEASON_PRIOR_MINUTES, level_scaled_minutes  # noqa: E402

PRIOR_STRENGTH: Final[float] = 6.0
Prediction = tuple[PlayerMatchObservation, float, float, float]  # (target, gamma shape, gamma rate, exposure in 90s)


def predictions(rows: Sequence[tuple[Any, ...]], market: str) -> list[Prediction]:
    index = 5 if market == "tackles" else 6
    engine = QuantEngine(game_state_k=tb.K_BY_MARKET[market])
    out: list[Prediction] = []
    by_position: dict[str, list[PlayerMatchObservation]] = defaultdict(list)
    for r in rows:
        if r[7] in qb.OUTFIELD:
            by_position[r[7]].append(PlayerMatchObservation(int(r[0]), int(r[1]), r[2].astimezone(timezone.utc),
                                                            bool(r[3]), float(r[4]), int(r[index])))
    for position, group in by_position.items():
        level = tb.level_cache(rows, market, position, SEASON_PRIOR_MINUTES)
        by_player: dict[int, list[PlayerMatchObservation]] = defaultdict(list)
        for o in group:
            by_player[o.player_id].append(o)
        for history in by_player.values():
            history.sort(key=lambda o: (o.kickoff_at, o.fixture_id))
            for i, target in enumerate(history):
                if (target.kickoff_at < tb.TUNE_START or target.kickoff_at >= tb.LOCKBOX_CUTOFF or not target.started
                        or i < qb.MIN_PRIOR_APPEARANCES):  # never a Gate 2 lockbox match (LOCKBOX_PROTOCOL.md)
                    continue
                on_pitch = [o for o in history[:i] if o.kickoff_at < target.kickoff_at and o.minutes_played > 0.0]
                starts = [o.minutes_played for o in on_pitch if o.started]
                subs = [o.minutes_played for o in on_pitch if not o.started]
                try:
                    exposure, rate = level_scaled_minutes([o.minutes_played for o in on_pitch],
                                                          [o.kickoff_at for o in on_pitch], level(target.kickoff_at),
                                                          target.kickoff_at)
                    lineup = LineupExpectation(1.0, 0.0, float(np.mean(starts)) if starts else 85.0,
                                               float(np.mean(subs)) if subs else 20.0)
                    projection = engine.project_count_market(
                        fixture_id=target.fixture_id, player_id=target.player_id, market_type=market,
                        counts=[o.count for o in on_pitch], minutes=exposure, league_rate_per90=rate,
                        referee=NEUTRAL_REFEREE, is_home=True, lineup=lineup, prior_strength_90s=PRIOR_STRENGTH,
                    )
                except ValueError:
                    continue
                out.append((target, projection.gamma_shape, projection.gamma_rate, lineup.exp_minutes_start / 90.0))
    return out


def over_probabilities(preds: Sequence[Prediction], line: float, multiplier: float) -> np.ndarray:
    """P(count > line) for a starter under NB(shape, (rate / c) / (rate / c + t)): the rate scaled by c."""
    shape = np.array([p[1] for p in preds])
    rate = np.array([p[2] for p in preds]) / multiplier
    exposure = np.array([p[3] for p in preds])
    return nbinom.sf(math.floor(line), shape, rate / (rate + exposure))


def evaluate(preds: Sequence[Prediction], line: float, multiplier: float, rng: np.random.Generator) -> dict[str, Any]:
    p = over_probabilities(preds, line, multiplier)
    y = np.array([float(t.count > line) for t, *_ in preds])
    fixtures = np.array([t.fixture_id for t, *_ in preds])
    base = np.full_like(p, y.mean())
    (lo, hi), _ = qb.clustered_interval(fixtures, p, y, base, rng)
    bins = [c for c in calibration_table(p, y) if c.n >= 50]
    a, b = qb.calibration_fit(p, y)
    worst = max((abs(c.mean_predicted - c.observed_rate) for c in bins), default=float("nan"))
    return {"n": len(preds), "fixtures": int(len(np.unique(fixtures))), "bss": 1 - brier_score(p, y) / brier_score(base, y),
            "bss_90ci": [lo, hi], "log_loss": log_loss(p, y), "slope": b, "intercept": a, "worst_bin": worst,
            "mean_pred": float(p.mean()), "base_rate": float(y.mean()),
            "gate2": bool(lo > 0 and 0.9 <= b <= 1.1 and worst <= 0.03),
            "bins": [[c.lower, c.upper, c.n, c.mean_predicted, c.observed_rate] for c in bins]}


def main() -> int:
    rows = qb.load_rows()
    rng = np.random.default_rng(qb.SEED)
    results: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "tune_start": tb.TUNE_START.isoformat(),
                               "test_start": tb.TEST_START.isoformat(), "prior_strength": PRIOR_STRENGTH,
                               "season_prior_minutes": SEASON_PRIOR_MINUTES, "k": tb.K_BY_MARKET,
                               "multipliers": {}, "test": {}}
    for market, lines in qb.LINES.items():
        preds = predictions(rows, market)
        tune = [p for p in preds if p[0].kickoff_at < tb.TEST_START]
        test = [p for p in preds if p[0].kickoff_at >= tb.TEST_START]
        actual = sum(t.count for t, *_ in tune)
        predicted = sum(shape / rate * exposure for _, shape, rate, exposure in tune)
        multiplier = actual / predicted
        results["multipliers"][market] = {"multiplier": multiplier, "tune_n": len(tune), "actual_counts": actual,
                                          "predicted_counts": predicted}
        print(f"{market}: tune window {len(tune)} starts, actual {actual} v predicted {predicted:.0f} counts -> "
              f"rate multiplier {multiplier:.4f}")
        for line in lines:
            before, after = evaluate(test, line, 1.0, rng), evaluate(test, line, multiplier, rng)
            results["test"][f"{market} {line:g}"] = {"before": before, "after": after}
            print(f"  TEST over {line:g} (n={after['n']}, {after['fixtures']} matches): "
                  f"mean pred {before['mean_pred']:.3f} -> {after['mean_pred']:.3f} v actual {after['base_rate']:.3f} | "
                  f"BSS {before['bss']:+.3f} -> {after['bss']:+.3f} [{after['bss_90ci'][0]:+.3f}, {after['bss_90ci'][1]:+.3f}]"
                  f" | slope {before['slope']:.2f} -> {after['slope']:.2f} | worst bin {before['worst_bin']:.3f} -> "
                  f"{after['worst_bin']:.3f} | Gate-2 calibration: {'PASS' if after['gate2'] else 'FAIL'}")
            print("     bins after: " + "; ".join(f"{lo_:.1f}-{hi_:.1f} n={n} pred {mp:.2f} obs {ob:.2f}"
                                                  for lo_, hi_, n, mp, ob in after["bins"]))
    out = ROOT / ".prop_alpha_state" / f"recalibration_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    print(f"\nfull results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
