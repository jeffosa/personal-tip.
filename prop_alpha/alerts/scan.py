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
In an `--odds oddspapi` run, a match where no bookmaker quote matched any player gets a fair-price board instead
(`send_boards`): each priced player's fair and minimum odds on the line closest to even, for manual comparison with
any bookmaker. Each match is boarded once (`fair_boards_sent.json` in the state directory).
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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

import numpy as np
import requests

from prop_alpha.alerts.closing import save_scan_fixtures
from prop_alpha.alerts.fx import BOOK_CURRENCY, SUPPORTED_CURRENCIES, ExchangeRate, bankroll_currency, book_to_bankroll
from prop_alpha.alerts.fx import identity as fx_identity
from prop_alpha.alerts.ledger import PlacedBetStore, combined_exposure
from prop_alpha.alerts.replies import USAGE, AlertItem
from prop_alpha.alerts.telegram import (
    DEFAULT_CRITERIA,
    PAPER_LABEL,
    TELEGRAM_MAX_TEXT,
    AlertContext,
    DeliveryLog,
    MessageIndex,
    SentAlertStore,
    TelegramNotifier,
    load_config,
)
from prop_alpha.config import DEFAULT_DSN, DEFAULT_ENV_FILE, DSN_ENV
from prop_alpha.ingestion import oddspapi, sportmonks
from prop_alpha.ingestion.pipeline import QuotaExhaustedError, VendorError
from prop_alpha.modeling.engine import FactorProjection, ProjectionError, QuantEngine
from prop_alpha.modeling.league_level import SEASON_START_MONTH, SeasonTotals, season_of, season_rates
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
    SCAN_GAME_STATE_K_BY_MARKET,
    SCAN_PRIOR_STRENGTH_BY_MARKET,
    SCAN_RATE_MULTIPLIER_BY_MARKET,
    Appearance,
    FairPrice,
    OddsCoverage,
    SquadPlayer,
    UpcomingFixture,
    build_projections,
    fair_price_board,
    mock_quotes,
    quotes_from_prices,
    scan_window,
)

logger = logging.getLogger("prop_alpha.scan")

MOCK_LABEL: Final[str] = "MOCK ODDS - test run, made-up bookmaker prices. NOT a real opportunity."
MOCK_STATE_FILE: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state" / "mock_sent_alerts.jsonl"
MOCK_INDEX_FILE: Final[Path] = MOCK_STATE_FILE.parent / "mock_alert_messages.jsonl"
MOCK_DELIVERY_FILE: Final[Path] = MOCK_STATE_FILE.parent / "mock_alert_deliveries.jsonl"
BANKROLL_ENV: Final[str] = "PROP_ALPHA_BANKROLL"
DEFAULT_BANKROLL: Final[float] = 1000.0
MAX_ALERTS_ENV: Final[str] = "PROP_ALPHA_MAX_ALERTS"
# Best expected profit first, sent as digest messages of 5 (telegram.DIGEST_SIZE); the rest of the run's
# candidates are not sent (the summary says so).
DEFAULT_MAX_ALERTS: Final[int] = 10
MAX_LISTING_PAGES: Final[int] = 5
STATE_DIR: Final[Path] = DEFAULT_ENV_FILE.parent / ".prop_alpha_state"
# Exit code of a real-odds run that had no usable bookmaker prices (the summary and boards were still sent).
ODDS_UNAVAILABLE_EXIT: Final[int] = 3
BOOKMAKERS_ENV: Final[str] = "ODDS_BOOKMAKERS"
DEFAULT_BOOKMAKERS: Final[tuple[str, ...]] = ("bet365", "skybet", "williamhill")
ODDS_QUOTA_RESERVE: Final[int] = 20
CATALOGUE_MAX_AGE: Final[timedelta] = timedelta(days=7)
QuoteSource = Callable[[UpcomingFixture, Mapping[ProjectionKey, FactorProjection]], list[LiveOddsQuote]]
BOARD_STATE_NAME: Final[str] = "fair_boards_sent.json"
# Rows (player, market) per Telegram message, keeping each board message well under the 4,096-character limit.
BOARD_ROWS_PER_MESSAGE: Final[int] = 25
# Markets whose bookmaker quotes may be staked and alerted. Fouls fails PRD Gate 2 calibration on the selection
# window (slope 0.79/0.84 v the required 0.9-1.1), so by maintainer decision (2026-10-05) it is priced (boards,
# the Gate 2 lockbox) but never alerted or staked until it passes.
ALERT_MARKETS: Final[frozenset[str]] = frozenset({"tackles"})
UNCALIBRATED_TAG: Final[str] = "[UNCALIBRATED - do not bet]"
BOARD_HEADER: Final[str] = (
    "FAIR-PRICE BOARD - no bookmaker line found for this match. Check any bookmaker yourself and bet a side "
    "only at or above its price. Valid until kickoff. Prices assume the bet is void if the player does not "
    "play: skip a book whose rule differs. Lineups are estimated, not confirmed. Token stakes only until the "
    "model is validated."
)

