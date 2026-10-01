from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np
import pytest
from conftest import make_lineup_payload

from prop_alpha.calibration.fitting import (
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
    GammaPosterior,
    LineupExpectation,
    ProjectionError,
    PropProjection,
    QuantEngine,
    RefereeProfile,
    VoidRule,
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


@pytest.mark.parametrize(("p_start", "p_sub"), [(0.33, 0.67), (0.7, 0.3), (0.1, 0.9), (0.5, 0.5)])
def test_complementary_lineup_leaves_no_negative_absence_weight(p_start: float, p_sub: float) -> None:
    lineup = LineupExpectation(p_start, p_sub)
    assert lineup.p_no_appearance == 0.0
    projection = PropProjection(1, 2, "tackles", 7.0, 3.0, lineup)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    over_price = price_for_ev(projection.side_probabilities(1.5, "over"), 0.10)
    quote = LiveOddsQuote(1, 2, "tackles", 1.5, over_price, None, 100.0, "Book", now)
    candidates, _ = InstitutionalAssetManager(1_000.0)._candidates(projection, [quote])
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


# ---------------- engine: dependence and fail-closed allocation ----------------
def test_bivariate_marginal_is_equidispersed_poisson() -> None:
    lambda_x, lambda_y, lambda_shared = 1.3, 0.8, 0.4
    engine = QuantEngine()
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
    engine = QuantEngine()
    exact = engine.joint_over_probability(1.5, 0.5, 1.3, 0.8, 0.4)
    xs, ys = engine.sample_bivariate_poisson(1.3, 0.8, 0.4, 200_000, np.random.default_rng(7))
    assert exact == pytest.approx(float(np.mean((xs > 1.5) & (ys > 0.5))), abs=0.005)


def test_corrupted_projection_is_skipped_never_traded() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    healthy = _reference_projection()
    corrupt = PropProjection(1001, 99, "tackles", 3.0, 1.5, LineupExpectation(1.0, 0.0))
    object.__setattr__(corrupt, "gamma_shape", float("nan"))  # simulates a validator bypass
    quotes = [
        _reference_quote(healthy, 1.5, 0.06, "BookA", now),
        LiveOddsQuote(1001, 99, "tackles", 2.5, 1.9, 1.25, 500.0, "BookA", now),
    ]
    report = InstitutionalAssetManager(10_000.0).allocate(
        {(1001, 55, "tackles"): healthy, (1001, 99, "tackles"): corrupt}, quotes, now=now
    )
    assert report.directives
    assert all(directive.player_id == 55 for directive in report.directives)
    assert any(
        key == (1001, 99, "tackles") and reason.startswith("projection unusable") for key, reason in report.skipped
    )


def test_projection_error_raised_for_non_finite_multiplier() -> None:
    with pytest.raises(ProjectionError):
        QuantEngine().predictive_gamma(GammaPosterior(4.0, 2.0), float("nan"))


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


def test_game_state_dispersion_recovers_k_and_returns_none_without_overdispersion() -> None:
    rng = np.random.default_rng(14)
    mu = rng.uniform(1.0, 3.0, size=40_000)
    k = 5.0
    overdispersed = rng.poisson(mu * rng.gamma(k, 1.0 / k, size=mu.size))
    estimate = fit_game_state_dispersion(overdispersed, mu)
    assert estimate is not None and estimate == pytest.approx(k, rel=0.15)
    assert fit_game_state_dispersion(np.array([1, 2, 1, 2]), np.array([1.5, 1.5, 1.5, 1.5])) is None


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
        QuantEngine(),
    )
    summary = summarize(predictions)
    assert summary["n"] == 8.0
    assert summary["brier_skill"] == pytest.approx(1.0 - summary["brier"] / summary["brier_climatology"])
    with pytest.raises(ValueError):
        summarize([])
