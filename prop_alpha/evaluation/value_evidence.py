"""Model-value evidence (audit 2026-10-25): what the logged predictions and the tips are worth, measured only on rows that
can be evidence.

Rows that count:
* predictions: lockbox-eligible lines only (evaluation/lockbox.py: the frozen digest, Sportmonks, logged before kickoff,
  kickoff from the protocol start, never an examined match, earliest log of a selection), never a re-price;
* outcomes: the result a bookmaker settled for that selection, read off the book by the maintainer and recorded against
  the tip of the same book (ledger `settled`, `result`, `pilot_ledger --record --book`); another book's result, a
  provisional automatic result and a void are not outcomes here;
* closing prices: two-way closing prices captured before kickoff (alerts/closing.py), de-vigged, for the market benchmark
  and closing-line value (PRD Gate 3: CLV = odds taken x de-vigged closing probability - 1).

No threshold below is tuned on these rows; the sample floors are the PRD's (Gate 2: 500 per line and 200 for the
benchmark; Gate 3: 300 settled bets across 60 fixtures), and below them a metric is reported as not judged, never as a
pass. ROI and CLV intervals resample whole fixtures (10,000 resamples, the caller's generator).

Pure: no I/O, no clock; randomness only through the `rng` argument.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

import numpy as np

from prop_alpha.alerts.replies import Reconciliation, same_book
from prop_alpha.evaluation.backtest import brier_score
from prop_alpha.evaluation.gate2 import (
    BOOTSTRAP_RESAMPLES,
    INTERVAL_PERCENTILES,
    MIN_BENCHMARK_PREDICTIONS,
    MIN_PREDICTIONS,
    calibration_fit,
    clustered_skill_interval,
    market_benchmark,
)
from prop_alpha.scanner.market_scanner import devig_two_way

# PRD Gate 3 criterion 1.
MIN_SETTLED_BETS: Final[int] = 300
MIN_BET_FIXTURES: Final[int] = 60
# Below this many rows with an outcome no metric is computed at all (a Brier score of a handful of rows is noise).
MIN_ROWS_FOR_METRICS: Final[int] = 30
SelectionKey = tuple[int, int, str, float]


@dataclass(frozen=True)
class BookResult:
    """A bookmaker's settlement of one selection, from the book the tip was priced at: `over_won` is 1 when the Over won,
    0 when it lost (a push or a void is not an outcome and is never stored here)."""

    fixture_id: int
    player_id: int
    market: str
    line: float
    book: str
    over_won: int

    def __post_init__(self) -> None:
        if self.over_won not in (0, 1) or not self.book:
            raise ValueError("a book result is a named bookmaker's win (1) or loss (0) of the Over")


@dataclass(frozen=True)
class Tip:
    """One tip with its price and a bookmaker's settlement from the same book: `profit` per `stake` (0 for a void)."""

    fixture_id: int
    market: str
    line: float
    stake: float
    profit: float
    clv: float | None

    def __post_init__(self) -> None:
        if not (math.isfinite(self.stake) and self.stake > 0.0 and math.isfinite(self.profit)):
            raise ValueError("a tip needs a positive stake and a finite profit")
        if self.clv is not None and not math.isfinite(self.clv):
            raise ValueError("closing-line value must be finite")


def book_results(settled: Sequence[tuple[Any, Any]]) -> tuple[dict[SelectionKey, int], dict[str, int]]:
    """Pure: the Over outcome per selection from (PlacedBet, Settlement) pairs, and how many pairs were left out and why.
    Only a manual settlement from the bet's own book counts (replies.same_book); conflicting results for one selection
    leave it out."""
    outcomes: dict[SelectionKey, set[int]] = defaultdict(set)
    left_out: dict[str, int] = defaultdict(int)
    for bet, settlement in settled:
        if settlement.source != "manual":
            left_out["automatic result (assumed rules, provisional)"] += 1
        elif not same_book(bet.book, settlement.book):
            left_out["another bookmaker's result"] += 1
        elif settlement.result == "void":
            left_out["void (not an outcome)"] += 1
        else:
            won = settlement.result == "won"
            outcomes[(int(bet.fixture_id), int(bet.player_id), str(bet.market), float(bet.line))].add(
                int(won if bet.side == "over" else not won))
    result = {key: next(iter(values)) for key, values in outcomes.items() if len(values) == 1}
    conflicting = sum(1 for values in outcomes.values() if len(values) > 1)
    if conflicting:
        left_out["conflicting results for one selection"] = conflicting
    return result, dict(left_out)


