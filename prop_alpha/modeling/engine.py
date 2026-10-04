"""Predictive engine: Gamma-Poisson posteriors with minutes exposure, referee adjustment,
game-state overdispersion, lineup/void handling, and bivariate Poisson dependence."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Final, Protocol, Sequence

import numpy as np
from numpy.typing import ArrayLike
from scipy.linalg import eigh_tridiagonal
from scipy.stats import nbinom, poisson

logger = logging.getLogger("prop_alpha.engine")

REFEREE_SENSITIVE_MARKETS: Final[frozenset[str]] = frozenset({"fouls", "fouls_drawn"})

# Relative-variance bounds on the per-90 match rate, i.e. Var/mu^2 = 1/shape.
# 1e-6 is the numerical Poisson limit (values below it are clamped with [MATH_CLAMP]); a value above
# 1e3 means the prior or history is degenerate and raises ProjectionError (maintainer decision 3, 2026-10-01).
MIN_RELATIVE_VARIANCE: Final[float] = 1e-6
MAX_RELATIVE_VARIANCE: Final[float] = 1e3

# Upper bound on minutes in one match (90 + stoppage + extra time); mirrors chk_minute_logic in schema.sql.
MAX_MATCH_MINUTES: Final[float] = 130.0

# Gauss nodes over the shared match factor G ~ Gamma(k, k); moments of G are exact to ~1e-12 for k in [0.05, 1e9].
GAME_STATE_QUADRATURE_NODES: Final[int] = 48


def validate_exposure_vector(
    minutes: Sequence[float], counts: Sequence[int], *, allow_empty: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """Validated (minutes float64, counts int64) arrays; raises ValueError on any polluted entry."""
    if len(minutes) != len(counts):
        raise ValueError(f"minutes and counts must have equal length, got {len(minutes)} and {len(counts)}")
    if len(minutes) == 0:
        if allow_empty:
            return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.int64)
        raise ValueError("exposure vectors must not be empty")
    raw_minutes = np.asarray(minutes)
    raw_counts = np.asarray(counts)
    if raw_minutes.ndim != 1 or raw_counts.ndim != 1:
        raise ValueError("exposure vectors must be one-dimensional")
    if raw_minutes.dtype.kind not in "iuf" or raw_counts.dtype.kind not in "iuf":
        raise ValueError("exposure vectors must be numeric; booleans, strings and None are rejected")
    np_minutes = raw_minutes.astype(np.float64)
    np_counts = raw_counts.astype(np.float64)
    if not (np.isfinite(np_minutes).all() and np.isfinite(np_counts).all()):
        raise ValueError("NaN or infinite value in exposure vectors")
    if (np_minutes < 0.0).any() or (np_minutes > MAX_MATCH_MINUTES).any():
        raise ValueError(f"minutes must lie in [0, {MAX_MATCH_MINUTES}]")
    if (np_counts < 0.0).any() or (np_counts != np.floor(np_counts)).any():
        raise ValueError("counts must be non-negative integers")
    if ((np_counts > 0.0) & (np_minutes == 0.0)).any():
        raise ValueError("a positive count cannot be recorded with zero minutes played")
    return np_minutes, np_counts.astype(np.int64)


class DiscreteDistribution(Protocol):
    def pmf(self, values: ArrayLike) -> float | np.ndarray: ...

    def cdf(self, values: ArrayLike) -> float | np.ndarray: ...

    def sf(self, values: ArrayLike) -> float | np.ndarray: ...

    def mean(self) -> float: ...

    def var(self) -> float: ...


class ProjectionError(ValueError):
    """Raised when inputs cannot produce a valid predictive distribution."""


class VoidRule(str, Enum):
    VOID_IF_NO_APPEARANCE = "void_if_no_appearance"
    VOID_IF_NOT_STARTING = "void_if_not_starting"
    NO_VOID = "no_void"


@dataclass(frozen=True)
class RefereeProfile:
    strictness_coefficient: float
    yellow_card_propensity: float
    red_card_propensity: float
    home_bias_ratio: float

    def __post_init__(self) -> None:
        values = (
            self.strictness_coefficient,
            self.yellow_card_propensity,
            self.red_card_propensity,
            self.home_bias_ratio,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("referee coefficients must be finite")
        if self.strictness_coefficient <= 0.0:
            raise ValueError("strictness_coefficient must be positive")
        if self.yellow_card_propensity < 0.0 or self.red_card_propensity < 0.0:
            raise ValueError("card propensities must be non-negative")
        if not -1.0 < self.home_bias_ratio < 1.0:
            raise ValueError("home_bias_ratio must lie strictly inside (-1, 1)")


# Multiplier 1 and no card propensity: used when no referee is known (backtest observations, pre-appointment
# scans). Referee-sensitive markets are then priced without a referee adjustment.
NEUTRAL_REFEREE: Final[RefereeProfile] = RefereeProfile(1.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class GammaPosterior:
    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.alpha) and math.isfinite(self.beta)):
            raise ValueError("Gamma parameters must be finite")
        if self.alpha <= 0.0 or self.beta <= 0.0:
            raise ValueError("Gamma parameters must be positive")

    @property
    def mean(self) -> float:
        return self.alpha / self.beta

    @property
    def variance(self) -> float:
        return self.alpha / self.beta**2


@dataclass(frozen=True)
class MinutesMixture:
    """Distribution of minutes played within one lineup scenario (early substitution, red card, full match)."""

    minutes: tuple[float, ...]
    weights: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.minutes or len(self.minutes) != len(self.weights):
            raise ValueError("minutes and weights must be non-empty and equal length")
        if any(not (math.isfinite(m) and 0.0 < m <= MAX_MATCH_MINUTES) for m in self.minutes):
            raise ValueError(f"mixture minutes must lie in (0, {MAX_MATCH_MINUTES}]")
        if any(not (math.isfinite(w) and w >= 0.0) for w in self.weights):
            raise ValueError("mixture weights must be finite and non-negative")
        if not math.isclose(math.fsum(self.weights), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("mixture weights must sum to 1")

    @property
    def mean(self) -> float:
        return math.fsum(m * w for m, w in zip(self.minutes, self.weights))


@dataclass(frozen=True)
class LineupExpectation:
    """Scenario probabilities and minutes. Without a mixture a scenario is priced at its expected minutes;
    with one, prices average over the mixture, whose mean must equal the expected minutes."""

    p_start: float
    p_sub_appearance: float
    exp_minutes_start: float = 85.0
    exp_minutes_sub: float = 20.0
    start_mixture: MinutesMixture | None = None
    sub_mixture: MinutesMixture | None = None

    def __post_init__(self) -> None:
        values = (self.p_start, self.p_sub_appearance, self.exp_minutes_start, self.exp_minutes_sub)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("lineup probabilities and expected minutes must be finite")
        if not (0.0 <= self.p_start <= 1.0 and 0.0 <= self.p_sub_appearance <= 1.0):
            raise ValueError("lineup probabilities must lie in [0, 1]")
        if self.p_start + self.p_sub_appearance > 1.0:
            raise ValueError("p_start + p_sub_appearance cannot exceed 1")
        for minutes in (self.exp_minutes_start, self.exp_minutes_sub):
            if not 0.0 < minutes <= MAX_MATCH_MINUTES:
                raise ValueError(f"expected minutes must lie in (0, {MAX_MATCH_MINUTES}]")
        for name, mixture, expected in (
            ("start_mixture", self.start_mixture, self.exp_minutes_start),
            ("sub_mixture", self.sub_mixture, self.exp_minutes_sub),
        ):
            if mixture is not None and not math.isclose(mixture.mean, expected, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(f"{name} mean {mixture.mean!r} must equal its expected minutes {expected!r}")

    @property
    def start_minutes(self) -> MinutesMixture:
        if self.start_mixture is not None:
            return self.start_mixture
        return MinutesMixture((self.exp_minutes_start,), (1.0,))

    @property
    def sub_minutes(self) -> MinutesMixture:
        if self.sub_mixture is not None:
            return self.sub_mixture
        return MinutesMixture((self.exp_minutes_sub,), (1.0,))

    @property
    def p_no_appearance(self) -> float:
        # Subtract the rounded sum once: __post_init__ guarantees it is <= 1, so 1 - sum is exact and
        # non-negative (Sterbenz). Chained subtraction can return -1.1e-16 for (0.33, 0.67).
        return 1.0 - (self.p_start + self.p_sub_appearance)


@dataclass(frozen=True)
class SideProbabilities:
    p_win: float
    p_lose: float
    p_void: float

    def __post_init__(self) -> None:
        values = (self.p_win, self.p_lose, self.p_void)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("side probabilities must be finite")
        if any(value < 0.0 or value > 1.0 for value in values):
            raise ValueError("side probabilities must lie in [0, 1]")
        if not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("win, lose, and void probabilities must sum to 1")


def _validate_line(line: float) -> None:
    if not math.isfinite(line) or line < 0.0 or not float(line * 2.0).is_integer():
        raise ValueError(f"only finite, non-negative whole and half lines are supported, got {line!r}")


def _line_masses(distribution: DiscreteDistribution, line: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(P(over), P(under), P(push)), elementwise when the distribution has array parameters."""
    _validate_line(line)
    floor_line = math.floor(line)
    p_over = np.asarray(distribution.sf(floor_line), dtype=np.float64)
    if float(line).is_integer():
        p_push = np.asarray(distribution.pmf(floor_line), dtype=np.float64)
        p_under = np.asarray(distribution.cdf(floor_line - 1), dtype=np.float64)
    else:
        p_under = np.asarray(distribution.cdf(floor_line), dtype=np.float64)
        p_push = np.zeros_like(p_under)
    return p_over, p_under, p_push


