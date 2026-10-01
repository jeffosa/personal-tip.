"""Hardened allocation: shared game-state factor across a fixture, exposure-aware caps, liquidity-aware bounds."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize
from scipy.special import roots_genlaguerre

from prop_alpha.modeling.engine import (
    GammaPosterior,
    LineupExpectation,
    ProjectionError,
    PropProjection,
    SideProbabilities,
    VoidRule,
)
from prop_alpha.scanner.market_scanner import (
    DEGENERATE_LOSS_PROBABILITY,
    Candidate,
    LiveOddsQuote,
    ProjectionKey,
    expected_value,
    kelly_fraction_with_push,
    outcome_model,
    price_for_ev,
)

logger = logging.getLogger("prop_alpha.scanner")

FACTOR_QUADRATURE_NODES: Final[int] = 48


def fold_multiplier_uncertainty(
    posterior: GammaPosterior, multiplier: float, multiplier_relative_variance: float
) -> GammaPosterior:
    """Moment-match c * Lambda to one Gamma when c itself is uncertain (relative variance v_c).

    Relative variance of a product of independent factors is prod(1 + v_i) - 1, formed with
    log1p/expm1 so no squared moments are subtracted (AGENT gate G1).
    """
    if not (math.isfinite(multiplier) and multiplier > 0.0):
        raise ProjectionError(f"multiplier={multiplier!r} must be finite and positive")
    if not (math.isfinite(multiplier_relative_variance) and multiplier_relative_variance >= 0.0):
        raise ProjectionError("multiplier_relative_variance must be finite and non-negative")
    relative_variance = math.expm1(math.log1p(1.0 / posterior.alpha) + math.log1p(multiplier_relative_variance))
    shape = 1.0 / relative_variance
    mean_rate = multiplier * posterior.alpha / posterior.beta
    return GammaPosterior(alpha=shape, beta=shape / mean_rate)


@dataclass(frozen=True)
class FactorProjection:
    """Player rate Lambda ~ Gamma(alpha, beta) per 90, times a match factor G ~ Gamma(k, k) shared by the fixture."""

    fixture_id: int
    player_id: int
    market_type: str
    rate_posterior: GammaPosterior
    game_state_k: float
    lineup: LineupExpectation

    def __post_init__(self) -> None:
        if not (math.isfinite(self.game_state_k) and self.game_state_k > 0.0):
            raise ProjectionError(f"game_state_k={self.game_state_k!r} must be finite and positive")

    def conditional(self, g: float) -> PropProjection:
        """X | G=g is NB(alpha, beta/(beta + g t)), i.e. a PropProjection with shape alpha and rate beta/g."""
        return PropProjection(
            self.fixture_id,
            self.player_id,
            self.market_type,
            self.rate_posterior.alpha,
            self.rate_posterior.beta / g,
            self.lineup,
        )

    def side_probabilities(
        self, line: float, side: str, void_rule: VoidRule | str = VoidRule.VOID_IF_NO_APPEARANCE
    ) -> SideProbabilities:
        nodes, weights = game_state_quadrature(self.game_state_k)
        splits = [self.conditional(float(g)).side_probabilities(line, side, void_rule) for g in nodes]
        return SideProbabilities(
            math.fsum(float(w) * s.p_win for w, s in zip(weights, splits)),
            math.fsum(float(w) * s.p_lose for w, s in zip(weights, splits)),
            math.fsum(float(w) * s.p_void for w, s in zip(weights, splits)),
        )


def game_state_quadrature(k: float, n_nodes: int = FACTOR_QUADRATURE_NODES) -> tuple[np.ndarray, np.ndarray]:
    """Nodes g_j and normalised weights w_j with sum_j w_j f(g_j) ~= E[f(G)], G ~ Gamma(k, rate=k)."""
    if not (math.isfinite(k) and k > 0.0):
        raise ProjectionError("game_state_k must be finite and positive")
    roots, raw_weights = roots_genlaguerre(n_nodes, k - 1.0)
    keep = raw_weights > 0.0
    weights = raw_weights[keep] / math.fsum(raw_weights[keep])
    return roots[keep] / k, weights


def fixture_moments(
    projections: Sequence[FactorProjection], candidate_sets: Sequence[Sequence[Candidate]]
) -> tuple[np.ndarray, np.ndarray]:
    """Exact mean and covariance of per-unit returns for every candidate in one fixture.

    Players are independent given G and share G. Within a player market the second moment comes from the
    exact outcome grid; across markets it is E_G[m_a(G) m_b(G)].
    """
    if len(projections) != len(candidate_sets) or not projections:
        raise ValueError("one candidate set per projection is required")
    ks = {projection.game_state_k for projection in projections}
    fixtures = {projection.fixture_id for projection in projections}
    if len(ks) != 1 or len(fixtures) != 1:
        raise ProjectionError("all projections in a fixture must share one fixture_id and one game_state_k")
    nodes, weights = game_state_quadrature(ks.pop())
    sizes = [len(candidates) for candidates in candidate_sets]
    offsets = np.cumsum([0, *sizes])
    total = int(offsets[-1])
    mean = np.zeros(total, dtype=np.float64)
    second = np.zeros((total, total), dtype=np.float64)
    for g, w in zip(nodes, weights):
        conditional_means: list[np.ndarray] = []
        for index, (projection, candidates) in enumerate(zip(projections, candidate_sets)):
            probabilities, payoffs = outcome_model(projection.conditional(float(g)), candidates)
            block = slice(offsets[index], offsets[index + 1])
            conditional_mean = probabilities @ payoffs
            conditional_means.append(conditional_mean)
            second[block, block] += w * (payoffs.T @ (payoffs * probabilities[:, None]))
        stacked = np.concatenate(conditional_means)
        mean += w * stacked
        cross = w * np.outer(stacked, stacked)
        for a in range(len(projections)):
            for b in range(len(projections)):
                if a != b:
                    second[offsets[a]:offsets[a + 1], offsets[b]:offsets[b + 1]] += cross[
                        offsets[a]:offsets[a + 1], offsets[b]:offsets[b + 1]
                    ]
    covariance = second - np.outer(mean, mean)
    covariance = 0.5 * (covariance + covariance.T)
    smallest = float(np.linalg.eigvalsh(covariance).min())
    if smallest < 0.0:
        covariance = covariance + np.eye(total) * -smallest
    return mean, covariance


def solve_grouped_mean_variance(
    mu: np.ndarray,
    cov: np.ndarray,
    upper: np.ndarray,
    groups: Sequence[tuple[np.ndarray, float]],
    risk_aversion: float,
) -> tuple[np.ndarray, str]:
    """Maximise f.mu - lambda f'Sigma f with 0 <= f <= upper and mask.f <= budget for every group."""
    n = len(mu)
    if cov.shape != (n, n) or upper.shape != (n,):
        raise ValueError("mean, covariance, and upper-bound dimensions do not match")
    if not (np.isfinite(mu).all() and np.isfinite(cov).all() and np.isfinite(upper).all()):
        raise ValueError("optimizer inputs must be finite")
    if (upper < 0.0).any() or risk_aversion <= 0.0 or any(budget < 0.0 for _, budget in groups):
        raise ValueError("bounds and budgets must be non-negative; risk_aversion must be positive")

    def utility(f: np.ndarray) -> float:
        return float(f @ mu - risk_aversion * f @ cov @ f)

    def feasible(f: np.ndarray) -> bool:
        return bool((f >= -1e-12).all() and (f <= upper + 1e-12).all()) and all(
            float(mask @ f) <= budget + 1e-12 for mask, budget in groups
        )

    best, best_utility = np.zeros(n), 0.0
    for i in range(n):
        if mu[i] <= 0.0 or upper[i] <= 0.0:
            continue
        cap = min([float(upper[i])] + [budget for mask, budget in groups if mask[i]])
        variance = float(cov[i, i])
        trial = np.zeros(n)
        trial[i] = cap if variance <= 0.0 else min(cap, float(mu[i]) / (2.0 * risk_aversion * variance))
        if utility(trial) > best_utility:
            best, best_utility = trial, utility(trial)
    if n <= 1:
        return best, "single"
    constraints = [
        {"type": "ineq", "fun": (lambda f, m=mask, b=budget: b - float(m @ f)), "jac": (lambda f, m=mask: -m)}
        for mask, budget in groups
    ]
    result = minimize(
        lambda f: -utility(f),
        x0=best,
        jac=lambda f: -(mu - 2.0 * risk_aversion * cov @ f),
        method="SLSQP",
        bounds=[(0.0, float(bound)) for bound in upper],
        constraints=constraints,
        options={"ftol": 1e-14, "maxiter": 1000},
    )
    if result.success:
        weights = np.clip(result.x, 0.0, upper)
        if feasible(weights) and utility(weights) >= best_utility - 1e-12:
            return weights, "qp"
    logger.warning("grouped QP did not improve on best single bet (%s); using single", result.message)
    return best, "single"