def clustered_mean_interval(fixtures: Sequence[int], values: Sequence[float], weights: Sequence[float],
                            rng: np.random.Generator, resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float, float]:
    """Pure: the weighted mean sum(values) / sum(weights) and its 90% interval, resampling whole fixtures. For ROI the values
    are profits and the weights stakes; for mean CLV the weights are ones."""
    v, w = np.asarray(values, dtype=np.float64), np.asarray(weights, dtype=np.float64)
    ids = np.asarray(fixtures)
    if not (v.shape == w.shape == ids.shape) or v.size == 0 or not np.isfinite(v).all() or not (w > 0).all():
        raise ValueError("one fixture, finite value and positive weight per row, at least one row")
    _, cluster = np.unique(ids, return_inverse=True)
    n = int(cluster.max()) + 1
    num, den = np.bincount(cluster, weights=v, minlength=n), np.bincount(cluster, weights=w, minlength=n)
    counts = rng.multinomial(n, np.full(n, 1.0 / n), size=resamples).astype(np.float64)
    means = (counts @ num) / (counts @ den)
    low, high = np.percentile(means, INTERVAL_PERCENTILES)
    return float(v.sum() / w.sum()), float(low), float(high)


def required_clusters(fixtures: Sequence[int], values: Sequence[float]) -> float:
    """Pure: PRD Gate 3 criterion 3, n >= (1.645 sigma_c / mean)^2 effective clusters, with sigma_c the standard deviation of
    the per-fixture mean; inf when the mean is not positive or a single fixture gives no spread."""
    per: dict[int, list[float]] = defaultdict(list)
    for fixture, value in zip(fixtures, values):
        per[int(fixture)].append(float(value))
    means = np.array([float(np.mean(v)) for v in per.values()])
    mean = float(np.mean(values)) if len(values) else math.nan
    if len(means) < 2 or not mean > 0.0:
        return math.inf
    return float((1.645 * float(np.std(means, ddof=1)) / mean) ** 2)


def line_evidence(rows: Sequence[tuple[int, float, int]], closing: Sequence[float | None],
                  rng: np.random.Generator) -> dict[str, Any]:
    """Pure: the metrics of one market and line from (fixture, model probability, Over outcome) rows and each row's
    de-vigged closing Over probability (None when no two-way closing price was captured)."""
    if len(rows) != len(closing):
        raise ValueError("one closing entry per row")
    n = len(rows)
    entry: dict[str, Any] = {"n": n, "n_fixtures": len({r[0] for r in rows}),
                             "n_closing": sum(1 for c in closing if c is not None), "judged": False}
    if n < MIN_ROWS_FOR_METRICS:
        entry["note"] = f"{n} rows with a bookmaker-settled outcome; no metric below {MIN_ROWS_FOR_METRICS}"
        return entry
    fixtures, p, y = (list(column) for column in zip(*rows))
    base = float(np.mean(y))
    entry.update(mean_predicted=float(np.mean(p)), observed_rate=base, brier=brier_score(p, y))
    if 0.0 < base < 1.0:
        entry["brier_skill"] = 1.0 - entry["brier"] / (base * (1.0 - base))
        entry["brier_skill_90"] = clustered_skill_interval(fixtures, p, y, rng)
        fit = calibration_fit(p, y)
        entry.update(calibration_slope=fit.slope, calibration_intercept=fit.intercept, calibration_note=fit.note)
    with_close = [(c, pi, yi) for c, pi, yi in zip(closing, p, y) if c is not None]
    if with_close:
        q, pm, ym = (list(column) for column in zip(*with_close))
        entry.update(brier_model_on_closing_rows=brier_score(pm, ym), brier_close=brier_score(q, ym),
                     mean_close=float(np.mean(q)))
        if len(with_close) >= MIN_BENCHMARK_PREDICTIONS:
            bench = market_benchmark(q, pm, ym)
            entry["market_benchmark"] = {"n": bench.n, "b_model": bench.b_model, "p_value": bench.p_value,
                                         "passed": bench.passed, "note": bench.note}
    entry["judged"] = n >= MIN_PREDICTIONS
    if not entry["judged"]:
        entry["note"] = f"{n} of the {MIN_PREDICTIONS} rows Gate 2 needs per line: reported, not judged"
    return entry