def _line_split(distribution: DiscreteDistribution, line: float) -> tuple[float, float, float]:
    """Return (P(over), P(under), P(push)) for a count distribution."""
    p_over, p_under, p_push = _line_masses(distribution, line)
    return float(p_over), float(p_under), float(p_push)


def _zero_split(line: float) -> tuple[float, float, float]:
    _validate_line(line)
    return (0.0, 0.0, 1.0) if line == 0.0 else (0.0, 1.0, 0.0)


ScenarioSplit = Callable[[MinutesMixture], tuple[float, float, float]]


def _lineup_side_probabilities(
    lineup: LineupExpectation, line: float, side: str, void_rule: VoidRule | str, split: ScenarioSplit
) -> SideProbabilities:
    """Combine per-scenario (over, under, push) splits under a void rule; `split` prices one minutes mixture."""
    if side not in ("over", "under"):
        raise ValueError("side must be 'over' or 'under'")
    rule = VoidRule(void_rule)

    def oriented(scenario_split: tuple[float, float, float]) -> tuple[float, float, float]:
        over_probability, under_probability, push_probability = scenario_split
        if side == "over":
            return over_probability, under_probability, push_probability
        return under_probability, over_probability, push_probability

    win_start, lose_start, push_start = oriented(split(lineup.start_minutes))
    win_sub, lose_sub, push_sub = oriented(split(lineup.sub_minutes))
    win_absent, lose_absent, push_absent = oriented(_zero_split(line))

    # p_void is summed from non-negative void and push masses, never formed as 1 - p_win - p_lose,
    # which rounds to about -1e-16 on roughly 5% of inputs and would fail validation.
    if rule is VoidRule.VOID_IF_NO_APPEARANCE:
        p_win = lineup.p_start * win_start + lineup.p_sub_appearance * win_sub
        p_lose = lineup.p_start * lose_start + lineup.p_sub_appearance * lose_sub
        p_void = lineup.p_no_appearance + lineup.p_start * push_start + lineup.p_sub_appearance * push_sub
    elif rule is VoidRule.VOID_IF_NOT_STARTING:
        p_win = lineup.p_start * win_start
        p_lose = lineup.p_start * lose_start
        p_void = (1.0 - lineup.p_start) + lineup.p_start * push_start
    else:
        p_win = (
            lineup.p_start * win_start
            + lineup.p_sub_appearance * win_sub
            + lineup.p_no_appearance * win_absent
        )
        p_lose = (
            lineup.p_start * lose_start
            + lineup.p_sub_appearance * lose_sub
            + lineup.p_no_appearance * lose_absent
        )
        p_void = (
            lineup.p_start * push_start
            + lineup.p_sub_appearance * push_sub
            + lineup.p_no_appearance * push_absent
        )
    return SideProbabilities(p_win, p_lose, p_void)


