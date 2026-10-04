# CLAUDE.md — prop_alpha

Pre-match pricing and mean-variance staking for tier-3/4 football player props (tackles, fouls, cards).
Output is `StakingDirective`s only; nothing places bets.

Full rules for any code change: **AGENT.md**. Requirements and go-live gates: `prop_alpha_PRD_v3.1.md`. Topology: `ARCHITECTURE.md`. Type contract, constraints and CI: `ARCHITECTURE-ESSENTIALS.md`. If this file conflicts with AGENT.md, AGENT.md wins.

## Status you must not overstate

- **SQL has run on PostgreSQL 18.6 only, and only in CI's database stage.** `schema.sql` installs cleanly; migrations 003 → 004 → 005 upgrade cleanly; `tests/test_merge_postgres.sql` passes (transfer, backfill, one reject). The PRD minimum is 15+, so 18 qualifies; 15–17 are untested. **Not run:** migration 002 on a real v2 database (none exists), the merge on real vendor data, and any concurrency soak. Tag claims beyond this **[U]**.
- **Odds collector: built for OddsPapi player tackles and fouls O/U in League One and Two, verified live on 2026-10-03** (bet365 answered; Sky Bet and William Hill had no fixtures; bet365 listed only goalscorer props a week out, so no tackles/fouls price has been seen yet; fixture matching 12/12 on bet365's real fixtures; player-name matching is unit-tested only). Odds are archived as files, not written to `odds_lines`.
- **Not built:** projection refresh into `model_projections`, the bet ledger, closing-line capture and execution components.
- **Ingestion runs on real data via Sportmonks only.** Backfill on 2026-10-03 into the working `prop_alpha` database: 1,243 of 1,306 finished League One and League Two fixtures (2025/26 complete, 2026/27 to that date), ~44,500 player rows, 0 merge rejects. The 63 not loaded are refused by the converter: 52 have an unidentified player who played, 2 have goal events that don't reproduce the score, 1 has duels won > duels, and 8 were network failures (re-runnable). `tackles_won` is capped at `TACKLES` (see `ingestion/sportmonks.py`). Clubs that changed league between seasons have their earlier home fixtures filed under their current league (fixtures take the home club's league). Sportmonks is on a trial that ends **2026-10-16**. OddsPapi is the odds source (`ingestion/oddspapi.py`); API-Football and The Odds API have keys but no adapter or caller.
- **Model not validated: no PRD go-live gate has been passed.** Gate 1: the fresh-install half of criterion 1 is met; the v2 upgrade half cannot be run (no v2 database exists). Criterion 2's backfill volume exists (1,243 merged fixtures, above), but the gate run is not done: the 63 refused fixtures are not root-caused (criterion 3), and reconciliation, idempotency, the concurrency soak and the lineups check (criteria 4–7) have not been run. Gates 2–4 depend on Gate 1 and on the components above.
- **Betting is manual only.** The maintainer places every bet by hand after a Telegram alert; nothing places bets, and no execution component will be built against a bookmaker. Bookmaker prices are read-only, through a licensed aggregator (OddsPapi). LiveScore Bet (`livescorebet`) was checked on 2026-10-04: 12 League One/Two fixtures for 10 Oct, only "Player Goals" props on 2 of them, no tackles, fouls or cards lines.
- **Runtime floor is Python 3.12,** set by the numpy 2.5.1 and scipy 1.18.0 pins. The suite has only run on 3.14.6.

## Design philosophy

