# Gate 2 calibration criterion: audit and pre-registered decision memo

**Status: DECIDED 2026-10-26. The maintainer chose Option 3, with 320 matches and a stop date of 31 May 2027 (section 7).**
- The decision was made at 20:44 UTC, before the lockbox started (2026-10-27 00:00 UTC) and before any lockbox prediction or outcome existed.
- It is implemented in `evaluation/gate2.py`, `evaluation/lockbox.py` and `scripts/gate2_prospective.py`, with `tests/test_gate2_calibration_rule.py`.
- It is recorded in the PRD (Gate 2 criterion 3) and in `LOCKBOX_PROTOCOL.md` section 4.
- The frozen model, `FROZEN_DIGEST`, the lockbox start, the bins and the 0.03 tolerance are unchanged.
- Sections 1 to 6 below are the analysis as it stood when the decision was made.

Prepared 2026-10-26 (UTC); revision history at the end. Reproduce from the repository root:

```
.venv\Scripts\python.exe -I .prop_alpha_state\analysis\scripts\gate2_calibration_oc.py
.venv\Scripts\python.exe -I .prop_alpha_state\analysis\scripts\gate2_calibration_oc.py --targeted
.venv\Scripts\python.exe -I .prop_alpha_state\analysis\scripts\gate2_exact_marginal_check.py
.venv\Scripts\python.exe -I .prop_alpha_state\analysis\scripts\gate2_exact_calibration_oc.py
```

**Outputs** (in `.prop_alpha_state/analysis/`):
- `gate2_calibration_oc_2026-10-26.json` and `gate2_calibration_oc_combos_2026-10-26.json` (method A, seeds 20261026 and 20261027);
- `gate2_exact_marginal_check_2026-10-26.json` (method B, seed 20261028);
- `gate2_exact_calibration_oc_2026-10-26.json` (method B, seed 20261029).

None of them read an outcome, opened a database or scored anything.

## 1. What the gate says today (re-verified at `7e06938`)

**PRD Gate 2 criterion 3** (`prop_alpha_PRD_v3.1.md:524`):
- The logistic calibration slope must lie in [0.9, 1.1].
- "Every calibration bin with ≥ 50 predictions must satisfy |mean p̂ − ȳ| ≤ 0.03."

**Code** (`prop_alpha/evaluation/gate2.py`):
- Constants: `MAX_BIN_GAP = 0.03`, `MIN_BIN_PREDICTIONS = 50`, `CALIBRATION_SLOPE_RANGE = (0.9, 1.1)`.
- Bins: 10 equal-width bins on [0, 1] (`backtest.calibration_table`).
- A line passes only if all of these hold:
  - the largest bin gap is ≤ 0.03 (a point estimate, with no allowance for noise);
  - the slope (also a point estimate) lies in range;
  - the fixture-clustered 90% lower bound of Brier skill is > 0.

**Lockbox and scorer:**
- `evaluation/lockbox.PROTOCOL.min_scored_per_line` is 500.
- `scripts/gate2_prospective.py` scores every line once, as soon as every Gate 2 line has 500 scored predictions.
- That result is final: `gate2_prospective_result.json` is written once.

**When the trigger arrives:**
- The 20 League One/Two matches logged on 2026-10-10 give about 481 scored predictions per line: 543 lines, weighted by each player's chance of playing.
- So the trigger arrives after about **21 fixtures**: roughly the first week of the lockbox, if every match is captured. The Sportmonks listing on 25 Oct showed 30 League One/Two fixtures in the next 7 days.

## 2. Audit of the earlier claims

The adversarial audit earlier on 2026-10-26 (its finding C1) made three claims.