@dataclass(frozen=True)
class PropProjection:
    """Predictive distribution for one player-market-fixture."""

    fixture_id: int
    player_id: int
    market_type: str
    gamma_shape: float
    gamma_rate: float
    lineup: LineupExpectation

    def __post_init__(self) -> None:
        for name, value in (("gamma_shape", self.gamma_shape), ("gamma_rate", self.gamma_rate)):
            if not (math.isfinite(value) and value > 0.0):
                raise ProjectionError(f"{name}={value!r} is not a finite positive number")

    @property
    def rate_per90(self) -> float:
        return self.gamma_shape / self.gamma_rate

    def count_distribution(self, minutes: float) -> DiscreteDistribution:
        if not (math.isfinite(minutes) and 0.0 < minutes <= MAX_MATCH_MINUTES):
            raise ProjectionError(f"minutes={minutes!r} must be finite and in (0, {MAX_MATCH_MINUTES}]")
        exposure = minutes / 90.0
        return nbinom(self.gamma_shape, self.gamma_rate / (self.gamma_rate + exposure))

    def side_probabilities(
        self,
        line: float,
        side: str,
        void_rule: VoidRule | str = VoidRule.VOID_IF_NO_APPEARANCE,
    ) -> SideProbabilities:
        return _lineup_side_probabilities(
            self.lineup, line, side, void_rule, lambda mixture: mixture_line_split(self, mixture, line)
        )


