# prop_alpha — Revision 3: Numerical Hardening, Concurrent Ingestion, Staging Merge, Mean-Variance Allocation

This revision implements the four refactor tasks against the v2 codebase. Every Python module below was extracted from the package and run under the test suite: **87 tests pass**, and the timing-based concurrency tests passed five consecutive runs. The full package, including the tests, is in `prop_alpha_v3_code.zip`.

| Task | File(s) | Status |
|---|---|---|
| 1. Engine hardening | `engine.py` | Implemented and tested. Two prescribed mechanisms **replaced**; see §A |
| 2. Concurrent ingestion | `pipeline.py`, `transform.py` | Implemented; tested against in-memory fakes of the pool, connection and vendor |
| 3. Staging + merge + index | `schema.sql`, `migrations/002_staging_and_indexes.sql` | Written. `TRUNCATE` **replaced**; see §A. **Not executed** against PostgreSQL (none available here) |
| 4. Mean-variance allocator | `market_scanner.py` | Implemented and tested, including exact covariance checks |

---

## A. Where this departs from the spec, and why

These are the places where following the spec literally would have lost money or data. Each replacement meets the stated goal of its task.

**A1. Variance floor `max(raw_variance, μ + 1e-5)` (Task 1) — replaced.**

- The quantity being floored is the variance of the per-90 **rate**, while $\mu + 10^{-5}$ is on the scale of a Poisson **count** variance. The floor therefore triggers on ordinary markets, not just degenerate ones.
- On a typical yellow-card projection (rate 0.184/90, true rate variance 0.00197), the floor raises the variance roughly 93-fold and drops P(carded) from **16.8% to 12.0%**. That 4.8-point bias would register as false "No card" edges across the whole slate.
- **What the crash risk actually is.** The analytic variance can never be ≤ 0. The real hazard is floating-point cancellation in the subtraction form $E[X^2] - \mu^2$, which returns $-1.4\times10^{-14}$ at $\alpha = 10^{17}$.
- **v3 fix.** v3 computes the exact relative form $\text{Var}/\mu^2 = 1/\alpha + 1/k + 1/(\alpha k)$. It is a sum of non-negative terms, so it cannot cancel. It is then clamped to $[10^{-6}, 10^{3}]$, bounds that never bind for sane inputs, so card pricing is unchanged. Regression tests cover both points.

**A2. Fallback to `poisson(0.0)` on corrupted parameters (Task 1) — replaced.**

- A zero-rate distribution asserts "Under is certain". Every Under price then shows positive EV: a 1.25 Under would read as +25% per unit. The allocator would stake its maximum on exactly the markets whose data is broken.
- **v3 fix.** v3 fails closed. Corrupt inputs raise `ProjectionError`, and a `PropProjection` with invalid parameters cannot be constructed, so `count_distribution` cannot fail downstream. The allocator catches the error, skips that market, and reports it in `AllocationReport.skipped`. The process never crashes and never trades corrupted data.

**A3. "Read staging, upsert, then TRUNCATE" (Task 3) — replaced with a claim-by-delete.**

- `TRUNCATE` takes an `ACCESS EXCLUSIVE` lock, stalling every ingestion writer during the merge.
- More seriously, rows committed between the read and the truncate are **silently destroyed**.
- **v3 fix.** `merge_staging_player_feed()` claims rows with `DELETE … RETURNING` inside a single statement, under a transaction-scoped advisory lock. It drains exactly the rows it merges; rows committed later wait for the next merge; writers are never blocked. Staging still ends empty of everything merged.
- **Additional behaviour:**
  - Latest row per key wins, so `ON CONFLICT` never touches a row twice.
  - Rows that would violate core constraints go to `staging_rejects` instead of aborting the batch.
  - All upserts order by their conflict key for a consistent lock order.

**A4. Mean-variance utility is computed per position, not per unit stake (Task 4).**

- **Why not per unit.** A per-unit score $EV - \lambda\,\text{Var}$ is dominated by the variance term: per-unit variance is ~0.76 at odds 1.80 but ~1.83 at 2.83, while EVs are ~0.06. It would pick the shortest price almost regardless of edge, and $\lambda$ would have no stable meaning.
- **What v3 maximises.** v3 maximises $U(f) = f^\top\mu - \lambda f^\top\Sigma f$ over stake fractions. $\Sigma$ is the **exact** covariance between every line on the player, derived from the predictive distribution; all lines settle on the same count.
- **Relationship to fractional Kelly.** For small edges the optimum is $f = \mu / (2\lambda\sigma^2)$ against full Kelly $\approx \mu/\sigma^2$. So $\lambda = 1/(2\psi)$ reproduces $\psi$-fractional Kelly, and the default $\lambda = 2$ behaves like quarter Kelly. Each line is also capped at its full-Kelly fraction.
- **Premise correction.** v2 would not have put "the entire Kelly allocation" on the 2.5 line: Kelly sizes longer odds smaller, so it would have staked 0.87%. The real defect was *selection*. v2 chose the 2.5 line by maximum EV, whereas v3 puts **1.94%** on the 1.5 line and **0.04%** on the 2.5 line. This scenario is a test case.

**A5. Concurrency cannot beat the vendor's rate limit (Task 2).**

- The shared token bucket still enforces the contracted requests per second across all threads. Parallelism removes the *latency* bottleneck (sequential round trips and retry sleeps), not the *rate* bottleneck. At 5 req/s, a 40-fixture slate needs at least 8 s however many workers you run.
- If the lineup window matters commercially, negotiate a higher rate for it (`DATA_VENDOR_RPS`).
- **The pre-kickoff lineup path.** The stats feed is not what changes 60 minutes before kickoff; the team sheet is. v3 therefore adds a `lineups` job. It writes confirmed starters and bench to `lineup_expectations` and zeroes every player not on the sheet, so the allocator immediately stops pricing players who will not play.

**A6. Index latency.** A B-tree lookup on a warm cache is sub-millisecond, not microseconds, and the network round trip to the database dominates either way.

- `(fixture_id, captured_at DESC)` was added and the BRIN dropped, as specified; out-of-order writes do defeat BRIN.
- The scanner's own hot query (latest price per player-market) is still best served by the existing `(fixture_id, market_type, player_id, captured_at DESC)` index.

## B. Architecture of the refactored ingestion path

```
vendor ──HTTP (thread-local sessions, shared token bucket)──► N worker threads
   each worker: fetch → parse/validate → [pooled conn, short txn] raw_payloads + staging_player_feed (UNLOGGED, append-only)
   no DB connection is held during network I/O (asserted by test)
   transient DB errors (deadlock, serialization, lock/statement timeout, dropped conn) → rollback + jittered retry
   one fixture failing or timing out never affects another
after the slate: one serialized call → merge_staging_player_feed() → referees, fixtures, players, player_match_stats
lineups job: worker → lineup_expectations upsert + zero absent players (disjoint rows per fixture, no merge needed)
```

The pool is sized at `max_workers + 1`, and construction fails if it is smaller. Every connection carries `statement_timeout = 10s` and `lock_timeout = 2s`, so a lock stall becomes a retry instead of a hang.

## C. What still stands between this and a fund-readiness claim

A code refactor cannot certify an 8.5/10, and I won't assign one. The remaining blockers are not code quality:

1. **The SQL has never run.** There is no PostgreSQL here. The merge function is the most complex untested piece. Run `schema.sql`, or `migrations/002` on an existing v2 database, on PostgreSQL 15+. Then run an end-to-end test: seed teams, ingest a payload, merge, and check the row counts and `staging_rejects`.
2. **The model is unvalidated.** The go-live gates in v2 §6.4 (walk-forward Brier skill, calibration, positive CLV over paper bets) still apply. None of this revision changes model accuracy.
3. **Market capacity.** These books cap stakes at roughly £50–£500 and restrict winning accounts. `max_stake` clips every position, so a "significant bankroll" cannot actually be deployed into these markets. Size expectations to the limits, not the bankroll.
4. **Uncalibrated assumption.** `bench_sub_probability` (default 0.35) is a placeholder. The schema does not currently record unused substitutes, which you need in order to estimate it; add that to the lineup feed.

---

## TASK 1 — `engine.py` (prop_alpha/modeling/engine.py)

