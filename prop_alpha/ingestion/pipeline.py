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
import re
import sys
import threading
import time
import uuid
from datetime import date
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Iterable, Mapping, Sequence, TypeVar
from urllib.parse import urlsplit

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prop_alpha.config import credential
from prop_alpha.ingestion import oddspapi, sportmonks, thestatsapi
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

# Vendor responses retried by VendorClient.get_json, each attempt metered by the rate limiter.
RETRYABLE_HTTP_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
# Longest Retry-After honoured before a fixture is deferred (decision 6, 2026-10-01). Starting value:
# review it with DATA_VENDOR_RPS against the vendor contract.
MAX_RETRY_AFTER_S: Final[float] = 2.0
_RETRY_AFTER_SECONDS: Final[re.Pattern[str]] = re.compile(r"\d+(\.\d+)?")
# Remaining-quota headers sent by the vendors in use (API-Football, The Odds API, generic). The vendor counts
# across processes and days, so these keep a quota intact across runs, which a per-run counter cannot.
QUOTA_REMAINING_HEADERS: Final[tuple[str, ...]] = (
    "x-ratelimit-requests-remaining", "x-requests-remaining", "x-ratelimit-remaining",
)
_NON_NEGATIVE_INT: Final[re.Pattern[str]] = re.compile(r"\d+")


PayloadAdapter = Callable[[Mapping[str, Any]], dict[str, Any]]
# An adapter that needs the fixture's vendor uid as well (TheStatsAPI: the player-stats reply carries no match details, which
# the adapter takes from the listing by uid).
FixtureAdapter = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class QuotaExhaustedError(RuntimeError):
    """The vendor request budget is spent: no further request is sent this run."""


class VendorError(ValueError):
    """The vendor answered 2xx but reported an error in the body (API-Football does this when over quota)."""

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


def _retry_after_seconds(header: str | None) -> float | None:
    """Seconds from a delay-seconds Retry-After header; None when absent or an HTTP-date (not honoured)."""
    if header is None or not _RETRY_AFTER_SECONDS.fullmatch(header.strip()):
        return None
    return float(header.strip())


