"""4-hourly opportunity scan: fixtures kicking off before the next run are priced and screened, and every new
high-value bet is sent to Telegram, followed by a one-message summary of the run. It never places a bet.

    python -m prop_alpha.alerts.scan --odds oddspapi --send      # real bookmaker odds (OddsPapi)
    python -m prop_alpha.alerts.scan --odds mock                 # print what would be sent
    python -m prop_alpha.alerts.scan --odds mock --send          # send alerts and the summary
    python -m prop_alpha.alerts.scan --odds mock --at 2026-10-10T12:00:00+00:00   # simulate a run time

I/O: one Sportmonks listing call per run (through ingestion.pipeline.VendorClient), one read-only database
transaction, the alert state files, the OddsPapi cache and odds archive under .prop_alpha_state/, and Telegram.

`--odds oddspapi` fetches real prices once per run, and only when the window has fixtures: one call per bookmaker
in ODDS_BOOKMAKERS, plus the market catalogue (weekly) and team names (when an unknown team appears). The free
plan has 250 requests, so the run first reads the unmetered account endpoint and skips odds when fetching would
leave fewer than ODDS_QUOTA_RESERVE. Every raw odds response is archived for closing-line analysis.
`--odds mock` prices from the model plus noise: its alerts are labelled MOCK on every message and recorded in
their own state file, so they never count toward real exposure. One fixture that cannot be priced is recorded in the summary and skipped (sanctioned
handler H9); the rest of the slate is still scanned.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

import numpy as np
import requests

from prop_alpha.alerts.status import DEFAULT_DSN, DSN_ENV
from prop_alpha.alerts.telegram import PAPER_LABEL, AlertContext, SentAlertStore, TelegramNotifier, load_config
from prop_alpha.config import DEFAULT_ENV_FILE
from prop_alpha.ingestion import oddspapi, sportmonks
from prop_alpha.ingestion.pipeline import QuotaExhaustedError
from prop_alpha.modeling.engine import FactorProjection, ProjectionError, QuantEngine
from prop_alpha.scanner.market_scanner import (
    MODEL_RISK_LOSS_FLOOR,
    AllocationReport,
    InstitutionalAssetManager,
    LiveOddsQuote,
    ProjectionKey,
)
from prop_alpha.scanner.slate import (
    FIXTURE_KICKOFF_TOLERANCE,
    HISTORY_MATCHES,
    RECENT_TEAM_MATCHES,
    SCAN_GAME_STATE_K,
    Appearance,
    OddsCoverage,
    SquadPlayer,
    UpcomingFixture,
    build_projections,
    mock_quotes,
    quotes_from_prices,
    scan_window,
)

logger = logging.getLogger("prop_alpha.scan")

MOCK_LABEL: Final[str] = "MOCK ODDS - test run, made-up bookmaker prices. NOT a real opportunity."
MOCK_STATE_FILE: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state" / "mock_sent_alerts.jsonl"
BANKROLL_ENV: Final[str] = "PROP_ALPHA_BANKROLL"
DEFAULT_BANKROLL: Final[float] = 1000.0
MAX_ALERTS_ENV: Final[str] = "PROP_ALPHA_MAX_ALERTS"
# Best expected profit first, sent as digest messages of 5 (telegram.DIGEST_SIZE); the rest of the run's
# candidates are not sent (the summary says so).
DEFAULT_MAX_ALERTS: Final[int] = 10
MAX_LISTING_PAGES: Final[int] = 5
STATE_DIR: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state"
BOOKMAKERS_ENV: Final[str] = "ODDS_BOOKMAKERS"
DEFAULT_BOOKMAKERS: Final[tuple[str, ...]] = ("bet365", "skybet", "williamhill")
ODDS_QUOTA_RESERVE: Final[int] = 20
CATALOGUE_MAX_AGE: Final[timedelta] = timedelta(days=7)
QuoteSource = Callable[[UpcomingFixture, Mapping[ProjectionKey, FactorProjection]], list[LiveOddsQuote]]
RATE_LOOKBACK: Final[timedelta] = timedelta(days=365)

TEAM_RECENT_SQL: Final[str] = """
    SELECT f.fixture_id
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.teams t ON t.team_id IN (f.home_team_id, f.away_team_id)
    WHERE t.vendor_team_uid = %s AND f.status = 'final' AND f.kickoff_at < %s
    ORDER BY f.kickoff_at DESC
    LIMIT %s
