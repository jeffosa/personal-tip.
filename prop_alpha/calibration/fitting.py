"""Offline estimators for the model's fitted parameters (kappa, beta1, beta2, k, state duel densities).

Every function is pure: it returns values and persists nothing. Callers store the results, for
example as the PROP_ALPHA_KAPPAS environment variable read by the ingestion pipeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import ArrayLike
from scipy.optimize import nnls
from scipy.special import gammaln

# Minimum sample for fitting k (maintainer decision 2, 2026-10-01): with a true k of 6 the moment estimate
# reports no overdispersion in 15.8% of 50-match samples, 1.7% at 200 and none at 1,000.
MIN_GAME_STATE_OBSERVATIONS: Final[int] = 200


@dataclass(frozen=True)
class GLMResult:
    coefficients: np.ndarray
    standard_errors: np.ndarray
    log_likelihood: float
    iterations: int

    def __post_init__(self) -> None:
        if self.coefficients.shape != self.standard_errors.shape:
            raise ValueError("coefficients and standard errors must have the same shape")
        if not (np.isfinite(self.coefficients).all() and np.isfinite(self.standard_errors).all()):
            raise ValueError("GLM estimates must be finite")
        if not math.isfinite(self.log_likelihood):
            raise ValueError("GLM log-likelihood must be finite")


def _as_vector(values: ArrayLike, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or array.size == 0:
        raise ValueError(f"{name} must be a non-empty one-dimensional array")
    if not np.isfinite(array).all():
        raise ValueError(f"{name} contains NaN or infinite values")
    return array


def _as_counts(values: ArrayLike, name: str) -> np.ndarray:
    counts = _as_vector(values, name)
    if (counts < 0.0).any() or (counts != np.floor(counts)).any():
        raise ValueError(f"{name} must be non-negative integers")
    return counts


def _as_positive(values: ArrayLike, name: str) -> np.ndarray:
    array = _as_vector(values, name)
    if (array <= 0.0).any():
        raise ValueError(f"{name} must be strictly positive")
    return array


def _poisson_log_likelihood(y: np.ndarray, eta: np.ndarray) -> float:
    return float(np.sum(y * eta - np.exp(eta) - gammaln(y + 1.0)))


def fit_poisson_glm(
    design: ArrayLike,
    counts: ArrayLike,
    offset: ArrayLike | None = None,
    max_iter: int = 100,
    tol: float = 1e-10,
) -> GLMResult:
    """Log-link Poisson GLM by Newton-Raphson with step halving; standard errors from the Fisher information.

    `offset` is on the log scale (for example log(minutes / 90)). Raises ValueError if the design is
    rank-deficient or the iteration does not converge; there is no partial result.
    """
    x = np.asarray(design, dtype=float)
    y = _as_counts(counts, "counts")
    if x.ndim != 2 or x.shape[0] != y.size:
        raise ValueError("design must be a 2-D array with one row per count")
    if not np.isfinite(x).all():
        raise ValueError("design contains NaN or infinite values")
    if np.linalg.matrix_rank(x) < x.shape[1]:
        raise ValueError("design matrix is rank-deficient")
    log_offset = np.zeros(y.size) if offset is None else _as_vector(offset, "offset")
    if log_offset.size != y.size:
        raise ValueError("offset must have one entry per count")
    if max_iter < 1 or not (math.isfinite(tol) and tol > 0.0):
        raise ValueError("max_iter must be >= 1 and tol finite and positive")

    beta = np.zeros(x.shape[1])
    log_likelihood = _poisson_log_likelihood(y, x @ beta + log_offset)
    for iteration in range(1, max_iter + 1):
        mean = np.exp(x @ beta + log_offset)
        gradient = x.T @ (y - mean)
        information = x.T @ (x * mean[:, None])
        step = np.linalg.solve(information, gradient)
        scale = 1.0
        candidate = beta + step
        candidate_ll = _poisson_log_likelihood(y, x @ candidate + log_offset)
        while not (math.isfinite(candidate_ll) and candidate_ll >= log_likelihood) and scale > 1e-10:
            scale *= 0.5
            candidate = beta + scale * step
            candidate_ll = _poisson_log_likelihood(y, x @ candidate + log_offset)
        if not math.isfinite(candidate_ll) or candidate_ll < log_likelihood:
            raise ValueError("Poisson GLM step halving failed to improve the likelihood")
        converged = float(np.max(np.abs(candidate - beta))) < tol
        beta, log_likelihood = candidate, candidate_ll
        if converged:
            mean = np.exp(x @ beta + log_offset)
            covariance = np.linalg.inv(x.T @ (x * mean[:, None]))
            return GLMResult(
                coefficients=beta,
                standard_errors=np.sqrt(np.diag(covariance)),
                log_likelihood=log_likelihood,
                iterations=iteration,
            )
    raise ValueError(f"Poisson GLM did not converge in {max_iter} iterations")


def fit_possession_kappa(counts: ArrayLike, minutes: ArrayLike, team_possession_pct: ArrayLike) -> float:
    """Possession elasticity kappa in E[count] = t * exp(a) * (opponent_possession / 50)^kappa.

    This is the exponent that transform.padj_scale divides out. Possession must lie strictly inside
    (0, 100); values at the bounds are rejected rather than clipped.
    """
    y = _as_counts(counts, "counts")
    exposure = _as_positive(minutes, "minutes") / 90.0
    possession = _as_vector(team_possession_pct, "team_possession_pct")
    if not (exposure.size == y.size == possession.size):
        raise ValueError("counts, minutes and possession must have equal length")
    if ((possession <= 0.0) | (possession >= 100.0)).any():
        raise ValueError("team_possession_pct must lie strictly inside (0, 100)")
    design = np.column_stack([np.ones(y.size), np.log((100.0 - possession) / 50.0)])
    return float(fit_poisson_glm(design, y, offset=np.log(exposure)).coefficients[1])


def fit_referee_betas(
    counts: ArrayLike,
    expected_counts: ArrayLike,
    strictness_coefficient: ArrayLike,
    home_bias_ratio: ArrayLike,
    is_home: ArrayLike,
) -> tuple[float, float]:
    """(beta1, beta2) in E[count] = expected * exp(a) * SC^beta1 * (1 -/+ HBR)^beta2.

    `expected_counts` is each player's referee-free expectation for the match (rate x exposure); it
    enters as the offset. The venue term is (1 - HBR) at home and (1 + HBR) away, as in
    QuantEngine.referee_multiplier.
    """
    y = _as_counts(counts, "counts")
    expected = _as_positive(expected_counts, "expected_counts")
    strictness = _as_positive(strictness_coefficient, "strictness_coefficient")
    bias = _as_vector(home_bias_ratio, "home_bias_ratio")
    home = np.asarray(is_home)
    if home.dtype != np.bool_ or home.shape != y.shape:
        raise ValueError("is_home must be a boolean array with one entry per count")
    if not (expected.size == strictness.size == bias.size == y.size):
        raise ValueError("all inputs must have equal length")
    if ((bias <= -1.0) | (bias >= 1.0)).any():
        raise ValueError("home_bias_ratio must lie strictly inside (-1, 1)")
    venue = np.where(home, 1.0 - bias, 1.0 + bias)
    design = np.column_stack([np.ones(y.size), np.log(strictness), np.log(venue)])
    coefficients = fit_poisson_glm(design, y, offset=np.log(expected)).coefficients
    return float(coefficients[1]), float(coefficients[2])


def fit_game_state_dispersion(
    counts: ArrayLike, expected_counts: ArrayLike, min_observations: int = MIN_GAME_STATE_OBSERVATIONS
) -> float:
    """NB2 method-of-moments shape k from out-of-sample expectations (PRD H2): 1/k = sum[(y - mu)^2 - y] / sum[mu^2].

    Raises instead of returning "no overdispersion": on small samples the estimate is <= 0 by sampling
    noise alone (26.7% of fits at 20 matches with a true k of 6), and there is no Poisson engine to fall
    back to. Callers keep the previously fitted k when this raises.
    """
    if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 1:
        raise ValueError("min_observations must be a positive integer")
    y = _as_counts(counts, "counts")
    mu = _as_positive(expected_counts, "expected_counts")
    if mu.size != y.size:
        raise ValueError("counts and expected_counts must have equal length")
    if y.size < min_observations:
        raise ValueError(f"need at least {min_observations} matches to fit k, got {y.size}")
    inverse_k = float(np.sum((y - mu) ** 2 - y) / np.sum(mu**2))
    if not inverse_k > 0.0:
        raise ValueError("no overdispersion detected; keep the previously fitted k instead of switching to Poisson")
    return 1.0 / inverse_k


def bench_sub_probability(
    bench_appearances: int, sub_appearances: int, league_mean: float, prior_strength: float
) -> float:
    """Beta-binomial posterior mean of P(comes on | named on the bench), shrunk towards the league mean.

    `bench_appearances` counts every bench selection, used or not. Nothing persists unused selections
    yet (vendor team sheets are not stored per fixture), so no caller can supply real counts today.
    """
    for name, value in (("bench_appearances", bench_appearances), ("sub_appearances", sub_appearances)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if bench_appearances < 0 or not 0 <= sub_appearances <= bench_appearances:
        raise ValueError("need 0 <= sub_appearances <= bench_appearances")
    if not (0.0 < league_mean < 1.0 and math.isfinite(prior_strength) and prior_strength > 0.0):
        raise ValueError("league_mean must lie in (0, 1) and prior_strength must be finite and positive")
    return (sub_appearances + prior_strength * league_mean) / (bench_appearances + prior_strength)


def fit_state_duel_density(
    duels: ArrayLike, leading_minutes: ArrayLike, level_minutes: ArrayLike, trailing_minutes: ArrayLike
) -> np.ndarray:
    """Non-negative duels per 90 in (leading, level, trailing) states: duels ~ sum_s density_s * minutes_s / 90."""
    y = _as_counts(duels, "duels")
    columns = [
        _as_vector(values, name)
        for values, name in (
            (leading_minutes, "leading_minutes"),
            (level_minutes, "level_minutes"),
            (trailing_minutes, "trailing_minutes"),
        )
    ]
    if any(column.size != y.size for column in columns):
        raise ValueError("duels and state minutes must have equal length")
    if any((column < 0.0).any() for column in columns):
        raise ValueError("state minutes must be non-negative")
    design = np.column_stack(columns) / 90.0
    if np.linalg.matrix_rank(design) < 3:
        raise ValueError("state minutes do not identify all three densities")
    densities, _ = nnls(design, y)
    return densities
