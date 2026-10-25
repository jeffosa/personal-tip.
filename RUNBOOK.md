# prop_alpha deployment runbook

**Status: PAPER / SHADOW ONLY.** No PRD go-live gate (Gate 1 in full, Gate 2, 3 or 4) has been passed, the tackles and
fouls market contract is unverified, and nothing in this runbook makes the system fit for real-money use. The
system sends `StakingDirective`s to Telegram as tips; the maintainer places every bet by hand (AGENT.md A5). No component
places bets or signs in to a sportsbook, and none will be built. Current evidence and gate status: `CLAUDE.md`,
`PRODUCTION_READINESS.md`, `MARKET_CONTRACT.md`.

## 1. Tested environment (only what has actually run)

| Item | Tested | Not tested |
|---|---|---|
| Python | 3.14.6 (local full suite and `ci/bootstrap.sh` at `c0b6768`), 3.14.7 (GitHub runner, reported from the maintainer's screenshots of the Actions page) | 3.12.14 full-suite attempt on 2026-10-11: 722 passed, 6 backup-push tests could not spawn Git because this sandbox returned `WinError 5`; this is not a clean full-suite or release-gate pass. 3.13 has no full-suite evidence. On 3.12.13 only `tests/test_model_freeze.py` ran (2026-10-06). |
| PostgreSQL | 18.6 (local service `postgresql-x64-18`, port 5432; CI service `postgres:18`) | 15, 16 and 17 (the schema needs 15 or newer; the GitHub runner's client is 16 against the 18 server) |
| Host | Windows 11, Git Bash for `ci/bootstrap.sh`, Windows Task Scheduler for the jobs | Linux scheduled jobs and Docker (untested here). The current backup code hard-codes the Windows PostgreSQL client path and scheduled wrappers are `.cmd`; a Linux VPS needs portability changes before it can run this schedule. |

The test baseline is `EXPECTED_TESTS` in `ci/bootstrap.sh`, the single source of truth. Dependencies are pinned exactly in
`requirements.txt` and installed as binary wheels only.

## 2. Configuration (variable names only; values live in the gitignored `.env` or the process environment)

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts, status and the bet-recording replies (only that chat is read) |
| `SPORTMONKS_API_TOKEN` | Active results, scans, and team-sheet capture; Starter trial recorded through 2026-11-08 |
| `THESTATSAPI_KEY` | Archive/backfill tooling only; TheStatsAPI trial ended 2026-10-20 |
| `THESTATSAPI_RPS` | Optional rate for TheStatsAPI archive tooling; default is 0.19 requests/second |
| `ODDSPAPI_KEY` | Bookmaker prices (read-only aggregator) |
| `PROP_ALPHA_CURRENCY` | Required bankroll currency: `GBP` or `USD` |
| `PROP_ALPHA_BANKROLL`, `PROP_ALPHA_MAX_ALERTS` | Bankroll and tips per run (optional; defaults in `alerts/scan.py`) |
| `PROP_ALPHA_DB_DSN` | Active working database `prop_alpha` (never contains a password; the password is in `%APPDATA%\postgresql\pgpass.conf`) |
| `PROP_ALPHA_PILOT_DSN` | Active pilot database `prop_alpha_pilot` (optional) |
| `ODDS_BOOKMAKERS`, `LADDER_BOOKMAKERS` | Bookmakers asked for League One/Two prices and for pilot ladders (optional) |
| `DATA_VENDOR`, `DATA_VENDOR_MAX_REQUESTS`, `DATA_VENDOR_QUOTA_RESERVE`, `DATA_VENDOR_RPS` | Legacy callers of `build_vendor_client` select Sportmonks (default) or generic; request-budget controls |
| `PROP_ALPHA_BACKUP_DIR` | Backup folder (must already exist when set) |
| `PROP_ALPHA_BACKUP_GIT_REMOTE` | Private repository that holds only backups |
| `PROP_ALPHA_PYTHON`, `PROP_ALPHA_CI_DATABASE_URL` | CI only: interpreter for `ci/bootstrap.sh`, and the disposable database for stage 6 |

Never print, log or commit values. `.env.example` is the template and holds no credentials.
The active scheduled workflows use Sportmonks and the databases `prop_alpha` and `prop_alpha_pilot`. TheStatsAPI databases
(`prop_alpha_tsa` and `prop_alpha_pilot_tsa`) are retained as archives. `build_vendor_client` selects the configured vendor;
the normal active setting is Sportmonks. Do not mix vendor IDs between the active and archived databases.

## 3. Install

1. Create the virtual environment and install the pins: `python -m venv .venv`, then
   `.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt`.
2. Install PostgreSQL 15 or newer. A fresh database gets `schema.sql` only (it already contains every migration). Never
   apply `migrations/002_staging_and_indexes.sql` to a v3.2 or newer schema. Upgrades apply the numbered migrations in order.
3. Create `.env` from `.env.example` and set `SPORTMONKS_API_TOKEN` for the active jobs. Keep `THESTATSAPI_KEY` only if
   archive/backfill tooling is needed; it is not used by the scheduled production paths.
4. Run the release gate (section 8) before relying on a checkout.

## 4. Scheduled tasks

Windows Task Scheduler, folder `\prop_alpha\`. Each task starts its script through `.venv\Scripts\pythonw.exe
scripts\run_hidden.py <script>.cmd` (no console window), runs on battery, catches up after sleep, needs a network and a
logged-in user, and retries a failed run. Task Scheduler history is off.

| Task | Schedule | Script | Log |
|---|---|---|---|
| `Telegram status` | every 4 hours from 00:00 UTC (00, 04, ... 20 UTC, since 2026-10-11) | `scripts/telegram_status.cmd`: results job, pilot results job, exchange rate, status report, League One/Two scan, pilot-leagues scan | `.prop_alpha_state\status.log` |
| `Bet ledger poll` | every 10 minutes | `scripts/ledger_poll.cmd`: reply poll, closing-price capture, T-60 team sheets, settlement, pilot closing prices, pilot paper-bet settlement | `.prop_alpha_state\ledger.log` |
| `Daily backup` | 03:30 | `scripts/backup.cmd`: `backup.py`, then `backup_push.py` | `.prop_alpha_state\backup.log` |

Moving to an always-on Ubuntu server (maintainer decision 2026-10-11): `deploy/VPS_SETUP.md` (portable runner `scripts/run_task.py`, systemd units in `deploy/systemd/`, restore check `scripts/restore_backup.py`, cut-over order). Never run the tasks on two hosts at once.

Read-only inventory (does not change anything):

```powershell
Get-ScheduledTask -TaskPath '\prop_alpha\' | Get-ScheduledTaskInfo | Format-Table TaskName,LastRunTime,LastTaskResult,NextRunTime
```

`schtasks /Query` has exited with an error from some automation shells; use the PowerShell form from a normal session. Tasks are
created and stopped only by the maintainer (`schtasks /Delete /TN "prop_alpha\<name>" /F` stops one). The pilot-leagues scan (`python -m prop_alpha.alerts.ladder_scan`: Brazil Serie A, Scottish Premiership, Scottish Championship; only Brazil Serie A can be tipped, and since 2026-10-25 its real-price tips are withheld until a settlement contract is verified) runs as
a step of `Telegram status`, after `python -m prop_alpha.ingestion.results --pilot`; its failed steps are `pilot-results`, `pilot-scan-no-odds`,
`pilot-scan-stale` and `pilot-scan`.

## 5. Backup and restore

- **Backup:** `python scripts/backup.py [--keep 14]` writes `prop_alpha_<stamp>/` under the backup folder: a `pg_dump -Fc` checked with
  `pg_restore --list` for every required table, the ledger, alert, prediction and team-sheet state files and `odds_archive/` copied
  and verified by SHA-256, and a `manifest.json` written last. `python scripts/backup_push.py` pushes the newest backup as a one-commit
  branch `backup/prop_alpha_<stamp>` to the private repository (it refuses a public one) and prunes to the newest 14.
- **Restore (always into a new database first):**
  1. `createdb -h localhost -U postgres prop_alpha_restore` (the PostgreSQL `bin` folder, `C:\Program Files\PostgreSQL\18\bin`, is not on PATH)
  2. `pg_restore -w -h localhost -U postgres -d prop_alpha_restore --no-owner prop_alpha.dump` from the backup folder. The pilot database, when the backup has `prop_alpha_pilot.dump`, restores the same way into a database of its own.
  3. Compare row counts with `.prop_alpha_state/status.log`'s database line and with `manifest.json`; check each state file's SHA-256 against the manifest.
  4. To go live on the restored data, stop the three tasks, copy the `state/` files back into `.prop_alpha_state/`, point `PROP_ALPHA_DB_DSN`
     at the restored database, run `python -m prop_alpha.alerts.status`, then re-enable the tasks.
  A restore from the off-machine branch was verified once (2026-10-08, counts matched). Pruning against the real repository and a restore from a later branch are not yet shown **[U]**.
- The repository's GitHub name ends with a dot (`jeffosa/personal-tip.`), so the remote URL `personal-tip..git` is correct.
- Take a backup before any migration. Applied migrations are immutable; a change is a new higher-numbered file.

## 6. Monitoring

- The 4-hourly status report (`python -m prop_alpha.alerts.status`) states database counts, last merge, alerts, open bets, the odds-archive
  evidence line (F-02), trial days left, and warnings (unknown alert deliveries, stale poll, unsent answers). Its pilot section shows the
  last pilot scan (`pilot_last_scan.json`; WARNING when older than 5 hours), pilot predictions logged, refused matches, open paper bets
  and results (the 8 reconstructed 2026-10-11 tips are listed apart from the model-scored total) and closing prices captured.
- `alerts/task_health.py`: each scheduled script reports its failed steps; two failing runs in a row raise a Telegram alert (at most every 6
  hours), recovery sends one message, and a task that has not run for 5 hours (status), 40 minutes (poll) or 26 hours (backup) is flagged.
  It cannot see a machine that is off, asleep or logged out. State: `.prop_alpha_state/task_health.json`.
- Delivery review: `python -m prop_alpha.alerts.delivery` lists messages whose delivery outcome is unknown; nothing is re-sent automatically.
- Settlement check: `python -m prop_alpha.alerts.ledger --reconcile`; prediction log: `python -m prop_alpha.alerts.predictions`.
- Pilot paper bets (never real bets): `python -m prop_alpha.alerts.pilot_ledger --list`; settle with `--settle [--send]`; after you read a bookmaker's result off the site,
  `--record <id> won|lost|void` then `--reconcile` (exit 1 on a discrepancy: the assumed settlement rules disagree with the book). `python scripts/pilot_score.py` scores the logged pilot predictions.
- The trial warning: the status report warns from 7 days before the Sportmonks trial end recorded in `status.DATA_TRIAL_ENDS`
  (2026-11-08). Confirm paid coverage and terms before relying on ingestion after the trial.

## 7. Rollback

- **Code:** `git revert` the offending commit (do not rewrite history); re-run the release gate. The pricing code is frozen
  (`modeling/model_freeze.py`): any pricing change re-freezes the model and moves the Gate 2 lockbox start, so never roll pricing code
  forward or back casually.
- **Data:** restore the last good backup into a new database (section 5); there is no down-migration.
- **Alerts:** stop `Telegram status` to stop scans; stop `Bet ledger poll` to stop reply recording. Alerted stakes still count as exposure.
- **Backups of state:** `.prop_alpha_state/*.jsonl` files are append-only; never edit them by hand.

## 8. Release gate (the commands that count)

Only against the disposable database `prop_alpha_ci`: its name must be exactly that, it must carry the comment `prop_alpha disposable
CI database`, and `fixtures` and `player_match_stats` must be empty; the script refuses otherwise and `DROP SCHEMA` is never run on `prop_alpha`.

```bash
bash ci/bootstrap.sh --preflight
PROP_ALPHA_CI_DATABASE_URL="postgresql://postgres@localhost:5432/prop_alpha_ci" bash ci/bootstrap.sh --release
python -m mypy
npx --yes pyright@1.1.406
```

## 9. Unresolved evidence and open blockers

- **Market contract unverified; real-price tipping disabled** (`MARKET_CONTRACT.md` sections 7-10, audit 2026-10-25): bet365's provider
  (Opta), tackle and foul definitions and non-starter void rule are leads from search summaries (help.bet365.com cannot be read from
  here); Betway's rules were not found. `alerts/market_contract.VERIFIED_CONTRACTS` is empty, so both scans withhold every real-price
  tip ("Tips withheld: N ..." in the scan summary) while still pricing, screening and logging. A contract is added only by the
  maintainer, with the evidence of `MARKET_CONTRACT.md` section 10, in a PR. Of 10 settled paper tips, 0 have a bet365 result; the 5
  SportyBet results are reported as another book's (`pilot_ledger --reconcile`: "OTHER BOOK"), never as agreement.
- **Model value not shown:** `python scripts/value_evidence.py` (read-only; writes `.prop_alpha_state/analysis/value_evidence_<date>.json`)
  reports settlement agreement, eligible predictions, bookmaker-settled outcomes, closing prices, ROI and CLV. On 2026-10-25: 0 eligible
  predictions, 0 own-book settlements, 0 closing prices. Run it after each matchday; it makes no value claim below the Gate 3 sample.
- **Prospective Gate 2 lockbox:** the reviewed protocol starts 2026-10-27 00:00 UTC on Sportmonks data. The 24 examined 2026-10-10 matches and all 2,715 predictions from them are excluded. The scorer uses only eligible predictions logged before kickoff and captures outcomes once after the revision grace. No new eligible predictions exist yet; Gate 2 needs at least 500 scored predictions per line, and its market benchmark and lineup requirements remain open. See `LOCKBOX_PROTOCOL.md`.
- **No tackles or fouls price at a League One/Two bookmaker has been seen**, so no League One/Two alert, closing price or paper bet exists.
- **Vendor and leagues:** current scheduled paths use Sportmonks with `prop_alpha` (League One/Two) and `prop_alpha_pilot` (Brazil Serie A, Scottish Premiership, English Championship). The Starter trial is recorded through 2026-11-08; confirm paid coverage and cost before relying on ingestion after it.
- **Pilot leagues are unvalidated:** Brazil Serie A is in `TIP_LEAGUES`, but its tips are withheld by the contract gate (above); the settlement definition and model-vs-market evidence remain unresolved. Scottish leagues are priced but never tipped. Do not treat any pilot result as validated betting performance.
- **Never run:** migration 002 on a real v2 database; the live T-60 line-up path; the 14-branch pruning of the backup repository.
