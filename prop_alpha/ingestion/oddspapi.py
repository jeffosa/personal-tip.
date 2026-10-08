"""Pure adapters for OddsPapi v4 (no I/O): market catalogue, participant names and player-prop odds.

Response shapes verified on 2026-10-03 (bet365, League One/Two): odds-by-tournaments returns a list of fixtures;
each carries bookmakerOdds[book].markets[marketId].outcomes[outcomeId].players[playerId] with playerName
("Last, First"), price (decimal), limit (often null) and active. Every O/U line is its own marketId; the catalogue
(/v4/markets) gives its marketType, handicap (the line) and which outcome is Over and which Under.

Market contract (assumed until the settlement check of PRD step 3): OddsPapi "Player Tackles" settles on the count
Sportmonks reports as TACKLES, "Player Fouls Committed" on FOULS, both including extra time, and a player who
does not play is void (VOID_IF_NO_APPEARANCE).
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Mapping, Sequence

BASE_URL: Final[str] = "https://api.oddspapi.io/v4"
AUTH_QUERY_PARAM: Final[str] = "apiKey"
SOCCER_SPORT_ID: Final[int] = 10
# OddsPapi tournament ids for the scanned leagues (Sportmonks name -> OddsPapi id).
TOURNAMENTS: Final[Mapping[str, int]] = {"League One": 24, "League Two": 25}
# OddsPapi marketType -> prop_alpha market. Card markets are not priced by the scan yet.
MARKET_TYPES: Final[Mapping[str, str]] = {"playertotals-tackles": "tackles", "playertotals-foulscommitted": "fouls"}
# Championship/Premier League ladders (pilot, 2026-10-08): "k+" is Over k - 0.5 and "0" is Under 0.5.
LADDER_MARKET_TYPES: Final[Mapping[str, str]] = {"players-tackles": "tackles", "players-foulscommitted": "fouls"}
# Lower-league prop limits are £50-£500 (PRD Gate 4); a quote without a published limit is assumed to take 50.
UNKNOWN_LIMIT_STAKE: Final[float] = 50.0

# The only replies counted as "no fixtures" (HTTP status -> text the body must contain), both seen live on
# 2026-10-04: skybet 400 INVALID_PARAMETER "...is not supported or does not have any fixtures available." and
# williamhill 404 FIXTURE_NOT_FOUND "No fixtures found for the specified tournament IDs and bookmaker." Any other
# 400 or 404 is an error. The 400 wording does not separate a misspelt bookmaker from one with no fixtures.
NO_FIXTURES_REPLIES: Final[Mapping[int, str]] = MappingProxyType(
    {400: "does not have any fixtures available", 404: "FIXTURE_NOT_FOUND"})

MARKETS_PATH: Final[str] =f"markets?sportId={SOCCER_SPORT_ID}"
PARTICIPANTS_PATH: Final[str] = f"participants?sportId={SOCCER_SPORT_ID}"
ACCOUNT_PATH: Final[str] = "account"

# Object keys carry ids as strings ("102661"); only ASCII digits are ids (str.isdigit also accepts "²").
_DIGITS: Final[re.Pattern[str]] = re.compile(r"[0-9]+")
_COMMON_TEAM_WORDS: Final[frozenset[str]] = frozenset({
    "fc", "afc", "town", "city", "united", "utd", "county", "rovers", "athletic", "wanderers", "albion", "the",
})


def odds_path(bookmaker: str, tournament_ids: Sequence[int]) -> str:
    """One bookmaker per call, in the documented `bookmakers` parameter. The v4 docs allow up to 3, but the live API
    (checked 2026-10-04) answers three with HTTP 400 "Please provide exactly one bookmaker"; with one book the
    documented plural returned bet365's 24 League One/Two fixtures, as the singular did."""
    return (f"odds-by-tournaments?bookmakers={bookmaker}&tournamentIds={','.join(str(t) for t in sorted(tournament_ids))}"
            "&oddsFormat=decimal")


