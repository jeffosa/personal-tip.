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


def line_catalogue(markets: Sequence[Mapping[str, Any]]) -> dict[int, LineMarket]:
    """marketId -> LineMarket for the player O/U markets in MARKET_TYPES."""
    catalogue: dict[int, LineMarket] = {}
    for market in markets:
        kind = MARKET_TYPES.get(str(market.get("marketType")))
        if kind is None or not market.get("playerProp"):
            continue
        sides = {int(o["outcomeId"]): str(o["outcomeName"]).lower() for o in market.get("outcomes") or []}
        if sorted(sides.values()) != ["over", "under"]:
            raise ValueError(f"market {market.get('marketId')}: outcomes are not Over/Under: {sorted(sides.values())}")
        catalogue[int(market["marketId"])] = LineMarket(kind, float(market["handicap"]), sides)
    return catalogue


def participant_names(body: Any) -> dict[int, str]:
    if not isinstance(body, Mapping):
        raise ValueError("OddsPapi participants response is not an object")
    return {int(key): str(name) for key, name in body.items()}


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


def _kickoff(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"OddsPapi startTime {raw!r} is not UTC")
    return value


def prop_prices(body: Any, catalogue: Mapping[int, LineMarket], bookmaker: str) -> list[PropPrice]:
    """Every active player O/U price in `body` (an odds-by-tournaments response) for MARKET_TYPES."""
    if not isinstance(body, list):
        raise ValueError("OddsPapi odds response is not a list")
    prices: list[PropPrice] = []
    for raw in body:
        fixture = OddsFixture(str(raw["fixtureId"]), int(raw["participant1Id"]), int(raw["participant2Id"]),
                              _kickoff(str(raw["startTime"])))
        book = (raw.get("bookmakerOdds") or {}).get(bookmaker)
        if not book or not book.get("bookmakerIsActive", True) or book.get("suspended"):
            continue
        for market_id, market in (book.get("markets") or {}).items():
            line_market = catalogue.get(int(market_id))
            if line_market is None or not market.get("marketActive", True):
                continue
            for outcome_id, outcome in (market.get("outcomes") or {}).items():
                side = line_market.sides.get(int(outcome_id))
                if side is None:
                    continue
                for player_ref, quote in (outcome.get("players") or {}).items():
                    price = quote.get("price")
                    if not quote.get("active", True) or not isinstance(price, (int, float)) or price <= 1.0:
                        continue
                    limit = quote.get("limit")
                    prices.append(PropPrice(
                        fixture, bookmaker, line_market.market, line_market.line, side, str(player_ref),
                        str(quote.get("playerName") or ""), float(price),
                        float(limit) if isinstance(limit, (int, float)) and limit > 0 else None,
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
