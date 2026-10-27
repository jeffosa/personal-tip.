"""Which bookmakers' tackles and fouls markets have a verified settlement contract (MARKET_CONTRACT.md), the evidence a
contract must carry, and the gate that keeps every other real-price tip from being sent (audit 2026-10-25: "if the model
cannot be shown to predict the statistic the bookmaker settles, label value estimates unverified and keep tipping
disabled").

A contract names the stored count the bookmaker settles on and its void rule. It is listed in VERIFIED_CONTRACTS only with an
evidence file under `contract_evidence/` that meets every criterion of `evidence_failures` (MARKET_CONTRACT.md section 10):
the bookmaker's own rules, first-hand, on every topic; at least MIN_EXAMPLES same-book settlements, all agreeing with the
contract, including discriminating ones, a substitute appearance and a named player who did not play.
`tests/test_market_contract.py` refuses a listed contract whose evidence fails. A tip is sent only when its book's
contract is verified AND settles on the very count and void rule the model priced: a contract on another count or void
rule keeps the tips withheld until the pricing is changed under a new freeze and a later lockbox start.

Both scans still price, log their predictions and run the allocator and the high-value screen (their summaries count what
was withheld); only the send is stopped. Mock runs are not gated: their prices are the model's own, labelled MOCK.

Adding a contract is a maintainer decision recorded in MARKET_CONTRACT.md with its evidence, never an agent's.

Pure: no I/O, no clock, no randomness (the caller reads the evidence files).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Mapping, Sequence

from prop_alpha.alerts.replies import SETTLEMENT_COLUMNS
from prop_alpha.modeling.engine import VoidRule
from prop_alpha.scanner.market_scanner import StakingDirective


@dataclass(frozen=True)
class Contract:
    """A verified settlement contract: `statistic` is the stored count the book settles on, as a sum of
    player_match_stats columns ("s.tackles_attempted", "(s.tackles_attempted + s.dribbled_past)"); `void_rule` an
    engine.VoidRule value; `evidence` the file name under contract_evidence/."""

    statistic: str
    void_rule: str
    evidence: str

    def __post_init__(self) -> None:
        statistic_columns(self.statistic)
        VoidRule(self.void_rule)
        if not self.evidence.endswith(".json") or "/" in self.evidence or "\\" in self.evidence:
            raise ValueError("a contract's evidence is a .json file name under contract_evidence/")


# (bookmaker, market) -> its verified contract. Empty: no bookmaker's tackles or fouls rules are verified (MARKET_CONTRACT.md,
# 2026-10-25): no first-hand rules and no same-book settlement exist.
VERIFIED_CONTRACTS: Final[Mapping[tuple[str, str], Contract]] = {}
# The counts the frozen League One/Two model prices and its ledger settles (the pilot passes its own, ladder_scan).
MODEL_STATISTICS: Final[Mapping[str, str]] = {market: f"s.{column}" for market, column in SETTLEMENT_COLUMNS.items()}
# Counts a book might settle on instead (MARKET_CONTRACT.md sections 2-2e and 7): a contract's evidence must contradict each.
RIVAL_STATISTICS: Final[Mapping[str, tuple[str, ...]]] = {
    "tackles": ("s.tackles_attempted", "s.tackles_won", "(s.tackles_attempted + s.dribbled_past)",
                "(s.tackles_attempted + s.interceptions)", "(s.tackles_won + s.interceptions)"),
    "fouls": ("s.fouls_committed", "(s.fouls_committed + s.fouls_drawn)"),
}
# Every rule topic the bookmaker's own wording must cover, and the sources that count as first-hand.
RULE_TOPICS: Final[tuple[str, ...]] = ("statistic and data provider", "extra time", "player participation and void",
                                       "abandoned or postponed match", "whole-line settlement")
# A rules document is the bookmaker's own published rules as a file (Betway's "Betting Rules" PDF, 2026-10-25).
FIRST_HAND_SOURCES: Final[tuple[str, ...]] = ("bookmaker rules page", "bookmaker rules document", "bookmaker support reply")
MIN_EXAMPLES: Final[int] = 20
MIN_DISCRIMINATING: Final[int] = 3
WITHHELD_NOTE: Final[str] = (
    "Tips withheld: {count} (the bookmaker's settlement rules for the market are not verified, MARKET_CONTRACT.md); "
    "their expected profits compare the model's count with a price that may be on another count, so they are unverified "
    "estimates, not value"
)
_STATISTIC: Final[re.Pattern[str]] = re.compile(r"s\.(\w+)|\((s\.\w+(?: \+ s\.\w+)+)\)")


def statistic_columns(statistic: str) -> tuple[str, ...]:
    """Pure: the player_match_stats columns a statistic sums ("s.a" or "(s.a + s.b ...)"); anything else is refused."""
    found = _STATISTIC.fullmatch(statistic)
    if found is None:
        raise ValueError(f"statistic {statistic!r} is not s.<column> or (s.<column> + s.<column> ...)")
    if found[1] is not None:
        return (found[1],)
    return tuple(part.strip()[2:] for part in found[2].split("+"))


def contract_status(contracts: Mapping[tuple[str, str], Contract] | None = None) -> str:
    """Pure: one line for a scan summary saying which bookmaker markets may be tipped on real prices."""
    known = VERIFIED_CONTRACTS if contracts is None else contracts
    listed = ", ".join(f"{book} {market}" for book, market in sorted(known))
    return ("real-price tips are sent only under a verified settlement contract: "
            + (listed if listed else "none yet, so none is sent (MARKET_CONTRACT.md)"))


def contract_verified(book: str, market: str, void_rule: str, statistic: str,
                      contracts: Mapping[tuple[str, str], Contract] | None = None) -> bool:
    """Pure: whether `book`'s `market` has a verified contract settling on the `statistic` and `void_rule` the price was
    modelled with (book names compared in lower case; `contracts` defaults to VERIFIED_CONTRACTS)."""
    known = VERIFIED_CONTRACTS if contracts is None else contracts
    contract = known.get((book.strip().lower(), market))
    return contract is not None and contract.void_rule == void_rule and contract.statistic == statistic


def automatic_settlement_verified(book: str, market: str,
                                  contracts: Mapping[tuple[str, str], Contract] | None = None) -> bool:
    """Pure: whether the ledger's automatic settlement of a `book` bet on `market` follows that book's verified rules:
    replies.grade settles on MODEL_STATISTICS[market] and voids a player who did not play (VOID_IF_NO_APPEARANCE), so
    the contract must be on exactly those (Gate 3 criterion 4: reconciled to the outcome and the book's void rule)."""
    return market in MODEL_STATISTICS and contract_verified(
        book, market, VoidRule.VOID_IF_NO_APPEARANCE.value, MODEL_STATISTICS[market], contracts)


def split_unverified(directives: Sequence[StakingDirective], statistics: Mapping[str, str],
                     contracts: Mapping[tuple[str, str], Contract] | None = None,
                     ) -> tuple[list[StakingDirective], list[StakingDirective]]:
    """Pure: (the directives that may be sent: a verified contract for their book and market on the count the model priced,
    `statistics[market]`, and their void rule; the directives withheld), each in the given order."""
    kept: list[StakingDirective] = []
    withheld: list[StakingDirective] = []
    for d in directives:
        verified = d.market_type in statistics and contract_verified(
            d.sportsbook, d.market_type, d.void_rule, statistics[d.market_type], contracts)
        (kept if verified else withheld).append(d)
    return kept, withheld


def withhold_unverified(directives: Sequence[StakingDirective], statistics: Mapping[str, str],
                        contracts: Mapping[tuple[str, str], Contract] | None = None,
                        ) -> tuple[list[StakingDirective], int]:
    """Pure: the directives that may be sent (split_unverified) and how many were withheld."""
    kept, withheld = split_unverified(directives, statistics, contracts)
    return kept, len(withheld)


def expected_result(statistic: str, void_rule: str, line: float, side: str, started: bool, minutes: int,
                    counts: Mapping[str, int]) -> str:
    """Pure: the result a contract on `statistic` and `void_rule` gives one selection: void for a player its rule voids, void
    for a count on a whole line (a push), else won or lost for `side`."""
    rule = VoidRule(void_rule)
    if (rule is VoidRule.VOID_IF_NO_APPEARANCE and minutes == 0) or (rule is VoidRule.VOID_IF_NOT_STARTING and not started):
        return "void"
    count = sum(int(counts[column]) for column in statistic_columns(statistic))
    if count == line:
        return "void"
    return "won" if (count > line) == (side == "over") else "lost"


def evidence_failures(evidence: Mapping[str, Any], book: str, market: str, contract: Contract) -> list[str]:
    """Pure: every reason `evidence` (a parsed contract_evidence/ file) does not verify `contract` for `book`'s `market`; an
    empty list means it does. Malformed evidence raises ValueError."""
    failures: list[str] = []
    if str(evidence.get("book", "")).lower() != book.lower() or evidence.get("market") != market:
        failures.append(f"the evidence is not for {book} {market}")
    covered: set[str] = set()
    for rule in evidence.get("rules", []):
        if rule.get("source") not in FIRST_HAND_SOURCES or not str(rule.get("wording", "")).strip() \
                or not str(rule.get("url_or_document", "")).strip():
            failures.append(f"rule on {rule.get('topic')!r} is not the bookmaker's own wording with its source")
            continue
        date.fromisoformat(str(rule["checked_on"]))
        covered.add(str(rule.get("topic")))
    failures += [f"no first-hand rule on {topic}" for topic in RULE_TOPICS if topic not in covered]
    examples: dict[tuple[str, str, float, str], Mapping[str, Any]] = {}
    for example in evidence.get("examples", []):
        if str(example.get("book", "")).lower() != book.lower() or example.get("market") != market:
            failures.append(f"example {example.get('player')!r} is another book's or market's settlement")
            continue
        if example.get("book_result") not in ("won", "lost", "void"):
            raise ValueError(f"example {example.get('player')!r} has no bookmaker result")
        examples.setdefault((str(example["fixture"]), str(example["player"]), float(example["line"]), str(example["side"])),
                            example)
    if len(examples) < MIN_EXAMPLES:
        failures.append(f"{len(examples)} same-book settled examples, {MIN_EXAMPLES} needed")
    rivals = [s for s in RIVAL_STATISTICS.get(market, ()) if s != contract.statistic]
    contradicted: set[str] = set()
    discriminating = substitutes = absent = 0
    needed = {column for statistic in (contract.statistic, *rivals) for column in statistic_columns(statistic)}
    for (fixture, player, line, side), example in examples.items():
        started, minutes, counts = bool(example["started"]), int(example["minutes"]), example["counts"]
        if not needed <= set(counts):
            raise ValueError(f"example {player!r} lacks the stored counts {sorted(needed - set(counts))}")
        expected = expected_result(contract.statistic, contract.void_rule, line, side, started, minutes, counts)
        if expected != example["book_result"]:
            failures.append(f"DISCREPANCY: {player} ({fixture}) {side} {line:g}: book {example['book_result']}, "
                            f"contract {expected}")
        outcomes = {rival: expected_result(rival, contract.void_rule, line, side, started, minutes, counts) for rival in rivals}
        beaten = {rival for rival, outcome in outcomes.items() if outcome != example["book_result"]}
        contradicted |= beaten
        discriminating += bool(beaten)
        substitutes += not started and minutes > 0
        absent += not started and minutes == 0
    if discriminating < MIN_DISCRIMINATING:
        failures.append(f"{discriminating} examples tell the rival counts apart, {MIN_DISCRIMINATING} needed")
    failures += [f"no example contradicts the rival count {rival}" for rival in rivals if rival not in contradicted]
    if not substitutes:
        failures.append("no example of a substitute appearance")
    if not absent:
        failures.append("no example of a named player who did not start or play")
    return failures
