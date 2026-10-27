from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pg_errors = pytest.importorskip("psycopg2.errors")

from conftest import make_fixture_payload, make_lineup_payload  # noqa: E402
import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402

from prop_alpha.ingestion.pipeline import (  # noqa: E402
    MAX_RETRY_AFTER_S,
    ConcurrentIngestionEngine,
    FixtureResult,
    MergeResult,
    QuotaExhaustedError,
    SlateReport,
    TokenBucketRateLimiter,
    VendorClient,
    VendorError,
    build_arg_parser,
)


# ---------------- in-memory fakes ----------------
class FakeDatabase:
    """Committed state plus scripted failures: (statement substring, exception) consumed on first match."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.raw_payloads: dict[str, str] = {}
        self.raw_sources: dict[str, str] = {}  # payload digest -> the source it was stored under
        self.staging: list[tuple[Any, ...]] = []
        self.merged_fixtures: set[str] = set()
        self.player_ids: dict[str, int] = {}
        self.lineups: dict[tuple[str, int], tuple[float, float]] = {}
        self.failures: list[tuple[str, BaseException]] = []
        self.merge_calls = 0

    def maybe_fail(self, sql: str) -> None:
        with self.lock:
            for index, (fragment, exc) in enumerate(self.failures):
                if fragment in sql:
                    del self.failures[index]
                    raise exc


class FakeConnectionInfo:
    encoding = "UTF8"


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._mogrified: list[tuple[Any, ...]] = []
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def mogrify(self, template: bytes | str, args: tuple[Any, ...]) -> bytes:
        self._mogrified.append(tuple(args))
        return repr(tuple(args)).encode("utf-8")

    def execute(self, sql: bytes | str, params: tuple[Any, ...] | None = None) -> None:
        text = sql.decode("utf-8") if isinstance(sql, bytes) else sql
        db = self.connection.db
        db.maybe_fail(text)
        rows, self._mogrified = self._mogrified, []
        ops = self.connection.pending
        if "raw_payloads" in text:
            assert params is not None
            ops.append(("raw", params))
        elif "staging_player_feed" in text and "INSERT" in text:
            ops.extend(("stage", row) for row in rows)
        elif "merge_staging_player_feed" in text:
            with db.lock:
                db.merge_calls += 1
                claimed = list(db.staging)
            fixtures = {row[1] for row in claimed}
            players = sum(1 for row in claimed if row[11] is not None)
            ops.append(("merge", tuple(claimed)))
            self._result = [(len(fixtures), players, 0)]
        elif "INSERT INTO prop_alpha.lineup_expectations" in text:
            listed: list[tuple[Any, ...]] = []
            for fixture_uid, player_uid, p_start, p_sub in rows:
                player_id = db.player_ids.get(player_uid)
                if player_id is not None:
                    ops.append(("lineup", (fixture_uid, player_id, p_start, p_sub)))
                    listed.append((player_id,))
            self._result = listed
        elif "UPDATE prop_alpha.lineup_expectations" in text:
            assert params is not None
            ops.append(("exclude", params))
        else:
            raise AssertionError(f"unexpected SQL: {text[:80]}")

    def fetchone(self) -> tuple[Any, ...]:
        return self._result[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        result, self._result = self._result, []
        return result


class FakeConnection:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db
        self.closed = 0
        self.info = FakeConnectionInfo()
        self.encoding = "UTF8"
        self.pending: list[tuple[str, Any]] = []

    def __enter__(self) -> FakeConnection:
        self.pending = []
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._commit()
        self.pending = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def _commit(self) -> None:
        db = self.db
        with db.lock:
            for kind, value in self.pending:
                if kind == "raw":
                    db.raw_payloads.setdefault(value[3], value[1])
                    db.raw_sources.setdefault(value[3], value[0])
                elif kind == "stage":
                    db.staging.append(value)
                elif kind == "merge":
                    for row in value:
                        db.staging.remove(row)
                        db.merged_fixtures.add(row[1])
                elif kind == "lineup":
                    fixture_uid, player_id, p_start, p_sub = value
                    db.lineups[(fixture_uid, player_id)] = (p_start, p_sub)
                elif kind == "exclude":
                    fixture_uid, listed = value
                    for key in list(db.lineups):
                        if key[0] == fixture_uid and key[1] not in listed:
                            db.lineups[key] = (0.0, 0.0)


class FakePool:
    def __init__(self, db: FakeDatabase, maxconn: int) -> None:
        self.db = db
        self.maxconn = maxconn
        self.lock = threading.Lock()
        self.holders: dict[int, int] = {}
        self.in_use = 0
        self.peak = 0
        self.gets = 0
        self.puts = 0

    def getconn(self) -> FakeConnection:
        with self.lock:
            if self.in_use >= self.maxconn:
                raise AssertionError("pool exhausted")
            self.gets += 1
            self.in_use += 1
            self.peak = max(self.peak, self.in_use)
            ident = threading.get_ident()
            self.holders[ident] = self.holders.get(ident, 0) + 1
        return FakeConnection(self.db)

    def putconn(self, conn: FakeConnection, close: bool = False) -> None:
        with self.lock:
            self.puts += 1
            self.in_use -= 1
            ident = threading.get_ident()
            self.holders[ident] -= 1
            if not self.holders[ident]:
                del self.holders[ident]

    def holds_connection(self) -> bool:
        with self.lock:
            return threading.get_ident() in self.holders


class FakeVendor:
    def __init__(self, pool: FakePool, payloads: dict[str, dict[str, Any]]) -> None:
        self.pool = pool
        self.payloads = payloads
        self.source = "sportmonks"  # VendorClient.source: what each raw payload is stored under
        self.lineups: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, BaseException] = {}
        self.before_fetch: Callable[[str], object] | None = None  # its return value is ignored
        self.violations: list[str] = []
        self.fetch_threads: set[int] = set()

    def _fetch(self, uid: str, source: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if self.pool.holds_connection():
            self.violations.append(uid)
        self.fetch_threads.add(threading.get_ident())
        if self.before_fetch is not None:
            self.before_fetch(uid)
        if uid in self.errors:
            raise self.errors[uid]
        return source[uid]

    def get_fixture_feed(self, uid: str) -> dict[str, Any]:
        return self._fetch(uid, self.payloads)

    def get_lineups(self, uid: str) -> dict[str, Any]:
        return self._fetch(uid, self.lineups)


def _setup(n_fixtures: int, workers: int = 4) -> tuple[FakeDatabase, FakePool, FakeVendor, list[str]]:
    db = FakeDatabase()
    pool = FakePool(db, workers + 1)
    uids = [f"FX-{i}" for i in range(n_fixtures)]
    vendor = FakeVendor(pool, {uid: make_fixture_payload(uid) for uid in uids})
    return db, pool, vendor, uids


def _engine(vendor: FakeVendor, pool: FakePool, workers: int = 4) -> ConcurrentIngestionEngine:
    return ConcurrentIngestionEngine(vendor, pool, max_workers=workers, retry_base_delay_s=0.001)  # type: ignore[arg-type]


# ---------------- tests ----------------
def test_slate_is_processed_concurrently() -> None:
    db, pool, vendor, uids = _setup(4)
    barrier = threading.Barrier(4, timeout=10.0)
    vendor.before_fetch = lambda uid: barrier.wait()
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats")
    assert report.succeeded == sorted(uids)
    assert report.fully_successful
    assert len(vendor.fetch_threads) == 4
    assert pool.gets == pool.puts and pool.in_use == 0
    # D2: one round drains the staged batches, a second round returns all zeros and ends the loop.
    assert db.merge_calls == 2 and db.merged_fixtures == set(uids) and not db.staging
    assert report.merge is not None and report.merge.fixtures_merged == 4


def test_no_connection_is_held_during_network_io() -> None:
    db, pool, vendor, uids = _setup(8)
    vendor.before_fetch = lambda uid: time.sleep(0.01)
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids + uids[:2], job="stats")
    assert vendor.violations == []
    assert len(report.results) == 8
    assert pool.peak <= 4


def test_transient_database_error_is_retried() -> None:
    db, pool, vendor, uids = _setup(1)
    db.failures.append(("staging_player_feed", pg_errors.DeadlockDetected("deadlock detected")))
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats", merge=False)
    result = report.results["FX-0"]
    assert result.ok and result.db_attempts == 2
    assert len(db.staging) == 1 + 4
    assert len(db.raw_payloads) == 1


def test_each_raw_payload_is_stored_under_its_clients_data_source() -> None:
    """2026-10-27: raw_payloads.source names the vendor (VendorClient.source), so the lockbox capture can refuse outcomes
    from any other source; it used to be the fixed label 'vendor_fixture_stats'."""
    db, pool, vendor, uids = _setup(2)
    with _engine(vendor, pool) as engine:
        assert engine.run_slate(uids, job="stats", merge=False).fully_successful
    assert len(db.raw_sources) == 2 and set(db.raw_sources.values()) == {"sportmonks"}


def test_a_vendor_client_with_an_unknown_data_source_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown raw payload source 'vendor_fixture_stats'"):
        VendorClient("https://example.test", "k", TokenBucketRateLimiter(100.0, 10), source="vendor_fixture_stats")


def test_statement_timeout_fails_closed_without_retry(caplog: pytest.LogCaptureFixture) -> None:
    db, pool, vendor, uids = _setup(1)
    db.failures.append(("staging_player_feed", pg_errors.QueryCanceled("canceling statement due to statement timeout")))
    with caplog.at_level(logging.ERROR, logger="prop_alpha.ingestion"), _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats")
    assert not report.results["FX-0"].ok
    assert "QueryCanceled" in report.failed["FX-0"]
    assert pool.gets == 1 and pool.puts == 1
    assert "[STATEMENT_TIMEOUT]" in caplog.text
    assert db.staging == [] and db.raw_payloads == {}
    assert db.merge_calls == 0


def test_retries_exhausted_marks_fixture_failed() -> None:
    db, pool, vendor, uids = _setup(1)
    db.failures.extend(
        ("raw_payloads", pg_errors.LockNotAvailable("lock timeout")) for _ in range(3)
    )
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats")
    assert "LockNotAvailable" in report.failed["FX-0"]
    assert pool.gets == 3 and pool.in_use == 0
    assert not report.fully_successful


def test_one_failing_fixture_does_not_affect_others() -> None:
    db, pool, vendor, uids = _setup(4)
    vendor.errors["FX-1"] = ConnectionError("vendor returned 503")
    vendor.payloads["FX-2"]["players"][0]["stats"]["tackles_won"] = 9
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats")
    assert report.succeeded == ["FX-0", "FX-3"]
    assert set(report.failed) == {"FX-1", "FX-2"}
    assert "tackles_won exceeds tackles_attempted" in report.failed["FX-2"]
    assert db.merged_fixtures == {"FX-0", "FX-3"}
    assert not report.fully_successful


def test_deadline_reports_timed_out_fixtures() -> None:
    db, pool, vendor, uids = _setup(3)
    release = threading.Event()
    vendor.before_fetch = lambda uid: release.wait(10.0) if uid == "FX-2" else None
    engine = _engine(vendor, pool)
    try:
        report = engine.run_slate(uids, job="stats", deadline_s=0.5)
        assert report.timed_out == ["FX-2"]
        assert report.succeeded == ["FX-0", "FX-1"]
        assert report.merge is not None and report.merge.fixtures_merged == 2
    finally:
        release.set()
        engine.close()
    assert ("FX-2", ) == tuple({row[1] for row in db.staging})


def test_merge_failure_is_recorded_and_staging_preserved() -> None:
    db, pool, vendor, uids = _setup(2)
    db.failures.append(("merge_staging_player_feed", pg_errors.QueryCanceled("statement timeout")))
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats")
    assert report.merge is None
    assert report.merge_error is not None and "QueryCanceled" in report.merge_error
    assert len(db.staging) == 2 * 5
    assert not report.fully_successful
    assert pool.in_use == 0


def test_empty_slate_is_not_successful() -> None:
    db, pool, vendor, _ = _setup(0)
    with _engine(vendor, pool) as engine:
        report = engine.run_slate([], job="stats")
    assert report.results == {} and report.merge is None
    assert not report.fully_successful


def test_rejected_merge_rows_make_the_slate_unsuccessful() -> None:
    ok = {"FX-0": FixtureResult("FX-0", True, 5, 1, 0.01)}
    assert SlateReport("stats", results=dict(ok), merge=MergeResult(1, 4, 0)).fully_successful
    assert not SlateReport("stats", results=dict(ok), merge=MergeResult(1, 4, 1)).fully_successful


@pytest.mark.parametrize(
    "base_url", ["http://vendor.invalid/api", "ftp://vendor.invalid", "vendor.invalid/api", "https://", ""]
)
def test_vendor_client_refuses_non_https_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        VendorClient(base_url, "test-key", TokenBucketRateLimiter(100.0, 10))


def test_lineup_job_upserts_and_zeroes_absent_players() -> None:
    db, pool, vendor, _ = _setup(0)
    sheet = make_lineup_payload("FX-L", n_bench=2)
    listed = [uid for side in sheet["lineups"].values() for uid in side["starters"] + side["bench"]]
    db.player_ids = {uid: 1000 + i for i, uid in enumerate(listed)}
    db.lineups[("FX-L", 999)] = (0.8, 0.1)
    vendor.lineups["FX-L"] = sheet
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(["FX-L"], job="lineups")
    assert report.fully_successful and report.merge is None
    assert report.results["FX-L"].rows == 26
    assert db.lineups[("FX-L", 999)] == (0.0, 0.0)
    starters = [db.lineups[("FX-L", db.player_ids[uid])] for uid in sheet["lineups"]["T-HOME"]["starters"]]
    assert set(starters) == {(1.0, 0.0)}
    bench_uid = sheet["lineups"]["T-AWAY"]["bench"][0]
    assert db.lineups[("FX-L", db.player_ids[bench_uid])] == (0.0, 0.35)
    assert db.merge_calls == 0


def test_pool_smaller_than_workers_is_rejected() -> None:
    db = FakeDatabase()
    vendor = FakeVendor(FakePool(db, 9), {})
    with pytest.raises(ValueError, match="max_workers \\+ 1"):
        ConcurrentIngestionEngine(vendor, FakePool(db, 8), max_workers=8)  # type: ignore[arg-type]


def test_rate_limiter_is_shared_across_threads() -> None:
    limiter = TokenBucketRateLimiter(rate_per_sec=20.0, burst=1)

    def worker() -> None:
        for _ in range(3):
            limiter.acquire()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started
    assert elapsed >= (12 - 1) / 20.0 * 0.9
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(rate_per_sec=0.0, burst=1)


def test_vendor_client_uses_one_session_per_thread() -> None:
    client = VendorClient("https://vendor.invalid/api", "test-key", TokenBucketRateLimiter(100.0, 10))
    sessions: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        first = client._session()
        assert client._session() is first
        with lock:
            sessions.append(first)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(session) for session in sessions}) == 3
    client.close()
    assert client._sessions == []


def test_cli_arguments_parse() -> None:
    args = build_arg_parser().parse_args(["lineups", "--workers", "6", "--deadline", "45"])
    assert (args.job, args.workers, args.deadline, args.bench_sub_probability) == ("lineups", 6, 45.0, 0.35)
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["odds"])


# ---------------- remediation I2, I3, D2, K2 ----------------
class CountingLimiter(TokenBucketRateLimiter):
    def __init__(self) -> None:
        super().__init__(rate_per_sec=1000.0, burst=1000)
        self.acquired = 0

    def acquire(self) -> None:
        self.acquired += 1


def _response(status: int, body: Any = None, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = repr(body).replace("'", '"').encode("utf-8") if body is not None else b"{}"
    response.headers.update(headers or {})
    response.url = "https://vendor.invalid/api/fixtures/FX-1/stats"
    return response


class ScriptedSession:
    """Stands in for requests.Session: replays responses and counts what reached the vendor."""

    def __init__(self, *responses: requests.Response) -> None:
        self.responses = list(responses)
        self.requests = 0

    def get(self, url: str, timeout: float, params: dict[str, str] | None = None) -> requests.Response:
        self.requests += 1
        return self.responses.pop(0)


def _client(session: ScriptedSession, limiter: CountingLimiter, **kwargs: Any) -> VendorClient:
    client = VendorClient("https://vendor.invalid/api", "test-key", limiter, **{"max_retries": 2, **kwargs})
    client._local.session = session
    return client


def test_vendor_client_retries_once_by_default() -> None:
    """C2: every retry spends vendor quota, so the default is one retry (two attempts), not two."""
    assert VendorClient("https://vendor.invalid/api", "k", TokenBucketRateLimiter(100.0, 10)).max_retries == 1


def test_error_body_with_http_200_is_a_failure_not_data() -> None:
    """C2: API-Football answers HTTP 200 with {"errors": {...}} when the daily quota is spent."""
    session = ScriptedSession(_response(200, {"errors": {"requests": "You have reached the request limit for the day"}}))
    with pytest.raises(VendorError, match="request limit"):
        _client(session, CountingLimiter()).get_json("fixtures/FX-1/stats")
    ok = ScriptedSession(_response(200, {"errors": [], "response": [1]}))
    assert _client(ok, CountingLimiter()).get_json("fixtures/FX-1/stats")["response"] == [1]


def test_vendor_remaining_quota_header_stops_further_requests() -> None:
    """C2: the vendor's own counter spans runs and days; at or below the reserve nothing more is sent."""
    session = ScriptedSession(
        _response(200, {"n": 1}, headers={"x-ratelimit-requests-remaining": "6"}),
        _response(200, {"n": 2}, headers={"x-ratelimit-requests-remaining": "5"}),
        _response(200, {"n": 3}),
    )
    client = _client(session, CountingLimiter(), quota_reserve=5)
    assert client.get_json("a")["n"] == 1
    assert client.get_json("b")["n"] == 2          # this answer is kept; it reported the reserve
    with pytest.raises(QuotaExhaustedError, match="5 requests remaining"):
        client.get_json("c")
    assert session.requests == 2


