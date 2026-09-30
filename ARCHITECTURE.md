# ARCHITECTURE.md — prop_alpha v3.1 Architectural Specification Manual

| Field | Value |
|---|---|
| Companion documents | `prop_alpha_PRD_v3.1.md` (requirements, gates) |
| Code baseline | `prop_alpha_v3.1_code.zip`: 94 tests passing |
| Schema baseline | `schema.sql`, `migrations/002_staging_and_indexes.sql`: **not yet executed against PostgreSQL** |
| Tags | **[T]** implemented and tested · **[U]** implemented, unexecuted against PostgreSQL · **[N]** not built |

**Scope correction to the commissioning brief.** Three components the brief assumes do not exist in v3.1. They are drawn and specified below as **[N]** so that no reader mistakes an interface for an implementation:

1. **Sportsbook odds ingestion.** `ConcurrentIngestionEngine` ingests the *statistics vendor* feed (`/fixtures/{uid}/stats`, `/fixtures/{uid}/lineups`). No component currently polls sportsbook APIs or writes `prop_alpha.odds_lines`.
2. **`MarketScanner`.** This class was removed in v3 and replaced by `InstitutionalAssetManager` in the same module, `prop_alpha/scanner/market_scanner.py`.
3. **Execution orders.** The terminal output is a `StakingDirective` inside an `AllocationReport`. No component places bets.

---

## SECTION 1: MODULAR TOPOLOGY & HARDWARE COMPONENT MATRIX

### 1.1 High-Level System Diagram

```
                                   EXTERNAL BOUNDARY
 ┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
 │ Statistics vendor HTTP API           │        │ Sportsbook HTTP APIs (N books)       │
 │  GET /fixtures/{uid}/stats           │        │  player O/U + yes/no card prices     │
 │  GET /fixtures/{uid}/lineups         │        │                                      │
 └──────────────────┬───────────────────┘        └──────────────────┬───────────────────┘
                    │ HTTPS, JSON                                   │ HTTPS, JSON
 ═══════════════════╪═══════════════════ PROCESS: ingestion ════════╪═════════════════════
                    │                                               │
 ┌──────────────────▼─────────────────────────────────┐   ┌────────▼───────────────────────┐
 │ VendorClient                                        │   │ [N] OddsPoller                 │
 │  threading.local() → one requests.Session / thread  │   │  must emit LiveOddsQuote and   │
 │  urllib3 Retry(total=2, backoff 0.25, 429/5xx)      │   │  INSERT odds_lines (UNIQUE     │
 │  TokenBucketRateLimiter (shared, DATA_VENDOR_RPS)   │   │  NULLS NOT DISTINCT snapshot)  │
 └──────────────────┬─────────────────────────────────┘   └────────┬───────────────────────┘
                    │ dict payload                                  │
 ┌──────────────────▼─────────────────────────────────────────────┐ │
 │ ConcurrentIngestionEngine  (ThreadPoolExecutor "ingest-*", W)  │ │
 │                                                                │ │
 │  worker k:  get_* ──► parse_fixture_payload / parse_lineup_    │ │
 │             payload ──► build_staging_rows      NO DB HANDLE   │ │
 │                           │                                    │ │
 │                           ▼                                    │ │
 │             _with_transaction(fn)  ◄── ONLY pool.getconn() site│ │
 │               ThreadedConnectionPool(maxconn = W + 1)          │ │
 │               options: statement_timeout=10000, lock_timeout=2000
 └───────┬────────────────────────────────┬───────────────────────┘ │
         │ job="stats"                    │ job="lineups"           │
 ════════╪════════════════════════════════╪═════ PostgreSQL 15+ ════╪═════════════════════
         │                                │                         │
 ┌───────▼──────────────────────┐   ┌─────▼──────────────────────┐  │
 │ raw_payloads (LOGGED, JSONB, │   │ lineup_expectations         │  │
 │   UNIQUE payload_sha256)     │   │  LINEUP_UPSERT_SQL          │  │
 │ staging_player_feed          │   │  LINEUP_EXCLUDE_SQL         │  │
 │   (UNLOGGED, no keys/index)  │   │  (bypasses staging)         │  │
 └───────┬──────────────────────┘   └─────┬──────────────────────┘  │
         │ merge_staging() on the spare   │                         │
         │ connection, after the slate    │                         │
 ┌───────▼──────────────────────────────────────────────┐           │
 │ prop_alpha.merge_staging_player_feed()               │           │
 │  pg_advisory_xact_lock ─► DELETE…RETURNING → claimed_ │           │
 │  feed (TEMP, ON COMMIT DROP) ─► staging_rejects       │           │
 │  ─► upsert referees, fixtures, players,               │           │
 │     player_match_stats (DISTINCT ON … ORDER BY key)   │           │
 └───────┬──────────────────────────────────────────────┘           │
         ▼                                                          ▼
 ┌────────────────────────────────────────────────────────────────────────────────────┐
 │ CORE RELATIONAL LAYER (LOGGED): leagues, teams, players, referees, fixtures,        │
 │ player_match_stats, lineup_expectations, odds_lines, model_projections, bets        │
 └──────┬──────────────────────────────────────────────────────────┬──────────────────┘
        │ nightly: referee_job (same advisory lock)                 │
        │ offline: calibration.fitting (κ, β₁, β₂, k)               │
 ═══════╪══════════════════════ PROCESS: pricing & allocation ══════╪═════════════════════
 ┌──────▼────────────────────────────────────┐   ┌──────────────────▼──────────────────┐
 │ [N] ProjectionRefresh                      │   │ [N] quote loader: latest odds_lines │
 │  reads player_match_stats, lineup_         │   │  per (fixture, book, market,        │
 │  expectations, referees → QuantEngine      │   │  player, line) → LiveOddsQuote      │
 │  → PropProjection → model_projections      │   └──────────────────┬──────────────────┘
 └──────┬────────────────────────────────────┘                       │
        │ Mapping[(fixture_id, player_id, market_type), PropProjection]
        ▼                                                             ▼
 ┌────────────────────────────────────────────────────────────────────────────────────┐
 │ InstitutionalAssetManager.allocate(projections, quotes, now)                        │
 │  staleness ─► _candidates (EV > α, best price, degenerate guard) ─► outcome_model    │
 │  ─► return_moments (μ, Σ) ─► solve_mean_variance (SLSQP) ─► fixture/slate caps       │
 │  ─► stake = min(f·(bankroll − open_exposure), max_stake) ─► AllocationReport          │
 └──────┬─────────────────────────────────────────────────────────────────────────────┘
        ▼
   AllocationReport{directives, skipped_directives, skipped, allocations, stale_quotes}
        │
        ▼  [N] bet-ledger writer (bets.is_paper = TRUE) · [N] execution component
```

