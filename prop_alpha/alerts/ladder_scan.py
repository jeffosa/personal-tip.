"""Pilot scan: the 4-hourly opportunity scan for the leagues outside the frozen League One/Two model: Brazil Serie A and
the two Scottish leagues (the maintainer's five leagues, 2026-10-11, with League One and League Two, which alerts/scan.py
covers). Brazil Serie A's bookmakers list tackles and fouls as "k+" ladders ("to make k or more") instead of over/under
lines. It never places a bet.

    python -m prop_alpha.alerts.ladder_scan --odds oddspapi --send      # real bet365 prices (OddsPapi)
    python -m prop_alpha.alerts.ladder_scan --odds mock                 # print what would be sent
    python -m prop_alpha.alerts.ladder_scan --odds mock --at 2026-10-13T16:30:00+00:00   # simulate a run time

It is the League One/Two scan (alerts/scan.py) with these differences, and nothing else:

* Leagues: Sportmonks league ids are read from the plan's own league list (sportmonks.PILOT_LEAGUES_WANTED), so a
  league the plan does not include is reported ("not in the Sportmonks plan"), never guessed (Sportmonks again since
  2026-10-21; TheStatsAPI from 2026-10-15 to 2026-10-20).
* Pricing is the pilot's, read from the pilot database `prop_alpha_pilot` (read-only), never from the working
  `prop_alpha`: tackles counted as pilot_ledger.PILOT_TACKLES_SQL (plain tackles_attempted since 2026-10-13, after settled
  results contradicted the 2026-10-12 tackles-plus-challenges-lost count, MARKET_CONTRACT.md 2e; loaded through PilotCursor so the
  frozen League One/Two queries are untouched), prior strength 6, no rate multiplier, and a game-state k per league and market (LEAGUE_GAME_STATE_K: each
  league's own fit on Sportmonks data, 2026-10-08 to 2026-10-21); Scottish Championship tackles are not priced (UNPRICED_MARKETS). These are NOT the frozen League One/Two settings and are not frozen themselves: the model_freeze
  fingerprint and the League One/Two lockbox are untouched, and no pilot lockbox exists.
* Tips: only leagues in TIP_LEAGUES can produce a tip: Brazil Serie A (turned on by the maintainer on 2026-10-11, paused on
  2026-10-12 while the gap between the model and the bookmakers' prices was diagnosed, resumed on 2026-10-15). Every pilot
  league is scanned, priced and logged for evaluation; the Scottish leagues are never tipped.
* Odds are ladders (oddspapi.ladder_prices) or over/under lines (oddspapi.prop_prices): both parsers read the same
  reply. A "k+" step is the Over of line k - 0.5 on its own; the "0" step is the Under of 0.5. They go through the same
  quote building, allocator and high-value screen as the other scan.
* Every message says it is a pilot. The settlement statistic and void rule of bet365's tackles and fouls markets are
  NOT verified (MARKET_CONTRACT.md), and the first comparisons found bet365 pricing events well above the model's
  plain counts; the screen cannot detect that, so a tip here is a model-versus-price gap and nothing more.
* Tips are recorded in pilot state files (`pilot_*.jsonl`), never the real alert store, message index or ledger: a
  reply to a pilot message is not recorded as a bet, and the exposure of pilot tips is its own.
* Freshness is checked per league: a league with a finished match of the last STALE_AFTER missing from the pilot
  database is not priced in this run, and the run says so and exits STALE_EXIT, so the scheduled task reports it. The
  pilot database is kept current by `python -m prop_alpha.ingestion.results --pilot` (run before this scan).

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
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Final, Mapping, Sequence

import numpy as np

from prop_alpha.alerts import pilot_ledger, scan
from prop_alpha.alerts.fx import BOOK_CURRENCY, SUPPORTED_CURRENCIES, ExchangeRate, bankroll_currency, book_to_bankroll
from prop_alpha.alerts.fx import identity as fx_identity
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.predictions import PredictionLog
from prop_alpha.alerts.telegram import (
    PAPER_LABEL,
    AlertContext,
    DeliveryLog,
    MessageIndex,
    SentAlertStore,
    TelegramNotifier,
    TipLine,
    load_config,
)
from prop_alpha.config import DEFAULT_PILOT_DSN, PILOT_DSN_ENV
from prop_alpha.ingestion import oddspapi, sportmonks
from prop_alpha.ingestion.results import PILOT_REFUSED_FILE, load_refused, pilot_leagues
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

# The pilot's pricing settings. Game-state dispersion k per league and market, fitted on Sportmonks data (prop_alpha_pilot; the
# data vendor again since 2026-10-21) by scripts/quick_backtest.py --level on each league's matches before 2026-03-01, scored
# on its starters after that date (plain tackles_attempted): Brazil Serie A tackles 496.882 (no overdispersion) and fouls 40.556
# (2026-10-11, 4,808 starters in 253 matches); the English Championship tackles 21.1 and fouls none (2026-10-08, 4,196 starters
# in 234 matches); the Scottish Premiership tackles none and fouls 47.86 (2026-10-21, 1,795 starters in 105 matches: tackles
# Brier skill +0.054 to +0.081, fouls +0.051 and +0.060). "None" (no game-state overdispersion found) is k 1000. As on
# TheStatsAPI's data (2026-10-15), k = 6 calibrated tackles slightly better in the scored window; the fitted k is kept, because
# choosing on the scored window would make it selection data. TheStatsAPI's fits of 2026-10-15 (Brazil 34.276 / 14.396,
# Scottish Premiership 52.918 / 28.765) priced its own counts and no longer apply. GAME_STATE_K is the fallback for a league
# without its own: the English Championship's fit.
DEFAULT_GAME_STATE_K: Final[float] = 6.0
GAME_STATE_K: Final[Mapping[str, float]] = {"tackles": 21.1, "fouls": 1000.0}
LEAGUE_GAME_STATE_K: Final[Mapping[str, Mapping[str, float]]] = {
    "Brazil Serie A": {"tackles": 496.882, "fouls": 40.556},
    "Championship": {"tackles": 21.1, "fouls": 1000.0},
    "Scottish Premiership": {"tackles": 1000.0, "fouls": 47.86},
}
# Markets left unpriced per league because the stored counts are incomplete. TheStatsAPI records 2-8 tackles a match in the
# Scottish Championship (none at all in 10 of its 39 matches of August-September 2026) against about 30 in every other
# league, while its fouls look normal (about 23); tackles priced from that would be about a fifth of the truth.
# The gap is at the source, not in loading (checked 2026-10-15): a fresh request for Ayr United v Stenhousemuir
# (mt_200131818) still gives 2 tackles beside 201 duels won and 27 fouls; SofaScore's own team statistics show 0-0 and 2-0
# tackles for two October 2026 Scottish Championship matches; FotMob (Opta) publishes no team or player statistics for the
# league at all, while it does for the Scottish Premiership (Motherwell v Celtic: 15 and 14 tackles); and the Sportmonks
# archive (prop_alpha_pilot) shows the same 2-8 a match. No source available here holds the real counts.
UNPRICED_MARKETS: Final[Mapping[str, frozenset[str]]] = {"Scottish Championship": frozenset({"tackles"})}
PRIOR_STRENGTH: Final[Mapping[str, float]] = {"tackles": 6.0, "fouls": 6.0}
# Only tackles are tipped, as in the League One/Two scan; fouls are priced but never staked or tipped (the pilot's
# fouls calibration slopes were 0.86 and 0.89 against the 0.9-1.1 the PRD requires).
ALERT_MARKETS: Final[frozenset[str]] = frozenset({"tackles"})
# The guard against implausible edges (maintainer, 2026-10-12): a bookmaker price at which the model's probability, if the
# player plays, would return more than this per unit staked is left out before the allocator sees it. On the archived Brazil
# ladders of 2026-10-11 the lines the screen passed reached "+50% to +224%" on the players the books disagreed with most:
# information the model lacks (role, team-mates, opponent), not edges. A 25% cap is the same as a model probability at most
# 1.25 times the book's. Shrinking towards the market would not do this: a blended edge is a fraction of the model's, so the
# outliers would still rank first.
PLAUSIBLE_EDGE_CAP: Final[float] = 0.25
# The bookmaker check (maintainer, 2026-10-15: "find mispriced prices first, then check they are value"): a real price goes on to
# the value checks only when another bookmaker quotes the same selection (fixture, player, market, line, side) and this price is
# at least this much above the median of the other books' prices. So a tip needs two signals that do not share a cause: the
# model says the price is long, and the rest of the market prices it shorter. A price no other book quotes cannot be confirmed
# and is left out. Measured on the archived Brazil ladders (bet365 v Betway, 1,683 tackles selections both quoted, runs of
# 11, 12 and 18 Oct): the books differ widely (bet365/Betway price ratio 0.64 to 1.50 from the 10th to the 90th percentile,
# median 0.97; median gap at the 2+ and 3+ steps 5-7%), so 10% asks for more than the usual gap. Of the 37 archived prices that
# passed the model's 4%-25% edge rule, 16 had no second book and 16 were at least 10% above the other book. A judgement, not a
# fitted value: too few settled outcomes exist to fit it. None turns the check off. Mock odds are never checked (one book).
MARKET_CHECK_MARGIN: Final[float | None] = 0.10
# The leagues that may produce a tip. Brazil Serie A was turned on by the maintainer on 2026-10-11 ("yes"), after being shown its
# backtest (tackles Brier skill +0.06 to +0.08 over the league average, slopes 0.93-1.02, the strict PRD bin test failing) and
# that the bookmakers' prices run far above the model's counts (mean expected profit about -60%). The Scottish leagues have
# neither a backtest nor a bookmaker price: they are scanned and priced, never tipped. Adding a league here is a maintainer decision.
# PAUSED by the maintainer on 2026-10-12 ("pause brazil tipping and diagnose the gap"): the model sits at about half the books'
# level (0.22 v 0.38-0.40), so its few tips were the players it most likely misjudges. Re-adding "Brazil Serie A" restores it.
# RESUMED by the maintainer on 2026-10-15 ("resume tipping"), for Brazil Serie A only, as before the pause: since then the pilot
# prices the plain tackles count that SportyBet's settlements support (MARKET_CONTRACT.md 2e), the PLAUSIBLE_EDGE_CAP guard
# leaves out implausible edges, and Brazil's k was refitted on TheStatsAPI data. Still open: bet365's settlement rule, and the
# mean expected profit of about -37% the archived bet365 ladders leave on this count. Tips stay PAPER bets (pilot_ledger).
TIP_LEAGUES: Final[frozenset[str]] = frozenset({"Brazil Serie A"})
BOOKMAKERS_ENV: Final[str] = "LADDER_BOOKMAKERS"
# Brazil Serie A: Betway lists the tackles and fouls ladders for every fixture seen on 2026-10-11 (7 of 7), bet365 for 4 of 13.
DEFAULT_BOOKMAKERS: Final[tuple[str, ...]] = ("bet365", "betway")
# Finished matches this old (and not within STALE_GRACE of now, which ingestion may not have reached yet) must be in the
# pilot database before a league is priced.
STALE_AFTER: Final[timedelta] = timedelta(days=10)
STALE_GRACE: Final[timedelta] = timedelta(hours=4)
# Exit code of a run that skipped a league because its results were missing (the scheduled task reports the step).
STALE_EXIT: Final[int] = 4
# Matches Sportmonks refused to load (vendor data errors, 2026-10-08), kept for the Sportmonks archive only; TheStatsAPI's
# unloadable matches are recorded in PILOT_REFUSED_FILE by the results job.
KNOWN_VENDOR_ERRORS: Final[frozenset[str]] = frozenset({"19427700", "19431984", "19729100", "19427583"})
PILOT_LABEL: Final[str] = (
    "PILOT LEAGUES - model not validated for them; bet365's tackle and foul rules are unverified and its prices have run "
    "above the model's plain counts. Token stakes only."
)
PILOT_USAGE: Final[str] = (
    "Pilot tips are not recorded by the bet ledger: a reply to this message is ignored. Track any bet yourself."
)
PILOT_STATE_FILE: Final[Path] = scan.STATE_DIR / "pilot_sent_alerts.jsonl"
PILOT_INDEX_FILE: Final[Path] = scan.STATE_DIR / "pilot_alert_messages.jsonl"
PILOT_DELIVERY_FILE: Final[Path] = scan.STATE_DIR / "pilot_alert_deliveries.jsonl"
PILOT_PREDICTIONS_FILE: Final[Path] = scan.STATE_DIR / "pilot_predictions.jsonl"
# Written after every real run, so the status report can say when the pilot last scanned and what it found (audit 2026-10-11).
PILOT_HEARTBEAT_FILE: Final[Path] = scan.STATE_DIR / "pilot_last_scan.json"
PILOT_ODDS_DIR: Final[str] = "pilot"
LADDER_CATALOGUE_FILE: Final[str] = "ladder_markets.json"


# The frozen scan's two queries that read the tackles count, and the pilot's versions of them (pilot_ledger.PILOT_TACKLES_SQL).
PILOT_QUERIES: Final[Mapping[str, str]] = {sql: pilot_ledger.recount(sql) for sql in (scan.HISTORY_SQL, scan.LEVEL_SQL)}


class PilotCursor:
    """Runs the pilot's queries in place of the frozen scan's two count queries (PILOT_QUERIES) and passes every other query
    through, so scan.load_slate_inputs, which is frozen for League One/Two, loads the pilot's history and league levels on
    the pilot's tackles count without a copy of it. `queries` replaces PILOT_QUERIES (the diagnostics price other counts)."""

    def __init__(self, cur: Any, queries: Mapping[str, str] = PILOT_QUERIES) -> None:
        self._cur = cur
        self._queries = queries

    def execute(self, sql: str, params: Any = None) -> None:
        self._cur.execute(self._queries.get(sql, sql), params)

    def fetchall(self) -> list[Any]:
        return list(self._cur.fetchall())


