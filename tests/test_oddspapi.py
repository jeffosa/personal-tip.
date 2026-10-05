from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
import requests

from prop_alpha.alerts import scan
from prop_alpha.ingestion import oddspapi
from prop_alpha.ingestion.pipeline import TokenBucketRateLimiter, VendorClient, build_odds_client
from prop_alpha.scanner.slate import (
    SquadPlayer,
    UpcomingFixture,
    match_fixture,
    match_player,
    quotes_from_prices,
)

KICKOFF = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
NOW = KICKOFF - timedelta(hours=2)
FIXTURE = UpcomingFixture(9001, "League Two", "76", "348", "Walsall", "Port Vale", KICKOFF)
SQUAD = [SquadPlayer(1, "Gatlin O'Donkor", "MID", "76"), SquadPlayer(2, "Jack Currie", "DEF", "348"),
         SquadPlayer(3, "Onel Hernández", "MID", "348")]
NAMES = {56: "Walsall", 95: "Port Vale", 70: "Barnet", 71: "Bromley"}
MARKETS = [
    {"marketId": 102661, "marketType": "playertotals-tackles", "playerProp": True, "handicap": 1.5,
     "outcomes": [{"outcomeId": 102661, "outcomeName": "Over"}, {"outcomeId": 102662, "outcomeName": "Under"}]},
    {"marketId": 104000, "marketType": "playertotals-foulscommitted", "playerProp": True, "handicap": 0.5,
     "outcomes": [{"outcomeId": 104000, "outcomeName": "Over"}, {"outcomeId": 104001, "outcomeName": "Under"}]},
    {"marketId": 10732, "marketType": "players-lastgoalscorer", "playerProp": True, "handicap": 0,
     "outcomes": [{"outcomeId": 10732, "outcomeName": "Yes"}]},
]


def _player(name: str, price: float, limit: float | None = None, active: bool = True) -> dict[str, Any]:
    return {"active": active, "playerName": name, "price": price, "limit": limit, "changedAt": "2026-10-10T09:00:00Z"}


def _odds(start: datetime = KICKOFF, home: int = 56, away: int = 95) -> list[dict[str, Any]]:
    """One fixture in the verified odds-by-tournaments shape (bet365, 2026-10-03)."""
    return [{
        "fixtureId": "id1000002472341252", "participant1Id": home, "participant2Id": away,
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "tournamentId": 25,
        "bookmakerOdds": {"bet365": {"bookmakerIsActive": True, "suspended": False, "markets": {
            "102661": {"marketActive": True, "outcomes": {
                "102661": {"players": {"2342841": _player("O'Donkor, Gatlin", 2.4, limit=100.0),
                                       "777": _player("Unknown, Somebody", 2.0)}},
                "102662": {"players": {"2342841": _player("O'Donkor, Gatlin", 1.55, limit=250.0)}},
            }},
            "104000": {"marketActive": True, "outcomes": {
                "104000": {"players": {"34706": _player("Hernandez, Onel", 1.9)}},
                "104001": {"players": {"34706": _player("Hernandez, Onel", 1.8, active=False)}},
            }},
            "10732": {"marketActive": True, "outcomes": {"10732": {"players": {"1": _player("Currie, Jack", 34.0)}}}},
        }}},
    }]


def test_catalogue_keeps_player_over_under_tackles_and_fouls_only() -> None:
    catalogue = oddspapi.line_catalogue(MARKETS)
    assert set(catalogue) == {102661, 104000}
    assert catalogue[102661] == oddspapi.LineMarket("tackles", 1.5, {102661: "over", 102662: "under"})
    with pytest.raises(ValueError, match="not Over/Under"):
        oddspapi.line_catalogue([{**MARKETS[0], "outcomes": [{"outcomeId": 1, "outcomeName": "Yes"}]}])


