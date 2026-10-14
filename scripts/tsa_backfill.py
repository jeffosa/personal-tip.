"""Build the TheStatsAPI databases (maintainer, 2026-10-13: "stick with TheStatsAPI", fresh databases, all five leagues).

    .venv\\Scripts\\python.exe -u scripts\\tsa_backfill.py [--create] [--list] [--seed] [--ingest] [--fix-leagues] [--check]

With no flag it runs every step in order; each step can be run alone and again (nothing already stored is fetched twice).

  --create       create `prop_alpha_tsa` (League One and Two) and `prop_alpha_pilot_tsa` (Brazil Serie A and the two Scottish
                 leagues) and install schema.sql in each; an existing database is left as it is
  --list         list every finished match of the five competitions from START to today (about 30 requests) into
                 .prop_alpha_state/tsa/matches.json
  --seed         insert the leagues and every club that appears (a club's league = its most recent listed match's)
  --ingest       ingest each listed finished match not yet stored through the real ConcurrentIngestionEngine, one
                 player-stats request per match, in batches; refusals are printed and recorded
  --fix-leagues  the merge files a fixture under its home club's current league; set each fixture's league from its
                 listed competition instead
  --check        per league: stored matches, player rows and players, and the Gate 1 reconcile check (every final fixture
                 has a payload, payload players = stored rows, staging empty)

Writes only to the two new databases: the Sportmonks databases (`prop_alpha`, `prop_alpha_pilot`) are never touched and stay as
an archive. About 2,400 requests in all; at the trial's 12 a minute that is about 3.5 hours. DATA_VENDOR_QUOTA_RESERVE (default
here 1000) of the monthly quota is always left unspent. The PostgreSQL password comes from pgpass, never from here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backup import pg_tool  # noqa: E402  (scripts/ is on sys.path when this file runs as a script)

from prop_alpha.ingestion import thestatsapi  # noqa: E402
from prop_alpha.ingestion.pipeline import ConcurrentIngestionEngine, build_pool, build_thestatsapi_client, load_kappas  # noqa: E402

WORKING_DB: Final[str] = "prop_alpha_tsa"
PILOT_DB: Final[str] = "prop_alpha_pilot_tsa"
START: Final[str] = "2025-07-01"
MAX_PAGES: Final[int] = 30
BATCH: Final[int] = 100
WORKERS: Final[int] = 4
QUOTA_RESERVE: Final[str] = "1000"
STATE: Final[Path] = ROOT / ".prop_alpha_state" / "tsa"
LISTING: Final[Path] = STATE / "matches.json"
REFUSED: Final[Path] = STATE / "refused.json"
COUNTRIES: Final[Mapping[str, str]] = {"League One": "England", "League Two": "England", "Brazil Serie A": "Brazil",
                                       "Scottish Premiership": "Scotland", "Scottish Championship": "Scotland"}
TIERS: Final[Mapping[str, int]] = {"League One": 3, "League Two": 4, "Brazil Serie A": 1, "Scottish Premiership": 1,
                                   "Scottish Championship": 2}


def dsn(database: str) -> str:
    return f"postgresql://postgres@localhost:5432/{database}"


def database_for(league: str) -> str:
    return WORKING_DB if league in thestatsapi.WORKING_LEAGUES else PILOT_DB


def psql(database: str, *args: str) -> str:
    return subprocess.run([pg_tool("psql"), "-w", "-h", "localhost", "-U", "postgres", "-d", database, "-X", "-v",
                           "ON_ERROR_STOP=1", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def create() -> None:
    for database in (WORKING_DB, PILOT_DB):
        exists = psql("postgres", "-At", "-c", f"SELECT 1 FROM pg_database WHERE datname = '{database}'").strip() == "1"
        if exists:
            print(f"{database}: exists, left as it is", flush=True)
            continue
        subprocess.run([pg_tool("createdb"), "-w", "-h", "localhost", "-U", "postgres", database], check=True)
        psql(database, "-q", "-f", str(ROOT / "schema.sql"))
        print(f"{database}: created and schema.sql installed", flush=True)


def list_matches() -> list[Mapping[str, Any]]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    client = build_thestatsapi_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "60", "DATA_VENDOR_QUOTA_RESERVE": QUOTA_RESERVE})
    found: dict[str, Mapping[str, Any]] = {}
    try:
        for competition, league in thestatsapi.COMPETITIONS.items():
            page, pages = 1, 1
            while page <= pages:
                if page > MAX_PAGES:
                    raise ValueError(f"{league}: listing has more than {MAX_PAGES} pages")
                rows, pages = thestatsapi.match_rows(client.get_json(
                    thestatsapi.matches_path(competition, START, today, page, status="finished")))
                for row in rows:
                    found[str(row["id"])] = row
                page += 1
            print(f"{league}: {sum(1 for m in found.values() if m.get('competition_id') == competition)} finished matches "
                  f"since {START}", flush=True)
    finally:
        client.close()
    STATE.mkdir(parents=True, exist_ok=True)
    LISTING.write_text(json.dumps(sorted(found.values(), key=lambda m: str(m["utc_date"])), ensure_ascii=False), encoding="utf-8")
    return list(found.values())


def load_listing() -> list[Mapping[str, Any]]:
    if not LISTING.is_file():
        raise ValueError(f"no listing at {LISTING}: run --list first")
    loaded: list[Mapping[str, Any]] = json.loads(LISTING.read_text(encoding="utf-8"))
    return loaded


def by_database(matches: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    split: dict[str, list[Mapping[str, Any]]] = {WORKING_DB: [], PILOT_DB: []}
    for match in matches:
        split[database_for(thestatsapi.COMPETITIONS[str(match["competition_id"])])].append(match)
    return split


def seed(matches: Sequence[Mapping[str, Any]]) -> None:
    import psycopg2

    for database, rows in by_database(matches).items():
        conn = psycopg2.connect(dsn(database))
        with conn, conn.cursor() as cur:
            league_ids: dict[str, int] = {}
            for name in sorted({thestatsapi.COMPETITIONS[str(m["competition_id"])] for m in rows}):
                cur.execute("INSERT INTO prop_alpha.leagues (name, country, tier) VALUES (%s, %s, %s) "
                            "ON CONFLICT (name) DO UPDATE SET tier = EXCLUDED.tier RETURNING league_id",
                            (name, COUNTRIES[name], TIERS[name]))
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"league {name} was not inserted or found")
                league_ids[name] = int(row[0])
            clubs = thestatsapi.clubs(rows)
            for uid, (name, competition, _) in sorted(clubs.items()):
                cur.execute("INSERT INTO prop_alpha.teams (league_id, name, vendor_team_uid) VALUES (%s, %s, %s) "
                            "ON CONFLICT (vendor_team_uid) DO UPDATE SET league_id = EXCLUDED.league_id, name = EXCLUDED.name",
                            (league_ids[thestatsapi.COMPETITIONS[competition]], name, uid))
            print(f"{database}: seeded {len(league_ids)} leagues and {len(clubs)} clubs", flush=True)
        conn.close()


def ingest(matches: Sequence[Mapping[str, Any]]) -> None:
    import psycopg2

    refused: dict[str, str] = json.loads(REFUSED.read_text(encoding="utf-8")) if REFUSED.is_file() else {}
    for database, rows in by_database(matches).items():
        conn = psycopg2.connect(dsn(database))
        with conn, conn.cursor() as cur:
            cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final'")
            have = {str(r[0]) for r in cur.fetchall()}
        conn.close()
        todo = [str(m["id"]) for m in rows if str(m["id"]) not in have and str(m["id"]) not in refused]
        print(f"{database}: {len(have)} stored, {len(todo)} to ingest, {len(refused)} refused before", flush=True)
        if not todo:
            continue
        client = build_thestatsapi_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": str(len(todo) + 20),
                                           "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", QUOTA_RESERVE)},
                                          {str(m["id"]): m for m in rows})
        pool = build_pool(dsn(database), WORKERS)
        try:
            with ConcurrentIngestionEngine(client, pool, load_kappas(None), max_workers=WORKERS) as engine:
                for i in range(0, len(todo), BATCH):
                    report = engine.run_slate(todo[i:i + BATCH], job="stats")
                    # A match the adapter or the merge refused is recorded and not tried again; one that only timed out or
                    # hit a network or quota stop is not, so the next run retries it.
                    for uid, reason in report.failed.items():
                        if "ValueError" in reason or "refused" in reason.lower():
                            refused[uid] = reason[:300]
                    REFUSED.write_text(json.dumps(refused, indent=1, ensure_ascii=False), encoding="utf-8")
                    print(f"  batch {i // BATCH + 1}: {len(report.succeeded)}/{len(todo[i:i + BATCH])} ok, merge "
                          f"{report.merge}, {len(report.failed)} failed, {len(report.timed_out)} timed out", flush=True)
                    for uid, reason in sorted(report.failed.items())[:5]:
                        print(f"    FAILED {uid}: {reason[:200]}", flush=True)
        finally:
            client.close()
            pool.closeall()


def fix_leagues(matches: Sequence[Mapping[str, Any]]) -> None:
    import psycopg2

    for database, rows in by_database(matches).items():
        conn = psycopg2.connect(dsn(database))
        with conn, conn.cursor() as cur:
            cur.execute("SELECT league_id, name FROM prop_alpha.leagues")
            by_name = {str(n): int(i) for i, n in cur.fetchall()}
            changed = 0
            for name in {thestatsapi.COMPETITIONS[str(m["competition_id"])] for m in rows}:
                uids = [str(m["id"]) for m in rows if thestatsapi.COMPETITIONS[str(m["competition_id"])] == name]
                cur.execute("UPDATE prop_alpha.fixtures SET league_id = %s WHERE vendor_fixture_uid = ANY(%s) AND league_id <> %s",
                            (by_name[name], uids, by_name[name]))
                changed += cur.rowcount
            print(f"{database}: fixtures re-filed under their listed competition: {changed}", flush=True)
        conn.close()


CHECK_SQL: Final[str] = """
SELECT l.name, count(DISTINCT f.fixture_id), count(s.*), count(DISTINCT s.player_id)
FROM prop_alpha.fixtures f JOIN prop_alpha.leagues l USING (league_id)
LEFT JOIN prop_alpha.player_match_stats s USING (fixture_id)
WHERE f.status = 'final' GROUP BY l.name ORDER BY l.name
"""
# The Gate 1 reconcile check (scripts/gate1_checks.py), on one line: the latest payload per final fixture.
RECONCILE_SQL: Final[str] = """
SELECT (SELECT count(*) FROM prop_alpha.fixtures f WHERE f.status = 'final'
          AND NOT EXISTS (SELECT 1 FROM prop_alpha.raw_payloads r WHERE r.vendor_fixture_uid = f.vendor_fixture_uid)),
       (SELECT count(*) FROM prop_alpha.player_match_stats s JOIN prop_alpha.fixtures f USING (fixture_id)
          WHERE f.status = 'final'),
       (SELECT coalesce(sum(jsonb_array_length(r.payload -> 'players')), 0) FROM (
          SELECT DISTINCT ON (vendor_fixture_uid) vendor_fixture_uid, payload FROM prop_alpha.raw_payloads
          ORDER BY vendor_fixture_uid, fetched_at DESC, payload_id DESC) r
          JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = r.vendor_fixture_uid WHERE f.status = 'final'),
       (SELECT count(*) FROM prop_alpha.staging_player_feed) + (SELECT count(*) FROM prop_alpha.staging_rejects)