| Earlier claim | Verdict | Supported statement |
|---|---|---|
| "Even a perfectly calibrated model would almost never pass … about 1 in 100,000 across all five lines" | **The number is unsupported.** It multiplied per-line pass rates as if the lines were independent, but a player's lines share one count. It also counted players who do not play and ignored dependence within a fixture. | At the trigger, the bin rule passed on all five lines in 0 of 1,000 method-A simulations (95% upper bound 0.4%). It passed in 0 of 2,000 method-B simulations under each dependence setting (upper bound 0.19%). |
| Per-line pass rates "0.04, 0.07, 0.40, 0.05, 0.17" at about 540 per line | **Preliminary.** These used 543 lines rather than about 481 scored, and no dependence. | 0.023, 0.036, 0.245, 0.040, 0.169 under method A with frozen-model dependence (section 4.2). Method B's per-line rates are in its output file. |
| "Fails a perfectly calibrated model at almost any sample size" (1.8% at about 8,700 per line) | **Confirmed only up to 320 fixtures** (about 7,700 per line). "Impossible" was too strong. | At 320 fixtures, the bin rule on all five lines passes 0.5% of the time under method A (95% CI 0.1–1.8%) and 0.10–1.55% under method B. Larger samples were not tested. |

**The rule is not logically impossible to pass.** At the trigger and at the largest sample tested, its pass rate for a perfectly calibrated model is far too low to be a meaningful test. It also rejects miscalibrated models at about the same rate, so it does not discriminate.

**Correction of an interim revision.** An interim revision said method A "does not represent a perfectly calibrated tackles model" because the tackles fit is approximate. That statement was wrong:
- In method A, the scored probability is the fitted marginal itself (`gate2_calibration_oc.py`, `model[market]["pred"]`).
- Outcomes are drawn from the same fitted parameters and the same game-state nodes, so the simulated model is exactly calibrated against its own truth, by construction.
- The saved output confirms it. With every simulation pooled, observed minus predicted is between −0.0003 and +0.0007 on every line, at 320 fixtures, under all four dependence settings.
- The fit error (mean absolute 0.0004, maximum 0.011 for tackles; mean 0.006 under the stress k) only means the simulated design probabilities differ very slightly from the logged ones. That changes the design a little; it is not a calibration error.

## 3. Methods

Two independent methods use the same design:
- **Design.** `.prop_alpha_state/predictions.jsonl` holds the 2,715 probabilities the frozen model logged on 2026-10-10: 20 League One/Two matches, 543 players, five Gate 2 lines each. These matches were examined on 2026-10-11 and are ineligible for the lockbox. Only their logged probabilities and play probabilities are used.
- **Voids.** In both methods, a player plays with probability 1 − p_void, and players who do not play are void and not scored, as in `lockbox.scoring_rows`.
- **Larger lockboxes.** Both resample whole fixtures of the design with replacement. Every copy is its own cluster, with its own dependence draw.

**Method A: the frozen model's own structure.**
1. **Count model.** For each player and market, the count given that he plays is X | G ~ NegBin(shape a, mean m·G).
   - G ~ Gamma(k, k), with mean 1, is shared by every player in a fixture (`engine.FactorProjection`).
   - G is drawn once per fixture and market, from 64 equiprobable nodes. The same nodes are used to fit and to simulate.
2. **Fitting.** (m, a) are fitted per player and market to the logged P(X > line):
   - fouls exactly (two lines);
   - tackles by least squares on the logit scale (three lines).
3. **Scored probability.** It is the fitted marginal, so the "perfect" model is exactly calibrated (section 2).
4. **Dependence settings.** The markets' factors are independent.

   | Setting | k tackles | k fouls | Source |
   |---|---|---|---|
   | Independent | ∞ | ∞ | — |
   | Frozen model | 6 | 1000 | `slate.SCAN_GAME_STATE_K_BY_MARKET` |
   | Refit | 11.8 | 1000 | 2026-10-15 refit |
   | Stress | 3 | 6 | — |

5. **Miscalibrations** (frozen dependence). The model's predictions stay fixed and the truth moves:
   - **Mean ±10%:** every player's true mean is 10% higher or lower. Calibration-in-the-large error per line is +0.022 to +0.030, or −0.022 to −0.033.
   - **Spread γ = 0.8:** the truth is shrunk towards the geometric mean, so the model is too extreme (slope about 0.80).
   - **Spread γ = 1.25:** the truth is stretched, so the model is too timid (slope about 1.27).