def mixture_line_split(projection: PropProjection, mixture: MinutesMixture, line: float) -> tuple[float, float, float]:
    """(P(over), P(under), P(push)) averaged over a minutes mixture; a one-point mixture is the plain split."""
    parts = [_line_split(projection.count_distribution(m), line) for m in mixture.minutes]
    return (
        math.fsum(w * p[0] for w, p in zip(mixture.weights, parts)),
        math.fsum(w * p[1] for w, p in zip(mixture.weights, parts)),
        math.fsum(w * p[2] for w, p in zip(mixture.weights, parts)),
    )


def game_state_quadrature(
    k: float, n_nodes: int = GAME_STATE_QUADRATURE_NODES
) -> tuple[np.ndarray, np.ndarray]:
    """Nodes g_j and weights w_j with sum_j w_j f(g_j) ~= E[f(G)] for G ~ Gamma(k, rate=k).

    Generalised Gauss-Laguerre by Golub-Welsch: the nodes are the eigenvalues of the Laguerre Jacobi
    matrix and the normalised weights are the squared first eigenvector components. No gamma function
    is evaluated, so the rule stays finite for large k where scipy's roots_genlaguerre overflows (k > ~170).
    """
    if not (math.isfinite(k) and k > 0.0):
        raise ProjectionError(f"game_state_k={k!r} must be finite and positive")
    if isinstance(n_nodes, bool) or not isinstance(n_nodes, int) or n_nodes < 1:
        raise ValueError("n_nodes must be a positive integer")
    index = np.arange(n_nodes, dtype=np.float64)
    upper = np.arange(1, n_nodes, dtype=np.float64)
    roots, vectors = eigh_tridiagonal(2.0 * index + k, np.sqrt(upper * (upper + k - 1.0)))
    raw_weights = vectors[0] ** 2
    total = math.fsum(raw_weights)
    nodes = roots / k
    if not (np.isfinite(nodes).all() and (nodes > 0.0).all() and math.isfinite(total) and total > 0.0):
        raise ProjectionError(f"game-state quadrature is not finite for k={k!r}")
    return nodes, raw_weights / total


@dataclass(frozen=True)
class FactorProjection:
    """Player rate Lambda ~ Gamma(alpha, beta) per 90, times a match factor G ~ Gamma(k, k) shared by the fixture.

    Given G = g the count is NB(alpha, beta / (beta + g t)), and players in one fixture are independent.
    Keeping the posterior and k apart (rather than one moment-matched Gamma) lets the allocator price the
    covariance between players in the same match. `rate_posterior` already includes any referee or card
    multiplier and its uncertainty (QuantEngine.rate_posterior).
    """

    fixture_id: int
    player_id: int
    market_type: str
    rate_posterior: GammaPosterior
    game_state_k: float
    lineup: LineupExpectation

    def __post_init__(self) -> None:
        if not (math.isfinite(self.game_state_k) and self.game_state_k > 0.0):
            raise ProjectionError(f"game_state_k={self.game_state_k!r} must be finite and positive")

    @property
    def rate_per90(self) -> float:
        return self.rate_posterior.alpha / self.rate_posterior.beta

    def conditional(self, g: float) -> PropProjection:
        """The projection given G = g: shape alpha, rate beta / g."""
        if not (math.isfinite(g) and g > 0.0):
            raise ProjectionError(f"game-state factor g={g!r} must be finite and positive")
        return PropProjection(
            self.fixture_id,
            self.player_id,
            self.market_type,
            self.rate_posterior.alpha,
            self.rate_posterior.beta / g,
            self.lineup,
        )

    def _mixture_split(self, mixture: MinutesMixture, line: float) -> tuple[float, float, float]:
        nodes, weights = game_state_quadrature(self.game_state_k)
        alpha, beta = self.rate_posterior.alpha, self.rate_posterior.beta
        if not (math.isfinite(alpha) and alpha > 0.0 and math.isfinite(beta) and beta > 0.0):
            raise ProjectionError(f"rate posterior ({alpha!r}, {beta!r}) must be finite and positive")
        over: list[float] = []
        under: list[float] = []
        push: list[float] = []
        for minutes, minutes_weight in zip(mixture.minutes, mixture.weights):
            p_over, p_under, p_push = _line_masses(nbinom(alpha, beta / (beta + nodes * (minutes / 90.0))), line)
            if not (np.isfinite(p_over).all() and np.isfinite(p_under).all() and np.isfinite(p_push).all()):
                raise ProjectionError("conditional count probabilities are not finite")
            over.append(minutes_weight * float(weights @ p_over))
            under.append(minutes_weight * float(weights @ p_under))
            push.append(minutes_weight * float(weights @ p_push))
        return math.fsum(over), math.fsum(under), math.fsum(push)

    def side_probabilities(
        self,
        line: float,
        side: str,
        void_rule: VoidRule | str = VoidRule.VOID_IF_NO_APPEARANCE,
    ) -> SideProbabilities:
        return _lineup_side_probabilities(
            self.lineup, line, side, void_rule, lambda mixture: self._mixture_split(mixture, line)
        )


