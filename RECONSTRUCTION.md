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
| `tests/test_core.py` | Test names from AGENT §2.2, §3.3 and PRD; no test code is printed in any document | RECONSTRUCTED | steps 1–2 |

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
  "exposure validation" to `test_integrity.py`. *To be moved in step 3.*
- **T2 — Tests with no documented name:**
  - `test_bimodal_substitution_has_no_absence_void_when_present` (AGENT §3.3 bimodal row);
  - `test_invalid_projection_parameters_fail_closed`;
  - `test_invalid_minutes_fail_closed`;
  - `test_referee_profile_rejects_non_finite_coefficients`;
  - `test_allocate_requires_explicit_utc_time`.
- **T3 — Documented tests missing at end of step 2:** `test_bivariate_marginal_is_equidispersed_poisson`
  (PRD R-MOD-5) and `test_corrupted_projection_is_skipped_never_traded` (PRD R-MOD-4). *To be added in
  step 3.*
- **T4 — Documented figures not pinned:**
  - `test_card_probability_not_distorted_by_variance_floor` checks the closed-form NB probability, not
    the documented 0.1675 → 0.1199 figures. The documents do not give the "representative
    projection" that produced them.
  - `test_predictive_gamma_stable_where_subtraction_form_cancels` asserts the clamped shape `1e6`, but
    does not reproduce the `-1.42e-14` subtraction result.
- **T5 — `test_return_moments_match_closed_form`** uses a synthetic two-state grid. AGENT U2 says
  "(all void rules)". *To be strengthened in step 3.*
- **T6 — Reference allocation pinned.** `test_mean_variance_prefers_low_variance_line_at_similar_ev`
  pins the documented 1.94% / 0.04% split (tolerance ±0.02 percentage points).
  `test_cross_line_covariance_matches_brute_force` sums x over [0, 300), as documented.

## Open questions for the maintainer

| ID | Question | Current behaviour |
|---|---|---|
| Q1 | Should `QuantEngine` accept `beta1`/`beta2` of 0 or below (for example a fitted exponent near zero)? | Rejected with `ValueError` (E10). |
| Q2 | Should `LiveOddsQuote` reject a non-finite price at construction (fail early), or accept it so that `_candidates` skips the side, as AGENT §3.3 describes? | Rejected at construction (S4). |
| Q3 | Should a quote whose `captured_at` is after `now` abort the whole `allocate` call, or be skipped and recorded per key? | Aborts the call (S6). |
| Q4 | Stakes: floor to the cent (never exceeds `max_stake`) or round to the cent (PRD R-ALLOC-5)? | Floored (S7). |

## Change log

| Commit | Content |
|---|---|
| `b085b3f` | Step 1: package skeleton, `engine.py`, core engine tests |
| `4bc7167` | Step 2: `market_scanner.py`, allocator tests (57 passed) |
| `1ce1cfd` | RECONSTRUCTION.md covering steps 1–2 |
| next | Fix E1, E5a, E6a in `engine.py`; two regression tests (62 passed) |

### AGENT §2.1 gate record for the E1/E5a/E6a engine change

| Gate | Outcome |
|---|---|
| G1 | No squared-moment subtraction introduced. The changes remove a rounding subtraction (`1 - p_win - p_lose`) rather than add one. |
| G2 | No new input to a division, log or distribution parameter. |
| G3 | The NB predictive distribution and `v = 1/α + 1/k + 1/(αk)` are unchanged. `p_void` and `p_no_appearance` are analytically identical to before, and differ only by removing about 1e-16 of round-off. |
| G4 | No handler added. Validation still raises. |
| G5 | Allocator untouched. The reference 1.94% / 0.04% test still passes. |
| G6 | Proof of bite. Pre-fix `engine.py` with the new tests: `3 failed, 59 passed`. Fixed: `62 passed`. |
