"""High-value bet screen: mispriced, at least the minimum EV, and robust across simulated match conditions.

A staking directive is a high-value bet when all three hold:

1. Mispriced. The model's probability of winning, given the bet settles, exceeds the book's devigged
   probability for that side (or 1/odds when the book quotes one side only).
2. Minimum return. Expected profit per 1 unit staked is at least `min_ev_per_unit` (default 0.04).
3. Edge survives. In more than `min_survival_rate` (default 60%) of `n_simulations` (default 1000)
   simulated match conditions, the bet still returns at least `min_ev_per_unit` at the book's price.

A simulated match condition draws what the model has data for:
  * the player's situation: starts, comes on as a substitute, or does not appear (LineupExpectation),
    and, when the lineup carries a minutes mixture, how long the player stays on;
  * the match-level scoring rate lambda. For a FactorProjection it is Lambda * G, with the player's
    posterior rate Lambda ~ Gamma(alpha, beta) (including the referee or card multiplier and its
    uncertainty) and the shared game-state factor G ~ Gamma(k, k). For a PropProjection it is one
    moment-matched Gamma(shape, rate).
Given a condition, the count is Poisson(lambda * minutes / 90), so the bet's EV in that condition is
exact. Averaging the conditions recovers the projection's prices.
Weather, formation and fitness are not simulated: the model has no data for them. A condition in
which the bet is void (for example the player does not appear) has EV 0, so it never counts as
surviving.

The model-risk loss floor (`HighValueCriteria.loss_floor`, the same value as the allocator's) is applied
to the headline EV and to every simulated condition: at least that much win probability is moved to
loss. Without it the screen draws the rate from the projection it is testing, measures the model's
confidence in itself, and structurally prefers short-priced Unders.

Pure module: no I/O, no clock, and randomness only through the `rng` argument (AGENT 1.4 P5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import poisson

from prop_alpha.modeling.engine import FactorProjection, MinutesMixture, PropProjection, VoidRule
from prop_alpha.scanner.market_scanner import StakingDirective, stressed_probabilities


@dataclass(frozen=True)
class HighValueCriteria:
    loss_floor: float
    min_ev_per_unit: float = 0.04
    min_survival_rate: float = 0.60
    n_simulations: int = 1000

    def __post_init__(self) -> None:
        if not (math.isfinite(self.loss_floor) and 0.0 <= self.loss_floor < 1.0):
            raise ValueError("loss_floor must lie in [0, 1)")
        if not (math.isfinite(self.min_ev_per_unit) and self.min_ev_per_unit > 0.0):
            raise ValueError("min_ev_per_unit must be finite and positive")
        if not 0.0 < self.min_survival_rate < 1.0:
            raise ValueError("min_survival_rate must lie strictly inside (0, 1)")
        if isinstance(self.n_simulations, bool) or not isinstance(self.n_simulations, int) or self.n_simulations < 1:
            raise ValueError("n_simulations must be a positive integer")


@dataclass(frozen=True)
class HighValueAssessment:
    ev_per_unit: float
    model_win_probability: float
    book_win_probability: float
    mispriced: bool
    meets_min_ev: bool
    surviving_simulations: int
    n_simulations: int
    survives: bool

    @property
    def survival_rate(self) -> float:
        return self.surviving_simulations / self.n_simulations

    @property
    def is_high_value(self) -> bool:
        return self.mispriced and self.meets_min_ev and self.survives


def _conditional_returns(
    side: str, line: float, price: float, rates_per90: np.ndarray, minutes: np.ndarray, loss_floor: float
) -> np.ndarray:
    """EV per unit per rate, with Poisson(rate * minutes / 90) and at least `loss_floor` moved from win to loss."""
    means = rates_per90 * (minutes / 90.0)
    floor_line = math.floor(line)
    if float(line).is_integer():
        push = poisson.pmf(floor_line, means)
        under = poisson.cdf(floor_line - 1, means)
    else:
        push = np.zeros_like(means)
        under = poisson.cdf(floor_line, means)
    over = poisson.sf(floor_line, means)
    win, lose = (over, under) if side == "over" else (under, over)
    if not np.isfinite(win).all() or not np.isfinite(lose).all() or (push < 0.0).any():
        raise FloatingPointError("conditional outcome probabilities are not finite")
    shift = np.minimum(np.maximum(loss_floor - lose, 0.0), win)
    return (win - shift) * (price - 1.0) - (lose + shift)


def _absent_return(side: str, line: float, price: float, void_rule: VoidRule) -> float:
    if void_rule is not VoidRule.NO_VOID or line == 0.0:
        return 0.0
    return price - 1.0 if side == "under" else -1.0


def _sample_rates(
    projection: PropProjection | FactorProjection, n_simulations: int, rng: np.random.Generator
) -> np.ndarray:
    if isinstance(projection, FactorProjection):
        posterior, k = projection.rate_posterior, projection.game_state_k
        return rng.gamma(posterior.alpha, 1.0 / posterior.beta, size=n_simulations) * rng.gamma(
            k, 1.0 / k, size=n_simulations
        )
    return rng.gamma(projection.gamma_shape, 1.0 / projection.gamma_rate, size=n_simulations)


def _sample_minutes(mixture: MinutesMixture, size: int, rng: np.random.Generator) -> np.ndarray:
    """Minutes for `size` conditions; a one-point mixture consumes no random numbers."""
    if len(mixture.minutes) == 1:
        return np.full(size, mixture.minutes[0], dtype=np.float64)
    weights = np.asarray(mixture.weights, dtype=np.float64)
    return rng.choice(np.asarray(mixture.minutes, dtype=np.float64), size=size, p=weights / weights.sum())


def simulate_edge_survival(
    projection: PropProjection | FactorProjection,
    side: str,
    line: float,
    price: float,
    void_rule: VoidRule | str,
    min_ev_per_unit: float,
    n_simulations: int,
    rng: np.random.Generator,
    loss_floor: float,
) -> int:
    """Number of simulated match conditions in which EV per unit, after the loss floor, is at least
    `min_ev_per_unit`."""
    if side not in ("over", "under"):
        raise ValueError("side must be 'over' or 'under'")
    if not (math.isfinite(price) and price > 1.0):
        raise ValueError("price must be finite and exceed 1.0")
    if not math.isfinite(line) or line < 0.0 or not float(line * 2.0).is_integer():
        raise ValueError("line must be a non-negative whole or half line")
    if n_simulations < 1:
        raise ValueError("n_simulations must be positive")
    if not (math.isfinite(loss_floor) and 0.0 <= loss_floor < 1.0):
        raise ValueError("loss_floor must lie in [0, 1)")
    rule = VoidRule(void_rule)
    lineup = projection.lineup
    weights = np.array([lineup.p_start, lineup.p_sub_appearance, lineup.p_no_appearance], dtype=np.float64)
    scenarios = rng.choice(3, size=n_simulations, p=weights / weights.sum())
    rates = _sample_rates(projection, n_simulations, rng)

    returns = np.zeros(n_simulations, dtype=np.float64)
    start, sub, absent = scenarios == 0, scenarios == 1, scenarios == 2
    start_minutes = _sample_minutes(lineup.start_minutes, int(start.sum()), rng)
    returns[start] = _conditional_returns(side, line, price, rates[start], start_minutes, loss_floor)
    if rule is VoidRule.VOID_IF_NOT_STARTING:
        returns[sub] = 0.0
    else:
        sub_minutes = _sample_minutes(lineup.sub_minutes, int(sub.sum()), rng)
        returns[sub] = _conditional_returns(side, line, price, rates[sub], sub_minutes, loss_floor)
    returns[absent] = _absent_return(side, line, price, rule)
    return int(np.count_nonzero(returns >= min_ev_per_unit))


def assess_high_value(
    directive: StakingDirective,
    projection: PropProjection | FactorProjection,
    criteria: HighValueCriteria,
    rng: np.random.Generator,
) -> HighValueAssessment:
    """Apply the three-part high-value definition to one directive priced off `projection`."""
    key = (directive.fixture_id, directive.player_id, directive.market_type)
    if key != (projection.fixture_id, projection.player_id, projection.market_type):
        raise ValueError(f"projection {projection.fixture_id, projection.player_id, projection.market_type} "
                         f"does not price directive {key}")
    probabilities = projection.side_probabilities(directive.line_value, directive.side, directive.void_rule)
    settled = probabilities.p_win + probabilities.p_lose
    if settled <= 0.0:
        raise ValueError("the bet can never settle, so it cannot be assessed")
    model_win = probabilities.p_win / settled
    book_win = (
        directive.fair_market_prob if directive.fair_market_prob is not None else 1.0 / directive.decimal_odds
    )
    stressed = stressed_probabilities(probabilities, criteria.loss_floor)
    ev = stressed.p_win * (directive.decimal_odds - 1.0) - stressed.p_lose
    surviving = simulate_edge_survival(
        projection, directive.side, directive.line_value, directive.decimal_odds, directive.void_rule,
        criteria.min_ev_per_unit, criteria.n_simulations, rng, criteria.loss_floor,
    )
    return HighValueAssessment(
        ev_per_unit=ev,
        model_win_probability=model_win,
        book_win_probability=book_win,
        mispriced=model_win > book_win,
        meets_min_ev=ev >= criteria.min_ev_per_unit,
        surviving_simulations=surviving,
        n_simulations=criteria.n_simulations,
        survives=surviving / criteria.n_simulations > criteria.min_survival_rate,
    )
