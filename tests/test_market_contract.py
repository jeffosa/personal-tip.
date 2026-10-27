"""The settlement-contract gate (alerts/market_contract.py, audits 2026-10-25): no real-price tip is sent for a bookmaker
market without a verified contract on the very count and void rule the model priced, none is verified, and a contract can
only be listed with evidence that meets every criterion."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, cast

import pytest

from prop_alpha.alerts import ladder_scan, market_contract, pilot_ledger, scan
from prop_alpha.alerts.market_contract import (
    MODEL_STATISTICS,
    RULE_TOPICS,
    WITHHELD_NOTE,
    Contract,
    contract_verified,
    evidence_failures,
    expected_result,
    statistic_columns,
    withhold_unverified,
)
from prop_alpha.modeling.engine import VoidRule
from prop_alpha.scanner.market_scanner import AllocationReport, StakingDirective
from prop_alpha.scanner.slate import OddsCoverage

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "contract_evidence"
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
NO_APPEARANCE = VoidRule.VOID_IF_NO_APPEARANCE.value
TACKLES = Contract("s.tackles_attempted", NO_APPEARANCE, "bet365_tackles.json")


def _directive(book: str, market: str = "tackles", void_rule: str = NO_APPEARANCE) -> StakingDirective:
    return StakingDirective(
        fixture_id=1, player_id=2, market_type=market, line_value=1.5, side="over", sportsbook=book, void_rule=void_rule,
        decimal_odds=2.5, p_win=0.45, p_lose=0.45, p_void=0.10, fair_market_prob=None, ev_per_unit=0.12,
        return_variance_per_unit=1.0, kelly_full=0.1, fraction=0.01, stake=10.0, min_acceptable_odds=2.3,
        expires_at=NOW + timedelta(hours=1),
    )


# ---------------- what is verified, and the evidence behind it ----------------
def test_no_bookmaker_market_is_verified_yet() -> None:
    # MARKET_CONTRACT.md, 2026-10-25: no first-hand bookmaker rule and no same-book settlement exist. Adding a contract is a
    # maintainer decision with its evidence file; this test then changes with it.
    assert dict(market_contract.VERIFIED_CONTRACTS) == {}
    for book in ("bet365", "betway", "skybet", "sportybet"):
        for market in ("tackles", "fouls"):
            assert not contract_verified(book, market, NO_APPEARANCE, MODEL_STATISTICS[market])


def test_every_listed_contract_has_evidence_that_meets_every_criterion() -> None:
    for (book, market), contract in market_contract.VERIFIED_CONTRACTS.items():
        path = EVIDENCE_DIR / contract.evidence
        assert path.is_file(), f"{book} {market}: evidence file {contract.evidence} missing"
        assert evidence_failures(json.loads(path.read_text(encoding="utf-8")), book, market, contract) == []


def test_the_evidence_template_parses_and_verifies_nothing() -> None:
    template = json.loads((EVIDENCE_DIR / "TEMPLATE.json").read_text(encoding="utf-8"))
    failures = evidence_failures(template, "bet365", "tackles", TACKLES)
    assert all(f"no first-hand rule on {topic}" in failures for topic in RULE_TOPICS)
    assert "0 same-book settled examples, 20 needed" in failures


def test_the_model_statistics_are_the_counts_the_scans_price() -> None:
    assert dict(MODEL_STATISTICS) == {"tackles": "s.tackles_attempted", "fouls": "s.fouls_committed"}
    for statistic in MODEL_STATISTICS.values():
        assert statistic in scan.HISTORY_SQL
    assert ladder_scan.PILOT_STATISTICS["tackles"] == pilot_ledger.PILOT_TACKLES_SQL
    assert statistic_columns("(s.tackles_attempted + s.dribbled_past)") == ("tackles_attempted", "dribbled_past")
    for bad in ("tackles_attempted", "s.tackles_attempted + s.x", "(s.a)", "s.a; DROP"):
        with pytest.raises(ValueError):
            statistic_columns(bad)
    with pytest.raises(ValueError):
        Contract("s.tackles_attempted", "void_sometimes", "x.json")
    with pytest.raises(ValueError):
        Contract("s.tackles_attempted", NO_APPEARANCE, "../x.json")


# ---------------- the gate ----------------
def test_a_contract_covers_its_book_market_count_and_void_rule_only() -> None:
    starting = VoidRule.VOID_IF_NOT_STARTING.value
    contracts = {("bet365", "tackles"): Contract("s.tackles_attempted", starting, "b.json")}
    assert contract_verified("Bet365", "tackles", starting, "s.tackles_attempted", contracts)
    assert not contract_verified("bet365", "tackles", NO_APPEARANCE, "s.tackles_attempted", contracts)  # other void rule
    assert not contract_verified("bet365", "tackles", starting, "s.tackles_won", contracts)  # the model prices another count
    assert not contract_verified("bet365", "fouls", starting, "s.fouls_committed", contracts)
    assert not contract_verified("betway", "tackles", starting, "s.tackles_attempted", contracts)


def test_unverified_directives_are_withheld_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    directives = [_directive("bet365"), _directive("betway"), _directive("bet365", "fouls")]
    assert withhold_unverified(directives, MODEL_STATISTICS) == ([], 3)
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {("bet365", "tackles"): TACKLES})
    assert withhold_unverified(directives, MODEL_STATISTICS) == ([directives[0]], 2)
    assert withhold_unverified(directives, {**MODEL_STATISTICS, "tackles": "s.tackles_won"}) == ([], 3)


def test_the_league_scan_summary_says_how_many_tips_were_withheld_and_why() -> None:
    summary = scan.ScanSummary(NOW, NOW + timedelta(hours=4), fixtures=["A v B 15:00"], withheld=3)
    text = summary.text("")
    assert WITHHELD_NOTE.format(count=3) in text and "unverified estimates, not value" in text
    assert "Tips withheld" not in scan.ScanSummary(NOW, NOW + timedelta(hours=4), fixtures=["A v B 15:00"]).text("")
    assert market_contract.contract_status() in text


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
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {("bet365", "tackles"): TACKLES})
    verified, notifier = _scan_with_one_directive(tmp_path, monkeypatch, "oddspapi")
    assert (verified.withheld, len(notifier.sent)) == (0, 1)
    monkeypatch.setattr(market_contract, "VERIFIED_CONTRACTS", {})
    mock, notifier = _scan_with_one_directive(tmp_path, monkeypatch, "mock")  # the model's own prices, labelled MOCK
    assert (mock.withheld, len(notifier.sent)) == (0, 1)


# ---------------- the evidence criteria ----------------
def _example(player: str, line: float, book_result: str, started: bool = True, minutes: int = 90, ta: int = 0, tw: int = 0,
             dp: int = 0, interceptions: int = 0) -> dict[str, Any]:
    return {"book": "bet365", "market": "tackles", "fixture": "19700001", "player": player, "line": line, "side": "over",
            "book_result": book_result, "started": started, "minutes": minutes, "settled_on": "2026-11-01",
            "source": "bet365 account history, read by the maintainer",
            "counts": {"tackles_attempted": ta, "tackles_won": tw, "dribbled_past": dp, "interceptions": interceptions}}


def _passing_evidence() -> dict[str, Any]:
    rules = [{"topic": topic, "source": "bookmaker rules page", "url_or_document": "https://help.example/rules",
              "checked_on": "2026-11-01", "wording": "the bookmaker's own sentence"} for topic in RULE_TOPICS]
    examples = [
        _example("won one, lost none", 0.5, "won", ta=1),                          # tackles won says lost
        _example("challenges lost only", 0.5, "lost", dp=2),                       # attempted + dribbled past says won
        _example("one of each", 1.5, "lost", ta=1, tw=1, interceptions=1),         # either count with interceptions says won
        _example("came on", 0.5, "won", started=False, minutes=30, ta=1, tw=1),   # a substitute appearance
        _example("on the bench", 0.5, "void", started=False, minutes=0),          # named, did not start or play
        *[_example(f"filler {n}", 1.5, "won", ta=2, tw=2) for n in range(16)],
    ]
    return {"book": "bet365", "market": "tackles", "rules": rules, "examples": examples}


def test_evidence_that_meets_every_criterion_verifies_the_contract() -> None:
    assert evidence_failures(_passing_evidence(), "bet365", "tackles", TACKLES) == []
    assert expected_result("s.tackles_attempted", NO_APPEARANCE, 1.0, "over", True, 90, {"tackles_attempted": 1}) == "void"
    assert expected_result("s.tackles_attempted", VoidRule.VOID_IF_NOT_STARTING.value, 0.5, "over", False, 30,
                           {"tackles_attempted": 3}) == "void"


def _drop_two_fillers(evidence: dict[str, Any]) -> None:
    del evidence["examples"][5:7]


@pytest.mark.parametrize("change, reason", [
    (lambda e: e["rules"][0].update(source="search summary"), "is not the bookmaker's own wording"),
    (lambda e: e["rules"].pop(), "no first-hand rule on whole-line settlement"),
    (lambda e: e["examples"][5].update(book="sportybet"), "another book's or market's settlement"),
    (lambda e: e["examples"][5].update(book_result="lost"), "DISCREPANCY: filler 0"),
    (_drop_two_fillers, "19 same-book settled examples, 20 needed"),
    (lambda e: e["examples"].pop(3), "no example of a substitute appearance"),
    (lambda e: e["examples"].pop(4), "no example of a named player who did not start or play"),
    (lambda e: e["examples"].pop(0), "no example contradicts the rival count s.tackles_won"),
])
def test_evidence_short_of_any_criterion_does_not_verify(change: Callable[[dict[str, Any]], Any], reason: str) -> None:
    evidence = deepcopy(_passing_evidence())
    change(evidence)
    failures = evidence_failures(evidence, "bet365", "tackles", TACKLES)
    assert any(reason in failure for failure in failures), failures


def test_a_selection_recorded_twice_with_a_different_result_is_a_conflict_never_a_silent_pick() -> None:
    """Contract audit 2026-10-26: the first record of a selection was kept and a later, contradicting one dropped, so 20
    agreeing examples plus a same-book settlement the other way still verified the contract."""
    evidence = deepcopy(_passing_evidence())
    evidence["examples"].append({**evidence["examples"][5], "book_result": "lost"})  # filler 0 again, settled the other way
    failures = evidence_failures(evidence, "bet365", "tackles", TACKLES)
    assert failures == ["CONFLICT: filler 0 (19700001) over 1.5 is recorded more than once with different results or counts"]
    substitute = deepcopy(_passing_evidence())
    substitute["examples"].append({**substitute["examples"][3], "book_result": "lost"})
    assert "no example of a substitute appearance" in evidence_failures(substitute, "bet365", "tackles", TACKLES)  # not counted
    repeated = deepcopy(_passing_evidence())
    repeated["examples"].append(deepcopy(repeated["examples"][5]))  # the same record twice: counted once, no conflict
    assert evidence_failures(repeated, "bet365", "tackles", TACKLES) == []


@pytest.mark.parametrize("missing", ["source", "settled_on"])
def test_an_example_that_does_not_say_where_and_when_the_result_was_read_is_not_counted(missing: str) -> None:
    evidence = deepcopy(_passing_evidence())
    evidence["examples"][5][missing] = ""
    assert evidence_failures(evidence, "bet365", "tackles", TACKLES) == [
        "example 'filler 0' does not say where and when the bookmaker's result was read"]
    substitute = deepcopy(_passing_evidence())
    del substitute["examples"][3][missing]  # the only substitute: without its provenance it is not counted
    assert "no example of a substitute appearance" in evidence_failures(substitute, "bet365", "tackles", TACKLES)


@pytest.mark.parametrize("field, value", [
    ("started", "false"), ("started", 0), ("minutes", "90"), ("minutes", -1), ("minutes", True), ("side", "Over"),
    ("line", 1.3), ("line", "1.5"), ("settled_on", "yesterday"), ("fixture", ""),
])
def test_a_malformed_example_raises_rather_than_being_read_loosely(field: str, value: Any) -> None:
    evidence = deepcopy(_passing_evidence())
    evidence["examples"][3][field] = value  # the substitute: bool("false") would have read it as a starter
    with pytest.raises(ValueError):
        evidence_failures(evidence, "bet365", "tackles", TACKLES)
    counted = deepcopy(_passing_evidence())
    counted["examples"][0]["counts"]["tackles_attempted"] = "1"
    with pytest.raises(ValueError, match="counts must be whole numbers"):
        evidence_failures(counted, "bet365", "tackles", TACKLES)


def test_a_listed_contract_is_keyed_as_the_gate_looks_it_up() -> None:
    """contract_verified looks up the lower-case bookmaker name: a key written otherwise would be shown as verified by
    contract_status while every tip stayed withheld (and its evidence file is named for its key)."""
    for (book, market), contract in market_contract.VERIFIED_CONTRACTS.items():
        assert book == book.strip().lower() and market in MODEL_STATISTICS
        assert contract.evidence == f"{book}_{market}.json"


def test_a_contract_the_settlements_contradict_fails_and_bad_evidence_raises() -> None:
    # The same examples against a contract on tackles won: the first example is a discrepancy.
    won = Contract("s.tackles_won", NO_APPEARANCE, "bet365_tackles.json")
    assert any(f.startswith("DISCREPANCY: won one, lost none")
               for f in evidence_failures(_passing_evidence(), "bet365", "tackles", won))
    # A contract that voids non-starters: the substitute's settled "won" contradicts it.
    starting = Contract("s.tackles_attempted", VoidRule.VOID_IF_NOT_STARTING.value, "bet365_tackles.json")
    assert any(f.startswith("DISCREPANCY: came on")
               for f in evidence_failures(_passing_evidence(), "bet365", "tackles", starting))
    broken = deepcopy(_passing_evidence())
    del broken["examples"][0]["counts"]["dribbled_past"]
    with pytest.raises(ValueError, match="lacks the stored counts"):
        evidence_failures(broken, "bet365", "tackles", TACKLES)


def test_betways_rules_document_covers_four_topics_and_voids_non_starters() -> None:
    """Betway Betting Rules v42 (10/02/2022, supplied by the maintainer 2026-10-25): first-hand wording on four topics for
    tackles, three for fouls; no same-book settlement yet; and its participation rule (players must start) is not the void
    rule the scans price, so even a fully evidenced Betway contract would keep the tips withheld."""
    tackles = json.loads((EVIDENCE_DIR / "betway_tackles.json").read_text(encoding="utf-8"))
    fouls = json.loads((EVIDENCE_DIR / "betway_fouls.json").read_text(encoding="utf-8"))
    starting = VoidRule.VOID_IF_NOT_STARTING.value
    tackle_failures = evidence_failures(tackles, "betway", "tackles", Contract("s.tackles_attempted", starting, "betway_tackles.json"))
    assert [f for f in tackle_failures if f.startswith("no first-hand rule")] == ["no first-hand rule on whole-line settlement"]
    assert "0 same-book settled examples, 20 needed" in tackle_failures
    foul_failures = evidence_failures(fouls, "betway", "fouls", Contract("s.fouls_committed", starting, "betway_fouls.json"))
    assert {f for f in foul_failures if f.startswith("no first-hand rule")} == {
        "no first-hand rule on statistic and data provider", "no first-hand rule on whole-line settlement"}
    assert "players must start" in tackles["rules"][2]["wording"]
    contracts = {("betway", "tackles"): Contract("s.tackles_attempted", starting, "betway_tackles.json")}
    assert not contract_verified("betway", "tackles", NO_APPEARANCE, MODEL_STATISTICS["tackles"], contracts)


def test_every_evidence_file_is_well_formed_and_holds_only_first_hand_rules() -> None:
    """2026-10-26: one evidence file per target bookmaker and market. Each must parse against the contract the model would
    need (its statistic, no-appearance void rule), and every rule recorded in it must be the bookmaker's own wording: a
    search summary or another book's page is never put in `rules`."""
    # Drafts from scripts/contract_evidence.py --write (<book>_<market>.draft.json) are work in progress, never evidence.
    files = sorted(p for p in EVIDENCE_DIR.glob("*.json") if p.name != "TEMPLATE.json" and not p.name.endswith(".draft.json"))
    assert {p.name for p in files} >= {"bet365_tackles.json", "bet365_fouls.json", "betway_tackles.json", "betway_fouls.json",
                                       "betfred_fouls.json"}
    for path in files:
        evidence = json.loads(path.read_text(encoding="utf-8"))
        book, market = path.stem.split("_", 1)
        assert (evidence["book"], evidence["market"]) == (book, market), path.name
        contract = Contract(MODEL_STATISTICS[market], NO_APPEARANCE, path.name)
        failures = evidence_failures(evidence, book, market, contract)  # raises on malformed evidence
        assert not any("is not the bookmaker's own wording" in f for f in failures), (path.name, failures)
        assert failures, f"{path.name} meets every criterion: list the contract in VERIFIED_CONTRACTS deliberately, with review"
