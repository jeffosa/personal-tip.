"""The settlement-contract gate (alerts/market_contract.py, audit 2026-10-25): no real-price tip is sent for a bookmaker
market whose settlement rules are not verified, and none is verified."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from prop_alpha.alerts import market_contract, scan
from prop_alpha.alerts.market_contract import WITHHELD_NOTE, contract_verified, withhold_unverified
from prop_alpha.modeling.engine import VoidRule
from prop_alpha.scanner.market_scanner import AllocationReport, StakingDirective
from prop_alpha.scanner.slate import OddsCoverage

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
NO_APPEARANCE = VoidRule.VOID_IF_NO_APPEARANCE.value


def _directive(book: str, market: str = "tackles", void_rule: str = NO_APPEARANCE) -> StakingDirective:
    return StakingDirective(
        fixture_id=1, player_id=2, market_type=market, line_value=1.5, side="over", sportsbook=book, void_rule=void_rule,
        decimal_odds=2.5, p_win=0.45, p_lose=0.45, p_void=0.10, fair_market_prob=None, ev_per_unit=0.12,
        return_variance_per_unit=1.0, kelly_full=0.1, fraction=0.01, stake=10.0, min_acceptable_odds=2.3,
        expires_at=NOW + timedelta(hours=1),
    )


def test_no_bookmaker_market_is_verified_yet() -> None:
    # MARKET_CONTRACT.md, 2026-10-25: neither bet365's nor Betway's tackles or fouls rules are verified. Adding one is a
    # maintainer decision with its evidence recorded there; this test then changes with it.
    assert dict(market_contract.VERIFIED_CONTRACTS) == {}
    for book in ("bet365", "betway", "skybet", "sportybet"):
        for market in ("tackles", "fouls"):
            assert not contract_verified(book, market, NO_APPEARANCE)


def test_a_contract_covers_its_book_market_and_void_rule_only() -> None:
    contracts = {("bet365", "tackles"): VoidRule.VOID_IF_NOT_STARTING.value}
    assert contract_verified("Bet365", "tackles", VoidRule.VOID_IF_NOT_STARTING.value, contracts)
    assert not contract_verified("bet365", "tackles", NO_APPEARANCE, contracts)  # priced on another void rule
    assert not contract_verified("bet365", "fouls", VoidRule.VOID_IF_NOT_STARTING.value, contracts)
    assert not contract_verified("betway", "tackles", VoidRule.VOID_IF_NOT_STARTING.value, contracts)


def test_unverified_directives_are_withheld_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    directives = [_directive("bet365"), _directive("betway"), _directive("bet365", "fouls")]
    assert withhold_unverified(directives) == ([], 3)
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {("bet365", "tackles"): NO_APPEARANCE})
    assert withhold_unverified(directives) == ([directives[0]], 2)


def test_the_league_scan_summary_says_how_many_tips_were_withheld_and_why() -> None:
    summary = scan.ScanSummary(NOW, NOW + timedelta(hours=4), fixtures=["A v B 15:00"], withheld=3)
    text = summary.text("")
    assert WITHHELD_NOTE.format(count=3) in text and "unverified estimates, not value" in text
    assert "Tips withheld" not in scan.ScanSummary(NOW, NOW + timedelta(hours=4), fixtures=["A v B 15:00"]).text("")


class _CountingNotifier:
    def __init__(self) -> None:
        self.sent: list[StakingDirective] = []

    def notify_digest(self, report: AllocationReport, *args: Any, **kwargs: Any) -> tuple[int, list[str]]:
        self.sent.extend(report.directives)
        return len(report.directives), []


def _scan_with_one_directive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, odds: str) -> tuple[Any, _CountingNotifier]:
    from test_scan import _patch_database, _StubSportmonks

    _patch_database(monkeypatch)
    directive = _directive("bet365")

    def one_directive(*args: Any, **kwargs: Any) -> tuple[dict[Any, Any], AllocationReport, list[Any]]:
        return {}, AllocationReport(directives=[directive]), []

    monkeypatch.setattr(scan, "scan_fixture", one_directive)
    monkeypatch.setattr(scan, "collect_odds", lambda *args, **kwargs: (OddsCoverage((), 0, 0, (), ()), [], True))
    notifier = _CountingNotifier()
    store = scan.SentAlertStore(tmp_path / "sent.jsonl")
    summary = scan.run_scan(datetime(2026, 10, 10, 12, tzinfo=timezone.utc), _StubSportmonks(), "postgresql://unused",
                            cast(Any, notifier), store, 1000.0, "", currency="GBP", odds=odds, odds_client=object(),
                            books=("bet365",), state_dir=tmp_path)
    return summary, notifier


def test_the_league_scan_withholds_real_price_tips_but_not_mock_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real, notifier = _scan_with_one_directive(tmp_path, monkeypatch, "oddspapi")
    assert (real.withheld, notifier.sent) == (1, []) and real.alerts_sent == 0
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {("bet365", "tackles"): NO_APPEARANCE})
    verified, notifier = _scan_with_one_directive(tmp_path, monkeypatch, "oddspapi")
    assert (verified.withheld, len(notifier.sent)) == (0, 1)
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {})
    mock, notifier = _scan_with_one_directive(tmp_path, monkeypatch, "mock")  # the model's own prices, labelled MOCK
    assert (mock.withheld, len(notifier.sent)) == (0, 1)


def test_every_scan_summary_states_the_contract_gate() -> None:
    assert market_contract.contract_status() == ("real-price tips are sent only under a verified settlement contract: "
                                                 "none yet, so none is sent (MARKET_CONTRACT.md)")
    assert market_contract.contract_status({("bet365", "tackles"): NO_APPEARANCE}).endswith(": bet365 tackles")
    assert market_contract.contract_status() in scan.ScanSummary(NOW, NOW, fixtures=["A v B 15:00"]).text("")
