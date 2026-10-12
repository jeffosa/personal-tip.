"""Settlement reconciliation (maintainer task 6, 2026-10-05): the maintainer's `settled <id> won|lost|void` replies
(the bookmaker's settlement) against automatic settlement from the stored statistics (the assumed market contract).
Only agreement passes; a disagreement is a discrepancy, and a bet that cannot be graded is not a pass either."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest

from prop_alpha.alerts import ledger
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.replies import (
    PlacedBet,
    Reconciliation,
    Settlement,
    reconcile,
    reconciliation_summary,
    reconciliation_text,
)

NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
Row = tuple[str, int | None, int | None, int | None]


def _bet(bet_id: int = 1, side: str = "over", line: float = 1.5, market: str = "tackles") -> PlacedBet:
    return PlacedBet(bet_id=bet_id, update_id=10 + bet_id, message_id=500, number=1, kind="alert", fixture_id=9001,
                     player_id=7, market=market, line=line, side=side, odds=2.0, stake=5.0, book="skybet",
                     min_odds=1.9, match="Walsall v Port Vale", player="A (Walsall)", placed_at=NOW - timedelta(days=2))


def _manual(bet_id: int, result: str) -> Settlement:
    return Settlement(bet_id, result, "manual", "reported", NOW, update_id=900 + bet_id)


def _auto(bet_id: int, result: str) -> Settlement:
    return Settlement(bet_id, result, "auto", "auto", NOW - timedelta(hours=1), 2, 90)


@pytest.mark.parametrize(("row", "reported", "outcome", "automatic"), [
    (("final", 90, 2, 0), "won", "agree", "won"),                # 2 tackles v over 1.5
    (("final", 90, 2, 0), "lost", "discrepancy", "won"),         # the book settled it differently: not a pass
    (("final", 90, 1, 0), "void", "discrepancy", "lost"),        # e.g. the book voids where the contract says lost
    (("final", 0, 0, 0), "void", "agree", "void"),               # did not play
    (("final", None, None, None), "won", "unverifiable", None),  # no stored row for the player
    (("live", 60, 3, 0), "won", "unverifiable", None),           # match not final in the database
    (None, "won", "unverifiable", None),                         # match never ingested
])
def test_a_bookmaker_result_is_compared_with_automatic_settlement(
        row: Row | None, reported: str, outcome: str, automatic: str | None) -> None:
    item = reconcile(_bet(), _manual(1, reported), row, None, NOW)
    assert (item.outcome, item.bookmaker_result, item.automatic_result) == (outcome, reported, automatic)
    assert not item.overridden


def test_the_market_decides_which_statistic_is_compared() -> None:
    fouls = _bet(market="fouls", line=0.5)
    assert reconcile(fouls, _manual(1, "won"), ("final", 90, 0, 1), None, NOW).outcome == "agree"
    assert reconcile(fouls, _manual(1, "won"), ("final", 90, 5, 0), None, NOW).outcome == "discrepancy"


def test_an_automatic_only_result_is_unconfirmed_and_an_override_is_counted() -> None:
    unconfirmed = reconcile(_bet(), _auto(1, "won"), ("final", 90, 2, 0), None, NOW)
    assert (unconfirmed.outcome, unconfirmed.bookmaker_result, unconfirmed.automatic_result) == (
        "unconfirmed", None, "won")
    overridden = reconcile(_bet(), _manual(1, "lost"), ("final", 90, 2, 0), _auto(1, "won"), NOW)
    assert overridden.outcome == "discrepancy" and overridden.overridden
    assert "overrode the recorded automatic result" in reconciliation_text(_bet(), overridden)
    confirmed = reconcile(_bet(), _manual(1, "won"), ("final", 90, 2, 0), _auto(1, "won"), NOW)
    assert confirmed.outcome == "agree" and not confirmed.overridden


def test_inputs_and_outcomes_are_validated() -> None:
    with pytest.raises(ValueError, match="not for bet"):
        reconcile(_bet(), _manual(2, "won"), ("final", 90, 2, 0), None, NOW)
    with pytest.raises(ValueError, match="UTC"):
        reconcile(_bet(), _manual(1, "won"), ("final", 90, 2, 0), None, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="no tackles count"):
        reconcile(_bet(), _manual(1, "won"), ("final", 90, None, 0), None, NOW)  # played, but no count stored
    bad_cases: list[tuple[str, str | None, str | None]] = [
        ("pass", "won", "won"), ("agree", "won", "lost"), ("discrepancy", "won", "won"), ("agree", "won", None),
        ("unverifiable", "won", "won"), ("unconfirmed", None, "maybe"),
    ]
    for outcome, bookmaker, automatic in bad_cases:
        with pytest.raises(ValueError):
            Reconciliation(1, outcome, bookmaker, automatic, "")


def test_summary_never_reports_a_discrepancy_or_an_unchecked_bet_as_a_pass() -> None:
    def item(outcome: str, bookmaker: str | None, automatic: str | None, overridden: bool = False) -> Reconciliation:
        return Reconciliation(1, outcome, bookmaker, automatic, "", overridden)

    clean = reconciliation_summary([item("agree", "won", "won"), item("unconfirmed", None, "lost")])
    assert "1 bookmaker result(s): 1 agree, 0 DISCREPANCY, 0 could not be checked" in clean
    assert "1 automatic result(s) not yet confirmed" in clean and "no discrepancy so far" in clean
    failing = reconciliation_summary([item("agree", "won", "won"), item("discrepancy", "lost", "won", True)])
    assert "1 DISCREPANCY" in failing and "FAIL" in failing and "1 override(s)" in failing
    unchecked = reconciliation_summary([item("unverifiable", "won", None)])
    assert "1 could not be checked" in unchecked and "nothing verified yet" in unchecked
    assert "nothing verified yet" in reconciliation_summary([])


def _store(tmp_path: Path) -> PlacedBetStore:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    for bet_id in (1, 2, 3):
        store.record_bet(_bet(bet_id))
    store.record_settlement(_auto(1, "won"))    # bet 1: automatic won, then the reply says lost
    store.record_settlement(_manual(1, "lost"))
    store.record_settlement(_manual(2, "won"))  # bet 2: reported won only
    store.record_settlement(_auto(3, "lost"))   # bet 3: automatic only
    return store


def test_reconcile_settlements_reads_only_confirmed_bets_and_finds_the_override(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    asked: list[list[int]] = []

    def rows(dsn: str, bets: Sequence[PlacedBet], settlement_sql: str = ledger.SETTLEMENT_SQL) -> dict[tuple[int, int], Any]:
        assert settlement_sql == ledger.SETTLEMENT_SQL  # the League One/Two ledger keeps tackles_attempted
        asked.append([bet.bet_id for bet in bets])
        return {(9001, 7): ("final", 90, 2, 0)}

    monkeypatch.setattr(ledger, "read_settlement_rows", rows)
    items = {bet.bet_id: item for bet, item in ledger.reconcile_settlements(store, "postgresql://unused", NOW)}
    assert asked == [[1, 2]]  # the automatic-only bet 3 needs no query
    assert (items[1].outcome, items[1].overridden) == ("discrepancy", True)
    assert items[2].outcome == "agree" and items[3].outcome == "unconfirmed"
    assert store.automatic_settlements().keys() == {1, 3}


def test_the_reconcile_command_exits_1_on_a_discrepancy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(ledger, "PlacedBetStore", lambda: store)
    monkeypatch.setattr(ledger, "read_settlement_rows", lambda dsn, bets, sql=ledger.SETTLEMENT_SQL: {(9001, 7): ("final", 90, 2, 0)})
    assert ledger.main(["--reconcile"]) == 1
    out = capsys.readouterr().out
    assert "#1 DISCREPANCY" in out and "#2 AGREE" in out and "#3" not in out and "FAIL" in out
    agreeing = PlacedBetStore(tmp_path / "agree.jsonl")
    agreeing.record_bet(_bet(1))
    agreeing.record_settlement(_manual(1, "won"))
    monkeypatch.setattr(ledger, "PlacedBetStore", lambda: agreeing)
    assert ledger.main(["--reconcile"]) == 0
    assert "no discrepancy so far" in capsys.readouterr().out
