# prop_alpha — Product Requirements Document
### v3.1 (Production-Hardened) · Tier-3/4 Football Player Prop Micro-Markets

| Field | Value |
|---|---|
| Document status | Draft for Strategy / Risk sign-off |
| Code baseline | `prop_alpha` v3.1 (94 unit and integration-fake tests passing) |
| Schema baseline | `schema.sql` + `migrations/002_staging_and_indexes.sql`, PostgreSQL 15+. **Not yet executed against a live instance** |
| Normative language | MUST / MUST NOT / SHOULD as defined in RFC 2119 |
| Implementation tags | **[T]** implemented and covered by tests · **[U]** implemented, not executed against PostgreSQL · **[N]** not built; this PRD mandates it |

---

## SECTION 1: SYSTEM SCOPE & VALIDATION HYPOTHESES

### 1.1 Scope

**In scope.** The system produces pre-match staking *directives* for individual player count markets:

- Markets: `tackles`, `fouls`, `fouls_drawn`, `yellow_card`, `red_card`.
- Competitions: EFL League One, EFL League Two, Serie C (Gironi A/B/C), Primera Federación, Segunda Federación.
- Book lines: whole or half lines only (`line_value * 2` integral, enforced by `CHECK` on `odds_lines.line_value`).

**Out of scope, which this document therefore does not authorise:**

- In-play trading.
- Team-level markets.
- Automated order submission. **No execution component exists in v3.1**; `InstitutionalAssetManager.allocate` returns an `AllocationReport` and places nothing.
- Account management.
- Any mechanism that circumvents bookmaker stake limits or account restrictions (multiple accounts, third-party placement). These breach operator terms and, in many jurisdictions, law.

**Preconditions.** Legality of sports betting, and of automated staking, MUST be confirmed for every jurisdiction in which the fund and its accounts operate before any directive is acted on.

### 1.2 Epistemic Status

Every source of return claimed for this system is a **hypothesis**. As of v3.1:

- no hypothesis in §1.3–§1.4 has been tested on market data;
- no model output has been scored out-of-sample;
- no paper bet has been recorded.

The system MUST NOT risk capital until every gate in Section 5 has passed. Each hypothesis below is stated with an explicit **falsification test**. A hypothesis that fails its test removes the corresponding source of edge, and the markets depending on it MUST be disabled.

The two hypotheses are necessary but not sufficient. An edge exists only if (a) the book behaves as hypothesised **and** (b) the model is better calibrated than the book on the same events. Condition (b) is tested in Gate 2.

### 1.3 Hypothesis H1 — Index-Linked Derivative Pricing

**Statement.** For tier-3/4 player count props, the book's implied player mean is a mechanical function of liquid macro-market prices and a static usage share, with no conditioning on match-specific player covariates.

**Stage 1 — team defensive workload from macro lines.**

\[
\lambda^{\text{def}}_{\text{team}} = \alpha + \beta_1\,\lvert \text{AH}_{\text{line}} \rvert + \beta_2\,\text{TG}_{\text{line}} + \beta_3\,(1 - \hat p_{\text{poss}})
\]

Here \(\text{AH}_{\text{line}}\) is the Asian Handicap line, \(\text{TG}_{\text{line}}\) the Total Goals line, and \(\hat p_{\text{poss}}\) a possession share inferred from the handicap magnitude.

**Stage 2 — flat usage-slice allocation.**

\[
\lambda_i^{\text{book}} = \lambda^{\text{def}}_{\text{team}} \cdot s_i, \qquad s_i = \frac{\text{Tkl}_i^{\text{YTD}}}{\sum_{j \in \text{team}} \text{Tkl}_j^{\text{YTD}}}
\]

**The linear allocation defect.** The true match-level share is \(s_{i,m} = s_i + \delta_{i,m}\). The deviation \(\delta_{i,m}\) is driven by covariates \(z_{i,m}\) that Stage 2 discards:

- Opponent attacking volume routed through player \(i\)'s zone.
- Expected game-state path: a trailing side presses, trading tackles for interceptions and fouls; a leading side defends deep and accumulates tackles.
- Role changes forced by rotation in thin squads.

The book's per-match allocation error is

\[
\varepsilon_{i,m} = \lambda^{\text{def}}_{\text{team},m}\,\delta_{i,m}, \qquad E[\varepsilon_{i,m}] = 0 \ \text{unconditionally}, \qquad E[\varepsilon_{i,m} \mid z_{i,m}] \neq 0 .
\]

The defect is therefore invisible in aggregate: the book is unbiased on average across the season. It is exploitable only conditionally, by a model that estimates \(E[\delta_{i,m} \mid z_{i,m}]\).

**Falsification test for H1 (per book, per league).**

1. From each two-way quote, devig (`devig_two_way`) and invert the fair Over probability under a Poisson assumption to obtain a book-implied mean \(\hat m^{\text{book}}_{i,m}\). For a half line \(L\), solve \(1 - F_{\text{Pois}}(\lfloor L \rfloor;\ \hat m) = p^{\text{fair}}_{\text{over}}\).
2. Fit
\[
\ln \hat m^{\text{book}}_{i,m} = a + b \ln \lambda^{\text{def}}_{\text{team},m} + c \ln s_i + d^{\top} z_{i,m} + u_{i,m}.
\]
3. H1 predicts \(d = 0\). **Reject H1** for that book and league if a Wald test gives \(p < 0.01\) against \(H_0: d = 0\), i.e. the book already prices the covariates.
4. Independently, the same covariates MUST show \(d \neq 0\) in an outcome regression (Poisson GLM on realised counts). If they do not, the covariates carry no information and the defect, even if present, cannot be exploited.

### 1.4 Hypothesis H2 — Game-State Overdispersion

**Statement.** The book prices from a Poisson distribution, which imposes equidispersion \(\text{Var}(X) = E[X]\). Realised player counts follow a Poisson–Gamma mixture:

\[
X_{i,m} \mid \Lambda_{i,m} \sim \text{Pois}(\Lambda_{i,m}\, t_{i,m}), \qquad \Lambda_{i,m} = \mu_i\, G_m, \qquad G_m \sim \text{Gamma}(k, k),\ E[G_m] = 1,
\]

with \(t_{i,m}\) the exposure in 90-minute units. Marginally \(X_{i,m} \sim \text{NB}\!\left(r = k,\ p = \tfrac{k}{k + \mu_i t_{i,m}}\right)\) and

\[
\text{Var}(X_{i,m}) = \mu_i t_{i,m} + \frac{(\mu_i t_{i,m})^2}{k}, \qquad D_{i,m} = \frac{\text{Var}(X_{i,m})}{E[X_{i,m}]} = 1 + \frac{\mu_i t_{i,m}}{k} > 1 \iff k < \infty .
\]

**Consequence for pricing (worked example, \(\mu t = 1.8\), \(k = 4\), \(D = 1.45\)).**

| Line | Poisson \(P(X > L)\) | NB \(P(X > L)\) | Poisson − NB |
|---|---|---|---|
| 0.5 | 0.8347 | 0.7738 | +0.0609 |
| 1.5 | 0.5372 | 0.4930 | +0.0442 |
| 3.5 | 0.1087 | 0.1398 | −0.0311 |

