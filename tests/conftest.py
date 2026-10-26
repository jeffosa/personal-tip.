from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

import pytest

HOME_UID = "T-HOME"
AWAY_UID = "T-AWAY"


def make_fixture_payload(uid: str = "FX-1", n_players: int = 2) -> dict[str, Any]:
    """A valid vendor stats payload: a 2-1 home win with possession and n players per side."""
    players: list[dict[str, Any]] = []
    for side, team_uid in (("h", HOME_UID), ("a", AWAY_UID)):
        for i in range(n_players):
            players.append({
                "uid": f"{uid}-{side}{i}",
                "name": f"Player {side}{i}",
                "position": "DM",
                "team_uid": team_uid,
                "started": i == 0,
                "stats": {
                    "minutes": 90 if i == 0 else 25,
                    "tackles_attempted": 4,
                    "tackles_won": 2,
                    "interceptions": 1,
                    "dribbled_past": 1,
                    "duels_total": 8,
                    "duels_won": 5,
                    "fouls_committed": 2,
                    "fouls_drawn": 1,
                    "yellow_cards": 0,
                    "red_cards": 0,
                    "zone14_entries": 0,
                    "halfspace_entries": 1,
                    "long_balls_targeted": 2,
                },
            })
    return {
        "fixture": {
            "uid": uid,
            "home_team_uid": HOME_UID,
            "away_team_uid": AWAY_UID,
            "kickoff_at": "2026-01-10T15:00:00+00:00",
            "status": "final",
            "home_score": 2,
            "away_score": 1,
            "matchday": 21,
        },
        "referee": {"uid": "R-1", "name": "Ref One"},
        "teams": {HOME_UID: {"possession_pct": 58.0}, AWAY_UID: {"possession_pct": 42.0}},
        "goals": [
            {"minute": 20, "team_uid": HOME_UID},
            {"minute": 55, "team_uid": AWAY_UID},
            {"minute": 80, "team_uid": HOME_UID},
        ],
        "players": players,
    }


def make_lineup_payload(uid: str = "FX-1", n_bench: int = 3) -> dict[str, Any]:
    lineups = {
        team_uid: {
            "starters": [f"{uid}-{side}s{i}" for i in range(11)],
            "bench": [f"{uid}-{side}b{i}" for i in range(n_bench)],
        }
        for side, team_uid in (("h", HOME_UID), ("a", AWAY_UID))
    }
    return {"fixture": {"uid": uid, "home_team_uid": HOME_UID, "away_team_uid": AWAY_UID}, "lineups": lineups}


@pytest.fixture
def fixture_payload() -> Callable[..., dict[str, Any]]:
    def factory(uid: str = "FX-1", n_players: int = 2) -> dict[str, Any]:
        return copy.deepcopy(make_fixture_payload(uid, n_players))

    return factory


# Variables that carry credentials or the bankroll currency; a test that needs one sets it itself.
CREDENTIAL_VARIABLES = ("SPORTMONKS_API_TOKEN", "THESTATSAPI_KEY", "ODDSPAPI_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                        "PROP_ALPHA_CURRENCY", "DATA_VENDOR", "DATA_VENDOR_API_KEY", "PROP_ALPHA_DB_DSN",
                        "PROP_ALPHA_PILOT_DSN")


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs as on a clean CI runner: the maintainer's `.env` and credential variables are invisible.
    (2026-10-05: a test passed locally only because it read SPORTMONKS_API_TOKEN from the real `.env`, and failed
    the GitHub release gate.) A test that passes its own env_file still reads that file."""
    from prop_alpha import config

    real_read = config.read_env_file
    real_file = config.DEFAULT_ENV_FILE.resolve()

    def read_env_file(env_file: Path = config.DEFAULT_ENV_FILE) -> str | None:
        return None if Path(env_file).resolve() == real_file else real_read(env_file)

    monkeypatch.setattr(config, "read_env_file", read_env_file)
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_results_heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The League One/Two scan trusts `.prop_alpha_state/results_last_ok.json` (ingestion/results.py): no test may write
    or read the real one (2026-10-26: a stubbed results run in a test wrote `listed: 0` there, which would have made the
    next live scan refuse)."""
    from prop_alpha.ingestion import results

    monkeypatch.setattr(results, "RESULTS_OK_FILE", tmp_path / "results_last_ok.json")
