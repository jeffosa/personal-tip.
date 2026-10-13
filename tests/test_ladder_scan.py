"""The pilot scan (alerts/ladder_scan.py): Brazil Serie A and the Scottish leagues, pilot pricing, ladder quotes, the
per-league stale-database gate, the tip policy, and the pilot's own alert state."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from prop_alpha.alerts import ladder_scan, pilot_ledger, scan
from prop_alpha.alerts.fx import identity as fx_identity
from prop_alpha.alerts.telegram import SentAlertStore, TelegramConfig, TelegramNotifier
from prop_alpha.ingestion import oddspapi, sportmonks
from prop_alpha.modeling.engine import QuantEngine
from prop_alpha.scanner.market_scanner import LiveOddsQuote
from prop_alpha.scanner.slate import (
    Appearance,
    SquadPlayer,
    UpcomingFixture,
    build_projections,
    quotes_from_prices,
)

FAKE_TOKEN = "123456789" + ":" + "AAH" + "x" * 32
NOW = datetime(2026, 10, 13, 16, 30, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 10, 13, 18, 45, tzinfo=timezone.utc)
FIXTURE = UpcomingFixture(9101, "Brazil Serie A", "76", "348", "Walsall", "Port Vale", KICKOFF)
RATES = {(position, market): rate for position in ("DEF", "MID") for market, rate in (("tackles", 1.8), ("fouls", 1.2))}
FINISHED_UID = "7001"
SCOTTISH_ID = 9103


def _history(player_id: int, team: str, starts: int, n: int = 10) -> list[Appearance]:
    rows = []
    for week in range(n):
        recent_index = n - 1 - week
        started = recent_index < starts if recent_index < 5 else True
        rows.append(Appearance(player_id, 100 + week, team, KICKOFF - timedelta(days=7 * (n - week)), started,
                               90.0 if started else 0.0, 2 if started else 0, 1 if started else 0))
    return rows


def _inputs() -> scan.SlateInputs:
    squad = (SquadPlayer(1, "Regular Starter", "MID", "76"), SquadPlayer(3, "Away Starter", "DEF", "348"))
    history = {1: tuple(_history(1, "76", 5)), 3: tuple(_history(3, "348", 4))}
    recent = {"76": (105, 106, 107, 108, 109), "348": (105, 106, 107, 108, 109)}
    return scan.SlateInputs(squad, history, recent, RATES)


def _row(fixture_id: int, state: str, kickoff: datetime, league: int = 648,
         names: tuple[str, str] = ("Walsall", "Port Vale")) -> dict[str, Any]:
    return {"id": fixture_id, "league_id": league, "state": {"developer_name": state},
            "starting_at_timestamp": int(kickoff.timestamp()),
            "participants": [{"id": 76, "name": names[0], "meta": {"location": "home"}},
                             {"id": 348, "name": names[1], "meta": {"location": "away"}}]}


LEAGUE_LIST = {"data": [{"id": 648, "name": "Serie A", "country": {"name": "Brazil"}},
                        {"id": 12, "name": "League One", "country": {"name": "England"}},
                        {"id": 501, "name": "Premiership", "country": {"name": "Scotland"}}],
               "pagination": {"has_more": False}}


class _StubSportmonks:
    """The plan lists Brazil Serie A and the Scottish Premiership (not the Scottish Championship). Every fixture
    listing answers with a Brazil Serie A match and a Scottish Premiership match about to start, a League One match, and
    a finished Brazil Serie A match."""

    def __init__(self, finished_kickoff: datetime = NOW - timedelta(days=5)) -> None:
        self.rows = [_row(9101, "NS", KICKOFF), _row(SCOTTISH_ID, "NS", KICKOFF, 501, ("Celtic", "Rangers")),
                     _row(9102, "NS", KICKOFF, league=12), _row(int(FINISHED_UID), "FT", finished_kickoff)]
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        if path.startswith("leagues?"):
            return LEAGUE_LIST
        return {"data": self.rows, "pagination": {"has_more": False}}


class _Recording(TelegramNotifier):
    def __init__(self, store: SentAlertStore) -> None:
        super().__init__(TelegramConfig(FAKE_TOKEN, "1"), store=store, sleep=lambda s: None)
        self.texts: list[str] = []

    def send_message(self, text: str, reply_to: int | None = None) -> int:
        self.texts.append(text)
        return len(self.texts)


def _generous_mock_quotes(projections: Mapping[Any, Any], captured_at: datetime, rng: Any) -> list[LiveOddsQuote]:
    """A mock book that prices every tackles 0.5 Over well above the model, so the screen has a bet to find."""
    return [LiveOddsQuote(key[0], key[1], key[2], 0.5, 2.6, 1.5, 100.0, "mock-book", captured_at, "void_if_no_appearance")
            for key in sorted(projections) if key[2] == "tackles"]


def _patch_database(monkeypatch: pytest.MonkeyPatch, stored: set[str]) -> None:
    class _Conn:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> Any:
            return self

        def execute(self, sql: str) -> None:
            assert "status = 'final'" in sql

        def fetchall(self) -> list[tuple[str]]:
            return [(uid,) for uid in sorted(stored)]

        def close(self) -> None:
            return None

    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    monkeypatch.setattr(scan, "load_slate_inputs", lambda cur, fixture: _inputs())


def _run(tmp_path: Path, notifier: _Recording, store: SentAlertStore, **kwargs: Any) -> scan.ScanSummary:
    options: dict[str, Any] = {"odds": "mock", "state_dir": tmp_path, "currency": "GBP"}
    return ladder_scan.run_ladder_scan(NOW, kwargs.pop("client", _StubSportmonks()), "postgresql://unused", notifier, store,
                                       1000.0, kwargs.pop("label", ladder_scan.PILOT_LABEL), **{**options, **kwargs})


# ---------------- pricing ----------------
def _tip_brazil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tipping is paused (TIP_LEAGUES is empty since 2026-10-12); the tests of the tipping path switch Brazil back on. Their
    prices are exaggerated to make a tip certain, so they also lift the implausible-edge guard, which has tests of its own."""
    monkeypatch.setattr(ladder_scan, "TIP_LEAGUES", frozenset({"Brazil Serie A"}))
    monkeypatch.setattr(ladder_scan, "PLAUSIBLE_EDGE_CAP", 100.0)


