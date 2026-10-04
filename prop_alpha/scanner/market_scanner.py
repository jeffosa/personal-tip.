"""Exact joint mean-variance allocation over live player-prop quotes, one problem per fixture."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize

from prop_alpha.modeling.engine import (
    FactorProjection,
    LineupExpectation,
    ProjectionError,
    PropProjection,
    SideProbabilities,
    VoidRule,
    game_state_quadrature,
)

logger = logging.getLogger("prop_alpha.scanner")
ProjectionKey = tuple[int, int, str]
DEGENERATE_LOSS_PROBABILITY: Final[float] = 1e-6

# Model-risk haircut: every side is priced as if it loses at least this often (maintainer decision 1,
# 2026-10-01). 0.03 removes the measured near-certain Under at 1.06 that took the full 2% cap. Not yet
# backtested: PRD Gate 3 must confirm it. The allocator and the value screen must use the same value.
MODEL_RISK_LOSS_FLOOR: Final[float] = 0.03


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
    void_rule: VoidRule | str
    # False when the book published no stake limit and max_stake is an assumption (oddspapi.UNKNOWN_LIMIT_STAKE).
    limit_published: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.limit_published, bool):
            raise ValueError("limit_published must be a bool")
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
    """One suggested stake. `min_acceptable_odds` is the price at which EV falls to alpha_threshold (rounded
    up to 0.001); nothing should be placed below it, nor at or after `expires_at`."""

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
    min_acceptable_odds: float
    expires_at: datetime
    # Copied from the quote: False means the stake was clipped to an assumed limit, not one the book published.
    limit_published: bool = True


@dataclass(frozen=True)
class FixtureAllocation:
    """Realised plan for one fixture, computed from the final fractions (after the slate cap)."""

    fixture_id: int
    n_markets: int
    n_candidates: int
    expected_return: float
    return_variance: float
    utility: float
    solver: str


@dataclass
class AllocationReport:
    directives: list[StakingDirective] = field(default_factory=list)
    skipped_directives: list[StakingDirective] = field(default_factory=list)
    allocations: list[FixtureAllocation] = field(default_factory=list)
    skipped: list[tuple[ProjectionKey, str]] = field(default_factory=list)
    stale_quotes: int = 0
    future_quotes: int = 0


@dataclass(frozen=True)
class Candidate:
    """One side of one book's quote. `probs`, `ev` and `kelly_full` are after the model-risk loss floor;
    `loss_shift` is the win mass the floor moved to loss."""

    quote: LiveOddsQuote
    side: str
    price: float
    probs: SideProbabilities
    fair_prob: float | None
    ev: float
    kelly_full: float
    loss_shift: float = 0.0

    @property
    def void_rule(self) -> VoidRule:
        return VoidRule(self.quote.void_rule)


def _is_projection_key(key: object) -> bool:
    return (
        isinstance(key, tuple)
        and len(key) == 3
        and all(isinstance(part, int) and not isinstance(part, bool) for part in key[:2])
        and isinstance(key[2], str)
        and bool(key[2])
    )


@dataclass(frozen=True)
class ExposureBook:
    """Currency already committed (filled or still pending) per market. Supplied by the bet ledger ([N])."""

    by_market: Mapping[ProjectionKey, float]

    def __post_init__(self) -> None:
        # Validate a private copy and freeze it: the caller's mapping can change after construction.
        snapshot = dict(self.by_market)
        for key, amount in snapshot.items():
            if not _is_projection_key(key):
                raise ValueError(f"exposure key {key!r} must be (fixture_id: int, player_id: int, market_type: str)")
            if not (math.isfinite(amount) and amount >= 0.0):
                raise ValueError(f"exposure for {key} must be finite and non-negative")
        object.__setattr__(self, "by_market", MappingProxyType(snapshot))

    @property
    def total(self) -> float:
        return math.fsum(self.by_market.values())

    def fixture_total(self, fixture_id: int) -> float:
        return math.fsum(amount for key, amount in self.by_market.items() if key[0] == fixture_id)

    def market_total(self, key: ProjectionKey) -> float:
        return self.by_market.get(key, 0.0)


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


def stressed_probabilities(probabilities: SideProbabilities, loss_floor: float) -> SideProbabilities:
    """Move win mass to loss until p_lose >= loss_floor (model-risk haircut; void mass untouched)."""
    if not (math.isfinite(loss_floor) and 0.0 <= loss_floor < 1.0):
        raise ValueError("loss_floor must lie in [0, 1)")
    shift = min(max(loss_floor - probabilities.p_lose, 0.0), probabilities.p_win)
    return SideProbabilities(probabilities.p_win - shift, probabilities.p_lose + shift, probabilities.p_void)


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
    """Build exact scenario/count probabilities and per-unit returns, including one exact tail bucket.

    Within a scenario the count masses are averaged over its minutes mixture (a single point when the
    lineup carries none), which keeps EV and covariance consistent with PropProjection.side_probabilities.
    """
    if not candidates:
        raise ValueError("no candidates")
    lineup: LineupExpectation = projection.lineup
    top_count = math.floor(max(candidate.quote.line_value for candidate in candidates)) + 1
    probabilities: list[float] = []
    payoffs: list[list[float]] = []
    counts = np.arange(top_count + 1)
    scenarios = (
        ("start", lineup.p_start, lineup.start_minutes),
        ("sub", lineup.p_sub_appearance, lineup.sub_minutes),
        ("absent", lineup.p_no_appearance, None),
    )
    for scenario, scenario_weight, mixture in scenarios:
        if scenario_weight == 0.0:
            continue
        if mixture is None:
            scenario_counts = np.array([0], dtype=np.int64)
            masses = np.array([1.0], dtype=np.float64)
        else:
            scenario_counts = counts
            masses = np.zeros(top_count + 1, dtype=np.float64)
            for minutes, minutes_weight in zip(mixture.minutes, mixture.weights):
                distribution = projection.count_distribution(minutes)
                masses += minutes_weight * np.append(distribution.pmf(counts[:-1]), distribution.sf(top_count - 1))
        for count, mass in zip(scenario_counts, masses):
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


MarketGrid = Sequence[tuple[np.ndarray, np.ndarray]]


def fixture_moments(grids: Sequence[MarketGrid], node_weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Exact mean and covariance of per-unit returns for every candidate of one fixture.

    `grids[a][j]` is outcome_model for market a given the shared match factor G = g_j, and `node_weights`
    are the quadrature weights of G. Players are independent given G, so across markets
    E[R_a R_b] = sum_j w_j m_a(g_j) m_b(g_j); within a market the second moment is the exact grid moment
    averaged over G. As in return_moments, the subtraction acts on per-unit returns bounded by [-1, O - 1]
    with kappa = 1 + mu^2/var about 1.005 on the reference case, so it is permitted by AGENT.md gate G1.
    """
    if not grids or any(len(grid) != node_weights.size for grid in grids):
        raise ValueError("every market needs one outcome grid per quadrature node")
    if not (np.isfinite(node_weights).all() and (node_weights >= 0.0).all()) or not math.isclose(
        float(node_weights.sum()), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("quadrature weights must be non-negative and sum to 1")
    sizes = [grid[0][1].shape[1] for grid in grids]
    offsets = np.cumsum([0, *sizes])
    total = int(offsets[-1])
    mean = np.zeros(total, dtype=np.float64)
    second = np.zeros((total, total), dtype=np.float64)
    for node, weight in enumerate(node_weights):
        conditional_means: list[np.ndarray] = []
        for market, grid in enumerate(grids):
            probabilities, payoffs = grid[node]
            if payoffs.shape[1] != sizes[market]:
                raise ValueError("candidate count differs between quadrature nodes")
            block = slice(int(offsets[market]), int(offsets[market + 1]))
            conditional_means.append(probabilities @ payoffs)
            second[block, block] += weight * (payoffs.T @ (payoffs * probabilities[:, None]))
        stacked = np.concatenate(conditional_means)
        mean += weight * stacked
        cross = weight * np.outer(stacked, stacked)
        for a in range(len(grids)):
            for b in range(len(grids)):
                if a != b:
                    rows = slice(int(offsets[a]), int(offsets[a + 1]))
                    columns = slice(int(offsets[b]), int(offsets[b + 1]))
                    second[rows, columns] += cross[rows, columns]
    if not (np.isfinite(mean).all() and np.isfinite(second).all()):
        raise FloatingPointError("fixture return moments are not finite")
    covariance = second - np.outer(mean, mean)
    covariance = 0.5 * (covariance + covariance.T)
    smallest_eigenvalue = float(np.linalg.eigvalsh(covariance).min())
    if smallest_eigenvalue < 0.0:
        covariance = covariance + np.eye(total) * -smallest_eigenvalue
    return mean, covariance


BudgetGroup = tuple[np.ndarray, float]


def solve_grouped_mean_variance(
    mu: np.ndarray,
    cov: np.ndarray,
    upper: np.ndarray,
    groups: Sequence[BudgetGroup],
    risk_aversion: float,
) -> tuple[np.ndarray, str]:
    """Maximise f.mu - lambda f'Sigma f with 0 <= f <= upper and mask.f <= budget for every (mask, budget)
    group; never return less utility than the best single position."""
    n_candidates = len(mu)
    if cov.shape != (n_candidates, n_candidates) or upper.shape != (n_candidates,):
        raise ValueError("mean, covariance, and upper-bound dimensions do not match")
    if any(mask.shape != (n_candidates,) for mask, _ in groups):
        raise ValueError("group masks must have one entry per candidate")
    if not (
        np.isfinite(mu).all()
        and np.isfinite(cov).all()
        and np.isfinite(upper).all()
        and all(math.isfinite(budget) for _, budget in groups)
        and math.isfinite(risk_aversion)
    ):
        raise ValueError("optimizer inputs must be finite")
    if (upper < 0.0).any() or any(budget < 0.0 for _, budget in groups) or risk_aversion <= 0.0:
        raise ValueError("upper bounds and budgets must be non-negative; risk_aversion must be positive")

    def utility(fractions: np.ndarray) -> float:
        return float(fractions @ mu - risk_aversion * fractions @ cov @ fractions)

    def feasible(fractions: np.ndarray) -> np.ndarray:
        """Clip to the bounds, then scale down each over-budget group (scaling never raises another group)."""
        result = np.clip(fractions, 0.0, upper)
        for mask, budget in groups:
            used = float(mask @ result)
            if used > budget:
                inside = mask > 0.0
                result[inside] *= budget / used
        return result

    best_single = np.zeros(n_candidates, dtype=np.float64)
    best_single_utility = 0.0
    for candidate_index in range(n_candidates):
        if mu[candidate_index] <= 0.0 or upper[candidate_index] <= 0.0:
            continue
        cap = min([float(upper[candidate_index])] + [budget for mask, budget in groups if mask[candidate_index] > 0.0])
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
                "fun": (lambda fractions, m=mask, b=budget: b - float(m @ fractions)),
                "jac": (lambda fractions, m=mask: -m),
            }
            for mask, budget in groups
        ],
        options={"ftol": 1e-14, "maxiter": 1000},
    )
    if result.success:
        weights = feasible(result.x)
        if utility(weights) >= best_single_utility - 1e-12:
            return weights, "qp"
    logger.warning("mean-variance QP did not improve on best single bet (%s); using single", result.message)
    return best_single, "single"


