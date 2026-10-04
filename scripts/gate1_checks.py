"""PRD Gate 1 checks on the working database.

    python scripts/gate1_checks.py refusals     # criterion 3: which finished fixtures never landed, and why
    python scripts/gate1_checks.py reconcile    # criterion 4: payloads v fixtures v player rows; staging empty
    python scripts/gate1_checks.py idempotency  # criterion 5: re-ingest 100 fixtures twice; pass 2 must change nothing

`refusals` lists every finished League One/Two fixture Sportmonks has from 1 July 2025 (about 30 listing calls),
compares it with the database, re-fetches each missing fixture once and runs it through the same adapter and
parser as ingestion (sportmonks.fixture_payload, transform.parse_fixture_payload), recording the exact error.
Nothing is written to the database.

`reconcile` is read-only SQL. Results are written to .prop_alpha_state/gate1_<check>_<date>.json.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.config import DEFAULT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.ingestion import sportmonks  # noqa: E402
from prop_alpha.ingestion.pipeline import build_vendor_client  # noqa: E402
from prop_alpha.ingestion.transform import parse_fixture_payload  # noqa: E402

START: Final[datetime] = datetime(2025, 7, 1, tzinfo=timezone.utc)
WINDOW: Final[timedelta] = timedelta(days=90)  # Sportmonks' between-dates listing allows at most 100 days
MAX_PAGES: Final[int] = 40


def _connect() -> Any:
    import psycopg2

    return psycopg2.connect(os.environ.get(DSN_ENV, DEFAULT_DSN),
                            options="-c default_transaction_read_only=on -c statement_timeout=120000")


def finished_fixtures(client: Any, now: datetime) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    start = START
    while start < now:
        end = min(start + WINDOW, now)
        for page in range(1, MAX_PAGES + 1):
            body = client.get_json(sportmonks.upcoming_path(start, end, page))
            for fixture in body.get("data") or []:
                state = (fixture.get("state") or {}).get("developer_name")
                if fixture.get("league_id") in sportmonks.SCAN_LEAGUES and state in sportmonks.FINAL_STATES:
                    found[str(fixture["id"])] = {"league": sportmonks.SCAN_LEAGUES[fixture["league_id"]],
                                                 "name": fixture.get("name"), "kickoff": fixture.get("starting_at")}
            if not (body.get("pagination") or {}).get("has_more"):
                break
        start = end + timedelta(days=1)
    return found


def refusals() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    client = build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "400", "DATA_VENDOR_QUOTA_RESERVE": "200"})
    try:
        vendor = finished_fixtures(client, now)
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final'")
                in_db = {str(r[0]) for r in cur.fetchall()}
        finally:
            conn.close()
        missing = sorted(set(vendor) - in_db)
        print(f"Sportmonks finished fixtures since {START:%Y-%m-%d}: {len(vendor)}; in database: "
              f"{len(set(vendor) & in_db)}; missing: {len(missing)}; in database but not listed: {len(in_db - set(vendor))}")
        details = []
        for uid in missing:
            try:
                parse_fixture_payload(client.get_fixture_feed(uid))
                reason = "parses now (was a transient failure: re-ingest it)"
            except Exception as exc:  # a diagnostic script: every reason is recorded, nothing is written
                reason = f"{type(exc).__name__}: {exc}"
            details.append({"uid": uid, **vendor[uid], "reason": reason})
            print(f"  {uid} {vendor[uid]['kickoff']} {vendor[uid]['name']}: {reason[:160]}")
    finally:
        client.close()
    kinds = Counter(d["reason"].split(":")[0] if not d["reason"].startswith("parses") else "parses now" for d in details)
    summary = Counter(_category(d["reason"]) for d in details)
    print("\nby category: " + "; ".join(f"{k}: {v}" for k, v in summary.most_common()))
    return {"vendor_finished": len(vendor), "in_database": len(set(vendor) & in_db), "missing": details,
            "by_exception": dict(kinds), "by_category": dict(summary)}


def _category(reason: str) -> str:
    text = reason.lower()
    if reason.startswith("parses now"):
        return "transient (parses now)"
    if "player" in text and ("unidentified" in text or "unknown" in text or "no player" in text or "player_id" in text):
        return "unidentified player"
    if "goal" in text or "score" in text:
        return "goal events v score"
    if "duels" in text:
        return "duels won > duels"
    return "other"


RECONCILE_SQL: Final[dict[str, str]] = {
    "final fixtures": "SELECT count(*) FROM prop_alpha.fixtures WHERE status = 'final'",
    "distinct payload fixtures (final)": """
        SELECT count(DISTINCT r.vendor_fixture_uid) FROM prop_alpha.raw_payloads r
        JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = r.vendor_fixture_uid WHERE f.status = 'final'""",
    "final fixtures without a payload": """
        SELECT count(*) FROM prop_alpha.fixtures f WHERE f.status = 'final'
          AND NOT EXISTS (SELECT 1 FROM prop_alpha.raw_payloads r WHERE r.vendor_fixture_uid = f.vendor_fixture_uid)""",
    "players in latest payloads (final)": """
        SELECT coalesce(sum(jsonb_array_length(r.payload->'players')), 0) FROM (
          SELECT DISTINCT ON (vendor_fixture_uid) vendor_fixture_uid, payload FROM prop_alpha.raw_payloads
          ORDER BY vendor_fixture_uid, fetched_at DESC, payload_id DESC) r
        JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = r.vendor_fixture_uid WHERE f.status = 'final'""",
    "player_match_stats rows (final)": """
        SELECT count(*) FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f USING (fixture_id)
        WHERE f.status = 'final'""",
    "staging rows": "SELECT count(*) FROM prop_alpha.staging_player_feed",
    "staging rejects": "SELECT count(*) FROM prop_alpha.staging_rejects",
}
MISMATCH_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, f.kickoff_at, jsonb_array_length(r.payload->'players') AS payload_players,
           (SELECT count(*) FROM prop_alpha.player_match_stats s WHERE s.fixture_id = f.fixture_id) AS stored
    FROM (SELECT DISTINCT ON (vendor_fixture_uid) vendor_fixture_uid, payload FROM prop_alpha.raw_payloads
          ORDER BY vendor_fixture_uid, fetched_at DESC, payload_id DESC) r
    JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = r.vendor_fixture_uid
    WHERE f.status = 'final'
      AND jsonb_array_length(r.payload->'players')
          <> (SELECT count(*) FROM prop_alpha.player_match_stats s WHERE s.fixture_id = f.fixture_id)
    ORDER BY f.kickoff_at LIMIT 20
"""


