"""4-hourly Telegram status report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from prop_alpha.alerts import status  # noqa: E402
from prop_alpha.alerts.ledger import PlacedBetStore, process_updates  # noqa: E402
from prop_alpha.alerts.replies import AlertItem, Settlement  # noqa: E402
from prop_alpha.alerts.telegram import PAPER_LABEL, SentAlert, SentAlertStore  # noqa: E402

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
SNAPSHOT = status.DatabaseSnapshot(120, 110, datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc), 4300, 0, 0, 125)


def test_report_states_database_alerts_trial_and_paper_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "read_snapshot", lambda dsn: SNAPSHOT)
    store = SentAlertStore(tmp_path / "sent.jsonl")
    store.append(SentAlert((1, 2, "tackles", 1.5, "over", "A"), 150.0, NOW - timedelta(hours=1)))
    store.append(SentAlert((1, 3, "fouls", 0.5, "over", "B"), 50.0, NOW - timedelta(hours=30)))
    report = status.build_report(NOW, "postgresql://unused", store)
    assert "Database: 120 fixtures (110 final), 4300 player rows" in report
    assert "Last merge: 2026-10-03 06:00 UTC" in report
    assert "Incomplete club-matches (a player without an ID played; left out of pricing): 0" in report
    assert "Alerts in the last 4h: 1; open alerted exposure: 200.00" in report
    assert "Sportmonks trial: 13 days left" in report
    assert report.endswith(PAPER_LABEL) and "WARNING" not in report
    assert len(report) < 4096


def test_backlog_and_rejects_are_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    backlog = status.DatabaseSnapshot(1, 1, None, 1, 37, 2, 1)
    monkeypatch.setattr(status, "read_snapshot", lambda dsn: backlog)
    lines = status.database_section("postgresql://unused")
    assert "Last merge: never" in lines[1]
    assert any("37 rows waiting in staging" in line for line in lines)
    assert any("2 rows rejected" in line for line in lines)


def test_unreachable_database_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(dsn: str) -> status.DatabaseSnapshot:
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(status, "read_snapshot", refuse)
    assert status.database_section("postgresql://unused") == [
        "Database: UNREACHABLE (OperationalError). Check the postgresql-x64-18 service."
    ]


def test_trial_expiry_turns_into_a_warning() -> None:
    after = datetime(2026, 10, 17, tzinfo=timezone.utc)
    assert "WARNING: Sportmonks trial ended 16 Oct" in status.format_report(after, [], 0, 0.0)


def test_default_database_is_never_the_ci_database() -> None:
    assert status.DEFAULT_DSN.endswith("/prop_alpha") and "_ci" not in status.DEFAULT_DSN


def test_report_lists_open_and_stale_bets_and_settled_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "read_snapshot", lambda dsn: SNAPSHOT)
    item = AlertItem(1, "alert", 9001, 7, "tackles", 1.5, (("over", 1.9),), "Walsall v Port Vale", "A", "skybet")
    bets = PlacedBetStore(tmp_path / "bets.jsonl")

    def update(update_id: int, text: str) -> dict[str, object]:
        return {"update_id": update_id, "message": {
            "message_id": 900 + update_id, "chat": {"id": 4242}, "text": text,
            "date": int((NOW - timedelta(days=9)).timestamp()), "reply_to_message": {"message_id": 500}}}

    process_updates([update(1, "placed 1 @2.0 5"), update(2, "placed 1 @2.0 3")], "4242", {500: {1: item}}, bets, NOW)
    bets.record_settlement(Settlement(2, "won", "auto", "test", NOW))
    report = status.build_report(NOW, "postgresql://unused", SentAlertStore(tmp_path / "sent.jsonl"), bets)
    assert "Open bets: 1 (stake 5.00)" in report
    assert "WARNING: 1 bet(s) open more than 7 days (#1): reply settled <bet id> won|lost|void" in report
    assert "1 settled bet(s) (1 won, 0 lost, 0 void): staked 3.00, profit +3.00" in report
    assert status.STATUS_NOTE in report and "bet ledger not built" not in report.replace("database bet ledger", "")


def test_report_shows_the_exchange_rate_and_flags_a_stale_one() -> None:
    from datetime import date

    from prop_alpha.alerts.fx import ExchangeRate

    rate = ExchangeRate("GBP", "USD", 1.3201, date(2026, 10, 2))
    assert status.fx_section(None, NOW) == []
    assert status.fx_section(rate, NOW) == ["Exchange rate: GBP->USD 1.3201 (ECB 02 Oct)"]
    [warning] = status.fx_section(rate, NOW + timedelta(days=6))
    assert warning.startswith("WARNING: exchange rate") and "scans refuse to run" in warning