# The club's last complete matches: a club-match in which a player without a Sportmonks identity played
# (incomplete_team_matches, migration 008) is skipped, because that player's start would be missing from it.
TEAM_RECENT_SQL: Final[str] = """
    SELECT f.fixture_id
    FROM prop_alpha.fixtures f
    JOIN prop_alpha.teams t ON t.team_id IN (f.home_team_id, f.away_team_id)
    WHERE t.vendor_team_uid = %s AND f.status = 'final' AND f.kickoff_at < %s
      AND NOT EXISTS (SELECT 1 FROM prop_alpha.incomplete_team_matches i
                      WHERE i.fixture_id = f.fixture_id AND i.team_id = t.team_id)
    ORDER BY f.kickoff_at DESC
    LIMIT %s
"""
# Position as of kickoff: the player's position in his latest of these (pre-kickoff) matches, from
# player_match_stats.match_position (migration 007), never players.primary_position, which follows his latest
# match overall and would leak later matches into an earlier price (audit 2026-10-04).
SQUAD_SQL: Final[str] = """
    SELECT DISTINCT ON (p.player_id) p.player_id, p.full_name, s.match_position
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.players p USING (player_id)
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.teams t ON t.team_id = s.team_id
    WHERE s.fixture_id = ANY(%s) AND t.vendor_team_uid = %s AND s.match_position IS NOT NULL
    ORDER BY p.player_id, f.kickoff_at DESC, f.fixture_id DESC
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
# Per position and kickoff, for the league level (modeling.league_level): totals from the current season and the
# LEVEL_SEASONS - 1 before it, strictly before the fixture's kickoff.
LEVEL_SQL: Final[str] = """
    SELECT s.match_position, f.kickoff_at,
           sum(s.tackles_attempted), sum(s.fouls_committed), sum(s.minutes_played)
    FROM prop_alpha.player_match_stats s
    JOIN prop_alpha.fixtures f USING (fixture_id)
    JOIN prop_alpha.leagues l USING (league_id)
    WHERE l.name = %s AND f.kickoff_at < %s AND f.kickoff_at >= %s AND s.minutes_played > 0
      AND s.match_position IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM prop_alpha.incomplete_team_matches i
                      WHERE i.fixture_id = s.fixture_id AND i.team_id = s.team_id)
    GROUP BY s.match_position, f.kickoff_at
