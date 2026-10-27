"""Closing prices: each bookmaker's player tackles/fouls prices shortly before kickoff, for closing-line value (PRD
Gate 3) and the market benchmark (Gate 2 criterion 4). Reads prices only; it never places a bet.

    python -m prop_alpha.alerts.closing     # run every 10 minutes by scripts\\ledger_poll.cmd

The 4-hourly scan saves the fixtures of its window, and which bookmakers returned fixtures for it, in
.prop_alpha_state/scan_fixtures.json. This job looks there for fixtures kicking off within CLOSING_LEAD that have
not been captured, and only then spends OddsPapi requests: one per bookmaker that answered the scan, through the
scan's own collector (alerts/scan.py collect_odds: quota reserve, per-bookmaker isolation, name matching, raw-response
archive). Matched quotes for those fixtures are appended to closing_quotes.jsonl with the capture time, and the
fixtures are marked captured. A run that captures nothing because every bookmaker failed exits 1 and tries again at
the next poll while the fixture is still within CLOSING_LEAD.

"Closing" here is the last price seen 0-15 minutes before kickoff (the poll runs every 10 minutes), not the price at
the whistle. I/O: OddsPapi (only when a fixture is due), one read-only database transaction for the squads, and the
three gitignored state files named below.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from prop_alpha.config import DEFAULT_DSN, DSN_ENV
from prop_alpha.scanner.market_scanner import LiveOddsQuote
from prop_alpha.scanner.slate import UpcomingFixture

logger = logging.getLogger("prop_alpha.closing")

CLOSING_LEAD: Final[timedelta] = timedelta(minutes=15)
SCAN_FIXTURES_NAME: Final[str] = "scan_fixtures.json"
CLOSING_QUOTES_NAME: Final[str] = "closing_quotes.jsonl"
CAPTURED_NAME: Final[str] = "closing_captured.json"
# Saved scan fixtures are dropped this long after kickoff.
KEEP_AFTER_KICKOFF: Final[timedelta] = timedelta(days=2)


def _fixture_record(fixture: UpcomingFixture, books: Sequence[str]) -> dict[str, Any]:
    return {**asdict(fixture), "kickoff_at": fixture.kickoff_at.isoformat(), "books": sorted(books)}


def _fixture_from_record(raw: Mapping[str, Any]) -> tuple[UpcomingFixture, tuple[str, ...]]:
    fields = {key: raw[key] for key in ("fixture_id", "league", "home_team_uid", "away_team_uid", "home_name",
                                        "away_name")}
    fixture = UpcomingFixture(**fields, kickoff_at=datetime.fromisoformat(str(raw["kickoff_at"])))
    books = raw.get("books")
    if not isinstance(books, list) or not all(isinstance(book, str) for book in books):
        raise ValueError(f"saved scan fixture {fixture.fixture_id} has no bookmaker list")
    return fixture, tuple(books)


def save_scan_fixtures(state_dir: Path, fixtures: Sequence[UpcomingFixture], books: Sequence[str],
                       now: datetime) -> None:
    """Called by the real-odds scan: remember its fixtures and the bookmakers that returned fixtures for them."""
    path = state_dir / SCAN_FIXTURES_NAME
    saved = load_scan_fixtures(state_dir)
    for fixture in fixtures:
        saved[fixture.fixture_id] = (fixture, tuple(books))
    keep = {fid: entry for fid, entry in saved.items() if entry[0].kickoff_at > now - KEEP_AFTER_KICKOFF}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([_fixture_record(f, b) for f, b in keep.values()]), encoding="utf-8")


def load_scan_fixtures(state_dir: Path) -> dict[int, tuple[UpcomingFixture, tuple[str, ...]]]:
    path = state_dir / SCAN_FIXTURES_NAME
    if not path.is_file():
        return {}
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path.name} is not a list of fixtures")
    entries = [_fixture_from_record(record) for record in raw]
    return {fixture.fixture_id: (fixture, books) for fixture, books in entries}


def due_fixtures(
    saved: Mapping[int, tuple[UpcomingFixture, tuple[str, ...]]], captured: frozenset[int], now: datetime,
) -> list[tuple[UpcomingFixture, tuple[str, ...]]]:
    """Pure: saved fixtures kicking off in (now, now + CLOSING_LEAD], not yet captured, with a bookmaker to ask;
    earliest kickoff first."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    due = [(fixture, books) for fixture, books in saved.values()
           if now < fixture.kickoff_at <= now + CLOSING_LEAD and fixture.fixture_id not in captured and books]
    return sorted(due, key=lambda entry: (entry[0].kickoff_at, entry[0].fixture_id))