def _quote(over: float | None, under: float | None) -> LiveOddsQuote:
    return LiveOddsQuote(9101, 1, "tackles", 1.5, over, under, 100.0, "bet365", NOW, "void_if_no_appearance")


def test_a_price_more_than_25_percent_above_the_book_is_left_out_side_by_side() -> None:
    """2026-10-12: on the archived Brazil ladders the screen's tips were the players the books disagreed with most, at
    'EV +50% to +224%'. A side whose edge (the model's return if the player plays) exceeds the cap never reaches the allocator."""
    projections, _ = ladder_scan.price_pilot(FIXTURE, _inputs(), *ladder_scan.pilot_engines(FIXTURE.league))
    over = projections[(9101, 1, "tackles")].side_probabilities(1.5, "over")
    under = projections[(9101, 1, "tackles")].side_probabilities(1.5, "under")
    q_over, q_under = over.p_win / (over.p_win + over.p_lose), under.p_win / (under.p_win + under.p_lose)
    fair_over, fair_under = 1.0 / q_over, 1.0 / q_under
    assert ladder_scan.PLAUSIBLE_EDGE_CAP == 0.25
    within = _quote(fair_over * 1.20, fair_under * 1.10)
    kept, removed = ladder_scan.without_implausible_edges([within], projections)
    assert kept == [within] and removed == 0
    too_good = _quote(fair_over * 1.30, fair_under * 1.10)  # the Over is implausible, the Under is not
    kept, removed = ladder_scan.without_implausible_edges([too_good], projections)
    assert removed == 1 and kept == [_quote(None, fair_under * 1.10)]
    both = _quote(fair_over * 1.30, None)
    assert ladder_scan.without_implausible_edges([both], projections) == ([], 1)  # nothing left: the quote goes
    unpriced = replace(within, player_id=999)
    assert ladder_scan.without_implausible_edges([unpriced], projections) == ([unpriced], 0)


def test_with_the_guard_on_the_juicy_ladder_is_left_out_and_the_run_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real-odds tip test's 2+ price of 9.00 is far beyond 25% above the model: with tipping on and the guard at its
    default, nothing is tipped and the summary counts the prices left out."""
    monkeypatch.setattr(ladder_scan, "TIP_LEAGUES", frozenset({"Brazil Serie A"}))
    _patch_database(monkeypatch, stored={FINISHED_UID})
    _names_cache(tmp_path)
    odds = _FakeOdds(_ladder_body({"Regular Starter": (2.6, 9.0, 1.45)}))
    store = SentAlertStore(tmp_path / "pilot.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store, odds="oddspapi", odds_client=odds)
    assert summary.alerts_sent == 0 and notifier.texts == [] and store.load() == []
    [note] = [n for n in summary.odds_notes if n.startswith("Prices left out as implausible")]
    assert "more than 25% if the player plays" in note and int(note.rsplit(": ", 1)[1]) >= 1


def test_tipping_is_paused_and_the_summary_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The maintainer turned Brazil Serie A on (2026-10-11) and paused it (2026-10-12) until the model-v-bookmaker gap is
    explained; the Scottish leagues have no backtest and no bookmaker price."""
    assert ladder_scan.TIP_LEAGUES == frozenset()
    assert "Tipped: nothing (tipping paused by the maintainer on 2026-10-12)" in ladder_scan.summary_text(
        scan.ScanSummary(NOW, NOW, fixtures=["x"]), "")
    _tip_brazil(monkeypatch)
    assert "Tipped: tackles in Brazil Serie A only" in ladder_scan.summary_text(scan.ScanSummary(NOW, NOW, fixtures=["x"]), "")