Overdispersion moves mass from the centre to both tails. A Poisson-priced book therefore **overprices Overs on lines below the mean and underprices Overs on lines above it**. The sign of the edge is line-specific and MUST be computed exactly per quote (§2.4); it MUST NOT be assumed.

**Falsification test for H2.**

1. On out-of-sample expected counts \(\hat\mu_{i,m}\), the method-of-moments estimator (`fit_game_state_dispersion`) is
\[
\widehat{k^{-1}} = \frac{\sum_{i,m} \left[(y_{i,m} - \hat\mu_{i,m})^2 - y_{i,m}\right]}{\sum_{i,m} \hat\mu_{i,m}^2} .
\]
Compute a fixture-clustered bootstrap 95% interval. **Reject H2** if the interval contains 0; the function returns `None` when the point estimate is ≤ 0. To prevent posterior uncertainty from inflating \(\widehat{k^{-1}}\), restrict the sample to players with ≥ 20 full matches.
2. Book-side test: for a player-market quoted at ≥ 2 lines by the same book, fit a single Poisson mean to all devigged line probabilities. If one \(m\) reproduces every line within quoting precision, the book is equidispersed as H2 requires. If a Negative Binomial fits materially better, the book already prices dispersion and the H2 edge is absent at that book.

---

## SECTION 2: CORE FUNCTIONAL REQUIREMENTS & ARCHITECTURAL PATTERNS

### 2.1 Parallelized Data Ingestion Engine

**Components:**

- `prop_alpha.ingestion.pipeline.ConcurrentIngestionEngine(client, pool, kappas, max_workers=8, max_db_attempts=3, retry_base_delay_s=0.05, bench_sub_probability=0.35)` [T]
- `VendorClient` [T]
- `TokenBucketRateLimiter` [T]
- `build_pool` [U]
- CLI: `python -m prop_alpha.ingestion.pipeline {stats|lineups} --workers W --deadline S` [T for argument parsing]

**Requirements.**

- **R-ING-1** — Slates MUST be processed by a `concurrent.futures.ThreadPoolExecutor` owned by the engine. Sequential per-fixture loops are prohibited. `run_slate(fixture_uids, job, deadline_s, merge)` de-duplicates fixture identifiers while preserving order, and returns a `SlateReport` with these fields:
  - `results: dict[str, FixtureResult]`
  - `timed_out: list[str]`
  - `merge: MergeResult | None`
  - `merge_error: str | None`
  - `elapsed_s`

  It also exposes the properties `succeeded`, `failed` and `fully_successful`. [T]
- **R-ING-2** — Each worker thread MUST use its own `requests.Session`, created lazily in `threading.local()`, because `requests.Session` is not documented as thread-safe. All sessions are registered and closed by `VendorClient.close()`. [T]
- **R-ING-3** — A worker MUST NOT hold a database connection during network I/O or parsing.
  - **Enforcement.** Enforced structurally: `_stats_task` and `_lineup_task` complete `get_*`, `parse_*` and `build_staging_rows` before calling `_with_transaction`, which is the only code that calls `pool.getconn()`.
  - **Verification.** `test_no_connection_is_held_during_network_io`. [T]
- **R-ING-4** — Every connection MUST come from a `psycopg2.pool.ThreadedConnectionPool` with `maxconn ≥ max_workers + 1`. The spare connection is reserved for the merge, because timed-out workers may still hold theirs. The constructor raises `ValueError` otherwise. Connections carry `options="-c statement_timeout=10000 -c lock_timeout=2000"`. [T for sizing; U for options]
- **R-ING-5** — The vendor request rate MUST be governed by one `TokenBucketRateLimiter` shared across all threads, so contract limits are never exceeded. Consequently, slate wall time for \(N\) fixtures, \(W\) workers, rate \(r\), burst \(B\) and per-request latency \(L\) is
\[
T_{\text{slate}} \gtrsim \max\!\left( \frac{\max(N - B,\ 0)}{r},\ \left\lceil \frac{N}{W} \right\rceil \bar L \right).
\]
Parallelism removes the latency term \(N \bar L\) of a sequential loop. It cannot remove the rate term. If the lineup window is rate-bound, the contracted rate (`DATA_VENDOR_RPS`) MUST be renegotiated; adding workers will not help. [T]
- **R-ING-6** — Fixtures MUST be isolated from one another. `_run_isolated` converts any exception into a failed `FixtureResult`, so one fixture's failure, timeout or malformed payload cannot affect another's transaction or result. [T]
- **R-ING-7** — `deadline_s` bounds how long the caller waits for the slate:
  - Unfinished fixtures are reported in `SlateReport.timed_out`, and their pending futures are cancelled.
  - A task that is already running continues. Its per-fixture transaction commits or rolls back independently, and a late commit is drained by the next merge.
  - Callers MUST treat `timed_out` as "not confirmed", not "not written". [T]

**T-60 pre-kickoff lineups job (`job="lineups"`).**

- **R-LUP-1** — `parse_lineup_payload(payload, bench_sub_probability)` MUST reject a team sheet if:
  - either side is missing;
  - either side does not have exactly 11 starters;
  - any player appears twice across both sides. [T]
- **R-LUP-2** — `LINEUP_UPSERT_SQL` writes `lineup_expectations` with `source = 'confirmed_team_sheet'`:
  - **Starters:** \(p_{\text{start}} = 1,\ p_{\text{sub}} = 0\).
  - **Bench:** \(p_{\text{start}} = 0,\ p_{\text{sub}} = \texttt{bench\_sub\_probability}\).
  - **Expected minutes:** `exp_minutes_start` and `exp_minutes_sub` are set to the player's historical mean minutes when starting and when substituted, from `player_match_stats` (`minutes_played > 0`), defaulting to 85.0 and 20.0.

  [U]
- **R-LUP-3** — In the same transaction, `LINEUP_EXCLUDE_SQL` MUST set \(p_{\text{start}} = p_{\text{sub}} = 0\) for every existing `lineup_expectations` row of that fixture whose `player_id` is not on the sheet. Verification: `test_lineup_job_upserts_and_zeroes_absent_players`. [T with fakes; U in SQL]
- **R-LUP-4 (effect on pricing)** — A zeroed player has \(p_{\varnothing} = 1\). The effect depends on the book's void rule:
  - Under `VOID_IF_NO_APPEARANCE` or `VOID_IF_NOT_STARTING`, every side has \(p_{\text{void}} = 1\), so \(\text{EV} = 0 \le \alpha\) and no candidate is generated.
  - Under `NO_VOID`, the Under becomes a certain win. This MUST be caught by the degenerate-outcome guard (R-ALLOC-7) and routed to manual review, never auto-staked. [T]

### 2.2 Relational Transaction Staging Layer

**Objects:**

- `prop_alpha.staging_player_feed` (UNLOGGED) [U]
- `prop_alpha.staging_rejects` [U]
- `prop_alpha.merge_staging_player_feed() RETURNS TABLE (fixtures_merged integer, player_rows_merged integer, rows_rejected integer)` [U]

**Requirements.**

