"""Championship pilot: price one upcoming match with the model and compare it with a bookmaker's player ladders.

    .venv\\Scripts\\python.exe -u scripts\\pilot_price_match.py [sportmonks_fixture_id] [bookmaker]

Default: Sportmonks fixture 19729059 (West Ham United v Queens Park Rangers, Friday 2026-10-09 19:00 UTC) against bet365.
Reads the pilot database (`prop_alpha_pilot`, read-only), makes two Sportmonks requests (the match, one listing) and one
OddsPapi request (the bookmaker's Championship reply, archived in .prop_alpha_state/pilot/). Nothing is alerted, stored
in the database or bet.

Pricing is the model's own build_projections with the Championship backtest settings, NOT the frozen League One/Two scan
settings: game-state k 21.1 for tackles and 1000 for fouls (fitted on Championship data before 2026-03-01), prior
strength 6, no rate multiplier. The comparison for each bookmaker quote is the model's probability that the quoted event
happens (the player appears and the count meets the quote) against 1 / price, and the expected profit per unit,
model probability x price - 1, which assumes the book voids a player who does not play as the model does (unverified).
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.scan import load_slate_inputs  # noqa: E402
from prop_alpha.config import credential  # noqa: E402
from prop_alpha.ingestion import oddspapi, pipeline, sportmonks  # noqa: E402
from prop_alpha.modeling.engine import QuantEngine, VoidRule  # noqa: E402
from prop_alpha.scanner.slate import UpcomingFixture, build_projections  # noqa: E402

PILOT_DSN: Final[str] = "postgresql://postgres@localhost:5432/prop_alpha_pilot"
GAME_STATE_K: Final[dict[str, float]] = {"tackles": 21.1, "fouls": 1000.0}
PRIOR_STRENGTH: Final[dict[str, float]] = {"tackles": 6.0, "fouls": 6.0}
PILOT_DIR: Final[Path] = ROOT / ".prop_alpha_state" / "pilot"


def upcoming(fixture_id: int) -> UpcomingFixture:
    c = pipeline.build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "6", "DATA_VENDOR_QUOTA_RESERVE": "200"})
    try:
        body = c.get_json(f"fixtures/{fixture_id}?include=participants;state")
    finally:
        c.close()
    fx = body["data"]
    home, away = sportmonks._sides(fx)  # noqa: SLF001 - the adapter's own reader
    names = {(p.get("meta") or {}).get("location"): str(p.get("name")) for p in fx.get("participants") or []}
    league = {8: "Premier League", 9: "Championship"}[int(fx["league_id"])]
    return UpcomingFixture(int(fx["id"]), league, home, away, names["home"], names["away"],
                           datetime.fromisoformat(sportmonks._kickoff(fx)))  # noqa: SLF001


def bookmaker_reply(bookmaker: str, tournament: int) -> list[dict[str, Any]]:
    reply = requests.get(f"{oddspapi.BASE_URL}/odds-by-tournaments", timeout=90, params={
        "apiKey": credential("ODDSPAPI_KEY", os.environ), "bookmakers": bookmaker, "tournamentIds": str(tournament),
        "oddsFormat": "decimal"})
    reply.raise_for_status()
    body: list[dict[str, Any]] = reply.json()
    PILOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with gzip.open(PILOT_DIR / f"{stamp}_{bookmaker}_championship.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(body, handle)
    return body


def match_player(name: str, squad_names: dict[str, int]) -> int | None:
    """Player id from a bookmaker's name: exact normalised name, else a unique surname plus first initial."""
    key = oddspapi.normalise(name)
    if key in squad_names:
        return squad_names[key]
    words = key.split()
    if len(words) < 2:
        return None
    hits = {pid for n, pid in squad_names.items() if n.split()[-1] == words[-1] and n[0] == words[0][0]}
    return next(iter(hits)) if len(hits) == 1 else None


