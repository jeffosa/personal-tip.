# prop_alpha production readiness: remediation status (2026-10-05)

**Position for review: paper evaluation only.** No PRD go-live gate has been passed. Nothing in this build is
evidence that real-money betting is safe, and nothing here should be read as a claim that it is. Bets are placed by
hand by the maintainer after a Telegram alert; no component places bets (AGENT.md A5).

Target: about 7/10, meaning credible for a controlled paper launch and limited review. Items 3–6 below are code and CI
work and are done in this change. Items 1, 2 and 7 need independent evidence (lockbox outcomes, bookmaker
settlements, closing prices) that code cannot produce. They take calendar time and real market data, and a defensible
7/10 depends on them.

Tags as in AGENT.md: **[T]** tested in this change · **[U]** implemented, not executed where it will run · **[N]** not built.

## Status on 2026-10-26: Gate 3 settlement integrity enforced in the evidence report (read this first)

**Still PAPER ONLY; no gate passed.**

**The gap.** `scripts/value_evidence.py` printed the reconciliation separately, while `evaluation/value_evidence.py` set
`value_shown` from the sample, the CLV interval and the power check alone. A sample with positive CLV could therefore have
been reported as value shown with a failed reconciliation. Also, a single market or line could have triggered it, while
Gate 3 is judged over all paper bets.

**Now [T]:**
- **Classification** (`value_evidence.settlement_integrity`, PRD Gate 3 criterion 4). Each `replies.reconcile` outcome is
  classified:

  | Outcome | Counts as |
  |---|---|
  | `agree` | reconciled |
  | `discrepancy`, `other_book_discrepancy`, any manual override (`Reconciliation.overridden`, whatever the outcome) | **failed** |
  | `unverifiable` (no gradable statistics), `unconfirmed` (automatic result only), `other_book_agree` (only another bookmaker's result) | **incomplete** |

  An unclassified outcome raises. Status: "failed" if any bet failed; else "incomplete" if any bet is incomplete or there
  is no bet; "passed" only when every bet reconciled.
- **Population.** Every settled paper bet of the Gate 3 population is checked: League One/Two ledger, lockbox match,
  placed before kickoff. That includes bets left out of the CLV sample (automatic result only, another book's result, a
  void). Pilot bets are not in it. The report also shows each ledger's integrity over all its settled bets, for
  information.
- **CLV result kept distinct.** The tips' statistics are now `clv_criteria_met` (sample, CLV lower bound above 0, power
  check: statistical only, never a pass on its own). `gate3_result` adds settlement integrity and gives Gate 3 "passed"
  only when all four criteria hold. It gives "failed" when integrity failed, or on the full sample when the CLV interval
  lies below 0 (or, with the power met, does not lie above 0); otherwise "incomplete". It is computed over all tips
  together (`tips["all"]`).
- **Verdict.** "value shown" now needs a verified contract **and** Gate 3 passed.
- **Today:** Gate 3 INCOMPLETE. 0 settled League One/Two paper bets; the pilot's 10 are all incomplete (5 another book's
  result, 5 automatic only).

No model, pricing, lockbox, settlement rule, risk default or PRD threshold changed.

## Status on 2026-10-25 (d): closing-price trail reviewed

**Still PAPER ONLY; no gate passed; real-price tips stay withheld.**

**What the closing-price trail had produced:**
- League One/Two: one capture, 10 Oct: 20 matches, 0 prices, because no bookmaker quotes those markets.
- Pilot: never ran in a kickoff window. The 10-minute poll ran on only 1-29% of each day since 5 Oct, not at all on 17, 19,
  20, 23 and 24 Oct, and all three tipped kickoffs (11 Oct 20:30, 12 Oct 22:30, 13 Oct 00:00 UTC) fell in gaps in the log.
- The CLV formulas are the PRD's (`closing_line_value`: odds taken x de-vigged close - 1).

| Weak point | Change [T] |
|---|---|
| With tips withheld, the pilot capture (tipped selections only) would never run again, and League One/Two picks were not kept | Withheld picks are kept unsent as **shadow picks** (`alerts/shadow_picks.py`); the pilot capture prices them; `scripts/value_evidence.py` reports their same-book closing value (price and de-vigged) with fixture-clustered 90% intervals; reported, never judged, not Gate 3 evidence |
| CLV fell back to another bookmaker's close | Only the bet's own bookmaker's close is used (`ledger.closing_value`, `pilot_ledger.closing_price`), as the PRD's ledger writer specifies |
| A match missed while the machine was off left no trace | The 4-hourly status report has a **lockbox coverage** line: matches kicked off, how many had a prediction logged before kickoff (missed ones flagged as WARNING), how many had a closing capture and prices (`lockbox.coverage`) |

