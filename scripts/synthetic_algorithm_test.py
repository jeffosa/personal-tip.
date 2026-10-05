"""Run the project's pricing path on the synthetic world (scripts/make_synthetic_world.py) and score it against truth.

    .venv\\Scripts\\python.exe -u scripts\\synthetic_algorithm_test.py [--dir synthetic_data]

The pricing is the same walk-forward path the real backtests use (scripts/recalibrate.predictions: data strictly before
kickoff, league level, per-market k from tune_backtest), with the rate multiplier fixed at 1.0 because the real 0.9483
tackles recalibration was fitted to real data. No database is touched.

Checks, each with a stated expectation:
  A. Recovery of the truth: the model's P(over) against the exact true probability, with a no-information baseline
     (position-and-line average); expect a clearly smaller error than the baseline.
  B. Outcome scoring: Brier skill vs climatology with a 90% fixture-clustered interval, calibration slope and worst bin.
  C. Betting against books of known quality, flat 1 unit, simplified rule (not the full allocator): back a side when
     (p - 0.03) x odds - 1 >= 0.04 (the 4% minimum EV after the 0.03 model-risk loss floor). Expect: profit vs the SOFT book,
     roughly -margin (or worse) vs CONTROL (no mispricing exists), and positive closing-line value vs SOFT.
     Controls: the same rule with the no-information baseline (expect less than the model) and with the true probability
     (the ceiling).
Writes .prop_alpha_state/synthetic_test_<date>.json.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import quick_backtest as qb  # noqa: E402
import recalibrate as rc  # noqa: E402
from prop_alpha.evaluation.backtest import brier_score, calibration_table  # noqa: E402

LOSS_FLOOR: Final[float] = 0.03
MIN_EV: Final[float] = 0.04
EVAL_FROM: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
SEED: Final[int] = 11


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_rows(directory: Path) -> list[tuple[Any, ...]]:
    """(player_id, fixture_id, kickoff, started, minutes, tackles, fouls, position), the backtests' row layout."""
    return [(int(r["player_id"]), int(r["fixture_id"]), datetime.fromisoformat(r["kickoff_at"]), bool(int(r["started"])),
             float(r["minutes_played"]), int(r["tackles_attempted"]), int(r["fouls_committed"]), r["position"])
            for r in read(directory / "player_match_stats.csv")]


