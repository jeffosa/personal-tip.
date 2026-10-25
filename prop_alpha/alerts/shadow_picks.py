"""Shadow picks: the real-price picks the scans withheld (alerts/market_contract.py), kept so their closing-line value can
be measured without sending a tip (closing-price review, 2026-10-25).

Since 2026-10-25 no real-price tip is sent until a bookmaker's settlement contract is verified, so the pilot's closing
capture (alerts/pilot_closing.py), which only looked for tipped selections, would never capture again, and the League
One/Two picks were not kept at all. Each real `--send` scan now appends every withheld directive here, before its summary
is sent: when, which match and kickoff, the selection, the bookmaker and price, and the model's probabilities. Nothing is
sent, staked or settled; a shadow pick is not a tip, not a paper bet and not Gate 3 evidence. Its one use is the question
"did the price the model liked shorten before kickoff?" (taken / closing - 1 at the same bookmaker), which needs no
settlement rule (scripts/value_evidence.py reports it).

I/O: the two append-only state files below (gitignored, in the daily backup's state folder).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from prop_alpha.alerts.ledger import STATE_DIR
from prop_alpha.scanner.market_scanner import StakingDirective
from prop_alpha.scanner.slate import UpcomingFixture

SHADOW_FILE: Final[Path] = STATE_DIR / "shadow_picks.jsonl"
PILOT_SHADOW_FILE: Final[Path] = STATE_DIR / "pilot_shadow_picks.jsonl"
SOURCES: Final[tuple[str, ...]] = ("league", "pilot")
REQUIRED: Final[tuple[str, ...]] = ("recorded_at", "source", "fixture_id", "league", "home_team_uid", "away_team_uid",
                                    "home_name", "away_name", "kickoff_at", "player_id", "player", "market", "line",
                                    "side", "book", "odds", "p_win", "p_lose")


def shadow_records(directives: Sequence[StakingDirective], fixtures: Mapping[int, UpcomingFixture],
                   names: Mapping[int, str], now: datetime, source: str) -> list[dict[str, Any]]:
    """Pure: one record per withheld directive, with the match fields pilot_ledger.fixture_of reads and the player's name
    (`names`, player id -> name), which the closing capture matches against the bookmaker's names. A directive whose
    fixture or player is unknown, or that is recorded at or after its kickoff, is refused: a shadow pick is a pre-kickoff
    price."""
    if source not in SOURCES:
        raise ValueError(f"unknown shadow-pick source {source!r}")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be a timezone-aware UTC datetime")
    records: list[dict[str, Any]] = []
    for directive in directives:
        fixture = fixtures.get(directive.fixture_id)
        if fixture is None:
            raise ValueError(f"withheld pick for unknown fixture {directive.fixture_id}")
        if fixture.kickoff_at <= now:
            raise ValueError(f"withheld pick for fixture {directive.fixture_id} recorded at or after kickoff")
        if directive.player_id not in names:
            raise ValueError(f"withheld pick for unknown player {directive.player_id}")
        records.append({
            "recorded_at": now.isoformat(), "source": source, "fixture_id": directive.fixture_id,
            "league": fixture.league, "home_team_uid": fixture.home_team_uid, "away_team_uid": fixture.away_team_uid,
            "home_name": fixture.home_name, "away_name": fixture.away_name, "kickoff_at": fixture.kickoff_at.isoformat(),
            "player_id": directive.player_id, "player": names[directive.player_id], "market": directive.market_type,
            "line": directive.line_value,
            "side": directive.side, "book": directive.sportsbook, "odds": directive.decimal_odds,
            "void_rule": directive.void_rule, "p_win": directive.p_win, "p_lose": directive.p_lose,
            "p_void": directive.p_void, "ev_per_unit": directive.ev_per_unit,
        })
    return records


class ShadowPickLog:
    def __init__(self, path: Path = SHADOW_FILE) -> None:
        self.path = path

    def record(self, directives: Sequence[StakingDirective], fixtures: Mapping[int, UpcomingFixture],
               names: Mapping[int, str], now: datetime, source: str) -> int:
        """Append the withheld directives (flushed and synced before returning); a failed write raises."""
        records = shadow_records(directives, fixtures, names, now, source)
        if not records:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return len(records)

    def load(self) -> list[dict[str, Any]]:
        """Every shadow pick recorded; a corrupt line raises."""
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record: Any = json.loads(line)
            if not isinstance(record, dict) or any(key not in record for key in REQUIRED):
                raise ValueError(f"{self.path.name} line {number} is not a shadow pick")
            records.append(record)
        return records