def tip_evidence(tips: Sequence[Tip], rng: np.random.Generator) -> dict[str, Any]:
    """Pure: ROI and closing-line value of tips settled by their own book, with fixture-clustered 90% intervals and the
    Gate 3 sample and power checks. Never a value claim below the Gate 3 sample."""
    entry: dict[str, Any] = {"n": len(tips), "n_fixtures": len({t.fixture_id for t in tips}),
                             "n_clv": sum(1 for t in tips if t.clv is not None)}
    enough = entry["n"] >= MIN_SETTLED_BETS and entry["n_fixtures"] >= MIN_BET_FIXTURES
    if tips:
        entry["roi"], entry["roi_low"], entry["roi_high"] = clustered_mean_interval(
            [t.fixture_id for t in tips], [t.profit for t in tips], [t.stake for t in tips], rng)
    priced = [t for t in tips if t.clv is not None]
    if priced:
        fixtures, values = [t.fixture_id for t in priced], [float(t.clv) for t in priced if t.clv is not None]
        entry["clv"], entry["clv_low"], entry["clv_high"] = clustered_mean_interval(
            fixtures, values, [1.0] * len(values), rng)
        entry["clv_required_fixtures"] = required_clusters(fixtures, values)
    entry["gate3_sample"] = enough
    entry["power_met"] = bool(priced and entry["clv_required_fixtures"] <= len({t.fixture_id for t in priced}))
    # The statistical CLV result only (Gate 3 criteria 1-3). It is never "value shown" or a passed Gate 3 on its own:
    # gate3_result adds settlement integrity (criterion 4), which can fail while this is met.
    entry["clv_criteria_met"] = bool(enough and priced and entry["clv_low"] > 0.0 and entry["power_met"])
    entry["note"] = ("CLV criteria met on the Gate 3 sample (statistical result only; Gate 3 also needs settlement integrity)"
                     if entry["clv_criteria_met"] else
                     f"CLV criteria not met: {entry['n']} settled tips in {entry['n_fixtures']} fixtures (Gate 3 needs "
                     f"{MIN_SETTLED_BETS} in {MIN_BET_FIXTURES}), {entry['n_clv']} with a two-way closing price")
    return entry


# PRD Gate 3 criterion 4: 100% of paper bets reconcile to the realised outcome and the book's void rule, with no manual
# overrides. Each replies.reconcile outcome is one of these; a manual override (Reconciliation.overridden) fails a bet
# whatever its outcome. Nothing unchecked, unresolved or settled by another bookmaker counts as reconciled.
INTEGRITY_RECONCILED: Final[tuple[str, ...]] = ("agree",)
INTEGRITY_FAILED: Final[tuple[str, ...]] = ("discrepancy", "other_book_discrepancy")
INTEGRITY_INCOMPLETE: Final[tuple[str, ...]] = ("unverifiable", "unconfirmed", "other_book_agree")
INTEGRITY_STATUSES: Final[tuple[str, ...]] = ("passed", "failed", "incomplete")


@dataclass(frozen=True)
class SettlementIntegrity:
    """Gate 3 criterion 4 over a set of settled paper bets. "passed": at least one bet and every one reconciled with no
    override; "failed": any discrepancy (at the tipped book or another) or any manual override; "incomplete": no failure,
    but a bet not reconciled (no stored statistics, automatic result only, or only another bookmaker's result), or no bet
    at all. `failed` and `incomplete` give one reason per bet."""

    status: str
    bets: int
    reconciled: int
    failed: tuple[str, ...]
    incomplete: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in INTEGRITY_STATUSES:
            raise ValueError(f"unknown settlement-integrity status {self.status!r}")
        if self.reconciled + len(self.failed) + len(self.incomplete) != self.bets:
            raise ValueError("every bet is reconciled, failed or incomplete, exactly once")
        expected = "failed" if self.failed else "incomplete" if self.incomplete or not self.bets else "passed"
        if self.status != expected:
            raise ValueError(f"status {self.status!r} does not fit the counts (expected {expected!r})")


