"""4-hourly Telegram status report."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    assert "Sportmonks trial: 22 days left" in report
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


def test_the_trial_warns_a_week_ahead_not_only_after_it_ended() -> None:
    far = datetime(2026, 10, 17, tzinfo=timezone.utc)   # 8 days left: plain information
    near = datetime(2026, 10, 18, tzinfo=timezone.utc)  # 7 days left: a warning
    assert "Sportmonks trial: 8 days left (ends 25 Oct)" in status.format_report(far, [], 0, 0.0)
    warning = status.format_report(near, [], 0, 0.0)
    assert "WARNING: Sportmonks trial ends 25 Oct (7 days left)" in warning and "choose a paid plan" in warning


def test_trial_expiry_turns_into_a_warning() -> None:
    after = datetime(2026, 10, 26, tzinfo=timezone.utc)
    assert "WARNING: Sportmonks trial ended 25 Oct" in status.format_report(after, [], 0, 0.0)


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


def _archive_reply(directory: Path, name: str, body: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(directory / name, "wt", encoding="utf-8") as handle:
        json.dump(body, handle)


# The OddsPapi shapes verified on 2026-10-03 (tests/test_oddspapi.py), cut down to one tackles and one fouls line.
ODDS_MARKETS: list[dict[str, Any]] = [
    {"marketId": 102661, "marketType": "playertotals-tackles", "playerProp": True, "handicap": 1.5,
     "outcomes": [{"outcomeId": 102661, "outcomeName": "Over"}, {"outcomeId": 102662, "outcomeName": "Under"}]},
    {"marketId": 104000, "marketType": "playertotals-foulscommitted", "playerProp": True, "handicap": 0.5,
     "outcomes": [{"outcomeId": 104000, "outcomeName": "Over"}, {"outcomeId": 104001, "outcomeName": "Under"}]},
]


def _price(name: str, price: float, active: bool = True) -> dict[str, Any]:
    return {"active": active, "playerName": name, "price": price, "limit": None}


def _odds() -> list[dict[str, Any]]:
    """bet365: three tackles prices (two players, both sides) and one active fouls price in one fixture."""
    return [{
        "fixtureId": "id1000002472341252", "participant1Id": 56, "participant2Id": 95,
        "startTime": "2026-10-10T14:00:00.000Z", "tournamentId": 25,
        "bookmakerOdds": {"bet365": {"bookmakerIsActive": True, "suspended": False, "markets": {
            "102661": {"marketActive": True, "outcomes": {
                "102661": {"players": {"2342841": _price("O'Donkor, Gatlin", 2.4), "777": _price("Other, One", 2.0)}},
                "102662": {"players": {"2342841": _price("O'Donkor, Gatlin", 1.55)}},
            }},
            "104000": {"marketActive": True, "outcomes": {
                "104000": {"players": {"34706": _price("Hernandez, Onel", 1.9)}},
                "104001": {"players": {"34706": _price("Hernandez, Onel", 1.8, active=False)}},
            }},
        }}},
    }]


def _odds_state(tmp_path: Path, with_catalogue: bool = True) -> tuple[Path, Path]:
    from prop_alpha.alerts import scan
    from prop_alpha.ingestion import oddspapi

    if with_catalogue:
        scan._save_catalogue(tmp_path / scan.ODDS_CACHE_DIR / scan.CATALOGUE_FILE,
                             oddspapi.line_catalogue(ODDS_MARKETS))
    return tmp_path, tmp_path / scan.ODDS_ARCHIVE_DIR


def test_quote_census_counts_each_archived_price_once_and_skips_rejected_replies(tmp_path: Path) -> None:
    """Audit F-02: the status report counts the tackles/fouls prices OddsPapi has sent so far."""
    state, archive = _odds_state(tmp_path)
    _archive_reply(archive, "20261006T080000Z_bet365.json.gz", _odds())
    _archive_reply(archive, "20261006T120000Z_bet365.json.gz", _odds())  # the same prices again: counted once
    _archive_reply(archive, "20261006T120000Z_skybet.json.gz", [])
    _archive_reply(archive, "20261006T120000Z_williamhill.rejected.json.gz", {"not": "a list"})
    (archive / "notes.txt").write_text("not a reply", encoding="utf-8")
    census = status.quote_census(archive, status.load_catalogue(state / "oddspapi" / "line_markets.json"))
    first = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    assert (census.replies, census.rejected) == (3, 1)
    assert (census.first_reply, census.last_reply) == (first, datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
    assert census.prices == {"fouls": 1, "tackles": 3} and census.fixtures == {"fouls": 1, "tackles": 1}
    assert census.first_seen == {"fouls": (first, "bet365"), "tackles": (first, "bet365")}
    assert status.quote_section(state) == [
        "Tackles/fouls quotes seen (F-02), 3 archived OddsPapi replies, 06 Oct-06 Oct: "
        "fouls 1 prices in 1 bookmaker-matches, first 06 Oct 08:00 UTC (bet365); "
        "tackles 3 prices in 1 bookmaker-matches, first 06 Oct 08:00 UTC (bet365); 1 malformed replies not counted"
    ]


def test_quote_section_says_f02_is_open_while_no_price_has_been_seen(tmp_path: Path) -> None:
    state, archive = _odds_state(tmp_path)
    assert status.quote_section(state) == ["Tackles/fouls quotes (F-02): no OddsPapi reply archived yet"]
    _archive_reply(archive, "20261006T080000Z_bet365.json.gz", [])
    assert status.quote_section(state) == [
        "F-02 open: no tackles/fouls price in 1 archived OddsPapi reply, 06 Oct-06 Oct"]


def test_quote_section_warns_when_replies_are_archived_without_a_catalogue(tmp_path: Path) -> None:
    state, archive = _odds_state(tmp_path, with_catalogue=False)
    assert status.quote_section(state) == ["Tackles/fouls quotes (F-02): no OddsPapi reply archived yet"]
    _archive_reply(archive, "20261006T080000Z_bet365.json.gz", [])
    [line] = status.quote_section(state)
    assert line.startswith("WARNING: tackles/fouls quotes not counted") and "line_markets.json" in line


def test_a_damaged_archived_reply_fails_the_count_instead_of_reading_as_zero(tmp_path: Path) -> None:
    state, archive = _odds_state(tmp_path)
    _archive_reply(archive, "20261006T080000Z_bet365.json.gz", {"not": "a list"})
    with pytest.raises(ValueError, match="not a list"):
        status.quote_section(state)


def test_quote_census_refuses_inconsistent_counts() -> None:
    at = datetime(2026, 10, 6, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="first sighting"):
        status.QuoteCensus(1, 0, at, at, {"fouls": 0, "tackles": 2}, {"fouls": 0, "tackles": 1}, {})
    with pytest.raises(ValueError, match="inconsistent"):
        status.QuoteCensus(1, 0, at, at, {"fouls": 0, "tackles": 1}, {"fouls": 0, "tackles": 2},
                           {"tackles": (at, "bet365")})
    with pytest.raises(ValueError, match="cover exactly"):
        status.QuoteCensus(0, 0, None, None, {"tackles": 0}, {"tackles": 0}, {})
    with pytest.raises(ValueError, match="reply times"):
        status.QuoteCensus(0, 0, at, None, {"fouls": 0, "tackles": 0}, {"fouls": 0, "tackles": 0}, {})


def test_report_includes_the_quote_count_when_given_the_odds_state(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "read_snapshot", lambda dsn: SNAPSHOT)
    state, archive = _odds_state(tmp_path)
    _archive_reply(archive, "20261006T080000Z_bet365.json.gz", [])
    report = status.build_report(NOW, "postgresql://unused", SentAlertStore(tmp_path / "sent.jsonl"),
                                 odds_state=state)
    assert "F-02 open: no tackles/fouls price in 1 archived OddsPapi reply" in report
