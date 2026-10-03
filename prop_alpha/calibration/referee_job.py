"""Referee coefficients: shrunk strictness (SC), card propensities (YCP, RCP) and home bias (HBR).

`compute_referee_coefficients` is pure. `run_referee_job` reads per-fixture discipline totals and writes
the coefficients to prop_alpha.referees under the same advisory lock as merge_staging_player_feed(),
which serialises the two writers of `referees` and removes their lock-order deadlock. psycopg2 is
imported inside run_referee_job only, so the pure core imports no database driver.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Final, Iterable

logger = logging.getLogger("prop_alpha.referee_job")

# Must equal the hashtext() key in merge_staging_player_feed() (schema.sql, migrations/003).
MERGE_LOCK_KEY: Final[str] = "prop_alpha.merge_staging_player_feed"

DISCIPLINE_SQL: Final[str] = """
    SELECT f.referee_id, f.league_id, f.fixture_id,
           COALESCE(sum(s.fouls_committed) FILTER (WHERE s.is_home), 0)     AS home_fouls,
           COALESCE(sum(s.fouls_committed) FILTER (WHERE NOT s.is_home), 0) AS away_fouls,
           COALESCE(sum(s.yellow_cards), 0)                                 AS yellow_cards,
           COALESCE(sum(s.red_cards), 0)                                    AS red_cards
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.player_match_stats s ON s.fixture_id = f.fixture_id
    WHERE f.league_id = %s
      AND f.status = 'final'
      AND f.referee_id IS NOT NULL
      AND f.kickoff_at >= now() - make_interval(days => %s)
    GROUP BY f.referee_id, f.league_id, f.fixture_id
    ORDER BY f.referee_id, f.fixture_id
"""

UPDATE_SQL: Final[str] = """
    UPDATE prop_alpha.referees AS r
    SET foul_strictness_coefficient = v.foul_strictness_coefficient,
        yellow_card_propensity      = v.yellow_card_propensity,
        red_card_propensity         = v.red_card_propensity,
        home_bias_ratio             = v.home_bias_ratio
    FROM (VALUES %s) AS v(referee_id, league_id, foul_strictness_coefficient, yellow_card_propensity,
                          red_card_propensity, home_bias_ratio)
    WHERE r.referee_id = v.referee_id
      AND r.league_id = v.league_id
