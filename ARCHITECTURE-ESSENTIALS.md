# ARCHITECTURE-ESSENTIALS.md — prop_alpha v3.2 Code Baselines, Type Contracts, Integrity Constraints, CI

| Field | Value |
|---|---|
| Companion documents | `prop_alpha_PRD_v3.1.md`, `ARCHITECTURE.md` |
| Code baseline | `prop_alpha_v3.2_code.zip`: **153 tests passing** (82 core, 38 integrity, 21 typing contract, 12 concurrency) on CPython 3.14.6 |
| Verification | Every code block in Sections 2–4 is copied verbatim from the tested repository by the build step, not retyped. SQL is **not executed against PostgreSQL**. `ci/bootstrap.sh` is syntax-checked with its preflight mode and gates exercised; stages 2–6 have not run end-to-end (§4.4). |
| Tags | **[T]** implemented and tested · **[U]** implemented, unexecuted · **[N]** not built |

**Corrections to the commissioning brief.** Each item below was measured, not assumed.

| Brief item | Defect | Resolution in this document |
|---|---|---|
| File tree `init.py` | The package marker is `__init__.py`. Markdown rendering strips the underscores. Without `__init__.py`, `prop_alpha.ingestion` is an implicit namespace package, and `tests/test_typing_contract.py` could not enumerate modules reliably. | §1.1 uses `__init__.py`, and adds the repository-level files the brief's tree omits. |
| Python template `validate_exposure_vector` | Executed against polluted inputs, it **accepts** a fractional count (2.7), infinite minutes, boolean counts, a count with zero minutes, and 900 minutes. Its NaN check on counts is unreachable, because `np.asarray([nan], dtype=int)` raises first. It discards the validated counts. It raises on empty history, which is exactly the prior-only case for a debutant or loan arrival. | §2.3 presents the production function now in `engine.py`, which rejects all of these inputs, returns both arrays, and allows empty history only where the caller opts in. |
| SQL constraint script | The third statement fails because `chk_tackles_won_le_attempted` already exists, and without `ON_ERROR_STOP` the rest of the script runs half-applied. Each `ADD CONSTRAINT … CHECK` without `NOT VALID` scans the whole table under `ACCESS EXCLUSIVE`, blocking ingestion and the merge. Three constraints duplicate existing column checks. `chk_minute_logic` has no matching reject predicate in `merge_staging_player_feed()`, so one violating row would abort an entire merge. | §3.3–§3.4: named constraints in `schema.sql`, the idempotent `migrations/003_integrity_constraints.sql` (`NOT VALID`, then `VALIDATE`), and merge-predicate parity, each covered by tests. |
| Bash script | `set -euo pipefail` sits inside the comment on the preceding line, so it **never executes**. `IFS=\$'\n\t'` assigns the literal characters `$'\n\t'`. The script hard-codes a database password and a `sk_prod_…` API key into source control. `numpy==1.26.4` has no wheel for Python ≥ 3.13; confirmed here, `pip` reports "No matching distribution found". `requests==2.31.0` is affected by CVE-2024-35195. The pins differ from every version the suite has run against. A missing `psycopg2` makes the concurrency tests skip while the script still reports success. | §4.3: `ci/bootstrap.sh` pins the versions actually tested, uses binary wheels only, needs no secrets, and runs a JUnit gate that fails on any skipped test (verified in §4.4). |

---

## SECTION 1: CORE REPOSITORY TOPOLOGY & DESCRIPTIVE MODULE MAP

### 1.1 File Tree Structure

The package tree requested by the brief, with correct package markers:

```
prop_alpha/
├── __init__.py
├── ingestion/
│   ├── __init__.py
│   ├── transform.py
│   └── pipeline.py
├── modeling/
│   ├── __init__.py
│   └── engine.py
├── scanner/
│   ├── __init__.py
│   └── market_scanner.py
├── calibration/
│   ├── __init__.py
│   ├── fitting.py
│   └── referee_job.py
└── evaluation/
    ├── __init__.py
    └── backtest.py
```

The complete repository, as shipped in `prop_alpha_v3.2_code.zip`:

```
prop_alpha_v3.2/
├── requirements.txt                         exact pins (==) of every version the suite ran against
├── schema.sql                               fresh-install DDL, PostgreSQL 15+, named integrity constraints
├── ci/
│   └── bootstrap.sh                         CI bootstrap: venv, pinned wheels, test matrix, schema smoke test
├── migrations/
│   ├── 002_staging_and_indexes.sql          v2 → v3: UNLOGGED staging, merge function, BRIN → B-tree
│   └── 003_integrity_constraints.sql        v3.1 → v3.2: named CHECKs (NOT VALID → VALIDATE), merge parity
├── prop_alpha/
│   ├── __init__.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── transform.py
│   │   └── pipeline.py
│   ├── modeling/
│   │   ├── __init__.py
│   │   └── engine.py
│   ├── scanner/
│   │   ├── __init__.py
│   │   └── market_scanner.py
│   ├── calibration/
│   │   ├── __init__.py
│   │   ├── fitting.py
│   │   └── referee_job.py
│   └── evaluation/
│       ├── __init__.py
│       └── backtest.py
└── tests/
    ├── test_core.py                         82: maths, engine hardening, allocator, estimators, transforms
    ├── test_integrity.py                    38: exposure validation, transform/SQL parity, migration structure
    ├── test_typing_contract.py              21: AST-enforced typing rules (Section 2), 3 per module
    └── test_pipeline_concurrency.py         12: concurrency, retries, fail-closed timeouts (requires psycopg2)
```

**Migration application rule.**

- **Fresh install:** apply `schema.sql` only. It already embodies every migration.
- **Upgrade:** apply the migrations above the database's current version, in numeric order.
- **Never** apply `002` to a database that has received `003`. `002` would reinstall the v3.0 body of `merge_staging_player_feed()` and drop the minute-logic reject predicates.
- A migration ledger table that records applied versions is **[N]**. Until it exists, the applied version MUST be recorded in the deployment log.

### 1.2 Structural Module Ownership

**Layering rules.** Stateless numeric modules never perform I/O. The only module that owns network and connection-pool I/O is `ingestion/pipeline.py`. The only other module that touches the database is `calibration/referee_job.py`, whose driver import is deferred into `run_referee_job`. The internal import graph is acyclic:

\[
\texttt{transform} \rightarrow \texttt{pipeline}, \qquad
\texttt{engine} \rightarrow \texttt{market\_scanner} \rightarrow \texttt{backtest}, \qquad
\texttt{engine} \rightarrow \texttt{backtest}
\]

**`prop_alpha/__init__.py` and every `*/__init__.py`.** Empty package markers. They make each directory a regular package, so imports resolve deterministically, and the typing-contract test enumerates exactly the modules shown. They MUST NOT contain side effects, re-exports or configuration.

**`ingestion/transform.py`: stateless parsing and feature transforms.** Every function is a pure function of its arguments: no network, no database, no clock, no logging.

- **`parse_fixture_payload(payload, match_length=90) -> ParsedFixture`** converts one vendor JSON object into frozen dataclasses: `FixtureRecord`, `RefereeRecord` and a tuple of `PlayerMatchRecord`. It rejects:
  - unknown statuses (`VALID_STATUSES`);
  - identical home and away UIDs;
  - duplicate player UIDs;
  - players belonging to neither side.
- **`PlayerMatchRecord.__post_init__`** is the first data-pollution barrier. It rejects:
  - negative counts;
  - `minute_off < minute_on`;
  - `minutes_played > MAX_MATCH_MINUTES` (130);
  - `tackles_won > tackles_attempted`;
  - `duels_won > duels_total`;
  - card counts outside \([0,2]\) and \([0,1]\);
  - possession outside \([0,100]\).
