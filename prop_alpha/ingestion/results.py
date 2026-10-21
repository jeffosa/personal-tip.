"""Results job: ingest every League One and League Two match (with `--pilot`, every match of the pilot leagues, into the
pilot database) that has finished since the latest final fixture in the database, so the scan prices from current history, bets settle (alerts/ledger.py --settle) and the Gate 2
lockbox fills. Runs first in scripts/telegram_status.cmd, every 4 hours.

    python -m prop_alpha.ingestion.results              # list, then ingest what is missing
    python -m prop_alpha.ingestion.results --dry-run    # list only (one or two vendor requests)
    python -m prop_alpha.ingestion.results --pilot      # the pilot leagues into the pilot database (alerts/ladder_scan.py)

Data: Sportmonks (back since 2026-10-21, maintainer: the Starter plan, whose counts match Opta's; TheStatsAPI from 2026-10-15
to 2026-10-20): one listing request per page, then one fixture request per missing match through ingestion/sportmonks.py.

The listing covers [latest stored final kickoff - LOOKBACK, today], at most MAX_SPAN per run, so a job that has
not run for weeks catches up over several runs. The latest stored final is taken per league of the run and the earliest of
them is used (2026-10-21): one league loaded further ahead (the pilot's Scottish Premiership, on the free plan) must not
move the window past matches another league is still missing. Fixtures already final in the database are skipped; the rest go
through ConcurrentIngestionEngine (job "stats"), which does all writes. Every listing page and every fixture spends
one Sportmonks request (DATA_VENDOR_MAX_REQUESTS, default DEFAULT_MAX_REQUESTS). A listing longer than
MAX_LISTING_PAGES raises rather than ingest part of it. Exit code 1 when any fixture failed or was refused.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Mapping, Sequence

from prop_alpha.config import DEFAULT_DSN, DEFAULT_ENV_FILE, DEFAULT_PILOT_DSN, DSN_ENV, PILOT_DSN_ENV
from prop_alpha.ingestion import sportmonks

logger = logging.getLogger("prop_alpha.results")

# Re-list this far behind the latest stored final: a match can turn final hours after a later one does.
LOOKBACK: Final[timedelta] = timedelta(days=3)
MAX_SPAN: Final[timedelta] = timedelta(days=14)
MAX_LISTING_PAGES: Final[int] = 10
DEFAULT_MAX_REQUESTS: Final[int] = 80
DEFAULT_WORKERS: Final[int] = 4
# Pilot fixtures the data adapter refused (a ValueError: a vendor data defect, e.g. a Scottish Championship match with no
# tackles in any line-up): fixture id -> reason. They are not retried, not counted as missing by this job, and not
# counted as stale by alerts/ladder_scan.py; a network error is never recorded, so it is retried.
PILOT_REFUSED_FILE: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state" / "pilot_refused.json"
# The same record for League One/Two (the working database), since 2026-10-15: before it, a League One/Two match that can
# never load (e.g. the backfill's ambiguous League Two matches, where both starting XIs list 10) stayed missing, was
# re-requested on every run and failed the job until the listing window moved past it.
REFUSED_FILE: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state" / "refused.json"

# TheStatsAPI's answer for a match it holds no player statistics for (HTTP 404): a permanent gap, recorded as refused.
STATS_NOT_AVAILABLE: Final[str] = "STATS_NOT_AVAILABLE_AT_SOURCE"
# The latest final kickoff of each named league (leagues.name, as in SCAN_LEAGUES and PILOT_LEAGUES_WANTED).
LATEST_FINAL_SQL: Final[str] = """
    SELECT l.name, max(f.kickoff_at) FROM prop_alpha.fixtures f JOIN prop_alpha.leagues l USING (league_id)
    WHERE f.status = 'final' AND l.name = ANY(%s) GROUP BY l.name
"""
# Final fixtures from a day before the listing window starts (kickoff dates are listed in UTC).
REFRESH_INCOMPLETE_SQL: Final[str] = "SELECT prop_alpha.refresh_incomplete_team_matches(%s)"
STORED_FINAL_SQL: Final[str] = """
    SELECT vendor_fixture_uid FROM prop_alpha.fixtures
    WHERE status = 'final' AND kickoff_at >= %s - %s - interval '1 day'
