"""Settle the pilot paper bets recorded on Sportmonks ids from TheStatsAPI's statistics, matched by club and player name.

The pilot's paper bets of 2026-10-11 and 2026-10-12 carry Sportmonks fixture and player ids. Since 2026-10-15 the pilot database
is TheStatsAPI's (prop_alpha_pilot_tsa), whose settlement query looks a match up by TheStatsAPI's "mt_" uid, so they could never
settle; and the Sportmonks archive (prop_alpha_pilot) never received their matches (its plan dropped Brazil Serie A on
2026-10-12). This finds each such bet's match in the pilot database by kickoff and club names (from pilot_tips.jsonl) and its
player by exact normalised name among that match's player rows, then grades it with ledger.settle_open_bets: the same code and
the same assumed market contract (pilot_ledger.PILOT_TACKLES_SQL) as every automatic settlement, so the result is provisional
in the same way. A bet whose match or player is not found exactly once stays open and is printed: a name that does not match is
never graded void.

Read-only on the database. Dry run by default; --write records the settlements in pilot_paper_bets.jsonl under the pilot
ledger's locks. Run from the repository root:
    .venv\\Scripts\\python.exe scripts\\pilot_settle_by_name.py [--write]
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts import ledger, pilot_ledger  # noqa: E402
from prop_alpha.alerts.ledger import PlacedBetStore  # noqa: E402
from prop_alpha.alerts.replies import PlacedBet, Settlement, settlement_text  # noqa: E402
from prop_alpha.config import DEFAULT_PILOT_DSN, PILOT_DSN_ENV  # noqa: E402
from prop_alpha.ingestion.oddspapi import normalise, team_tokens  # noqa: E402

# Paper bets placed before the pilot moved to TheStatsAPI carry Sportmonks ids (phase 2, 2026-10-15).
SWITCH: Final[datetime] = datetime(2026, 10, 15, tzinfo=timezone.utc)
# One row per player row of every match kicking off at one of the given times (a match with no player rows gives one row
# with the player columns NULL). The tackles column is the pilot's count (pilot_ledger.recount).
CANDIDATES_SQL: Final[str] = pilot_ledger.recount("""
    SELECT f.vendor_fixture_uid, f.status, f.kickoff_at, ht.name, at.name, p.full_name, s.minutes_played,
           s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.teams ht ON ht.team_id = f.home_team_id
    JOIN prop_alpha.teams at ON at.team_id = f.away_team_id
    LEFT JOIN prop_alpha.player_match_stats s ON s.fixture_id = f.fixture_id
    LEFT JOIN prop_alpha.players p ON p.player_id = s.player_id
    WHERE f.kickoff_at = ANY(%s)
""")
Candidate = tuple[str, str, datetime, str, str, str | None, int | None, int | None, int | None]
Row = tuple[str, int | None, int | None, int | None]


def player_name(bet: PlacedBet) -> str:
    """"Fernando (Bragantino)" -> "Fernando": the name the tip showed, without its club."""
    name, _, _ = bet.player.rpartition(" (")
    return name if name and bet.player.endswith(")") else bet.player


def match_rows(
    bets: Sequence[PlacedBet], tips: Mapping[int, Mapping[str, Any]], candidates: Sequence[Candidate],
) -> tuple[dict[tuple[int, int], Row], dict[int, str], list[str]]:
    """Pure: for each bet, its (fixture status, minutes, tackles, fouls) keyed as ledger.settle_open_bets reads them, the
    TheStatsAPI match uid it was found in, and a problem line for every bet not matched exactly once."""
    rows: dict[tuple[int, int], Row] = {}
    uids: dict[int, str] = {}
    problems: list[str] = []
    for bet in bets:
        tip = tips.get(bet.bet_id)
        if tip is None:
            problems.append(f"#{bet.bet_id}: no tip record, so no kickoff or clubs to match")
            continue
        kickoff = datetime.fromisoformat(str(tip["kickoff_at"]))
        home, away = team_tokens(str(tip["home_name"])), team_tokens(str(tip["away_name"]))
        found = {c[0] for c in candidates
                 if c[2] == kickoff and team_tokens(c[3]) & home and team_tokens(c[4]) & away}
        if len(found) != 1:
            problems.append(f"#{bet.bet_id}: {len(found)} matches for {tip['home_name']} v {tip['away_name']} at {kickoff:%d %b %H:%M}")
            continue
        uid = found.pop()
        wanted = normalise(player_name(bet))
        players = [c for c in candidates if c[0] == uid and c[5] is not None and normalise(c[5]) == wanted]
        if len(players) != 1:
            problems.append(f"#{bet.bet_id}: {len(players)} players named {player_name(bet)!r} in {uid}")
            continue
        _, status, _, _, _, _, minutes, tackles, fouls = players[0]
        rows[(bet.fixture_id, bet.player_id)] = (status, minutes, tackles, fouls)
        uids[bet.bet_id] = uid
    return rows, uids, problems


def read_candidates(dsn: str, kickoffs: Sequence[datetime]) -> list[Candidate]:
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=10000 -c default_transaction_read_only=on", connect_timeout=10)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(CANDIDATES_SQL, (list(kickoffs),))
            return [(str(r[0]), str(r[1]), r[2].astimezone(timezone.utc), str(r[3]), str(r[4]),
                     None if r[5] is None else str(r[5]), _int(r[6]), _int(r[7]), _int(r[8]))
                    for r in cur.fetchall()]
    finally:
        conn.close()


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--write", action="store_true", help="record the settlements (default: print them only)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    store = PlacedBetStore(pilot_ledger.PILOT_BETS_FILE)
    bets = [bet for bet in store.open_bets(now) if bet.placed_at < SWITCH]
    if not bets:
        print("no open pilot paper bet placed before the move to TheStatsAPI")
        return 0
    tips = pilot_ledger.load_tips(pilot_ledger.PILOT_TIPS_FILE)
    kickoffs = sorted({datetime.fromisoformat(str(tips[b.bet_id]["kickoff_at"])) for b in bets if b.bet_id in tips})
    rows, uids, problems = match_rows(bets, tips, read_candidates(os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN), kickoffs))
    settled: list[tuple[PlacedBet, Settlement]] = [
        (bet, replace(s, detail=f"{s.detail} (TheStatsAPI {uids[bet.bet_id]}, matched by club and player name)"))
        for bet, s in ledger.settle_open_bets(bets, rows, now)]
    for bet, settlement in settled:
        print(settlement_text(bet, settlement))
    for line in problems:
        print("LEFT OPEN " + line)
    if args.write and settled:
        with ledger.ledger_lock(pilot_ledger.lock_for(store)), ledger.ledger_lock(ledger.DEFAULT_LOCK_FILE):
            open_now = {bet.bet_id for bet in store.open_bets(now)}
            for bet, settlement in settled:
                if bet.bet_id in open_now:
                    store.record_settlement(settlement)
        print(f"recorded {len(settled)} settlement(s) in {pilot_ledger.PILOT_BETS_FILE.name}")
    elif settled:
        print("dry run: nothing recorded (--write records them)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
