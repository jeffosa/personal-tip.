"""Generate a fully synthetic League-One-like world with KNOWN true probabilities (no vendor data at all).

    .venv\\Scripts\\python.exe -u scripts\\make_synthetic_world.py [--seed 7] [--out synthetic_data]

Two seasons of a 24-team double round robin (2 x 552 = 1,104 fixtures: 2024/25 and 2025/26), invented teams, squads,
referees and players, with per-match tackles and fouls drawn from a stated data-generating process:
  - each player has a latent rate per 90 (position mean x lognormal player skill x team effect);
  - season 2 levels differ from season 1 (tackles x0.96, fouls x1.05, the drift seen in the real data);
  - each match has a shared game-state factor G ~ Gamma(k, k) (tackles k=8, fouls k=40: the model assumes 6 and
    1000, so the world is deliberately not the model's own);
  - fouls are scaled by the referee's strictness (lognormal sd 0.12); the scan prices with a neutral referee;
  - squads rotate: first-choice players miss a match 12% of the time, three substitutes are named, and starters are
    taken off at random (MID/ATT 25%, DEF 15%).
Evaluation fixtures are the ones kicking off on or after 2026-01-01 (the backtest's tune start; the pricing path
only prices from then). For each priced starter, market and line the file eval_odds.csv holds the TRUE probability
that the count goes over the line (exact: negative binomial over the game-state factor, mixed over minutes) and two
synthetic books' opening and closing quotes: the book sees the true probability moved by logit noise (SHARP: sd 0.10,
margin 5%; SOFT: sd 0.30, margin 8%; CONTROL: no noise, margin 6%), and its closing quote moves halfway to the truth.
Everything is invented; none of it is evidence about any real bookmaker, player or the model's real-world edge.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np
from scipy.stats import nbinom

ROOT = Path(__file__).resolve().parents[1]

TEAMS: Final[int] = 24
ROUNDS: Final[int] = 2 * (TEAMS - 1)
SEASON_STARTS: Final[tuple[datetime, ...]] = (datetime(2024, 8, 3, tzinfo=timezone.utc),
                                               datetime(2025, 8, 2, tzinfo=timezone.utc))
ROUND_SPACING_DAYS: Final[float] = 6.35
EVAL_FROM: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
FORMATION: Final[dict[str, int]] = {"GK": 1, "DEF": 4, "MID": 4, "ATT": 2}
RESERVES: Final[dict[str, int]] = {"GK": 1, "DEF": 3, "MID": 3, "ATT": 2}
RATE_PER_90: Final[dict[str, dict[str, float]]] = {
    "tackles": {"GK": 0.05, "DEF": 1.9, "MID": 2.1, "ATT": 1.0},
    "fouls": {"GK": 0.1, "DEF": 1.1, "MID": 1.2, "ATT": 1.0},
}
SEASON_LEVEL: Final[dict[str, tuple[float, float]]] = {"tackles": (1.0, 0.96), "fouls": (1.0, 1.05)}
GAME_STATE_K: Final[dict[str, float]] = {"tackles": 8.0, "fouls": 40.0}
PLAYER_SKILL_SD: Final[float] = 0.30
TEAM_EFFECT_SD: Final[float] = 0.08
REFEREE_SD: Final[float] = 0.12
FIRST_CHOICE_AVAILABLE: Final[float] = 0.88
SUB_OFF_PROBABILITY: Final[dict[str, float]] = {"GK": 0.0, "DEF": 0.15, "MID": 0.25, "ATT": 0.25}
SUB_OFF_MINUTES: Final[tuple[int, ...]] = (60, 67, 74, 81, 88)
LINES: Final[dict[str, tuple[float, ...]]] = {"tackles": (0.5, 1.5, 2.5), "fouls": (0.5, 1.5)}
BOOKS: Final[dict[str, tuple[float, float]]] = {"SHARP": (0.10, 1.05), "SOFT": (0.30, 1.08), "CONTROL": (0.0, 1.06)}
PROBABILITY_RANGE: Final[tuple[float, float]] = (0.05, 0.95)
KICKOFF_HOURS: Final[tuple[tuple[int, int], ...]] = ((15, 0),) * 8 + ((12, 30), (17, 30), (19, 45), (15, 0))


def round_robin(n: int) -> list[list[tuple[int, int]]]:
    """Circle-method schedule: n-1 rounds in which every team plays once; then the reverse fixtures."""
    teams = list(range(n))
    rounds: list[list[tuple[int, int]]] = []
    for r in range(n - 1):
        pairs = [(teams[i], teams[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) if (r + i) % 2 == 0 else (b, a) for i, (a, b) in enumerate(pairs)])
        teams = [teams[0], teams[-1], *teams[1:-1]]
    return rounds + [[(b, a) for a, b in pairs] for pairs in rounds]


def true_over_probability(rate90: float, k: float, line: float, plays_full: float) -> float:
    """P(count > line) for one starter: NB(k, k / (k + mu)) with mu = rate x minutes / 90, mixed over his minutes."""
    def sf(minutes: float) -> float:
        mu = rate90 * minutes / 90.0
        return float(nbinom.sf(math.floor(line), k, k / (k + mu)))

    return plays_full * sf(90.0) + (1.0 - plays_full) * float(np.mean([sf(m) for m in SUB_OFF_MINUTES]))


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="synthetic_data")
    args = parser.parse_args()
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    # ---- teams, squads, referees ------------------------------------------------------------------------------
    team_effect = {m: rng.normal(0.0, TEAM_EFFECT_SD, TEAMS) for m in RATE_PER_90}
    squads: list[dict[str, list[int]]] = []   # per team: position -> [first-choice ids..., reserve ids...]
    players: list[dict[str, Any]] = []
    for team in range(TEAMS):
        squad: dict[str, list[int]] = {}
        for position, firsts in FORMATION.items():
            ids = []
            for slot in range(firsts + RESERVES[position]):
                pid = len(players) + 1
                players.append({"player_id": pid, "name": f"Synthetic Player {pid:04d}", "position": position,
                                "team": team, "first_choice": slot < firsts,
                                **{f"skill_{m}": float(math.exp(rng.normal(0.0, PLAYER_SKILL_SD))) for m in RATE_PER_90}})
                ids.append(pid)
            squad[position] = ids
        squads.append(squad)
    by_id = {p["player_id"]: p for p in players}
    referees: list[dict[str, Any]] = [{"referee_id": i + 1, "name": f"Synthetic Referee {i + 1:02d}", "strictness": float(math.exp(rng.normal(0.0, REFEREE_SD)))}
                for i in range(20)]

    # ---- fixtures and match simulation ------------------------------------------------------------------------
    schedule = round_robin(TEAMS)
    fixtures: list[dict[str, Any]] = []
    stats: list[list[Any]] = []
    truth_rows: list[list[Any]] = []
    odds: list[list[Any]] = []
    fixture_id = 0
    for season, start in enumerate(SEASON_STARTS):
        for r, pairs in enumerate(schedule):
            day = start + timedelta(days=round(r * ROUND_SPACING_DAYS))
            order = rng.permutation(len(pairs))
            for slot, index in enumerate(order):
                home, away = pairs[int(index)]
                hour, minute = KICKOFF_HOURS[slot]
                kickoff = day.replace(hour=hour, minute=minute)
                fixture_id += 1
                referee = referees[int(rng.integers(len(referees)))]
                g = {m: float(rng.gamma(GAME_STATE_K[m], 1.0 / GAME_STATE_K[m])) for m in RATE_PER_90}
                fixtures.append({"fixture_id": fixture_id, "season": season, "kickoff": kickoff, "home": home, "away": away,
                                 "referee_id": referee["referee_id"]})
                for team in (home, away):
                    starters: list[int] = []
                    used: set[int] = set()
                    for position, firsts in FORMATION.items():
                        pool = squads[team][position]
                        for pid in pool[:firsts]:
                            if rng.random() < FIRST_CHOICE_AVAILABLE or position == "GK" and rng.random() < 0.97:
                                starters.append(pid)
                                used.add(pid)
                        while sum(1 for s in starters if by_id[s]["position"] == position) < firsts:
                            spare = [p for p in pool[firsts:] if p not in used]
                            starters.append(spare[0])
                            used.add(spare[0])
                    bench = [p for position in ("DEF", "MID", "ATT") for p in squads[team][position][FORMATION[position]:]
                             if p not in used]
                    subs = [bench[int(i)] for i in rng.choice(len(bench), size=3, replace=False)]
                    for pid, started in [(p, True) for p in starters] + [(p, False) for p in subs]:
                        player = by_id[pid]
                        position = player["position"]
                        full = 1.0 - SUB_OFF_PROBABILITY[position]
                        if started:
                            minutes = 90 if rng.random() < full else int(rng.choice(SUB_OFF_MINUTES))
                        else:
                            minutes = int(rng.choice((6, 14, 22, 30, 38))) if rng.random() < 0.7 else 0
                        rates: dict[str, float] = {}
                        counts: dict[str, int] = {}
                        for market, base in RATE_PER_90.items():
                            ref = referee["strictness"] if market == "fouls" else 1.0
                            rate = (base[position] * player[f"skill_{market}"] * math.exp(team_effect[market][team])
                                    * SEASON_LEVEL[market][season] * ref)
                            rates[market] = rate
                            counts[market] = int(rng.poisson(rate * minutes / 90.0 * g[market])) if minutes > 0 else 0
                        stats.append([pid, fixture_id, kickoff.isoformat(), team, int(team == home), int(started), minutes,
                                      counts["tackles"], counts["fouls"], position])
                        if started and kickoff >= EVAL_FROM and position != "GK":
                            truth_rows.append([fixture_id, pid, round(rates["tackles"], 5), round(rates["fouls"], 5)])
                            plays_full = 1.0 - SUB_OFF_PROBABILITY[position]
                            for market, lines in LINES.items():
                                for line in lines:
                                    p_true = true_over_probability(rates[market], GAME_STATE_K[market], line, plays_full)
                                    if not PROBABILITY_RANGE[0] < p_true < PROBABILITY_RANGE[1]:
                                        continue
                                    for book, (sd, margin) in BOOKS.items():
                                        noise = float(rng.normal(0.0, sd))
                                        base_logit = math.log(p_true / (1.0 - p_true))
                                        quotes = []
                                        for share in (1.0, 0.5):   # opening, then closing (halfway to the truth)
                                            q = 1.0 / (1.0 + math.exp(-(base_logit + share * noise)))
                                            q = min(max(q, 0.01), 0.99)
                                            quotes += [round(1.0 / (q * margin), 3), round(1.0 / ((1.0 - q) * margin), 3)]
                                        odds.append([fixture_id, pid, market, line, book, *quotes[:2], *quotes[2:],
                                                     round(p_true, 6)])

    # ---- write ------------------------------------------------------------------------------------------------
    files: dict[str, Any] = {}
    files["teams.csv"] = write_csv(out / "teams.csv", ["team_id", "name"], [[t + 1, f"Synthetic FC {t + 1:02d}"] for t in range(TEAMS)])
    files["referees.csv"] = write_csv(out / "referees.csv", ["referee_id", "name", "true_strictness"],
                                      [[r["referee_id"], r["name"], round(float(r["strictness"]), 5)] for r in referees])
    files["players.csv"] = write_csv(out / "players.csv", ["player_id", "name", "position", "team_id", "first_choice",
                                                          "true_skill_tackles", "true_skill_fouls"],
                                     [[p["player_id"], p["name"], p["position"], p["team"] + 1, int(p["first_choice"]),
                                       round(p["skill_tackles"], 5), round(p["skill_fouls"], 5)] for p in players])
    files["fixtures.csv"] = write_csv(
        out / "fixtures.csv", ["fixture_id", "season", "kickoff_at", "home_team_id", "away_team_id", "referee_id", "status"],
        [[f["fixture_id"], f["season"] + 1, f["kickoff"].isoformat(), f["home"] + 1, f["away"] + 1, f["referee_id"], "final"]
         for f in fixtures])
    files["player_match_stats.csv"] = write_csv(
        out / "player_match_stats.csv",
        ["player_id", "fixture_id", "kickoff_at", "team_id", "is_home", "started", "minutes_played", "tackles_attempted",
         "fouls_committed", "position"],
        [[s[0], s[1], s[2], s[3] + 1, *s[4:]] for s in stats])
    files["truth_rates.csv"] = write_csv(out / "truth_rates.csv", ["fixture_id", "player_id", "true_tackles_per90", "true_fouls_per90"],
                                         truth_rows)
    files["eval_odds.csv"] = write_csv(
        out / "eval_odds.csv",
        ["fixture_id", "player_id", "market", "line", "book", "open_over", "open_under", "close_over", "close_under", "true_p_over"],
        odds)
    evaluation = sum(1 for f in fixtures if f["kickoff"] >= EVAL_FROM)
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "seed": args.seed, "fixtures": len(fixtures),
                "evaluation_fixtures": evaluation, "players": len(players), "quotes": len(odds), "files": files,
                "generating_process": {"tackles_k": GAME_STATE_K["tackles"], "fouls_k": GAME_STATE_K["fouls"],
                                       "season_level": SEASON_LEVEL, "player_skill_sd": PLAYER_SKILL_SD,
                                       "referee_sd": REFEREE_SD, "books": BOOKS},
                "caveats": ["Everything here is invented. It can show whether the algorithm recovers a known truth and "
                            "how it behaves against books of known quality; it is no evidence about any real bookmaker, "
                            "player or the model's real-world edge."]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("fixtures", "evaluation_fixtures", "players", "quotes")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