def bootstrap_mean(values: np.ndarray, clusters: np.ndarray, rng: np.random.Generator, n: int = 1000) -> tuple[float, float]:
    unique, inverse = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inverse, weights=values, minlength=len(unique))
    sizes = np.bincount(inverse, minlength=len(unique))
    draws: list[float] = []
    for _ in range(n):
        pick = rng.integers(len(unique), size=len(unique))
        draws.append(float(sums[pick].sum()) / max(float(sizes[pick].sum()), 1.0))
    return float(np.percentile(draws, 5)), float(np.percentile(draws, 95))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="synthetic_data")
    directory = ROOT / parser.parse_args().dir
    rng = np.random.default_rng(SEED)

    rows = load_rows(directory)
    odds = read(directory / "eval_odds.csv")
    print(f"{len(rows)} player rows, {len(odds)} quotes; pricing starters walk-forward ...", flush=True)

    # model probability per (fixture, player, market, line)
    model_p: dict[tuple[int, int, str, float], float] = {}
    outcome: dict[tuple[int, int, str], int] = {}
    for r in rows:
        outcome[(r[1], r[0], "tackles")] = r[5]
        outcome[(r[1], r[0], "fouls")] = r[6]
    for market, lines in qb.LINES.items():
        preds = [p for p in rc.predictions(rows, market) if p[0].kickoff_at >= EVAL_FROM]
        for line in lines:
            probabilities = rc.over_probabilities(preds, line, 1.0)
            for (target, *_), p in zip(preds, probabilities):
                model_p[(target.fixture_id, target.player_id, market, line)] = float(p)

    # no-information baseline: P(count > line) among past starters of that position
    position = {r[0]: r[7] for r in rows}
    history: dict[tuple[str, str, float], list[int]] = defaultdict(list)
    for r in rows:
        if r[2] < EVAL_FROM and r[3] and r[7] != "GK":
            for market, lines in qb.LINES.items():
                for line in lines:
                    history[(r[7], market, line)].append(int(r[5 if market == "tackles" else 6] > line))
    baseline = {k: float(np.mean(v)) for k, v in history.items()}

    # ---- A and B: probabilities ---------------------------------------------------------------------------------
    keys, p_model, p_true, p_base, y, fixtures = [], [], [], [], [], []
    seen: set[tuple[int, int, str, float]] = set()
    for q in odds:
        key = (int(q["fixture_id"]), int(q["player_id"]), q["market"], float(q["line"]))
        if key in seen or key not in model_p:
            continue
        seen.add(key)
        keys.append(key)
        p_model.append(model_p[key])
        p_true.append(float(q["true_p_over"]))
        p_base.append(baseline[(position[key[1]], key[2], key[3])])
        y.append(float(outcome[(key[0], key[1], key[2])] > key[3]))
        fixtures.append(key[0])
    pm, pt, pb, yy, fx = map(np.array, (p_model, p_true, p_base, y, fixtures))
    result: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "n_predictions": len(pm),
                              "n_fixtures": int(len(np.unique(fx)))}
    result["A_recovery"] = {"model_mae_vs_truth": float(np.mean(np.abs(pm - pt))),
                            "baseline_mae_vs_truth": float(np.mean(np.abs(pb - pt))),
                            "correlation_model_truth": float(np.corrcoef(pm, pt)[0, 1]),
                            "correlation_baseline_truth": float(np.corrcoef(pb, pt)[0, 1]),
                            "oracle_brier": brier_score(pt, yy), "model_brier": brier_score(pm, yy),
                            "baseline_brier": brier_score(pb, yy)}
    base_rate = np.full_like(pm, yy.mean())
    (lo, hi), _ = qb.clustered_interval(fx, pm, yy, base_rate, rng)
    a, b = qb.calibration_fit(pm, yy)
    bins = [c for c in calibration_table(pm, yy) if c.n >= 50]
    result["B_scoring"] = {"bss_vs_climatology": 1 - brier_score(pm, yy) / brier_score(base_rate, yy), "bss_90ci": [lo, hi],
                           "oracle_bss": 1 - brier_score(pt, yy) / brier_score(base_rate, yy),
                           "calibration_slope": b, "calibration_intercept": a,
                           "worst_bin_gap": max((abs(c.mean_predicted - c.observed_rate) for c in bins), default=float("nan")),
                           "bins": [[c.lower, c.upper, c.n, c.mean_predicted, c.observed_rate] for c in bins]}

    # ---- C: betting against known books --------------------------------------------------------------------------
    def bets(source: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for book in ("SOFT", "SHARP", "CONTROL"):
            profit, true_ev, clv, clusters = [], [], [], []
            for q in odds:
                if q["book"] != book:
                    continue
                key = (int(q["fixture_id"]), int(q["player_id"]), q["market"], float(q["line"]))
                if key not in model_p:
                    continue
                p_true_over = float(q["true_p_over"])
                p_view = {"model": model_p[key], "baseline": baseline[(position[key[1]], key[2], key[3])],
                          "truth": p_true_over}[source]
                over_won = outcome[(key[0], key[1], key[2])] > key[3]
                for side, p_side, true_side, price, close, won in (
                    ("over", p_view, p_true_over, float(q["open_over"]), float(q["close_over"]), over_won),
                    ("under", 1.0 - p_view, 1.0 - p_true_over, float(q["open_under"]), float(q["close_under"]), not over_won),
                ):
                    if (p_side - LOSS_FLOOR) * price - 1.0 >= MIN_EV:
                        profit.append(price - 1.0 if won else -1.0)
                        true_ev.append(true_side * price - 1.0)
                        clv.append(price / close - 1.0)
                        clusters.append(key[0])
            n = len(profit)
            if n == 0:
                out[book] = {"bets": 0}
                continue
            pr, ev, cl, cluster = map(np.array, (profit, true_ev, clv, clusters))
            lo_roi, hi_roi = bootstrap_mean(pr, cluster, rng)
            out[book] = {"bets": n, "fixtures": int(len(np.unique(cluster))), "roi_realised": float(pr.mean()),
                         "roi_90ci": [lo_roi, hi_roi], "roi_expected_under_truth": float(ev.mean()),
                         "mean_clv": float(cl.mean())}
        return out

    result["C_betting"] = {source: bets(source) for source in ("model", "baseline", "truth")}
    out_path = ROOT / ".prop_alpha_state" / f"synthetic_test_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out_path.write_text(json.dumps(result, indent=1, default=float), encoding="utf-8")

    A, B = result["A_recovery"], result["B_scoring"]
    print(f"\nA. Recovery of the true probability ({result['n_predictions']} predictions, {result['n_fixtures']} fixtures)")
    print(f"   mean |model - truth| {A['model_mae_vs_truth']:.4f}   baseline {A['baseline_mae_vs_truth']:.4f}   "
          f"correlation with truth: model {A['correlation_model_truth']:.3f}, baseline {A['correlation_baseline_truth']:.3f}")
    print(f"B. Outcomes: Brier skill vs climatology {B['bss_vs_climatology']:+.4f} (90% CI {B['bss_90ci'][0]:+.4f} to "
          f"{B['bss_90ci'][1]:+.4f}); oracle (true p) {B['oracle_bss']:+.4f}")
    print(f"   calibration slope {B['calibration_slope']:.3f}, worst bin gap {B['worst_bin_gap']:.3f}")
    print("C. Flat-stake betting (4% minimum EV after the 0.03 floor):")
    for source in ("model", "baseline", "truth"):
        for book, v in result["C_betting"][source].items():
            if v["bets"] == 0:
                print(f"   {source:8s} v {book:7s}: no bets")
                continue
            print(f"   {source:8s} v {book:7s}: {v['bets']:5d} bets / {v['fixtures']} fixtures, ROI {v['roi_realised']:+.3f} "
                  f"[{v['roi_90ci'][0]:+.3f}, {v['roi_90ci'][1]:+.3f}], expected under truth {v['roi_expected_under_truth']:+.3f}, "
                  f"mean CLV {v['mean_clv']:+.3f}")
    print(f"\nresults: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