def load_pilot_inputs(cur: Any, fixture: UpcomingFixture) -> scan.SlateInputs:
    """scan.load_slate_inputs on the pilot's tackles count."""
    return scan.load_slate_inputs(PilotCursor(cur), fixture)


def without_implausible_edges(
    quotes: Sequence[LiveOddsQuote], projections: Mapping[ProjectionKey, FactorProjection],
) -> tuple[list[LiveOddsQuote], int]:
    """Pure: `quotes` with every side whose edge exceeds PLAUSIBLE_EDGE_CAP removed (a quote with no side left is dropped),
    and how many sides were removed. The edge is p_win / (p_win + p_lose) x price - 1, the model's return if the player
    plays, under the quote's void rule; a quote without a projection is passed on (the allocator cannot price it)."""
    kept: list[LiveOddsQuote] = []
    removed = 0
    for quote in quotes:
        projection = projections.get((quote.fixture_id, quote.player_id, quote.market_type))
        prices: dict[str, float | None] = {"over": quote.over_price, "under": quote.under_price}
        for side, price in list(prices.items()):
            if price is None or projection is None:
                continue
            probabilities = projection.side_probabilities(quote.line_value, side, quote.void_rule)
            live = probabilities.p_win + probabilities.p_lose
            if live > 0.0 and probabilities.p_win / live * price - 1.0 > PLAUSIBLE_EDGE_CAP:
                prices[side] = None
                removed += 1
        if prices["over"] is not None or prices["under"] is not None:
            kept.append(replace(quote, over_price=prices["over"], under_price=prices["under"]))
    return kept, removed


