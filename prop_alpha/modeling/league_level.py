"""League level: trailing positional rates per 90, and the re-scaling of a player's past exposure to today's level.

League-wide rates drift between seasons (League One/Two outfield starters: tackles per 90 1.552 in 2025/26, 1.485
in 2026/27 to 3 Oct; fouls 1.104 then 1.159). A player's history from last season then carries last season's
level. The fix models a player as a stable multiple of the league level: X_i ~ Pois(lambda * L(t_i) * m_i / 90),
so the rate today is lambda * L_now. In rate space this is the usual Gamma update with each past match's exposure
scaled by L(t_i) / L_now:

    alpha = s * L_now + sum c_i,    beta = s + sum (m_i / 90) * L(t_i) / L_now

`trailing_rate` gives L for a season as known at one moment: the season's rate to date, shrunk towards the
previous season's rate with SEASON_PRIOR_MINUTES pseudo-minutes, so the first weeks of a season lean on last
season and a completed season stands on its own. Pure: no I/O, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Mapping, Sequence

# Seasons start on 1 July (English and Italian leagues kick off in August).
SEASON_START_MONTH: Final[int] = 7
# Tuned on 2026-10-04 (scripts/tune_backtest.py): 30,000 beat 90,000 on the tune window (mean log loss 0.56152 v
# 0.56158) and cut the worst calibration bin on 4 of 5 lines in the untouched test window (2026-09-12 on). With one
# position in one league playing about 350,000 minutes a season, the current season carries half the weight after
# under a tenth of its matches.
SEASON_PRIOR_MINUTES: Final[float] = 30_000.0


def season_of(when: datetime) -> int:
    """The year the season containing `when` started (2026 for 2026/27)."""
    if when.tzinfo is None or when.utcoffset() != timedelta(0):
        raise ValueError("when must be a timezone-aware UTC datetime")
    return when.year if when.month >= SEASON_START_MONTH else when.year - 1


@dataclass(frozen=True)
class SeasonTotals:
    """Counts and minutes of one position in one season, from matches before the moment of pricing."""

    counts: float
    minutes: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.counts) and math.isfinite(self.minutes) and self.counts >= 0.0
                and self.minutes >= 0.0):
            raise ValueError("season totals must be finite and non-negative")


def trailing_rate(
    current: SeasonTotals | None, previous_rate: float | None, prior_minutes: float = SEASON_PRIOR_MINUTES
) -> float:
    """Rate per 90 for a season: its own totals shrunk towards `previous_rate` with `prior_minutes`.

    Raises ValueError when neither the season nor the one before it has any minutes: no level, no price.
    """
    if not (math.isfinite(prior_minutes) and prior_minutes > 0.0):
        raise ValueError("prior_minutes must be finite and positive")
    if previous_rate is not None and not (math.isfinite(previous_rate) and previous_rate > 0.0):
        raise ValueError("previous_rate must be finite and positive")
    minutes = current.minutes if current is not None else 0.0
    counts = current.counts if current is not None else 0.0
    if previous_rate is None:
        if minutes <= 0.0:
            raise ValueError("no minutes this season and no previous season: no league level")
        return 90.0 * counts / minutes
    return 90.0 * (counts + previous_rate * prior_minutes / 90.0) / (minutes + prior_minutes)


def season_rates(totals: Mapping[int, SeasonTotals], prior_minutes: float = SEASON_PRIOR_MINUTES) -> dict[int, float]:
    """Trailing rate for every season in `totals` (keyed by season start year), each shrunk towards the one
    before. A season with no minutes and no predecessor is left out."""
    rates: dict[int, float] = {}
    for season in sorted(totals):
        previous = rates.get(season - 1)
        current = totals[season]
        if previous is None and current.minutes <= 0.0:
            continue
        rates[season] = trailing_rate(current, previous, prior_minutes)
    return rates


def level_scaled_minutes(
    minutes: Sequence[float], kickoffs: Sequence[datetime], rates: Mapping[int, float], now: datetime
) -> tuple[list[float], float]:
    """(each past match's minutes scaled by L(season of that match) / L_now, L_now).

    Raises ValueError when a season of the history, or the season of `now`, has no rate.
    """
    if len(minutes) != len(kickoffs):
        raise ValueError("minutes and kickoffs must have equal length")
    season_now = season_of(now)
    if season_now not in rates:
        raise ValueError(f"no league level for season {season_now}")
    level_now = rates[season_now]
    scaled: list[float] = []
    for played, kickoff in zip(minutes, kickoffs):
        season = season_of(kickoff)
        if season not in rates:
            raise ValueError(f"no league level for season {season}")
        scaled.append(played * rates[season] / level_now)
    return scaled, level_now