- **R-STG-1** — Workers MUST write parsed data only to `staging_player_feed`, via one multi-row `INSERT` per fixture (`INSERT_STAGING_SQL`, `psycopg2.extras.execute_values`, `page_size=1000`), in the same transaction as the `raw_payloads` insert.

  The staging table has no keys, constraints or indexes, so concurrent writers contend only on heap extension.

  Row layout (`STAGING_COLUMNS`, 41 columns): one **fixture header row** per fixture, with all 30 player columns `NULL`, followed by one row per player. The header row lets fixtures with no player data (e.g. scheduled or postponed) still be created by the merge. [T]
- **R-STG-2 (UNLOGGED semantics)** — `staging_player_feed` is not WAL-logged. After a crash its contents are truncated during recovery, and it is not replicated to physical standbys.
  - **Durability.** Durability is provided by `raw_payloads` (logged, `payload JSONB`, deduplicated on `payload_sha256`). Rows lost from staging MUST be rebuilt by re-running `parse_fixture_payload` → `build_staging_rows` over `raw_payloads`.
  - **Replicas.** Analytics replicas MUST NOT be relied on to show staging state.
- **R-STG-3 (atomic drain)** — The merge MUST be one invocation of `merge_staging_player_feed()`. It is a single function call executing multiple statements inside one transaction, in this order:
  1. `pg_advisory_xact_lock(hashtext('prop_alpha.merge_staging_player_feed'))` serialises mergers. The lock is released automatically at commit or rollback.
  2. `WITH drained AS (DELETE FROM prop_alpha.staging_player_feed RETURNING *) INSERT INTO claimed_feed SELECT * FROM drained;` claims rows into an `ON COMMIT DROP` temporary table.
  3. Rows with unknown team UIDs or an invalid `fixture_status` are moved to `staging_rejects`, with reason `'unknown team uid or invalid fixture status'`.
  4. Player rows violating core constraints are moved to `staging_rejects` with reason `'player row violates core constraints'`. The checked constraints are:
     - `tackles_won > tackles_attempted`
     - `duels_won > duels_total`
     - card counts out of range
     - negative minutes
     - possession outside [0, 100]
     - `team_uid` not one of the two sides
  5. Upsert `referees`, `fixtures`, `players` and `player_match_stats`, each via `SELECT DISTINCT ON (<conflict key>) … ORDER BY <conflict key>, staged_at DESC`.
  6. `RETURN QUERY SELECT v_fixtures, v_rows, v_rejected`.
- **R-STG-4 (claim semantics)** — Under MVCC, the `DELETE` in step 2 operates on the snapshot taken at statement start:
  - It removes exactly the staging rows committed before that instant.
  - Rows committed by concurrent writers afterwards are invisible to it, survive, and are drained by the next merge.
  - No row is merged twice, and no committed row is lost.
- **R-STG-5 (TRUNCATE prohibited)** — `TRUNCATE` MUST NOT be used on `staging_player_feed` in any code path, for three reasons:
  1. **Lock conflict.** `TRUNCATE` acquires `ACCESS EXCLUSIVE`, which conflicts with the `ROW EXCLUSIVE` lock every ingestion `INSERT` holds. Every writer blocks for the duration of the merge transaction. Because PostgreSQL grants lock requests in queue order, writers arriving after the `TRUNCATE` request also queue behind it, even before it is granted. With `lock_timeout = 2000` this converts a merge into a burst of worker timeouts during the lineup window.
  2. **Structural data loss.** A merge that reads staging and then truncates destroys every row committed between the read and the truncate. Those rows are neither merged nor retained, and nothing records their loss.
  3. **MVCC violation.** `TRUNCATE` is not MVCC-safe. A concurrent transaction whose snapshot predates the truncation sees the table as empty once the truncation commits.
- **R-STG-6 (lock ordering)** — Every upsert inside the merge orders by its conflict key, so concurrent transactions touching the same rows acquire row locks in a consistent order. `prop_alpha.calibration.referee_job.run_referee_job` updates `referees` ordered by `referee_id`, while the merge orders by `vendor_referee_uid`. It therefore MUST take the same advisory lock before its `UPDATE`, which serialises the two and removes the lock-order deadlock (`MERGE_LOCK_KEY`, `test_referee_job_shares_merge_advisory_lock_key`). [T]
- **R-STG-7** — `merge_staging` MUST run under `_with_transaction`, so it inherits the retry and fail-closed rules of Section 3. On failure, `SlateReport.merge_error` is set and the staged rows remain for the next merge, because the rolled-back `DELETE` restores them. [T]

### 2.3 Hardened Predictive Modeling Block

**Components:** `prop_alpha.modeling.engine.QuantEngine`, `PropProjection`, `LineupExpectation`, `ProjectionError`, `VoidRule`. [T]

**Model hierarchy (per player \(i\), target fixture).**

\[
\Lambda_i \sim \text{Gamma}(\alpha_n, \beta_n), \qquad \alpha_n = \alpha_0 + \sum_j x_j, \qquad \beta_n = \beta_0 + \sum_j \frac{\text{min}_j}{90}
\]
\[
G \sim \text{Gamma}(k, k), \qquad X \mid \Lambda_i, G \sim \text{Pois}(c\,\Lambda_i\,G\,t)
\]

Here \(\alpha_0 = \beta_0 \bar\lambda_{\text{pos}}\), with the default \(\beta_0 = 6\) pseudo-90s (`positional_prior`). The referee multiplier is \(c = \widehat{\text{SC}}_r^{\,\beta_1}(1 \mp \widehat{\text{HBR}}_r)^{\beta_2}\), applied only to `fouls` and `fouls_drawn`, and for card markets chained with the card propensity. Exposure is \(t = \text{minutes}/90\).

**Moment matching.** The product \(c\Lambda_i G\) is matched to a single Gamma:

\[
\mu = c\,\frac{\alpha_n}{\beta_n}, \qquad v = \frac{\text{Var}}{\mu^2} = \frac{1}{\alpha_n} + \frac{1}{k} + \frac{1}{\alpha_n k}, \qquad a^* = \frac{1}{v}, \qquad b^* = \frac{a^*}{\mu}
\]

The predictive count is then

\[
X \sim \text{NB}\!\left(a^*,\ \frac{b^*}{b^* + t}\right).
\]

With no game-state term (\(k = \infty\)), \(a^* = \alpha_n\) and \(b^* = \beta_n / c\) exactly, which is the textbook Gamma–Poisson posterior predictive.

**Requirements.**

- **R-MOD-1** — Variance MUST be computed in the relative form \(v\) above, which is a sum of non-negative terms. The subtraction form \(E[\Lambda^2] - \mu^2\) MUST NOT be used: it cancels catastrophically and returns \(-1.42 \times 10^{-14}\) at \(\alpha_n = 10^{17}\) (`test_predictive_gamma_stable_where_subtraction_form_cancels`). [T]
- **R-MOD-2** — \(v\) MUST be clamped to \([\texttt{MIN\_RELATIVE\_VARIANCE}, \texttt{MAX\_RELATIVE\_VARIANCE}] = [10^{-6}, 10^{3}]\), logging `[MATH_CLAMP]` (Section 3).
  - **Lower bound:** binds only when \(\alpha_n > 10^6\) with no game-state term, which is the numerical Poisson limit.
  - **Upper bound:** binds only for \(k\) or \(\alpha_n\) below about \(10^{-3}\).
  - **Neither bound binds for any sane input.** In particular, a count-scale floor such as \(\max(\text{Var}, \mu + 10^{-5})\) is prohibited. It applies a count variance to a rate variance, and on a representative yellow-card projection it moves \(P(\text{carded})\) from 0.1675 to 0.1199 (`test_card_probability_not_distorted_by_variance_floor`). [T]