def beating_other_books(
    quotes: Sequence[LiveOddsQuote], margin: float,
) -> tuple[list[LiveOddsQuote], int, int]:
    """Pure: `quotes` with every side left out unless its price is at least `margin` above the median price the other
    bookmakers quote for the same selection (fixture, player, market, line, side); a quote with no side left is dropped.
    Returns (kept, sides no other book quotes, sides not that far above the others)."""
    if not 0.0 <= margin < 10.0:
        raise ValueError(f"the bookmaker-check margin must be in [0, 10), not {margin!r}")
    books: dict[tuple[int, int, str, float, str], list[tuple[str, float]]] = defaultdict(list)
    for quote in quotes:
        for side, price in (("over", quote.over_price), ("under", quote.under_price)):
            if price is not None:
                books[(quote.fixture_id, quote.player_id, quote.market_type, quote.line_value, side)].append(
                    (quote.sportsbook, price))
    kept: list[LiveOddsQuote] = []
    single = in_line = 0
    for quote in quotes:
        prices: dict[str, float | None] = {"over": quote.over_price, "under": quote.under_price}
        for side, price in list(prices.items()):
            if price is None:
                continue
            others = [other for book, other in books[(quote.fixture_id, quote.player_id, quote.market_type,
                                                      quote.line_value, side)] if book != quote.sportsbook]
            if not others:
                prices[side] = None
                single += 1
            elif price < statistics.median(others) * (1.0 + margin):
                prices[side] = None
                in_line += 1
        if prices["over"] is not None or prices["under"] is not None:
            kept.append(replace(quote, over_price=prices["over"], under_price=prices["under"]))
    return kept, single, in_line


