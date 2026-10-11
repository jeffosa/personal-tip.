from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

from prop_alpha.alerts.replies import USAGE, PlacedBet, PlaceCommand, ReplyError, parse_reply, resolve_bet
from prop_alpha.alerts.telegram import (
    AlertContext,
    DEFAULT_CRITERIA,
    EXPERIMENT_FOOTNOTE,
    TIP_TARGET_CHARS,
    MIN_SEND_INTERVAL_S,
    OPEN_POSITION_WINDOW,
    PAPER_LABEL,
    SentAlertStore,
    TelegramConfig,
    TelegramError,
    TelegramNotifier,
    _reference_manager,
    alert_key,
    demo_case,
    digest_items,
    split_tips,
    format_digest,
    format_directive,
    load_config,
    parse_env_file,
    reference_projection,
    reference_report,
    simulation_seed,
)
from prop_alpha.modeling.engine import VoidRule
from prop_alpha.scanner.market_scanner import (
    AllocationReport,
    ExposureBook,
    LiveOddsQuote,
    StakingDirective,
    price_for_ev,
)
from prop_alpha.scanner.value_screen import HighValueAssessment, HighValueCriteria, assess_high_value

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
    return TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=session, sleep=lambda s: None), session


def _flood(retry_after: float) -> FakeResponse:
    return FakeResponse(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                              "parameters": {"retry_after": retry_after}})


def test_telegram_rate_limit_is_waited_out_once() -> None:
    """Audit H3: a burst hit Telegram's 429 and aborted the rest of the batch."""
    waits: list[float] = []
    session = FakeSession(_flood(3), _ok({"message_id": 9}))
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "1"), session=session, sleep=waits.append)
    assert notifier.send_message("hi") == 9 and waits == [3.0]
    twice = FakeSession(_flood(3), _flood(3))
    with pytest.raises(TelegramError, match="Too Many Requests"):
        TelegramNotifier(TelegramConfig(FAKE_TOKEN, "1"), session=twice, sleep=lambda s: None).send_message("hi")
    too_long = FakeSession(_flood(600))
    with pytest.raises(TelegramError, match="Too Many Requests"):
        TelegramNotifier(TelegramConfig(FAKE_TOKEN, "1"), session=too_long, sleep=lambda s: None).send_message("hi")


def test_messages_are_spaced_to_telegram_per_chat_limit() -> None:
    waits: list[float] = []
    now = [100.0]
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "1"), session=FakeSession(_ok({"message_id": 1}),
                                _ok({"message_id": 2})), sleep=waits.append, clock=lambda: now[0])
    notifier.send_message("a")
    now[0] += 0.3
    notifier.send_message("b")
    assert waits == [pytest.approx(MIN_SEND_INTERVAL_S - 0.3)]


def test_malformed_env_line_does_not_block_alerts_configured_in_the_environment() -> None:
    environ = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "42"}
    assert TelegramConfig.from_sources(environ, "SPORTMONKS API TOKEN = oops\n").chat_id == "42"
    with pytest.raises(ValueError, match="line 1"):
        TelegramConfig.from_sources({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN}, "SPORTMONKS API TOKEN = oops\n")


