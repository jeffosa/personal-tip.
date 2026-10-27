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

Refuses to run on pricing code other than the protocol's. Writes only the outcome ledger (not with --dry-run) and the one
result file. Since the capture amendment of 2026-10-27 (LOCKBOX_PROTOCOL.md section 6) a capture within the re-fetch window
after a match's grace first re-fetches it from the vendor through the ingestion engine (the results job's own write path);
a later capture is rebuilt, read-only, from the last stored payload fetched at or before the grace.

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
from prop_alpha.ingestion.transform import parse_fixture_payload  # noqa: E402

STATE: Final[Path] = ROOT / ".prop_alpha_state"
OUTCOMES_FILE: Final[Path] = STATE / "lockbox_outcomes.jsonl"
RESULT_FILE: Final[Path] = STATE / "gate2_prospective_result.json"
RESULT_LOCK_FILE: Final[Path] = STATE / "gate2_prospective_result.lock"
OUTCOMES_LOCK_FILE: Final[Path] = STATE / "lockbox_outcomes.lock"
# PRD target markets the frozen model does not price: Gate 2 cannot pass while one is missing. Empty since the scope
# amendment of 2026-10-27 16:43 UTC (maintainer, GATE2_SCOPE_DECISION_MEMO.md option 2a, before any lockbox outcome was
# captured): Gate 2's target markets are tackles and fouls; yellow_card was ("yellow_card",) and gets its own freeze and
# lockbox if it is ever priced.
UNPRICED_TARGET_MARKETS: Final[tuple[str, ...]] = ()
# Option 1c of the same amendment: the closing-market benchmark is judged where a market exists, on a separate,
# pre-registered market lockbox (not built yet). This lockbox still judges it when it has the closing prices; without them
# Gate 2 stays NOT PASSED, and the result also says whether this lockbox's own skill and calibration criteria passed.
MARKET_LOCKBOX_NOTE: Final[str] = ("judged on a separate market lockbox under the 2026-10-27 scope amendment "
                                   "(GATE2_SCOPE_DECISION_MEMO.md 1c; not yet pre-registered), so Gate 2 is not passed")
# With when the match's stored vendor data was first and last fetched (each distinct payload is a raw_payloads row), so a
# capture records whether it holds data fetched after the intended capture time (lockbox.capture_timing).
MATCHES_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, f.kickoff_at, f.status = 'final',
           (SELECT min(r.fetched_at) FROM prop_alpha.raw_payloads r
            WHERE r.vendor_fixture_uid = f.vendor_fixture_uid AND r.source = %s),
           (SELECT max(r.fetched_at) FROM prop_alpha.raw_payloads r
            WHERE r.vendor_fixture_uid = f.vendor_fixture_uid AND r.source = %s),
           (SELECT r.source FROM prop_alpha.raw_payloads r WHERE r.vendor_fixture_uid = f.vendor_fixture_uid
            ORDER BY r.fetched_at DESC, r.payload_id DESC LIMIT 1)
    FROM prop_alpha.fixtures f WHERE f.vendor_fixture_uid = ANY(%s)
"""
PLAYERS_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, s.player_id, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f USING (fixture_id)
    WHERE f.vendor_fixture_uid = ANY(%s) AND f.status = 'final'
"""


# A capture rebuilt after the re-fetch window uses the last payload stored for the match at or before its grace (each
# distinct payload is a raw_payloads row; the payload is the vendor adapter's output, which parse_fixture_payload reads).
REBUILD_PAYLOAD_SQL: Final[str] = """
    SELECT payload, fetched_at FROM prop_alpha.raw_payloads
    WHERE vendor_fixture_uid = %s AND source = %s AND fetched_at <= %s
    ORDER BY fetched_at DESC, payload_id DESC LIMIT 1
"""
PLAYER_IDS_SQL: Final[str] = "SELECT vendor_player_uid, player_id FROM prop_alpha.players WHERE vendor_player_uid = ANY(%s)"
# Workers for the re-fetch at the grace: a handful of matches per capture run.
REFETCH_WORKERS: Final[int] = 2


