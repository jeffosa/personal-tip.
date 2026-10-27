# Gate 2 lockbox: audit and prospective protocol

Written 2026-10-25 at the maintainer's instruction ("audit and repair the Gate 2 lockbox evidence process"). Nothing here
changes pricing, staking, settlement or a risk default, and nothing places a bet. The original records are kept unchanged.

## 1. Audit: what was seen, scored or used

| When (UTC) | What | Matches | Outcomes read? | Used for a decision? | Record |
|---|---|---|---|---|---|
| 2026-10-08 01:14 | `scripts/gate2_lockbox.py` | 0 lockbox matches existed | no | no | `.prop_alpha_state/gate2_lockbox_2026-10-08.json` |
| 2026-10-10 13:44 | Real `--send` scan logged 2,715 predictions (digest `839e97ce`, Sportmonks data, lineup basis `estimated`) | 20 of the 24 matches of Sat 10 Oct, kickoff 14:00 | no | no | `.prop_alpha_state/predictions.jsonl` |
| 2026-10-11 10:37 | `scripts/gate2_lockbox.py` repriced and **scored** the lockbox | **24** final matches, all Sat 10 Oct (4 at 11:30, 19 at 14:00, 1 at 14:15) | **yes** | reported; no line passed (tackles slopes 0.90 / 1.08 / 1.25, worst bins 0.052 / 0.059 / 0.022; fouls slopes 0.65 / 1.01, worst bins 0.071 / 0.043; benchmark not judged, 0 closing prices) | `.prop_alpha_state/gate2_lockbox_2026-10-11.json` |
| 2026-10-15 09:59-10:25 | `quick_backtest.py --level` (scored from 2026-07-01), `tune_backtest.py` and `recalibrate.py` (test windows from 2026-09-12) on TheStatsAPI data (`prop_alpha_tsa`) | the open-ended test windows contained the **same 24 matches** (prop_alpha_tsa holds 24 League One/Two finals between 6 and 15 Oct, all on 10 Oct) | **yes** (TheStatsAPI counts) | **yes**: "the frozen settings stand" | `quick_backtest_2026-10-15_level.json`, `tune_backtest_2026-10-15.json`, `recalibration_2026-10-15.json` |
| 2026-10-21 | Maintainer restored `LOCKBOX_START` to 2026-10-06, knowing the 11 Oct results existed | the 24 | (decision) | **yes** | `model_freeze.py` comment, CLAUDE.md |
| 2026-10-21 | Repricing check of the 20 logged matches against their logged probabilities | 20 | no | no | CLAUDE.md "Pricing check" |
| 2026-10-25 04:00 | Results catch-up loaded the later League One/Two matches | 15 Oct (1), 17 Oct (23), 20 Oct (23), 21 Oct (1), 24 Oct (23) | loaded, not analysed | no | database `ingested_at` |

Findings:

- **Examined set:** the 24 League One/Two matches of Saturday 10 October 2026 (Sportmonks ids 19727666-19727677 and
  19728218-19728229). Last examined kickoff: **2026-10-10 14:15 UTC** (19728228). They are ineligible for any go-live
  gate, as are all 2,715 logged predictions (all for those matches).
- **No other League One/Two outcome** after 10 October has been scored, and **no prediction exists** for any later match:
  the Sportmonks plan did not list League One/Two from 12 to 25 October, so no scan ran. A retrospective repricing of
  those matches would not be a prediction made before kickoff, so they cannot be evidence either.
- **Process defects found:**
  1. The scorer repriced matches after the fact instead of scoring the logged predictions (125 of 2,235 differed on
     11 Oct because the history was revised after pricing).
  2. The tuning scripts' test windows are open-ended, so any run after the lockbox start reads lockbox outcomes.
  3. Nothing records the data source on a prediction (the vendor changed twice in October).
  4. A postponed match would never get a second prediction: the log keeps one per fixture and digest.
  5. Outcomes were re-read from the live database on every run, so a vendor revision could change a scored result.
  6. The scorer could be re-run and looked at at any time: nothing stops repeated looks.

## 2. Proposed start date: 2026-10-27 00:00 UTC

- **Later than every examined match** by 16 days and 10 hours (last examined kickoff 2026-10-10 14:15 UTC), and later
  than every outcome ever analysed (the 15 Oct backtests read nothing after 10 Oct for League One/Two).
- **After the decision to restart** (2026-10-25) and after this protocol is documented and implemented (2026-10-25),
  leaving 26 October for the maintainer to review it before the first eligible match. The only League One/Two match
  between the decision and the start (Monday 26 October 20:00) is excluded.