"""
SQUAD_SQL: Final[str] = """
    SELECT DISTINCT p.player_id, p.full_name, p.primary_position
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.players p USING (player_id)
    JOIN prop_alpha.teams t ON t.team_id = s.team_id
    WHERE s.fixture_id = ANY(%s) AND t.vendor_team_uid = %s
"""
HISTORY_SQL: Final[str] = """
    SELECT s.player_id, s.fixture_id, t.vendor_team_uid, f.kickoff_at, s.started, s.minutes_played,
           s.tackles_attempted, s.fouls_committed
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.teams t ON t.team_id = s.team_id
    WHERE s.player_id = ANY(%s) AND f.kickoff_at < %s
    ORDER BY s.player_id, f.kickoff_at DESC
"""
RATES_SQL: Final[str] = """
    SELECT p.primary_position,
           90.0 * sum(s.tackles_attempted) / sum(s.minutes_played),
           90.0 * sum(s.fouls_committed) / sum(s.minutes_played)
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.players p USING (player_id)
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.leagues l USING (league_id)
    WHERE l.name = %s AND f.kickoff_at < %s AND f.kickoff_at >= %s AND s.minutes_played > 0
    GROUP BY p.primary_position
"""


@dataclass(frozen=True)
class SlateInputs:
    squad: tuple[SquadPlayer, ...]
    history_by_player: Mapping[int, tuple[Appearance, ...]]
    team_recent_fixtures: Mapping[str, tuple[int, ...]]
    league_rates: Mapping[tuple[str, str], float]


@dataclass
class ScanSummary:
    window_start: datetime
    window_end: datetime
    fixtures: list[str] = field(default_factory=list)
    players_priced: int = 0
    quotes: int = 0
    directives: int = 0
    alerts_sent: int = 0
    alert_cap: int | None = None
    failures: list[str] = field(default_factory=list)
    odds_notes: list[str] = field(default_factory=list)

    def text(self, label: str) -> str:
        lines = [
            *((label,) if label else ()),
            f"prop_alpha scan {self.window_start:%a %d %b %H:%M}-{self.window_end:%H:%M} UTC",
            f"Matches kicking off in this window: {len(self.fixtures)}"
            + (f" ({'; '.join(self.fixtures)})" if self.fixtures else ""),
        ]
        if self.fixtures:
            lines += [
                f"Players priced: {self.players_priced}; quotes checked: {self.quotes}; "
                f"stakes suggested: {self.directives}; high-value alerts sent: {self.alerts_sent}",
            ]
            if self.alert_cap is not None and self.alerts_sent >= self.alert_cap:
                lines.append(f"Alert cap of {self.alert_cap} per run reached: the best by expected profit were sent")
        lines += self.odds_notes
        lines += [f"Could not price {failure}" for failure in self.failures]
        lines.append(PAPER_LABEL)
        return "\n".join(lines)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def fetch_upcoming(client: Any, start: datetime, end: datetime) -> list[UpcomingFixture]:
    """Not-started SCAN_LEAGUES fixtures with kickoff in [start, end)."""
    fixtures: list[UpcomingFixture] = []
    for page in range(1, MAX_LISTING_PAGES + 1):
        rows, has_more = sportmonks.upcoming_fixtures(client.get_json(sportmonks.upcoming_path(start, end, page)))
        fixtures += [UpcomingFixture(**row) for row in rows if start <= row["kickoff_at"] < end]
        if not has_more:
            break
    return sorted(fixtures, key=lambda f: (f.kickoff_at, f.fixture_id))


def load_slate_inputs(cur: Any, fixture: UpcomingFixture) -> SlateInputs:
    kickoff = fixture.kickoff_at
    recent: dict[str, tuple[int, ...]] = {}
    squad: list[SquadPlayer] = []
    for team in (fixture.home_team_uid, fixture.away_team_uid):
        cur.execute(TEAM_RECENT_SQL, (team, kickoff, RECENT_TEAM_MATCHES))
        recent[team] = tuple(int(row[0]) for row in cur.fetchall())
        cur.execute(SQUAD_SQL, (list(recent[team]), team))
        squad += [SquadPlayer(int(pid), str(name), str(position), team) for pid, name, position in cur.fetchall()]
    history: dict[int, list[Appearance]] = {}
    cur.execute(HISTORY_SQL, ([p.player_id for p in squad], kickoff))
    for pid, fid, team, kicked_off, started, minutes, tackles, fouls in cur.fetchall():
        rows = history.setdefault(int(pid), [])
        if len(rows) < HISTORY_MATCHES + RECENT_TEAM_MATCHES:
            rows.append(Appearance(int(pid), int(fid), str(team), _utc(kicked_off), bool(started), float(minutes),
                                   int(tackles), int(fouls)))
    cur.execute(RATES_SQL, (fixture.league, kickoff, kickoff - RATE_LOOKBACK))
    rates: dict[tuple[str, str], float] = {}
    for position, tackles_rate, fouls_rate in cur.fetchall():
        rates[(str(position), "tackles")] = float(tackles_rate)
        rates[(str(position), "fouls")] = float(fouls_rate)
    return SlateInputs(tuple(squad), {k: tuple(v) for k, v in history.items()}, recent, rates)


def scan_fixture(
    fixture: UpcomingFixture, inputs: SlateInputs, now: datetime, quotes_for: QuoteSource,
    manager: InstitutionalAssetManager, engine: QuantEngine, summary: ScanSummary, exposure: Any,
) -> tuple[dict[ProjectionKey, FactorProjection], AllocationReport | None, list[LiveOddsQuote]]:
    """Projections, quotes and the allocation for one fixture; a pricing failure is recorded and skipped."""
    try:
        projections, skipped = build_projections(
            fixture, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures, inputs.league_rates, engine
        )
        quotes = quotes_for(fixture, projections)
        report = manager.allocate(projections, quotes, now=now, exposure=exposure)
    except (ProjectionError, ValueError, FloatingPointError) as exc:
        summary.failures.append(f"{fixture.name}: {type(exc).__name__}: {exc}")
        logger.error("scan: %s skipped: %s", fixture.name, exc)
        return {}, None, []
    for reason in skipped:
        logger.info("scan: %s: %s", fixture.name, reason)
    return projections, report, quotes


def descriptions(
    fixture: UpcomingFixture, inputs: SlateInputs, projections: Mapping[ProjectionKey, FactorProjection]
) -> dict[ProjectionKey, AlertContext]:
    players = {p.player_id: p for p in inputs.squad}
    team_names = {fixture.home_team_uid: fixture.home_name, fixture.away_team_uid: fixture.away_name}
    return {
        key: AlertContext(
            f"{fixture.name}, {fixture.kickoff_at:%a %d %b %H:%M} UTC",
            f"{players[key[1]].name} ({team_names[players[key[1]].team_uid]})",
        )
        for key in projections
    }


def _load_catalogue(path: Path) -> dict[int, oddspapi.LineMarket]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(mid): oddspapi.LineMarket(m["market"], float(m["line"]), {int(o): side for o, side in m["sides"].items()})
        for mid, m in raw.items()
    }


def _save_catalogue(path: Path, catalogue: Mapping[int, oddspapi.LineMarket]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        str(mid): {"market": m.market, "line": m.line, "sides": {str(o): side for o, side in m.sides.items()}}
        for mid, m in catalogue.items()
    }), encoding="utf-8")


def _requests_left(account: Any) -> int:
    if not isinstance(account, Mapping):
        raise ValueError("OddsPapi account response is not an object")
    current = account.get("current_subscription_id")
    for subscription in account.get("subscriptions") or []:
        if subscription.get("subscription_id") == current:
            return int(subscription["request_limit"]) - int(subscription["request_count"])
    raise ValueError("OddsPapi account has no current subscription")


def collect_odds(
    client: Any, fixtures: Sequence[UpcomingFixture], squads: Mapping[int, Sequence[SquadPlayer]],
    now: datetime, window_end: datetime, books: Sequence[str], state_dir: Path,
) -> tuple[OddsCoverage, list[str]]:
    """Real player-prop quotes for `fixtures` from OddsPapi, plus summary lines on coverage and quota."""
    empty = OddsCoverage((), 0, 0, (), ())
    cache = state_dir / "oddspapi"
    catalogue_file, names_file = cache / "line_markets.json", cache / "participants.json"
    catalogue_fresh = catalogue_file.is_file() and (
        datetime.now(timezone.utc) - datetime.fromtimestamp(catalogue_file.stat().st_mtime, timezone.utc)
        < CATALOGUE_MAX_AGE
    )
    left = _requests_left(client.get_json_value(oddspapi.ACCOUNT_PATH))
    needed = len(books) + (0 if catalogue_fresh else 1) + (0 if names_file.is_file() else 1)
    if left - needed < ODDS_QUOTA_RESERVE:
        return empty, [f"Odds NOT fetched: OddsPapi has {left} requests left; this run needs {needed} and "
                       f"{ODDS_QUOTA_RESERVE} are kept in reserve"]
    if catalogue_fresh:
        catalogue = _load_catalogue(catalogue_file)
    else:
        catalogue = oddspapi.line_catalogue(client.get_json_value(oddspapi.MARKETS_PATH))
        _save_catalogue(catalogue_file, catalogue)
    tournaments = sorted({oddspapi.TOURNAMENTS[f.league] for f in fixtures if f.league in oddspapi.TOURNAMENTS})
    archive = state_dir / "odds_archive"
    archive.mkdir(parents=True, exist_ok=True)
    window_start = (fixtures[0].kickoff_at if fixtures else now) - FIXTURE_KICKOFF_TOLERANCE
    prices: list[oddspapi.PropPrice] = []
    no_odds: list[str] = []
    for book in books:
        body = client.get_json_value(oddspapi.odds_path(book, tournaments), empty_on=frozenset({400, 404}))
        if body is None:
            no_odds.append(book)
            continue
        with gzip.open(archive / f"{now:%Y%m%dT%H%M%SZ}_{book}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(body, handle)
        prices += [p for p in oddspapi.prop_prices(body, catalogue, book)
                   if window_start <= p.fixture.kickoff_at < window_end + FIXTURE_KICKOFF_TOLERANCE]
    names: dict[int, str] = (
        {int(k): v for k, v in json.loads(names_file.read_text(encoding="utf-8")).items()} if names_file.is_file() else {}
    )
    if any(team not in names for p in prices for team in (p.fixture.home_id, p.fixture.away_id)):
        names = oddspapi.participant_names(client.get_json_value(oddspapi.PARTICIPANTS_PATH))
        cache.mkdir(parents=True, exist_ok=True)
        names_file.write_text(json.dumps({str(k): v for k, v in names.items()}), encoding="utf-8")
    coverage = quotes_from_prices(prices, names, fixtures, squads, now)
    notes = [
        f"Odds: OddsPapi ({', '.join(books)}): {coverage.prices} player tackles/fouls prices in this window, "
        f"{coverage.matched_prices} matched to our players, {len(coverage.quotes)} lines; "
        f"about {left - client.requests_sent + 1} OddsPapi requests left",
    ]
    if no_odds:
        notes.append(f"No odds for these leagues from: {', '.join(no_odds)}")
    if coverage.unmatched_fixtures:
        notes.append(f"Odds for matches we could not match: {'; '.join(coverage.unmatched_fixtures)}")
    if coverage.unmatched_players:
        notes.append(f"Players we could not match: {len(coverage.unmatched_players)} "
                     f"(e.g. {'; '.join(coverage.unmatched_players[:3])})")
    for name in coverage.unmatched_players:
        logger.info("odds: unmatched player %s", name)
    return coverage, notes


def run_scan(
    now: datetime, client: Any, dsn: str, notifier: TelegramNotifier | None, store: SentAlertStore,
    bankroll: float, label: str, max_alerts: int = DEFAULT_MAX_ALERTS, *,
    odds: str = "mock", odds_client: Any | None = None, books: Sequence[str] = DEFAULT_BOOKMAKERS,
    state_dir: Path = STATE_DIR,
) -> ScanSummary:
    import psycopg2

    start, end = scan_window(now)
    summary = ScanSummary(start, end, alert_cap=max_alerts)
    fixtures = fetch_upcoming(client, start, end)
    summary.fixtures = [f"{f.name} {f.kickoff_at:%H:%M}" for f in fixtures]
    if not fixtures:
        return summary
    conn = psycopg2.connect(dsn, options="-c statement_timeout=30000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                slate = [(fixture, load_slate_inputs(cur, fixture)) for fixture in fixtures]
    finally:
        conn.close()
    if odds == "oddspapi":
        if odds_client is None:
            raise ValueError("--odds oddspapi needs an OddsPapi client")
        squads = {fixture.fixture_id: inputs.squad for fixture, inputs in slate}
        try:
            coverage, summary.odds_notes = collect_odds(odds_client, fixtures, squads, now, end, books, state_dir)
        except (requests.RequestException, QuotaExhaustedError, ValueError) as exc:
            # An odds outage must show up in the chat, not silence the run: the summary still goes out.
            summary.odds_notes = [f"Odds unavailable this run, nothing priced: {type(exc).__name__}: {exc}"]
            logger.error("scan: odds unavailable: %s: %s", type(exc).__name__, exc)
            coverage = OddsCoverage((), 0, 0, (), ())
        real_quotes = coverage.quotes

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            return [quote for quote in real_quotes if quote.fixture_id == fixture.fixture_id]
    elif odds == "mock":
        seed = int.from_bytes(hashlib.sha256(start.isoformat().encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            return mock_quotes(projections, now, rng)
    else:
        raise ValueError(f"unknown odds source {odds!r}")
    manager = InstitutionalAssetManager(bankroll, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    engine = QuantEngine(game_state_k=SCAN_GAME_STATE_K)
    combined = AllocationReport()
    all_projections: dict[ProjectionKey, FactorProjection] = {}
    all_descriptions: dict[ProjectionKey, AlertContext] = {}
    exposure = store.exposure(now)
    for fixture, inputs in slate:
        projections, report, quotes = scan_fixture(fixture, inputs, now, quotes_for, manager, engine, summary, exposure)
        if report is None:
            continue
        summary.players_priced += len({key[1] for key in projections})
        summary.quotes += len(quotes)
        summary.directives += len(report.directives)
        combined.directives.extend(report.directives)
        all_projections.update(projections)
        all_descriptions.update(descriptions(fixture, inputs, projections))
    combined.directives.sort(key=lambda d: (-d.ev_per_unit, d.fixture_id, d.player_id, d.market_type, d.line_value))
    if notifier is not None:
        summary.alerts_sent, _ = notifier.notify_digest(
            combined, all_projections, label=label, descriptions=all_descriptions, max_alerts=max_alerts
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The allocator logs every sub-minimum stake at INFO: hundreds of lines per window in the scheduled log.
    logging.getLogger("prop_alpha.scanner").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Scan fixtures before the next run and alert high-value bets.")
    parser.add_argument("--odds", choices=("oddspapi", "mock"), required=True,
                        help="oddspapi: real bookmaker prices; mock: model price plus noise, labelled MOCK")
    parser.add_argument("--send", action="store_true", help="send alerts and the summary to Telegram")
    parser.add_argument("--at", type=datetime.fromisoformat, default=None, help="simulated UTC run time (testing)")
    args = parser.parse_args(argv)
    now = _utc(args.at) if args.at is not None else datetime.now(timezone.utc)
    from prop_alpha.ingestion.pipeline import build_odds_client, build_vendor_client

    environ = {**os.environ, "DATA_VENDOR_MAX_REQUESTS": os.environ.get("DATA_VENDOR_MAX_REQUESTS", "10"),
               "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", "100")}
    client = build_vendor_client(environ)
    real = args.odds == "oddspapi"
    label = "" if real else MOCK_LABEL
    odds_client = build_odds_client(os.environ) if real else None
    store = SentAlertStore() if real else SentAlertStore(MOCK_STATE_FILE)
    notifier = TelegramNotifier(load_config(), store=store) if args.send else None
    books = tuple(b.strip() for b in os.environ.get(BOOKMAKERS_ENV, ",".join(DEFAULT_BOOKMAKERS)).split(",") if b.strip())
    try:
        summary = run_scan(now, client, os.environ.get(DSN_ENV, DEFAULT_DSN), notifier, store,
                           float(os.environ.get(BANKROLL_ENV, DEFAULT_BANKROLL)), label,
                           int(os.environ.get(MAX_ALERTS_ENV, DEFAULT_MAX_ALERTS)),
                           odds=args.odds, odds_client=odds_client, books=books)
    finally:
        client.close()
        if odds_client is not None:
            odds_client.close()
    text = summary.text(label)
    print(text)
    if notifier is not None:
        print(f"summary sent (message_id {notifier.send_message(text)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