```python
# file: prop_alpha/modeling/engine.py
"""Predictive engine: Gamma-Poisson posteriors with minutes exposure, referee adjustment,
game-state overdispersion, lineup/void handling, and bivariate Poisson dependence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

import numpy as np
from scipy.stats import nbinom, poisson

REFEREE_SENSITIVE_MARKETS = frozenset({"fouls", "fouls_drawn"})

# Relative-variance bounds on the per-90 match rate, i.e. Var/mu^2 = 1/shape.
# 1e-6 is the numerical Poisson limit; 1e3 caps shape at 1e-3, far beyond any fitted dispersion.
MIN_RELATIVE_VARIANCE = 1e-6
MAX_RELATIVE_VARIANCE = 1e3


class ProjectionError(ValueError):
    """Raised when inputs cannot produce a valid predictive distribution; callers must not trade it."""


class VoidRule(str, Enum):
    VOID_IF_NO_APPEARANCE = "void_if_no_appearance"
    VOID_IF_NOT_STARTING = "void_if_not_starting"
    NO_VOID = "no_void"


@dataclass(frozen=True)
class RefereeProfile:
    strictness_coefficient: float
    yellow_card_propensity: float
    red_card_propensity: float
    home_bias_ratio: float

    def __post_init__(self) -> None:
        if self.strictness_coefficient <= 0:
            raise ValueError("strictness_coefficient must be positive")
        if not -1.0 < self.home_bias_ratio < 1.0:
            raise ValueError("home_bias_ratio must lie strictly inside (-1, 1)")
        if self.yellow_card_propensity < 0 or self.red_card_propensity < 0:
            raise ValueError("card propensities must be non-negative")


@dataclass(frozen=True)
class GammaPosterior:
    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("Gamma parameters must be positive")

    @property
    def mean(self) -> float:
        return self.alpha / self.beta

    @property
    def variance(self) -> float:
        return self.alpha / self.beta ** 2


@dataclass(frozen=True)
class LineupExpectation:
    p_start: float
    p_sub_appearance: float
    exp_minutes_start: float = 85.0
    exp_minutes_sub: float = 20.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.p_start <= 1.0 and 0.0 <= self.p_sub_appearance <= 1.0):
            raise ValueError("lineup probabilities must lie in [0, 1]")
        if self.p_start + self.p_sub_appearance > 1.0 + 1e-9:
            raise ValueError("p_start + p_sub_appearance cannot exceed 1")
        for minutes in (self.exp_minutes_start, self.exp_minutes_sub):
            if not 0.0 < minutes <= 130.0:
                raise ValueError("expected minutes must lie in (0, 130]")

    @property
    def p_no_appearance(self) -> float:
        return max(0.0, 1.0 - self.p_start - self.p_sub_appearance)


@dataclass(frozen=True)
class SideProbabilities:
    p_win: float
    p_lose: float
    p_void: float


def _validate_line(line: float) -> None:
    if line < 0 or not float(line * 2).is_integer():
        raise ValueError(f"only whole and half lines are supported, got {line}")


def _line_split(dist, line: float) -> tuple[float, float, float]:
    """(P(over), P(under), P(push)) for a count distribution."""
    _validate_line(line)
    floor_line = math.floor(line)
    if float(line).is_integer():
        p_push = float(dist.pmf(floor_line))
        p_under = float(dist.cdf(floor_line - 1))
    else:
        p_push = 0.0
        p_under = float(dist.cdf(floor_line))
    return max(0.0, 1.0 - p_under - p_push), p_under, p_push


def _zero_split(line: float) -> tuple[float, float, float]:
    _validate_line(line)
    return (0.0, 0.0, 1.0) if line == 0 else (0.0, 1.0, 0.0)


@dataclass(frozen=True)
class PropProjection:
    """Predictive distribution for one player-market-fixture; prices any whole/half line."""

    fixture_id: int
    player_id: int
    market_type: str
    gamma_shape: float
    gamma_rate: float
    lineup: LineupExpectation

    def __post_init__(self) -> None:
        for name, value in (("gamma_shape", self.gamma_shape), ("gamma_rate", self.gamma_rate)):
            if not (math.isfinite(value) and value > 0.0):
                raise ProjectionError(f"{name}={value!r} is not a finite positive number")

    @property
    def rate_per90(self) -> float:
        return self.gamma_shape / self.gamma_rate

    def count_distribution(self, minutes: float):
        if not (math.isfinite(minutes) and minutes > 0.0):
            raise ProjectionError(f"minutes={minutes!r} must be finite and positive")
        t = minutes / 90.0
        return nbinom(self.gamma_shape, self.gamma_rate / (self.gamma_rate + t))

    def side_probabilities(
        self, line: float, side: str, void_rule: VoidRule | str = VoidRule.VOID_IF_NO_APPEARANCE
    ) -> SideProbabilities:
        if side not in ("over", "under"):
            raise ValueError("side must be 'over' or 'under'")
        rule = VoidRule(void_rule)
        lu = self.lineup

        def oriented(split: tuple[float, float, float]) -> tuple[float, float]:
            over, under, _ = split
            return (over, under) if side == "over" else (under, over)

        win_s, lose_s = oriented(_line_split(self.count_distribution(lu.exp_minutes_start), line))
        win_b, lose_b = oriented(_line_split(self.count_distribution(lu.exp_minutes_sub), line))
        win_0, lose_0 = oriented(_zero_split(line))

        if rule is VoidRule.VOID_IF_NO_APPEARANCE:
            p_win = lu.p_start * win_s + lu.p_sub_appearance * win_b
            p_lose = lu.p_start * lose_s + lu.p_sub_appearance * lose_b
        elif rule is VoidRule.VOID_IF_NOT_STARTING:
            p_win = lu.p_start * win_s
            p_lose = lu.p_start * lose_s
        else:
            p_win = lu.p_start * win_s + lu.p_sub_appearance * win_b + lu.p_no_appearance * win_0
            p_lose = lu.p_start * lose_s + lu.p_sub_appearance * lose_b + lu.p_no_appearance * lose_0
        return SideProbabilities(p_win=p_win, p_lose=p_lose, p_void=max(0.0, 1.0 - p_win - p_lose))


def _log_term(lam: float, n: int) -> float:
    if n == 0:
        return 0.0
    if lam == 0.0:
        return -math.inf
    return n * math.log(lam) - math.lgamma(n + 1)


class QuantEngine:
    def __init__(self, beta1: float = 1.0, beta2: float = 1.0, game_state_k: float | None = None):
        if game_state_k is not None and not (math.isfinite(game_state_k) and game_state_k > 0):
            raise ValueError("game_state_k must be finite and positive, or None")
        self.beta1 = beta1
        self.beta2 = beta2
        self.game_state_k = game_state_k

    # ---------------- bivariate Poisson (same-match dependence) ----------------
    @staticmethod
    def bivariate_poisson_pmf(x: int, y: int, lambda1: float, lambda2: float, lambda0: float) -> float:
        if x < 0 or y < 0:
            return 0.0
        if min(lambda1, lambda2, lambda0) < 0:
            raise ValueError("bivariate Poisson rates must be non-negative")
        base = -(lambda1 + lambda2 + lambda0)
        logs = [
            base + _log_term(lambda1, x - k) + _log_term(lambda2, y - k) + _log_term(lambda0, k)
            for k in range(min(x, y) + 1)
        ]
        finite = [v for v in logs if v != -math.inf]
        if not finite:
            return 0.0
        peak = max(finite)
        return math.exp(peak) * sum(math.exp(v - peak) for v in finite)

    @staticmethod
    def bivariate_from_moments(mean_x: float, mean_y: float, covariance: float) -> tuple[float, float, float]:
        if not 0.0 <= covariance < min(mean_x, mean_y):
            raise ValueError("covariance must satisfy 0 <= cov < min(mean_x, mean_y)")
        return mean_x - covariance, mean_y - covariance, covariance

    @staticmethod
    def bivariate_correlation(lambda1: float, lambda2: float, lambda0: float) -> float:
        return lambda0 / math.sqrt((lambda1 + lambda0) * (lambda2 + lambda0))

    @staticmethod
    def sample_bivariate_poisson(
        lambda1: float, lambda2: float, lambda0: float, n_samples: int, rng: np.random.Generator
    ) -> tuple[np.ndarray, np.ndarray]:
        w = rng.poisson(lambda0, size=n_samples)
        return w + rng.poisson(lambda1, size=n_samples), w + rng.poisson(lambda2, size=n_samples)

    def joint_over_probability(
        self, line_x: float, line_y: float, lambda1: float, lambda2: float, lambda0: float
    ) -> float:
        """P(X > line_x and Y > line_y) for half lines."""
        for line in (line_x, line_y):
            _validate_line(line)
            if float(line).is_integer():
                raise ValueError("joint_over_probability expects half lines")
        a, b = math.floor(line_x), math.floor(line_y)
        cdf_joint = sum(
            self.bivariate_poisson_pmf(x, y, lambda1, lambda2, lambda0)
            for x in range(a + 1) for y in range(b + 1)
        )
        cdf_x = float(poisson.cdf(a, lambda1 + lambda0))
        cdf_y = float(poisson.cdf(b, lambda2 + lambda0))
        return max(0.0, 1.0 - cdf_x - cdf_y + cdf_joint)

    # ---------------- referee adjustment ----------------
    def referee_multiplier(self, referee: RefereeProfile, is_home: bool) -> float:
        venue = (1.0 - referee.home_bias_ratio) if is_home else (1.0 + referee.home_bias_ratio)
        return referee.strictness_coefficient ** self.beta1 * venue ** self.beta2

    # ---------------- Bayesian updating ----------------
    @staticmethod
    def positional_prior(league_rate_per90: float, prior_strength_90s: float = 6.0) -> GammaPosterior:
        if league_rate_per90 <= 0 or prior_strength_90s <= 0:
            raise ValueError("league rate and prior strength must be positive")
        return GammaPosterior(alpha=prior_strength_90s * league_rate_per90, beta=prior_strength_90s)

    @staticmethod
    def gamma_poisson_update(
        prior: GammaPosterior, counts: Sequence[int], minutes: Sequence[float]
    ) -> GammaPosterior:
        if len(counts) != len(minutes):
            raise ValueError("counts and minutes must have equal length")
        if any(m < 0 for m in minutes) or any(c < 0 for c in counts):
            raise ValueError("counts and minutes must be non-negative")
        exposure = sum(m / 90.0 for m in minutes)
        return GammaPosterior(alpha=prior.alpha + sum(counts), beta=prior.beta + exposure)

    def predictive_gamma(self, posterior: GammaPosterior, multiplier: float) -> tuple[float, float]:
        """
        Moment-matched Gamma (shape, rate) for the per-90 match rate c * Lambda * G.

        Uses the exact relative form Var/mu^2 = 1/alpha + 1/k + 1/(alpha*k), which is a sum of
        non-negative terms. The subtraction form E[X^2] - mu^2 cancels catastrophically for large
        alpha and can return zero or negative variance in floating point.
        """
        if not (math.isfinite(multiplier) and multiplier > 0.0):
            raise ProjectionError(f"referee/card multiplier={multiplier!r} must be finite and positive")
        if not (math.isfinite(posterior.alpha) and math.isfinite(posterior.beta)):
            raise ProjectionError("posterior parameters must be finite")
        mu = multiplier * posterior.mean
        if not (math.isfinite(mu) and mu > 0.0):
            raise ProjectionError(f"projected rate mu={mu!r} must be finite and positive")

        inv_alpha = 1.0 / posterior.alpha
        inv_k = 0.0 if self.game_state_k is None else 1.0 / self.game_state_k
        relative_variance = inv_alpha + inv_k + inv_alpha * inv_k
        relative_variance = min(max(relative_variance, MIN_RELATIVE_VARIANCE), MAX_RELATIVE_VARIANCE)

        shape = 1.0 / relative_variance
        rate = shape / mu
        if not (math.isfinite(rate) and rate > 0.0):
            raise ProjectionError(f"derived gamma rate={rate!r} is invalid")
        return shape, rate

    # ---------------- projections ----------------
    def project_count_market(
        self,
        *,
        fixture_id: int,
        player_id: int,
        market_type: str,
        counts: Sequence[int],
        minutes: Sequence[float],
        league_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        lineup: LineupExpectation,
        prior_strength_90s: float = 6.0,
    ) -> PropProjection:
        posterior = self.gamma_poisson_update(
            self.positional_prior(league_rate_per90, prior_strength_90s), counts, minutes
        )
        multiplier = self.referee_multiplier(referee, is_home) if market_type in REFEREE_SENSITIVE_MARKETS else 1.0
        shape, rate = self.predictive_gamma(posterior, multiplier)
        return PropProjection(fixture_id, player_id, market_type, shape, rate, lineup)

    def project_card_market(
        self,
        *,
        fixture_id: int,
        player_id: int,
        foul_counts: Sequence[int],
        minutes: Sequence[float],
        league_foul_rate_per90: float,
        referee: RefereeProfile,
        is_home: bool,
        lineup: LineupExpectation,
        card_type: str = "yellow",
        player_card_factor: float = 1.0,
        prior_strength_90s: float = 6.0,
    ) -> PropProjection:
        if card_type not in ("yellow", "red"):
            raise ValueError("card_type must be 'yellow' or 'red'")
        if player_card_factor <= 0:
            raise ValueError("player_card_factor must be positive")
        posterior = self.gamma_poisson_update(
            self.positional_prior(league_foul_rate_per90, prior_strength_90s), foul_counts, minutes
        )
        propensity = referee.yellow_card_propensity if card_type == "yellow" else referee.red_card_propensity
        if propensity <= 0:
            raise ValueError("referee card propensity must be positive to price a card market")
        multiplier = self.referee_multiplier(referee, is_home) * propensity * player_card_factor
        shape, rate = self.predictive_gamma(posterior, multiplier)
        return PropProjection(fixture_id, player_id, f"{card_type}_card", shape, rate, lineup)

    @staticmethod
    def player_card_factor(
        player_cards: int, player_fouls: int, league_cards_per_foul: float, prior_fouls: float = 40.0
    ) -> float:
        if league_cards_per_foul <= 0:
            raise ValueError("league_cards_per_foul must be positive")
        shrunk = (player_cards + prior_fouls * league_cards_per_foul) / (player_fouls + prior_fouls)
        return shrunk / league_cards_per_foul
```

