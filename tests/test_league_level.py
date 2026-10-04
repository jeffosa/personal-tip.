from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prop_alpha.evaluation.backtest import PlayerMatchObservation, walk_forward_over_probability
from prop_alpha.modeling.engine import QuantEngine
from prop_alpha.modeling.league_level import (
    SEASON_PRIOR_MINUTES,
    SeasonTotals,
    level_ratios,
    level_scaled_minutes,
    season_of,
    season_rates,
    trailing_rate,
)


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 15, tzinfo=timezone.utc)


def test_season_starts_on_the_first_of_july() -> None:
    assert season_of(_utc(2026, 8, 15)) == 2026 and season_of(_utc(2026, 5, 25)) == 2025
    assert season_of(_utc(2026, 7, 1)) == 2026
    with pytest.raises(ValueError):
        season_of(datetime(2026, 8, 15))


def test_trailing_rate_moves_from_last_season_to_this_one() -> None:
    assert trailing_rate(None, 1.55) == pytest.approx(1.55)  # first day of a season: last season's level
    quarter = SeasonTotals(counts=1.40 * SEASON_PRIOR_MINUTES / 90.0, minutes=SEASON_PRIOR_MINUTES)
    assert trailing_rate(quarter, 1.60) == pytest.approx(1.50)  # equal weight at SEASON_PRIOR_MINUTES
    full = SeasonTotals(counts=1.40 * 40 * SEASON_PRIOR_MINUTES / 90.0, minutes=40 * SEASON_PRIOR_MINUTES)
    assert trailing_rate(full, 1.60) == pytest.approx(1.40, abs=0.01)  # a long season stands on its own
    assert trailing_rate(SeasonTotals(9.0, 540.0), None) == pytest.approx(1.5)
    for bad in ((None, None), (SeasonTotals(0.0, 0.0), None), (None, 0.0), (None, float("nan"))):
        with pytest.raises(ValueError):
            trailing_rate(*bad)


def test_season_rates_chain_and_skip_empty_first_seasons() -> None:
    m = SEASON_PRIOR_MINUTES
    rates = season_rates({2024: SeasonTotals(0.0, 0.0), 2025: SeasonTotals(1.552 * m / 90.0, m),
                          2026: SeasonTotals(1.485 * m / 90.0, m)})
    assert set(rates) == {2025, 2026}
    assert rates[2025] == pytest.approx(1.552) and rates[2026] == pytest.approx((1.485 + 1.552) / 2)


def test_history_is_rescaled_to_the_current_level() -> None:
    scaled, now_level = level_scaled_minutes([90.0, 90.0], [_utc(2025, 9, 1), _utc(2026, 9, 1)],
                                             {2025: 1.6, 2026: 1.5}, _utc(2026, 10, 3))
    assert now_level == 1.5 and scaled == pytest.approx([96.0, 90.0])
    with pytest.raises(ValueError):
        level_scaled_minutes([90.0], [_utc(2024, 9, 1)], {2025: 1.6, 2026: 1.5}, _utc(2026, 10, 3))
    with pytest.raises(ValueError):
        level_scaled_minutes([90.0], [_utc(2025, 9, 1)], {2025: 1.6}, _utc(2026, 10, 3))


def _history() -> list[PlayerMatchObservation]:
    start = _utc(2025, 9, 1)
    rows = [PlayerMatchObservation(1, i, start + timedelta(days=7 * i), True, 90.0, 2 + i % 2) for i in range(30)]
    return rows + [PlayerMatchObservation(1, 100 + i, _utc(2026, 8, 20) + timedelta(days=7 * i), True, 90.0, 2)
                   for i in range(4)]


def test_a_constant_level_reproduces_the_unscaled_backtest() -> None:
    engine = QuantEngine(game_state_k=6.0)
    plain = walk_forward_over_probability(_history(), 1.5, 1.5, engine)
    constant = walk_forward_over_probability(_history(), 1.5, 9.9, engine, level=lambda as_of: {2025: 1.5, 2026: 1.5})
    assert [p.p_over for p in constant] == pytest.approx([p.p_over for p in plain], abs=1e-12)


def test_a_lower_current_level_lowers_the_over_probability() -> None:
    engine = QuantEngine(game_state_k=6.0)
    same = walk_forward_over_probability(_history(), 1.5, 1.5, engine, level=lambda as_of: {2025: 1.5, 2026: 1.5})
    lower = walk_forward_over_probability(_history(), 1.5, 1.5, engine, level=lambda as_of: {2025: 1.6, 2026: 1.5})
    this_season = [i for i, p in enumerate(same) if p.kickoff_at >= _utc(2026, 7, 1)]
    assert this_season and all(lower[i].p_over < same[i].p_over for i in this_season)


def test_level_ratios_weight_minutes_without_inflating_them() -> None:
    """2026-10-04: a 125-minute match in a busier season became 137.5 scaled minutes, over MAX_MATCH_MINUTES, and
    refused the whole fixture. The ratio now goes to the engine as a weight; the minutes stay as played."""
    kickoffs = [_utc(2025, 9, 1), _utc(2026, 9, 1)]
    ratios, now_level = level_ratios(kickoffs, {2025: 1.65, 2026: 1.5}, _utc(2026, 10, 3))
    assert now_level == 1.5 and ratios == pytest.approx([1.1, 1.0])
    prior = QuantEngine.positional_prior(1.5)
    weighted = QuantEngine.gamma_poisson_update(prior, [3, 1], [125.0, 90.0], ratios)
    assert weighted.beta == pytest.approx(prior.beta + (125.0 * 1.1 + 90.0) / 90.0)
    with pytest.raises(ValueError, match="minutes must lie"):
        QuantEngine.gamma_poisson_update(prior, [3, 1], [125.0 * 1.1, 90.0])  # the old way
    for bad in ([1.1], [1.1, 0.0], [1.1, float("nan")]):
        with pytest.raises(ValueError, match="exposure_scale"):
            QuantEngine.gamma_poisson_update(prior, [3, 1], [125.0, 90.0], bad)
