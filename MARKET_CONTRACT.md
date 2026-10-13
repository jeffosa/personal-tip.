# Market contract: what bet365's player tackles and fouls markets settle on

Status: **UNVERIFIED. Treat the Championship tackles and fouls ladders as unsupported** until the table in section 3 is
filled from an authoritative source and section 4's decision is made. Nothing here changes the frozen League One/Two model.

Written 2026-10-10. Betting stays manual; this file only decides whether a bookmaker's quotes can be compared with the
model's statistics.

## 1. What is known, what is a lead, what is open

| Question | Finding | Source and status |
|---|---|---|
| Data provider | bet365 settles football player statistics on **Opta** data, by Opta's definitions | Search-tool summaries of bet365 Help Centre pages "Settlement - Soccer", "Match/Team/Player Statistics - Soccer Rules" and "What is Opta FAQ". **Lead, not verified:** Claude cannot open help.bet365.com (HTTP 403 for fetches, blocked in the browser pane). The maintainer should read the pages. |
| Definition of a tackle | A tackle is a legal ground-level challenge in which the player connects with the ball and **successfully takes it away from an opponent in controlled possession**; cutting out a pass (an interception) is not a tackle | Summary of the same Opta FAQ and an Opta definition quoted by a betting-guide site. **Lead.** Whether lost tackles count in the market is NOT stated in what I saw. |
| Definition of a foul committed | Not found. No source seen ties "Player Fouls Committed" to a stated Opta definition | **Open.** |
| Player does not start | Generally "no action" (void), except in competitions with rolling substitutes | Summary of "Match/Team/Player Statistics". **Lead.** |
| Player takes no part | A player who takes no part is void; any part in the match makes him a runner (the example given is a different market) | Summary of the Help Centre house rules. **Lead, may not apply to these markets.** |
| Substitutes | A substitute who comes on is not stated to be void; promotions ("Sub On, Play On", substitute guarantee) have their own terms and are not the default rule | Third-party promotion summaries. **Open for the default market.** |
| Extra time | Player match markets include extra time where played | Summary of the soccer rules. **Lead.** |
| Whole-number lines | The Championship markets are "k+" ladders, so there is no push: "k+" is count >= k, "0" is count = 0, "Yes" is count >= 1 | OddsPapi catalogue (`players-tackles`, `players-foulscommitted`). Verified as structure, not as rules. |
| Corrections after settlement | Not found | **Open.** |

## 2. What Sportmonks' own numbers say (no rule inferred from them)

Sportmonks' plain `tackles_attempted` and `fouls_committed` counts sit well **below** the events bet365's prices imply, and
`tackles_won` sits far below. Two different tests pointed to different Sportmonks combinations (`tackles_won + interceptions`
before the match; `tackles_attempted + tackles_won` or `+ interceptions` after it, on one match of 24 players), and the
bet365/Opta wording above says interceptions are not tackles. So **no combination is identified**, and the model's current
target (`tackles_attempted`, `fouls_committed`) is not shown to be what the book settles. Do not infer the rules from odds
ratios or one match's outcomes.

### 2a. Team-level totals and the margin caveat (added 2026-10-10)

- Sportmonks' team totals for West Ham v QPR: West Ham tackles attempted 13, won 8, interceptions 5, fouls committed 9;
  QPR tackles attempted 14, won 10, interceptions 9, fouls committed 9.