"""
LEVEL_SEASONS: Final[int] = 3


@dataclass(frozen=True)
class SlateInputs:
    """`league_rates`: (position, market) -> today's trailing league rate per 90. `season_levels`: (position,
    market) -> {season start year -> trailing rate}, used to re-scale each player's history to today's level;
    empty means no re-scaling."""

    squad: tuple[SquadPlayer, ...]
    history_by_player: Mapping[int, tuple[Appearance, ...]]
    team_recent_fixtures: Mapping[str, tuple[int, ...]]
    league_rates: Mapping[tuple[str, str], float]
    season_levels: Mapping[tuple[str, str], Mapping[int, float]] = field(default_factory=dict)


@dataclass
class ScanSummary:
    window_start: datetime
    window_end: datetime
    fixtures: list[str] = field(default_factory=list)
    players_priced: int = 0
    quotes: int = 0
    directives: int = 0
    alerts_sent: int = 0
    boards_sent: int = 0
    alert_cap: int | None = None
    bankroll: str = ""
    # True when a real-odds run had no usable bookmaker prices (outage, quota reserve, every book failed): main()
    # then exits ODDS_UNAVAILABLE_EXIT so the scheduled task records the run as failed.
    odds_unavailable: bool = False
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
                *((f"Bankroll: {self.bankroll}",) if self.bankroll else ()),
            ]
            if self.alert_cap is not None and self.alerts_sent >= self.alert_cap:
                lines.append(f"Alert cap of {self.alert_cap} per run reached: the best by expected profit were sent")
            if self.boards_sent:
                lines.append(f"Fair-price boards sent (matches with no bookmaker line): {self.boards_sent}")
        lines += self.odds_notes
        if self.fixtures:
            lines.append(f"Alerted markets: {', '.join(sorted(ALERT_MARKETS))}; other markets are priced for "
                         f"evaluation only (they fail Gate 2 calibration) and marked {UNCALIBRATED_TAG} on boards")
        lines += [f"Could not price {failure}" for failure in self.failures]
        lines.append(PAPER_LABEL)
        return "\n".join(lines)


def in_bankroll_currency(quotes: Sequence[LiveOddsQuote], book_rate: ExchangeRate) -> list[LiveOddsQuote]:
    """Quotes with max_stake converted from the books' currency into the bankroll's, so the allocator clips a
    bankroll-currency stake to a limit in the same currency."""
    if book_rate.identity:
        return list(quotes)
    return [replace(quote, max_stake=book_rate.convert(quote.max_stake)) for quote in quotes]


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def fetch_upcoming(client: Any, start: datetime, end: datetime) -> list[UpcomingFixture]:
    """Not-started SCAN_LEAGUES fixtures with kickoff in [start, end). Raises if the listing is longer than
    MAX_LISTING_PAGES pages: a scan of part of the window would report normally while missing fixtures."""
    fixtures: list[UpcomingFixture] = []
    for page in range(1, MAX_LISTING_PAGES + 1):
        rows, has_more = sportmonks.upcoming_fixtures(client.get_json(sportmonks.upcoming_path(start, end, page)))
        fixtures += [UpcomingFixture(**row) for row in rows if start <= row["kickoff_at"] < end]
        if not has_more:
            return sorted(fixtures, key=lambda f: (f.kickoff_at, f.fixture_id))
    raise ValueError(f"fixture listing for {start:%Y-%m-%d %H:%M}-{end:%H:%M} UTC has more than {MAX_LISTING_PAGES} "
                     "pages; scan refused rather than run on part of the window")


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
    season_now = season_of(kickoff)
    cur.execute(LEVEL_SQL, (fixture.league, kickoff,
                            datetime(season_now - LEVEL_SEASONS + 1, SEASON_START_MONTH, 1, tzinfo=timezone.utc)))
    totals: dict[tuple[str, str], dict[int, list[float]]] = {}
    for position, kicked_off, tackles, fouls, minutes in cur.fetchall():
        season = season_of(_utc(kicked_off))
        for market, count in (("tackles", tackles), ("fouls", fouls)):
            running = totals.setdefault((str(position), market), {}).setdefault(season, [0.0, 0.0])
            running[0] += float(count)
            running[1] += float(minutes)
    for by_season in totals.values():
        by_season.setdefault(season_now, [0.0, 0.0])  # no match yet this season: last season's level
    levels = {key: season_rates({season: SeasonTotals(c, m) for season, (c, m) in by_season.items()})
              for key, by_season in totals.items()}
    rates = {key: by_season[season_now] for key, by_season in levels.items() if season_now in by_season}
    return SlateInputs(tuple(squad), {k: tuple(v) for k, v in history.items()}, recent, rates, levels)


def scan_engines() -> tuple[QuantEngine, dict[str, QuantEngine]]:
    """The scan's engines: the default and one per market (SCAN_GAME_STATE_K_BY_MARKET)."""
    return (QuantEngine(game_state_k=SCAN_GAME_STATE_K),
            {market: QuantEngine(game_state_k=k) for market, k in SCAN_GAME_STATE_K_BY_MARKET.items()})


