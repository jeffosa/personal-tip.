"""Championship ladder scan (pilot): the 4-hourly opportunity scan for the English Championship, whose bookmakers
list tackles and fouls as "k+" ladders ("to make k or more") instead of over/under lines. It never places a bet.

    python -m prop_alpha.alerts.ladder_scan --odds oddspapi --send      # real bet365 ladders (OddsPapi)
    python -m prop_alpha.alerts.ladder_scan --odds mock                 # print what would be sent
    python -m prop_alpha.alerts.ladder_scan --odds mock --at 2026-10-13T16:30:00+00:00   # simulate a run time

It is the League One/Two scan (alerts/scan.py) with four differences, and nothing else:

* Pricing is the Championship pilot's, read from the pilot database `prop_alpha_pilot` (read-only), never from the
  working `prop_alpha`: game-state k 21.1 for tackles and 1000 for fouls, prior strength 6, no rate multiplier (the
  settings of the 2026-10-08 Championship backtest, scripts/pilot_price_match.py). These are NOT the frozen League
  One/Two settings and are not frozen themselves: the model_freeze fingerprint and the League One/Two lockbox are
  untouched, and no Championship lockbox exists.
* Odds are ladders (oddspapi.ladder_prices). A "k+" step is the Over of line k - 0.5 on its own; the "0" step is the
  Under of 0.5. They go through the same quote building, allocator and high-value screen as the other scan.
* Every message says it is a pilot. The settlement statistic and void rule of bet365's tackles and fouls markets are
  NOT verified (MARKET_CONTRACT.md), and the first comparisons found bet365 pricing events well above the model's
  plain counts; the screen cannot detect that, so a tip here is a model-versus-price gap and nothing more.
* Tips are recorded in pilot state files (`pilot_*.jsonl`), never the real alert store, message index or ledger: a
  reply to a pilot message is not recorded as a bet, and the exposure of pilot tips is its own.

The scan refuses to run (ValueError) when a finished Championship match of the last STALE_AFTER is missing from the
pilot database: the history it prices from would silently lack it. Refresh it first with
`scripts/pilot_backfill.py --list --ingest --fix-leagues`.

I/O: Sportmonks listings through VendorClient, one read-only pilot-database transaction, the pilot state files, the
OddsPapi cache and archive under .prop_alpha_state/, and Telegram. There is no exception handler here (AGENT.md 1.3):
an odds or listing failure ends the run with its error, and the per-fixture pricing failure is recorded by
scan.scan_fixture (H9).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Final, Mapping, Sequence

import numpy as np

from prop_alpha.alerts import scan
from prop_alpha.alerts.fx import BOOK_CURRENCY, SUPPORTED_CURRENCIES, ExchangeRate, bankroll_currency, book_to_bankroll
from prop_alpha.alerts.fx import identity as fx_identity
from prop_alpha.alerts.telegram import (
    PAPER_LABEL,
    AlertContext,
    DeliveryLog,
    MessageIndex,
    SentAlertStore,
    TelegramNotifier,
    load_config,
)
from prop_alpha.ingestion import oddspapi, sportmonks
from prop_alpha.modeling.engine import FactorProjection, QuantEngine
from prop_alpha.scanner.market_scanner import (
    MODEL_RISK_LOSS_FLOOR,
    AllocationReport,
    InstitutionalAssetManager,
    LiveOddsQuote,
    ProjectionKey,
)
from prop_alpha.scanner.slate import (
    FIXTURE_KICKOFF_TOLERANCE,
    OddsCoverage,
    SquadPlayer,
    UpcomingFixture,
    build_projections,
    mock_quotes,
    quotes_from_prices,
    scan_window,
)

logger = logging.getLogger("prop_alpha.ladder_scan")

LEAGUE: Final[str] = "Championship"
PILOT_DSN_ENV: Final[str] = "PROP_ALPHA_PILOT_DSN"
DEFAULT_PILOT_DSN: Final[str] = "postgresql://postgres@localhost:5432/prop_alpha_pilot"
# The Championship pilot's pricing settings (2026-10-08 backtest, fitted on Championship data before 2026-03-01).
DEFAULT_GAME_STATE_K: Final[float] = 6.0
GAME_STATE_K: Final[Mapping[str, float]] = {"tackles": 21.1, "fouls": 1000.0}
PRIOR_STRENGTH: Final[Mapping[str, float]] = {"tackles": 6.0, "fouls": 6.0}
# Only tackles are tipped, as in the League One/Two scan; fouls are priced but never staked or tipped (the pilot's
# fouls calibration slopes were 0.86 and 0.89 against the 0.9-1.1 the PRD requires).
ALERT_MARKETS: Final[frozenset[str]] = frozenset({"tackles"})
BOOKMAKERS_ENV: Final[str] = "LADDER_BOOKMAKERS"
DEFAULT_BOOKMAKERS: Final[tuple[str, ...]] = ("bet365",)
# Finished Championship matches this old (and not within STALE_GRACE of now, which ingestion may not have reached
# yet) must be in the pilot database before the scan prices anything.
STALE_AFTER: Final[timedelta] = timedelta(days=10)
STALE_GRACE: Final[timedelta] = timedelta(hours=4)
# Championship matches Sportmonks refuses to load (vendor data errors, 2026-10-08): never in the pilot database, so
# never counted as missing. 19427700 and 19431984: goal events do not reproduce the score; 19729100: duels won above
# duels; 19427583: card counts out of range.
KNOWN_VENDOR_ERRORS: Final[frozenset[str]] = frozenset({"19427700", "19431984", "19729100", "19427583"})
PILOT_LABEL: Final[str] = (
    "CHAMPIONSHIP PILOT - model not validated for this league; bet365's tackle and foul rules are unverified and its "
    "prices have run above the model's plain counts. Token stakes only."
)
PILOT_USAGE: Final[str] = (
    "Pilot tips are not recorded by the bet ledger: a reply to this message is ignored. Track any bet yourself."
)
PILOT_STATE_FILE: Final[Path] = scan.STATE_DIR / "pilot_sent_alerts.jsonl"
PILOT_INDEX_FILE: Final[Path] = scan.STATE_DIR / "pilot_alert_messages.jsonl"
PILOT_DELIVERY_FILE: Final[Path] = scan.STATE_DIR / "pilot_alert_deliveries.jsonl"
PILOT_ODDS_DIR: Final[str] = "pilot"
LADDER_CATALOGUE_FILE: Final[str] = "ladder_markets.json"


def championship_engines() -> tuple[QuantEngine, dict[str, QuantEngine]]:
    """The pilot's engines: the default and one per market (GAME_STATE_K)."""
    return QuantEngine(game_state_k=DEFAULT_GAME_STATE_K), {m: QuantEngine(game_state_k=k) for m, k in GAME_STATE_K.items()}