def pilot_settings_digest() -> str:
    """The identity of the pilot's pricing settings, written on every pilot prediction: a prediction made under other
    settings is never mistaken for one made under these (the pilot has no frozen code, so no source digest)."""
    settings = {"default_k": DEFAULT_GAME_STATE_K, "k": GAME_STATE_K, "league_k": LEAGUE_GAME_STATE_K,
                "prior": PRIOR_STRENGTH, "unpriced": {k: sorted(v) for k, v in UNPRICED_MARKETS.items()},
                "tackles_count": pilot_ledger.PILOT_TACKLES_SQL, "edge_cap": PLAUSIBLE_EDGE_CAP,
                "market_check": MARKET_CHECK_MARGIN}
    return "pilot:" + hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def pilot_engines(league: str) -> tuple[QuantEngine, dict[str, QuantEngine]]:
    """The engines for `league`: the default and one per market (LEAGUE_GAME_STATE_K, else GAME_STATE_K)."""
    ks = LEAGUE_GAME_STATE_K.get(league, GAME_STATE_K)
    return QuantEngine(game_state_k=DEFAULT_GAME_STATE_K), {m: QuantEngine(game_state_k=k) for m, k in ks.items()}


def price_pilot(
    fixture: UpcomingFixture, inputs: scan.SlateInputs, engine: QuantEngine,
    market_engines: Mapping[str, QuantEngine] | None = None,
) -> tuple[dict[ProjectionKey, FactorProjection], list[str]]:
    """The model's projections for one pilot-league fixture (scan.scan_fixture's `pricer`); pass the engines of
    `pilot_engines(fixture.league)`. A market in UNPRICED_MARKETS for the fixture's league is left out."""
    projections, notes = build_projections(
        fixture, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures, inputs.league_rates, engine,
        season_levels=inputs.season_levels or None, market_engines=market_engines, prior_strengths=PRIOR_STRENGTH,
    )
    unpriced = UNPRICED_MARKETS.get(fixture.league, frozenset())
    return {key: p for key, p in projections.items() if key[2] not in unpriced}, notes