def test_env_file_parsing_handles_comments_quotes_and_export() -> None:
    text = "# credentials\n\nexport TELEGRAM_BOT_TOKEN='abc'\nTELEGRAM_CHAT_ID = \"42\"\nOTHER=x=y\n"
    assert parse_env_file(text) == {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42", "OTHER": "x=y"}
    with pytest.raises(ValueError, match="line 1"):
        parse_env_file("not a pair\n")


def test_env_file_parsing_accepts_utf8_bom() -> None:
    assert parse_env_file("\ufeffODDSPAPI_KEY=example\n") == {"ODDSPAPI_KEY": "example"}


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
    assessment = assess_high_value(directive, projections[(1001, 55, "tackles")], DEFAULT_CRITERIA,
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


def test_a_new_run_does_not_resend_and_sizes_net_of_earlier_alerts(tmp_path: Path) -> None:
    """C3: run 2 is a fresh process. Before the store, it re-sent the same bet at full stake."""
    store = SentAlertStore(tmp_path / "state" / "sent_alerts.jsonl")
    projections, report = demo_case(NOW)
    first = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=FakeSession(_ok({"message_id": 1})),
                             store=store)
    assert first.notify(report, projections) == [1]
    sent = next(d for d in report.directives if d.line_value == 4.5)

    second_session = FakeSession()
    second = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=second_session, store=store)
    assert second.notify(report, projections) == [] and second_session.calls == []

    now = datetime.now(timezone.utc)
    book = store.exposure(now)
    assert book.market_total((1001, 55, "tackles")) == pytest.approx(sent.stake)
    projection = projections[(1001, 55, "tackles")]
    quote = LiveOddsQuote(1001, 55, "tackles", 4.5, None, price_for_ev(projection.side_probabilities(4.5, "under"), 0.08),
                          500.0, "SyntheticBookC", now, VoidRule.VOID_IF_NO_APPEARANCE)
    fresh = _reference_manager().allocate(projections, [quote], now=now, exposure=ExposureBook({}))
    netted = _reference_manager().allocate(projections, [quote], now=now, exposure=book)
    fresh_stake = sum(d.stake for d in fresh.directives)
    assert fresh_stake > 0.0 and sum(d.stake for d in netted.directives) < fresh_stake
    assert store.exposure(now + OPEN_POSITION_WINDOW + timedelta(seconds=1)).total == 0.0


def test_alert_is_recorded_before_sending_so_a_failed_send_is_not_repeated(tmp_path: Path) -> None:
    store = SentAlertStore(tmp_path / "sent_alerts.jsonl")
    projections, report = demo_case(NOW)
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"),
                                session=FakeSession(requests.ConnectionError("down")), store=store)
    with pytest.raises(TelegramError):
        notifier.notify(report, projections)
    assert len(store.load()) == 1


def test_corrupt_alert_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "sent_alerts.jsonl"
    for bad in ('{"key": [1, 2, "tackles", 1.5, "over"], "stake": 1, "sent_at": "2026-01-01T00:00:00+00:00"}',
                '{"key": [1, 2, "tackles", 1.5, "sideways", "A"], "stake": 1, "sent_at": "2026-01-01T00:00:00+00:00"}',
                '{"key": [1, 2, "tackles", 1.5, "over", "A"], "stake": -1, "sent_at": "2026-01-01T00:00:00+00:00"}',
                "not json"):
        path.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            SentAlertStore(path).load()