@dataclass(frozen=True)
class ExposureBook:
    """Currency already committed (filled or still pending) per market. Supplied by the bet ledger."""

    by_market: Mapping[ProjectionKey, float]

    def __post_init__(self) -> None:
        for key, amount in self.by_market.items():
            if not (math.isfinite(amount) and amount >= 0.0):
                raise ValueError(f"exposure for {key} must be finite and non-negative")

    @property
    def total(self) -> float:
        return math.fsum(self.by_market.values())

    def fixture_total(self, fixture_id: int) -> float:
        return math.fsum(amount for key, amount in self.by_market.items() if key[0] == fixture_id)

    def market_total(self, key: ProjectionKey) -> float:
        return self.by_market.get(key, 0.0)


@dataclass(frozen=True)
class HardenedDirective:
    fixture_id: int
    player_id: int
    market_type: str
    line_value: float
    side: str
    sportsbook: str
    void_rule: str
    decimal_odds: float
    min_acceptable_odds: float
    expires_at: datetime
    p_win: float
    p_lose: float
    p_void: float
    ev_per_unit: float
    fraction: float
    stake: float


@dataclass
class HardenedReport:
    directives: list[HardenedDirective] = field(default_factory=list)
    skipped: list[tuple[ProjectionKey, str]] = field(default_factory=list)
    stale_quotes: int = 0
    future_quotes: int = 0


