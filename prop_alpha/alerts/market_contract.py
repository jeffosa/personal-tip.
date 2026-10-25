"""Which bookmakers' tackles and fouls markets have a verified settlement contract (MARKET_CONTRACT.md), and the gate that
keeps every other real-price tip from being sent (audit 2026-10-25: "if the model cannot be shown to predict the statistic
the bookmaker settles, label value estimates unverified and keep tipping disabled").

A contract is verified only when the bookmaker's own rules and its settlements show which statistic, which void rule and
which period settle the market, and every compared settlement agrees (MARKET_CONTRACT.md section 7). Until then a bet's
"expected profit" compares the model's count with a price on a count that may be a different one, so it is not an
estimate of value at all. Both scans still price, log their predictions and run the allocator and the high-value screen
(their summaries count what was withheld); only the send is stopped. Mock runs are not gated: their prices are the
model's own, labelled MOCK, and test the message path.

Adding a contract is a maintainer decision recorded in MARKET_CONTRACT.md with its evidence, never an agent's.

Pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

from typing import Final, Mapping, Sequence

from prop_alpha.scanner.market_scanner import StakingDirective

# (bookmaker, market) -> the void rule its verified contract settles by (an engine.VoidRule value). Empty: no bookmaker's
# tackles or fouls rules are verified (MARKET_CONTRACT.md, 2026-10-25).
VERIFIED_CONTRACTS: Final[Mapping[tuple[str, str], str]] = {}
WITHHELD_NOTE: Final[str] = (
    "Tips withheld: {count} (the bookmaker's settlement rules for the market are not verified, MARKET_CONTRACT.md); "
    "their expected profits compare the model's count with a price that may be on another count, so they are unverified "
    "estimates, not value"
)


def contract_status(contracts: Mapping[tuple[str, str], str] | None = None) -> str:
    """Pure: one line for a scan summary saying which bookmaker markets may be tipped on real prices."""
    known = VERIFIED_CONTRACTS if contracts is None else contracts
    listed = ", ".join(f"{book} {market}" for book, market in sorted(known))
    return ("real-price tips are sent only under a verified settlement contract: "
            + (listed if listed else "none yet, so none is sent (MARKET_CONTRACT.md)"))


def contract_verified(book: str, market: str, void_rule: str,
                      contracts: Mapping[tuple[str, str], str] | None = None) -> bool:
    """Pure: whether `book`'s `market` has a verified contract settling by `void_rule` (book names compared in lower case;
    `contracts` defaults to VERIFIED_CONTRACTS)."""
    known = VERIFIED_CONTRACTS if contracts is None else contracts
    return known.get((book.strip().lower(), market)) == void_rule


def withhold_unverified(directives: Sequence[StakingDirective],
                        contracts: Mapping[tuple[str, str], str] | None = None,
                        ) -> tuple[list[StakingDirective], int]:
    """Pure: the directives that may be sent (a verified contract for their book, market and void rule) and how many were
    withheld."""
    kept = [d for d in directives if contract_verified(d.sportsbook, d.market_type, d.void_rule, contracts)]
    return kept, len(directives) - len(kept)