def test_alert_state_is_gitignored() -> None:
    assert ".prop_alpha_state/" in (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")


def test_reference_bets_are_screened_out() -> None:
    report = reference_report(NOW)
    notifier, session = _notifier()
    assert notifier.notify(report, {(1001, 55, "tackles"): reference_projection()}) == []
    assert session.calls == []


def test_stricter_criteria_and_sub_minimum_stakes_send_nothing() -> None:
    projections, report = demo_case(NOW)
    strict, strict_session = _notifier()
    assert strict.notify(report, projections, HighValueCriteria(loss_floor=DEFAULT_CRITERIA.loss_floor, min_ev_per_unit=0.09)) == []
    assert strict_session.calls == []
    report.skipped_directives.extend(report.directives)
    report.directives.clear()
    quiet, quiet_session = _notifier()
    assert quiet.notify(report, projections) == [] and quiet_session.calls == []


def test_directive_without_projection_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Decision 5: one unscreenable directive no longer stops the rest of the report from being alerted."""
    projections, report = demo_case(NOW)
    orphan = replace(report.directives[0], player_id=999)
    report.directives.insert(0, orphan)
    notifier, session = _notifier(_ok({"message_id": 1}))
    with caplog.at_level(logging.ERROR, logger="prop_alpha.alerts"):
        assert notifier.notify(report, projections) == [1]
    assert "cannot be screened" in caplog.text and "999" in caplog.text
    assert "UNDER 4.5" in session.calls[0][1]["text"]


def test_a_price_tick_does_not_resend_the_same_market() -> None:
    """V2: two reports differing only in price (1.714, then 1.72) send one alert, not two."""
    projections, report = demo_case(NOW)
    under = next(d for d in report.directives if d.line_value == 4.5)
    report.directives[:] = [under]
    ticked = AllocationReport(directives=[replace(under, decimal_odds=round(under.decimal_odds + 0.006, 3))])
    notifier, session = _notifier(_ok({"message_id": 1}), _ok({"message_id": 2}))
    assert notifier.notify(report, projections) == [1]
    assert notifier.notify(ticked, projections) == []
    assert len(session.calls) == 1
    assert alert_key(under) == alert_key(ticked.directives[0])
    assert simulation_seed(under) != simulation_seed(ticked.directives[0])


def test_alert_text_shows_the_price_floor_and_expiry() -> None:
    """V3: a person acting on the alert sees the minimum acceptable odds and when the directive expires."""
    projections, report = demo_case(NOW)
    directive = next(d for d in report.directives if d.line_value == 4.5)
    assessment = assess_high_value(directive, projections[(1001, 55, "tackles")], DEFAULT_CRITERIA,
                                   np.random.default_rng(1))
    text = format_directive(directive, assessment)
    assert f"Do not bet below {directive.min_acceptable_odds:.3f}" in text
    assert "valid until 12:05:00 UTC" in text
    assert directive.min_acceptable_odds <= directive.decimal_odds


def test_a_stake_capped_by_an_assumed_limit_says_so_in_pounds() -> None:
    """Audit 2026-10-04: a quote with no published limit is capped at UNKNOWN_LIMIT_STAKE, which the alert must
    call an assumption, in the bankroll's currency."""
    projections, report = demo_case(NOW)
    directive = next(d for d in report.directives if d.line_value == 4.5)
    assessment = assess_high_value(directive, projections[(1001, 55, "tackles")], DEFAULT_CRITERIA,
                                   np.random.default_rng(1))
    assert directive.limit_published
    assumed = replace(directive, limit_published=False)
    for published, text in ((directive, format_directive(directive, assessment, "GBP")),
                            (assumed, format_directive(assumed, assessment, "GBP")),
                            (directive, format_digest([(directive, assessment, None)], 1, 1, "", "GBP")),
                            (assumed, format_digest([(assumed, assessment, None)], 1, 1, "", "GBP"))):
        assert f"GBP {published.stake:.2f}" in text
        assert ("book limit not published, assumed GBP 50" in text) is (not published.limit_published)


def test_quote_limit_flag_must_be_a_bool() -> None:
    with pytest.raises(ValueError, match="limit_published"):
        LiveOddsQuote(1, 2, "tackles", 1.5, 2.0, 1.8, 50.0, "bet365", NOW, VoidRule.VOID_IF_NO_APPEARANCE,
                      limit_published=0)  # type: ignore[arg-type]


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


def _digest_notifier(n_bets: int, tmp_path: Path) -> tuple[TelegramNotifier, FakeSession, list[Any]]:
    """A notifier whose screen passes `n_bets` distinct copies of the demo's high-value bet."""
    projections, report = demo_case(NOW)
    base = next(d for d in report.directives if d.line_value == 4.5)
    assessment = assess_high_value(base, projections[(1001, 55, "tackles")], DEFAULT_CRITERIA, np.random.default_rng(1))
    bets = [(replace(base, player_id=100 + i), assessment) for i in range(n_bets)]
    session = FakeSession(*(_ok({"message_id": 50 + i}) for i in range(5)))
    store = SentAlertStore(tmp_path / "sent.jsonl")
    notifier = TelegramNotifier(TelegramConfig(FAKE_TOKEN, "987654321"), session=session, store=store,
                                sleep=lambda seconds: None)
    notifier._high_value = lambda *args: bets  # type: ignore[method-assign]
    return notifier, session, bets


@pytest.mark.parametrize(("n_bets", "expected_messages"), [(3, [3]), (7, [5, 2]), (10, [5, 5])])
def test_digest_sends_at_most_five_bets_per_message(n_bets: int, expected_messages: list[int], tmp_path: Path) -> None:
    notifier, session, bets = _digest_notifier(n_bets, tmp_path)
    contexts = {(1001, 100 + i, "tackles"): AlertContext("Walsall v Port Vale, Sat 10 Oct 14:00 UTC",
                                                         f"Player {i} (Walsall)") for i in range(n_bets)}
    alerted, ids = notifier.notify_digest(AllocationReport(), {}, label="MOCK", descriptions=contexts)
    texts = [call[1]["text"] for call in session.calls]
    assert alerted == n_bets and len(ids) == len(texts) == len(expected_messages)
    assert [len(re.findall(r"^\d+[a-z] ", text, re.MULTILINE)) for text in texts] == expected_messages
    assert all(text.startswith("MOCK\nTACKLES \u00b7 ") and PAPER_LABEL in text for text in texts)
    if len(texts) > 1:
        assert texts[0].startswith("MOCK\nTACKLES \u00b7 1/2") and texts[1].startswith("MOCK\nTACKLES \u00b7 2/2")
    first = texts[0]
    assert "\nWalsall vs Port Vale, Sat 10 Oct 14:00 UTC\n" in first  # no teams/kickoff given: the match line itself
    assert re.search(r"^1a Player 0 \u2014 UNDER 4\.5 tackles$", first, re.MULTILINE)  # the club is on the match line
    assert re.search(r"^   SyntheticBookC @ \d\.\d\d \| take it at ", first, re.MULTILINE)
    assert "HIGH-VALUE" not in first and "Edge:" not in first
    survival = bets[0][1].survival_rate
    assert f"edge held in {survival:.0%} of 1,000 simulated match conditions" in first
    assert first.count("simulated match conditions") == len(re.findall(r"^\d+[a-z] ", first, re.MULTILINE))
    assert len(SentAlertStore(tmp_path / "sent.jsonl").load()) == n_bets


def _tip_directives() -> tuple[StakingDirective, StakingDirective, StakingDirective]:
    _, report = demo_case(NOW)
    base = next(d for d in report.directives if d.line_value == 4.5)
    return (replace(base, fixture_id=1, player_id=11), replace(base, fixture_id=2, player_id=21),
            replace(base, fixture_id=1, player_id=12, side="over", line_value=0.5))


def test_tips_group_by_match_and_letter_each_bet() -> None:
    a, b, c = _tip_directives()
    contexts = {1: AlertContext("Team A v Team B, Sat 10 Oct 14:00 UTC", "Player A (Team A)"),
                2: AlertContext("Team C v Team D, Sat 10 Oct 15:00 UTC", "Player C (Team C)")}
    entries = [(a, None, contexts[1]), (b, None, contexts[2]), (c, None, AlertContext(contexts[1].match, "Player B (Team B)"))]
    text = format_digest(entries, 1, 1, "", "GBP")
    lines = text.splitlines()
    assert lines[0] == "TACKLES \u00b7 1/1"
    assert lines.index("Team A vs Team B, Sat 10 Oct 14:00 UTC") < lines.index("Team C vs Team D, Sat 10 Oct 15:00 UTC")
    # The third bet is the first match's second bet, so it is shown under match 1 as 1b, ahead of match 2's 2a.
    assert [line.split(" ")[0] for line in lines if re.match(r"^\d+[a-z] ", line)] == ["1a", "1b", "2a"]
    assert "\n1b Player B \u2014 OVER 0.5 tackles\n" in text and text.index("\n1b ") < text.index("\n2a ")
    items = digest_items(entries)
    assert [(i.number, i.label, i.player_id, tuple(i.sides)) for i in items] == [
        (1, "1a", 11, ("under",)), (2, "1b", 12, ("over",)), (3, "2a", 21, ("under",))]
    assert text.endswith(USAGE) and PAPER_LABEL in text


def test_each_tip_shows_its_survival_percentage_and_marks_the_model_numbers_experimental() -> None:
    a, b, _ = _tip_directives()
    passing = HighValueAssessment(0.08, 0.6, 0.5, True, True, 683, 1000, True)
    marginal = HighValueAssessment(0.05, 0.55, 0.5, True, True, 612, 1000, True)
    text = format_digest([(a, passing, None), (b, marginal, None), (replace(b, player_id=99), None, None)])
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("1a "))
    assert lines[first + 2] == "   model estimate 60%* | market implied 50% | EV estimate +8%*"
    assert lines[first + 3] == "   edge held in 68% of 1,000 simulated match conditions*"
    assert "   model estimate 55%* | market implied 50% | EV estimate +5%*" in lines
    assert "   edge held in 61% of 1,000 simulated match conditions*" in lines
    # An entry without an assessment shows no numbers rather than made-up ones.
    assert text.count("simulated match conditions") == 2 and text.count("model estimate") == 2
    assert text.count(EXPERIMENT_FOOTNOTE) == 1 and text.index(EXPERIMENT_FOOTNOTE) < text.index(PAPER_LABEL)
    assert EXPERIMENT_FOOTNOTE not in format_digest([(a, None, None)])  # nothing experimental is shown, nothing to footnote


def test_tip_messages_are_cut_by_size_at_complete_bets_and_every_part_repeats_its_warning(tmp_path: Path) -> None:
    """Maintainer, 2026-10-11: about 3,500 characters per message, never mid-bet, part number and paper-only line on each."""
    notifier, session, bets = _digest_notifier(5, tmp_path)
    assessment = bets[0][1]
    long_name = "A Very Long Player Name " * 24  # about 580 characters: five such bets cannot share one 3,500-character message
    contexts = {(1001, 100 + i, "tackles"): AlertContext("Walsall v Port Vale, Sat 10 Oct 14:00 UTC", f"{long_name}{i} (Walsall)")
                for i in range(5)}
    alerted, ids = notifier.notify_digest(AllocationReport(), {}, label="PILOT", descriptions=contexts)
    texts = [call[1]["text"] for call in session.calls]
    assert alerted == 5 and len(texts) == len(ids) > 1
    assert all(len(text) <= TIP_TARGET_CHARS for text in texts), [len(t) for t in texts]
    for number, text in enumerate(texts, start=1):
        assert text.splitlines()[1] == f"TACKLES \u00b7 {number}/{len(texts)}"
        assert PAPER_LABEL in text and text.endswith(USAGE)
        assert EXPERIMENT_FOOTNOTE in text  # the model's numbers are on every part, so is their warning
    shown = [line for text in texts for line in text.splitlines() if re.match(r"^\d+[a-z] ", line)]
    assert len(shown) == 5 and len(set(shown)) == 5  # every bet exactly once: none cut, none repeated
    assert len(SentAlertStore(tmp_path / "sent.jsonl").load()) == 5
    assert assessment.survival_rate > 0.6


