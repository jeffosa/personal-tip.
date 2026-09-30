from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Sequence, cast

import numpy as np
import pytest

from prop_alpha.modeling.engine import (
    MAX_RELATIVE_VARIANCE,
    MIN_RELATIVE_VARIANCE,
    GammaPosterior,
    LineupExpectation,
    ProjectionError,
    PropProjection,
    QuantEngine,
    RefereeProfile,
    VoidRule,
    validate_exposure_vector,
)
from prop_alpha.scanner.market_scanner import (
    Candidate,
    InstitutionalAssetManager,
    LiveOddsQuote,
    devig_two_way,
    kelly_fraction_with_push,
    outcome_model,
    price_for_ev,
    return_moments,
    solve_mean_variance,
)


def test_valid_vectors_are_returned_typed() -> None:
    minutes, counts = validate_exposure_vector([90.0, 0.0], [3, 0])
    assert minutes.dtype == np.float64
    assert counts.dtype == np.int64
    np.testing.assert_array_equal(minutes, np.array([90.0, 0.0]))
    np.testing.assert_array_equal(counts, np.array([3, 0]))


@pytest.mark.parametrize(
    ("minutes", "counts"),
    [
        ([90.0], [2.7]),
        ([float("nan")], [1]),
        ([float("inf")], [1]),
        ([90.0], [float("nan")]),
        ([90.0], [float("inf")]),
        ([90.0], [True]),
        ([0.0], [3]),
        ([131.0], [1]),
        ([90.0, 45.0], [1]),
        ([], []),
        ([90.0], [-1]),
    ],
)
def test_polluted_vectors_are_rejected(minutes: object, counts: object) -> None:
    with pytest.raises(ValueError):
        validate_exposure_vector(cast(Sequence[float], minutes), cast(Sequence[int], counts))


def test_gamma_update_uses_validation_and_allows_prior_only_history() -> None:
    prior = GammaPosterior(alpha=6.0, beta=3.0)
    posterior = QuantEngine.gamma_poisson_update(prior, [], [])
    assert posterior == prior
    updated = QuantEngine.gamma_poisson_update(prior, [2, 1], [90.0, 45.0])
    assert updated.alpha == 9.0
    assert updated.beta == 4.5
    with pytest.raises(ValueError):
        QuantEngine.gamma_poisson_update(prior, [1], [0.0])


def test_predictive_gamma_exact_without_game_state() -> None:
    posterior = GammaPosterior(alpha=12.0, beta=6.0)
    shape, rate = QuantEngine().predictive_gamma(posterior, 2.0)
    assert shape == posterior.alpha
    assert rate == posterior.beta / 2.0


def test_predictive_is_negative_binomial_with_posterior_mean() -> None:
    projection = PropProjection(1, 2, "tackles", 8.0, 4.0, LineupExpectation(1.0, 0.0))
    distribution = projection.count_distribution(90.0)
    assert float(distribution.mean()) == pytest.approx(2.0)


def test_game_state_mixing_widens_but_preserves_mean() -> None:
    posterior = GammaPosterior(alpha=9.0, beta=4.0)
    plain_shape, plain_rate = QuantEngine().predictive_gamma(posterior, 1.5)
    mixed_shape, mixed_rate = QuantEngine(game_state_k=4.0).predictive_gamma(posterior, 1.5)
    assert mixed_shape < plain_shape
    assert mixed_shape / mixed_rate == pytest.approx(plain_shape / plain_rate)


def test_predictive_gamma_stable_where_subtraction_form_cancels() -> None:
    posterior = GammaPosterior(alpha=1e17, beta=1e17 / 2.0)
    shape, rate = QuantEngine().predictive_gamma(posterior, 1.0)
    assert shape == 1.0 / MIN_RELATIVE_VARIANCE
    assert math.isfinite(shape) and math.isfinite(rate) and rate > 0.0


def test_predictive_gamma_bounded_under_extreme_overdispersion() -> None:
    shape, _ = QuantEngine(game_state_k=1e-9).predictive_gamma(GammaPosterior(4.0, 2.0), 1.0)
    assert shape == 1.0 / MAX_RELATIVE_VARIANCE


def test_math_clamp_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="prop_alpha.engine"):
        QuantEngine(game_state_k=1e-9).predictive_gamma(GammaPosterior(4.0, 2.0), 1.0)
    assert "[MATH_CLAMP]" in caplog.text