## TASK 2 — `pipeline.py` (prop_alpha/ingestion/pipeline.py)

```python
# file: prop_alpha/ingestion/pipeline.py
"""Concurrent vendor -> PostgreSQL ingestion.

Workers fetch and transform fixtures in parallel, holding no database connection during network I/O,
then append to the UNLOGGED staging table in a short per-fixture transaction. A single serialized
call to prop_alpha.merge_staging_player_feed() moves staged rows into the core tables. Confirmed team
sheets are written directly to lineup_expectations (one small, disjoint row set per fixture).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, TypeVar

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from prop_alpha.ingestion.transform import (
    DEFAULT_KAPPAS,
    STAGING_COLUMNS,
    build_staging_rows,
    parse_fixture_payload,
    parse_lineup_payload,
)

logger = logging.getLogger("prop_alpha.ingestion")

T = TypeVar("T")

# Deadlocks, serialization failures, lock/statement timeouts and dropped connections are all
# OperationalError/InterfaceError subclasses in psycopg2 and are safe to retry: the whole
# per-fixture transaction is rolled back before the retry.
RETRYABLE_DB_ERRORS: tuple[type[BaseException], ...] = (psycopg2.OperationalError, psycopg2.InterfaceError)

INSERT_RAW_SQL = """
    INSERT INTO prop_alpha.raw_payloads (source, vendor_fixture_uid, payload, payload_sha256)
    VALUES (%s, %s, %s::jsonb, %s)
    ON CONFLICT (payload_sha256) DO NOTHING
"""

INSERT_STAGING_SQL = f"INSERT INTO prop_alpha.staging_player_feed ({', '.join(STAGING_COLUMNS)}) VALUES %s"

MERGE_SQL = "SELECT fixtures_merged, player_rows_merged, rows_rejected FROM prop_alpha.merge_staging_player_feed()"

LINEUP_UPSERT_SQL = """
    INSERT INTO prop_alpha.lineup_expectations AS le
        (fixture_id, player_id, p_start, p_sub_appearance, exp_minutes_start, exp_minutes_sub, source, updated_at)
    SELECT f.fixture_id, p.player_id, v.p_start, v.p_sub,
           COALESCE(h.avg_start, 85.0), COALESCE(h.avg_sub, 20.0), 'confirmed_team_sheet', now()
    FROM (VALUES %s) AS v(vendor_fixture_uid, vendor_player_uid, p_start, p_sub)
    JOIN prop_alpha.fixtures f ON f.vendor_fixture_uid = v.vendor_fixture_uid
    JOIN prop_alpha.players  p ON p.vendor_player_uid  = v.vendor_player_uid
    LEFT JOIN LATERAL (
        SELECT avg(s.minutes_played) FILTER (WHERE s.started)     AS avg_start,
               avg(s.minutes_played) FILTER (WHERE NOT s.started) AS avg_sub
        FROM prop_alpha.player_match_stats s
        WHERE s.player_id = p.player_id AND s.minutes_played > 0
    ) h ON TRUE
    ORDER BY p.player_id
    ON CONFLICT (fixture_id, player_id) DO UPDATE SET
        p_start           = EXCLUDED.p_start,
        p_sub_appearance  = EXCLUDED.p_sub_appearance,
        exp_minutes_start = EXCLUDED.exp_minutes_start,
        exp_minutes_sub   = EXCLUDED.exp_minutes_sub,
        source            = EXCLUDED.source,
        updated_at        = EXCLUDED.updated_at
    RETURNING player_id
"""
LINEUP_TEMPLATE = "(%s, %s, %s::numeric, %s::numeric)"

LINEUP_EXCLUDE_SQL = """
    UPDATE prop_alpha.lineup_expectations AS le
    SET p_start = 0, p_sub_appearance = 0, source = 'confirmed_team_sheet', updated_at = now()
    FROM prop_alpha.fixtures f
    WHERE f.fixture_id = le.fixture_id
      AND f.vendor_fixture_uid = %s
      AND NOT (le.player_id = ANY(%s::int[]))
"""


class TokenBucketRateLimiter:
    """Thread-safe token bucket honouring a vendor's requests-per-second contract across all workers."""

    def __init__(self, rate_per_sec: float, burst: int):
        if rate_per_sec <= 0 or burst < 1:
            raise ValueError("rate_per_sec must be > 0 and burst >= 1")
        self._rate = rate_per_sec
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._lock = threading.Lock()
        self._last_refill = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last_refill) * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
            time.sleep(wait_s)


class VendorClient:
    """HTTP client with one requests.Session per worker thread (Session is not documented as thread-safe)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_limiter: TokenBucketRateLimiter,
        timeout: float = 5.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})
            retry_policy = Retry(
                total=self.max_retries,
                backoff_factor=0.25,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry_policy, pool_maxsize=2)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def get_json(self, path: str) -> dict[str, Any]:
        self.rate_limiter.acquire()
        resp = self._session().get(f"{self.base_url}/{path.lstrip('/')}", timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"expected a JSON object from {path}")
        return body

    def get_fixture_feed(self, fixture_vendor_uid: str) -> dict[str, Any]:
        return self.get_json(f"fixtures/{fixture_vendor_uid}/stats")

    def get_lineups(self, fixture_vendor_uid: str) -> dict[str, Any]:
        return self.get_json(f"fixtures/{fixture_vendor_uid}/lineups")

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()


@dataclass(frozen=True)
class FixtureResult:
    fixture_uid: str
    ok: bool
    rows: int
    db_attempts: int
    elapsed_s: float
    error: str | None = None


@dataclass(frozen=True)
class MergeResult:
    fixtures_merged: int
    player_rows_merged: int
    rows_rejected: int


@dataclass
class SlateReport:
    job: str
    results: dict[str, FixtureResult] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)
    merge: MergeResult | None = None
    merge_error: str | None = None
    elapsed_s: float = 0.0

    @property
    def succeeded(self) -> list[str]:
        return sorted(uid for uid, r in self.results.items() if r.ok)

    @property
    def failed(self) -> dict[str, str]:
        return {uid: r.error or "unknown error" for uid, r in self.results.items() if not r.ok}

    @property
    def fully_successful(self) -> bool:
        return not self.failed and not self.timed_out and self.merge_error is None


class ConcurrentIngestionEngine:
    """Processes a slate of fixtures in parallel with per-fixture transactional isolation."""

    JOBS = ("stats", "lineups")

    def __init__(
        self,
        client: VendorClient,
        pool: Any,
        kappas: Mapping[str, float] | None = None,
        max_workers: int = 8,
        max_db_attempts: int = 3,
        retry_base_delay_s: float = 0.05,
        bench_sub_probability: float = 0.35,
    ):
        if max_workers < 1 or max_db_attempts < 1:
            raise ValueError("max_workers and max_db_attempts must be >= 1")
        pool_size = getattr(pool, "maxconn", None)
        if pool_size is not None and pool_size < max_workers + 1:
            raise ValueError("connection pool maxconn must be >= max_workers + 1 (one spare for the merge)")
        self.client = client
        self.pool = pool
        self.kappas = dict(kappas or DEFAULT_KAPPAS)
        self.max_workers = max_workers
        self.max_db_attempts = max_db_attempts
        self.retry_base_delay_s = retry_base_delay_s
        self.bench_sub_probability = bench_sub_probability
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ingest")
        self._closed = False

    def __enter__(self) -> "ConcurrentIngestionEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._closed = True

    # ---------------- slate orchestration ----------------
    def run_slate(
        self, fixture_uids: Iterable[str], job: str = "stats", deadline_s: float | None = None, merge: bool = True
    ) -> SlateReport:
        """
        Run one job for every fixture concurrently. Fixtures not finished by `deadline_s` are reported
        as timed_out; a task already running keeps going and its transaction commits or rolls back on
        its own, so a timed-out fixture may still land and be picked up by the next merge.
        """
        if self._closed:
            raise RuntimeError("engine is closed")
        if job not in self.JOBS:
            raise ValueError(f"job must be one of {self.JOBS}")
        task: Callable[[str], tuple[int, int]] = self._stats_task if job == "stats" else self._lineup_task
        uids = list(dict.fromkeys(u for u in fixture_uids if u))
        report = SlateReport(job=job)
        started = time.monotonic()

        futures: dict[Future[FixtureResult], str] = {
            self._executor.submit(self._run_isolated, task, uid): uid for uid in uids
        }
        done, not_done = wait(futures, timeout=deadline_s)
        for fut in done:
            result = fut.result()
            report.results[result.fixture_uid] = result
        for fut in not_done:
            fut.cancel()
        report.timed_out = sorted(futures[f] for f in not_done)

        if job == "stats" and merge and report.succeeded:
            try:
                report.merge = self.merge_staging()
            except Exception as exc:
                report.merge_error = f"{type(exc).__name__}: {exc}"
                logger.exception("staging merge failed; rows remain in staging for the next merge")

        report.elapsed_s = time.monotonic() - started
        logger.info(
            "slate %s: %d ok, %d failed, %d timed out, merge=%s in %.2fs",
            job, len(report.succeeded), len(report.failed), len(report.timed_out), report.merge, report.elapsed_s,
        )
        return report

    def merge_staging(self) -> MergeResult:
        def merge(cur) -> MergeResult:
            cur.execute(MERGE_SQL)
            fixtures, rows, rejected = cur.fetchone()
            return MergeResult(int(fixtures), int(rows), int(rejected))

        result, _ = self._with_transaction(merge)
        if result.rows_rejected:
            logger.warning("merge rejected %d staging rows; see prop_alpha.staging_rejects", result.rows_rejected)
        return result

    # ---------------- per-fixture tasks ----------------
    def _run_isolated(self, task: Callable[[str], tuple[int, int]], uid: str) -> FixtureResult:
        started = time.monotonic()
        try:
            rows, attempts = task(uid)
            return FixtureResult(uid, True, rows, attempts, time.monotonic() - started)
        except Exception as exc:
            logger.error("fixture %s failed: %s: %s", uid, type(exc).__name__, exc)
            return FixtureResult(uid, False, 0, 0, time.monotonic() - started, f"{type(exc).__name__}: {exc}")

    def _stats_task(self, uid: str) -> tuple[int, int]:
        payload = self.client.get_fixture_feed(uid)
        parsed = parse_fixture_payload(payload)
        if parsed.fixture.vendor_uid != uid:
            raise ValueError(f"payload uid {parsed.fixture.vendor_uid} does not match request {uid}")
        rows = build_staging_rows(parsed, self.kappas, str(uuid.uuid4()))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

        def write(cur) -> int:
            cur.execute(INSERT_RAW_SQL, ("vendor_fixture_stats", uid, body, digest))
            psycopg2.extras.execute_values(cur, INSERT_STAGING_SQL, rows, page_size=1000)
            return len(rows) - 1

        return self._with_transaction(write)

    def _lineup_task(self, uid: str) -> tuple[int, int]:
        payload = self.client.get_lineups(uid)
        entries = parse_lineup_payload(payload, self.bench_sub_probability)
        if any(e.vendor_fixture_uid != uid for e in entries):
            raise ValueError(f"lineup payload does not match request {uid}")
        values = [(e.vendor_fixture_uid, e.vendor_player_uid, e.p_start, e.p_sub_appearance) for e in entries]

        def write(cur) -> int:
            returned = psycopg2.extras.execute_values(
                cur, LINEUP_UPSERT_SQL, values, template=LINEUP_TEMPLATE, page_size=100, fetch=True
            )
            listed_ids = [int(r[0]) for r in returned]
            if len(listed_ids) < len(values):
                logger.warning(
                    "fixture %s: %d of %d listed players unknown or fixture missing; seed them first",
                    uid, len(values) - len(listed_ids), len(values),
                )
            if not listed_ids:
                raise ValueError(f"fixture {uid}: no lineup rows matched existing fixture/players")
            cur.execute(LINEUP_EXCLUDE_SQL, (uid, listed_ids))
            return len(listed_ids)

        return self._with_transaction(write)

    # ---------------- transaction boundary ----------------
    def _with_transaction(self, fn: Callable[[Any], T]) -> tuple[T, int]:
        """Run fn(cursor) in its own pooled connection and transaction; retry transient failures."""
        last_error: BaseException | None = None
        for attempt in range(1, self.max_db_attempts + 1):
            conn = self.pool.getconn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        return fn(cur), attempt
            except RETRYABLE_DB_ERRORS as exc:
                last_error = exc
                logger.warning("transient database error (attempt %d/%d): %s", attempt, self.max_db_attempts, exc)
            finally:
                self.pool.putconn(conn, close=bool(conn.closed))
            if attempt < self.max_db_attempts:
                time.sleep(self.retry_base_delay_s * (2 ** (attempt - 1)) * (1.0 + random.random()))
        assert last_error is not None
        raise last_error


def build_pool(dsn: str, max_workers: int) -> psycopg2.pool.ThreadedConnectionPool:
    """Pool sized for all workers plus the merge; timeouts bound worst-case latency during lock contention."""
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=max_workers + 1,
        dsn=dsn,
        options="-c statement_timeout=10000 -c lock_timeout=2000",
    )


def load_kappas(raw: str | None) -> dict[str, float]:
    kappas = dict(DEFAULT_KAPPAS)
    if raw:
        overrides = json.loads(raw)
        unknown = set(overrides) - set(kappas)
        if unknown:
            raise ValueError(f"unknown kappa keys: {sorted(unknown)}")
        for key, value in overrides.items():
            if not 0.0 <= float(value) <= 2.0:
                raise ValueError(f"kappa {key}={value} outside [0, 2]")
            kappas[key] = float(value)
    return kappas


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    parser = argparse.ArgumentParser(description="Concurrent slate ingestion.")
    parser.add_argument("job", choices=ConcurrentIngestionEngine.JOBS)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--deadline", type=float, default=None, help="seconds to wait for the slate")
    parser.add_argument("--bench-sub-probability", type=float, default=0.35)
    args = parser.parse_args()

    fixture_uids = [u.strip() for u in os.environ.get("TARGET_FIXTURE_UIDS", "").split(",") if u.strip()]
    client = VendorClient(
        os.environ["DATA_VENDOR_BASE_URL"],
        os.environ["DATA_VENDOR_API_KEY"],
        TokenBucketRateLimiter(rate_per_sec=float(os.environ.get("DATA_VENDOR_RPS", "5")), burst=10),
    )
    pool = build_pool(os.environ["PROP_ALPHA_DB_DSN"], args.workers)
    try:
        with ConcurrentIngestionEngine(
            client, pool, load_kappas(os.environ.get("PROP_ALPHA_KAPPAS")),
            max_workers=args.workers, bench_sub_probability=args.bench_sub_probability,
        ) as engine:
            report = engine.run_slate(fixture_uids, job=args.job, deadline_s=args.deadline)
    finally:
        client.close()
        pool.closeall()
    for uid, error in report.failed.items():
        logger.error("FAILED %s: %s", uid, error)
    for uid in report.timed_out:
        logger.error("TIMED OUT %s", uid)
    return 0 if report.fully_successful else 1


if __name__ == "__main__":
    sys.exit(main())
```