def test_a_failing_tip_callback_leaves_nothing_recorded_and_nothing_sent(tmp_path: Path) -> None:
    """Audit 2026-10-11 (second pass): the callback ran after the alert store and the delivery record, so a failure left an
    alerted stake and a pending delivery for a tip that was never sent."""
    from prop_alpha.alerts.telegram import DeliveryLog

    notifier, session, _ = _digest_notifier(2, tmp_path)
    notifier.deliveries = DeliveryLog(tmp_path / "deliveries.jsonl")

    def refuse(tips: Any) -> None:
        raise OSError("paper ledger locked")

    with pytest.raises(OSError, match="locked"):
        notifier.notify_digest(AllocationReport(), {}, on_tips=refuse)
    assert session.calls == [] and SentAlertStore(tmp_path / "sent.jsonl").load() == []
    assert DeliveryLog(tmp_path / "deliveries.jsonl").load(NOW) == {}


def test_split_tips_respects_both_the_bet_count_and_the_size_and_keeps_order() -> None:
    a, b, c = _tip_directives()
    entries = [(replace(a, player_id=i), None, None) for i in range(7)]
    assert [len(chunk) for chunk in split_tips(entries, 3, lambda e: "x" * 10)] == [3, 3, 1]  # count cap
    sized = split_tips(entries, 5, lambda e: "x" * (100 * len(e)), limit=250)  # two bets fit, three do not
    assert [len(chunk) for chunk in sized] == [2, 2, 2, 1] and [e[0].player_id for chunk in sized for e in chunk] == list(range(7))
    assert [len(chunk) for chunk in split_tips(entries[:2], 5, lambda e: "x" * 9999, limit=100)] == [1, 1]  # one bet alone is never cut
    assert split_tips([], 5, lambda e: "") == []
    with pytest.raises(ValueError, match="positive"):
        split_tips(entries, 0, lambda e: "")


