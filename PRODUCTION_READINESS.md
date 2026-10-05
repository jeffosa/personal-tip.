# prop_alpha production readiness: remediation status (2026-10-05)

**Position for review: paper evaluation only.** No PRD go-live gate has been passed. Nothing in this build is
evidence that real-money betting is safe, and nothing here should be read as a claim that it is. Bets are placed by
hand by the maintainer after a Telegram alert; no component places bets (AGENT.md A5).

Target: about 7/10, meaning credible for a controlled paper launch and limited review. Items 3–6 below are code and CI
work and are done in this change. Items 1, 2 and 7 need independent evidence (lockbox outcomes, bookmaker
settlements, closing prices) that code cannot produce. They take calendar time and real market data, and a defensible
7/10 depends on them.

Tags as in AGENT.md: **[T]** tested in this change · **[U]** implemented, not executed where it will run · **[N]** not built.

## Summary

| # | Item | Status | Blocking a 7/10? |
|---|---|---|---|
| 1 | Locked model evaluation (PRD Gate 2) | Frozen; collecting. 0 lockbox outcomes yet | Yes: needs ≥ 500 lockbox predictions per market |
| 2 | Settlement verified against each bookmaker | Labelled provisional; confirmed-only total added [T]. Not yet verified | Yes: needs real settled examples |
| 3 | Database checks mandatory in release CI | Done [T] locally; GitHub workflow [U] | No |
| 4 | Type diagnostics resolved and gated in CI | Done [T]: pyright 32 → 0, mypy 29 → 0 | No |
| 5 | Odds-feed failure isolation per bookmaker | Done [T] | No |
| 6 | Alert-delivery recovery | Done [T] | No |
| 7 | Paper-trading gate (PRD Gate 3) | Closing capture built; 0 closing prices, 0 paper bets | Yes: no tackles/fouls line has been quoted yet |

## Done in this change

### 3. Release CI with a mandatory database stage

- `bash ci/bootstrap.sh --release` is the release gate. It refuses to start without `PROP_ALPHA_CI_DATABASE_URL` or
  without node/npx for pyright, rather than skipping stage 6 or pyright as the default mode does.
- Stage 6 (fresh install from `schema.sql`, merge on empty staging, `tests/test_merge_postgres.sql`, re-applying
  005–008, then replaying 003 → 008 and the merge test again) runs only when **three** independent checks agree that
  the database is disposable. The name must be exactly `prop_alpha_ci`. The database must carry the operator-set
  comment `prop_alpha disposable CI database`. Its `fixtures` and `player_match_stats` must be empty. Checked
  read-only on 2026-10-05: the working `prop_alpha` database has rows and no marker, so it would be refused on two
  counts. The local `prop_alpha_ci` was marked once with `COMMENT ON DATABASE`.
- `.github/workflows/ci.yml` runs the release gate on every push and pull request against a throwaway `postgres:18`
  service (trust authentication, reachable only from the runner, so no password exists to leak). **[U]**: it has not
  run on GitHub. The suite has only run on Windows with Python 3.14.6.
- Structural test: `test_ci_resets_only_a_named_marked_and_empty_database_and_release_never_skips_it`.

### 4. Type diagnostics

- The audit's pyright run reported 32 errors; mypy (already pinned) reported 29. Both now report none on
  `prop_alpha`, `scripts` and `tests`, with the configuration committed (`pyrightconfig.json`, `mypy.ini`) so the
  audit, the editor and CI check the same thing. Stage 5 of `ci/bootstrap.sh` runs both, and any error fails CI.
- Real fixes rather than suppressions:
  - `ledger.read_settlement_rows` builds its 4-tuple explicitly.
  - `results.refresh_incomplete` and `scripts/gate1_soak.py` raise on a missing row instead of subscripting `None`.
  - `evaluation/gate2.py` types its array inputs as `ArrayLike`; they were always converted with `np.asarray`.
  - `market_scanner.allocate` no longer reuses one variable name for two types; the allocation maths is unchanged
    (AGENT.md gates G1–G5 not engaged).
  - The test fakes now match the signatures they replace.