class VendorClient:
    """HTTP client with one requests.Session per worker thread (Session is not documented as thread-safe).

    Retries happen in get_json, not in the urllib3 adapter, so every attempt takes a rate-limiter token
    and a vendor's Retry-After longer than MAX_RETRY_AFTER_S fails the fixture at once instead of
    holding a worker past its timeout.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_limiter: TokenBucketRateLimiter,
        timeout: float = 5.0,
        max_retries: int = 1,
        max_requests: int | None = None,
        quota_reserve: int = 0,
        *,
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        auth_query_param: str | None = None,
        stats_path: str = "fixtures/{uid}/stats",
        lineups_path: str = "fixtures/{uid}/lineups",
        stats_adapter: PayloadAdapter | None = None,
        lineups_adapter: PayloadAdapter | None = None,
        fixture_adapter: FixtureAdapter | None = None,
        quota_headers: Sequence[str] = QUOTA_REMAINING_HEADERS,
    ) -> None:
        """Every attempt, retries included, spends vendor quota. `max_requests` caps attempts for this
        client's lifetime; `quota_reserve` stops all further requests once the vendor's remaining-quota
        header reports that many or fewer, leaving them for manual use.

        The key is sent as `{auth_header}: {auth_prefix}{api_key}`, or as the query parameter `auth_query_param`
        when that is set (OddsPapi). Errors never quote the request URL, which may carry the key. The adapters are pure functions from the
        vendor's response to the shapes transform.py parses (e.g. ingestion.sportmonks); they run in the
        worker before any connection is taken, and the adapted payload is what raw_payloads stores. `fixture_adapter`,
        when set, replaces `stats_adapter` and also receives the fixture's uid. `quota_headers` names the headers that
        report the vendor's remaining quota (TheStatsAPI's X-RateLimit-Remaining is a per-minute budget, not a quota)."""
        if stats_adapter is not None and fixture_adapter is not None:
            raise ValueError("give stats_adapter or fixture_adapter, not both")
        if "{uid}" not in stats_path or "{uid}" not in lineups_path:
            raise ValueError("stats_path and lineups_path must contain {uid}")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if max_requests is not None and (isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1):
            raise ValueError("max_requests must be a positive integer or None")
        if isinstance(quota_reserve, bool) or not isinstance(quota_reserve, int) or quota_reserve < 0:
            raise ValueError("quota_reserve must be a non-negative integer")
        # The API key travels in a header on every request, so refuse anything but HTTPS.
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("base_url must be an https:// URL with a host; the API key is sent on every request")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_requests = max_requests
        self.quota_reserve = quota_reserve
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.auth_query_param = auth_query_param
        self.stats_path = stats_path
        self.lineups_path = lineups_path
        self.stats_adapter = stats_adapter
        self.lineups_adapter = lineups_adapter
        self.fixture_adapter = fixture_adapter
        self.quota_headers = tuple(quota_headers)
        self.requests_sent = 0
        self._quota_stop: str | None = None
        self._quota_lock = threading.Lock()
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
            if self.auth_query_param is None:
                session.headers.update({self.auth_header: f"{self.auth_prefix}{self.api_key}"})
            # Only failed connects are retried here: they never reach the vendor, so they spend no quota.
            # Read errors and retryable statuses are handled (or raised) by get_json, one token per attempt.
            retry_policy = Retry(
                total=None, connect=self.max_retries, read=0, status=0, other=0, backoff_factor=0.25,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry_policy, pool_maxsize=2)
            session.mount("https://", adapter)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _drop_session(self) -> None:
        """Close and forget this thread's session; the next request opens a fresh one."""
        session = getattr(self._local, "session", None)
        if session is None:
            return
        self._local.session = None
        with self._sessions_lock:
            if session in self._sessions:
                self._sessions.remove(session)
        session.close()

    def _spend_request(self) -> None:
        """Reserve one request of the budget, or raise without sending anything."""
        with self._quota_lock:
            if self._quota_stop is not None:
                raise QuotaExhaustedError(self._quota_stop)
            if self.max_requests is not None and self.requests_sent >= self.max_requests:
                self._quota_stop = f"per-run request cap of {self.max_requests} reached"
                raise QuotaExhaustedError(self._quota_stop)
            self.requests_sent += 1

    def _record_remaining_quota(self, headers: Mapping[str, str]) -> None:
        for name in self.quota_headers:
            value = headers.get(name)
            if value is not None and _NON_NEGATIVE_INT.fullmatch(value.strip()):
                remaining = int(value.strip())
                if remaining <= self.quota_reserve:
                    with self._quota_lock:
                        self._quota_stop = (
                            f"vendor reports {remaining} requests remaining ({name}); reserve is {self.quota_reserve}"
                        )
                    logger.error("[QUOTA] %s; no further requests this run", self._quota_stop)
                return

    def get_json(self, path: str) -> dict[str, Any]:
        body = self.get_json_value(path)
        if not isinstance(body, dict):
            raise ValueError(f"expected a JSON object from {path}")
        return body

    def _get(self, url: str) -> requests.Response:
        params = {self.auth_query_param: self.api_key} if self.auth_query_param is not None else None
        try:
            return self._session().get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            # A failed request may leave the thread's pooled connection broken (2026-10-14/15: after a Wi-Fi dropout the
            # TheStatsAPI backfill failed every request for about 4 hours with "EOF occurred in violation of protocol" while
            # fresh connections worked), so the session is dropped and the next request opens a new one.
            self._drop_session()
            # requests quotes the full URL in its messages; with query authentication that includes the key.
            detail = "details withheld: the URL carries the API key" if params is not None else str(exc)
            raise requests.ConnectionError(f"{type(exc).__name__} ({detail})") from None

    def _scrub(self, text: str) -> str:
        """A response body for an error message: at most 300 characters, never the key."""
        return text[:300].replace(self.api_key, "<key>") if self.api_key else text[:300]

    def get_json_value(self, path: str, empty_on: Mapping[int, str] | None = None) -> Any:
        """Any JSON value (object or list) from `path`; retries, quota and error rules as get_json. `empty_on` maps an
        HTTP status to a text its body must contain for the reply to mean "no data" (None is returned): OddsPapi
        reports "this bookmaker has no fixtures here" as a 400 or a 404 with fixed wording. Any other body with that
        status raises, so a malformed query or a changed API is not mistaken for "no fixtures"."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        failure = "no attempt made"
        for attempt in range(self.max_retries + 1):
            self._spend_request()
            self.rate_limiter.acquire()
            resp = self._get(url)
            self._record_remaining_quota(resp.headers)
            if resp.status_code not in RETRYABLE_HTTP_STATUS:
                marker = (empty_on or {}).get(resp.status_code)
                if marker is not None and marker in resp.text:
                    logger.info("%s: HTTP %d treated as no data: %s", path, resp.status_code, self._scrub(resp.text))
                    return None
                if resp.status_code >= 400:
                    raise requests.HTTPError(f"{path}: HTTP {resp.status_code}: {self._scrub(resp.text)}")
                body = resp.json()
                if isinstance(body, dict):
                    errors = body.get("errors")
                    if errors:
                        raise VendorError(f"{path}: vendor reported errors: {errors}")
                    rate_limit = body.get("rate_limit")  # Sportmonks reports its quota in the body, per entity
                    if isinstance(rate_limit, Mapping) and isinstance(rate_limit.get("remaining"), int):
                        self._record_remaining_quota({"x-ratelimit-remaining": str(rate_limit["remaining"])})
                return body
            failure = f"HTTP {resp.status_code}"
            delay = 0.25 * (2 ** attempt)
            retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
            if retry_after is not None:
                if retry_after > MAX_RETRY_AFTER_S:
                    raise requests.HTTPError(
                        f"{path}: vendor asked to wait {retry_after:g}s (> {MAX_RETRY_AFTER_S:g}s); fixture deferred"
                    )
                delay = retry_after
            logger.warning("%s: attempt %d/%d got %s", path, attempt + 1, self.max_retries + 1, failure)
            if attempt < self.max_retries:
                time.sleep(delay)
        raise requests.HTTPError(f"{path}: failed after {self.max_retries + 1} attempts ({failure})")

    def get_fixture_feed(self, fixture_vendor_uid: str) -> dict[str, Any]:
        body = self.get_json(self.stats_path.format(uid=fixture_vendor_uid))
        if self.fixture_adapter is not None:
            return self.fixture_adapter(fixture_vendor_uid, body)
        return self.stats_adapter(body) if self.stats_adapter is not None else body

    def get_lineups(self, fixture_vendor_uid: str) -> dict[str, Any]:
        body = self.get_json(self.lineups_path.format(uid=fixture_vendor_uid))
        return self.lineups_adapter(body) if self.lineups_adapter is not None else body

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
        """True only if at least one fixture ran, none failed or timed out, and the merge (if any) neither
        failed nor rejected rows. An empty slate did no work, and a rejected row is data that never landed."""
        rejected = self.merge is not None and self.merge.rows_rejected > 0
        return bool(self.results) and not self.failed and not self.timed_out and self.merge_error is None and not rejected


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
        player_sub_probabilities: Mapping[str, float] | None = None,
    ) -> None:
        """`player_sub_probabilities` maps vendor player uid to a fitted bench sub probability
        (fitting.bench_sub_probability); other bench players get `bench_sub_probability`."""
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
        self.player_sub_probabilities = dict(player_sub_probabilities or {})
        self._slate_lock = threading.Lock()
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

        One slate at a time per engine: the pool holds max_workers + 1 connections, so a second
        concurrent slate would exhaust it and fail its merge with PoolError. It raises RuntimeError instead.
        """
        if self._closed:
            raise RuntimeError("engine is closed")
        if job not in self.JOBS:
            raise ValueError(f"job must be one of {self.JOBS}")
        if not self._slate_lock.acquire(blocking=False):
            raise RuntimeError("run_slate is already running on this engine; use one engine per concurrent slate")
        try:
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
        finally:
            self._slate_lock.release()

    def merge_staging(self, max_rounds: int = 50) -> MergeResult:
        """Drain staging in bounded transactions: each call of the merge function claims at most its batch
        limit (200 batches by default), so statement_timeout bounds one round, not the whole backlog.
        Loops until a round merges nothing; raises RuntimeError if the backlog outlasts `max_rounds`."""
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")

        def merge(cur: Any) -> MergeResult:
            cur.execute(MERGE_SQL)
            fixtures, rows, rejected = cur.fetchone()
            return MergeResult(int(fixtures), int(rows), int(rejected))

        total = MergeResult(0, 0, 0)
        for _ in range(max_rounds):
            result, _ = self._with_transaction(merge)
            if result == MergeResult(0, 0, 0):
                break
            total = MergeResult(
                total.fixtures_merged + result.fixtures_merged,
                total.player_rows_merged + result.player_rows_merged,
                total.rows_rejected + result.rows_rejected,
            )
        else:
            raise RuntimeError(f"staging backlog not drained after {max_rounds} merge rounds")
        if total.rows_rejected:
            logger.warning("merge rejected %d staging rows; see prop_alpha.staging_rejects", total.rows_rejected)
        return total

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
        entries = parse_lineup_payload(payload, self.bench_sub_probability, self.player_sub_probabilities)
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