6. **Scoring.**
   - The real `gate2.worst_bin_gap`, `gate2.calibration_fit` and `gate2.clustered_skill_interval` are used. The skill interval uses 1,000 resamples, not 10,000.
   - The alternatives use a fixture-clustered bootstrap with 1,000 resamples.
   - The first 3 simulations of each cell assert that the script's own bin gaps equal `worst_bin_gap`.
7. **Monte Carlo.** 400 simulations per cell (1,000 for the frozen-model cells at 20 and 40 fixtures).
8. **Analytic check** (independent, normal approximation, 20 fixtures). It predicts per-line bin-rule pass rates of 0.038, 0.048, 0.345, 0.042 and 0.157; the simulation gave 0.037, 0.043, 0.328, 0.040 and 0.160.

**Method B: exact logged marginals, Gaussian copula.**
1. **Outcomes.** Each player has one uniform u per market, and a line's outcome is 1[u < p_line].
   - Every outcome has exactly its logged probability.
   - Thresholds stay nested: 3+ implies 2+ implies 1+.
2. **Dependence.** It comes from a shared standard normal per fixture and market, with latent correlation ρ ∈ {0, 0.1, 0.3, 0.6}.
   - These are sensitivity settings, not estimates.
   - They are not calibrated to the frozen model's k; ρ = 0.6 is far stronger dependence than k = 6 implies.
3. **Miscalibrations.** The truth is shifted on the log-odds scale by ±0.10 or ±0.20, or its slope is set to 0.8 or 1.25 with the average rate preserved. The shifts' size, as the mean absolute change in probability (weighted by chance of playing), is:

   | Shift | tackles 0.5 | tackles 1.5 | tackles 2.5 | fouls 0.5 | fouls 1.5 |
   |---|---|---|---|---|---|
   | ±0.10 | 0.020 | 0.018 | 0.011 | 0.021 | 0.017 |
   | ±0.20 | 0.039 | 0.037 | 0.023 | 0.042 | 0.034 |

   So ±0.10 stays inside the 0.03 tolerance on every line. ±0.20 is above 0.03 on average (0.035), but not on tackles 2.5.
4. **Scoring.** Bins, ECE and the real `gate2.calibration_fit`. **Brier skill and the market benchmark are not simulated.**
5. **Monte Carlo.** 2,000 simulations per cell for the bin-only check; 300 per cell for the run that adds slope and miscalibration.
6. **Resampling.** At 20 fixtures, method B resamples fixtures with replacement (method A uses the 20 exactly).

**Monte Carlo error.** Rates are given with 95% Wilson intervals. When a rate is 0, the upper bound is 0.004 at 1,000 simulations, 0.010 at 400 and 0.013 at 300; half-widths are at most about ±0.05 at 300–400.

**Limitations (both methods):**
- The design is one Saturday's matches, resampled.
- The dependence range comes from the model (method A) or is illustrative (method B); neither is estimated from outcomes.
- Dependence across markets is not modelled, and postponements and duplicates are ignored.
- The miscalibration cases are examples, not every possible failure. A model miscalibrated in one region of probability only is not modelled.
- What counts as a "practically meaningful" miscalibration has not been pre-specified.

## 4. Results

### 4.1 Why the bin rule fails good models

Each bin's gap is an average of 50 to 200 outcomes, with sampling SD sqrt(Σp(1−p))/n: 0.02 to 0.07 at the bin sizes the lockbox produces. The table gives the analytic values per bin (20 fixtures, independent, expected bin sizes):

| Line | Bins with ≥ 50: expected n, SD of gap, P(within 0.03) | P(all bins within) |
|---|---|---|
| tackles 0.5 | 54, 0.068, 0.34 · 88, 0.053, 0.43 · 110, 0.045, 0.49 · 109, 0.042, 0.53 | 0.038 |
| tackles 1.5 | 67, 0.020, 0.86 · 68, 0.043, 0.52 · 119, 0.040, 0.55 · 98, 0.048, 0.47 · 83, 0.054, 0.42 | 0.048 |
| tackles 2.5 | 195, 0.015, 0.96 · 169, 0.027, 0.73 · 88, 0.045, 0.49 | 0.345 |
| fouls 0.5 | 81, 0.055, 0.41 · 118, 0.046, 0.49 · 135, 0.041, 0.54 · 56, 0.059, 0.39 | 0.042 |
| fouls 1.5 | 57, 0.021, 0.85 · 130, 0.032, 0.66 · 159, 0.034, 0.62 · 90, 0.050, 0.45 | 0.157 |

