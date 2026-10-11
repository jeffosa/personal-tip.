"""Alert-delivery tracking and the operator review path (production-readiness item 6, 2026-10-05).

Before this, an alert was written to the sent-alert store and then sent; a failed or cut-off send left nothing to
tell a delivered alert from a missed one, and no way to re-send one without risking a duplicate."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

from prop_alpha.alerts import delivery, status
from prop_alpha.alerts.replies import AlertItem
from prop_alpha.alerts.telegram import (
    DEFAULT_CRITERIA,
    PENDING_GRACE,
    DeliveryLog,
    MessageIndex,
    SentAlertStore,
    TelegramConfig,
    TelegramError,
    TelegramNotifier,
    demo_case,
)
from prop_alpha.scanner.market_scanner import AllocationReport
from prop_alpha.scanner.value_screen import assess_high_value
from test_alerts import FAKE_TOKEN, NOW, FakeSession, _ok

ITEM = AlertItem(number=1, kind="board", fixture_id=9001, player_id=7, market="tackles", line=1.5,
                 min_odds=(("over", 2.1), ("under", 1.9)), match="Walsall v Port Vale", player="A Player (Walsall)")


def _later() -> datetime:
    """A review time after every send of the test has passed PENDING_GRACE."""
    return datetime.now(timezone.utc) + PENDING_GRACE + timedelta(seconds=1)


def _tracked(tmp_path: Path, n_bets: int, *responses: Any) -> tuple[TelegramNotifier, FakeSession, DeliveryLog,
                                                                         MessageIndex, SentAlertStore]:
    """A notifier with every state file, whose screen passes `n_bets` copies of the demo's high-value bet, valid for
    two hours from now."""
    projections, report = demo_case(NOW)
    base = next(d for d in report.directives if d.line_value == 4.5)
    assessment = assess_high_value(base, projections[(1001, 55, "tackles")], DEFAULT_CRITERIA, np.random.default_rng(1))
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    bets = [(replace(base, player_id=100 + i, expires_at=expires), assessment) for i in range(n_bets)]
    session = FakeSession(*responses)
    log, index = DeliveryLog(tmp_path / "deliveries.jsonl"), MessageIndex(tmp_path / "messages.jsonl")
    store = SentAlertStore(tmp_path / "sent.jsonl")
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=session, store=store,
                                sleep=lambda seconds: None, index=index, deliveries=log)
    notifier._high_value = lambda *args: bets  # type: ignore[method-assign]
    return notifier, session, log, index, store


def test_a_confirmed_send_records_telegrams_message_id(tmp_path: Path) -> None:
    notifier, _, log, index, _ = _tracked(tmp_path, 7, _ok({"message_id": 50}), _ok({"message_id": 51}))
    notifier.notify_digest(AllocationReport(), {})
    deliveries = log.load(_later())
    assert [(d.kind, d.state, d.message_id, len(d.items)) for d in deliveries.values()] == [
        ("digest", "confirmed", 50, 5), ("digest", "confirmed", 51, 2)]
    assert set(index.load()) == {50, 51} and log.needs_review(_later()) == []


def test_a_failed_send_leaves_an_unknown_outcome_for_review_and_is_never_resent(tmp_path: Path) -> None:
    notifier, _, log, _, store = _tracked(tmp_path, 7, _ok({"message_id": 50}), requests.ReadTimeout("timed out"))
    with pytest.raises(TelegramError):
        notifier.notify_digest(AllocationReport(), {})
    assert log.load(datetime.now(timezone.utc))[2].state == "sending"  # within PENDING_GRACE: may still be in flight
    unknown = log.needs_review(_later())
    assert [(d.delivery_id, d.state, d.message_id) for d in unknown] == [(2, "unknown", None)]
    assert unknown[0].text.startswith("TACKLES \u00b7 2/2") and len(unknown[0].items) == 2
    assert len(store.load()) == 7  # still counted as exposure, as before
    restarted = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=FakeSession(), store=store,
                                 sleep=lambda seconds: None, deliveries=log)
    assert len(restarted._sent) == 7  # the next run treats all 7 as alerted: no automatic duplicate


def test_the_delivery_record_is_written_before_the_alert_store_and_the_send(tmp_path: Path) -> None:
    seen: list[tuple[str, int]] = []
    notifier, session, log, _, store = _tracked(tmp_path, 1)

    def post(url: str, json: dict[str, Any], timeout: float) -> Any:
        seen.append((log.load(datetime.now(timezone.utc))[1].state, len(store.load())))
        return _ok({"message_id": 70})

    session.post = post  # type: ignore[method-assign]
    notifier.notify_digest(AllocationReport(), {})
    assert seen == [("sending", 1)] and log.load(_later())[1].state == "confirmed"


def test_resend_is_labelled_indexed_and_confirmed_once(tmp_path: Path) -> None:
    notifier, _, log, index, _ = _tracked(tmp_path, 2, requests.ConnectionError("reset"))
    with pytest.raises(TelegramError):
        notifier.notify_digest(AllocationReport(), {})
    session = FakeSession(_ok({"message_id": 89}), _ok({"message_id": 90}))
    sender = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=session, sleep=lambda seconds: None,
                              index=index)
    assert delivery.resend(sender, log, 1, _later()) == 90
    # The header goes on its own, so the copy is exactly the original text and still fits one message.
    header, text = (call[1]["text"] for call in session.calls)
    assert header == delivery.RESENT_HEADER and text == log.load(_later())[1].text
    assert text.startswith("TACKLES \u00b7 1/1\n") and "\n1a " in text and "\n1b " in text
    resolved = log.load(_later())[1]
    assert (resolved.state, resolved.resolution, resolved.message_id) == ("confirmed", "resent", 90)
    assert sorted(index.load()[90]) == [1, 2]  # a "placed 2 @..." reply to the copy records the bet
    with pytest.raises(ValueError, match="only an unknown outcome"):
        delivery.resend(sender, log, 1, _later())


def test_an_expired_delivery_cannot_be_resent_but_can_be_dismissed_or_confirmed(tmp_path: Path) -> None:
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    began = datetime.now(timezone.utc) - timedelta(hours=3)
    for _ in range(2):
        log.begin("board", "FAIR-PRICE BOARD\n1. A Player", [ITEM], began + timedelta(hours=1), began)
    session = FakeSession()
    sender = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=session, sleep=lambda s: None)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="expired"):
        delivery.resend(sender, log, 1, now)
    assert session.calls == []
    delivery.dismiss(log, 1, now)
    delivery.mark_delivered(log, 2, now)
    states = {d.delivery_id: (d.state, d.resolution) for d in log.load(now).values()}
    assert states == {1: ("dismissed", "dismissed"), 2: ("confirmed", "confirmed_by_operator")}
    assert delivery.review_lines(log, now)[-1] == "2 deliveries recorded; 0 with an unknown outcome"
    with pytest.raises(ValueError, match="no delivery #3"):
        delivery.mark_delivered(log, 3, now)


def test_review_lists_only_unknown_deliveries_by_default(tmp_path: Path) -> None:
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    began = datetime.now(timezone.utc) - timedelta(hours=1)
    log.begin("board", "PAPER ONLY - x\nFAIR-PRICE BOARD - no line\n1. A Player", [ITEM], began + timedelta(hours=4),
              began)
    log.begin("board", "FAIR-PRICE BOARD second", [ITEM], began + timedelta(hours=4), began)
    log.resolve(2, "delivered", began, 77)
    lines = delivery.review_lines(log, datetime.now(timezone.utc))
    assert len(lines) == 2 and lines[0].startswith("#1 UNKNOWN: board with 1 bet(s)")
    assert lines[0].endswith("FAIR-PRICE BOARD - no line") and "check the chat" in lines[-1]
    assert len(delivery.review_lines(log, datetime.now(timezone.utc), everything=True)) == 3


def test_a_tracked_board_send_is_confirmed_and_indexed(tmp_path: Path) -> None:
    log, index = DeliveryLog(tmp_path / "deliveries.jsonl"), MessageIndex(tmp_path / "messages.jsonl")
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=FakeSession(_ok({"message_id": 5})),
                                sleep=lambda s: None, index=index, deliveries=log)
    kickoff = datetime.now(timezone.utc) + timedelta(hours=3)
    assert notifier.send_tracked("FAIR-PRICE BOARD\n1. A Player", "board", [ITEM], kickoff) == 5
    board = log.load(_later())[1]
    assert (board.state, board.message_id, board.valid_until, board.items) == ("confirmed", 5, kickoff, (ITEM,))
    assert index.load()[5] == {1: ITEM}


def test_delivery_records_are_validated_and_a_corrupt_log_fails_closed(tmp_path: Path) -> None:
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    at = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        log.begin("summary", "text", [], at, at)  # not an alert-bearing kind
    with pytest.raises(ValueError):
        log.begin("board", "   ", [], at, at)
    assert not log.path.exists()  # nothing written for a refused record
    log.begin("board", "text", [], at, at)
    with pytest.raises(ValueError, match="needs"):
        log.resolve(1, "delivered", at)
    with pytest.raises(ValueError, match="takes no"):
        log.resolve(1, "dismissed", at, 4)
    with pytest.raises(ValueError, match="not a delivery outcome"):
        log.resolve(1, "pending", at)
    for bad in ("not json", json.dumps({"delivery_id": "1", "event": "pending", "at": at.isoformat()}),
                json.dumps({"delivery_id": 9, "event": "delivered", "at": at.isoformat(), "message_id": 1})):
        corrupt = DeliveryLog(tmp_path / "corrupt.jsonl")
        corrupt.path.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            corrupt.load(at)


def test_status_report_warns_about_unknown_deliveries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = DeliveryLog(tmp_path / "deliveries.jsonl")
    began = datetime.now(timezone.utc) - timedelta(hours=1)
    log.begin("digest", "HIGH-VALUE BETS 1-1 of 1", [ITEM], began + timedelta(hours=2), began)
    assert status.delivery_section(log, datetime.now(timezone.utc)) == [
        "WARNING: 1 alert message(s) with an unknown delivery outcome (#1): python -m prop_alpha.alerts.delivery"]
    log.resolve(1, "confirmed_by_operator", datetime.now(timezone.utc))
    assert status.delivery_section(log, datetime.now(timezone.utc)) == []


def test_delivery_state_is_gitignored() -> None:
    root = Path(__file__).resolve().parents[1]
    assert ".prop_alpha_state/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
