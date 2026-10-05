from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from prop_alpha.alerts import ledger
from prop_alpha.alerts.fx import ExchangeRate
from prop_alpha.alerts.ledger import PlacedBetStore, combined_exposure, poll, process_updates
from prop_alpha.alerts.replies import (
    AlertItem,
    PlaceCommand,
    PlacedBet,
    ReplyError,
    SettleCommand,
    Settlement,
    UndoCommand,
    confirmation,
    grade,
    parse_reply,
    profit,
    resolve_bet,
)
from prop_alpha.alerts.telegram import (
    AlertContext,
    MessageIndex,
    SentAlert,
    SentAlertStore,
    TelegramConfig,
    TelegramNotifier,
)
from prop_alpha.scanner.market_scanner import AllocationReport

FAKE_TOKEN = "123456789" + ":" + "AAH" + "x" * 32
CHAT = "4242"
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
ALERT = AlertItem(1, "alert", 9001, 7, "tackles", 1.5, (("over", 1.97),), "Walsall v Port Vale", "A (Walsall)", "skybet")
BOARD = AlertItem(2, "board", 9001, 8, "fouls", 0.5, (("over", 1.70), ("under", 2.75)), "Walsall v Port Vale",
                  "B (Walsall)")
MESSAGES = {500: {1: ALERT, 2: BOARD}}


# ---------------- parsing ----------------
@pytest.mark.parametrize(("text", "expected"), [
    ("placed 3 @1.95 5", PlaceCommand(3, None, 1.95, 5.0, None)),
    ("Placed 2 under @ 2.10 £10 Sky Bet", PlaceCommand(2, "under", 2.10, 10.0, "Sky Bet", "£")),
    ("placed #1 o @1.9 5.50", PlaceCommand(1, "over", 1.9, 5.5, None)),
    ("undo 12", UndoCommand(12)),
    ("UNDO #3", UndoCommand(3)),
    ("settled 12 lost", SettleCommand(12, "lost")),
    ("Settle #4 WIN", SettleCommand(4, "won")),
    ("settled 5 void", SettleCommand(5, "void")),
])
def test_replies_parse(text: str, expected: object) -> None:
    assert parse_reply(text) == expected


@pytest.mark.parametrize("text", ["placed three @1.95 5", "placed 3 @1.00 5", "placed 3 @1.95 0", "placed 3 1.95 5",
                                  "hello"])
def test_malformed_replies_explain_the_form(text: str) -> None:
    result = parse_reply(text)
    assert isinstance(result, ReplyError) and "placed <number>" in result.message


def _resolve(command: PlaceCommand) -> PlacedBet | ReplyError:
    return resolve_bet(command, MESSAGES[500], bet_id=1, update_id=10, message_id=500, placed_at=NOW)


def test_alert_bet_takes_its_side_and_book() -> None:
    bet = _resolve(PlaceCommand(1, None, 2.05, 5.0, None))
    assert isinstance(bet, PlacedBet)
    assert (bet.side, bet.book, bet.min_odds, bet.kind, bet.below_minimum) == ("over", "skybet", 1.97, "alert", False)


def test_board_bet_needs_a_side_that_is_listed() -> None:
    assert isinstance(_resolve(PlaceCommand(2, None, 2.0, 5.0, None)), ReplyError)  # both sides listed
    assert isinstance(_resolve(PlaceCommand(1, "under", 2.0, 5.0, None)), ReplyError)  # alert offers OVER only
    missing = _resolve(PlaceCommand(9, None, 2.0, 5.0, None))
    assert isinstance(missing, ReplyError) and "lists: 1, 2" in missing.message
    bet = _resolve(PlaceCommand(2, "under", 2.80, 5.0, "bet365"))
    assert isinstance(bet, PlacedBet) and (bet.side, bet.min_odds, bet.book) == ("under", 2.75, "bet365")


def test_board_bet_without_a_book_is_recorded_as_unknown_and_low_odds_are_flagged() -> None:
    bet = _resolve(PlaceCommand(2, "over", 1.60, 5.0, None))
    assert isinstance(bet, PlacedBet) and bet.book == "unknown" and bet.below_minimum
    assert "WARNING: 1.60 is below the minimum 1.70" in confirmation(bet) and "undo 1" in confirmation(bet)