- **R-MOD-3 (fail-closed)** — `QuantEngine.predictive_gamma` MUST raise `ProjectionError` when any of the following is non-finite or non-positive:
  - the multiplier;
  - either posterior parameter;
  - \(\mu\);
  - the derived rate.

  `PropProjection.__post_init__` MUST raise `ProjectionError` for non-finite or non-positive `gamma_shape` or `gamma_rate`, and `count_distribution` MUST raise it for invalid minutes. An invalid projection therefore cannot exist, and `count_distribution` cannot fail downstream. [T]
- **R-MOD-4 (fallback prohibited)** — A zero-rate or other "safe" fallback distribution MUST NOT be substituted for a failed projection.
  - Under \(\text{Pois}(0)\), \(P(\text{Under}) = 1\) for every line \(L > 0\), so every Under price \(O\) evaluates to \(\text{EV} = O - 1 > 0\). A 1.25 Under reads as +25% per unit.
  - The allocator would then direct maximum permitted stakes to exactly the markets whose data is corrupt.
  - On `ProjectionError`, `InstitutionalAssetManager.allocate` MUST skip the market and append `(key, "projection unusable: …")` to `report.skipped` (`test_corrupted_projection_is_skipped_never_traded`). [T]
- **R-MOD-5** — The bivariate Poisson (`bivariate_poisson_pmf`, `joint_over_probability`) models only positive dependence between two props in the same match. Its marginals are exactly Poisson (`test_bivariate_marginal_is_equidispersed_poisson`), so it MUST NOT be presented as a source of overdispersion. [T]

### 2.4 Portfolio Asset Allocation Module

**Component:** `prop_alpha.scanner.market_scanner.InstitutionalAssetManager(bankroll, risk_aversion=2.0, alpha_threshold=0.02, max_position_fraction=0.02, max_fixture_fraction=0.05, max_total_fraction=0.20, max_quote_age=timedelta(minutes=5), open_exposure=0.0, min_stake=1.0)`. [T]

**Objective.** For player-market key \(\kappa = (\text{fixture\_id}, \text{player\_id}, \text{market\_type})\) with \(J\) candidates:

\[
\max_{f \in \mathbb{R}^J} \; U(f) = f^{\top}\mu - \lambda_{\text{risk}}\, f^{\top}\Sigma f
\quad \text{s.t.} \quad 0 \le f_j \le \min\!\left(f^{*}_j,\ f_{\text{pos}}\right), \quad \textstyle\sum_j f_j \le f_{\text{pos}}
\]

Here \(f^{*}_j\) is the push-aware full-Kelly fraction \((p_w b - p_l) / (b(p_w + p_l))\), and \(f_{\text{pos}}\) is `max_position_fraction`.

**Requirements.**

- **R-ALLOC-1 (exact moments).** \(\mu\) and \(\Sigma\) MUST be computed exactly on the joint outcome grid (`outcome_model`, `return_moments`).
  - **Scenarios:** start, substitute and absent, each weighted by `LineupExpectation`.
  - **Counts:** \(x = 0, \dots, \lfloor \max_j L_j \rfloor\), plus one tail bucket \(x \ge \lfloor \max_j L_j \rfloor + 1\). The tail bucket is exact, because every candidate settles identically there.
  - **Payoffs:** per-unit payoffs \(R_{s,j} \in \{b_j, -1, 0\}\) apply the candidate's void rule.
  - **Moments:** \(\mu = R^{\top}\pi\) and \(\Sigma = R^{\top}\text{diag}(\pi) R - \mu\mu^{\top}\), symmetrised, with any negative eigenvalue floored.
  - **Verification:** the diagonal and cross-line entries are checked against closed forms and brute force (`test_return_moments_match_closed_form`, `test_cross_line_covariance_matches_brute_force`).
- **R-ALLOC-2 (solver).** \(\Sigma \succeq 0\), so \(U\) is concave and the solution is global. The solve uses SLSQP with an analytic gradient. The result MUST be at least as good as the best single-candidate closed form \(f_j = \min\!\big(\mu_j / (2\lambda\Sigma_{jj}),\ \text{cap}_j\big)\); otherwise that single-candidate solution is used (`solver = "single"`).
- **R-ALLOC-3 (dominance).** Candidates MUST be de-duplicated to the best price per `(line_value, side, void_rule)` across books before optimisation. Identical payoff vectors at a lower price are strictly dominated.
- **R-ALLOC-4 (group caps).** After per-market optimisation, fractions MUST be scaled proportionally so that each fixture sums to at most `max_fixture_fraction` and the slate to at most `max_total_fraction`. Same-match props are positively correlated (§2.3, R-MOD-5), and independent sizing would overbet the joint position.
- **R-ALLOC-5 (bankroll and liquidity).** Each stake is \(\min(f \cdot (\text{bankroll} - \text{open\_exposure}),\ \texttt{max\_stake})\), rounded to 0.01. If available bankroll is ≤ 0, no directives are issued.
- **R-ALLOC-6.** `alpha_threshold` pre-filters candidates on per-unit EV before optimisation. Stale quotes and sub-minimum stakes follow Section 3.
- **R-ALLOC-7 (degenerate-outcome guard).** Any candidate whose modelled \(p_{\text{lose}} \le \texttt{DEGENERATE\_LOSS\_PROBABILITY} = 10^{-6}\) MUST be excluded from optimisation and recorded in `report.skipped` as `"degenerate outcome: … manual review required"`. A modelled certainty is more often a data or settlement-rule error than an edge (`test_certain_outcome_is_flagged_not_staked`). [T]

**Relationship to fractional Kelly.** For small edges the unconstrained single-line optimum is \(f = \mu / (2\lambda\sigma^2)\), against full Kelly \(f^{*} \approx \mu / \sigma^2\). Hence \(\lambda_{\text{risk}} = 1/(2\psi)\) reproduces \(\psi\)-fractional Kelly, and the default \(\lambda = 2\) corresponds to \(\psi = 1/4\).

**How the variance penalty dampens drawdown.** Approximate log-wealth under a fixed fraction \(f\) as Brownian motion. The drift is \(g = f m - \tfrac12 f^2\sigma^2\) and the volatility \(f\sigma\), where \(m\) is the true per-unit edge. The probability that wealth ever falls to a fraction \(q\) of its starting value is

\[
P(\text{ever reach } q) = q^{\,2g / (f^2\sigma^2)} = q^{\,2m/(f\sigma^2) - 1}.
\]

With \(f = \psi \hat m / \sigma^2\) and a true-to-modelled edge ratio \(\rho = m / \hat m\), the exponent is \(2\rho/\psi - 1 = 4\lambda\rho - 1\). The table gives \(P(\text{ever halve})\):

