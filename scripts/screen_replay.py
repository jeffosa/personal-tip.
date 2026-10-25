"""Replay the pilot's tip pipeline on archived real bookmaker prices and grade what it would have tipped (2026-10-25).

Why: the high-value screen's survival test simulates from the model's own uncertainty, so it measures the model's confidence
in itself, not whether it is right. The model-risk loss floor (MODEL_RISK_LOSS_FLOOR, 0.03) does not guard ordinary bets
against that: it only moves win probability to loss until the loss probability is at least 0.03, so it changes nothing for a
bet that already loses more than 3% of the time (every tackles ladder step seen so far); it stops near-certain bets only. This replays every archived pilot odds snapshot (`.prop_alpha_state/pilot/<stamp>_<book>_pilot.json.gz`)
through the real code path of a pilot scan: the current pilot pricing (history strictly before each kickoff, read from
`prop_alpha_pilot`), the bookmaker check, the implausible-edge guard, the allocator and the high-value screen, all at the
production settings. Each pick is then re-checked with every --haircuts value h: h of the model's win probability is moved
to loss and the pick is kept only if its expected profit is still at least the screen's 4%; h = 0 is the production rule. Every pick is then graded on the stored Sportmonks count (tackles_attempted / fouls_committed; void if
the player did not play), the same assumed market rules as the paper ledger. A selection is counted once, at its first
snapshot, as the sent-alert store would. Read-only: no request, no write except the optional --csv. The current settings
price every replayed match, so a pick here is what today's rules would have tipped, not what was tipped at the time.

    .venv\\Scripts\\python.exe scripts\\screen_replay.py [--haircuts 0,0.03,0.05,0.08,0.10] [--all-leagues] [--csv out.csv]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts import ladder_scan, scan  # noqa: E402
from prop_alpha.alerts.fx import book_to_bankroll, bankroll_currency  # noqa: E402
from prop_alpha.alerts.telegram import simulation_seed  # noqa: E402
from prop_alpha.config import DEFAULT_PILOT_DSN  # noqa: E402
from prop_alpha.ingestion import oddspapi  # noqa: E402
from prop_alpha.scanner.market_scanner import MODEL_RISK_LOSS_FLOOR, ExposureBook, InstitutionalAssetManager, StakingDirective  # noqa: E402
from prop_alpha.scanner.slate import UpcomingFixture, quotes_from_prices  # noqa: E402
from prop_alpha.scanner.value_screen import HighValueCriteria, assess_high_value  # noqa: E402

STATE: Final[Path] = ROOT / ".prop_alpha_state"
# Snapshots price matches kicking off up to this long after they were taken (a scan's window is 4 hours; by-hand runs
# priced the next evening's matches).
HORIZON: Final[timedelta] = timedelta(days=2)
FIXTURES_SQL: Final[str] = """
    SELECT f.fixture_id, f.vendor_fixture_uid, l.name, ht.vendor_team_uid, at.vendor_team_uid, ht.name, at.name, f.kickoff_at
    FROM prop_alpha.fixtures f JOIN prop_alpha.leagues l USING (league_id)
    JOIN prop_alpha.teams ht ON ht.team_id = f.home_team_id JOIN prop_alpha.teams at ON at.team_id = f.away_team_id
    WHERE f.status = 'final' AND f.kickoff_at > %s AND f.kickoff_at <= %s AND l.name = ANY(%s)
"""
OUTCOME_SQL: Final[str] = """
    SELECT s.player_id, s.minutes_played, s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s WHERE s.fixture_id = %s