VENDORS: Final[tuple[str, ...]] = ("sportmonks", "generic")


def build_vendor_client(environ: Mapping[str, str]) -> VendorClient:
    """DATA_VENDOR=sportmonks (the default) reads SPORTMONKS_API_TOKEN from the environment or `.env`;
    DATA_VENDOR=generic uses DATA_VENDOR_BASE_URL and DATA_VENDOR_API_KEY with a Bearer header and payloads
    already in transform.py's shape. Quota: DATA_VENDOR_MAX_REQUESTS (per run) and DATA_VENDOR_QUOTA_RESERVE."""
    vendor = environ.get("DATA_VENDOR", "sportmonks")
    if vendor not in VENDORS:
        raise ValueError(f"DATA_VENDOR must be one of {VENDORS}, got {vendor!r}")
    limiter = TokenBucketRateLimiter(rate_per_sec=float(environ.get("DATA_VENDOR_RPS", "5")), burst=10)
    max_requests = int(environ["DATA_VENDOR_MAX_REQUESTS"]) if environ.get("DATA_VENDOR_MAX_REQUESTS") else None
    quota_reserve = int(environ.get("DATA_VENDOR_QUOTA_RESERVE", "0"))
    if vendor == "sportmonks":
        return VendorClient(
            sportmonks.BASE_URL, credential("SPORTMONKS_API_TOKEN", environ), limiter,
            max_requests=max_requests, quota_reserve=quota_reserve,
            auth_header="Authorization", auth_prefix="",
            stats_path=sportmonks.STATS_PATH, lineups_path=sportmonks.LINEUPS_PATH,
            stats_adapter=sportmonks.fixture_payload, lineups_adapter=sportmonks.lineup_payload,
        )
    return VendorClient(
        environ["DATA_VENDOR_BASE_URL"], environ["DATA_VENDOR_API_KEY"], limiter,
        max_requests=max_requests, quota_reserve=quota_reserve,
    )