The rule takes the worst of 3 to 8 such bins per line, then the worst of five lines. More data does not cure it: bins narrow, but sparse bins in the tails cross 50 and join the maximum.

### 4.2 The rule as written, perfectly calibrated model, all five lines

| Fixtures (scored per line) | A: literal bin rule | B: literal bin rule (ρ 0 / 0.1 / 0.3 / 0.6) | A: slope in [0.9, 1.1] | A: skill lower bound > 0 | A: Gate 2 as written (skill + slope + bins) |
|---|---|---|---|---|---|
| 20 (481) — the trigger | 0.000 (≤ 0.004) | 0 / 0 / 0 / 0 (each ≤ 0.0019) | 0.025 | 0.61 | **0.000 (≤ 0.004)** |
| 40 (961) | 0.000 (≤ 0.004) | — | 0.081 | 0.91 | 0.000 (≤ 0.004) |
| 80 (1,922) | 0.000 (≤ 0.010) | — | 0.28 | 1.00 | 0.000 (≤ 0.010) |
| 160 (3,843) | 0.003 | — | 0.58–0.60 | 1.00 | 0.000–0.003 |
| 320 (7,691) | 0.005 (0.001–0.018) | 0.0155 / 0.0090 / 0.0015 / 0.0010 | 0.89 | 1.00 | 0.005 (0.001–0.018) |

Notes:
- Columns marked A use frozen-model dependence.
- Method B's literal bins plus slope at 320 fixtures (300 simulations) pass 0.000 / 0.003 / 0.000 at ρ 0 / 0.3 / 0.6.
- **Method A per-line bin pass rates at the trigger:**
  - frozen: 0.023, 0.036, 0.245, 0.040, 0.169;
  - independent: 0.037, 0.043, 0.328, 0.040, 0.160;
  - refit: 0.030, 0.025, 0.278, 0.060, 0.182;
  - stress: 0.028, 0.015, 0.207, 0.043, 0.107.
- Every setting of both methods gives 0 on all five lines at the trigger.

### 4.3 The slope criterion is also underpowered at the trigger

For a perfectly calibrated model under method A, frozen dependence:
- **Spread of the slope estimate.** Per line, the 5th to 95th percentile runs from 0.70–0.79 up to 1.27–1.38 at 20 fixtures. It narrows to 0.92–0.94 up to 1.06–1.09 only by 320 fixtures. The median is 1.00 at every size, so there is no bias.
- **Pass rate on all five lines:** 2.5% at the trigger, 28% at 80 fixtures and 89% at 320.

So whatever is decided about the bins, a single look at about 21 fixtures cannot pass a good model.

### 4.4 Candidate bin rules

A1 to A3 keep the 10 bins, the 0.03 tolerance and the minimum of 50 per bin; A4 raises the minimum.
- **A0, as written:** every bin with ≥ 50 predictions has |gap| ≤ 0.03.
- **A1, no evidence of a gap above 0.03:** a bin fails only if its fixture-clustered 90% interval for (observed − predicted) lies entirely outside [−0.03, 0.03].
- **A2, equivalence:** every bin's clustered 90% interval lies inside [−0.03, 0.03].
- **A3, upper bound of the weighted mean gap:** the gaps of the bins with ≥ 50 predictions are averaged in absolute size, weighted by each bin's count. This is the expected calibration error (ECE). Its clustered 90% upper bound must be ≤ 0.03.
- **A3b, point estimate of the same mean gap:** the ECE itself must be ≤ 0.03.
- **A4, large bins only:** A0 applied only to bins with ≥ 200 (or ≥ 400) predictions.

**The bin rule alone, perfect model, all five lines:**

