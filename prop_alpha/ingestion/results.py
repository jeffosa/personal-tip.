"""Results job: ingest every League One and League Two match that has finished since the latest final fixture in
the database, so the scan prices from current history, bets settle (alerts/ledger.py --settle) and the Gate 2
lockbox fills. Runs first in scripts/telegram_status.cmd, every 4 hours.

    python -m prop_alpha.ingestion.results              # list, then ingest what is missing
    python -m prop_alpha.ingestion.results --dry-run    # list only (one or two vendor requests)

The listing covers [latest stored final kickoff - LOOKBACK, today], at most MAX_SPAN per run, so a job that has
not run for weeks catches up over several runs. Fixtures already final in the database are skipped; the rest go
through ConcurrentIngestionEngine (job "stats"), which does all writes. Every listing page and every fixture spends
one Sportmonks request (DATA_VENDOR_MAX_REQUESTS, default DEFAULT_MAX_REQUESTS). A listing longer than
MAX_LISTING_PAGES raises rather than ingest part of it. Exit code 1 when any fixture failed or was refused.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Sequence

from prop_alpha.config import DEFAULT_DSN, DSN_ENV
from prop_alpha.ingestion import sportmonks

logger = logging.getLogger("prop_alpha.results")

# Re-list this far behind the latest stored final: a match can turn final hours after a later one does.
LOOKBACK: Final[timedelta] = timedelta(days=3)
MAX_SPAN: Final[timedelta] = timedelta(days=14)
MAX_LISTING_PAGES: Final[int] = 10
DEFAULT_MAX_REQUESTS: Final[int] = 80
DEFAULT_WORKERS: Final[int] = 4

LATEST_FINAL_SQL: Final[str] = "SELECT max(kickoff_at) FROM prop_alpha.fixtures WHERE status = 'final'"
# Final fixtures from a day before the listing window starts (kickoff dates are listed in UTC).
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

    @property
    def ok(self) -> bool:
        return not self.failed and not self.rejected_rows

    def text(self) -> str:
        line = (f"Results {self.start:%d %b}-{self.end:%d %b}: {self.listed} finished, {len(self.missing)} not yet "
                f"stored, {self.ingested} ingested")
        if self.failed:
            line += f", {len(self.failed)} FAILED ({', '.join(sorted(self.failed))})"
        if self.rejected_rows:
            line += f", {self.rejected_rows} rows REJECTED (see staging_rejects)"
        return line


def listing_window(latest_final: datetime | None, now: datetime) -> tuple[date, date]:
    """The dates to list: from LOOKBACK before the latest stored final, at most MAX_SPAN, never past today."""
    if latest_final is None:
        raise ValueError("no final fixture is stored: backfill the database before running the results job")
    start = (latest_final - LOOKBACK).date()
    return start, min(now.date(), start + MAX_SPAN)


def missing_fixtures(listed: Sequence[tuple[str, datetime]], stored_final: frozenset[str]) -> list[str]:
    """Listed finished fixtures not yet final in the database, earliest kickoff first."""
    return [uid for uid, _ in sorted(listed, key=lambda item: (item[1], item[0])) if uid not in stored_final]


def list_finished(client: Any, start: date, end: date) -> list[tuple[str, datetime]]:
    window = (datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
              datetime(end.year, end.month, end.day, tzinfo=timezone.utc))
    listed: list[tuple[str, datetime]] = []
    for page in range(1, MAX_LISTING_PAGES + 1):
        rows, has_more = sportmonks.finished_fixtures(client.get_json(sportmonks.upcoming_path(*window, page)))
        listed += rows
        if not has_more:
            return listed
    raise ValueError(f"results listing {start}..{end} has more than {MAX_LISTING_PAGES} pages; nothing ingested")


def read_stored(dsn: str) -> tuple[datetime | None, frozenset[str]]:
    """(latest final kickoff, final fixture ids from LOOKBACK plus a day before it), in one read-only
    transaction."""
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=10000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(LATEST_FINAL_SQL)
                latest = cur.fetchone()[0]
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


def run(now: datetime, client: Any, dsn: str, *, dry_run: bool = False, workers: int = DEFAULT_WORKERS) -> ResultsReport:
    latest, stored = read_stored(dsn)
    start, end = listing_window(latest, now)
    listed = list_finished(client, start, end)
    report = ResultsReport(start, end, listed=len(listed), missing=missing_fixtures(listed, stored))
    if dry_run or not report.missing:
        return report
    slate = ingest(client, dsn, report.missing, workers)
    report.failed = {str(uid): str(error) for uid, error in slate.failed.items()}
    report.failed.update({str(uid): "timed out" for uid in slate.timed_out})
    if slate.merge_error:
        report.failed["merge"] = str(slate.merge_error)
    report.ingested = slate.merge.fixtures_merged if slate.merge is not None else 0
    report.rejected_rows = slate.merge.rows_rejected if slate.merge is not None else 0
    return report


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest League One/Two results not yet in the database.")
    parser.add_argument("--dry-run", action="store_true", help="list what is missing; ingest nothing")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)
    from prop_alpha.ingestion.pipeline import build_vendor_client

    environ = {**os.environ,
               "DATA_VENDOR_MAX_REQUESTS": os.environ.get("DATA_VENDOR_MAX_REQUESTS", str(DEFAULT_MAX_REQUESTS)),
               "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", "100")}
    client = build_vendor_client(environ)
    try:
        report = run(datetime.now(timezone.utc), client, os.environ.get(DSN_ENV, DEFAULT_DSN), dry_run=args.dry_run,
                     workers=args.workers)
    finally:
        client.close()
    print(report.text())
    for uid, error in sorted(report.failed.items()):
        logger.error("FAILED %s: %s", uid, error)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