- **I/O isolation.** Only `ingestion/pipeline.py` does network and pool I/O (and `calibration/referee_job.run_referee_job` for its own job, and `alerts/telegram.py` for outbound Telegram messages and its sent-alert state file, and `alerts/status.py` for its read-only status query, and `alerts/scan.py` for the opportunity scan's read-only database query; its one Sportmonks listing call goes through `VendorClient`). `scanner/slate.py` is pure. `config.py` reads `.env`. Vendor adapters such as `ingestion/sportmonks.py` are pure: response in, transform payload out. Workers fetch and parse first, then take a pooled connection for one short transaction inside `ConcurrentIngestionEngine._with_transaction`, which is the only `pool.getconn()` call site. Never pass connections, cursors, pools or sessions into `transform.py`, `engine.py`, `market_scanner.py`, `fitting.py` or `backtest.py`.
- **Mathematical conservatism.** The allocator maximises \(f^\top\mu - \lambda f^\top\Sigma f\) jointly over every line, book and player in a fixture, using the exact covariance from `fixture_moments` (shared game-state factor), with caps net of open exposure (`ExposureBook`) and the model-risk loss floor (`MODEL_RISK_LOSS_FLOOR` = 0.03, un-backtested). Never revert to nominal-EV selection or per-line sizing. Don't change risk defaults (`risk_aversion=2.0`, the caps, `alpha_threshold`, `MODEL_RISK_LOSS_FLOOR`) without an explicit human instruction.
- **Fail closed.** Raise `ValueError` or `ProjectionError` at the point of detection. Catch only at the sanctioned handlers in AGENT.md §1.3, each of which records the failure. No fallback distributions, no silent floors. The one clamp (relative variance up to its 1e-6 minimum) logs `[MATH_CLAMP]`; a relative variance above 1e3 raises.
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
# Whole suite (408 tests). This is the command that counts.
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

Ingestion (Sportmonks by default; the token comes from the environment or `.env`). The two clubs of every fixture must already be rows in `prop_alpha.teams` with `vendor_team_uid` = the Sportmonks team id, or the merge rejects the fixture:

```bash
TARGET_FIXTURE_UIDS=19727702 DATA_VENDOR_MAX_REQUESTS=50 DATA_VENDOR_QUOTA_RESERVE=100 \
PROP_ALPHA_DB_DSN=postgresql://postgres@localhost:5432/prop_alpha_ci python -m prop_alpha.ingestion.pipeline stats
```

Every attempt, retries included, spends vendor quota. The client retries once by default, stops at `DATA_VENDOR_MAX_REQUESTS` per run, and stops for the run once the vendor reports `DATA_VENDOR_QUOTA_RESERVE` or fewer requests left (from a header, or Sportmonks' `rate_limit` body field). A 2xx reply with an `errors` body counts as a failure.

Telegram alerts (sends messages only; never places bets). Only high-value bets are sent (`scanner/value_screen.py`): mispriced by the book, at least 4% expected profit per unit staked, and an edge that still returns at least 4% in more than 60% of 1,000 simulated match conditions, all after the 0.03 model-risk loss floor. One alert per market, line, side and book. With a `SentAlertStore` (`.prop_alpha_state/sent_alerts.jsonl`, gitignored), that holds across runs, and `store.exposure(now)` must be passed to `allocate` so that a new run sizes net of earlier alerts. Until the bet ledger exists, every alert from the last 4 days counts as open exposure. The `--demo` stays in memory. Each alert shows the minimum acceptable odds and an expiry:

```bash
python -m prop_alpha.alerts.telegram --get-chat-id   # after sending /start to the bot
python -m prop_alpha.alerts.telegram --check         # token check + one test message
python -m prop_alpha.alerts.telegram --demo          # synthetic reference-case alerts, labelled as such
python -m prop_alpha.alerts.status [--send]          # status report (database, alerts, trial days); no vendor calls
python -m prop_alpha.alerts.scan --odds mock [--send] [--at 2026-10-10T12:00:00+00:00]   # opportunity scan
```

**Opportunity scan** (`alerts/scan.py`, `scanner/slate.py`). Each run covers League One and Two fixtures kicking off in [now, next 4-hour boundary), so every fixture is scanned once, in its last 4 hours. It lists them with one Sportmonks call; prices every regular starter (≥ 40% of the team's last 5 starts) for tackles and fouls from their own last 20 appearances, with a positional league rate over the past 365 days; then fetches odds, runs `allocate`, the high-value screen and `notify`. It sends at most `PROP_ALPHA_MAX_ALERTS` (default 10) bets, best expected profit first, as numbered digest messages of at most 5 bets each (`notify_digest`; each bet shows the match and kickoff, the player and club, the bet and odds, the minimum odds, the edge and the stake), then a one-message summary. Assumptions not yet fitted: lineups are estimated, not confirmed team sheets; the referee is neutral; `game_state_k` = 6.0; card markets are not scanned. **Odds: `--odds oddspapi`** (the scheduled task) fetches real prices only when the window has fixtures: it reads the unmetered account endpoint first and skips odds if fewer than 20 of the free plan's 250 requests would remain, then makes one call per bookmaker in `ODDS_BOOKMAKERS` (default bet365, skybet, williamhill; an HTTP 400/404 "no fixtures" from a bookmaker is noted, not fatal). Any other OddsPapi failure (403, 401, outage, request cap) puts "Odds unavailable this run, nothing priced: …" in the summary and the run still completes (handler H11). The market catalogue (weekly) and team names are cached in `.prop_alpha_state/oddspapi/`; every raw response is archived in `.prop_alpha_state/odds_archive/` for closing-line analysis. Fixtures are matched by kickoff (±15 min) and club-name words, players by normalised name (or unique surname plus initial); anything ambiguous is left out and counted in the summary. Assumed until checked against settlement (step 3): OddsPapi tackles = Sportmonks `TACKLES`, fouls = `FOULS`, void if the player does not play, and a missing stake limit means 50. Real alerts use the real alert store and carry no MOCK label. **`--odds mock`** (`slate.mock_quotes`: the model's own price plus logit noise, SD 0.2, and a 6% margin) is for testing: its alerts say MOCK on their first line and go to `.prop_alpha_state/mock_sent_alerts.jsonl`, never the real store. Bankroll: `PROP_ALPHA_BANKROLL` (default 1000).

A Windows Task Scheduler task, `prop_alpha\Telegram status`, sends the status report and then the opportunity scan every 4 hours (00:00, 04:00, …), running `scripts/telegram_status.cmd` with `.venv`, which CI never deletes. It logs to `.prop_alpha_state\status.log`. It runs on battery, catches up after sleep, starts only when a network is available, retries a failed run every 5 minutes up to 3 times, and needs the user to be logged in. A run that still fails leaves a traceback in the log; the most common cause is no network after waking from sleep. It reads the `prop_alpha` database, never `prop_alpha_ci`. To stop it: `schtasks /Delete /TN "prop_alpha\Telegram status" /F`.

## Local PostgreSQL

Supported: **PostgreSQL 15 or newer** (the schema needs 15 for `UNIQUE NULLS NOT DISTINCT`; CI refuses older servers). Verified: **18.6**, installed natively on this machine with the EDB Windows installer:

- service `postgresql-x64-18`, port 5432, superuser `postgres`;
- binaries in `C:\Program Files\PostgreSQL\18\bin`, which the installer does **not** add to PATH. `ci/bootstrap.sh` finds the newest version there by itself; for manual use, call `psql` by full path or add that folder to PATH. A tool that reports "no psql" has usually only checked PATH;
- the password is in `%APPDATA%\postgresql\pgpass.conf` (outside the repo; `psql` reads it automatically). Never put it in a URL, file or command in the repo;
- CI database: `prop_alpha_ci`. CI stage 6 runs `DROP SCHEMA prop_alpha CASCADE`, so `ci/bootstrap.sh` accepts only the database named exactly `prop_alpha_ci` (`CI_DATABASE_NAME`).

Run the full pipeline, including the database stage (from Git Bash):

```bash
PROP_ALPHA_CI_DATABASE_URL="postgresql://postgres@localhost:5432/prop_alpha_ci" bash ci/bootstrap.sh
```

Stage 6 then: drops and reinstalls the schema from `schema.sql`; runs the merge on empty staging; runs `tests/test_merge_postgres.sql` (it rolls itself back); re-applies 005 to check it is idempotent; replays the upgrade path 003 → 004 → 005; and runs the merge test again. Without `PROP_ALPHA_CI_DATABASE_URL`, stage 6 is skipped and says so.

Manual checks (PowerShell):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -w -h localhost -U postgres -d prop_alpha_ci -X -v ON_ERROR_STOP=1 -f schema.sql
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -w -h localhost -U postgres -d prop_alpha_ci -X -v ON_ERROR_STOP=1 -f tests/test_merge_postgres.sql
```

A **fresh database gets `schema.sql` only**, because it already includes every migration. Never apply `migrations/002_staging_and_indexes.sql` to a v3.2+ schema: it reinstalls the superseded v3.0 merge function and drops the minute-logic reject predicates. Migrations are for upgrading existing databases, in numeric order.

On a machine without a native install, Docker works too (untested here): `docker run --name prop_alpha_sandbox -e POSTGRES_DB=prop_alpha_ci -e POSTGRES_PASSWORD="$PGPASSWORD" -p 5432:5432 -d postgres:18-alpine`, with `PGPASSWORD` from `openssl rand -hex 16`, then the same CI command with `postgres:${PGPASSWORD}@` in the URL.

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
- **Secrets.** None in code, tests or scripts. Tests need no credentials. The Telegram bot token and chat id live only in the gitignored `.env` (template: `.env.example`) or the process environment; `test_no_secrets_in_repository_files` scans for token-shaped strings.

## Before you say a change is done

1. `python -m pytest tests -q -p no:cacheprovider` shows `N passed`, with no failures and no skips, and `N` ≥ the previous total (408). Update `EXPECTED_TESTS` in `ci/bootstrap.sh` if it grew.
2. `bash ci/bootstrap.sh --preflight` passes.
3. If you touched SQL: the full pipeline with `PROP_ALPHA_CI_DATABASE_URL` set ends with `schema smoke test passed`, and any new merge behaviour has a check in `tests/test_merge_postgres.sql`.
4. New behaviour has tests that failed before the change.
5. Report what you actually ran, quoting the summary lines, and tag SQL or deployment claims **[U]** (not executed) or **[N]** (not built).