**Process boundaries.** Ingestion and allocation are separate processes that share only PostgreSQL. No Python object crosses between them, so either can be restarted independently.

**Failure containment.** The ingestion process is failure-contained per fixture (`_run_isolated`). The allocation process is failure-contained per player-market key (`ProjectionError` is caught per key in `allocate`).

### 1.2 System Infrastructure Selection Matrix

| Layer | Selection | Configuration in v3.1 | Rationale |
|---|---|---|---|
| RDBMS | PostgreSQL ≥ 15 | `schema.sql` header; `CREATE SCHEMA prop_alpha` | See the feature analysis below. |
| DB driver | `psycopg2` (`psycopg2-binary` in `requirements.txt`) | `psycopg2.pool.ThreadedConnectionPool`, `psycopg2.extras.execute_values`, `psycopg2.errors.QueryCanceled` | Thread-safe pool; multi-row `VALUES` batching with `fetch=True` for `RETURNING`; SQLSTATE-typed exceptions for fail-closed routing. |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` | `max_workers = W` (default 8), `thread_name_prefix="ingest"`, owned for the engine's lifetime; `close()` calls `shutdown(wait=True, cancel_futures=True)`; `run_slate` uses `wait(futures, timeout=deadline_s)` | The workload is I/O-bound: HTTP latency dominates, and psycopg2 releases the GIL during network waits. Threads avoid process-pool serialisation of payloads and share one pool and one rate limiter directly. |
| HTTP | `requests` + `urllib3.util.retry.Retry` | Per-thread `Session`; `timeout=5.0` s; `Retry(total=2, backoff_factor=0.25, status_forcelist=(429,500,502,503,504), allowed_methods={"GET"}, respect_retry_after_header=True)`; `HTTPAdapter(pool_maxsize=2)` | `Session` is not documented as thread-safe. One session per thread keeps keep-alive benefits without shared mutable state. |
| Numerics | `numpy` | `outcome_model`, `return_moments` (dense \(\mathbb{R}^{S\times J}\) payoff matrix, eigen-floor via `np.linalg.eigvalsh`), `fit_poisson_glm` (Newton–Raphson) | Vectorised moment computation; closed-form linear algebra. |
| Distributions | `scipy.stats` | `nbinom` (predictive counts), `poisson` (bivariate marginals) | Exact CDF, PMF and SF with stable tail evaluation; `nbinom.sf` gives exact tail buckets. |
| Optimisation | `scipy.optimize` | `minimize(method="SLSQP", ftol=1e-14, maxiter=500)` with analytic gradient; `nnls` for state duel densities | Small (\(J \lesssim 10\)) concave QP with box and budget constraints; SLSQP handles both natively. |

**Why PostgreSQL 15 is the minimum version.** Each feature below is used by the schema:

1. **`UNIQUE NULLS NOT DISTINCT` (added in PostgreSQL 15)** on `odds_lines`:
   `CONSTRAINT uq_odds_snapshot UNIQUE NULLS NOT DISTINCT (fixture_id, sportsbook, market_type, player_id, line_value, captured_at)`.
   - Under the default `NULLS DISTINCT` semantics, two snapshots differing only in a `NULL` `player_id` never conflict, so duplicate captures would accumulate silently.
   - The v3.1 `market_type` domain contains only player markets. However, `player_id` is nullable by design, to admit team-level markets, and the constraint makes snapshot idempotency hold for them from day one.
   - It also makes a future `INSERT … ON CONFLICT ON CONSTRAINT uq_odds_snapshot DO NOTHING` in the [N] `OddsPoller` correct for every row.
2. **`UNLOGGED` tables** (`staging_player_feed`, §3.1).
3. **`pg_advisory_xact_lock`**, with automatic release at transaction end (§3.2).
4. **`DELETE … RETURNING` inside a data-modifying CTE** (`WITH drained AS (DELETE … RETURNING *) INSERT …`).
5. **`DISTINCT ON`** combined with `INSERT … ON CONFLICT DO UPDATE`, target-table aliasing (`AS fx`), `LEFT JOIN LATERAL`, and aggregate `FILTER` clauses.
6. **`CREATE INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY`** for online index changes (`migrations/002`).
7. **Table storage parameter `fillfactor = 90`** on `player_match_stats` and `lineup_expectations`. It leaves in-page free space so that upserts which change no indexed column can be HOT (heap-only tuple) updates.

**Thread-pool sizing.** Let \(N\) be fixtures per slate, \(W\) workers, \(r\) the contracted vendor rate, \(B\) the token-bucket burst (10), and \(\bar L\) the per-request latency. Then

\[
T_{\text{slate}} \gtrsim \max\!\left(\frac{\max(N - B, 0)}{r},\ \left\lceil \frac{N}{W} \right\rceil \bar L\right).
\]

Worked cases, with \(r = 5\) and \(\bar L = 1.8\) s:

| \(N\) | \(W\) | Rate bound | Latency bound | Binding | Sequential v2 |
|---|---|---|---|---|---|
| 12 | 8 | 0.4 s | 3.6 s | latency | 21.6 s |
| 40 | 8 | 6.0 s | 9.0 s | latency | 72.0 s |
| 40 | 16 | 6.0 s | 5.4 s | **rate** | 72.0 s |

At \(N = 40\), raising \(W\) from 8 to 16 makes the rate term bind, and further workers yield nothing. The pool must then be `maxconn = 17`, and the PostgreSQL `max_connections` budget must absorb it. **Size \(W\) as the smallest value for which the latency bound falls below the rate bound**, and renegotiate \(r\) if that is still too slow.

### 1.3 Core Directory Map

```
prop_alpha_v3.1/
├── requirements.txt                     numpy, scipy, requests, psycopg2-binary, pytest
├── schema.sql                           full DDL, PostgreSQL 15+ (fresh install)
├── migrations/
│   └── 002_staging_and_indexes.sql      v2 → v3 delta; transactional part, then CONCURRENTLY index swap
├── prop_alpha/
│   ├── __init__.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── transform.py                 PURE. No I/O, stdlib only.
│   │   └── pipeline.py                  I/O. The only module importing requests and psycopg2.pool.
│   ├── modeling/
│   │   ├── __init__.py
│   │   └── engine.py                    PURE numerics. numpy, scipy.stats. No I/O.
│   ├── scanner/
│   │   ├── __init__.py
│   │   └── market_scanner.py            PURE numerics. numpy, scipy.optimize. Imports modeling.engine.
│   ├── calibration/
│   │   ├── __init__.py
│   │   ├── fitting.py                   PURE estimators. numpy, scipy.optimize.
│   │   └── referee_job.py               Pure core + DB wrapper; psycopg2 imported lazily in run_referee_job.
│   └── evaluation/
│       ├── __init__.py
│       └── backtest.py                  PURE. Imports modeling.engine, scanner.market_scanner.devig_two_way.
└── tests/
    ├── test_core.py                     82 tests: maths, engine hardening, allocator, estimators, transforms
    └── test_pipeline_concurrency.py     12 tests: skipped when psycopg2 is absent