"""


@dataclass
class ResultsReport:
    start: date
    end: date
    listed: int = 0
    missing: list[str] = field(default_factory=list)
    ingested: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    rejected_rows: int = 0
    incomplete: int = 0
    skipped_refused: int = 0
    # Matches TheStatsAPI holds no statistics for (STATS_NOT_AVAILABLE): recorded as refused and reported, but not a failure
    # of the run (2026-10-15: the first run on TheStatsAPI raised a TASK FAILING alert over one such Scottish match).
    unavailable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.rejected_rows

    def text(self) -> str:
        line = (f"Results {self.start:%d %b}-{self.end:%d %b}: {self.listed} finished, {len(self.missing)} not yet "
                f"stored, {self.ingested} ingested")
        if self.skipped_refused:
            line += f" ({self.skipped_refused} refused earlier, not retried)"
        if self.unavailable:
            line += (f", {len(self.unavailable)} with no statistics at the source, recorded and not retried "
                     f"({', '.join(sorted(self.unavailable))})")
        if self.failed:
            line += f", {len(self.failed)} FAILED ({', '.join(sorted(self.failed))})"
        if self.rejected_rows:
            line += f", {self.rejected_rows} rows REJECTED (see staging_rejects)"
        if self.incomplete:
            line += f"; {self.incomplete} club-matches in the window have a player without an ID (left out of pricing)"
        return line


def listing_window(latest_final: datetime | None, now: datetime) -> tuple[date, date]:
    """The dates to list: from LOOKBACK before the latest stored final, at most MAX_SPAN, never past today."""
    if latest_final is None:
        raise ValueError("no final fixture is stored: backfill the database before running the results job")
    start = (latest_final - LOOKBACK).date()
    return start, min(now.date(), start + MAX_SPAN)


def earliest_latest(latest_by_league: Mapping[str, datetime], names: Sequence[str]) -> datetime | None:
    """The earliest of the named leagues' latest stored final kickoffs, so the listing starts behind every one of them;
    None when no league has one. A named league with none stored raises: backfill it first."""
    if not latest_by_league:
        return None
    absent = sorted(set(names) - set(latest_by_league))
    if absent:
        raise ValueError(f"no final fixture is stored for {', '.join(absent)}: backfill it before running the results job")
    return min(latest_by_league[name] for name in names)


def missing_fixtures(listed: Sequence[tuple[str, datetime]], stored_final: frozenset[str]) -> list[str]:
    """Listed finished fixtures not yet final in the database, earliest kickoff first."""
    return [uid for uid, _ in sorted(listed, key=lambda item: (item[1], item[0])) if uid not in stored_final]


def load_refused(path: Path) -> dict[str, str]:
    """The recorded refusals (fixture id -> reason); none when the file does not exist. A damaged file raises."""
    if not path.is_file():
        return {}
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} is not a refusal record; fix or delete it")
    return {str(uid): str(reason) for uid, reason in raw.items()}


def record_refused(path: Path, failed: Mapping[str, str]) -> int:
    """Remember the fixtures in `failed` that cannot load (the adapter refused them, a reason starting with ValueError, or
    TheStatsAPI has no statistics for them, STATS_NOT_AVAILABLE_AT_SOURCE); returns how many are new. Network errors and
    timeouts are not recorded: they are retried."""
    known = load_refused(path)
    new = {uid: reason[:200] for uid, reason in failed.items()
           if (reason.startswith("ValueError") or STATS_NOT_AVAILABLE in reason) and uid not in known}
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**known, **new}, indent=1), encoding="utf-8")
    return len(new)


def list_finished(
    client: Any, start: date, end: date, leagues: Mapping[int, str] = sportmonks.SCAN_LEAGUES
) -> list[tuple[str, datetime]]:
    """(fixture uid, kickoff) of every finished Sportmonks fixture of `leagues` (league id -> name) between two dates."""
    window = (datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
              datetime(end.year, end.month, end.day, tzinfo=timezone.utc))
    listed: list[tuple[str, datetime]] = []
    for page in range(1, MAX_LISTING_PAGES + 1):
        rows, has_more = sportmonks.finished_fixtures(client.get_json(sportmonks.upcoming_path(*window, page, leagues)),
                                                      leagues)
        listed += rows
        if not has_more:
            return listed
    raise ValueError(f"results listing {start}..{end} has more than {MAX_LISTING_PAGES} pages; nothing ingested")


def pilot_leagues(client: Any) -> tuple[dict[int, str], list[str]]:
    """(Sportmonks id -> name for the pilot leagues the plan includes, the ones it does not): one or two requests. The ids
    are read from the plan's own league list (sportmonks.resolve_leagues), never written down."""
    rows: list[Mapping[str, Any]] = []
    for page in range(1, MAX_LISTING_PAGES + 1):
        page_rows, has_more = sportmonks.league_rows(client.get_json(f"{sportmonks.LEAGUES_PATH}&page={page}"))
        rows += page_rows
        if not has_more:
            return sportmonks.resolve_leagues(rows)
    raise ValueError(f"league listing has more than {MAX_LISTING_PAGES} pages")


