"""TheStatsAPI (maintainer, 2026-10-13): the pure adapter (ingestion/thestatsapi.py), the client's match-aware adapter and
monthly-quota header (ingestion/pipeline.py), and the backfill's database split (scripts/tsa_backfill.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from prop_alpha.ingestion import thestatsapi
from prop_alpha.ingestion.pipeline import QUOTA_REMAINING_HEADERS, build_thestatsapi_client
from prop_alpha.ingestion.transform import parse_fixture_payload

ROOT = Path(__file__).resolve().parents[1]
MATCH: dict[str, Any] = {
    "id": "mt_1", "competition_id": "comp_4795", "matchday": 29, "status": "finished", "utc_date": "2026-10-08T22:30:00.000Z",
    "home_team": {"id": "tm_h", "name": "Santos"}, "away_team": {"id": "tm_a", "name": "Flamengo"},
    "score": {"home": 2, "away": 2},
}


def _row(pid: str, team: str, started: bool, minutes: int | None, *, tackles: int | None = 1, fouls: int | None = 2,
         position: str | None = "D", played: bool | None = None) -> dict[str, Any]:
    return {"player_id": pid, "player_name": f"  Player  {pid} ", "team_id": team, "position": position, "started": started,
            "played": (minutes or 0) > 0 if played is None else played, "minutes_played": minutes,
            "duels": {"duel_won": 3, "duel_lost": 4, "challenge_lost": 2, "aerial_won": 1},
            "defending": {"tackles": tackles, "interceptions": 1},
            "general": {"fouls": fouls, "was_fouled": 1, "yellow_cards": 1, "red_cards": 0,
                        "player_subbed_on": "pl_other", "player_subbed_off": None}}


def _stats(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(rows)}


def test_a_match_and_its_player_stats_become_the_payload_transform_parses() -> None:
    starter, sub, bench = _row("pl_1", "tm_h", True, 69), _row("pl_2", "tm_a", False, 21, position="F"), \
        _row("pl_3", "tm_a", False, 0, tackles=None, fouls=None, position=None)
    payload = thestatsapi.fixture_payload(MATCH, _stats(starter, sub, bench))
    parsed = parse_fixture_payload(payload)
    assert parsed.fixture.vendor_uid == "mt_1" and parsed.fixture.status == "final" and parsed.fixture.matchday == 29
    assert parsed.fixture.kickoff_at == "2026-10-08T22:30:00+00:00" and (parsed.fixture.home_score, parsed.fixture.away_score) == (2, 2)
    first, second, third = parsed.players
    assert (first.name, first.position, first.minutes_played, first.minute_on, first.minute_off) == ("Player pl_1", "DEF", 69, 0, 69)
    assert (first.tackles_attempted, first.dribbled_past, first.interceptions, first.duels_total, first.duels_won) == (1, 2, 1, 7, 3)
    assert (first.fouls_committed, first.fouls_drawn, first.yellow_cards, first.red_cards) == (2, 1, 1, 0)
    assert first.tackles_won == thestatsapi.TACKLES_WON_NOT_PROVIDED == 0  # no TheStatsAPI field; not a pricing input
    assert first.state_level_min == 69  # no goal minutes are fetched: every minute on counts as level
    assert (second.position, second.minute_on, second.minute_off) == ("ATT", 69, 90)  # a substitute's minutes end at 90
    assert (third.minutes_played, third.tackles_attempted, third.fouls_committed, third.position) == (0, 0, 0, None)
    assert payload["goals"] == [] and payload["referee"] is None and payload["vendor_response"]["match"]["id"] == "mt_1"


@pytest.mark.parametrize("row, match", [
    (_row("pl_1", "tm_h", True, 90, tackles=None), "defending.tackles is missing"),
    (_row("pl_1", "tm_h", True, 90, fouls=None), "general.fouls is missing"),
    (_row("pl_1", "tm_h", True, 0), "started but has no minutes_played"),
    (_row("pl_1", "tm_x", True, 90), "starter's team is neither side"),
    (_row("pl_1", "tm_h", True, 90, position="Z"), "unknown position"),
    (_row("pl_1", "tm_h", True, 90, tackles=2.5), "not a whole number"),  # type: ignore[arg-type]
    ({**_row("pl_1", "tm_h", True, 90), "started": "yes"}, "'started' is not a boolean"),
])
def test_a_missing_or_malformed_number_refuses_the_match_instead_of_becoming_zero(row: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        thestatsapi.fixture_payload(MATCH, _stats(row))


def test_a_player_filed_under_another_team_id_belongs_to_the_side_his_club_team_id_names() -> None:
    """Colchester v Harrogate (2025-10-18): a substitute's team_id was a youth side's, his club_team_id Colchester's."""
    youth = {**_row("pl_9", "tm_youth", False, 1), "club_team_id": "tm_h"}
    payload = thestatsapi.fixture_payload(MATCH, _stats(_row("pl_1", "tm_h", True, 90), youth))
    assert [p["team_uid"] for p in payload["players"]] == ["tm_h", "tm_h"]
    # Salford v Crawley (2025-11-29): a 1-minute substitute whose team_id and club_team_id are both other clubs' is left out
    # and logged (as Sportmonks' unidentified substitutes are); a starter who cannot be placed refuses the match.
    stranger = {**_row("pl_9", "tm_loan", False, 1), "club_team_id": "tm_other"}
    payload = thestatsapi.fixture_payload(MATCH, _stats(_row("pl_1", "tm_h", True, 90), stranger))
    assert [p["uid"] for p in payload["players"]] == ["pl_1"]
    assert payload["vendor_response"]["player_stats"]["data"][1]["player_id"] == "pl_9"  # the raw row is kept