```

**Module boundaries (exported contract → forbidden responsibilities).**

| Module | Exports | Must not |
|---|---|---|
| `ingestion/transform.py` | `STATS_COLUMNS`, `STAGING_COLUMNS` (41; 11 fixture-prefix + 30 player columns), `DEFAULT_KAPPAS`, `padj_scale`, `dribbled_past_rate`, `GoalEvent`, `match_state_minutes`, `FixtureRecord`, `RefereeRecord`, `PlayerMatchRecord` (validating `__post_init__`), `ParsedFixture`, `parse_fixture_payload`, `build_stats_row`, `build_staging_rows`, `LineupEntry`, `parse_lineup_payload` | Perform network or database I/O; read the clock; log. Every function is deterministic in its arguments. |
| `ingestion/pipeline.py` | `TokenBucketRateLimiter`, `VendorClient`, `FixtureResult`, `MergeResult`, `SlateReport`, `ConcurrentIngestionEngine`, `build_pool`, `load_kappas`, `main`; SQL constants `INSERT_RAW_SQL`, `INSERT_STAGING_SQL`, `MERGE_SQL`, `LINEUP_UPSERT_SQL`, `LINEUP_TEMPLATE`, `LINEUP_EXCLUDE_SQL`; `RETRYABLE_DB_ERRORS` | Contain parsing or feature maths (delegated to `transform`); take a connection outside `_with_transaction`. |
| `modeling/engine.py` | `VoidRule`, `RefereeProfile`, `GammaPosterior`, `LineupExpectation`, `SideProbabilities`, `PropProjection`, `QuantEngine`, `ProjectionError`, `MIN_RELATIVE_VARIANCE`, `MAX_RELATIVE_VARIANCE`, `REFEREE_SENSITIVE_MARKETS` | Access the database; return a distribution built from invalid parameters. |
| `scanner/market_scanner.py` | `LiveOddsQuote`, `StakingDirective`, `MarketAllocation`, `AllocationReport`, `Candidate`, `InstitutionalAssetManager`, `outcome_model`, `return_moments`, `solve_mean_variance`, `devig_two_way`, `expected_value`, `kelly_fraction_with_push`, `price_for_ev`, `DEGENERATE_LOSS_PROBABILITY` | Place bets; read quotes from any source other than its `quotes` argument. |
| `calibration/fitting.py` | `GLMResult`, `fit_poisson_glm`, `fit_possession_kappa`, `fit_referee_betas`, `fit_game_state_dispersion`, `fit_state_duel_density` | Write parameters anywhere. Callers persist the results (e.g. `PROP_ALPHA_KAPPAS`). |
| `calibration/referee_job.py` | `FixtureDiscipline`, `RefereeCoefficients`, `compute_referee_coefficients`, `DISCIPLINE_SQL`, `UPDATE_SQL`, `UPDATE_TEMPLATE`, `MERGE_LOCK_KEY`, `run_referee_job`, `main` | Update `referees` without first acquiring `pg_advisory_xact_lock(hashtext(MERGE_LOCK_KEY))`. |
| `evaluation/backtest.py` | `brier_score`, `log_loss`, `CalibrationBin`, `calibration_table`, `closing_line_value`, `PlayerMatchObservation`, `BacktestPrediction`, `walk_forward_over_probability`, `summarize` | Use any observation at or after a prediction's kickoff. |

**Import graph (acyclic).**

\[
\texttt{transform} \leftarrow \texttt{pipeline}, \qquad
\texttt{engine} \leftarrow \texttt{market\_scanner} \leftarrow \texttt{backtest}, \qquad
\texttt{engine} \leftarrow \texttt{backtest}, \qquad
\texttt{fitting}, \ \texttt{referee\_job}: \text{no internal imports}
\]

---

## SECTION 2: CONCURRENT INGESTION BOUNDARIES & IO WORKFLOWS

### 2.1 Non-Blocking I/O Isolation

**Per-fixture task anatomy (`job="stats"`, `ConcurrentIngestionEngine._stats_task`).**

```
 phase   operation                                     DB handle held   typical duration
 ─────   ─────────────────────────────────────────     ──────────────   ────────────────
  P1     TokenBucketRateLimiter.acquire()               no              0 … (N−B)/r
  P2     VendorClient.get_fixture_feed(uid)             no              L̄ (network), ≤ 5 s timeout ×3 tries
  P3     parse_fixture_payload(payload)                 no              < 1 ms
  P4     uid check; build_staging_rows(...); json.dumps; no              < 1 ms
         sha256
  P5     _with_transaction(write):                      YES             few ms
           getconn → BEGIN → INSERT raw_payloads →
           execute_values(INSERT_STAGING_SQL, page_size=1000)
           → COMMIT → putconn
