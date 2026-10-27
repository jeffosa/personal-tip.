# Gate 3 (paper betting and CLV): code-to-requirement trace and maintainer decision memo

**Status:**
- Prepared 2026-10-26 (UTC) for maintainer decision.
- The PRD, the thresholds, the contract-evidence standard and the frozen model are unchanged. Nothing was bet, and shadow picks are not counted anywhere.
- Only defects where the PRD is unambiguous were fixed (section 2), with tests in `tests/test_gate3_paper_bets.py`.
- Everything the PRD leaves open is put to the maintainer in section 4. No option there is in force.

## 1. Trace: PRD Gate 3 clause → code → status

The PRD text (`prop_alpha_PRD_v3.1.md`, Gate 3):
- **Prerequisite:** "The projection-refresh job … and a bet-ledger writer are built." The writer MUST:
  - "persist every `StakingDirective` to `bets` with `is_paper = TRUE`, linked to `model_projections.projection_id` and `odds_lines.line_id`";
  - "at kickoff, set `closing_line_id` to the latest `odds_lines` row for the same `(fixture_id, sportsbook, market_type, player_id, line_value)` with `is_closing = TRUE`";
  - "compute `clv` with `closing_line_value`".
- **Criteria:**
  1. "≥ 300 settled paper bets across ≥ 60 distinct fixtures".
  2. CLV = O_taken · p_fair_close − 1 (de-vigged close of the side taken); fixture-clustered bootstrap, 10,000 resamples, seed archived; 90% lower bound > 0.
  3. Power check, n ≥ (1.645 σc / mean CLV)² clusters; "Do not lower the bar".
  4. "100% of paper bets reconcile to the realised `player_match_stats` outcome and the book's void rule, with no manual overrides."
- **Gate 4 item 4:** the bench-substitution constant 0.35 "MUST be replaced by a per-player estimate before Gate 3 results are accepted".
- **PRD §1.2:** "The system MUST NOT risk capital until every gate in Section 5 has passed."

| PRD clause | Where it lives in the code | Status |
|---|---|---|
| Projection-refresh job | Not built. The scan prices from history every 4 hours and persists nothing to `model_projections`. | **Not met [N].** Since this change it is a Gate 3 blocker (`value_evidence.GATE3_PREREQUISITES`). |
| Ledger writer: every `StakingDirective` stored with `is_paper` | The `bets` table exists in `schema.sql` and is never written. Stand-ins: see the table below. | **Not met [N]**: a blocker. Which records may count is the main open question (D1). |
| `closing_line_id`: the same book's latest closing row | `alerts/closing.py` captures every bookmaker's prices 0–15 minutes before kickoff into `closing_quotes.jsonl`. `ledger.closing_value` takes the same fixture, player, market, line and **book**, the latest capture, two-way quotes only. | **Equivalent [T]**, file-based rather than `odds_lines`. The "close" may be up to 15 minutes before kickoff. Live: 0 closing prices captured; capture needs the machine on. |
| `clv` with `closing_line_value` | `ledger.closing_value` calls `backtest.closing_line_value`: O_taken × de-vigged probability − 1, proportional de-vig. One-sided "k+" ladders give None. | **Met [T]**; ladders: D4. |
| Criterion 1, sample | `value_evidence.tip_evidence`: `gate3_sample` = 300 settled tips in 60 fixtures. | **Defect fixed** (section 2). Bets without a closing price still count towards the 300: D3. |
| Criterion 2, CLV interval | `value_evidence.clustered_mean_interval`: fixture resampling, `BOOTSTRAP_RESAMPLES` = 10,000, seed `lockbox.PROTOCOL.seed` (20261027). `gate3_result` fails Gate 3 when the interval lies below 0. | **Met [T].** |
| Criterion 3, power | `value_evidence.required_clusters`: σc is the SD of per-fixture mean CLV; compared with the fixtures that have a CLV. | **Met [T]**, with σc read as the SD of cluster means. |
| Criterion 4, settlement integrity | `replies.reconcile` → `value_evidence.settlement_integrity` | **Defect fixed** (section 2). |
| Gate 4 item 4, bench probability before Gate 3 | Not built: `pipeline` uses `bench_sub_probability` = 0.35. | **Not met [N]**: a blocker. |
| PRD §1.2, no capital at risk before every gate | Betting is manual. Gate 3's population and the contract-evidence standard both draw on bets settled by a bookmaker (below). | **In tension**: D1, D2. |

