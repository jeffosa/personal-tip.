"""Matchday bookmaker coverage probe (maintainer, 2026-10-21): which OddsPapi bookmakers quote player tackles or fouls for
League One, League Two and the English Championship.

Why: "no bookmaker quotes League One/Two tackles or fouls" rested on a probe of 2026-10-08 and one matchday of bet365 alone
(2026-10-10), and the English Championship joined the pilot with only bet365 and Betway asked. One odds request per
bookmaker (the three tournaments in one request), read with the cached market catalogues (over/under lines and "k+"
ladders); no other request is metered. Every raw reply is archived under .prop_alpha_state/odds_probe/<stamp>/ with the
report; --send also sends the report to Telegram. It refuses to run when the OddsPapi quota would fall below
scan.ODDS_QUOTA_RESERVE. One bookmaker's failure is recorded in the report and the others are still asked.

    .venv\\Scripts\\python.exe scripts\\odds_probe.py [--send] [--books bet365,ladbrokes,...]
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts import scan  # noqa: E402
from prop_alpha.alerts.ladder_scan import LADDER_CATALOGUE_FILE, load_ladder_catalogue  # noqa: E402
from prop_alpha.ingestion import oddspapi  # noqa: E402

# OddsPapi tournament ids: League One, League Two, the English Championship.
TOURNAMENTS: Final[Mapping[int, str]] = {24: "League One", 25: "League Two", 18: "Championship"}
# Every bookmaker OddsPapi answered for, or that listed Championship props, in the probes of 2026-10-03 to 2026-10-08, and
# three more UK books (unibet, betvictor, boylesports) whose OddsPapi names are unconfirmed: a name OddsPapi does not
# know is answered like "no fixtures" (its 400 wording does not tell the two apart).
DEFAULT_BOOKS: Final[tuple[str, ...]] = (
    "bet365", "skybet", "williamhill", "ladbrokes", "coral", "paddypower", "betfair", "betfred", "betway", "bwin",
    "pinnacle", "livescorebet", "unibet", "betvictor", "boylesports",
)
PROP_MARKETS: Final[frozenset[str]] = frozenset({"tackles", "fouls"})
STATE: Final[Path] = ROOT / ".prop_alpha_state"


def coverage(body: Any, book: str, lines: Mapping[int, Any], ladders: Mapping[int, Any]) -> dict[str, dict[str, Any]]:
    """Pure: per league in TOURNAMENTS, the fixtures listed, the fixtures with any tackles or fouls price, and the prices per
    market (over/under lines and ladder steps together) in one odds reply of `book`."""
    league_of = {str(f.get("fixtureId")): TOURNAMENTS.get(int(f.get("tournamentId") or 0), "other")
                 for f in body if isinstance(f, dict)}
    result: dict[str, dict[str, Any]] = {name: {"fixtures": 0, "with_props": set(), "prices": defaultdict(int)}
                                         for name in [*TOURNAMENTS.values(), "other"]}
    for league in league_of.values():
        result[league]["fixtures"] += 1
    found = [(p.fixture.odds_fixture_id, p.market) for p in oddspapi.prop_prices(body, lines, book)]
    found += [(p.fixture.odds_fixture_id, p.market) for p in oddspapi.ladder_prices(body, ladders, book)]
    for fixture_id, market in found:
        if market in PROP_MARKETS:
            league = league_of.get(fixture_id, "other")
            result[league]["with_props"].add(fixture_id)
            result[league]["prices"][market] += 1
    return {league: {"fixtures": v["fixtures"], "with_props": len(v["with_props"]), "prices": dict(v["prices"])}
            for league, v in result.items() if league != "other" or v["fixtures"]}


def report(at: datetime, results: Mapping[str, Any]) -> str:
    """Pure: the probe's report, one line per bookmaker and a verdict per league."""
    lines = [f"OddsPapi bookmaker probe {at:%a %d %b %H:%M} UTC: player tackles and fouls for "
             f"{', '.join(TOURNAMENTS.values())} (one request per bookmaker)"]
    quoting: dict[str, list[str]] = defaultdict(list)
    for book, outcome in results.items():
        if isinstance(outcome, str):
            lines.append(f"{book}: {outcome}")
            continue
        parts = []
        for league in TOURNAMENTS.values():
            c = outcome.get(league, {"fixtures": 0, "with_props": 0, "prices": {}})
            prices = ", ".join(f"{m} {n}" for m, n in sorted(c["prices"].items())) or "no tackles/fouls"
            parts.append(f"{league} {c['with_props']}/{c['fixtures']} fixtures ({prices})")
            if c["with_props"]:
                quoting[league].append(book)
        lines.append(f"{book}: " + "; ".join(parts))
    lines.append("")
    for league in TOURNAMENTS.values():
        books = quoting.get(league, [])
        lines.append(f"{league}: " + (f"tackles/fouls quoted by {', '.join(books)}" if books
                                      else "no bookmaker quoted player tackles or fouls"))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--send", action="store_true", help="also send the report to Telegram")
    parser.add_argument("--books", default=",".join(DEFAULT_BOOKS), help="comma-separated OddsPapi bookmaker names")
    args = parser.parse_args(argv)
    books = [b.strip() for b in args.books.split(",") if b.strip()]
    from prop_alpha.ingestion.pipeline import build_odds_client

    now = datetime.now(timezone.utc)
    out = STATE / "odds_probe" / f"{now:%Y%m%dT%H%M%SZ}"
    lines_catalogue = scan.load_catalogue(STATE / scan.ODDS_CACHE_DIR / scan.CATALOGUE_FILE)
    ladder_catalogue = load_ladder_catalogue(STATE / scan.ODDS_CACHE_DIR / LADDER_CATALOGUE_FILE)
    client = build_odds_client(dict(os.environ))
    results: dict[str, Any] = {}
    try:
        subscriptions = client.get_json_value(oddspapi.ACCOUNT_PATH).get("subscriptions") or []
        left = min(int(s["request_limit"]) - int(s["request_count"]) for s in subscriptions)
        if left - len(books) < scan.ODDS_QUOTA_RESERVE:
            raise SystemExit(f"refused: {left} OddsPapi requests left, {len(books)} needed, reserve {scan.ODDS_QUOTA_RESERVE}")
        out.mkdir(parents=True, exist_ok=True)
        for book in books:
            try:
                body = client.get_json_value(oddspapi.odds_path(book, sorted(TOURNAMENTS)), empty_on=oddspapi.NO_FIXTURES_REPLIES)
            except Exception as exc:  # one bookmaker's failure is recorded in the report; the others are still asked
                results[book] = f"FAILED ({type(exc).__name__}: {str(exc)[:120]})"
                continue
            if body is None:
                results[book] = "no fixtures (or a bookmaker name OddsPapi does not know)"
                continue
            with gzip.open(out / f"{book}.json.gz", "wt", encoding="utf-8") as handle:
                json.dump(body, handle)
            results[book] = coverage(body, book, lines_catalogue, ladder_catalogue)
    finally:
        client.close()
    text = report(now, results)
    (out / "report.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.send:
        from prop_alpha.alerts.telegram import TelegramNotifier, load_config

        TelegramNotifier(load_config()).send_message(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