- **`match_state_minutes`** derives leading, level and trailing minutes from the goal timeline.
- **`padj_scale`** applies \(\text{raw}\cdot(50/\text{OppPoss})^{\kappa}\), and **`dribbled_past_rate`** returns `None` when the rate is undefined.
- **`build_staging_rows`** emits one fixture header row plus one row per player, ordered exactly as `STAGING_COLUMNS` (41 columns).
- **`parse_lineup_payload`** validates a team sheet: both sides present, exactly 11 starters each, no duplicate players.
- **`DEFAULT_KAPPAS`** is a read-only `MappingProxyType`.

**`ingestion/pipeline.py`: concurrent network I/O and staging.**

- **`ConcurrentIngestionEngine`** owns a `ThreadPoolExecutor`. It fans a slate out to `_run_isolated` tasks, each of which catches every exception into a `FixtureResult`.
- **`VendorClient`** gives each thread its own `requests.Session` (`threading.local`), shares one `TokenBucketRateLimiter`, and applies the urllib3 `Retry` policy.
- **`_with_transaction`** is the sole call site of `pool.getconn()`. Each worker completes its fetch and parse before taking a connection, and holds it only for one short transaction. That transaction appends to `staging_player_feed` (stats job) or upserts `lineup_expectations` (lineups job).
- **Error routing:**
  - transient database errors (`55P03`, `40P01`, `40001`, connection loss) are retried with jittered exponential backoff;
  - `57014` (`QueryCanceled`) fails closed with `[STATEMENT_TIMEOUT]`.
- **`merge_staging`** invokes `prop_alpha.merge_staging_player_feed()` once per slate.
- **`build_pool`** sizes `ThreadedConnectionPool` at `max_workers + 1`, with `statement_timeout=10000` and `lock_timeout=2000`.

**`modeling/engine.py`: posterior updating and relative-variance moment matching.**

- **`validate_exposure_vector`** (§2.3) guards every Gamma–Poisson update.
- **`QuantEngine.gamma_poisson_update`** computes
\[
\alpha_n = \alpha_0 + \sum_j x_j, \qquad \beta_n = \beta_0 + \tfrac{1}{90}\sum_j m_j .
\]
- **`predictive_gamma`** computes
\[
v = \frac{1}{\alpha_n} + \frac{1}{k} + \frac{1}{\alpha_n k},
\]
  clamps it to \([10^{-6}, 10^{3}]\) (logging `[MATH_CLAMP]`), and returns \((a^*, b^*) = (1/v,\ 1/(v\mu))\).
- **`PropProjection`** refuses non-finite or non-positive parameters. It prices any whole or half line under the three `VoidRule`s and the start/substitute/absent mixture in `LineupExpectation`.
- **`ProjectionError`** is the fail-closed signal. There is no fallback distribution.

**`scanner/market_scanner.py`: mean-variance utility maximisation.**

- **`InstitutionalAssetManager.allocate`** processes each player-market key in turn:
  1. drops stale quotes (`report.stale_quotes`);
  2. builds candidates: EV above `alpha_threshold`, best price per `(line, side, void_rule)`, degenerate outcomes excluded;
  3. computes exact moments \(\mu = R^\top\pi\) and \(\Sigma = R^\top\operatorname{diag}(\pi)R - \mu\mu^\top\) on the joint outcome grid (`outcome_model`, `return_moments`);
  4. solves
\[
\max_f\ f^\top\mu - \lambda_{\text{risk}} f^\top\Sigma f
\]
  subject to Kelly, position and budget bounds (`solve_mean_variance`, SLSQP with a single-candidate fallback);
  5. applies fixture and slate caps, clips each stake to `max_stake`, and routes sub-`min_stake` directives to `report.skipped_directives`.
- It performs no I/O and places no bets.

**`calibration/fitting.py`: offline estimators.**

- `fit_poisson_glm` (Newton–Raphson with step halving and standard errors);
- `fit_possession_kappa` (\(\kappa\));
- `fit_referee_betas` (\(\beta_1, \beta_2\));
- `fit_game_state_dispersion` (NB2 method-of-moments \(k\), `None` when there is no overdispersion);
- `fit_state_duel_density` (NNLS).

These functions return values only; persisting parameters is the caller's responsibility.

**`calibration/referee_job.py`: referee coefficients.**

- `compute_referee_coefficients` (pure) produces shrunk \(\widehat{\text{SC}}, \widehat{\text{YCP}}, \widehat{\text{RCP}}, \widehat{\text{HBR}}\).
- `run_referee_job` reads `DISCIPLINE_SQL` and writes `UPDATE_SQL` under `pg_advisory_xact_lock(hashtext(MERGE_LOCK_KEY))`, which serialises it with the merge and removes the lock-order deadlock on `referees`.

**`evaluation/backtest.py`: out-of-sample evaluation.**

- Scoring functions: `brier_score`, `log_loss`, `calibration_table`, `closing_line_value`.
- `walk_forward_over_probability` uses only observations strictly before each kickoff.
- `summarize` reports Brier skill against climatology.

---

## SECTION 2: HARDENED TYPE ASSERTIONS & CODE DEFENSE PATTERNS

### 2.1 Type Parity Rules

**Language level.** Source uses Python 3.10 syntax: PEP 604 unions `X | None`, and builtin generics such as `tuple[float, ...]` and `dict[str, float]`. The **runtime floor is 3.12**, because the pinned `numpy==2.5.1` and `scipy==1.18.0` declare `Requires-Python >= 3.12`. The only interpreter the suite has executed on is CPython 3.14.6.

| Rule | Requirement | Enforcement |
|---|---|---|
| R-TYPE-1 | Every module's first statement after its docstring is `from __future__ import annotations`. | `test_module_uses_postponed_annotations` [T] |
| R-TYPE-2 | Every function and method, including nested closures, annotates every parameter except `self`/`cls`, and its return type. `__init__` returns `None`. | `test_every_function_is_fully_annotated` [T] |
| R-TYPE-3 | Every module-level UPPER_CASE binding is annotated `Final[...]`, except `TypeVar` declarations. | `test_module_constants_are_final` [T] |
| R-TYPE-4 | Read-only collection parameters are typed with abstract protocols: `Sequence[...]` (e.g. `counts: Sequence[int]`), `Mapping[...]` (e.g. `projections: Mapping[ProjectionKey, PropProjection]`, `kappas: Mapping[str, float]`), `Iterable[...]` where only one pass is made. Return types are concrete (`list`, `tuple`, `np.ndarray`, dataclasses). | Code review; mypy [N] |
| R-TYPE-5 | Constants are immutable in value as well as in binding: `frozenset` (`VALID_STATUSES`, `REFEREE_SENSITIVE_MARKETS`), `tuple` (`STATS_COLUMNS`, `STAGING_COLUMNS`), `MappingProxyType` (`DEFAULT_KAPPAS`). `Final` alone prevents rebinding, not mutation. | `test_default_kappas_are_immutable` [T] |
| R-TYPE-6 | Array-accepting estimators take `numpy.typing.ArrayLike` and convert once with `np.asarray(..., dtype=float)`. | R-TYPE-2 test [T] |
| R-TYPE-7 | Third-party objects without public types are described structurally: `engine.DiscreteDistribution(Protocol)` declares `pmf`, `cdf`, `sf`, `mean`, `var` for frozen `scipy.stats` distributions. Private SciPy classes are never imported. | Code review |
| R-TYPE-8 | `typing.Any` is permitted only at I/O boundaries: vendor JSON payloads (`Mapping[str, Any]`), the psycopg2 cursor passed to `_with_transaction` closures, and the connection pool object. | Code review |
| R-TYPE-9 | Value objects are `@dataclass(frozen=True)` and validate in `__post_init__`: `RefereeProfile`, `GammaPosterior`, `LineupExpectation`, `PropProjection`, `PlayerMatchRecord`. An invalid instance cannot exist. | Tests in `test_core.py` and `test_integrity.py` [T] |
| R-TYPE-10 | Static type checking with mypy or pyright in CI. | **[N]**: no type checker has been run against this code. It MUST be added to `requirements.txt` and `ci/bootstrap.sh`, and must pass, before this rule is marked [T]. The AST tests enforce the *presence* of annotations, not their *correctness*. |

