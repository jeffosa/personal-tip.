"""Shadow picks (alerts/shadow_picks.py, closing-price review 2026-10-25): the real-price picks the contract gate withholds are
kept, unsent, and the pilot's closing capture looks for their prices, so closing-line value can be measured without a tip."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from prop_alpha.alerts import market_contract, pilot_closing, pilot_ledger, scan
from prop_alpha.alerts.ledger import PlacedBetStore
from prop_alpha.alerts.shadow_picks import ShadowPickLog, shadow_records
from prop_alpha.modeling.engine import VoidRule
from prop_alpha.scanner.market_scanner import AllocationReport, StakingDirective
from prop_alpha.scanner.slate import OddsCoverage, UpcomingFixture
from test_pilot_ledger import KICKOFF, NOW, _FakeOdds, _ladder_reply, _names

FIXTURE = UpcomingFixture(500, "Brazil Serie A", "76", "348", "Flamengo", "Fluminense", KICKOFF)


def _directive(fixture_id: int = 500, player_id: int = 70, book: str = "bet365") -> StakingDirective:
    return StakingDirective(
        fixture_id=fixture_id, player_id=player_id, market_type="tackles", line_value=1.5, side="over", sportsbook=book,
        void_rule=VoidRule.VOID_IF_NO_APPEARANCE.value, decimal_odds=3.0, p_win=0.40, p_lose=0.50, p_void=0.10,
        fair_market_prob=None, ev_per_unit=0.2, return_variance_per_unit=1.0, kelly_full=0.1, fraction=0.01, stake=5.0,
        min_acceptable_odds=2.8, expires_at=KICKOFF,
    )


def test_a_shadow_pick_records_the_match_the_selection_and_the_model_and_is_pre_kickoff_only(tmp_path: Path) -> None:
    [record] = shadow_records([_directive()], {500: FIXTURE}, {70: "Alpha Lima"}, NOW, "pilot")
    assert pilot_ledger.fixture_of(record) == FIXTURE  # the closing capture rebuilds the match from it
    assert (record["player"], record["book"], record["odds"], record["line"], record["side"]) == (
        "Alpha Lima", "bet365", 3.0, 1.5, "over")
    with pytest.raises(ValueError, match="at or after kickoff"):
        shadow_records([_directive()], {500: FIXTURE}, {70: "Alpha Lima"}, KICKOFF, "pilot")
    with pytest.raises(ValueError, match="unknown fixture"):
        shadow_records([_directive(fixture_id=1)], {500: FIXTURE}, {70: "Alpha Lima"}, NOW, "pilot")
    with pytest.raises(ValueError, match="unknown player"):
        shadow_records([_directive(player_id=1)], {500: FIXTURE}, {70: "Alpha Lima"}, NOW, "pilot")
    with pytest.raises(ValueError, match="source"):
        shadow_records([], {}, {}, NOW, "tips")
    log = ShadowPickLog(tmp_path / "shadow.jsonl")
    assert log.record([_directive()], {500: FIXTURE}, {70: "Alpha Lima"}, NOW, "pilot") == 1
    assert log.record([], {}, {}, NOW, "pilot") == 0 and len(log.load()) == 1
    log.path.write_text(json.dumps({"fixture_id": 500}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a shadow pick"):
        log.load()


def test_the_pilot_closing_capture_prices_shadow_picks_without_any_paper_bet(tmp_path: Path) -> None:
    _names(tmp_path)
    log = ShadowPickLog(tmp_path / "shadow.jsonl")
    log.record([_directive(), _directive(player_id=71)], {500: FIXTURE}, {70: "Alpha Lima", 71: "Bravo Mike"},
               NOW - timedelta(hours=3), "pilot")
    store = PlacedBetStore(tmp_path / "paper.jsonl")  # no paper bet at all: tips are withheld
    paths: dict[str, Any] = {"closing_file": tmp_path / "closing.jsonl", "handled_file": tmp_path / "handled.json",
             "tips_file": tmp_path / "tips.jsonl"}
    assert [f.fixture_id for f in pilot_closing.due_fixtures([], {}, set(), NOW, log.load())] == [500]
    assert pilot_closing.due_fixtures([], {}, set(), NOW) == []  # without the shadow picks nothing was due
    odds = _FakeOdds(_ladder_reply({"Alpha Lima": 2.4, "Bravo Mike": 3.5, "Somebody Else": 1.9}))
    written, notes, available = pilot_closing.capture(NOW, odds, store, tmp_path, **paths, shadow_log=log)
    assert available and written == 2 and any("closing prices: 2 for 1 fixture(s)" in n for n in notes)
    closing = pilot_ledger.load_closing(paths["closing_file"])
    assert sorted((q["player_id"], q["over"]) for q in closing) == [(70, 2.4), (71, 3.5)]
    assert pilot_closing.capture(NOW, odds, store, tmp_path, **paths, shadow_log=log) == (0, [], True)  # once


class _CountingNotifier:
    def __init__(self) -> None:
        self.sent: list[StakingDirective] = []

    def notify_digest(self, report: AllocationReport, *args: Any, **kwargs: Any) -> tuple[int, list[str]]:
        self.sent.extend(report.directives)
        return len(report.directives), []


def _scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, odds: str, log: ShadowPickLog) -> tuple[Any, _CountingNotifier]:
    from test_scan import _patch_database, _StubSportmonks

    _patch_database(monkeypatch)
    player = scan.load_slate_inputs(None, None).squad[0]  # type: ignore[arg-type]
    directive = _directive(fixture_id=9001, player_id=player.player_id)

    def one_directive(*args: Any, **kwargs: Any) -> tuple[dict[Any, Any], AllocationReport, list[Any]]:
        return {}, AllocationReport(directives=[directive]), []

    monkeypatch.setattr(scan, "scan_fixture", one_directive)
    monkeypatch.setattr(scan, "collect_odds", lambda *args, **kwargs: (OddsCoverage((), 0, 0, (), ()), [], True))
    notifier = _CountingNotifier()
    summary = scan.run_scan(datetime(2026, 10, 10, 12, tzinfo=timezone.utc), _StubSportmonks(), "postgresql://unused",
                            cast(Any, notifier), scan.SentAlertStore(tmp_path / "sent.jsonl"), 1000.0, "",
                            currency="GBP", odds=odds, odds_client=object(), books=("bet365",), state_dir=tmp_path,
                            shadow=log)
    return summary, notifier


def test_the_league_scan_keeps_each_withheld_pick_as_a_shadow_pick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = ShadowPickLog(tmp_path / "shadow.jsonl")
    summary, notifier = _scan(tmp_path, monkeypatch, "oddspapi", log)
    [pick] = log.load()
    assert summary.withheld == 1 and notifier.sent == [] and (pick["source"], pick["fixture_id"]) == ("league", 9001)
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {("bet365", "tackles"): market_contract.Contract(
        "s.tackles_attempted", VoidRule.VOID_IF_NO_APPEARANCE.value, "bet365_tackles.json")})
    _, notifier = _scan(tmp_path, monkeypatch, "oddspapi", log)
    assert len(notifier.sent) == 1 and len(log.load()) == 1  # a sent tip is not a shadow pick
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {})
    _scan(tmp_path, monkeypatch, "mock", log)
    assert len(log.load()) == 1  # mock prices are the model's own: never kept
