"""No backtest, tuning or export path can read lockbox outcomes (operational audit, 2026-10-26).

Every script that reads match outcomes from the working database (League One/Two, the lockbox leagues) must stop before
the lockbox start (it references lockbox.tuning_cutoff, LOCKBOX_CUTOFF, LOCKBOX_START or PROTOCOL.start), or be one of
the few readers listed below, each for a reason that is not tuning or evaluation. A new script that reads outcomes without
a cutoff fails here until it is given one (scripts/make_static_dataset.py did, before 2026-10-26: it sampled evaluation
fixtures from 2026-01-01 with no upper bound and exported every final match's outcomes).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from prop_alpha.evaluation.lockbox import PROTOCOL

ROOT = Path(__file__).resolve().parents[1]
WORKING_DB = re.compile(r"\b(DEFAULT_DSN|DSN_ENV)\b")
OUTCOMES = re.compile(r"player_match_stats|tackles_attempted|fouls_committed")
LOADS_ROWS = re.compile(r"\b(qb|quick_backtest)\.load_rows\(")
CUTOFF = re.compile(r"\btuning_cutoff\b|\bLOCKBOX_CUTOFF\b|\bLOCKBOX_START\b|PROTOCOL\.start")
# Readers of working-database outcomes that neither tune nor evaluate the model, with the reason.
ALLOWED: dict[str, str] = {
    "gate2_prospective.py": "the lockbox scorer itself: counts until complete, then scores once (LOCKBOX_PROTOCOL.md)",
    "gate2_lockbox.py": "retired: main() refuses to run (its old body is kept for the record)",
    "gate1_checks.py": "Gate 1 integrity counts (payload players v stored rows, refusals); no outcome is analysed",
    "contract_evidence.py": "the stored counts of bets a bookmaker settled, for settlement reconciliation only",
}


def _reads_working_outcomes(source: str) -> bool:
    return bool((WORKING_DB.search(source) and OUTCOMES.search(source)) or LOADS_ROWS.search(source))


def test_every_script_reading_working_database_outcomes_stops_at_the_lockbox_start() -> None:
    readers = {path.name: path.read_text(encoding="utf-8") for path in sorted((ROOT / "scripts").glob("*.py"))
               if _reads_working_outcomes(path.read_text(encoding="utf-8"))}
    unguarded = sorted(name for name, source in readers.items() if name not in ALLOWED and not CUTOFF.search(source))
    assert unguarded == [], f"read lockbox outcomes with no cutoff: {unguarded}"
    # The allow-list holds only scripts that still read outcomes (a stale entry would hide nothing, but mislead).
    assert set(ALLOWED) <= set(readers), sorted(set(ALLOWED) - set(readers))
    # The known tuning and backtest paths are among the readers, so the check is not vacuous.
    assert {"quick_backtest.py", "tune_backtest.py", "recalibrate.py", "tune_fouls.py", "make_static_dataset.py"} <= set(readers)


def test_the_detector_catches_an_unguarded_outcome_reader() -> None:
    leaky = 'conn = psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN))\ncur.execute("SELECT tackles_attempted FROM x")\n'
    assert _reads_working_outcomes(leaky) and not CUTOFF.search(leaky)
    assert _reads_working_outcomes("rows = qb.load_rows()\n")
    # The pilot database is not the lockbox's: PILOT_DSN_ENV and DEFAULT_PILOT_DSN are not the working database.
    assert not _reads_working_outcomes("os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN)\nSELECT tackles_attempted\n")


def _script(name: str) -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(f"prop_alpha_{name}_isolation", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_static_dataset_stops_before_the_lockbox() -> None:
    dataset = _script("make_static_dataset")
    assert dataset.LOCKBOX_CUTOFF == PROTOCOL.start
    for name, sql in dataset.TABLES:
        if name in ("fixtures", "player_match_stats"):
            assert "kickoff_at < %(cutoff)s" in sql, name