def price_fixture(
    fixture: UpcomingFixture, inputs: SlateInputs, engine: QuantEngine,
    market_engines: Mapping[str, QuantEngine] | None = None,
) -> tuple[dict[ProjectionKey, FactorProjection], list[str]]:
    """The model's projections for one fixture, exactly as the scan prices it. The Gate 2 lockbox
    (scripts/gate2_lockbox.py) scores this function, and modeling/model_freeze.py fingerprints it."""
    return build_projections(
        fixture, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures, inputs.league_rates, engine,
        season_levels=inputs.season_levels or None, market_engines=market_engines,
        rate_multipliers=SCAN_RATE_MULTIPLIER_BY_MARKET, prior_strengths=SCAN_PRIOR_STRENGTH_BY_MARKET,
    )


def scan_fixture(
    fixture: UpcomingFixture, inputs: SlateInputs, now: datetime, quotes_for: QuoteSource,
    manager: InstitutionalAssetManager, engine: QuantEngine, summary: ScanSummary, exposure: Any,
    market_engines: Mapping[str, QuantEngine] | None = None,
) -> tuple[dict[ProjectionKey, FactorProjection], AllocationReport | None, list[LiveOddsQuote]]:
    """Projections, quotes and the allocation for one fixture; a pricing failure is recorded and skipped."""
    try:
        projections, skipped = price_fixture(fixture, inputs, engine, market_engines)
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


def format_board(
    fixture: UpcomingFixture, inputs: SlateInputs, entries: Sequence[FairPrice]
) -> list[tuple[str, list[AlertItem]]]:
    """Board messages for one fixture, each with the numbered items it lists (for bet-recording replies): one
    numbered row per player and market with each listed side's minimum odds, home side first; split every
    BOARD_ROWS_PER_MESSAGE rows. Numbers run on across the messages of one board."""
    players = {p.player_id: p for p in inputs.squad}
    team_names = {fixture.home_team_uid: fixture.home_name, fixture.away_team_uid: fixture.away_name}
    rows: dict[tuple[int, str, float], dict[str, FairPrice]] = {}
    for entry in entries:
        rows.setdefault((entry.player_id, entry.market, entry.line), {})[entry.side] = entry

    def order(key: tuple[int, str, float]) -> tuple[bool, str, str]:
        player = players[key[0]]
        return player.team_uid != fixture.home_team_uid, player.name, key[1]

    title = f"{fixture.name}, {fixture.kickoff_at:%a %d %b %H:%M} UTC"
    numbered: list[tuple[str, AlertItem]] = []
    for number, key in enumerate(sorted(rows, key=order), start=1):
        player, sides = players[key[0]], rows[key]
        over, under = sides.get("over"), sides.get("under")
        p_over = over.model_probability if over is not None else 1.0 - sides["under"].model_probability
        listed = [(side, entry) for side, entry in (("over", over), ("under", under)) if entry is not None]
        prices = " | ".join(f"{side.title()} >= {entry.min_odds:.2f}" for side, entry in listed)
        who = f"{player.name} ({team_names[player.team_uid]})"
        numbered.append((
            f"{number}. {who} {key[1]} {key[2]:g}: {prices} (model: over {p_over:.0%})"
            + ("" if key[1] in ALERT_MARKETS else f" {UNCALIBRATED_TAG}"),
            AlertItem(number=number, kind="board", fixture_id=fixture.fixture_id, player_id=player.player_id,
                      market=key[1], line=key[2], min_odds=tuple((side, entry.min_odds) for side, entry in listed),
                      match=title, player=who),
        ))
    def message(part: int, rows: Sequence[tuple[str, AlertItem]]) -> str:
        return "\n".join([PAPER_LABEL, *((BOARD_HEADER,) if part == 0 else ()),
                          title + (" (continued)" if part else ""), *(line for line, _ in rows), "", USAGE])

    # A message closes at BOARD_ROWS_PER_MESSAGE rows or before it would pass Telegram's limit, whichever comes
    # first: 25 rows of long names reached 4,095 of 4,096 characters, and a longer board was cut silently.
    chunks: list[list[tuple[str, AlertItem]]] = []
    for row in numbered:
        if chunks and len(chunks[-1]) < BOARD_ROWS_PER_MESSAGE \
                and len(message(len(chunks) - 1, [*chunks[-1], row])) <= TELEGRAM_MAX_TEXT:
            chunks[-1].append(row)
        else:
            chunks.append([row])
    messages = [(message(part, rows), [item for _, item in rows]) for part, rows in enumerate(chunks)]
    too_long = [len(text) for text, _ in messages if len(text) > TELEGRAM_MAX_TEXT]
    if too_long:
        raise ValueError(f"board row too long for one Telegram message ({max(too_long)} characters)")
    return messages


