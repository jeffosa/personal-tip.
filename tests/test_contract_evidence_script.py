"""scripts/contract_evidence.py (2026-10-25): only a bookmaker's own settlement of a bet priced at that bookmaker is an
example; a result must name the bookmaker it was read from (M2, 2026-10-26)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

from prop_alpha.alerts.replies import PlacedBet, Settlement

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prop_alpha_contract_evidence_script", ROOT / "scripts" / "contract_evidence.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bet(bet_id: int, book: str = "bet365", market: str = "tackles") -> PlacedBet:
    return PlacedBet(bet_id=bet_id, update_id=bet_id, message_id=1, number=1, kind="alert", fixture_id=10, player_id=bet_id,
                     market=market, line=1.5, side="over", odds=2.5, stake=5.0, book=book, min_odds=2.2, match="A v B",
                     player=f"P{bet_id}", placed_at=NOW - timedelta(days=2))


def _manual(bet_id: int, book: str | None) -> Settlement:
    return Settlement(bet_id, "won", "manual", "x", NOW, update_id=bet_id, book=book)


def test_only_the_books_own_settlements_of_its_own_prices_are_examples() -> None:
    script = _script()
    settled = [
        (_bet(1), _manual(1, "bet365")),                      # bet365's result for a bet365 price: an example
        (_bet(2), _manual(2, None)),                          # no book named: never the bet's own book's (M2, 2026-10-26)
        (_bet(3), _manual(3, "sportybet")),                   # another book's result
        (_bet(4, book="betway"), _manual(4, "bet365")),       # bet365's result for a Betway price
        (_bet(5), Settlement(5, "won", "auto", "auto", NOW, 2, 90)),  # automatic
        (_bet(6, market="fouls"), _manual(6, "bet365")),      # another market
    ]
    assert [b.bet_id for b, _ in script.same_book_results(settled, "bet365", "tackles")] == [1]
    assert [b.bet_id for b, _ in script.same_book_results(settled, "Bet365", "tackles")] == [1]