def read_stored(dsn: str, names: Sequence[str]) -> tuple[datetime | None, frozenset[str]]:
    """(the earliest of the named leagues' latest final kickoffs, final fixture ids from LOOKBACK plus a day before it),
    in one read-only transaction."""
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=10000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(LATEST_FINAL_SQL, (list(names),))
                latest = earliest_latest({str(name): kickoff for name, kickoff in cur.fetchall()}, names)
                stored: frozenset[str] = frozenset()
                if latest is not None:
                    cur.execute(STORED_FINAL_SQL, (latest, LOOKBACK))
                    stored = frozenset(str(row[0]) for row in cur.fetchall())
    finally:
        conn.close()
    return latest, stored


def ingest(client: Any, dsn: str, fixture_uids: Sequence[str], workers: int) -> Any:
    from prop_alpha.ingestion.pipeline import ConcurrentIngestionEngine, build_pool, load_kappas

    pool = build_pool(dsn, workers)
    try:
        with ConcurrentIngestionEngine(client, pool, load_kappas(os.environ.get("PROP_ALPHA_KAPPAS")),
                                       max_workers=workers) as engine:
            return engine.run_slate(list(fixture_uids), job="stats")
    finally:
        pool.closeall()


def refresh_incomplete(dsn: str, since: date) -> int:
    """Recompute incomplete_team_matches (migration 008) for fixtures from `since`, in one short transaction; returns
    the rows written. Run after every ingest, so newly loaded matches with an unidentified player are quarantined
    before the next scan prices from them."""
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=60000", connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(REFRESH_INCOMPLETE_SQL, (datetime(since.year, since.month, since.day, tzinfo=timezone.utc),))
                row = cur.fetchone()
                if row is None:
                    raise ValueError("incomplete_team_matches refresh returned no row count")
                return int(row[0])
    finally:
        conn.close()


def run(
    now: datetime, client: Any, dsn: str, *, dry_run: bool = False, workers: int = DEFAULT_WORKERS,
    leagues: Mapping[int, str] = sportmonks.SCAN_LEAGUES, refused_path: Path,
) -> ResultsReport:
    """Fixtures recorded in `refused_path` (REFUSED_FILE, or PILOT_REFUSED_FILE for the pilot; required, so no caller can
    leave it out) are left out of `missing`, and the new permanent refusals are recorded after the ingest."""
    latest, stored = read_stored(dsn, sorted(set(leagues.values())))
    start, end = listing_window(latest, now)
    listed = list_finished(client, start, end, leagues)
    refused = frozenset(load_refused(refused_path))
    not_stored = missing_fixtures(listed, stored)
    report = ResultsReport(start, end, listed=len(listed), missing=[uid for uid in not_stored if uid not in refused],
                           skipped_refused=sum(uid in refused for uid in not_stored))
    if dry_run or not report.missing:
        return report
    slate = ingest(client, dsn, report.missing, workers)
    report.incomplete = refresh_incomplete(dsn, start - timedelta(days=1))
    report.failed = {str(uid): str(error) for uid, error in slate.failed.items()}
    report.failed.update({str(uid): "timed out" for uid in slate.timed_out})
    record_refused(refused_path, report.failed)
    report.unavailable = sorted(uid for uid, reason in report.failed.items() if STATS_NOT_AVAILABLE in reason)
    for uid in report.unavailable:
        del report.failed[uid]
    if slate.merge_error:
        report.failed["merge"] = str(slate.merge_error)
    report.ingested = slate.merge.fixtures_merged if slate.merge is not None else 0
    report.rejected_rows = slate.merge.rows_rejected if slate.merge is not None else 0
    return report


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest League One/Two results not yet in the database.")
    parser.add_argument("--dry-run", action="store_true", help="list what is missing; ingest nothing")
    parser.add_argument("--pilot", action="store_true",
                        help="the pilot leagues (sportmonks.PILOT_LEAGUES_WANTED the plan includes) into prop_alpha_pilot")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)
    from prop_alpha.ingestion.pipeline import build_vendor_client

    environ = {**os.environ,
               "DATA_VENDOR_MAX_REQUESTS": os.environ.get("DATA_VENDOR_MAX_REQUESTS", str(DEFAULT_MAX_REQUESTS)),
               "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", "100")}
    client = build_vendor_client(environ)
    try:
        if args.pilot:
            leagues, absent = pilot_leagues(client)
            if absent:
                print(f"Not in the Sportmonks plan, not ingested: {', '.join(absent)}")
            report = run(datetime.now(timezone.utc), client, os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN),
                         dry_run=args.dry_run, workers=args.workers, leagues=leagues, refused_path=PILOT_REFUSED_FILE)
        else:
            report = run(datetime.now(timezone.utc), client, os.environ.get(DSN_ENV, DEFAULT_DSN), dry_run=args.dry_run,
                         workers=args.workers, refused_path=REFUSED_FILE)
    finally:
        client.close()
    print(report.text())
    for uid, error in sorted(report.failed.items()):
        logger.error("FAILED %s: %s", uid, error)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