class _QueryLog:
    """A cursor that records every query and answers each with no rows."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.queries.append(sql)

    def fetchall(self) -> list[Any]:
        return []


def test_the_pilot_loads_history_and_league_levels_on_its_own_tackles_count() -> None:
    """2026-10-13: the pilot is back on plain tackles_attempted (settled results contradicted tackles plus challenges lost,
    MARKET_CONTRACT.md 2e), so its queries are the frozen ones. The count stays a setting: another count is loaded through
    the same frozen loader by rewriting its two count queries only."""
    assert pilot_ledger.PILOT_TACKLES_SQL == "s.tackles_attempted"
    pilot, frozen = _QueryLog(), _QueryLog()
    ladder_scan.load_pilot_inputs(pilot, FIXTURE)
    scan.load_slate_inputs(frozen, FIXTURE)
    assert scan.HISTORY_SQL in frozen.queries and scan.LEVEL_SQL in frozen.queries
    assert pilot.queries == frozen.queries
    wider = {sql: pilot_ledger.recount(sql, pilot_ledger.TACKLES_PLUS_CHALLENGES_LOST_SQL) for sql in ladder_scan.PILOT_QUERIES}
    other = _QueryLog()
    scan.load_slate_inputs(ladder_scan.PilotCursor(other, wider), FIXTURE)
    recounted = [sql for sql in other.queries if "dribbled_past" in sql]
    assert recounted == [wider[scan.HISTORY_SQL], wider[scan.LEVEL_SQL]]
    assert "(s.tackles_attempted + s.dribbled_past), s.fouls_committed" in recounted[0]
    assert "sum((s.tackles_attempted + s.dribbled_past)), sum(s.fouls_committed)" in recounted[1]
    assert len(other.queries) == len(frozen.queries)  # every other query passes through unchanged


def test_the_pilot_digest_names_its_tackles_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prediction priced on the old count is never mistaken for one priced on the new."""
    current = ladder_scan.pilot_settings_digest()
    monkeypatch.setattr(pilot_ledger, "PILOT_TACKLES_SQL", pilot_ledger.TACKLES_PLUS_CHALLENGES_LOST_SQL)
    assert ladder_scan.pilot_settings_digest() != current


def test_the_pilot_prices_with_its_own_settings_by_league_not_the_frozen_ones() -> None:
    inputs = _inputs()
    scottish = replace(FIXTURE, league="Scottish Premiership")
    engine, market_engines = ladder_scan.pilot_engines(scottish.league)
    fallback, _ = ladder_scan.price_pilot(scottish, inputs, engine, market_engines)
    # A league without its own fit uses the English Championship's: tackles 21.1, fouls 1000.
    assert fallback[(9101, 1, "tackles")].game_state_k == 21.1 and fallback[(9101, 1, "fouls")].game_state_k == 1000.0
    brazil, _ = ladder_scan.price_pilot(FIXTURE, inputs, *ladder_scan.pilot_engines(FIXTURE.league))
    assert brazil[(9101, 1, "tackles")].game_state_k == 496.882 and brazil[(9101, 1, "fouls")].game_state_k == 40.556
    frozen, _ = scan.price_fixture(FIXTURE, inputs, *scan.scan_engines())
    assert frozen[(9101, 1, "tackles")].game_state_k == scan.SCAN_GAME_STATE_K_BY_MARKET["tackles"] not in (21.1, 496.882)
    # The pilot applies no rate multiplier and its own prior strength: the same call by hand agrees.
    by_hand, _ = build_projections(FIXTURE, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures,
                                   RATES, QuantEngine(game_state_k=6.0),
                                   market_engines={m: QuantEngine(game_state_k=k)
                                                   for m, k in ladder_scan.LEAGUE_GAME_STATE_K["Brazil Serie A"].items()},
                                   prior_strengths=ladder_scan.PRIOR_STRENGTH)
    assert brazil[(9101, 1, "tackles")].rate_posterior.alpha == pytest.approx(by_hand[(9101, 1, "tackles")].rate_posterior.alpha)


# ---------------- listings ----------------
PILOT = {648: "Brazil Serie A", 501: "Scottish Premiership"}


def test_the_plans_league_list_resolves_the_wanted_leagues_and_reports_the_rest() -> None:
    rows, more = sportmonks.league_rows(LEAGUE_LIST)
    found, absent = sportmonks.resolve_leagues(rows)
    assert not more and found == PILOT and absent == ["Scottish Championship"]
    # A league is told apart by country; an unwanted league (England's Championship, no longer a focus) is ignored.
    both = [*rows, {"id": 504, "name": "Championship", "country": {"name": "Scotland"}}]
    assert sportmonks.resolve_leagues(both) == ({**PILOT, 504: "Scottish Championship"}, [])
    english = {"id": 9, "name": "Championship", "country": {"name": "England"}}
    assert sportmonks.resolve_leagues([english, {"id": 648, "name": "Serie A", "country": None}]) == (
        {}, ["Brazil Serie A", "Scottish Championship", "Scottish Premiership"])
    with pytest.raises(ValueError, match="no 'data' list"):
        sportmonks.league_rows({"oops": 1})


def test_the_listing_helpers_default_to_league_one_and_two_and_take_the_pilot_leagues() -> None:
    body = {"data": [_row(1, "NS", KICKOFF, 648), _row(2, "NS", KICKOFF, 12), _row(3, "FT", KICKOFF, 648),
                     _row(4, "FT", KICKOFF, 501), _row(5, "NS", KICKOFF, 501)], "pagination": {"has_more": False}}
    rows, _ = sportmonks.upcoming_fixtures(body, PILOT)
    assert [(r["fixture_id"], r["league"]) for r in rows] == [(1, "Brazil Serie A"), (5, "Scottish Premiership")]
    assert [r["fixture_id"] for r in sportmonks.upcoming_fixtures(body)[0]] == [2]  # the default is unchanged
    assert sportmonks.finished_with_leagues(body, PILOT)[0] == [("3", KICKOFF, "Brazil Serie A"),
                                                                ("4", KICKOFF, "Scottish Premiership")]
    assert [uid for uid, _ in sportmonks.finished_fixtures(body, PILOT)[0]] == ["3", "4"]
    assert sportmonks.finished_fixtures(body)[0] == []
    assert "fixtureLeagues:501,648&" in sportmonks.upcoming_path(NOW, NOW, 1, PILOT)
    assert "fixtureLeagues:12,14&" in sportmonks.upcoming_path(NOW, NOW, 1)