| Fixtures | A0 (A) | A1 (A) | A2 (A) | A3 (A) | A3b (A) | A3b (B: ρ 0 / 0.1 / 0.3 / 0.6) | A4 ≥ 200 (A) | A4 ≥ 400 (A) |
|---|---|---|---|---|---|---|---|---|
| 20 | 0.000 | 0.55 | 0.000 | 0.000 | 0.014 | 0.029 / 0.015 / 0.007 / 0.001 | 0.000 | 0.000 |
| 80 | 0.000 | 0.75 | 0.000 | 0.000 | 0.64 | — | 0.02 | 0.23 |
| 160 | 0.003 | 0.78–0.80 | 0.000 | 0.02–0.03 [stress 0.00] | 0.96–0.97 [stress 0.91] | — | 0.04 | 0.23 |
| 320 | 0.005 | 0.86–0.88 [stress 0.86] | 0.000 | 0.87 [stress 0.54] | 1.00 [stress 0.99] | 1.000 / 1.000 / 0.974 / 0.863 | 0.20 | 0.33 |

The method A columns use frozen dependence. A2 and A4 never become usable, so they are not considered further. Method B evaluated A0 and A3b only.

### 4.5 Good and bad models under each candidate

**Method A: skill + slope + bin rule, all five lines** (400 simulations a cell; 95% half-widths about ±0.05):

| Truth (frozen dependence unless stated) | Fixtures | A0 (as written) | A1 | A3 | A3b |
|---|---|---|---|---|---|
| Perfect | 160 | 0.000 | 0.50 | 0.01 | 0.58 |
| Perfect | 320 | 0.005 | 0.76 | 0.77 | 0.88 |
| Perfect, stress dependence | 160 | 0.000 | 0.46 | 0.00 | 0.53 |
| Perfect, stress dependence | 320 | 0.005 | 0.77 | 0.49 | 0.87 |
| Mean +10% (in the large +0.022 to +0.030) | 160 | 0.000 | 0.14 | 0.00 | 0.08 |
| Mean +10% | 320 | 0.000 | 0.12 | 0.00 | 0.11 |
| Mean −10% (in the large −0.022 to −0.033) | 160 | 0.000 | 0.07 | 0.00 | 0.04 |
| Mean −10% | 320 | 0.000 | 0.10 | 0.00 | 0.08 |
| Spread too wide (slope about 0.80) | 160, 320 | 0.000 | 0.000 | 0.000 | 0.000 |
| Spread too narrow (slope about 1.27) | 160, 320 | 0.000 | 0.000 | 0.000 | 0.000 |

**Method B: slope + bin rule, all five lines, 320 fixtures** (300 simulations a cell; 95% Wilson interval in brackets). At 20 fixtures every cell is 0 (≤ 0.013) for both rules.

| Truth | Literal bins + slope (ρ 0 / 0.3 / 0.6) | A3b + slope, ρ 0 | A3b + slope, ρ 0.3 | A3b + slope, ρ 0.6 |
|---|---|---|---|---|
| Exact marginals (perfect) | 0.000 / 0.003 / 0.000 | 0.897 [0.857, 0.926] | 0.850 [0.805, 0.886] | 0.713 [0.660, 0.762] |
| Log-odds +0.10 (inside tolerance) | 0 / 0 / 0 | 0.820 [0.773, 0.859] | 0.493 [0.437, 0.550] | 0.383 [0.330, 0.439] |
| Log-odds −0.10 (inside tolerance) | 0 / 0 / 0 | 0.767 [0.716, 0.811] | 0.463 [0.408, 0.520] | 0.347 [0.295, 0.402] |
| Log-odds +0.20 (above 0.03 on average) | 0 / 0 / 0 | 0.000 [0, 0.013] | 0.020 [0.009, 0.043] | 0.043 [0.025, 0.073] |
| Log-odds −0.20 (above 0.03 on average) | 0 / 0 / 0 | 0.000 [0, 0.013] | 0.013 [0.005, 0.034] | 0.017 [0.007, 0.038] |
| Slope 0.8 / slope 1.25 | 0 / 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

### 4.6 What the two methods establish, and what they do not

