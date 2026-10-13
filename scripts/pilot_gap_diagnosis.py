"""Why the pilot model prices Brazil Serie A tackles at about half the bookmakers' level (maintainer, 2026-10-12: "pause
brazil tipping and diagnose the gap").

    .venv\\Scripts\\python.exe scripts\\pilot_gap_diagnosis.py [stamp]     # default stamp 20261011T073542Z

Offline and read-only: the archived bet365 and Betway replies of one pilot run (.prop_alpha_state/pilot/<stamp>_<book>_
pilot.json.gz), the cached OddsPapi catalogues and participant names, the model's logged probabilities
(pilot_predictions.jsonl) and the stored Brazil Serie A matches in prop_alpha_pilot (a read-only transaction). No vendor
request, nothing written.

Each "k+" ladder price is matched to a stored player (club by name words, player by normalised name). For every matched
player the script takes his own starts of 70+ minutes before the run and compares four candidate counts, all from
Sportmonks: TA (tackles_attempted, what the model prices), TW (tackles_won), TW+INT, TA+INT and TA+DP (plus dribbled_past:
challenges lost). Four views:

1. Level: the book's mean implied probability per step against how often each count reached that step.
2. Shape: the step-to-step ratio P(k+1)+ / P(k+), which a margin that scales every price alike does not change; the count
   whose ratios match the book's is the best candidate for what the book counts.
3. The eight tips of 2026-10-11, with each player's own TA history.
5. Model v book: the matched fixtures re-priced through the real pilot pricing (alerts/ladder_scan.price_pilot) on the
   pilot's count (plain TA again since 2026-10-13, ladder_scan.load_pilot_inputs and its k) and on the count of 2026-10-12
   only (TA+DP, k 17.312), each line's probability and expected profit against the book's price, and how many lines reach the
   screen's 4%.

Not identified by any of this: the book's actual margin (no "0" step is quoted beside "1+") and its data provider.
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

import numpy as np

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.ladder_scan import LADDER_CATALOGUE_FILE, PLAUSIBLE_EDGE_CAP, load_ladder_catalogue  # noqa: E402
from prop_alpha.ingestion import oddspapi  # noqa: E402

STATE: Final[Path] = ROOT / ".prop_alpha_state"
PILOT_DSN: Final[str] = "postgresql://postgres@localhost:5432/prop_alpha_pilot"
DEFAULT_STAMP: Final[str] = "20261011T073542Z"
BOOKS: Final[tuple[str, ...]] = ("bet365", "betway")
LEAGUE: Final[str] = "Brazil Serie A"
MIN_MINUTES: Final[int] = 70
MAX_STARTS: Final[int] = 20
MIN_STARTS: Final[int] = 5
STEPS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5)
Row = tuple[int, int, int, int, int]  # tackles_attempted, tackles_won, interceptions, minutes, dribbled_past
COUNTS: Final[Mapping[str, Callable[[Row], int]]] = {
    "TA": lambda r: r[0], "TW": lambda r: r[1], "TW+INT": lambda r: r[1] + r[2], "TA+INT": lambda r: r[0] + r[2],
    "TA+DP": lambda r: r[0] + r[4],
}

HISTORY_SQL: Final[str] = """
SELECT s.player_id, p.full_name, t.name, f.kickoff_at, s.tackles_attempted, s.tackles_won, s.interceptions,
       s.minutes_played, s.started, s.dribbled_past
