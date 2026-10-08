"""Championship pilot: parse archived OddsPapi replies for the player tackles/fouls "k+" ladders and summarise them.

    .venv\\Scripts\\python.exe scripts\\pilot_ladder_summary.py [archive.json.gz ...]

Reads archives from .prop_alpha_state/pilot/ (named <stamp>_<bookmaker>_<league>.json.gz; bet365 only when the name does
not say) unless files are given. The market catalogue is fetched once (one OddsPapi request) and cached in
.prop_alpha_state/pilot/markets.json. Read-only: nothing is priced, alerted or stored in the database.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.config import credential  # noqa: E402
from prop_alpha.ingestion import oddspapi  # noqa: E402

PILOT_DIR = ROOT / ".prop_alpha_state" / "pilot"


def catalogue_reply() -> list[dict[str, Any]]:
    cache = PILOT_DIR / "markets.json"
    if cache.is_file():
        loaded: list[dict[str, Any]] = json.loads(cache.read_text(encoding="utf-8"))
        return loaded
    reply = requests.get(f"{oddspapi.BASE_URL}/markets", params={"apiKey": credential("ODDSPAPI_KEY", os.environ), "sportId": "10"},
                         timeout=90)
    reply.raise_for_status()
    markets: list[dict[str, Any]] = reply.json()
    PILOT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(markets), encoding="utf-8")
    return markets


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]] or sorted(PILOT_DIR.glob("*.json.gz"))
    if not files:
        print("no archives in", PILOT_DIR)
        return 1
    catalogue = oddspapi.ladder_catalogue(catalogue_reply())
    for path in files:
        book = path.name.split("_")[1] if path.name.count("_") >= 2 else "bet365"
        body = json.load(gzip.open(path, "rt", encoding="utf-8"))
        prices = oddspapi.ladder_prices(body, catalogue, book)
        print(f"\n{path.name}: {len(body)} fixtures, {len(prices)} ladder prices from {book}")
        steps: dict[str, Counter[str]] = defaultdict(Counter)
        players: dict[tuple[str, str], set[str]] = defaultdict(set)
        for p in prices:
            steps[p.market][p.step] += 1
            players[(p.fixture.odds_fixture_id, p.market)].add(p.player_ref)
        for market, counter in sorted(steps.items()):
            per_fixture = [len(v) for (fid, m), v in players.items() if m == market]
            print(f"  {market}: ladder steps {dict(counter)}; players per fixture "
                  f"{min(per_fixture)}-{max(per_fixture)} over {len(per_fixture)} fixtures")
        sample = sorted(prices, key=lambda p: (p.fixture.odds_fixture_id, p.market, p.player_name, p.line, p.side))[:8]
        for p in sample:
            print(f"    {p.fixture.kickoff_at:%d %b %H:%M} {p.market:7s} {p.player_name:24s} {p.step:>3s} ({p.side} {p.line:g}) @ {p.price}"
                  + (f" limit {p.limit:g}" if p.limit else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