def missing_by_league(
    finished: Sequence[tuple[str, datetime, str]], stored: Collection[str], now: datetime,
    refused: Collection[str] = (),
) -> dict[str, list[tuple[str, datetime]]]:
    """Pure: per league, the finished matches in `finished` ((Sportmonks fixture id, kickoff, league)) that are not in
    `stored`, oldest first, leaving out KNOWN_VENDOR_ERRORS, `refused` (the results job's recorded refusals) and those
    that kicked off within STALE_GRACE of `now` (ingestion may not have reached them). A league with none is not in the
    result."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    missing: dict[str, list[tuple[str, datetime]]] = {}
    for uid, kickoff, league in sorted(finished, key=lambda item: item[1]):
        if uid not in stored and uid not in KNOWN_VENDOR_ERRORS and uid not in refused and kickoff <= now - STALE_GRACE:
            missing.setdefault(league, []).append((uid, kickoff))
    return missing


def stale_message(league: str, missing: Sequence[tuple[str, datetime]]) -> str:
    examples = ", ".join(f"{uid} ({kickoff:%d %b})" for uid, kickoff in missing[:5])
    return (f"{league} skipped, not priced: {len(missing)} finished match(es) of the last {STALE_AFTER.days} days are not "
            f"in the pilot database (e.g. {examples}). Run `python -m prop_alpha.ingestion.results --pilot`, or "
            "`scripts/pilot_backfill.py --list --seed --ingest` for a league never loaded.")


def finished_listing(client: Any, now: datetime, leagues: Mapping[int, str]) -> list[tuple[str, datetime, str]]:
    """Finished matches of `leagues` from the last STALE_AFTER, from the Sportmonks listing (at most MAX_LISTING_PAGES)."""
    finished: list[tuple[str, datetime, str]] = []
    for page in range(1, scan.MAX_LISTING_PAGES + 1):
        body = client.get_json(sportmonks.upcoming_path(now - STALE_AFTER, now, page, leagues))
        rows, has_more = sportmonks.finished_with_leagues(body, leagues)
        finished += rows
        if not has_more:
            return finished
    raise ValueError(f"finished pilot listing has more than {scan.MAX_LISTING_PAGES} pages; check refused")


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


def collect_pilot_odds(
    client: Any, fixtures: Sequence[UpcomingFixture], squads: Mapping[int, Sequence[SquadPlayer]],
    now: datetime, window_end: datetime, books: Sequence[str], state_dir: Path,
) -> tuple[OddsCoverage, list[str], bool]:
    """Real quotes for `fixtures` from OddsPapi (ladders and over/under lines), summary lines, and whether real odds
    were available (False when the quota reserve stopped the fetch). Same quota rule as scan.collect_odds: the unmetered
    account endpoint is read first and odds are skipped when fewer than ODDS_QUOTA_RESERVE requests would remain. A
    bookmaker that reports no fixtures is recorded; any other failure, or a reply a parser does not understand, raises.
    One request per bookmaker covers every pilot league (at most five tournaments per request). Every raw reply is
    archived under pilot/ as <stamp>_<book>_pilot.json.gz."""
    empty = OddsCoverage((), 0, 0, (), ())
    cache = state_dir / scan.ODDS_CACHE_DIR
    line_file, ladder_file, names_file = cache / scan.CATALOGUE_FILE, cache / LADDER_CATALOGUE_FILE, cache / "participants.json"

    def fresh(path: Path) -> bool:
        return path.is_file() and (datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                                   < scan.CATALOGUE_MAX_AGE)

    catalogues_fresh = fresh(line_file) and fresh(ladder_file)
    left = scan._requests_left(client.get_json_value(oddspapi.ACCOUNT_PATH))  # noqa: SLF001 - the scan's own reader
    needed = len(books) + (0 if catalogues_fresh else 1) + (0 if names_file.is_file() else 1)
    if left - needed < scan.ODDS_QUOTA_RESERVE:
        return empty, [f"Odds NOT fetched: OddsPapi has {left} requests left; this run needs {needed} and "
                       f"{scan.ODDS_QUOTA_RESERVE} are kept in reserve"], False
    if catalogues_fresh:
        line_catalogue = scan.load_catalogue(line_file)
        ladder_catalogue = load_ladder_catalogue(ladder_file)
    else:
        markets = client.get_json_value(oddspapi.MARKETS_PATH)
        line_catalogue = oddspapi.line_catalogue(markets)
        ladder_catalogue = oddspapi.ladder_catalogue(markets)
        scan._save_catalogue(line_file, line_catalogue)  # noqa: SLF001 - the same file the League One/Two scan reads
        _save_ladder_catalogue(ladder_file, ladder_catalogue)
    tournaments = sorted({oddspapi.PILOT_TOURNAMENTS[f.league] for f in fixtures if f.league in oddspapi.PILOT_TOURNAMENTS})
    archive = state_dir / PILOT_ODDS_DIR
    archive.mkdir(parents=True, exist_ok=True)
    window_start = (fixtures[0].kickoff_at if fixtures else now) - FIXTURE_KICKOFF_TOLERANCE
    prices: list[oddspapi.PropPrice] = []
    no_odds: list[str] = []
    for book in books:
        body: Any = client.get_json_value(oddspapi.odds_path(book, tournaments), empty_on=oddspapi.NO_FIXTURES_REPLIES)
        if body is None:
            no_odds.append(book)
            continue
        with gzip.open(archive / f"{now:%Y%m%dT%H%M%SZ}_{book}_pilot.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(body, handle)
        book_prices = [*oddspapi.prop_prices(body, line_catalogue, book),
                       *oddspapi.ladder_as_prop_prices(oddspapi.ladder_prices(body, ladder_catalogue, book))]
        prices += [p for p in book_prices if window_start <= p.fixture.kickoff_at < window_end + FIXTURE_KICKOFF_TOLERANCE]
    names = scan._load_names(names_file) if names_file.is_file() else {}  # noqa: SLF001
    if any(team not in names for p in prices for team in (p.fixture.home_id, p.fixture.away_id)):
        names = oddspapi.participant_names(client.get_json_value(oddspapi.PARTICIPANTS_PATH))
        cache.mkdir(parents=True, exist_ok=True)
        names_file.write_text(json.dumps({str(k): v for k, v in names.items()}), encoding="utf-8")
    coverage = quotes_from_prices(prices, names, fixtures, squads, now)
    notes = [
        f"Odds: OddsPapi ({', '.join(books)}): {coverage.prices} tackles/fouls prices in this window, "
        f"{coverage.matched_prices} matched to our players, {len(coverage.quotes)} lines; "
        f"about {left - client.requests_sent + 1} OddsPapi requests left",
    ]
    if no_odds:
        notes.append(f"No odds for the pilot leagues from: {', '.join(no_odds)}")
    if coverage.unmatched_fixtures:
        notes.append(f"Odds for matches we could not match: {'; '.join(coverage.unmatched_fixtures)}")
    if coverage.unmatched_players:
        notes.append(f"Players we could not match: {len(coverage.unmatched_players)} "
                     f"(e.g. {'; '.join(coverage.unmatched_players[:3])})")
    return coverage, notes, True


# Fixtures given by hand (--fixture, 2026-10-12: the Sportmonks plan stopped listing Brazil Serie A): each club's latest stored
# final match must be at most this old, or the fixture is refused as stale (the listing-based freshness check needs Sportmonks).
MANUAL_HISTORY_MAX_AGE: Final[timedelta] = timedelta(days=10)
LATEST_STORED_SQL: Final[str] = """
    SELECT max(f.kickoff_at) FROM prop_alpha.fixtures f
    JOIN prop_alpha.teams t ON t.team_id IN (f.home_team_id, f.away_team_id)
    WHERE t.vendor_team_uid = %s AND f.status = 'final' AND f.kickoff_at < %s
