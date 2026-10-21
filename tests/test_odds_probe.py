"""scripts/odds_probe.py (2026-10-21): the matchday bookmaker coverage probe. The pure counting and the report; the run
itself makes OddsPapi requests and is started by the one-off task "prop_alpha\\Odds probe"."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from prop_alpha.ingestion import oddspapi

ROOT = Path(__file__).resolve().parents[1]
AT = datetime(2026, 10, 24, 13, 0, tzinfo=timezone.utc)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("odds_probe", ROOT / "scripts" / "odds_probe.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LADDERS = oddspapi.ladder_catalogue([{"marketId": 5001, "marketType": "players-tackles", "playerProp": True,
                                      "outcomes": [{"outcomeId": 1, "outcomeName": "1+"},
                                                   {"outcomeId": 2, "outcomeName": "2+"}]}])


def _fixture(fixture_id: str, tournament: int, book: str, with_ladder: bool) -> dict[str, Any]:
    markets: dict[str, Any] = {}
    if with_ladder:
        outcomes = {str(o): {"players": {"p1": {"active": True, "price": price, "playerName": "A Player", "limit": 50}}}
                    for o, price in ((1, 1.5), (2, 3.0))}
        markets = {"5001": {"marketActive": True, "outcomes": outcomes}}
    return {"fixtureId": fixture_id, "tournamentId": tournament, "participant1Id": 1, "participant2Id": 2,
            "startTime": "2026-10-24T14:00:00Z", "bookmakerOdds": {book: {"bookmakerIsActive": True, "markets": markets}}}


def test_coverage_counts_fixtures_and_tackles_prices_per_league() -> None:
    probe = _module()
    body = [_fixture("c1", 18, "bet365", True), _fixture("c2", 18, "bet365", False), _fixture("l1", 24, "bet365", False)]
    found = probe.coverage(body, "bet365", {}, LADDERS)
    assert found["Championship"] == {"fixtures": 2, "with_props": 1, "prices": {"tackles": 2}}
    assert found["League One"] == {"fixtures": 1, "with_props": 0, "prices": {}}
    assert found["League Two"] == {"fixtures": 0, "with_props": 0, "prices": {}}


def test_the_report_names_the_books_quoting_each_league_and_keeps_failures() -> None:
    probe = _module()
    results = {"bet365": probe.coverage([_fixture("c1", 18, "bet365", True)], "bet365", {}, LADDERS),
               "skybet": "no fixtures (or a bookmaker name OddsPapi does not know)",
               "coral": "FAILED (HTTPError: 500)"}
    text = probe.report(AT, results)
    assert text.startswith("OddsPapi bookmaker probe Sat 24 Oct 13:00 UTC")
    assert "bet365: League One 0/0 fixtures (no tackles/fouls); League Two 0/0 fixtures (no tackles/fouls); " \
           "Championship 1/1 fixtures (tackles 2)" in text
    assert "\nskybet: no fixtures" in text and "\ncoral: FAILED (HTTPError: 500)" in text
    assert text.endswith("League One: no bookmaker quoted player tackles or fouls\n"
                         "League Two: no bookmaker quoted player tackles or fouls\n"
                         "Championship: tackles/fouls quoted by bet365")