- Justified suppressions: exactly two, both `# pyright: ignore` comments in `modeling/engine.py`. scipy's stubs
  type `nbinom(...)` as continuous-or-discrete, but it is discrete. Comments were chosen over casts because
  `engine.py` is fingerprinted by `model_freeze`, and any code change there would re-freeze the model and roll the
  Gate 2 lockbox forward. `test_pricing_code_is_the_frozen_model` still passes, so the frozen model is untouched.
- mypy treats `psycopg2` and `scipy` as untyped (no stub package is pinned). Pinning `types-psycopg2` and
  `scipy-stubs` would tighten this further.

### 5. Odds-feed failure isolation

- Before: a malformed OddsPapi reply from one bookmaker either raised `ValueError`, which handler H11 turned into
  "no odds this run" and so discarded every bookmaker's prices, or raised `KeyError`/`TypeError`/`AttributeError`,
  which no handler catches, so the 4-hourly scan crashed before sending its summary. A `NaN` price was passed on.
- Now:
  - `oddspapi.prop_prices`, `line_catalogue` and `participant_names` check every shape and raise only `ValueError`.
  - Handler H12 covers parsing as well as the request, so one malformed reply drops that bookmaker's prices whole
    and the others are kept.
  - The summary says "Odds FAILED from <book>", and the raw body is archived as `<stamp>_<book>.rejected.json.gz`,
    a name that closing capture never picks up.
  - The account reply and the cached catalogue and name files raise `ValueError`, which H11 reports.
- Still fail-closed: a run in which every bookmaker failed prices nothing and exits non-zero.
- Widening H12 to `ValueError` changes the sanctioned-handler set. It was made at the maintainer's request and
  **awaits formal approval** (AGENT.md §1.3).
- Proof of bite: the 15 new tests failed on the previous code and pass now.

### 6. Alert-delivery recovery

- Every alert, digest and fair-price board message goes into `.prop_alpha_state/alert_deliveries.jsonl`. A
  *pending* record with the full text and numbered bets is written before the alert store and the send. A
  *delivered* record with Telegram's message id is written after Telegram confirms.
- A send that fails, times out or is cut off by a stopped process stays pending. After 5 minutes it is reported as
  **outcome unknown**, because only the operator can see whether it reached the chat. Nothing is re-sent
  automatically, since a second copy could double a bet.
- Operator path: `python -m prop_alpha.alerts.delivery` lists unknown deliveries, and the 4-hourly status report
  carries a WARNING line with their ids. For each one the operator either:
  - runs `--mark-delivered <id>` if the message is in the chat;
  - runs `--resend <id>` if it is not. The re-send is headed RESENT and indexed, so a "placed" reply to it records
    the bet. It is refused after the bets' expiry or the board's kickoff;
  - runs `--dismiss <id>` to leave it unsent.
- No new exception handler was needed: a failure simply leaves the record pending.
- Proof of bite: 10 new tests. Moving the delivery record after the alert store fails 4 of them.

## Needs evidence (not satisfiable by code)

### 1. Locked model evaluation (Gate 2)

- **State:** pricing code frozen 2026-10-05 (`modeling/model_freeze.py`, enforced by a test). The lockbox counts
  matches from 2026-10-06. `scripts/gate2_lockbox.py` refuses to score unfrozen code. No lockbox match has kicked off.
- **Criteria (PRD):**
  - ≥ 500 out-of-sample predictions per target market;
  - a fixture-clustered bootstrap 90% interval for Brier skill with a lower bound above 0;
  - calibration slope in [0.9, 1.1], and every bin with ≥ 50 predictions within 0.03;
  - market benchmark with b₂ > 0 at p < 0.05.
- **Known issues to investigate before approving a market:**
  - On the 2026-09-12 to 2026-10-04 selection window (diagnostic only, not evidence), tackles passed calibration on
    2 of 3 lines and fouls failed (slope 0.78 and 0.83).
  - Fouls is therefore priced but never alerted (`scan.ALERT_MARKETS`).
  - Any fix to fouls is a pricing change. It re-freezes the model and moves the lockbox start; it must never be made
    in response to lockbox results.