**Still open:**
- **Uptime.** An always-on host is needed: Oracle chosen 2026-10-25, sign-up in progress; `deploy/VPS_SETUP.md` not yet run
  [U]. Until then, keep the laptop on mains power and awake through matchdays.
- **One-sided ladders.** The "k+" ladders can be de-vigged only at the 0.5 line, so PRD CLV is available only there; the
  shadow report shows the price value beside it.
- **Nothing to capture for League One/Two** until a bookmaker quotes those markets (odds probe, 31 Oct).

## Status on 2026-10-25 (c): contract criteria enforced; still no evidence

**Still PAPER ONLY; no go-live gate passed; real-price tips stay withheld.**
- Brazil Serie A tips: every pilot tip is a paper bet (`pilot_ledger.record_tips`) and carries the PILOT label. No execution
  component exists, and every real-price tip is now withheld by the contract gate.
- No pricing, risk default or settlement grading changed; `FROZEN_DIGEST` is unchanged.
- Nothing in the pilot results, backtests, screen replay, edge calibration or model-v-book comparisons is evidence of an edge.
  The edge calibration found the model's favoured prices mostly the model's errors.

| Requirement | State | Evidence |
|---|---|---|
| First-hand rules per book and market | **Not met.** bet365: none. Betway: its own rules document (v42, dated 10/02/2022, supplied by the maintainer) covers 4 of 5 topics for tackles and 3 of 5 for fouls, and says players must start, which is **not** the void rule priced (`MARKET_CONTRACT.md` section 12) | bet365 help pages answer 403; betway.com is refused by the browser pane; Betway's regional pages render no text (`MARKET_CONTRACT.md` section 11) |
| 20+ same-book settlements per market, discriminating, a substitute, a non-starter | **0 of 20** for every pair | `scripts/contract_evidence.py` on 2026-10-25 |
| Reconciliation | 0 same-book results; 5 SportyBet results on bet365 tips are another book's, never counted | `pilot_ledger --reconcile` |
| Contract listed | none (`VERIFIED_CONTRACTS` empty); a listed contract now fails CI without evidence meeting every criterion [T] | `tests/test_market_contract.py` |
| Model statistic and void rule match the contract | cannot be judged without a contract. The gate now requires the contract's count and void rule to equal the ones priced [T] | `market_contract.contract_verified` |
| Prospective predictions, same-book outcomes, two-way closing prices | 0, 0, 0 | `scripts/value_evidence.py` |
| Calibration, Brier skill, benchmark, ROI, CLV per market and line | **not judged**: no rows. ROI and CLV are now also reported per line [T] | `evaluation/value_evidence.py` |

**Changes in this pass** (tests fail without them):
- `market_contract.Contract`, `evidence_failures` and `expected_result`; the gate checks the priced count.
- `contract_evidence/` (README, TEMPLATE).
- `scripts/contract_evidence.py`: same-book results only; a pilot result must name its bookmaker.
- Tips' ROI and CLV are reported per line.

**Gates that could not be completed, and why:** first-hand rules (blocked from here; the maintainer's paste or a support
reply is needed); same-book settlements (none exist; they need bets placed by hand or results read in a bookmaker account);
every value metric (the lockbox opens 2026-10-27 and no closing price or same-book outcome exists).

## Status on 2026-10-25 (b): settlement rules and model value audited

**Still PAPER ONLY; no go-live gate passed. Real-price tipping is now disabled** until a bookmaker's settlement contract is
verified (`alerts/market_contract.py`). No pricing, staking, risk default or settlement grading changed; `FROZEN_DIGEST` is
unchanged, so the lockbox protocol below is unaffected. Details: `MARKET_CONTRACT.md` sections 7-10.

**How a pick becomes a tip (traced in the code, 2026-10-25):**

