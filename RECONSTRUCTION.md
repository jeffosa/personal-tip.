# RECONSTRUCTION.md — provenance of the rebuilt prop_alpha baseline

**This is a reconstruction, not the original v3.3 code.** The original package (`prop_alpha_v3.3_code.zip`,
180 tests) is not available. Every file here was rebuilt from the specification documents in this
repository. Where a document prints code verbatim it was copied. Everything else was written from prose
descriptions, and any behaviour the documents do not pin down is an interpretation, listed below.

- No PRD go-live gate has been passed.
- All SQL is **[U]**: it has never run against PostgreSQL.
- The odds poller, projection refresh, bet ledger and execution components are **[N]**.

## Source hierarchy used

1. `AGENT.md` (v3.3; overrides everything)
2. `ARCHITECTURE-ESSENTIALS.md` (v3.2 type contract, constraints, verbatim code)
3. `ARCHITECTURE.md` (v3.1), then `prop_alpha_PRD_v3.1.md`
4. `prop_alpha_v3_refactor.md` (v3.0 code): a starting point only where newer documents do not supersede it

## Status legend

| Status | Meaning |
|---|---|
| VERBATIM | Copied exactly from a block the documents print in full as the current code. |
| ADAPTED | Started from v3.0 code in `prop_alpha_v3_refactor.md`, changed to meet v3.2/v3.3 requirements. |
| RECONSTRUCTED | No code in any document; written from a prose description. |

Mixed files list each part separately.

## File table

