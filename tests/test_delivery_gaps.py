"""Delivery audit (2026-10-05): every way found for an alert, a board or a bet-reply answer to be lost, duplicated or
silently cut, each pinned by a test that failed on the code before the fix."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from prop_alpha.alerts import scan, status
from prop_alpha.alerts.delivery import RESENT_HEADER
from prop_alpha.alerts.ledger import (
    DELAYED_PREFIX,
    EDIT_NOTICE,
    MAX_ANSWER_ATTEMPTS,
    AnswerOutbox,
    PlacedBetStore,
    poll,
)
from prop_alpha.alerts.telegram import (
    TELEGRAM_MAX_TEXT,
    DeliveryLog,
    MessageIndex,
    TelegramConfig,
    TelegramError,
    TelegramNotifier,
    split_text,
)
from prop_alpha.scanner.slate import FairPrice, SquadPlayer, UpcomingFixture
from test_alerts import FakeSession, _ok
from test_ledger import ALERT, BOARD, CHAT, FAKE_TOKEN, NOW, _FakeTelegram, _update


class _FlakyTelegram(_FakeTelegram):
    """Telegram that refuses the first `failures` sends."""

    def __init__(self, updates: list[Any], failures: int) -> None:
        super().__init__(updates)
        self.failures = failures

    def send_message(self, text: str, reply_to: int | None = None) -> int:
        if self.failures > 0:
            self.failures -= 1
            raise TelegramError("sendMessage failed: ConnectTimeout (details withheld: they contain the token)")
        return super().send_message(text, reply_to)


def _state(tmp_path: Path) -> tuple[MessageIndex, PlacedBetStore, Path, AnswerOutbox]:
    index = MessageIndex(tmp_path / "index.jsonl")
    index.record(500, [ALERT, BOARD], NOW)
    return index, PlacedBetStore(tmp_path / "bets.jsonl"), tmp_path / "offset.json", AnswerOutbox(tmp_path / "ans.jsonl")


# ---------------- gap 1: an answer lost after the offset moved on ----------------
def test_an_answer_that_failed_to_send_is_resent_by_the_next_poll_and_the_bet_recorded_once(tmp_path: Path) -> None:
    index, store, offset_file, outbox = _state(tmp_path)
    flaky = _FlakyTelegram([_update(1, "placed 1 @2.05 5")], failures=1)
    with pytest.raises(TelegramError):
        poll(flaky, index, store, offset_file, NOW, outbox=outbox)
    assert len(store.open_bets(NOW)) == 1 and json.loads(offset_file.read_text(encoding="utf-8")) == {"offset": 2}
    assert [(reply_to, attempts) for _, reply_to, _, attempts in outbox.unsent()] == [(901, 1)]
    assert poll(flaky, index, store, offset_file, NOW, outbox=outbox) == 1
    [(text, reply_to)] = flaky.sent
    assert reply_to == 901 and text.startswith(DELAYED_PREFIX + "Recorded bet #1")
    assert outbox.unsent() == [] and len(store.open_bets(NOW)) == 1  # answered, never recorded twice


def test_an_answer_telegram_keeps_refusing_stops_after_max_attempts_and_is_reported(tmp_path: Path) -> None:
    index, store, offset_file, outbox = _state(tmp_path)
    flaky = _FlakyTelegram([_update(1, "placed 1 @2.05 5")], failures=MAX_ANSWER_ATTEMPTS)
    for _ in range(MAX_ANSWER_ATTEMPTS):
        with pytest.raises(TelegramError):
            poll(flaky, index, store, offset_file, NOW, outbox=outbox)
    flaky.updates.append(_update(2, "placed 2 over @1.75 2"))
    assert poll(flaky, index, store, offset_file, NOW, outbox=outbox) == 1  # the new answer is not held back
    assert [reply_to for _, reply_to in flaky.sent] == [902]
    assert len(outbox.abandoned()) == 1
    heartbeat = tmp_path / "beat.json"
    heartbeat.write_text(json.dumps({"at": NOW.isoformat()}), encoding="utf-8")
    assert any("1 answer(s) to your bet replies failed 3 sends" in line
               for line in status.poll_section(heartbeat, outbox, NOW))


# ---------------- gap 2: a reply to an alert whose send was never confirmed ----------------
def test_a_reply_to_an_unconfirmed_alert_records_the_bet_and_confirms_the_delivery(tmp_path: Path) -> None:
    index, store, offset_file, outbox = _state(tmp_path)
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    text = "PAPER ONLY\nHIGH-VALUE BETS 1-1 of 1\n\n1. Walsall v Port Vale"
    log.begin("digest", text, [ALERT], NOW + timedelta(hours=2), NOW - timedelta(hours=1))  # send timed out
    reply = _update(1, "placed 1 @2.05 5", reply_to=777)
    reply["message"]["reply_to_message"]["text"] = text + "\n"  # Telegram quotes the message replied to
    stranger = _update(2, "placed 1 @2.05 5", reply_to=778, chat="999")
    stranger["message"]["reply_to_message"]["text"] = text
    before = _FakeTelegram([reply])
    poll(before, MessageIndex(tmp_path / "old_index.jsonl"), PlacedBetStore(tmp_path / "old.jsonl"),
         tmp_path / "old_offset.json", NOW)
    assert "not an alert or board I sent" in before.sent[0][0]  # the old behaviour: the bet was refused
    fake = _FakeTelegram([reply, stranger])
    poll(fake, index, store, offset_file, NOW, outbox=outbox, deliveries=log)
    assert fake.sent[0][0].startswith("Recorded bet #1") and len(store.open_bets(NOW)) == 1
    confirmed = log.load(NOW)[1]
    assert (confirmed.state, confirmed.resolution, confirmed.message_id) == ("confirmed", "confirmed_by_reply", 777)
    assert index.load()[777] == {1: ALERT} and 778 not in index.load()  # another chat proves nothing


# ---------------- gap 3: edited replies were not even fetched ----------------
def test_an_edited_bet_reply_is_answered_and_never_applied(tmp_path: Path) -> None:
    index, store, offset_file, outbox = _state(tmp_path)
    edit = {"update_id": 2, "edited_message": {"message_id": 901, "chat": {"id": int(CHAT)}, "text": "placed 1 @2.10 5"}}
    other = {"update_id": 3, "edited_message": {"message_id": 905, "chat": {"id": 999}, "text": "placed 1 @9 5"}}
    fake = _FakeTelegram([_update(1, "placed 1 @2.05 5"), edit, other])
    poll(fake, index, store, offset_file, NOW, outbox=outbox)
    assert fake.sent[1] == (EDIT_NOTICE, 901) and len(fake.sent) == 2
    [bet] = store.open_bets(NOW)
    assert bet.odds == 2.05  # the edit changed nothing


def test_get_updates_asks_telegram_for_edited_messages() -> None:
    session = FakeSession(_ok([]))
    TelegramNotifier(TelegramConfig(FAKE_TOKEN, CHAT), session=session, sleep=lambda s: None).get_updates(None)
    assert session.calls[0][1]["allowed_updates"] == ["message", "edited_message"]


# ---------------- gap 4: a repeated reply silently doubled a position ----------------
def test_an_identical_open_bet_is_recorded_but_flagged_and_same_batch_bets_are_visible(tmp_path: Path) -> None:
    index, store, offset_file, outbox = _state(tmp_path)
    fake = _FakeTelegram([_update(1, "placed 1 @2.05 5"), _update(2, "placed 1 @2.05 5"), _update(3, "undo 2")])
    poll(fake, index, store, offset_file, NOW, outbox=outbox)
    assert "POSSIBLE DUPLICATE of open bet #1" in fake.sent[1][0] and "undo 2" in fake.sent[1][0]
    assert fake.sent[2][0] == "Cancelled bet #2." and [b.bet_id for b in store.open_bets(NOW)] == [1]


# ---------------- gap 5: replies dropped by Telegram while the poller was not running ----------------
def test_status_warns_when_bet_replies_are_not_being_read(tmp_path: Path) -> None:
    outbox, heartbeat = AnswerOutbox(tmp_path / "ans.jsonl"), tmp_path / "beat.json"
    assert "no bet-reply poll has been recorded" in status.poll_section(heartbeat, outbox, NOW)[0]
    for age, warned, lost in ((timedelta(minutes=20), False, False), (timedelta(hours=2), True, False),
                              (timedelta(hours=30), True, True)):
        heartbeat.write_text(json.dumps({"at": (NOW - age).isoformat()}), encoding="utf-8")
        lines = status.poll_section(heartbeat, outbox, NOW)
        assert bool(lines) == warned and any("Telegram drops a reply" in line for line in lines) == lost
    index, store, offset_file, _ = _state(tmp_path)
    poll(_FakeTelegram([]), index, store, offset_file, NOW, heartbeat_file=heartbeat)
    assert status.poll_section(heartbeat, outbox, NOW) == []  # every poll that reads Telegram records the time


# ---------------- gap 6: messages over Telegram's limit were cut silently ----------------
def test_a_long_plain_message_is_split_at_line_breaks_never_truncated() -> None:
    lines = [f"line {i:04d} " + "x" * 80 for i in range(120)]
    session = FakeSession(*(_ok({"message_id": 10 + i}) for i in range(5)))
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, CHAT), session=session, sleep=lambda s: None)
    assert notifier.send_message("\n".join(lines), reply_to=55) == 10 + len(session.calls) - 1
    sent = [call[1] for call in session.calls]
    assert len(sent) == 3 and all(len(p["text"]) <= TELEGRAM_MAX_TEXT for p in sent)
    assert "\n".join(p["text"] for p in sent).split("\n") == lines  # nothing dropped
    assert "reply_parameters" in sent[0] and all("reply_parameters" not in p for p in sent[1:])
    assert split_text("a" * 9000, 4096) == ["a" * 4096, "a" * 4096, "a" * 808]


def test_an_alert_message_over_the_limit_is_refused_before_anything_is_recorded(tmp_path: Path) -> None:
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    session = FakeSession()
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, CHAT), session=session, sleep=lambda s: None, deliveries=log)
    with pytest.raises(ValueError, match="Telegram takes 4096"):
        notifier.send_tracked("x" * (TELEGRAM_MAX_TEXT + 1), "board", [BOARD], NOW + timedelta(hours=1))
    assert session.calls == [] and not log.path.exists()


def test_a_board_of_long_names_fits_every_message_and_keeps_every_row() -> None:
    kickoff = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
    fixture = UpcomingFixture(1, "League Two", "1", "2", "Accrington Stanley", "Milton Keynes Dons", kickoff)
    squad = tuple(SquadPlayer(i, f"Christopherson Montgomery-Abernathy Junior{i:02d}", "MID", "1" if i % 2 else "2")
                  for i in range(1, 30))
    entries = [FairPrice(1, p.player_id, market, 1.5, side, 0.48, 2.08, 2.24)
               for p in squad for market in ("tackles", "fouls") for side in ("over", "under")]
    messages = scan.format_board(fixture, scan.SlateInputs(squad, {}, {}, {}), entries)
    assert all(len(text) <= TELEGRAM_MAX_TEXT for text, _ in messages)  # 25 such rows used to need 4,270
    assert [item.number for _, items in messages for item in items] == list(range(1, 59))
    assert all(len(items) <= scan.BOARD_ROWS_PER_MESSAGE for _, items in messages)
    assert len(RESENT_HEADER) < TELEGRAM_MAX_TEXT  # a re-send adds a separate message, never longer text


# ---------------- gap 7: a retried run repeated board messages already sent ----------------
def test_a_board_message_already_recorded_is_not_sent_again(tmp_path: Path) -> None:
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    session = FakeSession(_ok({"message_id": 5}), _ok({"message_id": 6}))
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, CHAT), session=session, sleep=lambda s: None, deliveries=log)
    kickoff = NOW + timedelta(hours=3)
    assert notifier.send_tracked("FAIR-PRICE BOARD part 1", "board", [BOARD], kickoff) == 5
    assert notifier.send_tracked("FAIR-PRICE BOARD part 1", "board", [BOARD], kickoff) is None
    assert notifier.send_tracked("FAIR-PRICE BOARD part 2", "board", [BOARD], kickoff) == 6
    assert len(session.calls) == 2


# ---------------- delivery audit rerun (2026-10-05, second pass) ----------------
def test_a_message_stopped_between_send_and_confirmation_still_takes_bet_replies(tmp_path: Path) -> None:
    """The message was indexed only after its delivery was confirmed: stopped in between, it was confirmed but not
    indexed, so every reply was refused and the quoted-text recovery (unconfirmed deliveries only) skipped it."""
    log, index = DeliveryLog(tmp_path / "deliveries.jsonl"), MessageIndex(tmp_path / "messages.jsonl")
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, CHAT), session=FakeSession(_ok({"message_id": 77})),
                                sleep=lambda s: None, index=index, deliveries=log)

    def stopped(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt("process stopped")

    log.resolve = stopped  # type: ignore[method-assign]
    # The notifier stamps deliveries with the real clock, so the times here come from it too: a fixed date in the
    # test (it was 2026-10-10 12:00) stops working once the real clock passes it (found 2026-10-10 at 13:59 UTC).
    real_now = datetime.now(timezone.utc)
    with pytest.raises(KeyboardInterrupt):
        notifier.send_tracked("FAIR-PRICE BOARD\n1. B (Walsall)", "board", [BOARD], real_now + timedelta(hours=3))
    assert index.load()[77] == {2: BOARD}  # a "placed 2 under @..." reply to message 77 is recorded
    assert DeliveryLog(tmp_path / "deliveries.jsonl").needs_review(real_now + timedelta(hours=1))  # and it is reviewed


def test_two_ledger_writers_cannot_run_at_once_and_a_finished_one_releases_the_lock(tmp_path: Path) -> None:
    """A manual --poll during the scheduled one could read the same update in both and record one bet twice."""
    from prop_alpha.alerts.ledger import ledger_lock

    lock = tmp_path / "ledger.lock"
    with ledger_lock(lock):
        with pytest.raises(OSError):
            with ledger_lock(lock):
                pytest.fail("a second writer must not get the lock")
    with ledger_lock(lock):  # released: the next run proceeds
        pass


def test_a_simulated_run_time_is_refused_with_real_odds(capsys: pytest.CaptureFixture[str]) -> None:
    """--at with --odds oddspapi sent real alerts into the real store and archived real prices under the simulated
    time; --at is for mock runs only."""
    with pytest.raises(SystemExit) as refused:
        scan.main(["--odds", "oddspapi", "--at", "2026-10-10T12:00:00+00:00"])
    assert refused.value.code == 2 and "works only with --odds mock" in capsys.readouterr().err


# ---------------- third pass: a settlement notice lost after the bets were settled ----------------
def test_a_settlement_notice_that_failed_to_send_is_queued_and_resent_by_the_next_poll(tmp_path: Path) -> None:
    """--settle --send records each settlement, then sends BETS SETTLED. Before this, a failed send was never repeated:
    the next run finds nothing new to settle, so the maintainer never saw those automatic results to check."""
    from prop_alpha.alerts.ledger import DELAYED_NOTICE_PREFIX, SETTLED_HEADER, send_queued_notice

    index, store, offset_file, outbox = _state(tmp_path)
    flaky = _FlakyTelegram([], failures=1)
    notice = f"{SETTLED_HEADER}\n#1 WON (provisional) +5.25: A Player"
    with pytest.raises(TelegramError):
        send_queued_notice(flaky, outbox, notice, NOW)
    assert [(reply_to, text, attempts) for _, reply_to, text, attempts in outbox.unsent()] == [(None, notice, 1)]
    assert poll(flaky, index, store, offset_file, NOW, outbox=outbox) == 1
    assert flaky.sent == [(DELAYED_NOTICE_PREFIX + notice, None)]  # a notice, not a reply to any message
    assert outbox.unsent() == []
    assert send_queued_notice(flaky, outbox, notice, NOW) > 0 and outbox.unsent() == []  # sent at once: nothing queued


def test_a_settlement_notice_telegram_keeps_refusing_is_reported_not_retried_forever(tmp_path: Path) -> None:
    from prop_alpha.alerts.ledger import send_queued_notice

    index, store, offset_file, outbox = _state(tmp_path)
    flaky = _FlakyTelegram([], failures=MAX_ANSWER_ATTEMPTS)
    with pytest.raises(TelegramError):
        send_queued_notice(flaky, outbox, "BETS SETTLED\n#1 LOST", NOW)
    for _ in range(MAX_ANSWER_ATTEMPTS - 1):
        with pytest.raises(TelegramError):
            poll(flaky, index, store, offset_file, NOW, outbox=outbox)
    assert poll(flaky, index, store, offset_file, NOW, outbox=outbox) == 0 and flaky.sent == []
    [(_, reply_to, _, attempts)] = outbox.abandoned()
    assert reply_to is None and attempts == MAX_ANSWER_ATTEMPTS
    heartbeat = tmp_path / "beat.json"
    heartbeat.write_text(json.dumps({"at": NOW.isoformat()}), encoding="utf-8")
    assert any("settlement notices included" in line for line in status.poll_section(heartbeat, outbox, NOW))


def test_the_settle_command_queues_its_notice_before_sending() -> None:
    import inspect

    from prop_alpha.alerts import ledger

    source = inspect.getsource(ledger.main)
    settle_branch = source[source.index("if args.settle:"):source.index("if args.reconcile:")]
    assert "send_queued_notice(" in settle_branch and ".send_message(" not in settle_branch