### 2.2 Code Defense Patterns

| Pattern | Where | Contract |
|---|---|---|
| Validate at the boundary, once | `parse_fixture_payload`, `PlayerMatchRecord.__post_init__`, `parse_lineup_payload`, `validate_exposure_vector` | Downstream code may assume validated invariants. Violations raise `ValueError` with the offending identifier. |
| Fail closed, never fall back | `ProjectionError` from `predictive_gamma`, `PropProjection`, `count_distribution` | The market is skipped and recorded in `report.skipped`. Substituting a "safe" distribution is prohibited: \(\text{Pois}(0)\) turns every Under into a false +\((O-1)\) edge. |
| Numerically stable forms | `predictive_gamma` | The variance is computed as the sum of non-negative terms \(1/\alpha + 1/k + 1/(\alpha k)\), never as \(E[X^2]-\mu^2\), which returns \(-1.42\times10^{-14}\) at \(\alpha = 10^{17}\). |
| Bounded clamps are loud | `predictive_gamma` | A clamp applies the bound and logs `[MATH_CLAMP]`. It never occurs silently. |
| Certainty is suspicious | `_candidates` | \(p_{\text{lose}} \le 10^{-6}\) is routed to manual review, never auto-staked. |
| Per-unit isolation | `_run_isolated` (per fixture), `allocate` (per market key) | One failure never propagates to a sibling unit. |
| Single resource acquisition site | `_with_transaction` | Connections are released in `finally`, and broken connections are discarded (`close=bool(conn.closed)`). |

### 2.3 Production Code Hardening Template

The brief's template lives in production as `validate_exposure_vector` in `prop_alpha/modeling/engine.py`, and `QuantEngine.gamma_poisson_update` calls it on every posterior update.

**Constants live in exactly one place.** The template redeclares engine constants; duplicating them invites silent drift. The brief's four constants map to their single sources of truth as follows:

| Brief constant | Single source of truth |
|---|---|
| `MIN_RELATIVE_VARIANCE`, `MAX_RELATIVE_VARIANCE` | `prop_alpha/modeling/engine.py` (below) |
| `MIN_ALPHA_THRESHOLD = 0.02` | `InstitutionalAssetManager(alpha_threshold=0.02)`: a per-instance risk parameter, deliberately not a module constant |
| `MAX_POSSESSION_BOUND = 100.0` | `chk_possession_bounds` (§3.3) and `PlayerMatchRecord.__post_init__`. Possession is not an input to the exposure vector. |

The function additionally enforces the bound \(0 \le m_j \le 130\), which mirrors `chk_minute_logic` and `LineupExpectation`.

**Behaviour on the polluted inputs that the brief's template accepted** (`test_polluted_vectors_are_rejected`, 11 parametrised cases) [T]:

| Input | Brief template | Production function |
|---|---|---|
| `counts=[2.7]` | accepted; count truncated to 2 and discarded | `ValueError: counts must be non-negative integers` |
| `counts=[nan]` | numpy `ValueError: cannot convert float NaN to integer`; the template's own check is unreachable | `ValueError: NaN or infinite value in exposure vectors` |
| `minutes=[inf]` | accepted | `ValueError: NaN or infinite value in exposure vectors` |
| `counts=[True, False]` | accepted as `[1, 0]` | `ValueError: exposure vectors must be numeric; …` |
| `minutes=[0], counts=[3]` | accepted | `ValueError: a positive count cannot be recorded with zero minutes played` |
| `minutes=[900]` | accepted | `ValueError: minutes must lie in [0, 130.0]` |
| `minutes=[], counts=[]` | rejected, which breaks prior-only projection for players with no history | rejected by default; `allow_empty=True` (used by `gamma_poisson_update`) returns empty arrays and the posterior equals the prior |

`prop_alpha/modeling/engine.py`, from the top of the module through `validate_exposure_vector` (verbatim):

```python
# file: prop_alpha/modeling/engine.py
"""Predictive engine: Gamma-Poisson posteriors with minutes exposure, referee adjustment,
game-state overdispersion, lineup/void handling, and bivariate Poisson dependence."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol, Sequence

import numpy as np
from numpy.typing import ArrayLike
from scipy.stats import nbinom, poisson

logger = logging.getLogger("prop_alpha.engine")

REFEREE_SENSITIVE_MARKETS: Final[frozenset[str]] = frozenset({"fouls", "fouls_drawn"})

# Relative-variance bounds on the per-90 match rate, i.e. Var/mu^2 = 1/shape.
# 1e-6 is the numerical Poisson limit; 1e3 caps shape at 1e-3, far beyond any fitted dispersion.
MIN_RELATIVE_VARIANCE: Final[float] = 1e-6
MAX_RELATIVE_VARIANCE: Final[float] = 1e3

# Upper bound on minutes in one match (90 + stoppage + extra time); mirrors chk_minute_logic in schema.sql.
MAX_MATCH_MINUTES: Final[float] = 130.0


def validate_exposure_vector(
    minutes: Sequence[float], counts: Sequence[int], *, allow_empty: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """Validated (minutes float64, counts int64) arrays; raises ValueError on any polluted entry."""
    if len(minutes) != len(counts):
        raise ValueError(f"minutes and counts must have equal length, got {len(minutes)} and {len(counts)}")
    if len(minutes) == 0:
        if allow_empty:
            return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.int64)
        raise ValueError("exposure vectors must not be empty")
    raw_minutes = np.asarray(minutes)
    raw_counts = np.asarray(counts)
    if raw_minutes.ndim != 1 or raw_counts.ndim != 1:
        raise ValueError("exposure vectors must be one-dimensional")
    if raw_minutes.dtype.kind not in "iuf" or raw_counts.dtype.kind not in "iuf":
        raise ValueError("exposure vectors must be numeric; booleans, strings and None are rejected")
    np_minutes = raw_minutes.astype(np.float64)
    np_counts = raw_counts.astype(np.float64)
    if not (np.isfinite(np_minutes).all() and np.isfinite(np_counts).all()):
        raise ValueError("NaN or infinite value in exposure vectors")
    if (np_minutes < 0.0).any() or (np_minutes > MAX_MATCH_MINUTES).any():
        raise ValueError(f"minutes must lie in [0, {MAX_MATCH_MINUTES}]")
    if (np_counts < 0.0).any() or (np_counts != np.floor(np_counts)).any():
        raise ValueError("counts must be non-negative integers")
    if ((np_counts > 0.0) & (np_minutes == 0.0)).any():
        raise ValueError("a positive count cannot be recorded with zero minutes played")
    return np_minutes, np_counts.astype(np.int64)
```

`QuantEngine.gamma_poisson_update` (verbatim):

```python
    @staticmethod
    def gamma_poisson_update(
        prior: GammaPosterior, counts: Sequence[int], minutes: Sequence[float]
    ) -> GammaPosterior:
        np_minutes, np_counts = validate_exposure_vector(minutes, counts, allow_empty=True)
        return GammaPosterior(
            alpha=prior.alpha + float(np_counts.sum()),
            beta=prior.beta + float(np_minutes.sum()) / 90.0,
        )
```

---

## SECTION 3: DATABASE SCHEMA INTEGRITY & DATA POLLUTION PROTECTION

### 3.1 Why the Database Must Enforce Its Own Invariants

Python validation (§2) protects only data that passes through `parse_fixture_payload`. The core tables have other writers and other failure modes:

1. **Writers that bypass the transform:**
   - manual `psql` corrections;
   - historical backfills;
   - replays of `raw_payloads` through an *older* transform version;
   - the future `OddsPoller` [N] writing `odds_lines`;
   - any analyst with `INSERT` privileges.
   A `CHECK` constraint is the only invariant that holds regardless of the code path.
2. **Upstream feed corruption.** A vendor payload can carry impossible combinations that still parse as valid JSON: a won count exceeding its attempt count, a substitution minute before kick-on, possession of 101%. Each passes a type check and fails only a *relational* predicate.
3. **Numeric coercion is not validation.** PostgreSQL coerces rather than rejects in several cases:
   - `NUMERIC(5,2)` **rounds** excess fractional digits: `47.333 → 47.33`.
   - Integers overflowing `SMALLINT` raise `22003 numeric_value_out_of_range`, but only beyond \(\pm32767\), far outside any plausible count.
   - A value like `minutes_played = 9000` fits the type perfectly.

   Range and consistency invariants therefore need explicit predicates. Types alone never provide them.
4. **Downstream amplification.** A single impossible row poisons every estimator that reads it:
   - `tackles_won > tackles_attempted` makes \(\text{DPR}\) negative;
   - `minutes_played = 900` deflates a player's \(\hat\lambda\) through \(\beta_n = \beta_0 + \sum m_j/90\);
   - an `over_price_decimal` of 0.95 makes \(b = O - 1 < 0\) and inverts the Kelly sign.

**Three-layer parity invariant.** Every relational invariant on `player_match_stats` is enforced three times, and the layers MUST agree:

| Layer | Mechanism | Effect of a violation |
|---|---|---|
| L1 transform | `PlayerMatchRecord.__post_init__` | That fixture fails in the worker; nothing is staged. |
| L2 merge | `merge_staging_player_feed()` reject predicates | That row moves to `staging_rejects` with its full JSON; the merge continues. |
| L3 database | Named `CHECK` constraint | The statement, and therefore the **whole merge transaction**, aborts. |

L3 must never be the first layer to catch a row arriving through staging. A constraint that exists in L3 without an L2 predicate converts one bad row into a merge outage for every fixture on the slate. That is the brief's `chk_minute_logic` defect, closed here by adding `c.minutes_played > 130` and `c.minute_off < c.minute_on` to the reject predicate. `test_merge_rejects_every_row_a_core_constraint_would_refuse` enforces parity on both `schema.sql` and migration `003` [T].

| Constraint | Predicate | L1 (transform) | L2 (merge reject) |
|---|---|---|---|
| `chk_possession_bounds` | \(0 \le \text{team\_possession\_pct} \le 100\) | yes | `c.team_possession_pct NOT BETWEEN 0 AND 100` |
| `chk_minute_logic` | \(\text{minute\_off} \ge \text{minute\_on}\ \wedge\ \text{minutes\_played} \le 130\) | yes | `c.minute_off < c.minute_on`, `c.minutes_played > 130` |
| `chk_tackles_won_le_attempted` | \(\text{tackles\_won} \le \text{tackles\_attempted}\) | yes | `c.tackles_won > c.tackles_attempted` |
| `chk_duels_won_le_total` | \(\text{duels\_won} \le \text{duels\_total}\) | yes | `c.duels_won > c.duels_total` |
| `chk_price_realism` | \(O_{\text{over}} > 1 \wedge O_{\text{under}} > 1\) | n/a (odds do not pass through staging) | n/a |
| `chk_line_increment` | \(L \ge 0 \wedge 2L \in \mathbb{Z}\) | n/a | n/a |

**NULL semantics.** A `CHECK` rejects only when its predicate evaluates to `FALSE`. `NULL` passes.

- `team_possession_pct IS NULL` satisfies `chk_possession_bounds`: possession unknown.
- In `chk_price_realism`, a yes-only card market with `under_price_decimal IS NULL` evaluates `TRUE AND NULL = NULL` and is accepted. A price of 0.95 on either side evaluates `FALSE` and is rejected.
- `chk_some_price` separately requires at least one price.

### 3.2 Locking Profile of Constraint Changes

| Operation | Lock | Table scan | Blocks concurrent `INSERT`/`UPDATE` |
|---|---|---|---|
| `ADD CONSTRAINT … CHECK (…)` | `ACCESS EXCLUSIVE` | yes, **while holding the lock** | yes, for the full scan |
| `ADD CONSTRAINT … CHECK (…) NOT VALID` | `ACCESS EXCLUSIVE` | no (catalog only) | only for milliseconds |
| `VALIDATE CONSTRAINT …` | `SHARE UPDATE EXCLUSIVE` | yes | **no** |
| `DROP CONSTRAINT …` | `ACCESS EXCLUSIVE` | no | only for milliseconds |

A pending `ACCESS EXCLUSIVE` request queues behind any long transaction and blocks every later request queued behind it, including ingestion inserts. The migration therefore sets `lock_timeout = '5s'`, so a blocked request aborts instead of freezing the table. Run it outside match-day ingestion windows regardless.

### 3.3 Production SQL Check Assertions

**Fresh install (`schema.sql`).** The named constraints are declared in the table definitions, replacing the unnamed column checks of v3.1. Verbatim excerpts:

```sql
CREATE TABLE player_match_stats (
    fixture_id               BIGINT   NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    player_id                INTEGER  NOT NULL REFERENCES players(player_id),
    team_id                  INTEGER  NOT NULL REFERENCES teams(team_id),
    is_home                  BOOLEAN  NOT NULL,
    started                  BOOLEAN  NOT NULL,
    minute_on                SMALLINT NOT NULL DEFAULT 0 CHECK (minute_on >= 0),
    minute_off               SMALLINT NOT NULL DEFAULT 0 CHECK (minute_off >= 0),
    minutes_played           SMALLINT NOT NULL DEFAULT 0 CHECK (minutes_played >= 0),
    match_state_leading_min  SMALLINT NOT NULL DEFAULT 0,
    match_state_level_min    SMALLINT NOT NULL DEFAULT 0,
    match_state_trailing_min SMALLINT NOT NULL DEFAULT 0,

    tackles_attempted        SMALLINT NOT NULL DEFAULT 0,
    tackles_won              SMALLINT NOT NULL DEFAULT 0,
    interceptions            SMALLINT NOT NULL DEFAULT 0,
    dribbled_past            SMALLINT NOT NULL DEFAULT 0,
    duels_total              SMALLINT NOT NULL DEFAULT 0,
    duels_won                SMALLINT NOT NULL DEFAULT 0,

    fouls_committed          SMALLINT NOT NULL DEFAULT 0,
    fouls_drawn              SMALLINT NOT NULL DEFAULT 0,
    yellow_cards             SMALLINT NOT NULL DEFAULT 0 CHECK (yellow_cards BETWEEN 0 AND 2),
    red_cards                SMALLINT NOT NULL DEFAULT 0 CHECK (red_cards BETWEEN 0 AND 1),

    zone14_entries           SMALLINT NOT NULL DEFAULT 0,
    halfspace_entries        SMALLINT NOT NULL DEFAULT 0,
    long_balls_targeted      SMALLINT NOT NULL DEFAULT 0,

    team_possession_pct      NUMERIC(5,2),

    padj_tackles             NUMERIC(7,3),
    padj_interceptions       NUMERIC(7,3),
    padj_fouls               NUMERIC(7,3),
    dribbled_past_rate       NUMERIC(5,4),     -- NULL when undefined (no duels)

    source_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fixture_id, player_id),
    CONSTRAINT chk_possession_bounds        CHECK (team_possession_pct BETWEEN 0.00 AND 100.00),
    CONSTRAINT chk_minute_logic             CHECK (minute_off >= minute_on AND minutes_played <= 130),
    CONSTRAINT chk_tackles_won_le_attempted CHECK (tackles_won <= tackles_attempted),
    CONSTRAINT chk_duels_won_le_total       CHECK (duels_won <= duels_total)
);
```

