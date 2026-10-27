"""Closing-price capture (alerts/closing.py), closing-line value in the ledger, and the Gate 2 market benchmark."""

from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prop_alpha.alerts import closing, ledger, scan
from prop_alpha.alerts.replies import PlacedBet, Settlement, settlement_text
from prop_alpha.evaluation import gate2
from prop_alpha.evaluation.backtest import closing_line_value
from test_oddspapi import FIXTURE, KICKOFF, SQUAD, FakeOddsClient, _odds


def _patch_squads(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Conn:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> Any:
            return self

        def close(self) -> None:
            return None

    import psycopg2

    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: _Conn())
    monkeypatch.setattr(scan, "load_slate_inputs",
                        lambda cur, fixture: scan.SlateInputs(tuple(SQUAD), {}, {}, {}))


def test_only_fixtures_kicking_off_within_the_lead_and_not_yet_captured_are_due(tmp_path: Path) -> None:
    other = scan.UpcomingFixture(9002, "League Two", "1", "2", "A", "B", KICKOFF + timedelta(hours=3))
    closing.save_scan_fixtures(tmp_path, [FIXTURE, other], ["bet365"], KICKOFF - timedelta(hours=4))
    saved = closing.load_scan_fixtures(tmp_path)
    assert set(saved) == {9001, 9002} and saved[9001][1] == ("bet365",)
    assert closing.due_fixtures(saved, frozenset(), KICKOFF - timedelta(minutes=30)) == []
    due = closing.due_fixtures(saved, frozenset(), KICKOFF - timedelta(minutes=10))
    assert [fixture.fixture_id for fixture, _ in due] == [9001]
    assert closing.due_fixtures(saved, frozenset({9001}), KICKOFF - timedelta(minutes=10)) == []
    assert closing.due_fixtures(saved, frozenset(), KICKOFF + timedelta(minutes=1)) == []  # kicked off: too late
    closing.save_scan_fixtures(tmp_path, [], [], KICKOFF + timedelta(days=3))  # old fixtures are pruned
    assert set(closing.load_scan_fixtures(tmp_path)) == {9002} or closing.load_scan_fixtures(tmp_path) == {}


def test_a_fixture_saved_without_a_bookmaker_spends_no_request(tmp_path: Path) -> None:
    closing.save_scan_fixtures(tmp_path, [FIXTURE], [], KICKOFF - timedelta(hours=4))
    assert closing.due_fixtures(closing.load_scan_fixtures(tmp_path), frozenset(), KICKOFF - timedelta(minutes=5)) == []


def test_capture_writes_matched_closing_quotes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_squads(monkeypatch)
    closing.save_scan_fixtures(tmp_path, [FIXTURE], ["bet365"], KICKOFF - timedelta(hours=4))
    now = KICKOFF - timedelta(minutes=10)
    client = FakeOddsClient(left=200, odds=_odds())
    fixtures, quotes, _ = closing.capture(now, client, "postgresql://unused", tmp_path)
    assert fixtures == 1 and quotes == 2
    records = closing.load_closing_quotes(tmp_path)
    assert {r["market"] for r in records} == {"tackles", "fouls"} and all(r["fixture_id"] == 9001 for r in records)
    assert all(r["captured_at"] == now.isoformat() for r in records)
    again = FakeOddsClient(left=200, odds=_odds())
    assert closing.capture(now, again, "postgresql://unused", tmp_path)[:2] == (0, 0) and again.paths == []


def test_empty_successful_response_is_retried_and_later_quotes_are_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_squads(monkeypatch)
    closing.save_scan_fixtures(tmp_path, [FIXTURE], ["bet365"], KICKOFF - timedelta(hours=4))
    now = KICKOFF - timedelta(minutes=10)

    empty = FakeOddsClient(left=200, odds=[])
    assert closing.capture(now, empty, "postgresql://unused", tmp_path)[:2] == (0, 0)
    assert closing.due_fixtures(closing.load_scan_fixtures(tmp_path), frozenset(), now) == [(FIXTURE, ("bet365",))]
    assert not (tmp_path / closing.CAPTURED_NAME).exists()

    available = FakeOddsClient(left=200, odds=_odds())
    assert closing.capture(now, available, "postgresql://unused", tmp_path)[:2] == (1, 2)
    records = closing.load_closing_quotes(tmp_path)
    assert len(records) == 2

    duplicate = FakeOddsClient(left=200, odds=_odds())
    assert closing.capture(now, duplicate, "postgresql://unused", tmp_path)[:2] == (0, 0)
    assert duplicate.paths == [] and closing.load_closing_quotes(tmp_path) == records