def test_alert_items_are_validated() -> None:
    for bad in (dict(kind="tip"), dict(number=0), dict(line=1.3), dict(min_odds=()),
                dict(min_odds=(("over", 1.9), ("over", 2.0))), dict(min_odds=(("both", 1.9),)),
                dict(min_odds=(("over", 1.0),))):
        fields: dict[str, Any] = {**ALERT.__dict__, **bad}
        with pytest.raises(ValueError):
            AlertItem(**fields)


# ---------------- processing updates ----------------
def _update(update_id: int, text: str, reply_to: int | None = 500, chat: str = CHAT) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": 900 + update_id, "date": int(NOW.timestamp()), "chat": {"id": int(chat)},
                               "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def test_updates_record_bets_once_and_only_from_the_configured_chat(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    updates = [
        _update(1, "placed 1 @2.05 5"),
        _update(2, "placed 1 @2.05 500", chat="999"),          # another chat: ignored
        _update(3, "thanks bot"),                               # not a command: ignored
        _update(4, "placed 2 over @1.80 3", reply_to=None),     # not a reply: explained
        _update(5, "placed 2 over @1.80 3", reply_to=777),      # reply to an unknown message: explained
        _update(6, "placed 2 under @2.90 4 skybet"),
    ]
    replies, offset = process_updates(updates, CHAT, MESSAGES, store, NOW)
    assert offset == 7
    assert [reply_to for reply_to, _ in replies] == [901, 904, 905, 906]
    assert replies[0][1].startswith("Recorded bet #1") and replies[3][1].startswith("Recorded bet #2")
    assert "Reply to the alert or board message itself" in replies[1][1]
    assert "not an alert or board I sent" in replies[2][1]
    bets = store.open_bets(NOW)
    assert [(b.bet_id, b.side, b.odds, b.stake, b.book) for b in bets] == [(1, "over", 2.05, 5.0, "skybet"),
                                                                           (2, "under", 2.90, 4.0, "skybet")]
    again, _ = process_updates(updates, CHAT, MESSAGES, store, NOW)  # Telegram re-delivers the same updates
    assert not [text for _, text in again if text.startswith("Recorded")] and len(store.open_bets(NOW)) == 2


def test_undo_cancels_once(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5")], CHAT, MESSAGES, store, NOW)
    replies, _ = process_updates([_update(2, "undo 1"), _update(3, "undo 1"), _update(4, "undo 9")], CHAT,
                                 MESSAGES, store, NOW)
    assert [text for _, text in replies] == ["Cancelled bet #1.", "No open recorded bet #1 to cancel.",
                                             "No open recorded bet #9 to cancel."]
    assert store.open_bets(NOW) == []


def test_exposure_takes_the_larger_of_alerted_and_placed_per_market(tmp_path: Path) -> None:
    alerts = SentAlertStore(tmp_path / "sent.jsonl")
    alerts.append(SentAlert((9001, 7, "tackles", 1.5, "over", "skybet"), 20.0, NOW - timedelta(hours=1)))
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5"), _update(2, "placed 2 under @2.90 4")], CHAT, MESSAGES,
                    store, NOW)
    book = combined_exposure(alerts, store, NOW).by_market
    assert book == {(9001, 7, "tackles"): 20.0, (9001, 8, "fouls"): 4.0}  # board bet counted; alert not doubled
    # Audit 2026-10-04: the alert leaves after OPEN_POSITION_WINDOW, the placed bets stay until settled.
    later = NOW + timedelta(days=5)
    assert combined_exposure(alerts, store, later).by_market == {(9001, 7, "tackles"): 5.0, (9001, 8, "fouls"): 4.0}
    store.record_settlement(Settlement(1, "lost", "auto", "test", later))
    assert combined_exposure(alerts, store, later).by_market == {(9001, 8, "fouls"): 4.0}