```sql
CREATE TABLE odds_lines (
    line_id             BIGSERIAL PRIMARY KEY,
    fixture_id          BIGINT NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    sportsbook          VARCHAR(32) NOT NULL,
    market_type         VARCHAR(32) NOT NULL
                        CHECK (market_type IN ('tackles', 'fouls', 'fouls_drawn', 'yellow_card', 'red_card')),
    player_id           INTEGER REFERENCES players(player_id),
    line_value          NUMERIC(5,2) NOT NULL,
    over_price_decimal  NUMERIC(7,3),
    under_price_decimal NUMERIC(7,3),
    void_rule           VARCHAR(32) NOT NULL DEFAULT 'void_if_no_appearance'
                        CHECK (void_rule IN ('void_if_no_appearance', 'void_if_not_starting', 'no_void')),
    max_stake           NUMERIC(10,2),
    is_closing          BOOLEAN NOT NULL DEFAULT FALSE,
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_some_price     CHECK (over_price_decimal IS NOT NULL OR under_price_decimal IS NOT NULL),
    -- A NULL side (yes-only card markets) makes the conjunction NULL, which a CHECK accepts.
    CONSTRAINT chk_price_realism  CHECK (over_price_decimal > 1.000 AND under_price_decimal > 1.000),
    CONSTRAINT chk_line_increment CHECK (line_value >= 0.00 AND (line_value * 2) = trunc(line_value * 2)),
    CONSTRAINT uq_odds_snapshot UNIQUE NULLS NOT DISTINCT
        (fixture_id, sportsbook, market_type, player_id, line_value, captured_at)
);
```

**Pre-flight audit.** Run this before applying migration `003` to an existing database. It counts the rows each constraint would reject, using the same predicates and `NULL` semantics (`NOT (NULL)` is `NULL`, so such rows are not counted). Every count MUST be 0 before `VALIDATE`. A non-zero count requires correcting or deleting the named rows first.

```sql
SELECT 'chk_possession_bounds' AS constraint_name, count(*) AS violating_rows
FROM prop_alpha.player_match_stats
WHERE NOT (team_possession_pct BETWEEN 0.00 AND 100.00)
UNION ALL
SELECT 'chk_minute_logic', count(*)
FROM prop_alpha.player_match_stats
WHERE NOT (minute_off >= minute_on AND minutes_played <= 130)
UNION ALL
SELECT 'chk_tackles_won_le_attempted', count(*)
FROM prop_alpha.player_match_stats
WHERE NOT (tackles_won <= tackles_attempted)
UNION ALL
SELECT 'chk_duels_won_le_total', count(*)
FROM prop_alpha.player_match_stats
WHERE NOT (duels_won <= duels_total)
UNION ALL
SELECT 'chk_price_realism', count(*)
FROM prop_alpha.odds_lines
WHERE NOT (over_price_decimal > 1.000 AND under_price_decimal > 1.000)
UNION ALL
SELECT 'chk_line_increment', count(*)
FROM prop_alpha.odds_lines
WHERE NOT (line_value >= 0.00 AND (line_value * 2) = trunc(line_value * 2));
```

### 3.4 Migration 003 (upgrade path, verbatim)

The migration:

- is idempotent: each constraint is added only if absent from `pg_constraint`;
- is non-scanning under `ACCESS EXCLUSIVE`: `NOT VALID`;
- validates under `SHARE UPDATE EXCLUSIVE`;
- replaces the merge function with the parity-complete reject predicate;
- drops the superseded auto-named column checks only after their named replacements have validated.

Run with `psql -v ON_ERROR_STOP=1`. Without it, psql continues past a failed statement.