- **Freeze rules (model_freeze.py, AGENT.md):** a pricing change needs a re-freeze and a later start; this is not a pricing
  change. The pricing code, `FROZEN_DIGEST` (`839e97ce...`) and `FROZEN_ON` (2026-10-05) are unchanged; only
  `LOCKBOX_START` moves later, which the freeze test allows (it requires `LOCKBOX_START` after `FROZEN_ON`). It is not a
  re-freeze in response to lockbox results: it removes seen data, it does not select settings. **No conflict.** It
  supersedes the 2026-10-21 restoration, at the maintainer's instruction.

## 3. The protocol (frozen in `prop_alpha/evaluation/lockbox.py`, `PROTOCOL`)

**Frozen before any eligible prediction:**

| Item | Value |
|---|---|
| Start | kickoff on or after 2026-10-27 00:00 UTC |
| End (amendment of 2026-10-26) | kickoff before 2027-06-01 00:00 UTC (stop date 31 May 2027); the lockbox closes 14 days later (2027-06-15) |
| Scoring threshold (amendment of 2026-10-26) | 500 scored predictions from 320 distinct matches on every Gate 2 line |
| Capture (amendment of 2026-10-27, section 6) | re-fetched at the first capture run within 4 h after the grace; later, rebuilt from the last payload fetched at or before the grace; excluded when there is none |
| Pricing code | digest `839e97ce504eaecd29a7b435fea9980e0a3d3401e648d4cee70a39b1ca63275f` (frozen 2026-10-05; settings are part of the fingerprinted code: game-state k 6 / 1000, tackles multiplier 0.9483, prior strength 6, 30,000 pseudo-minutes) |
| Data source | Sportmonks (`DATA_VENDOR=sportmonks`), database `prop_alpha` |
| Leagues | League One, League Two |
| Markets and lines | tackles over 0.5, 1.5, 2.5; fouls over 0.5, 1.5 (`model_freeze.GATE2_LINES`) |
| Outcome | `tackles_attempted`, `fouls_committed` in `player_match_stats`; a player who did not play (0 minutes or no row) is void and not scored |
| Probability scored | the logged over probability given the player plays: p_win / (p_win + p_lose) |
| Scoring | `evaluation/gate2.py` (PRD Gate 2 thresholds; bin criterion: count-weighted mean gap of bins of 50+ within 0.03, since the 2026-10-26 amendment), fixture-clustered bootstrap, seed 20261027 |

**Eligible prediction:** a logged line with
1. the protocol's digest;
2. data vendor `sportmonks` recorded on it;
3. kickoff on or after the start and before the end;
4. not one of the examined matches;
5. logged strictly before kickoff;
6. a protocol market and line;
7. a logged kickoff within 15 minutes of the match's stored kickoff.

**Edge cases:**
- **Duplicates:** for each (match, player, market, line) only the earliest logged line counts; later ones are reported as duplicates.
- **Postponed or rescheduled match:** its earlier prediction is ineligible (kickoff moved). The log now writes a new prediction when the same match is scanned with a new kickoff, and that one is eligible.
- **Missing outcomes:** a match not final in the database, or inside the revision grace, is pending. A match still not final 14 days after kickoff is reported as missing. Neither is scored.
- **Revised vendor data:** each player's outcome is captured once, 72 hours after kickoff (the revision grace), into the append-only `.prop_alpha_state/lockbox_outcomes.jsonl`. Later database revisions are reported, never applied. (Operations, 2026-10-26, not a protocol change: the 4-hourly task captures with `gate2_prospective.py --capture-only`, so a capture happens within about 4 hours of the grace while the machine is on; it never scores.)
- **Model or data changes:** a different pricing digest or data vendor makes a prediction ineligible. Any pricing change, or a change of data source, ends this lockbox and needs a new protocol with a later start. `tests/test_lockbox.py` fails while `PROTOCOL` and `model_freeze` disagree, and the scorer refuses to run on unfrozen code.

**Keeping the lockbox away from tuning:**
- `quick_backtest.py` (League One/Two), `tune_backtest.py` and `recalibrate.py` never score a match kicking off on or after the start (`lockbox.tuning_cutoff`).
- The old `gate2_lockbox.py` is retired: it refuses to run, and its records are kept.