def _quote_record(quote: LiveOddsQuote, kickoff_at: datetime) -> dict[str, Any]:
    return {"fixture_id": quote.fixture_id, "player_id": quote.player_id, "market": quote.market_type,
            "line": quote.line_value, "book": quote.sportsbook, "over": quote.over_price, "under": quote.under_price,
            "captured_at": quote.captured_at.isoformat(), "kickoff_at": kickoff_at.isoformat()}


def load_closing_quotes(state_dir: Path) -> list[dict[str, Any]]:
    """Every captured closing quote. A corrupt line raises."""
    path = state_dir / CLOSING_QUOTES_NAME
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record: Any = json.loads(line)
        required = ("fixture_id", "player_id", "market", "line", "book", "over", "under", "captured_at")
        if not isinstance(record, dict) or any(key not in record for key in required):
            raise ValueError(f"{path.name} line {number} is not a closing quote")
        records.append(record)
    return records


def capture(now: datetime, odds_client: Any, dsn: str, state_dir: Path) -> tuple[int, int, list[str]]:
    """(fixtures captured, quotes written, summary notes). Spends no request when nothing is due."""
    import psycopg2

    from prop_alpha.alerts.ledger import ledger_lock
    from prop_alpha.alerts.scan import collect_odds, load_slate_inputs

    with ledger_lock(state_dir / "closing_capture.lock", wait=True):
        captured_path = state_dir / CAPTURED_NAME
        captured = set(int(fid) for fid in json.loads(captured_path.read_text(encoding="utf-8"))) \
            if captured_path.is_file() else set()
        # A crash after the quote append but before the handled-fixture file is saved must not duplicate the quotes.
        captured.update(int(record["fixture_id"]) for record in load_closing_quotes(state_dir))
        due = due_fixtures(load_scan_fixtures(state_dir), frozenset(captured), now)
        if not due:
            return 0, 0, []
        fixtures = [fixture for fixture, _ in due]
        books = sorted({book for _, entry_books in due for book in entry_books})
        conn = psycopg2.connect(dsn, options="-c statement_timeout=30000 -c default_transaction_read_only=on",
                                connect_timeout=10)
        try:
            with conn:
                with conn.cursor() as cur:
                    squads = {fixture.fixture_id: load_slate_inputs(cur, fixture).squad for fixture in fixtures}
        finally:
            conn.close()
        coverage, notes, available = collect_odds(odds_client, fixtures, squads, now, fixtures[-1].kickoff_at, books,
                                                  state_dir)
        if not available:
            raise RuntimeError("closing prices: no bookmaker answered: " + "; ".join(notes))
        kickoffs = {fixture.fixture_id: fixture.kickoff_at for fixture in fixtures}
        quotes = [quote for quote in coverage.quotes if quote.fixture_id in kickoffs]
        if not quotes:
            return 0, 0, notes
        captured_fixtures = {int(quote.fixture_id) for quote in quotes}
        path = state_dir / CLOSING_QUOTES_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for quote in quotes:
                handle.write(json.dumps(_quote_record(quote, kickoffs[quote.fixture_id])) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        captured_path.write_text(json.dumps(sorted(captured | captured_fixtures)), encoding="utf-8")
        return len(captured_fixtures), len(quotes), notes


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from prop_alpha.alerts.scan import STATE_DIR
    from prop_alpha.ingestion.pipeline import build_odds_client

    now = datetime.now(timezone.utc)
    captured = frozenset(int(fid) for fid in json.loads((STATE_DIR / CAPTURED_NAME).read_text(encoding="utf-8"))) \
        if (STATE_DIR / CAPTURED_NAME).is_file() else frozenset()
    if not due_fixtures(load_scan_fixtures(STATE_DIR), captured, now):
        return 0  # nothing kicks off soon: no key needed, no request spent
    odds_client = build_odds_client(os.environ)
    try:
        fixtures, quotes, notes = capture(now, odds_client, os.environ.get(DSN_ENV, DEFAULT_DSN), STATE_DIR)
    finally:
        odds_client.close()
    print(f"closing prices: {fixtures} fixtures, {quotes} quotes captured")
    for note in notes:
        print(f"  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