| \(\lambda_{\text{risk}}\) | \(\psi\) | \(\rho = 1\) (model correct) | \(\rho = 0.5\) (edge overstated 2×) |
|---|---|---|---|
| 1 | 0.500 | \(0.5^{3} = 0.125\) | \(0.5^{1} = 0.500\) |
| 2 | 0.250 | \(0.5^{7} = 0.0078\) | \(0.5^{3} = 0.125\) |
| 4 | 0.125 | \(0.5^{15} \approx 3\times10^{-5}\) | \(0.5^{7} = 0.0078\) |

Two conclusions follow:

1. **The variance penalty is the drawdown control.** Each unit of \(\lambda\) adds 4 to the exponent.
2. **The control degrades with model error.** An edge overstated by 2× turns a 0.8% halving probability into 12.5%. Until Gates 2–3 establish \(\rho\) empirically, \(\lambda\) MUST NOT be set below 2.

This is a continuous-time approximation for a single stationary return stream. It is a planning bound, not a guarantee.

**Correction: mean-variance optimisation is not nominal-EV selection.** Take two candidates on the same player (tackles, \(p_{\text{start}} = 0.92\)):

| Line | Odds | EV/unit | \(\sigma^2\)/unit | \(U^{*}\) at \(\lambda = 2\): \(\mu^2 / (8\sigma^2)\) | Kelly growth \(\approx \mu^2 / (2\sigma^2)\) |
|---|---|---|---|---|---|
| Over 1.5 | 1.797 | 0.060 | 0.757 | \(5.95\times10^{-4}\) | \(2.38\times10^{-3}\) |
| Over 2.5 | 2.834 | 0.062 | 1.827 | \(2.63\times10^{-4}\) | \(1.05\times10^{-3}\) |

- **Nominal-EV selection** (the v2 rule) chooses Over 2.5.
- **Risk-adjusted selection** prefers Over 1.5 by a factor of 2.26, under both the mean-variance utility and the growth-optimal Kelly criterion. The allocator stakes 1.94% on Over 1.5 and 0.04% on Over 2.5 (`test_mean_variance_prefers_low_variance_line_at_similar_ev`).
- **Correcting the prior criticism.** v2 did not concentrate "the entire allocation" on the longer line. Kelly already sizes longer odds smaller: 0.87% at \(\psi = 0.25\). The defect was the *selection* criterion.
- **Scope of the variance penalty.** Variance is a second-moment proxy. It penalises dispersion symmetrically and does not model path-dependent drawdown directly; that link is the approximation above.

---

## SECTION 3: CORE BOUNDARY CONDITIONS & SYSTEM LIMITS

**Table A — mandated boundary conditions.**

| # | Boundary variable | Condition | Mechanism / identifier | Mandated action | Status |
|---|---|---|---|---|---|
| B1 | Database transaction lock | Lock wait exceeds `lock_timeout = 2000` ms → SQLSTATE `55P03` (`psycopg2.errors.LockNotAvailable`). The same path covers `40P01` deadlock, `40001` serialization failure, and dropped connections (`InterfaceError`). | `build_pool(... options="-c lock_timeout=2000")`; `_with_transaction` catches `RETRYABLE_DB_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)` | The transaction rolls back (`with conn:` exit). The connection is returned (`putconn(conn, close=bool(conn.closed))`). The code sleeps \(\Delta_a = \texttt{retry\_base\_delay\_s}\cdot 2^{a-1}\cdot(1+U)\), \(U \sim \mathcal{U}(0,1)\), and retries up to `max_db_attempts = 3`. When attempts are exhausted, the fixture is recorded in `SlateReport.failed` with the exception text. | T (fakes), U (live) |
| B2 | Database query window | Statement exceeds `statement_timeout = 10000` ms → SQLSTATE `57014` (`psycopg2.errors.QueryCanceled`; psycopg2 has no `StatementTimeout` class). 57014 is also raised by an operator `pg_cancel_backend`. | `_with_transaction`: `except psycopg2.errors.QueryCanceled`, placed **before** the retryable clause | Log `[STATEMENT_TIMEOUT]` at ERROR. Terminate **that fixture's** retry loop immediately; the slate continues. Roll back and **do not retry**: a statement that consumed the full window will not finish faster, and retries would triple lock-window occupancy. Fail closed: the fixture is marked failed, nothing is staged, and staging rows already claimed by a merge are restored by its rollback (`test_statement_timeout_fails_closed_without_retry`). | T (fakes), U (live) |
| B3 | Parameter mapping rate | \(v = 1/\alpha_n + 1/k + 1/(\alpha_n k) \notin [10^{-6}, 10^{3}]\) | `QuantEngine.predictive_gamma`; `MIN_RELATIVE_VARIANCE`, `MAX_RELATIVE_VARIANCE` | Clamp to the nearest bound and log `[MATH_CLAMP]` at WARNING on logger `prop_alpha.engine`, with `alpha`, `beta`, `k` and `multiplier`. Pricing proceeds. Any `[MATH_CLAMP]` in production MUST be investigated, because neither bound binds for sane inputs (`test_math_clamp_is_logged`, `test_no_math_clamp_for_ordinary_inputs`). | T |
| B4 | Staking threshold | \(\text{stake} = \min(f\cdot(\text{bankroll}-\text{open\_exposure}),\ \texttt{max\_stake}) < \texttt{min\_stake}\) | `InstitutionalAssetManager.allocate` | Build the `StakingDirective` in full, append it to `report.skipped_directives` (not `report.directives`), and log it at INFO (`test_sub_minimum_stakes_recorded_in_skipped_directives`). | T |
| B5 | Quote staleness | \(\text{now} - \texttt{captured\_at} > \texttt{max\_quote\_age} = 5\ \text{min}\) | `InstitutionalAssetManager.allocate` | Discard the quote before pricing and increment `report.stale_quotes`. A stale quote never reaches `_candidates`. `captured_at` MUST be timezone-aware UTC (`test_best_price_kept_and_stale_quotes_dropped`). | T |

**Table B — further enforced limits (not in the original mandate; listed for completeness).**

| Limit | Default | Enforcement |
|---|---|---|
| Pool size | `maxconn ≥ max_workers + 1` | `ValueError` at construction |
| Slate deadline | `deadline_s` (caller-set) | `SlateReport.timed_out` |
| EV pre-filter | `alpha_threshold = 0.02` | Candidate excluded |
| Per player-market cap | `max_position_fraction = 0.02` | QP bound and budget constraint |
| Per-line Kelly cap | \(f_j \le f^*_j\) | QP bound |
| Per-fixture cap | `max_fixture_fraction = 0.05` | Proportional scaling |
| Slate cap | `max_total_fraction = 0.20` | Proportional scaling |
| Book liquidity | `LiveOddsQuote.max_stake` | Stake clipped |
| Degenerate outcome | \(p_{\text{lose}} \le 10^{-6}\) | `report.skipped`; manual review |
| Corrupted projection | `ProjectionError` | `report.skipped`; market untradeable |
| Line granularity | whole or half lines | `ValueError` on quarter lines; `CHECK` in `odds_lines` |

---

## SECTION 4: OPERATIONAL INGESTION SCENARIOS & RUNTIME JOURNEYS

