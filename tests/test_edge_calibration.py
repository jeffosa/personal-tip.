"""scripts/edge_calibration.py (2026-10-25): how far the pilot model overstates its chances on the prices it favours. The
pure measurement; the run itself reads the pilot database and the odds archive."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("edge_calibration", ROOT / "scripts" / "edge_calibration.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(module: ModuleType, player: int, chance: float, odds: float, won: bool, fixture: int = 1) -> Any:
    return module.Scored(fixture, "A v B", "Brazil Serie A", player, "tackles", 1.5, "over", "bet365", odds, chance, won)


def test_the_edge_is_the_models_chance_times_the_odds_less_one() -> None:
    calibration = _module()
    assert _row(calibration, 1, 0.5, 2.4, False).edge == pytest.approx(0.2)


def test_overstatement_is_the_mean_chance_less_the_share_won_resampling_whole_players() -> None:
    calibration = _module()
    # Player 1: two ladder steps, both lost (one shared outcome); player 2: one step, won.
    rows = [_row(calibration, 1, 0.6, 2.0, False), _row(calibration, 1, 0.4, 3.0, False), _row(calibration, 2, 0.5, 2.0, True)]
    estimate, low, high = calibration.overstatement(rows, np.random.default_rng(1), draws=500)
    assert estimate == pytest.approx((0.6 + 0.4 + 0.5) / 3 - 1 / 3)
    # A draw holds whole players: all of player 1 (gap +0.5 a step) or all of player 2 (-0.5), never one step of player 1.
    assert low == pytest.approx(-0.5) and high == pytest.approx(0.5)
    with pytest.raises(ValueError, match="no prices"):
        calibration.overstatement([], np.random.default_rng(1))


def test_the_table_groups_prices_by_claimed_edge() -> None:
    calibration = _module()
    rows = [_row(calibration, 1, 0.30, 2.0, False), _row(calibration, 2, 0.55, 2.0, False, fixture=2)]
    lines = calibration.table(rows, np.random.default_rng(1))
    assert len(lines) == 2
    assert lines[0].strip().startswith("edge < +0%") and "model 30.0%, came in  0.0%" in lines[0]
    assert lines[1].strip().startswith("edge +10% to +25%") and "model too high by +0.550" in lines[1]