def send_boards(
    notifier: TelegramNotifier,
    candidates: Sequence[tuple[UpcomingFixture, SlateInputs, Mapping[ProjectionKey, FactorProjection]]],
    state_file: Path,
) -> int:
    """Send a fair-price board for each candidate fixture not boarded before (state_file keeps the fixture ids, so
    a retried run does not repeat a board). Returns the number of fixtures boarded."""
    sent: set[int] = set(json.loads(state_file.read_text(encoding="utf-8"))) if state_file.is_file() else set()
    boarded = 0
    for fixture, inputs, projections in candidates:
        if fixture.fixture_id in sent:
            continue
        entries = fair_price_board(projections, DEFAULT_CRITERIA.loss_floor, DEFAULT_CRITERIA.min_ev_per_unit)
        if not entries:
            continue
        for text, items in format_board(fixture, inputs, entries):
            notifier.send_tracked(text, "board", items, fixture.kickoff_at)
        sent.add(fixture.fixture_id)
        boarded += 1
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(sorted(sent)), encoding="utf-8")
    return boarded


def _load_catalogue(path: Path) -> dict[int, oddspapi.LineMarket]:
    """The cached catalogue. A damaged cache raises ValueError (handler H11 reports it); delete the file to refetch."""
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(m, dict) and isinstance(m.get("market"), str) and isinstance(m.get("line"), (int, float))
        and isinstance(m.get("sides"), dict) for m in raw.values()
    ):
        raise ValueError(f"{path} is not a market catalogue; delete it so the next run fetches a fresh one")
    return {
        int(mid): oddspapi.LineMarket(m["market"], float(m["line"]), {int(o): str(side) for o, side in m["sides"].items()})
        for mid, m in raw.items()
    }


def _save_catalogue(path: Path, catalogue: Mapping[int, oddspapi.LineMarket]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        str(mid): {"market": m.market, "line": m.line, "sides": {str(o): side for o, side in m.sides.items()}}
        for mid, m in catalogue.items()
    }), encoding="utf-8")


def _archive(path: Path, body: Any) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(body, handle)


def _load_names(path: Path) -> dict[int, str]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a participant-name map; delete it so the next run fetches a fresh one")
    return {int(k): str(v) for k, v in raw.items()}


def _requests_left(account: Any) -> int:
    if not isinstance(account, Mapping):
        raise ValueError("OddsPapi account response is not an object")
    current = account.get("current_subscription_id")
    subscriptions: Any = account.get("subscriptions") or []
    if not isinstance(subscriptions, list):
        raise ValueError("OddsPapi account subscriptions are not a list")
    for subscription in subscriptions:
        if isinstance(subscription, Mapping) and subscription.get("subscription_id") == current:
            limit, count = subscription.get("request_limit"), subscription.get("request_count")
            if not (isinstance(limit, int) and isinstance(count, int)) or isinstance(limit, bool) \
                    or isinstance(count, bool):
                raise ValueError("OddsPapi account subscription has no integer request_limit and request_count")
            return limit - count
    raise ValueError("OddsPapi account has no current subscription")