def settlement_integrity(items: Sequence[Reconciliation]) -> SettlementIntegrity:
    """Pure: Gate 3 criterion 4 over `items` (replies.reconcile results, one per settled paper bet); fails closed."""
    failed: list[str] = []
    incomplete: list[str] = []
    reconciled = 0
    for item in items:
        if item.overridden:
            failed.append(f"bet {item.bet_id}: manual override of a recorded automatic result ({item.outcome})")
        elif item.outcome in INTEGRITY_FAILED:
            failed.append(f"bet {item.bet_id}: {item.outcome} (bookmaker {item.bookmaker_result}, automatic "
                          f"{item.automatic_result})")
        elif item.outcome in INTEGRITY_INCOMPLETE:
            incomplete.append(f"bet {item.bet_id}: {item.outcome}")
        elif item.outcome in INTEGRITY_RECONCILED:
            reconciled += 1
        else:
            raise ValueError(f"bet {item.bet_id}: reconciliation outcome {item.outcome!r} is not classified")
    status = "failed" if failed else "incomplete" if incomplete or not items else "passed"
    return SettlementIntegrity(status, len(items), reconciled, tuple(failed), tuple(incomplete))


def gate3_result(sample: Mapping[str, Any], integrity: SettlementIntegrity) -> dict[str, Any]:
    """Pure: PRD Gate 3 over every settled paper bet (`sample`: tip_evidence of all of them) and their settlement
    integrity. "passed" needs every criterion; "failed" when integrity failed, or on the full sample when the CLV interval
    lies below 0 (or, with the power met, does not lie above 0); otherwise "incomplete". A met CLV criterion never passes the gate alone."""
    criteria = {
        f"sample ({MIN_SETTLED_BETS}+ settled bets in {MIN_BET_FIXTURES}+ fixtures)": bool(sample["gate3_sample"]),
        "CLV 90% lower bound above 0": bool("clv_low" in sample and sample["clv_low"] > 0.0),
        "power check": bool(sample.get("power_met", False)),
        "settlement integrity (100% reconciled, no manual override)": integrity.status == "passed",
    }
    # PRD: an interval that excludes zero from below is a failure; with the power met, a lower bound at or below 0 is too.
    clv_refuted = bool(sample["gate3_sample"] and "clv_low" in sample
                       and (sample["clv_high"] < 0.0 or (sample.get("power_met") and sample["clv_low"] <= 0.0)))
    status = ("failed" if integrity.status == "failed" or clv_refuted
              else "passed" if all(criteria.values()) else "incomplete")
    return {"status": status, "criteria": criteria, "blockers": [name for name, ok in criteria.items() if not ok],
            "clv_criteria_met": bool(sample["clv_criteria_met"]),
            "settlement_integrity": {"status": integrity.status, "bets": integrity.bets, "reconciled": integrity.reconciled,
                                     "failed": list(integrity.failed), "incomplete": list(integrity.incomplete)}}


def closing_over(quotes: Sequence[Mapping[str, Any]]) -> dict[SelectionKey, float]:
    """Pure: the de-vigged closing Over probability per (match, player, market, line), from the latest two-way capture (a
    one-sided ladder price cannot be de-vigged and is left out)."""
    latest: dict[SelectionKey, Mapping[str, Any]] = {}
    for quote in sorted(quotes, key=lambda q: str(q["captured_at"])):
        if quote.get("over") is not None and quote.get("under") is not None:
            latest[(int(quote["fixture_id"]), int(quote["player_id"]), str(quote["market"]), float(quote["line"]))] = quote
    return {key: devig_two_way(float(q["over"]), float(q["under"]))[0] for key, q in latest.items()}


def over_probability(record: Mapping[str, Any]) -> float:
    """Pure: the logged Over probability given the bet settles (the player plays): p_win / (p_win + p_lose)."""
    settled = float(record["p_win"]) + float(record["p_lose"])
    if not settled > 0.0:
        raise ValueError("a prediction that never settles has no Over probability")
    return float(record["p_win"]) / settled