def test_prop_prices_read_active_player_prices() -> None:
    prices = oddspapi.prop_prices(_odds(), oddspapi.line_catalogue(MARKETS), "bet365")
    assert {(p.player_name, p.market, p.line, p.side, p.price) for p in prices} == {
        ("O'Donkor, Gatlin", "tackles", 1.5, "over", 2.4), ("Unknown, Somebody", "tackles", 1.5, "over", 2.0),
        ("O'Donkor, Gatlin", "tackles", 1.5, "under", 1.55), ("Hernandez, Onel", "fouls", 0.5, "over", 1.9),
    }
    assert oddspapi.prop_prices(_odds(), oddspapi.line_catalogue(MARKETS), "skybet") == []


def test_names_normalise_across_vendors() -> None:
    assert oddspapi.normalise("O'Donkor, Gatlin") == oddspapi.normalise("Gatlin O'Donkor") == "gatlin odonkor"
    assert oddspapi.normalise("Hernandez, Onel") == oddspapi.normalise("Onel Hernández")
    assert match_player("Currie, J.", SQUAD) == SQUAD[1]
    assert match_player("Smith, John", SQUAD) is None
    twins = [SquadPlayer(8, "Jack Smith", "MID", "76"), SquadPlayer(9, "James Smith", "MID", "76")]
    assert match_player("Smith, J", twins) is None  # ambiguous: never guessed


def test_fixture_matching_needs_kickoff_and_both_clubs() -> None:
    odds_fixture = oddspapi.prop_prices(_odds(), oddspapi.line_catalogue(MARKETS), "bet365")[0].fixture
    assert match_fixture(odds_fixture, NAMES, [FIXTURE]) == FIXTURE
    moved = oddspapi.OddsFixture("x", 56, 95, KICKOFF + timedelta(hours=1))
    assert match_fixture(moved, NAMES, [FIXTURE]) is None
    other = oddspapi.OddsFixture("y", 70, 71, KICKOFF)
    assert match_fixture(other, NAMES, [FIXTURE]) is None


def test_quotes_pair_sides_and_take_the_smaller_limit() -> None:
    prices = oddspapi.prop_prices(_odds(), oddspapi.line_catalogue(MARKETS), "bet365")
    coverage = quotes_from_prices(prices, NAMES, [FIXTURE], {9001: SQUAD}, NOW)
    by_player = {(q.player_id, q.market_type): q for q in coverage.quotes}
    tackles = by_player[(1, "tackles")]
    assert (tackles.over_price, tackles.under_price, tackles.max_stake) == (2.4, 1.55, 100.0)
    assert tackles.limit_published
    fouls = by_player[(3, "fouls")]
    assert (fouls.over_price, fouls.under_price, fouls.max_stake) == (1.9, None, oddspapi.UNKNOWN_LIMIT_STAKE)
    assert not fouls.limit_published
    assert all(q.sportsbook == "bet365" and q.captured_at == NOW for q in coverage.quotes)
    assert coverage.matched_prices == 3 and len(coverage.unmatched_players) == 1


class FakeOddsClient:
    def __init__(self, left: int, odds: list[dict[str, Any]]) -> None:
        self.account = {"current_subscription_id": "s", "subscriptions": [
            {"subscription_id": "s", "request_limit": 250, "request_count": 250 - left}]}
        self.odds = odds
        self.garbled: Any = None
        self.paths: list[str] = []
        self.requests_sent = 0

    def get_json_value(self, path: str, empty_on: Mapping[int, str] | None = None) -> Any:
        self.paths.append(path)
        self.requests_sent += 1
        if "bookmakers=skybet" in path:
            assert empty_on == oddspapi.NO_FIXTURES_REPLIES
            return None
        if "bookmakers=broken" in path:
            raise requests.HTTPError("odds-by-tournaments: HTTP 500: upstream error")
        if "bookmakers=garbled" in path:
            return self.garbled
        if path == oddspapi.ACCOUNT_PATH:
            return self.account
        if path == oddspapi.MARKETS_PATH:
            return MARKETS
        if path == oddspapi.PARTICIPANTS_PATH:
            return {str(k): v for k, v in NAMES.items()}
        return self.odds