# ---------------- the stale-database gate ----------------
def test_missing_by_league_lists_finished_matches_not_stored_and_spares_the_last_hours() -> None:
    finished = [("1", NOW - timedelta(days=4), "Brazil Serie A"), ("2", NOW - timedelta(days=9), "Brazil Serie A"),
                ("3", NOW - timedelta(hours=1), "Brazil Serie A"), ("4", NOW - timedelta(days=2), "Brazil Serie A"),
                ("5", NOW - timedelta(days=2), "Scottish Premiership")]
    assert ladder_scan.missing_by_league(finished, {"4"}, NOW) == {
        "Brazil Serie A": [("2", NOW - timedelta(days=9)), ("1", NOW - timedelta(days=4))],
        "Scottish Premiership": [("5", NOW - timedelta(days=2))]}
    # "3" kicked off an hour ago (not yet due); a league with nothing missing is not listed.
    assert ladder_scan.missing_by_league(finished, {"1", "2", "4"}, NOW) == {
        "Scottish Premiership": [("5", NOW - timedelta(days=2))]}
    assert ladder_scan.missing_by_league([("19729100", NOW - timedelta(days=3), "Brazil Serie A")], set(), NOW) == {}
    with pytest.raises(ValueError, match="UTC"):
        ladder_scan.missing_by_league(finished, set(), datetime(2026, 10, 13, 16))