"""
# league_id guard: a referee seen in several leagues is registered to one (referees.league_id, set when the
# merge first inserts them). Only that league's run may write their coefficients, so running league A and
# then league B no longer leaves a referee with whichever league ran last.
UPDATE_TEMPLATE: Final[str] = "(%s::int, %s::int, %s::numeric, %s::numeric, %s::numeric, %s::numeric)"

ADVISORY_LOCK_SQL: Final[str] = "SELECT pg_advisory_xact_lock(hashtext(%s))"
# The job holds the merge's advisory lock for its whole transaction, so it must not wait or run unbounded:
# lock_timeout bounds the wait for a running merge; statement_timeout bounds the read and the update.
CONNECTION_OPTIONS: Final[str] = "-c lock_timeout=10000 -c statement_timeout=60000"


@dataclass(frozen=True)
class FixtureDiscipline:
    referee_id: int
    league_id: int
    fixture_id: int
    home_fouls: int
    away_fouls: int
    yellow_cards: int
    red_cards: int

    def __post_init__(self) -> None:
        for name in ("home_fouls", "away_fouls", "yellow_cards", "red_cards"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"fixture {self.fixture_id}: {name} must be a non-negative integer")


@dataclass(frozen=True)
class RefereeCoefficients:
    referee_id: int
    matches: int
    strictness_coefficient: float
    yellow_card_propensity: float
    red_card_propensity: float
    home_bias_ratio: float


def compute_referee_coefficients(
    rows: Iterable[FixtureDiscipline], shrink_matches: float = 15.0
) -> list[RefereeCoefficients]:
    """Shrink each referee toward their league with `shrink_matches` pseudo-matches of league-average data.

    With F the referee's fouls over n matches and F_bar the league's fouls per match:
      SC  = (F + m0 F_bar) / ((n + m0) F_bar)
      YCP = (Y + m0 F_bar y_bar) / (F + m0 F_bar)        y_bar = league yellows per foul (RCP likewise)
      HBR = (A - H + m0 F_bar h_bar) / (A + H + m0 F_bar) h_bar = league (away - home) / total fouls
    Venue multipliers are (1 - HBR) at home and (1 + HBR) away (QuantEngine.referee_multiplier).
    Output is ordered by referee_id, the lock order UPDATE_SQL relies on.
    """
    if not shrink_matches > 0.0:
        raise ValueError("shrink_matches must be positive")
    by_referee: dict[int, list[FixtureDiscipline]] = defaultdict(list)
    by_league: dict[int, list[FixtureDiscipline]] = defaultdict(list)
    seen: set[tuple[int, int]] = set()
    for row in rows:
        if (row.referee_id, row.fixture_id) in seen:
            raise ValueError(f"duplicate discipline row for referee {row.referee_id}, fixture {row.fixture_id}")
        seen.add((row.referee_id, row.fixture_id))
        by_referee[row.referee_id].append(row)
        by_league[row.league_id].append(row)

    league_rates: dict[int, tuple[float, float, float, float]] = {}
    for league_id, league_rows in by_league.items():
        home = sum(r.home_fouls for r in league_rows)
        away = sum(r.away_fouls for r in league_rows)
        fouls = home + away
        if fouls == 0:
            raise ValueError(f"league {league_id} has no recorded fouls; coefficients are undefined")
        league_rates[league_id] = (
            fouls / len(league_rows),
            sum(r.yellow_cards for r in league_rows) / fouls,
            sum(r.red_cards for r in league_rows) / fouls,
            (away - home) / fouls,
        )

    results: list[RefereeCoefficients] = []
    for referee_id in sorted(by_referee):
        referee_rows = by_referee[referee_id]
        leagues = {r.league_id for r in referee_rows}
        if len(leagues) != 1:
            raise ValueError(f"referee {referee_id} appears in several leagues {sorted(leagues)}")
        fouls_per_match, yellow_per_foul, red_per_foul, league_bias = league_rates[leagues.pop()]
        home = sum(r.home_fouls for r in referee_rows)
        away = sum(r.away_fouls for r in referee_rows)
        fouls = home + away
        matches = len(referee_rows)
        pseudo_fouls = shrink_matches * fouls_per_match
        results.append(RefereeCoefficients(
            referee_id=referee_id,
            matches=matches,
            strictness_coefficient=(fouls + pseudo_fouls) / ((matches + shrink_matches) * fouls_per_match),
            yellow_card_propensity=(sum(r.yellow_cards for r in referee_rows) + pseudo_fouls * yellow_per_foul)
            / (fouls + pseudo_fouls),
            red_card_propensity=(sum(r.red_cards for r in referee_rows) + pseudo_fouls * red_per_foul)
            / (fouls + pseudo_fouls),
            home_bias_ratio=(away - home + pseudo_fouls * league_bias) / (fouls + pseudo_fouls),
        ))
    return results


def run_referee_job(dsn: str, league_id: int, lookback_days: int = 730, shrink_matches: float = 15.0) -> int:
    """Recompute and store one league's referee coefficients in a single transaction; returns rows updated."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(dsn, options=CONNECTION_OPTIONS)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ADVISORY_LOCK_SQL, (MERGE_LOCK_KEY,))
                cur.execute(DISCIPLINE_SQL, (league_id, lookback_days))
                rows = [FixtureDiscipline(*(int(value) for value in record)) for record in cur.fetchall()]
                coefficients = compute_referee_coefficients(rows, shrink_matches)
                values = [
                    (c.referee_id, league_id, c.strictness_coefficient, c.yellow_card_propensity,
                     c.red_card_propensity, c.home_bias_ratio)
                    for c in coefficients
                ]
                if values:
                    psycopg2.extras.execute_values(cur, UPDATE_SQL, values, template=UPDATE_TEMPLATE)
    finally:
        conn.close()
    logger.info("league %d: updated %d referees from %d fixtures", league_id, len(values), len(rows))
    return len(values)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Recompute referee coefficients for one league.")
    parser.add_argument("--league-id", type=int, required=True)
    parser.add_argument("--lookback-days", type=int, default=730)
    parser.add_argument("--shrink-matches", type=float, default=15.0)
    args = parser.parse_args(argv)
    run_referee_job(os.environ["PROP_ALPHA_DB_DSN"], args.league_id, args.lookback_days, args.shrink_matches)
    return 0


if __name__ == "__main__":
    sys.exit(main())