def _xi(team: str, n: int, prefix: str) -> list[dict[str, Any]]:
    return [_row(f"{prefix}{i}", team, True, 90) for i in range(n)]


def test_an_unplaced_starter_completes_the_one_starting_xi_short_of_eleven_or_refuses_the_match() -> None:
    """Notts County v Walsall (2025-12-20): Jid Okeke started for Walsall but was filed under another club's id (his parent
    club, a loan); Walsall listed 10 starters. The starter fills the one XI that is short; anything ambiguous refuses."""
    loanee = {**_row("pl_loan", "tm_parent", True, 90), "club_team_id": "tm_other"}
    payload = thestatsapi.fixture_payload(MATCH, _stats(*_xi("tm_h", 11, "h"), *_xi("tm_a", 10, "a"), loanee))
    assert {p["uid"]: p["team_uid"] for p in payload["players"]}["pl_loan"] == "tm_a"
    with pytest.raises(ValueError, match=r"starting XIs \(10 and 10\) do not say which"):  # both sides short
        thestatsapi.fixture_payload(MATCH, _stats(*_xi("tm_h", 10, "h"), *_xi("tm_a", 10, "a"), loanee))
    with pytest.raises(ValueError, match=r"starting XIs \(11 and 11\) do not say which"):  # nobody short
        thestatsapi.fixture_payload(MATCH, _stats(*_xi("tm_h", 11, "h"), *_xi("tm_a", 11, "a"), loanee))
    second = {**loanee, "player_id": "pl_loan2"}
    with pytest.raises(ValueError, match="do not say which"):  # two unplaced, one place
        thestatsapi.fixture_payload(MATCH, _stats(*_xi("tm_h", 11, "h"), *_xi("tm_a", 10, "a"), loanee, second))


def test_unstorable_matches_and_replies_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be stored"):
        thestatsapi.fixture_payload({**MATCH, "status": "cancelled"}, _stats(_row("pl_1", "tm_h", True, 90)))
    with pytest.raises(ValueError, match="malformed player-stats reply"):
        thestatsapi.fixture_payload(MATCH, {"data": {"players": []}})
    with pytest.raises(ValueError, match="no player has minutes"):
        thestatsapi.fixture_payload(MATCH, _stats(_row("pl_3", "tm_a", False, 0)))
    with pytest.raises(ValueError, match="no home team"):
        thestatsapi.fixture_payload({**MATCH, "home_team": None}, _stats())


