"""Quick out-of-sample backtest (PRD Gate 2 preview) of tackles and fouls over-probabilities.

Fit on 2025/26: positional league rates per 90 and the game-state dispersion k (fit_game_state_dispersion on
players with >= 20 prior on-pitch matches). Score on 2026/27: every outfield starter with >= 5 earlier
appearances, each prediction from data strictly before kickoff (evaluation.backtest.walk_forward_over_probability,
lineup fixed at team-sheet time, neutral referee).

Compared with: climatology (the evaluation sample's own base rate, which flatters climatology) and a plain Poisson
on the player's own past rate (no shrinkage, no overdispersion; roughly how H1 says books price). Reports Brier
skill with a 90% fixture-clustered bootstrap interval, log loss, the logistic calibration slope and intercept, and
the worst calibration bin with >= 50 predictions.

Read-only on the working database (PROP_ALPHA_DB_DSN). Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\quick_backtest.py
Writes the full results to .prop_alpha_state/quick_backtest_<date>.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np
from scipy.stats import poisson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.config import DEFAULT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.calibration.fitting import fit_game_state_dispersion  # noqa: E402
from prop_alpha.evaluation.backtest import (  # noqa: E402
    BacktestFailure,
    LevelSource,
    PlayerMatchObservation,
    brier_score,
    calibration_table,
    log_loss,
    walk_forward_over_probability,
)
from prop_alpha.modeling.engine import QuantEngine  # noqa: E402
from prop_alpha.modeling.league_level import SeasonTotals, season_of, season_rates  # noqa: E402

SEASON_SPLIT: Final[datetime] = datetime(2026, 7, 1, tzinfo=timezone.utc)
LINES: Final[dict[str, tuple[float, ...]]] = {"tackles": (0.5, 1.5, 2.5), "fouls": (0.5, 1.5)}
COLUMN: Final[dict[str, str]] = {"tackles": "tackles_attempted", "fouls": "fouls_committed"}
OUTFIELD: Final[tuple[str, ...]] = ("DEF", "MID", "ATT")
PRIOR_STRENGTH_90S: Final[float] = 6.0
MIN_PRIOR_APPEARANCES: Final[int] = 5
K_FIT_MIN_PRIOR: Final[int] = 20
BOOTSTRAP: Final[int] = 2000
SEED: Final[int] = 20261004
# The live scan's k (slate.SCAN_GAME_STATE_K) and the stand-in for "no game-state overdispersion" when the fit
# finds none (1/k <= 0): k this large adds under 0.1% relative variance.
SCAN_K: Final[float] = 6.0
NO_OVERDISPERSION_K: Final[float] = 1000.0

QUERY: Final[str] = """
    SELECT s.player_id, s.fixture_id, f.kickoff_at, s.started, s.minutes_played,
           s.tackles_attempted, s.fouls_committed, p.primary_position
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.players p USING (player_id)
    WHERE f.status = 'final'