**Established under both methods:**
- The literal bin rule almost never passes a perfectly calibrated model, at the trigger or at 320 fixtures.
- The slope rule cannot pass a good model at the trigger.
- Scoring at the current trigger therefore spends the single look on an almost certain fail, whatever the model's quality.

**Consistent across the methods:**
- At 320 fixtures, A3b + slope passes a perfect model 0.88 of the time under method A (stress 0.87; with skill) and 0.71–0.90 under method B (without skill), falling as dependence grows.
- It rejects clearly wrong slopes.
- It rarely passes errors above the tolerance:
  - method A, mean ±10% (borderline, in the large up to about 0.03): 0.04–0.11;
  - method B, ±0.20 log-odds: up to 0.043, with upper bound 0.073.
- It often passes errors inside the tolerance: method B, ±0.10 log-odds, 0.35–0.82. Whether that is desirable depends on what the maintainer means by "calibrated within 0.03".

**Not established:**
- the full Gate 2 operating characteristic: method B leaves out skill, and neither method includes the market benchmark;
- the realistic dependence: neither method estimates it from outcomes;
- behaviour above 320 fixtures;
- A1 and A3 under exact marginals (method B did not run them);
- a pre-specified definition of an acceptable versus an unacceptable miscalibration.

These figures support a decision not to score at the current trigger. On their own, they are not approval-grade evidence for any specific replacement rule.

## 5. Options for decision

**Every option keeps:**
- the frozen model and `FROZEN_DIGEST`;
- the lockbox start and eligibility rules;
- the skill rule, the slope range [0.9, 1.1], the 10 equal-width bins and the 0.03 tolerance;
- the single look.

**Approval needed:**
- Options 2 to 4 change the PRD wording of criterion 3. AGENT.md A3 reserves any change to a PRD go-live gate to an explicit instruction from the maintainer.
- Option 1 changes only when the single look happens: a lockbox-protocol amendment, within PRD criterion 1 (at least 500).

| Option | What changes | Perfect model passes (320 fixtures unless stated) | Inside-tolerance error passes | Above-tolerance error passes | Clearly wrong slope passes | Evidence gaps |
|---|---|---|---|---|---|---|
| **0. Status quo** | Nothing; scored at about 21 fixtures | ≤ 0.004 (A) at the trigger | ≈ 0 | ≈ 0 | ≈ 0 | — The gate cannot be passed, and the look is spent on a near-certain fail |
| **1. Defer the look only** | Score once at a pre-registered fixture count (≥ 320 tested) and ≥ 500 per line; PRD rules literal | 0.005 (A); 0.000–0.003 with slope (B) | ≈ 0 | ≈ 0 | ≈ 0 | Same outcome as 0, later |
| **2. Defer + A1** | As 1, bin rule A1 | 0.76–0.77 (A) | not run | 0.10–0.12 (A, borderline ±10%) | 0 (A) | Not run under exact marginals; at small N it passes bad models (A1 alone at 20 fixtures: 0.20–0.32) |
| **3. Defer + A3b** | As 1, bin rule A3b | 0.87–0.88 (A); 0.71–0.90 (B) | 0.35–0.82 (B, ±0.10) | 0.04–0.11 (A, borderline ±10%); 0–0.043 (B, ±0.20; upper 0.073) | 0 | No allowance for noise, so rigour rests on reaching the sample; pass rate falls with dependence |
| **4. Defer + A3** | As 1, bin rule A3 | 0.77 (A; stress 0.49) | not run | 0.00 (A) | 0 (A) | Not run under exact marginals; a good model passes much less often under strong dependence, so a larger sample may be needed (not simulated) |

**No option is recommended.** The trade-off the maintainer must weigh:
- **Options 2 and 3:** a good model passes more often, but borderline-bad models sometimes pass too.
- **Option 4:** borderline-bad models pass less often, but a good model is rejected more often, especially if dependence is strong.

**Further analysis** that would make a choice approval-grade, using no lockbox data:
- (a) estimate within-fixture outcome dependence from pre-lockbox history (kickoff before 2026-10-27), to narrow the k / ρ range;
- (b) pre-specify which miscalibrations must fail and which may pass;
- (c) run A1 and A3 under exact marginals, with skill included;
- (d) use at least 1,000 simulations for every cell the decision rests on.