### Scenario A — Mid-week ingestion under network latency limits

**Context.** Tuesday evening, 12 League One/Two fixtures final at 21:45. The post-match `stats` slate runs at 22:30. Parameters:

- \(W = 8\) workers
- `DATA_VENDOR_RPS = 5`, burst \(B = 10\)
- Vendor p95 latency \(\bar L = 1.8\) s
- `deadline_s = 6`

**Expected wall time (R-ING-5).**

\[
T \gtrsim \max\!\left(\tfrac{12 - 10}{5},\ \lceil 12/8 \rceil \cdot 1.8\right) = \max(0.4,\ 3.6) = 3.6\ \text{s}
\]

The sequential v2 loop would take \(12 \times 1.8 = 21.6\) s.

**Runtime journey.**

1. **Invocation.** The scheduler runs `python -m prop_alpha.ingestion.pipeline stats --workers 8 --deadline 6` with `TARGET_FIXTURE_UIDS` holding the 12 UIDs. `build_pool` opens a pool with `maxconn = 9`, and `ConcurrentIngestionEngine` starts the executor.
2. **Dispatch.** `run_slate` submits 12 `_run_isolated(_stats_task, uid)` futures.
   - Ten requests pass the token bucket immediately; two wait 0.2 s and 0.4 s.
   - Each worker lazily creates its thread-local `requests.Session`.
3. **Normal fixtures (9 of 12).** For each:
   - `get_fixture_feed`, `parse_fixture_payload` and `build_staging_rows` run with no connection held.
   - `_with_transaction` then checks out a connection, inserts into `raw_payloads` (`ON CONFLICT (payload_sha256) DO NOTHING`) and stages 1 + \(n_{\text{players}}\) rows, all in one transaction of a few milliseconds.
   - Each returns `FixtureResult(ok=True, db_attempts=1)`.
4. **Partial data drop — vendor 503 (fixture F7).**
   - urllib3 `Retry(total=2, backoff_factor=0.25, status_forcelist=(429, 500, 502, 503, 504), respect_retry_after_header=True)` retries twice inside the worker.
   - On exhaustion, urllib3 raises `MaxRetryError`, which `requests` surfaces as `requests.exceptions.RetryError`, a `RequestException`.
   - `_run_isolated` records `FixtureResult(ok=False, error="RetryError: … too many 503 error responses")`.
   - No database work occurred for F7.
5. **Partial data drop — truncated payload (fixture F9).** The payload omits `players[3].stats.tackles_attempted` and reports `tackles_won = 4`.
   - `PlayerMatchRecord.__post_init__` raises `ValueError("… tackles_won exceeds tackles_attempted")` during parsing, before any connection is taken.
   - F9 fails. Nothing for F9 is staged; partial fixtures are never written.
6. **Worker timeout isolation (fixture F12).**
   - The vendor stalls for 9 s on F12. At \(t = 6\) s, `wait(futures, timeout=6)` returns with F12 in `not_done`, and `report.timed_out = ["F12"]`.
   - The F12 thread continues and commits its staging rows at \(t \approx 9\) s.
7. **Merge.** Because `report.succeeded` is non-empty, `merge_staging()` runs on the spare connection.
   - It drains the 9 committed fixtures.
   - F12's rows, committed after the `DELETE` snapshot, remain in staging.
   - It returns `MergeResult(fixtures_merged=9, player_rows_merged=…, rows_rejected=0)`.
8. **Exit and remediation.** `fully_successful` is `False` (F7 and F9 failed, F12 timed out), so the process exits 1 and the scheduler alerts.
   - **F7 and F12:** the remediation run MUST be `run_slate(list(report.failed) + report.timed_out, job="stats")`. Re-ingesting F12 is idempotent: `raw_payloads` de-duplicates identical payloads by hash, and the merge's `DISTINCT ON … staged_at DESC` keeps the latest staged row per key.
   - **F9:** F9 fails again until the vendor corrects the record. It MUST be escalated to the vendor, not retried in a loop.

### Scenario B — Lineup drop at T-60 minutes

**Context.** Saturday, 15:00 kickoffs. Team sheets publish from 14:00. The `lineups` job runs at 14:00:30 with `deadline_s = 45`, and repeats every 60 s until every fixture on the slate succeeds.

**Runtime journey.**

1. **Invocation.** `run_slate(uids, job="lineups", deadline_s=45)`. There is no merge, because lineup rows are disjoint per fixture and are written directly.
2. **Validation.** For each fixture, `get_lineups` then `parse_lineup_payload(payload, bench_sub_probability=0.35)`.
   - A sheet with 10 listed starters, a missing side, or a duplicated player raises `ValueError`. The fixture is reported failed and retried on the next cycle, and no partial sheet is ever written.
3. **Upsert.** `LINEUP_UPSERT_SQL` (with `LINEUP_TEMPLATE` casts) writes starters with \((1, 0)\) and bench players with \((0, 0.35)\).
   - Expected minutes come from each player's history via the `LATERAL` aggregate over `player_match_stats`.
   - `RETURNING player_id` yields the matched set. Listed players unknown to `players` are logged as a warning. If none match, the task raises, because an empty match means the fixture or player seed is missing.
4. **Zeroing.** `LINEUP_EXCLUDE_SQL` sets \(p_{\text{start}} = p_{\text{sub}} = 0\), with `source = 'confirmed_team_sheet'`, for every other `lineup_expectations` row of the fixture, in the same transaction.
5. **Re-projection [N].** A projection-refresh job MUST rebuild `PropProjection`s from the updated `lineup_expectations` and persist them to `model_projections`, before the next `allocate` call reads them. v3.1 has no loader from `lineup_expectations` to `LineupExpectation`. This is the critical missing link in the T-60 path, and it MUST be built before Gate 3.
6. **Effect on a confirmed starter** (tackles, 1.5 line, same posterior as §2.4):

| State | \(p_{\text{win}}\) (Over) | \(p_{\text{lose}}\) | \(p_{\text{void}}\) | \(P(\text{Over} \mid \text{plays})\) |
|---|---|---|---|---|
| Pre-sheet: \(p_{\text{start}} = 0.75\), \(p_{\text{sub}} = 0.15\) | 0.4794 | 0.4206 | 0.1000 | 0.5327 |
| Post-sheet: confirmed starter | 0.6171 | 0.3829 | 0.0000 | 0.6171 |

   The conditional probability rises by 8.4 percentage points, because the substitute-minutes scenario (\(t = 20/90\)) is eliminated. Book quotes compiled before the sheet do not reflect this. The quote-age rule (B5) measures when *we* captured the quote, not when the book last updated its price, so a pre-sheet price re-captured after 14:00 is a valid candidate.
7. **Effect on absent players.** The effect depends on the book's void rule:
   - **`VOID_IF_NO_APPEARANCE` or `VOID_IF_NOT_STARTING`:** \(p_{\text{void}} = 1\), \(\text{EV} = 0\), no candidate.
   - **`NO_VOID`:** the Under shows \(p_{\text{lose}} = 0\). R-ALLOC-7 routes it to `report.skipped` for manual review (`test_certain_outcome_is_flagged_not_staked`).