def test_corrupt_state_files_raise(tmp_path: Path) -> None:
    (tmp_path / "bets.jsonl").write_text('{"event": "guess"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        PlacedBetStore(tmp_path / "bets.jsonl").load()
    (tmp_path / "index.jsonl").write_text('{"items": []}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        MessageIndex(tmp_path / "index.jsonl").load()


# ---------------- Telegram round trip ----------------
class _FakeTelegram(TelegramNotifier):
    def __init__(self, updates: list[Any], index: MessageIndex | None = None) -> None:
        super().__init__(TelegramConfig(FAKE_TOKEN, CHAT), sleep=lambda s: None, index=index)
        self.updates = updates
        self.offsets: list[int | None] = []
        self.sent: list[tuple[str, int | None]] = []

    def get_updates(self, offset: int | None) -> list[Any]:
        self.offsets.append(offset)
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def send_message(self, text: str, reply_to: int | None = None) -> int:
        self.sent.append((text, reply_to))
        return 1000 + len(self.sent)


def test_poll_answers_each_reply_and_remembers_the_offset(tmp_path: Path) -> None:
    index = MessageIndex(tmp_path / "index.jsonl")
    index.record(500, [ALERT, BOARD], NOW)
    store, offset_file = PlacedBetStore(tmp_path / "bets.jsonl"), tmp_path / "offset.json"
    fake = _FakeTelegram([_update(1, "placed 1 @2.05 5"), _update(2, "placed 2 over @1.75 2")])
    assert poll(fake, index, store, offset_file, NOW) == 2
    assert [reply_to for _, reply_to in fake.sent] == [901, 902]
    assert json.loads(offset_file.read_text(encoding="utf-8")) == {"offset": 3}
    assert poll(fake, index, store, offset_file, NOW) == 0 and fake.offsets == [None, 3]
    assert len(store.open_bets(NOW)) == 2


def test_digest_messages_are_indexed_by_message_id(tmp_path: Path) -> None:
    from prop_alpha.alerts.telegram import demo_case

    projections, report = demo_case(NOW)
    index = MessageIndex(tmp_path / "index.jsonl")
    fake = _FakeTelegram([], index=index)
    sent, message_ids = fake.notify_digest(report, projections, descriptions={
        key: AlertContext("Demo match", "Demo player") for key in projections})
    assert sent >= 1 and message_ids
    indexed = index.load()
    assert set(indexed) == set(message_ids)
    first = indexed[message_ids[0]][1]
    assert first.kind == "alert" and first.player == "Demo player" and len(first.sides) == 1
    assert "placed <number>" in fake.sent[0][0]


def test_ledger_list_prints_open_bets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 2 over @1.60 5")], CHAT, MESSAGES, store, datetime.now(timezone.utc))
    monkeypatch.setattr(ledger, "DEFAULT_BETS_FILE", tmp_path / "bets.jsonl")
    monkeypatch.setattr(ledger, "PlacedBetStore", lambda path=tmp_path / "bets.jsonl": PlacedBetStore(path))
    assert ledger.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "OVER 0.5 fouls @ 1.60 stake 5" in out and "BELOW MINIMUM" in out
    assert "1 open bet(s), total stake 5" in out


def test_empty_report_indexes_nothing(tmp_path: Path) -> None:
    index = MessageIndex(tmp_path / "index.jsonl")
    fake = _FakeTelegram([], index=index)
    assert fake.notify_digest(AllocationReport(), {}) == (0, [])
    assert index.load() == {}


# ---------------- settlement (audit 2026-10-04) ----------------
def _bet(side: str = "over", line: float = 1.5, market: str = "tackles", stake: float = 10.0,
         odds: float = 2.0) -> PlacedBet:
    return PlacedBet(bet_id=1, update_id=10, message_id=500, number=1, kind="alert", fixture_id=9001, player_id=7,
                     market=market, line=line, side=side, odds=odds, stake=stake, book="skybet", min_odds=1.9,
                     match="Walsall v Port Vale", player="A (Walsall)", placed_at=NOW)


@pytest.mark.parametrize(("side", "line", "status", "minutes", "count", "expected"), [
    ("over", 1.5, "final", 90, 2, "won"),
    ("over", 1.5, "final", 90, 1, "lost"),
    ("under", 1.5, "final", 90, 1, "won"),
    ("under", 1.5, "final", 75, 3, "lost"),
    ("over", 2.0, "final", 90, 2, "void"),     # whole line, count on it: push
    ("under", 0.5, "final", 0, 0, "void"),     # in the squad, did not play
    ("over", 1.5, "final", None, None, None),  # no stored row: left for settling by hand
    ("over", 1.5, "live", 60, 4, None),        # not final yet
    ("over", 1.5, "postponed", None, None, None),
])
def test_grade_settles_on_the_stored_count(side: str, line: float, status: str, minutes: int | None,
                                           count: int | None, expected: str | None) -> None:
    settlement = grade(_bet(side, line), status, minutes, count, NOW)
    assert (settlement.result if settlement else None) == expected
    if settlement is not None:
        assert settlement.source == "auto" and "assumed market rules" in settlement.detail


def test_grade_refuses_an_unknown_market_or_a_missing_count() -> None:
    with pytest.raises(ValueError, match="no settlement statistic"):
        grade(_bet(market="yellow_card"), "final", 90, 1, NOW)
    with pytest.raises(ValueError, match="no tackles count"):
        grade(_bet(), "final", 90, None, NOW)


def test_profit_by_result() -> None:
    bet = _bet(stake=10.0, odds=2.5)
    results = [profit(bet, Settlement(1, result, "auto", "", NOW)) for result in ("won", "lost", "void")]
    assert results == [15.0, -10.0, 0.0]
    with pytest.raises(ValueError, match="another bet"):
        profit(bet, Settlement(2, "won", "auto", "", NOW))


def test_settlement_records_are_validated() -> None:
    with pytest.raises(ValueError):
        Settlement(1, "half-won", "auto", "", NOW)
    with pytest.raises(ValueError, match="Telegram update"):
        Settlement(1, "won", "manual", "", NOW)
    with pytest.raises(ValueError, match="UTC"):
        Settlement(1, "won", "auto", "", NOW.replace(tzinfo=None))


def test_settle_open_bets_grades_only_final_matches_with_a_player_row(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5"), _update(2, "placed 2 under @2.90 4")], CHAT, MESSAGES,
                    store, NOW)
    # bet 1: tackles over 1.5 for player 7 (3 tackles); bet 2: fouls under 0.5 for player 8, not ingested yet.
    rows: dict[tuple[int, int], tuple[str, int | None, int | None, int | None] | None] = {
        (9001, 7): ("final", 90, 3, 1), (9001, 8): None,
    }
    settled = ledger.settle_open_bets(store.open_bets(NOW), rows, NOW)
    assert [(bet.bet_id, s.result, s.count) for bet, s in settled] == [(1, "won", 3)]
    for _, settlement in settled:
        store.record_settlement(settlement)
    assert [bet.bet_id for bet in store.open_bets(NOW)] == [2]
    assert "1 settled bet(s) (1 won, 0 lost, 0 void): staked 5.00, profit +5.25" in ledger.results_summary(
        store.settled_bets())


def test_a_settled_reply_overrides_the_automatic_result_and_is_recorded_once(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5")], CHAT, MESSAGES, store, NOW)
    store.record_settlement(Settlement(1, "won", "auto", "auto", NOW))
    replies, _ = process_updates([_update(2, "settled 1 lost"), _update(3, "settled 9 won")], CHAT, MESSAGES,
                                 store, NOW)
    assert replies[0][1].startswith("Recorded #1 LOST -5.00") and replies[1][1] == "No recorded bet #9 to settle."
    store.record_settlement(Settlement(1, "won", "auto", "a later automatic run", NOW))  # never beats a manual one
    [(_, settlement)] = store.settled_bets()
    assert settlement.source == "manual" and settlement.result == "lost"
    again, _ = process_updates([_update(2, "settled 1 lost")], CHAT, MESSAGES, store, NOW)
    assert again == []  # the same Telegram update is not recorded twice


def test_old_open_bets_stay_open_and_are_flagged_stale(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5")], CHAT, MESSAGES, store, NOW)
    later = NOW + ledger.STALE_AFTER + timedelta(hours=1)
    assert [b.bet_id for b in store.open_bets(later)] == [1]
    assert store.stale_bets(NOW) == [] and [b.bet_id for b in store.stale_bets(later)] == [1]
    assert store.exposure(later).by_market == {(9001, 7, "tackles"): 5.0}


def test_corrupt_settled_record_raises(tmp_path: Path) -> None:
    path = tmp_path / "bets.jsonl"
    record = {"event": "settled", "bet_id": 1, "result": "maybe", "source": "auto", "at": "x"}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bets.jsonl line 1"):
        PlacedBetStore(path).load()


# ---------------- stake currency (USD bankroll, GBP books; 2026-10-04) ----------------
def test_a_pound_stake_is_converted_to_the_bankroll_currency_and_kept_as_entered(tmp_path: Path) -> None:
    rate = ExchangeRate("GBP", "USD", 1.32, date(2026, 10, 9))
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    replies, _ = process_updates([_update(1, "placed 1 @2.05 £10"), _update(2, "placed 2 under @2.90 $4"),
                                  _update(3, "placed 2 over @1.80 €5")], CHAT, MESSAGES, store, NOW, "USD", rate)
    assert "stake 13.2 (skybet) = GBP 10 as entered" in replies[0][1]
    assert "€" not in replies[2][1] and "no current EUR rate" in replies[2][1]
    bets = store.open_bets(NOW)
    assert [(b.stake, b.entered_stake, b.entered_currency) for b in bets] == [(13.2, 10.0, "GBP"), (4.0, None, None)]
    assert store.exposure(NOW).by_market == {(9001, 7, "tackles"): 13.2, (9001, 8, "fouls"): 4.0}


def test_a_pound_stake_without_a_rate_is_refused_not_guessed(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    replies, _ = process_updates([_update(1, "placed 1 @2.05 £10")], CHAT, MESSAGES, store, NOW, "USD", None)
    assert "no current GBP rate" in replies[0][1] and store.open_bets(NOW) == []


def test_bets_recorded_before_the_currency_fields_still_load(tmp_path: Path) -> None:
    path = tmp_path / "bets.jsonl"
    store = PlacedBetStore(path)
    process_updates([_update(1, "placed 1 @2.05 5")], CHAT, MESSAGES, store, NOW)
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["entered_stake"], record["entered_currency"]
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    [bet] = PlacedBetStore(path).open_bets(NOW)
    assert bet.stake == 5.0 and bet.entered_stake is None


def test_automatic_results_are_labelled_provisional_and_manual_ones_are_not(tmp_path: Path) -> None:
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5"), _update(2, "placed 2 under @2.90 4")], CHAT, MESSAGES,
                    store, NOW)
    store.record_settlement(Settlement(1, "won", "auto", "auto", NOW))
    process_updates([_update(3, "settled 2 lost")], CHAT, MESSAGES, store, NOW)
    texts = [ledger.settlement_text(bet, s) for bet, s in store.settled_bets()]
    assert "WON (provisional)" in texts[0] and "LOST" in texts[1] and "provisional" not in texts[1]
    assert "1 provisional (automatic, assumed market rules" in ledger.results_summary(store.settled_bets())


def test_the_confirmed_only_total_leaves_out_provisional_automatic_results(tmp_path: Path) -> None:
    """Readiness item 2 (2026-10-05): automatic results rest on the assumed market contract, so a performance claim
    quotes the confirmed-only total, which counts manual settlements alone."""
    store = PlacedBetStore(tmp_path / "bets.jsonl")
    process_updates([_update(1, "placed 1 @2.05 5"), _update(2, "placed 2 under @2.90 4")], CHAT, MESSAGES,
                    store, NOW)
    store.record_settlement(Settlement(1, "won", "auto", "auto", NOW))
    process_updates([_update(3, "settled 2 lost")], CHAT, MESSAGES, store, NOW)
    summary = ledger.results_summary(store.settled_bets())
    assert "staked 9.00, profit +1.25" in summary  # all results, provisional included: +5.25 - 4.00
    assert "confirmed only: 1 bet(s), staked 4.00, profit -4.00" in summary
    manual_only = [(bet, s) for bet, s in store.settled_bets() if s.source == "manual"]
    assert "confirmed only" not in ledger.results_summary(manual_only)  # nothing provisional: one total is enough
