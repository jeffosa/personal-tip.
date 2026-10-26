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
| Pricing code | digest `839e97ce504eaecd29a7b435fea9980e0a3d3401e648d4cee70a39b1ca63275f` (frozen 2026-10-05; settings are part of the fingerprinted code: game-state k 6 / 1000, tackles multiplier 0.9483, prior strength 6, 30,000 pseudo-minutes) |
| Data source | Sportmonks (`DATA_VENDOR=sportmonks`), database `prop_alpha` |
| Leagues | League One, League Two |
| Markets and lines | tackles over 0.5, 1.5, 2.5; fouls over 0.5, 1.5 (`model_freeze.GATE2_LINES`) |
| Outcome | `tackles_attempted`, `fouls_committed` in `player_match_stats`; a player who did not play (0 minutes or no row) is void and not scored |
| Probability scored | the logged over probability given the player plays: p_win / (p_win + p_lose) |
| Scoring | `evaluation/gate2.py` (PRD Gate 2 thresholds), fixture-clustered bootstrap, seed 20261027 |

**Eligible prediction:** a logged line with
1. the protocol's digest;
2. data vendor `sportmonks` recorded on it;
3. kickoff on or after the start;
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
- `scripts/gate2_prospective.py` reports only counts (eligible, scored, pending, excluded by reason) until every Gate 2 line has at least 500 scored predictions.
- It then computes the Gate 2 metrics once and writes `.prop_alpha_state/gate2_prospective_result.json`; a later run refuses to score again.
- The report gives, per market and line: sample counts, Brier skill with its 90% interval, the calibration slope and intercept, the calibration bins, the worst bin of at least 50, and the closing-market benchmark where at least 200 two-way closing prices exist.
- A missing benchmark or sample is a fail, never a pass.

**What Gate 2 still cannot pass on, whatever the lockbox shows:**
- **`yellow_card`:** a PRD target market the frozen model does not price.
- **The market benchmark:** it needs closing prices, and none has been captured; no bookmaker has been seen quoting League One/Two tackles or fouls.
- **The PRD's "lineups fixed at team-sheet time":** predictions are made on estimated line-ups up to 4 hours before kickoff.
