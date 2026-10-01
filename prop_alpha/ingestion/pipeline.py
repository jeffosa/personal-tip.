"""Concurrent vendor -> PostgreSQL ingestion.

Workers fetch and transform fixtures in parallel, holding no database connection during network I/O,
then append to the UNLOGGED staging table in a short per-fixture transaction. A single serialized
call to prop_alpha.merge_staging_player_feed() moves staged rows into the core tables. Confirmed team
sheets are written directly to lineup_expectations (one small, disjoint row set per fixture).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Iterable, Mapping, TypeVar

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prop_alpha.ingestion.transform import (
    DEFAULT_KAPPAS,
    STAGING_COLUMNS,
    build_staging_rows,
    parse_fixture_payload,
    parse_lineup_payload,
)

logger = logging.getLogger("prop_alpha.ingestion")

T = TypeVar("T")

# Deadlocks, serialization failures, lock timeouts and dropped connections are all
# OperationalError/InterfaceError subclasses in psycopg2 and are safe to retry: the whole
# per-fixture transaction is rolled back before the retry. QueryCanceled (57014, statement_timeout)
# is also an OperationalError, but _with_transaction catches it first and fails closed.
RETRYABLE_DB_ERRORS: Final[tuple[type[BaseException], ...]] = (psycopg2.OperationalError, psycopg2.InterfaceError)

INSERT_RAW_SQL: Final[str] = """
    INSERT INTO prop_alpha.raw_payloads (source, vendor_fixture_uid, payload, payload_sha256)
    VALUES (%s, %s, %s::jsonb, %s)
    ON CONFLICT (payload_sha256) DO NOTHING
"""

INSERT_STAGING_SQL: Final[str] = (
    f"INSERT INTO prop_alpha.staging_player_feed ({', '.join(STAGING_COLUMNS)}) VALUES %s"
)

MERGE_SQL: Final[str] = (
    "SELECT fixtures_merged, player_rows_merged, rows_rejected FROM prop_alpha.merge_staging_player_feed()"
)

LINEUP_UPSERT_SQL: Final[str] = """
    INSERT INTO prop_alpha.lineup_expectations AS le
        (fixture_id, player_id, p_start, p_sub_appearance, exp_minutes_start, exp_minutes_sub, source, updated_at)
    SELECT f.fixture_id, p.player_id, v.p_start, v.p_sub,
           COALESCE(h.avg_start, 85.0), COALESCE(h.avg_sub, 20.0), 'confirmed_team_sheet', now()
    FROM (VALUES %s) AS v(vendor_fixture_uid, vendor_player_uid, p_start, p_sub)
    JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = v.vendor_fixture_uid
    JOIN prop_alpha.players  p ON p.vendor_player_uid  = v.vendor_player_uid
    LEFT JOIN LATERAL (
        SELECT avg(s.minutes_played) FILTER (WHERE s.started)     AS avg_start,
               avg(s.minutes_played) FILTER (WHERE NOT s.started) AS avg_sub
        FROM prop_alpha.player_match_stats s
        WHERE s.player_id = p.player_id AND s.minutes_played > 0
    ) h ON TRUE
    ORDER BY p.player_id
    ON CONFLICT (fixture_id, player_id) DO UPDATE SET
        p_start           = EXCLUDED.p_start,
        p_sub_appearance  = EXCLUDED.p_sub_appearance,
        exp_minutes_start = EXCLUDED.exp_minutes_start,
        exp_minutes_sub   = EXCLUDED.exp_minutes_sub,
        source            = EXCLUDED.source,
        updated_at        = EXCLUDED.updated_at
    RETURNING player_id
"""
LINEUP_TEMPLATE: Final[str] = "(%s, %s, %s::numeric, %s::numeric)"

LINEUP_EXCLUDE_SQL: Final[str] = """
    UPDATE prop_alpha.lineup_expectations AS le
    SET p_start = 0, p_sub_appearance = 0, source = 'confirmed_team_sheet', updated_at = now()
    FROM prop_alpha.fixtures f
    WHERE f.fixture_id = le.fixture_id
      AND f.vendor_fixture_uid = %s
      AND NOT (le.player_id = ANY(%s::int[]))
