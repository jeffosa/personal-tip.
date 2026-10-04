"""PRD Gate 1 criterion 6: concurrency soak, on a throwaway copy of the working database.

    python scripts/gate1_soak.py [--slates 10] [--slate-size 12]

Copies the working database (pg_dump, pg_restore) into `prop_alpha_soak`, drops it first if it exists, and never
touches the working database. Then runs SLATES back-to-back real `stats` slates of SLATE_SIZE already-loaded
fixtures (re-fetched from Sportmonks, 8 workers, the production pool settings: lock_timeout 2 s, statement_timeout
10 s) while three things run against the same copy:
  - run_referee_job for every league, in a loop;
  - a synthetic analyst: a REPEATABLE READ transaction that reads player_match_stats and holds its snapshot ~15 s;
  - CREATE INDEX CONCURRENTLY then DROP INDEX CONCURRENTLY on player_match_stats, in a loop.
Pass requires zero unrecovered failures (no failed or timed-out fixture, no merge_error, no 40P01 in the engine's
log, the referee job or the server's deadlock counter) and every "transient database error" retry followed by a
success or a recorded failure. Records p50 and p99 of merge and slate duration. Results:
.prop_alpha_state/gate1_soak_<date>.json. Spends about SLATES x SLATE_SIZE Sportmonks requests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.calibration.referee_job import run_referee_job  # noqa: E402
from prop_alpha.ingestion import pipeline  # noqa: E402
from prop_alpha.ingestion.pipeline import ConcurrentIngestionEngine, build_pool, build_vendor_client, load_kappas  # noqa: E402

BIN: Final[Path] = Path(r"C:\Program Files\PostgreSQL\18\bin")
SOAK_DB: Final[str] = "prop_alpha_soak"
SOURCE_DB: Final[str] = "prop_alpha"
HOST_DSN: Final[str] = "postgresql://postgres@localhost:5432/{db}"
SEED: Final[int] = 20261004
INDEX_NAME: Final[str] = "idx_soak_pms_tmp"
ANALYST_HOLD_S: Final[float] = 15.0


class LogTally(logging.Handler):
    """Counts the engine's retry and failure log lines."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.transient: list[str] = []
        self.statement_timeouts: list[str] = []
        self.failures: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        # logging.Handler.handle() already holds self.lock (an RLock) around emit(); do not replace or retake it.
        if "transient database error" in text:
            self.transient.append(text)
        if "[STATEMENT_TIMEOUT]" in text:
            self.statement_timeouts.append(text)
        if record.levelno >= logging.ERROR:
            self.failures.append(text)


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def copy_database() -> None:
    import psycopg2

    dump = ROOT / ".prop_alpha_state" / "backups" / "soak_source.dump"
    dump.parent.mkdir(parents=True, exist_ok=True)
    run([str(BIN / "pg_dump"), "-w", "-h", "localhost", "-U", "postgres", "-d", SOURCE_DB, "-Fc", "-f", str(dump)])
    admin = psycopg2.connect(HOST_DSN.format(db="postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SOAK_DB} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {SOAK_DB}")
    admin.close()
    run([str(BIN / "pg_restore"), "-w", "-h", "localhost", "-U", "postgres", "-d", SOAK_DB, "--no-owner", str(dump)])


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))] if ordered else float("nan")


class Background:
    """The three concurrent workloads. Each loops until stop is set and records errors rather than hiding them."""

    def __init__(self, dsn: str, league_ids: list[int]) -> None:
        self.dsn, self.league_ids = dsn, league_ids
        self.stop = threading.Event()
        self.counts = {"referee_jobs": 0, "analyst_snapshots": 0, "index_builds": 0, "index_drops": 0}
        self.errors: list[str] = []
        self.threads = [threading.Thread(target=t, daemon=True) for t in (self.referee, self.analyst, self.indexer)]

    def _record(self, who: str, exc: BaseException) -> None:
        self.errors.append(f"{who}: {type(exc).__name__} {getattr(exc, 'pgcode', '')} {str(exc).strip()[:200]}")

    def referee(self) -> None:
        while not self.stop.is_set():
            for league in self.league_ids:
                try:
                    run_referee_job(self.dsn, league)
                    self.counts["referee_jobs"] += 1
                except Exception as exc:  # noqa: BLE001 - soak harness: record every failure
                    self._record("referee_job", exc)
            self.stop.wait(1.0)

    def analyst(self) -> None:
        import psycopg2

        while not self.stop.is_set():
            try:
                conn = psycopg2.connect(self.dsn)
                conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*), avg(tackles_attempted) FROM prop_alpha.player_match_stats")
                    cur.fetchall()
                    self.stop.wait(ANALYST_HOLD_S)  # keeps the snapshot (and the xmin horizon) open
                    cur.execute("SELECT count(*) FROM prop_alpha.player_match_stats")
                    cur.fetchall()
                conn.rollback()
                conn.close()
                self.counts["analyst_snapshots"] += 1
            except Exception as exc:  # noqa: BLE001
                self._record("analyst", exc)
            self.stop.wait(1.0)

    def indexer(self) -> None:
        import psycopg2

        while not self.stop.is_set():
            try:
                conn = psycopg2.connect(self.dsn)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                                "ON prop_alpha.player_match_stats (fixture_id, minutes_played)")
                    self.counts["index_builds"] += 1
                    cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS prop_alpha.{INDEX_NAME}")
                    self.counts["index_drops"] += 1
                conn.close()
            except Exception as exc:  # noqa: BLE001
                self._record("indexer", exc)
            self.stop.wait(1.0)


