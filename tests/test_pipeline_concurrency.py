from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402

from conftest import make_fixture_payload, make_lineup_payload  # noqa: E402
from prop_alpha.ingestion.pipeline import (  # noqa: E402
    ConcurrentIngestionEngine,
    TokenBucketRateLimiter,
    VendorClient,
    build_arg_parser,
)


# ---------------- in-memory fakes ----------------
class FakeDatabase:
    """Committed state plus scripted failures: (statement substring, exception) consumed on first match."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.raw_payloads: dict[str, str] = {}
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
        self.lineups: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, BaseException] = {}
        self.before_fetch: Callable[[str], None] | None = None
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
    assert db.merge_calls == 1 and db.merged_fixtures == set(uids) and not db.staging
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
    db.failures.append(("staging_player_feed", psycopg2.errors.DeadlockDetected("deadlock detected")))
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats", merge=False)
    result = report.results["FX-0"]
    assert result.ok and result.db_attempts == 2
    assert len(db.staging) == 1 + 4
    assert len(db.raw_payloads) == 1


def test_statement_timeout_fails_closed_without_retry(caplog: pytest.LogCaptureFixture) -> None:
    db, pool, vendor, uids = _setup(1)
    db.failures.append(("staging_player_feed", psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")))
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
        ("raw_payloads", psycopg2.errors.LockNotAvailable("lock timeout")) for _ in range(3)
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
    db.failures.append(("merge_staging_player_feed", psycopg2.errors.QueryCanceled("statement timeout")))
    with _engine(vendor, pool) as engine:
        report = engine.run_slate(uids, job="stats")
    assert report.merge is None
    assert report.merge_error is not None and "QueryCanceled" in report.merge_error
    assert len(db.staging) == 2 * 5
    assert not report.fully_successful
    assert pool.in_use == 0


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
