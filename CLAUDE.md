# CLAUDE.md — prop_alpha

Pre-match pricing and mean-variance staking for tier-3/4 football player props (tackles, fouls, cards).
Output is `StakingDirective`s only; nothing places bets.

Full rules for any code change: **AGENT.md**. Requirements and go-live gates: `prop_alpha_PRD_v3.1.md`. Topology: `ARCHITECTURE.md`. Type contract, constraints and CI: `ARCHITECTURE-ESSENTIALS.md`. If this file conflicts with AGENT.md, AGENT.md wins.

## Status you must not overstate

- **The SQL has never run against PostgreSQL:** `schema.sql`, the migrations, and `merge_staging_player_feed()`. Say so whenever you touch it.
- **Not built:** the odds poller, projection refresh, bet ledger and execution components.
- **Model not validated:** no PRD go-live gate has been passed.
- **Runtime floor is Python 3.12,** set by the numpy 2.5.1 and scipy 1.18.0 pins. The suite has only run on 3.14.6.

## Design philosophy

- **I/O isolation.** Only `ingestion/pipeline.py` does network and pool I/O (and `calibration/referee_job.run_referee_job` for its own job). Workers fetch and parse first, then take a pooled connection for one short transaction inside `ConcurrentIngestionEngine._with_transaction`, which is the only `pool.getconn()` call site. Never pass connections, cursors, pools or sessions into `transform.py`, `engine.py`, `market_scanner.py`, `fitting.py` or `backtest.py`.
- **Mathematical conservatism.** The allocator maximises \(f^\top\mu - \lambda f^\top\Sigma f\) jointly over all lines for a player, using the exact covariance from `return_moments`. Never revert to nominal-EV selection or per-line sizing. Don't change risk defaults (`risk_aversion=2.0`, the caps, `alpha_threshold`) without an explicit human instruction.
- **Fail closed.** Raise `ValueError` or `ProjectionError` at the point of detection. Catch only at the five handlers in AGENT.md §1.3, each of which records the failure. No fallback distributions, no silent floors. The one clamp (relative variance) logs `[MATH_CLAMP]`.
- **Type rigidity.** Follow these rules; they are enforced by `tests/test_typing_contract.py`:
  - `from __future__ import annotations` in every module;
  - every parameter and return annotated;
  - `Sequence`/`Mapping` for read-only inputs;
  - `Final[...]` on module constants;
  - frozen dataclasses that validate in `__post_init__` for value objects.

## Commands

Run everything from the repository root.

```bash
# Full CI pipeline: fresh .venv-ci, pinned binary wheels, all suites, and a no-skip gate
bash ci/bootstrap.sh

# Fast checks only (interpreter >= 3.12, required files, every requirement pinned ==); installs nothing
bash ci/bootstrap.sh --preflight
```

For manual setup, use a virtual environment. Pins are exact, and binary wheels only keeps installs reproducible:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
```

Tests:

```bash
# Whole suite (180 tests). This is the command that counts.
python -m pytest tests -q -p no:cacheprovider

# Individual suites
python -m pytest tests/test_core.py -v                  # maths, engine, allocator, estimators, transforms
python -m pytest tests/test_integrity.py -v             # exposure validation, transform/SQL parity, migrations
python -m pytest tests/test_typing_contract.py -v       # annotations, Final constants
python -m pytest tests/test_agent_contract.py -v        # handlers, pure-module imports, placeholders, G1
python -m pytest tests/test_pipeline_concurrency.py -v -rs
```

`test_pipeline_concurrency.py` **skips silently if psycopg2 is not importable**. `-rs` prints the skip reason. A run that shows "1 skipped" tested nothing, and `ci/bootstrap.sh` fails it.

**Coverage.** `pytest-cov` is not in `requirements.txt`, so `--cov` errors with "unrecognized arguments". To use coverage, add an exact `pytest-cov==<version>` pin to `requirements.txt` in the same change, then run `python -m pytest tests --cov=prop_alpha --cov-report=term-missing`.

## Local PostgreSQL sandbox

This recipe is **untested here**: the authoring machine has no Docker. Use a throwaway database whose name ends in `_ci`; `ci/bootstrap.sh` refuses to reset any other.

```bash
export PGPASSWORD="$(openssl rand -hex 16)"            # never commit a password
docker run --name prop_alpha_sandbox -e POSTGRES_DB=prop_alpha_ci -e POSTGRES_PASSWORD="$PGPASSWORD" \
  -p 5432:5432 -d postgres:15-alpine
