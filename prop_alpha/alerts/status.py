"""Periodic Telegram status report (scheduled every 4 hours). Sends text only; it never places a bet.

Reads the database (one short read-only transaction), the sent-alert, alert-delivery and placed-bet state files,
the archived OddsPapi replies (to count the tackles/fouls prices seen so far, audit F-02) and the clock, and calls no
vendor API, so it spends no data quota. Open bets older than ledger.STALE_AFTER are flagged. A database that cannot be reached is reported in the message
(sanctioned handler H8) rather than silencing the report.

    python -m prop_alpha.alerts.status            # print the report
    python -m prop_alpha.alerts.status --send     # print and send it to the Telegram chat
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from prop_alpha.alerts.fx import MAX_RATE_AGE, ExchangeRate, cached_rate, is_fresh
from prop_alpha.alerts.closing import load_closing_quotes
from prop_alpha.alerts.ledger import (
    DEFAULT_HEARTBEAT_FILE,
    MAX_ANSWER_ATTEMPTS,
    STALE_AFTER,
    AnswerOutbox,
    PlacedBetStore,
    closing_values,
    last_poll,
    results_summary,
)
from prop_alpha.alerts.scan import CATALOGUE_FILE, ODDS_ARCHIVE_DIR, ODDS_CACHE_DIR, STATE_DIR, load_catalogue
from prop_alpha.alerts.telegram import PAPER_LABEL, DeliveryLog, SentAlertStore, TelegramNotifier, load_config
from prop_alpha.config import DEFAULT_DSN, DSN_ENV
from prop_alpha.ingestion import oddspapi

logger = logging.getLogger("prop_alpha.status")

STATUS_NOTE: Final[str] = (
    "No PRD go-live gate passed. Bets are settled from Sportmonks under assumed market rules (provisional); closing "
    "prices are captured 0-15 min before kickoff; the database bet ledger is not built."
)
REPORT_INTERVAL: Final[timedelta] = timedelta(hours=4)
# The bet-reply poll runs every 10 minutes; an hour without one means the task is not running.
POLL_STALE_AFTER: Final[timedelta] = timedelta(hours=1)
# Telegram keeps an update the bot has not read for 24 hours, then drops it: a reply older than that is lost.
TELEGRAM_UPDATE_RETENTION: Final[timedelta] = timedelta(hours=24)
# Sportmonks plan seen on 2026-10-02: "Starter - Trialing until 2026-10-16".
SPORTMONKS_TRIAL_ENDS: Final[date] = date(2026, 10, 16)
# An archived OddsPapi reply as alerts/scan.py names it: <run time>_<bookmaker>.json.gz. A malformed reply is archived
# as <run time>_<bookmaker>.rejected.json.gz and is not counted.
ARCHIVED_REPLY: Final[re.Pattern[str]] = re.compile(r"([0-9]{8}T[0-9]{6}Z)_([a-z0-9]+)\.json\.gz")
REJECTED_REPLY_SUFFIX: Final[str] = ".rejected.json.gz"
QUOTE_MARKETS: Final[tuple[str, ...]] = tuple(sorted(set(oddspapi.MARKET_TYPES.values())))

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


@dataclass(frozen=True)
class QuoteCensus:
    """Player tackles/fouls prices in every archived OddsPapi reply (audit F-02: no target-market quote seen yet).

    A price is one (bookmaker, OddsPapi fixture, market, line, side, player); the same price in several replies is
    counted once. Counted from the whole reply, so prices outside the scan's window and players we could not match
    are included: this measures what the feed offers, not what the scan could use."""

    replies: int
    rejected: int
    first_reply: datetime | None
    last_reply: datetime | None
    prices: Mapping[str, int]
    fixtures: Mapping[str, int]
    first_seen: Mapping[str, tuple[datetime, str]]

    def __post_init__(self) -> None:
        if self.replies < 0 or self.rejected < 0:
            raise ValueError("reply counts must not be negative")
        if (self.first_reply is None) != (self.replies == 0) or (self.last_reply is None) != (self.replies == 0):
            raise ValueError("first and last reply times are set exactly when a reply was counted")
        if set(self.prices) != set(QUOTE_MARKETS) or set(self.fixtures) != set(QUOTE_MARKETS):
            raise ValueError(f"price and fixture counts must cover exactly {QUOTE_MARKETS}")
        for market in QUOTE_MARKETS:
            if self.prices[market] < 0 or not 0 <= self.fixtures[market] <= self.prices[market]:
                raise ValueError(f"{market}: inconsistent price and fixture counts")
            if (market in self.first_seen) != (self.prices[market] > 0):
                raise ValueError(f"{market}: first sighting is set exactly when a price was counted")


def _reply_time(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _archived_reply(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def quote_census(archive: Path, catalogue: Mapping[int, oddspapi.LineMarket]) -> QuoteCensus:
    """Re-parses every archived reply with `catalogue` (oddspapi.prop_prices). Each was parsed when it was archived,
    so a reply that now raises ValueError, or an unreadable file, has been damaged since: it raises, and the status
    run fails and is reported by task_health rather than counted as zero."""
    names = sorted(path.name for path in archive.iterdir()) if archive.is_dir() else []
    replies = [(_reply_time(match.group(1)), match.group(2), archive / name)
               for name in names if (match := ARCHIVED_REPLY.fullmatch(name)) is not None]
    seen: set[tuple[str, str, str, float, str, str]] = set()
    fixtures: dict[str, set[tuple[str, str]]] = {market: set() for market in QUOTE_MARKETS}
    first_seen: dict[str, tuple[datetime, str]] = {}
    for at, book, path in replies:
        for price in oddspapi.prop_prices(_archived_reply(path), catalogue, book):
            seen.add((book, price.fixture.odds_fixture_id, price.market, price.line, price.side, price.player_ref))
            fixtures[price.market].add((book, price.fixture.odds_fixture_id))
            first_seen.setdefault(price.market, (at, book))
    times = [at for at, _, _ in replies]
    return QuoteCensus(
        replies=len(replies), rejected=sum(1 for name in names if name.endswith(REJECTED_REPLY_SUFFIX)),
        first_reply=min(times, default=None), last_reply=max(times, default=None),
        prices={market: sum(1 for key in seen if key[2] == market) for market in QUOTE_MARKETS},
        fixtures={market: len(fixtures[market]) for market in QUOTE_MARKETS}, first_seen=first_seen,
    )


def quote_section(state_dir: Path) -> list[str]:
    """One line on the tackles/fouls prices OddsPapi has sent so far (audit F-02)."""
    archive = state_dir / ODDS_ARCHIVE_DIR
    catalogue_file = state_dir / ODDS_CACHE_DIR / CATALOGUE_FILE
    if not catalogue_file.is_file():
        if archive.is_dir() and any(ARCHIVED_REPLY.fullmatch(path.name) for path in archive.iterdir()):
            return ["WARNING: tackles/fouls quotes not counted: OddsPapi replies are archived but the market "
                    f"catalogue ({ODDS_CACHE_DIR}/{CATALOGUE_FILE}) is missing; the next real-odds scan restores it"]
        return ["Tackles/fouls quotes (F-02): no OddsPapi reply archived yet"]
    census = quote_census(archive, load_catalogue(catalogue_file))
    if census.first_reply is None or census.last_reply is None:
        line = "Tackles/fouls quotes (F-02): no OddsPapi reply archived yet"
    else:
        span = (f"{census.replies} archived OddsPapi {'reply' if census.replies == 1 else 'replies'}, "
                f"{census.first_reply:%d %b}-{census.last_reply:%d %b}")
        if not any(census.prices.values()):
            line = f"F-02 open: no tackles/fouls price in {span}"
        else:
            counts = "; ".join(
                f"{market} {census.prices[market]} prices in {census.fixtures[market]} bookmaker-matches"
                + (f", first {census.first_seen[market][0]:%d %b %H:%M} UTC ({census.first_seen[market][1]})"
                   if market in census.first_seen else "")
                for market in QUOTE_MARKETS)
            line = f"Tackles/fouls quotes seen (F-02), {span}: {counts}"
    if census.rejected:
        line += f"; {census.rejected} malformed replies not counted"
    return [line]


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


def poll_section(heartbeat_file: Path, outbox: AnswerOutbox, now: datetime) -> list[str]:
    """Whether bet replies are being read, and answers to them that could not be sent (delivery audit 2026-10-05)."""
    lines: list[str] = []
    last = last_poll(heartbeat_file)
    if last is None:
        lines.append("WARNING: no bet-reply poll has been recorded yet (task prop_alpha\\Bet ledger poll)")
    elif now - last > POLL_STALE_AFTER:
        line = (f"WARNING: bet replies last read {(now - last).total_seconds() / 3600:.1f}h ago "
                f"({last:%d %b %H:%M} UTC); the poll should run every 10 minutes")
        if now - last > TELEGRAM_UPDATE_RETENTION:
            line += (". Telegram drops a reply it has kept unread for 24 hours: re-send any placed reply sent "
                     f"before {now - TELEGRAM_UPDATE_RETENTION:%d %b %H:%M} UTC")
        lines.append(line)
    abandoned = outbox.abandoned()
    if abandoned:
        lines.append(f"WARNING: {len(abandoned)} answer(s) to your bet replies failed {MAX_ANSWER_ATTEMPTS} sends "
                     "and were not delivered (settlement notices included): python -m prop_alpha.alerts.ledger --list "
                     "shows them")
    return lines


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
                 rate: ExchangeRate | None = None, deliveries: DeliveryLog | None = None,
                 poll_state: tuple[Path, AnswerOutbox] | None = None, odds_state: Path | None = None) -> str:
    alerts = [alert for alert in store.load() if alert.sent_at > now - REPORT_INTERVAL]
    return format_report(now, database_section(dsn), len(alerts), store.exposure(now).total,
                         [*(bet_section(bets, now) if bets is not None else ()),
                          *(delivery_section(deliveries, now) if deliveries is not None else ()),
                          *(poll_section(*poll_state, now) if poll_state is not None else ()),
                          *(quote_section(odds_state) if odds_state is not None else ()),
                          *fx_section(rate, now)])


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Send the prop_alpha status report to Telegram.")
    parser.add_argument("--send", action="store_true", help="send the report (default: print only)")
    args = parser.parse_args(argv)
    report = build_report(datetime.now(timezone.utc), os.environ.get(DSN_ENV, DEFAULT_DSN), SentAlertStore(),
                          PlacedBetStore(), cached_rate(), DeliveryLog(), (DEFAULT_HEARTBEAT_FILE, AnswerOutbox()),
                          STATE_DIR)
    print(report)
    if args.send:
        message_id = TelegramNotifier(load_config()).send_message(report)
        print(f"sent (message_id {message_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