```

**Proof that no pool handle is held during P1–P4.**

1. **Single acquisition site.** `self.pool.getconn()` occurs exactly once in the code base (`pipeline.py`, inside `_with_transaction`). `putconn` occurs exactly once, in the `finally` block of the same loop iteration. Every connection is therefore scoped to one invocation of `fn`.
2. **Three callers.** `_with_transaction` is called from exactly three places: `_stats_task`, `_lineup_task` and `merge_staging`. In `_stats_task` and `_lineup_task`, the network call (`get_fixture_feed` or `get_lineups`) and parsing (`parse_fixture_payload`, `build_staging_rows` or `parse_lineup_payload`) are **statements executed before** the call. Their results are bound to local variables and captured by the closure `write`. The closure itself performs only SQL.
3. **Consequence.** A connection is held only for the duration of `write(cur)` plus `COMMIT`, which is bounded by `statement_timeout`. It is never held across HTTP latency, retries, backoff sleeps or rate-limiter waits. The number of simultaneously held connections is at most the number of workers currently in P5. It is at most \(W\), and in practice much smaller: the test suite observes a peak of 1 with instantaneous fake DB writes.
4. **Leak freedom.** `putconn` runs in `finally` for every exit path: return, retryable exception, fail-closed `QueryCanceled`, and non-retryable exceptions. A broken connection is discarded rather than recycled (`close=bool(conn.closed)`).
5. **Verification.** `test_no_connection_is_held_during_network_io` records the pool's holder thread IDs and asserts that no thread fetches while holding a connection. `test_slate_is_processed_concurrently` asserts `pool.gets == pool.puts` and `pool.in_use == 0` after the slate. [T]

**Thread-local sessions.** `VendorClient._session()` reads `self._local.session`. On first use in a thread it constructs a `requests.Session` with the auth headers and a mounted `HTTPAdapter`, then registers it in `self._sessions` under `self._sessions_lock` for deterministic `close()`. Sessions are never shared, so the connection pools inside each adapter (`pool_maxsize=2`) are also thread-confined.

**Rate governance.** `TokenBucketRateLimiter.acquire()` refills and decrements under a single `threading.Lock` and sleeps **outside** the lock. Contention is therefore limited to the arithmetic, and blocked threads do not serialise one another's sleeps. The limiter is shared by all workers, so the aggregate request rate is at most \(r\) by construction (`test_rate_limiter_is_shared_across_threads`). [T]

**Slate processing for \(N \le 40\).** `run_slate`:

1. De-duplicates the UIDs.
2. Submits \(N\) futures of `_run_isolated(task, uid)`.
3. Blocks in `concurrent.futures.wait(futures, timeout=deadline_s)`.
4. Collects `FixtureResult`s from the completed futures, and cancels the unfinished ones. Cancellation affects only futures still queued; running ones continue.
5. Runs `merge_staging()` if `job == "stats"` and at least one fixture succeeded.

`_run_isolated` catches `Exception`, so `future.result()` never raises and one fixture cannot abort slate collection.

### 2.2 Database Pool Allocation Control

**Pool construction (`build_pool`).**

```python
psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=max_workers + 1,
    dsn=dsn,
    options="-c statement_timeout=10000 -c lock_timeout=2000",
)
```

**Sizing invariant.** `ConcurrentIngestionEngine.__init__` raises `ValueError` if `pool.maxconn < max_workers + 1` (`test_pool_smaller_than_workers_is_rejected`). Why \(W + 1\):

- The merge is invoked from the **calling thread** (the thread that called `run_slate`), not from a pool worker. It runs after `wait()` returns.
- At that moment, workers whose fixtures timed out may still be executing and may be inside P5, holding connections.
- Up to \(W\) handles can therefore be in use concurrently with the merge. The \((W+1)\)-th handle guarantees the merge never blocks on pool exhaustion.

`ThreadedConnectionPool.getconn` raises `PoolError` rather than waiting when exhausted, so under-sizing would convert into merge failures.

**Server-side runtime caps.** The `options` string sets both parameters per session at connect time, so they apply to every statement on every pooled connection. This includes the merge function, whose internal statements inherit the session setting.

| Parameter | Value | SQLSTATE on breach | psycopg2 class | Engine behaviour |
|---|---|---|---|---|
| `lock_timeout` | 2000 ms | `55P03` | `psycopg2.errors.LockNotAvailable` (⊂ `OperationalError`) | Rollback. Retry with \(\Delta_a = \texttt{retry\_base\_delay\_s}\cdot 2^{a-1}(1+U)\), \(U\sim\mathcal U(0,1)\), up to `max_db_attempts = 3`. |
| `statement_timeout` | 10000 ms | `57014` | `psycopg2.errors.QueryCanceled` (⊂ `OperationalError`) | Log `[STATEMENT_TIMEOUT]`. Rollback. **No retry**; the fixture fails closed. |
| (deadlock) | — | `40P01` | `DeadlockDetected` | As `lock_timeout`. |
| (serialization) | — | `40001` | `SerializationFailure` | As `lock_timeout`. |
| (connection loss) | — | — | `InterfaceError`, `OperationalError` | As `lock_timeout`; the connection is discarded (`close=True`). |

The `except psycopg2.errors.QueryCanceled` clause precedes `except RETRYABLE_DB_ERRORS` in `_with_transaction`. Python's first-match exception semantics therefore route `57014` to fail-closed, even though it is also an `OperationalError` (`test_statement_timeout_fails_closed_without_retry`). [T]

**Lock-hang bound.** Each retry attempt waits at most 2 s on locks. Worst-case wall time for one fixture's database phase is

\[
T_{\text{db}}^{\max} \approx \sum_{a=1}^{3} \left(2\ \text{s} + t_{\text{exec}}\right) + \sum_{a=1}^{2} \Delta_a ,
\]

where \(t_{\text{exec}}\) is the statement execution time. Without `lock_timeout`, one idle-in-transaction session holding a row lock would stall a worker indefinitely.

### 2.3 Pre-Kickoff Lineups Job Routing

**Why it bypasses staging.** Each fixture's team sheet touches a row set that is disjoint by `fixture_id` in `lineup_expectations`: primary key `(fixture_id, player_id)`. Concurrent lineup workers therefore never contend for the same rows. The job needs no cross-fixture reconciliation, and it must complete inside the T-60 window. Staging would add a serialised merge to the critical path without removing any contention.

**Execution path (`_lineup_task`, one transaction per fixture).**

```
 VendorClient.get_lineups(uid)                              no DB handle
   └► parse_lineup_payload(payload, bench_sub_probability)  no DB handle
        rejects: missing side │ ≠ 11 starters │ duplicate player  → ValueError → fixture failed
 _with_transaction(write):
   1. execute_values(LINEUP_UPSERT_SQL, values, template=LINEUP_TEMPLATE, page_size=100, fetch=True)
        VALUES (vendor_fixture_uid, vendor_player_uid, p_start::numeric, p_sub::numeric)
        JOIN fixtures, players on vendor UIDs
        LEFT JOIN LATERAL (avg minutes_played FILTER (WHERE started) / (WHERE NOT started)
                           FROM player_match_stats WHERE minutes_played > 0) h
        INSERT … ON CONFLICT (fixture_id, player_id) DO UPDATE
          SET p_start, p_sub_appearance, exp_minutes_start, exp_minutes_sub,
              source = 'confirmed_team_sheet', updated_at
        ORDER BY p.player_id                                → consistent row-lock order
        RETURNING player_id                                 → listed_ids
   2. if |listed_ids| < |values|: WARNING (unknown players or fixture)
      if listed_ids = ∅: raise ValueError                   → rollback, fixture failed
   3. cur.execute(LINEUP_EXCLUDE_SQL, (uid, listed_ids))
        UPDATE lineup_expectations le SET p_start = 0, p_sub_appearance = 0,
               source = 'confirmed_team_sheet', updated_at = now()
        FROM fixtures f
        WHERE f.fixture_id = le.fixture_id AND f.vendor_fixture_uid = %s
          AND NOT (le.player_id = ANY(%s::int[]))
   COMMIT
