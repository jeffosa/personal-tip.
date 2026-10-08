from __future__ import annotations

import copy
from typing import Any

import pytest

from prop_alpha.ingestion import oddspapi

CATALOGUE_REPLY: list[dict[str, Any]] = [
    {"marketId": 102638, "marketName": "Player Tackles (incl. overtime)", "playerProp": True, "marketType": "players-tackles",
     "outcomes": [{"outcomeId": 102649, "outcomeName": "0"}, {"outcomeId": 102650, "outcomeName": "1+"},
                  {"outcomeId": 102651, "outcomeName": "2+"}, {"outcomeId": 102652, "outcomeName": "3+"}]},
    {"marketId": 102700, "marketName": "Player Fouls Committed (incl. overtime)", "playerProp": True,
     "marketType": "players-foulscommitted",
     "outcomes": [{"outcomeId": 102702, "outcomeName": "2+"}, {"outcomeId": 102703, "outcomeName": "3+"}]},
    # Over/Under markets and cards are not ladders: left to line_catalogue, ignored here.
    {"marketId": 102661, "marketName": "Over Under Player Tackles", "playerProp": True, "marketType": "playertotals-tackles",
     "handicap": 1.5, "outcomes": [{"outcomeId": 1, "outcomeName": "Over"}, {"outcomeId": 2, "outcomeName": "Under"}]},
    {"marketId": 555, "marketName": "Player To Be Carded", "playerProp": True, "marketType": "players-carded", "outcomes": []},
]


def _quote(name: str, price: float, **extra: Any) -> dict[str, Any]:
    return {"active": True, "playerName": name, "price": price, "limit": None, **extra}


def _body() -> list[dict[str, Any]]:
    return [{
        "fixtureId": "id100", "participant1Id": 37, "participant2Id": 1, "startTime": "2026-10-09T19:00:00.000Z",
        "bookmakerOdds": {"bet365": {"bookmakerIsActive": True, "suspended": False, "markets": {
            "102638": {"marketActive": True, "outcomes": {
                "102649": {"players": {"11": _quote("Cirkin, Dennis", 6.0)}},
                "102650": {"players": {"11": _quote("Cirkin, Dennis", 1.4), "12": _quote("Kamara, Glen", 1.2)}},
                "102651": {"players": {"11": _quote("Cirkin, Dennis", 2.2), "12": _quote("Kamara, Glen", 1.9, limit=250)}},
                "102652": {"players": {"11": _quote("Cirkin, Dennis", 4.33)}},
            }},
            "102700": {"marketActive": True, "outcomes": {"102702": {"players": {"13": _quote("Saito, Koki", 1.58)}}}},
            "102661": {"marketActive": True, "outcomes": {"1": {"players": {"11": _quote("Cirkin, Dennis", 1.9)}}}},
        }}},
    }]


def test_the_catalogue_keeps_tackles_and_fouls_ladders_with_their_thresholds_only() -> None:
    catalogue = oddspapi.ladder_catalogue(CATALOGUE_REPLY)
    assert set(catalogue) == {102638, 102700}
    assert catalogue[102638] == oddspapi.LadderMarket("tackles", {
        102649: (0.5, "under", "0"), 102650: (0.5, "over", "1+"), 102651: (1.5, "over", "2+"), 102652: (2.5, "over", "3+")})
    assert catalogue[102700].market == "fouls"
    assert catalogue[102700].steps == {102702: (1.5, "over", "2+"), 102703: (2.5, "over", "3+")}
    assert set(oddspapi.line_catalogue(CATALOGUE_REPLY)) == {102661}  # the over/under parser is untouched


def test_a_ladder_step_that_is_not_k_plus_is_refused() -> None:
    bad = copy.deepcopy(CATALOGUE_REPLY)
    bad[0]["outcomes"][0]["outcomeName"] = "Over"
    with pytest.raises(ValueError, match="ladder step"):
        oddspapi.ladder_catalogue(bad)
    bad[0]["outcomes"][0]["outcomeName"] = "0+"
    with pytest.raises(ValueError, match="ladder step"):
        oddspapi.ladder_catalogue(bad)
    with pytest.raises(ValueError, match="not a list"):
        oddspapi.ladder_catalogue({"markets": []})


def test_prices_carry_the_threshold_player_price_limit_and_the_equivalent_over_line() -> None:
    catalogue = oddspapi.ladder_catalogue(CATALOGUE_REPLY)
    prices = oddspapi.ladder_prices(_body(), catalogue, "bet365")
    assert len(prices) == 7                                  # the over/under quote is not a ladder price
    by_key = {(p.market, p.player_name, p.step): p for p in prices}
    three = by_key[("tackles", "Cirkin, Dennis", "3+")]
    assert (three.price, three.line, three.side) == (4.33, 2.5, "over")     # 3 or more = over 2.5
    assert by_key[("tackles", "Kamara, Glen", "2+")].limit == 250.0
    assert by_key[("tackles", "Kamara, Glen", "1+")].limit is None
    assert by_key[("fouls", "Saito, Koki", "2+")].line == 1.5
    zero = by_key[("tackles", "Cirkin, Dennis", "0")]
    assert (zero.line, zero.side, zero.price) == (0.5, "under", 6.0)        # no tackles at all = under 0.5
    assert prices[0].fixture.odds_fixture_id == "id100" and prices[0].fixture.home_id == 37


def test_inactive_suspended_and_non_prices_are_skipped_and_other_books_ignored() -> None:
    catalogue = oddspapi.ladder_catalogue(CATALOGUE_REPLY)
    body = _body()
    outcomes = body[0]["bookmakerOdds"]["bet365"]["markets"]["102638"]["outcomes"]
    outcomes["102650"]["players"]["11"]["active"] = False
    outcomes["102650"]["players"]["12"]["price"] = 1.0
    assert len(oddspapi.ladder_prices(body, catalogue, "bet365")) == 5
    assert oddspapi.ladder_prices(body, catalogue, "coral") == []
    body[0]["bookmakerOdds"]["bet365"]["suspended"] = True
    assert oddspapi.ladder_prices(body, catalogue, "bet365") == []


@pytest.mark.parametrize("damage", ["not-a-list", "no-fixture-id", "bad-price", "bad-kickoff"])
def test_a_malformed_reply_raises_value_error_only(damage: str) -> None:
    catalogue = oddspapi.ladder_catalogue(CATALOGUE_REPLY)
    body: Any = _body()
    if damage == "not-a-list":
        body = {"fixtures": []}
    elif damage == "no-fixture-id":
        body[0]["fixtureId"] = None
    elif damage == "bad-price":
        body[0]["bookmakerOdds"]["bet365"]["markets"]["102638"]["outcomes"]["102650"]["players"]["11"]["price"] = "evens"
    else:
        body[0]["startTime"] = 12
    with pytest.raises(ValueError, match="malformed OddsPapi response"):
        oddspapi.ladder_prices(body, catalogue, "bet365")