| Step | Where | What it does | Verified? |
|---|---|---|---|
| Model probability | frozen `slate.build_projections` (League One/Two) or `ladder_scan.price_pilot`; `FactorProjection.side_probabilities` | P(win), P(lose), P(void) of each line from the player's Sportmonks `tackles_attempted` / `fouls_committed` history, under the quote's void rule | Statistic = Opta "Tackles" on one Brazil match (29/29) and 8 tips; never compared on a League One/Two match |
| Bookmaker price | OddsPapi, `slate.quotes_from_prices` | Over/under lines, or "k+" ladder steps read as Over k-0.5; every real quote is given `VOID_IF_NO_APPEARANCE` | The void rule is an assumption; bet365 (lead) voids non-starters |
| De-vigging | `market_scanner.devig_two_way` | Only when both sides are quoted; ladder steps are one-sided, so the screen compares with 1/odds (margin included) | Correct as built; one-sided prices cannot be de-vigged |
| Other-book check (pilot) | `ladder_scan.beating_other_books` | Needs another book at least 10% shorter (median) | A judgement, not fitted |
| Edge guard (pilot) | `ladder_scan.without_implausible_edges` | Drops prices where the model's return if he plays exceeds 25% | A judgement, not fitted |
| Allocator | `InstitutionalAssetManager.allocate` | Joint mean-variance, caps net of exposure, 0.03 loss floor | Risk defaults unchanged |
| High-value screen | `value_screen.assess_high_value` (in `notify_digest`) | Mispriced, EV >= 4%, survival > 60% | Survival uses the model's own posterior |
| **Contract gate (new)** | `market_contract.withhold_unverified` in `scan.run_scan` and `ladder_scan._scan_slate` | A real-price directive is sent only for a `(book, market)` with a verified contract and the same void rule; the summary counts what was withheld and says the estimates are unverified | `VERIFIED_CONTRACTS` is empty: nothing is sent; mock runs are not gated |
| Prediction log | `PredictionLog.record` per fixture, before any send | Gate 2 lines with digest, data source and time | Lockbox-eligible only from 2026-10-27 |

**Evidence measured today (`scripts/value_evidence.py`, report `.prop_alpha_state/analysis/value_evidence_2026-10-25.json`):**

| Evidence | Count | Reading |
|---|---|---|
| Bookmaker rules read first-hand | bet365 0 (help.bet365.com blocked: 403 and pane refusal); Sky Bet's Opta definitions read in full | Contract **unverified** for bet365 and Betway |
| Settled paper bets | 10 (pilot, bet365 prices, 4 matches) | At bet365: **0 agree, 0 discrepancies, 10 unresolved**; SportyBet results on 5: 5 agree (another book, not counted); 5 automatic only |
| Prospective predictions eligible as evidence | **0** (2,715 League One/Two lines all for examined matches; 2,190 pilot lines not evidence: no frozen pricing or protocol, matches inspected on 2026-10-25) | No calibration, Brier skill or benchmark can be computed |
| Bookmaker-settled outcomes for eligible predictions | 0 | n/a |
| Captured closing prices | 0 (`closing_quotes.jsonl` empty; no pilot closing price) | No CLV, no market benchmark |
| Tips settled by their own book | 0 | ROI and CLV not computable; Gate 3 needs 300 bets in 60 fixtures with the CLV 90% lower bound above 0 |

**Defects fixed (with tests that fail without the fix):**
- `--reconcile` counted another bookmaker's result as agreement with the tipped book (the 5 SportyBet results on bet365 tips
  were reported "5 AGREE"). A manual settlement now carries its bookmaker (`Settlement.book`; older records are read from
  their detail), and `replies.reconcile` reports another book's result as `other_book_agree` / `other_book_discrepancy`, never as
  agreement; either discrepancy fails the run (exit 1). `tests/test_settlement_reconciliation.py`, `tests/test_pilot_ledger.py`.
- Real-price tips were sent (pilot: Brazil Serie A since 2026-10-15) although the statistic and void rule the bookmaker settles
  on are not established. The contract gate withholds them; Brazil Serie A stays in `TIP_LEAGUES`, so its prices are still
  priced, screened, logged and counted. `tests/test_market_contract.py`, `tests/test_ladder_scan.py`.
- New, pure: `evaluation/value_evidence.py` (outcomes only from the tipped book's own settlement, de-vigged two-way closing
  prices, calibration, Brier skill with a fixture-clustered 90% interval, ROI and CLV with fixture-clustered intervals, the Gate 3
  power check, no metric below 30 rows, no judgement below the PRD samples). `tests/test_value_evidence.py`.

