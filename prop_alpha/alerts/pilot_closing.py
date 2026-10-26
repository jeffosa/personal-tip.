"""Closing-price capture for the pilot scan's paper bets (audit 2026-10-11, finding 3).

    python -m prop_alpha.alerts.pilot_closing     # run by the ledger task every 10 minutes; a no-op when nothing is due

Every pilot tip is a paper bet (alerts/pilot_ledger.py). For each fixture with an open paper bet that kicks off within
CLOSING_WINDOW, this reads the bookmakers' prices once (alerts/ladder_scan.collect_pilot_odds: one OddsPapi request per
bookmaker the tips were made at, the account endpoint first, the same quota reserve) and appends the price of every
selection that was tipped (same fixture, player, market and line, at every book that listed it) to
`pilot_closing_quotes.jsonl`. `pilot_ledger --list` then reports each paper bet's closing-price value (taken odds / closing
odds - 1). Since 2026-10-25 real-price tips are withheld (alerts/market_contract.py), so it also captures the closing prices of
the pilot's shadow picks (alerts/shadow_picks.py: the withheld picks, kept unsent), whose closing-price value
scripts/value_evidence.py reports; without them this capture would never run again. A fixture is handled once, whether or not a price was found, so the quota is not spent twice; a run that could
not fetch odds (quota reserve) leaves it for the next poll.

The League One/Two closing capture (alerts/closing.py) is separate and untouched.

I/O: the pilot paper-bet, tip and closing files, and OddsPapi through ladder_scan. No exception handler (AGENT.md 1.3).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Final, Mapping, Sequence

from prop_alpha.alerts import ladder_scan, pilot_ledger, scan
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.replies import PlacedBet
from prop_alpha.alerts.shadow_picks import PILOT_SHADOW_FILE, ShadowPickLog
from prop_alpha.scanner.slate import SquadPlayer, UpcomingFixture

logger = logging.getLogger("prop_alpha.pilot_closing")

CLOSING_WINDOW: Final[timedelta] = timedelta(minutes=15)
HANDLED_FILE: Final[Path] = pilot_ledger.STATE_DIR / "pilot_closing_handled.json"
_CLUB_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\s*\([^)]*\)\s*$")


def due_fixtures(
    open_bets: Sequence[PlacedBet], tips: Mapping[int, Mapping[str, object]], handled: Collection[int], now: datetime,
    shadow: Sequence[Mapping[str, Any]] = (),
) -> list[UpcomingFixture]:
    """Pure: the fixtures with an open paper bet or a shadow pick that kick off in (now, now + CLOSING_WINDOW] and are not
    yet handled, earliest first. A bet without a tip line has no fixture to look up and is skipped."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    found: dict[int, UpcomingFixture] = {}
    records = [*(tips[bet.bet_id] for bet in open_bets if bet.bet_id in tips), *shadow]
    for record in records:
        fixture = pilot_ledger.fixture_of(record)
        if fixture.fixture_id in handled or fixture.fixture_id in found:
            continue
        if now < fixture.kickoff_at <= now + CLOSING_WINDOW:
            found[fixture.fixture_id] = fixture
    return sorted(found.values(), key=lambda f: (f.kickoff_at, f.fixture_id))


def squads_of(bets: Sequence[PlacedBet], shadow: Sequence[Mapping[str, Any]] = ()) -> dict[int, list[SquadPlayer]]:
    """Pure: per fixture, a squad of just the tipped and shadow-picked players (their plain names), enough to match a
    bookmaker's names."""
    squads: dict[int, dict[int, SquadPlayer]] = {}
    for bet in bets:
        name = _CLUB_SUFFIX.sub("", bet.player)
        squads.setdefault(bet.fixture_id, {})[bet.player_id] = SquadPlayer(bet.player_id, name, "MID", "")
    for pick in shadow:
        fixture_id, player_id = int(pick["fixture_id"]), int(pick["player_id"])
        squads.setdefault(fixture_id, {}).setdefault(player_id, SquadPlayer(player_id, str(pick["player"]), "MID", ""))
    return {fixture_id: list(players.values()) for fixture_id, players in squads.items()}


def load_handled(path: Path) -> frozenset[int]:
    return frozenset(int(f) for f in json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else frozenset()


def capture(
    now: datetime, odds_client: object, store: PlacedBetStore, state_dir: Path = pilot_ledger.STATE_DIR,
    tips_file: Path = pilot_ledger.PILOT_TIPS_FILE, closing_file: Path = pilot_ledger.PILOT_CLOSING_FILE,
    handled_file: Path = HANDLED_FILE, shadow_log: ShadowPickLog | None = None,
) -> tuple[int, list[str], bool]:
    """(closing prices written, notes, whether odds were available). A run with nothing due makes no request. With
    `shadow_log` (the scheduled run passes the pilot's) its shadow picks' fixtures are due as well."""
    open_bets = store.open_bets(now)
    shadow = shadow_log.load() if shadow_log is not None else []
    due = due_fixtures(open_bets, pilot_ledger.load_tips(tips_file), load_handled(handled_file), now, shadow)
    if not due:
        return 0, [], True
    due_ids = {fixture.fixture_id for fixture in due}
    tipped = [bet for bet in open_bets if bet.fixture_id in due_ids]
    picks = [pick for pick in shadow if int(pick["fixture_id"]) in due_ids]
    books = sorted({bet.book for bet in tipped} | {str(pick["book"]) for pick in picks})
    coverage, notes, available = ladder_scan.collect_pilot_odds(
        odds_client, due, squads_of(tipped, picks), now, now + CLOSING_WINDOW, books, state_dir)
    if not available:
        return 0, notes, False
    wanted = ({(bet.fixture_id, bet.player_id, bet.market, bet.line) for bet in tipped}
              | {(int(p["fixture_id"]), int(p["player_id"]), str(p["market"]), float(p["line"])) for p in picks})
    written = 0
    closing_file.parent.mkdir(parents=True, exist_ok=True)
    with closing_file.open("a", encoding="utf-8") as handle:
        for quote in coverage.quotes:
            if (quote.fixture_id, quote.player_id, quote.market_type, quote.line_value) not in wanted:
                continue
            handle.write(json.dumps({
                "captured_at": now.isoformat(), "fixture_id": quote.fixture_id, "player_id": quote.player_id,
                "market": quote.market_type, "line": quote.line_value, "book": quote.sportsbook,
                "over": quote.over_price, "under": quote.under_price,
            }) + "\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    handled_file.write_text(json.dumps(sorted(load_handled(handled_file) | due_ids)), encoding="utf-8")
    return written, [*notes, f"closing prices: {written} for {len(due)} fixture(s)"], True


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from prop_alpha.ingestion.pipeline import build_odds_client

    now = datetime.now(timezone.utc)
    store = PlacedBetStore(pilot_ledger.PILOT_BETS_FILE)
    # No open paper bet or shadow pick kicking off soon: nothing to do, and no client is built.
    if not due_fixtures(store.open_bets(now), pilot_ledger.load_tips(pilot_ledger.PILOT_TIPS_FILE),
                        load_handled(HANDLED_FILE), now, ShadowPickLog(PILOT_SHADOW_FILE).load()):
        return 0
    client = build_odds_client(os.environ)
    try:
        _, notes, available = capture(now, client, store, shadow_log=ShadowPickLog(PILOT_SHADOW_FILE))
    finally:
        client.close()
    print("\n".join(notes))
    return 0 if available else scan.ODDS_UNAVAILABLE_EXIT


if __name__ == "__main__":
    sys.exit(main())