**Scored once:**
- `scripts/gate2_prospective.py` reports only counts (eligible, scored, pending, excluded by reason) until every Gate 2 line has at least 500 scored predictions from at least 320 distinct matches.
- It then computes the Gate 2 metrics once and writes `.prop_alpha_state/gate2_prospective_result.json`; a later run refuses to score again.
- The report gives, per market and line:
  - sample counts;
  - Brier skill with its 90% interval;
  - the calibration slope and intercept;
  - the calibration bins, their count-weighted mean gap (the criterion) and the worst bin of at least 50 (reported, not judged);
  - the closing-market benchmark, where at least 200 two-way closing prices exist.
- A missing benchmark or sample is a fail, never a pass.
- **Expiry:** a lockbox still short of the threshold when it closes (2027-06-15: the end plus the 14 days for missing outcomes) is NOT PASSED. The scorer records that once in the same result file, without computing any metric, and the lockbox is never scored.

## 4. Amendment of 2026-10-26: calibration rule and scoring threshold

Decided by the maintainer on 2026-10-26, before the start and before any lockbox prediction or outcome existed. It was Option 3 of `GATE2_CALIBRATION_DECISION_MEMO.md`, with 320 matches and a stop date of 31 May 2027.

**The calibration rule:**
- **Why it changed.** The PRD's bin rule, "every bin of 50+ within 0.03", almost never passes a perfectly calibrated model. Two simulations found this at the original trigger of about 21 matches, and at 320 matches. The slope rule cannot pass a good model at the original trigger either.
- **New rule.** The PRD's bin criterion is now the count-weighted mean gap of bins of 50+, at most 0.03.

**Unchanged:**
- the bins and the 0.03 tolerance;
- the slope range and the skill rule;
- the pricing code and `FROZEN_DIGEST`;
- the start and the eligibility rules;
- the single look.

**The scoring threshold:**
- The single look waits for 500 scored predictions from 320 matches on every line.
- Only kickoffs before 2027-06-01 count.

**Operating characteristics** (memo, sections 4.5 and 5): at 320 matches this rule plus the slope rule passes a perfectly calibrated model about 0.71–0.90 of the time, depending on dependence within matches. It passes:
- errors inside the tolerance 0.35–0.82 of the time;
- errors above it at most 0.11;
- clearly wrong slopes never.

**Implementation:**
- `evaluation/gate2.mean_bin_gap`;
- the `PROTOCOL` fields `end`, `min_scored_fixtures_per_line` and `amended_on`;
- `lockbox.ready_to_score`, `closed` and `expired`;
- `tests/test_gate2_calibration_rule.py`.

**What Gate 2 still cannot pass on, whatever the lockbox shows:**
- **`yellow_card`:** a PRD target market the frozen model does not price.
- **The market benchmark:** it needs closing prices, and none has been captured; no bookmaker has been seen quoting League One/Two tackles or fouls.
- **The PRD's "lineups fixed at team-sheet time":** predictions are made on estimated line-ups up to 4 hours before kickoff.

## 5. Late captures: finding (2026-10-27) and the options considered (option D chosen: section 6)

**The rule:** "each player's outcome is captured once, 72 hours after kickoff (the revision grace) … Later database
revisions are reported, never applied."

**What the code does:**
- `gate2_prospective.py --capture-only` runs inside the 4-hourly task, only while the machine is on.
- It copies whatever `player_match_stats` holds at that moment (`lockbox.outcomes_to_capture`), once per (match, player).
- `lockbox.revised` reports later changes against that capture.
- The results job never re-fetches a match already stored as final (`results.missing_fixtures`).
- The stored row keeps no history. Only `raw_payloads` does: one row per distinct vendor payload, with `fetched_at`.

**Findings:**
1. **A late capture can include revisions made after the intended capture time, and nothing recorded that.** The match must be re-ingested between kickoff + 72 h and the capture, or first ingested after kickoff + 72 h. Causes include the machine being off at the 72 h mark (it was silent for 12.4 h on 26-27 Oct), a failing results job, or a manual re-ingestion. The capture then holds the later data.
2. **The grace does not take in vendor corrections, even for an on-time capture.** Nothing re-fetches a stored match, so the captured value is normally the vendor's data at first ingestion, a few hours after full time. A correction the vendor makes within the 72 h never reaches the database unless the match is re-ingested by hand. The grace protects only against database changes after the capture.