@dataclass(frozen=True)
class LineMarket:
    market: str
    line: float
    sides: Mapping[int, str]  # outcomeId -> "over" | "under"


def _object(value: Any, where: str) -> Mapping[str, Any]:
    """`value` as a JSON object; any other shape is a malformed response (ValueError, never KeyError/TypeError)."""
    if not isinstance(value, Mapping):
        raise ValueError(f"malformed OddsPapi response: {where} is not an object")
    return value


def _identifier(value: Any, where: str) -> int:
    """A non-negative integer id, given as a JSON number or as a string of ASCII digits (object keys)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        return int(value)
    raise ValueError(f"malformed OddsPapi response: {where} {value!r} is not an id")


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"malformed OddsPapi response: {where} {value!r} is not a finite number")
    return float(value)


def line_catalogue(markets: Any) -> dict[int, LineMarket]:
    """marketId -> LineMarket for the player O/U markets in MARKET_TYPES. Raises ValueError on a malformed list."""
    if not isinstance(markets, list):
        raise ValueError("malformed OddsPapi response: the market catalogue is not a list")
    catalogue: dict[int, LineMarket] = {}
    for raw in markets:
        market = _object(raw, "a catalogue market")
        kind = MARKET_TYPES.get(str(market.get("marketType")))
        if kind is None or not market.get("playerProp"):
            continue
        market_id = _identifier(market.get("marketId"), "catalogue marketId")
        outcomes = market.get("outcomes") or []
        if not isinstance(outcomes, list):
            raise ValueError(f"malformed OddsPapi response: market {market_id} outcomes are not a list")
        sides = {
            _identifier(outcome.get("outcomeId"), f"market {market_id} outcomeId"): str(outcome.get("outcomeName")).lower()
            for outcome in (_object(o, f"market {market_id} outcome") for o in outcomes)
        }
        if sorted(sides.values()) != ["over", "under"]:
            raise ValueError(f"market {market_id}: outcomes are not Over/Under: {sorted(sides.values())}")
        catalogue[market_id] = LineMarket(kind, _finite(market.get("handicap"), f"market {market_id} handicap"), sides)
    return catalogue


def participant_names(body: Any) -> dict[int, str]:
    names = _object(body, "the participants response")
    return {_identifier(key, "participant id"): str(name) for key, name in names.items()}


@dataclass(frozen=True)
class OddsFixture:
    odds_fixture_id: str
    home_id: int
    away_id: int
    kickoff_at: datetime


@dataclass(frozen=True)
class PropPrice:
    """One side of one bookmaker's player line."""

    fixture: OddsFixture
    bookmaker: str
    market: str
    line: float
    side: str
    player_ref: str
    player_name: str
    price: float
    limit: float | None


def _kickoff(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"malformed OddsPapi response: startTime {raw!r} is not a timestamp")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"OddsPapi startTime {raw!r} is not UTC")
    return value