```sql
-- Migration 003: named integrity constraints and merge reject-predicate parity (v3.1 -> v3.2).
-- Run with: psql -v ON_ERROR_STOP=1 -f migrations/003_integrity_constraints.sql
-- Lock profile:
--   ADD CONSTRAINT ... NOT VALID  ACCESS EXCLUSIVE, catalog-only, no table scan (milliseconds)
--   VALIDATE CONSTRAINT           SHARE UPDATE EXCLUSIVE: concurrent INSERT/UPDATE continue
--   DROP CONSTRAINT               ACCESS EXCLUSIVE, catalog-only
-- lock_timeout stops an ACCESS EXCLUSIVE request from queueing behind a long transaction and
-- stalling every writer queued behind it. Run outside match-day ingestion windows regardless.

SET lock_timeout = '5s';

BEGIN;

DO $migration$
DECLARE
    spec record;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('prop_alpha.player_match_stats'::regclass, 'chk_possession_bounds',
             'CHECK (team_possession_pct BETWEEN 0.00 AND 100.00)'),
            ('prop_alpha.player_match_stats'::regclass, 'chk_minute_logic',
             'CHECK (minute_off >= minute_on AND minutes_played <= 130)'),
            ('prop_alpha.player_match_stats'::regclass, 'chk_tackles_won_le_attempted',
             'CHECK (tackles_won <= tackles_attempted)'),
            ('prop_alpha.player_match_stats'::regclass, 'chk_duels_won_le_total',
             'CHECK (duels_won <= duels_total)'),
            ('prop_alpha.odds_lines'::regclass, 'chk_price_realism',
             'CHECK (over_price_decimal > 1.000 AND under_price_decimal > 1.000)'),
            ('prop_alpha.odds_lines'::regclass, 'chk_line_increment',
             'CHECK (line_value >= 0.00 AND (line_value * 2) = trunc(line_value * 2))')
        ) AS v(tbl, conname, definition)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conrelid = spec.tbl AND conname = spec.conname
        ) THEN
            EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s NOT VALID', spec.tbl, spec.conname, spec.definition);
            RAISE NOTICE 'added % on % (NOT VALID)', spec.conname, spec.tbl;
        ELSE
            RAISE NOTICE '% already present on %; skipped', spec.conname, spec.tbl;
        END IF;
    END LOOP;
END
$migration$;

CREATE OR REPLACE FUNCTION prop_alpha.merge_staging_player_feed()
RETURNS TABLE (fixtures_merged integer, player_rows_merged integer, rows_rejected integer)
LANGUAGE plpgsql
AS $$
DECLARE
    v_fixtures  integer := 0;
    v_rows      integer := 0;
    v_rejected  integer := 0;
    v_step      integer := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('prop_alpha.merge_staging_player_feed'));

    DROP TABLE IF EXISTS pg_temp.claimed_feed;
    CREATE TEMP TABLE claimed_feed (LIKE prop_alpha.staging_player_feed) ON COMMIT DROP;

    WITH drained AS (
        DELETE FROM prop_alpha.staging_player_feed RETURNING *
    )
    INSERT INTO claimed_feed SELECT * FROM drained;

    WITH bad AS (
        DELETE FROM claimed_feed c
        WHERE NOT EXISTS (SELECT 1 FROM prop_alpha.teams t WHERE t.vendor_team_uid = c.home_team_uid)
           OR NOT EXISTS (SELECT 1 FROM prop_alpha.teams t WHERE t.vendor_team_uid = c.away_team_uid)
           OR c.fixture_status NOT IN ('scheduled', 'live', 'final', 'postponed')
        RETURNING c.*
    )
    INSERT INTO prop_alpha.staging_rejects (vendor_fixture_uid, vendor_player_uid, reason, row_data)
    SELECT b.vendor_fixture_uid, b.vendor_player_uid, 'unknown team uid or invalid fixture status', to_jsonb(b)
    FROM bad b;
    GET DIAGNOSTICS v_step = ROW_COUNT;
    v_rejected := v_rejected + v_step;

    WITH bad AS (
        DELETE FROM claimed_feed c
        WHERE c.vendor_player_uid IS NOT NULL
          AND (   c.team_uid IS NULL
               OR c.team_uid NOT IN (c.home_team_uid, c.away_team_uid)
               OR c.player_name IS NULL
               OR c.player_position IS NULL
               OR c.tackles_won > c.tackles_attempted
               OR c.duels_won > c.duels_total
               OR c.yellow_cards NOT BETWEEN 0 AND 2
               OR c.red_cards NOT BETWEEN 0 AND 1
               OR c.minutes_played < 0
               OR c.minutes_played > 130
               OR c.minute_off < c.minute_on
               OR c.minute_on < 0
               OR c.minute_off < 0
               OR (c.team_possession_pct IS NOT NULL AND c.team_possession_pct NOT BETWEEN 0 AND 100))
        RETURNING c.*
    )
    INSERT INTO prop_alpha.staging_rejects (vendor_fixture_uid, vendor_player_uid, reason, row_data)
    SELECT b.vendor_fixture_uid, b.vendor_player_uid, 'player row violates core constraints', to_jsonb(b)
    FROM bad b;
    GET DIAGNOSTICS v_step = ROW_COUNT;
    v_rejected := v_rejected + v_step;

    INSERT INTO prop_alpha.referees AS r (full_name, league_id, vendor_referee_uid)
    SELECT DISTINCT ON (c.referee_uid)
           COALESCE(c.referee_name, 'unknown'), t.league_id, c.referee_uid
    FROM claimed_feed c
    JOIN prop_alpha.teams t ON t.vendor_team_uid = c.home_team_uid
    WHERE c.referee_uid IS NOT NULL
    ORDER BY c.referee_uid, c.staged_at DESC
    ON CONFLICT (vendor_referee_uid) DO UPDATE SET full_name = EXCLUDED.full_name;

    INSERT INTO prop_alpha.fixtures AS fx (
        league_id, home_team_id, away_team_id, referee_id, referee_confirmed_at,
        kickoff_at, matchday, home_score, away_score, status, vendor_fixture_uid
    )
    SELECT th.league_id, th.team_id, ta.team_id, rf.referee_id,
           CASE WHEN rf.referee_id IS NOT NULL THEN now() END,
           x.kickoff_at, x.matchday, x.home_score, x.away_score, x.fixture_status, x.vendor_fixture_uid
    FROM (
        SELECT DISTINCT ON (cf.vendor_fixture_uid) cf.*
        FROM claimed_feed cf
        ORDER BY cf.vendor_fixture_uid, cf.staged_at DESC
    ) x
    JOIN prop_alpha.teams th ON th.vendor_team_uid = x.home_team_uid
    JOIN prop_alpha.teams ta ON ta.vendor_team_uid = x.away_team_uid
    LEFT JOIN prop_alpha.referees rf ON rf.vendor_referee_uid = x.referee_uid
    ORDER BY x.vendor_fixture_uid
    ON CONFLICT (vendor_fixture_uid) DO UPDATE SET
        referee_id           = COALESCE(EXCLUDED.referee_id, fx.referee_id),
        referee_confirmed_at = COALESCE(fx.referee_confirmed_at, EXCLUDED.referee_confirmed_at),
        kickoff_at           = EXCLUDED.kickoff_at,
        matchday             = COALESCE(EXCLUDED.matchday, fx.matchday),
        home_score           = EXCLUDED.home_score,
        away_score           = EXCLUDED.away_score,
        status               = EXCLUDED.status,
        ingested_at          = now();
    GET DIAGNOSTICS v_fixtures = ROW_COUNT;

    INSERT INTO prop_alpha.players AS pl (full_name, primary_position, current_team_id, vendor_player_uid)
    SELECT DISTINCT ON (c.vendor_player_uid)
           c.player_name, c.player_position, t.team_id, c.vendor_player_uid
    FROM claimed_feed c
    JOIN prop_alpha.teams t ON t.vendor_team_uid = c.team_uid
    WHERE c.vendor_player_uid IS NOT NULL
    ORDER BY c.vendor_player_uid, c.staged_at DESC
    ON CONFLICT (vendor_player_uid) DO UPDATE SET
        full_name        = EXCLUDED.full_name,
        primary_position = EXCLUDED.primary_position,
        updated_at       = now();

    INSERT INTO prop_alpha.player_match_stats AS s (
        fixture_id, player_id, team_id, is_home, started, minute_on, minute_off, minutes_played,
        match_state_leading_min, match_state_level_min, match_state_trailing_min,
        tackles_attempted, tackles_won, interceptions, dribbled_past, duels_total, duels_won,
        fouls_committed, fouls_drawn, yellow_cards, red_cards,
        zone14_entries, halfspace_entries, long_balls_targeted, team_possession_pct,
        padj_tackles, padj_interceptions, padj_fouls, dribbled_past_rate
    )
    SELECT f.fixture_id, p.player_id, t.team_id,
           COALESCE(x.is_home, x.team_uid = x.home_team_uid), COALESCE(x.started, FALSE),
           COALESCE(x.minute_on, 0), COALESCE(x.minute_off, 0), COALESCE(x.minutes_played, 0),
           COALESCE(x.match_state_leading_min, 0), COALESCE(x.match_state_level_min, 0),
           COALESCE(x.match_state_trailing_min, 0),
           COALESCE(x.tackles_attempted, 0), COALESCE(x.tackles_won, 0), COALESCE(x.interceptions, 0),
           COALESCE(x.dribbled_past, 0), COALESCE(x.duels_total, 0), COALESCE(x.duels_won, 0),
           COALESCE(x.fouls_committed, 0), COALESCE(x.fouls_drawn, 0),
           COALESCE(x.yellow_cards, 0), COALESCE(x.red_cards, 0),
           COALESCE(x.zone14_entries, 0), COALESCE(x.halfspace_entries, 0), COALESCE(x.long_balls_targeted, 0),
           x.team_possession_pct, x.padj_tackles, x.padj_interceptions, x.padj_fouls, x.dribbled_past_rate
    FROM (
        SELECT DISTINCT ON (cf.vendor_fixture_uid, cf.vendor_player_uid) cf.*
        FROM claimed_feed cf
        WHERE cf.vendor_player_uid IS NOT NULL
        ORDER BY cf.vendor_fixture_uid, cf.vendor_player_uid, cf.staged_at DESC
    ) x
    JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = x.vendor_fixture_uid
    JOIN prop_alpha.players  p ON p.vendor_player_uid  = x.vendor_player_uid
    JOIN prop_alpha.teams    t ON t.vendor_team_uid    = x.team_uid
    ORDER BY f.fixture_id, p.player_id
    ON CONFLICT (fixture_id, player_id) DO UPDATE SET
        team_id                  = EXCLUDED.team_id,
        is_home                  = EXCLUDED.is_home,
        started                  = EXCLUDED.started,
        minute_on                = EXCLUDED.minute_on,
        minute_off               = EXCLUDED.minute_off,
        minutes_played           = EXCLUDED.minutes_played,
        match_state_leading_min  = EXCLUDED.match_state_leading_min,
        match_state_level_min    = EXCLUDED.match_state_level_min,
        match_state_trailing_min = EXCLUDED.match_state_trailing_min,
        tackles_attempted        = EXCLUDED.tackles_attempted,
        tackles_won              = EXCLUDED.tackles_won,
        interceptions            = EXCLUDED.interceptions,
        dribbled_past            = EXCLUDED.dribbled_past,
        duels_total              = EXCLUDED.duels_total,
        duels_won                = EXCLUDED.duels_won,
        fouls_committed          = EXCLUDED.fouls_committed,
        fouls_drawn              = EXCLUDED.fouls_drawn,
        yellow_cards             = EXCLUDED.yellow_cards,
        red_cards                = EXCLUDED.red_cards,
        zone14_entries           = EXCLUDED.zone14_entries,
        halfspace_entries        = EXCLUDED.halfspace_entries,
        long_balls_targeted      = EXCLUDED.long_balls_targeted,
        team_possession_pct      = EXCLUDED.team_possession_pct,
        padj_tackles             = EXCLUDED.padj_tackles,
        padj_interceptions       = EXCLUDED.padj_interceptions,
        padj_fouls               = EXCLUDED.padj_fouls,
        dribbled_past_rate       = EXCLUDED.dribbled_past_rate,
        source_updated_at        = now();
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    RETURN QUERY SELECT v_fixtures, v_rows, v_rejected;
END;
$$;

COMMIT;

-- Scans existing rows without blocking writers. A failure names the violated constraint; locate the
-- offending rows with the pre-flight audit in ARCHITECTURE-ESSENTIALS.md section 3.3 and fix them first.
ALTER TABLE prop_alpha.player_match_stats VALIDATE CONSTRAINT chk_possession_bounds;
ALTER TABLE prop_alpha.player_match_stats VALIDATE CONSTRAINT chk_minute_logic;
ALTER TABLE prop_alpha.player_match_stats VALIDATE CONSTRAINT chk_tackles_won_le_attempted;
ALTER TABLE prop_alpha.player_match_stats VALIDATE CONSTRAINT chk_duels_won_le_total;
ALTER TABLE prop_alpha.odds_lines         VALIDATE CONSTRAINT chk_price_realism;
ALTER TABLE prop_alpha.odds_lines         VALIDATE CONSTRAINT chk_line_increment;

-- Superseded auto-named column checks from schema v2/v3 (identical predicates, now duplicated by the
-- named constraints above). Dropped only after the named replacements have validated.
ALTER TABLE prop_alpha.player_match_stats DROP CONSTRAINT IF EXISTS player_match_stats_team_possession_pct_check;
ALTER TABLE prop_alpha.odds_lines         DROP CONSTRAINT IF EXISTS odds_lines_line_value_check;
ALTER TABLE prop_alpha.odds_lines         DROP CONSTRAINT IF EXISTS odds_lines_over_price_decimal_check;
ALTER TABLE prop_alpha.odds_lines         DROP CONSTRAINT IF EXISTS odds_lines_under_price_decimal_check;

RESET lock_timeout;
```