## TASK 2 (supporting) — `transform.py` (prop_alpha/ingestion/transform.py): validation, staging rows, lineup parsing

```python
# file: prop_alpha/ingestion/transform.py
"""Pure parsing and feature transforms for vendor fixture payloads (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

VALID_STATUSES = frozenset({"scheduled", "live", "final", "postponed"})

# Standard linear possession adjustment until fitted values (Section 6.1) are loaded.
DEFAULT_KAPPAS: dict[str, float] = {"tackles": 1.0, "interceptions": 1.0, "fouls": 1.0}

STATS_COLUMNS: tuple[str, ...] = (
    "fixture_id", "player_id", "team_id", "is_home", "started",
    "minute_on", "minute_off", "minutes_played",
    "match_state_leading_min", "match_state_level_min", "match_state_trailing_min",
    "tackles_attempted", "tackles_won", "interceptions", "dribbled_past",
    "duels_total", "duels_won",
    "fouls_committed", "fouls_drawn", "yellow_cards", "red_cards",
    "zone14_entries", "halfspace_entries", "long_balls_targeted",
    "team_possession_pct",
    "padj_tackles", "padj_interceptions", "padj_fouls", "dribbled_past_rate",
)


def padj_scale(raw_count: float, team_possession_pct: float | None, kappa: float) -> float:
    """raw * (50 / opponent_possession)^kappa; deflates actions made while the opponent dominates the ball."""
    if team_possession_pct is None:
        return float(raw_count)
    opponent_possession = 100.0 - min(max(float(team_possession_pct), 1.0), 99.0)
    return float(raw_count) * (50.0 / opponent_possession) ** kappa


def dribbled_past_rate(dribbled_past: int, tackles_attempted: int) -> float | None:
    denominator = dribbled_past + tackles_attempted
    if denominator == 0:
        return None
    return dribbled_past / denominator


@dataclass(frozen=True)
class GoalEvent:
    minute: int
    team_uid: str


def match_state_minutes(
    goals: Sequence[GoalEvent], team_uid: str, minute_on: int, minute_off: int
) -> tuple[int, int, int]:
    """Minutes spent (leading, level, trailing) by `team_uid` while the player was on the pitch."""
    if minute_off <= minute_on:
        return 0, 0, 0
    cuts = sorted({g.minute for g in goals if minute_on < g.minute < minute_off})
    edges = [minute_on, *cuts, minute_off]
    leading = level = trailing = 0
    for start, end in zip(edges[:-1], edges[1:]):
        diff = sum(1 if g.team_uid == team_uid else -1 for g in goals if g.minute <= start)
        span = end - start
        if diff > 0:
            leading += span
        elif diff < 0:
            trailing += span
        else:
            level += span
    return leading, level, trailing


@dataclass(frozen=True)
class FixtureRecord:
    vendor_uid: str
    home_team_uid: str
    away_team_uid: str
    kickoff_at: str
    status: str
    home_score: int | None
    away_score: int | None
    matchday: int | None


@dataclass(frozen=True)
class RefereeRecord:
    vendor_uid: str
    name: str


@dataclass(frozen=True)
class PlayerMatchRecord:
    player_uid: str
    name: str
    position: str
    team_uid: str
    is_home: bool
    started: bool
    minute_on: int
    minute_off: int
    minutes_played: int
    state_leading_min: int
    state_level_min: int
    state_trailing_min: int
    tackles_attempted: int
    tackles_won: int
    interceptions: int
    dribbled_past: int
    duels_total: int
    duels_won: int
    fouls_committed: int
    fouls_drawn: int
    yellow_cards: int
    red_cards: int
    zone14_entries: int
    halfspace_entries: int
    long_balls_targeted: int
    team_possession_pct: float | None

    def __post_init__(self) -> None:
        counts = (
            self.minute_on, self.minute_off, self.minutes_played, self.tackles_attempted, self.tackles_won,
            self.interceptions, self.dribbled_past, self.duels_total, self.duels_won, self.fouls_committed,
            self.fouls_drawn, self.zone14_entries, self.halfspace_entries, self.long_balls_targeted,
        )
        if any(c < 0 for c in counts):
            raise ValueError(f"player {self.player_uid}: negative count in vendor stats")
        if self.tackles_won > self.tackles_attempted:
            raise ValueError(f"player {self.player_uid}: tackles_won exceeds tackles_attempted")
        if self.duels_won > self.duels_total:
            raise ValueError(f"player {self.player_uid}: duels_won exceeds duels_total")
        if not 0 <= self.yellow_cards <= 2 or not 0 <= self.red_cards <= 1:
            raise ValueError(f"player {self.player_uid}: card counts out of range")
        if self.team_possession_pct is not None and not 0.0 <= self.team_possession_pct <= 100.0:
            raise ValueError(f"player {self.player_uid}: possession out of range")

    def derived_features(self, kappas: Mapping[str, float]) -> tuple[float, float, float, float | None]:
        return (
            padj_scale(self.tackles_attempted, self.team_possession_pct, kappas["tackles"]),
            padj_scale(self.interceptions, self.team_possession_pct, kappas["interceptions"]),
            padj_scale(self.fouls_committed, self.team_possession_pct, kappas["fouls"]),
            dribbled_past_rate(self.dribbled_past, self.tackles_attempted),
        )


@dataclass(frozen=True)
class ParsedFixture:
    fixture: FixtureRecord
    referee: RefereeRecord | None
    players: tuple[PlayerMatchRecord, ...]


def _count(stats: Mapping[str, Any], key: str) -> int:
    value = stats.get(key)
    return int(value) if value is not None else 0


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def parse_fixture_payload(payload: Mapping[str, Any], match_length: int = 90) -> ParsedFixture:
    """
    Expected shape:
    {
      "fixture": {"uid", "home_team_uid", "away_team_uid", "kickoff_at", "status",
                  "home_score"?, "away_score"?, "matchday"?},
      "referee": {"uid", "name"} | null,
      "teams": {"<team_uid>": {"possession_pct": 47.3}},
      "goals": [{"minute": 23, "team_uid": "..."}],          # team_uid = side credited with the goal
      "players": [{"uid", "name", "position", "team_uid", "started",
                   "minute_on"?, "minute_off"?, "match_state_minutes"?: {"leading","level","trailing"},
                   "stats": {"minutes", "tackles_attempted" | "tackles", "tackles_won", "interceptions",
                             "dribbled_past", "duels_total", "duels_won", "fouls_committed",
                             "fouls_drawn", "yellow_cards", "red_cards", "zone14_entries",
                             "halfspace_entries", "long_balls_targeted"}}]
    }
    """
    fx = payload["fixture"]
    status = str(fx["status"]).lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown fixture status: {status!r}")

    fixture = FixtureRecord(
        vendor_uid=str(fx["uid"]),
        home_team_uid=str(fx["home_team_uid"]),
        away_team_uid=str(fx["away_team_uid"]),
        kickoff_at=str(fx["kickoff_at"]),
        status=status,
        home_score=_optional_int(fx.get("home_score")),
        away_score=_optional_int(fx.get("away_score")),
        matchday=_optional_int(fx.get("matchday")),
    )
    if fixture.home_team_uid == fixture.away_team_uid:
        raise ValueError("home and away team uids are identical")

    referee_raw = payload.get("referee")
    referee = RefereeRecord(str(referee_raw["uid"]), str(referee_raw["name"])) if referee_raw else None

    goals = [GoalEvent(int(g["minute"]), str(g["team_uid"])) for g in payload.get("goals", [])]
    possession = {
        str(uid): float(team["possession_pct"])
        for uid, team in payload.get("teams", {}).items()
        if team.get("possession_pct") is not None
    }

    seen: set[str] = set()
    players: list[PlayerMatchRecord] = []
    for p in payload.get("players", []):
        uid = str(p["uid"])
        if uid in seen:
            raise ValueError(f"duplicate player uid in payload: {uid}")
        seen.add(uid)

        team_uid = str(p["team_uid"])
        if team_uid not in (fixture.home_team_uid, fixture.away_team_uid):
            raise ValueError(f"player {uid} belongs to neither side: {team_uid}")

        stats = p.get("stats", {})
        minutes = _count(stats, "minutes")
        started = bool(p.get("started", False))
        default_on = 0 if started else max(match_length - minutes, 0)
        minute_on = int(p.get("minute_on", default_on))
        minute_off = int(p.get("minute_off", minute_on + minutes))

        explicit_states = p.get("match_state_minutes")
        if explicit_states:
            states = (
                int(explicit_states["leading"]),
                int(explicit_states["level"]),
                int(explicit_states["trailing"]),
            )
        elif minutes > 0:
            states = match_state_minutes(goals, team_uid, minute_on, minute_off)
        else:
            states = (0, 0, 0)

        tackles_attempted = _count(stats, "tackles_attempted") if "tackles_attempted" in stats else _count(stats, "tackles")

        players.append(PlayerMatchRecord(
            player_uid=uid,
            name=str(p["name"]),
            position=str(p["position"]),
            team_uid=team_uid,
            is_home=team_uid == fixture.home_team_uid,
            started=started,
            minute_on=minute_on,
            minute_off=minute_off,
            minutes_played=minutes,
            state_leading_min=states[0],
            state_level_min=states[1],
            state_trailing_min=states[2],
            tackles_attempted=tackles_attempted,
            tackles_won=_count(stats, "tackles_won"),
            interceptions=_count(stats, "interceptions"),
            dribbled_past=_count(stats, "dribbled_past"),
            duels_total=_count(stats, "duels_total"),
            duels_won=_count(stats, "duels_won"),
            fouls_committed=_count(stats, "fouls_committed"),
            fouls_drawn=_count(stats, "fouls_drawn"),
            yellow_cards=_count(stats, "yellow_cards"),
            red_cards=_count(stats, "red_cards"),
            zone14_entries=_count(stats, "zone14_entries"),
            halfspace_entries=_count(stats, "halfspace_entries"),
            long_balls_targeted=_count(stats, "long_balls_targeted"),
            team_possession_pct=possession.get(team_uid),
        ))

    return ParsedFixture(fixture=fixture, referee=referee, players=tuple(players))


def build_stats_row(
    record: PlayerMatchRecord, fixture_id: int, player_id: int, team_id: int, kappas: Mapping[str, float]
) -> tuple:
    """Row ordered exactly as STATS_COLUMNS."""
    padj_tackles, padj_interceptions, padj_fouls, dpr = record.derived_features(kappas)
    return (
        fixture_id, player_id, team_id, record.is_home, record.started,
        record.minute_on, record.minute_off, record.minutes_played,
        record.state_leading_min, record.state_level_min, record.state_trailing_min,
        record.tackles_attempted, record.tackles_won, record.interceptions, record.dribbled_past,
        record.duels_total, record.duels_won,
        record.fouls_committed, record.fouls_drawn, record.yellow_cards, record.red_cards,
        record.zone14_entries, record.halfspace_entries, record.long_balls_targeted,
        record.team_possession_pct,
        padj_tackles, padj_interceptions, padj_fouls, dpr,
    )


# ---------------- staging rows (append-only landing table) ----------------
STAGING_COLUMNS: tuple[str, ...] = (
    "batch_id", "vendor_fixture_uid", "home_team_uid", "away_team_uid", "kickoff_at", "fixture_status",
    "home_score", "away_score", "matchday", "referee_uid", "referee_name",
    "vendor_player_uid", "player_name", "player_position", "team_uid", "is_home", "started",
    "minute_on", "minute_off", "minutes_played",
    "match_state_leading_min", "match_state_level_min", "match_state_trailing_min",
    "tackles_attempted", "tackles_won", "interceptions", "dribbled_past", "duels_total", "duels_won",
    "fouls_committed", "fouls_drawn", "yellow_cards", "red_cards",
    "zone14_entries", "halfspace_entries", "long_balls_targeted", "team_possession_pct",
    "padj_tackles", "padj_interceptions", "padj_fouls", "dribbled_past_rate",
)
_FIXTURE_PREFIX_WIDTH = 11
_PLAYER_WIDTH = len(STAGING_COLUMNS) - _FIXTURE_PREFIX_WIDTH


def build_staging_rows(parsed: ParsedFixture, kappas: Mapping[str, float], batch_id: str) -> list[tuple]:
    """One fixture header row (player columns NULL) plus one row per player, ordered as STAGING_COLUMNS."""
    fx = parsed.fixture
    prefix = (
        batch_id, fx.vendor_uid, fx.home_team_uid, fx.away_team_uid, fx.kickoff_at, fx.status,
        fx.home_score, fx.away_score, fx.matchday,
        parsed.referee.vendor_uid if parsed.referee else None,
        parsed.referee.name if parsed.referee else None,
    )
    rows: list[tuple] = [prefix + (None,) * _PLAYER_WIDTH]
    for rec in parsed.players:
        padj_tackles, padj_interceptions, padj_fouls, dpr = rec.derived_features(kappas)
        rows.append(prefix + (
            rec.player_uid, rec.name, rec.position, rec.team_uid, rec.is_home, rec.started,
            rec.minute_on, rec.minute_off, rec.minutes_played,
            rec.state_leading_min, rec.state_level_min, rec.state_trailing_min,
            rec.tackles_attempted, rec.tackles_won, rec.interceptions, rec.dribbled_past,
            rec.duels_total, rec.duels_won,
            rec.fouls_committed, rec.fouls_drawn, rec.yellow_cards, rec.red_cards,
            rec.zone14_entries, rec.halfspace_entries, rec.long_balls_targeted, rec.team_possession_pct,
            padj_tackles, padj_interceptions, padj_fouls, dpr,
        ))
    return rows


# ---------------- confirmed team sheets ----------------
@dataclass(frozen=True)
class LineupEntry:
    vendor_fixture_uid: str
    vendor_player_uid: str
    p_start: float
    p_sub_appearance: float


def parse_lineup_payload(payload: Mapping[str, Any], bench_sub_probability: float) -> list[LineupEntry]:
    """
    Expected shape:
    {"fixture": {"uid", "home_team_uid", "away_team_uid"},
     "lineups": {"<team_uid>": {"starters": [player_uid x 11], "bench": [player_uid, ...]}}}
    Both sides are required so that players absent from the sheet can be safely zeroed.
    """
    if not 0.0 <= bench_sub_probability <= 1.0:
        raise ValueError("bench_sub_probability must lie in [0, 1]")
    fx = payload["fixture"]
    fixture_uid = str(fx["uid"])
    sides = (str(fx["home_team_uid"]), str(fx["away_team_uid"]))
    lineups = payload["lineups"]
    missing = [side for side in sides if side not in lineups]
    if missing:
        raise ValueError(f"team sheet incomplete for fixture {fixture_uid}; missing sides {missing}")

    seen: set[str] = set()
    entries: list[LineupEntry] = []
    for side in sides:
        starters = [str(u) for u in lineups[side].get("starters", [])]
        bench = [str(u) for u in lineups[side].get("bench", [])]
        if len(starters) != 11:
            raise ValueError(f"fixture {fixture_uid} side {side}: expected 11 starters, got {len(starters)}")
        for uid, p_start, p_sub in [(u, 1.0, 0.0) for u in starters] + [(u, 0.0, bench_sub_probability) for u in bench]:
            if uid in seen:
                raise ValueError(f"fixture {fixture_uid}: player {uid} listed twice")
            seen.add(uid)
            entries.append(LineupEntry(fixture_uid, uid, p_start, p_sub))
    return entries
```

