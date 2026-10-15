"""scripts/pilot_settle_by_name.py (2026-10-15): pilot paper bets recorded on Sportmonks ids are settled from TheStatsAPI's
statistics by club and player name. A bet matched to anything but exactly one match and one player stays open: a name that
does not match is never graded void."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from prop_alpha.alerts import ledger
from prop_alpha.alerts.replies import PlacedBet

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pilot_settle_by_name", ROOT / "scripts" / "pilot_settle_by_name.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bet(bet_id: int, player: str, line: float = 0.5) -> PlacedBet:
    return PlacedBet(bet_id=bet_id, update_id=bet_id, message_id=0, number=1, kind="alert", fixture_id=19621785,
                     player_id=100 + bet_id, market="tackles", line=line, side="over", odds=1.41, stake=4.0, book="bet365",
                     min_odds=1.3, match="Bragantino v Cruzeiro", player=player, placed_at=datetime(2026, 10, 12, 19, 44,
                                                                                                  tzinfo=timezone.utc))


def _tip(bet_id: int, kickoff: datetime = KICKOFF) -> dict[str, Any]:
    return {"bet_id": bet_id, "fixture_id": 19621785, "league": "Brazil Serie A", "home_team_uid": "1", "away_team_uid": "2",
            "home_name": "Bragantino", "away_name": "Cruzeiro", "kickoff_at": kickoff.isoformat()}


CANDIDATES = [
    ("mt_028974343", "final", KICKOFF, "Red Bull Bragantino", "Cruzeiro", "Wallace Yan", 65, 1, 0),
    ("mt_028974343", "final", KICKOFF, "Red Bull Bragantino", "Cruzeiro", "Fernando", 90, 0, 1),
    ("mt_028974343", "final", KICKOFF, "Red Bull Bragantino", "Cruzeiro", "Bruno Silva", 90, 2, 1),
    ("mt_028974343", "final", KICKOFF, "Red Bull Bragantino", "Cruzeiro", "Bruno  Silva", 10, 0, 0),
    ("mt_999999999", "final", KICKOFF, "Palmeiras", "Corinthians", "Wallace Yan", 90, 5, 0),
]


def test_a_bet_is_settled_from_the_one_match_and_player_with_its_name() -> None:
    script = _module()
    bets = [_bet(10, "Wallace Yan (Bragantino)"), _bet(1, "Fernando (Bragantino)", line=1.5)]
    rows, uids, problems = script.match_rows(bets, {b.bet_id: _tip(b.bet_id) for b in bets}, CANDIDATES)
    assert problems == [] and uids == {10: "mt_028974343", 1: "mt_028974343"}
    assert rows[(19621785, 110)] == ("final", 65, 1, 0)  # not the other match's Wallace Yan at the same kickoff
    results = {bet.bet_id: s.result for bet, s in ledger.settle_open_bets(bets, rows, NOW)}
    assert results == {10: "won", 1: "lost"}


def test_an_unmatched_or_ambiguous_bet_stays_open_and_is_never_voided() -> None:
    script = _module()
    bets = [_bet(2, "Someone Else (Bragantino)"), _bet(3, "Bruno Silva (Bragantino)"), _bet(4, "Fernando (Bragantino)"),
            _bet(5, "Fernando (Bragantino)")]
    tips = {2: _tip(2), 3: _tip(3), 4: _tip(4, kickoff=datetime(2026, 10, 14, tzinfo=timezone.utc))}
    rows, _, problems = script.match_rows(bets, tips, CANDIDATES)
    assert rows == {} and ledger.settle_open_bets(bets, rows, NOW) == []
    assert [line.split(":")[0] for line in problems] == ["#2", "#3", "#4", "#5"]
    assert "0 players named 'Someone Else'" in problems[0] and "2 players named 'Bruno Silva'" in problems[1]
    assert "0 matches" in problems[2] and "no tip record" in problems[3]


def test_the_player_name_drops_only_the_club_in_brackets() -> None:
    script = _module()
    assert script.player_name(_bet(1, "Léo Pereira (Flamengo)")) == "Léo Pereira"
    assert script.player_name(_bet(1, "Marcinho")) == "Marcinho"