**Status:** [U]. Structure is asserted by `test_migration_adds_not_valid_then_validates` and `test_migration_adds_constraints_without_scanning_and_is_idempotent`, but the SQL has not executed against a server. `ci/bootstrap.sh` stage 6 executes it (§4).

---

## SECTION 4: CONTINUOUS INTEGRATION & ENVIRONMENT BOOTSTRAPPING

### 4.1 Deterministic Initialization Strategy

A CI run is **deterministic** when two runs on the same commit and interpreter version install byte-identical dependency sets and execute the same tests with the same outcomes. `ci/bootstrap.sh` enforces this with eight controls:

1. **Interpreter floor.** Python \(\ge 3.12\), checked before anything is created.
2. **Hermetic environment.** A fresh virtual environment is created every run. An existing directory is deleted only if it contains `pyvenv.cfg`; the script refuses to delete anything else. The venv interpreter is invoked by path, never via `source activate`, so the shell's `set -u` cannot trip over activation-script variables.
3. **Exact pins.** `requirements.txt` pins every direct and transitive dependency with `==`, to the versions the 153 tests executed against. The preflight rejects any unpinned line. `pip` itself is pinned (`26.2.1`).
4. **Binary wheels only.** `--only-binary=:all:` makes an unavailable wheel a hard failure, rather than a source build whose compiler and BLAS vary by runner. This is exactly how the brief's `numpy==1.26.4` fails on Python 3.14: loudly, at install time.
5. **Dependency consistency.** `pip check` verifies that the resolved set has no conflicts. `pip freeze --all` is archived to `ci-artifacts/pip-freeze.txt`.
6. **Reproducible hashing.** `PYTHONHASHSEED=0` fixes set and dict iteration order for any hash-seeded behaviour.
7. **No silent skips.** The concurrency suite uses `pytest.importorskip("psycopg2")`. If the driver failed to install, pytest would report success with the module skipped. The script imports `psycopg2` explicitly (stage 4), then parses the JUnit report (stage 5) and fails unless \(\text{tests} \ge 153\), \(\text{skipped} = 0\), \(\text{failures} = 0\) and \(\text{errors} = 0\).
8. **No secrets.** The suite needs no vendor key or production database: all I/O tests use in-memory fakes. The only credential the script reads is the optional `PROP_ALPHA_CI_DATABASE_URL`, injected from the CI secret store. It is honoured only if the target database name ends in `_ci`, because stage 6 runs `DROP SCHEMA prop_alpha CASCADE`.

**Remaining gaps [N]:**

- Hash-locked requirements (`pip install --require-hashes` against a lockfile generated by `pip-compile --generate-hashes`), to guarantee artefact integrity as well as version identity.
- mypy or pyright (R-TYPE-10).
- A migration ledger.
- An end-to-end ingest → merge → reconcile integration test against PostgreSQL (PRD Gate 1).

### 4.2 Pinned Dependency Manifest (`requirements.txt`, verbatim)

```text
# Exact versions the prop_alpha v3.2 test suite was executed against (CPython 3.14.6).
# numpy 2.5.1 and scipy 1.18.0 declare Requires-Python >= 3.12, which sets the runtime floor.
# psycopg2-binary is pinned to the current release but has NOT yet run against a live server.
numpy==2.5.1
scipy==1.18.0
requests==2.34.2
urllib3==2.7.0
certifi==2026.6.17
idna==3.18
charset-normalizer==3.4.7
psycopg2-binary==2.9.13
pytest==9.1.1
iniconfig==2.3.0
pluggy==1.6.0
packaging==26.2
pygments==2.20.0
colorama==0.4.6 ; sys_platform == "win32"
```

### 4.3 Bash Hardening Boot Script (`ci/bootstrap.sh`, verbatim)

