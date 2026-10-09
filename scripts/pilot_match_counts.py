"""Championship pilot: after a match, test which Sportmonks statistic bet365's ladder prices behave like.

    .venv\\Scripts\\python.exe -u scripts\\pilot_match_counts.py [sportmonks_fixture_id] [bookmaker]

Needs .prop_alpha_state/pilot/compare_<fixture>_<bookmaker>.json from scripts/pilot_price_match.py (the quotes that were
compared before kickoff). Fetches the final match from Sportmonks (one request), prints each compared player's minutes and
counts (tackles attempted, tackles won, interceptions, fouls committed and drawn), and for each candidate definition the
share of the bookmaker's quotes whose event happened against the average probability the prices implied. A definition
that matches the book's settlement statistic should show realised frequencies close to the implied ones (allowing for the
margin and a small sample); one match, 27 players: it is a check, not a proof. Read-only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Final

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.ingestion import oddspapi, pipeline  # noqa: E402
from prop_alpha.ingestion.transform import PlayerMatchRecord, parse_fixture_payload  # noqa: E402

PILOT_DIR: Final[Path] = ROOT / ".prop_alpha_state" / "pilot"
TACKLE_DEFINITIONS: Final[dict[str, Callable[[PlayerMatchRecord], int]]] = {
    "tackles_attempted": lambda p: p.tackles_attempted,
    "tackles_won": lambda p: p.tackles_won,
    "tackles_won + interceptions": lambda p: p.tackles_won + p.interceptions,
    "tackles_attempted + interceptions": lambda p: p.tackles_attempted + p.interceptions,
    "tackles_attempted + tackles_won": lambda p: p.tackles_attempted + p.tackles_won,
}
FOUL_DEFINITIONS: Final[dict[str, Callable[[PlayerMatchRecord], int]]] = {
    "fouls_committed": lambda p: p.fouls_committed,
    "fouls_committed + fouls_drawn": lambda p: p.fouls_committed + p.fouls_drawn,
}


def event_happened(step: str, count: int) -> bool:
    if step == "0":
        return count == 0
    if step == "Yes":
        return count >= 1
    return count >= int(step.rstrip("+"))


def main() -> int:
    fixture_id = int(sys.argv[1]) if len(sys.argv) > 1 else 19729059
    bookmaker = sys.argv[2] if len(sys.argv) > 2 else "bet365"
    compared: dict[str, Any] = json.loads((PILOT_DIR / f"compare_{fixture_id}_{bookmaker}.json").read_text(encoding="utf-8"))
    c = pipeline.build_vendor_client({**os.environ, "DATA_VENDOR_MAX_REQUESTS": "6", "DATA_VENDOR_QUOTA_RESERVE": "200"})
    try:
        state = c.get_json(f"fixtures/{fixture_id}?include=state")["data"].get("state", {}).get("developer_name")
        if state not in ("FT", "AET", "FT_PEN"):
            print(f"{compared['fixture']}: state {state}, not final; nothing to check yet")
            return 1
        parsed = parse_fixture_payload(c.get_fixture_feed(str(fixture_id)))
    finally:
        c.close()
    by_name = {oddspapi.normalise(p.name): p for p in parsed.players}
    print(f"{compared['fixture']}: final {parsed.fixture.home_score}-{parsed.fixture.away_score}; "
          f"{len(parsed.players)} players in the Sportmonks feed", flush=True)

    rows: list[dict[str, Any]] = []
    missing: set[str] = set()
    for r in compared["rows"]:
        player = by_name.get(oddspapi.normalise(r["player"]))
        if player is None:
            missing.add(r["player"])
        else:
            rows.append({**r, "rec": player})
    names = sorted({r["player"] for r in rows})
    print(f"{len(rows)} quotes for {len(names)} compared players matched to the feed"
          + (f"; not in the feed: {sorted(missing)}" if missing else ""))

    print("\nplayer                      min  tack_att tack_won intercept  fouls_com fouls_drawn")
    for name in names:
        p = next(r["rec"] for r in rows if r["player"] == name)
        print(f"  {name:26s} {p.minutes_played:3d}  {p.tackles_attempted:7d} {p.tackles_won:8d} {p.interceptions:9d}  "
              f"{p.fouls_committed:9d} {p.fouls_drawn:10d}")

    def score(market: str, definitions: dict[str, Callable[[PlayerMatchRecord], int]]) -> None:
        quotes = [r for r in rows if r["market"] == market and r["rec"].minutes_played > 0]
        if not quotes:
            return
        implied = np.array([r["implied"] for r in quotes])
        print(f"\n{market.upper()}: {len(quotes)} quotes (players who played). mean implied probability {implied.mean():.3f}; "
              "share of events that happened under each definition")
        for label, fn in definitions.items():
            realised = np.array([event_happened(r["step"], fn(r["rec"])) for r in quotes], dtype=float)
            # Brier-style distance between each quote's implied probability and the outcome: smaller is closer.
            print(f"   {label:36s} realised {realised.mean():.3f}   mean |implied - outcome| {np.abs(implied - realised).mean():.3f}"
                  f"   model-probability Brier {np.mean((np.array([r['model'] for r in quotes]) - realised) ** 2):.3f}")
        print("   (a margin makes implied exceed the true rate by about 5-10%; the closer definition has the smaller distance)")

    score("tackles", TACKLE_DEFINITIONS)
    score("fouls", FOUL_DEFINITIONS)
    out = PILOT_DIR / f"counts_{fixture_id}.json"
    out.write_text(json.dumps({
        "fixture": compared["fixture"], "players": {
            n: {"minutes": p.minutes_played, "tackles_attempted": p.tackles_attempted, "tackles_won": p.tackles_won,
                "interceptions": p.interceptions, "fouls_committed": p.fouls_committed, "fouls_drawn": p.fouls_drawn}
            for n in names for p in [next(r["rec"] for r in rows if r["player"] == n)]}}, indent=1), encoding="utf-8")
    print(f"\ncounts saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