**Gate 3 data model today:**

| Record | Created by | Real money? | Settled by | Counted for Gate 3? |
|---|---|---|---|---|
| `placed_bets.jsonl` (`replies.PlacedBet`) | Your `placed …` reply to an alert | **Yes**: a bet you placed | Automatic (`replies.grade`, assumed rules) or your `settled <id> <result> <book>` | **Yes**: the population (lockbox match, placed before kickoff). A stand-in for the PRD ledger. |
| `sent_alerts.jsonl` | Each sent alert | No | Not settled | No |
| `shadow_picks.jsonl` | Directives withheld by the contract gate | No | Not settled | **No, never** (instruction) |
| `pilot_paper_bets.jsonl` | Each sent pilot tip (paper) | No | Automatic from the pilot counts; your `result …` | No: pilot leagues have no frozen model or lockbox |

## 2. Defects fixed (unambiguous against the PRD) [T]

**1. Settlement integrity required a bookmaker's settlement.**
- **Before:** `replies.reconcile` classed every automatic result as `unconfirmed`, and Gate 3 counted that as incomplete. A bet could reconcile only through your manual result from the tipped book (`agree`).
- **Why it was wrong:** criterion 4 reconciles to "the realised `player_match_stats` outcome and the book's void rule". It asks for no bookmaker's settlement, and a paper bet cannot have one.
- **Now:** `reconcile(..., rules_verified=True)` re-grades an automatic result on the statistics stored now, when the bet's book has a verified contract on the count and void rule automatic settlement uses (`market_contract.automatic_settlement_verified`: `MODEL_STATISTICS` and `VOID_IF_NO_APPEARANCE`). The outcomes:
  - `verified_rules_agree` counts as reconciled;
  - `verified_rules_discrepancy` (the stored statistics have changed under the recorded result) counts as failed, and as a failure for `--reconcile` too;
  - no gradable statistics counts as incomplete;
  - without a verified contract, the result stays `unconfirmed`.

  `scripts/value_evidence.py` passes the predicate for the League One/Two ledger only; `ledger --reconcile` keeps its behaviour.
- **Unchanged:** manual overrides fail; other-book and unnamed results are as before.

**2. The CLV and ROI sample needed a bookmaker's own result.**
- **Before:** `scripts/value_evidence.py` admitted only bets with a manual, own-book, won or lost settlement.
- **Now:** a bet counts only when its own book and market have a verified settlement contract and its settlement reconciled (`value_evidence.tip_qualifies`): your own-book result agreeing, or an automatic result under those verified rules. It must also have no override and be won or lost.
- **Value shown** needs Gate 3 passed and a verified contract for the book and market of every bet in the sample. Before the review of 2026-10-27, an own-book agreement at a book with no verified contract qualified, and the verdict checked only that some contract was registered.

**3. Gate 3 could pass with the PRD's prerequisites unbuilt.**
- **Now:** `gate3_result` adds each of `GATE3_PREREQUISITES` as a criterion: the projection refresh, the paper-bet ledger writer, and the per-player bench probability. All are `False` today, so Gate 3 cannot pass, whatever the sample shows. This is stricter, not looser.

**4. The report now names its population.** It says it is the bets you recorded as placed, standing in for the PRD's paper-bet ledger, and that shadow picks are never counted.

**Effect today: none.** `VERIFIED_CONTRACTS` is empty, so every automatic result stays `unconfirmed`, and Gate 3 stays INCOMPLETE (0 settled League One/Two bets).

