# Gate 2 scope: three blockers no amount of lockbox data can close (decision memo)

**Status: DECIDED 2026-10-27 16:43 UTC. The maintainer chose 1c, 2a and 3a (section 6).** Prepared the same afternoon.
The decision came before the first lockbox outcome could be captured (Friday 2026-10-30, from about 19:45 UTC, 72 hours
after the first lockbox kickoffs) and before any lockbox match had kicked off: no lockbox outcome had been captured, scored
or looked at, so the amendment cannot have been chosen with the results in view.

## 1. Where the lockbox stands

- Started 2026-10-27 00:00 UTC. The first 6 matches (League One/Two, 19:45 UTC) have 790 eligible predictions, logged at
  16:00 UTC by the frozen model (`839e97ce`) on Sportmonks data (`gate2_prospective.py --dry-run`, 16:33 UTC).
- It scores once, when every line has 500 scored predictions from 320 matches: at roughly 24-30 League One/Two matches a
  week, about mid-to-late January 2027 if no scan is missed. Kickoffs count until 2027-06-01; short of the threshold it
  closes NOT PASSED on 2027-06-15.
- Coverage: a missed 4-hourly scan is caught up when the laptop wakes (the task runs on wake), so a match is lost only when
  the machine is off for its whole last 4 hours before kickoff.

## 2. The three blockers

`LOCKBOX_PROTOCOL.md` section 3: "A missing benchmark or sample is a fail, never a pass." Today each of these makes Gate 2
fail whatever the model's accuracy.

### B1. Market benchmark (PRD Gate 2 criterion 4): no market exists

- It needs 200+ two-way, de-vigged closing prices per market, to test whether the model adds information beyond the market.
- No bookmaker has been seen quoting League One/Two player tackles or fouls: OddsPapi probes of 2026-10-08 and 2026-10-26,
  the 4-hourly scans (tonight: bet365, Sky Bet, William Hill, 0 prices), and LiveScore Bet on 2026-10-04.
- So the League One/Two benchmark cannot be computed, and Gate 2 fails on it. The same fact means a League One/Two tip can
  never be sent: there is no price to bet on.
- Where markets do exist (bet365 and Betway "k+" ladders in Brazil Serie A and the Championship), the model is the pilot's,
  which has no freeze or lockbox. The ladders are one-sided, so only the 0.5 line can be de-vigged (`GATE3_DECISION_MEMO.md`
  D4), and closing prices need the machine on at kickoff.

### B2. `yellow_card` (criterion 1): the model does not price it

- The PRD requires 500 predictions per target market: tackles, fouls and yellow_card.
- The frozen model prices tackles and fouls only (`slate.SCAN_MARKETS`; cards need referee propensities).
- Adding cards changes frozen pricing code (`model_freeze.PRICING_CODE`), so `FROZEN_DIGEST` changes and the lockbox must
  restart for every market.

### B3. Line-ups (protocol): estimated, not team sheets

- The PRD protocol says "Lineups are fixed at team-sheet time."
- The lockbox logs at the 4-hourly scan, up to 4 hours before kickoff, on estimated line-ups (`lineup_basis: estimated`).
  The T-60 team sheets are captured (`lineup_capture`), but no prediction is made from them.
- Tips are issued at that same scan, before team sheets exist. So the estimated-line-up prediction is the one a bet would
  rest on.

## 3. Options

### B1 (benchmark)

- **1a. Keep as written.** The League One/Two lockbox still measures skill and calibration, but Gate 2 is recorded as
  failed on the benchmark. Honest, but the gate can never pass.
- **1b. "Not applicable where no market exists."** Gate 2 could then pass on skill and calibration alone. Not recommended:
  a pass would say nothing about beating the market, and with no market there is nothing to bet.
- **1c. Keep the League One/Two lockbox unchanged as the calibration test, and pre-register a second, market lockbox where
  prices exist** (Brazil Serie A: Betway quotes every fixture seen, bet365 some; possibly the Championship). This needs
  the pilot's settings frozen (a digest like `FROZEN_DIGEST`), its own start date, and closing-price capture running at
  kickoff (the always-on server). The benchmark would be judged there. Recommended: decide the principle now, build after
  the server is running.

### B2 (cards)

- **2a. Amend the target markets to tackles and fouls.** Cards are neither priced nor tipped; a card model can come later
  with its own freeze and lockbox. Recommended: the lockbox keeps running untouched.
- **2b. Build a card model now.** The lockbox restarts for every market, with today's 790 predictions lost.

### B3 (line-ups)

- **3a. Amend the protocol to "estimated line-ups at the scan, up to 4 hours before kickoff".** This tests the
  prediction a tip actually rests on. Recommended. Team-sheet predictions can be added later as a separate diagnostic.
- **3b. Add a T-60 prediction from the confirmed team sheets and score that stream.** New logging path; the stream would
  start late (every match it misses is lost), and it tests a prediction no tip uses.

## 4. Recommendation

1c, 2a and 3a.
- The League One/Two lockbox continues exactly as it is: same model, same start, same 500 predictions from 320 matches.
- Gate 2's scope is written down before any outcome is seen: tackles and fouls; estimated line-ups at the scan; the
  benchmark judged on a second, pre-registered lockbox in a league that has a market.
- The second lockbox waits for the always-on server, because closing prices must be captured at kickoff.

## 5. What does not change

`FROZEN_DIGEST`, the pricing code, the lockbox start, eligibility, the 500 / 320 threshold, the calibration rule, the end
date and the expiry rule. Nothing here is scored or looked at.

## 6. Decision record (maintainer)

2026-10-27 16:43 UTC, by the maintainer's answers in the session (the three options marked "Rec."):
- **1c.** The League One/Two lockbox continues unchanged as the skill and calibration test. The market benchmark is judged
  on a separate, pre-registered market lockbox in a league with prices (Brazil Serie A the candidate), built after the
  always-on server; until it exists and passes, Gate 2 is NOT PASSED.
- **2a.** Gate 2's target markets are tackles and fouls. yellow_card is out of scope; it would get its own freeze and lockbox.
- **3a.** Gate 2 predictions are the frozen model's, on line-ups estimated at the 4-hourly scan, up to 4 hours before
  kickoff: the prediction a tip rests on.

Implemented in `scripts/gate2_prospective.py`:
- `UNPRICED_TARGET_MARKETS` is empty.
- A missing benchmark blocker names the market lockbox.
- The result adds `skill_and_calibration_passed`, this lockbox's own verdict.
- Gate 2's `passed` still needs every blocker cleared.
- Tests: `tests/test_lockbox.py`.

Recorded in the PRD (Gate 2) and `LOCKBOX_PROTOCOL.md` section 7. Unchanged: `FROZEN_DIGEST`, the pricing code, the start,
eligibility, the 500 / 320 threshold, the calibration rule, the end date and the expiry rule.
