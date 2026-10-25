"""How far the pilot model overstates its chances on the prices it favours: the evidence for a model-risk haircut (2026-10-25).

Why: tips are sent only where the model beats the bookmaker, and those are disproportionately the places where the model is
wrong (the winner's curse); the high-value screen cannot see that (it simulates from the model's own uncertainty) and the
0.03 loss floor does not touch ordinary bets (scripts/screen_replay.py). Screened picks are too rare to size a haircut on
(7 in the archive on 2026-10-25), so this scores every archived real price instead: for each match, the last archived pilot
snapshot before kickoff (`.prop_alpha_state/pilot/`), priced with today's pilot settings from history strictly before kickoff
(`prop_alpha_pilot`), every bookmaker's every tackles and fouls price, graded on the stored Sportmonks count under the
assumed rules (void if the player did not play; a push is left out). Prices are grouped by the edge the model claims
(its chance x the odds - 1), and each group shows the model's mean chance against how often the bet came in. The gap on the
favoured prices is the haircut estimate, with a 90% interval from a bootstrap over players-in-matches (the ladder steps of
one player in one match share one outcome, so they are resampled together). Read-only; nothing is written but --csv.

    .venv\\Scripts\\python.exe scripts\\edge_calibration.py [--min-edge 0.04] [--csv out.csv]
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from prop_alpha.alerts import ladder_scan, scan  # noqa: E402
from prop_alpha.config import DEFAULT_PILOT_DSN  # noqa: E402
from prop_alpha.ingestion import oddspapi  # noqa: E402
from prop_alpha.scanner.market_scanner import LiveOddsQuote  # noqa: E402
from prop_alpha.scanner.slate import UpcomingFixture, quotes_from_prices  # noqa: E402
from screen_replay import FIXTURES_SQL, HORIZON, OUTCOME_SQL, STATE, settle, snapshots  # noqa: E402

# Edge groups (the model's claimed expected profit per unit, if the player plays): lower bounds.
EDGE_BUCKETS: Final[tuple[float, ...]] = (-1.0, 0.0, 0.04, 0.10, 0.25)
BOOTSTRAP_DRAWS: Final[int] = 2000
SEED: Final[int] = 20261025


@dataclass(frozen=True)
class Scored:
    fixture_id: int
    fixture: str
    league: str
    player_id: int
    market: str
    line: float
    side: str
    book: str
    odds: float
    model_chance: float  # the model's chance the bet wins if the player plays
    won: bool

    @property
    def edge(self) -> float:
        return self.model_chance * self.odds - 1.0


def bucket_label(low: float, high: float | None) -> str:
    if low <= -1.0:
        return f"edge < {high:+.0%}" if high is not None else "all"
    return f"edge {low:+.0%} to {high:+.0%}" if high is not None else f"edge >= {low:+.0%}"


def overstatement(rows: Sequence[Scored], rng: np.random.Generator, draws: int = BOOTSTRAP_DRAWS) -> tuple[float, float, float]:
    """Pure (randomness only through `rng`): the model's mean chance minus the share that won, and its 90% interval from a
    bootstrap that resamples whole players-in-matches."""
    if not rows:
        raise ValueError("no prices to measure")
    clusters: dict[tuple[int, int], list[float]] = defaultdict(list)
    for row in rows:
        clusters[(row.fixture_id, row.player_id)].append(row.model_chance - float(row.won))
    sums = np.array([sum(v) for v in clusters.values()])
    counts = np.array([len(v) for v in clusters.values()], dtype=float)
    estimate = float(sums.sum() / counts.sum())
    picks = rng.integers(0, len(sums), size=(draws, len(sums)))
    boot = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)
    low, high = np.quantile(boot, [0.05, 0.95])
    return estimate, float(low), float(high)


def table(rows: Sequence[Scored], rng: np.random.Generator) -> list[str]:
    """Pure: one line per edge group: prices, players-in-matches, the model's mean chance, the share that won, the price's
    implied chance, the return per unit at the price, and the overstatement with its 90% interval."""
    lines = []
    uppers: list[float | None] = [*EDGE_BUCKETS[1:], None]
    for low, high in zip(EDGE_BUCKETS, uppers):
        group = [r for r in rows if r.edge >= low and (high is None or r.edge < high)]
        if not group:
            continue
        gap, gap_low, gap_high = overstatement(group, rng)
        players = len({(r.fixture_id, r.player_id) for r in group})
        won = sum(r.won for r in group) / len(group)
        model = sum(r.model_chance for r in group) / len(group)
        implied = sum(1.0 / r.odds for r in group) / len(group)
        ret = sum((r.odds - 1.0) if r.won else -1.0 for r in group) / len(group)
        lines.append(f"  {bucket_label(low, high):<20} {len(group):5} prices, {players:4} players: model {model:5.1%}, "
                     f"came in {won:5.1%}, price implied {implied:5.1%}, return {ret:+6.1%}; "
                     f"model too high by {gap:+.3f} [{gap_low:+.3f}, {gap_high:+.3f}]")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    import psycopg2

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--min-edge", type=float, default=0.04,
                        help="the claimed edge from which a price counts as favoured (default: the screen's 4%%)")
    parser.add_argument("--csv", type=Path, help="write every scored price here")
    args = parser.parse_args(argv)
    line_catalogue = scan.load_catalogue(STATE / scan.ODDS_CACHE_DIR / scan.CATALOGUE_FILE)
    ladder_catalogue = ladder_scan.load_ladder_catalogue(STATE / scan.ODDS_CACHE_DIR / ladder_scan.LADDER_CATALOGUE_FILE)
    names = scan._load_names(STATE / scan.ODDS_CACHE_DIR / "participants.json")  # noqa: SLF001 - the scans' own cache
    leagues = sorted(oddspapi.PILOT_TOURNAMENTS)
    archive = snapshots()
    # Each match is scored on the prices of the last snapshot before its kickoff that carried any for it (later prices carry
    # later team news; a book can pull a market close to kickoff, so the very last snapshot may hold none).
    fixtures_by_id: dict[int, tuple[UpcomingFixture, int]] = {}
    quotes_by_fixture: dict[int, list[LiveOddsQuote]] = {}
    inputs_by_fixture: dict[int, scan.SlateInputs] = {}
    conn = psycopg2.connect(DEFAULT_PILOT_DSN, options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    scored: list[Scored] = []
    try:
        with conn, conn.cursor() as cur:
            for stamp in sorted(archive):
                taken = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                cur.execute(FIXTURES_SQL, (taken, taken + HORIZON, leagues))
                fixtures = []
                for fixture_id, uid, league, home, away, home_name, away_name, kickoff in cur.fetchall():
                    fixture = UpcomingFixture(int(uid), league, str(home), str(away), home_name, away_name,
                                              kickoff.astimezone(timezone.utc))
                    fixtures_by_id[fixture.fixture_id] = (fixture, int(fixture_id))
                    if fixture.fixture_id not in inputs_by_fixture:
                        inputs_by_fixture[fixture.fixture_id] = ladder_scan.load_pilot_inputs(cur, fixture)
                    fixtures.append(fixture)
                if not fixtures:
                    continue
                prices: list[oddspapi.PropPrice] = []
                for book, body in archive[stamp].items():
                    prices += [*oddspapi.prop_prices(body, line_catalogue, book),
                               *oddspapi.ladder_as_prop_prices(oddspapi.ladder_prices(body, ladder_catalogue, book))]
                coverage = quotes_from_prices(prices, names, fixtures,
                                              {f.fixture_id: inputs_by_fixture[f.fixture_id].squad for f in fixtures}, taken)
                for fixture in fixtures:
                    found = [q for q in coverage.quotes if q.fixture_id == fixture.fixture_id]
                    if found:
                        quotes_by_fixture[fixture.fixture_id] = found  # stamps run in order: the latest with prices wins
            for uid, quotes in sorted(quotes_by_fixture.items()):
                fixture, db_fixture_id = fixtures_by_id[uid]
                engine, market_engines = ladder_scan.pilot_engines(fixture.league)
                try:
                    projections, _ = ladder_scan.price_pilot(fixture, inputs_by_fixture[uid], engine, market_engines)
                except ValueError as exc:  # ProjectionError is a ValueError; the match is reported and left out
                    print(f"  {fixture.name}: not priced: {exc}")
                    continue
                cur.execute(OUTCOME_SQL, (db_fixture_id,))
                outcome = {int(pid): (int(minutes or 0), {"tackles": tackles, "fouls": fouls})
                           for pid, minutes, tackles, fouls in cur.fetchall()}
                for quote in quotes:
                    projection = projections.get((quote.fixture_id, quote.player_id, quote.market_type))
                    minutes, counts = outcome.get(quote.player_id, (0, {}))
                    if projection is None:
                        continue
                    for side, price in (("over", quote.over_price), ("under", quote.under_price)):
                        if price is None:
                            continue
                        result = settle(side, quote.line_value, price, minutes > 0, counts.get(quote.market_type))
                        sides = projection.side_probabilities(quote.line_value, side, quote.void_rule)
                        settled = sides.p_win + sides.p_lose
                        if result is None or result == 0.0 or settled <= 0.0:
                            continue
                        scored.append(Scored(fixture.fixture_id, fixture.name, fixture.league, quote.player_id,
                                             quote.market_type, quote.line_value, side, quote.sportsbook, price,
                                             sides.p_win / settled, result > 0.0))
    finally:
        conn.close()
    rng = np.random.default_rng(SEED)
    print(f"Scored {len(scored)} archived real prices in {len({r.fixture_id for r in scored})} matches "
          f"({len(archive)} snapshots; each match on the last one before kickoff with prices for it), books: {', '.join(sorted({r.book for r in scored}))}")
    for market in sorted({r.market for r in scored}):
        rows = [r for r in scored if r.market == market]
        print(f"{market}:")
        print("\n".join(table(rows, rng)))
        favoured = [r for r in rows if r.edge >= args.min_edge]
        if favoured:
            gap, low, high = overstatement(favoured, rng)
            print(f"  -> on the {len(favoured)} prices the model favours by {args.min_edge:.0%} or more, it is too high by "
                  f"{gap:+.3f} (90% interval {low:+.3f} to {high:+.3f}): the haircut this evidence points to")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[*Scored.__dataclass_fields__, "edge"])
            writer.writeheader()
            writer.writerows({**asdict(r), "edge": r.edge} for r in scored)
        print(f"scored prices written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