"""


def check(matches: Sequence[Mapping[str, Any]]) -> None:
    listed = Counter(thestatsapi.COMPETITIONS[str(m["competition_id"])] for m in matches)
    for database in (WORKING_DB, PILOT_DB):
        for line in psql(database, "-At", "-F", "|", "-c", CHECK_SQL).splitlines():
            league, fixtures, rows, players = line.split("|")
            print(f"{database}: {league}: {fixtures} of {listed.get(league, 0)} listed matches stored, {rows} player rows, "
                  f"{players} players", flush=True)
        missing, stored, payload, staging = psql(database, "-At", "-F", "|", "-c", RECONCILE_SQL).strip().split("|")
        print(f"{database}: reconcile: final fixtures without a payload {missing}, payload players {payload} = stored rows "
              f"{stored}: {'yes' if payload == stored else 'NO'}, staging rows {staging}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    flags = set(sys.argv[1:] if argv is None else argv)
    unknown = flags - {"--create", "--list", "--seed", "--ingest", "--fix-leagues", "--check"}
    if unknown:
        print(f"unknown flags: {sorted(unknown)}", file=sys.stderr)
        return 2
    everything = not flags
    if everything or "--create" in flags:
        create()
    matches = list_matches() if everything or "--list" in flags else load_listing()
    if everything or "--seed" in flags:
        seed(matches)
    if everything or "--ingest" in flags:
        ingest(matches)
    if everything or "--fix-leagues" in flags:
        fix_leagues(matches)
    if everything or "--check" in flags:
        check(matches)
    return 0


if __name__ == "__main__":
    sys.exit(main())