- **Scope gap:** the PRD lists `yellow_card` as a target market, but card markets are not priced. Either build them
  or record a maintainer decision to scope Gate 2 to tackles (and fouls).
- **Blocker:**
  - The Sportmonks trial ends **2026-10-16**. Without results ingestion after that date, lockbox outcomes cannot be
    scored.
  - Lockbox outcomes are scored from Sportmonks match data, which a single League One/Two weekend supplies.
  - A subscription decision is needed before 16 Oct.
- **Do not:** relax the criteria, re-freeze in response to lockbox results, or count the selection window as
  evidence.

### 2. Settlement verified against each bookmaker

- **State:** automatic settlement uses an **assumed** contract:
  - tackles = Sportmonks `tackles_attempted`, fouls = `fouls_committed`;
  - void if the player does not play;
  - a whole-line count is a push.

  Automatic results are labelled "(provisional)". The ledger now also reports a **confirmed-only** total (results
  the maintainer reported from the bookmaker with `settled <id> won|lost|void`), and any performance figure must
  quote that total. No bet has been settled yet.
- **To verify, per bookmaker that quotes these markets:**
  1. which statistic settles "player tackles" and "player fouls committed", and its data provider;
  2. whether extra time counts;
  3. the void rule (did not start, did not play, substituted before a minimum minute);
  4. abandoned or postponed matches;
  5. how a whole-number line settles.

  Then reconcile real settled examples with `python -m prop_alpha.alerts.ledger --reconcile` (built 2026-10-05,
  [T]). It compares every `settled` reply with what automatic settlement gives from the stored statistics. Each bet
  is reported as AGREE, DISCREPANCY (the command exits 1), NOT CHECKED (match not final or no stored row) or
  UNCONFIRMED (an automatic result with no reply yet). A reply that changed a recorded automatic result is counted as
  an override. On the real ledger on 2026-10-05: no bet recorded yet, so "nothing verified yet".
- **PRD Gate 3, criterion 4:** 100% of paper bets must reconcile to the realised outcome and the book's void rule
  with **no manual overrides**. A `settled` reply that disagrees with the automatic result is therefore a recorded
  discrepancy to investigate, not a pass.

### 7. Paper-trading gate (Gate 3)

- **Criteria (PRD):**
  - ≥ 300 settled paper bets across ≥ 60 fixtures;
  - a fixture-clustered 90% interval for mean CLV with a lower bound above 0;
  - a power check;
  - 100% settlement integrity.
- **State:**
  - Closing-price capture is built (`alerts/closing.py`, every 10 minutes), but no closing price has been captured.
  - As of 2026-10-05, bet365 lists only goalscorer props for League One/Two, and Sky Bet and William Hill report no
    fixtures. So no tackles or fouls line has been quoted, and no paper bet or CLV can exist yet.
  - The PRD also requires per-player `bench_sub_probability` before Gate 3 results are accepted **[N]**.
- **Keep the lockbox intact** while collecting this evidence. Paper bets must come from the frozen model.

## Second pass (later on 2026-10-05)

- **Release gate on GitHub:** the first run failed in stage 5. Two causes: a test read the maintainer's `.env`, and
  pyright checked 3.14 stdlib files as 3.12. Fixed in 13add6f. Whether the next GitHub run is green has not been
  seen **[U]**: `gh` is not installed here.
- **Alert and bet-reply delivery:** ten gaps closed, each pinned by a test in `tests/test_delivery_gaps.py` that
  fails on the earlier code. Seven are in 355b47f: answers re-sent after a failed send, replies to unconfirmed
  alerts, edited replies, repeated replies, the 24-hour unread limit, messages over 4,096 characters, and repeated
  board parts. Three are in a97234b:
  - a message is indexed for replies before its delivery is confirmed;
  - an OS lock stops two ledger writers recording one reply twice;
  - `scan --at` is refused with real odds.
