"""Exact joint mean-variance allocation over live player-prop quotes."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Final, Hashable, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize

from prop_alpha.modeling.engine import (
    LineupExpectation,
    ProjectionError,
    PropProjection,
    SideProbabilities,
    VoidRule,
)

logger = logging.getLogger("prop_alpha.scanner")
ProjectionKey = tuple[int, int, str]
DEGENERATE_LOSS_PROBABILITY: Final[float] = 1e-6


@dataclass(frozen=True)
class LiveOddsQuote:
    fixture_id: int
    player_id: int
    market_type: str
    line_value: float
    over_price: float | None
    under_price: float | None
    max_stake: float
    sportsbook: str
    captured_at: datetime
    void_rule: VoidRule | str = VoidRule.VOID_IF_NO_APPEARANCE

    def __post_init__(self) -> None:
        if not isinstance(self.captured_at, datetime) or self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be a timezone-aware UTC datetime")
        if self.captured_at.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be UTC")
        if not math.isfinite(self.line_value) or self.line_value < 0.0 or not (self.line_value * 2.0).is_integer():
            raise ValueError("line_value must be a finite non-negative whole or half line")
        for side, price in (("over", self.over_price), ("under", self.under_price)):
            if price is not None and not math.isfinite(price):
                raise ValueError(f"{side}_price must be finite when present")
        if not math.isfinite(self.max_stake) or self.max_stake < 0.0:
            raise ValueError("max_stake must be finite and non-negative")
        if not self.sportsbook:
            raise ValueError("sportsbook must not be empty")
        object.__setattr__(self, "void_rule", VoidRule(self.void_rule))


@dataclass(frozen=True)
class StakingDirective:
    fixture_id: int
    player_id: int
    market_type: str
    line_value: float
    side: str
    sportsbook: str
    void_rule: str
    decimal_odds: float
    p_win: float
    p_lose: float
    p_void: float
    fair_market_prob: float | None
    ev_per_unit: float
    return_variance_per_unit: float
    kelly_full: float
    fraction: float
    stake: float


@dataclass(frozen=True)
class MarketAllocation:
    key: ProjectionKey
    n_candidates: int
    expected_return: float
    return_variance: float
    utility: float
    solver: str


@dataclass
class AllocationReport:
    directives: list[StakingDirective] = field(default_factory=list)
    skipped_directives: list[StakingDirective] = field(default_factory=list)
    allocations: list[MarketAllocation] = field(default_factory=list)
    skipped: list[tuple[ProjectionKey, str]] = field(default_factory=list)
    stale_quotes: int = 0


@dataclass(frozen=True)
class Candidate:
    quote: LiveOddsQuote
    side: str
    price: float
    probs: SideProbabilities
    fair_prob: float | None
    ev: float
    kelly_full: float

    @property
    def void_rule(self) -> VoidRule:
        return VoidRule(self.quote.void_rule)


def devig_two_way(price_a: float, price_b: float) -> tuple[float, float]:
    if not (math.isfinite(price_a) and math.isfinite(price_b) and price_a > 1.0 and price_b > 1.0):
        raise ValueError("decimal prices must be finite and exceed 1.0")
    inverse_a, inverse_b = 1.0 / price_a, 1.0 / price_b
    total = inverse_a + inverse_b
    return inverse_a / total, inverse_b / total


def expected_value(probabilities: SideProbabilities, decimal_odds: float) -> float:
    if not (math.isfinite(decimal_odds) and decimal_odds > 1.0):
        raise ValueError("decimal odds must be finite and exceed 1.0")
    return probabilities.p_win * (decimal_odds - 1.0) - probabilities.p_lose


def kelly_fraction_with_push(probabilities: SideProbabilities, decimal_odds: float) -> float:
    net_odds = decimal_odds - 1.0
    settled_probability = probabilities.p_win + probabilities.p_lose
    if net_odds <= 0.0 or settled_probability <= 0.0:
        return 0.0
    return max(
        0.0,
        (probabilities.p_win * net_odds - probabilities.p_lose)
        / (net_odds * settled_probability),
    )


def _payoff(candidate: Candidate, scenario: str, count: int) -> float:
    if scenario == "absent" and candidate.void_rule is not VoidRule.NO_VOID:
        return 0.0
    if scenario == "sub" and candidate.void_rule is VoidRule.VOID_IF_NOT_STARTING:
        return 0.0
    if count == candidate.quote.line_value:
        return 0.0
    over_wins = count > candidate.quote.line_value
    wins = over_wins if candidate.side == "over" else not over_wins
    return candidate.price - 1.0 if wins else -1.0


def outcome_model(
    projection: PropProjection, candidates: Sequence[Candidate]
) -> tuple[np.ndarray, np.ndarray]:
    """Build exact scenario/count probabilities and per-unit returns, including one exact tail bucket."""
    if not candidates:
        raise ValueError("no candidates")
    lineup: LineupExpectation = projection.lineup
    top_count = math.floor(max(candidate.quote.line_value for candidate in candidates)) + 1
    probabilities: list[float] = []
    payoffs: list[list[float]] = []
    scenarios: tuple[tuple[str, float, float | None], ...] = (
        ("start", lineup.p_start, lineup.exp_minutes_start),
        ("sub", lineup.p_sub_appearance, lineup.exp_minutes_sub),
        ("absent", lineup.p_no_appearance, None),
    )
    for scenario, scenario_weight, minutes in scenarios:
        if scenario_weight == 0.0:
            continue
        if minutes is None:
            counts = np.array([0], dtype=np.int64)
            masses = np.array([1.0], dtype=np.float64)
        else:
            distribution = projection.count_distribution(minutes)
            counts = np.arange(top_count + 1)
            masses = np.append(distribution.pmf(counts[:-1]), distribution.sf(top_count - 1))
        for count, mass in zip(counts, masses):
            if mass <= 0.0:
                continue
            probabilities.append(scenario_weight * float(mass))
            payoffs.append([_payoff(candidate, scenario, int(count)) for candidate in candidates])
    probability_array = np.asarray(probabilities, dtype=np.float64)
    payoff_array = np.asarray(payoffs, dtype=np.float64)
    if not math.isclose(float(probability_array.sum()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise FloatingPointError("joint outcome probabilities do not sum to 1")
    return probability_array, payoff_array


def return_moments(probabilities: np.ndarray, payoffs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Compute exact grid moments; on the documented reference case kappa=1+mu^2/var is about 1.005.

    The subtraction form is limited to per-unit returns from the exact small outcome grid. It is
    permitted here by AGENT.md gate G1; engine.py uses the stable relative-variance sum instead.
    """
    if probabilities.ndim != 1 or payoffs.ndim != 2 or payoffs.shape[0] != probabilities.size:
        raise ValueError("probabilities and payoff grid have incompatible dimensions")
    if not (np.isfinite(probabilities).all() and np.isfinite(payoffs).all()):
        raise ValueError("probabilities and payoffs must be finite")
    if (probabilities < 0.0).any() or not math.isclose(
        float(probabilities.sum()), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("outcome probabilities must be non-negative and sum to 1")
    mean_returns = probabilities @ payoffs
    second_moment = payoffs.T @ (payoffs * probabilities[:, None])
    covariance = second_moment - np.outer(mean_returns, mean_returns)
    covariance = 0.5 * (covariance + covariance.T)
    smallest_eigenvalue = float(np.linalg.eigvalsh(covariance).min()) if covariance.size else 0.0
    if smallest_eigenvalue < 0.0:
        covariance = covariance + np.eye(len(mean_returns)) * -smallest_eigenvalue
    return mean_returns, covariance


def solve_mean_variance(
    mu: np.ndarray,
    cov: np.ndarray,
    upper: np.ndarray,
    budget: float,
    risk_aversion: float,
) -> tuple[np.ndarray, str]:
    """Solve the concave joint allocation and never return less utility than the best single position."""
    n_candidates = len(mu)
    if cov.shape != (n_candidates, n_candidates) or upper.shape != (n_candidates,):
        raise ValueError("mean, covariance, and upper-bound dimensions do not match")
    if not (
        np.isfinite(mu).all()
        and np.isfinite(cov).all()
        and np.isfinite(upper).all()
        and math.isfinite(budget)
        and math.isfinite(risk_aversion)
    ):
        raise ValueError("optimizer inputs must be finite")
    if (upper < 0.0).any() or budget < 0.0 or risk_aversion <= 0.0:
        raise ValueError("upper bounds and budget must be non-negative; risk_aversion must be positive")

    def utility(fractions: np.ndarray) -> float:
        return float(fractions @ mu - risk_aversion * fractions @ cov @ fractions)

    best_single = np.zeros(n_candidates, dtype=np.float64)
    best_single_utility = 0.0
    for candidate_index in range(n_candidates):
        if mu[candidate_index] <= 0.0 or upper[candidate_index] <= 0.0:
            continue
        cap = min(float(upper[candidate_index]), budget)
        variance = float(cov[candidate_index, candidate_index])
        fraction = cap if variance <= 0.0 else min(cap, mu[candidate_index] / (2.0 * risk_aversion * variance))
        trial = np.zeros(n_candidates, dtype=np.float64)
        trial[candidate_index] = fraction
        trial_utility = utility(trial)
        if trial_utility > best_single_utility:
            best_single, best_single_utility = trial, trial_utility

    if n_candidates <= 1:
        return best_single, "single"

    result = minimize(
        lambda fractions: -utility(fractions),
        x0=best_single,
        jac=lambda fractions: -(mu - 2.0 * risk_aversion * cov @ fractions),
        method="SLSQP",
        bounds=[(0.0, float(bound)) for bound in upper],
        constraints=[
            {
                "type": "ineq",
                "fun": lambda fractions: budget - float(fractions.sum()),
                "jac": lambda fractions: -np.ones(n_candidates),
            }
        ],
        options={"ftol": 1e-14, "maxiter": 500},
    )
    if result.success:
        weights = np.clip(result.x, 0.0, upper)
        total_weight = float(weights.sum())
        if total_weight > budget:
            weights *= budget / total_weight
        if utility(weights) >= best_single_utility - 1e-12:
            return weights, "qp"
    logger.warning("mean-variance QP did not improve on best single bet (%s); using single", result.message)
    return best_single, "single"


class InstitutionalAssetManager:
    def __init__(
        self,
        bankroll: float,
        risk_aversion: float = 2.0,
        alpha_threshold: float = 0.02,
        max_position_fraction: float = 0.02,
        max_fixture_fraction: float = 0.05,
        max_total_fraction: float = 0.20,
        max_quote_age: timedelta = timedelta(minutes=5),
        open_exposure: float = 0.0,
        min_stake: float = 1.0,
    ) -> None:
        if not (math.isfinite(bankroll) and bankroll > 0.0):
            raise ValueError("bankroll must be finite and positive")
        if not (math.isfinite(risk_aversion) and risk_aversion > 0.0):
            raise ValueError("risk_aversion must be finite and positive")
        if not (math.isfinite(alpha_threshold) and alpha_threshold >= 0.0):
            raise ValueError("alpha_threshold must be finite and non-negative")
        for name, value in (
            ("max_position_fraction", max_position_fraction),
            ("max_fixture_fraction", max_fixture_fraction),
            ("max_total_fraction", max_total_fraction),
        ):
            if not (math.isfinite(value) and 0.0 < value <= 1.0):
                raise ValueError(f"{name} must lie in (0, 1]")
        if not (math.isfinite(open_exposure) and open_exposure >= 0.0):
            raise ValueError("open_exposure must be finite and non-negative")
        if not (math.isfinite(min_stake) and min_stake >= 0.0):
            raise ValueError("min_stake must be finite and non-negative")
        if max_quote_age < timedelta(0):
            raise ValueError("max_quote_age must be non-negative")
        self.bankroll = bankroll
        self.risk_aversion = risk_aversion
        self.alpha_threshold = alpha_threshold
        self.max_position_fraction = max_position_fraction
        self.max_fixture_fraction = max_fixture_fraction
        self.max_total_fraction = max_total_fraction
        self.max_quote_age = max_quote_age
        self.open_exposure = open_exposure
        self.min_stake = min_stake

    def allocate(
        self,
        projections: Mapping[ProjectionKey, PropProjection],
        quotes: Iterable[LiveOddsQuote],
        now: datetime | None = None,
    ) -> AllocationReport:
        if now is None or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be provided as a timezone-aware UTC datetime")
        report = AllocationReport()
        grouped_quotes: dict[ProjectionKey, list[LiveOddsQuote]] = defaultdict(list)
        for quote in quotes:
            if quote.captured_at > now:
                raise ValueError("quote captured_at cannot be later than now")
            if now - quote.captured_at > self.max_quote_age:
                report.stale_quotes += 1
                continue
            grouped_quotes[(quote.fixture_id, quote.player_id, quote.market_type)].append(quote)

        positions: list[tuple[Candidate, float, float]] = []
        for key, market_quotes in grouped_quotes.items():
            projection = projections.get(key)
            if projection is None:
                report.skipped.append((key, "no projection"))
                continue
            try:
                candidates, candidate_skips = self._candidates(projection, market_quotes)
                report.skipped.extend((key, reason) for reason in candidate_skips)
                if not candidates:
                    if not candidate_skips:
                        report.skipped.append((key, f"no candidate above alpha={self.alpha_threshold}"))
                    continue
                probabilities, payoffs = outcome_model(projection, candidates)
                mean_returns, covariance = return_moments(probabilities, payoffs)
            except (ProjectionError, ValueError, FloatingPointError) as exc:
                report.skipped.append((key, f"projection unusable: {exc}"))
                logger.error("market %s skipped, projection unusable: %s", key, exc)
                continue

            upper = np.array(
                [min(candidate.kelly_full, self.max_position_fraction) for candidate in candidates],
                dtype=np.float64,
            )
            weights, solver = solve_mean_variance(
                mean_returns,
                covariance,
                upper,
                self.max_position_fraction,
                self.risk_aversion,
            )
            if weights.sum() <= 0.0:
                report.skipped.append((key, "optimizer allocated nothing"))
                continue
            report.allocations.append(
                MarketAllocation(
                    key=key,
                    n_candidates=len(candidates),
                    expected_return=float(weights @ mean_returns),
                    return_variance=float(weights @ covariance @ weights),
                    utility=float(weights @ mean_returns - self.risk_aversion * weights @ covariance @ weights),
                    solver=solver,
                )
            )
            for candidate_index, candidate in enumerate(candidates):
                if weights[candidate_index] > 0.0:
                    positions.append((candidate, float(weights[candidate_index]), float(covariance[candidate_index, candidate_index])))

        fractions = [fraction for _, fraction, _ in positions]
        fractions = self._apply_group_cap(
            positions, fractions, lambda candidate: candidate.quote.fixture_id, self.max_fixture_fraction
        )
        fractions = self._apply_group_cap(positions, fractions, lambda candidate: "slate", self.max_total_fraction)

        available_bankroll = self.bankroll - self.open_exposure
        if available_bankroll <= 0.0:
            logger.warning("no bankroll available after open exposure; no directives issued")
            return report
        directive_rows = sorted(
            zip(positions, fractions),
            key=lambda row: (-row[0][0].ev, row[0][0].quote.fixture_id, row[0][0].quote.line_value, row[0][0].side),
        )
        for (candidate, _, variance), fraction in directive_rows:
            raw_stake = min(fraction * available_bankroll, candidate.quote.max_stake)
            stake = math.floor(raw_stake * 100.0 + 1e-9) / 100.0
            directive = StakingDirective(
                fixture_id=candidate.quote.fixture_id,
                player_id=candidate.quote.player_id,
                market_type=candidate.quote.market_type,
                line_value=candidate.quote.line_value,
                side=candidate.side,
                sportsbook=candidate.quote.sportsbook,
                void_rule=candidate.void_rule.value,
                decimal_odds=candidate.price,
                p_win=round(candidate.probs.p_win, 5),
                p_lose=round(candidate.probs.p_lose, 5),
                p_void=round(candidate.probs.p_void, 5),
                fair_market_prob=None if candidate.fair_prob is None else round(candidate.fair_prob, 5),
                ev_per_unit=round(candidate.ev, 5),
                return_variance_per_unit=round(variance, 5),
                kelly_full=round(candidate.kelly_full, 6),
                fraction=round(fraction, 6),
                stake=stake,
            )
            if stake < self.min_stake:
                report.skipped_directives.append(directive)
                logger.info("sub-minimum stake skipped: %s", directive)
            else:
                report.directives.append(directive)
        return report

    def _candidates(
        self, projection: PropProjection, quotes: Sequence[LiveOddsQuote]
    ) -> tuple[list[Candidate], list[str]]:
        best: dict[tuple[float, str, VoidRule], Candidate] = {}
        skipped: list[str] = []
        for quote in quotes:
            fair_probabilities: dict[str, float] = {}
            over_price = quote.over_price
            under_price = quote.under_price
            if (
                over_price is not None
                and under_price is not None
                and math.isfinite(over_price)
                and math.isfinite(under_price)
                and over_price > 1.0
                and under_price > 1.0
            ):
                fair_probabilities["over"], fair_probabilities["under"] = devig_two_way(over_price, under_price)
            for side, price in (("over", over_price), ("under", under_price)):
                if price is None or not (math.isfinite(price) and price > 1.0):
                    continue
                side_probabilities = projection.side_probabilities(quote.line_value, side, quote.void_rule)
                if side_probabilities.p_lose <= DEGENERATE_LOSS_PROBABILITY:
                    skipped.append(f"degenerate outcome: {quote.line_value:g} {side}; manual review required")
                    continue
                ev = expected_value(side_probabilities, price)
                if ev <= self.alpha_threshold:
                    continue
                candidate = Candidate(
                    quote=quote,
                    side=side,
                    price=price,
                    probs=side_probabilities,
                    fair_prob=fair_probabilities.get(side),
                    ev=ev,
                    kelly_full=kelly_fraction_with_push(side_probabilities, price),
                )
                slot = (quote.line_value, side, VoidRule(quote.void_rule))
                current = best.get(slot)
                if current is None or candidate.price > current.price or (
                    candidate.price == current.price and candidate.quote.sportsbook < current.quote.sportsbook
                ):
                    best[slot] = candidate
        ordered = sorted(best.values(), key=lambda candidate: (candidate.quote.line_value, candidate.side, candidate.void_rule.value))
        return ordered, skipped

    @staticmethod
    def _apply_group_cap(
        positions: Sequence[tuple[Candidate, float, float]],
        fractions: Sequence[float],
        key_fn: Callable[[Candidate], Hashable],
        cap: float,
    ) -> list[float]:
        totals: dict[Hashable, float] = defaultdict(float)
        for (candidate, _, _), fraction in zip(positions, fractions):
            totals[key_fn(candidate)] += fraction
        return [
            fraction * (cap / totals[key_fn(candidate)]) if totals[key_fn(candidate)] > cap else fraction
            for (candidate, _, _), fraction in zip(positions, fractions)
        ]


def price_for_ev(probabilities: SideProbabilities, target_ev: float) -> float:
    if not (math.isfinite(target_ev) and probabilities.p_win > 0.0):
        raise ValueError("target EV must be finite and win probability must be positive")
    return 1.0 + (target_ev + probabilities.p_lose) / probabilities.p_win