## 3. Does the implementation wrongly require real money or a bookmaker's settlement?

- **A bookmaker's settlement: yes, until this change** (defects 1 and 2). It is now needed only where the book's rules are not verified.
- **Real money, in two places, neither required by the PRD:**
  - **(a) The population is your real placements** (`placed_bets.jsonl`). The PRD's paper bets are every directive, stored with `is_paper`, and need no placement.
  - **(b) Verifying any settlement contract needs real-money bets.** `market_contract.evidence_failures` (`MARKET_CONTRACT.md` section 10, enforced since PR #17) asks for 20 or more settlements by the same book, including discriminating examples, a substitute and a non-starter. A bookmaker settles only bets actually placed, and criterion 4 under verified rules depends on that contract.

  Both conflict with PRD §1.2 ("MUST NOT risk capital until every gate … has passed"). Changing either is a PRD-level or standard-level decision, so neither was changed (D1, D2).

## 4. Decisions for the maintainer (the PRD is ambiguous or silent)

**D1. Which records are Gate 3 paper bets while real-price tips are withheld?** The PRD persists "every `StakingDirective`" and says nothing of a send gate.
- (a) **Build the PRD ledger writer for every directive**, withheld ones included (today those are written only as shadow picks). Count a bet only if its book's contract was verified **before the bet was created**, so nothing is counted retroactively. This is the literal PRD, and it needs D2 first.
- (b) **Count only directives sent as tips under a verified contract.** Stricter; nothing accrues until a contract is verified and a market exists.
- (c) **Keep your real placements as the population** (today's stand-in). It needs real money (§1.2), and your choice of what to place biases the sample. Not recommended as Gate 3 evidence.
- **Whatever is chosen:** League One/Two have no bookmaker quoting these markets (odds probes of 8 and 26 Oct), so no League One/Two paper bet can accrue until one does. And Gate 3 follows Gate 2.

**D2. How can a settlement contract be verified without risking capital?** The PRD requires "the book's void rule" (and, implicitly, the count it settles on). It does not require bookmaker-settled examples.
- (a) **Keep the current standard.** Capital must be risked before Gate 3 can start, which conflicts with §1.2.
- (b) **Accept first-hand evidence without same-book settlements:** the bookmaker's published rules, a written support reply naming the statistic, provider and void rule, and a check of the provider's own definition against Sportmonks counts. This needs an explicit change to `MARKET_CONTRACT.md` section 10 and `evidence_failures`; it is a weaker standard, so it is the maintainer's call.
- (c) **Authorise a capped number of minimum-stake verification bets** as a written exception to §1.2.

**D3. Bets with no captured closing price.** The PRD's writer computes CLV for every bet, so each counted bet would have one. Today such bets count towards the 300, while the CLV interval uses only the priced ones; capture depends on uptime, so that subset may be biased.
- (a) **Count only bets with a CLV**, needing 300 priced bets in 60 fixtures. Stricter.
- (b) **Keep it as is.**

**D4. One-sided "k+" ladders.** The PRD's CLV needs a de-vigged close, and a ladder can be de-vigged only at the 0.5 line. Ladder bets on other lines then never have a CLV.
- (a) **Keep it so** (no change).
- (b) **Approve an alternative de-vig method.** That would be a PRD change.

**D5. Restricting the population to lockbox matches placed before kickoff** (frozen model, prospective period). This is a project choice the PRD does not state. It is stricter; confirm or reject it.

## 5. Evidence still needed before Gate 3 can pass

1. The prerequisites built: the projection refresh, the paper-bet ledger writer (D1), and the per-player bench probability.
2. A verified settlement contract for each book and market tipped (D2).
3. A bookmaker quoting League One/Two tackles or fouls, or a scope decision (D1).
4. At least 300 settled, reconciled paper bets in at least 60 fixtures, with same-book two-way closing prices (D3). That needs the machine on at kickoff.
5. A CLV 90% lower bound above 0 with the power check met.
6. Gate 2 passed first (the gates are sequential).
