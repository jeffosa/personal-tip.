"""Pilot paper ledger: the evidence trail for the pilot scan's tips (alerts/ladder_scan.py).

Audit 2026-10-11 (findings 3 and 4): pilot tips were sent with no record of what was tipped, no settlement and no closing
price, so they could never show whether the model, or the assumed bookmaker settlement rules, were right. Every pilot tip is
now a PAPER bet: `record_tips` (called just before each tip message is sent) writes it to `pilot_paper_bets.jsonl`, with the
odds, book and the stake the system suggested, and a line to `pilot_tips.jsonl` with the match, the league and the
model's numbers. Nothing here is a bet the maintainer placed: the real ledger (alerts/ledger.py) and its exposure never
see these files. The paper P&L is "had the tip been taken at the alerted price and stake".

    python -m prop_alpha.alerts.pilot_ledger --settle [--send]   # grade open paper bets from the pilot database
    python -m prop_alpha.alerts.pilot_ledger --list              # open and settled paper bets, with closing-price value
    python -m prop_alpha.alerts.pilot_ledger --record 3 won [--book sportybet]   # the bookmaker's result for paper bet 3
    (or reply "result 3 won [book]" in the Telegram chat: the 10-minute poll records it the same way, since 2026-10-25)
    python -m prop_alpha.alerts.pilot_ledger --reconcile         # your recorded results v automatic settlement; exit 1 on a discrepancy

Automatic settlement is the assumed market contract (tackles = PILOT_TACKLES_SQL, the count the pilot prices: Sportmonks
`tackles_attempted` again since 2026-10-13; fouls = `fouls_committed`, void if
the player does not play, a whole-line count is a push), read from `prop_alpha_pilot`. It is provisional until bookmaker results
agree: a `--record` result is the bookmaker's, and `--reconcile` says AGREE or DISCREPANCY for each. Only the AGREE count says anything
about whether the contract is right (alerts/closing is the League One/Two equivalent). Closing prices come from
alerts/pilot_closing.py; with a one-sided ladder price the value is price-based: taken odds / closing odds - 1.

I/O: the paper-bet, tip and closing files, one read-only pilot-database transaction (through alerts/ledger), and Telegram for
`--send`. No exception handler (AGENT.md 1.3).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Final, Mapping, Sequence

from prop_alpha.alerts import ledger
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.replies import (
    RESULTS,
    PlacedBet,
    Settlement,
    profit,
    reconciliation_summary,
    reconciliation_text,
    settlement_text,
)
from prop_alpha.alerts.telegram import TelegramNotifier, TipLine, load_config
from prop_alpha.config import DEFAULT_PILOT_DSN, PILOT_DSN_ENV
from prop_alpha.scanner.slate import UpcomingFixture

# The pilot's tackles count: plain `tackles_attempted` (Opta's "Tackles"), as the frozen League One/Two model (maintainer,
# 2026-10-13). From 2026-10-12 to 2026-10-13 it was tackles attempted plus challenges lost, because the Brazil Serie A ladders
# price a count about 45% above tackles_attempted and that one fitted their level and shape best (MARKET_CONTRACT.md 2b). The
# first settled results that tell the counts apart contradict it (section 2e): SportyBet settled Marcinho 2+ LOST with 0 tackles
# and 3 challenges lost, and Wallace Yan 2+ LOST with 1 tackle and 1 interception, so that book counts neither challenges lost
# nor interceptions, and the price gap reads as margin. The pilot prices AND settles on this count. It stays a setting (recount,
# ladder_scan.PilotCursor) so a confirmed bet365 rule can be adopted in one line.
PILOT_TACKLES_SQL: Final[str] = "s.tackles_attempted"
# The count of 2026-10-12, kept for the diagnostics that compare the two (scripts/pilot_gap_diagnosis.py).
TACKLES_PLUS_CHALLENGES_LOST_SQL: Final[str] = "(s.tackles_attempted + s.dribbled_past)"


def recount(sql: str, count: str = PILOT_TACKLES_SQL) -> str:
    """`sql` with its one `s.tackles_attempted` replaced by `count`; anything else is refused, so a change to the query it
    copies cannot silently leave the pilot on another count."""
    if sql.count("s.tackles_attempted") != 1:
        raise ValueError(f"expected exactly one s.tackles_attempted in the query, found {sql.count('s.tackles_attempted')}")
    return sql.replace("s.tackles_attempted", count)


PILOT_SETTLEMENT_SQL: Final[str] = recount(ledger.SETTLEMENT_SQL)

logger = logging.getLogger("prop_alpha.pilot_ledger")

STATE_DIR: Final[Path] = ledger.STATE_DIR
PILOT_BETS_FILE: Final[Path] = STATE_DIR / "pilot_paper_bets.jsonl"
PILOT_TIPS_FILE: Final[Path] = STATE_DIR / "pilot_tips.jsonl"
PILOT_CLOSING_FILE: Final[Path] = STATE_DIR / "pilot_closing_quotes.jsonl"
SETTLED_HEADER: Final[str] = "PILOT PAPER TIPS SETTLED (provisional: assumed market rules; these were never bets you placed)"
TIP_FIELDS: Final[tuple[str, ...]] = (
    "bet_id", "fixture_id", "league", "home_team_uid", "away_team_uid", "home_name", "away_name", "kickoff_at",
)


def lock_for(store: PlacedBetStore) -> Path:
    """The lock every writer of this paper-bet store holds: the scan recording tips and the settlement job (one file per store,
    so a test store never touches the real one's lock)."""
    return store.path.with_name(store.path.stem + ".lock")


def selection(bet: PlacedBet) -> tuple[int, int, str, float, str, str]:
    """What makes two paper bets the same tip: fixture, player, market, line, side and book."""
    return bet.fixture_id, bet.player_id, bet.market, bet.line, bet.side, bet.book.lower()


def paper_bet(tip: TipLine, bet_id: int, now: datetime) -> PlacedBet:
    """Pure: the paper bet for one tip, at the alerted odds, book and suggested stake (message id 0: not known before sending)."""
    directive = tip.directive
    return PlacedBet(
        bet_id=bet_id, update_id=bet_id, message_id=0, number=tip.number, kind="alert", fixture_id=directive.fixture_id,
        player_id=directive.player_id, market=directive.market_type, line=directive.line_value,
        side=directive.side.lower(), odds=directive.decimal_odds, stake=directive.stake, book=directive.sportsbook,
        min_odds=directive.min_acceptable_odds,
        match=tip.context.match if tip.context else f"Fixture {directive.fixture_id}",
        player=tip.context.player if tip.context else f"player {directive.player_id}", placed_at=now,
    )


def tip_record(tip: TipLine, bet: PlacedBet, fixture: UpcomingFixture) -> dict[str, Any]:
    """Pure: the tip's line in pilot_tips.jsonl: the fixture (to find its closing price later) and the model's numbers."""
    assessment = tip.assessment
    return {
        "bet_id": bet.bet_id, "fixture_id": fixture.fixture_id, "league": fixture.league,
        "home_team_uid": fixture.home_team_uid, "away_team_uid": fixture.away_team_uid, "home_name": fixture.home_name,
        "away_name": fixture.away_name, "kickoff_at": fixture.kickoff_at.isoformat(), "label": tip.label,
        "recorded_at": bet.placed_at.isoformat(),
        "survival_rate": None if assessment is None else assessment.survival_rate,
        "model_win_probability": None if assessment is None else assessment.model_win_probability,
        "book_win_probability": None if assessment is None else assessment.book_win_probability,
        "ev_per_unit": None if assessment is None else assessment.ev_per_unit,
    }


def record_tips(
    store: PlacedBetStore, tips_file: Path, tips: Sequence[TipLine], fixtures: Mapping[int, UpcomingFixture], now: datetime,
) -> list[PlacedBet]:
    """Record each tip as a paper bet and a tip line, under the store's lock (a second writer fails at once rather than
    interleave). A tip already recorded (same selection, not undone) is skipped, so an overlapping or retried run never
    records it twice. The tip line is written before the bet: a crash between them leaves a tip line with no bet, which
    nothing reads, never a bet whose match cannot be found. A tip whose fixture is unknown raises before anything is written."""
    missing = sorted({tip.directive.fixture_id for tip in tips if tip.directive.fixture_id not in fixtures})
    if missing:
        raise ValueError(f"tip for fixture {missing[0]} has no fixture to settle against")
    recorded: list[PlacedBet] = []
    with ledger.ledger_lock(lock_for(store)):
        bets, cancelled, _, _ = store.load_all()
        known = {selection(bet) for bet_id, bet in bets.items() if bet_id not in cancelled}
        next_id = max(bets, default=0) + 1
        for tip in tips:
            bet = paper_bet(tip, next_id, now)
            if selection(bet) in known:
                continue
            tips_file.parent.mkdir(parents=True, exist_ok=True)
            with tips_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(tip_record(tip, bet, fixtures[tip.directive.fixture_id])) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            store.record_bet(bet)
            known.add(selection(bet))
            recorded.append(bet)
            next_id += 1
    return recorded


def load_tips(path: Path) -> dict[int, dict[str, Any]]:
    """bet id -> tip line. A damaged line raises: an unreadable record is not guessed."""
    tips: dict[int, dict[str, Any]] = {}
    if not path.is_file():
        return tips
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record: Any = json.loads(line)
        if not isinstance(record, dict) or any(field not in record for field in TIP_FIELDS):
            raise ValueError(f"{path.name} line {number} is not a pilot tip record")
        tips[int(record["bet_id"])] = record
    return tips


def fixture_of(record: Mapping[str, Any]) -> UpcomingFixture:
    return UpcomingFixture(
        int(record["fixture_id"]), str(record["league"]), str(record["home_team_uid"]), str(record["away_team_uid"]),
        str(record["home_name"]), str(record["away_name"]), datetime.fromisoformat(str(record["kickoff_at"])),
    )


def load_closing(path: Path) -> list[dict[str, Any]]:
    """Every captured closing price: fixture_id, player_id, market, line, book, over, under, captured_at."""
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record: Any = json.loads(line)
        if not isinstance(record, dict) or any(k not in record for k in ("fixture_id", "player_id", "market", "line", "book", "captured_at")):
            raise ValueError(f"{path.name} line {number} is not a closing price")
        records.append(record)
    return records


def closing_price(bet: PlacedBet, closing: Sequence[Mapping[str, Any]]) -> tuple[float, str] | None:
    """Pure: the closing price of the bet's own selection (same fixture, player, market, line and side), preferring the
    book it was tipped at, then the latest capture; None when none was captured."""
    side = "over" if bet.side == "over" else "under"
    candidates = [q for q in closing
                  if (int(q["fixture_id"]), int(q["player_id"]), str(q["market"]), float(q["line"]))
                  == (bet.fixture_id, bet.player_id, bet.market, bet.line) and q.get(side) is not None]
    if not candidates:
        return None
    best = max(candidates, key=lambda q: (str(q["book"]).lower() == bet.book.lower(), str(q["captured_at"])))
    return float(best[side]), str(best["book"])


def price_clv(taken_odds: float, closing_odds: float) -> float:
    """Closing-price value of a one-sided price: taken odds / closing odds - 1 (positive: the price taken beat the close)."""
    if not (taken_odds > 1.0 and closing_odds > 1.0):
        raise ValueError("odds must be above 1")
    return taken_odds / closing_odds - 1.0


def clv_summary(bets: Sequence[PlacedBet], closing: Sequence[Mapping[str, Any]]) -> str:
    values = [price_clv(bet.odds, found[0]) for bet in bets if (found := closing_price(bet, closing)) is not None]
    if not values:
        return "no closing price captured yet"
    return f"mean closing-price value {sum(values) / len(values):+.1%} over {len(values)} paper bet(s) with a closing price"


def reconstructed_ids(tips: Mapping[int, Mapping[str, Any]]) -> set[int]:
    """Paper bets rebuilt after the fact (the 8 tips of 2026-10-11 sent before this ledger existed): they have no model numbers."""
    return {bet_id for bet_id, record in tips.items() if record.get("reconstructed")}


def results_text(settled: Sequence[tuple[PlacedBet, Settlement]], closing: Sequence[Mapping[str, Any]],
                 reconstructed: Collection[int] = ()) -> str:
    """Count, stake, paper profit and return on stake of the settled paper bets, the confirmed-only split and the CLV. Bets
    in `reconstructed` are counted in the paper totals but kept out of a separate model-scored total (no model numbers)."""
    staked = sum(bet.stake for bet, _ in settled)
    total = sum(profit(bet, s) for bet, s in settled)
    by_result = {result: sum(1 for _, s in settled if s.result == result) for result in RESULTS}
    roi = f", return on stake {total / staked:+.1%}" if staked > 0 else ""
    confirmed = [(bet, s) for bet, s in settled if s.source != "auto"]
    return (f"{len(settled)} settled paper bet(s) ({by_result['won']} won, {by_result['lost']} lost, {by_result['void']} void): "
            f"staked {staked:.2f}, paper profit {total:+.2f}{roi}; {len(settled) - len(confirmed)} provisional (automatic, assumed "
            f"market rules), {len(confirmed)} with a bookmaker result you recorded; "
            f"{clv_summary([bet for bet, _ in settled], closing)}" + _model_scored(settled, reconstructed))


def _model_scored(settled: Sequence[tuple[PlacedBet, Settlement]], reconstructed: Collection[int]) -> str:
    rebuilt = [bet for bet, _ in settled if bet.bet_id in reconstructed]
    if not rebuilt:
        return ""
    scored = [(bet, s) for bet, s in settled if bet.bet_id not in reconstructed]
    return (f"; {len(rebuilt)} reconstructed (no model numbers, excluded from model scoring): model-scored "
            f"{len(scored)} settled, paper profit {sum(profit(bet, s) for bet, s in scored):+.2f}")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Paper ledger for the pilot scan's tips (never real bets).")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--settle", action="store_true", help="grade open paper bets whose match is final in the pilot database")
    action.add_argument("--list", action="store_true", help="print the open and settled paper bets")
    action.add_argument("--record", nargs=2, metavar=("BET_ID", "RESULT"), help="the bookmaker's result: won, lost or void")
    action.add_argument("--reconcile", action="store_true", help="compare recorded bookmaker results with automatic settlement")
    parser.add_argument("--send", action="store_true", help="with --settle: send the new results to Telegram")
    parser.add_argument("--book", help="with --record: the bookmaker the result was read from (default: not given)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    store = PlacedBetStore(PILOT_BETS_FILE)
    dsn = os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN)
    closing = load_closing(PILOT_CLOSING_FILE)
    if args.list:
        for bet in store.open_bets(now):
            found = closing_price(bet, closing)
            print(f"#{bet.bet_id} {bet.placed_at:%d %b %H:%M} {bet.match} | {bet.player} | {bet.side.upper()} {bet.line:g} "
                  f"{bet.market} @ {bet.odds:.2f} stake {bet.stake:g} ({bet.book})"
                  + ("" if found is None else f" | close {found[0]:.2f} ({price_clv(bet.odds, found[0]):+.1%})"))
        settled = store.settled_bets()
        for bet, settlement in settled:
            print(settlement_text(bet, settlement))
        print(results_text(settled, closing, reconstructed_ids(load_tips(PILOT_TIPS_FILE))))
        return 0
    if args.record:
        bet_id, result = int(args.record[0]), str(args.record[1]).lower()
        if result not in RESULTS:
            parser.error(f"RESULT must be one of {', '.join(RESULTS)}")
        bets, cancelled, _, _ = store.load_all()
        if bet_id not in bets:
            parser.error(f"no paper bet #{bet_id}")
        # update_id 0: the settlement is the maintainer's, not a Telegram reply (a manual result needs one).
        book = args.book or "book not given"
        store.record_settlement(Settlement(bet_id, result, "manual", f"bookmaker result ({book}) recorded by hand", now,
                                           update_id=0))
        print(f"recorded {result.upper()} for paper bet #{bet_id} as the bookmaker's result ({book})")
        return 0
    if args.reconcile:
        items = ledger.reconcile_settlements(store, dsn, now, PILOT_SETTLEMENT_SQL)
        lines = [reconciliation_text(bet, item) for bet, item in items if item.outcome != "unconfirmed"]
        print("\n".join([*lines, reconciliation_summary([item for _, item in items])]))
        return 1 if any(item.outcome == "discrepancy" for _, item in items) else 0
    # The real ledger's lock as well: the notice goes through its answer queue (ledger_answers.jsonl), which only one writer
    # may touch at a time.
    # Keep the same global lock order as ledger.poll: real ledger first, then pilot store. The poll records Telegram
    # pilot results while holding the real-ledger lock, so reversing the order here could deadlock concurrent runs.
    with ledger.ledger_lock(ledger.DEFAULT_LOCK_FILE), ledger.ledger_lock(lock_for(store)):
        settled = ledger.settle(store, dsn, now, PILOT_SETTLEMENT_SQL)
        lines = [settlement_text(bet, settlement) for bet, settlement in settled]
        print("\n".join(lines) if lines else "no open paper bet has a final match in the pilot database")
        if args.send and lines:
            notice = "\n".join([SETTLED_HEADER, *lines, results_text(store.settled_bets(), closing,
                                                                      reconstructed_ids(load_tips(PILOT_TIPS_FILE)))])
            # Queued before it is sent (audit 2026-10-11): the settlements are already recorded, so a failed send used to be
            # lost for good; the 10-minute ledger poll re-sends a queued notice ("Delayed") up to MAX_ANSWER_ATTEMPTS times.
            ledger.send_queued_notice(TelegramNotifier(load_config()), ledger.AnswerOutbox(ledger.DEFAULT_ANSWERS_FILE),
                                      notice, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