"""


@dataclass(frozen=True)
class Pick:
    haircut: float
    stamp: str
    fixture: str
    player_id: int
    market: str
    line: float
    side: str
    book: str
    odds: float
    stake: float
    model_win: float
    ev: float
    profit_per_unit: float | None  # None: void (the player did not play) or no stored row


def settle(side: str, line: float, odds: float, played: bool, count: int | None) -> float | None:
    """Pure: profit per unit staked under the assumed rules (void if the player did not play; a count on a whole line
    pushes, 0.0), or None when the bet is void or the count is unknown."""
    if not played or count is None:
        return None
    if count == line:
        return 0.0
    won = count > line if side == "over" else count < line
    return odds - 1.0 if won else -1.0


def haircut_ev(p_win: float, p_lose: float, odds: float, haircut: float) -> float:
    """Pure: expected profit per unit staked after moving `haircut` of win probability to loss (void mass untouched)."""
    if not 0.0 <= haircut < 1.0:
        raise ValueError(f"the haircut must be in [0, 1), not {haircut!r}")
    shift = min(haircut, p_win)
    return (p_win - shift) * (odds - 1.0) - (p_lose + shift)


def summarise(picks: Sequence[Pick]) -> str:
    """Pure: one line per haircut: picks, settled, won, the mean price-implied chance, unit and stake-weighted return."""
    lines = []
    for haircut in sorted({p.haircut for p in picks}):
        rows = [p for p in picks if p.haircut == haircut]
        settled = [p for p in rows if p.profit_per_unit is not None]
        if not settled:
            lines.append(f"haircut {haircut:.2f}: {len(rows)} picks, none settled")
            continue
        won = sum(p.profit_per_unit > 0 for p in settled if p.profit_per_unit is not None)
        unit = sum(p.profit_per_unit for p in settled if p.profit_per_unit is not None) / len(settled)
        staked = sum(p.stake for p in settled)
        weighted = sum(p.stake * p.profit_per_unit for p in settled if p.profit_per_unit is not None) / staked if staked else 0.0
        implied = sum(1.0 / p.odds for p in settled) / len(settled)
        model = sum(p.model_win for p in settled) / len(settled)
        claimed = sum(p.ev for p in settled) / len(settled)
        lines.append(f"haircut {haircut:.2f}: {len(rows)} picks, {len(settled)} settled, won {won} ({won / len(settled):.0%}); "
                     f"model said {model:.0%}, price implied {implied:.0%}; claimed EV {claimed:+.1%}; "
                     f"return per unit {unit:+.1%}, stake-weighted {weighted:+.1%}")
    return "\n".join(lines)


def snapshots() -> dict[str, dict[str, Any]]:
    """Archived pilot replies, stamp -> book -> body."""
    found: dict[str, dict[str, Any]] = defaultdict(dict)
    for path in sorted((STATE / ladder_scan.PILOT_ODDS_DIR).glob("*_pilot.json.gz")):
        stamp, book = path.name.split("_")[:2]
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            found[stamp][book] = json.load(handle)
    return found


def main(argv: Sequence[str] | None = None) -> int:
    import psycopg2

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--haircuts", default="0,0.03,0.05,0.08,0.10",
                        help="win probability moved to loss before re-checking the 4%% minimum (0 = production)")
    parser.add_argument("--all-leagues", action="store_true",
                        help="replay every pilot league with prices, not only ladder_scan.TIP_LEAGUES")
    parser.add_argument("--csv", type=Path, help="write every pick here")
    args = parser.parse_args(argv)
    haircuts = [float(h) for h in args.haircuts.split(",")]
    leagues = sorted(oddspapi.PILOT_TOURNAMENTS) if args.all_leagues else sorted(ladder_scan.TIP_LEAGUES)
    line_catalogue = scan.load_catalogue(STATE / scan.ODDS_CACHE_DIR / scan.CATALOGUE_FILE)
    ladder_catalogue = ladder_scan.load_ladder_catalogue(STATE / scan.ODDS_CACHE_DIR / ladder_scan.LADDER_CATALOGUE_FILE)
    names = scan._load_names(STATE / scan.ODDS_CACHE_DIR / "participants.json")  # noqa: SLF001 - the scans' own cache
    currency = bankroll_currency()
    rate = book_to_bankroll(currency, datetime.now(timezone.utc).date())
    bankroll = float(os.environ.get(scan.BANKROLL_ENV, scan.DEFAULT_BANKROLL))
    seen: set[tuple[int, int, str, float, str, str]] = set()
    manager = InstitutionalAssetManager(bankroll, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    criteria = HighValueCriteria(loss_floor=MODEL_RISK_LOSS_FLOOR)
    picks: list[Pick] = []
    priced_matches = quotes_total = 0
    conn = psycopg2.connect(DEFAULT_PILOT_DSN, options="-c statement_timeout=60000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            for stamp, bodies in sorted(snapshots().items()):
                taken = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                cur.execute(FIXTURES_SQL, (taken, taken + HORIZON, leagues))
                rows = cur.fetchall()
                fixtures = [UpcomingFixture(int(uid), league, str(home), str(away), home_name, away_name,
                                            kickoff.astimezone(timezone.utc))
                            for _, uid, league, home, away, home_name, away_name, kickoff in rows]
                db_id = {int(uid): fixture_id for fixture_id, uid, *_ in rows}
                if not fixtures:
                    continue
                slate = [(f, ladder_scan.load_pilot_inputs(cur, f)) for f in fixtures]
                prices: list[oddspapi.PropPrice] = []
                for book, body in bodies.items():
                    prices += [*oddspapi.prop_prices(body, line_catalogue, book),
                               *oddspapi.ladder_as_prop_prices(oddspapi.ladder_prices(body, ladder_catalogue, book))]
                coverage = quotes_from_prices(prices, names, fixtures, {f.fixture_id: i.squad for f, i in slate}, taken)
                for fixture, inputs in slate:
                    quotes = scan.in_bankroll_currency([q for q in coverage.quotes if q.fixture_id == fixture.fixture_id
                                                        and q.market_type in ladder_scan.ALERT_MARKETS], rate)
                    if not quotes:
                        continue
                    engine, market_engines = ladder_scan.pilot_engines(fixture.league)
                    try:
                        projections, _ = ladder_scan.price_pilot(fixture, inputs, engine, market_engines)
                    except ValueError as exc:  # ProjectionError is a ValueError; the match is reported and left out
                        print(f"  {stamp} {fixture.name}: not priced: {exc}")
                        continue
                    priced_matches += 1
                    if ladder_scan.MARKET_CHECK_MARGIN is not None:
                        quotes, _, _ = ladder_scan.beating_other_books(quotes, ladder_scan.MARKET_CHECK_MARGIN)
                    quotes, _ = ladder_scan.without_implausible_edges(quotes, projections)
                    quotes_total += len(quotes)
                    cur.execute(OUTCOME_SQL, (db_id[fixture.fixture_id],))
                    outcome = {int(pid): (int(minutes or 0), {"tackles": tackles, "fouls": fouls})
                               for pid, minutes, tackles, fouls in cur.fetchall()}
                    report = manager.allocate(projections, quotes, now=taken, exposure=ExposureBook({}))
                    for d in report.directives:
                        picks_key = (d.fixture_id, d.player_id, d.market_type, d.line_value, d.side, d.sportsbook)
                        if picks_key in seen:
                            continue
                        assessment = assess_high_value(d, projections[(d.fixture_id, d.player_id, d.market_type)],
                                                       criteria, np.random.default_rng(simulation_seed(d)))
                        if not assessment.is_high_value:
                            continue
                        seen.add(picks_key)
                        for haircut in haircuts:
                            ev = haircut_ev(d.p_win, d.p_lose, d.decimal_odds, haircut)
                            if ev >= criteria.min_ev_per_unit:
                                picks.append(_pick(haircut, stamp, fixture, d, assessment.model_win_probability, ev, outcome))
    finally:
        conn.close()
    print(f"Replayed {len(snapshots())} snapshots: {priced_matches} match-snapshots priced with real quotes, "
          f"{quotes_total} quotes after the bookmaker check and the implausible-edge guard; leagues: {', '.join(leagues)}")
    print(summarise(picks) if picks else "No pick passed the screen.")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(Pick.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(p.__dict__ for p in picks)
        print(f"picks written to {args.csv}")
    return 0


def _pick(haircut: float, stamp: str, fixture: UpcomingFixture, d: StakingDirective, model_win: float, ev: float,
          outcome: Mapping[int, tuple[int, Mapping[str, int | None]]]) -> Pick:
    minutes, counts = outcome.get(d.player_id, (0, {}))
    return Pick(haircut, stamp, fixture.name, d.player_id, d.market_type, d.line_value, d.side, d.sportsbook, d.decimal_odds,
                d.stake, model_win, ev, settle(d.side, d.line_value, d.decimal_odds, minutes > 0, counts.get(d.market_type)))


if __name__ == "__main__":
    sys.exit(main())