def collect_odds(
    client: Any, fixtures: Sequence[UpcomingFixture], squads: Mapping[int, Sequence[SquadPlayer]],
    now: datetime, window_end: datetime, books: Sequence[str], state_dir: Path,
) -> tuple[OddsCoverage, list[str], bool]:
    """Real player-prop quotes for `fixtures` from OddsPapi, summary lines on coverage and quota, and whether real
    odds were available: False when the quota reserve stopped the fetch or every bookmaker failed (a bookmaker that
    reports no fixtures is not a failure). One failing bookmaker is recorded and skipped (handler H12); the others'
    prices are kept (audit 2026-10-04: a William Hill 404 had discarded bet365's prices). A bookmaker whose reply is
    malformed (oddspapi.prop_prices raises ValueError) fails the same way: its whole reply is left out, never part of
    it, and the raw body is archived as <stamp>_<book>.rejected.json.gz for diagnosis (readiness item 5, 2026-10-05).
    Its name does not match <stamp>_<book>.json.gz, so closing capture never asks that bookmaker on this scan's
    behalf."""
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
                       f"{ODDS_QUOTA_RESERVE} are kept in reserve"], False
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
    failed: dict[str, str] = {}
    answered = 0
    for book in books:
        body: Any = None
        try:
            body = client.get_json_value(oddspapi.odds_path(book, tournaments), empty_on=oddspapi.NO_FIXTURES_REPLIES)
            book_prices = [] if body is None else oddspapi.prop_prices(body, catalogue, book)
        except (requests.RequestException, VendorError, ValueError) as exc:
            failed[book] = f"{type(exc).__name__}: {exc}"
            logger.error("odds: %s failed: %s", book, failed[book])
            if body is not None:
                _archive(archive / f"{now:%Y%m%dT%H%M%SZ}_{book}.rejected.json.gz", body)
            continue
        answered += 1
        if body is None:
            no_odds.append(book)
            continue
        _archive(archive / f"{now:%Y%m%dT%H%M%SZ}_{book}.json.gz", body)
        prices += [p for p in book_prices
                   if window_start <= p.fixture.kickoff_at < window_end + FIXTURE_KICKOFF_TOLERANCE]
    names = _load_names(names_file) if names_file.is_file() else {}
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
    for book, error in failed.items():
        notes.append(f"Odds FAILED from {book}, its prices left out: {error}")
    if coverage.unmatched_fixtures:
        notes.append(f"Odds for matches we could not match: {'; '.join(coverage.unmatched_fixtures)}")
    if coverage.unmatched_players:
        notes.append(f"Players we could not match: {len(coverage.unmatched_players)} "
                     f"(e.g. {'; '.join(coverage.unmatched_players[:3])})")
    for name in coverage.unmatched_players:
        logger.info("odds: unmatched player %s", name)
    return coverage, notes, answered > 0 or not failed