def price_championship(
    fixture: UpcomingFixture, inputs: scan.SlateInputs, engine: QuantEngine,
    market_engines: Mapping[str, QuantEngine] | None = None,
) -> tuple[dict[ProjectionKey, FactorProjection], list[str]]:
    """The model's projections for one Championship fixture with the pilot's settings (scan.scan_fixture's `pricer`)."""
    return build_projections(
        fixture, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures, inputs.league_rates, engine,
        season_levels=inputs.season_levels or None, market_engines=market_engines, prior_strengths=PRIOR_STRENGTH,
    )


def missing_results(
    finished: Sequence[tuple[str, datetime]], stored: Collection[str], now: datetime,
) -> list[tuple[str, datetime]]:
    """Pure: the finished matches in `finished` ((Sportmonks fixture id, kickoff)) that are not in `stored`, oldest
    first), leaving out KNOWN_VENDOR_ERRORS and those that kicked off within STALE_GRACE of `now` (ingestion may not have
    reached them)."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    return sorted(((uid, kickoff) for uid, kickoff in finished
                   if uid not in stored and uid not in KNOWN_VENDOR_ERRORS and kickoff <= now - STALE_GRACE),
                  key=lambda item: item[1])


def stale_message(missing: Sequence[tuple[str, datetime]]) -> str:
    shown = ", ".join(f"{uid} ({kickoff:%d %b})" for uid, kickoff in missing[:5])
    return (f"{len(missing)} finished Championship match(es) of the last {STALE_AFTER.days} days are not in the pilot "
            f"database (e.g. {shown}): its history would be out of date. Refresh it with "
            "`scripts/pilot_backfill.py --list --ingest --fix-leagues`, then run the scan again.")


def finished_listing(client: Any, now: datetime) -> list[tuple[str, datetime]]:
    """Finished Championship matches from the last STALE_AFTER, from the Sportmonks listing (at most MAX_LISTING_PAGES)."""
    finished: list[tuple[str, datetime]] = []
    for page in range(1, scan.MAX_LISTING_PAGES + 1):
        body = client.get_json(sportmonks.upcoming_path(now - STALE_AFTER, now, page, sportmonks.CHAMPIONSHIP_LEAGUES))
        rows, has_more = sportmonks.finished_fixtures(body, sportmonks.CHAMPIONSHIP_LEAGUES)
        finished += rows
        if not has_more:
            return finished
    raise ValueError(f"finished Championship listing has more than {scan.MAX_LISTING_PAGES} pages; check refused")


def load_ladder_catalogue(path: Path) -> dict[int, oddspapi.LadderMarket]:
    """The cached ladder catalogue. A damaged cache raises ValueError: delete the file to refetch."""
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(m, dict) and isinstance(m.get("market"), str) and isinstance(m.get("steps"), dict)
        for m in raw.values()
    ):
        raise ValueError(f"{path} is not a ladder catalogue; delete it so the next run fetches a fresh one")
    return {
        int(mid): oddspapi.LadderMarket(
            m["market"], {int(o): (float(s[0]), str(s[1]), str(s[2])) for o, s in m["steps"].items()})
        for mid, m in raw.items()
    }


def _save_ladder_catalogue(path: Path, catalogue: Mapping[int, oddspapi.LadderMarket]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        str(mid): {"market": m.market, "steps": {str(o): list(step) for o, step in m.steps.items()}}
        for mid, m in catalogue.items()
    }), encoding="utf-8")


def collect_ladder_odds(
    client: Any, fixtures: Sequence[UpcomingFixture], squads: Mapping[int, Sequence[SquadPlayer]],
    now: datetime, window_end: datetime, books: Sequence[str], state_dir: Path,
) -> tuple[OddsCoverage, list[str], bool]:
    """Real ladder quotes for `fixtures` from OddsPapi, summary lines, and whether real odds were available (False when
    the quota reserve stopped the fetch). Same quota rule as scan.collect_odds: the unmetered account endpoint is read
    first and odds are skipped when fewer than ODDS_QUOTA_RESERVE requests would remain. A bookmaker that reports no
    fixtures is recorded; any other failure, or a reply the ladder parser does not understand, raises. Every raw reply is
    archived under pilot/ as <stamp>_<book>_championship.json.gz."""
    empty = OddsCoverage((), 0, 0, (), ())
    cache = state_dir / scan.ODDS_CACHE_DIR
    catalogue_file, names_file = cache / LADDER_CATALOGUE_FILE, cache / "participants.json"
    catalogue_fresh = catalogue_file.is_file() and (
        datetime.now(timezone.utc) - datetime.fromtimestamp(catalogue_file.stat().st_mtime, timezone.utc)
        < scan.CATALOGUE_MAX_AGE
    )
    left = scan._requests_left(client.get_json_value(oddspapi.ACCOUNT_PATH))  # noqa: SLF001 - the scan's own reader
    needed = len(books) + (0 if catalogue_fresh else 1) + (0 if names_file.is_file() else 1)
    if left - needed < scan.ODDS_QUOTA_RESERVE:
        return empty, [f"Odds NOT fetched: OddsPapi has {left} requests left; this run needs {needed} and "
                       f"{scan.ODDS_QUOTA_RESERVE} are kept in reserve"], False
    if catalogue_fresh:
        catalogue = load_ladder_catalogue(catalogue_file)
    else:
        catalogue = oddspapi.ladder_catalogue(client.get_json_value(oddspapi.MARKETS_PATH))
        _save_ladder_catalogue(catalogue_file, catalogue)
    tournaments = sorted({oddspapi.LADDER_TOURNAMENTS[f.league] for f in fixtures if f.league in oddspapi.LADDER_TOURNAMENTS})
    archive = state_dir / PILOT_ODDS_DIR
    archive.mkdir(parents=True, exist_ok=True)
    window_start = (fixtures[0].kickoff_at if fixtures else now) - FIXTURE_KICKOFF_TOLERANCE
    prices: list[oddspapi.LadderPrice] = []
    no_odds: list[str] = []
    for book in books:
        body: Any = client.get_json_value(oddspapi.odds_path(book, tournaments), empty_on=oddspapi.NO_FIXTURES_REPLIES)
        if body is None:
            no_odds.append(book)
            continue
        with gzip.open(archive / f"{now:%Y%m%dT%H%M%SZ}_{book}_championship.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(body, handle)
        prices += [p for p in oddspapi.ladder_prices(body, catalogue, book)
                   if window_start <= p.fixture.kickoff_at < window_end + FIXTURE_KICKOFF_TOLERANCE]
    names = scan._load_names(names_file) if names_file.is_file() else {}  # noqa: SLF001
    if any(team not in names for p in prices for team in (p.fixture.home_id, p.fixture.away_id)):
        names = oddspapi.participant_names(client.get_json_value(oddspapi.PARTICIPANTS_PATH))
        cache.mkdir(parents=True, exist_ok=True)
        names_file.write_text(json.dumps({str(k): v for k, v in names.items()}), encoding="utf-8")
    coverage = quotes_from_prices(oddspapi.ladder_as_prop_prices(prices), names, fixtures, squads, now)
    notes = [
        f"Odds: OddsPapi ({', '.join(books)}): {coverage.prices} Championship tackles/fouls ladder prices in this window, "
        f"{coverage.matched_prices} matched to our players, {len(coverage.quotes)} lines; "
        f"about {left - client.requests_sent + 1} OddsPapi requests left",
    ]
    if no_odds:
        notes.append(f"No odds for the Championship from: {', '.join(no_odds)}")
    if coverage.unmatched_fixtures:
        notes.append(f"Odds for matches we could not match: {'; '.join(coverage.unmatched_fixtures)}")
    if coverage.unmatched_players:
        notes.append(f"Players we could not match: {len(coverage.unmatched_players)} "
                     f"(e.g. {'; '.join(coverage.unmatched_players[:3])})")
    return coverage, notes, True


def summary_text(summary: scan.ScanSummary, label: str) -> str:
    """The run's one-message summary (not scan.ScanSummary.text, whose wording is the League One/Two scan's)."""
    lines = [
        *((label,) if label else ()),
        f"Championship pilot scan {summary.window_start:%a %d %b %H:%M}-{summary.window_end:%H:%M} UTC",
        f"Matches in this window: {len(summary.fixtures)}",
    ]
    if summary.fixtures:
        lines.append(f"Players priced: {summary.players_priced} | quotes checked: {summary.quotes} | "
                     f"tips sent: {summary.alerts_sent}")
        if summary.bankroll:
            lines.append(f"Bankroll: {summary.bankroll}")
        if summary.alert_cap is not None and summary.alerts_sent >= summary.alert_cap:
            lines.append(f"Tip cap of {summary.alert_cap} per run reached: the best by expected profit were sent")
        if summary.unquoted_matches:
            lines.append(f"Matches with no bookmaker ladder (nothing to qualify against): {summary.unquoted_matches}")
        lines += summary.odds_notes
        lines.append(f"Tipped markets: {', '.join(sorted(ALERT_MARKETS))} (fouls are priced, never tipped: the pilot's "
                     "fouls calibration was below the PRD's)")
    lines += [f"Could not price {failure}" for failure in summary.failures]
    lines.append(PAPER_LABEL)
    return "\n".join(lines)


def run_ladder_scan(
    now: datetime, client: Any, dsn: str, notifier: TelegramNotifier | None, store: SentAlertStore,
    bankroll: float, label: str, max_alerts: int = scan.DEFAULT_MAX_ALERTS, *,
    odds: str = "mock", odds_client: Any | None = None, books: Sequence[str] = DEFAULT_BOOKMAKERS,
    state_dir: Path = scan.STATE_DIR, currency: str, book_rate: ExchangeRate | None = None,
) -> scan.ScanSummary:
    """One scan of the Championship matches kicking off in [now, next 4-hour boundary). `store` is the pilot (or mock)
    alert store: exposure is net of its stakes only. Raises ValueError when the pilot database is stale."""
    import psycopg2

    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"currency {currency!r} is not supported")
    if book_rate is None:
        if currency != BOOK_CURRENCY:
            raise ValueError(f"a {currency} bankroll needs a {BOOK_CURRENCY}->{currency} rate")
        book_rate = fx_identity(currency)
    if book_rate.quote != currency or book_rate.base not in (BOOK_CURRENCY, currency):
        raise ValueError(f"rate {book_rate.describe()} does not convert {BOOK_CURRENCY} into {currency}")
    rate = book_rate
    start, end = scan_window(now)
    conversion = "" if rate.identity else f"; book limits converted at {rate.describe()}"
    summary = scan.ScanSummary(start, end, alert_cap=max_alerts, bankroll=f"{currency} {bankroll:,.2f}{conversion}")
    fixtures = scan.fetch_upcoming(client, start, end, sportmonks.CHAMPIONSHIP_LEAGUES)
    summary.fixtures = [f"{f.name} {f.kickoff_at:%H:%M}" for f in fixtures]
    if not fixtures:
        return summary
    finished = finished_listing(client, now)
    conn = psycopg2.connect(dsn, options="-c statement_timeout=30000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final'")
                stored = {str(row[0]) for row in cur.fetchall()}
                missing = missing_results(finished, stored, now)
                if missing:
                    raise ValueError(stale_message(missing))
                slate = [(fixture, scan.load_slate_inputs(cur, fixture)) for fixture in fixtures]
    finally:
        conn.close()
    if odds == "oddspapi":
        if odds_client is None:
            raise ValueError("--odds oddspapi needs an OddsPapi client")
        squads = {fixture.fixture_id: inputs.squad for fixture, inputs in slate}
        coverage, summary.odds_notes, available = collect_ladder_odds(odds_client, fixtures, squads, now, end, books,
                                                                      state_dir)
        summary.odds_unavailable = not available
        real_quotes = coverage.quotes

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            return scan.in_bankroll_currency([q for q in real_quotes if q.fixture_id == fixture.fixture_id
                                              and q.market_type in ALERT_MARKETS], rate)
    elif odds == "mock":
        seed = int.from_bytes(hashlib.sha256(start.isoformat().encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            return scan.in_bankroll_currency([q for q in mock_quotes(projections, now, rng)
                                              if q.market_type in ALERT_MARKETS], rate)
    else:
        raise ValueError(f"unknown odds source {odds!r}")
    manager = InstitutionalAssetManager(bankroll, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    engine, market_engines = championship_engines()
    combined = AllocationReport()
    all_projections: dict[ProjectionKey, FactorProjection] = {}
    all_descriptions: dict[ProjectionKey, AlertContext] = {}
    exposure = store.exposure(now)
    for fixture, inputs in slate:
        projections, report, quotes = scan.scan_fixture(fixture, inputs, now, quotes_for, manager, engine, summary,
                                                        exposure, market_engines, pricer=price_championship)
        if report is None:
            continue
        if odds == "oddspapi" and not quotes and projections:
            summary.unquoted_matches += 1
        summary.players_priced += len({key[1] for key in projections})
        summary.quotes += len(quotes)
        summary.directives += len(report.directives)
        combined.directives.extend(report.directives)
        all_projections.update(projections)
        all_descriptions.update(scan.descriptions(fixture, inputs, projections))
    combined.directives.sort(key=lambda d: (-d.ev_per_unit, d.fixture_id, d.player_id, d.market_type, d.line_value))
    if notifier is not None:
        summary.alerts_sent, _ = notifier.notify_digest(
            combined, all_projections, bankroll_currency=currency, label=label, descriptions=all_descriptions,
            max_alerts=max_alerts, book_rate=rate, usage=PILOT_USAGE,
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("prop_alpha.scanner").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Scan the next Championship matches and tip high-value bets (pilot).")
    parser.add_argument("--odds", choices=("oddspapi", "mock"), required=True,
                        help="oddspapi: real bet365 ladders; mock: model price plus noise, labelled MOCK")
    parser.add_argument("--send", action="store_true", help="send tips and the summary to Telegram")
    parser.add_argument("--at", type=datetime.fromisoformat, default=None, help="simulated UTC run time (testing)")
    args = parser.parse_args(argv)
    if args.at is not None and args.odds == "oddspapi":
        parser.error("--at simulates the run time, so it works only with --odds mock; real odds are priced now")
    now = args.at.astimezone(timezone.utc) if args.at is not None else datetime.now(timezone.utc)
    currency = bankroll_currency()
    book_rate = book_to_bankroll(currency, now.date())
    from prop_alpha.ingestion.pipeline import build_odds_client, build_vendor_client

    environ = {**os.environ, "DATA_VENDOR_MAX_REQUESTS": os.environ.get("DATA_VENDOR_MAX_REQUESTS", "12"),
               "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", "100")}
    client = build_vendor_client(environ)
    real = args.odds == "oddspapi"
    label = PILOT_LABEL if real else f"{scan.MOCK_LABEL}\n{PILOT_LABEL}"
    odds_client = build_odds_client(os.environ) if real else None
    # Mock tips are kept apart from the pilot's, as the League One/Two scan keeps its mock tips from the real ones.
    store = SentAlertStore(PILOT_STATE_FILE if real else scan.MOCK_STATE_FILE)
    index = MessageIndex(PILOT_INDEX_FILE if real else scan.MOCK_INDEX_FILE)
    deliveries = DeliveryLog(PILOT_DELIVERY_FILE if real else scan.MOCK_DELIVERY_FILE)
    notifier = TelegramNotifier(load_config(), store=store, index=index, deliveries=deliveries) if args.send else None
    books = tuple(b.strip() for b in os.environ.get(BOOKMAKERS_ENV, ",".join(DEFAULT_BOOKMAKERS)).split(",") if b.strip())
    try:
        summary = run_ladder_scan(
            now, client, os.environ.get(PILOT_DSN_ENV, DEFAULT_PILOT_DSN), notifier, store,
            float(os.environ.get(scan.BANKROLL_ENV, scan.DEFAULT_BANKROLL)), label,
            int(os.environ.get(scan.MAX_ALERTS_ENV, scan.DEFAULT_MAX_ALERTS)),
            odds=args.odds, odds_client=odds_client, books=books, currency=currency, book_rate=book_rate,
        )
    finally:
        client.close()
        if odds_client is not None:
            odds_client.close()
    text = summary_text(summary, label)
    print(text)
    if notifier is not None:
        print(f"summary sent (message_id {notifier.send_message(text)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