```

**Terminology correction.** Absent players are excluded by a **single set-based `UPDATE`** (step 3), not by a row-by-row loop. The statement's predicate `NOT (player_id = ANY(listed_ids))` selects every non-listed row for the fixture at once.

**Atomicity.** Steps 1–3 share one transaction. A reader therefore sees either the previous `lineup_expectations` state or the complete confirmed sheet, never a sheet with starters written but absentees not yet zeroed.

**Resulting pricing state per player** (`LineupExpectation`):

| Sheet status | `p_start` | `p_sub_appearance` | `exp_minutes_start` / `exp_minutes_sub` |
|---|---|---|---|
| Starter | 1 | 0 | historical mean when starting (default 85.0) / historical mean as substitute (default 20.0) |
| Bench | 0 | `bench_sub_probability` (default 0.35, **uncalibrated**) | as above |
| Absent | 0 | 0 | unchanged |

**Downstream [N].** A projection-refresh job must rebuild `PropProjection.lineup` from these rows before the next `allocate`. Without it, the confirmed sheet never reaches pricing.

---

## SECTION 3: RELATIONAL STAGING MIGRATION & ADVISORY LOCKING

### 3.1 The Unlogged Landing Zone Pattern

**Definition.** `CREATE UNLOGGED TABLE prop_alpha.staging_player_feed (…)` has 42 columns: `batch_id UUID`, `staged_at TIMESTAMPTZ DEFAULT clock_timestamp()`, and the 40 remaining `STAGING_COLUMNS`. It has **no primary key, no foreign keys, no `CHECK` constraints and no indexes**.

**Two properties together reduce write cost.**

1. **No WAL for the staging heap.** Changes to an unlogged relation generate no WAL records, so the WAL volume attributable to staged rows is zero.

   **Precision about the gain.** The per-fixture transaction also inserts into `raw_payloads`, which *is* logged. Each worker's `COMMIT` therefore still waits for a WAL flush (`synchronous_commit = on`); unlogging staging does not make the commit asynchronous or remove the fsync. What it removes is the WAL bytes for the ~1 + \(n_{\text{players}}\) staging rows per fixture. Those rows duplicate data already durable in `raw_payloads.payload`. That lowers WAL generation, WAL flush size and replication traffic during a burst.

   Staging heap pages are still written to data files by the background writer and checkpointer. "Bypassing WAL" is therefore not "bypassing disk".
2. **No constraint or index maintenance.** With no indexes, an `INSERT` modifies only the heap: no B-tree page splits, no index-page lock contention, no unique checks and no foreign-key lookups against core tables. Concurrent writers contend only on heap-page selection and relation extension. This is the dominant concurrency benefit, and it would hold even for a logged table with no indexes.

**Durability contract.**

- On crash recovery, PostgreSQL resets every unlogged table to empty.
- Unlogged tables are not replicated to physical standbys.
- Staged but unmerged rows are therefore **recoverable, not durable**.
- The recovery source is `raw_payloads` (logged, `payload JSONB`, `UNIQUE payload_sha256`). Rows are rebuilt by re-running `parse_fixture_payload` → `build_staging_rows` over the payloads fetched since the last successful merge, then merging.
- Core tables are logged, so nothing already merged is at risk.

### 3.2 Non-Blocking Claim-By-Delete Mechanics

**Object type.** `prop_alpha.merge_staging_player_feed()` is a PL/pgSQL **function** (`RETURNS TABLE (fixtures_merged integer, player_rows_merged integer, rows_rejected integer)`), not a `PROCEDURE`.

**Transaction behaviour.** It runs inside the caller's transaction, which `_with_transaction` opens (`MERGE_SQL = "SELECT fixtures_merged, player_rows_merged, rows_rejected FROM prop_alpha.merge_staging_player_feed()"`). Every statement below therefore commits or rolls back as one unit.

```
 step  statement                                                        locks acquired
 ────  ───────────────────────────────────────────────────────────────  ─────────────────────────────────
  1    PERFORM pg_advisory_xact_lock(                                   advisory lock (exclusive) on key
         hashtext('prop_alpha.merge_staging_player_feed'))              hashtext(...); released at txn end
  2    DROP TABLE IF EXISTS pg_temp.claimed_feed;                       session-local temp objects only
       CREATE TEMP TABLE claimed_feed (LIKE staging_player_feed)
         ON COMMIT DROP;
  3    WITH drained AS (DELETE FROM staging_player_feed RETURNING *)    ROW EXCLUSIVE on staging;
       INSERT INTO claimed_feed SELECT * FROM drained;                  row locks on deleted tuples
  4    WITH bad AS (DELETE FROM claimed_feed c WHERE <unknown team uid  (temp table only)
         OR invalid fixture_status> RETURNING c.*)                      ROW EXCLUSIVE on staging_rejects
       INSERT INTO staging_rejects (… reason = 'unknown team uid or
         invalid fixture status', row_data = to_jsonb(b)) …
  5    WITH bad AS (DELETE … WHERE <player constraint violations>)      as step 4
       INSERT INTO staging_rejects (… 'player row violates core
         constraints' …)
  6    INSERT INTO referees AS r … SELECT DISTINCT ON (referee_uid) …    ROW EXCLUSIVE; row locks in
         ORDER BY referee_uid, staged_at DESC ON CONFLICT                 vendor_referee_uid order
         (vendor_referee_uid) DO UPDATE …
  7    INSERT INTO fixtures AS fx … DISTINCT ON (vendor_fixture_uid) …   ROW EXCLUSIVE; row locks in
         ORDER BY x.vendor_fixture_uid ON CONFLICT (vendor_fixture_uid)   vendor_fixture_uid order
         DO UPDATE …
  8    INSERT INTO players AS pl … DISTINCT ON (vendor_player_uid) …     ROW EXCLUSIVE; vendor_player_uid
         ORDER BY vendor_player_uid, staged_at DESC ON CONFLICT …         order
  9    INSERT INTO player_match_stats AS s … DISTINCT ON                 ROW EXCLUSIVE; (fixture_id,
         (vendor_fixture_uid, vendor_player_uid) … ORDER BY               player_id) order
         f.fixture_id, p.player_id ON CONFLICT (fixture_id, player_id)
         DO UPDATE …
 10    RETURN QUERY SELECT v_fixtures, v_rows, v_rejected;
```

**Why the drain is non-blocking for writers.**

- Step 3 takes `ROW EXCLUSIVE` on `staging_player_feed`. This is the same lock mode every ingestion `INSERT` takes, and `ROW EXCLUSIVE` does not conflict with itself. Writers therefore proceed concurrently with the merge.
- Step 3's row locks apply only to the tuples being deleted, which no writer touches, because writers only insert new tuples.
- Serialisation between mergers is by advisory lock (step 1), not by any table lock.

### 3.3 Structural Edge Protections

**P1 — No late-arriving record is dropped.** Step 3's `DELETE` executes against the snapshot taken when the statement starts (READ COMMITTED: one snapshot per statement). Let \(\mathcal{S}_0\) be the set of staging tuples committed before that instant.

- The `DELETE` removes exactly \(\mathcal{S}_0\), and `RETURNING *` copies exactly \(\mathcal{S}_0\) into `claimed_feed`.
- A tuple committed by a worker after the snapshot is invisible to the `DELETE`. It is neither removed nor claimed, so it remains in staging for the next merge.
- Deletion and copy are the same statement, so no interleaving exists in which a tuple is removed without being claimed.

**P2 — No record is merged twice.** A tuple claimed by merge \(M_1\) is deleted in \(M_1\)'s transaction. Any later merge \(M_2\) starts after \(M_1\) commits or rolls back, because \(M_2\) waits on the advisory lock.

- **\(M_1\) commits:** the tuple no longer exists.
- **\(M_1\) rolls back:** the `DELETE` is undone, and the tuple is claimed exactly once, by \(M_2\).

Consecutive re-ingestions of the same fixture stage duplicate tuples. Each merge collapses them with `DISTINCT ON (…) ORDER BY …, staged_at DESC`, and `ON CONFLICT DO UPDATE` makes cross-merge repetition idempotent.

**P3 — Contrast with `TRUNCATE` (prohibited).** A read-then-`TRUNCATE` sequence removes the whole heap, including tuples committed after the read. That violates P1 with no trace. `TRUNCATE` also takes `ACCESS EXCLUSIVE`, which conflicts with writers' `ROW EXCLUSIVE`. Worse, because lock requests are granted in queue order, writers arriving after a pending `TRUNCATE` queue behind it even before it is granted. Under `lock_timeout = 2000` that surfaces as a burst of `55P03` failures in the workers.

**P4 — Deadlock prevention by consistent lock order.** Two terms need separating:

- **Chronological ordering.** This governs *which* staged version wins: `staged_at DESC` inside `DISTINCT ON`.
- **Lock ordering.** This governs *the order in which rows are locked*: each upsert's `ORDER BY` on its conflict key. For `player_match_stats` that is `ORDER BY f.fixture_id, p.player_id`, matching the primary key `(fixture_id, player_id)`.

A deadlock requires two transactions to acquire overlapping row locks in opposite orders. Within one table, every writer that orders by the same key acquires overlapping locks in the same order, so no cycle can form. The lineup upsert orders by `p.player_id` within a single fixture, which is compatible.

Across tables, the merge locks rows in the fixed sequence referees → fixtures → players → player_match_stats. The only other transaction updating `referees` is `run_referee_job`, which orders by `referee_id`, a different key. It therefore acquires the **same advisory lock** first (`MERGE_LOCK_KEY = "prop_alpha.merge_staging_player_feed"`), which makes that interleaving impossible. `test_referee_job_shares_merge_advisory_lock_key` asserts that the key string in the Python job and in `schema.sql` are identical.

**P5 — Invalid data isolation.** Steps 4–5 move rows that would violate core constraints into `prop_alpha.staging_rejects (reject_id, vendor_fixture_uid, vendor_player_uid, reason, row_data JSONB, rejected_at)` before any core write. The row travels through `to_jsonb(b)`, preserving every staged column for forensics.

- **Fixture-level predicate:** home or away team UID absent from `teams.vendor_team_uid`, or `fixture_status ∉ {scheduled, live, final, postponed}`.
- **Player-level predicate:** `team_uid` null or not one of the two sides, null name or position, `tackles_won > tackles_attempted`, `duels_won > duels_total`, `yellow_cards ∉ [0,2]`, `red_cards ∉ [0,1]`, negative minutes, or `team_possession_pct ∉ [0,100]`.

Without this step, a single bad tuple would raise a `CHECK` violation and abort the entire merge transaction, blocking every fixture behind it. The same predicates are enforced earlier in Python by `PlayerMatchRecord.__post_init__`. The SQL layer is the backstop for rows staged by any other writer, such as a replay from `raw_payloads` with an older transform version.

**P6 — Failure rollback preserves staging.** If any step raises, for example `57014` or a `55P03` retried to exhaustion, the transaction rolls back. The rollback undoes step 3's `DELETE`, the `ON COMMIT DROP` temp table disappears, and the advisory lock releases. Staging is left exactly as it was, and `SlateReport.merge_error` records the cause.

---

## SECTION 4: MEAN-VARIANCE MATRICES & EXECUTION FLOWS

### 4.1 Predictive Parameter Mapping

Worked example: `tackles`, `player_id = 55`, `fixture_id = 1001`, `game_state_k = 6`. The numbers are those produced by the v3.1 engine.

```
 player_match_stats (player_id = 55, minutes_played > 0, ordered by kickoff)
   counts  x = [3, 2, 4, 3, 1, 3]
   minutes m = [90, 90, 90, 85, 90, 90]                 [N] loader: ProjectionRefresh
        │
        ▼
 QuantEngine.positional_prior(league_rate_per90 = 2.2, prior_strength_90s = 6)
   α₀ = 6 · 2.2 = 13.2,  β₀ = 6
        │
        ▼
 QuantEngine.gamma_poisson_update(prior, counts, minutes)
   α_n = α₀ + Σx = 13.2 + 16 = 29.2
   β_n = β₀ + Σ m/90 = 6 + 535/90 = 11.944
   E[Λ] = α_n / β_n = 2.4447 per 90
        │
        ▼
 QuantEngine.referee_multiplier   (market ∉ REFEREE_SENSITIVE_MARKETS → c = 1)
        │
        ▼
 QuantEngine.predictive_gamma(posterior, c)
   μ = c α_n/β_n = 2.4447
   v = 1/α_n + 1/k + 1/(α_n k) = 0.03425 + 0.16667 + 0.00571 = 0.20662   (∈ [1e-6, 1e3]: no [MATH_CLAMP])
   a* = 1/v = 4.840,  b* = a*/μ = 1.980
        │
        ▼
 PropProjection(gamma_shape = 4.840, gamma_rate = 1.980,
                lineup = LineupExpectation(0.92, 0.05, 86, 20))       __post_init__: finite, > 0
        │
        ▼
 count_distribution(86):  X ~ NB(a*, b*/(b* + 86/90))
   E[X] = μ t = 2.336,  Var[X] = μ t (1 + t/b*) = 3.464,  D = 1.48
```

**Formal mapping.** Take \(\mathbf{x} \in \mathbb{N}_0^{n}\), \(\mathbf{m} \in \mathbb{R}_{>0}^{n}\), and \(t = m/90\). The chain from data to predictive distribution is

\[
(\mathbf{x}, \mathbf{m}) \mapsto \Big(\alpha_n, \beta_n\Big) = \Big(\alpha_0 + \mathbf{1}^\top \mathbf{x},\ \beta_0 + \tfrac{1}{90}\mathbf{1}^\top \mathbf{m}\Big)
\mapsto \Big(\mu, v\Big) = \Big(c\tfrac{\alpha_n}{\beta_n},\ \tfrac{1}{\alpha_n} + \tfrac{1}{k} + \tfrac{1}{\alpha_n k}\Big)
\mapsto \Big(a^*, b^*\Big) = \Big(\tfrac{1}{\bar v},\ \tfrac{1}{\bar v \mu}\Big),
\]

where \(\bar v = \min(\max(v, 10^{-6}), 10^{3})\). Each arrow validates its outputs and raises `ProjectionError` on a non-finite or non-positive value.

**Variance form.** \(v\) is computed as a sum of non-negative terms. It never forms \(E[\Lambda^2] - \mu^2\), whose floating-point evaluation returns \(-1.42\times10^{-14}\) at \(\alpha_n = 10^{17}\).

### 4.2 Exact Covariance Asset Optimization

**Outcome grid.** For player-market key \(\kappa\), the allocator first filters candidates. `_candidates` keeps quotes with EV > `alpha_threshold`, the best price per `(line_value, side, void_rule)`, and \(p_{\text{lose}} > 10^{-6}\). That leaves \(J\) candidates with lines \(L_j\), sides, prices \(O_j = 1 + b_j\) and void rules.

`outcome_model` enumerates the joint outcomes \(s = (\sigma, x)\), where the scenario \(\sigma \in \{\text{start}, \text{sub}, \text{absent}\}\):

- **Start and sub:** \(x \in \{0, \dots, x_{\text{top}} - 1\} \cup \{\ge x_{\text{top}}\}\), with \(x_{\text{top}} = \lfloor \max_j L_j \rfloor + 1\).
- **Absent:** \(x = 0\).

\[
\pi_s = w_\sigma \cdot
\begin{cases}
\text{NB.pmf}(x;\ t_\sigma) & x < x_{\text{top}} \\
\text{NB.sf}(x_{\text{top}} - 1;\ t_\sigma) & x = \text{tail}
\end{cases},
\qquad w = (p_{\text{start}},\ p_{\text{sub}},\ p_\varnothing)
\]

\[
R_{s,j} =
\begin{cases}
0 & \text{void under rule}_j \text{ in scenario } \sigma, \text{ or } x = L_j \\
b_j & \text{side}_j \text{ wins at } x \\
-1 & \text{otherwise}
\end{cases}
\]

**Why the tail bucket is exact.** \(x \ge x_{\text{top}} > \max_j L_j\) implies every Over wins and every Under loses, so every row in the tail has the same payoff vector.

**Why \(\Sigma\) is dense.** All candidates on a player settle on the **same** count \(X\). Their payoffs are deterministic functions of one random variable, so every off-diagonal entry is generically non-zero.

**Moments** (`return_moments`):

\[
\mu = R^\top \pi \in \mathbb{R}^J, \qquad \Sigma = R^\top \operatorname{diag}(\pi)\, R - \mu\mu^\top \in \mathbb{R}^{J\times J}, \qquad \Sigma \leftarrow \tfrac12(\Sigma + \Sigma^\top) + \max(0, -\lambda_{\min})\, I
\]

The eigen-floor term is inactive in exact arithmetic, since \(\Sigma\) is a covariance matrix and is PSD. It guards only against round-off.

**Worked matrix.** Over 1.5 at 1.797 and Over 2.5 at 2.834, `VOID_IF_NO_APPEARANCE`, \(p_\varnothing = 0.03\). The grid has 9 rows: 4 start, 4 sub, 1 absent.

\[
\mu = \begin{pmatrix} 0.0600 \\ 0.0620 \end{pmatrix}, \qquad
\Sigma \approx \begin{pmatrix} 0.757 & 0.758 \\ 0.758 & 1.827 \end{pmatrix}, \qquad
\rho_{12} = \frac{\Sigma_{12}}{\sqrt{\Sigma_{11}\Sigma_{22}}} \approx 0.645
\]

The off-diagonal term follows from nesting: \(\{X \ge 3\} \subset \{X \ge 2\}\). With \(p_w = (0.5733,\ 0.3642)\) and \(p_l = (0.3967,\ 0.6058)\):

\[
E[R_1R_2] = p_{w,2}\, b_1 b_2 - (p_{w,1} - p_{w,2})\, b_1 + p_{l,1}
= 0.3642(0.797)(1.834) - 0.2091(0.797) + 0.3967 \approx 0.762,
\]
\[
\Sigma_{12} \approx 0.762 - (0.060)(0.062) \approx 0.758 .
\]

`test_cross_line_covariance_matches_brute_force` verifies this construction against a direct summation over \(x \in [0, 300)\).

**Optimisation** (`solve_mean_variance`):

\[
\max_{f} \; U(f) = f^\top \mu - \lambda_{\text{risk}}\, f^\top \Sigma f
\quad \text{s.t.} \quad 0 \le f_j \le \min(f_j^*, f_{\text{pos}}),\ \ \mathbf{1}^\top f \le f_{\text{pos}}
\]

- \(\Sigma \succeq 0\), so \(U\) is concave and the SLSQP optimum is global.
- The result is compared against the best single-candidate closed form \(f_j = \min(\mu_j / (2\lambda\Sigma_{jj}),\ \text{cap}_j)\). The better of the two is returned (`solver ∈ {"qp", "single"}`).

**Result** (\(\lambda = 2\), \(f_{\text{pos}} = 0.02\)): \(f = (0.01937,\ 0.00044)\).

| Allocation rule | \(f\) | \(E[f^\top R]\) | \(\text{Var}[f^\top R]\) | \(U\) | Growth drift \(g = E - \tfrac12 \text{Var}\) | Drawdown exponent \(2g/\text{Var}\) |
|---|---|---|---|---|---|---|
| Mean-variance (v3.1) | \((0.01937, 0.00044)\) | \(1.190\times10^{-3}\) | \(2.974\times10^{-4}\) | \(5.95\times10^{-4}\) | \(1.041\times10^{-3}\) | 7.0 |
| Nominal-EV sort, quarter Kelly (v2) | \((0, 0.0087)\) | \(5.40\times10^{-4}\) | \(1.39\times10^{-4}\) | \(2.63\times10^{-4}\) | \(4.71\times10^{-4}\) | 6.8 |

**What this proves, and what it does not.**

1. **Complete ruin is excluded by the caps, not by the objective.** A slate's loss is at most \(\sum_j \text{stake}_j \le f_{\text{tot}} \cdot (\text{bankroll} - \text{open\_exposure})\), with \(f_{\text{tot}} = 0.20\). After \(n\) consecutive total-loss slates, wealth is \(W_n \ge (1 - f_{\text{tot}})^n W_0 > 0\), so wealth can never reach zero in finite time. This guarantee is deterministic and model-free. It holds equally for any allocator subject to the same caps, including linear EV sorting.
2. **The mean-variance objective controls the *rate of drawdown relative to growth*.** Under the Brownian approximation of §2.4 of the PRD, \(P(\text{wealth ever reaches } qW_0) = q^{2g/\text{Var}}\). In the table both rules have a drawdown exponent of about 7, i.e. \(P(\text{ever halve}) \approx 0.8\%\). The mean-variance allocation reaches it with **2.2× the growth drift**, at the same risk budget.

   Equivalently, at matched growth, EV sorting must stake the higher-variance line more heavily and accepts a smaller exponent. The advantage comes from two things linear EV sorting cannot use:
   - the variance ratio \(\Sigma_{22}/\Sigma_{11} = 2.41\), which dwarfs the EV difference of 0.002;
   - the correlation \(\rho_{12} = 0.645\), which penalises holding both lines.
3. **The protection is conditional on the model.** \(\mu\) and \(\Sigma\) are model outputs. If the realised edge is a fraction \(\rho\) of the modelled one, the exponent becomes \(4\lambda\rho - 1\). This is why the PRD holds \(\lambda \ge 2\) until the paper-betting gate (PRD Gate 3) has measured \(\rho\).

**Post-optimisation caps.** `_apply_group_cap` scales fractions proportionally: first per `fixture_id` to `max_fixture_fraction = 0.05`, then across the slate to `max_total_fraction = 0.20`. Stakes are \(\min(f\cdot(\text{bankroll}-\text{open\_exposure}),\ \texttt{max\_stake})\). Scaling preserves each market's internal line mix but is not a joint re-optimisation across fixtures, because cross-fixture covariance is not modelled in v3.1.

### 4.3 Concrete Sequence Map

The brief's `MarketScanner` is `InstitutionalAssetManager` in v3.1. Components tagged [N] do not exist.

```
 Sportsbook API    [N] OddsPoller     PostgreSQL          [N] Projection     InstitutionalAssetManager         [N] Ledger /
      │                  │               (core)             Refresh              .allocate()                    Execution
      │                  │                 │                    │                     │                              │
      │ JSON quote       │                 │                    │                     │                              │
      │ payload ────────►│                 │                    │                     │                              │
      │                  │ INSERT odds_lines ON CONFLICT ON     │                     │                              │
      │                  │ CONSTRAINT uq_odds_snapshot DO NOTHING                     │                              │
      │                  │────────────────►│                    │                     │                              │
      │                  │                 │  (T-60 lineups job │                     │                              │
      │                  │                 │   has updated      │                     │                              │
      │                  │                 │   lineup_expectations)                   │                              │
      │                  │                 │◄── SELECT pms, lineup_expectations,      │                              │
      │                  │                 │    referees ───────│                     │                              │
      │                  │                 │                    │ QuantEngine.project_count_market /                 │
      │                  │                 │                    │ project_card_market │                              │
      │                  │                 │                    │  positional_prior → gamma_poisson_update           │
      │                  │                 │                    │  → referee_multiplier → predictive_gamma           │
      │                  │                 │                    │  (ProjectionError → market not emitted)            │
      │                  │                 │◄── INSERT model_projections             │                              │
      │                  │                 │                    │── projections: Mapping[key, PropProjection] ──────►│
      │                  │                 │── latest odds_lines per (fixture, book, market, player, line)           │
      │                  │                 │   via idx_odds_market_latest → LiveOddsQuote list ───────►│             │
      │                  │                 │                    │                     │                              │
      │                  │                 │                    │                     │ 1. for each quote:           │
      │                  │                 │                    │                     │    now − captured_at > 5 min │
      │                  │                 │                    │                     │    → report.stale_quotes += 1│
      │                  │                 │                    │                     │ 2. group by (fixture_id,     │
      │                  │                 │                    │                     │    player_id, market_type)   │
      │                  │                 │                    │                     │    no projection →           │
      │                  │                 │                    │                     │    report.skipped            │
      │                  │                 │                    │                     │ 3. _candidates: side_prob-   │
      │                  │                 │                    │                     │    abilities per (line, side,│
      │                  │                 │                    │                     │    void_rule); EV > α;       │
      │                  │                 │                    │                     │    best price; p_lose ≤ 1e-6 │
      │                  │                 │                    │                     │    → skipped "degenerate"    │
      │                  │                 │                    │                     │    ProjectionError/ValueError│
      │                  │                 │                    │                     │    → skipped "unusable"      │
      │                  │                 │                    │                     │ 4. outcome_model → (π, R)    │
      │                  │                 │                    │                     │ 5. return_moments → (μ, Σ)   │
      │                  │                 │                    │                     │ 6. solve_mean_variance       │
      │                  │                 │                    │                     │    (SLSQP ∥ single) → f      │
      │                  │                 │                    │                     │    → report.allocations      │
      │                  │                 │                    │                     │ 7. _apply_group_cap: fixture │
      │                  │                 │                    │                     │    0.05, slate 0.20          │
      │                  │                 │                    │                     │ 8. stake = min(f·available,  │
      │                  │                 │                    │                     │    max_stake), round 0.01    │
      │                  │                 │                    │                     │    stake < min_stake →       │
      │                  │                 │                    │                     │    report.skipped_directives │
      │                  │                 │                    │                     │    else → report.directives  │
      │                  │                 │                    │                     │──── StakingDirective ───────►│
      │                  │                 │◄──────────────────────────────── INSERT bets (is_paper = TRUE) ─────────│
      │                  │                 │                    │                     │                              │
      │ ◄───────────────────────────────── [N] submission: re-read live price & max_stake, re-clip,               │
      │                                        abort on price move or staleness ────────────────────────────────── │
```

**Timing contract for the allocation step.** Everything inside `allocate` is in-process numerical work over at most a few candidates per key: a \(J \times J\) QP with \(J \lesssim 10\), and a grid of at most \(2(x_{\text{top}}+1)+1\) rows. The end-to-end latency of the loop is therefore dominated by quote capture, the database round trip for the latest-quote read, and the projection-refresh cadence, all of which are [N]. Capture-to-decision latency MUST be benchmarked once `OddsPoller` exists, and `max_quote_age` (default 5 min) should be tightened to match.