8. **Failure branches.**
   - **Unknown fixture.** The vendor publishes a sheet for a fixture not yet in `fixtures`, because the scheduled-fixture `stats` ingestion never ran. The upsert then matches zero rows and the task raises. Remediation: run `stats` for the scheduled fixture (which creates it through the header row and merge), then re-run `lineups`.
   - **Transient database errors** follow B1. A `57014` follows B2.

### Scenario C — Database lock contention during the merge

**Context.** The Saturday 17:05 `stats` slate merges while the following run concurrently:

- (i) an analyst's long read-only query on `player_match_stats`;
- (ii) an analyst session that ran `SELECT … FROM prop_alpha.referees … FOR UPDATE` and left its transaction open;
- (iii) the `referee_job` for league 2, started manually;
- (iv) a developer running a non-concurrent `CREATE INDEX` on `player_match_stats`.

**Lock analysis and runtime journey.**

1. **Analyst read-only query (i).** A plain `SELECT` takes `ACCESS SHARE`, which conflicts only with `ACCESS EXCLUSIVE`. Under MVCC, readers and writers do not block each other, so the merge proceeds unaffected.
   - **Secondary cost.** The query's snapshot holds back the xmin horizon, so autovacuum cannot reclaim the dead tuples the merge's `DELETE` leaves in `staging_player_feed`. That table churns its entire contents every merge, so a long snapshot causes staging bloat.
   - **Requirement.** Analyst roles MUST set `idle_in_transaction_session_timeout` and SHOULD query a physical replica. Core tables replicate; UNLOGGED staging does not, which is acceptable for analytics.
2. **Merge acquires the advisory lock.** `pg_advisory_xact_lock(hashtext('prop_alpha.merge_staging_player_feed'))` is uncontended, then the drain begins.
3. **Row lock held by the analyst (ii).**
   - The merge's `INSERT INTO prop_alpha.referees … ON CONFLICT … DO UPDATE` must lock a referee row that the analyst session holds `FOR UPDATE`.
   - After 2 s, `lock_timeout` raises `55P03`. The merge transaction rolls back, and the rollback restores the drained staging rows.
   - B1 then retries with jittered backoff. If the analyst transaction stays open through three attempts, `merge_staging` raises and `SlateReport.merge_error` is set. The data remains in staging and is not lost.
   - **Operations.** Terminate the idle transaction (`pg_terminate_backend`) and invoke `engine.merge_staging()` or the next slate. `FOR UPDATE` and any other explicit row locks on core tables MUST be prohibited for analyst roles via role-level grants.
4. **`referee_job` (iii).** `run_referee_job` executes `SELECT pg_advisory_xact_lock(hashtext(%s))` with `MERGE_LOCK_KEY` before reading discipline data. Whichever transaction arrives second waits on the advisory lock.
   - **Merge arrives second.** Its wait is subject to `lock_timeout`, which covers advisory locks as well as table and row locks. After 2 s it raises `55P03` and retries under B1. `referee_job` completes in seconds, so a subsequent attempt succeeds.
   - **`referee_job` arrives second.** Its connection is opened with `psycopg2.connect(dsn)` and has no `lock_timeout`, so it waits for the merge, which is short, then proceeds.
   - **Deadlock.** Without the shared lock, the two would take `referees` row locks in different orders (merge by `vendor_referee_uid`, job by `referee_id`), and PostgreSQL would abort one with `40P01`. The shared advisory lock makes that interleaving impossible.
   - **Scheduling.** `referee_job` MUST be scheduled outside match-day windows. Because the job connection has no `lock_timeout`, a manual run during a slate is safe, but it delays merges by up to the job's duration.
5. **Non-concurrent index build (iv).**
   - `CREATE INDEX` without `CONCURRENTLY` takes a `SHARE` lock on `player_match_stats`, which conflicts with the merge's `ROW EXCLUSIVE`. The merge's upsert into that table waits and hits `lock_timeout`, which triggers B1 retries. If the build outlasts them, `merge_error` is set.
   - **Requirement.** All index operations on core tables MUST use `CREATE INDEX CONCURRENTLY` or `DROP INDEX CONCURRENTLY`, as `migrations/002_staging_and_indexes.sql` does.
   - **Match-day windows.** `ALTER TABLE`, `VACUUM FULL`, `CLUSTER` and `TRUNCATE`, all of which take `ACCESS EXCLUSIVE`, are prohibited on core tables during match-day windows.
6. **Statement timeout during the merge.** If the merge itself exceeds 10 s (e.g. a very large backfill batch), `57014` is raised. B2 applies: no retry, `merge_error` is set, and the staged rows are restored by rollback. Remediation: merge in smaller batches by running `stats` slates with fewer fixtures. The merge drains all visible rows, so batch size is controlled at ingestion.
7. **Post-condition.** In every branch the invariants hold:
   - no staged row is lost;
   - no row is merged twice;
   - no core row is left partially updated, because each merge is one transaction;
   - the advisory lock is released at commit or rollback, with no manual release path.

---

## SECTION 5: GO-LIVE VALIDATION GATES & PROXIMATE CAPITAL LIMITS

The gates are sequential. A gate is passed only when every criterion is met and the evidence is archived: queries, notebooks and bootstrap seeds. Failing any gate after go-live (monitored monthly) MUST suspend automated staking for the affected market.

### Gate 1 — Relational Integration Pass

**Environment.** A live PostgreSQL 15+ instance, fresh-installed from `schema.sql`. Separately, a v2 instance MUST be migrated with `migrations/002_staging_and_indexes.sql` to prove the upgrade path.

**Criteria.**

1. **Clean install.** Both `schema.sql` and the migration apply without error. `\df prop_alpha.merge_staging_player_feed` resolves, and `idx_odds_fixture_captured` exists with no `idx_odds_captured_brin`.
2. **Backfill.** At least 1,000 `final` fixtures are ingested through `ConcurrentIngestionEngine` (`--workers 8`) and merged.
3. **Zero rejects.** `SELECT count(*) FROM prop_alpha.staging_rejects` returns 0 after the backfill. Any reject is a blocking defect: root-cause it (seed data, vendor mapping, or transform validation), fix it, and re-run the full backfill. Rejects MUST NOT be deleted to pass the gate.
4. **Reconciliation.**
   - \(\lvert\{\text{distinct } \texttt{vendor\_fixture\_uid} \in \texttt{raw\_payloads}\}\rvert = \lvert \texttt{fixtures} \rvert\) for the backfilled set.
   - \(\sum\) players across the latest payload per fixture \(= \lvert \texttt{player\_match\_stats} \rvert\).
   - `staging_player_feed` is empty after the final merge.
5. **Idempotency.** Re-ingesting and re-merging 100 randomly chosen fixtures changes no row in `player_match_stats` except `source_updated_at`.
6. **Concurrency soak.** Run 8 workers while `referee_job`, a synthetic long-running analyst `SELECT`, and a `CREATE INDEX CONCURRENTLY` execute concurrently. Required outcomes:
   - zero unrecovered failures;
   - zero `40P01` involving `referees`;
   - every B1 retry ends in success or a recorded failure.

   Record the p50 and p99 of merge duration and slate duration. Proposed thresholds, to be confirmed by Engineering: merge p99 < 2 s for 12-fixture slates; lineup slate p99 < 10 s.
