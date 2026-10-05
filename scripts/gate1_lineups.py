"""PRD Gate 1 criterion 7: the lineups path, on a throwaway copy of the working database, from archived team sheets.

    python scripts/gate1_lineups.py [--fixtures 50]

"Archived team sheets" here are the line-ups inside each fixture's archived Sportmonks response
(raw_payloads.payload->'vendor_response'->'lineups': type 11 = starting XI, type 12 = bench), stored when the match was
ingested. They are the line-ups Sportmonks recorded for the match, not team sheets captured at T-60 before kickoff:
no pre-kickoff sheet has ever been archived.

Copies the working database (pg_dump, pg_restore) into `prop_alpha_lineups`, dropping it first if it exists, and never
writes to the working database. Picks FIXTURES final fixtures at random (fixed seed) among those with no unidentified
player (incomplete_team_matches); then, before the run, seeds an 'estimate' row (p_start 0.5, p_sub 0.2) for up to
SEED_PER_SIDE players per club who played for it within 120 days of the kickoff but are not on this sheet, so the zeroing half of the
criterion is exercised rather than vacuous. Then runs the real lineups job (ConcurrentIngestionEngine job "lineups",
the production pool settings, 8 workers) with a client that replays each archived response through the real
sportmonks.lineup_payload adapter instead of calling Sportmonks: no vendor request is spent.

Pass (PRD wording): for every chosen fixture, lineup_expectations holds exactly 11 starters (p_start = 1) per side,
and every other row for those fixtures has p_start = p_sub = 0, except the bench, which the job writes with
p_start = 0 and p_sub = bench_sub_probability (0.35): the PRD's "every other row" can only mean players not on the
sheet. Also reported: what the job does with a fixture whose archived sheet lists a player without a Sportmonks id.
Results: .prop_alpha_state/gate1_lineups_<date>.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Final, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prop_alpha.ingestion import sportmonks  # noqa: E402
from prop_alpha.ingestion.pipeline import ConcurrentIngestionEngine, build_pool  # noqa: E402

BIN: Final[Path] = Path(r"C:\Program Files\PostgreSQL\18\bin")
COPY_DB: Final[str] = "prop_alpha_lineups"
SOURCE_DB: Final[str] = "prop_alpha"
HOST_DSN: Final[str] = "postgresql://postgres@localhost:5432/{db}"
SEED: Final[int] = 20261005
SEED_PER_SIDE: Final[int] = 8
BENCH_SUB_PROBABILITY: Final[float] = 0.35
WORKERS: Final[int] = 8

CANDIDATES_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.raw_payloads r ON r.vendor_fixture_uid = f.vendor_fixture_uid
    WHERE f.status = 'final' AND jsonb_array_length(r.payload->'vendor_response'->'lineups') > 0
      AND NOT EXISTS (SELECT 1 FROM prop_alpha.incomplete_team_matches i WHERE i.fixture_id = f.fixture_id)
    ORDER BY f.vendor_fixture_uid
"""
UNIDENTIFIED_SQL: Final[str] = """
    SELECT DISTINCT f.vendor_fixture_uid
    FROM prop_alpha.fixtures f JOIN prop_alpha.incomplete_team_matches i USING (fixture_id)
    ORDER BY 1 LIMIT 3
"""
ARCHIVE_SQL: Final[str] = """
    SELECT DISTINCT ON (vendor_fixture_uid) vendor_fixture_uid, payload->'vendor_response'
    FROM prop_alpha.raw_payloads WHERE vendor_fixture_uid = ANY(%s)
    ORDER BY vendor_fixture_uid, fetched_at DESC
"""
# Club players of that season who are not on this fixture's sheet: they get a pre-existing estimate to be zeroed.
SEED_SQL: Final[str] = """
    INSERT INTO prop_alpha.lineup_expectations (fixture_id, player_id, p_start, p_sub_appearance, source)
    SELECT f.fixture_id, o.player_id, 0.5, 0.2, 'estimate'
    FROM prop_alpha.fixtures f
    CROSS JOIN LATERAL (
        SELECT ranked.player_id FROM (
            SELECT squad.player_id, row_number() OVER (PARTITION BY squad.team_id ORDER BY squad.player_id) AS n
            FROM (SELECT DISTINCT s.team_id, s.player_id
                  FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures g USING (fixture_id)
                  WHERE s.team_id IN (f.home_team_id, f.away_team_id)
                    AND g.kickoff_at BETWEEN f.kickoff_at - interval '120 days' AND f.kickoff_at + interval '120 days'
                    AND s.player_id <> ALL(%s::int[])) squad
        ) ranked WHERE ranked.n <= %s
    ) o
    WHERE f.vendor_fixture_uid = %s
    ON CONFLICT DO NOTHING
"""
SHEET_IDS_SQL: Final[str] = "SELECT player_id FROM prop_alpha.players WHERE vendor_player_uid = ANY(%s)"
CHECK_SQL: Final[str] = """
    SELECT f.vendor_fixture_uid, t.vendor_team_uid, p.vendor_player_uid, le.p_start, le.p_sub_appearance, le.source
    FROM prop_alpha.lineup_expectations le
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.players p USING (player_id)
    LEFT JOIN prop_alpha.player_match_stats s ON s.fixture_id = le.fixture_id AND s.player_id = le.player_id
    LEFT JOIN prop_alpha.teams t ON t.team_id = s.team_id
    WHERE f.vendor_fixture_uid = ANY(%s)
"""