def reconcile() -> dict[str, Any]:
    conn = _connect()
    out: dict[str, Any] = {}
    try:
        with conn, conn.cursor() as cur:
            for name, sql in RECONCILE_SQL.items():
                cur.execute(sql)
                out[name] = int(cur.fetchone()[0])
                print(f"  {name}: {out[name]}")
            cur.execute(MISMATCH_SQL)
            out["player count mismatches (first 20)"] = [[str(c) for c in row] for row in cur.fetchall()]
    finally:
        conn.close()
    checks = {
        "every final fixture has a payload": out["final fixtures without a payload"] == 0,
        "payload players = stored rows": out["players in latest payloads (final)"] == out["player_match_stats rows (final)"],
        "staging empty": out["staging rows"] == 0,
        "zero rejects": out["staging rejects"] == 0,
    }
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    for row in out["player count mismatches (first 20)"]:
        print(f"     mismatch {row}")
    out["checks"] = checks
    return out


IDEMPOTENCY_SAMPLE: Final[int] = 100
IDEMPOTENCY_SEED: Final[int] = 20261004
# Columns that legitimately change on every re-merge, so are left out of the comparison.
VOLATILE: Final[dict[str, tuple[str, ...]]] = {
    "player_match_stats": ("source_updated_at",), "fixtures": ("ingested_at",), "players": ("updated_at",),
}