**Implemented (reporting only, as the rule authorises: "reported, never applied"):**
- Every capture records `due_at` (kickoff + 72 h) and `data_first_fetched_at` / `data_last_fetched_at` (the match's `raw_payloads` fetch times).
- The scorer prints, and stores with the one-time result, `lockbox.capture_timing`:
  - matches captured more than 4 h (one capture run) after the grace;
  - the latest lateness;
  - matches whose data was first fetched after the grace;
  - matches with a revised payload fetched after it.
- No captured outcome is changed, dropped or re-captured. Eligibility and scoring are unchanged. Tests: `tests/test_lockbox_capture_timing.py`.

**Options considered** (the maintainer chose D on 2026-10-27, with a 4 h window and exclusion when no payload predates the
grace: section 6).
- **A. Keep it, with reporting (today's behaviour).** Late captures are scored as captured, and their timing is reported beside the result.
  - Simple, and no uptime-driven exclusion.
  - But the scored set can hold post-grace data, and the grace never takes in vendor corrections (finding 2).
- **B. Exclude late post-grace data.** An outcome whose data was fetched more than a set tolerance (e.g. 24 h) after the grace is not scored.
  - Strict.
  - But which matches survive depends on when the laptop was on, which can bias the sample. Proportions must be reported.
- **C. As-of reconstruction.** Score each outcome from the latest stored payload fetched at or before the grace (`raw_payloads` keeps every distinct payload; the adapter is pure). When no payload predates the grace, exclude it, or take the first one after.
  - The most faithful to "the vendor's data 72 h after kickoff".
  - Needs code to parse stored payloads through the adapter.
- **D. A re-fetch at the grace, plus C for late runs.** Re-fetch each lockbox match at its first capture run after the grace and capture from that fresh data. This makes the grace do what it says (finding 2).
  - A late run still sees later data, so pair it with C, or with A plus reporting.
  - Spends vendor requests: about one per match.

Whichever is chosen, pre-register three things before the first capture:
- the tolerance;
- what happens when no payload predates the grace;
- that the sensitivity of the Gate 2 result to excluding late captures is reported beside it, without changing the result.

**Evidence to collect by hand:**
- Keep the machine on through every 72 h mark after a lockbox matchday. `scheduler_gaps.jsonl` records when it was silent, and the status report's coverage line shows outcomes due against those captured.
- Any manual re-ingestion of a lockbox match (backfill, repair, the Starter-plan catch-ups): note the date, the matches and the reason in this file before doing it. It changes `raw_payloads` and the stored counts, and it becomes "revised after due" for any capture still to come.
- Keep the daily database dump (the off-host copy currently alters the state files: see the 2026-10-26 audit). `raw_payloads` is the only history from which option C could reconstruct an outcome.


## 6. Capture amendment of 2026-10-27: option D (in force)

**Decided by the maintainer on 2026-10-27 at 10:38 UTC** (`PROTOCOL.capture_policy_decided_at`):
- after the lockbox start;
- before any capture could be due (the earliest is 2026-10-30 00:00 UTC, start plus the grace; the protocol refuses a later decision time);
- before any outcome existed.

It changes how an outcome is captured. The predictions that count, the start, the end and the scoring rules are unchanged.

**The rule:**
- **On time:** at a match's first capture run within 4 hours after the 72 h grace (`PROTOCOL.refetch_window`), the match is re-fetched from the vendor through the ingestion engine (the results job's own write path), and its outcome is captured from that fresh data. Basis: "refetched at the grace".
- **Re-fetch failed:** nothing is captured that run. The next run is then past the window and rebuilds.
- **Late:** a capture run after the window rebuilds the outcome from the last stored payload fetched at or before the grace (`raw_payloads`, parsed as at ingestion). Basis: "rebuilt from the last payload fetched before the grace". Data fetched after the grace is never captured.
- **No early data:** a late capture with no payload fetched at or before the grace is recorded as excluded, once. It is never scored or retried, and is counted in the report (state "excluded (captured late with no data fetched before the grace)").

**Unchanged:**
- each (match, player) is captured once;
- revisions after capture are reported, never applied;
- `--dry-run` fetches and writes nothing.

**Reported** (`lockbox.capture_timing`, stored with the one-time result): matches re-fetched, rebuilt and excluded; captures more than 4 h after the grace; any captured data fetched after the grace plus 4 h.

**Cost:** one vendor request per match, at its re-fetch. **Tests:** `tests/test_lockbox_capture_policy.py`.

**Evidence to collect by hand:**
- **Keep the machine on mains and awake through every 72 h mark after a lockbox matchday** (Tuesday 19:45 kickoffs fall due on Friday at 19:45; Saturday 15:00 kickoffs on Tuesday at 15:00). A missed window turns every capture into a rebuild from the first-ingestion data, or an exclusion. `scheduler_gaps.jsonl` records the silence.
- **Before any manual re-ingestion of a lockbox match:** note the date, the matches and the reason in this file.
- **Keep the daily database dump.** `raw_payloads` is the only source a rebuild can use.