class ArchivedLineups:
    """The VendorClient surface the lineups job uses, answered from archived Sportmonks responses."""

    def __init__(self, archive: Mapping[str, Mapping[str, Any]]) -> None:
        self.archive = archive
        self.requests_sent = 0

    def get_lineups(self, fixture_vendor_uid: str) -> dict[str, Any]:
        return sportmonks.lineup_payload({"data": self.archive[fixture_vendor_uid]})

    def close(self) -> None:
        return None


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def copy_database() -> None:
    import psycopg2

    dump = ROOT / ".prop_alpha_state" / "backups" / "lineups_source.dump"
    dump.parent.mkdir(parents=True, exist_ok=True)
    run([str(BIN / "pg_dump"), "-w", "-h", "localhost", "-U", "postgres", "-d", SOURCE_DB, "-Fc", "-f", str(dump)])
    admin = psycopg2.connect(HOST_DSN.format(db="postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {COPY_DB} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {COPY_DB}")
    admin.close()
    run([str(BIN / "pg_restore"), "-w", "-h", "localhost", "-U", "postgres", "-d", COPY_DB, "--no-owner", str(dump)])


def sheet_of(response: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
    """team uid -> {"starters", "bench"} vendor player uids, straight from the archived response."""
    return sportmonks.lineup_payload({"data": response})["lineups"]


def judge(uid: str, sheet: Mapping[str, Mapping[str, list[str]]], rows: list[tuple[Any, ...]]) -> list[str]:
    """Every way the stored rows of one fixture break criterion 7 (empty list = pass)."""
    problems: list[str] = []
    listed = {player: (team, role) for team, roles in sheet.items() for role in ("starters", "bench")
              for player in roles[role]}
    by_player = {str(row[2]): row for row in rows}
    for team, roles in sheet.items():
        starters = [p for p in roles["starters"] if p in by_player and float(by_player[p][3]) == 1.0]
        if len(starters) != 11:
            problems.append(f"{uid}: side {team} has {len(starters)} stored starters, not 11")
    for player, row in by_player.items():
        p_start, p_sub, source = float(row[3]), float(row[4]), str(row[5])
        role = listed.get(player, (None, None))[1]
        if role == "starters" and (p_start, p_sub) != (1.0, 0.0):
            problems.append(f"{uid}: starter {player} stored as ({p_start}, {p_sub})")
        elif role == "bench" and (p_start, p_sub) != (0.0, BENCH_SUB_PROBABILITY):
            problems.append(f"{uid}: bench {player} stored as ({p_start}, {p_sub})")
        elif role is None and (p_start, p_sub) != (0.0, 0.0):
            problems.append(f"{uid}: off-sheet {player} stored as ({p_start}, {p_sub}), not (0, 0)")
        if source != "confirmed_team_sheet":
            problems.append(f"{uid}: {player} still has source {source!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    import psycopg2

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--fixtures", type=int, default=50)
    args = parser.parse_args(argv)
    copy_database()
    dsn = HOST_DSN.format(db=COPY_DB)
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(CANDIDATES_SQL)
        candidates = [str(row[0]) for row in cur.fetchall()]
        cur.execute(UNIDENTIFIED_SQL)
        unidentified = [str(row[0]) for row in cur.fetchall()]
        chosen = sorted(random.Random(SEED).sample(candidates, args.fixtures))
        cur.execute(ARCHIVE_SQL, (chosen + unidentified,))
        archive = {str(uid): response for uid, response in cur.fetchall()}
        seeded = 0
        for uid in chosen:
            listed = [p for roles in sheet_of(archive[uid]).values() for p in roles["starters"] + roles["bench"]]
            cur.execute(SHEET_IDS_SQL, (listed,))
            ids = [int(row[0]) for row in cur.fetchall()]
            cur.execute(SEED_SQL, (ids, SEED_PER_SIDE, uid))
            seeded += cur.rowcount
    conn.close()

    pool = build_pool(dsn, WORKERS)
    try:
        with ConcurrentIngestionEngine(ArchivedLineups(archive), pool, max_workers=WORKERS,  # type: ignore[arg-type]
                                       bench_sub_probability=BENCH_SUB_PROBABILITY) as engine:
            report = engine.run_slate(chosen, job="lineups", merge=False)
            unidentified_report = engine.run_slate(unidentified, job="lineups", merge=False) if unidentified else None
    finally:
        pool.closeall()

    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(CHECK_SQL, (chosen,))
        stored = cur.fetchall()
    conn.close()
    problems: list[str] = [f"{uid}: job failed: {error}" for uid, error in report.failed.items()]
    problems += [f"{uid}: timed out" for uid in report.timed_out]
    for uid in chosen:
        problems += judge(uid, sheet_of(archive[uid]), [row for row in stored if str(row[0]) == uid])
    zeroed = sum(1 for row in stored if row[5] == "confirmed_team_sheet"
                 and (float(row[3]), float(row[4])) == (0.0, 0.0))
    result = {
        "date": date.today().isoformat(), "copy_database": COPY_DB, "seed": SEED, "fixtures": chosen,
        "candidates": len(candidates), "estimate_rows_seeded": seeded, "rows_checked": len(stored),
        "off_sheet_rows_zeroed": zeroed, "succeeded": len(report.succeeded), "failed": report.failed,
        "slate_seconds": round(report.elapsed_s, 2), "problems": problems,
        "unidentified_player_fixtures": {
            "fixtures": unidentified,
            "failed": unidentified_report.failed if unidentified_report is not None else {},
            "succeeded": unidentified_report.succeeded if unidentified_report is not None else [],
        },
        "passed": not problems and len(report.succeeded) == len(chosen),
        "note": "archived post-match Sportmonks line-ups, not pre-kickoff T-60 team sheets",
    }
    out = ROOT / ".prop_alpha_state" / f"gate1_lineups_{date.today().isoformat()}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"criterion 7: {'PASS' if result['passed'] else 'FAIL'}; {len(report.succeeded)}/{len(chosen)} fixtures, "
          f"{len(stored)} rows checked, {seeded} estimate rows seeded, {zeroed} off-sheet rows zeroed, "
          f"{len(problems)} problems; slate {report.elapsed_s:.2f}s")
    for problem in problems[:20]:
        print("  " + problem)
    print(f"fixtures with an unidentified player: {result['unidentified_player_fixtures']}")
    print(f"written: {out}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
