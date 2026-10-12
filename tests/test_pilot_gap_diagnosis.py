"""scripts/pilot_gap_diagnosis.py (2026-10-12): the pure helpers that match bookmaker names to stored players and read a
ladder's shape. The report itself reads archived replies and the pilot database and is run by hand."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from scipy.stats import poisson

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pilot_gap_diagnosis", ROOT / "scripts" / "pilot_gap_diagnosis.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bookmaker_names_are_matched_to_one_stored_player_or_none() -> None:
    gap = _module()
    squad = {1: "Arthur Melo", 2: "Léo Pereira", 3: "Leo Ortiz", 4: "Lyanco Vojnovic", 5: "Bruno Henrique", 6: "Bruno Fuchs"}
    assert gap.person("Melo, Arthur") == "arthur melo"
    assert gap.match_player("Melo, Arthur", squad) == 1  # bet365 writes "Surname, First"
    assert gap.match_player("Pereira, Leo", squad) == 2  # accents do not matter
    assert gap.match_player("L. Ortiz", squad) == 3  # surname with the same initial
    assert gap.match_player("Lyanco", squad) == 4  # a mononym that appears once
    assert gap.match_player("Bruno", squad) is None  # ambiguous: never guessed
    assert gap.match_player("Someone Else", squad) is None


def test_the_ladder_shape_gives_back_the_poisson_mean_whatever_the_margin() -> None:
    gap = _module()
    for mean in (0.8, 1.9, 3.0):
        fair = {k: float(poisson.sf(k - 1, mean)) for k in (1, 2, 3)}
        for margin in (1.0, 1.08, 1.25):  # a margin that scales every step alike leaves the ratios unchanged
            assert gap.poisson_mean_from_shape({k: p * margin for k, p in fair.items()}) == pytest.approx(mean, abs=0.02)
    assert gap.poisson_mean_from_shape({1: 0.5, 2: 0.5, 3: 0.5}) is None  # no Poisson mean fits: off the grid