FROM prop_alpha.player_match_stats s
JOIN prop_alpha.fixtures f ON f.fixture_id = s.fixture_id
JOIN prop_alpha.leagues l ON l.league_id = f.league_id
JOIN prop_alpha.players p ON p.player_id = s.player_id
JOIN prop_alpha.teams t ON t.team_id = s.team_id
WHERE l.name = %s AND f.status = 'final' AND f.kickoff_at < %s
ORDER BY f.kickoff_at
"""


def person(name: str) -> str:
    """'Melo, Arthur' -> 'arthur melo'; a single name stays as it is."""
    last, comma, first = name.partition(",")
    return oddspapi.normalise(f"{first} {last}" if comma else name)


def match_player(name: str, candidates: Mapping[int, str]) -> int | None:
    """A unique exact normalised name, else a unique surname with the same first initial, else a unique surname."""
    wanted = person(name)
    exact = [pid for pid, full in candidates.items() if oddspapi.normalise(full) == wanted]
    if len(exact) == 1:
        return exact[0]
    words = wanted.split()
    if not words:
        return None
    surname = [pid for pid, full in candidates.items() if oddspapi.normalise(full).split()[-1:] == words[-1:]]
    initial = [pid for pid in surname if len(words) > 1 and oddspapi.normalise(candidates[pid])[:1] == words[0][:1]]
    for group in (initial, surname):
        if len(group) == 1:
            return group[0]
    mononym = [pid for pid, full in candidates.items() if len(words) == 1 and words[0] in oddspapi.normalise(full).split()]
    return mononym[0] if len(mononym) == 1 else None


def load_history(at: datetime) -> tuple[dict[int, list[Row]], dict[str, dict[int, str]], list[Row]]:
    """(player -> his 70+ minute starts, newest last; club name -> {player: name} for players seen there in the last
    year; every 70+ minute start of the season to date) from the pilot database, read-only."""
    import psycopg2

    conn = psycopg2.connect(PILOT_DSN)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(HISTORY_SQL, (LEAGUE, at))
            rows = cur.fetchall()
    finally:
        conn.close()
    starts: dict[int, list[Row]] = defaultdict(list)
    clubs: dict[str, dict[int, str]] = defaultdict(dict)
    season: list[Row] = []
    for pid, name, club, kickoff, ta, tw, ints, minutes, started, dribbled in rows:
        if (at - kickoff).days <= 365:
            clubs[club][pid] = name
        if started and minutes >= MIN_MINUTES:
            row = (int(ta), int(tw), int(ints), int(minutes), int(dribbled or 0))
            starts[pid].append(row)
            if kickoff.year == at.year:
                season.append(row)
    return starts, clubs, season


def club_for(odds_name: str, clubs: Sequence[str]) -> str | None:
    tokens = oddspapi.team_tokens(odds_name)
    found = [club for club in clubs if tokens & oddspapi.team_tokens(club)]
    return found[0] if len(found) == 1 else None


def at_least(rows: Sequence[Row], count: Callable[[Row], int], k: int) -> float:
    return float(np.mean([count(r) >= k for r in rows]))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    stamp = args[0] if args else DEFAULT_STAMP
    at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    catalogue = load_ladder_catalogue(STATE / "oddspapi" / LADDER_CATALOGUE_FILE)
    names: dict[str, str] = json.loads((STATE / "oddspapi" / "participants.json").read_text(encoding="utf-8"))
    starts, clubs, season = load_history(at)
    print(f"Run {stamp}; {LEAGUE} starts of {MIN_MINUTES}+ minutes stored before it: {sum(map(len, starts.values())):,} "
          f"({len(season):,} in {at.year})")

    # (book, player) -> {k: implied probability of k+}
    ladders: dict[tuple[str, int], dict[int, float]] = defaultdict(dict)
    matched_fixtures: dict[str, tuple[str, str, datetime]] = {}
    fixture_of: dict[tuple[str, int], str] = {}
    unmatched_players = 0
    for book in BOOKS:
        body: Any = json.loads(gzip.open(STATE / "pilot" / f"{stamp}_{book}_pilot.json.gz", "rt", encoding="utf-8").read())
        for price in oddspapi.ladder_prices(body, catalogue, book):
            if price.market != "tackles" or price.side != "over":
                continue
            fixture = price.fixture
            home, away = (club_for(names.get(str(team), ""), list(clubs)) for team in (fixture.home_id, fixture.away_id))
            if home is None or away is None:
                continue  # another league in the same reply, or a club name that does not match one way
            matched_fixtures[fixture.odds_fixture_id] = (home, away, fixture.kickoff_at)
            pid = match_player(price.player_name, {**clubs[home], **clubs[away]})
            if pid is None:
                unmatched_players += 1
                continue
            ladders[(book, pid)][int(price.step.rstrip("+"))] = 1.0 / price.price
            fixture_of[(book, pid)] = fixture.odds_fixture_id
    usable = {key: steps for key, steps in ladders.items() if len(starts.get(key[1], [])) >= MIN_STARTS}
    print(f"Brazil fixtures matched: {len(matched_fixtures)}; ladders matched to a stored player: {len(ladders)} "
          f"({unmatched_players} prices unmatched); with {MIN_STARTS}+ stored starts: {len(usable)}")

    print("\n1. LEVEL: book's mean implied P(k+) v how often each count reached k in the same players' own last "
          f"{MAX_STARTS} starts of {MIN_MINUTES}+ min")
    print("   (league column: every 70+ minute start of the season, all players)")
    header = f"   {'book':7} {'k+':>3} {'n':>4} {'implied':>8} " + " ".join(f"{name:>8}" for name in COUNTS)
    print(header + f" {'TA league':>9}")
    for book in BOOKS:
        for k in STEPS:
            pairs = [(steps[k], starts[pid][-MAX_STARTS:]) for (b, pid), steps in usable.items() if b == book and k in steps]
            if len(pairs) < 10:
                continue
            implied = float(np.mean([p for p, _ in pairs]))
            freq = {name: float(np.mean([at_least(rows, fn, k) for _, rows in pairs])) for name, fn in COUNTS.items()}
            print(f"   {book:7} {k:>2}+ {len(pairs):>4} {implied:8.3f} " + " ".join(f"{freq[n]:8.3f}" for n in COUNTS)
                  + f" {at_least(season, COUNTS['TA'], k):9.3f}")

    print("\n2. SHAPE (margin-free): mean P((k+1)+) / P(k+) for the same players; the count whose ratios track the book's")
    print("   is the best candidate for what the book counts")
    print(f"   {'book':7} {'step':>6} {'n':>4} {'book':>7} " + " ".join(f"{name:>8}" for name in COUNTS))
    for book in BOOKS:
        for k in STEPS[:-1]:
            pairs = [(steps[k + 1] / steps[k], starts[pid][-MAX_STARTS:]) for (b, pid), steps in usable.items()
                     if b == book and k in steps and k + 1 in steps]
            if len(pairs) < 10:
                continue
            ratios: dict[str, float] = {}
            for name, fn in COUNTS.items():
                values = [at_least(rows, fn, k + 1) / at_least(rows, fn, k) for _, rows in pairs if at_least(rows, fn, k) > 0]
                ratios[name] = float(np.mean(values)) if values else float("nan")
            print(f"   {book:7} {k}->{k + 1:<3} {len(pairs):>4} {np.mean([r for r, _ in pairs]):7.3f} "
                  + " ".join(f"{ratios[n]:8.3f}" for n in COUNTS))

    print("\n3. MEAN COUNT PER START the book implies (Poisson fitted to each ladder's shape, margin-free) v each count's mean")
    for book in BOOKS:
        implied_means: list[float] = []
        own: dict[str, list[float]] = {name: [] for name in COUNTS}
        for (b, pid), steps in usable.items():
            if b != book or not {1, 2, 3} <= set(steps):
                continue
            lam = poisson_mean_from_shape(steps)
            if lam is None:
                continue
            implied_means.append(lam)
            for name, fn in COUNTS.items():
                own[name].append(float(np.mean([fn(r) for r in starts[pid][-MAX_STARTS:]])))
        if implied_means:
            print(f"   {book}: n {len(implied_means)}, book-implied mean {np.median(implied_means):.2f} (median); own means "
                  + ", ".join(f"{name} {np.median(values):.2f}" for name, values in own.items())
                  + "; correlation with the book: "
                  + ", ".join(f"{name} {np.corrcoef(implied_means, values)[0, 1]:.2f}" for name, values in own.items()))

    print("\n4. THE EIGHT TIPS OF 2026-10-11 (bet365 ladders, as tipped)")
    tips = [json.loads(line) for line in (STATE / "pilot_paper_bets.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "placed"]
    for tip in tips:
        rows = starts.get(int(tip["player_id"]), [])[-MAX_STARTS:]
        k = int(float(tip["line"]) + 0.5)
        print(f"   {tip['player']}: {k}+ tackles @ {tip['odds']} (implied {1 / float(tip['odds']):.2f}); his last "
              f"{len(rows)} starts of {MIN_MINUTES}+ min: TA {[r[0] for r in rows]} -> TA {k}+ in "
              f"{at_least(rows, COUNTS['TA'], k):.0%}" if rows else f"   {tip['player']}: no stored starts")
    model_v_book(matched_fixtures, ladders, fixture_of, clubs)
    return 0


TA_DP_TACKLES_K: Final[float] = 17.312  # Brazil's tackles k on tackles plus challenges lost (2026-10-12)
SCREEN_EV: Final[float] = 0.04


def model_v_book(fixtures: Mapping[str, tuple[str, str, datetime]], ladders: Mapping[tuple[str, int], Mapping[int, float]],
                 fixture_of: Mapping[tuple[str, int], str], clubs: Mapping[str, Mapping[int, str]]) -> None:
    """Section 5: re-price the matched fixtures on both counts (read-only) and compare with the books' ladders."""
    import psycopg2

    from prop_alpha.alerts import ladder_scan, scan
    from prop_alpha.modeling.engine import QuantEngine
    from prop_alpha.scanner.slate import UpcomingFixture

    from prop_alpha.alerts import pilot_ledger

    wider = {sql: pilot_ledger.recount(sql, pilot_ledger.TACKLES_PLUS_CHALLENGES_LOST_SQL) for sql in ladder_scan.PILOT_QUERIES}
    wider_engines = (QuantEngine(game_state_k=ladder_scan.DEFAULT_GAME_STATE_K),
                     {"tackles": QuantEngine(game_state_k=TA_DP_TACKLES_K),
                      "fouls": QuantEngine(game_state_k=ladder_scan.LEAGUE_GAME_STATE_K[LEAGUE]["fouls"])})

    def load_wider(cur: Any, fixture: UpcomingFixture) -> scan.SlateInputs:
        return scan.load_slate_inputs(ladder_scan.PilotCursor(cur, wider), fixture)

    variants = {"TA (pilot)": (ladder_scan.load_pilot_inputs, ladder_scan.pilot_engines(LEAGUE)),
                "TA+DP (12 Oct)": (load_wider, wider_engines)}
    # variant -> (odds fixture, player, k) -> (p_win, p_lose)
    priced: dict[str, dict[tuple[str, int, int], tuple[float, float]]] = {label: {} for label in variants}
    conn = psycopg2.connect(PILOT_DSN)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT t.name, t.vendor_team_uid FROM prop_alpha.teams t JOIN prop_alpha.leagues l USING (league_id) "
                        "WHERE l.name = %s", (LEAGUE,))
            uid = {str(name): str(team) for name, team in cur.fetchall()}
            for n, (odds_id, (home, away, kickoff)) in enumerate(sorted(fixtures.items())):
                fixture = UpcomingFixture(-(n + 1), LEAGUE, uid[home], uid[away], home, away, kickoff)
                for label, (loader, engines) in variants.items():
                    projections, _ = ladder_scan.price_pilot(fixture, loader(cur, fixture), *engines)
                    for (_, pid, market), projection in projections.items():
                        if market == "tackles":
                            for k in STEPS:
                                side = projection.side_probabilities(k - 0.5, "over")
                                priced[label][(odds_id, pid, k)] = (side.p_win, side.p_lose)
    finally:
        conn.close()
    names = {pid: name for squad in clubs.values() for pid, name in squad.items()}
    print("\n5. MODEL V BOOK: the matched fixtures re-priced by the pilot pricing; model P(k+ | plays) v the book's implied "
          "P(k+),\n   expected profit per unit at the book's price (void if he does not play), lines at or above the screen's "
          f"{SCREEN_EV:.0%}")
    for label in variants:
        for book in BOOKS:
            rows = []
            for (b, pid), steps in ladders.items():
                if b != book:
                    continue
                for k, implied in steps.items():
                    found = priced[label].get((fixture_of[(b, pid)], pid, k))
                    if found is not None and sum(found) > 0:
                        p_win, p_lose = found
                        rows.append((p_win / (p_win + p_lose), implied, p_win * (1 / implied - 1) - p_lose, pid, k))
            if not rows:
                continue
            model_p, book_p = np.array([r[0] for r in rows]), np.array([r[1] for r in rows])
            ev = np.array([r[2] for r in rows])
            edge = model_p / book_p - 1.0  # the guard's measure: the model's return if he plays
            print(f"   {label:12} {book:7} lines {len(rows):4}: model {model_p.mean():.3f} v implied {book_p.mean():.3f} "
                  f"(ratio {book_p.mean() / model_p.mean():.2f}), correlation {np.corrcoef(model_p, book_p)[0, 1]:.2f}, "
                  f"mean EV {ev.mean():+.1%}, lines >= {SCREEN_EV:.0%}: {int((ev >= SCREEN_EV).sum())}, of which within the "
                  f"{PLAUSIBLE_EDGE_CAP:.0%} guard: {int(((ev >= SCREEN_EV) & (edge <= PLAUSIBLE_EDGE_CAP)).sum())}")
            if label == "TA (pilot)":
                within = [r for r in rows if r[2] >= SCREEN_EV and r[0] / r[1] - 1.0 <= PLAUSIBLE_EDGE_CAP]
                for p_model, p_book, value, pid, k in sorted(within, key=lambda r: -r[2])[:8]:
                    print(f"      within the guard: {names.get(pid, pid)} {k}+: model {p_model:.2f} v implied {p_book:.2f}, "
                          f"EV {value:+.1%}")


def poisson_mean_from_shape(steps: Mapping[int, float]) -> float | None:
    """The Poisson mean whose P(2+)/P(1+) and P(3+)/P(2+) best match the ladder's (least squares on a grid); a margin that
    scales every step alike cancels out of the ratios."""
    target = [steps[2] / steps[1], steps[3] / steps[2]]
    grid = np.linspace(0.05, 12.0, 1200)
    from scipy.stats import poisson

    tail = [poisson.sf(k - 1, grid) for k in (1, 2, 3)]
    fit = (tail[1] / tail[0] - target[0]) ** 2 + (tail[2] / tail[1] - target[1]) ** 2
    best = int(np.argmin(fit))
    return None if best in (0, len(grid) - 1) else float(grid[best])


if __name__ == "__main__":
    sys.exit(main())
