from __future__ import annotations

import math
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