def solve_mean_variance(
    mu: np.ndarray,
    cov: np.ndarray,
    upper: np.ndarray,
    budget: float,
    risk_aversion: float,
) -> tuple[np.ndarray, str]:
    """Single-budget form of solve_grouped_mean_variance: 0 <= f <= upper and sum(f) <= budget."""
    if not math.isfinite(budget):
        raise ValueError("optimizer inputs must be finite")
    return solve_grouped_mean_variance(mu, cov, upper, [(np.ones(len(mu)), budget)], risk_aversion)


@dataclass(frozen=True)
class _MarketPlan:
    key: ProjectionKey
    projection: FactorProjection
    candidates: list[Candidate]
    grids: list[tuple[np.ndarray, np.ndarray]]
    node_weights: np.ndarray


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
        min_stake: float = 1.0,
        *,
        model_risk_loss_floor: float,
    ) -> None:
        """`model_risk_loss_floor` has no default: pass MODEL_RISK_LOSS_FLOOR unless a backtest says otherwise."""
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
        if not (math.isfinite(min_stake) and min_stake >= 0.0):
            raise ValueError("min_stake must be finite and non-negative")
        if max_quote_age < timedelta(0):
            raise ValueError("max_quote_age must be non-negative")
        if not (math.isfinite(model_risk_loss_floor) and 0.0 <= model_risk_loss_floor < 1.0):
            raise ValueError("model_risk_loss_floor must lie in [0, 1)")
        self.bankroll = bankroll
        self.risk_aversion = risk_aversion
        self.alpha_threshold = alpha_threshold
        self.max_position_fraction = max_position_fraction
        self.max_fixture_fraction = max_fixture_fraction
        self.max_total_fraction = max_total_fraction
        self.max_quote_age = max_quote_age
        self.min_stake = min_stake
        self.model_risk_loss_floor = model_risk_loss_floor

    def allocate(
        self,
        projections: Mapping[ProjectionKey, FactorProjection],
        quotes: Iterable[LiveOddsQuote],
        *,
        now: datetime,
        exposure: ExposureBook,
    ) -> AllocationReport:
        """One joint mean-variance problem per fixture, with caps net of the exposure already open.

        Per-market, per-slot (Kelly) and fixture budgets are linear constraints of the fixture problem;
        the slate cap then scales all fixtures pro rata. Every budget is what remains after `exposure`.
        """
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be provided as a timezone-aware UTC datetime")
        for key, projection in projections.items():
            identity = (projection.fixture_id, projection.player_id, projection.market_type)
            if identity != key:
                # A misfiled projection would be priced against another market's quotes; the whole
                # mapping is suspect, so refuse it rather than skip the one entry.
                raise ValueError(f"projection filed under {key} identifies itself as {identity}")
        report = AllocationReport()
        available = self.bankroll - exposure.total
        if available <= 0.0:
            logger.warning("no bankroll available after open exposure; no directives issued")
            return report

        grouped_quotes: dict[ProjectionKey, list[LiveOddsQuote]] = defaultdict(list)
        for quote in quotes:
            key = (quote.fixture_id, quote.player_id, quote.market_type)
            if quote.captured_at > now:
                report.future_quotes += 1
                report.skipped.append((key, "quote captured_at later than now (clock skew); quote ignored"))
                continue
            if now - quote.captured_at > self.max_quote_age:
                report.stale_quotes += 1
                continue
            grouped_quotes[key].append(quote)

        plans_by_fixture: dict[int, list[_MarketPlan]] = defaultdict(list)
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
                nodes, node_weights = game_state_quadrature(projection.game_state_k)
                grids = [outcome_model(projection.conditional(float(g)), candidates) for g in nodes]
            except (ProjectionError, ValueError, FloatingPointError) as exc:
                report.skipped.append((key, f"projection unusable: {exc}"))
                logger.error("market %s skipped, projection unusable: %s", key, exc)
                continue
            plans_by_fixture[key[0]].append(_MarketPlan(key, projection, candidates, grids, node_weights))

        slate_room = max(self.max_total_fraction - exposure.total / available, 0.0)
        rows: list[tuple[int, Candidate, float, float]] = []
        solved: dict[int, tuple[list[_MarketPlan], np.ndarray, np.ndarray, str]] = {}
        for fixture_id, plans in sorted(plans_by_fixture.items()):
            fixture_room = max(self.max_fixture_fraction - exposure.fixture_total(fixture_id) / available, 0.0)
            reason = self._fixture_skip_reason(plans, fixture_room, slate_room)
            if reason is not None:
                report.skipped.extend((plan.key, reason) for plan in plans)
                continue
            mean_returns, covariance = fixture_moments([plan.grids for plan in plans], plans[0].node_weights)
            flat = [candidate for plan in plans for candidate in plan.candidates]
            # Mean returns take the same model-risk haircut as the candidate filter: EV falls by shift * price.
            mean_returns = mean_returns - np.array([c.loss_shift * c.price for c in flat], dtype=np.float64)
            upper = np.array(
                [min(c.kelly_full, self.max_position_fraction, c.quote.max_stake / available) for c in flat],
                dtype=np.float64,
            )
            weights, solver = solve_grouped_mean_variance(
                mean_returns, covariance, upper, self._budget_groups(plans, flat, fixture_room, slate_room, exposure, available),
                self.risk_aversion,
            )
            if weights.sum() <= 0.0:
                report.skipped.extend((plan.key, "optimizer allocated nothing") for plan in plans)
                continue
            solved[fixture_id] = (plans, mean_returns, covariance, solver)
            rows.extend(
                (fixture_id, candidate, float(weights[index]), float(covariance[index, index]))
                for index, candidate in enumerate(flat)
                if weights[index] > 0.0
            )

        issued = math.fsum(fraction for _, _, fraction, _ in rows)
        if issued > slate_room:
            scale = slate_room / issued
            rows = [(fixture_id, candidate, fraction * scale, variance) for fixture_id, candidate, fraction, variance in rows]

        for fixture_id, (plans, mean_returns, covariance, solver) in sorted(solved.items()):
            flat = [candidate for plan in plans for candidate in plan.candidates]
            final = np.zeros(len(flat), dtype=np.float64)
            for row_fixture, candidate, fraction, _ in rows:
                if row_fixture == fixture_id:
                    final[next(i for i, c in enumerate(flat) if c is candidate)] = fraction
            report.allocations.append(
                FixtureAllocation(
                    fixture_id=fixture_id,
                    n_markets=len(plans),
                    n_candidates=len(flat),
                    expected_return=float(final @ mean_returns),
                    return_variance=float(final @ covariance @ final),
                    utility=float(final @ mean_returns - self.risk_aversion * final @ covariance @ final),
                    solver=solver,
                )
            )

        for _, candidate, fraction, variance in sorted(
            rows,
            key=lambda row: (-row[1].ev, row[0], row[1].quote.line_value, row[1].side, row[1].quote.sportsbook),
        ):
            directive = self._directive(candidate, fraction, variance, available)
            if directive.stake < self.min_stake:
                report.skipped_directives.append(directive)
                logger.info("sub-minimum stake skipped: %s", directive)
            else:
                report.directives.append(directive)
        return report

    @staticmethod
    def _fixture_skip_reason(plans: Sequence[_MarketPlan], fixture_room: float, slate_room: float) -> str | None:
        if len({plan.projection.game_state_k for plan in plans}) != 1:
            return "projections in one fixture disagree on game_state_k; fixture not priced"
        if slate_room <= 0.0:
            return "slate cap already used by open exposure"
        if fixture_room <= 0.0:
            return "fixture cap already used by open exposure"
        return None

    def _budget_groups(
        self,
        plans: Sequence[_MarketPlan],
        flat: Sequence[Candidate],
        fixture_room: float,
        slate_room: float,
        exposure: ExposureBook,
        available: float,
    ) -> list[BudgetGroup]:
        """Fixture budget, one budget per market (net of its open exposure), and one Kelly budget per
        (line, side, void rule) slot quoted by several books."""
        n_candidates = len(flat)
        groups: list[BudgetGroup] = [(np.ones(n_candidates, dtype=np.float64), min(fixture_room, slate_room))]
        start = 0
        for plan in plans:
            mask = np.zeros(n_candidates, dtype=np.float64)
            mask[start:start + len(plan.candidates)] = 1.0
            market_room = max(self.max_position_fraction - exposure.market_total(plan.key) / available, 0.0)
            groups.append((mask, market_room))
            slots: dict[tuple[float, str, VoidRule], list[int]] = defaultdict(list)
            for offset, candidate in enumerate(plan.candidates):
                slots[(candidate.quote.line_value, candidate.side, candidate.void_rule)].append(start + offset)
            for indices in slots.values():
                if len(indices) > 1:
                    slot_mask = np.zeros(n_candidates, dtype=np.float64)
                    slot_mask[indices] = 1.0
                    groups.append((slot_mask, max(flat[index].kelly_full for index in indices)))
            start += len(plan.candidates)
        return groups

    def _directive(self, candidate: Candidate, fraction: float, variance: float, available: float) -> StakingDirective:
        raw_stake = min(fraction * available, candidate.quote.max_stake)
        floor_price = price_for_ev(candidate.probs, self.alpha_threshold)
        return StakingDirective(
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
            stake=math.floor(raw_stake * 100.0 + 1e-9) / 100.0,
            min_acceptable_odds=math.ceil(floor_price * 1000.0 - 1e-9) / 1000.0,
            expires_at=candidate.quote.captured_at + self.max_quote_age,
            limit_published=candidate.quote.limit_published,
        )

    def _candidates(
        self, projection: PropProjection | FactorProjection, quotes: Sequence[LiveOddsQuote]
    ) -> tuple[list[Candidate], list[str]]:
        """Every book is its own candidate (no best-price de-duplication), so depth at a worse price is kept."""
        candidates: list[Candidate] = []
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
                stressed = stressed_probabilities(side_probabilities, self.model_risk_loss_floor)
                ev = expected_value(stressed, price)
                if ev <= self.alpha_threshold:
                    continue
                if quote.max_stake <= 0.0:
                    skipped.append(f"{quote.sportsbook} accepts no stake on {quote.line_value:g} {side}")
                    continue
                candidates.append(
                    Candidate(
                        quote=quote,
                        side=side,
                        price=price,
                        probs=stressed,
                        fair_prob=fair_probabilities.get(side),
                        ev=ev,
                        kelly_full=kelly_fraction_with_push(stressed, price),
                        loss_shift=side_probabilities.p_win - stressed.p_win,
                    )
                )
        ordered = sorted(
            candidates,
            key=lambda c: (c.quote.line_value, c.side, c.void_rule.value, -c.price, c.quote.sportsbook),
        )
        return ordered, skipped


def price_for_ev(probabilities: SideProbabilities, target_ev: float) -> float:
    if not (math.isfinite(target_ev) and probabilities.p_win > 0.0):
        raise ValueError("target EV must be finite and win probability must be positive")
    return 1.0 + (target_ev + probabilities.p_lose) / probabilities.p_win