def test_no_math_clamp_for_ordinary_inputs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="prop_alpha.engine"):
        QuantEngine(game_state_k=6.0).predictive_gamma(GammaPosterior(20.0, 10.0), 1.0)
    assert "[MATH_CLAMP]" not in caplog.text


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


def _reference_quote(
    projection: PropProjection,
    line: float,
    target_ev: float,
    sportsbook: str,
    now: object,
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
    )


def test_mean_variance_prefers_low_variance_line_at_similar_ev() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = _reference_projection()
    quotes = [
        _reference_quote(projection, 1.5, 0.060, "BookA", now),
        _reference_quote(projection, 2.5, 0.062, "BookB", now),
    ]
    report = InstitutionalAssetManager(10_000.0).allocate(
        {(1001, 55, "tackles"): projection}, quotes, now=now
    )
    fractions = {directive.line_value: directive.fraction for directive in report.directives}
    assert fractions[1.5] == pytest.approx(0.0194, abs=0.0002)
    assert fractions[2.5] == pytest.approx(0.0004, abs=0.0002)


def test_return_moments_match_closed_form() -> None:
    probabilities = np.array([0.25, 0.75], dtype=np.float64)
    payoffs = np.array([[1.5, -1.0], [-1.0, 0.4]], dtype=np.float64)
    mean_returns, covariance = return_moments(probabilities, payoffs)
    expected_mean = probabilities @ payoffs
    expected_covariance = payoffs.T @ (payoffs * probabilities[:, None]) - np.outer(expected_mean, expected_mean)
    np.testing.assert_allclose(mean_returns, expected_mean)
    np.testing.assert_allclose(covariance, expected_covariance)


def test_cross_line_covariance_matches_brute_force() -> None:
    projection = _reference_projection()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quotes = [
        _reference_quote(projection, 1.5, 0.060, "BookA", now),
        _reference_quote(projection, 2.5, 0.062, "BookB", now),
    ]
    manager = InstitutionalAssetManager(10_000.0)
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
        (1001, player_id, "tackles"): _reference_projection(1001, player_id)
        for player_id in (55, 56, 57)
    }
    quotes = [
        _reference_quote(projection, 1.5, 0.06, f"Book{player_id}", now)
        for (_, player_id, _), projection in projections.items()
    ]
    report = InstitutionalAssetManager(10_000.0).allocate(projections, quotes, now=now)
    assert sum(directive.fraction for directive in report.directives) <= 0.05001
    assert sum(directive.stake for directive in report.directives) <= 500.0


def test_certain_outcome_is_flagged_not_staked() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = PropProjection(1, 2, "tackles", 3.0, 2.0, LineupExpectation(0.0, 0.0))
    quote = LiveOddsQuote(1, 2, "tackles", 0.5, None, 1.5, 100.0, "Book", now, VoidRule.NO_VOID)
    report = InstitutionalAssetManager(1_000.0).allocate({(1, 2, "tackles"): projection}, [quote], now)
    assert not report.directives
    assert any("degenerate outcome" in reason for _, reason in report.skipped)


def test_best_price_kept_and_stale_quotes_dropped() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = _reference_projection()
    best = _reference_quote(projection, 1.5, 0.06, "BookA", now)
    better = LiveOddsQuote(**{**best.__dict__, "over_price": best.over_price + 0.05, "sportsbook": "BookB"})
    stale = LiveOddsQuote(**{**best.__dict__, "captured_at": now - timedelta(minutes=6)})
    report = InstitutionalAssetManager(10_000.0).allocate(
        {(1001, 55, "tackles"): projection}, [best, better, stale], now=now
    )
    assert report.stale_quotes == 1
    assert any(directive.sportsbook == "BookB" for directive in report.directives)


def test_sub_minimum_stakes_recorded_in_skipped_directives() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    projection = _reference_projection()
    quote = _reference_quote(projection, 1.5, 0.06, "Book", now)
    report = InstitutionalAssetManager(10.0, min_stake=1.0).allocate(
        {(1001, 55, "tackles"): projection}, [quote], now=now
    )
    assert not report.directives
    assert report.skipped_directives


def test_devig_sums_to_one() -> None:
    over_probability, under_probability = devig_two_way(1.9, 1.9)
    assert over_probability + under_probability == pytest.approx(1.0)


def test_allocate_requires_explicit_utc_time() -> None:
    manager = InstitutionalAssetManager(100.0)
    with pytest.raises(ValueError):
        manager.allocate({}, [])