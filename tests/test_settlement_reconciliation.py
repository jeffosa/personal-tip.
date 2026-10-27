"""Settlement reconciliation (maintainer task 6, 2026-10-05): the maintainer's `settled <id> won|lost|void` replies
(the bookmaker's settlement) against automatic settlement from the stored statistics (the assumed market contract).
Only agreement passes; a disagreement is a discrepancy, and a bet that cannot be graded is not a pass either."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest

from prop_alpha.alerts import ledger
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.replies import (
    RECONCILE_FAILURES,
    PlacedBet,
    Reconciliation,
    Settlement,
    book_from_detail,
    reconcile,
    reconciliation_summary,
    reconciliation_text,
    same_book,
)

NOW = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
Row = tuple[str, int | None, int | None, int | None]


def _bet(bet_id: int = 1, side: str = "over", line: float = 1.5, market: str = "tackles") -> PlacedBet:
    return PlacedBet(bet_id=bet_id, update_id=10 + bet_id, message_id=500, number=1, kind="alert", fixture_id=9001,
                     player_id=7, market=market, line=line, side=side, odds=2.0, stake=5.0, book="skybet",
                     min_odds=1.9, match="Walsall v Port Vale", player="A (Walsall)", placed_at=NOW - timedelta(days=2))


def _manual(bet_id: int, result: str, book: str | None = "skybet") -> Settlement:
    """A bookmaker result read off the tipped book (skybet) unless another, or no, bookmaker is named."""
    return Settlement(bet_id, result, "manual", "reported", NOW, update_id=900 + bet_id, book=book)


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
    assert "1 result(s) from the tipped book: 1 agree, 0 DISCREPANCY, 0 could not be checked" in clean
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


# ---------------- another bookmaker's result (audit 2026-10-25) ----------------
def test_another_books_result_is_compared_but_never_counted_as_agreement_with_the_tipped_book() -> None:
    bet = _bet()  # tipped at skybet
    sportybet = Settlement(1, "won", "manual", "bookmaker result (sportybet) recorded by hand", NOW, update_id=0,
                           book="sportybet")
    agreeing = reconcile(bet, sportybet, ("final", 90, 2, 0), None, NOW)
    assert agreeing.outcome == "other_book_agree" and "sportybet's result, not skybet's" in agreeing.detail
    disagreeing = reconcile(bet, replace(sportybet, result="lost"), ("final", 90, 2, 0), None, NOW)
    assert disagreeing.outcome == "other_book_discrepancy"
    # The tipped book's own result, named in any case, is the tipped book's.
    assert reconcile(bet, replace(sportybet, book="SkyBet"), ("final", 90, 2, 0), None, NOW).outcome == "agree"
    summary = reconciliation_summary([agreeing, disagreeing])
    assert "0 result(s) from the tipped book: 0 agree" in summary and "2 result(s) from another book (1 agree, 1 DISAGREE" in summary
    assert summary.endswith("PRD Gate 3 needs every bet to reconcile.") and "FAIL" in summary
    assert "nothing verified yet at the tipped book" in reconciliation_summary([agreeing])
    assert "OTHER BOOK, DISAGREES" in reconciliation_text(bet, disagreeing)
    assert set(RECONCILE_FAILURES) == {"discrepancy", "other_book_discrepancy", "unnamed_book_discrepancy",
                                       "verified_rules_discrepancy"}  # Gate 3 trace, 2026-10-26


def test_a_settlement_names_its_bookmaker_only_when_manual_and_trimmed() -> None:
    with pytest.raises(ValueError, match="names a bookmaker"):
        Settlement(1, "won", "auto", "auto", NOW, 2, 90, book="bet365")
    for bad in ("", " bet365"):
        with pytest.raises(ValueError, match="names a bookmaker"):
            Settlement(1, "won", "manual", "x", NOW, update_id=1, book=bad)


def test_older_records_name_their_bookmaker_in_the_detail(tmp_path: Path) -> None:
    assert book_from_detail("bookmaker result (sportybet (maintainer's ticket 11-12 Oct, MARKET_CONTRACT.md 2d/2e)) "
                            "recorded by hand") == "sportybet"
    assert book_from_detail("bookmaker result (betway) recorded from Telegram") == "betway"
    assert book_from_detail("bookmaker result (book not given) recorded by hand") is None
    assert book_from_detail("reported") is None
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    store.record_bet(_bet(1))
    store.record_bet(_bet(2))
    store.path.open("a", encoding="utf-8").write(json.dumps({  # written before settlements carried "book"
        "event": "settled", "bet_id": 1, "result": "lost", "source": "manual", "update_id": 0,
        "detail": "bookmaker result (sportybet (ticket)) recorded by hand", "at": NOW.isoformat()}) + "\n")
    store.record_settlement(Settlement(2, "won", "manual", "bookmaker result (bet365) recorded by hand", NOW,
                                       update_id=0, book="bet365"))
    assert {bet.bet_id: settlement.book for bet, settlement in store.settled_bets()} == {1: "sportybet", 2: "bet365"}


# ---------------- a result naming no bookmaker (M2, audit 2026-10-26) ----------------
def test_a_result_naming_no_bookmaker_is_never_agreement_with_the_tipped_book() -> None:
    """Until 2026-10-26 a manual result with no bookmaker was taken to be the tipped book's, so a SportyBet ticket typed as
    `result 3 lost` reconciled as AGREE at bet365. It is now unverified (agreeing) or a failure (disagreeing)."""
    bet = _bet()  # tipped at skybet
    named_same = reconcile(bet, _manual(1, "won", "SkyBet"), ("final", 90, 2, 0), None, NOW)
    named_other = reconcile(bet, _manual(1, "won", "sportybet"), ("final", 90, 2, 0), None, NOW)
    unnamed = reconcile(bet, _manual(1, "won", None), ("final", 90, 2, 0), None, NOW)  # this case used to be "agree"
    unnamed_wrong = reconcile(bet, _manual(1, "lost", None), ("final", 90, 2, 0), None, NOW)
    assert (named_same.outcome, named_other.outcome) == ("agree", "other_book_agree")
    assert unnamed.outcome == "unnamed_book_agree" and "no bookmaker named on this result" in unnamed.detail
    assert unnamed_wrong.outcome == "unnamed_book_discrepancy" and "unnamed_book_discrepancy" in RECONCILE_FAILURES
    assert "NO BOOK NAMED, AGREES" in reconciliation_text(bet, unnamed)
    summary = reconciliation_summary([unnamed, unnamed_wrong])
    assert "0 result(s) from the tipped book: 0 agree" in summary and "2 result(s) naming no bookmaker (1 agree, 1 DISAGREE" in summary
    assert "FAIL" in summary and "nothing verified yet at the tipped book" in reconciliation_summary([unnamed])
    # Without a gradable statistic it stays unverifiable, named or not.
    assert reconcile(bet, _manual(1, "won", None), None, None, NOW).outcome == "unverifiable"
    assert not same_book("skybet", None) and same_book("skybet", " SKYBET ") and not same_book("skybet", "bet365")


def test_a_settled_reply_can_name_its_bookmaker_and_one_that_does_not_stays_unverified(tmp_path: Path) -> None:
    from prop_alpha.alerts.replies import SettleCommand, parse_reply

    assert parse_reply("settled 12 lost") == SettleCommand(12, "lost", None)
    assert parse_reply("settled #12 won Bet365") == SettleCommand(12, "won", "Bet365")
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    store.record_bet(_bet(1))
    store.record_settlement(Settlement(1, "won", "manual", "settled by hand (no bookmaker named)", NOW, update_id=5))
    [(_, settlement)] = store.settled_bets()
    assert settlement.book is None  # an older or unnamed record is not read as the bet's book
    assert reconcile(_bet(1), settlement, ("final", 90, 2, 0), None, NOW).outcome == "unnamed_book_agree"