def test_a_tip_message_holds_one_market_and_names_fouls() -> None:
    a, _, _ = _tip_directives()
    fouls = replace(a, market_type="fouls")
    with pytest.raises(ValueError, match="one market"):
        format_digest([(a, None, None), (fouls, None, None)])
    assert format_digest([(fouls, None, None)]).startswith("FOULS \u00b7 1/1\n")
    assert "\u2014 UNDER 4.5 fouls" in format_digest([(fouls, None, None)])
    with pytest.raises(ValueError):
        format_digest([])


def test_notify_digest_sends_each_market_as_its_own_message(tmp_path: Path) -> None:
    notifier, session, bets = _digest_notifier(2, tmp_path)
    fouls = (replace(bets[1][0], market_type="fouls"), bets[1][1])
    notifier._high_value = lambda *args: [bets[0], fouls]  # type: ignore[method-assign]
    alerted, ids = notifier.notify_digest(AllocationReport(), {})
    texts = [call[1]["text"] for call in session.calls]
    assert alerted == 2 and len(ids) == 2
    assert texts[0].startswith("TACKLES \u00b7 1/1\n") and texts[1].startswith("FOULS \u00b7 1/1\n")
    assert "2." not in texts[0].splitlines()[2]  # each message numbers its own matches from 1


def test_a_reply_quoting_a_bet_label_resolves_the_bet() -> None:
    a, b, c = _tip_directives()
    entries = [(a, None, AlertContext("Team A v Team B, x", "Player A (Team A)")),
               (b, None, AlertContext("Team C v Team D, y", "Player C (Team C)")),
               (c, None, AlertContext("Team A v Team B, x", "Player B (Team B)"))]
    items = {item.number: item for item in digest_items(entries)}
    command = parse_reply("placed 1B @1.85 5")
    assert isinstance(command, PlaceCommand) and command.number == "1b"
    bet = resolve_bet(command, items, bet_id=1, update_id=1, message_id=9, placed_at=NOW)
    assert isinstance(bet, PlacedBet) and bet.player == "Player B (Team B)" and bet.side == "over" and bet.number == 2
    missing = resolve_bet(PlaceCommand("1c", None, 1.8, 5.0, None), items, bet_id=1, update_id=1, message_id=9,
                          placed_at=NOW)
    assert isinstance(missing, ReplyError) and "it lists: 1a, 1b, 2a" in missing.message
    # The over/under shorthand after a plain number still parses (letters o and u are never bet labels).
    plain = parse_reply("placed 3 u @1.9 5")
    assert isinstance(plain, PlaceCommand) and plain.number == "3" and plain.side == "under"
    with pytest.raises(ValueError, match="label"):
        replace(items[1], label="1")


def test_digest_rejects_an_empty_page_size(tmp_path: Path) -> None:
    notifier, _, _ = _digest_notifier(1, tmp_path)
    with pytest.raises(ValueError):
        notifier.notify_digest(AllocationReport(), {}, per_message=0)


def test_env_template_holds_no_credentials() -> None:
    """2026-10-04: real keys were found pasted into the tracked .env.example. Credentials belong in .env only."""
    template = parse_env_file((Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8"))
    secret_names = [key for key in template if key.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))]
    assert secret_names and {key: template[key] for key in secret_names if template[key]} == {}