def prop_prices(body: Any, catalogue: Mapping[int, LineMarket], bookmaker: str) -> list[PropPrice]:
    """Every active player O/U price in `body` (an odds-by-tournaments response) for MARKET_TYPES.

    A malformed body (wrong shape, missing fixture fields, a non-finite price or limit) raises ValueError, so the
    caller can drop this bookmaker's reply whole and keep the others' (scan.collect_odds, handler H12). An inactive
    quote, or a price of 1.0 or less, is not malformed: it is simply not on offer and is skipped."""
    if not isinstance(body, list):
        raise ValueError("malformed OddsPapi response: the odds response is not a list")
    prices: list[PropPrice] = []
    for raw_fixture in body:
        raw = _object(raw_fixture, "a fixture")
        if raw.get("fixtureId") in (None, ""):
            raise ValueError("malformed OddsPapi response: a fixture has no fixtureId")
        fixture = OddsFixture(str(raw["fixtureId"]), _identifier(raw.get("participant1Id"), "participant1Id"),
                              _identifier(raw.get("participant2Id"), "participant2Id"), _kickoff(raw.get("startTime")))
        book = _object(raw.get("bookmakerOdds") or {}, f"fixture {fixture.odds_fixture_id} bookmakerOdds").get(bookmaker)
        if not book:
            continue
        book = _object(book, f"fixture {fixture.odds_fixture_id} {bookmaker} odds")
        if not book.get("bookmakerIsActive", True) or book.get("suspended"):
            continue
        for market_id, raw_market in _object(book.get("markets") or {}, "markets").items():
            market = _object(raw_market, f"market {market_id}")
            line_market = catalogue.get(_identifier(market_id, "marketId"))
            if line_market is None or not market.get("marketActive", True):
                continue
            for outcome_id, raw_outcome in _object(market.get("outcomes") or {}, f"market {market_id} outcomes").items():
                side = line_market.sides.get(_identifier(outcome_id, f"market {market_id} outcomeId"))
                if side is None:
                    continue
                outcome = _object(raw_outcome, f"market {market_id} outcome {outcome_id}")
                for player_ref, raw_quote in _object(outcome.get("players") or {}, f"outcome {outcome_id} players").items():
                    quote = _object(raw_quote, f"player {player_ref} quote")
                    if not quote.get("active", True) or quote.get("price") is None:
                        continue
                    price = _finite(quote.get("price"), f"player {player_ref} price")
                    if price <= 1.0:
                        continue
                    limit = quote.get("limit")
                    stake_limit = None if limit is None else _finite(limit, f"player {player_ref} limit")
                    prices.append(PropPrice(
                        fixture, bookmaker, line_market.market, line_market.line, side, str(player_ref),
                        str(quote.get("playerName") or ""), price,
                        stake_limit if stake_limit is not None and stake_limit > 0 else None,
                    ))
    return prices


_LADDER_OUTCOME: Final[re.Pattern[str]] = re.compile(r"([0-9]+)\+")
_LADDER_ZERO: Final[str] = "0"
_LADDER_YES: Final[str] = "Yes"  # "Player To Commit A Foul": at least one, Over 0.5


@dataclass(frozen=True)
class LadderMarket:
    """`steps`: outcomeId -> (line, side, step name). "k+" (the player makes k or more) is Over k - 0.5; the outcome "0"
    (no tackles/fouls at all) is Under 0.5, and "Yes" (to commit a foul) is Over 0.5."""

    market: str
    steps: Mapping[int, tuple[float, str, str]]


@dataclass(frozen=True)
class LadderPrice:
    """One bookmaker's price on one ladder step for one player, as the equivalent over/under line and side."""

    fixture: OddsFixture
    bookmaker: str
    market: str
    line: float
    side: str
    step: str
    player_ref: str
    player_name: str
    price: float
    limit: float | None


def ladder_catalogue(markets: Any) -> dict[int, LadderMarket]:
    """marketId -> LadderMarket for the player ladder markets in LADDER_MARKET_TYPES. An outcome that is not "k+" with
    k >= 1, "0" or "Yes", raises ValueError: a ladder this parser does not understand is refused, not guessed."""
    if not isinstance(markets, list):
        raise ValueError("malformed OddsPapi response: the market catalogue is not a list")
    catalogue: dict[int, LadderMarket] = {}
    for raw in markets:
        market = _object(raw, "a catalogue market")
        kind = LADDER_MARKET_TYPES.get(str(market.get("marketType")))
        if kind is None or not market.get("playerProp"):
            continue
        market_id = _identifier(market.get("marketId"), "catalogue marketId")
        outcomes = market.get("outcomes") or []
        if not isinstance(outcomes, list):
            raise ValueError(f"malformed OddsPapi response: market {market_id} outcomes are not a list")
        steps: dict[int, tuple[float, str, str]] = {}
        for outcome in (_object(o, f"market {market_id} outcome") for o in outcomes):
            name = str(outcome.get("outcomeName"))
            matched = _LADDER_OUTCOME.fullmatch(name)
            if name == _LADDER_ZERO:
                step = (0.5, "under", name)
            elif name == _LADDER_YES:
                step = (0.5, "over", name)
            elif matched is not None and int(matched.group(1)) >= 1:
                step = (int(matched.group(1)) - 0.5, "over", name)
            else:
                raise ValueError(f"market {market_id}: outcome {name!r} is not a k+ ladder step, 0 or Yes")
            steps[_identifier(outcome.get("outcomeId"), f"market {market_id} outcomeId")] = step
        catalogue[market_id] = LadderMarket(kind, steps)
    return catalogue


