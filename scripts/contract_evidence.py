"""Settlement-contract evidence for one bookmaker and market (MARKET_CONTRACT.md section 10, contract_evidence/README.md).

Gathers every result recorded as that bookmaker's own settlement in both ledgers (`placed_bets.jsonl` and
`pilot_paper_bets.jsonl`: a manual result from the bet's own book, replies.same_book), adds each player's stored Sportmonks
counts and start (read-only, from the ledger's own database), merges the rules already written in
`contract_evidence/<book>_<market>.json` if it exists, and prints every criterion still missing for a contract on the
model's statistic and void rule (market_contract.evidence_failures). Another bookmaker's results and automatic results are
never examples. `--write` saves `contract_evidence/<book>_<market>.draft.json` for the maintainer to complete and review; it
never lists a contract (that is VERIFIED_CONTRACTS, the maintainer's decision, in a PR).

    .venv\\Scripts\\python.exe scripts\\contract_evidence.py --book bet365 --market tackles [--void-rule void_if_not_starting] [--write]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prop_alpha.alerts.ledger import PlacedBetStore  # noqa: E402
from prop_alpha.alerts.market_contract import (  # noqa: E402
    MODEL_STATISTICS,
    RIVAL_STATISTICS,
    Contract,
    evidence_failures,
    statistic_columns,
)
from prop_alpha.alerts.pilot_ledger import PILOT_BETS_FILE  # noqa: E402
from prop_alpha.alerts.replies import PlacedBet, Settlement, same_book  # noqa: E402
from prop_alpha.config import DEFAULT_DSN, DEFAULT_PILOT_DSN  # noqa: E402
from prop_alpha.modeling.engine import VoidRule  # noqa: E402

EVIDENCE_DIR: Final[Path] = ROOT / "contract_evidence"
ROW_SQL: Final[str] = """
    SELECT s.started, s.minutes_played, {columns}
    FROM prop_alpha.fixtures f JOIN prop_alpha.player_match_stats s ON s.fixture_id = f.fixture_id
    WHERE f.vendor_fixture_uid = %s AND s.player_id = %s AND f.status = 'final'
"""


def same_book_results(settled: Sequence[tuple[PlacedBet, Settlement]], book: str,
                      market: str) -> list[tuple[PlacedBet, Settlement]]:
    """The results `book` itself settled for `market`: manual, naming the bet's own book (replies.same_book: a result naming
    no bookmaker never counts), and the bet priced at `book`."""
    return [(bet, s) for bet, s in settled if s.source == "manual" and bet.market == market
            and bet.book.lower() == book.lower() and same_book(bet.book, s.book)]


def stored_row(dsn: str, bet: PlacedBet, columns: Sequence[str]) -> tuple[bool, int, dict[str, int]] | None:
    import psycopg2

    conn = psycopg2.connect(dsn, options="-c statement_timeout=10000 -c default_transaction_read_only=on")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(ROW_SQL.format(columns=", ".join(f"s.{c}" for c in columns)), (str(bet.fixture_id), bet.player_id))
            found = cur.fetchone()
    finally:
        conn.close()
    if found is None:
        return None
    return bool(found[0]), int(found[1] or 0), {c: int(v or 0) for c, v in zip(columns, found[2:])}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--book", required=True)
    parser.add_argument("--market", required=True, choices=sorted(MODEL_STATISTICS))
    parser.add_argument("--void-rule", default=VoidRule.VOID_IF_NO_APPEARANCE.value, choices=[r.value for r in VoidRule])
    parser.add_argument("--write", action="store_true", help="save a draft evidence file under contract_evidence/")
    args = parser.parse_args(argv)
    book, market = str(args.book).lower(), str(args.market)
    name = f"{book}_{market}.json"
    contract = Contract(MODEL_STATISTICS[market], str(args.void_rule), name)
    columns = sorted({c for s in (contract.statistic, *RIVAL_STATISTICS[market]) for c in statistic_columns(s)})
    existing = EVIDENCE_DIR / name
    evidence: dict[str, Any] = (json.loads(existing.read_text(encoding="utf-8")) if existing.is_file()
                                else {"book": book, "market": market, "rules": [], "examples": []})
    examples: list[dict[str, Any]] = []
    missing_rows: list[str] = []
    for store, dsn in ((PlacedBetStore(), DEFAULT_DSN), (PlacedBetStore(PILOT_BETS_FILE), DEFAULT_PILOT_DSN)):
        for bet, settlement in same_book_results(store.settled_bets(), book, market):
            row = stored_row(dsn, bet, columns)
            if row is None:
                missing_rows.append(f"#{bet.bet_id} {bet.player} ({bet.match}): no stored final row")
                continue
            started, minutes, counts = row
            examples.append({"book": book, "market": market, "fixture": str(bet.fixture_id), "player": bet.player,
                             "line": bet.line, "side": bet.side, "book_result": settlement.result, "started": started,
                             "minutes": minutes, "counts": counts, "settled_on": settlement.settled_at.date().isoformat(),
                             "source": f"{store.path.name} #{bet.bet_id}: {settlement.detail}"})
    evidence["examples"] = examples
    print(f"{book} {market}: {len(examples)} same-book settled result(s) in the ledgers; contract checked: "
          f"{contract.statistic}, {contract.void_rule}")
    for line in missing_rows:
        print(f"  not usable: {line}")
    failures = evidence_failures(evidence, book, market, contract)
    for failure in failures:
        print(f"  missing: {failure}")
    print("NOT VERIFIED: every criterion above must be met before the maintainer may list the contract."
          if failures else "All criteria met: the maintainer may list the contract (VERIFIED_CONTRACTS, in a PR, with a test).")
    if args.write:
        out = EVIDENCE_DIR / f"{book}_{market}.draft.json"
        out.write_text(json.dumps(evidence, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Draft written: {out.relative_to(ROOT)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