| File | Source section(s) | Status | Built in |
|---|---|---|---|
| `prop_alpha/__init__.py` and the six sub-package `__init__.py` | ESSENTIALS §1.1, §1.2 (empty package markers) | VERBATIM (empty) | step 1 |
| `prop_alpha/modeling/engine.py` | ESSENTIALS §2.3 (module top through `validate_exposure_vector`; `gamma_poisson_update`); refactor TASK 1 (v3.0 engine); AGENT §1.3, §2.1–§2.2, §4.2; PRD §2.3, §3 B3 | Mixed: see [engine.py](#prop_alphamodelingenginepy) | step 1 |
| `prop_alpha/scanner/market_scanner.py` | refactor TASK 4 (v3.0 scanner); AGENT §1.3 H5, §2.2 U1–U3, §4.2; PRD §2.4, §3 B4–B5; ARCHITECTURE §4.2–§4.3 | ADAPTED | step 2 |
| `tests/test_core.py` | Test names from AGENT §2.2, §3.3 and PRD; no test code is printed in any document | RECONSTRUCTED | steps 1–3 |
| `prop_alpha/ingestion/transform.py` | refactor TASK 2 (supporting) (v3.0 transform); ESSENTIALS §1.2, §3.1, R-TYPE-5; AGENT §4.1–§4.2 | ADAPTED | step 3 |
| `prop_alpha/ingestion/pipeline.py` | refactor TASK 2 (v3.0 pipeline); AGENT §1.3 H1–H4, §1.4 P3, §3.4; ARCHITECTURE §2; PRD §2.1, §3 B1–B2 | ADAPTED | step 3 |
| `prop_alpha/calibration/fitting.py` | ESSENTIALS §1.2; ARCHITECTURE §1.3 export list; PRD §1.4 (the H2 estimator formula) | RECONSTRUCTED | step 3 |
| `prop_alpha/calibration/referee_job.py` | ESSENTIALS §1.2; ARCHITECTURE §1.3, §3.3 P4; AGENT §1.4 P4, §3.4, §4.1–§4.2; PRD R-STG-6, Scenario C | RECONSTRUCTED | step 3 |
| `prop_alpha/evaluation/backtest.py` | ESSENTIALS §1.2; ARCHITECTURE §1.3; AGENT §4.2 signature; PRD Gates 2–3 | RECONSTRUCTED | step 3 |
| `tests/conftest.py` | none (shared payload builders) | RECONSTRUCTED | step 3 |
| `tests/test_integrity.py` | ESSENTIALS §1.1, §2.3, §3.1; AGENT §3.3 boundary catalogue | RECONSTRUCTED | step 3 (SQL parts in step 4) |
| `tests/test_pipeline_concurrency.py` (including the in-memory fakes) | ARCHITECTURE §2.1–§2.3; PRD §2.1, B1–B2; AGENT §3.3 | RECONSTRUCTED | step 3 |
| `migrations/002_staging_and_indexes.sql` | refactor TASK 3 | VERBATIM (byte-exact; superseded by 003, never apply to a v3.2+ schema) **[U]** | step 4 |
| `migrations/003_integrity_constraints.sql` | ESSENTIALS §3.4 | VERBATIM (byte-exact) **[U]** | step 4 |
| `schema.sql` | ESSENTIALS §3.3 (`player_match_stats`, `odds_lines`), §3.4 (merge function); refactor TASK 3 (staging DDL); everything else from column usage in code and documents | Mixed: see [schema.sql](#schemasql-mixed-u) **[U]** | step 4 |
| `ci/bootstrap.sh` | ESSENTIALS §4.3 | VERBATIM except `EXPECTED_TESTS` (as instructed) | step 4 |
| `.gitattributes` | none (forces LF on `*.sh` and `*.sql`; bash rejects CRLF) | RECONSTRUCTED | step 4 |
| `tests/test_typing_contract.py` | ESSENTIALS §2.1 R-TYPE-1 to R-TYPE-3 | RECONSTRUCTED | step 5 |
| `tests/test_agent_contract.py` | AGENT §1.1 A3–A4, §1.2 Z1, §1.3 F2–F6, §1.4 P1–P5, §2.1 G1 | RECONSTRUCTED | step 5 |
| `prop_alpha/alerts/__init__.py`, `prop_alpha/alerts/telegram.py` | **Not in any document.** New feature requested by the maintainer after step 2. | NEW | step 6 |
| `tests/test_alerts.py` | none | NEW | step 6 (updated step 7) |
| `prop_alpha/scanner/value_screen.py`, `tests/test_value_screen.py` | **Not in any document.** High-value definition set by the maintainer. | NEW | step 7 |
| `.env.example` | none (empty template; the real `.env` is gitignored) | NEW | step 6 |
| `AGENT.md` (row H6), `CLAUDE.md` (I/O isolation, secrets, alert commands) | edited to match step 6 | EDITED | step 6 |

## Interpretations by file

Each entry is a place where the code does something the documents do not state exactly. IDs are
stable so later notes can refer to them.

### `prop_alpha/modeling/engine.py`

Parts and their status:

| Part | Status | Source |
|---|---|---|
| Imports, `logger`, `REFEREE_SENSITIVE_MARKETS`, `MIN/MAX_RELATIVE_VARIANCE`, `MAX_MATCH_MINUTES`, `validate_exposure_vector` | VERBATIM in content, with the drift noted in E1 | ESSENTIALS §2.3 |
| `QuantEngine.gamma_poisson_update` | VERBATIM | ESSENTIALS §2.3 |
| `ProjectionError`, `VoidRule`, `RefereeProfile`, `GammaPosterior`, `LineupExpectation`, `SideProbabilities`, `PropProjection`, `_validate_line`, `_line_split`, `_zero_split`, bivariate Poisson methods, `referee_multiplier`, `positional_prior`, `predictive_gamma`, `project_count_market`, `project_card_market`, `player_card_factor` | ADAPTED | refactor TASK 1 |
| `DiscreteDistribution(Protocol)` | RECONSTRUCTED | ESSENTIALS R-TYPE-7 |
| `[MATH_CLAMP]` warning in `predictive_gamma` | RECONSTRUCTED | PRD §3 B3; AGENT F5 |

- **E1 — Verbatim drift (defect).** The module docstring and the two comments above the variance and
  minutes constants differ from the verbatim block in ESSENTIALS §2.3. The docstring was shortened to
  one line, the comments were dropped, and `DiscreteDistribution`, `ProjectionError` and `VoidRule`
  sat between the constants and `validate_exposure_vector`. **Fixed** (commit "Restore verbatim
  engine header"): the file now starts with the §2.3 block byte for byte, and the three classes
  follow it.
- **E2 — `DiscreteDistribution`.** R-TYPE-7 names the five methods (`pmf`, `cdf`, `sf`, `mean`,
  `var`) but not their signatures. Implemented as `values: ArrayLike -> float | np.ndarray`, and
  `mean()`/`var()` returning `float`.
- **E3 — `RefereeProfile`** adds a finiteness check on all four coefficients (AGENT G2 intent). The
  v3.0 range rules are unchanged.
- **E4 — `GammaPosterior.__post_init__`** rejects non-finite as well as non-positive parameters. AGENT
  G2 places the finiteness check in `predictive_gamma`. That check is kept, but it can no longer be
  reached.
- **E5 — `LineupExpectation`:**
  - finiteness checks added;
  - the 130-minute bound uses `MAX_MATCH_MINUTES` (v3.0 used the literal);
  - v3.0's `1e-9` tolerance on `p_start + p_sub_appearance <= 1` was removed, so the check is strict;
  - v3.0's `max(0.0, …)` in `p_no_appearance` was removed, following AGENT F5 ("the only clamp").
    **This introduced a defect:** `LineupExpectation(0.33, 0.67).p_no_appearance` is `-1.1e-16`. See
    E5a.
- **E5a — Negative no-appearance weight (defect, found during this write-up).** A negative weight of
  order 1e-16 reaches `outcome_model` as a scenario weight, because the zero-weight check is
  `== 0.0`. `return_moments` then rejects the negative probability, and `allocate` skips a valid
  market as "projection unusable". This fails closed but is still wrong. **Fixed** by computing
  `1.0 - (p_start + p_sub_appearance)`. That form is exact and non-negative whenever the validator's
  `p_start + p_sub_appearance <= 1` holds (Sterbenz lemma), so no clamp is needed. Regression test:
  `test_complementary_lineup_leaves_no_negative_absence_weight`.
- **E6 — `SideProbabilities`** validates that each value is finite and in [0, 1], and that the three
  sum to 1 within 1e-12. v3.0 had no validation and computed `p_void = max(0, 1 - p_win - p_lose)`.
  Step 2 computed `p_void = 1 - p_win - p_lose` with no clamp.
- **E6a — `p_void` rounding (defect, found during this write-up).** `1 - p_win - p_lose` rounds to
  about -1e-16 on roughly 5% of inputs: 6,406 of 130,800 calls in a random probe. The strict
  validator then raised `ValueError`, and `allocate` would skip those markets as "projection
  unusable". **Fixed** by summing `p_void` directly from non-negative masses: the void-scenario weight
  plus the push probability of each scenario. This is analytically identical and needs no clamp.
  Regression test: `test_side_probabilities_never_fail_on_rounding` (fixed seed).
- **E7 — `_line_split`** takes P(over) from `distribution.sf(floor(line))`, where v3.0 used
  `max(0, 1 - under - push)`. This is exact in the tail and removes a clamp.
- **E8 — `count_distribution`** rejects minutes outside (0, 130]. AGENT G2 requires only "finite and
  > 0"; the upper bound is an addition.
- **E9 — `[MATH_CLAMP]` message** carries `relative_variance`, `alpha`, `beta`, `k` and `multiplier`
  (PRD B3 lists the last four). It is logged at WARNING on `prop_alpha.engine`.
- **E10 — `QuantEngine.__init__`** rejects `beta1`/`beta2` that are non-finite or <= 0. This is not
  documented. It rules out beta = 0 (no referee effect) and negative fitted exponents. **Open question
  Q1.**
- **E11 — `referee_multiplier`** raises `ProjectionError` if the result is non-finite or <= 0 (G2
  extension; v3.0 returned it unchecked).
- **E12 — Bivariate Poisson:**
  - rates validated finite and >= 0;
  - `math.fsum` used for the log-sum-exp and the joint CDF;
  - the `max(0, …)` in `joint_over_probability` removed;
  - `bivariate_from_moments` requires positive means;
  - `bivariate_correlation` raises on a zero denominator.

  Parameter names changed from v3.0 (`x, y, lambda1, lambda2, lambda0` became `count_x, count_y,
  lambda_x, lambda_y, lambda_shared`). No document lists these signatures.
- **E13 — `project_card_market`** raises `ProjectionError` (a `ValueError` subclass) for a
  non-positive card propensity, where v3.0 raised `ValueError`.
- **E14 — `player_card_factor`** validates its counts and rates (not in v3.0).

### `prop_alpha/scanner/market_scanner.py` (ADAPTED from refactor TASK 4)

- **S1 — Degenerate-outcome guard.** `DEGENERATE_LOSS_PROBABILITY: Final = 1e-6` (R-ALLOC-7). It is
  checked in `_candidates` **before** the EV filter, so a degenerate side is recorded even when its EV
  is below `alpha_threshold`. The reason text is `"degenerate outcome: <line> <side>; manual review
  required"`.
- **S2 — `_candidates`** returns `(candidates, skip_reasons)` rather than a list. It is private and
  has no documented signature.
- **S3 — Sub-minimum stakes.** These go to `AllocationReport.skipped_directives` and are logged at
  INFO (PRD B4). This field is new relative to v3.0.
- **S4 — `LiveOddsQuote.__post_init__` validation** (not in v3.0 or the docs):
  - `captured_at` must be timezone-aware UTC;
  - whole or half lines only;
  - prices must be finite when present;
  - `max_stake` must be finite and >= 0;
  - `sportsbook` must be non-empty;
  - `void_rule` is coerced to `VoidRule`.

  **Consequence:** the AGENT §3.3 boundary row "`over_price=float("inf")`: side skipped by
  `_candidates`, no exception escapes `allocate`" cannot be reached, because the quote cannot be
  constructed. The finiteness test in `_candidates` is dead code. **Open question Q2.**
- **S5 — `now` must be passed.** `allocate` keeps the documented signature (`now: datetime | None =
  None`) but raises `ValueError` if `now` is `None` or not UTC. v3.0 read the clock, which AGENT P5
  forbids in a pure module.
- **S6 — Future-dated quotes.** `allocate` raises `ValueError` if any quote's `captured_at` is later
  than `now`. That aborts the whole call rather than skipping one key, which contrasts with per-key
  isolation (AGENT F6, H5). **Open question Q3.**
- **S7 — Stake rounding.** Stakes are floored to the cent (`floor(x*100 + 1e-9)/100`). PRD R-ALLOC-5
  and v3.0 **round** to 0.01. Flooring can never exceed `max_stake`; rounding could by half a cent.
  This changes stake amounts, so it is **open question Q4** (AGENT A7).
- **S8 — Determinism tie-breaks** (not in v3.0):
  - equal best prices: the alphabetically first sportsbook wins;
  - candidates sort by `(line, side, void_rule)`;
  - directives sort by `(-ev, fixture_id, line, side)`.
- **S9 — `return_moments`:**
  - validates dimensions, finiteness, and probabilities that are non-negative and sum to 1 within
    1e-12;
  - the eigen-floor adds exactly `-λ_min`, where v3.0 added `-λ_min + 1e-12`;
  - the docstring carries the G1 condition-number argument (κ ≈ 1.005 on the reference case).
- **S10 — `outcome_model`** raises `FloatingPointError` if the grid probabilities do not sum to 1
  within 1e-12. It skips a scenario only when its weight is exactly `0.0` (v3.0: `<= 0.0`), which is
  what exposed E5a.
- **S11 — `solve_mean_variance`** validates shapes, finiteness, non-negative bounds and budget, and a
  positive `risk_aversion`. The algorithm is unchanged: SLSQP with an analytic gradient, `ftol=1e-14`,
  `maxiter=500`, and a best-single fallback.
- **S12 — `InstitutionalAssetManager.__init__`** adds validation of `alpha_threshold`,
  `open_exposure`, `min_stake` and `max_quote_age`. **All risk defaults are unchanged.**
- **S13 — `expected_value` and `price_for_ev`** validate their inputs.
- **S14 — Dropped from v3.0:** the `main()` demo (not in the ARCHITECTURE §1.3 export list), the
  long module docstring, and the class docstring on λ ↔ ψ. The unused `QuantEngine` and
  `RefereeProfile` imports were also removed.

### `tests/test_core.py` (RECONSTRUCTED; 57 tests at the end of step 2)

- **T1 — Test placement.** It holds `test_valid_vectors_are_returned_typed`,
  `test_polluted_vectors_are_rejected` (11 cases, as ESSENTIALS §2.3 documents) and
  `test_gamma_update_uses_validation_and_allows_prior_only_history`. ESSENTIALS §1.1 assigns
  "exposure validation" to `test_integrity.py`. **Done in step 3:** all three now live in
  `test_integrity.py`, unchanged.
- **T2 — Tests with no documented name:**
  - `test_bimodal_substitution_has_no_absence_void_when_present` (AGENT §3.3 bimodal row);
  - `test_invalid_projection_parameters_fail_closed`;
  - `test_invalid_minutes_fail_closed`;
  - `test_referee_profile_rejects_non_finite_coefficients`;
  - `test_allocate_requires_explicit_utc_time`.
- **T3 — Documented tests missing at end of step 2:** `test_bivariate_marginal_is_equidispersed_poisson`
  (PRD R-MOD-5) and `test_corrupted_projection_is_skipped_never_traded` (PRD R-MOD-4). **Added in
  step 3.**
- **T4 — Documented figures not pinned:**
  - `test_card_probability_not_distorted_by_variance_floor` checks the closed-form NB probability, not
    the documented 0.1675 → 0.1199 figures. The documents do not give the "representative
    projection" that produced them.
  - `test_predictive_gamma_stable_where_subtraction_form_cancels` asserts the clamped shape `1e6`, but
    does not reproduce the `-1.42e-14` subtraction result.
- **T5 — `test_return_moments_match_closed_form`** uses a synthetic two-state grid. AGENT U2 says
  "(all void rules)". **Strengthened in step 3:** it now checks the closed-form mean and variance of a
  single candidate built from a real projection, under all three void rules, on a whole and a half
  line (6 cases).
- **T6 — Reference allocation pinned.** `test_mean_variance_prefers_low_variance_line_at_similar_ev`
  pins the documented 1.94% / 0.04% split (tolerance ±0.02 percentage points).
  `test_cross_line_covariance_matches_brute_force` sums x over [0, 300), as documented.

### `prop_alpha/ingestion/transform.py` (ADAPTED from refactor TASK 2 supporting)

- **X1 — L1 minute logic.** `PlayerMatchRecord.__post_init__` adds the two v3.2 rules: it rejects
  `minute_off < minute_on` and `minutes_played > MAX_MATCH_MINUTES`. This is L1 parity with
  `chk_minute_logic` (ESSENTIALS §3.1).
- **X2 — Import of `MAX_MATCH_MINUTES` from `engine.py`.** ARCHITECTURE §1.3 (v3.1) describes
  transform as "stdlib only". AGENT §3.2 and CLAUDE.md say a constant is defined once and imported,
  never redeclared, and ESSENTIALS §1.2 names `MAX_MATCH_MINUTES` in the transform's rules. The
  higher-ranked documents win, so transform imports from engine. That adds the edge engine →
  transform, which keeps the graph acyclic (engine imports nothing internal) and imports no I/O
  library (P1).
- **X3 — Immutable constants (R-TYPE-5).** `DEFAULT_KAPPAS` is a `MappingProxyType`. All
  module-level upper-case names, including the private widths, are `Final`.
- **X4 — Carried over from v3.0 unchanged, with known weaknesses (left for the hardening task):**
  - `_count` maps a missing or `None` stat to 0;
  - `int()` truncates a fractional count;
  - a bool counts as an int.

### `prop_alpha/ingestion/pipeline.py` (ADAPTED from refactor TASK 2)

- **P1 — H3 added.** `except psycopg2.errors.QueryCanceled` precedes `except RETRYABLE_DB_ERRORS`. It
  logs `[STATEMENT_TIMEOUT]` at ERROR on `prop_alpha.ingestion` and re-raises with no retry (AGENT H3,
  PRD B2).
- **P2 — v3.0's `assert last_error is not None` replaced** with an explicit `RuntimeError`. It cannot
  be reached, but an assert disappears under `python -O`.
- **P3 — `build_arg_parser()` factored out of `main()`,** and `main(argv=None)` takes argv, so that the
  "[T for argument parsing]" item of PRD §2.1 is testable without environment variables. The CLI
  behaviour is unchanged.
- **P4 — Typing:**
  - SQL strings and `RETRYABLE_DB_ERRORS` are `Final`;
  - `JOBS` is a `Final` class attribute;
  - closures annotate `cur: Any` (R-TYPE-8).
- **P5 — Kept from v3.0:**
  - `DATA_VENDOR_RPS` is an environment variable read in `main()`, not a module constant;
  - the pool-size check is skipped when the pool has no `maxconn` attribute.

### `prop_alpha/calibration/fitting.py` (RECONSTRUCTED)

No code in any document; signatures are interpretations.

- **F1 — `fit_poisson_glm(design, counts, offset=None, max_iter=100, tol=1e-10) -> GLMResult`:**
  - log link, Newton–Raphson from beta = 0;
  - each step is halved until the log-likelihood does not decrease;
  - standard errors come from the inverse Fisher information at convergence.
  - Fail-closed: it **raises** `ValueError` on a rank-deficient design, non-convergence, or failed
    step halving. It never returns a partial result (AGENT F3).
  - `GLMResult` is frozen and validated: `coefficients`, `standard_errors`, `log_likelihood`,
    `iterations`.
- **F2 — `fit_possession_kappa(counts, minutes, team_possession_pct) -> float`:**
  - model: `E[count] = t * exp(a) * (opp/50)^kappa`, with `opp = 100 - team_possession`;
  - that is the exponent `padj_scale` divides out.
  - Possession at 0 or 100 is **rejected** rather than clipped: the [1, 99] clip in `padj_scale` is
    documented, but adding a second clip here would be a new clamp (AGENT F5).
- **F3 — `fit_referee_betas(counts, expected_counts, strictness_coefficient, home_bias_ratio, is_home)
  -> (beta1, beta2)`:**
  - a GLM on `[1, log SC, log venue]`, with venue `1 - HBR` at home and `1 + HBR` away;
  - offset `log(expected_counts)`, the player's referee-free expectation.
- **F4 — `fit_game_state_dispersion(counts, expected_counts) -> float | None`:**
  - exactly the PRD §1.4 formula;
  - returns `None` when 1/k <= 0 (documented), which maps to `QuantEngine(game_state_k=None)`;
  - the fixture-clustered bootstrap interval and the ">= 20 full matches" sample filter are **[N]**:
    they are part of the falsification protocol, not of this function.
- **F5 — `fit_state_duel_density(duels, leading, level, trailing) -> ndarray[3]`:** NNLS for duels per
  90 in each match state. It raises if the three states are not identified.

### `prop_alpha/calibration/referee_job.py` (RECONSTRUCTED)

- **R1 — Estimators**, each shrunk toward its league with `shrink_matches` pseudo-matches of
  league-average data:
  - `SC = (F + m0*F_bar) / ((n + m0)*F_bar)`;
  - `YCP` and `RCP`: cards per foul;
  - `HBR = (away - home + m0*F_bar*h_bar) / (fouls + m0*F_bar)`, which stays inside (-1, 1).

  The documents name the outputs but give no formulas. The units (cards per foul; venue multipliers
  `1 ∓ HBR`) follow from how `QuantEngine.project_card_market` and `referee_multiplier` consume them.
- **R2 — Input checks.** It raises if a league has zero fouls, if a referee appears in several leagues,
  or if a (referee, fixture) row is duplicated.
- **R3 — SQL:**
  - `DISCIPLINE_SQL` aggregates `player_match_stats` per final fixture of the league inside the
    lookback window;
  - `UPDATE_SQL` uses `UPDATE … AS r … FROM (VALUES %s)` through `execute_values` with
    `UPDATE_TEMPLATE`;
  - rows are passed ordered by `referee_id` (ARCHITECTURE §3.3 P4).

  The column names come from AGENT §4.1. The full SQL is reconstructed. **[U]**
- **R4 — Lock statement.** The advisory lock is `ADVISORY_LOCK_SQL = "SELECT
  pg_advisory_xact_lock(hashtext(%s))"`, executed first with `MERGE_LOCK_KEY` (PRD Scenario C step 4).
  `ADVISORY_LOCK_SQL` is a new constant name.
- **R5 — Connection.** `psycopg2.connect(dsn)`, with no `lock_timeout`, as PRD Scenario C states. There
  are no exception handlers in the module, consistent with the sanctioned set.

### `prop_alpha/evaluation/backtest.py` (RECONSTRUCTED)

- **B1 — Lineup at team-sheet time.** `walk_forward_over_probability` prices a starter with `p_start =
  1` and a used substitute with `p_sub = 1`. An unused substitute (0 minutes, not started) is treated as
  voided and gets no prediction.
- **B2 — Expected minutes** are the player's prior mean minutes when starting or coming on, else
  `default_start_minutes` or `default_sub_minutes` (mirrors `LINEUP_UPSERT_SQL`).
- **B3 — Neutral referee.** Observations carry no referee, so a neutral `RefereeProfile(1, 0, 0, 0)`
  (multiplier 1) is used. **Consequence:** referee-sensitive markets (`fouls`, cards) are backtested
  **without** the referee adjustment. PRD Gate 2 needs `fouls` and `yellow_card`, so this function is
  only complete for `tackles`. **Open question Q5.**
- **B4 — Half lines only.** Integer lines raise `ValueError`, so every outcome settles as 0/1.
- **B5 — League rate is fixed.** `league_rate_per90` is a fixed argument (the documented signature);
  PRD Gate 2's "trailing league rate" is the caller's responsibility.
- **B6 — `summarize`** returns `dict[str, float]` (n, base_rate, brier, brier_climatology,
  brier_skill, log_loss). Climatology is the sample's own base rate. The function raises when that
  Brier score is zero.
- **B7 — `log_loss` does not clip.** A confident miss returns `inf` rather than an epsilon-clipped
  value (AGENT F5: no new clamps).

### Tests added in step 3

- **`test_core.py`** (+28 to 85):
  - the two documented tests from T3;
  - transforms: payload parsing, impossible stats (6 cases), malformed fixtures, `DEFAULT_KAPPAS`
    immutability, `padj_scale` at 0/100 possession, `dribbled_past_rate`, match-state minutes;
  - lineup parsing;
  - estimators, each recovering known parameters from fixed-seed simulations;
  - referee coefficients;
  - scoring rules, calibration bins, CLV, walk-forward leakage and summarize.
- **`test_integrity.py`** (25):
  - the 13 moved exposure tests and a zero-exposure test;
  - L1 minute-logic and possession boundaries;
  - staging and stats row widths and column parity;
  - `run_referee_job` against a recording fake: lock first, then read, then update in `referee_id`
    order.
- **`test_pipeline_concurrency.py`** (13):
  - fakes of the pool, connection, cursor and vendor. The fake cursor implements `mogrify` and
    `connection.encoding`, so the **real** `psycopg2.extras.execute_values` runs;
  - the 12 behaviours ARCHITECTURE §2 and the PRD name, plus CLI parsing;
  - concurrency is proven with a `threading.Barrier` rather than wall-clock timing. The only timing
    assertion is the rate limiter's lower bound, with a 10% margin.
- **Proof of bite** on a scratch copy of `pipeline.py`:
  - removing the H3 handler: `2 failed, 11 passed`;
  - moving the vendor fetch inside `_with_transaction`: `3 failed, 10 passed`;
  - restored: `122 passed`.

### `schema.sql` (mixed; [U])

**Status [U]:** never executed against PostgreSQL. There is no PostgreSQL, Docker or psql on this
machine.

| Part | Status |
|---|---|
| `player_match_stats` and `odds_lines` | VERBATIM (ESSENTIALS §3.3) |
| `staging_player_feed` and `staging_rejects` | VERBATIM (migration 002 DDL) |
| `merge_staging_player_feed()` | VERBATIM, byte-identical to migration 003. A test enforces this (AGENT M4, Z5). |
| `leagues`, `teams`, `players`, `referees`, `fixtures`, `lineup_expectations`, `raw_payloads`, `model_projections`, `bets`, indexes | RECONSTRUCTED |

- **Q-S1 — Unqualified names.** The verbatim excerpts use unqualified table names, so the file runs
  `CREATE SCHEMA IF NOT EXISTS prop_alpha; SET search_path TO prop_alpha, public;` and ends with
  `RESET search_path`.
- **Q-S2 — Reconstructed columns** come from what the code and the merge function read and write:
  - `referees` has neutral defaults (SC 1, propensities 0, HBR 0), because the merge inserts only
    name, league and UID;
  - `raw_payloads.payload_sha256` is `UNIQUE`;
  - `fixtures.vendor_fixture_uid`, `players.vendor_player_uid` and `teams.vendor_team_uid` are
    `UNIQUE`, as the `ON CONFLICT` targets require.
- **Q-S3 — Undocumented CHECKs added.** Each is parity-safe:
  - `fixtures.status IN (…)`: L1 is `VALID_STATUSES`, L2 is the merge's status reject;
  - `lineup_expectations.p_start` and `p_sub_appearance` in [0, 1]: L1 is `parse_lineup_payload` and
    `LineupExpectation`. These rows bypass staging, so L2 is n/a, as for `odds_lines`;
  - `leagues.tier BETWEEN 1 AND 6` and `bets.side`: no merge path.

  No CHECK was added on a staged column without an L2 predicate.
- **Q-S4 — Fillfactor.** `player_match_stats` gets `fillfactor = 90` through a following `ALTER TABLE`,
  so the verbatim `CREATE TABLE` stays untouched. `lineup_expectations` declares it inline
  (ARCHITECTURE §1.2 item 7).
- **Q-S5 — Indexes:**
  - `idx_odds_market_latest` and `idx_odds_fixture_captured` are documented;
  - `idx_pms_player` (for the lineup `LATERAL` and history reads) and `idx_fixtures_league_kickoff`
    (for `DISCIPLINE_SQL`) are additions;
  - `idx_odds_captured_brin` is absent, as PRD Gate 1 requires.
- **Q-S6 — `model_projections` and `bets`** are minimal reconstructions for the [N] ProjectionRefresh
  and ledger writer. They hold only the columns PRD Gate 3 names (`is_paper`, `closing_line_id`,
  `clv`, links to `projection_id` and `line_id`) plus the directive's sizing fields.

### Migrations and CI

- **M-1 — No migration 001.** The prompt asked for "001-003 as documented", but no document contains
  or describes a migration 001. ESSENTIALS §1.1 ships only 002 and 003, and 002 is described as "v2 →
  v3". The v2 base schema is not documented, so 001 was **not invented**. A v2 database cannot be
  rebuilt from these files.
- **M-2 — 002 is verbatim.** It has no warning header, because adding one would break "verbatim". The
  never-apply-after-003 rule lives in CLAUDE.md, ESSENTIALS §1.1 and the `schema.sql` header.
  `test_superseded_002_merge_lacks_minute_logic_predicates` documents why.
- **M-3 — `bootstrap.sh` is the v3.2 script.** Its required-files list does not include
  `tests/test_agent_contract.py` (added in v3.3). It was left as is, because the instruction was
  verbatim except `EXPECTED_TESTS`.
- **M-4 — Preflight at the step 4 checkpoint** failed as expected:
  `FATAL: repository is missing tests/test_typing_contract.py`. That file is created in step 5, and
  the step 5 preflight passes.

### Tests added in step 4 (`test_integrity.py`, +16 to 41)

These are structural, read-only checks of the SQL text:
- L2/L3 parity for every core CHECK, in `schema.sql` and in 003;
- the merge function is identical in `schema.sql` and 003;
- the 002 body is the superseded one;
- `NOT VALID` inside the transaction, `VALIDATE` after `COMMIT`, and drops after validation;
- `lock_timeout`, idempotency guards, no `TRUNCATE`;
- concurrent index changes in 002;
- the advisory-lock key equals `MERGE_LOCK_KEY`;
- the lock is the first statement, then the single-statement drain, then the return signature;
- lock-order `ORDER BY` and `staged_at DESC`;
- staging DDL equals `STAGING_COLUMNS` (42 columns);
- the stats DDL and the merge `INSERT` and `UPDATE` lists equal `STATS_COLUMNS`;
- PostgreSQL 15 features;
- application SQL references existing columns.

**Proof of bite.** Three mutations were made to a copy:
- deleting `OR c.minutes_played > 130` from 003;
- appending a `TRUNCATE` to `schema.sql`;
- the regex test fixed alongside.

Result: `4 failed, 37 passed`. The restored files give `139 passed`.

### Contract suites (step 5)

**`tests/test_typing_contract.py` (27 tests):**
- the three documented rules (postponed annotations, full annotations including closures, `Final`
  module constants), each parametrized over the 7 package modules. That gives the documented 21
  ("3 per module").
- `test_every_package_marker_is_empty` (ESSENTIALS §1.2: the markers must have no side effects).
- 5 detector self-tests that feed deliberate violations to the rule functions.

Interpretations:
- **TC1 — Upper-case names.** "Module-level UPPER_CASE" is matched as `^_?[A-Z][A-Z0-9_]*$`, so
  private constants such as `_FIXTURE_PREFIX_WIDTH` must also be `Final`.
- **TC2 — Exemptions.** `TypeVar` is exempt (R-TYPE-3). So is the first parameter of a
  non-static method when it is named `self` or `cls`.

**`tests/test_agent_contract.py` (39 tests):**

| AGENT rule | Test(s) |
|---|---|
| F2 | `test_only_sanctioned_exception_handlers_exist`: set equality with `SANCTIONED_HANDLERS`, i.e. (module, enclosing function, unparsed exception type) for H1–H5 |
| F2, F3 | `test_every_handler_records_or_reraises`: a call or `raise` in every handler body; no bare `except`; no `contextlib.suppress` |
| H3 ordering | `test_statement_timeout_is_caught_before_retryable_errors`; `putconn` in `finally` |
| F6 | `test_cli_exit_code_signals_an_incomplete_slate`: `main()` returns 1 when a fixture timed out, driven through stubs |
| P1 | `test_pure_modules_import_no_io_libraries` (×5) |
| P2 | `test_pure_functions_accept_no_database_or_session_handles` (×5) |
| P5 | `test_pure_modules_read_no_clock_environment_or_random_state` (×5): no `datetime.now`, `time.*`, `os.environ`/`getenv`, `random.*` or `np.random.*` calls |
| P3 | `test_with_transaction_is_the_single_connection_acquisition_site`; `test_fetch_and_parse_complete_before_the_transaction` (×2): calls precede `_with_transaction`, and the closure contains no fetch or parse |
| P4 | `test_referee_job_defers_its_database_driver_import`: AST check, plus a subprocess importing the pure core with `psycopg2` blocked |
| Z1 | `test_no_placeholders_or_truncation_markers`: placeholder markers, `pass`/`...` bodies outside `Protocol` classes, `raise NotImplementedError`; scans package, tests, SQL and CI |
| G1 | `test_engine_contains_no_raw_moment_subtraction`: any `a - b**2`, `a - b*b`, `a - outer(…)` or `a - square(…)` in `engine.py`; `test_return_moments_documents_its_condition_number_bound` |
| A3 | `test_risk_defaults_and_numerical_bounds_are_unchanged`: every `InstitutionalAssetManager` default, plus `MIN`/`MAX_RELATIVE_VARIANCE`, `MAX_MATCH_MINUTES` and `DEGENERATE_LOSS_PROBABILITY` |
| A4 | `test_no_secrets_in_repository_files`: Telegram-token, live-key, inline-password DSN and private-key patterns in every repository file except `.env` (gitignored) and virtualenvs. `${VAR}` placeholders are allowed. |
| (all) | `test_contract_detectors_flag_deliberate_violations` (×12): each detector fires on a deliberate violation |

- **AC1 — No clamp detector.** AGENT F5 ("the only clamp") has no static test. Distinguishing a
  clamp from legitimate bounds handling (for example SLSQP's `np.clip` back onto the box, or
  `padj_scale`'s documented [1, 99] clip) needs judgement. It is enforced by review only.
- **AC2 — Count above target.** The documented breakdown is 27 agent-contract tests. This
  reconstruction has 39, because P5, A3, A4, F6 and the detector self-tests are counted separately.
  None is a duplicate.

**Proof of bite on a scratch copy:**

| Mutation | Result |
|---|---|
| A silent `except ZeroDivisionError: pass` in `engine.py`, `import requests` in `transform.py`, and a placeholder marker in `fitting.py` | `3 failed, 35 passed` |
| (same, after fixing the detector) | The marker was **not** caught at first. The regex's final `\b` was in a non-raw string, so it was a backspace. Fixed, and a text-marker detector case added. The marker alone then gives `1 failed, 38 passed`. |
| Restored | `205 passed` |

### Step 6 — Telegram alerts (NEW, not part of v3.3)

The maintainer asked for a Telegram connection that sends each high-value opportunity as it is found,
with the API key kept out of the code base and out of git.

**What it does:**
- `TelegramNotifier.notify(report)` sends one plain-text message per `report.directives` entry.
- Every message starts with `PAPER ONLY - model not validated, no PRD go-live gate passed`.
- It never sends `skipped_directives` (sub-minimum stakes) or skipped markets.
- It never places a bet (AGENT A5).
- **"High value"** means every directive the allocator emits (EV > `alpha_threshold` = 0.02, stake >=
  `min_stake`). `min_ev_per_unit` can raise the bar. This was the default offered to the maintainer,
  who did not choose otherwise.

**Credentials:**
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are read from the process environment first, then from
  the repository-root `.env`.
- `.env` is gitignored (`git check-ignore -v .env` → `.gitignore:11:.env`). Only `.env.example`, with
  empty values, is committed.
- The token is `repr=False` on the config and appears in no log or exception message.
- `test_no_secrets_in_repository_files` fails the suite if a token-shaped string lands in any
  repository file other than `.env`.

**Sanctioned handler H6 (new):**
- `TelegramNotifier._call` catches `requests.RequestException` and re-raises `TelegramError` with the
  URL removed (`from None`). Telegram puts the token in the URL path, and `requests` quotes that URL
  in connection-error messages, so without H6 every network failure would print the token.
- H6 is in `SANCTIONED_HANDLERS` and the AGENT.md F2 table. AGENT.md requires a human maintainer's
  approval for a new handler: **open question Q6.**
- Proof of bite: without H6, `test_transport_errors_are_sanitised` fails (`1 failed, 11 passed`).

**Layering:**
- ESSENTIALS §1.2 says `pipeline.py` is the only network module. `alerts/telegram.py` is a second one,
  so CLAUDE.md's I/O-isolation line was updated.
- The module imports `engine` and `market_scanner`, while the pure modules import nothing from it.
- It is not in `PURE_MODULES`.
- The typing contract now covers 8 modules (24 rule tests).

**What is not done:**
- **Nothing calls `notify` in production.** The allocation process that would call `allocate` on live
  quotes, and the odds poller that would feed it, are **[N]**. Until they exist, alerts come only
  from `--demo`, which uses synthetic quotes and says so in the first message.
- **No duplicate memory across restarts.** The record of sent directives lives in memory only, so a
  restarted process can repeat an alert; a persistent store is [N].
- **No player names.** Messages show IDs, because `StakingDirective` carries IDs only. A name lookup
  needs the database [N].

**Verification:**
- 12 offline tests against a fake HTTP session.
- One live call to `api.telegram.org` with a deliberately fake, well-formed token returned `getMe
  rejected by Telegram (HTTP 401): Unauthorized`. That shows the endpoint is reachable from this
  machine, and that the error path is sanitised.
- **The maintainer's own bot has not been contacted.** The token pasted in chat must be revoked and
  was written nowhere. `--check` with the new token in `.env` is the remaining step.

### Step 7 — High-value bet screen (NEW, not part of v3.3)

The maintainer defined a **high-value bet**, and only those are sent to Telegram. The thresholds were
chosen in this session: 4% minimum return, and "edge survives" as the meaning of the 60% rule.

A high-value bet must pass all three of these:
1. **Mispriced.** The model's win probability, given the bet settles, is above the book's devigged
   probability for that side, or above 1/odds when the book quotes one side only.
2. **At least 4% expected profit per 1 unit staked:** `HighValueCriteria.min_ev_per_unit = 0.04`.
3. **The edge survives.** In **more than** 60% of 1,000 simulated match conditions, the bet still
   returns at least 4% at the book's price. Exactly 600/1000 fails.

**What a simulated condition varies** (only what the model has data for, as the maintainer chose):
- the player's situation, drawn from `LineupExpectation`: start, substitute or absent;
- the match rate λ ~ Gamma(shape, rate) from the projection. This carries game-state
  overdispersion k, posterior uncertainty and the referee adjustment.

Given a condition, the count is Poisson(λ·t), so the EV in that condition is exact. Averaging the
conditions reproduces the NB prices; a test checks this. Each directive's simulation is seeded by
SHA-256 of its key, so re-runs give the same count.

**Not simulated** (no data): weather, formation and fitness. Referee *uncertainty* is not simulated
either, because the referee job produces point estimates only.

**Interpretations:**
- **V1 — Void conditions never count as surviving.** A condition in which the bet is void (EV 0), for
  example the player not appearing, never survives. So a player with a 40% chance of missing the
  match cannot qualify.
- **V2 — Mispricing is nearly implied by EV.** With a normal book margin, EV > 0 already implies the
  model probability is above the devigged book probability. Test 1 only bites when the book's two
  sides sum to less than 1, i.e. an arbitrage.
- **V3 — Consequence: the 60% rule favours Unders.** An Over profits only when the player has an
  above-average match, and the Gamma distribution puts more than half its mass below its mean. On
  the documented reference case:
  - Over 1.5 (+6.0%) survives 539/1000 and Over 2.5 (+6.2%) 457/1000, so neither qualifies;
  - Under 4.5 at +8% survives 684–698/1000 and qualifies.
- **V4 — A thin edge rarely qualifies.** A bet just above 4% rarely passes, because any condition
  worse than average pushes it below 4%. Even a very stable player (shape 400) at +4.5% survives only
  727/1000.
- **V5 — The screen does not change the allocator.** It filters alerts only. `allocate`, its risk
  defaults and the stakes are unchanged (AGENT A3).
- **V6 — Pure module.** `value_screen.py` follows the pure-module rules (no I/O, randomness only
  through an explicit `rng`), and was added to `PURE_MODULES` in the agent contract.

**Proof of bite on a scratch copy:**

| Mutation | Result |
|---|---|
| Accept exactly 60% | `1 failed` |
| Drop the 4% gate | first **not** caught, because the low-EV test bet also failed the simulation; after adding `test_high_value_requires_all_three_conditions`: `1 failed, 16 passed` |
| Restored | `245 passed` |

## Open questions for the maintainer

| ID | Question | Current behaviour |
|---|---|---|
| Q1 | Should `QuantEngine` accept `beta1`/`beta2` of 0 or below (for example a fitted exponent near zero)? | Rejected with `ValueError` (E10). |
| Q2 | Should `LiveOddsQuote` reject a non-finite price at construction (fail early), or accept it so that `_candidates` skips the side, as AGENT §3.3 describes? | Rejected at construction (S4). |
| Q3 | Should a quote whose `captured_at` is after `now` abort the whole `allocate` call, or be skipped and recorded per key? | **Answered 2026-10-01 (remediation S5):** skipped, counted in `AllocationReport.future_quotes` and recorded in `report.skipped`; other markets are still allocated. |
| Q4 | Stakes: floor to the cent (never exceeds `max_stake`) or round to the cent (PRD R-ALLOC-5)? | Floored (S7). |
| Q5 | How should the backtest get referee data for `fouls` and card markets (Gate 2)? Add an optional referee to `PlayerMatchObservation`? | A neutral referee is used (B3). |
| Q6 | Approve sanctioned handler H6 (`TelegramNotifier._call`, token-sanitising re-raise)? AGENT.md requires a maintainer's approval for any new handler. | Added and enforced; marked "awaiting formal approval" in AGENT.md. |

## Change log

| Commit | Content |
|---|---|
| `b085b3f` | Step 1: package skeleton, `engine.py`, core engine tests |
| `4bc7167` | Step 2: `market_scanner.py`, allocator tests (57 passed) |
| `1ce1cfd` | RECONSTRUCTION.md covering steps 1–2 |
| `1a0ce66` | Fix E1, E5a, E6a in `engine.py`; two regression tests (62 passed) |
| `fe7a261` | Step 3: transform, pipeline, fitting, referee_job, backtest, and their tests (123 passed) |
| `a25ab8c` | Step 4: schema.sql, migrations 002–003, ci/bootstrap.sh, .gitattributes, SQL structure tests (139 passed) |
| `a6eaa4a` | Step 5: typing and agent contract suites; `EXPECTED_TESTS=205` (205 passed; preflight passed) |
| `1dc8c2d` | Step 6: Telegram alerts, H6, `.env.example`, CLAUDE.md/AGENT.md updates; `EXPECTED_TESTS=220` (220 passed) |
| (uncommitted) | Remediation plan of 2026-10-01: 21 changes (I1–I3, S1–S7, V1–V3, E1–E4, K1–K2, D1–D2), migration 004, AGENT.md/CLAUDE.md updates; `EXPECTED_TESTS=307` (307 passed). See [Remediation](#remediation-2026-10-01). |

### Final verification (2026-10-01, CPython 3.14.6, Windows 11, Git Bash)

| Command | Result |
|---|---|
| `.venv\Scripts\python.exe -m compileall -q prop_alpha tests` | exit 0, no output |
| `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider` | `220 passed` |
| `bash ci/bootstrap.sh --preflight` | `preflight passed; nothing created or installed` |
| `bash ci/bootstrap.sh` (no database URL) | stages 1–5 passed; `junit totals: {'tests': 220, 'failures': 0, 'errors': 0, 'skipped': 0}`; `stage 6/6 skipped: PROP_ALPHA_CI_DATABASE_URL is unset`; `SUCCESS`, exit 0 |

**Test count by suite:**

| Suite | This reconstruction | Documented v3.3 |
|---|---|---|
| Core | 85 | 82 |
| Integrity | 41 | 38 |
| Agent contract | 39 | 27 |
| Typing contract | 27 | 21 |
| Concurrency | 13 | 12 |
| Alerts (new in step 6) | 12 | none |
| **Total** | **220** | **180** |

The counts differ because the tests were reconstructed independently, not because these are the
original 180 with 40 added. Documented test names that were reconstructed: all of them. None was
omitted.

### AGENT §2.1 gate record for the E1/E5a/E6a engine change

| Gate | Outcome |
|---|---|
| G1 | No squared-moment subtraction introduced. The changes remove a rounding subtraction (`1 - p_win - p_lose`) rather than add one. |
| G2 | No new input to a division, log or distribution parameter. |
| G3 | The NB predictive distribution and `v = 1/α + 1/k + 1/(αk)` are unchanged. `p_void` and `p_no_appearance` are analytically identical to before, and differ only by removing about 1e-16 of round-off. |
| G4 | No handler added. Validation still raises. |
| G5 | Allocator untouched. The reference 1.94% / 0.04% test still passes. |
| G6 | Proof of bite. Pre-fix `engine.py` with the new tests: `3 failed, 59 passed`. Fixed: `62 passed`. |

## Remediation (2026-10-01)

Implements the remediation plan written from the audit of `e67d8de` (suite then 245 passed). Twenty-one
changes landed. B1–B3 (projection refresh, bet ledger, executor) were left unbuilt at the maintainer's
direction and stay **[N]**.

### Maintainer decisions (2026-10-01)

| # | Decision | Applied as |
|---|---|---|
| 1 | Model-risk loss floor 0.03 | `market_scanner.MODEL_RISK_LOSS_FLOOR`; required `model_risk_loss_floor` keyword on `InstitutionalAssetManager`; required `loss_floor` on `HighValueCriteria`; added to the AGENT A3 list. Not backtested. |
| 2 | At least 200 matches to fit k | `fitting.MIN_GAME_STATE_OBSERVATIONS`; `fit_game_state_dispersion` raises below it and when no overdispersion is detected |
| 3 | Raise above `MAX_RELATIVE_VARIANCE` | `predictive_gamma` and `rate_posterior` raise `ProjectionError`; the lower `[MATH_CLAMP]` clamp stays. AGENT F5, G2, V1 and the §3.3 catalogue updated. |
| 4 | Land breaking signatures without shims | `QuantEngine(*, game_state_k)`, `predictive_gamma(..., multiplier_relative_variance)`, `project_card_market(card_multiplier_relative_variance=...)`, `LiveOddsQuote.void_rule` (no default), `allocate(..., *, now, exposure)`, `HighValueCriteria(loss_floor=...)`, `StakingDirective` (two new fields) |
| 5 | `notify` logs and skips a directive without a projection | `logger.error` and `continue`. No exception handler is involved (the old code raised directly, not inside an `except`), so the sanctioned set is unchanged: H1–H6. |
| 6 | Keep `MAX_RETRY_AFTER_S = 2.0` and 200 batches per merge | `pipeline.MAX_RETRY_AFTER_S`; `merge_staging_player_feed(p_max_batches integer DEFAULT 200)`. Both still need the vendor contract and a timed merge on a real server. |

### Deviations from the plan text

| Item | Plan | Implemented | Why |
|---|---|---|---|
| S2 | "Under 2.5 at 1.06 gets no directive at floor 0.03" | The allocator test uses 1.05. At 1.06 the haircut EV is 0.97 x 0.06 - 0.03 = 0.028, above `alpha_threshold` 0.02, so the allocator still stakes it; the value screen (4% minimum, V1) stops the alert. | The plan's sentence does not hold arithmetically; the audit prototype itself tested 1.05. Removing it in the allocator at 1.06 would need a floor of about 0.038 or a higher `alpha_threshold`: a maintainer decision. |
| S2 | Haircut EV for the candidate filter and Kelly bound | Also applied to the QP mean vector: each candidate's mean return is reduced by shift x price | Otherwise the filter and the sizing would use different EVs for the same bet. |
| S3 | `scipy.special.roots_genlaguerre` | Golub-Welsch on the Laguerre Jacobi matrix (`engine.game_state_quadrature`) | `roots_genlaguerre` returns NaN weights for k above about 170; the new rule is exact to about 1e-12 for k in [0.05, 1e9] and matches scipy to 1e-14 at k = 6. |
| S1/S3 | Fixture cap via `_apply_residual_cap` after optimisation | Fixture cap, per-market budget (net of open exposure) and per-slot Kelly budget are constraints of one QP per fixture; only the slate cap scales afterwards | S3 replaces per-key optimisation, so the fixture cap belongs inside the joint problem. |
| I2 | Retry `ConnectionError`/`Timeout` in `get_json` with `try/except` | urllib3 retries connects only (`connect=max_retries, read=0, status=0`); `get_json` retries vendor responses without a `try` | The plan's code added two unsanctioned exception handlers (including `except ValueError: return None`, an F3 violation). A failed connect never reaches the vendor, so it spends no quota. A read timeout now fails the fixture (H1) instead of being retried. |
| D1/D2 | `SET lock_timeout = '2s'` in migration 004 | `'5s'` | AGENT M3. |
| E4 | P(under 1.5) = 0.3420 for a 90/70/45 mix | Tested against an independent weighted sum of fixed-minute prices | The plan does not give the projection behind 0.3420. |
| K2 | Per-player values replace the scalar | Per-player overrides; the scalar stays as the fallback | Nothing persists unused bench selections yet, so no caller can produce per-player counts. |

### AGENT §2.1 gate record (engine.py, market_scanner.py)

| Gate | Outcome |
|---|---|
| G1 | `engine.py` forms every relative variance as `expm1(sum of log1p)`; `test_engine_contains_no_raw_moment_subtraction` passes. `fixture_moments` (market_scanner) subtracts `outer(mean, mean)` from per-unit returns bounded by [-1, O - 1], with kappa about 1.005 on the reference case; the proof is in its docstring, as for `return_moments`. |
| G2 | New inputs validated and raising: `multiplier_relative_variance` (finite, >= 0), `game_state_k` (required, finite, > 0) in `QuantEngine`, `FactorProjection` and `game_state_quadrature`, the quadrature node g, `MinutesMixture` minutes and weights, `model_risk_loss_floor` and `loss_floor` in [0, 1), `ExposureBook` amounts (finite, >= 0). |
| G3 | `PropProjection` remains the moment-matched NB with v = (1 + 1/alpha)(1 + 1/k)(1 + v_c) - 1, which equals 1/alpha + 1/k + 1/(alpha k) within 1e-15 when v_c = 0. The allocator prices with the unmatched `FactorProjection`: its quadrature marginal P(over 1.5) = 0.61245 matches the audit's Monte Carlo value. |
| G4 | No exception handler added or widened; `test_only_sanctioned_exception_handlers_exist` passes with H1–H6. Above-cap variance now raises. |
| G5 | Joint mean-variance over all candidates of a fixture, exact moments from `outcome_model` grids, Kelly, position, book-limit, market, slot and fixture constraints, then the slate cap. The reference test (`test_mean_variance_prefers_low_variance_line_at_similar_ev`) gives 1.942% on Over 1.5 and 0.044% on Over 2.5. |
| G6 | Proof of bite on a worktree of `e67d8de`: the 26 new ingestion tests (I1 x18, I2 x4, I3, D2, K2, and the D2 merge count) fail (`26 failed, 12 passed`; the 12 are unchanged tests). Allocator, alert, engine and fitting tests import APIs that do not exist at `e67d8de`, so the old behaviour was reproduced directly there: exposure 5.0%, 9.8%, 14.3%, 18.5% over four cycles (S1); a $20,000 near-certain Under (S2); a future-dated quote aborting the call (S5); two alerts across a price tick (V2); `QuantEngine()` accepted with k = None (E1); a 1e-4 league rate clamped instead of refused (E2); the k fitter returning None in 55 of 300 samples of 20 matches (K1). |

### Verification (2026-10-01, CPython 3.14.6, Windows 11, Git Bash)

| Command | Result |
|---|---|
| `.venv\Scripts\python.exe -m compileall -q prop_alpha tests` | exit 0, no output |
| `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider` | `307 passed` |
| `bash ci/bootstrap.sh --preflight` | `preflight passed; nothing created or installed` |
| `ci/bootstrap.sh` stage 6 (PostgreSQL) | **not run**: no database available. `schema.sql` and `migrations/004_merge_hardening.sql` are **[U]**. Stage 6 now applies 004 for idempotency, then 003 followed by 004 to rehearse the v3.2 upgrade. |

| Suite | Tests |
|---|---|
| Core | 112 |
| Integrity | 65 |
| Agent contract | 42 |
| Typing contract | 33 |
| Concurrency | 20 |
| Alerts | 16 |
| Value screen | 19 |
| **Total** | **307** |

The audit prototype (`audit/hardened_allocator.py`, `audit/check_hardened_allocator.py`, added in `d140c30`)
is superseded by the package code and was deleted; it remains in git history at that commit.
