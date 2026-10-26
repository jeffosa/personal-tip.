"""Build a static, self-contained test dataset of 250 evaluation fixtures (read-only on the working database).

    .venv\\Scripts\\python.exe -u scripts\\make_static_dataset.py [--fixtures 250] [--out static_data]

Writes CSV files plus manifest.json (row counts, sha256, seed, caveats) to the output folder:
  REAL (exported from the working database, Sportmonks data):
    leagues, teams, players, referees, fixtures, player_match_stats  - every final fixture (the history the
        model prices from) with its internal and vendor ids;
    eval_fixtures.csv   - the evaluation fixtures: final, kicked off 2026-01-01 or later, chosen at random with a fixed seed;
    outcomes.csv        - the realised tackles and fouls of every priced starter in them (from player_match_stats).
  SYNTHETIC (clearly labelled, never real prices):
    synthetic_odds.csv  - for each priced starter, market and line: an opening over/under quote (the model's own
        probability moved by N(0, MOCK_MISPRICING_SD) on the logit scale, with MOCK_BOOK_MARGIN, as slate.mock_quotes
        does) and a closing quote (the opening logit plus N(0, 0.1) noise). The closing price carries no information,
        so it exercises closing-line plumbing but says nothing about CLV or edge.
Model probabilities come from the same walk-forward pricing the backtests use (scripts/recalibrate.predictions: only
data strictly before kickoff, league level, per-market k, tackles multiplier). Nothing kicking off on or after the lockbox
start (lockbox.tuning_cutoff) is exported or evaluated (operational audit 2026-10-26): the fixtures and
player_match_stats tables, the evaluation fixtures and the outcomes all stop before it, so the dataset can never carry a
lockbox outcome. The folder is gitignored: the real
files are vendor data and must not be redistributed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import quick_backtest as qb  # noqa: E402
import recalibrate as rc  # noqa: E402
from prop_alpha.alerts.status import DEFAULT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.evaluation.lockbox import tuning_cutoff  # noqa: E402
from prop_alpha.scanner.slate import (  # noqa: E402
    MOCK_BOOK_MARGIN, MOCK_MISPRICING_SD, MOCK_SPORTSBOOK, SCAN_RATE_MULTIPLIER_BY_MARKET,
)

SEED: Final[int] = 20261005
EVAL_FROM: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
# The lockbox start for League One/Two (the working database): no fixture from it on is exported or evaluated.
LOCKBOX_CUTOFF: Final[datetime] = tuning_cutoff(None) or datetime.max.replace(tzinfo=timezone.utc)
CLOSING_NOISE_SD: Final[float] = 0.1
PROBABILITY_RANGE: Final[tuple[float, float]] = (0.05, 0.95)
TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("leagues", "SELECT * FROM prop_alpha.leagues ORDER BY league_id"),
    ("teams", "SELECT * FROM prop_alpha.teams ORDER BY team_id"),
    ("players", "SELECT * FROM prop_alpha.players ORDER BY player_id"),
    ("referees", "SELECT * FROM prop_alpha.referees ORDER BY referee_id"),
    ("fixtures", "SELECT * FROM prop_alpha.fixtures WHERE status = 'final' AND kickoff_at < %(cutoff)s ORDER BY fixture_id"),
    ("player_match_stats", "SELECT s.* FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f USING (fixture_id) "
                           "WHERE f.status = 'final' AND f.kickoff_at < %(cutoff)s ORDER BY s.fixture_id, s.player_id"),
)


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def export_tables(out: Path) -> tuple[dict[str, Any], dict[int, str], dict[int, str]]:
    import psycopg2

    files: dict[str, Any] = {}
    conn = psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN),
                            options="-c default_transaction_read_only=on -c statement_timeout=120000")
    try:
        with conn, conn.cursor() as cur:
            for name, sql in TABLES:
                cur.execute(sql, {"cutoff": LOCKBOX_CUTOFF})
                if cur.description is None:
                    raise ValueError(f"{name}: the query returned no result set")
                header = [d[0] for d in cur.description]
                files[f"{name}.csv"] = write_csv(out / f"{name}.csv", header, [list(r) for r in cur.fetchall()])
            cur.execute("SELECT fixture_id, vendor_fixture_uid FROM prop_alpha.fixtures")
            fixture_uid = {int(a): str(b) for a, b in cur.fetchall()}
            cur.execute("SELECT player_id, vendor_player_uid FROM prop_alpha.players")
            player_uid = {int(a): str(b) for a, b in cur.fetchall()}
    finally:
        conn.close()
    return files, fixture_uid, player_uid


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def price_pair(p_over: float) -> tuple[float, float]:
    return round(1.0 / (p_over * MOCK_BOOK_MARGIN), 3), round(1.0 / ((1.0 - p_over) * MOCK_BOOK_MARGIN), 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=int, default=250)
    parser.add_argument("--out", default="static_data")
    args = parser.parse_args()
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    print("exporting real tables ...", flush=True)
    files, fixture_uid, player_uid = export_tables(out)

    print("pricing starters (walk-forward) ...", flush=True)
    rows = qb.load_rows()
    by_market = {market: rc.predictions(rows, market) for market in qb.LINES}
    candidates = sorted({p[0].fixture_id for preds in by_market.values() for p in preds
                         if EVAL_FROM <= p[0].kickoff_at < LOCKBOX_CUTOFF})
    if len(candidates) < args.fixtures:
        raise SystemExit(f"only {len(candidates)} evaluation fixtures available, need {args.fixtures}")
    chosen = set(random.Random(SEED).sample(candidates, args.fixtures))
    rng = np.random.default_rng(SEED)

    eval_rows: dict[int, datetime] = {}
    odds: list[list[Any]] = []
    outcomes: dict[tuple[int, int], list[Any]] = {}
    for market, preds in by_market.items():
        subset = [p for p in preds if p[0].fixture_id in chosen and EVAL_FROM <= p[0].kickoff_at < LOCKBOX_CUTOFF]
        multiplier = SCAN_RATE_MULTIPLIER_BY_MARKET.get(market, 1.0)
        for line in qb.LINES[market]:
            probabilities = rc.over_probabilities(subset, line, multiplier)
            for (target, *_), p_model in zip(subset, probabilities):
                if not PROBABILITY_RANGE[0] < p_model < PROBABILITY_RANGE[1]:
                    continue
                opening = 1.0 / (1.0 + math.exp(-(logit(float(p_model)) + float(rng.normal(0.0, MOCK_MISPRICING_SD)))))
                closing = 1.0 / (1.0 + math.exp(-(logit(opening) + float(rng.normal(0.0, CLOSING_NOISE_SD)))))
                opening = min(max(opening, 0.01), 0.99)
                closing = min(max(closing, 0.01), 0.99)
                o_over, o_under = price_pair(opening)
                c_over, c_under = price_pair(closing)
                eval_rows[target.fixture_id] = target.kickoff_at
                odds.append([fixture_uid[target.fixture_id], player_uid[target.player_id], market, line,
                             o_over, o_under, (target.kickoff_at - timedelta(hours=3)).isoformat(),
                             c_over, c_under, (target.kickoff_at - timedelta(minutes=5)).isoformat(),
                             round(float(p_model), 5), MOCK_SPORTSBOOK, "SYNTHETIC"])
                outcomes[(target.fixture_id, target.player_id)] = [fixture_uid[target.fixture_id],
                                                                   player_uid[target.player_id], target.minutes_played]
    # realised counts for every priced starter, from the stored rows
    counts = {(int(r[1]), int(r[0])): (int(r[5]), int(r[6])) for r in rows}  # rows are (player_id, fixture_id, ...)
    outcome_rows = [[*value, counts[key][0], counts[key][1]] for key, value in sorted(outcomes.items())]

    files["eval_fixtures.csv"] = write_csv(
        out / "eval_fixtures.csv", ["vendor_fixture_uid", "kickoff_at"],
        [[fixture_uid[f], eval_rows[f].isoformat()] for f in sorted(eval_rows)])
    files["outcomes.csv"] = write_csv(
        out / "outcomes.csv", ["vendor_fixture_uid", "vendor_player_uid", "minutes_played", "tackles", "fouls"],
        outcome_rows)
    files["synthetic_odds.csv"] = write_csv(
        out / "synthetic_odds.csv",
        ["vendor_fixture_uid", "vendor_player_uid", "market", "line", "open_over", "open_under", "open_at",
         "close_over", "close_under", "close_at", "model_p_over", "sportsbook", "label"], odds)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "seed": SEED,
        "evaluation_fixtures": len(eval_rows), "priced_starters": len(outcome_rows), "synthetic_quotes": len(odds),
        "history_fixtures": files["fixtures.csv"]["rows"], "files": files,
        "synthetic": {"file": "synthetic_odds.csv", "mispricing_sd": MOCK_MISPRICING_SD, "margin": MOCK_BOOK_MARGIN,
                      "closing_noise_sd": CLOSING_NOISE_SD},
        "caveats": [
            "Real: stats, lineups-derived rows, referees and outcomes exported from the working database (Sportmonks); not for redistribution.",
            "Synthetic: every price in synthetic_odds.csv is generated from the model's own probability plus noise. "
            "It tests plumbing, volume and stress behaviour only; it says nothing about edge, CLV or whether any bookmaker quotes these lines.",
            "The model probabilities use the walk-forward pricing of the backtests (data strictly before kickoff).",
            "No bookmaker has quoted tackles or fouls lines for League One/Two as of 2026-10-05."],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("evaluation_fixtures", "priced_starters", "synthetic_quotes",
                                                "history_fixtures")}))
    print("written to", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