"""


class TokenBucketRateLimiter:
    """Thread-safe token bucket honouring a vendor's requests-per-second contract across all workers."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        if rate_per_sec <= 0 or burst < 1:
            raise ValueError("rate_per_sec must be > 0 and burst >= 1")
        self._rate = rate_per_sec
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._lock = threading.Lock()
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last_refill) * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
            time.sleep(wait_s)


class VendorClient:
    """HTTP client with one requests.Session per worker thread (Session is not documented as thread-safe)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_limiter: TokenBucketRateLimiter,
        timeout: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})
            retry_policy = Retry(
                total=self.max_retries,
                backoff_factor=0.25,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry_policy, pool_maxsize=2)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def get_json(self, path: str) -> dict[str, Any]:
        self.rate_limiter.acquire()
        resp = self._session().get(f"{self.base_url}/{path.lstrip('/')}", timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"expected a JSON object from {path}")
        return body

    def get_fixture_feed(self, fixture_vendor_uid: str) -> dict[str, Any]:
        return self.get_json(f"fixtures/{fixture_vendor_uid}/stats")

    def get_lineups(self, fixture_vendor_uid: str) -> dict[str, Any]:
        return self.get_json(f"fixtures/{fixture_vendor_uid}/lineups")

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()


@dataclass(frozen=True)
class FixtureResult:
    fixture_uid: str
    ok: bool
    rows: int
    db_attempts: int
    elapsed_s: float
    error: str | None = None


@dataclass(frozen=True)
class MergeResult:
    fixtures_merged: int
    player_rows_merged: int
    rows_rejected: int


@dataclass
class SlateReport:
    job: str
    results: dict[str, FixtureResult] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)
    merge: MergeResult | None = None
    merge_error: str | None = None
    elapsed_s: float = 0.0

    @property
    def succeeded(self) -> list[str]:
        return sorted(uid for uid, r in self.results.items() if r.ok)

    @property
    def failed(self) -> dict[str, str]:
        return {uid: r.error or "unknown error" for uid, r in self.results.items() if not r.ok}

    @property
    def fully_successful(self) -> bool:
        return not self.failed and not self.timed_out and self.merge_error is None


class ConcurrentIngestionEngine:
    """Processes a slate of fixtures in parallel with per-fixture transactional isolation."""

    JOBS: Final[tuple[str, ...]] = ("stats", "lineups")

    def __init__(
        self,
        client: VendorClient,
        pool: Any,
        kappas: Mapping[str, float] | None = None,
        max_workers: int = 8,
        max_db_attempts: int = 3,
        retry_base_delay_s: float = 0.05,
        bench_sub_probability: float = 0.35,
    ) -> None:
        if max_workers < 1 or max_db_attempts < 1:
            raise ValueError("max_workers and max_db_attempts must be >= 1")
        pool_size = getattr(pool, "maxconn", None)
        if pool_size is not None and pool_size < max_workers + 1:
            raise ValueError("connection pool maxconn must be >= max_workers + 1 (one spare for the merge)")
        self.client = client
        self.pool = pool
        self.kappas = dict(kappas or DEFAULT_KAPPAS)
        self.max_workers = max_workers
        self.max_db_attempts = max_db_attempts
        self.retry_base_delay_s = retry_base_delay_s
        self.bench_sub_probability = bench_sub_probability
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ingest")
        self._closed = False

    def __enter__(self) -> ConcurrentIngestionEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._closed = True

    # ---------------- slate orchestration ----------------
    def run_slate(
        self, fixture_uids: Iterable[str], job: str = "stats", deadline_s: float | None = None, merge: bool = True
    ) -> SlateReport:
        """
        Run one job for every fixture concurrently. Fixtures not finished by `deadline_s` are reported
        as timed_out; a task already running keeps going and its transaction commits or rolls back on
        its own, so a timed-out fixture may still land and be picked up by the next merge.
        """
        if self._closed:
            raise RuntimeError("engine is closed")
        if job not in self.JOBS:
            raise ValueError(f"job must be one of {self.JOBS}")
        task: Callable[[str], tuple[int, int]] = self._stats_task if job == "stats" else self._lineup_task
        uids = list(dict.fromkeys(u for u in fixture_uids if u))
        report = SlateReport(job=job)
        started = time.monotonic()

        futures: dict[Future[FixtureResult], str] = {
            self._executor.submit(self._run_isolated, task, uid): uid for uid in uids
        }
        done, not_done = wait(futures, timeout=deadline_s)
        for fut in done:
            result = fut.result()
            report.results[result.fixture_uid] = result
        for fut in not_done:
            fut.cancel()
        report.timed_out = sorted(futures[f] for f in not_done)

        if job == "stats" and merge and report.succeeded:
            try:
                report.merge = self.merge_staging()
            except Exception as exc:
                report.merge_error = f"{type(exc).__name__}: {exc}"
                logger.exception("staging merge failed; rows remain in staging for the next merge")

        report.elapsed_s = time.monotonic() - started
        logger.info(
            "slate %s: %d ok, %d failed, %d timed out, merge=%s in %.2fs",
            job, len(report.succeeded), len(report.failed), len(report.timed_out), report.merge, report.elapsed_s,
        )
        return report

    def merge_staging(self) -> MergeResult:
        def merge(cur: Any) -> MergeResult:
            cur.execute(MERGE_SQL)
            fixtures, rows, rejected = cur.fetchone()
            return MergeResult(int(fixtures), int(rows), int(rejected))

        result, _ = self._with_transaction(merge)
        if result.rows_rejected:
            logger.warning("merge rejected %d staging rows; see prop_alpha.staging_rejects", result.rows_rejected)
        return result

    # ---------------- per-fixture tasks ----------------
    def _run_isolated(self, task: Callable[[str], tuple[int, int]], uid: str) -> FixtureResult:
        started = time.monotonic()
        try:
            rows, attempts = task(uid)
            return FixtureResult(uid, True, rows, attempts, time.monotonic() - started)
        except Exception as exc:
            logger.error("fixture %s failed: %s: %s", uid, type(exc).__name__, exc)
            return FixtureResult(uid, False, 0, 0, time.monotonic() - started, f"{type(exc).__name__}: {exc}")

    def _stats_task(self, uid: str) -> tuple[int, int]:
        payload = self.client.get_fixture_feed(uid)
        parsed = parse_fixture_payload(payload)
        if parsed.fixture.vendor_uid != uid:
            raise ValueError(f"payload uid {parsed.fixture.vendor_uid} does not match request {uid}")
        rows = build_staging_rows(parsed, self.kappas, str(uuid.uuid4()))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

        def write(cur: Any) -> int:
            cur.execute(INSERT_RAW_SQL, ("vendor_fixture_stats", uid, body, digest))
            psycopg2.extras.execute_values(cur, INSERT_STAGING_SQL, rows, page_size=1000)
            return len(rows) - 1

        return self._with_transaction(write)

    def _lineup_task(self, uid: str) -> tuple[int, int]:
        payload = self.client.get_lineups(uid)
        entries = parse_lineup_payload(payload, self.bench_sub_probability)
        if any(e.vendor_fixture_uid != uid for e in entries):
            raise ValueError(f"lineup payload does not match request {uid}")
        values = [(e.vendor_fixture_uid, e.vendor_player_uid, e.p_start, e.p_sub_appearance) for e in entries]

        def write(cur: Any) -> int:
            returned = psycopg2.extras.execute_values(
                cur, LINEUP_UPSERT_SQL, values, template=LINEUP_TEMPLATE, page_size=100, fetch=True
            )
            listed_ids = [int(r[0]) for r in returned]
            if len(listed_ids) < len(values):
                logger.warning(
                    "fixture %s: %d of %d listed players unknown or fixture missing; seed them first",
                    uid, len(values) - len(listed_ids), len(values),
                )
            if not listed_ids:
                raise ValueError(f"fixture {uid}: no lineup rows matched existing fixture/players")
            cur.execute(LINEUP_EXCLUDE_SQL, (uid, listed_ids))
            return len(listed_ids)

        return self._with_transaction(write)

    # ---------------- transaction boundary ----------------
    def _with_transaction(self, fn: Callable[[Any], T]) -> tuple[T, int]:
        """Run fn(cursor) in its own pooled connection and transaction; retry transient failures.

        QueryCanceled (SQLSTATE 57014, statement_timeout) is caught before the retryable clause and
        re-raised at once: a statement that consumed the whole window will not finish faster.
        """
        last_error: BaseException | None = None
        for attempt in range(1, self.max_db_attempts + 1):
            conn = self.pool.getconn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        return fn(cur), attempt
            except psycopg2.errors.QueryCanceled as exc:
                logger.error("[STATEMENT_TIMEOUT] attempt %d failed closed without retry: %s", attempt, exc)
                raise
            except RETRYABLE_DB_ERRORS as exc:
                last_error = exc
                logger.warning("transient database error (attempt %d/%d): %s", attempt, self.max_db_attempts, exc)
            finally:
                self.pool.putconn(conn, close=bool(conn.closed))
            if attempt < self.max_db_attempts:
                time.sleep(self.retry_base_delay_s * (2 ** (attempt - 1)) * (1.0 + random.random()))
        if last_error is None:
            raise RuntimeError("transaction loop ended without a result or an error")
        raise last_error


def build_pool(dsn: str, max_workers: int) -> psycopg2.pool.ThreadedConnectionPool:
    """Pool sized for all workers plus the merge; timeouts bound worst-case latency during lock contention."""
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=max_workers + 1,
        dsn=dsn,
        options="-c statement_timeout=10000 -c lock_timeout=2000",
    )


def load_kappas(raw: str | None) -> dict[str, float]:
    kappas = dict(DEFAULT_KAPPAS)
    if raw:
        overrides = json.loads(raw)
        unknown = set(overrides) - set(kappas)
        if unknown:
            raise ValueError(f"unknown kappa keys: {sorted(unknown)}")
        for key, value in overrides.items():
            if not 0.0 <= float(value) <= 2.0:
                raise ValueError(f"kappa {key}={value} outside [0, 2]")
            kappas[key] = float(value)
    return kappas


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Concurrent slate ingestion.")
    parser.add_argument("job", choices=ConcurrentIngestionEngine.JOBS)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--deadline", type=float, default=None, help="seconds to wait for the slate")
    parser.add_argument("--bench-sub-probability", type=float, default=0.35)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    args = build_arg_parser().parse_args(argv)

    fixture_uids = [u.strip() for u in os.environ.get("TARGET_FIXTURE_UIDS", "").split(",") if u.strip()]
    client = VendorClient(
        os.environ["DATA_VENDOR_BASE_URL"],
        os.environ["DATA_VENDOR_API_KEY"],
        TokenBucketRateLimiter(rate_per_sec=float(os.environ.get("DATA_VENDOR_RPS", "5")), burst=10),
    )
    pool = build_pool(os.environ["PROP_ALPHA_DB_DSN"], args.workers)
    try:
        with ConcurrentIngestionEngine(
            client, pool, load_kappas(os.environ.get("PROP_ALPHA_KAPPAS")),
            max_workers=args.workers, bench_sub_probability=args.bench_sub_probability,
        ) as engine:
            report = engine.run_slate(fixture_uids, job=args.job, deadline_s=args.deadline)
    finally:
        client.close()
        pool.closeall()
    for uid, error in report.failed.items():
        logger.error("FAILED %s: %s", uid, error)
    for uid in report.timed_out:
        logger.error("TIMED OUT %s", uid)
    return 0 if report.fully_successful else 1


if __name__ == "__main__":
    sys.exit(main())