def snapshot(fixture_uids: list[str]) -> dict[str, list[list[Any]]]:
    """Every column of the rows the sample's re-merge can touch, minus VOLATILE, in key order."""
    conn = _connect()
    out: dict[str, list[list[Any]]] = {}
    try:
        with conn, conn.cursor() as cur:
            queries = {
                "fixtures": "SELECT * FROM prop_alpha.fixtures WHERE vendor_fixture_uid = ANY(%s) ORDER BY fixture_id",
                "player_match_stats": """SELECT s.* FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f
                    USING (fixture_id) WHERE f.vendor_fixture_uid = ANY(%s) ORDER BY s.fixture_id, s.player_id""",
                "players": """SELECT DISTINCT p.* FROM prop_alpha.players p JOIN prop_alpha.player_match_stats s
                    USING (player_id) JOIN prop_alpha.fixtures f USING (fixture_id)
                    WHERE f.vendor_fixture_uid = ANY(%s) ORDER BY p.player_id""",
            }
            for table, sql in queries.items():
                cur.execute(sql, (fixture_uids,))
                columns = [d[0] for d in cur.description]
                keep = [i for i, c in enumerate(columns) if c not in VOLATILE[table]]
                out[table] = [[columns[i] for i in keep]] + [[row[i] for i in keep] for row in cur.fetchall()]
    finally:
        conn.close()
    return out


def differences(before: dict[str, list[list[Any]]], after: dict[str, list[list[Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for table in before:
        header, old_rows, new_rows = before[table][0], before[table][1:], after[table][1:]
        old = {tuple(r[:2]): r for r in old_rows}
        new = {tuple(r[:2]): r for r in new_rows}
        changed = [(key, [header[i] for i in range(len(header)) if old[key][i] != new[key][i]])
                   for key in old.keys() & new.keys() if old[key] != new[key]]
        result[table] = {"rows": len(old_rows), "added": len(new.keys() - old.keys()),
                         "removed": len(old.keys() - new.keys()), "changed": len(changed),
                         "changed_columns": dict(Counter(c for _, cols in changed for c in cols))}
    return result


def ingest(fixture_uids: list[str]) -> Any:
    from prop_alpha.ingestion.pipeline import ConcurrentIngestionEngine, build_pool, load_kappas

    client = build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "150", "DATA_VENDOR_QUOTA_RESERVE": "200"})
    pool = build_pool(os.environ.get(DSN_ENV, DEFAULT_DSN), 8)
    try:
        with ConcurrentIngestionEngine(client, pool, load_kappas(os.environ.get("PROP_ALPHA_KAPPAS")),
                                       max_workers=8) as engine:
            return engine.run_slate(fixture_uids, job="stats")
    finally:
        client.close()
        pool.closeall()


def idempotency() -> dict[str, Any]:
    import random

    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final' ORDER BY 1")
            all_uids = [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
    sample = sorted(random.Random(IDEMPOTENCY_SEED).sample(all_uids, IDEMPOTENCY_SAMPLE))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result: dict[str, Any] = {"sample": sample, "seed": IDEMPOTENCY_SEED}
    for attempt in ("pass 1 (vendor corrections since the backfill)", "pass 2 (idempotency)"):
        before = snapshot(sample)
        backup = ROOT / ".prop_alpha_state" / f"gate1_idempotency_snapshot_{stamp}_{attempt[:6].replace(' ', '')}.json"
        backup.write_text(json.dumps(before, default=str), encoding="utf-8")
        report = ingest(sample)
        after = snapshot(sample)
        diff = differences(before, after)
        result[attempt] = {"ingest": {"ok": len(report.succeeded), "failed": report.failed,
                                      "timed_out": report.timed_out, "merge": str(report.merge)},
                           "differences": diff, "snapshot_before": str(backup)}
        print(f"{attempt}: {len(report.succeeded)} ok, {len(report.failed)} failed, merge {report.merge}")
        for table, d in diff.items():
            print(f"   {table}: {d['rows']} rows; added {d['added']}, removed {d['removed']}, changed {d['changed']} "
                  f"{d['changed_columns'] or ''}")
    second = result["pass 2 (idempotency)"]["differences"]
    result["idempotent"] = all(d["added"] == d["removed"] == d["changed"] == 0 for d in second.values())
    print(f"{'PASS' if result['idempotent'] else 'FAIL'}  re-ingesting and re-merging {len(sample)} fixtures changed "
          f"{'nothing' if result['idempotent'] else 'rows'} beyond the update timestamps")
    return result


def main() -> int:
    checks = {"refusals": refusals, "reconcile": reconcile, "idempotency": idempotency}
    if len(sys.argv) != 2 or sys.argv[1] not in checks:
        print(__doc__)
        return 2
    check = sys.argv[1]
    result = checks[check]()
    out = ROOT / ".prop_alpha_state" / f"gate1_{check}_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(f"\nresults: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