def test_collect_odds_fetches_caches_and_archives(tmp_path: Path) -> None:
    client = FakeOddsClient(left=200, odds=_odds())
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                        ("bet365",), tmp_path)
    assert len(coverage.quotes) == 2 and "3 matched to our players" in notes[0]
    assert client.paths == [oddspapi.ACCOUNT_PATH, oddspapi.MARKETS_PATH,
                            oddspapi.odds_path("bet365", [25]), oddspapi.PARTICIPANTS_PATH]
    archived = list((tmp_path / "odds_archive").glob("*_bet365.json.gz"))
    assert len(archived) == 1 and json.loads(gzip.open(archived[0], "rt", encoding="utf-8").read()) == _odds()
    again = FakeOddsClient(left=200, odds=_odds())
    scan.collect_odds(again, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4), ("bet365",), tmp_path)
    assert again.paths == [oddspapi.ACCOUNT_PATH, oddspapi.odds_path("bet365", [25])]  # catalogue and names cached


def test_a_bookmaker_without_fixtures_is_noted_not_fatal(tmp_path: Path) -> None:
    client = FakeOddsClient(left=200, odds=_odds())
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                        ("bet365", "skybet"), tmp_path)
    assert len(coverage.quotes) == 2
    assert "No odds for these leagues from: skybet" in notes


def test_vendor_400_can_mean_no_data_and_errors_carry_the_scrubbed_body() -> None:
    secret = "k" * 36
    client = VendorClient("https://api.oddspapi.io/v4", secret, TokenBucketRateLimiter(100.0, 10), max_retries=0,
                          auth_query_param="apiKey")

    class BadRequest:
        def get(self, url: str, timeout: float, params: dict[str, str] | None = None) -> requests.Response:
            response = requests.Response()
            response.status_code = 400
            response._content = f'{{"error": "no fixtures", "echo": "{secret}"}}'.encode()
            return response

    client._local.session = BadRequest()
    assert client.get_json_value("odds", empty_on={400: "no fixtures"}) is None
    with pytest.raises(requests.HTTPError) as caught:
        client.get_json_value("odds")
    assert "no fixtures" in str(caught.value) and secret not in str(caught.value)


def test_only_the_known_no_fixtures_400_counts_as_empty() -> None:
    client = VendorClient("https://api.oddspapi.io/v4", "k" * 36, TokenBucketRateLimiter(100.0, 10), max_retries=0,
                          auth_query_param="apiKey")
    bodies = {
        "empty": '{"error": {"message": "Invalid bookmaker specified.", "code": "INVALID_PARAMETER", "details": '
                 '"The bookmaker skybet is not supported or does not have any fixtures available."}}',
        "malformed": '{"error": {"message": "Invalid tournamentIds.", "code": "INVALID_PARAMETER"}}',
    }

    class Reply:
        def __init__(self, body: str) -> None:
            self.body = body

        def get(self, url: str, timeout: float, params: dict[str, str] | None = None) -> requests.Response:
            response = requests.Response()
            response.status_code = 400
            response._content = self.body.encode()
            return response

    def fetch(kind: str) -> Any:
        client._local.session = Reply(bodies[kind])
        return client.get_json_value("odds", empty_on=oddspapi.NO_FIXTURES_REPLIES)

    assert fetch("empty") is None
    with pytest.raises(requests.HTTPError, match="Invalid tournamentIds"):
        fetch("malformed")


def test_collect_odds_keeps_the_quota_reserve(tmp_path: Path) -> None:
    client = FakeOddsClient(left=scan.ODDS_QUOTA_RESERVE + 2, odds=_odds())
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                        ("bet365", "skybet"), tmp_path)
    assert coverage.quotes == () and "NOT fetched" in notes[0]
    assert client.paths == [oddspapi.ACCOUNT_PATH]


def test_odds_outside_the_window_are_ignored(tmp_path: Path) -> None:
    client = FakeOddsClient(left=200, odds=_odds(start=KICKOFF + timedelta(days=3)))
    coverage, _, _ = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                    ("bet365",), tmp_path)
    assert coverage.prices == 0 and coverage.unmatched_fixtures == ()