## TASK 3 — `schema.sql` delta as a runnable migration (migrations/002_staging_and_indexes.sql)

```sql
-- Migration 002: v2 -> v3 (staging landing zone, merge function, odds index swap).
-- Run with psql. The transactional part runs first; the index swap uses CONCURRENTLY so
-- odds_lines stays writable, and CONCURRENTLY cannot run inside a transaction block.

BEGIN;

ALTER TABLE prop_alpha.player_match_stats  SET (fillfactor = 90);
ALTER TABLE prop_alpha.lineup_expectations SET (fillfactor = 90);

-- ============================================================
-- Append-only landing zone. UNLOGGED: no WAL, so inserts are cheap, but contents are lost on a
-- crash and are not replicated to standbys. raw_payloads (logged) is the durable copy, so any
-- lost staging rows can be rebuilt by re-running the transform over raw_payloads.
-- No keys, constraints or indexes: writers never contend on anything but the heap.
-- ============================================================
CREATE UNLOGGED TABLE IF NOT EXISTS prop_alpha.staging_player_feed (
    batch_id                 UUID        NOT NULL,
    staged_at                TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    vendor_fixture_uid       VARCHAR(64) NOT NULL,
    home_team_uid            VARCHAR(64) NOT NULL,
    away_team_uid            VARCHAR(64) NOT NULL,
    kickoff_at               TIMESTAMPTZ NOT NULL,
    fixture_status           VARCHAR(16) NOT NULL,
    home_score               SMALLINT,
    away_score               SMALLINT,
    matchday                 SMALLINT,
    referee_uid              VARCHAR(64),
    referee_name             VARCHAR(128),
    vendor_player_uid        VARCHAR(64),     -- NULL on the per-fixture header row
    player_name              VARCHAR(128),
    player_position          VARCHAR(16),
    team_uid                 VARCHAR(64),
    is_home                  BOOLEAN,
    started                  BOOLEAN,
    minute_on                SMALLINT,
    minute_off               SMALLINT,
    minutes_played           SMALLINT,
    match_state_leading_min  SMALLINT,
    match_state_level_min    SMALLINT,
    match_state_trailing_min SMALLINT,
    tackles_attempted        SMALLINT,
    tackles_won              SMALLINT,
    interceptions            SMALLINT,
    dribbled_past            SMALLINT,
    duels_total              SMALLINT,
    duels_won                SMALLINT,
    fouls_committed          SMALLINT,
    fouls_drawn              SMALLINT,
    yellow_cards             SMALLINT,
    red_cards                SMALLINT,
    zone14_entries           SMALLINT,
    halfspace_entries        SMALLINT,
    long_balls_targeted      SMALLINT,
    team_possession_pct      NUMERIC(5,2),
    padj_tackles             NUMERIC(7,3),
    padj_interceptions       NUMERIC(7,3),
    padj_fouls               NUMERIC(7,3),
    dribbled_past_rate       NUMERIC(5,4)
);

CREATE TABLE IF NOT EXISTS prop_alpha.staging_rejects (
    reject_id          BIGSERIAL PRIMARY KEY,
    vendor_fixture_uid VARCHAR(64),
    vendor_player_uid  VARCHAR(64),
    reason             TEXT        NOT NULL,
    row_data           JSONB       NOT NULL,
    rejected_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- merge_staging_player_feed(): atomically drains staging into the core tables.
--   * pg_advisory_xact_lock serialises mergers; ingestion writers are never blocked.
--   * DELETE ... RETURNING claims exactly the rows committed before the statement's snapshot.
--     TRUNCATE is deliberately NOT used: it takes an ACCESS EXCLUSIVE lock (stalling every writer)
--     and a read-then-truncate sequence silently discards rows committed between the two steps.
--   * Latest row per key wins (DISTINCT ON ... staged_at DESC), so ON CONFLICT never touches a
--     row twice in one statement.
--   * Rows that would violate core constraints go to staging_rejects instead of aborting the batch.
--   * Every upsert orders by its conflict key, giving a consistent lock order.
-- Call: SELECT * FROM prop_alpha.merge_staging_player_feed();
-- ============================================================
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

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_odds_fixture_captured
    ON prop_alpha.odds_lines (fixture_id, captured_at DESC);
DROP INDEX CONCURRENTLY IF EXISTS prop_alpha.idx_odds_captured_brin;
```