**Practical points for options 1 to 4:**
- **Calendar time.** 320 fixtures take about 11 to 13 weeks with every match captured (25 to 30 a week), roughly mid to late January 2027. At October's measured uptime, it would take far longer.
- **Data plan.** It needs the Sportmonks plan paid beyond 2026-11-08.
- **Stop rule.** A stop date must be pre-registered: if the threshold is not reached by then, Gate 2 stays NOT PASSED, unscored.
- **Other blockers remain.** No option fixes Gate 2's other blockers: the market benchmark (no League One/Two closing prices), `yellow_card`, and team-sheet line-ups.

## 6. Timing and what must not happen before a decision

**No lockbox outcome has been looked at.**
- The lockbox opens at 00:00 UTC on 27 Oct.
- Outcomes are first captured into `lockbox_outcomes.jsonl` about 72 hours after the first eligible kickoff, around 30 Oct.
- The scheduled `--capture-only` step prints counts only.

**For a decision to count as pre-registered:**
- Do not run `scripts/gate2_prospective.py` without a flag. That run is the single look.
- Do not open or analyse `lockbox_outcomes.jsonl`.

**Deadlines:**
- A decision is cleanest before the first capture (about 30 Oct).
- It must be made before the trigger is reached (about 21 fixtures). Otherwise a deliberate scoring run under the current rules would spend the lockbox.

**If no option is approved,** the gate stays exactly as written. The analyses above show it will almost certainly not pass.

## 7. Decision record (maintainer)

- **Option chosen:** 3. The bin criterion is the count-weighted mean of |mean p̂ − ȳ| over the 10 equal-width bins with ≥ 50 predictions, at most 0.03. The slope range [0.9, 1.1] and the skill rule (fixture-clustered 90% lower bound > 0) are unchanged.
- **Scoring threshold and stop date:**
  - Score once, when every Gate 2 line has at least 500 scored predictions from at least 320 distinct matches.
  - Only kickoffs before 2027-06-01 00:00 UTC (stop date 31 May 2027) count.
  - A lockbox still short of the threshold when it closes on 2027-06-15 (the end plus the 14 days for missing outcomes) is NOT PASSED, recorded once and never scored.
- **Bootstrap settings:** none needed. The chosen rule is a point estimate; the skill interval keeps the protocol's 10,000 resamples and seed 20261027.
- **PRD wording change approved:** yes. Gate 2 criterion 3 now reads: "Over the calibration bins (10 equal-width bins on [0, 1]) with ≥ 50 predictions, the mean of |p̄ − ȳ| weighted by each bin's count must be ≤ 0.03", with an amendment note.
- **Further analysis required before implementation:** not requested.
- **Decided by:** the maintainer, in this session, on 2026-10-26 at 20:44 UTC. No lockbox outcome existed: the lockbox starts 2026-10-27 00:00 UTC.

**Implementation after approval** is a separate change, with tests that fail before it:
- the new criterion in `evaluation/gate2.py` (pure);
- the new trigger in `evaluation/lockbox.PROTOCOL` (minimum fixtures, stop date);
- `scripts/gate2_prospective.py` scoring and its result fields;
- `LOCKBOX_PROTOCOL.md`, the PRD and CLAUDE.md.

None of these files are in `model_freeze.PRICING_CODE`, so `FROZEN_DIGEST` and the lockbox start are unaffected.

## Revision history

- **v1 (2026-10-26):** method A; recommended Option 3.
- **v2 (2026-10-26, interim):** added method B and withdrew the recommendation. It wrongly stated that method A was not exactly calibrated for tackles, and it left the old options table and an Option 4 recommendation in place.
- **v3 (2026-10-26, this version):**
  - corrected that statement (section 2);
  - put the two methods side by side;
  - added method B's ±0.10 log-odds results and the per-line size of each shift;
  - rebuilt the options table from both methods;
  - removed every recommendation.
- **v4 (2026-10-26):** the maintainer chose Option 3 with 320 matches and stop date 31 May 2027. The status and decision record were filled in, and the decision was implemented in a separate change.