- **Third delivery pass (afternoon 2026-10-05):**
  - The BETS SETTLED message is now queued before it is sent. Before, a failed send was lost for good, because the
    next run finds nothing new to settle; the next ledger poll now re-sends it. Pinned by 3 tests in
    `tests/test_delivery_gaps.py` that fail on the earlier code.
  - **Open, needs a scheduler decision:** since about 10:51 BST every scheduled run has been killed with exit code
    0xC000013A (console closed or Ctrl+C) before it wrote a log line. No scan, status report or ledger poll has run
    since then, and nothing reported it: `task_health` runs inside the killed script, and is not wired in yet. By
    hand the ledger script completes in 7.5 s with exit 0.
- **Gate 1 criterion 7 (lineups):** passes on 50 archived line-ups (f55bb65): 50/50 succeeded, 0 problems in 2,574
  rows. These are post-match line-ups as Sportmonks recorded them, not team sheets captured 60 minutes before
  kickoff, so the live pre-kickoff path is **[U]**.
- **Unknown positions (migration 009):** a missing Sportmonks `position_id` had been stored as the position 'PNone'.
  It is now stored as NULL. 009 was applied to the working database after a backup: 14 match rows and 3 players were
  repaired. The pricing code and its frozen digest are unchanged, and 009 was applied before the first lockbox
  kickoff.
- **Built, not yet scheduled [U]:**
  - `alerts/task_health.py`: Telegram alerts for failing scheduled tasks, plus a cross-check for a task that stops
    running.
  - `scripts/backup.py`: a verified `pg_dump` plus the ledger and alert state files, keeping the newest 14. First
    run 2026-10-05.

## Decisions needed from the maintainer

1. **Formally approve the sanctioned handlers:** H12 as widened to `ValueError` here, and H6 and H8–H11, which are
   still marked "awaiting formal approval" in AGENT.md.
2. **Sportmonks after 2026-10-16:** subscribe, or name the replacement. Results, settlement and lockbox scoring stop
   without it.
3. **Gate 2 scope:** build card markets, or formally scope Gate 2 to tackles (and fouls).
4. **Gate 2 bin criterion:** keep the PRD's fixed 0.03 bin gap, or adopt the proposed max(0.03, 2.5 × SE) per bin
   plus a Spiegelhalter test. This must be fixed before any lockbox result is scored.
5. **Exposure of alerts that were never delivered:**
   - Every alerted stake counts as open exposure for 4 days, whether or not the message arrived. This is
     conservative and unchanged.
   - Releasing the stake of a delivery the operator dismisses would change staking, so it was not done without a
     decision (AGENT.md A7).
6. **Scheduling the failure alerts and backups:** approve adding `task_health` to the two `.cmd` scripts and a daily
   backup task. Choose an off-machine backup folder (`PROP_ALPHA_BACKUP_DIR`); the default folder is on the same
   disk.
7. **Both tasks stopped:** nothing on this machine can detect that the machine is off, asleep or logged out. An
   outside "dead man's switch" monitor would be a new external service.
8. **Bogus player row 954** (`vendor_player_uid` 'None', created before 2026-10-04): repair or leave it. Its 3 rows
   with minutes are quarantined, and no price has used it.
9. **Merge the branch** into main by PR.

## Verification (2026-10-05)

| Command | Result |
|---|---|
| `python -m pytest tests -q -p no:cacheprovider` | `591 passed`, 0 skipped |
| `python -m mypy` | `Success: no issues found in 57 source files` |
| `npx --yes pyright@1.1.406` | `0 errors, 0 warnings, 0 informations` |
| `bash ci/bootstrap.sh --preflight` | `preflight passed` |
| `bash ci/bootstrap.sh --release` without `PROP_ALPHA_CI_DATABASE_URL` | refused: `--release requires PROP_ALPHA_CI_DATABASE_URL` |
| `bash ci/bootstrap.sh --release` with the local `prop_alpha_ci` (PostgreSQL 18.6) | `junit totals: {'tests': 591, 'failures': 0, 'errors': 0, 'skipped': 0}`, mypy and pyright clean, `schema smoke test passed on prop_alpha_ci`, `SUCCESS (release gate)` |