"""


def load_rows() -> list[tuple[Any, ...]]:
    import psycopg2

    conn = psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN),
                            options="-c default_transaction_read_only=on -c statement_timeout=120000")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(QUERY)
            return list(cur.fetchall())
    finally:
        conn.close()


def positional_rates(rows: Sequence[tuple[Any, ...]], market: str) -> dict[str, float]:
    """Rate per 90 by position, from the fit season only."""
    index = 5 if market == "tackles" else 6
    counts: dict[str, float] = defaultdict(float)
    minutes: dict[str, float] = defaultdict(float)
    for row in rows:
        if row[2] < SEASON_SPLIT and float(row[4]) > 0.0:
            counts[row[7]] += int(row[index])
            minutes[row[7]] += float(row[4])
    return {pos: 90.0 * counts[pos] / minutes[pos] for pos in minutes if minutes[pos] > 0.0}


def level_source(rows: Sequence[tuple[Any, ...]], market: str, position: str) -> LevelSource:
    """Season start year -> trailing rate per 90 for `position` (both leagues pooled), from matches strictly
    before the given kickoff. Cached per kickoff: many players share one."""
    index = 5 if market == "tackles" else 6
    by_season: dict[int, list[tuple[datetime, float, int]]] = defaultdict(list)
    for r in rows:
        if r[7] == position and float(r[4]) > 0.0:
            kickoff = r[2].astimezone(timezone.utc)
            by_season[season_of(kickoff)].append((kickoff, float(r[4]), int(r[index])))
    cumulative: dict[int, tuple[list[datetime], np.ndarray, np.ndarray]] = {}
    for season, samples in by_season.items():
        samples.sort()
        cumulative[season] = ([s[0] for s in samples], np.cumsum([s[1] for s in samples]),
                              np.cumsum([s[2] for s in samples]))
    cache: dict[datetime, dict[int, float]] = {}

    def source(as_of: datetime) -> dict[int, float]:
        if as_of not in cache:
            totals = {}
            for season, (times, minutes, counts) in cumulative.items():
                i = bisect_left(times, as_of)
                if i:
                    totals[season] = SeasonTotals(float(counts[i - 1]), float(minutes[i - 1]))
            # No match yet this season: last season's level (as the live scan does).
            totals.setdefault(season_of(as_of), SeasonTotals(0.0, 0.0))
            cache[as_of] = season_rates(totals)
        return cache[as_of]

    return source


def fit_k(observations: Sequence[PlayerMatchObservation], rate: dict[int, float]) -> float:
    """Method-of-moments k on fit-season starts, from each player's shrunk rate before kickoff."""
    by_player: dict[int, list[PlayerMatchObservation]] = defaultdict(list)
    for o in observations:
        by_player[o.player_id].append(o)
    y: list[int] = []
    mu: list[float] = []
    for pid, history in by_player.items():
        history.sort(key=lambda o: (o.kickoff_at, o.fixture_id))
        alpha, beta, on_pitch = PRIOR_STRENGTH_90S * rate[pid], PRIOR_STRENGTH_90S, 0
        for o in history:
            if o.kickoff_at >= SEASON_SPLIT:
                break
            if o.minutes_played > 0.0:
                if o.started and on_pitch >= K_FIT_MIN_PRIOR:
                    y.append(o.count)
                    mu.append(alpha / beta * o.minutes_played / 90.0)
                alpha += o.count
                beta += o.minutes_played / 90.0
                on_pitch += 1
    return fit_game_state_dispersion(np.array(y), np.array(mu))


def poisson_baseline(target: PlayerMatchObservation, prior: Sequence[PlayerMatchObservation], line: float) -> float:
    on_pitch = [o for o in prior if o.minutes_played > 0.0]
    minutes = sum(o.minutes_played for o in on_pitch)
    starts = [o.minutes_played for o in on_pitch if o.started]
    rate = 90.0 * sum(o.count for o in on_pitch) / minutes if minutes > 0.0 else 0.0
    expected_minutes = float(np.mean(starts)) if starts else 85.0
    return float(poisson.sf(math.floor(line), rate * expected_minutes / 90.0))