def stressed_probabilities(probabilities: SideProbabilities, loss_floor: float) -> SideProbabilities:
    """Move win mass to loss until p_lose >= loss_floor (model-risk haircut; void mass untouched)."""
    shift = min(max(loss_floor - probabilities.p_lose, 0.0), probabilities.p_win)
    return SideProbabilities(probabilities.p_win - shift, probabilities.p_lose + shift, probabilities.p_void)


class HardenedAssetManager:
    def __init__(
        self,
        bankroll: float,
        *,
        model_risk_loss_floor: float,
        risk_aversion: float = 2.0,
        alpha_threshold: float = 0.02,
        max_position_fraction: float = 0.02,
        max_fixture_fraction: float = 0.05,
        max_total_fraction: float = 0.20,
        max_quote_age: timedelta = timedelta(minutes=5),
        min_stake: float = 1.0,
    ) -> None:
        if not (math.isfinite(bankroll) and bankroll > 0.0):
            raise ValueError("bankroll must be finite and positive")
        if not (math.isfinite(model_risk_loss_floor) and 0.0 <= model_risk_loss_floor < 1.0):
            raise ValueError("model_risk_loss_floor must lie in [0, 1)")
        for name, value in (
            ("max_position_fraction", max_position_fraction),
            ("max_fixture_fraction", max_fixture_fraction),
            ("max_total_fraction", max_total_fraction),
        ):
            if not (math.isfinite(value) and 0.0 < value <= 1.0):
                raise ValueError(f"{name} must lie in (0, 1]")
        if not (math.isfinite(risk_aversion) and risk_aversion > 0.0):
            raise ValueError("risk_aversion must be finite and positive")
        if not (math.isfinite(alpha_threshold) and alpha_threshold >= 0.0):
            raise ValueError("alpha_threshold must be finite and non-negative")
        if max_quote_age < timedelta(0) or not (math.isfinite(min_stake) and min_stake >= 0.0):
            raise ValueError("max_quote_age and min_stake must be non-negative")
        self.bankroll = bankroll
        self.model_risk_loss_floor = model_risk_loss_floor
        self.risk_aversion = risk_aversion
        self.alpha_threshold = alpha_threshold
        self.max_position_fraction = max_position_fraction
        self.max_fixture_fraction = max_fixture_fraction
        self.max_total_fraction = max_total_fraction
        self.max_quote_age = max_quote_age
        self.min_stake = min_stake

    def allocate(
        self,
        projections: Mapping[ProjectionKey, FactorProjection],
        quotes: Iterable[LiveOddsQuote],
        *,
        now: datetime,
        exposure: ExposureBook,
    ) -> HardenedReport:
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be a timezone-aware UTC datetime")
        report = HardenedReport()
        available = self.bankroll - exposure.total
        if available <= 0.0:
            logger.warning("no bankroll available after open exposure; no directives issued")
            return report

        grouped: dict[ProjectionKey, list[LiveOddsQuote]] = defaultdict(list)
        for quote in quotes:
            key = (quote.fixture_id, quote.player_id, quote.market_type)
            if quote.captured_at > now:
                report.future_quotes += 1
                report.skipped.append((key, "quote captured_at later than now (clock skew); quote ignored"))
                continue
            if now - quote.captured_at > self.max_quote_age:
                report.stale_quotes += 1
                continue
            grouped[key].append(quote)

        by_fixture: dict[int, list[tuple[ProjectionKey, FactorProjection, list[Candidate]]]] = defaultdict(list)
        for key, market_quotes in grouped.items():
            projection = projections.get(key)
            if projection is None:
                report.skipped.append((key, "no projection"))
                continue
            try:
                candidates = self._candidates(projection, market_quotes, report, key)
            except (ProjectionError, ValueError, FloatingPointError) as exc:
                report.skipped.append((key, f"projection unusable: {exc}"))
                logger.error("market %s skipped, projection unusable: %s", key, exc)
                continue
            if candidates:
                by_fixture[key[0]].append((key, projection, candidates))

        slate_used = exposure.total / available
        slate_room = max(self.max_total_fraction - slate_used, 0.0)
        rows: list[tuple[Candidate, float]] = []
        for fixture_id, markets in sorted(by_fixture.items()):
            fixture_room = max(self.max_fixture_fraction - exposure.fixture_total(fixture_id) / available, 0.0)
            if fixture_room <= 0.0:
                report.skipped.extend((key, "fixture cap already used by open exposure") for key, _, _ in markets)
                continue
            try:
                mu, cov = fixture_moments([m[1] for m in markets], [m[2] for m in markets])
            except (ProjectionError, ValueError, FloatingPointError) as exc:
                report.skipped.extend((key, f"fixture moments unusable: {exc}") for key, _, _ in markets)
                logger.error("fixture %s skipped, moments unusable: %s", fixture_id, exc)
                continue
            flat = [candidate for _, _, candidates in markets for candidate in candidates]
            n = len(flat)
            upper = np.array(
                [
                    min(c.kelly_full, self.max_position_fraction, c.quote.max_stake / available)
                    for c in flat
                ],
                dtype=np.float64,
            )
            groups: list[tuple[np.ndarray, float]] = [(np.ones(n), min(fixture_room, slate_room))]
            start = 0
            for key, _, candidates in markets:
                mask = np.zeros(n)
                mask[start:start + len(candidates)] = 1.0
                market_room = max(self.max_position_fraction - exposure.market_total(key) / available, 0.0)
                groups.append((mask, market_room))
                slots: dict[tuple[float, str, VoidRule], list[int]] = defaultdict(list)
                for offset, candidate in enumerate(candidates):
                    slots[(candidate.quote.line_value, candidate.side, candidate.void_rule)].append(start + offset)
                for indices in slots.values():
                    if len(indices) > 1:
                        slot_mask = np.zeros(n)
                        slot_mask[indices] = 1.0
                        groups.append((slot_mask, max(flat[i].kelly_full for i in indices)))
                start += len(candidates)
            weights, _ = solve_grouped_mean_variance(mu, cov, upper, groups, self.risk_aversion)
            rows.extend((flat[i], float(weights[i])) for i in range(n) if weights[i] > 0.0)

        issued = math.fsum(fraction for _, fraction in rows)
        if issued > slate_room > 0.0 or (slate_room <= 0.0 and issued > 0.0):
            scale = slate_room / issued if issued > 0.0 else 0.0
            rows = [(candidate, fraction * scale) for candidate, fraction in rows]

        for candidate, fraction in sorted(
            rows, key=lambda row: (-row[0].ev, row[0].quote.fixture_id, row[0].quote.line_value, row[0].side)
        ):
            stake = math.floor(min(fraction * available, candidate.quote.max_stake) * 100.0 + 1e-9) / 100.0
            if stake < self.min_stake:
                continue
            report.directives.append(
                HardenedDirective(
                    fixture_id=candidate.quote.fixture_id,
                    player_id=candidate.quote.player_id,
                    market_type=candidate.quote.market_type,
                    line_value=candidate.quote.line_value,
                    side=candidate.side,
                    sportsbook=candidate.quote.sportsbook,
                    void_rule=candidate.void_rule.value,
                    decimal_odds=candidate.price,
                    min_acceptable_odds=math.ceil(price_for_ev(candidate.probs, self.alpha_threshold) * 1000.0) / 1000.0,
                    expires_at=candidate.quote.captured_at + self.max_quote_age,
                    p_win=candidate.probs.p_win,
                    p_lose=candidate.probs.p_lose,
                    p_void=candidate.probs.p_void,
                    ev_per_unit=candidate.ev,
                    fraction=fraction,
                    stake=stake,
                )
            )
        return report

    def _candidates(
        self,
        projection: FactorProjection,
        quotes: Sequence[LiveOddsQuote],
        report: HardenedReport,
        key: ProjectionKey,
    ) -> list[Candidate]:
        """Every book is its own candidate (no best-price dedupe), so depth at worse prices is not discarded."""
        candidates: list[Candidate] = []
        for quote in quotes:
            for side, price in (("over", quote.over_price), ("under", quote.under_price)):
                if price is None or not price > 1.0:
                    continue
                probabilities = projection.side_probabilities(quote.line_value, side, quote.void_rule)
                if probabilities.p_lose <= DEGENERATE_LOSS_PROBABILITY:
                    report.skipped.append((key, f"degenerate outcome: {quote.line_value:g} {side}; manual review required"))
                    continue
                stressed = stressed_probabilities(probabilities, self.model_risk_loss_floor)
                ev = expected_value(stressed, price)
                if ev <= self.alpha_threshold or quote.max_stake <= 0.0:
                    continue
                candidates.append(
                    Candidate(
                        quote=quote,
                        side=side,
                        price=price,
                        probs=stressed,
                        fair_prob=None,
                        ev=ev,
                        kelly_full=kelly_fraction_with_push(stressed, price),
                    )
                )
        return sorted(
            candidates,
            key=lambda c: (c.quote.line_value, c.side, c.void_rule.value, -c.price, c.quote.sportsbook),
        )
