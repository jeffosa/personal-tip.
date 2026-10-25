"""scripts/screen_replay.py (2026-10-25): replaying archived real prices through the pilot's tip pipeline. The pure grading
and haircut; the replay itself reads the pilot database and the odds archive."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from prop_alpha.scanner.market_scanner import SideProbabilities, stressed_probabilities

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("screen_replay", ROOT / "scripts" / "screen_replay.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_settle_follows_the_assumed_market_rules() -> None:
    replay = _module()
    assert replay.settle("over", 1.5, 3.0, True, 2) == pytest.approx(2.0)
    assert replay.settle("over", 1.5, 3.0, True, 1) == -1.0
    assert replay.settle("under", 0.5, 4.0, True, 0) == pytest.approx(3.0)
    assert replay.settle("over", 2.0, 2.0, True, 2) == 0.0          # a whole line pushes
    assert replay.settle("over", 0.5, 1.5, False, 0) is None         # did not play: void
    assert replay.settle("over", 0.5, 1.5, True, None) is None       # no stored count


def test_the_production_loss_floor_leaves_an_ordinary_bet_untouched_but_a_haircut_does_not() -> None:
    # Why the replay tests haircuts: MODEL_RISK_LOSS_FLOOR only lifts the loss probability to the floor, so a bet that
    # already loses 40% of the time is not stressed at all, while a 0.05 haircut costs it 0.05 * odds of expected profit.
    replay = _module()
    p = SideProbabilities(0.55, 0.40, 0.05)
    assert stressed_probabilities(p, 0.03) == p
    assert replay.haircut_ev(0.55, 0.40, 2.0, 0.0) == pytest.approx(0.55 - 0.40)
    assert replay.haircut_ev(0.55, 0.40, 2.0, 0.05) == pytest.approx(0.50 - 0.45)
    with pytest.raises(ValueError, match="haircut"):
        replay.haircut_ev(0.55, 0.40, 2.0, 1.0)


def test_summary_reports_each_haircut_with_unit_and_stake_weighted_returns() -> None:
    replay = _module()
    pick = replay.Pick
    rows = [pick(0.0, "s", "A v B", 1, "tackles", 0.5, "over", "bet365", 2.0, 10.0, 0.6, 0.1, 1.0),
            pick(0.0, "s", "A v B", 2, "tackles", 0.5, "over", "bet365", 2.0, 30.0, 0.6, 0.1, -1.0),
            pick(0.0, "s", "A v B", 3, "tackles", 0.5, "over", "bet365", 2.0, 5.0, 0.6, 0.1, None)]
    text = replay.summarise(rows)
    assert text.startswith("haircut 0.00: 3 picks, 2 settled, won 1 (50%)")
    assert "return per unit +0.0%, stake-weighted -50.0%" in text