def test_query_param_key_never_appears_in_errors() -> None:
    secret = "k" * 36
    client = VendorClient("https://api.oddspapi.io/v4", secret, TokenBucketRateLimiter(100.0, 10), max_retries=0,
                          auth_query_param="apiKey")

    class Failing:
        def get(self, url: str, timeout: float, params: dict[str, str] | None = None) -> requests.Response:
            assert params is not None
            raise requests.ConnectionError(f"Max retries exceeded with url: /v4/account?apiKey={params['apiKey']}")

    client._local.session = Failing()
    with pytest.raises(requests.ConnectionError) as caught:
        client.get_json_value("account")
    assert secret not in str(caught.value) and "withheld" in str(caught.value)

    class NotFound:
        def get(self, url: str, timeout: float, params: dict[str, str] | None = None) -> requests.Response:
            assert params is not None
            response = requests.Response()
            response.status_code, response.url = 404, f"https://api.oddspapi.io/v4/x?apiKey={params['apiKey']}"
            return response

    client._local.session = NotFound()
    with pytest.raises(requests.HTTPError) as http_error:
        client.get_json_value("x")
    assert secret not in str(http_error.value)


def test_odds_client_sends_the_key_as_a_query_parameter_not_a_header() -> None:
    client = build_odds_client({"ODDSPAPI_KEY": "test-key"})
    session = client._session()
    assert "Authorization" not in session.headers and client.auth_query_param == "apiKey"
    assert (client.max_retries, client.max_requests) == (0, 8)
    client.close()


def test_one_failing_bookmaker_keeps_the_other_books_prices(tmp_path: Path) -> None:
    """Audit 2026-10-05: a William Hill 404 used to discard bet365's prices for the whole run (handler H12)."""
    client = FakeOddsClient(left=200, odds=_odds())
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                                   ("bet365", "broken", "skybet"), tmp_path)
    assert len(coverage.quotes) == 2 and available
    assert any(n.startswith("Odds FAILED from broken, its prices left out: HTTPError") for n in notes)
    assert "No odds for these leagues from: skybet" in notes


def test_every_bookmaker_failing_means_no_real_odds(tmp_path: Path) -> None:
    client = FakeOddsClient(left=200, odds=_odds())
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                                   ("broken",), tmp_path)
    assert coverage.quotes == () and not available
    _, _, none_listed = scan.collect_odds(FakeOddsClient(left=200, odds=_odds()), [FIXTURE], {9001: SQUAD}, NOW,
                                          NOW + timedelta(hours=4), ("skybet",), tmp_path)
    assert none_listed  # "no fixtures" from every book is coverage, not an outage


def test_the_williamhill_404_counts_as_no_fixtures_and_the_request_uses_the_documented_parameter() -> None:
    client = VendorClient("https://api.oddspapi.io/v4", "k" * 36, TokenBucketRateLimiter(100.0, 10), max_retries=0,
                          auth_query_param="apiKey")
    replies = {
        404: '{"error": {"message": "No fixtures found for the specified tournament IDs and bookmaker.", '
             '"code": "FIXTURE_NOT_FOUND"}}',
        403: '{"error": {"message": "Forbidden", "code": "FORBIDDEN"}}',
    }

    class Reply:
        def __init__(self, status: int) -> None:
            self.status = status

        def get(self, url: str, timeout: float, params: dict[str, str] | None = None) -> requests.Response:
            response = requests.Response()
            response.status_code = self.status
            response._content = replies[self.status].encode()
            return response

    client._local.session = Reply(404)
    assert client.get_json_value("odds", empty_on=oddspapi.NO_FIXTURES_REPLIES) is None
    client._local.session = Reply(403)
    with pytest.raises(requests.HTTPError, match="HTTP 403"):
        client.get_json_value("odds", empty_on=oddspapi.NO_FIXTURES_REPLIES)
    assert "bookmakers=bet365&" in oddspapi.odds_path("bet365", [25, 24])