**Not changed, on purpose:** the void rule priced and graded (`VOID_IF_NO_APPEARANCE`; bet365's non-starter rule is only a lead,
and changing it changes pricing and settlement semantics: AGENT.md A7, and a pricing change needs a new freeze and a later
lockbox start); the model; the risk defaults; the pilot's other-book margin and edge cap (no evaluation window to judge them on
that is not already inspected).

**To pass the gates, still needed:**
1. **Settlement contract (blocks all tipping and Gate 3 criterion 4):** for bet365 and Betway, the rules first-hand (maintainer
   pastes the pages, or bet365 support's written reply, `MARKET_CONTRACT.md` section 5), then at least 20 own-book settlements per
   market, including discriminating legs (tip 10, Wallace Yan 1+: 1 tackle attempted, 0 won), a substitute and a non-starter, all
   AGREE (section 10).
2. **Gate 2:** 500 scored lockbox predictions per line from 2026-10-27; 200+ two-way closing prices per market for the benchmark;
   `yellow_card`; team-sheet line-ups.
3. **Gate 3:** 300 paper bets settled by their own book across 60 fixtures, CLV 90% lower bound above 0 and the power check met,
   100% reconciliation with no overrides. None exists: no League One/Two tackles or fouls price has been seen at any bookmaker,
   and tips are disabled until (1).
4. **Statistic check on League One/Two:** compare Sportmonks with an Opta-based source for at least one League One/Two match (by hand, where one publishes player counts).

## Status on 2026-10-25: Gate 2 lockbox audited and restarted

**Still PAPER ONLY; no go-live gate passed.** At the maintainer's instruction the Gate 2 evidence process was audited and
repaired (`LOCKBOX_PROTOCOL.md`). No pricing, staking, settlement or risk setting changed; `FROZEN_DIGEST` is unchanged.

| Item | State on 2026-10-25 |
|---|---|
| Examined matches | The 24 League One/Two matches of 2026-10-10 were scored on 2026-10-11 (`gate2_lockbox.py`) and fell inside the 2026-10-15 backtest, tune and recalibration windows. They and all 2,715 logged predictions (all for those matches) are **ineligible**. No other League One/Two outcome was scored, and no prediction exists for any later match. |
| New lockbox start | **2026-10-27 00:00 UTC** (`model_freeze.LOCKBOX_START` = `evaluation/lockbox.PROTOCOL.start`): after the last examined kickoff (2026-10-10 14:15 UTC) and after the restart decision (2026-10-25). Not a re-freeze; no conflict with the freeze rules. |
| Protocol [T] | Frozen in `evaluation/lockbox.py`: digest, data source (Sportmonks), leagues, Gate 2 lines, outcome columns, eligibility (digest, data source, start, examined matches, logged before kickoff, protocol line, kickoff within 15 minutes of the stored one, earliest of duplicates), outcomes captured once 72 h after kickoff (revisions reported, not applied), missing after 14 days, players who did not play void. The logged probability is scored, never a re-price. |
| Scoring [T] | `scripts/gate2_prospective.py`: counts only until every Gate 2 line has 500 scored predictions; then every metric once (skill and 90% interval, slope and intercept, bins, worst bin, market benchmark where 200+ closing prices exist) into `gate2_prospective_result.json`, never re-scored. A missing benchmark or sample is a fail. `gate2_lockbox.py` is retired (refuses to run; records kept). |
| Lockbox kept from tuning [T] | `quick_backtest.py` (League One/Two), `tune_backtest.py` and `recalibrate.py` never score a match from the lockbox start. |
| Predictions [T] | Every line now records its data source (`data_vendor`); a match whose kickoff moves is logged again when next scanned. |
| Today's count | 0 eligible predictions (`gate2_prospective.py --dry-run`: 2,715 not eligible, all "match examined before the restart"). |

**Still open (Gate 2 cannot pass until each is closed):** 500 scored predictions per line from 2026-10-27 (calendar time,
the laptop on and the scheduled scans running); the market benchmark (no closing price captured; no bookmaker has been seen
quoting League One/Two tackles or fouls); `yellow_card`, a PRD target market the model does not price (build it or a
maintainer decision on scope); line-ups are estimated, not team-sheet based, a PRD protocol deviation. Settlement
verification and Gate 3 are unchanged from the 2026-10-15 table below.

## Status on 2026-10-15 (history)

**Still PAPER ONLY. Settlement and model validation are open (High).** No PRD go-live gate has been passed, and none can be
passed by code: each needs evidence that only calendar time, bookmaker results or the maintainer can supply. Measured today:

| Gate evidence | State on 2026-10-15 | What closes it, and who |
|---|---|---|
| Gate 2 lockbox (League One/Two, frozen model) | **0 lockbox predictions.** The lockbox restarted on 2026-10-17 with the move to TheStatsAPI (`model_freeze.LOCKBOX_START`); all 2,715 logged predictions are earlier and do not count. The phase 3 backtests on TheStatsAPI data confirmed the frozen settings, so FROZEN_DIGEST is unchanged and no re-freeze moved the start again. | Time: the first countable matches are Saturday 17 Oct. At roughly 20 scored starters a match (an estimate from the backtests), 500 per market needs about two full rounds; the calibration criteria may need more. **Blocker (maintainer): TheStatsAPI's trial ends 2026-10-20.** Without results ingestion after that, no lockbox match can be scored. |
| Settlement contract (`MARKET_CONTRACT.md`) | **Unverified.** bet365's rules pages cannot be opened from here (403). The only settled bookmaker evidence is two SportyBet legs (section 2e), which support plain tackles at that book only. | Maintainer: read the bet365 pages named in `MARKET_CONTRACT.md` section 1 and fill its section 3 table; report real bookmaker results (`settled <id> won|lost|void` for real bets, `pilot_ledger --record <id> won|lost|void` for paper bets). |
| Settlement reconciliation (Gate 3, criterion 4) | **0 bookmaker results recorded** in either ledger (`ledger --reconcile` and `pilot_ledger --reconcile`: nothing verified). The 10 pilot paper bets were stranded on Sportmonks ids after the vendor move; `scripts/pilot_settle_by_name.py` settled them on 2026-10-15 from TheStatsAPI's counts, matched by club and player name: 1 won, 9 lost, paper profit -88.73 on 95.03 staked, **all provisional** (assumed rules, no bookmaker result). | Needs bookmaker results to compare. Automatic settlement alone cannot verify itself. |
| Paper trading (Gate 3) | **0 paper bets from the frozen model, 0 closing prices.** No bookmaker has quoted a League One/Two tackles or fouls line (OddsPapi, since 2026-10-03), so the frozen model has nothing to paper-trade against. The 10 pilot paper bets are from the unfrozen pilot (8 reconstructed without model numbers) and do not count. | A bookmaker quoting these markets for League One/Two, or a maintainer decision on which markets and books Gate 3 covers. Per-player `bench_sub_probability` is still **[N]**. |
| Scope | Card markets (a PRD target) are not priced. | Maintainer decision: build them or scope Gate 2 to tackles and fouls. |

Changes on 2026-10-15 [T]: `replies.AUTO_BASIS` now says TheStatsAPI stats (it still said Sportmonks after the phase 2 switch);
`scripts/pilot_settle_by_name.py` refuses, rather than voids, any bet not matched to exactly one match and one player
(`tests/test_pilot_settle_by_name.py`).

### Release gate and live path, 2026-10-15 (after the external audit scoring readiness 2.5/10)

- **Release gate [T]:** `bash ci/bootstrap.sh --release` against the disposable `prop_alpha_ci` passed at 11:56 UTC on
  Python 3.14.6: 848 passed, 0 skipped, mypy and pyright clean, schema install, re-apply and upgrade path passed. The tree it
  tested is `main` at `b5e067e` (the Telegram bot commands, committed and pushed to `main` directly during the run) plus the
  then uncommitted source-gap change and status wording below, so that change still has to be committed, merged and seen
  green on GitHub Actions **[U]**.
- **Observed live on TheStatsAPI [T]:** the League One/Two results job (10:16 and 13:00 BST runs: 24 finished, 0 missing);
  the pilot results job recording a match the source has no statistics for (mt_156353671) so it is not requested again; the
  status report and both scan summaries sent (0 matches in those windows); the 10-minute ledger task clean on every run;
  Saturday's lockbox logging path checked in code: the real `--send` scan logs every priced fixture's predictions before and
  regardless of odds (`scan.run_scan`).
- **Defects found and fixed here [T]:** a match with no statistics at the source failed the run that first met it and raised a
  TASK FAILING alert (now recorded and reported, not a failure: `results.ResultsReport.unavailable`); the status footer still
  said bets are settled from Sportmonks.
- **Operational risk seen [T]:** at 13:00 BST the pilot results job failed on a dropped connection to TheStatsAPI (the
  laptop's Wi-Fi). The task retries and alerts, but an always-on host (`deploy/VPS_SETUP.md`, not set up **[U]**) removes the
  cause.
- **Not yet observed on TheStatsAPI [U]:** a real-odds scan with matches in the window, a prediction logged for a lockbox match
  (first: Saturday 17 Oct), a captured T-60 team sheet, a settlement of a real bet, a backup and restore of the `*_tsa`
  databases.

**What closes the remaining gates, and who:** (1) TheStatsAPI access past 2026-10-20: the maintainer (paid plan or another
feed with the five leagues). (2) The lockbox: calendar time from 2026-10-17, no model change in response to its results.
(3) Settlement rules and reconciliation: the maintainer reads the bookmaker rules (`MARKET_CONTRACT.md`) and records real
bookmaker results. (4) Paper trading: a bookmaker quoting League One/Two tackles or fouls, or a maintainer decision on which
markets and books Gate 3 covers. (5) Scope: a maintainer decision on card markets; `bench_sub_probability` changes prices, so
building it re-freezes the model and restarts the lockbox, and must be decided before 2026-10-17 or deferred to a planned
re-freeze. (6) Live path: Saturday's runs, then a restore test of the `*_tsa` databases. (7) Release: commit, pull request,
green Actions.

## Status on 2026-10-10 (history)

**PAPER / SHADOW ONLY.** Deployment, scheduling, backup, restore, monitoring and rollback are in `RUNBOOK.md`.

- **Release gate [T]:** `bash ci/bootstrap.sh --release` against the disposable `prop_alpha_ci` passed on Python 3.14.6 at 20:18 UTC: 728 collected, 728 passed, 0 skipped, mypy and pyright clean, schema install, re-apply and the 003 to 009 upgrade path passed. It ran on the working tree at commit `c0b6768`, before the later edits to this file, `RUNBOOK.md` and the `EXPECTED_TESTS` constant. GitHub Actions for newer commits has not been seen **[U]**.
- **Backups [T]:** the prediction log and the T-60 team-sheet files are now in the daily backup list, with tests that their manifest hashes match and that absent files are skipped. The next scheduled backup is the first to include them on the real state **[U]**; the lineup files do not exist yet.
- **Scheduled tasks:** `Telegram status`, `Bet ledger poll` and `Daily backup` are registered (read-only inventory 2026-10-10) and the last two last returned 0. `Telegram status` returned 1 at 20:00 local because its results step could not reach Sportmonks for four matches.
- **Audit 2026-10-11, High findings:** (1) the machine slept for most of match day: not fixed by code, needs the power settings and task wake settings below (maintainer's decision); (2) the Sportmonks trial: the status report now warns 7 days ahead, the plan decision is the maintainer's; (3) pilot tips now leave a prediction log, a paper-bet ledger, settlement, closing-price capture and a scoring script (the first real settled result is still to come); (4) the market definition is unresolved, but `pilot_ledger --record` and `--reconcile` now exist to test it against bookmaker results.
- **Audit 2026-10-11, Medium and Low findings:** (5) `prop_alpha_pilot` is now dumped by the daily backup (41 MB, verified and pushed like the main dump) and the pilot state files are in it; (6) and (7) are evidence, not code: Gate 2 is not passed (24 lockbox matches, calibration failing) and no League One/Two tackles or fouls price has been seen; (8) the unreviewed import change in `lineup_capture.py` is reverted, and the remote URL `personal-tip..git` is correct (the repository's name ends with a dot); a pull request and the GitHub Actions result still need the maintainer (no `gh` here); (9) `CLAUDE.md` no longer contradicts itself on the trial, the schedule, the task steps and the backup; (10) two unused keys in `.env` and (11) the OddsPapi key printed in a session log need the maintainer (removal and rotation; `.env` is never edited by an assistant); (12) the pilot scan's stale-league exit code now follows `ScanSummary.stale_leagues`, not the wording of a failure line.
- **Evidence still open (blocks any move beyond paper):** the tackles and fouls settlement contract is unverified (`MARKET_CONTRACT.md`); 2,715 logged predictions are unscored; no League One/Two tackles or fouls price, closing price, paper bet or T-60 sheet has been seen; the Sportmonks trial ends 2026-10-16; the pilot-leagues scan (Brazil Serie A, Scottish Premiership, Scottish Championship; only Brazil Serie A is tipped) has no freeze, lockbox or settlement evidence.

## Second audit 2026-10-11: code fixes (maintainer chose all code fixes, urgent first)

- **Schedule [T]:** `Telegram status` now runs at 00/04/08/12/16/20 UTC, aligned with the scan's UTC windows; a fixture is logged once per pricing digest; tests cover every kickoff being scanned exactly once across the clock change and a late catch-up run.
- **Pilot ledger [T]:** paper-bet recording is idempotent per selection, locked and crash-safe; the settled notice is queued and retried; a failing tip callback stops the send before anything is recorded; reconstructed tips are kept out of the model-scored total.
- **Monitoring [T]:** pilot heartbeat and a pilot section in the status report.
- **Scoring [T]:** `scripts/pilot_score.py` column bug fixed; `scripts/gate2_lockbox.py` compares repriced with logged predictions (2,235 in 20 matches: 125 differ, largest 3.7e-03; 4 matches not logged).
- **Tip layout [T]:** the maintainer's layout (league/market heading, match header with UTC kickoff, pick line, book/minimum/stake line).
- **Maintainer decisions (2026-10-11):** (1) tips stay paper-only until the market settlement rules (`MARKET_CONTRACT.md`) and the model pass the PRD validation gates; (2) Sportmonks continues after the 2026-10-25 trial only if its paid coverage and cost are acceptable; (3) changes go through pull requests and are merged only after GitHub Actions is green, with no more direct pushes to `main`; (4) the scheduled tasks move to an always-on host, but first the Windows-specific scheduling (Task Scheduler, `.cmd` scripts, `run_hidden.py`) and the backup setup are made portable and a restore is tested on that host. Until then the laptop stays plugged in with the tasks set to wake it. Portable runner, systemd units, row-counted backups and a checked restore are built (`deploy/VPS_SETUP.md`); a restore into throwaway databases matched every count on this machine **[T]**; no server has been set up **[U]**.

## Audit rerun on 2026-10-11

**Strict readiness score: 5.8/10**, unchanged from the previous audit. The independent validation evidence is still missing, so the system remains **PAPER / SHADOW ONLY**.

- On the working tree based on `c0b6768`, the full suite collected **728 tests**. On the available bundled Python 3.12.14, **722 passed and 6 failed** because the sandbox denied Python's child-process launch of `git` (`WinError 5: Access is denied`) in the backup-push tests. This is not a clean full-suite result or a finding that the push logic is defective. The three focused backup-copy, manifest-hash and optional-file tests passed.
- `python -m compileall -q prop_alpha tests` completed successfully; `python -m mypy` reported no issues in 81 source files.
- Pyright did not run: the available `pnpm dlx` failed with `EPERM` while resolving the user profile. `ci/bootstrap.sh --preflight` could not start because Git Bash was denied by the execution environment.
- The full `--release` gate was not rerun. Read-only `psql` checks for `prop_alpha_ci` failed with `no password supplied`; the disposable marker and empty-table conditions could not be reverified, so stage 6 was not invoked. No database was reset or modified.
- Scheduled-task state could not be reverified: `Get-ScheduledTask` returned access denied and `schtasks /Query` failed in this session. No tasks were created or changed.
- The earlier Python 3.14.6 release-gate pass recorded above is prior evidence for `c0b6768`; this rerun does not extend it to the current uncommitted documentation/test changes. A clean release-gate run on the final reviewed tree remains required.

## Summary

| # | Item | Status | Blocking a 7/10? |
|---|---|---|---|
| 1 | Locked model evaluation (PRD Gate 2) | Frozen; lockbox restarted 2026-10-17 on TheStatsAPI data; 0 lockbox outcomes (2026-10-15) | Yes: needs ≥ 500 lockbox predictions per market |
| 2 | Settlement verified against each bookmaker | Labelled provisional; confirmed-only total added [T]. Not yet verified: 0 bookmaker results (2026-10-15) | Yes: needs real settled examples |
| 3 | Database checks mandatory in release CI | Done [T] locally; GitHub workflow [U] | No |
| 4 | Type diagnostics resolved and gated in CI | Done [T]: pyright 32 → 0, mypy 29 → 0 | No |
| 5 | Odds-feed failure isolation per bookmaker | Done [T] | No |
| 6 | Alert-delivery recovery | Done [T] | No |
| 7 | Paper-trading gate (PRD Gate 3) | Closing capture built; 0 closing prices, 0 frozen-model paper bets (2026-10-15) | Yes: no tackles/fouls line has been quoted yet |

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
  service (trust authentication, reachable only from the runner, so no password exists to leak). The first 11 GitHub
  runs failed (the last cause: the emptiness check failed on a fresh database, fixed 2026-10-08 in `a1322e6`); runs #12 (`a1322e6`) and #13
  (`636625e`) are green in the maintainer's screenshot of the Actions page (Claude has no GitHub access). The suite now also runs on Linux with Python 3.14.7.
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
  - **Fixed the same day at the maintainer's request (hidden launch, `scripts/run_hidden.py`):** each run opened a
    visible console that took the keyboard focus, and a Ctrl+C or close killed it (`^C` marks in the logs). Both
    tasks, started by the scheduler afterwards, finished with result 0 and wrote their logs. Earlier finding:
    since about 10:51 BST every scheduled run had been killed with exit code
    0xC000013A (console closed or Ctrl+C) before it wrote a log line. No scan, status report or ledger poll ran
    until the fix, and nothing reported it: `task_health` runs inside the killed script, and is not wired in yet. By
    hand the ledger script completes in 7.5 s with exit 0.
- **Gate 1 criterion 7 (lineups):** passes on 50 archived line-ups (f55bb65): 50/50 succeeded, 0 problems in 2,574
  rows. These are post-match line-ups as Sportmonks recorded them, not team sheets captured 60 minutes before
  kickoff, so the live pre-kickoff path is **[U]**.
- **Unknown positions (migration 009):** a missing Sportmonks `position_id` had been stored as the position 'PNone'.
  It is now stored as NULL. 009 was applied to the working database after a backup: 14 match rows and 3 players were
  repaired. The pricing code and its frozen digest are unchanged, and 009 was applied before the first lockbox
  kickoff.
- **Wired and scheduled 2026-10-05 (maintainer approved):**
  - `alerts/task_health.py` is called at the end of all three scheduled scripts with the names of their failed
    steps. The ledger poll reported through it at 17:01 UTC ("ok"); the 4-hourly task's first report is **[U]**
    until its next run.
  - `scripts/backup.py` runs daily at 03:30 (task `prop_alpha\Daily backup`, `scripts/backup.cmd`), then
    `scripts/backup_push.py` pushes the newest backup to a private GitHub repository (the maintainer's choice for the
    off-machine copy). End-to-end run at 17:07 UTC: the backup succeeded (25 MB dump); the push failed as designed
    because `PROP_ALPHA_BACKUP_GIT_REMOTE` is not set yet. **Backups are still on this disk only** until the
    maintainer creates the repository and sets the remote; until then the backup task fails daily and alerts.

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
6. **Off-machine backups (approved 2026-10-05: GitHub; one action left):** create an empty **private** repository
   (e.g. `prop_alpha-backups`) and set `PROP_ALPHA_BACKUP_GIT_REMOTE=https://github.com/<owner>/<repo>.git` in
   `.env`. The push refuses a public repository.
7. **Both tasks stopped:** nothing on this machine can detect that the machine is off, asleep or logged out. An
   outside "dead man's switch" monitor would be a new external service.
8. **Bogus player row 954** (`vendor_player_uid` 'None', created before 2026-10-04): repair or leave it. Its 3 rows
   with minutes are quarantined, and no price has used it.
9. **Merge the branch** into main by PR.

## Verification (2026-10-05)

**Historical.** The results below are the 2026-10-05 baseline of 591 tests, kept as a record of that day. The current baseline is `EXPECTED_TESTS` in `ci/bootstrap.sh` (703 on 2026-10-08, release gate passed locally and reported green on GitHub); see CLAUDE.md for the current status.

| Command | Result |
|---|---|
| `python -m pytest tests -q -p no:cacheprovider` | `591 passed`, 0 skipped |
| `python -m mypy` | `Success: no issues found in 57 source files` |
| `npx --yes pyright@1.1.406` | `0 errors, 0 warnings, 0 informations` |
| `bash ci/bootstrap.sh --preflight` | `preflight passed` |
| `bash ci/bootstrap.sh --release` without `PROP_ALPHA_CI_DATABASE_URL` | refused: `--release requires PROP_ALPHA_CI_DATABASE_URL` |
| `bash ci/bootstrap.sh --release` with the local `prop_alpha_ci` (PostgreSQL 18.6) | `junit totals: {'tests': 591, 'failures': 0, 'errors': 0, 'skipped': 0}`, mypy and pyright clean, `schema smoke test passed on prop_alpha_ci`, `SUCCESS (release gate)` |
