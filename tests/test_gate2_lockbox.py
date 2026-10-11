"""scripts/gate2_lockbox.py: the repriced lockbox compared with the predictions logged before kickoff (audit 2026-10-11,
second pass: Gate 2 recomputed prices and never checked them against what was actually predicted)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from prop_alpha.modeling.model_freeze import FROZEN_DIGEST

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prop_alpha_gate2_lockbox_script", ROOT / "scripts" / "gate2_lockbox.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _logged(player: int, p_win: float, digest: str = FROZEN_DIGEST, logged: str = "2026-10-10T13:44:00+00:00") -> dict[str, Any]:
    return {"logged_at": logged, "window_start": logged, "kickoff_at": "2026-10-10T14:00:00+00:00", "fixture_id": 1,
            "player_id": player, "market": "tackles", "line": 1.5, "p_win": p_win, "p_lose": 0.9 - p_win, "p_void": 0.1,
            "lineup_basis": "estimated", "digest": digest}


def test_repriced_probabilities_are_compared_with_the_logged_ones_and_differences_are_named() -> None:
    lockbox = _module()
    repriced = {(1, 7, "tackles", 1.5): 0.5 / 0.9, (1, 8, "tackles", 1.5): 0.31, (2, 9, "tackles", 1.5): 0.4}
    logged = [_logged(7, 0.5), _logged(8, 0.3),
              _logged(8, 0.6, logged="2026-10-10T13:50:00+00:00"),  # a later duplicate: the earliest stands
              _logged(7, 0.1, digest="not-frozen")]  # made by other code: never compared
    check = lockbox.compare_with_log(repriced, logged)
    assert check["compared"] == 2 and check["differ"] == 1 and check["differing"] == [[1, 8, "tackles", 1.5]]
    assert check["max_difference"] == pytest.approx(abs(0.31 - 0.3 / 0.9))
    assert check["matches_logged"] == 1 and check["matches_not_logged"] == 1  # fixture 2 was never logged
    assert lockbox.compare_with_log(repriced, [])["compared"] == 0