def main() -> int:
    import psycopg2

    parser = argparse.ArgumentParser()
    parser.add_argument("--slates", type=int, default=10)
    parser.add_argument("--slate-size", type=int, default=12)
    parser.add_argument("--reuse", action="store_true", help="keep the existing %s instead of re-copying" % SOAK_DB)
    args = parser.parse_args()

    if not args.reuse:
        print("copying the working database into", SOAK_DB, flush=True)
        copy_database()
    dsn = HOST_DSN.format(db=SOAK_DB)
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final' ORDER BY 1")
        uids = [str(r[0]) for r in cur.fetchall()]
        cur.execute("SELECT league_id FROM prop_alpha.leagues ORDER BY 1")
        leagues = [int(r[0]) for r in cur.fetchall()]
        cur.execute("SELECT deadlocks FROM pg_stat_database WHERE datname = %s", (SOAK_DB,))
        deadlocks_before = int(cur.fetchone()[0])
    conn.close()
    chosen = random.Random(SEED).sample(uids, args.slates * args.slate_size)

    tally = LogTally()
    logging.getLogger().addHandler(tally)
    background = Background(dsn, leagues)
    pool = build_pool(dsn, 8)
    merge_times: list[float] = []
    slate_times: list[float] = []
    slates: list[dict[str, Any]] = []
    started = time.monotonic()
    for t in background.threads:
        t.start()
    time.sleep(2.0)
    try:
        with ConcurrentIngestionEngine(build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "400",
                                                            "DATA_VENDOR_QUOTA_RESERVE": "300"}),
                                       pool, load_kappas(None), max_workers=8) as engine:
            original = engine.merge_staging

            def timed_merge(*a: Any, **k: Any) -> Any:
                t0 = time.monotonic()
                try:
                    return original(*a, **k)
                finally:
                    merge_times.append(time.monotonic() - t0)

            engine.merge_staging = timed_merge  # type: ignore[method-assign]
            for i in range(args.slates):
                batch = chosen[i * args.slate_size:(i + 1) * args.slate_size]
                report = engine.run_slate(batch, job="stats", deadline_s=60.0)
                slate_times.append(report.elapsed_s)
                slates.append({"n": len(batch), "ok": len(report.succeeded), "failed": report.failed,
                               "timed_out": report.timed_out, "merge_error": report.merge_error,
                               "merge": str(report.merge), "elapsed_s": round(report.elapsed_s, 2)})
                print(f"slate {i + 1}/{args.slates}: {len(report.succeeded)}/{len(batch)} ok, "
                      f"{report.elapsed_s:.1f}s, merge_error={report.merge_error}", flush=True)
    finally:
        background.stop.set()
        for t in background.threads:
            t.join(timeout=ANALYST_HOLD_S + 30)
        pool.closeall()

    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT deadlocks FROM pg_stat_database WHERE datname = %s", (SOAK_DB,))
        deadlocks = int(cur.fetchone()[0]) - deadlocks_before
        cur.execute("SELECT count(*) FROM prop_alpha.staging_player_feed")
        staged_left = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM prop_alpha.staging_rejects")
        rejects = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname = %s", (INDEX_NAME,))
        leftover_index = int(cur.fetchone()[0])
    conn.close()

    failed = {uid: err for s in slates for uid, err in s["failed"].items()}
    vendor_network = sorted(uid for uid, err in failed.items() if "HTTPSConnectionPool" in err or "ReadTimeout" in err)
    unrecovered = list(failed) + [u for s in slates for u in s["timed_out"]]
    database_unrecovered = [u for u in unrecovered if u not in vendor_network]
    merge_errors = [s["merge_error"] for s in slates if s["merge_error"]]
    deadlock_errors = [e for e in background.errors + tally.failures if "40P01" in e or "deadlock" in e.lower()]
    result: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(), "database": SOAK_DB, "slates": slates,
        "wall_clock_s": round(time.monotonic() - started, 1), "background": background.counts,
        "background_errors": background.errors, "transient_retries": len(tally.transient),
        "statement_timeouts": len(tally.statement_timeouts), "error_log_lines": tally.failures,
        "server_deadlocks": deadlocks, "deadlock_errors": deadlock_errors, "staging_left": staged_left,
        "rejects": rejects, "leftover_index": leftover_index,
        "merge_s": {"n": len(merge_times), "p50": percentile(merge_times, 0.5), "p99": percentile(merge_times, 0.99),
                    "max": max(merge_times, default=float("nan"))},
        "slate_s": {"n": len(slate_times), "p50": statistics.median(slate_times) if slate_times else float("nan"),
                    "p99": percentile(slate_times, 0.99), "max": max(slate_times, default=float("nan"))},
        "unrecovered": unrecovered, "vendor_network_failures": vendor_network,
        "database_unrecovered": database_unrecovered, "merge_errors": merge_errors,
    }
    result["pass"] = bool(slates and not unrecovered and not merge_errors and not deadlock_errors and deadlocks == 0
                          and staged_left == 0 and rejects == 0 and not background.errors
                          and background.counts["referee_jobs"] > 0 and background.counts["index_builds"] > 0
                          and background.counts["analyst_snapshots"] > 0)
    for key in ("background", "transient_retries", "statement_timeouts", "server_deadlocks", "staging_left", "rejects",
                "merge_s", "slate_s", "background_errors", "error_log_lines"):
        print(f"{key}: {result[key]}")
    print(f"vendor network failures (Sportmonks read timeouts, not database): {vendor_network}")
    print(f"{'PASS' if result['pass'] else 'FAIL'}  concurrency soak: {len(unrecovered)} unrecovered failures "
          f"({len(database_unrecovered)} database), "
          f"{deadlocks} server deadlocks, {len(deadlock_errors)} 40P01 errors")
    out = ROOT / ".prop_alpha_state" / f"gate1_soak_{datetime.now(timezone.utc):%Y-%m-%d}.json"
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(f"results: {out}")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