# TheStatsAPI's trial allows 12 requests a minute (the paid Starter plan 120): the default stays under the trial's.
THESTATSAPI_RPS: Final[float] = 0.19


def build_thestatsapi_client(environ: Mapping[str, str], matches: Mapping[str, Mapping[str, Any]] | None = None) -> VendorClient:
    """TheStatsAPI client: key from THESTATSAPI_KEY (environment or `.env`) as a Bearer token, at most THESTATSAPI_RPS
    requests a second (THESTATSAPI_RPS in the environment overrides it), the monthly quota read from
    X-Monthly-Quota-Remaining, DATA_VENDOR_MAX_REQUESTS and DATA_VENDOR_QUOTA_RESERVE as for any vendor. With `matches`
    (match uid -> its listing row), get_fixture_feed returns transform.py's payload (thestatsapi.fixture_payload); a uid
    not in it is refused, since the player-stats reply alone has no teams, kickoff or score."""
    def adapt(uid: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if matches is None or uid not in matches:
            raise ValueError(f"match {uid}: not in the listing given to the client")
        return thestatsapi.fixture_payload(matches[uid], body)

    max_requests = int(environ["DATA_VENDOR_MAX_REQUESTS"]) if environ.get("DATA_VENDOR_MAX_REQUESTS") else None
    return VendorClient(
        thestatsapi.BASE_URL, credential(thestatsapi.KEY_NAME, environ),
        TokenBucketRateLimiter(rate_per_sec=float(environ.get("THESTATSAPI_RPS", THESTATSAPI_RPS)), burst=1),
        timeout=30.0, max_requests=max_requests, quota_reserve=int(environ.get("DATA_VENDOR_QUOTA_RESERVE", "0")),
        auth_header="Authorization", auth_prefix="Bearer ",
        stats_path=thestatsapi.PLAYER_STATS_PATH, lineups_path=thestatsapi.LINEUPS_PATH,
        fixture_adapter=adapt if matches is not None else None, lineups_adapter=thestatsapi.lineup_payload,
        quota_headers=thestatsapi.QUOTA_HEADERS,
    )


# More pages than this for one competition and one window means something is wrong: refused rather than half-listed.
MAX_TSA_PAGES: Final[int] = 30


def list_matches(client: Any, competitions: Mapping[str, str], date_from: date, date_to: date,
                 status: str) -> list[Mapping[str, Any]]:
    """Every TheStatsAPI match of `competitions` with `status` between two dates (inclusive), soonest first; one request
    per competition and page of 100. A listing longer than MAX_TSA_PAGES pages raises."""
    rows: list[Mapping[str, Any]] = []
    for competition in sorted(competitions):
        page, pages = 1, 1
        while page <= pages:
            if page > MAX_TSA_PAGES:
                raise ValueError(f"{competitions[competition]}: match listing has more than {MAX_TSA_PAGES} pages")
            found, pages = thestatsapi.match_rows(client.get_json(thestatsapi.matches_path(
                competition, f"{date_from:%Y-%m-%d}", f"{date_to:%Y-%m-%d}", page, status=status)))
            rows += found
            page += 1
    return sorted(rows, key=lambda m: (str(m.get("utc_date")), str(m.get("id"))))


def build_odds_client(environ: Mapping[str, str]) -> VendorClient:
    """OddsPapi client: key from ODDSPAPI_KEY (environment or `.env`) as the apiKey query parameter, at most one
    request per second (the vendor's cooldown), no automatic retries (each attempt spends the 250-request free
    quota), and at most ODDS_MAX_REQUESTS (default 8) requests per run."""
    return VendorClient(
        oddspapi.BASE_URL, credential("ODDSPAPI_KEY", environ), TokenBucketRateLimiter(rate_per_sec=0.9, burst=1),
        timeout=60.0, max_retries=0, max_requests=int(environ.get("ODDS_MAX_REQUESTS", "8")),
        auth_query_param=oddspapi.AUTH_QUERY_PARAM,
    )


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
    client = build_vendor_client(os.environ)
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
    if not report.results and not report.timed_out:
        logger.error("NO WORK: TARGET_FIXTURE_UIDS named no fixtures")
    if report.merge is not None and report.merge.rows_rejected:
        logger.error("REJECTED %d staged rows; see prop_alpha.staging_rejects", report.merge.rows_rejected)
    return 0 if report.fully_successful else 1


if __name__ == "__main__":
    sys.exit(main())