"""


def parse_manual_fixture(raw: str) -> UpcomingFixture:
    """'league|sportmonks fixture id|home team id|away team id|home|away|kickoff (ISO, UTC)' -> UpcomingFixture."""
    parts = raw.split("|")
    if len(parts) != 7:
        raise ValueError(f"--fixture needs 7 fields separated by '|', got {len(parts)}: {raw!r}")
    league, fixture_id, home_uid, away_uid, home, away, kickoff = (part.strip() for part in parts)
    if league not in sportmonks.PILOT_LEAGUES_WANTED.values():
        raise ValueError(f"{league!r} is not a pilot league")
    if not fixture_id.isdigit():
        raise ValueError(f"the fixture id must be a Sportmonks fixture number, not {fixture_id!r}")
    return UpcomingFixture(int(fixture_id), league, home_uid, away_uid, home, away,
                           datetime.fromisoformat(kickoff).astimezone(timezone.utc))


def summary_text(summary: scan.ScanSummary, label: str) -> str:
    """The run's one-message summary (not scan.ScanSummary.text, whose wording is the League One/Two scan's)."""
    lines = [
        *((label,) if label else ()),
        f"Pilot leagues scan {summary.window_start:%a %d %b %H:%M}-{summary.window_end:%H:%M} UTC",
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
            lines.append(f"Tip-league matches with no bookmaker price (nothing to qualify against): {summary.unquoted_matches}")
    lines += summary.odds_notes
    if summary.fixtures and summary.tip_override is not None:
        lines.append(f"Tipped FOR THIS RUN ONLY (maintainer's choice): {', '.join(sorted(ALERT_MARKETS))} in "
                     f"{', '.join(summary.tip_override) or 'nothing'}; the standing setting is "
                     + (", ".join(sorted(TIP_LEAGUES)) if TIP_LEAGUES else "paused"))
    elif summary.fixtures:
        lines.append(f"Tipped: {', '.join(sorted(ALERT_MARKETS))} in {', '.join(sorted(TIP_LEAGUES))} only (fouls are priced, never tipped)"
                     if TIP_LEAGUES else "Tipped: nothing (tipping paused by the maintainer on 2026-10-12); every pilot league is "
                     "priced and logged for evaluation")
    lines += [f"Could not price {failure}" for failure in summary.failures]
    lines.append(PAPER_LABEL)
    return "\n".join(lines)


def run_ladder_scan(
    now: datetime, client: Any, dsn: str, notifier: TelegramNotifier | None, store: SentAlertStore,
    bankroll: float, label: str, max_alerts: int = scan.DEFAULT_MAX_ALERTS, *,
    odds: str = "mock", odds_client: Any | None = None, books: Sequence[str] = DEFAULT_BOOKMAKERS,
    state_dir: Path = scan.STATE_DIR, currency: str, book_rate: ExchangeRate | None = None,
    predictions: PredictionLog | None = None, paper_bets: PlacedBetStore | None = None,
    tips_file: Path = pilot_ledger.PILOT_TIPS_FILE, manual: Sequence[UpcomingFixture] = (),
    tip_leagues: Collection[str] | None = None,
) -> scan.ScanSummary:
    """One scan of the pilot-league matches kicking off in [now, next 4-hour boundary). `store` is the pilot (or mock)
    alert store: exposure is net of its stakes only. A league whose results are missing from the pilot database is
    skipped and recorded in `summary.failures` (the caller exits STALE_EXIT). With `predictions`, every fixture's Gate 2 lines
    are logged before anything is sent (a failed write raises, so no tip goes out on a price that was not recorded); with
    `paper_bets`, each tip is recorded as a paper bet just before its message is sent (alerts/pilot_ledger.py).

    With `manual` fixtures (given by hand when the Sportmonks plan does not list their league) nothing is listed through
    Sportmonks: the window runs from `now` to the last kickoff, and a fixture is refused when either club's latest stored
    match is older than MANUAL_HISTORY_MAX_AGE. `tip_leagues` replaces TIP_LEAGUES for this run only, and says so."""
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
    tipping = TIP_LEAGUES if tip_leagues is None else frozenset(tip_leagues)
    if manual:
        if any(fixture.kickoff_at <= now for fixture in manual):
            raise ValueError("a fixture given by hand has already kicked off")
        start, end = now, max(fixture.kickoff_at for fixture in manual) + timedelta(minutes=1)
    else:
        start, end = scan_window(now)
    conversion = "" if rate.identity else f"; book limits converted at {rate.describe()}"
    summary = scan.ScanSummary(start, end, alert_cap=max_alerts, bankroll=f"{currency} {bankroll:,.2f}{conversion}",
                               tip_override=None if tip_leagues is None else tuple(sorted(tipping)))
    if manual:
        return _scan_slate(now, list(manual), None, dsn, notifier, store, bankroll, label, max_alerts, summary, tipping,
                           start, end, rate, currency, odds=odds, odds_client=odds_client, books=books, state_dir=state_dir,
                           predictions=predictions, paper_bets=paper_bets, tips_file=tips_file)
    leagues, absent = pilot_leagues(client)
    if absent:
        summary.odds_notes.append(f"Not in the Sportmonks plan, not scanned: {', '.join(name for name in absent)}")
    if not leagues:
        return summary
    listed = scan.fetch_upcoming(client, start, end, leagues)
    if not listed:
        return summary
    return _scan_slate(now, listed, finished_listing(client, now, leagues), dsn, notifier, store, bankroll, label, max_alerts,
                       summary, tipping, start, end, rate, currency, odds=odds, odds_client=odds_client, books=books,
                       state_dir=state_dir, predictions=predictions, paper_bets=paper_bets, tips_file=tips_file)


def _scan_slate(
    now: datetime, listed: list[UpcomingFixture], finished: Sequence[tuple[str, datetime, str]] | None, dsn: str,
    notifier: TelegramNotifier | None, store: SentAlertStore, bankroll: float, label: str, max_alerts: int,
    summary: scan.ScanSummary, tipping: frozenset[str], start: datetime, end: datetime, rate: ExchangeRate, currency: str, *,
    odds: str, odds_client: Any | None, books: Sequence[str], state_dir: Path, predictions: PredictionLog | None,
    paper_bets: PlacedBetStore | None, tips_file: Path,
) -> scan.ScanSummary:
    """run_ladder_scan from the listed fixtures on. `finished` is the Sportmonks listing of recent finished matches for the
    freshness check, or None for fixtures given by hand, which are checked club by club instead (MANUAL_HISTORY_MAX_AGE)."""
    import psycopg2

    summary.fixtures = [f"{f.name} {f.kickoff_at:%H:%M} ({f.league})" for f in listed]
    summary.odds_notes.append("Leagues: " + ", ".join(f"{name} {count}" for name, count in sorted(Counter(f.league for f in listed).items())))
    if finished is None:
        summary.odds_notes.append("Fixtures given by hand (--fixture): not listed through Sportmonks")
    conn = psycopg2.connect(dsn, options="-c statement_timeout=30000 -c default_transaction_read_only=on",
                            connect_timeout=10)
    try:
        with conn:
            with conn.cursor() as cur:
                if finished is None:
                    fixtures = []
                    for fixture in listed:
                        latest: list[datetime] = []
                        for team in (fixture.home_team_uid, fixture.away_team_uid):
                            cur.execute(LATEST_STORED_SQL, (team, fixture.kickoff_at))
                            found = cur.fetchone()
                            if found is not None and found[0] is not None:
                                latest.append(found[0].astimezone(timezone.utc))
                        if len(latest) < 2 or min(latest) < now - MANUAL_HISTORY_MAX_AGE:
                            summary.failures.append(f"{fixture.name} skipped, not priced: a club's latest stored match is older "
                                                    f"than {MANUAL_HISTORY_MAX_AGE.days} days or missing")
                            summary.stale_leagues.append(fixture.league)
                        else:
                            fixtures.append(fixture)
                            summary.odds_notes.append(f"{fixture.name}: both clubs' history stored to {min(latest):%d %b}")
                else:
                    cur.execute("SELECT vendor_fixture_uid FROM prop_alpha.fixtures WHERE status = 'final'")
                    stored = {str(row[0]) for row in cur.fetchall()}
                    refused = load_refused(state_dir / PILOT_REFUSED_FILE.name)
                    stale = missing_by_league(finished, stored, now, refused)
                    unloadable = Counter(league for uid, _, league in finished if uid in refused and uid not in stored)
                    if unloadable:
                        summary.odds_notes.append("Recent matches the data feed cannot load (no tackles or other data defect), "
                                                  "priced without them: " + ", ".join(f"{name} {count}"
                                                                                      for name, count in sorted(unloadable.items())))
                    for league, missing in sorted(stale.items()):
                        summary.failures.append(stale_message(league, missing))
                        summary.stale_leagues.append(league)
                    fixtures = [f for f in listed if f.league not in stale]
                slate = [(fixture, load_pilot_inputs(cur, fixture)) for fixture in fixtures]
    finally:
        conn.close()
    if not fixtures:
        return summary
    if odds == "oddspapi":
        if odds_client is None:
            raise ValueError("--odds oddspapi needs an OddsPapi client")
        squads = {fixture.fixture_id: inputs.squad for fixture, inputs in slate}
        coverage, notes, available = collect_pilot_odds(odds_client, fixtures, squads, now, end, books, state_dir)
        summary.odds_notes += notes
        summary.odds_unavailable = not available
        real_quotes = coverage.quotes

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            if fixture.league not in tipping:
                return []
            return scan.in_bankroll_currency([q for q in real_quotes if q.fixture_id == fixture.fixture_id
                                              and q.market_type in ALERT_MARKETS], rate)
    elif odds == "mock":
        seed = int.from_bytes(hashlib.sha256(start.isoformat().encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)

        def quotes_for(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
            if fixture.league not in tipping:
                return []
            return scan.in_bankroll_currency([q for q in mock_quotes(projections, now, rng)
                                              if q.market_type in ALERT_MARKETS], rate)
    else:
        raise ValueError(f"unknown odds source {odds!r}")
    implausible: list[int] = []
    one_book: list[int] = []
    in_line: list[int] = []

    def guarded(fixture: UpcomingFixture, projections: Mapping[ProjectionKey, FactorProjection]) -> list[LiveOddsQuote]:
        quotes = quotes_for(fixture, projections)
        margin = MARKET_CHECK_MARGIN
        if odds == "oddspapi" and margin is not None:
            # The bookmaker check first, on every book's raw price, then the model's implausible-edge guard.
            quotes, single, level = beating_other_books(quotes, margin)
            one_book.append(single)
            in_line.append(level)
        kept, removed = without_implausible_edges(quotes, projections)
        implausible.append(removed)
        return kept

    manager = InstitutionalAssetManager(bankroll, model_risk_loss_floor=MODEL_RISK_LOSS_FLOOR)
    combined = AllocationReport()
    all_projections: dict[ProjectionKey, FactorProjection] = {}
    all_descriptions: dict[ProjectionKey, AlertContext] = {}
    exposure = store.exposure(now)
    for fixture, inputs in slate:
        engine, market_engines = pilot_engines(fixture.league)
        projections, report, quotes = scan.scan_fixture(fixture, inputs, now, guarded, manager, engine, summary,
                                                        exposure, market_engines, pricer=price_pilot)
        if report is None:
            continue
        if predictions is not None and projections:
            predictions.record(fixture, projections, now, start, digest=pilot_settings_digest())
        if odds == "oddspapi" and fixture.league in tipping and not quotes and projections:
            summary.unquoted_matches += 1
        summary.players_priced += len({key[1] for key in projections})
        summary.quotes += len(quotes)
        summary.directives += len(report.directives)
        combined.directives.extend(report.directives)
        all_projections.update(projections)
        all_descriptions.update(scan.descriptions(fixture, inputs, projections))
    if sum(one_book) or sum(in_line):
        summary.odds_notes.append(f"Bookmaker check (a tip needs another book pricing it at least {MARKET_CHECK_MARGIN:.0%} "
                                  f"shorter): {sum(one_book)} price(s) no other book quotes and {sum(in_line)} in line "
                                  "with the other books were left out")
    if sum(implausible):
        summary.odds_notes.append(f"Prices left out as implausible (the model above the book by more than "
                                  f"{PLAUSIBLE_EDGE_CAP:.0%} if the player plays): {sum(implausible)}")
    combined.directives.sort(key=lambda d: (-d.ev_per_unit, d.fixture_id, d.player_id, d.market_type, d.line_value))
    if notifier is not None:
        fixtures_by_id = {f.fixture_id: f for f in fixtures}

        def record_paper_bets(tips: Sequence[TipLine]) -> None:
            if paper_bets is not None:
                pilot_ledger.record_tips(paper_bets, tips_file, tips, fixtures_by_id, now)

        summary.alerts_sent, _ = notifier.notify_digest(
            combined, all_projections, bankroll_currency=currency, label=label, descriptions=all_descriptions,
            max_alerts=max_alerts, book_rate=rate, usage=PILOT_USAGE, on_tips=record_paper_bets,
        )
    return summary


def heartbeat_record(summary: scan.ScanSummary, now: datetime) -> dict[str, object]:
    """Pure: what the status report shows about the latest real pilot run."""
    return {"at": now.isoformat(), "window_end": summary.window_end.isoformat(), "fixtures": len(summary.fixtures),
            "players_priced": summary.players_priced, "quotes": summary.quotes, "tips": summary.alerts_sent,
            "failures": len(summary.failures), "stale_leagues": list(summary.stale_leagues),
            "odds_unavailable": summary.odds_unavailable}


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("prop_alpha.scanner").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Scan the next pilot-league matches and tip high-value bets (pilot).")
    parser.add_argument("--odds", choices=("oddspapi", "mock"), required=True,
                        help="oddspapi: real bet365 prices; mock: model price plus noise, labelled MOCK")
    parser.add_argument("--send", action="store_true", help="send tips and the summary to Telegram")
    parser.add_argument("--at", type=datetime.fromisoformat, default=None, help="simulated UTC run time (testing)")
    parser.add_argument("--fixture", action="append", default=[], metavar="SPEC", type=parse_manual_fixture,
                        help="scan this fixture instead of the Sportmonks listing (repeatable): "
                             "'league|sportmonks fixture id|home team id|away team id|home|away|kickoff ISO UTC'")
    parser.add_argument("--tip", action="append", default=None, metavar="LEAGUE",
                        help="tip this league for this run only, whatever TIP_LEAGUES says (repeatable; needs --fixture)")
    args = parser.parse_args(argv)
    if args.at is not None and args.odds == "oddspapi":
        parser.error("--at simulates the run time, so it works only with --odds mock; real odds are priced now")
    if args.tip is not None and not args.fixture:
        parser.error("--tip is a one-off: it works only with --fixture")
    manual: list[UpcomingFixture] = args.fixture
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
            # Mock runs keep no record: their tips are not tips. Real runs log predictions and record paper bets.
            predictions=PredictionLog(PILOT_PREDICTIONS_FILE) if real else None,
            paper_bets=PlacedBetStore(pilot_ledger.PILOT_BETS_FILE) if real and args.send else None,
            manual=manual, tip_leagues=args.tip,
        )
    finally:
        client.close()
        if odds_client is not None:
            odds_client.close()
    text = summary_text(summary, label)
    print(text)
    if real and not manual:  # the heartbeat is the scheduled scan's; a run by hand does not stand in for it
        PILOT_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PILOT_HEARTBEAT_FILE.write_text(json.dumps(heartbeat_record(summary, now)), encoding="utf-8")
    # Every 4 hours most windows have no pilot match: the chat hears about a run only when there was something to say.
    if notifier is not None and (summary.fixtures or summary.failures):
        print(f"summary sent (message_id {notifier.send_message(text)})")
    if summary.stale_leagues:
        return STALE_EXIT
    return scan.ODDS_UNAVAILABLE_EXIT if real and summary.odds_unavailable else 0


if __name__ == "__main__":
    sys.exit(main())