def test_capture_fails_loudly_when_every_bookmaker_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_squads(monkeypatch)
    closing.save_scan_fixtures(tmp_path, [FIXTURE], ["broken"], KICKOFF - timedelta(hours=4))
    with pytest.raises(RuntimeError, match="no bookmaker answered"):
        closing.capture(KICKOFF - timedelta(minutes=10), FakeOddsClient(left=200, odds=_odds()),
                        "postgresql://unused", tmp_path)
    assert not (tmp_path / closing.CAPTURED_NAME).is_file()  # retried at the next poll


def _bet(side: str = "over", odds: float = 2.2, book: str = "bet365") -> PlacedBet:
    return PlacedBet(bet_id=1, update_id=1, message_id=1, number=1, kind="alert", fixture_id=9001, player_id=1,
                     market="tackles", line=1.5, side=side, odds=odds, stake=5.0, book=book, min_odds=2.0,
                     match="Walsall v Port Vale", player="A", placed_at=KICKOFF - timedelta(hours=2))


def _quote(book: str, over: float | None, under: float | None, at: str) -> dict[str, Any]:
    return {"fixture_id": 9001, "player_id": 1, "market": "tackles", "line": 1.5, "book": book, "over": over,
            "under": under, "captured_at": at}


def test_closing_line_value_uses_only_the_devigged_two_way_close_of_the_same_book() -> None:
    quotes = [_quote("skybet", 1.8, 2.1, "2026-10-10T13:50:00+00:00"),
              _quote("bet365", 2.0, 1.9, "2026-10-10T13:52:00+00:00"),
              _quote("bet365", 1.9, None, "2026-10-10T13:55:00+00:00")]  # one-sided: unusable
    value = ledger.closing_value(_bet(), quotes)
    assert value is not None and value.book == "bet365" and value.closing_price == 2.0
    assert value.clv == pytest.approx(closing_line_value(2.2, 2.0, 1.9))
    # Another book's close has another margin: a bet whose book has no captured close gets no CLV (PRD Gate 3, 2026-10-25).
    assert ledger.closing_value(_bet(side="under", odds=2.0, book="unknown"), quotes) is None
    under = ledger.closing_value(_bet(side="under", odds=2.0, book="SkyBet"), quotes)  # its own, earlier close
    assert under is not None and under.book == "skybet" and under.clv == pytest.approx(closing_line_value(2.0, 2.1, 1.8))
    assert ledger.closing_value(_bet(), [quotes[2]]) is None
    text = settlement_text(_bet(), Settlement(1, "won", "auto", "auto", KICKOFF), value)
    assert "close 2.00 at bet365, CLV" in text
    summary = ledger.results_summary([(_bet(), Settlement(1, "won", "auto", "auto", KICKOFF))], {1: value})
    assert f"mean CLV {value.clv:+.1%} over 1 bet(s)" in summary
    assert "no closing price captured yet" in ledger.results_summary([(_bet(), Settlement(1, "won", "auto", "", KICKOFF))])


def test_market_benchmark_detects_information_beyond_the_market() -> None:
    rng = np.random.default_rng(5)
    truth = rng.uniform(0.2, 0.8, 4000)
    y = (rng.uniform(size=4000) < truth).astype(int)
    market = np.clip(truth + rng.normal(0, 0.12, 4000), 0.05, 0.95)  # noisy market
    informed = np.clip(truth + rng.normal(0, 0.03, 4000), 0.05, 0.95)  # model closer to the truth
    fit = gate2.market_benchmark(market.tolist(), informed.tolist(), y.tolist())
    assert fit.converged and fit.b_model > 0 and fit.p_value < 0.05 and fit.passed
    copy = gate2.market_benchmark(market.tolist(), market.tolist(), y.tolist())
    assert not copy.converged and not copy.passed and math.isnan(copy.b_model)  # identical prices: no information
    few = gate2.market_benchmark(market[:100].tolist(), informed[:100].tolist(), y[:100].tolist())
    assert not few.passed  # under MIN_BENCHMARK_PREDICTIONS it is never judged a pass


def test_the_scan_saves_its_fixtures_for_closing_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_scan import _patch_database, _StubSportmonks

    _patch_database(monkeypatch)

    class OneBook(FakeOddsClient):
        pass

    store = scan.SentAlertStore(tmp_path / "sent.jsonl")
    scan.run_scan(KICKOFF - timedelta(hours=2), _StubSportmonks(), "postgresql://unused", None, store, 1000.0, "",
                  currency="GBP", odds="oddspapi", odds_client=OneBook(left=200, odds=_odds()),
                  books=("bet365", "skybet"), state_dir=tmp_path)
    raw = json.loads((tmp_path / closing.SCAN_FIXTURES_NAME).read_text(encoding="utf-8"))
    assert raw and all(entry["books"] == ["bet365"] for entry in raw)  # skybet had no fixtures: never asked again
