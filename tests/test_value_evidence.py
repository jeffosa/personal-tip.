"""Model-value evidence (evaluation/value_evidence.py, audit 2026-10-25): only a bookmaker's own settlement is an outcome,
only two-way closing prices are de-vigged, and no metric or value claim is made below the PRD samples."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pytest

from prop_alpha.alerts.replies import PlacedBet, Settlement
from prop_alpha.evaluation import value_evidence
from prop_alpha.evaluation.value_evidence import BookResult, Tip

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def _bet(bet_id: int, side: str = "over", book: str = "bet365", fixture_id: int = 50, player_id: int = 7) -> PlacedBet:
    return PlacedBet(bet_id=bet_id, update_id=bet_id, message_id=1, number=1, kind="alert", fixture_id=fixture_id,
                     player_id=player_id, market="tackles", line=1.5, side=side, odds=2.5, stake=10.0, book=book,
                     min_odds=2.2, match="A v B", player="P (A)", placed_at=NOW - timedelta(days=3))


def _manual(bet_id: int, result: str, book: str | None = None) -> Settlement:
    return Settlement(bet_id, result, "manual", "reported", NOW, update_id=bet_id, book=book)


def test_only_the_tipped_books_own_won_or_lost_result_is_an_outcome() -> None:
    settled = [
        (_bet(1), _manual(1, "won")),                                         # own book (no book named): Over won
        (_bet(2, side="under", player_id=8), _manual(2, "won", "bet365")),     # own book named; Under won = Over lost
        (_bet(3, player_id=9), _manual(3, "lost", "sportybet")),              # another book's result
        (_bet(4, player_id=10), Settlement(4, "won", "auto", "auto", NOW, 2, 90)),  # provisional automatic result
        (_bet(5, player_id=11), _manual(5, "void")),                          # a void is not an outcome
        (_bet(6, player_id=12), _manual(6, "won")), (_bet(7, side="under", player_id=12), _manual(7, "won")),  # conflict
    ]
    outcomes, left_out = value_evidence.book_results(settled)
    assert outcomes == {(50, 7, "tackles", 1.5): 1, (50, 8, "tackles", 1.5): 0}
    assert left_out == {"another bookmaker's result": 1, "automatic result (assumed rules, provisional)": 1,
                        "void (not an outcome)": 1, "conflicting results for one selection": 1}
    with pytest.raises(ValueError):
        BookResult(1, 2, "tackles", 1.5, "bet365", 2)


def test_closing_prices_are_devigged_from_the_latest_two_way_capture_only() -> None:
    quotes = [
        {"fixture_id": 1, "player_id": 2, "market": "tackles", "line": 1.5, "over": 2.0, "under": 1.8,
         "captured_at": "2026-11-01T14:50:00+00:00"},
        {"fixture_id": 1, "player_id": 2, "market": "tackles", "line": 1.5, "over": 2.2, "under": 1.7,
         "captured_at": "2026-11-01T14:55:00+00:00"},
        {"fixture_id": 1, "player_id": 3, "market": "tackles", "line": 1.5, "over": 2.0, "under": None,  # a ladder step
         "captured_at": "2026-11-01T14:55:00+00:00"},
    ]
    closing = value_evidence.closing_over(quotes)
    assert list(closing) == [(1, 2, "tackles", 1.5)]
    assert closing[(1, 2, "tackles", 1.5)] == pytest.approx((1 / 2.2) / (1 / 2.2 + 1 / 1.7))


def test_roi_and_clv_intervals_resample_whole_fixtures() -> None:
    rng = np.random.default_rng(3)
    mean, low, high = value_evidence.clustered_mean_interval([1, 1, 2, 3], [15.0, -10.0, -10.0, 5.0], [10.0] * 4, rng)
    assert mean == pytest.approx(0.0 / 40.0) and low <= mean <= high
    with pytest.raises(ValueError):
        value_evidence.clustered_mean_interval([1], [1.0], [0.0], rng)
    assert math.isinf(value_evidence.required_clusters([1, 2], [-0.1, 0.05]))   # no positive mean: never enough
    assert value_evidence.required_clusters([1, 2, 3], [0.10, 0.02, 0.06]) == pytest.approx((1.645 * 0.04 / 0.06) ** 2)


def _rows(n: int, seed: int = 1) -> list[tuple[int, float, int]]:
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.2, 0.7, n)
    return [(i // 10, float(pi), int(rng.uniform() < pi)) for i, pi in enumerate(p)]


def test_a_line_gets_no_metric_below_the_floor_and_no_judgement_below_the_gate_sample() -> None:
    rng = np.random.default_rng(7)
    few = value_evidence.line_evidence(_rows(10), [None] * 10, rng)
    assert few["judged"] is False and "brier" not in few and "no metric below 30" in few["note"]
    some = value_evidence.line_evidence(_rows(120), [0.4] * 60 + [None] * 60, rng)
    assert some["judged"] is False and "120 of the 500 rows" in some["note"]
    assert {"brier", "brier_skill", "brier_skill_90", "calibration_slope", "brier_close"} <= set(some)
    assert some["n_closing"] == 60 and "market_benchmark" not in some  # under 200 closing prices: not fitted
    full = value_evidence.line_evidence(_rows(600), [None] * 600, rng)
    assert full["judged"] is True and "note" not in full


def test_tips_show_value_only_on_the_gate3_sample_with_the_clv_lower_bound_above_zero() -> None:
    rng = np.random.default_rng(11)
    small = value_evidence.tip_evidence([Tip(1, "tackles", 1.5, 10.0, 15.0, 0.08)] * 20, rng)
    assert small["value_shown"] is False and small["note"].startswith("no value claim: 20 settled tips in 1 fixtures")
    noise = np.random.default_rng(2)
    good = [Tip(i % 80, "tackles", 1.5, 10.0, float(noise.choice([15.0, -10.0])), float(0.05 + noise.normal(0, 0.02)))
            for i in range(320)]
    entry = value_evidence.tip_evidence(good, rng)
    assert entry["gate3_sample"] and entry["clv_low"] > 0.0 and entry["value_shown"] is True
    bad = [Tip(t.fixture_id, t.market, t.line, t.stake, t.profit, -0.05) for t in good]
    assert value_evidence.tip_evidence(bad, rng)["value_shown"] is False
    with pytest.raises(ValueError):
        Tip(1, "tackles", 1.5, 0.0, 1.0, None)


def test_evidence_scores_eligible_predictions_against_book_outcomes_by_selection() -> None:
    predictions: list[dict[str, Any]] = [
        {"fixture_id": f, "player_id": p, "market": "tackles", "line": 1.5, "p_win": 0.36, "p_lose": 0.54}
        for f in range(40) for p in (1, 2)]
    outcomes = {(f, 1, "tackles", 1.5): f % 2 for f in range(40)}  # only player 1 has a book outcome
    report = value_evidence.evidence(predictions, outcomes, {}, [], {"tackles": (0.5, 1.5)}, np.random.default_rng(5))
    line = report["lines"]["tackles over 1.5"]
    assert (line["eligible_predictions"], line["n"], line["n_fixtures"]) == (80, 40, 40)
    assert line["mean_predicted"] == pytest.approx(0.4) and line["observed_rate"] == pytest.approx(0.5)
    assert report["lines"]["tackles over 0.5"]["n"] == 0 and report["tips"]["tackles"]["n"] == 0
    tips = [Tip(f, "tackles", 1.5, 10.0, 15.0 if f % 2 else -10.0, None) for f in range(6)] + [Tip(9, "tackles", 0.5, 10.0, 4.0, None)]
    split = value_evidence.evidence(predictions, outcomes, {}, tips, {"tackles": (0.5, 1.5)}, np.random.default_rng(5))
    assert [split["tips"][key]["n"] for key in ("tackles over 0.5", "tackles over 1.5", "tackles")] == [1, 6, 7]
    assert split["tips"]["tackles over 1.5"]["roi"] == pytest.approx(15.0 / 60.0) and not split["tips"]["tackles"]["value_shown"]
    with pytest.raises(ValueError):
        value_evidence.over_probability({"p_win": 0.0, "p_lose": 0.0})


# ---------------- shadow picks' closing value (closing-price review, 2026-10-25) ----------------
def _pick(fixture_id: int = 1, player_id: int = 7, odds: float = 3.0, book: str = "bet365") -> dict[str, Any]:
    return {"recorded_at": "2026-11-01T12:00:00+00:00", "kickoff_at": "2026-11-01T15:00:00+00:00", "fixture_id": fixture_id,
            "player_id": player_id, "market": "tackles", "line": 1.5, "side": "over", "book": book, "odds": odds}


def _close(over: float, under: float | None, at: str, book: str = "bet365", player_id: int = 7) -> dict[str, Any]:
    return {"fixture_id": 1, "player_id": player_id, "market": "tackles", "line": 1.5, "book": book, "over": over,
            "under": under, "captured_at": at}


def test_a_shadow_picks_closing_value_uses_its_own_books_last_close_before_kickoff() -> None:
    closing = [
        _close(2.6, None, "2026-11-01T14:40:00+00:00"),
        _close(2.5, 1.5, "2026-11-01T14:50:00+00:00"),            # the last close before kickoff: used
        _close(1.9, 1.9, "2026-11-01T15:05:00+00:00"),            # after kickoff: never
        _close(2.0, 1.8, "2026-11-01T14:55:00+00:00", book="betway"),  # another book: never
        _close(9.0, 1.1, "2026-11-01T11:00:00+00:00"),            # before the pick was made: never
    ]
    [(fixture, price, clv)] = value_evidence.shadow_closing_values([_pick()], closing)
    assert fixture == 1 and price == pytest.approx(3.0 / 2.5 - 1.0)
    assert clv == pytest.approx(3.0 * (1 / 2.5) / (1 / 2.5 + 1 / 1.5) - 1.0)
    assert value_evidence.shadow_closing_values([_pick(book="skybet")], closing) == []
    one_sided = value_evidence.shadow_closing_values([_pick()], closing[:1])
    assert one_sided == [(1, pytest.approx(3.0 / 2.6 - 1.0), None)]
    entry = value_evidence.shadow_evidence([_pick(), _pick(player_id=8)], closing, np.random.default_rng(1))
    assert (entry["picks"], entry["with_close"], entry["with_two_way_close"], entry["judged"]) == (2, 1, 1, False)
    assert entry["price_value"] == pytest.approx(0.2) and entry["clv"] == pytest.approx(clv)
    assert "clv" not in value_evidence.shadow_evidence([_pick()], closing[:1], np.random.default_rng(1))
