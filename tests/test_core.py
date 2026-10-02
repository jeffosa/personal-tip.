from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
import pytest
from conftest import make_lineup_payload

from prop_alpha.calibration.fitting import (
    MIN_GAME_STATE_OBSERVATIONS,
    bench_sub_probability,
    fit_game_state_dispersion,
    fit_poisson_glm,
    fit_possession_kappa,
    fit_referee_betas,
    fit_state_duel_density,
)
from prop_alpha.calibration.referee_job import FixtureDiscipline, compute_referee_coefficients
from prop_alpha.evaluation.backtest import (
    PlayerMatchObservation,
    brier_score,
    calibration_table,
    closing_line_value,
    log_loss,
    summarize,
    walk_forward_over_probability,
)
from prop_alpha.ingestion.transform import (
    DEFAULT_KAPPAS,
    GoalEvent,
    dribbled_past_rate,
    match_state_minutes,
    padj_scale,
    parse_fixture_payload,
    parse_lineup_payload,
)
from prop_alpha.modeling.engine import (
    MAX_RELATIVE_VARIANCE,
    MIN_RELATIVE_VARIANCE,
    FactorProjection,
    GammaPosterior,
    LineupExpectation,
    MinutesMixture,
    ProjectionError,
    PropProjection,
    QuantEngine,
    RefereeProfile,
    SideProbabilities,
    VoidRule,
    game_state_quadrature,
)
from prop_alpha.scanner.market_scanner import (
    DEGENERATE_LOSS_PROBABILITY,
    MODEL_RISK_LOSS_FLOOR,
    AllocationReport,
    Candidate,
    ExposureBook,
    InstitutionalAssetManager,
    LiveOddsQuote,
    ProjectionKey,
    devig_two_way,
    fixture_moments,
    kelly_fraction_with_push,
    outcome_model,
    price_for_ev,
    return_moments,
    solve_mean_variance,
    stressed_probabilities,
)

NO_EXPOSURE = ExposureBook({})


def _manager(bankroll: float, **kwargs: Any) -> InstitutionalAssetManager:
    return InstitutionalAssetManager(bankroll, model_risk_loss_floor=kwargs.pop("floor", MODEL_RISK_LOSS_FLOOR), **kwargs)


# k -> infinity limit: a finite k so large that 1/k vanishes against 1/alpha in double precision.
NO_GAME_STATE_K = 1e300


def test_predictive_gamma_exact_without_game_state() -> None:
    posterior = GammaPosterior(alpha=12.0, beta=6.0)
    shape, rate = QuantEngine(game_state_k=NO_GAME_STATE_K).predictive_gamma(posterior, 2.0, 0.0)
    assert shape == pytest.approx(posterior.alpha, rel=1e-14)
    assert rate == pytest.approx(posterior.beta / 2.0, rel=1e-14)


def test_predictive_is_negative_binomial_with_posterior_mean() -> None:
    projection = PropProjection(1, 2, "tackles", 8.0, 4.0, LineupExpectation(1.0, 0.0))
    distribution = projection.count_distribution(90.0)
    assert float(distribution.mean()) == pytest.approx(2.0)


def test_game_state_mixing_widens_but_preserves_mean() -> None:
    posterior = GammaPosterior(alpha=9.0, beta=4.0)
    plain_shape, plain_rate = QuantEngine(game_state_k=NO_GAME_STATE_K).predictive_gamma(posterior, 1.5, 0.0)
    mixed_shape, mixed_rate = QuantEngine(game_state_k=4.0).predictive_gamma(posterior, 1.5, 0.0)
    assert mixed_shape < plain_shape
    assert mixed_shape / mixed_rate == pytest.approx(plain_shape / plain_rate)


def test_predictive_gamma_stable_where_subtraction_form_cancels() -> None:
    posterior = GammaPosterior(alpha=1e17, beta=1e17 / 2.0)
    shape, rate = QuantEngine(game_state_k=NO_GAME_STATE_K).predictive_gamma(posterior, 1.0, 0.0)
    assert shape == 1.0 / MIN_RELATIVE_VARIANCE
    assert math.isfinite(shape) and math.isfinite(rate) and rate > 0.0


def test_predictive_gamma_raises_under_extreme_overdispersion() -> None:
    # Decision 3 (2026-10-01): above MAX_RELATIVE_VARIANCE the inputs are degenerate; raise, never clamp.
    with pytest.raises(ProjectionError, match="exceeds"):
        QuantEngine(game_state_k=1e-9).predictive_gamma(GammaPosterior(4.0, 2.0), 1.0, 0.0)


def test_tiny_league_rate_raises_instead_of_pricing_a_needle() -> None:
    # Prior-only player at league rate 1e-4: v = 1/alpha = 1/6e-4 ~ 1667 > MAX_RELATIVE_VARIANCE.
    engine = QuantEngine(game_state_k=6.0)
    posterior = engine.positional_prior(1e-4)
    assert 1.0 / posterior.alpha > MAX_RELATIVE_VARIANCE
    with pytest.raises(ProjectionError, match="degenerate"):
        engine.predictive_gamma(posterior, 1.0, 0.0)


def test_math_clamp_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="prop_alpha.engine"):
        QuantEngine(game_state_k=NO_GAME_STATE_K).predictive_gamma(GammaPosterior(1e17, 1e17), 1.0, 0.0)
    assert "[MATH_CLAMP]" in caplog.text


def test_no_math_clamp_for_ordinary_inputs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="prop_alpha.engine"):
        QuantEngine(game_state_k=6.0).predictive_gamma(GammaPosterior(20.0, 10.0), 1.0, 0.0)
    assert "[MATH_CLAMP]" not in caplog.text


def test_game_state_k_is_mandatory() -> None:
    with pytest.raises(TypeError):
        QuantEngine()  # type: ignore[call-arg]
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            QuantEngine(game_state_k=bad)


def test_relative_variance_without_multiplier_matches_the_additive_form() -> None:
    posterior = GammaPosterior(alpha=29.2, beta=11.944)
    k = 6.0
    shape, rate = QuantEngine(game_state_k=k).predictive_gamma(posterior, 1.0, 0.0)
    additive = 1.0 / posterior.alpha + 1.0 / k + 1.0 / (posterior.alpha * k)
    assert abs(1.0 / shape - additive) <= 1e-15
    assert shape / rate == pytest.approx(posterior.alpha / posterior.beta, rel=1e-15)


def test_multiplier_variance_widens_the_predictive_distribution() -> None:
    engine = QuantEngine(game_state_k=6.0)
    posterior = GammaPosterior(alpha=30.0, beta=25.0)
    plain_shape, plain_rate = engine.predictive_gamma(posterior, 0.15, 0.0)
    wide_shape, wide_rate = engine.predictive_gamma(posterior, 0.15, 0.25)
    assert wide_shape < plain_shape
    assert wide_shape / wide_rate == pytest.approx(plain_shape / plain_rate, rel=1e-14)
    expected = (1.0 + 1.0 / 30.0) * (1.0 + 1.0 / 6.0) * 1.25 - 1.0
    assert 1.0 / wide_shape == pytest.approx(expected, rel=1e-14)
    with pytest.raises(ProjectionError):
        engine.predictive_gamma(posterior, 0.15, -0.1)


def test_card_projection_takes_the_multiplier_variance() -> None:
    engine = QuantEngine(game_state_k=6.0)
    kwargs: dict[str, Any] = dict(
        fixture_id=1, player_id=2, foul_counts=[2, 1, 3, 2], minutes=[90.0, 90.0, 80.0, 90.0],
        league_foul_rate_per90=1.4, referee=RefereeProfile(1.1, 0.15, 0.01, 0.05), is_home=False,
        lineup=LineupExpectation(1.0, 0.0),
    )
    certain = engine.project_card_market(card_multiplier_relative_variance=0.0, **kwargs)
    uncertain = engine.project_card_market(card_multiplier_relative_variance=0.25, **kwargs)
    assert uncertain.gamma_shape < certain.gamma_shape
    assert uncertain.rate_per90 == pytest.approx(certain.rate_per90, rel=1e-14)
    factor = engine.factor_card_market(card_multiplier_relative_variance=0.25, **kwargs)
    assert factor.rate_per90 == pytest.approx(certain.rate_per90, rel=1e-14)
    assert factor.rate_posterior.alpha < engine.factor_card_market(card_multiplier_relative_variance=0.0, **kwargs).rate_posterior.alpha
    with pytest.raises(TypeError):
        engine.project_card_market(**kwargs)


# ---------------- minutes mixtures (E4) ----------------
def test_minutes_mixture_prices_the_mix_not_its_mean() -> None:
    mixture = MinutesMixture((90.0, 70.0, 45.0), (0.65, 0.15, 0.20))
    assert mixture.mean == pytest.approx(78.0)
    fixed = PropProjection(1, 2, "fouls", 6.0, 6.0 / 1.4, LineupExpectation(1.0, 0.0, 78.0, 20.0))
    mixed = PropProjection(1, 2, "fouls", 6.0, 6.0 / 1.4, LineupExpectation(1.0, 0.0, 78.0, 20.0, start_mixture=mixture))
    by_hand = math.fsum(
        weight * float(fixed.count_distribution(minutes).cdf(1))
        for minutes, weight in zip(mixture.minutes, mixture.weights)
    )
    under_mixed = mixed.side_probabilities(1.5, "under").p_win
    assert under_mixed == pytest.approx(by_hand, abs=1e-14)
    assert under_mixed > fixed.side_probabilities(1.5, "under").p_win + 0.002


def test_minutes_mixture_keeps_ev_and_covariance_consistent() -> None:
    mixture = MinutesMixture((90.0, 70.0, 45.0), (0.65, 0.15, 0.20))
    projection = PropProjection(1, 2, "fouls", 6.0, 6.0 / 1.4, LineupExpectation(0.8, 0.1, 78.0, 20.0, start_mixture=mixture))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quote = LiveOddsQuote(1, 2, "fouls", 1.5, 2.0, None, 100.0, "Book", now, VoidRule.NO_VOID)
    probs = projection.side_probabilities(1.5, "over", VoidRule.NO_VOID)
    mean_returns, _ = return_moments(*outcome_model(projection, [Candidate(quote, "over", 2.0, probs, None, 0.0, 0.0)]))
    assert mean_returns[0] == pytest.approx(probs.p_win * 1.0 - probs.p_lose, abs=1e-12)


@pytest.mark.parametrize(
    ("minutes", "weights"),
    [((), ()), ((90.0,), (0.5,)), ((0.0,), (1.0,)), ((131.0,), (1.0,)), ((90.0, 45.0), (1.2, -0.2))],
)
def test_minutes_mixture_rejects_invalid_input(minutes: tuple[float, ...], weights: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        MinutesMixture(minutes, weights)


def test_lineup_mixture_mean_must_match_expected_minutes() -> None:
    with pytest.raises(ValueError, match="must equal"):
        LineupExpectation(1.0, 0.0, 85.0, 20.0, start_mixture=MinutesMixture((90.0, 45.0), (0.5, 0.5)))


@pytest.mark.parametrize("void_rule", list(VoidRule))
@pytest.mark.parametrize("line", [0.0, 0.5, 1.0, 1.5, 2.0, 2.5])
def test_side_probabilities_partition(line: float, void_rule: VoidRule) -> None:
    projection = PropProjection(1, 2, "tackles", 7.0, 3.0, LineupExpectation(0.65, 0.2))
    over = projection.side_probabilities(line, "over", void_rule)
    under = projection.side_probabilities(line, "under", void_rule)
    assert over.p_win + over.p_lose == pytest.approx(under.p_win + under.p_lose)
    assert over.p_void == pytest.approx(under.p_void)
    assert sum((over.p_win, over.p_lose, over.p_void)) == pytest.approx(1.0)
    assert sum((under.p_win, under.p_lose, under.p_void)) == pytest.approx(1.0)


@pytest.mark.parametrize(("p_start", "p_sub"), [(0.33, 0.67), (0.7, 0.3), (0.1, 0.9), (0.5, 0.5)])
def test_complementary_lineup_leaves_no_negative_absence_weight(p_start: float, p_sub: float) -> None:
    lineup = LineupExpectation(p_start, p_sub)
    assert lineup.p_no_appearance == 0.0
    projection = PropProjection(1, 2, "tackles", 7.0, 3.0, lineup)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    over_price = price_for_ev(projection.side_probabilities(1.5, "over"), 0.10)
    quote = LiveOddsQuote(1, 2, "tackles", 1.5, over_price, None, 100.0, "Book", now, VoidRule.VOID_IF_NO_APPEARANCE)
    candidates, _ = _manager(1_000.0)._candidates(projection, [quote])
    probabilities, _ = outcome_model(projection, candidates)
    assert (probabilities >= 0.0).all()


def test_side_probabilities_never_fail_on_rounding() -> None:
    rng = np.random.default_rng(20260930)
    for _ in range(100):
        p_start = float(rng.choice([1.0, 0.92, 0.75, 0.5, 0.33, 0.0]))
        p_sub = float(rng.choice([0.0, 0.05, 0.35, 1.0 - p_start]))
        if p_start + p_sub > 1.0:
            continue
        lineup = LineupExpectation(p_start, p_sub, float(rng.uniform(10.0, 120.0)), float(rng.uniform(5.0, 40.0)))
        projection = PropProjection(1, 2, "tackles", float(rng.uniform(0.5, 50.0)), float(rng.uniform(0.2, 20.0)), lineup)
        for line in (0.0, 0.5, 1.0, 2.5):
            for side in ("over", "under"):
                for void_rule in VoidRule:
                    result = projection.side_probabilities(line, side, void_rule)
                    assert min(result.p_win, result.p_lose, result.p_void) >= 0.0


def test_bimodal_substitution_has_no_absence_void_when_present() -> None:
    projection = PropProjection(1, 2, "tackles", 7.0, 3.0, LineupExpectation(0.5, 0.5))
    result = projection.side_probabilities(1.5, "over", VoidRule.VOID_IF_NO_APPEARANCE)
    assert result.p_void == pytest.approx(0.0)


@pytest.mark.parametrize("shape, rate", [(0.0, 1.0), (1.0, 0.0), (float("nan"), 1.0), (1.0, float("inf"))])
def test_invalid_projection_parameters_fail_closed(shape: float, rate: float) -> None:
    with pytest.raises(ProjectionError):
        PropProjection(1, 2, "tackles", shape, rate, LineupExpectation(1.0, 0.0))


def test_invalid_minutes_fail_closed() -> None:
    projection = PropProjection(1, 2, "tackles", 3.0, 2.0, LineupExpectation(1.0, 0.0))
    for minutes in (0.0, -1.0, float("nan"), float("inf"), 131.0):
        with pytest.raises(ProjectionError):
            projection.count_distribution(minutes)


def test_referee_profile_rejects_non_finite_coefficients() -> None:
    with pytest.raises(ValueError):
        RefereeProfile(float("inf"), 0.1, 0.01, 0.0)


def test_card_probability_not_distorted_by_variance_floor() -> None:
    projection = PropProjection(1, 2, "yellow_card", 4.0, 4.0 / 0.184, LineupExpectation(1.0, 0.0))
    probability = projection.side_probabilities(0.5, "over", VoidRule.NO_VOID).p_win
    exposure = projection.lineup.exp_minutes_start / 90.0
    assert probability == pytest.approx(
        1.0 - (projection.gamma_rate / (projection.gamma_rate + exposure)) ** projection.gamma_shape
    )


def _reference_projection(fixture_id: int = 1001, player_id: int = 55) -> PropProjection:
    return QuantEngine(game_state_k=6.0).project_count_market(
        fixture_id=fixture_id,
        player_id=player_id,
        market_type="tackles",
        counts=[3, 2, 4, 3, 1, 3],
        minutes=[90.0, 90.0, 90.0, 85.0, 90.0, 90.0],
        league_rate_per90=2.2,
        referee=RefereeProfile(1.0, 0.13, 0.004, 0.0),
        is_home=True,
        lineup=LineupExpectation(0.92, 0.05, 86.0, 20.0),
    )


def _reference_factor(fixture_id: int = 1001, player_id: int = 55) -> FactorProjection:
    return QuantEngine(game_state_k=6.0).factor_count_market(
        fixture_id=fixture_id,
        player_id=player_id,
        market_type="tackles",
        counts=[3, 2, 4, 3, 1, 3],
        minutes=[90.0, 90.0, 90.0, 85.0, 90.0, 90.0],
        league_rate_per90=2.2,
        referee=RefereeProfile(1.0, 0.13, 0.004, 0.0),
        is_home=True,
        lineup=LineupExpectation(0.92, 0.05, 86.0, 20.0),
    )


def _reference_quote(
    projection: PropProjection | FactorProjection,
    line: float,
    target_ev: float,
    sportsbook: str,
    now: datetime,
) -> LiveOddsQuote:
    over_probabilities = projection.side_probabilities(line, "over")
    under_probabilities = projection.side_probabilities(line, "under")
    return LiveOddsQuote(
        fixture_id=projection.fixture_id,
        player_id=projection.player_id,
        market_type=projection.market_type,
        line_value=line,
        over_price=price_for_ev(over_probabilities, target_ev),
        under_price=price_for_ev(under_probabilities, -0.10),
        max_stake=500.0,
        sportsbook=sportsbook,
        captured_at=now,
        void_rule=VoidRule.VOID_IF_NO_APPEARANCE,
    )


def test_mean_variance_prefers_low_variance_line_at_similar_ev() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = _reference_factor()
    quotes = [
        _reference_quote(projection, 1.5, 0.060, "BookA", now),
        _reference_quote(projection, 2.5, 0.062, "BookB", now),
    ]
    report = _manager(10_000.0).allocate({(1001, 55, "tackles"): projection}, quotes, now=now, exposure=NO_EXPOSURE)
    fractions = {directive.line_value: directive.fraction for directive in report.directives}
    assert fractions[1.5] == pytest.approx(0.0194, abs=0.0002)
    assert fractions[2.5] == pytest.approx(0.0004, abs=0.0002)


@pytest.mark.parametrize("line", [1.0, 1.5])
@pytest.mark.parametrize("void_rule", list(VoidRule))
def test_return_moments_match_closed_form(void_rule: VoidRule, line: float) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = PropProjection(1, 2, "tackles", 7.0, 3.0, LineupExpectation(0.7, 0.2, 86.0, 20.0))
    probs = projection.side_probabilities(line, "over", void_rule)
    price = 2.1
    quote = LiveOddsQuote(1, 2, "tackles", line, price, None, 100.0, "Book", now, void_rule)
    candidate = Candidate(quote, "over", price, probs, None, 0.0, 0.0)
    mean_returns, covariance = return_moments(*outcome_model(projection, [candidate]))
    net = price - 1.0
    expected_mean = probs.p_win * net - probs.p_lose
    expected_variance = probs.p_win * net**2 + probs.p_lose - expected_mean**2
    assert mean_returns[0] == pytest.approx(expected_mean, abs=1e-12)
    assert covariance[0, 0] == pytest.approx(expected_variance, abs=1e-12)


def test_cross_line_covariance_matches_brute_force() -> None:
    projection = _reference_projection()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quotes = [
        _reference_quote(projection, 1.5, 0.060, "BookA", now),
        _reference_quote(projection, 2.5, 0.062, "BookB", now),
    ]
    manager = _manager(10_000.0)
    candidates, _ = manager._candidates(projection, quotes)
    probabilities, payoffs = outcome_model(projection, candidates)
    _, covariance = return_moments(probabilities, payoffs)

    brute_probabilities: list[float] = []
    brute_payoffs: list[list[float]] = []
    def brute_payoff(candidate: Candidate, scenario: str, count: int) -> float:
        if scenario == "absent" and candidate.void_rule is not VoidRule.NO_VOID:
            return 0.0
        if scenario == "sub" and candidate.void_rule is VoidRule.VOID_IF_NOT_STARTING:
            return 0.0
        if count == candidate.quote.line_value:
            return 0.0
        over_wins = count > candidate.quote.line_value
        wins = over_wins if candidate.side == "over" else not over_wins
        return candidate.price - 1.0 if wins else -1.0

    for scenario, weight, minutes in (
        ("start", projection.lineup.p_start, projection.lineup.exp_minutes_start),
        ("sub", projection.lineup.p_sub_appearance, projection.lineup.exp_minutes_sub),
    ):
        distribution = projection.count_distribution(minutes)
        for count in range(301):
            probability_mass = float(distribution.pmf(count)) if count < 300 else float(distribution.sf(299))
            if count == 300:
                probability = weight * probability_mass
            else:
                probability = weight * probability_mass
            if probability > 0.0:
                brute_probabilities.append(probability)
                brute_payoffs.append([brute_payoff(candidate, scenario, count) for candidate in candidates])
    brute_probabilities.append(projection.lineup.p_no_appearance)
    brute_payoffs.append([brute_payoff(candidate, "absent", 0) for candidate in candidates])
    brute_probabilities_array = np.asarray(brute_probabilities, dtype=np.float64)
    brute_payoffs_array = np.asarray(brute_payoffs, dtype=np.float64)
    _, brute_covariance = return_moments(brute_probabilities_array, brute_payoffs_array)
    np.testing.assert_allclose(covariance, brute_covariance, atol=1e-12)


def test_single_candidate_matches_closed_form_optimum() -> None:
    mean_returns = np.array([0.08], dtype=np.float64)
    covariance = np.array([[0.5]], dtype=np.float64)
    upper = np.array([0.02], dtype=np.float64)
    fractions, solver = solve_mean_variance(mean_returns, covariance, upper, 0.02, 2.0)
    assert solver == "single"
    assert fractions[0] == pytest.approx(0.02)


def test_higher_risk_aversion_reduces_exposure() -> None:
    mean_returns = np.array([0.05], dtype=np.float64)
    covariance = np.array([[0.8]], dtype=np.float64)
    upper = np.array([0.02], dtype=np.float64)
    low_risk, _ = solve_mean_variance(mean_returns, covariance, upper, 0.02, 1.0)
    high_risk, _ = solve_mean_variance(mean_returns, covariance, upper, 0.02, 4.0)
    assert high_risk.sum() <= low_risk.sum()


def test_fixture_cap_scales_correlated_positions() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projections = {
        (1001, player_id, "tackles"): _reference_factor(1001, player_id)
        for player_id in (55, 56, 57)
    }
    quotes = [
        _reference_quote(projection, 1.5, 0.06, f"Book{player_id}", now)
        for (_, player_id, _), projection in projections.items()
    ]
    report = _manager(10_000.0).allocate(projections, quotes, now=now, exposure=NO_EXPOSURE)
    assert sum(directive.fraction for directive in report.directives) <= 0.05001
    assert sum(directive.stake for directive in report.directives) <= 500.0


def test_certain_outcome_is_flagged_not_staked() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = FactorProjection(1, 2, "tackles", GammaPosterior(3.0, 2.0), 6.0, LineupExpectation(0.0, 0.0))
    quote = LiveOddsQuote(1, 2, "tackles", 0.5, None, 1.5, 100.0, "Book", now, VoidRule.NO_VOID)
    report = _manager(1_000.0).allocate({(1, 2, "tackles"): projection}, [quote], now=now, exposure=NO_EXPOSURE)
    assert not report.directives
    assert any("degenerate outcome" in reason for _, reason in report.skipped)


def test_best_price_kept_and_stale_quotes_dropped() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = _reference_factor()
    best = _reference_quote(projection, 1.5, 0.06, "BookA", now)
    assert best.over_price is not None
    better = LiveOddsQuote(**{**best.__dict__, "over_price": best.over_price + 0.05, "sportsbook": "BookB"})
    stale = LiveOddsQuote(**{**best.__dict__, "captured_at": now - timedelta(minutes=6)})
    report = _manager(10_000.0).allocate(
        {(1001, 55, "tackles"): projection}, [best, better, stale], now=now, exposure=NO_EXPOSURE
    )
    assert report.stale_quotes == 1
    assert any(directive.sportsbook == "BookB" for directive in report.directives)


def test_sub_minimum_stakes_recorded_in_skipped_directives() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = _reference_factor()
    quote = _reference_quote(projection, 1.5, 0.06, "Book", now)
    report = _manager(10.0, min_stake=1.0).allocate(
        {(1001, 55, "tackles"): projection}, [quote], now=now, exposure=NO_EXPOSURE
    )
    assert not report.directives
    assert report.skipped_directives


def test_devig_sums_to_one() -> None:
    over_probability, under_probability = devig_two_way(1.9, 1.9)
    assert over_probability + under_probability == pytest.approx(1.0)


def test_allocate_requires_explicit_utc_time_and_exposure() -> None:
    manager = _manager(100.0)
    with pytest.raises(ValueError):
        manager.allocate({}, [], now=datetime(2026, 1, 1), exposure=NO_EXPOSURE)
    with pytest.raises(TypeError):
        manager.allocate({}, [])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        manager.allocate({}, [], now=datetime(2026, 1, 1, tzinfo=timezone.utc))  # type: ignore[call-arg]


# ---------------- hardened allocator (S1-S7) ----------------
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
STARTER = LineupExpectation(1.0, 0.0)
AUDIT_POSTERIOR = GammaPosterior(29.2, 11.944)


def _quote(
    fixture_id: int, player_id: int, line: float, over: float | None, under: float | None,
    max_stake: float = 1e9, book: str = "A", at: datetime = NOW,
) -> LiveOddsQuote:
    return LiveOddsQuote(fixture_id, player_id, "tackles", line, over, under, max_stake, book, at,
                         VoidRule.VOID_IF_NO_APPEARANCE)


def _audit_fixture(n_players: int = 4) -> dict[ProjectionKey, FactorProjection]:
    return {
        (9, player, "tackles"): FactorProjection(9, player, "tackles", AUDIT_POSTERIOR, 6.0, STARTER)
        for player in range(1, n_players + 1)
    }


def _book_from(report: AllocationReport, book: dict[ProjectionKey, float]) -> dict[ProjectionKey, float]:
    for directive in report.directives:
        key = (directive.fixture_id, directive.player_id, directive.market_type)
        book[key] = book.get(key, 0.0) + directive.stake
    return book


def test_exposure_book_stops_restaking_a_persistent_edge() -> None:
    """S1: four cycles feeding stakes back keep the fixture at 5% (previously 5.0, 9.8, 14.3, 18.5%)."""
    projections = _audit_fixture()
    quotes = [_quote(9, player, 1.5, 1.714, None) for player in range(1, 5)]
    manager = _manager(1e6)
    book: dict[ProjectionKey, float] = {}
    issued: list[float] = []
    for _ in range(4):
        report = manager.allocate(projections, quotes, now=NOW, exposure=ExposureBook(dict(book)))
        issued.append(math.fsum(d.stake for d in report.directives))
        _book_from(report, book)
    assert issued[0] > 0.0 and issued[1:] == [0.0, 0.0, 0.0]
    assert math.fsum(book.values()) <= 0.05 * 1e6 + 0.01
    last = manager.allocate(projections, quotes, now=NOW, exposure=ExposureBook(dict(book)))
    assert all("cap already used" in reason for _, reason in last.skipped)


def test_exposure_book_caps_each_market_net_of_its_open_position() -> None:
    projections = _audit_fixture(1)
    quote = _quote(9, 1, 1.5, 1.714, None)
    first = _manager(1e6).allocate(projections, [quote], now=NOW, exposure=NO_EXPOSURE)
    assert 0.01 < first.directives[0].fraction <= 0.02
    open_amount = 0.015 * 1e6
    second = _manager(1e6).allocate(
        projections, [quote], now=NOW, exposure=ExposureBook({(9, 1, "tackles"): open_amount})
    )
    available = 1e6 - open_amount
    assert second.directives[0].stake + open_amount <= 0.02 * available + 0.01
    assert second.directives[0].stake < first.directives[0].stake


def test_exposure_book_rejects_invalid_amounts() -> None:
    for amount in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ExposureBook({(1, 2, "tackles"): amount})


def test_exposure_book_rejects_malformed_keys() -> None:
    for key in ((1, 2), (1, "2", "tackles"), (True, 2, "tackles"), (1, 2, ""), "1-2-tackles"):
        with pytest.raises(ValueError):
            ExposureBook({key: 1.0})  # type: ignore[dict-item]


def test_exposure_book_is_isolated_from_later_mutation_of_its_input() -> None:
    source = {(9, 1, "tackles"): 100.0}
    book = ExposureBook(source)
    source[(9, 1, "tackles")] = -1e9
    source[(9, 2, "tackles")] = 50.0
    assert book.total == 100.0 and book.market_total((9, 2, "tackles")) == 0.0
    with pytest.raises(TypeError):
        book.by_market[(9, 1, "tackles")] = -1.0  # type: ignore[index]


def test_allocate_rejects_projection_filed_under_another_key() -> None:
    misfiled = FactorProjection(999, 999, "fouls", AUDIT_POSTERIOR, 6.0, STARTER)
    with pytest.raises(ValueError, match="identifies itself as"):
        _manager(1e6).allocate(
            {(9, 1, "tackles"): misfiled}, [_quote(9, 1, 1.5, 1.714, None)], now=NOW, exposure=NO_EXPOSURE
        )


def _near_certain_under(price: float) -> tuple[dict[ProjectionKey, FactorProjection], LiveOddsQuote]:
    """0.31 tackles per 90 (posterior 14.4 / 46.0): Under 2.5 loses well under 1% of the time."""
    projection = FactorProjection(7, 7, "tackles", GammaPosterior(14.4, 46.0), 6.0, STARTER)
    return {(7, 7, "tackles"): projection}, LiveOddsQuote(
        7, 7, "tackles", 2.5, 8.0, price, 1e9, "A", NOW, VoidRule.VOID_IF_NO_APPEARANCE
    )


def test_loss_floor_removes_the_near_certain_under() -> None:
    """S2: at 1.05 this Under takes the full 2% cap ($20,000 to win $1,000) without the floor and nothing with
    it (haircut EV 0.97 x 0.05 - 0.03 < alpha 0.02). At 1.06 the haircut EV is 0.028 > alpha, so the
    allocator still sizes it; the value screen's 4% minimum stops the alert (test_value_screen)."""
    projections, quote = _near_certain_under(1.05)
    probabilities = projections[(7, 7, "tackles")].side_probabilities(2.5, "under")
    assert DEGENERATE_LOSS_PROBABILITY < probabilities.p_lose < 0.01
    unfloored = _manager(1e6, floor=0.0).allocate(projections, [quote], now=NOW, exposure=NO_EXPOSURE)
    assert [(d.side, d.stake) for d in unfloored.directives] == [("under", 20_000.0)]
    floored = _manager(1e6).allocate(projections, [quote], now=NOW, exposure=NO_EXPOSURE)
    assert not floored.directives
    assert any(key == (7, 7, "tackles") for key, _ in floored.skipped)



def test_stressed_probabilities_move_win_mass_to_loss_only() -> None:
    stressed = stressed_probabilities(SideProbabilities(0.9, 0.004, 0.096), 0.03)
    assert (stressed.p_win, stressed.p_lose, stressed.p_void) == pytest.approx((0.874, 0.03, 0.096))
    untouched = SideProbabilities(0.5, 0.4, 0.1)
    assert stressed_probabilities(untouched, 0.03) == untouched
    with pytest.raises(ValueError):
        stressed_probabilities(untouched, 1.0)
    with pytest.raises(ValueError):
        _manager(1e6, floor=-0.01)
    with pytest.raises(TypeError):
        InstitutionalAssetManager(1e6)  # type: ignore[call-arg]


def test_shared_game_state_covariance_matches_monte_carlo() -> None:
    """S3: four players in one fixture share G; the cross-player covariance matches simulation."""
    projections = _audit_fixture()
    price = 1.714
    manager = _manager(1e6)
    candidate_sets = [manager._candidates(projections[(9, p, "tackles")], [_quote(9, p, 1.5, price, None)])[0]
                      for p in range(1, 5)]
    nodes, weights = game_state_quadrature(6.0)
    grids = [[outcome_model(projections[(9, p, "tackles")].conditional(float(g)), candidates) for g in nodes]
             for p, candidates in zip(range(1, 5), candidate_sets)]
    mean, covariance = fixture_moments(grids, weights)
    rng = np.random.default_rng(1)
    n = 400_000
    shared = rng.gamma(6.0, 1.0 / 6.0, n)
    returns = np.array([
        np.where(rng.poisson(rng.gamma(29.2, 1.0 / 11.944, n) * shared * 85.0 / 90.0) > 1.5, price - 1.0, -1.0)
        for _ in range(4)
    ])
    simulated = np.cov(returns)
    assert abs(covariance[0, 1] - simulated[0, 1]) < 3e-3
    assert covariance[0, 1] > 0.05
    assert mean[0] == pytest.approx(float(returns[0].mean()), abs=5e-3)
    marginal = projections[(9, 1, "tackles")].side_probabilities(1.5, "over").p_win
    assert marginal == pytest.approx(float(np.mean(returns[0] > 0.0)), abs=3e-3)


def test_game_state_quadrature_is_exact_for_the_first_moments_and_large_k() -> None:
    for k in (0.5, 6.0, 1e3, 1e6):
        nodes, weights = game_state_quadrature(k)
        assert float(weights @ nodes) == pytest.approx(1.0, abs=1e-12)
        assert float(weights @ (nodes - 1.0) ** 2) == pytest.approx(1.0 / k, rel=1e-9)
    with pytest.raises(ProjectionError):
        game_state_quadrature(0.0)


def test_corrupt_factor_projection_is_skipped_never_traded() -> None:
    healthy = _reference_factor()
    corrupt = FactorProjection(1001, 99, "tackles", GammaPosterior(3.0, 1.5), 6.0, LineupExpectation(1.0, 0.0))
    object.__setattr__(corrupt.rate_posterior, "alpha", float("nan"))  # simulates a validator bypass
    quotes = [
        _reference_quote(healthy, 1.5, 0.06, "BookA", NOW),
        LiveOddsQuote(1001, 99, "tackles", 2.5, 1.9, 1.25, 500.0, "BookA", NOW, VoidRule.VOID_IF_NO_APPEARANCE),
    ]
    report = _manager(10_000.0).allocate(
        {(1001, 55, "tackles"): healthy, (1001, 99, "tackles"): corrupt}, quotes, now=NOW, exposure=NO_EXPOSURE
    )
    assert report.directives and all(d.player_id == 55 for d in report.directives)
    assert any(key == (1001, 99, "tackles") and reason.startswith("projection unusable") for key, reason in report.skipped)


def test_void_rule_must_be_stated_on_every_quote() -> None:
    """S4: a book that settles NO_VOID must not be priced as VOID_IF_NO_APPEARANCE by default."""
    with pytest.raises(TypeError):
        LiveOddsQuote(1, 2, "tackles", 1.5, 1.9, None, 100.0, "Book", NOW)  # type: ignore[call-arg]


def test_future_dated_quote_is_skipped_and_counted() -> None:
    """S5: one book clock 2 s fast no longer blocks every other market."""
    projections = _audit_fixture(2)
    quotes = [_quote(9, 1, 1.5, 1.714, None), _quote(9, 2, 1.5, 1.714, None, at=NOW + timedelta(seconds=2))]
    report = _manager(1e6).allocate(projections, quotes, now=NOW, exposure=NO_EXPOSURE)
    assert report.future_quotes == 1
    assert [d.player_id for d in report.directives] == [1]
    assert any(key == (9, 2, "tackles") and "clock skew" in reason for key, reason in report.skipped)


def test_small_limit_book_no_longer_hides_a_deep_book() -> None:
    """S6: a $5-limit book at a better price and a $50,000 book both get directives, within their limits."""
    projections = _audit_fixture(1)
    quotes = [_quote(9, 1, 1.5, 1.72, None, max_stake=5.0, book="Tiny"),
              _quote(9, 1, 1.5, 1.71, None, max_stake=50_000.0, book="Deep")]
    report = _manager(1e6).allocate(projections, quotes, now=NOW, exposure=NO_EXPOSURE)
    stakes = {d.sportsbook: d.stake for d in report.directives}
    assert set(stakes) == {"Tiny", "Deep"}
    assert stakes["Tiny"] <= 5.0 and stakes["Deep"] > 1_000.0
    assert math.fsum(d.fraction for d in report.directives) <= 0.02 + 1e-9


def test_book_limit_is_in_the_optimiser_not_clipped_afterwards() -> None:
    projections = _audit_fixture(1)
    report = _manager(1e6).allocate(projections, [_quote(9, 1, 1.5, 1.714, None, max_stake=50.0)],
                                    now=NOW, exposure=NO_EXPOSURE)
    (directive,) = report.directives
    assert directive.stake == pytest.approx(50.0) and directive.fraction == pytest.approx(50.0 / 1e6)
    (allocation,) = report.allocations
    assert allocation.expected_return == pytest.approx(directive.fraction * directive.ev_per_unit, rel=1e-3)


def test_directive_carries_price_floor_and_expiry() -> None:
    """S7: min_acceptable_odds is the alpha_threshold price rounded up to 0.001; expiry is capture + 5 min."""
    projections = _audit_fixture(1)
    report = _manager(1e6).allocate(projections, [_quote(9, 1, 1.5, 1.714, None)], now=NOW, exposure=NO_EXPOSURE)
    (directive,) = report.directives
    probabilities = _stressed_audit_probabilities(directive.line_value, directive.side)
    expected_floor = math.ceil(price_for_ev(probabilities, 0.02) * 1000.0 - 1e-9) / 1000.0
    assert directive.min_acceptable_odds == pytest.approx(expected_floor, abs=1e-12)
    assert directive.min_acceptable_odds <= directive.decimal_odds
    assert directive.expires_at == NOW + timedelta(minutes=5)


def _stressed_audit_probabilities(line: float, side: str) -> SideProbabilities:
    projection = FactorProjection(9, 1, "tackles", AUDIT_POSTERIOR, 6.0, STARTER)
    return stressed_probabilities(projection.side_probabilities(line, side), MODEL_RISK_LOSS_FLOOR)


# ---------------- engine: dependence and fail-closed allocation ----------------
def test_bivariate_marginal_is_equidispersed_poisson() -> None:
    lambda_x, lambda_y, lambda_shared = 1.3, 0.8, 0.4
    engine = QuantEngine(game_state_k=6.0)
    marginal = np.array([
        math.fsum(engine.bivariate_poisson_pmf(x, y, lambda_x, lambda_y, lambda_shared) for y in range(60))
        for x in range(40)
    ])
    support = np.arange(40)
    mean = float(support @ marginal)
    variance = float((support - mean) ** 2 @ marginal)
    assert mean == pytest.approx(lambda_x + lambda_shared, rel=1e-10)
    assert variance == pytest.approx(mean, rel=1e-10)
    assert engine.bivariate_correlation(lambda_x, lambda_y, lambda_shared) > 0.0


def test_joint_over_probability_matches_monte_carlo() -> None:
    engine = QuantEngine(game_state_k=6.0)
    exact = engine.joint_over_probability(1.5, 0.5, 1.3, 0.8, 0.4)
    xs, ys = engine.sample_bivariate_poisson(1.3, 0.8, 0.4, 200_000, np.random.default_rng(7))
    assert exact == pytest.approx(float(np.mean((xs > 1.5) & (ys > 0.5))), abs=0.005)


def test_projection_error_raised_for_non_finite_multiplier() -> None:
    with pytest.raises(ProjectionError):
        QuantEngine(game_state_k=6.0).predictive_gamma(GammaPosterior(4.0, 2.0), float("nan"), 0.0)


# ---------------- transforms ----------------
def test_parse_valid_payload(fixture_payload: Callable[..., dict[str, Any]]) -> None:
    parsed = parse_fixture_payload(fixture_payload())
    assert parsed.fixture.status == "final"
    assert parsed.referee is not None and parsed.referee.vendor_uid == "R-1"
    starter = parsed.players[0]
    assert (starter.minute_on, starter.minute_off, starter.minutes_played) == (0, 90, 90)
    assert (starter.state_leading_min, starter.state_level_min, starter.state_trailing_min) == (45, 45, 0)
    substitute = parsed.players[1]
    assert (substitute.minute_on, substitute.minute_off) == (65, 90)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("stats", "tackles_won"), 9),
        (("stats", "duels_won"), 20),
        (("stats", "yellow_cards"), 3),
        (("stats", "red_cards"), 2),
        (("stats", "fouls_committed"), -1),
        (("team_uid",), "T-OTHER"),
    ],
)
def test_parse_rejects_impossible_stats(
    fixture_payload: Callable[..., dict[str, Any]], path: tuple[str, ...], value: object
) -> None:
    payload = fixture_payload()
    target: dict[str, Any] = payload["players"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        parse_fixture_payload(payload)


def test_parse_rejects_malformed_fixtures(fixture_payload: Callable[..., dict[str, Any]]) -> None:
    unknown_status = fixture_payload()
    unknown_status["fixture"]["status"] = "abandoned"
    same_sides = fixture_payload()
    same_sides["fixture"]["away_team_uid"] = same_sides["fixture"]["home_team_uid"]
    duplicate = fixture_payload()
    duplicate["players"][1]["uid"] = duplicate["players"][0]["uid"]
    for payload in (unknown_status, same_sides, duplicate):
        with pytest.raises(ValueError):
            parse_fixture_payload(payload)


def test_default_kappas_are_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT_KAPPAS["tackles"] = 2.0  # type: ignore[index]


def test_padj_scale_is_finite_at_extreme_possession() -> None:
    assert padj_scale(4, 100.0, 1.0) == pytest.approx(4 * 50.0 / 1.0)
    assert padj_scale(4, 0.0, 1.0) == pytest.approx(4 * 50.0 / 99.0)
    assert padj_scale(4, 50.0, 0.7) == pytest.approx(4.0)
    assert padj_scale(4, None, 1.0) == 4.0


def test_dribbled_past_rate_undefined_without_duels() -> None:
    assert dribbled_past_rate(0, 0) is None
    assert dribbled_past_rate(1, 3) == pytest.approx(0.25)


def test_match_state_minutes_partition_time_on_pitch() -> None:
    goals = [GoalEvent(20, "H"), GoalEvent(55, "A"), GoalEvent(80, "H")]
    assert match_state_minutes(goals, "H", 0, 90) == (45, 45, 0)
    assert match_state_minutes(goals, "A", 30, 90) == (0, 25, 35)
    assert match_state_minutes(goals, "H", 70, 70) == (0, 0, 0)


def test_lineup_payload_assigns_start_and_bench_probabilities() -> None:
    entries = parse_lineup_payload(make_lineup_payload(n_bench=2), 0.35)
    assert len(entries) == 26
    assert sum(entry.p_start for entry in entries) == 22.0
    assert {entry.p_sub_appearance for entry in entries if entry.p_start == 0.0} == {0.35}


def test_lineup_payload_rejects_incomplete_or_duplicated_sheets() -> None:
    missing_side = make_lineup_payload()
    del missing_side["lineups"]["T-AWAY"]
    ten_starters = make_lineup_payload()
    ten_starters["lineups"]["T-HOME"]["starters"].pop()
    duplicated = make_lineup_payload()
    duplicated["lineups"]["T-AWAY"]["bench"].append(duplicated["lineups"]["T-HOME"]["starters"][0])
    for payload in (missing_side, ten_starters, duplicated):
        with pytest.raises(ValueError):
            parse_lineup_payload(payload, 0.35)
    with pytest.raises(ValueError):
        parse_lineup_payload(make_lineup_payload(), 1.5)


# ---------------- estimators ----------------
def test_poisson_glm_recovers_coefficients_with_standard_errors() -> None:
    rng = np.random.default_rng(11)
    covariate = rng.normal(size=4000)
    exposure = rng.uniform(0.3, 1.2, size=4000)
    counts = rng.poisson(exposure * np.exp(0.4 + 0.3 * covariate))
    result = fit_poisson_glm(np.column_stack([np.ones(4000), covariate]), counts, offset=np.log(exposure))
    np.testing.assert_allclose(result.coefficients, [0.4, 0.3], atol=4 * result.standard_errors.max())
    assert (result.standard_errors > 0.0).all() and result.standard_errors.max() < 0.05


def test_poisson_glm_rejects_rank_deficient_design() -> None:
    column = np.arange(10.0)
    with pytest.raises(ValueError):
        fit_poisson_glm(np.column_stack([column, 2.0 * column]), np.ones(10))


def test_possession_kappa_recovered() -> None:
    rng = np.random.default_rng(12)
    possession = rng.uniform(20.0, 80.0, size=40_000)
    minutes = rng.uniform(45.0, 90.0, size=40_000)
    counts = rng.poisson(minutes / 90.0 * 2.0 * ((100.0 - possession) / 50.0) ** 0.8)
    assert fit_possession_kappa(counts, minutes, possession) == pytest.approx(0.8, abs=0.08)


def test_referee_betas_recovered() -> None:
    rng = np.random.default_rng(13)
    n = 6000
    strictness = rng.uniform(0.7, 1.4, size=n)
    bias = rng.uniform(-0.2, 0.2, size=n)
    is_home = rng.random(n) < 0.5
    expected = rng.uniform(0.8, 2.0, size=n)
    venue = np.where(is_home, 1.0 - bias, 1.0 + bias)
    counts = rng.poisson(expected * strictness**1.2 * venue**0.9)
    beta1, beta2 = fit_referee_betas(counts, expected, strictness, bias, is_home)
    assert beta1 == pytest.approx(1.2, abs=0.12)
    assert beta2 == pytest.approx(0.9, abs=0.3)


def test_game_state_dispersion_recovers_k() -> None:
    rng = np.random.default_rng(14)
    mu = rng.uniform(1.0, 3.0, size=40_000)
    k = 5.0
    overdispersed = rng.poisson(mu * rng.gamma(k, 1.0 / k, size=mu.size))
    assert fit_game_state_dispersion(overdispersed, mu) == pytest.approx(k, rel=0.15)


def test_game_state_dispersion_raises_instead_of_returning_poisson() -> None:
    """K1: no overdispersion, or too few matches, raises; the caller keeps its previously fitted k."""
    assert MIN_GAME_STATE_OBSERVATIONS == 200
    flat_counts, flat_mu = np.array([1, 2] * 100), np.full(200, 1.5)
    with pytest.raises(ValueError, match="no overdispersion"):
        fit_game_state_dispersion(flat_counts, flat_mu)
    rng = np.random.default_rng(15)
    mu = rng.uniform(1.0, 3.0, size=199)
    with pytest.raises(ValueError, match="at least 200"):
        fit_game_state_dispersion(rng.poisson(mu * rng.gamma(6.0, 1.0 / 6.0, size=199)), mu)


def test_bench_sub_probability_shrinks_towards_the_league_mean() -> None:
    """K2: no history gives the league mean; growing history moves towards the player's own rate."""
    assert bench_sub_probability(0, 0, 0.35, 10.0) == pytest.approx(0.35)
    few = bench_sub_probability(5, 5, 0.35, 10.0)
    many = bench_sub_probability(200, 200, 0.35, 10.0)
    assert 0.35 < few < many < 1.0
    assert bench_sub_probability(200, 20, 0.35, 10.0) == pytest.approx((20 + 3.5) / 210.0)
    for args in ((-1, 0, 0.35, 10.0), (3, 4, 0.35, 10.0), (3, 1, 1.0, 10.0), (3, 1, 0.35, 0.0)):
        with pytest.raises(ValueError):
            bench_sub_probability(*args)


def test_state_duel_density_exact_on_noiseless_data() -> None:
    leading = np.array([30.0, 0.0, 10.0, 40.0])
    level = np.array([30.0, 40.0, 60.0, 50.0])
    trailing = np.array([30.0, 50.0, 20.0, 0.0])
    duels = (leading * 9.0 + level * 18.0 + trailing * 27.0) / 90.0
    np.testing.assert_array_equal(duels, np.round(duels))
    np.testing.assert_allclose(fit_state_duel_density(duels, leading, level, trailing), [9.0, 18.0, 27.0], atol=1e-9)


# ---------------- referee coefficients ----------------
def test_referee_coefficients_shrink_toward_league() -> None:
    rows = [FixtureDiscipline(1, 7, f, 14, 16, 4, 0) for f in range(20)]
    rows += [FixtureDiscipline(2, 7, 100 + f, 9, 11, 2, 0) for f in range(2)]
    strict, lenient = compute_referee_coefficients(rows, shrink_matches=15.0)
    assert (strict.referee_id, lenient.referee_id) == (1, 2)
    league_fouls_per_match = (20 * 30 + 2 * 20) / 22
    assert strict.strictness_coefficient > 1.0 > lenient.strictness_coefficient
    raw_lenient = 20 / league_fouls_per_match
    assert raw_lenient < lenient.strictness_coefficient < 1.0
    assert 0.0 < strict.home_bias_ratio < 1.0


def test_referee_coefficients_reject_bad_input() -> None:
    with pytest.raises(ValueError):
        compute_referee_coefficients([FixtureDiscipline(1, 7, 1, 0, 0, 0, 0)])
    with pytest.raises(ValueError):
        FixtureDiscipline(1, 7, 1, -1, 0, 0, 0)
    with pytest.raises(ValueError):
        compute_referee_coefficients([FixtureDiscipline(1, 7, 1, 5, 5, 1, 0)] * 2)


# ---------------- evaluation ----------------
def test_scoring_rules_on_known_values() -> None:
    assert brier_score([0.8, 0.3], [1, 0]) == pytest.approx((0.04 + 0.09) / 2)
    assert log_loss([0.8, 0.3], [1, 0]) == pytest.approx(-(math.log(0.8) + math.log(0.7)) / 2)
    assert log_loss([0.0], [1]) == math.inf
    with pytest.raises(ValueError):
        brier_score([1.2], [1])


def test_calibration_table_bins_predictions() -> None:
    table = calibration_table([0.05, 0.15, 0.12, 1.0], [0, 1, 0, 1], n_bins=10)
    assert [(b.lower, b.n) for b in table] == [(0.0, 1), (0.1, 2), (0.9, 1)]
    assert table[1].observed_rate == pytest.approx(0.5)


def test_closing_line_value_uses_devigged_close() -> None:
    assert closing_line_value(2.10, 1.90, 1.90) == pytest.approx(2.10 * 0.5 - 1.0)


def _observation(
    fixture: int, day: int, started: bool, minutes: float, count: int, player: int = 9
) -> PlayerMatchObservation:
    kickoff = datetime(2026, 1, day, 15, tzinfo=timezone.utc)
    return PlayerMatchObservation(player, fixture, kickoff, started, minutes, count)


def test_walk_forward_uses_only_strictly_prior_observations() -> None:
    observations = [
        _observation(1, 1, True, 90.0, 3),
        _observation(2, 8, True, 88.0, 1),
        _observation(3, 8, True, 90.0, 6, player=10),
        _observation(4, 15, False, 0.0, 0),
        _observation(5, 22, False, 30.0, 2),
    ]
    engine = QuantEngine(game_state_k=6.0)
    predictions = walk_forward_over_probability(observations, 1.5, 2.2, engine)
    by_fixture = {p.fixture_id: p for p in predictions}
    assert set(by_fixture) == {1, 2, 3, 5}
    assert (by_fixture[1].n_history, by_fixture[2].n_history, by_fixture[5].n_history) == (0, 1, 3)
    assert (by_fixture[1].outcome, by_fixture[2].outcome, by_fixture[5].outcome) == (1, 0, 1)
    later = walk_forward_over_probability(observations + [_observation(6, 29, True, 90.0, 9)], 1.5, 2.2, engine)
    assert {p.fixture_id: p.p_over for p in later if p.fixture_id != 6} == {p.fixture_id: p.p_over for p in predictions}
    with pytest.raises(ValueError):
        walk_forward_over_probability(observations, 2.0, 2.2, engine)


def test_summarize_reports_brier_skill_against_climatology() -> None:
    predictions = walk_forward_over_probability(
        [_observation(i, 1 + i, True, 90.0, count) for i, count in enumerate([0, 3, 1, 4, 2, 0, 5, 1])],
        1.5,
        2.0,
        QuantEngine(game_state_k=6.0),
    )
    summary = summarize(predictions)
    assert summary["n"] == 8.0
    assert summary["brier_skill"] == pytest.approx(1.0 - summary["brier"] / summary["brier_climatology"])
    with pytest.raises(ValueError):
        summarize([])