## TASK 4 — `market_scanner.py` (prop_alpha/scanner/market_scanner.py)

```python
# file: prop_alpha/scanner/market_scanner.py
"""Mean-variance allocation over live player-prop quotes.

For each player-market, every fresh quote (all books, lines and sides) is priced off the stored
predictive distribution. Because all of a player's lines settle on the same count X, their returns are
jointly determined; the exact per-unit mean vector and covariance matrix are computed on the joint
(lineup scenario, count) outcome grid. Stakes then solve

    maximise  f . mu - lambda_risk * f' Sigma f
    subject to 0 <= f_j <= min(full Kelly_j, max_position_fraction),  sum_j f_j <= max_position_fraction

which is concave (Sigma is PSD), so the optimum is global. Fixture- and slate-level caps are applied
afterwards by proportional scaling.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Hashable, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize

from prop_alpha.modeling.engine import (
    LineupExpectation,
    ProjectionError,
    PropProjection,
    QuantEngine,
    RefereeProfile,
    SideProbabilities,
    VoidRule,
)

logger = logging.getLogger("prop_alpha.scanner")

ProjectionKey = tuple[int, int, str]


@dataclass(frozen=True)
class LiveOddsQuote:
    fixture_id: int
    player_id: int
    market_type: str
    line_value: float
    over_price: float | None
    under_price: float | None
    max_stake: float
    sportsbook: str
    captured_at: datetime
    void_rule: VoidRule = VoidRule.VOID_IF_NO_APPEARANCE


@dataclass(frozen=True)
class StakingDirective:
    fixture_id: int
    player_id: int
    market_type: str
    line_value: float
    side: str
    sportsbook: str
    void_rule: str
    decimal_odds: float
    p_win: float
    p_lose: float
    p_void: float
    fair_market_prob: float | None
    ev_per_unit: float
    return_variance_per_unit: float
    kelly_full: float
    fraction: float
    stake: float


@dataclass(frozen=True)
class MarketAllocation:
    key: ProjectionKey
    n_candidates: int
    expected_return: float
    return_variance: float
    utility: float
    solver: str


@dataclass
class AllocationReport:
    directives: list[StakingDirective] = field(default_factory=list)
    allocations: list[MarketAllocation] = field(default_factory=list)
    skipped: list[tuple[ProjectionKey, str]] = field(default_factory=list)
    stale_quotes: int = 0


def devig_two_way(price_a: float, price_b: float) -> tuple[float, float]:
    if price_a <= 1.0 or price_b <= 1.0:
        raise ValueError("decimal prices must exceed 1.0")
    inv_a, inv_b = 1.0 / price_a, 1.0 / price_b
    total = inv_a + inv_b
    return inv_a / total, inv_b / total


def expected_value(probs: SideProbabilities, decimal_odds: float) -> float:
    return probs.p_win * (decimal_odds - 1.0) - probs.p_lose


def kelly_fraction_with_push(probs: SideProbabilities, decimal_odds: float) -> float:
    b = decimal_odds - 1.0
    settled = probs.p_win + probs.p_lose
    if b <= 0 or settled <= 0:
        return 0.0
    return max(0.0, (probs.p_win * b - probs.p_lose) / (b * settled))


@dataclass(frozen=True)
class Candidate:
    quote: LiveOddsQuote
    side: str
    price: float
    probs: SideProbabilities
    fair_prob: float | None
    ev: float
    kelly_full: float

    @property
    def void_rule(self) -> VoidRule:
        return VoidRule(self.quote.void_rule)


def _payoff(candidate: Candidate, scenario: str, count: int) -> float:
    rule = candidate.void_rule
    if scenario == "absent" and rule is not VoidRule.NO_VOID:
        return 0.0
    if scenario == "sub" and rule is VoidRule.VOID_IF_NOT_STARTING:
        return 0.0
    line = candidate.quote.line_value
    if count == line:
        return 0.0
    over_wins = count > line
    wins = over_wins if candidate.side == "over" else not over_wins
    return candidate.price - 1.0 if wins else -1.0


def outcome_model(projection: PropProjection, candidates: Sequence[Candidate]) -> tuple[np.ndarray, np.ndarray]:
    """
    Joint outcome grid for one player-market: returns (pi[S], R[S, J]) where pi are outcome
    probabilities and R the per-unit return of each candidate. Counts above the highest line are
    collapsed into one tail bucket, which is exact because every candidate settles identically there.
    """
    if not candidates:
        raise ValueError("no candidates")
    lu: LineupExpectation = projection.lineup
    x_top = int(math.floor(max(c.quote.line_value for c in candidates))) + 1
    probabilities: list[float] = []
    payoffs: list[list[float]] = []
    scenarios = (
        ("start", lu.p_start, lu.exp_minutes_start),
        ("sub", lu.p_sub_appearance, lu.exp_minutes_sub),
        ("absent", lu.p_no_appearance, None),
    )
    for scenario, weight, minutes in scenarios:
        if weight <= 0.0:
            continue
        if minutes is None:
            counts = np.array([0])
            mass = np.array([1.0])
        else:
            dist = projection.count_distribution(minutes)
            counts = np.arange(x_top + 1)
            mass = np.append(dist.pmf(counts[:-1]), dist.sf(x_top - 1))
        for count, p in zip(counts, mass):
            if p <= 0.0:
                continue
            probabilities.append(weight * float(p))
            payoffs.append([_payoff(c, scenario, int(count)) for c in candidates])
    return np.asarray(probabilities, dtype=float), np.asarray(payoffs, dtype=float)


def return_moments(probabilities: np.ndarray, payoffs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = probabilities @ payoffs
    second = payoffs.T @ (payoffs * probabilities[:, None])
    cov = second - np.outer(mu, mu)
    cov = 0.5 * (cov + cov.T)
    min_eig = float(np.linalg.eigvalsh(cov).min()) if cov.size else 0.0
    if min_eig < 0.0:
        cov = cov + np.eye(len(mu)) * (-min_eig + 1e-12)
    return mu, cov


def solve_mean_variance(
    mu: np.ndarray, cov: np.ndarray, upper: np.ndarray, budget: float, risk_aversion: float
) -> tuple[np.ndarray, str]:
    """Long-only mean-variance weights; never returns a portfolio worse than the best single bet."""
    n = len(mu)

    def utility(f: np.ndarray) -> float:
        return float(f @ mu - risk_aversion * f @ cov @ f)

    best_single = np.zeros(n)
    best_utility = 0.0
    for j in range(n):
        if mu[j] <= 0.0 or upper[j] <= 0.0:
            continue
        cap = min(upper[j], budget)
        f_j = cap if cov[j, j] <= 0.0 else min(cap, mu[j] / (2.0 * risk_aversion * cov[j, j]))
        trial = np.zeros(n)
        trial[j] = f_j
        u = utility(trial)
        if u > best_utility:
            best_single, best_utility = trial, u

    if n == 1:
        return best_single, "single"

    result = minimize(
        lambda f: -utility(f),
        x0=best_single,
        jac=lambda f: -(mu - 2.0 * risk_aversion * cov @ f),
        method="SLSQP",
        bounds=[(0.0, float(u)) for u in upper],
        constraints=[{"type": "ineq", "fun": lambda f: budget - f.sum(), "jac": lambda f: -np.ones(n)}],
        options={"ftol": 1e-14, "maxiter": 500},
    )
    if result.success:
        weights = np.clip(result.x, 0.0, upper)
        total = weights.sum()
        if total > budget:
            weights *= budget / total
        if utility(weights) >= best_utility - 1e-12:
            return weights, "qp"
    logger.warning("mean-variance QP did not improve on best single bet (%s); using single", result.message)
    return best_single, "single"


class InstitutionalAssetManager:
    """
    risk_aversion is lambda_risk in U = EV - lambda_risk * Var (position level). For small edges the
    unconstrained optimum is f = mu / (2 lambda sigma^2) against full Kelly f* ~ mu / sigma^2, so
    lambda_risk = 1 / (2 psi) reproduces psi-fractional Kelly: the default 2.0 behaves like quarter Kelly.
    """

    def __init__(
        self,
        bankroll: float,
        risk_aversion: float = 2.0,
        alpha_threshold: float = 0.02,
        max_position_fraction: float = 0.02,
        max_fixture_fraction: float = 0.05,
        max_total_fraction: float = 0.20,
        max_quote_age: timedelta = timedelta(minutes=5),
        open_exposure: float = 0.0,
        min_stake: float = 1.0,
    ):
        if not (math.isfinite(bankroll) and bankroll > 0):
            raise ValueError("bankroll must be finite and positive")
        if not (math.isfinite(risk_aversion) and risk_aversion > 0):
            raise ValueError("risk_aversion must be finite and positive")
        for name, value in (
            ("max_position_fraction", max_position_fraction),
            ("max_fixture_fraction", max_fixture_fraction),
            ("max_total_fraction", max_total_fraction),
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must lie in (0, 1]")
        if open_exposure < 0:
            raise ValueError("open_exposure must be non-negative")
        self.bankroll = bankroll
        self.risk_aversion = risk_aversion
        self.alpha_threshold = alpha_threshold
        self.max_position_fraction = max_position_fraction
        self.max_fixture_fraction = max_fixture_fraction
        self.max_total_fraction = max_total_fraction
        self.max_quote_age = max_quote_age
        self.open_exposure = open_exposure
        self.min_stake = min_stake

    def allocate(
        self,
        projections: Mapping[ProjectionKey, PropProjection],
        quotes: Iterable[LiveOddsQuote],
        now: datetime | None = None,
    ) -> AllocationReport:
        now = now or datetime.now(timezone.utc)
        report = AllocationReport()

        grouped: dict[ProjectionKey, list[LiveOddsQuote]] = defaultdict(list)
        for quote in quotes:
            if now - quote.captured_at > self.max_quote_age:
                report.stale_quotes += 1
                continue
            grouped[(quote.fixture_id, quote.player_id, quote.market_type)].append(quote)

        positions: list[tuple[Candidate, float, float]] = []
        for key, market_quotes in grouped.items():
            projection = projections.get(key)
            if projection is None:
                report.skipped.append((key, "no projection"))
                continue
            try:
                candidates = self._candidates(projection, market_quotes)
                if not candidates:
                    report.skipped.append((key, f"no candidate above alpha={self.alpha_threshold}"))
                    continue
                probabilities, payoffs = outcome_model(projection, candidates)
                mu, cov = return_moments(probabilities, payoffs)
            except (ProjectionError, ValueError, FloatingPointError) as exc:
                report.skipped.append((key, f"projection unusable: {exc}"))
                logger.error("market %s skipped, projection unusable: %s", key, exc)
                continue

            upper = np.array([min(c.kelly_full, self.max_position_fraction) for c in candidates])
            weights, solver = solve_mean_variance(mu, cov, upper, self.max_position_fraction, self.risk_aversion)
            if weights.sum() <= 0.0:
                report.skipped.append((key, "optimizer allocated nothing"))
                continue
            report.allocations.append(MarketAllocation(
                key=key,
                n_candidates=len(candidates),
                expected_return=float(weights @ mu),
                return_variance=float(weights @ cov @ weights),
                utility=float(weights @ mu - self.risk_aversion * weights @ cov @ weights),
                solver=solver,
            ))
            for j, c in enumerate(candidates):
                if weights[j] > 0.0:
                    positions.append((c, float(weights[j]), float(cov[j, j])))

        fractions = [w for _, w, _ in positions]
        fractions = self._apply_group_cap(positions, fractions, lambda c: c.quote.fixture_id, self.max_fixture_fraction)
        fractions = self._apply_group_cap(positions, fractions, lambda c: "slate", self.max_total_fraction)

        available = self.bankroll - self.open_exposure
        if available <= 0:
            logger.warning("no bankroll available after open exposure; no directives issued")
            return report
        for (c, _, variance), fraction in sorted(zip(positions, fractions), key=lambda item: -item[0][0].ev):
            stake = round(min(fraction * available, c.quote.max_stake), 2)
            if stake < self.min_stake:
                continue
            report.directives.append(StakingDirective(
                fixture_id=c.quote.fixture_id,
                player_id=c.quote.player_id,
                market_type=c.quote.market_type,
                line_value=c.quote.line_value,
                side=c.side,
                sportsbook=c.quote.sportsbook,
                void_rule=c.void_rule.value,
                decimal_odds=c.price,
                p_win=round(c.probs.p_win, 5),
                p_lose=round(c.probs.p_lose, 5),
                p_void=round(c.probs.p_void, 5),
                fair_market_prob=None if c.fair_prob is None else round(c.fair_prob, 5),
                ev_per_unit=round(c.ev, 5),
                return_variance_per_unit=round(variance, 5),
                kelly_full=round(c.kelly_full, 6),
                fraction=round(fraction, 6),
                stake=stake,
            ))
        return report

    def _candidates(self, projection: PropProjection, quotes: Sequence[LiveOddsQuote]) -> list[Candidate]:
        """Positive-edge candidates, keeping only the best price per (line, side, void rule)."""
        best: dict[tuple[float, str, VoidRule], Candidate] = {}
        for quote in quotes:
            fair: dict[str, float] = {}
            if quote.over_price and quote.under_price and quote.over_price > 1.0 and quote.under_price > 1.0:
                fair["over"], fair["under"] = devig_two_way(quote.over_price, quote.under_price)
            for side, price in (("over", quote.over_price), ("under", quote.under_price)):
                if price is None or not (math.isfinite(price) and price > 1.0):
                    continue
                probs = projection.side_probabilities(quote.line_value, side, quote.void_rule)
                ev = expected_value(probs, price)
                if ev <= self.alpha_threshold:
                    continue
                candidate = Candidate(quote, side, price, probs, fair.get(side), ev,
                                      kelly_fraction_with_push(probs, price))
                slot = (quote.line_value, side, VoidRule(quote.void_rule))
                if slot not in best or candidate.price > best[slot].price:
                    best[slot] = candidate
        return sorted(best.values(), key=lambda c: (c.quote.line_value, c.side))

    @staticmethod
    def _apply_group_cap(
        positions: Sequence[tuple[Candidate, float, float]],
        fractions: Sequence[float],
        key_fn: Callable[[Candidate], Hashable],
        cap: float,
    ) -> list[float]:
        totals: dict[Hashable, float] = defaultdict(float)
        for (c, _, _), f in zip(positions, fractions):
            totals[key_fn(c)] += f
        return [
            f * (cap / totals[key_fn(c)]) if totals[key_fn(c)] > cap else f
            for (c, _, _), f in zip(positions, fractions)
        ]


def price_for_ev(probs: SideProbabilities, target_ev: float) -> float:
    """Decimal price at which a side with these outcome probabilities has the given EV per unit."""
    return 1.0 + (target_ev + probs.p_lose) / probs.p_win


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(timezone.utc)
    engine = QuantEngine(game_state_k=6.0)
    projection = engine.project_count_market(
        fixture_id=1001, player_id=55, market_type="tackles",
        counts=[3, 2, 4, 3, 1, 3], minutes=[90, 90, 90, 85, 90, 90],
        league_rate_per90=2.2, referee=RefereeProfile(1.0, 0.13, 0.004, 0.0), is_home=True,
        lineup=LineupExpectation(p_start=0.92, p_sub_appearance=0.05, exp_minutes_start=86, exp_minutes_sub=20),
    )
    quotes = [
        LiveOddsQuote(
            1001, 55, "tackles", line,
            over_price=price_for_ev(projection.side_probabilities(line, "over"), over_ev),
            under_price=price_for_ev(projection.side_probabilities(line, "under"), -0.10),
            max_stake=500.0, sportsbook=book, captured_at=now,
        )
        for line, over_ev, book in ((1.5, 0.060, "BookA"), (2.5, 0.062, "BookB"))
    ]
    manager = InstitutionalAssetManager(bankroll=10_000.0)
    report = manager.allocate({(1001, 55, "tackles"): projection}, quotes, now=now)
    for allocation in report.allocations:
        logger.info("ALLOCATION %s", allocation)
    for directive in report.directives:
        logger.info("DIRECTIVE line=%.1f %s @%.3f ev=%.4f var=%.3f fraction=%.5f stake=%.2f",
                    directive.line_value, directive.side, directive.decimal_odds, directive.ev_per_unit,
                    directive.return_variance_per_unit, directive.fraction, directive.stake)
    for key, reason in report.skipped:
        logger.info("SKIPPED %s: %s", key, reason)


if __name__ == "__main__":
    main()
```

## Tests

`tests/test_core.py` (76 tests: maths, engine hardening, allocator, estimators, transforms) and `tests/test_pipeline_concurrency.py` (11 tests: parallel speed-up, retry, isolation, deadlines, lineups, pool sizing, rate limiting) are in the zip. Run `pip install -r requirements.txt` then `pytest`.
