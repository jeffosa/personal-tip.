"""The frozen pricing model (modeling/model_freeze.py) and the Gate 2 scoring maths (evaluation/gate2.py)."""

from __future__ import annotations

import math
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from prop_alpha.evaluation import gate2
from prop_alpha.modeling import model_freeze


def test_pricing_code_is_the_frozen_model() -> None:
    """Fails on any change to how a price is computed. Revert it, or re-freeze (model_freeze.py docstring): new
    FROZEN_DIGEST, FROZEN_ON and a later LOCKBOX_START, which discards earlier lockbox evidence."""
    assert model_freeze.pricing_digest() == model_freeze.FROZEN_DIGEST


def test_the_lockbox_starts_after_the_freeze() -> None:
    assert model_freeze.LOCKBOX_START.date() > model_freeze.FROZEN_ON
    assert model_freeze.LOCKBOX_START.tzinfo is timezone.utc
    assert set(model_freeze.GATE2_LINES) == {"tackles", "fouls"}


def test_digest_ignores_comments_but_not_logic(tmp_path: Path) -> None:
    for module in model_freeze.PRICING_CODE:
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(model_freeze.REPOSITORY_ROOT / module, tmp_path / module)
    frozen = model_freeze.pricing_digest(tmp_path)
    slate = tmp_path / "prop_alpha/scanner/slate.py"
    source = slate.read_text(encoding="utf-8")
    slate.write_text(source.replace("HISTORY_MATCHES: Final[int] = 20", "HISTORY_MATCHES: Final[int] = 20  # note"),
                     encoding="utf-8")
    assert model_freeze.pricing_digest(tmp_path) == frozen
    slate.write_text(source.replace("HISTORY_MATCHES: Final[int] = 20", "HISTORY_MATCHES: Final[int] = 19"),
                     encoding="utf-8")
    assert model_freeze.pricing_digest(tmp_path) != frozen
    slate.write_text(source.replace("def build_projections(", "def build_projections_renamed("), encoding="utf-8")
    with pytest.raises(ValueError, match="no longer defines build_projections"):
        model_freeze.pricing_digest(tmp_path)


def _calibrated_sample(n_fixtures: int, per_fixture: int, slope: float, seed: int) -> tuple[list[int], np.ndarray,
                                                                                            np.ndarray]:
    rng = np.random.default_rng(seed)
    truth = rng.uniform(0.1, 0.9, n_fixtures * per_fixture)
    logit = np.log(truth / (1 - truth))
    predicted = 1 / (1 + np.exp(-logit / slope))  # slope < 1 in the fit means these are too extreme
    outcomes = (rng.uniform(size=truth.size) < truth).astype(int)
    fixtures = [i // per_fixture for i in range(truth.size)]
    return fixtures, predicted, outcomes


def test_a_calibrated_skilful_model_passes_and_an_overconfident_one_fails() -> None:
    fixtures, p, y = _calibrated_sample(1000, 20, 1.0, 1)
    good = gate2.score_line(fixtures, p, y, np.random.default_rng(2), resamples=500)
    assert good.n == 20000 and good.n_fixtures == 1000 and good.worst_bin_gap <= gate2.MAX_BIN_GAP
    assert 0.9 <= good.calibration_slope <= 1.1 and good.skill_low > 0 and good.passed
    fixtures, p, y = _calibrated_sample(200, 10, 0.7, 3)
    bad = gate2.score_line(fixtures, p, y, np.random.default_rng(4), resamples=500)
    assert bad.calibration_slope < 0.9 and not bad.criteria["calibration slope in [0.9, 1.1]"] and not bad.passed


def test_too_few_predictions_fail_the_sample_size_criterion() -> None:
    fixtures, p, y = _calibrated_sample(40, 10, 1.0, 5)
    score = gate2.score_line(fixtures, p, y, np.random.default_rng(6), resamples=500)
    assert score.n == 400 and not score.criteria["sample size"] and not score.passed


def test_the_clustered_interval_brackets_the_point_skill() -> None:
    fixtures, p, y = _calibrated_sample(150, 12, 1.0, 7)
    low, high = gate2.clustered_skill_interval(fixtures, p, y, np.random.default_rng(8), resamples=1000)
    point = 1 - np.mean((p - y) ** 2) / (y.mean() * (1 - y.mean()))
    assert low < point < high


@pytest.mark.parametrize(("p", "y", "message"), [
    ([0.0, 0.5], [0, 1], "strictly between"),
    ([0.5, 1.0], [0, 1], "strictly between"),
    ([0.5, math.nan], [0, 1], "strictly between"),
    ([0.5, 0.5], [0, 2], "0 or 1"),
    ([0.5], [0, 1], "equal-length"),
])
def test_invalid_inputs_are_refused(p: list[float], y: list[int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        gate2.calibration_fit(p, y)


def test_an_unfittable_calibration_fails_the_line_instead_of_stopping_the_run() -> None:
    """Audit 2026-10-04: perfectly separated outcomes have no finite calibration fit."""
    p = [0.2] * 50 + [0.8] * 50
    y = [0] * 50 + [1] * 50
    fit = gate2.calibration_fit(p, y)
    assert not fit.converged and math.isnan(fit.slope) and fit.note
    score = gate2.score_line(list(range(100)), p, y, np.random.default_rng(0), resamples=200)
    assert not score.criteria["calibration slope in [0.9, 1.1]"] and not score.passed and score.calibration_note


def test_identical_outcomes_are_refused_not_scored() -> None:
    with pytest.raises(ValueError, match="every outcome is the same"):
        gate2.score_line([1, 2, 3], [0.2, 0.4, 0.6], [1, 1, 1], np.random.default_rng(0), resamples=100)


def test_lockbox_constants_are_timezone_aware() -> None:
    assert model_freeze.LOCKBOX_START == datetime(2026, 10, 5, tzinfo=timezone.utc)