def read_matches(dsn: str, fixture_ids: Sequence[int]) -> dict[int, lockbox.StoredMatch]:
    """Read-only: each match as stored now (kickoff, final or not, the protocol counts per player) with when its vendor
    data was first and last fetched."""
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            uids = [str(u) for u in fixture_ids]
            cur.execute(MATCHES_SQL, (lockbox.PROTOCOL.data_vendor, lockbox.PROTOCOL.data_vendor, uids))
            found = {int(uid): (kickoff.astimezone(timezone.utc), bool(final), first, last, source)
                     for uid, kickoff, final, first, last, source in cur.fetchall()}
            cur.execute(PLAYERS_SQL, (uids,))
            players: dict[int, dict[int, tuple[int, Mapping[str, int]]]] = {}
            for uid, player_id, minutes, tackles, fouls in cur.fetchall():
                players.setdefault(int(uid), {})[int(player_id)] = (int(minutes or 0), {"tackles": tackles, "fouls": fouls})
    finally:
        conn.close()
    return {uid: lockbox.StoredMatch(kickoff, final, players.get(uid, {}),
                                     None if first is None else first.astimezone(timezone.utc),
                                     None if last is None else last.astimezone(timezone.utc), source)
            for uid, (kickoff, final, first, last, source) in found.items()}


def refetch(dsn: str, fixture_ids: Sequence[int]) -> set[int]:
    """The re-fetch at the grace: the matches fetched again from the vendor and merged through the ingestion engine
    (results.ingest, the results job's own path); returns those re-fetched and merged. One vendor request per match."""
    from prop_alpha.ingestion import results
    from prop_alpha.ingestion.pipeline import build_vendor_client

    environ = {**os.environ,
               "DATA_VENDOR_MAX_REQUESTS": os.environ.get("DATA_VENDOR_MAX_REQUESTS", str(2 * len(fixture_ids) + 2)),
               "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", "100")}
    client = build_vendor_client(environ)
    try:
        slate = results.ingest(client, dsn, [str(u) for u in fixture_ids], REFETCH_WORKERS)
    finally:
        client.close()
    if slate.merge_error is not None:
        return set()
    failed = {str(uid) for uid in slate.failed} | {str(uid) for uid in slate.timed_out}
    return {uid for uid in fixture_ids if str(uid) not in failed}


def match_from_payload(payload: Mapping[str, Any], fetched_at: datetime, kickoff_at: datetime,
                       player_ids: Mapping[str, int], source: str) -> lockbox.StoredMatch | None:
    """Pure: the match as the stored payload had it (protocol counts per player id), or None when that payload was not of a
    final match. A player the players table does not know is left out (no lockbox prediction can be for him)."""
    if source != lockbox.PROTOCOL.data_vendor:
        raise ValueError(f"fixture payload source {source!r} is not {lockbox.PROTOCOL.data_vendor!r}")
    parsed = parse_fixture_payload(payload)
    if parsed.fixture.status != "final":
        return None
    players = {player_ids[p.player_uid]: (p.minutes_played, {"tackles": p.tackles_attempted, "fouls": p.fouls_committed})
               for p in parsed.players if p.player_uid in player_ids}
    fetched = fetched_at.astimezone(timezone.utc)
    return lockbox.StoredMatch(kickoff_at, True, players, fetched, fetched, source)


