"""Operator review of alert deliveries (production-readiness item 6, 2026-10-05). Sends text only; it never places a bet.

Every alert, digest and board message is recorded in telegram.DeliveryLog before it is sent and confirmed with
Telegram's message id after it. A message whose send failed, timed out or was cut off by a stopped process has no
confirmation: it may or may not be in the chat, and nothing re-sends it automatically, because a second copy could
double a bet. This tool lets the operator settle each one:

    python -m prop_alpha.alerts.delivery                       # list deliveries with an unknown outcome
    python -m prop_alpha.alerts.delivery --all                 # every delivery and its state
    python -m prop_alpha.alerts.delivery --mark-delivered 12   # it is in the chat: confirm it
    python -m prop_alpha.alerts.delivery --resend 12           # it is not in the chat: send it again, labelled
    python -m prop_alpha.alerts.delivery --dismiss 12          # leave it unsent (e.g. its bets have expired)
    python -m prop_alpha.alerts.delivery --mock ...            # the same for mock-odds runs

A re-sent copy follows a separate RESENT_HEADER message (sent on its own, so the copy keeps the original's length
and still fits one Telegram message) and is indexed like the original, so a bet-recording reply to it works. Only an
unknown delivery can be re-sent, and only until its valid_until (the last bet's expiry, or a board's kickoff).
Re-sending never changes exposure: the alert store already counts every alerted stake, sent or not.

I/O: the delivery, message-index and mock state files under .prop_alpha_state/, and Telegram (re-send only).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Final, Sequence

from prop_alpha.alerts.scan import MOCK_DELIVERY_FILE, MOCK_INDEX_FILE
from prop_alpha.alerts.telegram import (
    Delivery,
    DeliveryLog,
    MessageIndex,
    TelegramNotifier,
    load_config,
)

logger = logging.getLogger("prop_alpha.delivery")

RESENT_HEADER: Final[str] = (
    "RESENT - the next message repeats one whose first send was not confirmed. If you already have it, it is the "
    "same message with the same bet numbers: do not bet twice."
)


def describe(delivery: Delivery) -> str:
    """One line per delivery for the operator: id, state, kind, when, validity and its first line of content."""
    headline = next((line for line in delivery.text.splitlines()
                     if line.strip() and not line.startswith(("PAPER ONLY", "MOCK ODDS"))), "")
    state = delivery.state.upper() + (f" ({delivery.resolution})" if delivery.resolution else "")
    message = f", message {delivery.message_id}" if delivery.message_id is not None else ""
    return (f"#{delivery.delivery_id} {state}{message}: {delivery.kind} with {len(delivery.items)} bet(s), began "
            f"{delivery.began_at:%d %b %H:%M:%S} UTC, valid until {delivery.valid_until:%d %b %H:%M} UTC - {headline}")


def review_lines(log: DeliveryLog, now: datetime, everything: bool = False) -> list[str]:
    deliveries = sorted(log.load(now).values(), key=lambda d: d.delivery_id)
    shown = deliveries if everything else [d for d in deliveries if d.state == "unknown"]
    lines = [describe(d) for d in shown]
    unknown = sum(1 for d in deliveries if d.state == "unknown")
    lines.append(f"{len(deliveries)} deliveries recorded; {unknown} with an unknown outcome"
                 + (": check the chat, then --mark-delivered, --resend or --dismiss each one" if unknown else ""))
    return lines


def _unknown(log: DeliveryLog, delivery_id: int, now: datetime) -> Delivery:
    delivery = log.load(now).get(delivery_id)
    if delivery is None:
        raise ValueError(f"no delivery #{delivery_id} in {log.path.name}")
    if delivery.state != "unknown":
        raise ValueError(f"delivery #{delivery_id} is {delivery.state}; only an unknown outcome can be resolved")
    return delivery


def mark_delivered(log: DeliveryLog, delivery_id: int, now: datetime) -> None:
    """The operator found the message in the chat."""
    _unknown(log, delivery_id, now)
    log.resolve(delivery_id, "confirmed_by_operator", now)


def dismiss(log: DeliveryLog, delivery_id: int, now: datetime) -> None:
    """The operator decided not to send it (not in the chat, and no longer wanted)."""
    _unknown(log, delivery_id, now)
    log.resolve(delivery_id, "dismissed", now)


def resend(notifier: TelegramNotifier, log: DeliveryLog, delivery_id: int, now: datetime) -> int:
    """Send an unknown delivery again under RESENT_HEADER, confirm it and index its bets; returns the message id.
    Refused once the delivery is past its valid_until: its bets have expired."""
    delivery = _unknown(log, delivery_id, now)
    if now >= delivery.valid_until:
        raise ValueError(f"delivery #{delivery_id} expired at {delivery.valid_until:%d %b %H:%M} UTC; "
                         "use --dismiss instead")
    notifier.send_message(RESENT_HEADER)
    message_id = notifier.send_message(delivery.text)
    # Index before confirming, as TelegramNotifier._send_and_confirm does: a copy that takes no reply is worse than
    # one still listed for review.
    if notifier.index is not None and delivery.items:
        notifier.index.record(message_id, delivery.items, datetime.now(timezone.utc))
    log.resolve(delivery_id, "resent", datetime.now(timezone.utc), message_id)
    return message_id


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Review alert deliveries whose outcome is unknown.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--all", action="store_true", help="list every delivery, not only the unknown ones")
    action.add_argument("--mark-delivered", type=int, metavar="ID", help="the message is in the chat")
    action.add_argument("--resend", type=int, metavar="ID", help="send the message again, labelled RESENT")
    action.add_argument("--dismiss", type=int, metavar="ID", help="do not send the message")
    parser.add_argument("--mock", action="store_true", help="act on the mock-odds delivery record")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    log = DeliveryLog(MOCK_DELIVERY_FILE) if args.mock else DeliveryLog()
    if args.mark_delivered is not None:
        mark_delivered(log, args.mark_delivered, now)
        print(f"delivery #{args.mark_delivered} confirmed by the operator")
    elif args.dismiss is not None:
        dismiss(log, args.dismiss, now)
        print(f"delivery #{args.dismiss} dismissed: it will not be sent")
    elif args.resend is not None:
        index = MessageIndex(MOCK_INDEX_FILE) if args.mock else MessageIndex()
        message_id = resend(TelegramNotifier(load_config(), index=index), log, args.resend, now)
        print(f"delivery #{args.resend} re-sent (message_id {message_id})")
    else:
        print("\n".join(review_lines(log, now, everything=args.all)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