def _poisson_log_term(rate: float, count: int) -> float:
    if count == 0:
        return 0.0
    if rate == 0.0:
        return -math.inf
    return count * math.log(rate) - math.lgamma(count + 1)


def _validate_poisson_rates(*rates: float) -> None:
    if not all(math.isfinite(rate) and rate >= 0.0 for rate in rates):
        raise ValueError("bivariate Poisson rates must be finite and non-negative")


class QuantEngine:
    def __init__(self, *, game_state_k: float, beta1: float = 1.0, beta2: float = 1.0) -> None:
        """`game_state_k` is required: without it the engine would price established players as near-Poisson."""
        if not (math.isfinite(beta1) and beta1 > 0.0 and math.isfinite(beta2) and beta2 > 0.0):
            raise ValueError("referee exponents beta1 and beta2 must be finite and positive")
        if not (math.isfinite(game_state_k) and game_state_k > 0.0):
            raise ValueError("game_state_k must be fitted and passed explicitly (finite, positive)")
        self.beta1 = beta1
        self.beta2 = beta2
        self.game_state_k = game_state_k

    @staticmethod
    def bivariate_poisson_pmf(
        count_x: int, count_y: int, lambda_x: float, lambda_y: float, lambda_shared: float
    ) -> float:
        if count_x < 0 or count_y < 0:
            return 0.0
        _validate_poisson_rates(lambda_x, lambda_y, lambda_shared)
        base = -(lambda_x + lambda_y + lambda_shared)
        log_terms = [
            base
            + _poisson_log_term(lambda_x, count_x - shared_count)
            + _poisson_log_term(lambda_y, count_y - shared_count)
            + _poisson_log_term(lambda_shared, shared_count)
            for shared_count in range(min(count_x, count_y) + 1)
        ]
        finite_terms = [term for term in log_terms if term != -math.inf]
        if not finite_terms:
            return 0.0
        peak = max(finite_terms)
        return math.exp(peak) * math.fsum(math.exp(term - peak) for term in finite_terms)

    @staticmethod
    def bivariate_from_moments(mean_x: float, mean_y: float, covariance: float) -> tuple[float, float, float]:
        if not all(math.isfinite(value) for value in (mean_x, mean_y, covariance)):
            raise ValueError("bivariate Poisson moments must be finite")
        if mean_x <= 0.0 or mean_y <= 0.0 or not 0.0 <= covariance < min(mean_x, mean_y):
            raise ValueError("means must be positive and covariance must lie in [0, min(mean_x, mean_y))")
        return mean_x - covariance, mean_y - covariance, covariance

    @staticmethod
    def bivariate_correlation(lambda_x: float, lambda_y: float, lambda_shared: float) -> float:
        _validate_poisson_rates(lambda_x, lambda_y, lambda_shared)
        denominator = math.sqrt((lambda_x + lambda_shared) * (lambda_y + lambda_shared))
        if denominator <= 0.0:
            raise ValueError("bivariate Poisson marginals must have positive means")
        return lambda_shared / denominator

    @staticmethod
    def sample_bivariate_poisson(
        lambda_x: float,
        lambda_y: float,
        lambda_shared: float,
        n_samples: int,
        rng: np.random.Generator,
    ) -> tuple[np.ndarray, np.ndarray]:
        _validate_poisson_rates(lambda_x, lambda_y, lambda_shared)
        if n_samples < 0:
            raise ValueError("n_samples must be non-negative")
        shared = rng.poisson(lambda_shared, size=n_samples)
        return shared + rng.poisson(lambda_x, size=n_samples), shared + rng.poisson(lambda_y, size=n_samples)

    def joint_over_probability(
        self, line_x: float, line_y: float, lambda_x: float, lambda_y: float, lambda_shared: float
    ) -> float:
        """Return the joint probability that both bivariate-Poisson counts exceed half lines."""
        for line in (line_x, line_y):
            _validate_line(line)
            if float(line).is_integer():
                raise ValueError("joint_over_probability expects half lines")
        _validate_poisson_rates(lambda_x, lambda_y, lambda_shared)
        threshold_x = math.floor(line_x)
        threshold_y = math.floor(line_y)
        joint_cdf = math.fsum(
            self.bivariate_poisson_pmf(count_x, count_y, lambda_x, lambda_y, lambda_shared)
            for count_x in range(threshold_x + 1)
            for count_y in range(threshold_y + 1)
        )
        marginal_x = float(poisson.cdf(threshold_x, lambda_x + lambda_shared))
        marginal_y = float(poisson.cdf(threshold_y, lambda_y + lambda_shared))
        return 1.0 - marginal_x - marginal_y + joint_cdf

    def referee_multiplier(self, referee: RefereeProfile, is_home: bool) -> float:
        venue_multiplier = (1.0 - referee.home_bias_ratio) if is_home else (1.0 + referee.home_bias_ratio)
        multiplier = referee.strictness_coefficient**self.beta1 * venue_multiplier**self.beta2
        if not (math.isfinite(multiplier) and multiplier > 0.0):
            raise ProjectionError("referee multiplier must be finite and positive")
        return multiplier

    @staticmethod
    def positional_prior(league_rate_per90: float, prior_strength_90s: float = 6.0) -> GammaPosterior:
        if not (
            math.isfinite(league_rate_per90)
            and league_rate_per90 > 0.0
            and math.isfinite(prior_strength_90s)
            and prior_strength_90s > 0.0
        ):
            raise ValueError("league rate and prior strength must be finite and positive")
        return GammaPosterior(alpha=prior_strength_90s * league_rate_per90, beta=prior_strength_90s)

    @staticmethod
    def gamma_poisson_update(
        prior: GammaPosterior, counts: Sequence[int], minutes: Sequence[float],
        exposure_scale: Sequence[float] | None = None,
    ) -> GammaPosterior:
        """Conjugate update: alpha + sum(counts), beta + sum(minutes x scale) / 90. `exposure_scale` weights each
        match's minutes (the league level of its season over today's, modeling.league_level.level_ratios); the
        minutes themselves are validated as played, so a weight above 1 never fails the MAX_MATCH_MINUTES check."""
        np_minutes, np_counts = validate_exposure_vector(minutes, counts, allow_empty=True)
        if exposure_scale is not None:
            scale = np.asarray(exposure_scale, dtype=np.float64)
            if scale.shape != np_minutes.shape or not np.isfinite(scale).all() or (scale <= 0.0).any():
                raise ValueError("exposure_scale needs one finite, positive weight per match")
            np_minutes = np_minutes * scale
        return GammaPosterior(
            alpha=prior.alpha + float(np_counts.sum()),
            beta=prior.beta + float(np_minutes.sum()) / 90.0,
        )

    @staticmethod
    def _validated_mean_rate(
        posterior: GammaPosterior, multiplier: float, multiplier_relative_variance: float
    ) -> float:
        if not (math.isfinite(multiplier) and multiplier > 0.0):
            raise ProjectionError(f"referee/card multiplier={multiplier!r} must be finite and positive")
        if not (math.isfinite(multiplier_relative_variance) and multiplier_relative_variance >= 0.0):
            raise ProjectionError("multiplier_relative_variance must be finite and non-negative")
        if not (math.isfinite(posterior.alpha) and math.isfinite(posterior.beta)):
            raise ProjectionError("posterior parameters must be finite")
        mean_rate = multiplier * posterior.alpha / posterior.beta
        if not (math.isfinite(mean_rate) and mean_rate > 0.0):
            raise ProjectionError(f"projected rate mu={mean_rate!r} must be finite and positive")
        return mean_rate

    def _gamma_from_relative_variance(
        self, relative_variance: float, mean_rate: float, posterior: GammaPosterior, multiplier: float
    ) -> tuple[float, float]:
        """(shape, rate) for relative variance v and mean mu. v above MAX_RELATIVE_VARIANCE raises; v below
        MIN_RELATIVE_VARIANCE is the one clamp (AGENT F5) and logs [MATH_CLAMP]."""
        if not math.isfinite(relative_variance) or relative_variance > MAX_RELATIVE_VARIANCE:
            raise ProjectionError(
                f"relative variance {relative_variance!r} exceeds {MAX_RELATIVE_VARIANCE}; prior or history is degenerate"
            )
        bounded_variance = max(relative_variance, MIN_RELATIVE_VARIANCE)
        if bounded_variance != relative_variance:
            logger.warning(
                "[MATH_CLAMP] relative_variance=%r alpha=%r beta=%r k=%r multiplier=%r",
                relative_variance,
                posterior.alpha,
                posterior.beta,
                self.game_state_k,
                multiplier,
            )
        gamma_shape = 1.0 / bounded_variance
        gamma_rate = gamma_shape / mean_rate
        if not (math.isfinite(gamma_rate) and gamma_rate > 0.0):
            raise ProjectionError(f"derived gamma rate={gamma_rate!r} is invalid")
        return gamma_shape, gamma_rate

    def predictive_gamma(
        self, posterior: GammaPosterior, multiplier: float, multiplier_relative_variance: float
    ) -> tuple[float, float]:
        """Moment-matched (shape, rate) for c * Lambda * G.

        For independent factors Var/mu^2 = (1 + 1/alpha)(1 + 1/k)(1 + v_c) - 1, formed with log1p/expm1 so
        no moments are subtracted (gate G1). With v_c = 0 it equals 1/alpha + 1/k + 1/(alpha k).
        """
        mean_rate = self._validated_mean_rate(posterior, multiplier, multiplier_relative_variance)
        relative_variance = math.expm1(
            math.log1p(1.0 / posterior.alpha)
            + math.log1p(1.0 / self.game_state_k)
            + math.log1p(multiplier_relative_variance)
        )
        return self._gamma_from_relative_variance(relative_variance, mean_rate, posterior, multiplier)

    def rate_posterior(
        self, posterior: GammaPosterior, multiplier: float, multiplier_relative_variance: float
    ) -> GammaPosterior:
        """Gamma moment-matched to c * Lambda alone (no game-state factor), for FactorProjection.

        Var/mu^2 = (1 + 1/alpha)(1 + v_c) - 1 via log1p/expm1; same bounds and clamp as predictive_gamma.
        """
        mean_rate = self._validated_mean_rate(posterior, multiplier, multiplier_relative_variance)
        relative_variance = math.expm1(math.log1p(1.0 / posterior.alpha) + math.log1p(multiplier_relative_variance))
        shape, rate = self._gamma_from_relative_variance(relative_variance, mean_rate, posterior, multiplier)
        return GammaPosterior(alpha=shape, beta=rate)

    def _count_inputs(
        self,
        market_type: str,
        counts: Sequence[int],
        minutes: Sequence[float],
        league_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        prior_strength_90s: float,
        exposure_scale: Sequence[float] | None = None,
    ) -> tuple[GammaPosterior, float]:
        posterior = self.gamma_poisson_update(
            self.positional_prior(league_rate_per90, prior_strength_90s), counts, minutes, exposure_scale
        )
        multiplier = (
            self.referee_multiplier(referee, is_home)
            if market_type in REFEREE_SENSITIVE_MARKETS
            else 1.0
        )
        return posterior, multiplier

    def _card_inputs(
        self,
        foul_counts: Sequence[int],
        minutes: Sequence[float],
        league_foul_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        card_type: str,
        player_card_factor: float,
        prior_strength_90s: float,
    ) -> tuple[GammaPosterior, float]:
        if card_type not in ("yellow", "red"):
            raise ValueError("card_type must be 'yellow' or 'red'")
        if not (math.isfinite(player_card_factor) and player_card_factor > 0.0):
            raise ValueError("player_card_factor must be finite and positive")
        posterior = self.gamma_poisson_update(
            self.positional_prior(league_foul_rate_per90, prior_strength_90s), foul_counts, minutes
        )
        propensity = referee.yellow_card_propensity if card_type == "yellow" else referee.red_card_propensity
        if not (math.isfinite(propensity) and propensity > 0.0):
            raise ProjectionError("referee card propensity must be finite and positive to price a card market")
        referee_factor = self.referee_multiplier(referee, is_home)
        return posterior, referee_factor * propensity * player_card_factor

    def project_count_market(
        self,
        *,
        fixture_id: int,
        player_id: int,
        market_type: str,
        counts: Sequence[int],
        minutes: Sequence[float],
        league_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        lineup: LineupExpectation,
        prior_strength_90s: float = 6.0,
        exposure_scale: Sequence[float] | None = None,
    ) -> PropProjection:
        posterior, multiplier = self._count_inputs(
            market_type, counts, minutes, league_rate_per90, referee, is_home, prior_strength_90s, exposure_scale
        )
        gamma_shape, gamma_rate = self.predictive_gamma(posterior, multiplier, 0.0)
        return PropProjection(fixture_id, player_id, market_type, gamma_shape, gamma_rate, lineup)

    def factor_count_market(
        self,
        *,
        fixture_id: int,
        player_id: int,
        market_type: str,
        counts: Sequence[int],
        minutes: Sequence[float],
        league_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        lineup: LineupExpectation,
        prior_strength_90s: float = 6.0,
        exposure_scale: Sequence[float] | None = None,
    ) -> FactorProjection:
        """As project_count_market, but keeps the posterior and k apart for fixture-level covariance."""
        posterior, multiplier = self._count_inputs(
            market_type, counts, minutes, league_rate_per90, referee, is_home, prior_strength_90s, exposure_scale
        )
        return FactorProjection(
            fixture_id, player_id, market_type, self.rate_posterior(posterior, multiplier, 0.0), self.game_state_k, lineup
        )

    def project_card_market(
        self,
        *,
        fixture_id: int,
        player_id: int,
        foul_counts: Sequence[int],
        minutes: Sequence[float],
        league_foul_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        lineup: LineupExpectation,
        card_multiplier_relative_variance: float,
        card_type: str = "yellow",
        player_card_factor: float = 1.0,
        prior_strength_90s: float = 6.0,
    ) -> PropProjection:
        """`card_multiplier_relative_variance` is the relative variance of referee factor x propensity x
        player factor. It is required until calibration estimates it from referee sample sizes."""
        posterior, multiplier = self._card_inputs(
            foul_counts, minutes, league_foul_rate_per90, referee, is_home, card_type, player_card_factor,
            prior_strength_90s,
        )
        gamma_shape, gamma_rate = self.predictive_gamma(posterior, multiplier, card_multiplier_relative_variance)
        return PropProjection(fixture_id, player_id, f"{card_type}_card", gamma_shape, gamma_rate, lineup)

    def factor_card_market(
        self,
        *,
        fixture_id: int,
        player_id: int,
        foul_counts: Sequence[int],
        minutes: Sequence[float],
        league_foul_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        lineup: LineupExpectation,
        card_multiplier_relative_variance: float,
        card_type: str = "yellow",
        player_card_factor: float = 1.0,
        prior_strength_90s: float = 6.0,
    ) -> FactorProjection:
        """As project_card_market, with the multiplier uncertainty folded into the rate posterior."""
        posterior, multiplier = self._card_inputs(
            foul_counts, minutes, league_foul_rate_per90, referee, is_home, card_type, player_card_factor,
            prior_strength_90s,
        )
        return FactorProjection(
            fixture_id,
            player_id,
            f"{card_type}_card",
            self.rate_posterior(posterior, multiplier, card_multiplier_relative_variance),
            self.game_state_k,
            lineup,
        )

    @staticmethod
    def player_card_factor(
        player_cards: int, player_fouls: int, league_cards_per_foul: float, prior_fouls: float = 40.0
    ) -> float:
        if player_cards < 0 or player_fouls < 0:
            raise ValueError("player card and foul counts must be non-negative")
        if not (
            math.isfinite(league_cards_per_foul)
            and league_cards_per_foul > 0.0
            and math.isfinite(prior_fouls)
            and prior_fouls > 0.0
        ):
            raise ValueError("league card rate and prior fouls must be finite and positive")
        shrunk_rate = (player_cards + prior_fouls * league_cards_per_foul) / (player_fouls + prior_fouls)
        return shrunk_rate / league_cards_per_foul