def main() -> int:
    import psycopg2

    fixture_id = int(sys.argv[1]) if len(sys.argv) > 1 else 19729059
    bookmaker = sys.argv[2] if len(sys.argv) > 2 else "bet365"
    fixture = upcoming(fixture_id)
    print(f"{fixture.name}, {fixture.kickoff_at:%a %d %b %H:%M} UTC ({fixture.league})", flush=True)
    conn = psycopg2.connect(PILOT_DSN, options="-c default_transaction_read_only=on")
    with conn, conn.cursor() as cur:
        inputs = load_slate_inputs(cur, fixture)
    conn.close()
    engine = QuantEngine(game_state_k=6.0)
    projections, skipped = build_projections(
        fixture, inputs.squad, inputs.history_by_player, inputs.team_recent_fixtures, inputs.league_rates, engine,
        season_levels=inputs.season_levels or None,
        market_engines={m: QuantEngine(game_state_k=k) for m, k in GAME_STATE_K.items()}, prior_strengths=PRIOR_STRENGTH)
    priced = sorted({key[1] for key in projections})
    print(f"squad {len(inputs.squad)}; priced regular starters {len(priced)}; left out {len(skipped)}", flush=True)

    squad_names = {oddspapi.normalise(p.name): p.player_id for p in inputs.squad}
    player_name = {p.player_id: p.name for p in inputs.squad}
    markets = requests.get(f"{oddspapi.BASE_URL}/markets", timeout=90,
                           params={"apiKey": credential("ODDSPAPI_KEY", os.environ), "sportId": "10"}).json()
    catalogue = oddspapi.ladder_catalogue(markets)
    body = bookmaker_reply(bookmaker, 18)
    prices = [p for p in oddspapi.ladder_prices(body, catalogue, bookmaker)
              if abs((p.fixture.kickoff_at - fixture.kickoff_at).total_seconds()) < 900]
    print(f"{bookmaker}: {len(prices)} ladder prices for this match, {len({p.player_ref for p in prices})} players", flush=True)

    rows: list[dict[str, Any]] = []
    unmatched: set[str] = set()
    for p in prices:
        pid = match_player(p.player_name, squad_names)
        if pid is None:
            unmatched.add(p.player_name)
            continue
        projection = projections.get((fixture.fixture_id, pid, p.market))
        if projection is None:
            continue                                     # not a regular starter: the model does not price him
        sides = projection.side_probabilities(p.line, p.side, VoidRule.VOID_IF_NO_APPEARANCE)
        model_p = sides.p_win / (sides.p_win + sides.p_lose)
        rows.append({"player": player_name[pid], "market": p.market, "step": p.step, "side": p.side, "line": p.line,
                     "price": p.price, "implied": 1.0 / p.price, "model": model_p, "ev": model_p * p.price - 1.0,
                     "p_void": sides.p_void})
    print(f"compared {len(rows)} quotes for {len({r['player'] for r in rows})} players "
          f"({len(unmatched)} bookmaker names unmatched or not regular starters)", flush=True)
    if not rows:
        return 1
    ev = np.array([r["ev"] for r in rows])
    model = np.array([r["model"] for r in rows])
    implied = np.array([r["implied"] for r in rows])
    print(f"\nmean model probability {model.mean():.3f} v mean implied (1/price) {implied.mean():.3f}; "
          f"correlation {np.corrcoef(model, implied)[0, 1]:.3f}")
    print(f"expected profit per unit: mean {ev.mean():+.3f}, median {np.median(ev):+.3f}; "
          f"quotes with EV >= +4%: {(ev >= 0.04).sum()}, >= +10%: {(ev >= 0.10).sum()}, <= -10%: {(ev <= -0.10).sum()}")
    by_step: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_step[(r["market"], r["step"])].append(r)
    print("\nby market and step: n, mean model p, mean implied p, mean EV")
    for key in sorted(by_step, key=lambda k: (k[0], 0 if k[1] in ("0", "Yes") else int(k[1].rstrip("+")))):
        group = by_step[key]
        print(f"  {key[0]:8s} {key[1]:>4s}  n={len(group):3d}  model {np.mean([g['model'] for g in group]):.3f}  "
              f"implied {np.mean([g['implied'] for g in group]):.3f}  EV {np.mean([g['ev'] for g in group]):+.3f}")
    ranked = sorted(rows, key=lambda r: -r["ev"])
    print("\nlargest model-over-market gaps (model likelier than the price implies):")
    for r in ranked[:8]:
        print(f"  {r['player']:26s} {r['market']:7s} {r['step']:>3s} price {r['price']:6.2f} implied {r['implied']:.2f} model {r['model']:.2f} EV {r['ev']:+.2f}")
    print("largest gaps the other way (market likelier than the model):")
    for r in ranked[-5:]:
        print(f"  {r['player']:26s} {r['market']:7s} {r['step']:>3s} price {r['price']:6.2f} implied {r['implied']:.2f} model {r['model']:.2f} EV {r['ev']:+.2f}")
    out = PILOT_DIR / f"compare_{fixture.fixture_id}_{bookmaker}.json"
    out.write_text(json.dumps({"fixture": fixture.name, "kickoff": fixture.kickoff_at.isoformat(), "bookmaker": bookmaker,
                               "settings": {"game_state_k": GAME_STATE_K, "prior_strength": PRIOR_STRENGTH,
                                            "rate_multiplier": None}, "rows": rows,
                               "unmatched": sorted(unmatched)}, indent=1), encoding="utf-8")
    print(f"\nresults: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