```

A **fresh database gets `schema.sql` only**, because it already includes every migration. Never apply `migrations/002_staging_and_indexes.sql` to a v3.2+ schema: it reinstalls the superseded v3.0 merge function and drops the minute-logic reject predicates. Migrations are for upgrading existing databases, in numeric order.

```bash
docker exec -i prop_alpha_sandbox psql -U postgres -d prop_alpha_ci -v ON_ERROR_STOP=1 < schema.sql
docker exec -i prop_alpha_sandbox psql -U postgres -d prop_alpha_ci -v ON_ERROR_STOP=1 \
  -c 'SELECT * FROM prop_alpha.merge_staging_player_feed();'   # PL/pgSQL is only fully checked on first execution
```

Or, with host `psql` installed, let CI run the whole schema stage:

```bash
PROP_ALPHA_CI_DATABASE_URL="postgresql://postgres:${PGPASSWORD}@localhost:5432/prop_alpha_ci" bash ci/bootstrap.sh
```

## Conventions

- **Naming.** `snake_case` for functions, parameters and columns (`padj_tackles`, `minutes_played`). `UPPER_CASE` with `Final[...]` for module constants (`MIN_RELATIVE_VARIANCE`, `MAX_MATCH_MINUTES`). Define each constant in exactly one module and import it; never redeclare.
- **Upsert ordering.** Two different orderings apply, and they must not be confused:
  - **Lock order:** every multi-row upsert orders by its conflict key, e.g. `ORDER BY f.fixture_id, p.player_id` for `player_match_stats`, so concurrent writers take row locks in the same order and cannot deadlock.
  - **Chronological order:** `DISTINCT ON (...) ORDER BY ..., staged_at DESC` only decides which staged version wins.

  Anything else that updates `referees` must take the merge's advisory lock (`referee_job.MERGE_LOCK_KEY`).
- **Numerics.** Vectorise with numpy when operating on data arrays: exposure vectors, outcome grids, estimator inputs. Plain Python `sum` is correct, and preferred, for exact integer tallies over dataclasses (`compute_referee_coefficients`) and for short fixed-size loops (log-sum-exp over a handful of terms). Never compute a variance as \(E[X^2]-\mu^2\) in `engine.py`: use the relative form \(1/\alpha + 1/k + 1/(\alpha k)\) (AGENT.md gate G1).
- **Database changes.**
  - Put every change in a new `migrations/NNN_*.sql`. Never edit an applied migration.
  - Mirror the change in `schema.sql`.
  - Add constraints `NOT VALID`, then `VALIDATE`; build indexes `CONCURRENTLY`; set `lock_timeout`.
  - Every new `CHECK` needs a matching Python validation and a merge reject predicate.
  - Never use `TRUNCATE` on staging.
- **Secrets.** None in code, tests or scripts. Tests need no credentials.

## Before you say a change is done

1. `python -m pytest tests -q -p no:cacheprovider` shows `N passed`, with no failures and no skips, and `N` ≥ the previous total (180). Update `EXPECTED_TESTS` in `ci/bootstrap.sh` if it grew.
2. `bash ci/bootstrap.sh --preflight` passes.
3. New behaviour has tests that failed before the change.
4. Report what you actually ran, quoting the summary lines, and tag SQL or deployment claims **[U]** (not executed) or **[N]** (not built).
