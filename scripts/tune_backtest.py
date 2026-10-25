"""Tune prior strength and the season blend of the league level, then test the chosen setting once.

Windows (kickoff, UTC):
    tune  2026-01-01 .. 2026-09-12   2025/26 second half (prior strength) and the first weeks of 2026/27 (the
                                     season blend only matters across a season boundary, and 2024/25 is not loaded)
    test  2026-09-12 ..              selection window: its results chose settings on 2026-10-04, so it is no
                                     longer a holdout (Gate 2 scores the frozen model on the lockbox, model_freeze.py)
The setting with the lowest mean log loss over both markets' lines in the tune window is chosen; the test window
then compares it with the current setting (prior strength 6, 90,000 pseudo-minutes).

Each prediction uses only data strictly before kickoff, priced exactly as evaluation.backtest._over_probability
does with a league level, except that one projection serves every line (the walk-forward rebuilds it per line).
k is fixed per market from the quick backtest: 6 for tackles, 1000 (no overdispersion) for fouls.

Read-only on the working database. Run from the repository root:
    .venv\\Scripts\\python.exe -u scripts\\tune_backtest.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import quick_backtest as qb  # noqa: E402
from prop_alpha.evaluation.lockbox import PROTOCOL  # noqa: E402
from prop_alpha.evaluation.backtest import (  # noqa: E402
    PlayerMatchObservation,
    brier_score,
    calibration_table,
    log_loss,
)
from prop_alpha.modeling.engine import NEUTRAL_REFEREE, LineupExpectation, QuantEngine  # noqa: E402
from prop_alpha.modeling.league_level import SeasonTotals, level_scaled_minutes, season_of, season_rates  # noqa: E402

TUNE_START: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEST_START: Final[datetime] = datetime(2026, 9, 12, tzinfo=timezone.utc)
# No League One/Two match from the Gate 2 lockbox start is ever scored by a tuning run (LOCKBOX_PROTOCOL.md, 2026-10-25):
# the test window used to be open-ended, so a run after the start read lockbox outcomes.
LOCKBOX_CUTOFF: Final[datetime] = PROTOCOL.start
PRIOR_STRENGTHS: Final[tuple[float, ...]] = (6.0, 3.0, 1.5)
BLEND_MINUTES: Final[tuple[float, ...]] = (90_000.0, 30_000.0)
CURRENT: Final[tuple[float, float]] = (6.0, 90_000.0)
K_BY_MARKET: Final[dict[str, float]] = {"tackles": 6.0, "fouls": 1000.0}


def level_cache(rows: Sequence[tuple[Any, ...]], market: str, position: str, blend: float) -> Any:
    """As quick_backtest.level_source, with the season blend as a parameter."""
    from bisect import bisect_left

    index = 5 if market == "tackles" else 6
    by_season: dict[int, list[tuple[datetime, float, int]]] = defaultdict(list)
    for r in rows:
        if r[7] == position and float(r[4]) > 0.0:
            kickoff = r[2].astimezone(timezone.utc)
            by_season[season_of(kickoff)].append((kickoff, float(r[4]), int(r[index])))
    cumulative = {}
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
            totals.setdefault(season_of(as_of), SeasonTotals(0.0, 0.0))
            cache[as_of] = season_rates(totals, blend)
        return cache[as_of]

    return source


def predict(
    obs_by_position: dict[str, list[PlayerMatchObservation]], rows: Sequence[tuple[Any, ...]], market: str,
    lines: Sequence[float], prior_strength: float, blend: float,
) -> list[tuple[PlayerMatchObservation, int, dict[float, float]]]:
    """(target, prior appearances, {line: P(over)}) for every starter from TUNE_START on with >= 5 earlier rows."""
    engine = QuantEngine(game_state_k=K_BY_MARKET[market])
    out: list[tuple[PlayerMatchObservation, int, dict[float, float]]] = []
    for position, group in obs_by_position.items():
        level = level_cache(rows, market, position, blend)
        by_player: dict[int, list[PlayerMatchObservation]] = defaultdict(list)
        for o in group:
            by_player[o.player_id].append(o)
        for history in by_player.values():
            history.sort(key=lambda o: (o.kickoff_at, o.fixture_id))
            for i, target in enumerate(history):
                if (target.kickoff_at < TUNE_START or target.kickoff_at >= LOCKBOX_CUTOFF or not target.started
                        or i < qb.MIN_PRIOR_APPEARANCES):
                    continue
                prior = [o for o in history[:i] if o.kickoff_at < target.kickoff_at]
                on_pitch = [o for o in prior if o.minutes_played > 0.0]
                starts = [o.minutes_played for o in on_pitch if o.started]
                subs = [o.minutes_played for o in on_pitch if not o.started]
                try:
                    exposure, rate = level_scaled_minutes([o.minutes_played for o in on_pitch],
                                                          [o.kickoff_at for o in on_pitch], level(target.kickoff_at),
                                                          target.kickoff_at)
                    projection = engine.project_count_market(
                        fixture_id=target.fixture_id, player_id=target.player_id, market_type=market,
                        counts=[o.count for o in on_pitch], minutes=exposure, league_rate_per90=rate,
                        referee=NEUTRAL_REFEREE, is_home=True,
                        lineup=LineupExpectation(1.0, 0.0, float(np.mean(starts)) if starts else 85.0,
                                                 float(np.mean(subs)) if subs else 20.0),
                        prior_strength_90s=prior_strength,
                    )
                except ValueError:
                    continue
                out.append((target, len(prior),
                            {line: projection.side_probabilities(line, "over").p_win for line in lines}))
    return out


def scores(preds: Sequence[tuple[PlayerMatchObservation, int, dict[float, float]]], line: float,
           start: datetime, end: datetime | None) -> dict[str, Any]:
    chosen = [(t, p) for t, _, p in preds if t.kickoff_at >= start and (end is None or t.kickoff_at < end)]
    p = np.array([probs[line] for _, probs in chosen])
    y = np.array([float(t.count > line) for t, _ in chosen])
    bins = [c for c in calibration_table(p, y) if c.n >= 50]
    a, b = qb.calibration_fit(p, y)
    return {"n": len(chosen), "fixtures": len({t.fixture_id for t, _ in chosen}), "log_loss": log_loss(p, y),
            "brier": brier_score(p, y), "bss": 1 - brier_score(p, y) / brier_score(np.full_like(p, y.mean()), y),
            "slope": b, "intercept": a,
            "worst_bin": max((abs(c.mean_predicted - c.observed_rate) for c in bins), default=float("nan")),
            "mean_pred": float(p.mean()), "base_rate": float(y.mean())}


def main() -> int:
    rows = qb.load_rows()
    results: dict[str, Any] = {"run_at": datetime.now(timezone.utc).isoformat(), "tune_start": TUNE_START.isoformat(),
                               "test_start": TEST_START.isoformat(), "k": K_BY_MARKET, "grid": {}}
    predictions: dict[tuple[float, float], dict[str, Any]] = {}
    for prior_strength in PRIOR_STRENGTHS:
        for blend in BLEND_MINUTES:
            setting = (prior_strength, blend)
            predictions[setting] = {}
            tune_losses: list[float] = []
            line_report = []
            for market, lines in qb.LINES.items():
                index = 5 if market == "tackles" else 6
                obs_by_position: dict[str, list[PlayerMatchObservation]] = defaultdict(list)
                for r in rows:
                    if r[7] in qb.OUTFIELD:
                        obs_by_position[r[7]].append(PlayerMatchObservation(
                            int(r[0]), int(r[1]), r[2].astimezone(timezone.utc), bool(r[3]), float(r[4]), int(r[index])))
                preds = predict(obs_by_position, rows, market, lines, prior_strength, blend)
                predictions[setting][market] = preds
                for line in lines:
                    tune = scores(preds, line, TUNE_START, TEST_START)
                    tune_losses.append(tune["log_loss"])
                    line_report.append(f"{market} O{line:g}: logloss {tune['log_loss']:.4f} slope {tune['slope']:.2f} "
                                       f"worst {tune['worst_bin']:.3f} pred {tune['mean_pred']:.3f} v {tune['base_rate']:.3f}")
            mean_loss = float(np.mean(tune_losses))
            results["grid"][f"{prior_strength:g}/{blend:g}"] = {"mean_tune_log_loss": mean_loss}
            print(f"prior {prior_strength:g}, blend {blend:,.0f}: mean tune log loss {mean_loss:.5f}")
            for text in line_report:
                print("   " + text)
    best = min(predictions, key=lambda s: results["grid"][f"{s[0]:g}/{s[1]:g}"]["mean_tune_log_loss"])
    results["chosen"] = {"prior_strength": best[0], "blend_minutes": best[1]}
    print(f"\nchosen on the tune window: prior strength {best[0]:g}, blend {best[1]:,.0f} pseudo-minutes")
    print("TEST window (2026-09-12 on), current -> chosen:")
    results["test"] = {}
    for market, lines in qb.LINES.items():
        for line in lines:
            now, new = (scores(predictions[s][market], line, TEST_START, None) for s in (CURRENT, best))
            results["test"][f"{market} {line:g}"] = {"current": now, "chosen": new}
            print(f"  {market} O{line:g} (n={new['n']}, {new['fixtures']} matches): BSS {now['bss']:+.3f} -> "
                  f"{new['bss']:+.3f} | slope {now['slope']:.2f} -> {new['slope']:.2f} | worst bin "
                  f"{now['worst_bin']:.3f} -> {new['worst_bin']:.3f} | mean pred {now['mean_pred']:.3f} -> "
                  f"{new['mean_pred']:.3f} v actual {new['base_rate']:.3f}")
    out = ROOT / ".prop_alpha_state" / f"tune_backtest_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    print(f"\nfull results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
