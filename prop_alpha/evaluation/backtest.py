"""Out-of-sample evaluation: walk-forward over-probabilities, proper scoring rules, calibration, CLV."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Final, Iterable, Mapping, Sequence

import numpy as np
from numpy.typing import ArrayLike
from scipy.special import xlogy

from prop_alpha.modeling.engine import (
    MAX_MATCH_MINUTES,
    NEUTRAL_REFEREE,
    LineupExpectation,
    ProjectionError,
    QuantEngine,
)
from prop_alpha.modeling.league_level import level_scaled_minutes

# Season start year -> trailing league rate per 90, as known strictly before the given kickoff.
LevelSource = Callable[[datetime], Mapping[int, float]]
from prop_alpha.scanner.market_scanner import devig_two_way


def _probabilities_and_outcomes(probabilities: ArrayLike, outcomes: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if p.ndim != 1 or p.shape != y.shape or p.size == 0:
        raise ValueError("probabilities and outcomes must be non-empty 1-D arrays of equal length")
    if not (np.isfinite(p).all() and ((p >= 0.0) & (p <= 1.0)).all()):
        raise ValueError("probabilities must be finite and lie in [0, 1]")
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError("outcomes must be 0 or 1")
    return p, y


def brier_score(probabilities: ArrayLike, outcomes: ArrayLike) -> float:
    p, y = _probabilities_and_outcomes(probabilities, outcomes)
    return float(np.mean((p - y) ** 2))


def log_loss(probabilities: ArrayLike, outcomes: ArrayLike) -> float:
    """Mean negative log-likelihood. A confident miss (p = 0 with y = 1) returns inf; nothing is clipped."""
    p, y = _probabilities_and_outcomes(probabilities, outcomes)
    with np.errstate(divide="ignore"):
        return float(-np.mean(xlogy(y, p) + xlogy(1.0 - y, 1.0 - p)))


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    n: int
    mean_predicted: float
    observed_rate: float


def calibration_table(probabilities: ArrayLike, outcomes: ArrayLike, n_bins: int = 10) -> list[CalibrationBin]:
    """Equal-width bins on [0, 1]; the last bin is closed. Empty bins are omitted."""
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    p, y = _probabilities_and_outcomes(probabilities, outcomes)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    index = np.minimum(np.searchsorted(edges, p, side="right") - 1, n_bins - 1)
    table: list[CalibrationBin] = []
    for b in range(n_bins):
        mask = index == b
        count = int(mask.sum())
        if count:
            table.append(CalibrationBin(
                lower=float(edges[b]),
                upper=float(edges[b + 1]),
                n=count,
                mean_predicted=float(p[mask].mean()),
                observed_rate=float(y[mask].mean()),
            ))
    return table


def closing_line_value(taken_odds: float, closing_price_taken_side: float, closing_price_other_side: float) -> float:
    """CLV = O_taken * p_fair_close - 1, with the closing two-way market devigged proportionally."""
    if not (math.isfinite(taken_odds) and taken_odds > 1.0):
        raise ValueError("taken_odds must be finite and exceed 1.0")
    fair_taken, _ = devig_two_way(closing_price_taken_side, closing_price_other_side)
    return taken_odds * fair_taken - 1.0


@dataclass(frozen=True)
class PlayerMatchObservation:
    player_id: int
    fixture_id: int
    kickoff_at: datetime
    started: bool
    minutes_played: float
    count: int

    def __post_init__(self) -> None:
        if self.kickoff_at.tzinfo is None or self.kickoff_at.utcoffset() != timedelta(0):
            raise ValueError("kickoff_at must be a timezone-aware UTC datetime")
        if not (math.isfinite(self.minutes_played) and 0.0 <= self.minutes_played <= MAX_MATCH_MINUTES):
            raise ValueError(f"minutes_played must lie in [0, {MAX_MATCH_MINUTES}]")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("count must be a non-negative integer")
        # A positive count with zero minutes is valid data (an unused substitute booked on the bench) and
        # is stored by ingestion; walk_forward_over_probability keeps it out of the per-minute rate.


@dataclass(frozen=True)
class BacktestPrediction:
    player_id: int
    fixture_id: int
    kickoff_at: datetime
    line: float
    p_over: float
    outcome: int
    n_history: int


@dataclass(frozen=True)
class BacktestFailure:
    player_id: int
    fixture_id: int
    error: str




def walk_forward_over_probability(
    observations: Iterable[PlayerMatchObservation],
    line: float,
    league_rate_per90: float,
    engine: QuantEngine,
    market_type: str = "tackles",
    prior_strength_90s: float = 6.0,
    default_start_minutes: float = 85.0,
    default_sub_minutes: float = 20.0,
    failures: list[BacktestFailure] | None = None,
    level: LevelSource | None = None,
) -> list[BacktestPrediction]:
    """P(count > line) for every appearance, using only that player's observations strictly before kickoff.

    With `level`, the league rate is the trailing rate of the target's season as known before its kickoff, and
    each past match's minutes are scaled by that match's season rate over today's (league_level), so league-wide
    drift between seasons does not carry into the price; `league_rate_per90` is then unused.

    The lineup is fixed at team-sheet time: a starter is priced with p_start = 1, a used substitute with
    p_sub = 1. Unused substitutes (0 minutes, not started) are voided and produce no prediction. Expected
    minutes are the player's prior mean when starting / coming on, else the defaults (as LINEUP_UPSERT_SQL).
    The referee is neutral (multiplier 1): observations carry no referee, so referee-sensitive markets
    are evaluated without the referee adjustment.

    Without `failures`, the first appearance that cannot be priced raises. With a list, each such appearance
    is appended to it and skipped (sanctioned handler H7), so one bad row cannot stop a full-season run;
    report len(failures) next to the scores, because skipped rows are not scored.
    """
    if not (math.isfinite(line) and line >= 0.0 and line * 2.0 % 2.0 == 1.0):
        raise ValueError("walk-forward evaluation needs a half line (e.g. 1.5) so every outcome settles")
    by_player: dict[int, list[PlayerMatchObservation]] = defaultdict(list)
    for observation in observations:
        by_player[observation.player_id].append(observation)

    predictions: list[BacktestPrediction] = []
    for player_id in sorted(by_player):
        history = sorted(by_player[player_id], key=lambda o: (o.kickoff_at, o.fixture_id))
        for target in history:
            prior = [o for o in history if o.kickoff_at < target.kickoff_at]
            if not target.started and target.minutes_played == 0.0:
                continue
            try:
                p_over = _over_probability(
                    target, prior, line, league_rate_per90, engine, market_type, prior_strength_90s,
                    default_start_minutes, default_sub_minutes, level,
                )
            except (ProjectionError, ValueError, FloatingPointError) as exc:
                if failures is None:
                    raise
                failures.append(BacktestFailure(player_id, target.fixture_id, f"{type(exc).__name__}: {exc}"))
                continue
            predictions.append(BacktestPrediction(
                player_id=player_id,
                fixture_id=target.fixture_id,
                kickoff_at=target.kickoff_at,
                line=line,
                p_over=p_over,
                outcome=int(target.count > line),
                n_history=len(prior),
            ))
    return sorted(predictions, key=lambda p: (p.kickoff_at, p.fixture_id, p.player_id))


def _over_probability(
    target: PlayerMatchObservation,
    prior: Sequence[PlayerMatchObservation],
    line: float,
    league_rate_per90: float,
    engine: QuantEngine,
    market_type: str,
    prior_strength_90s: float,
    default_start_minutes: float,
    default_sub_minutes: float,
    level: LevelSource | None = None,
) -> float:
    # Only time on the pitch informs a per-minute rate: zero-minute rows (including bench bookings,
    # which the engine's exposure validation rejects) are left out of the rate inputs.
    on_pitch = [o for o in prior if o.minutes_played > 0.0]
    exposure = [o.minutes_played for o in on_pitch]
    if level is not None:
        exposure, league_rate_per90 = level_scaled_minutes(
            exposure, [o.kickoff_at for o in on_pitch], level(target.kickoff_at), target.kickoff_at
        )
    start_minutes = [o.minutes_played for o in on_pitch if o.started]
    sub_minutes = [o.minutes_played for o in on_pitch if not o.started]
    lineup = LineupExpectation(
        p_start=1.0 if target.started else 0.0,
        p_sub_appearance=0.0 if target.started else 1.0,
        exp_minutes_start=float(np.mean(start_minutes)) if start_minutes else default_start_minutes,
        exp_minutes_sub=float(np.mean(sub_minutes)) if sub_minutes else default_sub_minutes,
    )
    projection = engine.project_count_market(
        fixture_id=target.fixture_id,
        player_id=target.player_id,
        market_type=market_type,
        counts=[o.count for o in on_pitch],
        minutes=exposure,
        league_rate_per90=league_rate_per90,
        referee=NEUTRAL_REFEREE,
        is_home=True,
        lineup=lineup,
        prior_strength_90s=prior_strength_90s,
    )
    return projection.side_probabilities(line, "over").p_win


def summarize(predictions: Sequence[BacktestPrediction]) -> dict[str, float]:
    """Brier score, log loss and Brier skill against climatology (the sample's own base rate)."""
    if not predictions:
        raise ValueError("no predictions to summarize")
    p = np.array([prediction.p_over for prediction in predictions])
    y = np.array([prediction.outcome for prediction in predictions], dtype=float)
    base_rate = float(y.mean())
    brier = brier_score(p, y)
    brier_climatology = brier_score(np.full_like(p, base_rate), y)
    if brier_climatology == 0.0:
        raise ValueError("climatology Brier score is zero (all outcomes identical); skill is undefined")
    return {
        "n": float(len(predictions)),
        "base_rate": base_rate,
        "brier": brier,
        "brier_climatology": brier_climatology,
        "brier_skill": 1.0 - brier / brier_climatology,
        "log_loss": log_loss(p, y),
    }