def run_scan(
    now: datetime, client: Any, dsn: str, notifier: TelegramNotifier | None, store: SentAlertStore,
    bankroll: float, label: str, max_alerts: int = DEFAULT_MAX_ALERTS, *,
    odds: str = "mock", odds_client: Any | None = None, books: Sequence[str] = DEFAULT_BOOKMAKERS,
    state_dir: Path = STATE_DIR, bets: PlacedBetStore | None = None, currency: str,
    book_rate: ExchangeRate | None = None,
) -> ScanSummary:
    """With `bets`, exposure per market is the larger of the stakes alerted and the stakes recorded as placed.
    `currency` (fx.bankroll_currency()) is the bankroll's; when it is not BOOK_CURRENCY, `book_rate`
    (fx.book_to_bankroll()) converts every book limit into it, and every stake is shown in both."""
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
    summary = ScanSummary(start, end, alert_cap=max_alerts, bankroll=f"{currency} {bankroll:,.2f}{conversion}")
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
            coverage, summary.odds_notes, available = collect_odds(odds_client, fixtures, squads, now, end, books,
                                                                   state_dir)
            summary.odds_unavailable = not available
            # Closing-price capture (alerts/closing.py) asks only the bookmakers that returned fixtures here.
            answered = [book for book in books
                        if (state_dir / "odds_archive" / f"{now:%Y%m%dT%H%M%SZ}_{book}.json.gz").is_file()]
            save_scan_fixtures(state_dir, fixtures, answered, now)
        except (requests.RequestException, QuotaExhaustedError, ValueError) as exc:
            # An odds outage must show up in the chat, not silence the run: the summary still goes out.
            summary.odds_notes = [f"Odds unavailable this run, nothing priced: {type(exc).__name__}: {exc}"]
            summary.odds_unavailable = True
            logger.error("scan: odds unavailable: %s: %s", type(exc).__name__, exc)
            coverage = OddsCoverage((), 0, 0, (), ())
        real_quotes = coverage.quotes

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            return in_bankroll_currency([quote for quote in real_quotes if quote.fixture_id == fixture.fixture_id
                                         and quote.market_type in ALERT_MARKETS], rate)
    elif odds == "mock":
        seed = int.from_bytes(hashlib.sha256(start.isoformat().encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            return in_bankroll_currency([quote for quote in mock_quotes(projections, now, rng)
                                         if quote.market_type in ALERT_MARKETS], rate)
    else:
        raise ValueError(f"unknown odds source {odds!r}")
    manager = InstitutionalAssetManager(bankroll, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    engine, market_engines = scan_engines()
    combined = AllocationReport()
    all_projections: dict[ProjectionKey, FactorProjection] = {}
    all_descriptions: dict[ProjectionKey, AlertContext] = {}
    exposure = store.exposure(now) if bets is None else combined_exposure(store, bets, now)
    unquoted: list[tuple[UpcomingFixture, SlateInputs, Mapping[ProjectionKey, FactorProjection]]] = []
    for fixture, inputs in slate:
        projections, report, quotes = scan_fixture(fixture, inputs, now, quotes_for, manager, engine, summary, exposure,
                                                   market_engines)
        if report is None:
            continue
        if odds == "oddspapi" and not quotes and projections:
            unquoted.append((fixture, inputs, projections))
        summary.players_priced += len({key[1] for key in projections})
        summary.quotes += len(quotes)
        summary.directives += len(report.directives)
        combined.directives.extend(report.directives)
        all_projections.update(projections)
        all_descriptions.update(descriptions(fixture, inputs, projections))
    combined.directives.sort(key=lambda d: (-d.ev_per_unit, d.fixture_id, d.player_id, d.market_type, d.line_value))
    if notifier is not None:
        summary.alerts_sent, _ = notifier.notify_digest(
            combined, all_projections, bankroll_currency=currency, label=label, descriptions=all_descriptions,
            max_alerts=max_alerts, book_rate=rate,
        )
        # No bookmaker line to compare against: give the manual bettor the model's prices instead.
        summary.boards_sent = send_boards(notifier, unquoted, state_dir / BOARD_STATE_NAME)
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
    currency = bankroll_currency()
    book_rate = book_to_bankroll(currency, now.date())
    from prop_alpha.ingestion.pipeline import build_odds_client, build_vendor_client

    environ = {**os.environ, "DATA_VENDOR_MAX_REQUESTS": os.environ.get("DATA_VENDOR_MAX_REQUESTS", "10"),
               "DATA_VENDOR_QUOTA_RESERVE": os.environ.get("DATA_VENDOR_QUOTA_RESERVE", "100")}
    client = build_vendor_client(environ)
    real = args.odds == "oddspapi"
    label = "" if real else MOCK_LABEL
    odds_client = build_odds_client(os.environ) if real else None
    store = SentAlertStore() if real else SentAlertStore(MOCK_STATE_FILE)
    # Mock messages are indexed separately, so a reply to one can never be recorded as a real bet.
    index = MessageIndex() if real else MessageIndex(MOCK_INDEX_FILE)
    deliveries = DeliveryLog() if real else DeliveryLog(MOCK_DELIVERY_FILE)
    notifier = TelegramNotifier(load_config(), store=store, index=index, deliveries=deliveries) if args.send else None
    books = tuple(b.strip() for b in os.environ.get(BOOKMAKERS_ENV, ",".join(DEFAULT_BOOKMAKERS)).split(",") if b.strip())
    try:
        summary = run_scan(now, client, os.environ.get(DSN_ENV, DEFAULT_DSN), notifier, store,
                           float(os.environ.get(BANKROLL_ENV, DEFAULT_BANKROLL)), label,
                           int(os.environ.get(MAX_ALERTS_ENV, DEFAULT_MAX_ALERTS)),
                           odds=args.odds, odds_client=odds_client, books=books,
                           bets=PlacedBetStore() if real else None, currency=currency, book_rate=book_rate)
    finally:
        client.close()
        if odds_client is not None:
            odds_client.close()
    text = summary.text(label)
    print(text)
    if notifier is not None:
        print(f"summary sent (message_id {notifier.send_message(text)})")
    if real and summary.odds_unavailable:
        logger.error("scan finished without real odds; exiting %d", ODDS_UNAVAILABLE_EXIT)
        return ODDS_UNAVAILABLE_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
