"""Periodic Telegram status report (scheduled every 4 hours). Sends text only; it never places a bet.

Reads the database (one short read-only transaction), the sent-alert, alert-delivery and placed-bet state files
and the clock,
and calls no vendor API, so it spends no data quota. Open bets older than ledger.STALE_AFTER are flagged. A database that cannot be reached is reported in the message
(sanctioned handler H8) rather than silencing the report.

    python -m prop_alpha.alerts.status            # print the report
    python -m prop_alpha.alerts.status --send     # print and send it to the Telegram chat
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Final, Sequence

from prop_alpha.alerts.fx import MAX_RATE_AGE, ExchangeRate, cached_rate, is_fresh
from prop_alpha.alerts.closing import load_closing_quotes
from prop_alpha.alerts.ledger import STALE_AFTER, PlacedBetStore, closing_values, results_summary
from prop_alpha.alerts.telegram import PAPER_LABEL, DeliveryLog, SentAlertStore, TelegramNotifier, load_config
from prop_alpha.config import DEFAULT_DSN, DSN_ENV

logger = logging.getLogger("prop_alpha.status")

STATUS_NOTE: Final[str] = (
    "No PRD go-live gate passed. Bets are settled from Sportmonks under assumed market rules (provisional); closing "
    "prices are captured 0-15 min before kickoff; the database bet ledger is not built."
)
REPORT_INTERVAL: Final[timedelta] = timedelta(hours=4)
# Sportmonks plan seen on 2026-10-02: "Starter - Trialing until 2026-10-16".
SPORTMONKS_TRIAL_ENDS: Final[date] = date(2026, 10, 16)

SNAPSHOT_SQL: Final[str] = """
    SELECT (SELECT count(*) FROM prop_alpha.fixtures),
           (SELECT count(*) FROM prop_alpha.fixtures WHERE status = 'final'),
           (SELECT max(ingested_at) FROM prop_alpha.fixtures),
           (SELECT count(*) FROM prop_alpha.player_match_stats),
           (SELECT count(*) FROM prop_alpha.staging_player_feed),
           (SELECT count(*) FROM prop_alpha.staging_rejects WHERE rejected_at > now() - interval '24 hours'),
           (SELECT count(*) FROM prop_alpha.raw_payloads),
           (SELECT count(*) FROM prop_alpha.incomplete_team_matches)
