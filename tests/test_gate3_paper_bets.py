"""Gate 3 trace (2026-10-26, GATE3_DECISION_MEMO.md): PRD Gate 3 criterion 4 reconciles a paper bet "to the realised
player_match_stats outcome and the book's void rule, with no manual overrides". It asks for no bookmaker's settlement (a
paper bet has none), so an automatic result under the book's verified settlement contract reconciles when the stored
statistics still give it. Without a verified contract nothing changes. And Gate 3 cannot pass while the PRD's
prerequisites (projection refresh, the paper-bet ledger writer, the per-player bench probability) are not built."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pytest

from prop_alpha.alerts import ledger, market_contract
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.market_contract import Contract, automatic_settlement_verified
from prop_alpha.alerts.replies import RECONCILE_FAILURES, PlacedBet, Reconciliation, Settlement, reconcile
from prop_alpha.evaluation import value_evidence
from prop_alpha.evaluation.value_evidence import Tip
from prop_alpha.modeling.engine import VoidRule

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
VERIFIED = {("bet365", "tackles"): Contract("s.tackles_attempted", VoidRule.VOID_IF_NO_APPEARANCE.value, "bet365_tackles.json")}


def _bet(bet_id: int = 1, book: str = "bet365", market: str = "tackles") -> PlacedBet:
    return PlacedBet(bet_id=bet_id, update_id=bet_id, message_id=1, number=1, kind="alert", fixture_id=50 + bet_id,
                     player_id=7, market=market, line=1.5, side="over", odds=2.5, stake=10.0, book=book, min_odds=2.2,
                     match="A v B", player="P (A)", placed_at=NOW - timedelta(days=3))


def _auto(bet_id: int, result: str, count: int = 2) -> Settlement:
    return Settlement(bet_id, result, "auto", "auto", NOW - timedelta(days=2), count, 90)


# ---------------- which rules count as verified ----------------
def test_only_a_contract_on_the_count_and_void_rule_automatic_settlement_uses_verifies_it() -> None:
    assert not automatic_settlement_verified("bet365", "tackles")  # VERIFIED_CONTRACTS is empty today
    assert automatic_settlement_verified("Bet365", "tackles", VERIFIED)
    assert not automatic_settlement_verified("betway", "tackles", VERIFIED)
    assert not automatic_settlement_verified("bet365", "fouls", VERIFIED)
    for other in (Contract("(s.tackles_attempted + s.dribbled_past)", VoidRule.VOID_IF_NO_APPEARANCE.value, "x.json"),
                  Contract("s.tackles_attempted", VoidRule.VOID_IF_NOT_STARTING.value, "x.json")):
        assert not automatic_settlement_verified("bet365", "tackles", {("bet365", "tackles"): other})


# ---------------- reconciliation ----------------
def test_an_automatic_result_without_verified_rules_stays_unconfirmed() -> None:
    item = reconcile(_bet(), _auto(1, "won"), ("final", 90, 2, 0), None, NOW)
    assert item.outcome == "unconfirmed"
    assert value_evidence.settlement_integrity([item]).status == "incomplete"


def test_an_automatic_result_under_verified_rules_reconciles_without_a_bookmakers_result() -> None:
    item = reconcile(_bet(), _auto(1, "won"), ("final", 90, 2, 0), None, NOW, rules_verified=True)
    assert (item.outcome, item.bookmaker_result, item.automatic_result) == ("verified_rules_agree", None, "won")
    assert "bet365's verified settlement rules" in item.detail
    integrity = value_evidence.settlement_integrity([item])
    assert (integrity.status, integrity.reconciled) == ("passed", 1)


def test_an_automatic_result_the_stored_statistics_no_longer_give_fails_integrity() -> None:
    item = reconcile(_bet(), _auto(1, "won"), ("final", 90, 1, 0), None, NOW, rules_verified=True)  # revised to 1
    assert item.outcome == "verified_rules_discrepancy" and item.automatic_result == "lost"
    assert "recorded automatic result won" in item.detail and item.outcome in RECONCILE_FAILURES
    assert value_evidence.settlement_integrity([item]).status == "failed"


def test_an_automatic_result_under_verified_rules_with_no_gradable_statistics_is_unverifiable() -> None:
    for row in (None, ("postponed", None, None, None), ("final", None, None, None)):
        item = reconcile(_bet(), _auto(1, "won"), row, None, NOW, rules_verified=True)
        assert item.outcome == "unverifiable"
        assert value_evidence.settlement_integrity([item]).status == "incomplete"
    with pytest.raises(ValueError):
        Reconciliation(1, "verified_rules_agree", "won", "won", "a bookmaker result is not a re-graded automatic one")


def test_reconcile_settlements_re_grades_only_automatic_bets_whose_rules_are_verified(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    for bet_id, book in ((1, "bet365"), (2, "skybet")):
        store.record_bet(_bet(bet_id, book))
        store.record_settlement(_auto(bet_id, "won"))
    queried: list[int] = []

    def rows(dsn: str, bets: Sequence[PlacedBet], sql: str = "") -> dict[tuple[int, int], Any]:
        queried.extend(bet.bet_id for bet in bets)
        return {(bet.fixture_id, bet.player_id): ("final", 90, 2, 0) for bet in bets}

    monkeypatch.setattr(ledger, "read_settlement_rows", rows)
    items = ledger.reconcile_settlements(store, "unused", NOW,
                                         rules_verified=lambda bet: automatic_settlement_verified(bet.book, bet.market,
                                                                                                  VERIFIED))
    assert queried == [1] and [item.outcome for _, item in items] == ["verified_rules_agree", "unconfirmed"]
    queried.clear()
    assert [item.outcome for _, item in ledger.reconcile_settlements(store, "unused", NOW)] == ["unconfirmed"] * 2
    assert queried == []  # without the predicate nothing changes (ledger --reconcile)


# ---------------- the Gate 3 sample ----------------
def test_a_bet_counts_towards_the_gate3_sample_only_when_its_settlement_reconciled() -> None:
    def qualifies(item: Reconciliation, result: str) -> bool:
        return value_evidence.tip_qualifies(item, result, contract_verified=True)

    assert qualifies(Reconciliation(1, "verified_rules_agree", None, "won", "x"), "won")
    assert qualifies(Reconciliation(1, "agree", "lost", "lost", "x"), "lost")
    assert not qualifies(Reconciliation(1, "unconfirmed", None, "won", "x"), "won")
    assert not qualifies(Reconciliation(1, "other_book_agree", "won", "won", "x"), "won")
    assert not qualifies(Reconciliation(1, "agree", "won", "won", "x", overridden=True), "won")
    assert not qualifies(Reconciliation(1, "verified_rules_agree", None, "void", "x"), "void")


def test_a_bet_at_a_book_and_market_without_a_verified_contract_never_counts_even_when_its_own_book_agreed() -> None:
    """Review 2026-10-27: an own-book agreement qualified whatever the contract registry held, and the value verdict only
    checked that some contract was registered, so a later value claim could rest on unverified markets."""
    agreed = Reconciliation(1, "agree", "won", "won", "bet365 settled it, no contract verified")
    assert not value_evidence.tip_qualifies(agreed, "won", contract_verified=False)
    assert value_evidence.tip_qualifies(agreed, "won", contract_verified=True)


def _good_tips() -> list[Tip]:
    rng = np.random.default_rng(5)
    return [Tip(f, "tackles", 1.5, 10.0, float(rng.choice([15.0, -10.0])), 0.05 + 0.01 * float(rng.standard_normal()))
            for f in range(80) for _ in range(4)]


def test_gate3_cannot_pass_while_the_prds_prerequisites_are_not_built() -> None:
    assert not any(value_evidence.GATE3_PREREQUISITES.values())  # none is built (2026-10-26)
    sample = value_evidence.tip_evidence(_good_tips(), np.random.default_rng(11))
    integrity = value_evidence.settlement_integrity(
        [Reconciliation(n, "verified_rules_agree", None, "won", "x") for n in range(1, 321)])
    assert sample["clv_criteria_met"] and integrity.status == "passed"
    today = value_evidence.gate3_result(sample, integrity)
    assert today["status"] == "incomplete"
    assert today["blockers"] == [f"prerequisite: {name}" for name in value_evidence.GATE3_PREREQUISITES]
    built = value_evidence.gate3_result(sample, integrity, {name: True for name in value_evidence.GATE3_PREREQUISITES})
    assert built["status"] == "passed" and built["blockers"] == []


def test_today_no_automatic_result_reconciles_because_no_contract_is_verified() -> None:
    assert dict(market_contract.VERIFIED_CONTRACTS) == {}
    assert not automatic_settlement_verified("bet365", "tackles") and not automatic_settlement_verified("betway", "fouls")