def test_per_run_request_cap_counts_retries() -> None:
    session = ScriptedSession(_response(503), _response(200, {"n": 1}), _response(200, {"n": 2}))
    client = _client(session, CountingLimiter(), max_requests=2)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(time, "sleep", lambda seconds: None)
        assert client.get_json("a")["n"] == 1      # 503 then 200: two requests
    with pytest.raises(QuotaExhaustedError, match="cap of 2"):
        client.get_json("b")
    assert session.requests == 2 and client.requests_sent == 2
    with pytest.raises(ValueError):
        VendorClient("https://vendor.invalid/api", "k", TokenBucketRateLimiter(100.0, 10), max_requests=0)


def test_every_vendor_attempt_takes_a_rate_limiter_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """I2: 429, 429, 200 is three vendor requests and three tokens (previously one token, retries unmetered)."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    limiter = CountingLimiter()
    session = ScriptedSession(_response(429), _response(429), _response(200, {"ok": 1}))
    assert _client(session, limiter).get_json("fixtures/FX-1/stats") == {"ok": 1}
    assert session.requests == 3 and limiter.acquired == 3
    assert sleeps == [0.25, 0.5]


def test_long_retry_after_fails_fast_instead_of_blocking_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """I2: Retry-After: 4 used to hold one call for 8 s despite timeout=5; now it raises at once."""
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail(f"slept {seconds}s"))
    limiter = CountingLimiter()
    session = ScriptedSession(_response(429, headers={"Retry-After": "4"}))
    started = time.monotonic()
    with pytest.raises(requests.HTTPError, match="asked to wait 4s"):
        _client(session, limiter).get_json("fixtures/FX-1/stats")
    assert time.monotonic() - started < 0.1
    assert MAX_RETRY_AFTER_S == 2.0


def test_short_retry_after_is_honoured_and_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    limiter = CountingLimiter()
    session = ScriptedSession(*(_response(503, headers={"Retry-After": "1"}) for _ in range(3)))
    with pytest.raises(requests.HTTPError, match="failed after 3 attempts"):
        _client(session, limiter).get_json("fixtures/FX-1/stats")
    assert sleeps == [1.0, 1.0] and limiter.acquired == 3
    not_found = ScriptedSession(_response(404))
    with pytest.raises(requests.HTTPError):
        _client(not_found, CountingLimiter()).get_json("fixtures/FX-1/stats")
    assert not_found.requests == 1


def test_adapter_retries_only_connects_which_never_reach_the_vendor() -> None:
    client = VendorClient("https://vendor.invalid/api", "test-key", TokenBucketRateLimiter(100.0, 10))
    adapter = client._session().get_adapter("https://vendor.invalid")
    assert isinstance(adapter, HTTPAdapter)
    retries = adapter.max_retries
    assert (retries.total, retries.connect, retries.read, retries.status) == (None, client.max_retries, 0, 0)
    assert client.max_retries == 1
    client.close()


def test_second_concurrent_slate_on_one_engine_is_refused() -> None:
    """I3: the pool has one spare connection; a second concurrent slate fails loudly instead of PoolError."""
    db, pool, vendor, uids = _setup(1)
    entered, release = threading.Event(), threading.Event()

    def block(uid: str) -> None:
        entered.set()
        release.wait(10.0)

    vendor.before_fetch = block
    engine = _engine(vendor, pool)
    first: list[Any] = []
    runner = threading.Thread(target=lambda: first.append(engine.run_slate(uids, job="stats")))
    try:
        runner.start()
        assert entered.wait(10.0)
        with pytest.raises(RuntimeError, match="already running"):
            engine.run_slate(uids, job="stats")
    finally:
        release.set()
        runner.join(10.0)
    assert first and first[0].fully_successful
    second = engine.run_slate([], job="stats")
    assert second.results == {}
    engine.close()


def test_merge_loop_sums_rounds_and_raises_when_the_backlog_outlasts_it() -> None:
    """D2: rounds are summed until one merges nothing; a backlog that never empties raises."""
    db, pool, vendor, uids = _setup(2)
    with _engine(vendor, pool) as engine:
        engine.run_slate(uids, job="stats", merge=False)
        assert len(db.staging) == 2 * 5
        with pytest.raises(RuntimeError, match="not drained after 1 merge rounds"):
            engine.merge_staging(max_rounds=1)
        assert not db.staging and db.merge_calls == 1
        assert engine.merge_staging().fixtures_merged == 0
        with pytest.raises(ValueError):
            engine.merge_staging(max_rounds=0)


def test_lineup_job_uses_per_player_bench_probabilities() -> None:
    """K2: a fitted per-player probability replaces the league-level 0.35 for that player only."""
    db, pool, vendor, _ = _setup(0)
    sheet = make_lineup_payload("FX-L", n_bench=2)
    listed = [uid for side in sheet["lineups"].values() for uid in side["starters"] + side["bench"]]
    db.player_ids = {uid: 1000 + i for i, uid in enumerate(listed)}
    vendor.lineups["FX-L"] = sheet
    regular, rotation = sheet["lineups"]["T-AWAY"]["bench"]
    engine = ConcurrentIngestionEngine(
        vendor, pool, max_workers=4, retry_base_delay_s=0.001,  # type: ignore[arg-type]
        player_sub_probabilities={regular: 0.8},
    )
    with engine:
        assert engine.run_slate(["FX-L"], job="lineups").fully_successful
    assert db.lineups[("FX-L", db.player_ids[regular])] == (0.0, 0.8)
    assert db.lineups[("FX-L", db.player_ids[rotation])] == (0.0, 0.35)