"""


@dataclass(frozen=True)
class DatabaseSnapshot:
    fixtures: int
    final_fixtures: int
    last_merge: datetime | None
    player_rows: int
    staged_rows: int
    rejects_24h: int
    raw_payloads: int
    incomplete_team_matches: int = 0


def read_snapshot(dsn: str) -> DatabaseSnapshot:
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=10000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(SNAPSHOT_SQL)
                row = cur.fetchone()
                if row is None:
                    raise ValueError("status snapshot query returned no row")
    finally:
        conn.close()
    if row is None:
        raise ValueError("status snapshot query returned no row")
    fixtures, final_fixtures, last_merge, player_rows, staged, rejects, raw, incomplete = row
    return DatabaseSnapshot(int(fixtures), int(final_fixtures), last_merge, int(player_rows), int(staged),
                            int(rejects), int(raw), int(incomplete))


def database_section(dsn: str) -> list[str]:
    import psycopg2

    try:
        snapshot = read_snapshot(dsn)
    except psycopg2.Error as exc:
        logger.error("status report: database unreachable: %s", type(exc).__name__)
        return [f"Database: UNREACHABLE ({type(exc).__name__}). Check the postgresql-x64-18 service."]
    last = f"{snapshot.last_merge:%Y-%m-%d %H:%M} UTC" if snapshot.last_merge is not None else "never"
    lines = [
        f"Database: {snapshot.fixtures} fixtures ({snapshot.final_fixtures} final), {snapshot.player_rows} player rows",
        f"Last merge: {last}; raw payloads stored: {snapshot.raw_payloads}",
        f"Incomplete club-matches (a player without an ID played; left out of pricing): "
        f"{snapshot.incomplete_team_matches}",
    ]
    if snapshot.staged_rows:
        lines.append(f"WARNING: {snapshot.staged_rows} rows waiting in staging (merge failed or not run)")
    if snapshot.rejects_24h:
        lines.append(f"WARNING: {snapshot.rejects_24h} rows rejected in the last 24h (see staging_rejects)")
    return lines


def bet_section(bets: PlacedBetStore, now: datetime) -> list[str]:
    """Open bets, the stale ones to settle by hand, and the settled results so far."""
    open_bets = bets.open_bets(now)
    lines = [f"Open bets: {len(open_bets)} (stake {sum(bet.stake for bet in open_bets):.2f})"]
    stale = bets.stale_bets(now)
    if stale:
        lines.append(f"WARNING: {len(stale)} bet(s) open more than {STALE_AFTER.days} days "
                     f"({', '.join(f'#{bet.bet_id}' for bet in stale)}): reply settled <bet id> won|lost|void")
    settled = bets.settled_bets()
    if settled:
        lines.append(results_summary(settled, closing_values(settled, load_closing_quotes(bets.path.parent))))
    return lines


def delivery_section(deliveries: DeliveryLog, now: datetime) -> list[str]:
    """Alert messages whose delivery outcome is unknown (readiness item 6): each may or may not be in the chat."""
    unknown = deliveries.needs_review(now)
    if not unknown:
        return []
    return [f"WARNING: {len(unknown)} alert message(s) with an unknown delivery outcome "
            f"({', '.join(f'#{d.delivery_id}' for d in unknown)}): python -m prop_alpha.alerts.delivery"]


def fx_section(rate: ExchangeRate | None, now: datetime) -> list[str]:
    """The cached book-currency rate, flagged when the scan would refuse it as too old."""
    if rate is None:
        return []
    if is_fresh(rate, now.date()):
        return [f"Exchange rate: {rate.describe()}"]
    return [f"WARNING: exchange rate {rate.describe()} is older than {MAX_RATE_AGE.days} days; scans refuse to run "
            "until python -m prop_alpha.alerts.fx --refresh succeeds"]


def format_report(now: datetime, database_lines: list[str], alerts_in_window: int, open_exposure: float,
                  bet_lines: Sequence[str] = ()) -> str:
    days_left = (SPORTMONKS_TRIAL_ENDS - now.date()).days
    trial = (
        f"Sportmonks trial: {days_left} days left (ends {SPORTMONKS_TRIAL_ENDS:%d %b})" if days_left > 0
        else f"WARNING: Sportmonks trial ended {SPORTMONKS_TRIAL_ENDS:%d %b}; ingestion will fail without a plan"
    )
    return "\n".join([
        f"prop_alpha status {now:%Y-%m-%d %H:%M} UTC",
        *database_lines,
        f"Alerts in the last {int(REPORT_INTERVAL.total_seconds() // 3600)}h: {alerts_in_window}; "
        f"open alerted exposure: {open_exposure:.2f}",
        *bet_lines,
        trial,
        STATUS_NOTE,
        PAPER_LABEL,
    ])


def build_report(now: datetime, dsn: str, store: SentAlertStore, bets: PlacedBetStore | None = None,
                 rate: ExchangeRate | None = None, deliveries: DeliveryLog | None = None) -> str:
    alerts = [alert for alert in store.load() if alert.sent_at > now - REPORT_INTERVAL]
    return format_report(now, database_section(dsn), len(alerts), store.exposure(now).total,
                         [*(bet_section(bets, now) if bets is not None else ()),
                          *(delivery_section(deliveries, now) if deliveries is not None else ()),
                          *fx_section(rate, now)])


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Send the prop_alpha status report to Telegram.")
    parser.add_argument("--send", action="store_true", help="send the report (default: print only)")
    args = parser.parse_args(argv)
    report = build_report(datetime.now(timezone.utc), os.environ.get(DSN_ENV, DEFAULT_DSN), SentAlertStore(),
                          PlacedBetStore(), cached_rate(), DeliveryLog())
    print(report)
    if args.send:
        message_id = TelegramNotifier(load_config()).send_message(report)
        print(f"sent (message_id {message_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