def calibration_fit(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Logistic regression logit P(y=1) = a + b logit(p), by Newton's method; returns (a, b)."""
    q = np.clip(p, 1e-6, 1 - 1e-6)
    x = np.column_stack([np.ones_like(q), np.log(q / (1 - q))])
    w = np.zeros(2)
    for _ in range(50):
        eta = x @ w
        prob = 1.0 / (1.0 + np.exp(-eta))
        grad = x.T @ (y - prob)
        hess = (x * (prob * (1 - prob))[:, None]).T @ x
        step = np.linalg.solve(hess, grad)
        w += step
        if np.abs(step).max() < 1e-10:
            break
    return float(w[0]), float(w[1])


def clustered_interval(
    fixtures: np.ndarray, p: np.ndarray, y: np.ndarray, base: np.ndarray, rng: np.random.Generator
) -> tuple[tuple[float, float], tuple[float, float]]:
    """90% intervals, resampling fixtures, for skill vs climatology (base rate re-estimated per resample) and
    skill vs `base` predictions."""
    unique, inverse = np.unique(fixtures, return_inverse=True)
    members = [np.flatnonzero(inverse == i) for i in range(len(unique))]
    clim, rel = [], []
    for _ in range(BOOTSTRAP):
        pick = np.concatenate([members[i] for i in rng.integers(0, len(unique), len(unique))])
        bs = np.mean((p[pick] - y[pick]) ** 2)
        bs_clim = np.mean((y[pick].mean() - y[pick]) ** 2)
        bs_base = np.mean((base[pick] - y[pick]) ** 2)
        clim.append(1 - bs / bs_clim if bs_clim > 0 else np.nan)
        rel.append(1 - bs / bs_base if bs_base > 0 else np.nan)
    return (tuple(np.nanpercentile(clim, [5, 95])), tuple(np.nanpercentile(rel, [5, 95])))  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", action="store_true",
                        help="trailing league level and level-scaled history (modeling.league_level)")
    args = parser.parse_args()
    rows = load_rows()
    rng = np.random.default_rng(SEED)
    excluded_positions = sorted({r[7] for r in rows if r[7] not in OUTFIELD})
    results: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "seed": SEED, "level": args.level,
                               "excluded_positions": excluded_positions, "markets": {}}
    print(f"league level: {'trailing, history scaled' if args.level else 'fixed 2025/26 positional rates'}")
    print(f"rows {len(rows)}; positions excluded from scoring: {excluded_positions}")
    for market, lines in LINES.items():
        index = 5 if market == "tackles" else 6
        rates = positional_rates(rows, market)
        obs_by_position: dict[str, list[PlayerMatchObservation]] = defaultdict(list)
        started_of: dict[tuple[int, int], bool] = {}
        for r in rows:
            if r[7] not in OUTFIELD:
                continue
            o = PlayerMatchObservation(int(r[0]), int(r[1]), r[2].astimezone(timezone.utc), bool(r[3]),
                                       float(r[4]), int(r[index]))
            obs_by_position[r[7]].append(o)
            started_of[(o.player_id, o.fixture_id)] = o.started
        all_obs = [o for group in obs_by_position.values() for o in group]
        player_rate = {o.player_id: rates[pos] for pos, group in obs_by_position.items() for o in group}
        try:
            fitted: float | None = fit_k(all_obs, player_rate)
        except ValueError as exc:
            fitted = None
            print(f"\n{market}: k fit refused: {exc}")
        print(f"\n{market}: positional rates (2025/26) {{{', '.join(f'{p}: {v:.2f}' for p, v in sorted(rates.items()))}}}; "
              f"fitted k = {'none (no overdispersion)' if fitted is None else f'{fitted:.2f}'}")
        results["markets"][market] = {"k_fitted": fitted, "rates": rates, "runs": {}}
        history_by_player: dict[int, list[PlayerMatchObservation]] = defaultdict(list)
        for o in all_obs:
            history_by_player[o.player_id].append(o)
        for label, k in (("fitted" if fitted is not None else "no-overdispersion", fitted or NO_OVERDISPERSION_K),
                         ("scan k=6", SCAN_K)):
            print(f" k = {k:g} ({label})")
            levels = {pos: level_source(rows, market, pos) for pos in obs_by_position} if args.level else None
            results["markets"][market]["runs"][label] = score_market(
                market, lines, k, obs_by_position, rates, started_of, all_obs, history_by_player, rng, levels)
    suffix = "_level" if args.level else ""
    out = ROOT / ".prop_alpha_state" / f"quick_backtest_{datetime.now(timezone.utc):%Y-%m-%d}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    print(f"\nfull results: {out}")
    return 0