def ladder_prices(body: Any, catalogue: Mapping[int, LadderMarket], bookmaker: str) -> list[LadderPrice]:
    """Every active player ladder price in `body` (an odds-by-tournaments response). Same rules as prop_prices: a
    malformed body raises ValueError; an inactive quote, or a price of 1.0 or less, is skipped."""
    if not isinstance(body, list):
        raise ValueError("malformed OddsPapi response: the odds response is not a list")
    prices: list[LadderPrice] = []
    for raw_fixture in body:
        raw = _object(raw_fixture, "a fixture")
        if raw.get("fixtureId") in (None, ""):
            raise ValueError("malformed OddsPapi response: a fixture has no fixtureId")
        fixture = OddsFixture(str(raw["fixtureId"]), _identifier(raw.get("participant1Id"), "participant1Id"),
                              _identifier(raw.get("participant2Id"), "participant2Id"), _kickoff(raw.get("startTime")))
        book = _object(raw.get("bookmakerOdds") or {}, f"fixture {fixture.odds_fixture_id} bookmakerOdds").get(bookmaker)
        if not book:
            continue
        book = _object(book, f"fixture {fixture.odds_fixture_id} {bookmaker} odds")
        if not book.get("bookmakerIsActive", True) or book.get("suspended"):
            continue
        for market_id, raw_market in _object(book.get("markets") or {}, "markets").items():
            market = _object(raw_market, f"market {market_id}")
            ladder = catalogue.get(_identifier(market_id, "marketId"))
            if ladder is None or not market.get("marketActive", True):
                continue
            for outcome_id, raw_outcome in _object(market.get("outcomes") or {}, f"market {market_id} outcomes").items():
                step = ladder.steps.get(_identifier(outcome_id, f"market {market_id} outcomeId"))
                if step is None:
                    continue
                outcome = _object(raw_outcome, f"market {market_id} outcome {outcome_id}")
                for player_ref, raw_quote in _object(outcome.get("players") or {}, f"outcome {outcome_id} players").items():
                    quote = _object(raw_quote, f"player {player_ref} quote")
                    if not quote.get("active", True) or quote.get("price") is None:
                        continue
                    price = _finite(quote.get("price"), f"player {player_ref} price")
                    if price <= 1.0:
                        continue
                    limit = quote.get("limit")
                    stake_limit = None if limit is None else _finite(limit, f"player {player_ref} limit")
                    prices.append(LadderPrice(
                        fixture, bookmaker, ladder.market, step[0], step[1], step[2], str(player_ref),
                        str(quote.get("playerName") or ""), price,
                        stake_limit if stake_limit is not None and stake_limit > 0 else None,
                    ))
    return prices


def normalise(name: str) -> str:
    """Lower-case ASCII words in first-last order: "O'Donkor, Gatlin" -> "gatlin odonkor"."""
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first} {last}"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z ]", "", ascii_name.lower().replace("-", " ")).split())


def team_tokens(name: str) -> frozenset[str]:
    """Distinctive words of a club name ("Milton Keynes Dons" -> {milton, keynes, dons})."""
    return frozenset(word for word in normalise(name).split() if word not in _COMMON_TEAM_WORDS)