def rebuild(dsn: str, fixture_id: int, kickoff_at: datetime, due: datetime) -> lockbox.StoredMatch | None:
    """Read-only: the match rebuilt from its last payload fetched at or before `due` (the end of its grace); None when
    there is none, or it was not of a final match (the outcome is then excluded, never scored)."""
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(REBUILD_PAYLOAD_SQL, (str(fixture_id), lockbox.PROTOCOL.data_vendor, due))
            row = cur.fetchone()
            if row is None:
                return None
            payload, fetched_at = row
            uids = [str(player["uid"]) for player in payload.get("players", [])]
            cur.execute(PLAYER_IDS_SQL, (uids,))
            player_ids = {str(uid): int(player_id) for uid, player_id in cur.fetchall()}
    finally:
        conn.close()
    return match_from_payload(payload, fetched_at, kickoff_at, player_ids, lockbox.PROTOCOL.data_vendor)


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
    """Every Gate 2 metric per market and line, and the verdict with each reason it is not a pass; `skill_and_calibration_
    passed` is this lockbox's own verdict on every line's criteria, the benchmark left out."""
    rng = np.random.default_rng(lockbox.PROTOCOL.seed)
    result: dict[str, Any] = {"markets": {}, "blockers": []}
    line_failures = 0
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
                line_failures += 1
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
            result["blockers"].append(f"{market} market benchmark: {len(bench)} predictions with a two-way closing price "
                                      f"here, {MIN_BENCHMARK_PREDICTIONS} needed: {MARKET_LOCKBOX_NOTE}")
        result["markets"][market] = entry
    result["blockers"] += [f"{m}: a PRD target market the frozen model does not price" for m in UNPRICED_TARGET_MARKETS]
    result["skill_and_calibration_passed"] = line_failures == 0
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
    dsn = os.environ.get(DSN_ENV, DEFAULT_DSN)
    records = PredictionLog(STATE / "predictions.jsonl").load()
    candidates = sorted({int(r["fixture_id"]) for r in records if lockbox.record_reason(r) is None})
    matches = read_matches(dsn, candidates)
    selection = lockbox.eligible(records, {uid: m.kickoff_at for uid, m in matches.items()})
    captured = load_outcomes(OUTCOMES_FILE)
    # The capture amendment of 2026-10-27 (LOCKBOX_PROTOCOL.md section 6): within the re-fetch window after the grace a
    # match is re-fetched and captured from the fresh data; later it is rebuilt from the last payload fetched at or before
    # the grace, or excluded when there is none. A dry run fetches and writes nothing.
    plan = lockbox.capture_plan(selection.eligible, captured, matches, now)
    to_refetch = sorted(uid for uid, action in plan.items() if action == lockbox.REFETCH)
    to_rebuild = sorted(uid for uid, action in plan.items() if action == lockbox.REBUILD)
    new: list[dict[str, Any]] = []
    refetched: set[int] = set()
    if not args.dry_run:
        refetched = refetch(dsn, to_refetch) if to_refetch else set()
        fresh = read_matches(dsn, sorted(refetched)) if refetched else {}
        new += lockbox.outcomes_to_capture(selection.eligible, captured, fresh, now, basis=lockbox.REFETCHED)
        rebuilt = {uid: rebuild(dsn, uid, matches[uid].kickoff_at, matches[uid].kickoff_at + protocol.revision_grace)
                   for uid in to_rebuild}
        new += lockbox.outcomes_to_capture(selection.eligible, captured,
                                           {uid: m for uid, m in rebuilt.items() if m is not None}, now,
                                           basis=lockbox.REBUILT)
        new += lockbox.exclusion_records(selection.eligible, captured, matches,
                                         {uid for uid, m in rebuilt.items() if m is None}, now)
    if new:
        OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OUTCOMES_FILE.open("a", encoding="utf-8") as handle:
            for outcome in new:
                handle.write(json.dumps(outcome) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        captured = load_outcomes(OUTCOMES_FILE)
    scoring = lockbox.scoring_rows(selection.eligible, captured, now)
    changed = lockbox.revised(captured, matches)
    print(f"Gate 2 prospective lockbox: kickoff from {protocol.start:%Y-%m-%d %H:%M} UTC, digest {protocol.pricing_digest[:12]}, "
          f"data {protocol.data_vendor}; {len(records)} logged lines, {len(selection.eligible)} eligible, "
          f"{len({int(r['fixture_id']) for r in selection.eligible})} matches")
    for reason, count in sorted(selection.excluded.items()):
        print(f"  not eligible: {count} - {reason}")
    for state, count in sorted(scoring.states.items()):
        print(f"  eligible, {state}: {count}")
    print(f"  capture plan this run: {len(to_refetch)} match(es) to re-fetch at the grace ({len(refetched)} re-fetched), "
          f"{len(to_rebuild)} to rebuild from data before the grace"
          + (" - dry run: nothing fetched or written" if args.dry_run else ""))
    print(f"  outcomes captured: {len(captured)} ({len(new)} new{' - dry run, not written' if args.dry_run else ''}); "
          f"revised at the source since capture (not applied): {len(changed)}")
    timing = lockbox.capture_timing(captured)
    print("  " + lockbox.capture_timing_line(timing))
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
        print("  Also open whatever the lockbox shows: the market benchmark needs two-way closing prices, none here (no "
              f"bookmaker quotes these markets); it is {MARKET_LOCKBOX_NOTE}. Scope (amendment of 2026-10-27): tackles "
              "and fouls; line-ups estimated at the scan, the prediction a tip rests on.")
        return 1
    if args.dry_run:
        print("Ready to score; not scored in a dry run (scoring happens once and is written).")
        return 1
    result = score(scoring, closing_over(load_closing_quotes(STATE)))
    result.update({"scored_at": now.isoformat(), "protocol_start": protocol.start.isoformat(),
                   "pricing_digest": protocol.pricing_digest, "data_vendor": protocol.data_vendor, "seed": protocol.seed,
                   "eligible": len(selection.eligible), "excluded": selection.excluded, "states": scoring.states,
                   "revised_since_capture": len(changed), "capture_timing": asdict(timing)})
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