def test_a_stale_league_is_skipped_and_named_while_the_others_are_still_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finished Brazil Serie A match is not stored: Brazil is not priced and the run says how to fix it,
    but the Scottish Premiership (nothing finished in its listing) is still priced."""
    _patch_database(monkeypatch, stored=set())
    store = SentAlertStore(tmp_path / "s.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store)
    [failure] = summary.failures
    assert failure.startswith("Brazil Serie A skipped, not priced") and FINISHED_UID in failure
    assert "ingestion.results --pilot" in failure and "pilot_backfill.py --list --seed --ingest" in failure
    assert summary.players_priced == 2 and notifier.texts == []  # the Scottish fixture only; it is never tipped
    assert "Could not price Brazil Serie A skipped" in ladder_scan.summary_text(summary, "")
    assert summary.stale_leagues == ["Brazil Serie A"]


def test_a_window_with_no_pilot_match_reads_no_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: pytest.fail("the database must not be opened"))
    empty = _StubSportmonks()
    empty.rows = [_row(9102, "NS", KICKOFF, league=12)]  # League One only: the other scan's
    summary = _run(tmp_path, _Recording(SentAlertStore(tmp_path / "s.jsonl")), SentAlertStore(tmp_path / "s.jsonl"),
                   client=empty)
    assert summary.fixtures == [] and "Matches in this window: 0" in ladder_scan.summary_text(summary, "")
    assert all("fixtureLeagues:501,648&" in path for path in empty.paths if not path.startswith("leagues?"))


def test_a_token_with_none_of_the_pilot_leagues_scans_nothing_and_says_so(tmp_path: Path) -> None:
    bare = _StubSportmonks()
    bare.get_json = lambda path: {"data": [], "pagination": {"has_more": False}}  # type: ignore[method-assign]
    summary = _run(tmp_path, _Recording(SentAlertStore(tmp_path / "s.jsonl")), SentAlertStore(tmp_path / "s.jsonl"),
                   client=bare)
    assert summary.fixtures == []
    assert "Not in the Sportmonks plan, not scanned: Brazil Serie A, Scottish Championship, Scottish Premiership" in summary.odds_notes[0]


# ---------------- ladder quotes ----------------
def _ladder_price(step: str, line: float, side: str, price: float, name: str = "Regular Starter") -> oddspapi.LadderPrice:
    fixture = oddspapi.OddsFixture("id1", 11, 12, KICKOFF)
    return oddspapi.LadderPrice(fixture, "bet365", "tackles", line, side, step, "p1", name, price, 100.0)


def test_ladder_steps_become_one_sided_lines_and_the_zero_step_pairs_with_one_plus() -> None:
    prices = [_ladder_price("1+", 0.5, "over", 1.5), _ladder_price("0", 0.5, "under", 2.6),
              _ladder_price("2+", 1.5, "over", 3.4)]
    coverage = quotes_from_prices(oddspapi.ladder_as_prop_prices(prices), {11: "Walsall", 12: "Port Vale"}, [FIXTURE],
                                  {9101: _inputs().squad}, NOW)
    by_line = {q.line_value: q for q in coverage.quotes}
    assert by_line[0.5].over_price == 1.5 and by_line[0.5].under_price == 2.6  # a two-way quote
    assert by_line[1.5].over_price == 3.4 and by_line[1.5].under_price is None  # a ladder step has one side only
    assert coverage.matched_prices == 3 and coverage.unmatched_players == ()
    assert all(q.fixture_id == 9101 and q.sportsbook == "bet365" and q.limit_published for q in coverage.quotes)


def test_the_ladder_catalogue_cache_round_trips_and_a_damaged_one_is_refused(tmp_path: Path) -> None:
    catalogue = {5001: oddspapi.LadderMarket("tackles", {1: (0.5, "over", "1+"), 3: (0.5, "under", "0")})}
    path = tmp_path / "ladder_markets.json"
    ladder_scan._save_ladder_catalogue(path, catalogue)  # noqa: SLF001
    assert ladder_scan.load_ladder_catalogue(path) == catalogue
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="delete it"):
        ladder_scan.load_ladder_catalogue(path)


# ---------------- the scan ----------------
def test_a_mock_scan_tips_tackles_in_the_tip_format_with_the_pilot_label_and_its_own_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tip_brazil(monkeypatch)
    _patch_database(monkeypatch, stored={FINISHED_UID})
    monkeypatch.setattr(ladder_scan, "mock_quotes", _generous_mock_quotes)
    store = SentAlertStore(tmp_path / "mock.jsonl")
    notifier = _Recording(store)
    label = f"{scan.MOCK_LABEL}\n{ladder_scan.PILOT_LABEL}"
    summary = _run(tmp_path, notifier, store, label=label, odds="mock")
    assert summary.fixtures == ["Walsall v Port Vale 18:45 (Brazil Serie A)", "Celtic v Rangers 18:45 (Scottish Premiership)"]
    assert summary.players_priced == 4  # both leagues are priced
    assert summary.alerts_sent >= 1 and len(notifier.texts) == 1  # one tip message holds up to five bets
    first = notifier.texts[0]
    assert first.startswith(label + "\nBRAZIL SERIE A \u00b7 TACKLES \u00b7 1/1\n") and "\nWalsall vs Port Vale | 18:45 UTC\n" in first
    assert first.endswith(ladder_scan.PILOT_USAGE) and "placed" not in first
    assert "Celtic" not in first  # the Scottish Premiership has no backtest: it is priced, never tipped
    assert {alert.key[0] for alert in store.load()} == {9101}  # recorded in the store it was given, never the real one
    text = ladder_scan.summary_text(summary, label)
    assert "Pilot leagues scan" in text and "Leagues: Brazil Serie A 1, Scottish Premiership 1" in text
    assert "Tipped: tackles in Brazil Serie A only" in text and "League One" not in text
    assert "Not in the Sportmonks plan, not scanned: Scottish Championship" in text


def _market_catalogue() -> list[dict[str, Any]]:
    return [{"marketId": 5001, "marketType": "players-tackles", "playerProp": True,
             "outcomes": [{"outcomeId": 1, "outcomeName": "1+"}, {"outcomeId": 2, "outcomeName": "2+"},
                          {"outcomeId": 3, "outcomeName": "0"}]}]


def _ladder_fixture(fixture_id: str, home: int, away: int,
                    prices: Mapping[str, tuple[float, float, float]]) -> dict[str, Any]:
    """One bet365 fixture: `prices` maps a player name to the (1+, 2+, 0) prices of his tackles ladder."""
    outcomes: dict[str, dict[str, Any]] = {"1": {"players": {}}, "2": {"players": {}}, "3": {"players": {}}}
    for ref, (name, (one, two, zero)) in enumerate(prices.items(), start=1):
        for outcome, price in (("1", one), ("2", two), ("3", zero)):
            outcomes[outcome]["players"][f"p{ref}"] = {"active": True, "price": price, "playerName": name, "limit": 100}
    return {"fixtureId": fixture_id, "participant1Id": home, "participant2Id": away, "startTime": "2026-10-13T18:45:00Z",
            "bookmakerOdds": {"bet365": {"bookmakerIsActive": True,
                                         "markets": {"5001": {"marketActive": True, "outcomes": outcomes}}}}}


def _ladder_body(prices: Mapping[str, tuple[float, float, float]]) -> list[dict[str, Any]]:
    return [_ladder_fixture("id1", 11, 12, prices)]


class _FakeOdds:
    def __init__(self, body: Any, left: int = 200) -> None:
        self.body, self.left, self.requests_sent = body, left, 0
        self.paths: list[str] = []

    def get_json_value(self, path: str, empty_on: Mapping[int, str] | None = None) -> Any:
        self.paths.append(path)
        if path == oddspapi.ACCOUNT_PATH:
            return {"current_subscription_id": "s",
                    "subscriptions": [{"subscription_id": "s", "request_limit": 250, "request_count": 250 - self.left}]}
        self.requests_sent += 1
        if path == oddspapi.MARKETS_PATH:
            return _market_catalogue()
        return self.body


def _names_cache(tmp_path: Path) -> None:
    cache = tmp_path / scan.ODDS_CACHE_DIR
    cache.mkdir(parents=True)
    (cache / "participants.json").write_text(
        json.dumps({"11": "Walsall", "12": "Port Vale", "21": "Celtic", "22": "Rangers"}), encoding="utf-8")


def test_a_real_ladder_price_above_the_models_becomes_a_pilot_tip_and_is_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tip_brazil(monkeypatch)
    _patch_database(monkeypatch, stored={FINISHED_UID})
    _names_cache(tmp_path)
    # A juicy ladder for the Scottish Premiership match as well: it is matched, priced and never tipped.
    body = [*_ladder_body({"Regular Starter": (2.6, 9.0, 1.45), "Away Starter": (1.10, 1.5, 7.0)}),
            _ladder_fixture("id2", 21, 22, {"Regular Starter": (3.0, 12.0, 1.2)})]
    odds = _FakeOdds(body)
    store = SentAlertStore(tmp_path / "pilot.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store, odds="oddspapi", odds_client=odds)
    assert not summary.odds_unavailable and summary.quotes >= 2
    # One request covers every pilot league in the window: Brazil Serie A (325) and the Scottish Premiership (36).
    assert any(path.startswith("odds-by-tournaments?bookmakers=bet365&tournamentIds=36,325&") for path in odds.paths)
    [archived] = (tmp_path / ladder_scan.PILOT_ODDS_DIR).glob("*_bet365_pilot.json.gz")
    assert [f["fixtureId"] for f in json.loads(gzip.open(archived, "rt", encoding="utf-8").read())] == ["id1", "id2"]
    assert (tmp_path / scan.ODDS_CACHE_DIR / ladder_scan.LADDER_CATALOGUE_FILE).is_file()
    assert (tmp_path / scan.ODDS_CACHE_DIR / scan.CATALOGUE_FILE).is_file()  # the line catalogue the other scan also reads
    assert summary.alerts_sent >= 1 and notifier.texts[0].startswith(ladder_scan.PILOT_LABEL)
    assert "\n1a Regular Starter \u2014 OVER 1.5 tackles\n" in notifier.texts[0]  # the 2+ ladder step
    assert "   bet365 @ 9.00 | take it at" in notifier.texts[0] and "simulated match conditions" in notifier.texts[0]
    # The short-priced Away Starter ladder (1.10 for 1+) is no edge: it is never tipped.
    assert "Away Starter" not in notifier.texts[0] and "Celtic" not in notifier.texts[0]
    assert {alert.key[0] for alert in store.load()} == {9101}


def test_while_paused_the_same_juicy_ladder_is_priced_and_archived_but_never_tipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-12: with tipping paused, the ladder that tips in the test above is still matched, priced and archived (the
    evaluation evidence) and its players priced, but nothing is offered to the allocator, sent or recorded."""
    _patch_database(monkeypatch, stored={FINISHED_UID})
    _names_cache(tmp_path)
    odds = _FakeOdds(_ladder_body({"Regular Starter": (2.6, 9.0, 1.45)}))
    store = SentAlertStore(tmp_path / "pilot.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store, odds="oddspapi", odds_client=odds)
    assert summary.players_priced >= 1 and summary.quotes == 0 and odds.requests_sent >= 1  # fetched, not offered for tips
    assert summary.alerts_sent == 0 and notifier.texts == [] and store.load() == []
    assert list((tmp_path / ladder_scan.PILOT_ODDS_DIR).glob("*_bet365_pilot.json.gz"))


def test_odds_are_not_fetched_below_the_quota_reserve_and_no_tip_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_database(monkeypatch, stored={FINISHED_UID})
    odds = _FakeOdds([], left=scan.ODDS_QUOTA_RESERVE)
    store = SentAlertStore(tmp_path / "pilot.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store, odds="oddspapi", odds_client=odds)
    assert summary.odds_unavailable and summary.alerts_sent == 0 and notifier.texts == []
    assert "Odds NOT fetched" in "\n".join(summary.odds_notes) and odds.requests_sent == 0


def test_a_malformed_ladder_reply_ends_the_run_instead_of_being_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_database(monkeypatch, stored={FINISHED_UID})
    _names_cache(tmp_path)
    store = SentAlertStore(tmp_path / "pilot.jsonl")
    with pytest.raises(ValueError, match="malformed OddsPapi response"):
        _run(tmp_path, _Recording(store), store, odds="oddspapi", odds_client=_FakeOdds({"not": "a list"}))


def test_exposure_is_net_of_pilot_stakes_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running the same mock scan twice tips each bet once: the first run's alerts are in the store it was given."""
    _tip_brazil(monkeypatch)
    _patch_database(monkeypatch, stored={FINISHED_UID})
    monkeypatch.setattr(ladder_scan, "mock_quotes", _generous_mock_quotes)
    store = SentAlertStore(tmp_path / "mock.jsonl")
    first = _Recording(store)
    one = _run(tmp_path, first, store, odds="mock")
    again = _Recording(store)
    two = _run(tmp_path, again, store, odds="mock")
    assert one.alerts_sent >= 1 and two.alerts_sent == 0 and again.texts == []


def test_real_odds_cannot_be_combined_with_a_simulated_time() -> None:
    with pytest.raises(SystemExit):
        ladder_scan.main(["--odds", "oddspapi", "--at", "2026-10-13T16:30:00+00:00"])


def test_the_scan_adds_no_exception_handler_and_the_frozen_model_is_unchanged() -> None:
    import ast

    tree = ast.parse(Path(ladder_scan.__file__).read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert ladder_scan.ALERT_MARKETS <= scan.ALERT_MARKETS
    assert fx_identity("GBP").identity


def test_recorded_vendor_refusals_are_not_stale_but_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recent match the data feed cannot load (no tackles in any line-up) must not make its league stale for ever."""
    assert ladder_scan.missing_by_league([("77", NOW - timedelta(days=2), "Scottish Championship")], set(), NOW,
                                         refused={"77"}) == {}
    _patch_database(monkeypatch, stored=set())
    (tmp_path / "pilot_refused.json").write_text(json.dumps({FINISHED_UID: "ValueError: no tackles"}), encoding="utf-8")
    store = SentAlertStore(tmp_path / "s.jsonl")
    summary = _run(tmp_path, _Recording(store), store)
    assert summary.failures == [] and summary.players_priced == 4
    assert any("cannot load" in note and "Brazil Serie A 1" in note for note in summary.odds_notes)


def test_the_scan_logs_predictions_and_records_every_tip_as_a_paper_bet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit 2026-10-11 (finding 3): a pilot tip leaves a prediction log line for every priced line, a paper bet and a tip line."""
    _tip_brazil(monkeypatch)
    from prop_alpha.alerts.ledger import PlacedBetStore
    from prop_alpha.alerts.predictions import PredictionLog, counts_for_lockbox

    _patch_database(monkeypatch, stored={FINISHED_UID})
    monkeypatch.setattr(ladder_scan, "mock_quotes", _generous_mock_quotes)
    store = SentAlertStore(tmp_path / "mock.jsonl")
    notifier = _Recording(store)
    log, bets, tips_file = PredictionLog(tmp_path / "pred.jsonl"), PlacedBetStore(tmp_path / "bets.jsonl"), tmp_path / "tips.jsonl"
    summary = _run(tmp_path, notifier, store, odds="mock", predictions=log, paper_bets=bets, tips_file=tips_file)
    records = log.load()
    assert records and {r["fixture_id"] for r in records} == {9101, SCOTTISH_ID}  # both leagues are logged, before any tip
    assert {r["digest"] for r in records} == {ladder_scan.pilot_settings_digest()}
    assert ladder_scan.pilot_settings_digest().startswith("pilot:")
    assert not any(counts_for_lockbox(r) for r in records)  # a pilot price never counts towards the frozen lockbox
    placed = bets.open_bets(NOW)
    assert len(placed) == summary.alerts_sent >= 1 and all(b.fixture_id == 9101 for b in placed)  # tips: Brazil only
    assert all(b.odds == 2.6 and b.stake > 0 and b.side == "over" for b in placed)
    from prop_alpha.alerts.pilot_ledger import load_tips

    tips = load_tips(tips_file)
    assert set(tips) == {b.bet_id for b in placed} and {t["league"] for t in tips.values()} == {"Brazil Serie A"}
    assert all(0.6 < t["survival_rate"] <= 1.0 for t in tips.values())  # the screen's own number is kept with the tip


def test_a_prediction_that_cannot_be_recorded_stops_the_scan_before_any_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prop_alpha.alerts.predictions import PredictionLog

    _patch_database(monkeypatch, stored={FINISHED_UID})
    monkeypatch.setattr(ladder_scan, "mock_quotes", _generous_mock_quotes)

    def refuse(self: Any, *args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(PredictionLog, "record", refuse)
    store = SentAlertStore(tmp_path / "mock.jsonl")
    notifier = _Recording(store)
    with pytest.raises(OSError, match="disk full"):
        _run(tmp_path, notifier, store, odds="mock", predictions=PredictionLog(tmp_path / "pred.jsonl"))
    assert notifier.texts == [] and store.load() == []  # no tip, and nothing counted as alerted


def test_no_paper_bet_is_recorded_for_a_run_that_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prop_alpha.alerts.ledger import PlacedBetStore

    _patch_database(monkeypatch, stored={FINISHED_UID})
    monkeypatch.setattr(ladder_scan, "mock_quotes", _generous_mock_quotes)
    bets = PlacedBetStore(tmp_path / "bets.jsonl")
    _run(tmp_path, None, SentAlertStore(tmp_path / "mock.jsonl"), odds="mock", paper_bets=bets, tips_file=tmp_path / "tips.jsonl")  # type: ignore[arg-type]
    assert bets.open_bets(NOW) == [] and not (tmp_path / "tips.jsonl").exists()


def test_the_stale_exit_code_follows_the_skipped_leagues_not_the_wording_of_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit 2026-10-11, finding 12: main() used to look for the text "skipped, not priced" in the failure lines."""
    import prop_alpha.ingestion.pipeline as pipeline

    class _Client:
        def close(self) -> None:
            return None

    monkeypatch.setattr(ladder_scan, "bankroll_currency", lambda: "GBP")
    monkeypatch.setattr(ladder_scan, "book_to_bankroll", lambda currency, day: fx_identity("GBP"))
    monkeypatch.setattr(pipeline, "build_vendor_client", lambda environ: _Client())
    outcomes = {"stale": (["reworded failure"], ["Brazil Serie A"]), "other failure": (["Could not price X"], []), "clean": ([], [])}
    codes: dict[str, int] = {}
    for name, (failures, stale) in outcomes.items():
        def fake_run(*args: Any, failures: list[str] = failures, stale: list[str] = stale, **kwargs: Any) -> scan.ScanSummary:
            return scan.ScanSummary(NOW, NOW, failures=list(failures), stale_leagues=list(stale))

        monkeypatch.setattr(ladder_scan, "run_ladder_scan", fake_run)
        codes[name] = ladder_scan.main(["--odds", "mock"])
    capsys.readouterr()
    assert codes == {"stale": ladder_scan.STALE_EXIT, "other failure": 0, "clean": 0}


def test_the_heartbeat_records_what_the_status_report_shows() -> None:
    summary = scan.ScanSummary(NOW, KICKOFF, fixtures=["a", "b"], players_priced=40, quotes=12, alerts_sent=1,
                               failures=["x"], stale_leagues=["Scottish Championship"], odds_unavailable=True)
    assert ladder_scan.heartbeat_record(summary, NOW) == {
        "at": NOW.isoformat(), "window_end": KICKOFF.isoformat(), "fixtures": 2, "players_priced": 40, "quotes": 12,
        "tips": 1, "failures": 1, "stale_leagues": ["Scottish Championship"], "odds_unavailable": True}


# ---------------- fixtures given by hand (2026-10-12: the Sportmonks plan stopped listing Brazil Serie A) ----------------
class _NoSportmonks:
    def get_json(self, path: str) -> dict[str, Any]:
        raise AssertionError(f"a run with fixtures given by hand must not call Sportmonks ({path})")


def _patch_history(monkeypatch: pytest.MonkeyPatch, latest: Mapping[str, datetime | None]) -> list[tuple[str, Any]]:
    """Each club's latest stored match (by Sportmonks team id); records every query."""
    queries: list[tuple[str, Any]] = []

    class _Conn:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> Any:
            return self

        def execute(self, sql: str, params: Any = None) -> None:
            queries.append((sql, params))

        def fetchone(self) -> tuple[datetime | None]:
            return (latest.get(queries[-1][1][0]),)

        def close(self) -> None:
            return None

    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    monkeypatch.setattr(scan, "load_slate_inputs", lambda cur, fixture: _inputs())
    return queries


def test_a_fixture_is_given_by_hand_as_seven_fields() -> None:
    spec = "Brazil Serie A|19621780|710|3432|Chapecoense|Athletico PR|2026-10-12T22:30:00+00:00"
    assert ladder_scan.parse_manual_fixture(spec) == UpcomingFixture(
        19621780, "Brazil Serie A", "710", "3432", "Chapecoense", "Athletico PR", datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="7 fields"):
        ladder_scan.parse_manual_fixture("Brazil Serie A|19621780|710|3432")
    with pytest.raises(ValueError, match="not a pilot league"):
        ladder_scan.parse_manual_fixture(spec.replace("Brazil Serie A", "League One"))


def test_fixtures_given_by_hand_skip_sportmonks_and_tip_only_for_that_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = NOW - timedelta(days=4)
    queries = _patch_history(monkeypatch, {"76": fresh, "348": fresh})
    store = SentAlertStore(tmp_path / "mock.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store, client=_NoSportmonks(), manual=[FIXTURE], tip_leagues=["Brazil Serie A"])
    assert ladder_scan.TIP_LEAGUES == frozenset()  # the standing setting is untouched
    assert (summary.window_start, summary.window_end) == (NOW, KICKOFF + timedelta(minutes=1))
    assert [params for sql, params in queries if sql == ladder_scan.LATEST_STORED_SQL] == [("76", KICKOFF), ("348", KICKOFF)]
    text = ladder_scan.summary_text(summary, "")
    assert "Fixtures given by hand (--fixture)" in text and "history stored to 09 Oct" in text
    assert "Tipped FOR THIS RUN ONLY (maintainer's choice): tackles in Brazil Serie A; the standing setting is paused" in text
    assert summary.players_priced >= 1 and summary.quotes >= 1  # tipping on for the run: quotes reach the allocator
    paused = _run(tmp_path / "p", _Recording(SentAlertStore(tmp_path / "p.jsonl")), SentAlertStore(tmp_path / "p.jsonl"),
                  client=_NoSportmonks(), manual=[FIXTURE])
    assert paused.quotes == 0 and "Tipped: nothing (tipping paused" in ladder_scan.summary_text(paused, "")


def test_a_fixture_given_by_hand_with_stale_history_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_history(monkeypatch, {"76": NOW - timedelta(days=4), "348": NOW - timedelta(days=11)})
    store = SentAlertStore(tmp_path / "mock.jsonl")
    summary = _run(tmp_path, _Recording(store), store, client=_NoSportmonks(), manual=[FIXTURE], tip_leagues=["Brazil Serie A"])
    assert summary.stale_leagues == ["Brazil Serie A"] and summary.players_priced == 0 and summary.alerts_sent == 0
    assert "older than 10 days or missing" in summary.failures[0]
    with pytest.raises(ValueError, match="already kicked off"):
        _run(tmp_path, _Recording(store), store, client=_NoSportmonks(), manual=[replace(FIXTURE, kickoff_at=NOW)])


def test_tip_is_refused_without_fixtures_given_by_hand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        ladder_scan.main(["--odds", "mock", "--tip", "Brazil Serie A"])
    assert "--tip is a one-off: it works only with --fixture" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ladder_scan.main(["--odds", "mock", "--fixture", "Brazil Serie A|1|2"])
    assert "invalid parse_manual_fixture value: 'Brazil Serie A|1|2'" in capsys.readouterr().err
