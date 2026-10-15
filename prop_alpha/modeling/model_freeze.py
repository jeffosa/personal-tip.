"""The frozen pricing model and the PRD Gate 2 lockbox (maintainer decision 2026-10-04).

The 2026-09-12 to 2026-10-04 window chose the scan's settings (scripts/tune_backtest.py, scripts/recalibrate.py),
so it is selection data, not a holdout. Gate 2 evidence comes only from matches kicking off on or after
LOCKBOX_START, priced by the model as it was frozen on FROZEN_ON and scored once by scripts/gate2_lockbox.py.

The freeze is enforced: tests/test_model_freeze.py fails when the pricing code (PRICING_CODE, fingerprinted by
pricing_digest, which ignores comments, docstrings and formatting) differs from FROZEN_DIGEST. A pricing change
must either be reverted or re-frozen: set FROZEN_DIGEST, FROZEN_ON to the day of the change and LOCKBOX_START to a
later day. Lockbox results before the new start then no longer count. Never re-freeze in response to lockbox
results: that turns the lockbox into selection data too.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Final, Mapping

FROZEN_ON: Final[date] = date(2026, 10, 5)
# Restarted (maintainer, 2026-10-15) with the move from Sportmonks to TheStatsAPI: the pricing code is unchanged (same
# FROZEN_DIGEST), but its input data now come from another vendor whose counts differ (tackles for about 1 player in 8),
# so matches priced on Sportmonks data (from 2026-10-06) no longer count. Only matches from this date, priced and scored
# on TheStatsAPI data (prop_alpha_tsa), count towards Gate 2.
LOCKBOX_START: Final[datetime] = datetime(2026, 10, 17, tzinfo=timezone.utc)
# Lines scored per market: the lines the selection backtests reported, fixed before any lockbox match.
GATE2_LINES: Final[Mapping[str, tuple[float, ...]]] = {"tackles": (0.5, 1.5, 2.5), "fouls": (0.5, 1.5)}
# Every top-level definition that decides a price ("*" = the whole module). Odds matching, alerts, mock prices and
# the fair-price board are left out: they do not change the model's probabilities.
PRICING_CODE: Final[Mapping[str, tuple[str, ...]]] = {
    "prop_alpha/modeling/engine.py": ("*",),
    "prop_alpha/modeling/league_level.py": ("*",),
    "prop_alpha/scanner/slate.py": (
        "SCAN_MARKETS", "RECENT_TEAM_MATCHES", "HISTORY_MATCHES", "MIN_P_START", "SCAN_GAME_STATE_K",
        "SCAN_GAME_STATE_K_BY_MARKET", "SCAN_RATE_MULTIPLIER_BY_MARKET", "DEFAULT_PRIOR_STRENGTH_90S",
        "SCAN_PRIOR_STRENGTH_BY_MARKET", "UpcomingFixture", "SquadPlayer",
        "Appearance", "lineup_estimate", "build_projections",
    ),
    "prop_alpha/alerts/scan.py": (
        "TEAM_RECENT_SQL", "SQUAD_SQL", "HISTORY_SQL", "LEVEL_SQL", "LEVEL_SEASONS", "SlateInputs",
        "load_slate_inputs", "scan_engines", "price_fixture",
    ),
}
FROZEN_DIGEST: Final[str] = "839e97ce504eaecd29a7b435fea9980e0a3d3401e648d4cee70a39b1ca63275f"
REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


def _name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]


def canonical_dump(node: object) -> str:
    """ast.dump(node) as Python 3.13+ writes it (no attributes, None optional fields and empty lists left out), on any
    interpreter. Python 3.12's ast.dump keeps those fields, so hashing ast.dump gave the same code two digests (audit
    2026-10-06: 9d071ed9 on 3.12 v the frozen 839e97ce on 3.13+); this keeps the frozen value on every version."""
    if isinstance(node, ast.AST):
        cls = type(node)
        fields: list[str] = []
        for name in node._fields:
            if not hasattr(node, name):
                continue
            value = getattr(node, name)
            if (value is None and getattr(cls, name, ...) is None) or (isinstance(value, list) and not value):
                continue
            fields.append(f"{name}={canonical_dump(value)}")
        return f"{cls.__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return f"[{', '.join(canonical_dump(item) for item in node)}]"
    return repr(node)


def pricing_digest(root: Path = REPOSITORY_ROOT) -> str:
    """SHA-256 over the syntax trees of PRICING_CODE (no comments, docstrings or layout). Raises if a named
    definition is missing, so a rename cannot drop code from the fingerprint."""
    digest = hashlib.sha256()
    for module, names in sorted(PRICING_CODE.items()):
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        _strip_docstrings(tree)
        if names == ("*",):
            selected = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        else:
            by_name = {_name(node): node for node in tree.body}
            missing = [name for name in names if name not in by_name]
            if missing:
                raise ValueError(f"{module} no longer defines {', '.join(missing)}: update PRICING_CODE and re-freeze")
            selected = [by_name[name] for name in names]
        digest.update(module.encode("utf-8"))
        for node in selected:
            digest.update(canonical_dump(node).encode("utf-8"))
    return digest.hexdigest()