7. **Lineups path.** For 50 historical fixtures with archived team sheets:
   - `lineup_expectations` holds exactly 11 starters per side;
   - every other row for those fixtures has \(p_{\text{start}} = p_{\text{sub}} = 0\).

### Gate 2 — Predictive Accuracy Pass

**Protocol.** Walk-forward (`walk_forward_over_probability`):

- Each prediction uses only data strictly before kickoff.
- The league prior rate is itself trailing.
- Lineups are fixed at team-sheet time.
- \(\kappa\), \(\beta_1\), \(\beta_2\) and \(k\) are fitted on a period disjoint from evaluation.

**Criteria.**

1. **Sample size.** ≥ 500 out-of-sample predictions per target market (`tackles`, `fouls`, `yellow_card`).
2. **Skill.**
\[
\text{BSS} = 1 - \frac{\text{BS}_{\text{model}}}{\text{BS}_{\text{clim}}}
\]
The fixture-clustered bootstrap 90% interval of BSS MUST have a **lower bound > 0**; a positive point estimate is not sufficient.
3. **Calibration.** Fit a logistic calibration regression \(\text{logit}\,P(y = 1) = a + b\,\text{logit}\,\hat p\). The slope \(b\) must lie in [0.9, 1.1]. Every calibration bin with ≥ 50 predictions must satisfy \(\lvert\bar{\hat p} - \bar y\rvert \le 0.03\).
4. **Market benchmark.** This criterion is additional to the original mandate. It is required because beating climatology is necessary but far from sufficient: a model can beat the base rate while knowing less than the market. On the subset with a devigged closing price \(p^{\text{mkt}}\), fit
\[
\text{logit}\,P(y = 1) = a + b_1\,\text{logit}\,p^{\text{mkt}} + b_2\,\text{logit}\,\hat p .
\]
The model carries information beyond the market only if \(b_2 > 0\) with Wald \(p < 0.05\).
5. **`red_card` exclusion.** `red_card` markets are excluded from automated staking until ≥ 50 positive outcomes are scored. At base rates below 1%, 500 predictions contain fewer than 5 events, and BSS is statistically meaningless.

### Gate 3 — Paper Betting & Closing Line Value

**Prerequisite.** The projection-refresh job (Scenario B, step 5) and a bet-ledger writer are built [N]. The ledger writer MUST:

- persist every `StakingDirective` to `bets` with `is_paper = TRUE`, linked to `model_projections.projection_id` and `odds_lines.line_id`;
- at kickoff, set `closing_line_id` to the latest `odds_lines` row for the same `(fixture_id, sportsbook, market_type, player_id, line_value)` with `is_closing = TRUE`;
- compute `clv` with `closing_line_value`.

**Criteria.**

1. **Sample size.** ≥ 300 settled paper bets across ≥ 60 distinct fixtures.
2. **CLV.** For each bet,
\[
\text{CLV} = O_{\text{taken}} \cdot p^{\text{fair}}_{\text{close}} - 1,
\]
where \(p^{\text{fair}}_{\text{close}}\) is the devigged closing probability of the side taken. Using a fixture-clustered bootstrap (10,000 resamples of fixtures; seed archived), the 90% confidence interval for mean CLV MUST lie **strictly above zero**: lower bound > 0. An interval that excludes zero from below is a failure.
3. **Power check.** With clustered standard deviation \(\sigma_c\) and mean \(\overline{\text{CLV}}\), the required sample is roughly \(n \ge (1.645\,\sigma_c / \overline{\text{CLV}})^2\) effective clusters. If this exceeds the sample collected, extend paper trading. Do not lower the bar.
4. **Settlement integrity.** 100% of paper bets reconcile to the realised `player_match_stats` outcome and the book's void rule, with no manual overrides.

### Gate 4 — Capacity & Capital Controls

**Criteria.**

1. **Liquidity clipping.** Every directive's `stake` is clipped to the quoting book's `max_stake`. This is enforced in `allocate` [T]. Any future execution component [N] MUST re-read the live `max_stake` and price immediately before submission. It MUST re-clip, and it MUST abort if the price has moved against the directive by more than a configured tolerance or if `captured_at` has breached B5.
2. **Deployable capacity.** Observed lower-tier limits are £50–£500 per bet, and winning accounts are restricted. The expected profit per slate is therefore bounded by
\[
E[\Pi_{\text{slate}}] \le \sum_{j \in \text{directives}} \texttt{max\_stake}_j \cdot \text{EV}_j ,
\]
independently of bankroll. Capital beyond \(\sum_j \texttt{max\_stake}_j\) per slate is idle. Strategy sizing MUST be based on this bound, not on bankroll fractions.
3. **Account-restriction telemetry [N].** Per book, record accepted versus requested stake, and the date of every limit reduction. When a book's median accepted stake falls below `min_stake`, remove that book from the quote universe.
4. **`bench_sub_probability` recalibration [N].** The constant 0.35 is uncalibrated and MUST be replaced by a per-player estimate before Gate 3 results are accepted.
   - **Schema.** Add `lineup_expectations.squad_role VARCHAR(8) CHECK (squad_role IN ('starter','bench','absent'))`, written by the lineups job. Inferring bench status from \(p_{\text{sub}} > 0\) is ambiguous when the configured probability is 0.
   - **Outcome.** For each bench designation \(t\) of player \(i\), \(y_{i,t} = \mathbb{1}[\texttt{minutes\_played} > 0]\) from `player_match_stats`.
   - **Estimator.** An exponentially weighted moving average with half-life \(h\) matches, shrunk toward the league bench-appearance rate \(\bar p\) with \(n_0\) pseudo-observations:
\[
\hat p_i = \frac{\sum_t w_t\, y_{i,t} + n_0\,\bar p}{\sum_t w_t + n_0}, \qquad w_t = 2^{-(\text{age}_t / h)}
\]
   - **Tuning.** Tune \(h\) and \(n_0\) by out-of-sample log-loss on bench appearances.
   - **Use.** The lineups job then writes \(p_{\text{sub}} = \hat p_i\) per player instead of the constant.
5. **Risk parameters at launch.**
   - `risk_aversion` \(\ge 2\), i.e. at most quarter-Kelly-equivalent (see the drawdown table in §2.4).
   - `max_position_fraction ≤ 0.02`, `max_fixture_fraction ≤ 0.05`, `max_total_fraction ≤ 0.20`.

   Loosening any of these requires Gate 3 evidence that \(\rho = m / \hat m\), the ratio of realised to modelled edge estimated from CLV, is ≥ 0.75.

### Gate readiness at v3.1

| Gate | Blocking items |
|---|---|
| 1 | PostgreSQL 15+ environment. All SQL is **[U]**: never executed. |
| 2 | Historical data backfill (depends on Gate 1); fitting runs for \(\kappa, \beta_1, \beta_2, k\); market-benchmark dataset. |
| 3 | Projection-refresh job **[N]**; bet-ledger writer and closing-line capture **[N]**. |
| 4 | Execution component **[N]**; restriction telemetry **[N]**; `squad_role` column and bench estimator **[N]**. |
