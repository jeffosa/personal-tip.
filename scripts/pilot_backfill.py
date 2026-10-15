"""Pilot leagues (Scottish Premiership, Scottish Championship, Brazil Serie A): seed clubs and backfill finished matches
into the PILOT database.

    .venv\\Scripts\\python.exe -u scripts\\pilot_backfill.py [--list] [--seed] [--ingest] [--fix-leagues]

Writes only to `prop_alpha_pilot` (never the working `prop_alpha`): the pilot data must not touch the League One/Two
database, its counts or the frozen lockbox. With no flag it runs all four steps in order.
  --list         list finished Premier League (8) and Championship (9) fixtures since 2025-07-01 (about 30 listing
                 requests) and save them to .prop_alpha_state/pilot/finished_fixtures.json
  --seed         insert the two leagues and every club that appears (league = the club's most recent fixture's league)
  --ingest       ingest the listed fixtures not yet in the database through the real ConcurrentIngestionEngine
                 (8 workers, one Sportmonks request per fixture), in batches
  --fix-leagues  the merge files a fixture under its home club's current league; set each fixture's league from the
                 Sportmonks league_id kept in its stored payload instead
Spends about 1,200 Sportmonks requests in all, of the 2,000 an hour the plan allows.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.ingestion import results, sportmonks  # noqa: E402
from prop_alpha.ingestion.pipeline import ConcurrentIngestionEngine, build_pool, build_vendor_client, load_kappas  # noqa: E402

PILOT_DSN: Final[str] = "postgresql://postgres@localhost:5432/prop_alpha_pilot"
START: Final[datetime] = datetime(2025, 7, 1, tzinfo=timezone.utc)
WINDOW: Final[timedelta] = timedelta(days=90)
MAX_PAGES: Final[int] = 40
BATCH: Final[int] = 100
# Filled by main() from the plan's own league list (sportmonks.PILOT_LEAGUES_WANTED): Sportmonks id -> (database name,
# tier). The Premier League (8) was this pilot's until 2026-10-11 and its stored matches stay, but it is no longer
# listed or updated; a wanted league the plan does not include is reported and skipped.
LEAGUES: Final[dict[int, tuple[str, int]]] = {}
TIERS: Final[dict[str, int]] = {"Scottish Premiership": 1, "Scottish Championship": 2, "Brazil Serie A": 1}
COUNTRIES: Final[dict[str, str]] = {"Scottish Premiership": "Scotland", "Scottish Championship": "Scotland", "Brazil Serie A": "Brazil"}
OUT: Final[Path] = ROOT / ".prop_alpha_state" / "pilot" / "finished_fixtures.json"


def client(max_requests: int) -> Any:
    return build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": str(max_requests),
                                "DATA_VENDOR_QUOTA_RESERVE": "200"})


def list_finished() -> list[dict[str, Any]]:
    c = client(80)
    found: dict[int, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    start = START
    leagues = ",".join(str(i) for i in sorted(LEAGUES))
    try:
        while start < now:
            end = min(start + WINDOW, now)
            for page in range(1, MAX_PAGES + 1):
                body = c.get_json(f"fixtures/between/{start:%Y-%m-%d}/{end:%Y-%m-%d}?filters=fixtureLeagues:{leagues}"
                                  f"&include=participants;state&per_page=50&page={page}")
                for fx in body.get("data") or []:
                    state = (fx.get("state") or {}).get("developer_name")
                    if fx.get("league_id") in LEAGUES and state in sportmonks.FINAL_STATES:
                        home, away = sportmonks._sides(fx)  # noqa: SLF001 - the adapter's own home/away reader
                        names = {(p.get("meta") or {}).get("location"): str(p.get("name")) for p in fx.get("participants") or []}
                        found[int(fx["id"])] = {"id": int(fx["id"]), "league_id": int(fx["league_id"]), "home": home, "away": away,
                                                "home_name": names.get("home", home), "away_name": names.get("away", away),
                                                "kickoff": fx.get("starting_at")}
                if not (body.get("pagination") or {}).get("has_more"):
                    break
            start = end + timedelta(days=1)
    finally:
        c.close()
    fixtures = sorted(found.values(), key=lambda f: (f["kickoff"], f["id"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixtures), encoding="utf-8")
    by_league = {LEAGUES[i][0]: sum(1 for f in fixtures if f["league_id"] == i) for i in LEAGUES}
    print(f"listed {len(fixtures)} finished fixtures: {by_league}; saved {OUT}", flush=True)
    return fixtures


def load_listing() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(OUT.read_text(encoding="utf-8"))
    return loaded


def seed(fixtures: list[dict[str, Any]]) -> None:
    import psycopg2

    latest: dict[str, tuple[str, int, str]] = {}   # club uid -> (kickoff, league id, name)
    for f in fixtures:
        for uid, name in ((f["home"], f["home_name"]), (f["away"], f["away_name"])):
            if uid not in latest or f["kickoff"] >= latest[uid][0]:
                latest[uid] = (f["kickoff"], f["league_id"], name)
    conn = psycopg2.connect(PILOT_DSN)
    with conn, conn.cursor() as cur:
        league_ids: dict[int, int] = {}
        for sm_id, (name, tier) in LEAGUES.items():
            cur.execute("INSERT INTO prop_alpha.leagues (name, country, tier) VALUES (%s, %s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET tier = EXCLUDED.tier RETURNING league_id",
                        (name, COUNTRIES.get(name, "England"), tier))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"league {name} was not inserted or found")
            league_ids[sm_id] = int(row[0])
        for uid, (_, sm_league, name) in sorted(latest.items(), key=lambda kv: int(kv[0])):
            cur.execute("INSERT INTO prop_alpha.teams (league_id, name, vendor_team_uid) VALUES (%s, %s, %s) "
                        "ON CONFLICT (vendor_team_uid) DO UPDATE SET league_id = EXCLUDED.league_id, name = EXCLUDED.name",
                        (league_ids[sm_league], name, uid))
        cur.execute("SELECT count(*) FROM prop_alpha.teams")
        counted = cur.fetchone()
        if counted is None:
            raise RuntimeError("could not count the seeded clubs")
        print(f"seeded {len(league_ids)} leagues and {counted[0]} clubs into prop_alpha_pilot", flush=True)
    conn.close()


def ingest(fixtures: list[dict[str, Any]]) -> None:
    import psycopg2

    conn = psycopg2.connect(PILOT_DSN)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final'")
        have = {str(r[0]) for r in cur.fetchall()}
    conn.close()
    todo = [str(f["id"]) for f in fixtures if str(f["id"]) not in have]
    print(f"{len(have)} already stored, {len(todo)} to ingest in batches of {BATCH}", flush=True)
    c = client(len(todo) + 60)
    pool = build_pool(PILOT_DSN, 8)
    failed: dict[str, str] = {}
    try:
        with ConcurrentIngestionEngine(c, pool, load_kappas(os.environ.get("PROP_ALPHA_KAPPAS")), max_workers=8) as engine:
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                report = engine.run_slate(batch, job="stats")
                failed.update(report.failed)
                results.record_refused(results.PILOT_REFUSED_FILE, {str(u): str(e) for u, e in report.failed.items()})
                print(f"  batch {i // BATCH + 1}: {len(report.succeeded)}/{len(batch)} ok, merge {report.merge}, "
                      f"{len(report.timed_out)} timed out", flush=True)
    finally:
        c.close()
        pool.closeall()
    print(f"ingest finished: {len(failed)} failed", flush=True)
    for uid, reason in sorted(failed.items()):
        print(f"  FAILED {uid}: {reason[:200]}", flush=True)


def fix_leagues(fixtures: list[dict[str, Any]]) -> None:
    import psycopg2

    conn = psycopg2.connect(PILOT_DSN)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT league_id, name FROM prop_alpha.leagues")
        by_name = {str(n): int(i) for i, n in cur.fetchall()}
        changed = 0
        for sm_id, (name, _) in LEAGUES.items():
            uids = [str(f["id"]) for f in fixtures if f["league_id"] == sm_id]
            cur.execute("UPDATE prop_alpha.fixtures SET league_id = %s WHERE vendor_fixture_uid = ANY(%s) AND league_id <> %s",
                        (by_name[name], uids, by_name[name]))
            changed += cur.rowcount
        print(f"fixtures re-filed under the league Sportmonks lists them in: {changed}", flush=True)
    conn.close()


def resolve_leagues() -> None:
    """Fill LEAGUES from the Sportmonks plan's league list (a few requests). This is the Sportmonks archive's backfill: the
    pilot database is built from TheStatsAPI since 2026-10-15 (scripts/tsa_backfill.py)."""
    c = client(5)
    try:
        rows: list[Any] = []
        for page in range(1, 6):
            page_rows, has_more = sportmonks.league_rows(c.get_json(f"{sportmonks.LEAGUES_PATH}&page={page}"))
            rows += page_rows
            if not has_more:
                break
        found, absent = sportmonks.resolve_leagues(rows)
    finally:
        c.close()
    LEAGUES.clear()
    LEAGUES.update({sm_id: (name, TIERS[name]) for sm_id, name in found.items()})
    print(f"pilot leagues in the plan: {sorted(name for name, _ in LEAGUES.values())}; not in the plan: {absent}", flush=True)


def main() -> int:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    unknown = flags - {"--list", "--seed", "--ingest", "--fix-leagues"}
    if unknown:
        print(__doc__)
        return 2
    everything = not flags
    resolve_leagues()
    fixtures = list_finished() if everything or "--list" in flags else load_listing()
    if everything or "--seed" in flags:
        seed(fixtures)
    if everything or "--ingest" in flags:
        ingest(fixtures)
    if everything or "--fix-leagues" in flags:
        fix_leagues(fixtures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
