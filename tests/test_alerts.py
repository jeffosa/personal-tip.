from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

from prop_alpha.alerts.telegram import (
    PAPER_LABEL,
    TelegramConfig,
    TelegramError,
    TelegramNotifier,
    demo_case,
    format_directive,
    load_config,
    parse_env_file,
    reference_projection,
    reference_report,
)
from prop_alpha.scanner.value_screen import HighValueCriteria, assess_high_value

# Assembled at runtime so the repository secrets scan never sees a token-shaped literal.
FAKE_TOKEN = "123456789" + ":" + "AAH" + "x" * 32
NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append((url, json))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _ok(result: Any) -> FakeResponse:
    return FakeResponse(200, {"ok": True, "result": result})


def _notifier(*responses: FakeResponse | Exception) -> tuple[TelegramNotifier, FakeSession]:
    session = FakeSession(*responses)
    return TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=session), session


def test_env_file_parsing_handles_comments_quotes_and_export() -> None:
    text = "# credentials\n\nexport TELEGRAM_BOT_TOKEN='abc'\nTELEGRAM_CHAT_ID = \"42\"\nOTHER=x=y\n"
    assert parse_env_file(text) == {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42", "OTHER": "x=y"}
    with pytest.raises(ValueError, match="line 1"):
        parse_env_file("not a pair\n")


def test_config_prefers_environment_and_hides_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID=111\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "222")
    config = load_config(env_file)
    assert config.chat_id == "222"
    assert FAKE_TOKEN not in repr(config) and FAKE_TOKEN not in str(config)


def test_config_rejects_missing_or_malformed_credentials() -> None:
    for token in ("", "not-a-token", "123:short"):
        with pytest.raises(ValueError):
            TelegramConfig(token, "1")
    with pytest.raises(ValueError):
        TelegramConfig(FAKE_TOKEN, "chat one")
    assert TelegramConfig.from_sources({}, f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n").chat_id is None


def test_alert_text_carries_the_paper_label_and_the_screen_evidence() -> None:
    projections, report = demo_case(NOW)
    directive = next(d for d in report.directives if d.line_value == 4.5)
    assessment = assess_high_value(directive, projections[(1001, 55, "tackles")], HighValueCriteria(),
                                   np.random.default_rng(1))
    text = format_directive(directive, assessment, "GBP")
    assert text.startswith(PAPER_LABEL)
    assert "HIGH-VALUE BET" in text and "UNDER 4.5" in text and f"GBP {directive.stake:.2f}" in text
    assert f"{assessment.surviving_simulations} of 1000 simulated match conditions" in text
    assert "Expected profit +8.00% per 1 staked" in text


def test_check_connection_and_send_message_call_the_bot_api() -> None:
    notifier, session = _notifier(_ok({"username": "Jeff_tips_bot"}), _ok({"message_id": 7}))
    assert notifier.check_connection() == "Jeff_tips_bot"
    assert notifier.send_message("hello") == 7
    assert session.calls[0][0].endswith("/getMe")
    assert session.calls[1][1] == {"chat_id": "987654321", "text": "hello", "disable_web_page_preview": True}


def test_notify_sends_only_high_value_bets_and_each_once() -> None:
    projections, report = demo_case(NOW)
    assert len(report.directives) == 2
    notifier, session = _notifier(_ok({"message_id": 1}))
    assert notifier.notify(report, projections) == [1]
    assert notifier.notify(report, projections) == []
    assert len(session.calls) == 1
    assert "UNDER 4.5" in session.calls[0][1]["text"]


def test_reference_bets_are_screened_out() -> None:
    report = reference_report(NOW)
    notifier, session = _notifier()
    assert notifier.notify(report, {(1001, 55, "tackles"): reference_projection()}) == []
    assert session.calls == []


def test_stricter_criteria_and_sub_minimum_stakes_send_nothing() -> None:
    projections, report = demo_case(NOW)
    strict, strict_session = _notifier()
    assert strict.notify(report, projections, HighValueCriteria(min_ev_per_unit=0.09)) == []
    assert strict_session.calls == []
    report.skipped_directives.extend(report.directives)
    report.directives.clear()
    quiet, quiet_session = _notifier()
    assert quiet.notify(report, projections) == [] and quiet_session.calls == []


def test_directive_without_projection_cannot_be_screened() -> None:
    _, report = demo_case(NOW)
    notifier, _ = _notifier()
    with pytest.raises(ValueError, match="cannot be screened"):
        notifier.notify(report, {})


def test_telegram_rejection_raises_without_the_token() -> None:
    notifier, _ = _notifier(FakeResponse(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}))
    with pytest.raises(TelegramError, match="Unauthorized") as caught:
        notifier.check_connection()
    assert FAKE_TOKEN not in str(caught.value)


def test_transport_errors_are_sanitised() -> None:
    leaking = requests.ConnectionError(f"Max retries exceeded with url: /bot{FAKE_TOKEN}/sendMessage")
    notifier, _ = _notifier(leaking)
    with pytest.raises(TelegramError) as caught:
        notifier.send_message("hello")
    assert FAKE_TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_sending_without_a_chat_id_is_refused() -> None:
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN), session=FakeSession())
    with pytest.raises(ValueError, match="--get-chat-id"):
        notifier.send_message("hello")


def test_recent_chats_lists_senders() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 555, "first_name": "Jeff"}}},
        {"update_id": 2, "message": {"chat": {"id": 555, "first_name": "Jeff"}}},
    ]
    notifier, _ = _notifier(_ok(updates))
    assert notifier.recent_chats() == [("555", "Jeff")]


def test_env_file_is_gitignored() -> None:
    ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored
