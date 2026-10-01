from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pytest
from scipy.stats import gamma, poisson

from prop_alpha.alerts.telegram import demo_case, reference_projection
from prop_alpha.modeling.engine import LineupExpectation, PropProjection, VoidRule
from prop_alpha.scanner.market_scanner import StakingDirective, price_for_ev
from prop_alpha.scanner.value_screen import (
    HighValueAssessment,
    HighValueCriteria,
    assess_high_value,
    simulate_edge_survival,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _directive(projection: PropProjection, line: float, side: str, ev: float, void_rule: str = "void_if_no_appearance",
               fair: float | None = None) -> StakingDirective:
    probabilities = projection.side_probabilities(line, side, void_rule)
    price = price_for_ev(probabilities, ev)
    return StakingDirective(
        fixture_id=projection.fixture_id, player_id=projection.player_id, market_type=projection.market_type,
        line_value=line, side=side, sportsbook="Book", void_rule=void_rule, decimal_odds=price,
        p_win=probabilities.p_win, p_lose=probabilities.p_lose, p_void=probabilities.p_void,
        fair_market_prob=fair, ev_per_unit=ev, return_variance_per_unit=1.0, kelly_full=0.1,
        fraction=0.01, stake=100.0,
    )


def test_simulated_conditions_average_to_the_exact_price() -> None:
    projection = reference_projection()
    rates = gamma(projection.gamma_shape, scale=1.0 / projection.gamma_rate).rvs(
        size=400_000, random_state=np.random.default_rng(21)
    )
    minutes = projection.lineup.exp_minutes_start / 90.0
    simulated_over = float(np.mean(poisson.sf(1, rates * minutes)))
    exact_over = float(projection.count_distribution(projection.lineup.exp_minutes_start).sf(1))
    assert simulated_over == pytest.approx(exact_over, abs=0.002)


def test_survival_count_is_reproducible_for_a_seed() -> None:
    projection = reference_projection()
    args = (projection, "under", 4.5, 1.227, "void_if_no_appearance", 0.04, 1000)
    first = simulate_edge_survival(*args, rng=np.random.default_rng(5))
    second = simulate_edge_survival(*args, rng=np.random.default_rng(5))
    assert first == second


def test_reference_overs_are_mispriced_but_not_robust() -> None:
    projection = reference_projection()
    for line, ev in ((1.5, 0.060), (2.5, 0.062)):
        assessment = assess_high_value(
            _directive(projection, line, "over", ev), projection, HighValueCriteria(), np.random.default_rng(1)
        )
        assert assessment.mispriced and assessment.meets_min_ev
        assert assessment.surviving_simulations <= 600
        assert not assessment.is_high_value


def test_robust_under_with_enough_edge_is_high_value() -> None:
    projection = reference_projection()
    assessment = assess_high_value(
        _directive(projection, 4.5, "under", 0.08), projection, HighValueCriteria(), np.random.default_rng(1)
    )
    assert assessment.is_high_value
    assert assessment.survival_rate > 0.60 and assessment.ev_per_unit == pytest.approx(0.08)


@pytest.mark.parametrize("ev", [0.035, 0.0399])
def test_below_four_percent_is_never_high_value(ev: float) -> None:
    projection = PropProjection(1, 2, "tackles", 400.0, 400.0 / 2.0, LineupExpectation(1.0, 0.0))
    directive = _directive(projection, 4.5, "under", ev)
    assessment = assess_high_value(directive, projection, HighValueCriteria(), np.random.default_rng(1))
    assert not assessment.meets_min_ev and not assessment.is_high_value


def test_four_percent_with_a_stable_player_qualifies() -> None:
    projection = PropProjection(1, 2, "tackles", 400.0, 400.0 / 2.0, LineupExpectation(1.0, 0.0))
    assessment = assess_high_value(
        _directive(projection, 4.5, "under", 0.045), projection, HighValueCriteria(), np.random.default_rng(1)
    )
    assert assessment.meets_min_ev and 0.6 < assessment.survival_rate < 0.95 and assessment.is_high_value


def test_player_likely_to_miss_the_match_cannot_survive() -> None:
    projection = PropProjection(1, 2, "tackles", 400.0, 400.0 / 2.0, LineupExpectation(0.5, 0.0))
    assessment = assess_high_value(
        _directive(projection, 4.5, "under", 0.08), projection, HighValueCriteria(), np.random.default_rng(1)
    )
    assert assessment.meets_min_ev
    assert assessment.survival_rate < 0.6 and not assessment.is_high_value


def test_exactly_sixty_percent_is_not_enough() -> None:
    projection = PropProjection(1, 2, "tackles", 400.0, 400.0 / 2.0, LineupExpectation(0.6, 0.0))
    directive = _directive(projection, 4.5, "under", 0.08)
    criteria = HighValueCriteria(n_simulations=10)
    rates = [assess_high_value(directive, projection, criteria, np.random.default_rng(seed)).surviving_simulations
             for seed in range(200)]
    seed = rates.index(6)
    assessment = assess_high_value(directive, projection, criteria, np.random.default_rng(seed))
    assert assessment.surviving_simulations == 6 and not assessment.survives


def test_mispricing_uses_the_devigged_book_probability() -> None:
    projection = reference_projection()
    directive = _directive(projection, 4.5, "under", 0.08)
    settled = directive.p_win / (directive.p_win + directive.p_lose)
    overpriced_book = replace(directive, fair_market_prob=settled + 0.01)
    assessment = assess_high_value(overpriced_book, projection, HighValueCriteria(), np.random.default_rng(1))
    assert not assessment.mispriced and not assessment.is_high_value
    one_sided = assess_high_value(directive, projection, HighValueCriteria(), np.random.default_rng(1))
    assert one_sided.book_win_probability == pytest.approx(1.0 / directive.decimal_odds)


def test_void_rules_are_applied_in_absent_and_substitute_conditions() -> None:
    projection = PropProjection(1, 2, "tackles", 400.0, 200.0, LineupExpectation(0.0, 1.0, 85.0, 20.0))
    price = 1.5
    not_starting = simulate_edge_survival(projection, "under", 4.5, price, VoidRule.VOID_IF_NOT_STARTING, 0.04, 200,
                                          np.random.default_rng(2))
    appearing = simulate_edge_survival(projection, "under", 4.5, price, VoidRule.VOID_IF_NO_APPEARANCE, 0.04, 200,
                                       np.random.default_rng(2))
    assert not_starting == 0 and appearing == 200
    absent = PropProjection(1, 2, "tackles", 400.0, 200.0, LineupExpectation(0.0, 0.0))
    assert simulate_edge_survival(absent, "under", 0.5, price, VoidRule.NO_VOID, 0.04, 50, np.random.default_rng(2)) == 50
    assert simulate_edge_survival(absent, "over", 0.5, price, VoidRule.NO_VOID, 0.04, 50, np.random.default_rng(2)) == 0


def test_criteria_and_inputs_are_validated() -> None:
    for kwargs in ({"min_ev_per_unit": 0.0}, {"min_survival_rate": 1.0}, {"n_simulations": 0}):
        with pytest.raises(ValueError):
            HighValueCriteria(**kwargs)  # type: ignore[arg-type]
    projection = reference_projection()
    with pytest.raises(ValueError):
        assess_high_value(replace(_directive(projection, 4.5, "under", 0.08), player_id=99), projection,
                          HighValueCriteria(), np.random.default_rng(1))
    with pytest.raises(ValueError):
        simulate_edge_survival(projection, "under", 4.25, 1.5, "no_void", 0.04, 10, np.random.default_rng(1))


def test_demo_case_contains_one_high_value_bet() -> None:
    projections, report = demo_case(NOW)
    outcomes = {
        (d.side, d.line_value): assess_high_value(
            d, projections[(1001, 55, "tackles")], HighValueCriteria(), np.random.default_rng(9)
        ).is_high_value
        for d in report.directives
    }
    assert outcomes[("under", 4.5)] is True
    assert sum(outcomes.values()) == 1


@pytest.mark.parametrize(
    ("mispriced", "meets_min_ev", "surviving", "expected"),
    [(True, True, 601, True), (False, True, 900, False), (True, False, 900, False), (True, True, 600, False)],
)
def test_high_value_requires_all_three_conditions(
    mispriced: bool, meets_min_ev: bool, surviving: int, expected: bool
) -> None:
    assessment = HighValueAssessment(
        ev_per_unit=0.05 if meets_min_ev else 0.03, model_win_probability=0.6, book_win_probability=0.55,
        mispriced=mispriced, meets_min_ev=meets_min_ev, surviving_simulations=surviving, n_simulations=1000,
        survives=surviving / 1000 > 0.6,
    )
    assert assessment.is_high_value is expected