```bash
#!/usr/bin/env bash
# prop_alpha CI bootstrap: isolated virtual environment, exact-pinned dependencies, full test matrix,
# and an optional PostgreSQL schema smoke test.
#
# Usage:   ci/bootstrap.sh [--preflight]
#   --preflight   run interpreter and repository checks only; create nothing, install nothing.
#
# Environment (all optional):
#   PROP_ALPHA_PYTHON             interpreter used to build the venv          (default: python3)
#   PROP_ALPHA_VENV_DIR           virtual environment location                (default: <repo>/.venv-ci)
#   PROP_ALPHA_ARTIFACT_DIR       pip freeze and JUnit output                 (default: <repo>/ci-artifacts)
#   PROP_ALPHA_CI_DATABASE_URL    disposable PostgreSQL 15+ database whose name ends in _ci; enables stage 6.
#                                 Inject from the CI secret store. Never commit it.
#
# The test suite needs no vendor credentials or production database: tests use in-memory fakes.

set -Eeuo pipefail
IFS=$'\n\t'

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly PYTHON_BIN="${PROP_ALPHA_PYTHON:-python3}"
readonly VENV_DIR="${PROP_ALPHA_VENV_DIR:-${REPO_ROOT}/.venv-ci}"
readonly ARTIFACT_DIR="${PROP_ALPHA_ARTIFACT_DIR:-${REPO_ROOT}/ci-artifacts}"
readonly PIP_VERSION="26.2.1"
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=12
readonly EXPECTED_TESTS=153

log() { printf '%s [ci] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*" >&2; exit 1; }
on_error() {
  local status=$?
  log "FATAL: '${BASH_COMMAND}' exited with status ${status} at line ${BASH_LINENO[0]}" >&2
  exit "${status}"
}
trap on_error ERR

PREFLIGHT_ONLY=0
if [[ $# -gt 1 ]]; then
  die "usage: ci/bootstrap.sh [--preflight]"
elif [[ $# -eq 1 ]]; then
  [[ "$1" == "--preflight" ]] || die "unknown argument '$1'; usage: ci/bootstrap.sh [--preflight]"
  PREFLIGHT_ONLY=1
fi
readonly PREFLIGHT_ONLY

cd -- "${REPO_ROOT}"

log "stage 1/6: interpreter and repository checks"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "interpreter '${PYTHON_BIN}' not found; set PROP_ALPHA_PYTHON"
if ! "${PYTHON_BIN}" -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)"; then
  die "Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} is required by the pinned numpy/scipy; found $("${PYTHON_BIN}" -V 2>&1)"
fi
log "interpreter: $("${PYTHON_BIN}" -V 2>&1)"

for required in \
  requirements.txt schema.sql migrations/003_integrity_constraints.sql prop_alpha/__init__.py \
  tests/test_core.py tests/test_integrity.py tests/test_pipeline_concurrency.py tests/test_typing_contract.py; do
  [[ -f "${required}" ]] || die "repository is missing ${required}"
done

unpinned="$(grep -Ev '^[[:space:]]*(#|$)' requirements.txt \
  | grep -Ev '^[A-Za-z0-9._-]+==[^[:space:];]+([[:space:]]*;.*)?$' || true)"
[[ -z "${unpinned}" ]] || die "requirements.txt contains entries not pinned with '==': ${unpinned}"

if [[ "${PREFLIGHT_ONLY}" -eq 1 ]]; then
  log "preflight passed; nothing created or installed"
  exit 0
fi

log "stage 2/6: fresh virtual environment at ${VENV_DIR}"
if [[ -e "${VENV_DIR}" ]]; then
  [[ -f "${VENV_DIR}/pyvenv.cfg" ]] || die "${VENV_DIR} exists but is not a virtual environment; refusing to delete it"
  rm -rf -- "${VENV_DIR}"
fi
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
if [[ -x "${VENV_DIR}/bin/python" ]]; then
  VENV_PY="${VENV_DIR}/bin/python"
elif [[ -x "${VENV_DIR}/Scripts/python.exe" ]]; then
  VENV_PY="${VENV_DIR}/Scripts/python.exe"
else
  die "virtual environment at ${VENV_DIR} has no interpreter"
fi
readonly VENV_PY

log "stage 3/6: pinned dependencies (binary wheels only)"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONHASHSEED=0
"${VENV_PY}" -m pip install --quiet "pip==${PIP_VERSION}"
"${VENV_PY}" -m pip install --quiet --only-binary=:all: --requirement requirements.txt
"${VENV_PY}" -m pip check
mkdir -p -- "${ARTIFACT_DIR}"
"${VENV_PY}" -m pip freeze --all > "${ARTIFACT_DIR}/pip-freeze.txt"

log "stage 4/6: import and byte-compile checks"
"${VENV_PY}" -c "import numpy, scipy, requests, psycopg2, pytest; print('numpy', numpy.__version__, '| scipy', scipy.__version__, '| requests', requests.__version__, '| psycopg2', psycopg2.__version__, '| pytest', pytest.__version__)"
"${VENV_PY}" -m compileall -q prop_alpha tests

log "stage 5/6: test matrix (maths, integrity, typing contract, concurrency)"
"${VENV_PY}" -m pytest tests -vv -p no:cacheprovider --strict-markers --junitxml="${ARTIFACT_DIR}/junit.xml"
"${VENV_PY}" - "${ARTIFACT_DIR}/junit.xml" "${EXPECTED_TESTS}" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
totals = {key: sum(int(suite.get(key, 0)) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
expected = int(sys.argv[2])
print(f"junit totals: {totals}")
if totals["skipped"] or totals["failures"] or totals["errors"] or totals["tests"] < expected:
    sys.exit(f"test matrix incomplete: {totals}; expected at least {expected} executed tests and none skipped")
PY

if [[ -n "${PROP_ALPHA_CI_DATABASE_URL:-}" ]]; then
  log "stage 6/6: PostgreSQL schema smoke test"
  command -v psql >/dev/null 2>&1 || die "psql is required when PROP_ALPHA_CI_DATABASE_URL is set"
  psql_ci() { psql "${PROP_ALPHA_CI_DATABASE_URL}" -X -q -v ON_ERROR_STOP=1 "$@"; }
  db_name="$(psql_ci -At -c 'SELECT current_database()')"
  [[ "${db_name}" == *_ci ]] || die "refusing to reset database '${db_name}': CI database names must end in _ci"
  server_version_num="$(psql_ci -At -c 'SHOW server_version_num')"
  (( server_version_num >= 150000 )) || die "PostgreSQL 15+ required; server_version_num=${server_version_num}"
  psql_ci -c 'DROP SCHEMA IF EXISTS prop_alpha CASCADE'
  psql_ci -f schema.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  psql_ci -f migrations/003_integrity_constraints.sql
  psql_ci -c 'SELECT * FROM prop_alpha.merge_staging_player_feed()'
  log "schema smoke test passed on ${db_name}"
else
  log "stage 6/6 skipped: PROP_ALPHA_CI_DATABASE_URL is unset, so schema.sql was not executed in this run"
fi

log "SUCCESS: environment built from pinned wheels; ${EXPECTED_TESTS}+ tests passed with none skipped"
```

**Stage summary.**

| Stage | Action | Failure means |
|---|---|---|
| 1 | Interpreter ≥ 3.12; required files present; every requirement pinned `==`. `--preflight` stops here. | Wrong runner image or an unpinned dependency. |
| 2 | Fresh venv, with a guarded delete. | Filesystem or interpreter problem. |
| 3 | `pip==26.2.1`; binary-only pinned install; `pip check`; freeze artifact. | A pin has no wheel for this platform or Python, or the set is inconsistent. |
| 4 | Import `numpy`, `scipy`, `requests`, `psycopg2`, `pytest`; `compileall`. | Broken wheel, or a syntax error anywhere in `prop_alpha/` or `tests/`. |
| 5 | Full pytest matrix with JUnit output; the gate requires ≥ 153 executed tests, 0 skipped, 0 failures, 0 errors. | A regression, or a silently skipped suite. |
| 6 | Optional, when `PROP_ALPHA_CI_DATABASE_URL` is set. Checks that the database name ends in `_ci` and that the server is ≥ 150000. Then: `DROP SCHEMA`, apply `schema.sql`, execute `merge_staging_player_feed()` on empty staging (PL/pgSQL bodies are only fully checked on first execution), apply migration `003` (proves idempotency on a v3.2 schema), and execute the merge again. | DDL or PL/pgSQL error, or a non-idempotent migration. |

### 4.4 Verification Status of the CI Script

| Check | Result |
|---|---|
| `bash -n ci/bootstrap.sh` | passed |
| `ci/bootstrap.sh --preflight` (Python 3.14.6) | passed: stage 1 only, nothing created |
| Missing interpreter (`PROP_ALPHA_PYTHON=nonexistent_python`) | exit 1: `interpreter 'nonexistent_python' not found` |
| Unknown argument | exit 1 with usage |
| Unpinned line injected (`scipy>=1.12`) | exit 1: `requirements.txt contains entries not pinned with '=='` |
| JUnit gate on a full run | exit 0: `{'tests': 153, 'failures': 0, 'errors': 0, 'skipped': 0}` |
| JUnit gate with `psycopg2` absent | exit 1: `{'tests': 142, …, 'skipped': 1}`. The silent-skip case the brief's script would pass. |
| Stages 2–6 end-to-end | **not executed.** They download tens of megabytes of wheels from PyPI and require a PostgreSQL 15+ instance. `shellcheck` is not installed on the authoring machine. |