def test_the_listing_path_rows_and_clubs() -> None:
    assert thestatsapi.matches_path("comp_0196", "2025-07-01", "2026-10-13", 2, "finished") == (
        "football/matches?competition_id=comp_0196&date_from=2025-07-01&date_to=2026-10-13&per_page=100&page=2"
        "&sort=utc_date&status=finished")
    with pytest.raises(ValueError, match="not a competition this project uses"):
        thestatsapi.matches_path("comp_9999", "2025-07-01", "2026-10-13", 1)
    assert thestatsapi.match_rows({"data": [MATCH, "junk"], "meta": {"total_pages": 3}}) == ([MATCH], 3)
    with pytest.raises(ValueError, match="malformed TheStatsAPI match listing"):
        thestatsapi.match_rows({"data": [MATCH]})
    later = {**MATCH, "id": "mt_2", "competition_id": "comp_1085", "utc_date": "2026-10-20T20:00:00Z"}
    clubs = thestatsapi.clubs([later, MATCH])  # a club's league is its most recent match's, whatever the order
    assert clubs["tm_h"][:2] == ("Santos", "comp_1085") and set(clubs) == {"tm_h", "tm_a"}


def test_the_client_hands_the_listed_match_to_the_adapter_and_reads_only_the_monthly_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environ = {"THESTATSAPI_KEY": "test-key", "DATA_VENDOR_QUOTA_RESERVE": "100"}
    client = build_thestatsapi_client(environ, {"mt_1": MATCH})
    asked: list[str] = []

    def get_json(path: str) -> dict[str, Any]:
        asked.append(path)
        return _stats(_row("pl_1", "tm_h", True, 90))

    monkeypatch.setattr(client, "get_json", get_json)
    payload = client.get_fixture_feed("mt_1")
    assert asked == ["football/matches/mt_1/player-stats"] and payload["fixture"]["home_team_uid"] == "tm_h"
    with pytest.raises(ValueError, match="not in the listing"):
        client.get_fixture_feed("mt_404")
    assert client.base_url == thestatsapi.BASE_URL and (client.auth_header, client.auth_prefix) == ("Authorization", "Bearer ")
    # TheStatsAPI's X-RateLimit-Remaining is a per-minute budget (12 on a trial): it must not stop the run as a quota.
    client._record_remaining_quota({"x-ratelimit-remaining": "3"})  # noqa: SLF001
    assert client._quota_stop is None  # noqa: SLF001
    client._record_remaining_quota({"x-monthly-quota-remaining": "99"})  # noqa: SLF001
    assert "x-monthly-quota-remaining" in str(client._quota_stop)  # noqa: SLF001
    assert "x-ratelimit-remaining" in QUOTA_REMAINING_HEADERS  # the other vendors keep their headers
    client.close()


def test_a_client_takes_one_kind_of_adapter() -> None:
    from prop_alpha.ingestion.pipeline import TokenBucketRateLimiter, VendorClient

    with pytest.raises(ValueError, match="not both"):
        VendorClient("https://example.com", "k", TokenBucketRateLimiter(rate_per_sec=1.0, burst=1),
                     stats_adapter=lambda body: dict(body), fixture_adapter=lambda uid, body: dict(body))


def _backfill() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("tsa_backfill", ROOT / "scripts" / "tsa_backfill.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_league_one_and_two_go_to_the_working_database_and_the_rest_to_the_pilot_one() -> None:
    backfill = _backfill()
    matches: list[Mapping[str, Any]] = [{**MATCH, "id": f"mt_{c}", "competition_id": c} for c in thestatsapi.COMPETITIONS]
    split = backfill.by_database(matches)
    assert sorted(m["competition_id"] for m in split["prop_alpha_tsa"]) == ["comp_0196", "comp_4023"]
    assert sorted(m["competition_id"] for m in split["prop_alpha_pilot_tsa"]) == ["comp_4795", "comp_6236", "comp_6387"]
    assert backfill.WORKING_DB != "prop_alpha" and backfill.PILOT_DB != "prop_alpha_pilot"  # the Sportmonks archive is never written


def test_a_failed_request_drops_the_threads_connection_so_the_next_one_starts_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-10-14/15: after a Wi-Fi dropout the backfill reused a broken connection and failed every request for hours."""
    import requests

    client = build_thestatsapi_client({"THESTATSAPI_KEY": "test-key"})
    broken = client._session()  # noqa: SLF001

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise requests.exceptions.SSLError("EOF occurred in violation of protocol")

    monkeypatch.setattr(broken, "get", fail)
    with pytest.raises(requests.ConnectionError, match="SSLError"):
        client._get("https://api.thestatsapi.com/api/health")  # noqa: SLF001
    fresh = client._session()  # noqa: SLF001
    assert fresh is not broken and broken not in client._sessions and client._sessions == [fresh]  # noqa: SLF001
    client.close()
