"""PRD Gate 2 on the prospective lockbox (LOCKBOX_PROTOCOL.md, evaluation/lockbox.py PROTOCOL; maintainer 2026-10-25).

Scores the predictions the scan logged before kickoff (alerts/predictions.py), never a re-price, against each player's
outcome captured once into `.prop_alpha_state/lockbox_outcomes.jsonl` (append-only; captured 72 hours after kickoff from
Sportmonks' counts in `prop_alpha`; a later vendor revision is reported, never applied).

Until every Gate 2 line has PROTOCOL.min_scored_per_line scored predictions from PROTOCOL.min_scored_fixtures_per_line
distinct matches (amendment of 2026-10-26: 500 from 320; only kickoffs before PROTOCOL.end count) it reports counts only:
eligible, scored, pending, missing, void and excluded by reason, so the lockbox cannot be looked at, or tuned on, before it
is complete. Then it computes every Gate 2 metric once (evaluation/gate2.py: Brier skill with its fixture-clustered 90%
interval, calibration slope and intercept, the calibration bins, their count-weighted mean gap (the criterion) and worst
bin of >= 50, and the closing-market benchmark where at least MIN_BENCHMARK_PREDICTIONS two-way closing prices exist) and
writes `.prop_alpha_state/gate2_prospective_result.json`; a later run prints that result and refuses to score again. A
missing benchmark or sample is a fail, never a pass. A lockbox that closes (PROTOCOL.end plus PROTOCOL.missing_after)
short of the threshold gets a one-time NOT PASSED result in the same file, with no metric computed: it is never scored.

Refuses to run on pricing code other than the protocol's. Read-only on the database; writes only the outcome ledger
(not with --dry-run) and the one result file.

    .venv\\Scripts\\python.exe scripts\\gate2_prospective.py [--dry-run | --capture-only]

`--capture-only` (the 4-hourly task's `lockbox-capture` step since 2026-10-26, so each outcome is captured within about 4
hours of the 72-hour grace rather than whenever someone runs this) captures due outcomes and prints the counts; it never
scores, whatever the counts: scoring stays a deliberate run without the flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.closing import load_closing_quotes  # noqa: E402
from prop_alpha.alerts.ledger import ledger_lock  # noqa: E402
from prop_alpha.alerts.predictions import PredictionLog  # noqa: E402
from prop_alpha.config import DEFAULT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.evaluation import lockbox  # noqa: E402
from prop_alpha.evaluation.backtest import calibration_table  # noqa: E402
from prop_alpha.evaluation.gate2 import MIN_BENCHMARK_PREDICTIONS, market_benchmark, score_line  # noqa: E402
from prop_alpha.modeling.model_freeze import FROZEN_DIGEST, LOCKBOX_START, pricing_digest  # noqa: E402
from prop_alpha.evaluation.value_evidence import closing_over  # noqa: E402

STATE: Final[Path] = ROOT / ".prop_alpha_state"
OUTCOMES_FILE: Final[Path] = STATE / "lockbox_outcomes.jsonl"
RESULT_FILE: Final[Path] = STATE / "gate2_prospective_result.json"
RESULT_LOCK_FILE: Final[Path] = STATE / "gate2_prospective_result.lock"
# PRD target markets the frozen model does not price: Gate 2 cannot pass while one is missing.
UNPRICED_TARGET_MARKETS: Final[tuple[str, ...]] = ("yellow_card",)
MATCHES_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, f.kickoff_at, f.status = 'final'
    FROM prop_alpha.fixtures f WHERE f.vendor_fixture_uid = ANY(%s)
"""
PLAYERS_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, s.player_id, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f USING (fixture_id)
    WHERE f.vendor_fixture_uid = ANY(%s) AND f.status = 'final'
