# Settlement-contract evidence

One JSON file per bookmaker and market (`<book>_<market>.json`, shape in `TEMPLATE.json`). A file here does not verify
anything on its own: a contract is verified only when the maintainer lists it in
`prop_alpha/alerts/market_contract.VERIFIED_CONTRACTS`, and `tests/test_market_contract.py` then fails unless
`market_contract.evidence_failures` finds nothing missing (criteria: `MARKET_CONTRACT.md` section 10).

- `rules`: the bookmaker's own wording, first-hand (`bookmaker rules page`, `bookmaker rules document` or
  `bookmaker support reply`), with its URL or
  document and the date read, on each of: statistic and data provider, extra time, player participation and void,
  abandoned or postponed match, whole-line settlement. Search-engine summaries and other bookmakers' pages do not count.
- `examples`: results settled by this bookmaker (a bet slip or account history the maintainer read), at least 20 per
  market, with the stored Sportmonks counts of every rival statistic (`market_contract.RIVAL_STATISTICS`). They must include
  examples that contradict every rival count, a substitute appearance and a named player who did not start or play, and
  every one must agree with the contract. Another bookmaker's results never count.
  Each example must say where and when the bookmaker's result was read: `source` (bet slip or account history, and by
  whom) and `settled_on` (YYYY-MM-DD). An example missing either is not counted.
  A selection recorded twice with different results or counts is a CONFLICT and is not counted; resolve it in the file.
  Typed fields must have their types (`started` true/false, `minutes` and counts whole numbers, `side` over/under, a
  whole or half `line`), or the check raises. Take the counts from `scripts/contract_evidence.py --write`, never by hand.

`python scripts/contract_evidence.py --book bet365 --market tackles` gathers the same-book results recorded in both ledgers,
adds their stored counts (read-only) and prints what is still missing; `--write` saves a draft
`<book>_<market>.draft.json` here for the maintainer to complete with the rules and review.
