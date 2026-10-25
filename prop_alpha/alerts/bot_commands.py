"""Answers to the bot commands sent in the Telegram chat (maintainer request, 2026-10-15): /help, /status, /bets, /pilot.

Read-only: an answer reads the same state the command-line tools print (alerts/status.py, `ledger --list`,
`pilot_ledger --list`) and changes nothing. Nothing here places, records or settles a bet. The 10-minute ledger poll
(`python -m prop_alpha.alerts.ledger --poll`) collects the commands from TELEGRAM_CHAT_ID only and answers them after it
has saved its offset, so a command is answered within about 10 minutes, and an answer that fails is not retried forever.

I/O: the state files through their stores, and the status report's own read-only database query (alerts/status.py).
No exception handler (AGENT.md 1.3).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Final

from prop_alpha.alerts import ledger, pilot_ledger
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.replies import settlement_text

HELP_TEXT: Final[str] = "\n".join([
    "Commands (read-only; answered by the next ledger poll, within about 10 minutes):",
    "/status - the status report: database, alerts, open bets, data trial, pilot",
    "/bets - your recorded bets: open ones and settled results",
    "/pilot - the pilot's paper tips: open ones and settled results",
    "/help - this list",
    "To record a bet, reply to the alert itself: placed 3 @1.95 5 (also undo 12, settled 12 won|lost|void).",
    "To record the bookmaker's result for a pilot paper tip: result 3 won [book] (tip numbers: /pilot).",
])
# The newest settled bets listed one by one; older ones are only in the totals.
SETTLED_SHOWN: Final[int] = 10


def answer(command: str, now: datetime, state_dir: Path = ledger.STATE_DIR) -> str:
    """The chat answer to `/command` (as ledger.bot_command_name gives it) at `now`."""
    if command in ("help", "start"):
        return HELP_TEXT
    if command == "status":
        return status_text(now, state_dir)
    if command == "bets":
        return bets_text(PlacedBetStore(state_dir / ledger.DEFAULT_BETS_FILE.name), now, state_dir)
    if command == "pilot":
        return pilot_text(PlacedBetStore(state_dir / pilot_ledger.PILOT_BETS_FILE.name), now, state_dir)
    return f"Unknown command /{command}.\n{HELP_TEXT}"


def status_text(now: datetime, state_dir: Path) -> str:
    """The status report, as `python -m prop_alpha.alerts.status` prints it."""
    from prop_alpha.alerts import status
    from prop_alpha.alerts.fx import cached_rate
    from prop_alpha.alerts.telegram import DeliveryLog, SentAlertStore
    from prop_alpha.config import DEFAULT_DSN, DSN_ENV

    return status.build_report(
        now, os.environ.get(DSN_ENV, DEFAULT_DSN), SentAlertStore(), PlacedBetStore(state_dir / ledger.DEFAULT_BETS_FILE.name),
        cached_rate(), DeliveryLog(), (state_dir / ledger.DEFAULT_HEARTBEAT_FILE.name, ledger.AnswerOutbox()), state_dir,
        pilot_state=state_dir)


def bets_text(store: PlacedBetStore, now: datetime, state_dir: Path) -> str:
    """Open recorded bets and the settled results, as `ledger --list` gives them."""
    from prop_alpha.alerts.closing import load_closing_quotes

    open_bets = store.open_bets(now)
    stale = {bet.bet_id for bet in store.stale_bets(now)}
    lines = [f"BETS: {len(open_bets)} open (stake {sum(b.stake for b in open_bets):g})"]
    lines += [f"#{bet.bet_id} {bet.player} | {bet.side.upper()} {bet.line:g} {bet.market} @ {bet.odds:.2f} "
              f"stake {bet.stake:g} ({bet.book}) | {bet.match}" + ("  STALE: settle by hand" if bet.bet_id in stale else "")
              for bet in open_bets]
    settled = store.settled_bets()
    if not settled:
        return "\n".join([*lines, "No settled bet yet."])
    closing = ledger.closing_values(settled, load_closing_quotes(state_dir))
    lines += [settlement_text(bet, settlement, closing.get(bet.bet_id)) for bet, settlement in settled[-SETTLED_SHOWN:]]
    return "\n".join([*lines, ledger.results_summary(settled, closing)])


def pilot_text(store: PlacedBetStore, now: datetime, state_dir: Path) -> str:
    """The pilot's paper tips, open and settled, as `pilot_ledger --list` gives them."""
    open_bets = store.open_bets(now)
    lines = [f"PILOT PAPER TIPS (never real bets): {len(open_bets)} open"]
    lines += [f"#{bet.bet_id} {bet.player} | {bet.side.upper()} {bet.line:g} {bet.market} @ {bet.odds:.2f} "
              f"stake {bet.stake:g} ({bet.book}) | {bet.match}" for bet in open_bets]
    settled = store.settled_bets()
    if not settled:
        return "\n".join([*lines, "No settled paper tip yet."])
    lines += [settlement_text(bet, settlement) for bet, settlement in settled[-SETTLED_SHOWN:]]
    closing = pilot_ledger.load_closing(state_dir / pilot_ledger.PILOT_CLOSING_FILE.name)
    reconstructed = pilot_ledger.reconstructed_ids(pilot_ledger.load_tips(state_dir / pilot_ledger.PILOT_TIPS_FILE.name))
    return "\n".join([*lines, pilot_ledger.results_text(settled, closing, reconstructed)])
