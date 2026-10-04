"""PRD Gate 2 scoring of over-probabilities (pure: numpy only, no I/O, no clock; randomness only from the caller's
generator).

Per market and line: Brier skill against climatology with a fixture-clustered bootstrap 90% interval, the logistic
calibration slope, and the worst calibration bin with at least MIN_BIN_PREDICTIONS predictions. For a binary
outcome the climatology Brier score of a sample is ybar (1 - ybar), so each resample's skill is computed from
per-fixture sums without re-scoring every prediction. Thresholds are the PRD's (prop_alpha_PRD_v3.1.md, Gate 2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Sequence

import numpy as np

from prop_alpha.evaluation.backtest import brier_score, calibration_table, log_loss

MIN_PREDICTIONS: Final[int] = 500
CALIBRATION_SLOPE_RANGE: Final[tuple[float, float]] = (0.9, 1.1)
MAX_BIN_GAP: Final[float] = 0.03
MIN_BIN_PREDICTIONS: Final[int] = 50
BOOTSTRAP_RESAMPLES: Final[int] = 10_000
INTERVAL_PERCENTILES: Final[tuple[float, float]] = (5.0, 95.0)
NEWTON_STEPS: Final[int] = 100
NEWTON_TOLERANCE: Final[float] = 1e-10
# Above this condition number the Newton step is not trusted: separated outcomes or near-constant predictions.
MAX_HESSIAN_CONDITION: Final[float] = 1e12


def _arrays(probabilities: Sequence[float], outcomes: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(probabilities, dtype=np.float64)
    y = np.asarray(outcomes, dtype=np.float64)
    if p.ndim != 1 or p.shape != y.shape or len(p) == 0:
        raise ValueError("probabilities and outcomes must be equal-length, non-empty vectors")
    if not np.isfinite(p).all() or ((p <= 0.0) | (p >= 1.0)).any():
        raise ValueError("probabilities must lie strictly between 0 and 1")
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("outcomes must be 0 or 1")
    return p, y


@dataclass(frozen=True)
class CalibrationFit:
    """logit P(y = 1) = intercept + slope logit(p). When the fit has no reliable maximum (separated outcomes,
    near-constant predictions), `converged` is False, the coefficients are nan and `note` says why: the line then
    fails the calibration criterion instead of stopping the whole evaluation (audit 2026-10-04)."""

    intercept: float
    slope: float
    converged: bool
    note: str = ""


def calibration_fit(probabilities: Sequence[float], outcomes: Sequence[int]) -> CalibrationFit:
    """The logistic calibration fit, by Newton's method with a conditioning check before every step."""
    p, y = _arrays(probabilities, outcomes)
    x = np.column_stack([np.ones_like(p), np.log(p / (1.0 - p))])
    w = np.zeros(2)
    for _ in range(NEWTON_STEPS):
        prob = 1.0 / (1.0 + np.exp(-(x @ w)))
        hess = (x * (prob * (1.0 - prob))[:, None]).T @ x
        condition = np.linalg.cond(hess)
        if not np.isfinite(condition) or condition > MAX_HESSIAN_CONDITION:
            return CalibrationFit(math.nan, math.nan, False, "singular fit (separated outcomes or constant predictions)")
        step = np.linalg.solve(hess, x.T @ (y - prob))
        w += step
        if not np.isfinite(w).all():
            return CalibrationFit(math.nan, math.nan, False, "fit diverged")
        if np.abs(step).max() < NEWTON_TOLERANCE:
            return CalibrationFit(float(w[0]), float(w[1]), True)
    return CalibrationFit(math.nan, math.nan, False, f"no convergence in {NEWTON_STEPS} steps")


def clustered_skill_interval(
    fixtures: Sequence[int], probabilities: Sequence[float], outcomes: Sequence[int], rng: np.random.Generator,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[float, float]:
    """90% interval of the Brier skill score against climatology, resampling whole fixtures with replacement."""
    p, y = _arrays(probabilities, outcomes)
    ids = np.asarray(fixtures)
    if ids.shape != p.shape:
        raise ValueError("one fixture id per prediction")
    if resamples < 100:
        raise ValueError("at least 100 resamples")
    _, cluster = np.unique(ids, return_inverse=True)
    n_clusters = int(cluster.max()) + 1
    size = np.bincount(cluster, minlength=n_clusters).astype(np.float64)
    hits = np.bincount(cluster, weights=y, minlength=n_clusters)
    squared_error = np.bincount(cluster, weights=(p - y) ** 2, minlength=n_clusters)
    counts = rng.multinomial(n_clusters, np.full(n_clusters, 1.0 / n_clusters), size=resamples).astype(np.float64)
    n = counts @ size
    ybar = (counts @ hits) / n
    climatology = ybar * (1.0 - ybar)
    with np.errstate(divide="ignore", invalid="ignore"):
        skill = 1.0 - ((counts @ squared_error) / n) / climatology
    skill = skill[np.isfinite(skill)]
    if len(skill) < resamples // 2:
        raise ValueError("too few resamples with both outcomes to bound the skill score")
    low, high = np.percentile(skill, INTERVAL_PERCENTILES)
    return float(low), float(high)


def worst_bin_gap(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Largest |mean predicted - observed| over calibration bins with >= MIN_BIN_PREDICTIONS; nan if none."""
    p, y = _arrays(probabilities, outcomes)
    gaps = [abs(b.mean_predicted - b.observed_rate) for b in calibration_table(p, y) if b.n >= MIN_BIN_PREDICTIONS]
    return max(gaps) if gaps else math.nan


@dataclass(frozen=True)
class LineScore:
    n: int
    n_fixtures: int
    base_rate: float
    brier: float
    brier_climatology: float
    skill: float
    skill_low: float
    skill_high: float
    log_loss: float
    calibration_intercept: float
    calibration_slope: float
    worst_bin_gap: float
    calibration_note: str = ""

    @property
    def criteria(self) -> dict[str, bool]:
        """Each PRD Gate 2 criterion this line can be judged on (the market benchmark needs closing prices)."""
        return {
            "sample size": self.n >= MIN_PREDICTIONS,
            "skill (90% lower bound > 0)": self.skill_low > 0.0,
            "calibration slope in [0.9, 1.1]": not math.isnan(self.calibration_slope)
            and CALIBRATION_SLOPE_RANGE[0] <= self.calibration_slope <= CALIBRATION_SLOPE_RANGE[1],
            "every bin of >= 50 within 0.03": not math.isnan(self.worst_bin_gap) and self.worst_bin_gap <= MAX_BIN_GAP,
        }

    @property
    def passed(self) -> bool:
        return all(self.criteria.values())


def score_line(fixtures: Sequence[int], probabilities: Sequence[float], outcomes: Sequence[int],
               rng: np.random.Generator, resamples: int = BOOTSTRAP_RESAMPLES) -> LineScore:
    p, y = _arrays(probabilities, outcomes)
    base = float(y.mean())
    brier, climatology = brier_score(p, y), base * (1.0 - base)
    if climatology == 0.0:
        raise ValueError("every outcome is the same: skill against climatology is undefined")
    low, high = clustered_skill_interval(fixtures, p, y, rng, resamples)
    fit = calibration_fit(p, y)
    return LineScore(
        n=len(p), n_fixtures=len(set(int(f) for f in fixtures)), base_rate=base, brier=brier,
        brier_climatology=climatology, skill=1.0 - brier / climatology, skill_low=low, skill_high=high,
        log_loss=log_loss(p, y), calibration_intercept=fit.intercept, calibration_slope=fit.slope,
        worst_bin_gap=worst_bin_gap(p, y), calibration_note=fit.note,
    )