"""


def load_outcomes(path: Path) -> dict[tuple[int, int], dict[str, Any]]:
    """The captured outcomes, (match, player) -> record; the first capture of a key is the scored one."""
    captured: dict[tuple[int, int], dict[str, Any]] = {}
    if not path.is_file():
        return captured
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record: Any = json.loads(line)
        if not isinstance(record, dict) or "fixture_id" not in record or "player_id" not in record or "minutes" not in record:
            raise ValueError(f"{path.name} line {number} is not an outcome record")
        captured.setdefault((int(record["fixture_id"]), int(record["player_id"])), record)
    return captured


def score(scoring: lockbox.ScoringRows, closing: Mapping[tuple[int, int, str, float], float]) -> dict[str, Any]:
    """Every Gate 2 metric per market and line, and the verdict with each reason it is not a pass."""
    rng = np.random.default_rng(lockbox.PROTOCOL.seed)
    result: dict[str, Any] = {"markets": {}, "blockers": []}
    for market, lines in lockbox.PROTOCOL.lines.items():
        entry: dict[str, Any] = {}
        bench: list[tuple[float, float, int]] = []
        for line in lines:
            rows = scoring.rows[(market, line)]
            fixtures, _, p, y = zip(*rows)
            line_score = score_line(fixtures, p, y, rng)
            entry[str(line)] = {**asdict(line_score), "criteria": line_score.criteria, "passed": line_score.passed,
                                "bins": [asdict(b) for b in calibration_table(p, y)]}
            if not line_score.passed:
                failed = [name for name, ok in line_score.criteria.items() if not ok]
                result["blockers"].append(f"{market} over {line}: " + "; ".join(failed))
            bench += [(closing[(f, player, market, line)], p_model, outcome)
                      for f, player, p_model, outcome in rows if (f, player, market, line) in closing]
        if len(bench) >= MIN_BENCHMARK_PREDICTIONS:
            q_market, p_model, outcomes = zip(*bench)
            fit = market_benchmark(q_market, p_model, outcomes)
            entry["market_benchmark"] = {**asdict(fit), "judged": True, "passed": fit.passed}
            if not fit.passed:
                result["blockers"].append(f"{market} market benchmark: b2 {fit.b_model:+.3f}, p {fit.p_value:.3f}")
        else:
            entry["market_benchmark"] = {"n": len(bench), "judged": False, "passed": False}
            result["blockers"].append(f"{market} market benchmark: {len(bench)} predictions with a two-way closing price, "
                                      f"{MIN_BENCHMARK_PREDICTIONS} needed: not judged, so not passed")
        result["markets"][market] = entry
    result["blockers"] += [f"{m}: a PRD target market the frozen model does not price" for m in UNPRICED_TARGET_MARKETS]
    result["passed"] = not result["blockers"]
    return result


def expired_result(scoring: lockbox.ScoringRows, now: datetime) -> dict[str, Any]:
    """The result of a lockbox that closed short of the threshold: NOT PASSED, no metric computed."""
    protocol = lockbox.PROTOCOL
    counts = {f"{market} over {line}": {"scored": len(rows), "matches": len({r[0] for r in rows})}
              for (market, line), rows in sorted(scoring.rows.items())}
    return {"scored_at": now.isoformat(), "expired": True, "passed": False, "counts": counts,
            "blockers": [f"the lockbox closed short of {protocol.min_scored_per_line} scored predictions from "
                         f"{protocol.min_scored_fixtures_per_line} matches on every line (kickoffs before "
                         f"{protocol.end:%Y-%m-%d}): NOT PASSED, never scored"]}


def stored_summary(stored: Mapping[str, Any]) -> list[str]:
    """How a stored result is shown on every later run."""
    head = (f"Lockbox closed unscored on {stored['scored_at']}: NOT PASSED; it is never scored." if stored.get("expired")
            else f"Scored once on {stored['scored_at']}: {'PASS' if stored['passed'] else 'NOT PASSED'}; not scored again.")
    return [head, *(f"  - {blocker}" for blocker in stored["blockers"])]


def write_result_once(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Atomically enforce the protocol's one-look rule across concurrent scorer processes."""
    with ledger_lock(RESULT_LOCK_FILE):
        if RESULT_FILE.is_file():
            stored: Any = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError(f"{RESULT_FILE.name} is not a result object")
            return stored
        RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULT_FILE.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        return None