- Two third-party sites (search summaries, provider not stated, one a live snapshot at 92+4') show team fouls 9-9 (FootyStats),
  8-8 (WinDrawWin) and team tackles 13-11 (FootyStats). Sportmonks matches the fouls 9-9 and West Ham's 13 tackles
  attempted but not QPR's 11 (it has 14 attempted, 10 won). These sites are **not** attributed to Opta here, so this
  neither confirms nor refutes the settlement statistic; it only shows Sportmonks' plain counts are in line with some other feed
  at team level.
- Earlier notes read the gap between bet365's implied probabilities and Sportmonks' frequencies as a definition mismatch,
  assuming a margin of about 5-10%. **That margin was never measured.** The "0" step that would let it be measured (with
  "1+" it covers every outcome) is not quoted next to "1+" in the archived replies. Player-prop ladders can carry much larger
  margins, and larger on long odds, which would also make the gap grow with the step. So a definition mismatch is
  **possible, not established**, and a large margin plus longshot shading is an equally live explanation.
- The 2026-10-08 13:33 UTC archive shows ladders on 3 Championship fixtures (the first archive had 1 of 12).

### 2b. Brazil Serie A ladders against five candidate counts (added 2026-10-12, after tipping was paused)

`scripts/pilot_gap_diagnosis.py` (offline, read-only): the bet365 and Betway tackles ladders archived at 07:35 UTC on
2026-10-11 (7 Brazil fixtures; 524 player ladders matched to stored players, 394 with 5+ stored starts), against each
player's own last 20 starts of 70+ minutes in `prop_alpha_pilot`. TA = `tackles_attempted` (what the model prices),
TW = `tackles_won`, INT = `interceptions`, DP = `dribbled_past` (challenges lost).

| bet365 step | implied | TA | TW | TW+INT | TA+INT | **TA+DP** | implied / TA | implied / TA+DP |
|---|---|---|---|---|---|---|---|---|
| 1+ | 0.833 | 0.688 | 0.522 | 0.746 | 0.822 | **0.792** | 1.21 | 1.05 |
| 2+ | 0.579 | 0.383 | 0.224 | 0.489 | 0.608 | **0.551** | 1.51 | 1.05 |
| 3+ | 0.372 | 0.193 | 0.079 | 0.283 | 0.408 | **0.358** | 1.93 | 1.04 |
| 4+ | 0.239 | 0.102 | 0.029 | 0.153 | 0.266 | **0.217** | 2.34 | 1.10 |
| 5+ | 0.174 | 0.057 | 0.011 | 0.081 | 0.171 | **0.148** | 3.05 | 1.18 |

- **Level.** Under TA+DP, bet365's implied probability is 4-10% above the frequency, rising to 18% at 5+: the pattern of
  an ordinary margin with longshot shading. Under TA (the model's count) the gap runs from 21% to 205%, which no plausible
  margin on a 1.20 price explains. Betway is similar (implied / TA+DP 1.02, 0.96, 0.98, 1.16, 1.49).
- **Shape** (step-to-step ratios, which a margin that scales every price alike does not change): bet365 0.63, 0.58,
  0.55, 0.57; TA+DP 0.62, 0.56, 0.50, 0.56; TA+INT 0.67, 0.59, 0.56, 0.52; TA 0.47, 0.42, 0.41, 0.42. The book's count
  falls off far more slowly than TA.
- **Mean per start** implied by each ladder's shape (Poisson): about 1.9 for both books, against TA 1.3-1.4, TA+DP 2.0-2.1,
  TA+INT 2.3, TW+INT 1.8.

**Reading (a lead, not a rule):** both books price a count about 45% larger than `tackles_attempted`, closest to
`tackles_attempted + dribbled_past`, i.e. tackles won plus tackles lost plus challenges lost, which would match an
Opta-style "tackles attempted" (the bet365 wording in section 1 describes only a won tackle; that is not reconciled).
TA+INT fits nearly as well on level. Caveats: one snapshot, 7 fixtures; longshot shading is not a scale factor, so the
shape test is not fully margin-free; no settled result has been compared.

**Why every tip was suspect.** With the model counting about 30% fewer events than the book, almost every line looks
overpriced to it; the few it called value were players the books priced **below even their own TA history** (Léo
Pereira 2+ implied 0.23 against 45% of his last 20 starts; Fernando 2+ 0.29 against 50%). The books had a reason the
model lacks (role, opponent, team style), so the screen selected exactly the players it misjudged. On SportyBet,
2026-10-11, Léo Pereira and Vitor Roque each made fewer than 2 tackles by that book's count (maintainer's ticket; not a
bet365 settlement).

### 2c. The pilot re-targeted, backtested and re-priced (2026-10-12)

The pilot now prices and settles tackles as `tackles_attempted + dribbled_past` (`pilot_ledger.PILOT_TACKLES_SQL`); the
frozen League One/Two model and its ledger keep `tackles_attempted`.

- **Backtest** (`BACKTEST_TACKLES=attempted_plus_dribbled_past scripts/quick_backtest.py --level`, Brazil Serie A, fit
  before 2026-03-01, 4,808 starters in 253 matches): Brier skill against the league average +0.071, +0.086, +0.084, +0.062
  for over 0.5 to 3.5 (90% intervals above 0), calibration slopes 1.00, 0.96, 0.97, 0.95 at the fitted game-state k 17.31
  (now the pilot's Brazil tackles k). The old count on the same run: +0.077, +0.076, +0.062, slopes 1.02, 0.97, 0.93. The
  strict PRD bin test fails on every line of both (worst bin 0.06-0.10).
- **Re-priced against the archived ladders** (`scripts/pilot_gap_diagnosis.py` section 5, the 7 fixtures of 2026-10-11):

  | count | book | lines | model P(k+ given he plays) | implied | mean EV | lines at 4%+ |
  |---|---|---|---|---|---|---|
  | TA (old) | bet365 | 674 | 0.286 | 0.485 | -37.4% | 13 |
  | TA+DP (new) | bet365 | 674 | 0.400 | 0.485 | -13.2% | **136** |
  | TA (old) | Betway | 622 | 0.262 | 0.439 | -40.7% | 1 |
  | TA+DP (new) | Betway | 622 | 0.364 | 0.439 | -18.3% | **84** |

**Reading.** The new count closes most of the level gap (book/model 1.69 to 1.21; part of what is left is that the model
includes substitute appearances, which these books void). But it makes the screen fire ten times as often, and on the same
players the books disagree with most: Fernando 3+ (model 0.43, bet365 0.13, "EV +224%"), Yago Felipe 3+ (0.52 v 0.18),
Léo Pereira 2+ (0.49 v 0.23). Expected profits of +50% to +220% against a major book are not edges; they are information the
model lacks (role, team-mates, opponent, the books' own data). **Tipping stays paused.** Before it can resume: a guard
against implausible edges (a cap on the model-to-book ratio or the expected profit, or shrinking towards the market), and
the count confirmed against settled results (section 6).

**Guard added (2026-10-12).** `ladder_scan.PLAUSIBLE_EDGE_CAP` = 0.25: a price at which the model's probability, if the
player plays, would return more than 25% is left out before the allocator, and the run summary counts it. On the archived
ladders it keeps 72 of the 136 bet365 lines at 4%+ (Betway 52 of 84), all between +4% and +25%, and removes every one of
the +50% to +224% outliers. Tipping stays paused: what is left is still an unvalidated model's disagreement with the book.

### 2d. Sportmonks against Opta, and the first settled examples (2026-10-12)

- **Data feed = Opta.** FotMob's match pages carry Opta player ids and Opta counts. For Santos v Flamengo (2026-10-08, 2-2,
  Sportmonks fixture 19621797) every one of the 29 players who played has the same tackles, interceptions and fouls in
  Sportmonks as on FotMob, and the same dribbled past for 28 of 29 (Álvaro Barreal: FotMob 1, Sportmonks 0). Sportmonks'
  `tackles_attempted` is therefore Opta's "Tackles" as FotMob shows it, and `dribbled_past` is Opta's "Dribbled past". Which
  Opta count a book settles on is still not known.
- **Settled examples (SportyBet "Player tackles (incl. overtime)", the maintainer's tickets of 2026-10-11, not bet365):**

  | player, match | bet | result | Opta tackles | dribbled past | interceptions |
  |---|---|---|---|---|---|
  | Léo Pereira, Flamengo 1-0 Fluminense | 2+ | lost | 1 | 0 | 0 |
  | Vitor Roque, Palmeiras 4-1 Corinthians (69 min) | 2+ | lost | 1 | 0 | 0 |

  Both lose under every candidate count, so they do not tell the counts apart. Pending: the same ticket's legs on
  Chapecoense v Athletico PR (2026-10-12 22:30 UTC: Arthur Dias 3+, Doma 2+) and Bragantino v Cruzeiro (2026-10-13 00:00
  UTC: Fernando 2+, 3+). A leg where tackles alone and tackles plus dribbled past fall on different sides of the line is the
  first settled example that discriminates. FotMob's pages were read by hand in a browser; nothing scrapes them.

### 2e. The first settled results that tell the counts apart (2026-10-13)

Opta counts from FotMob's match pages (read by hand), against SportyBet's settlement of "Player tackles (incl. overtime)" on the
maintainer's tickets. Every player below took part, so no leg was void.

| player, match (FT) | minutes | Opta tackles | dribbled past | interceptions | SportyBet bet | settled | TA | TA+DP | TA+INT |
|---|---|---|---|---|---|---|---|---|---|
| Marcinho, Chapecoense 1-4 Athletico PR (2026-10-12) | 45 | 0 | 3 | 0 | 2+ | **lost** | 0 lose | **3 win** | 0 lose |
| Wallace Yan, Bragantino 4-2 Cruzeiro (2026-10-13) | 65 | 1 | 0 | 1 | 2+ | **lost** | 1 lose | 1 lose | **2 win** |
| Doma, Chapecoense 1-4 Athletico PR | 45 | 0 | 0 | 1 | 2+ | lost | lose | lose | lose |
| Arthur Dias, Chapecoense 1-4 Athletico PR | 45 | 0 | 0 | 1 | 3+ | lost | lose | lose | lose |
| Fernando, Bragantino 4-2 Cruzeiro | 90 | 0 | 0 | 1 | 2+, 3+ | lost | lose | lose | lose |

**Reading.** Marcinho's leg would have won under tackles plus challenges lost, and Wallace Yan's under tackles plus
interceptions; both lost. So SportyBet counts **neither challenges lost nor interceptions**: its count agrees with plain
`tackles_attempted` (Opta "Tackles") on every leg so far (7 of 7 including section 2d). The 45% level gap of section 2b
therefore reads as **bookmaker margin and longshot shading**, not a wider count. Caveats: SportyBet's data provider is not
known, so this is SportyBet's rule, not bet365's; two discriminating legs, not a sample. **Decision (maintainer,
2026-10-13):** the pilot prices and settles on plain `tackles_attempted` again (`pilot_ledger.PILOT_TACKLES_SQL`; Brazil
tackles k back to 496.882); the count stays a one-line setting. On the archived 2026-10-11 ladders the plain count leaves
the model at 0.286 against bet365's 0.485 (mean expected profit -37%), with 8 bet365 lines between +4% and the 25% guard:
the books' overs are expensive, and real edges on them will be rare.

The system's own tips of 2026-10-12 (paper bets 9 and 10, bet365 1+): by the Opta counts Wallace Yan 1+ won (1 tackle) and
Marcinho 1+ lost (0 tackles; substituted at half-time). Not recorded with `pilot_ledger --record`, which is for bet365's own
results.

## 3. Evidence table to fill (West Ham United v Queens Park Rangers, 2026-10-09, final 1-1)

Sportmonks counts are filled in from `.prop_alpha_state/pilot/counts_19729059.json`. The last three columns need an
**authoritative** figure: bet365's own settlement of a bet on that player (only if you placed or were shown one), or an
official Opta-based count for the match (for example a source that states it is Opta data). Fill every row you can, and
include players who started, who came on, and who did not play.

| Player | Role in the match | SM tackles attempted | SM tackles won | SM interceptions | SM fouls committed | SM fouls drawn | Authoritative tackles | Authoritative fouls committed | Source and date |
|---|---|---|---|---|---|---|---|---|---|
| Amadou Mbengue | came on / taken off (10 min) | 2 | 1 | 0 | 1 | 0 | | | |
| Arne Engels | 90 min | 2 | 1 | 1 | 0 | 0 | | | |
| Divine Mukasa | came on / taken off (68 min) | 0 | 0 | 0 | 0 | 0 | | | |
| Glen Kamara | 90 min | 1 | 0 | 2 | 1 | 0 | | | |
| Harvey Vale | came on / taken off (30 min) | 1 | 1 | 0 | 0 | 0 | | | |
| Jake Clarke-Salter | 90 min | 0 | 0 | 0 | 1 | 0 | | | |
| Jarrod Bowen | 90 min | 0 | 0 | 0 | 0 | 2 | | | |
| Jimmy Dunne | 90 min | 2 | 2 | 0 | 1 | 0 | | | |
| Jonathan Varane | did not play | 0 | 0 | 0 | 0 | 0 | | | |
| Joël Piroe | came on / taken off (22 min) | 0 | 0 | 0 | 0 | 0 | | | |
| Joël Veltman | did not play | 0 | 0 | 0 | 0 | 0 | | | |
| Konstantinos Mavropanos | 90 min | 3 | 2 | 0 | 0 | 1 | | | |
| Kwame Poku | 80 min | 3 | 3 | 1 | 0 | 3 | | | |
| Kyle Walker-Peters | came on / taken off (46 min) | 1 | 1 | 1 | 2 | 0 | | | |
| Manor Solomon | came on / taken off (68 min) | 0 | 0 | 0 | 0 | 2 | | | |
| Maximilian Kilman | 90 min | 1 | 1 | 1 | 1 | 1 | | | |
| Morato | 90 min | 2 | 2 | 1 | 2 | 1 | | | |
| Nicolas Madsen | 90 min | 1 | 1 | 0 | 0 | 2 | | | |
| Oliver Scarles | did not play | 0 | 0 | 0 | 0 | 0 | | | |
| Pablo | came on / taken off (44 min) | 1 | 1 | 0 | 2 | 2 | | | |
| Paul Smyth | came on / taken off (60 min) | 1 | 0 | 0 | 1 | 2 | | | |
| Ronnie Edwards | 90 min | 1 | 1 | 1 | 0 | 0 | | | |
| Tariq Lamptey | came on / taken off (21 min) | 0 | 0 | 2 | 0 | 0 | | | |
| Taty Castellanos | 90 min | 0 | 0 | 0 | 1 | 0 | | | |

## 4. Decision rule (applied only after section 3 is filled)

1. If the authoritative tackles equal one Sportmonks field or a fixed sum of them for **every** player checked, record that
   mapping here and rebuild the Championship history target from it (this changes pricing code: new freeze, new lockbox).
2. If they differ by player in a way no Sportmonks field explains, mark the Championship tackles market **unsupported**.
3. The same test applies separately to fouls. A market can be supported while the other is not.
4. Settlement of any recorded paper bet uses the verified mapping and the verified void rules, never the current assumption
   (`tackles_attempted`, `fouls_committed`, void if the player does not play).

## 5. Written questions for bet365 support (to send in writing, keep the reply)

> Subject: Settlement rules for player tackles and fouls markets (English Championship)
>
> For the markets "Player Tackles" and "Player Fouls Committed" (the "1+, 2+, 3+ ..." markets) on English Championship
> matches, please confirm in writing:
> 1. Which data provider and which statistic definition settle each market (the provider's statistic name).
> 2. Whether "Player Tackles" counts only successful tackles (the player wins the ball), or all tackles including
>    unsuccessful ones, and whether interceptions, blocks or clearances count.
> 3. What "Player Fouls Committed" counts, and whether it is fouls awarded by the referee against the player.
> 4. What happens if the player is in the squad but does not start, starts and is substituted, comes on as a substitute, or
>    takes no part: void, or settled on the count?
> 5. Whether extra time or stoppage time counts, and whether the settled figures can be corrected after settlement.
> 6. If possible, the official count it used for each player in West Ham United v Queens Park Rangers on 9 October 2026.

## 6. Open items

- [ ] Maintainer reads bet365's soccer rules pages (Settlement; Match/Team/Player Statistics; Player Performance Markets;
      What is Opta) and pastes the relevant paragraphs here.
- [ ] Written reply from bet365 support (section 5).
- [ ] Section 3 filled for at least 5 players, including a substitute and a non-player.
- [ ] Decision under section 4 recorded, with the date.
- [ ] Section 2b lead tested against settled results: for named Brazil players, a public Opta-based count (or a bet365
      settlement) compared with Sportmonks `tackles_attempted`, `+ dribbled_past` and `+ interceptions` for the same match.