def evidence(predictions: Sequence[Mapping[str, Any]], outcomes: Mapping[SelectionKey, int],
             closing: Mapping[SelectionKey, float], tips: Sequence[Tip], lines: Mapping[str, Sequence[float]],
             rng: np.random.Generator, *, integrity: SettlementIntegrity) -> dict[str, Any]:
    """Pure: per market and line, the predictions' metrics against bookmaker-settled outcomes and closing prices; per
    market, line and overall ("all") the tips' ROI and CLV; and the Gate 3 result over all tips and `integrity`, the
    settlement integrity of every settled paper bet in the Gate 3 population. `predictions` must already be the eligible
    ones (lockbox.eligible)."""
    report: dict[str, Any] = {"lines": {}, "tips": {}}
    for market, market_lines in lines.items():
        for line in market_lines:
            chosen = [r for r in predictions if str(r["market"]) == market and float(r["line"]) == float(line)]
            keyed = [((int(r["fixture_id"]), int(r["player_id"]), market, float(line)), r) for r in chosen]
            rows = [(key[0], over_probability(r), outcomes[key]) for key, r in keyed if key in outcomes]
            close = [closing.get(key) for key, _ in keyed if key in outcomes]
            report["lines"][f"{market} over {line:g}"] = {"eligible_predictions": len(chosen),
                                                          **line_evidence(rows, close, rng)}
            report["tips"][f"{market} over {line:g}"] = tip_evidence(
                [t for t in tips if t.market == market and t.line == float(line)], rng)
        report["tips"][market] = tip_evidence([t for t in tips if t.market == market], rng)
    report["tips"]["all"] = tip_evidence(tips, rng)
    report["gate3"] = gate3_result(report["tips"]["all"], integrity)
    return report


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"time {value!r} has no timezone")
    return parsed.astimezone(timezone.utc)


def shadow_closing_values(picks: Sequence[Mapping[str, Any]],
                          closing: Sequence[Mapping[str, Any]]) -> list[tuple[int, float, float | None]]:
    """Pure: (match, price value, de-vigged value or None) for each shadow pick (alerts/shadow_picks.py) with a closing price
    of its own selection at its own bookmaker, captured after the pick and no later than kickoff (the latest such capture).
    Price value = odds taken / closing odds - 1; de-vigged value = odds taken x de-vigged closing probability - 1, only when
    both sides closed (PRD Gate 3's CLV). A shadow pick is not a tip: this measures whether the prices the model liked
    shortened before kickoff, nothing about settlement."""
    values: list[tuple[int, float, float | None]] = []
    for pick in picks:
        side, other = ("over", "under") if pick["side"] == "over" else ("under", "over")
        key = (int(pick["fixture_id"]), int(pick["player_id"]), str(pick["market"]), float(pick["line"]))
        recorded, kickoff = _utc(pick["recorded_at"]), _utc(pick["kickoff_at"])
        candidates = [q for q in closing
                      if (int(q["fixture_id"]), int(q["player_id"]), str(q["market"]), float(q["line"])) == key
                      and str(q["book"]).strip().lower() == str(pick["book"]).strip().lower()
                      and q.get(side) is not None and recorded < _utc(q["captured_at"]) <= kickoff]
        if not candidates:
            continue
        close = max(candidates, key=lambda q: _utc(q["captured_at"]))
        taken, closed = float(pick["odds"]), float(close[side])
        devigged = (None if close.get(other) is None else
                    taken * devig_two_way(closed, float(close[other]))[0] - 1.0)
        values.append((key[0], taken / closed - 1.0, devigged))
    return values


def shadow_evidence(picks: Sequence[Mapping[str, Any]], closing: Sequence[Mapping[str, Any]],
                    rng: np.random.Generator) -> dict[str, Any]:
    """Pure: counts and the mean closing values of the shadow picks with fixture-clustered 90% intervals. Reported, never
    judged: shadow picks are not paper bets, so no Gate 3 criterion applies."""
    values = shadow_closing_values(picks, closing)
    entry: dict[str, Any] = {"picks": len(picks), "with_close": len(values),
                             "with_two_way_close": sum(1 for v in values if v[2] is not None),
                             "fixtures": len({v[0] for v in values}), "judged": False}
    if values:
        fixtures, price = [v[0] for v in values], [v[1] for v in values]
        entry["price_value"], entry["price_value_low"], entry["price_value_high"] = clustered_mean_interval(
            fixtures, price, [1.0] * len(price), rng)
    two_way = [(v[0], v[2]) for v in values if v[2] is not None]
    if two_way:
        entry["clv"], entry["clv_low"], entry["clv_high"] = clustered_mean_interval(
            [f for f, _ in two_way], [float(c) for _, c in two_way], [1.0] * len(two_way), rng)
    return entry