def _garbled_bodies() -> list[Any]:
    """Malformed odds-by-tournaments replies from bookmaker "garbled". Before readiness item 5 (2026-10-05) the first
    raised ValueError, which handler H11 turned into "no odds at all" (bet365's prices discarded too); the next three
    raised KeyError/AttributeError, which no handler catches, so the scan crashed before its summary; the NaN price
    was passed on as a price."""
    nested = _odds()
    nested[0]["bookmakerOdds"] = {"garbled": {"markets": [1, 2]}}
    nan_price = _odds()
    nan_price[0]["bookmakerOdds"] = {"garbled": nan_price[0]["bookmakerOdds"]["bet365"]}
    nan_price[0]["bookmakerOdds"]["garbled"]["markets"]["102661"]["outcomes"]["102661"]["players"]["2342841"]["price"]         = float("nan")
    return [
        {"error": "not a list"},
        [{"fixtureId": "f1", "startTime": "2026-10-10T14:00:00.000Z"}],
        nested,
        [{**_odds()[0], "bookmakerOdds": {"garbled": {"markets": {"102661": {"outcomes": {"102661": {
            "players": {"9": "2.40"}}}}}}}}],
        nan_price,
    ]


@pytest.mark.parametrize("body", _garbled_bodies())
def test_prop_prices_refuse_a_malformed_reply_with_value_error_only(body: Any) -> None:
    with pytest.raises(ValueError, match="malformed OddsPapi response"):
        oddspapi.prop_prices(body, oddspapi.line_catalogue(MARKETS), "garbled")


@pytest.mark.parametrize("body", _garbled_bodies())
def test_one_malformed_bookmaker_reply_is_dropped_and_the_others_prices_kept(tmp_path: Path, body: Any) -> None:
    client = FakeOddsClient(left=200, odds=_odds())
    client.garbled = body
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                                   ("garbled", "bet365"), tmp_path)
    assert len(coverage.quotes) == 2 and {q.sportsbook for q in coverage.quotes} == {"bet365"} and available
    assert any(n.startswith("Odds FAILED from garbled, its prices left out: ValueError: malformed OddsPapi response")
               for n in notes)
    archive = tmp_path / "odds_archive"
    assert len(list(archive.glob("*_garbled.rejected.json.gz"))) == 1  # kept for diagnosis
    assert list(archive.glob("*_garbled.json.gz")) == []  # so closing capture never asks this book for the scan
    assert len(list(archive.glob("*_bet365.json.gz"))) == 1


def test_every_bookmaker_malformed_means_no_real_odds(tmp_path: Path) -> None:
    client = FakeOddsClient(left=200, odds=_odds())
    client.garbled = _garbled_bodies()[1]
    coverage, notes, available = scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4),
                                                   ("garbled",), tmp_path)
    assert coverage.quotes == () and not available  # fail closed: run_scan exits ODDS_UNAVAILABLE_EXIT


@pytest.mark.parametrize("account", [
    {"current_subscription_id": "s", "subscriptions": [{"subscription_id": "s"}]},
    {"current_subscription_id": "s", "subscriptions": [{"subscription_id": "s", "request_limit": "250",
                                                         "request_count": 3}]},
    {"current_subscription_id": "s", "subscriptions": {"s": 1}},
    ["not", "an", "object"],
])
def test_a_malformed_account_or_catalogue_raises_value_error_for_handler_h11(tmp_path: Path, account: Any) -> None:
    """Run-level replies (account, catalogue, names) have no per-bookmaker fallback: they must raise ValueError, which
    H11 reports as "Odds unavailable this run", never KeyError/AttributeError, which would crash the run."""
    client = FakeOddsClient(left=200, odds=_odds())
    client.account = account
    with pytest.raises(ValueError):
        scan.collect_odds(client, [FIXTURE], {9001: SQUAD}, NOW, NOW + timedelta(hours=4), ("bet365",), tmp_path)
    for catalogue in ({"not": "a list"}, [{**MARKETS[0], "marketId": None}], [{**MARKETS[0], "handicap": "1.5"}],
                      [{**MARKETS[0], "outcomes": [None]}]):
        with pytest.raises(ValueError):
            oddspapi.line_catalogue(catalogue)
    with pytest.raises(ValueError):
        oddspapi.participant_names({"x56": "Walsall"})
