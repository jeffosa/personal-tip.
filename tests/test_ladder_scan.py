"""The Championship ladder scan (alerts/ladder_scan.py): pilot pricing, ladder quotes, the stale-database gate, and
the pilot's own alert state."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from prop_alpha.alerts import ladder_scan, scan
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
FIXTURE = UpcomingFixture(9101, "Championship", "76", "348", "Walsall", "Port Vale", KICKOFF)
RATES = {(position, market): rate for position in ("DEF", "MID") for market, rate in (("tackles", 1.8), ("fouls", 1.2))}
FINISHED_UID = "7001"


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


def _row(fixture_id: int, state: str, kickoff: datetime, league: int = 9) -> dict[str, Any]:
    return {"id": fixture_id, "league_id": league, "state": {"developer_name": state},
            "starting_at_timestamp": int(kickoff.timestamp()),
            "participants": [{"id": 76, "name": "Walsall", "meta": {"location": "home"}},
                             {"id": 348, "name": "Port Vale", "meta": {"location": "away"}}]}


class _StubSportmonks:
    """Answers every listing with one Championship match about to start, one in League One, and one finished."""

    def __init__(self, finished_kickoff: datetime = NOW - timedelta(days=5)) -> None:
        self.rows = [_row(9101, "NS", KICKOFF), _row(9102, "NS", KICKOFF, league=12),
                     _row(int(FINISHED_UID), "FT", finished_kickoff)]
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
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
def test_the_pilot_prices_with_the_championship_settings_not_the_frozen_ones() -> None:
    inputs = _inputs()
    engine, market_engines = ladder_scan.championship_engines()
    pilot, _ = ladder_scan.price_championship(FIXTURE, inputs, engine, market_engines)
    assert pilot[(9101, 1, "tackles")].game_state_k == 21.1 and pilot[(9101, 1, "fouls")].game_state_k == 1000.0
    frozen, _ = scan.price_fixture(FIXTURE, inputs, *scan.scan_engines())
    assert frozen[(9101, 1, "tackles")].game_state_k == scan.SCAN_GAME_STATE_K_BY_MARKET["tackles"] != 21.1
    # The pilot applies no rate multiplier and its own prior strength: the same call by hand agrees.
    by_hand, _ = build_projections(FIXTURE, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures,
                                   RATES, QuantEngine(game_state_k=6.0),
                                   market_engines={m: QuantEngine(game_state_k=k) for m, k in ladder_scan.GAME_STATE_K.items()},
                                   prior_strengths=ladder_scan.PRIOR_STRENGTH)
    assert pilot[(9101, 1, "tackles")].rate_posterior.alpha == pytest.approx(by_hand[(9101, 1, "tackles")].rate_posterior.alpha)


# ---------------- listings ----------------
def test_the_listing_helpers_default_to_league_one_and_two_and_take_the_championship() -> None:
    body = {"data": [_row(1, "NS", KICKOFF, 9), _row(2, "NS", KICKOFF, 12), _row(3, "FT", KICKOFF, 9)],
            "pagination": {"has_more": False}}
    rows, _ = sportmonks.upcoming_fixtures(body, sportmonks.CHAMPIONSHIP_LEAGUES)
    assert [(r["fixture_id"], r["league"]) for r in rows] == [(1, "Championship")]
    assert [r["fixture_id"] for r in sportmonks.upcoming_fixtures(body)[0]] == [2]  # the default is unchanged
    assert [uid for uid, _ in sportmonks.finished_fixtures(body, sportmonks.CHAMPIONSHIP_LEAGUES)[0]] == ["3"]
    assert sportmonks.finished_fixtures(body)[0] == []
    assert "fixtureLeagues:9&" in sportmonks.upcoming_path(NOW, NOW, 1, sportmonks.CHAMPIONSHIP_LEAGUES)
    assert "fixtureLeagues:12,14&" in sportmonks.upcoming_path(NOW, NOW, 1)


# ---------------- the stale-database gate ----------------
def test_missing_results_lists_finished_matches_not_stored_and_spares_the_last_hours() -> None:
    finished = [("1", NOW - timedelta(days=4)), ("2", NOW - timedelta(days=9)), ("3", NOW - timedelta(hours=1)),
                ("4", NOW - timedelta(days=2))]
    assert ladder_scan.missing_results(finished, {"4"}, NOW) == [("2", NOW - timedelta(days=9)),
                                                                 ("1", NOW - timedelta(days=4))]
    assert ladder_scan.missing_results(finished, {"1", "2", "4"}, NOW) == []  # "3" kicked off an hour ago: not yet due
    assert ladder_scan.missing_results([("19729100", NOW - timedelta(days=3))], set(), NOW) == []  # a known vendor defect
    with pytest.raises(ValueError, match="UTC"):
        ladder_scan.missing_results(finished, set(), datetime(2026, 10, 13, 16))


def test_a_stale_pilot_database_refuses_the_scan_and_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_database(monkeypatch, stored=set())
    notifier = _Recording(SentAlertStore(tmp_path / "s.jsonl"))
    with pytest.raises(ValueError, match="not in the pilot database") as caught:
        _run(tmp_path, notifier, SentAlertStore(tmp_path / "s.jsonl"))
    assert "pilot_backfill.py --list --ingest --fix-leagues" in str(caught.value) and FINISHED_UID in str(caught.value)
    assert notifier.texts == []


def test_a_window_with_no_championship_match_reads_nothing_else(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: pytest.fail("the database must not be opened"))
    empty = _StubSportmonks()
    empty.rows = [_row(9102, "NS", KICKOFF, league=12)]  # League One only
    summary = _run(tmp_path, _Recording(SentAlertStore(tmp_path / "s.jsonl")), SentAlertStore(tmp_path / "s.jsonl"),
                   client=empty)
    assert summary.fixtures == [] and "Matches in this window: 0" in ladder_scan.summary_text(summary, "")
    assert all("fixtureLeagues:9&" in path for path in empty.paths)


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
    _patch_database(monkeypatch, stored={FINISHED_UID})
    monkeypatch.setattr(ladder_scan, "mock_quotes", _generous_mock_quotes)
    store = SentAlertStore(tmp_path / "mock.jsonl")
    notifier = _Recording(store)
    label = f"{scan.MOCK_LABEL}\n{ladder_scan.PILOT_LABEL}"
    summary = _run(tmp_path, notifier, store, label=label, odds="mock")
    assert summary.fixtures == ["Walsall v Port Vale 18:45"] and summary.players_priced == 2
    assert summary.alerts_sent >= 1 and len(notifier.texts) == 1  # one tip message holds up to five bets
    first = notifier.texts[0]
    assert first.startswith(label) and "\nTACKLE" in first and "Walsall vs Port Vale, Tue 13 Oct 18:45 UTC" in first
    assert first.endswith(ladder_scan.PILOT_USAGE) and "placed" not in first
    assert len(store.load()) == summary.alerts_sent  # recorded in the store it was given, never the real one
    text = ladder_scan.summary_text(summary, label)
    assert "Championship pilot scan" in text and "Tipped markets: tackles" in text and "League One" not in text


def _market_catalogue() -> list[dict[str, Any]]:
    return [{"marketId": 5001, "marketType": "players-tackles", "playerProp": True,
             "outcomes": [{"outcomeId": 1, "outcomeName": "1+"}, {"outcomeId": 2, "outcomeName": "2+"},
                          {"outcomeId": 3, "outcomeName": "0"}]}]


def _ladder_body(prices: Mapping[str, tuple[float, float, float]]) -> list[dict[str, Any]]:
    """One bet365 fixture: `prices` maps a player name to the (1+, 2+, 0) prices of his tackles ladder."""
    outcomes: dict[str, dict[str, Any]] = {"1": {"players": {}}, "2": {"players": {}}, "3": {"players": {}}}
    for ref, (name, (one, two, zero)) in enumerate(prices.items(), start=1):
        for outcome, price in (("1", one), ("2", two), ("3", zero)):
            outcomes[outcome]["players"][f"p{ref}"] = {"active": True, "price": price, "playerName": name, "limit": 100}
    return [{"fixtureId": "id1", "participant1Id": 11, "participant2Id": 12, "startTime": "2026-10-13T18:45:00Z",
             "bookmakerOdds": {"bet365": {"bookmakerIsActive": True,
                                          "markets": {"5001": {"marketActive": True, "outcomes": outcomes}}}}}]


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
    (cache / "participants.json").write_text(json.dumps({"11": "Walsall", "12": "Port Vale"}), encoding="utf-8")


def test_a_real_ladder_price_above_the_models_becomes_a_pilot_tip_and_is_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_database(monkeypatch, stored={FINISHED_UID})
    _names_cache(tmp_path)
    odds = _FakeOdds(_ladder_body({"Regular Starter": (2.6, 9.0, 1.45), "Away Starter": (1.10, 1.5, 7.0)}))
    store = SentAlertStore(tmp_path / "pilot.jsonl")
    notifier = _Recording(store)
    summary = _run(tmp_path, notifier, store, odds="oddspapi", odds_client=odds)
    assert not summary.odds_unavailable and summary.quotes >= 2
    assert any(path.startswith("odds-by-tournaments?bookmakers=bet365&tournamentIds=18&") for path in odds.paths)
    [archived] = (tmp_path / ladder_scan.PILOT_ODDS_DIR).glob("*_bet365_championship.json.gz")
    assert json.loads(gzip.open(archived, "rt", encoding="utf-8").read())[0]["fixtureId"] == "id1"
    assert (tmp_path / scan.ODDS_CACHE_DIR / ladder_scan.LADDER_CATALOGUE_FILE).is_file()
    assert summary.alerts_sent >= 1 and notifier.texts[0].startswith(ladder_scan.PILOT_LABEL)
    assert "1a) Regular Starter (Walsall) tackle over 1.5 9.00 odd" in notifier.texts[0]  # the 2+ ladder step
    assert "bet365 | take it at" in notifier.texts[0] and "simulated match conditions" in notifier.texts[0]
    # The short-priced Away Starter ladder (1.10 for 1+) is no edge: it is never tipped.
    assert "Away Starter" not in notifier.texts[0]


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
