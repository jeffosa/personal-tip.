"""Model-value evidence report (audit 2026-10-25): settlement agreement, then calibration, Brier skill, ROI and closing-line
value, measured only on rows that can be evidence (evaluation/value_evidence.py).

1. Settlement: every settled bet in both ledgers (`placed_bets.jsonl`, `pilot_paper_bets.jsonl`) reconciled against the
   automatic settlement (replies.reconcile): agreement and discrepancy at the tipped book, another book's results, results
   that could not be checked, and automatic results no bookmaker has confirmed, reported separately.
2. Predictions: the League One/Two prediction log filtered by the lockbox protocol (evaluation/lockbox.py); the pilot log
   is counted and left out (no frozen pricing, no protocol, and its matches were inspected on 2026-10-25 by
   scripts/screen_replay.py and scripts/edge_calibration.py).
3. Metrics per market and line against bookmaker-settled outcomes of the same book and the de-vigged closing prices
   captured before kickoff; the tips' ROI and CLV with fixture-clustered 90% intervals, over the bets whose settlement
   reconciled: the tipped book's own result agreeing, or an automatic result under that book's verified settlement
   contract (PRD Gate 3 criterion 4 reconciles to the outcome and the book's void rule; it needs no bookmaker's result).
4. Gate 3 over that population, which stands in for the PRD's paper-bet ledger (not built; GATE3_PREREQUISITES): it
   cannot pass until the prerequisites are built (GATE3_DECISION_MEMO.md).

Read-only on both databases; writes only `.prop_alpha_state/analysis/value_evidence_<date>.json`. It scores nothing for a
gate and is not the lockbox scorer (scripts/gate2_prospective.py); it never looks at an outcome the lockbox has not
released: an outcome here is a bookmaker's settlement, recorded by hand.

    .venv\\Scripts\\python.exe scripts\\value_evidence.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts import ledger  # noqa: E402
from prop_alpha.alerts.closing import load_closing_quotes  # noqa: E402
from prop_alpha.alerts.market_contract import (  # noqa: E402
    automatic_settlement_verified,
    contract_status,
)
from prop_alpha.alerts.ledger import PlacedBetStore  # noqa: E402
from prop_alpha.alerts.pilot_ledger import (  # noqa: E402
    PILOT_BETS_FILE,
    PILOT_CLOSING_FILE,
    PILOT_SETTLEMENT_SQL,
    load_closing,
)
from prop_alpha.alerts.shadow_picks import PILOT_SHADOW_FILE, SHADOW_FILE, ShadowPickLog  # noqa: E402
from prop_alpha.alerts.predictions import PredictionLog  # noqa: E402
from prop_alpha.alerts.replies import (  # noqa: E402
    PlacedBet,
    Reconciliation,
    profit,
    reconciliation_summary,
    reconciliation_text,
)
from prop_alpha.config import DEFAULT_DSN, DEFAULT_PILOT_DSN, DSN_ENV  # noqa: E402
from prop_alpha.evaluation import lockbox, value_evidence  # noqa: E402

STATE: Final[Path] = ROOT / ".prop_alpha_state"
PILOT_PREDICTIONS: Final[Path] = STATE / "pilot_predictions.jsonl"
KICKOFFS_SQL: Final[str] = "SELECT vendor_fixture_uid, kickoff_at FROM prop_alpha.fixtures WHERE vendor_fixture_uid = ANY(%s)"


def stored_kickoffs(dsn: str, fixture_ids: Sequence[int]) -> dict[int, datetime]:
    import psycopg2

    if not fixture_ids:
        return {}
    conn = psycopg2.connect(dsn, options="-c statement_timeout=30000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(KICKOFFS_SQL, ([str(f) for f in fixture_ids],))
            return {int(uid): kickoff.astimezone(timezone.utc) for uid, kickoff in cur.fetchall()}
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    now = datetime.now(timezone.utc)
    dsn = os.environ.get(DSN_ENV, DEFAULT_DSN)
    report: dict[str, Any] = {"run_at": now.isoformat(), "contract": contract_status(), "settlement": {}}

    print("1. Settlement agreement (bookmaker result v automatic settlement on stored Sportmonks counts)")
    reconciled: dict[str, list[tuple[PlacedBet, Reconciliation]]] = {}
    # An automatic League One/Two result can reconcile under its book's verified contract (PRD Gate 3 criterion 4 needs no
    # bookmaker's settlement); the pilot settles on its own counts and is not in the Gate 3 population.
    for name, store, db, sql, verified in (
            ("League One/Two ledger", PlacedBetStore(), dsn, ledger.SETTLEMENT_SQL,
             lambda bet: automatic_settlement_verified(bet.book, bet.market)),
            ("pilot paper tips", PlacedBetStore(PILOT_BETS_FILE), DEFAULT_PILOT_DSN, PILOT_SETTLEMENT_SQL, None)):
        items = ledger.reconcile_settlements(store, db, now, sql, verified)
        reconciled[name] = items
        counts = Counter(item.outcome for _, item in items)
        whole = value_evidence.settlement_integrity([item for _, item in items])
        report["settlement"][name] = {"outcomes": dict(counts), "integrity": whole.status}
        print(f"  {name}: {len(items)} settled bet(s); settlement integrity over all of them: {whole.status.upper()} "
              f"({whole.reconciled} reconciled, {len(whole.failed)} failed, {len(whole.incomplete)} incomplete)")
        for bet, item in items:
            if item.outcome != "unconfirmed":
                print("    " + reconciliation_text(bet, item))
        print("    " + reconciliation_summary([item for _, item in items]))

    print("2. Predictions that can be evidence")
    records = PredictionLog(STATE / "predictions.jsonl").load()
    candidates = sorted({int(r["fixture_id"]) for r in records if lockbox.record_reason(r) is None})
    selection = lockbox.eligible(records, stored_kickoffs(dsn, candidates))
    pilot_lines = PredictionLog(PILOT_PREDICTIONS).load() if PILOT_PREDICTIONS.is_file() else []
    report["predictions"] = {"logged": len(records), "eligible": len(selection.eligible), "excluded": selection.excluded,
                             "pilot_logged_not_evidence": len(pilot_lines)}
    print(f"  League One/Two: {len(records)} logged, {len(selection.eligible)} eligible (kickoff from "
          f"{lockbox.PROTOCOL.start:%Y-%m-%d}, digest {lockbox.PROTOCOL.pricing_digest[:8]}, {lockbox.PROTOCOL.data_vendor})")
    for reason, count in sorted(selection.excluded.items()):
        print(f"    not eligible: {count} - {reason}")
    print(f"  pilot: {len(pilot_lines)} logged, none evidence (no frozen pricing or protocol; matches inspected 2026-10-25)")

    print("3. Metrics (bookmaker-settled outcomes of the tipped book only; closing prices captured before kickoff)")
    real_settled, pilot_settled = PlacedBetStore().settled_bets(), PlacedBetStore(PILOT_BETS_FILE).settled_bets()
    outcomes, left_out = value_evidence.book_results([*real_settled, *pilot_settled])
    quotes = load_closing_quotes(STATE)
    closing = value_evidence.closing_over(quotes)
    kickoff = {int(r["fixture_id"]): datetime.fromisoformat(str(r["kickoff_at"])) for r in selection.eligible}
    tips = []
    not_tips: Counter[str] = Counter()
    tip_books: list[tuple[str, str]] = []
    reconciliation = {bet.bet_id: item for bet, item in reconciled["League One/Two ledger"]}
    for bet, settlement in real_settled:
        if bet.fixture_id not in kickoff or bet.placed_at >= kickoff[bet.fixture_id]:
            not_tips["not a lockbox match, or placed after kickoff"] += 1
        elif not automatic_settlement_verified(bet.book, bet.market):
            not_tips["no verified settlement contract for its book and market"] += 1
        elif not value_evidence.tip_qualifies(reconciliation[bet.bet_id], settlement.result, contract_verified=True):
            not_tips["not reconciled (own-book result or verified rules), overridden, or void"] += 1
        else:
            tip_books.append((bet.book, bet.market))
            value = ledger.closing_value(bet, quotes)
            tips.append(value_evidence.Tip(bet.fixture_id, bet.market, bet.line, bet.stake, profit(bet, settlement),
                                           None if value is None else value.clv))
    if pilot_settled:
        not_tips["pilot paper tip (pilot matches are not evidence)"] += len(pilot_settled)
    # Gate 3 criterion 4 covers every settled paper bet of the Gate 3 population (a lockbox match, placed before kickoff),
    # including those left out of the CLV sample (automatic result only, another book's result, a void): each must
    # reconcile, with no manual override (value_evidence.settlement_integrity). Pilot bets are not in the population.
    population = [item for bet, item in reconciled["League One/Two ledger"]
                  if bet.fixture_id in kickoff and bet.placed_at < kickoff[bet.fixture_id]]
    integrity = value_evidence.settlement_integrity(population)
    result = value_evidence.evidence(selection.eligible, outcomes, closing, tips, lockbox.PROTOCOL.lines,
                                     np.random.default_rng(lockbox.PROTOCOL.seed), integrity=integrity)
    report.update(result, outcomes_left_out=left_out, tips_left_out=dict(not_tips), closing_quotes=len(quotes),
                  two_way_closing=len(closing))
    print(f"  bookmaker-settled outcomes: {len(outcomes)}; left out: "
          + (", ".join(f"{n} {why}" for why, n in sorted(left_out.items())) or "none"))
    print(f"  closing quotes captured: {len(quotes)} ({len(closing)} two-way selections)")
    for name, entry in result["lines"].items():
        print(f"  {name}: {entry['eligible_predictions']} eligible predictions, {entry['n']} with a bookmaker-settled "
              f"outcome, {entry['n_closing']} with a closing price - " + entry.get("note", "metrics in the JSON report"))
    for market, entry in result["tips"].items():
        print(f"  {market} tips: {entry['note']}")
    print("  tips left out: " + (", ".join(f"{n} {why}" for why, n in sorted(not_tips.items())) or "none"))
    print("4. Shadow picks (withheld picks kept unsent; not tips, not Gate 3 evidence): did their prices shorten by kickoff?")
    pilot_closing = load_closing(PILOT_CLOSING_FILE)
    report["shadow"] = {}
    for name, log, closing_quotes in (("League One/Two", ShadowPickLog(SHADOW_FILE), quotes),
                                      ("pilot", ShadowPickLog(PILOT_SHADOW_FILE), pilot_closing)):
        entry = value_evidence.shadow_evidence(log.load(), closing_quotes, np.random.default_rng(lockbox.PROTOCOL.seed))
        report["shadow"][name] = entry
        price_text = (f"; price value {entry['price_value']:+.1%} (90% {entry['price_value_low']:+.1%} to "
                 f"{entry['price_value_high']:+.1%})" if "price_value" in entry else "")
        clv = (f"; de-vigged CLV {entry['clv']:+.1%} (90% {entry['clv_low']:+.1%} to {entry['clv_high']:+.1%})"
               if "clv" in entry else "")
        print(f"  {name}: {entry['picks']} shadow picks, {entry['with_close']} with a same-book closing price "
              f"({entry['with_two_way_close']} two-way) in {entry['fixtures']} matches{price_text}{clv}; reported, not judged")
    gate3 = result["gate3"]
    print(f"5. Gate 3 (all settled paper bets): {gate3['status'].upper()}")
    print("  population: the League One/Two bets you recorded as placed (placed_bets.jsonl), standing in for the PRD's paper-bet"
          " ledger (every StakingDirective persisted with is_paper), which is not built; shadow picks are never counted")
    print(f"  CLV criteria (statistical result only, never a passed gate on its own): "
          f"{'met' if gate3['clv_criteria_met'] else 'not met'}")
    print(f"  settlement integrity (100% reconciled, no manual override): {integrity.status.upper()} over "
          f"{integrity.bets} bet(s): {integrity.reconciled} reconciled, {len(integrity.failed)} failed, "
          f"{len(integrity.incomplete)} incomplete")
    for reason in [*integrity.failed, *integrity.incomplete]:
        print(f"    {reason}")
    for blocker in gate3["blockers"]:
        print(f"  not met: {blocker}")
    # Value is claimed only when Gate 3 passed (CLV and settlement integrity together) and every bet in its sample is at a
    # book and market with a verified contract (review 2026-10-27: this checked only that some contract existed).
    shown = (gate3["status"] == "passed" and bool(tip_books)
             and all(automatic_settlement_verified(book, market) for book, market in tip_books))
    report["value_shown"] = shown
    print(f"Verdict: value {'shown: Gate 3 passed' if shown else 'NOT shown'}; Gate 3 {gate3['status']}; "
          f"{contract_status()}. No value claim is made without a verified contract and a passed Gate 3.")
    out = STATE / "analysis" / f"value_evidence_{now:%Y-%m-%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"Written: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