def score_market(
    market: str, lines: Sequence[float], k: float, obs_by_position: dict[str, list[PlayerMatchObservation]],
    rates: dict[str, float], started_of: dict[tuple[int, int], bool], all_obs: Sequence[PlayerMatchObservation],
    history_by_player: dict[int, list[PlayerMatchObservation]], rng: np.random.Generator,
    levels: dict[str, LevelSource] | None = None,
) -> dict[str, Any]:
    engine = QuantEngine(game_state_k=k)
    scores: dict[str, Any] = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for line in lines:
            failures: list[BacktestFailure] = []
            predictions = []
            for position, group in obs_by_position.items():
                predictions += walk_forward_over_probability(
                    group, line, rates[position], engine, market, PRIOR_STRENGTH_90S, failures=failures,
                    level=None if levels is None else levels[position])
            scored = [pr for pr in predictions if pr.kickoff_at >= SEASON_SPLIT and started_of[(pr.player_id, pr.fixture_id)]
                      and pr.n_history >= MIN_PRIOR_APPEARANCES]
            target_of = {(o.player_id, o.fixture_id): o for o in all_obs}
            base = np.array([
                poisson_baseline(target_of[(pr.player_id, pr.fixture_id)],
                                 [o for o in history_by_player[pr.player_id] if o.kickoff_at < pr.kickoff_at], line)
                for pr in scored
            ])
            p = np.array([pr.p_over for pr in scored])
            y = np.array([pr.outcome for pr in scored], dtype=float)
            fixtures = np.array([pr.fixture_id for pr in scored])
            bs, bs_base = brier_score(p, y), brier_score(base, y)
            bs_clim = brier_score(np.full_like(p, y.mean()), y)
            (clim_lo, clim_hi), (rel_lo, rel_hi) = clustered_interval(fixtures, p, y, base, rng)
            a, b = calibration_fit(p, y)
            bins = [c for c in calibration_table(p, y) if c.n >= 50]
            worst = max((abs(c.mean_predicted - c.observed_rate) for c in bins), default=float("nan"))
            entry = {
                "n": len(scored), "fixtures": int(len(np.unique(fixtures))), "failures": len(failures),
                "base_rate": float(y.mean()), "brier": bs, "brier_climatology": bs_clim, "brier_poisson": bs_base,
                "bss_climatology": 1 - bs / bs_clim, "bss_climatology_90ci": [clim_lo, clim_hi],
                "skill_vs_poisson": 1 - bs / bs_base, "skill_vs_poisson_90ci": [rel_lo, rel_hi],
                "log_loss": log_loss(p, y), "calibration_intercept": a, "calibration_slope": b,
                "worst_bin_gap_n50": worst,
                "bins": [[c.lower, c.upper, c.n, c.mean_predicted, c.observed_rate] for c in calibration_table(p, y)],
            }
            scores[str(line)] = entry
            gate = ("PASS" if clim_lo > 0 and 0.9 <= b <= 1.1 and worst <= 0.03 else "FAIL")
            print(f"  over {line}: n={entry['n']} ({entry['fixtures']} matches, {len(failures)} unpriced) "
                  f"base {y.mean():.1%} | BSS vs climatology {entry['bss_climatology']:+.3f} "
                  f"[{clim_lo:+.3f}, {clim_hi:+.3f}] | vs Poisson {entry['skill_vs_poisson']:+.3f} "
                  f"[{rel_lo:+.3f}, {rel_hi:+.3f}] | slope {b:.2f} | worst bin {worst:.3f} | Gate-2 criteria: {gate}")
            print("     bins (n>=50): " + "; ".join(f"{c.lower:.1f}-{c.upper:.1f} n={c.n} pred {c.mean_predicted:.2f} "
                                                   f"obs {c.observed_rate:.2f}" for c in bins))
    return scores


if __name__ == "__main__":
    sys.exit(main())