def main(argv: Sequence[str] | None = None) -> int:
    import psycopg2

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="capture no outcome; report only")
    mode.add_argument("--capture-only", action="store_true",
                      help="capture due outcomes and report counts; never score (the scheduled step)")
    args = parser.parse_args(argv)
    protocol = lockbox.PROTOCOL
    digest = pricing_digest()
    if not digest == FROZEN_DIGEST == protocol.pricing_digest or LOCKBOX_START != protocol.start:
        print(f"REFUSED: the pricing code ({digest[:12]}), the freeze ({FROZEN_DIGEST[:12]}, start {LOCKBOX_START:%Y-%m-%d}) "
              f"and the protocol ({protocol.pricing_digest[:12]}, start {protocol.start:%Y-%m-%d}) disagree: a pricing "
              "change needs a new protocol with a later start (LOCKBOX_PROTOCOL.md).")
        return 2
    now = datetime.now(timezone.utc)
    records = PredictionLog(STATE / "predictions.jsonl").load()
    candidates = sorted({int(r["fixture_id"]) for r in records if lockbox.record_reason(r) is None})
    matches: dict[int, lockbox.StoredMatch] = {}
    conn = psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN),
                            options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            uids = [str(u) for u in candidates]
            cur.execute(MATCHES_SQL, (uids,))
            found = {int(uid): (kickoff.astimezone(timezone.utc), bool(final)) for uid, kickoff, final in cur.fetchall()}
            cur.execute(PLAYERS_SQL, (uids,))
            players: dict[int, dict[int, tuple[int, Mapping[str, int]]]] = {}
            for uid, player_id, minutes, tackles, fouls in cur.fetchall():
                players.setdefault(int(uid), {})[int(player_id)] = (int(minutes or 0), {"tackles": tackles, "fouls": fouls})
    finally:
        conn.close()
    matches = {uid: lockbox.StoredMatch(kickoff, final, players.get(uid, {})) for uid, (kickoff, final) in found.items()}
    selection = lockbox.eligible(records, {uid: m.kickoff_at for uid, m in matches.items()})
    captured = load_outcomes(OUTCOMES_FILE)
    new = lockbox.outcomes_to_capture(selection.eligible, captured, matches, now)
    if new and not args.dry_run:
        OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OUTCOMES_FILE.open("a", encoding="utf-8") as handle:
            for outcome in new:
                handle.write(json.dumps(outcome) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        captured = load_outcomes(OUTCOMES_FILE)
    elif new:
        captured = {**captured, **{(int(o["fixture_id"]), int(o["player_id"])): o for o in new}}
    scoring = lockbox.scoring_rows(selection.eligible, captured, now)
    changed = lockbox.revised(captured, matches)
    print(f"Gate 2 prospective lockbox: kickoff from {protocol.start:%Y-%m-%d %H:%M} UTC, digest {protocol.pricing_digest[:12]}, "
          f"data {protocol.data_vendor}; {len(records)} logged lines, {len(selection.eligible)} eligible, "
          f"{len({int(r['fixture_id']) for r in selection.eligible})} matches")
    for reason, count in sorted(selection.excluded.items()):
        print(f"  not eligible: {count} - {reason}")
    for state, count in sorted(scoring.states.items()):
        print(f"  eligible, {state}: {count}")
    print(f"  outcomes captured: {len(captured)} ({len(new)} new{' - dry run, not written' if args.dry_run else ''}); "
          f"revised at the source since capture (not applied): {len(changed)}")
    for (market, line), rows in sorted(scoring.rows.items()):
        print(f"  {market} over {line}: {len(rows)} scored of {protocol.min_scored_per_line} needed, "
              f"{len({r[0] for r in rows})} matches of {protocol.min_scored_fixtures_per_line} needed")
    print(f"  lockbox end: kickoffs before {protocol.end:%Y-%m-%d}; it closes unscored on "
          f"{protocol.end + protocol.missing_after:%Y-%m-%d} if the threshold is not reached")
    if args.capture_only:
        print("Capture only: outcomes captured, nothing scored (scoring is a separate, deliberate run).")
        return 0
    if RESULT_FILE.is_file():
        stored = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        print("\n".join(stored_summary(stored)))
        return 0
    if lockbox.expired(scoring, now):
        if args.dry_run:
            print("Closed short of the threshold: NOT PASSED; a run without --dry-run records that once.")
            return 1
        stored = write_result_once(expired_result(scoring, now))
        print("\n".join(stored_summary(stored if stored is not None else expired_result(scoring, now))))
        return 1
    if not lockbox.ready_to_score(scoring):
        print("NOT SCORED: the metrics are computed once, when every line has the required scored predictions from the "
              "required matches; until then only counts are shown. Gate 2 is not passed.")
        print(f"  Also open whatever the lockbox shows: {', '.join(UNPRICED_TARGET_MARKETS)} is not priced; the market "
              "benchmark needs closing prices (none captured so far); predictions use estimated line-ups, not team sheets.")
        return 1
    if args.dry_run:
        print("Ready to score; not scored in a dry run (scoring happens once and is written).")
        return 1
    result = score(scoring, closing_over(load_closing_quotes(STATE)))
    result.update({"scored_at": now.isoformat(), "protocol_start": protocol.start.isoformat(),
                   "pricing_digest": protocol.pricing_digest, "data_vendor": protocol.data_vendor, "seed": protocol.seed,
                   "eligible": len(selection.eligible), "excluded": selection.excluded, "states": scoring.states,
                   "revised_since_capture": len(changed)})
    stored = write_result_once(result)
    if stored is not None:
        print("\n".join(stored_summary(stored)))
        return 0 if stored["passed"] else 1
    print(f"Scored once: {'PASS' if result['passed'] else 'NOT PASSED'} (written to {RESULT_FILE.name})")
    for blocker in result["blockers"]:
        print(f"  - {blocker}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
