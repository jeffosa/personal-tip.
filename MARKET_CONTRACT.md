# Market contract: what bet365's player tackles and fouls markets settle on

Status (audit 2026-10-25): **UNVERIFIED for every bookmaker and market. Tipping on real prices is disabled**
(`alerts/market_contract.py`: `VERIFIED_CONTRACTS` is empty, so both scans withhold every real-price tip and say so in their
summary). No bookmaker's tackles or fouls settlement rule has been read first-hand from the bookmaker, and no bookmaker has
settled a bet on a price this system tipped (0 of 10 paper tips). Sections 7-10 record the rules found, the settled-bet audit,
the model-statistic question and what would verify a contract. Nothing here changes the frozen League One/Two model.

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
      What is Opta) and pastes the relevant paragraphs here. Still blocked for Claude on 2026-10-25 (section 7).
- [ ] Written reply from bet365 support (section 5).
- [ ] Section 3 filled for at least 5 players, including a substitute and a non-player.
- [ ] Decision under section 4 recorded, with the date.
- [x] Section 2b lead tested against settled results (2026-10-13, section 2e): SportyBet's settlements count neither
      challenges lost nor interceptions. Not tested at bet365 or Betway.
- [ ] Section 10's evidence collected for each book the scans price (bet365, Betway): first-hand rules and own-book settlements.

## 7. Rules found, by bookmaker (checked 2026-10-25)

How each source was read matters, so it is stated per row. "Summary" means a web-search tool's summary of the page, not the
page itself: help.bet365.com still answers HTTP 403 to a fetch and is refused by the browser pane ("not allowed due to safety
restrictions"), and web.archive.org cannot be fetched from here. Quotes are kept short; the rest is paraphrase.

| Book | Question | Rule found | Source, URL, how read | Status |
|---|---|---|---|---|
| bet365 | Provider | Match, team and player statistics markets are settled on Opta data and Opta's definitions, or the official competition website outside Opta's coverage | "Settlement - Soccer", https://help.bet365.com/s/en-us/sportsrules/soccer/soccer-settlement, and "Match/Team/Player Statistics - Soccer Rules", https://help.bet365.com/s/en-us/sportsrules/soccer/match-statistics; summary, 2026-10-25 | Lead (two summaries agree; not read first-hand) |
| bet365 | What a tackle is | Opta's definition: a legal ground-level challenge that "successfully takes the ball away from the opposition player", who must be in controlled possession; interceptions are not tackles | "What is Opta FAQ", https://help.bet365.com/s/en-us/sports/how-is-my-bet-settled; summary, 2026-10-25 | Lead. It does not say whether Opta's "tackle lost" (ball taken, then straight back to an opponent) counts; see section 9 |
| bet365 | What a foul committed is | An infringement the referee penalises as foul play that results in a free kick or penalty; advantage played and then a caution does not count unless a free kick or penalty is taken | Same FAQ, and the Spanish soccer rules page https://help.bet365.com/s/es-us/sportsrules/soccer/match-statistics; summary, 2026-10-25 | Lead |
| bet365 | Player participation | A selected player who does not start: void ("no action"); the Swedish page excepts competitions with rolling substitutes. Pages differ by region | https://help.bet365.com/s/en-us/sportsrules/soccer/match-statistics and https://help.bet365.com/s/en-se/sportsrules/soccer/match-statistics; summary, 2026-10-25 | Lead, region-dependent. **Conflicts with the project's assumption** (void only if the player does not play; section 9) |
| bet365 | Extra time | Player match markets include extra time where played | Match-statistics page; summary, 2026-10-25 | Lead (league matches have no extra time, so the scanned leagues are not affected) |
| bet365 | Abandoned or postponed match; corrections after settlement | Not found | n/a | Open |
| Sky Bet (not a book the scans price) | Opta definitions | A tackle is a player connecting with the ball in a legal ground-level challenge; **tackles won and lost both count** (lost: the tackle is made but the ball goes to an opponent). Fouls as bet365's, offsides excluded. Settled after 90 minutes plus injury time; extra time and shoot-outs excluded unless stated; Opta may amend statistics later | "Opta Football Statistics - Definitions", https://support.skybet.com/app/answers/detail/opta-football-statistics-definitions; **read in full** (fetch), 2026-10-25; no date on the page | Sky Bet's published rule; evidence about Opta's definition, not about bet365's market |
| Betway | Any rule | No published tackles or fouls rule found online; **superseded by section 12** (Betway's own rules document, supplied by the maintainer) | Web search, 2026-10-25 | See section 12 |
| SportyBet | Any rule | No published rule found; the market is labelled "Player tackles (incl. overtime)" | Web search, 2026-10-25; market label from the maintainer's tickets | Open; its settlements are in section 2e |
| Sportradar (a feed many books use; not shown to be Betway's or SportyBet's) | Player markets | A soccer player not in the starting line-up: void; overtime included, shoot-outs excluded | https://docs.sportradar.com/uof/data-and-features/player-markets/pre-match-player-markets-rules-and-faqs; read in full (fetch), 2026-10-25 | Industry practice only |

## 8. Settled paper bets audited against the automatic settlement (2026-10-25)

All 10 paper tips (pilot, Brazil Serie A, all bet365 prices, 11-12 Oct) have a settled result. Their stored Sportmonks counts were
re-read from `prop_alpha_pilot` (the five provisional results had been graded from TheStatsAPI on 2026-10-15) and compared with
Opta's counts where FotMob was read (section 2e). Every player started, so the void-rule conflict of section 7 did not arise.

| Tip | Player, bet (bet365 price) | Bookmaker result | Automatic (Sportmonks: tackles attempted / won, minutes) | Opta (FotMob) tackles | Class |
|---|---|---|---|---|---|
| 1 | Fernando 3+ @ 7.50 | lost (SportyBet) | lost (0 / 0, 90) | 0 | other book, agrees |
| 2 | Fernando 2+ @ 3.40 | lost (SportyBet) | lost (0 / 0, 90) | 0 | other book, agrees |
| 3 | Yago Felipe 3+ @ 5.50 | none | lost (0 / 0, 70) | not read | unconfirmed |
| 4 | Yago Felipe 2+ @ 2.50 | none | lost (0 / 0, 70) | not read | unconfirmed |
| 5 | Léo Pereira 2+ @ 4.33 | lost (SportyBet) | lost (1 / 1, 90) | 1 | other book, agrees |
| 6 | Arthur Dias 3+ @ 6.00 | lost (SportyBet) | lost (0 / 0, 46) | 0 | other book, agrees |
| 7 | Arthur Dias 2+ @ 2.62 | none | lost (0 / 0, 46) | 0 | unconfirmed |
| 8 | Vitor Roque 2+ @ 4.33 | lost (SportyBet) | lost (1 / 1, 69) | 1 | other book, agrees |
| 9 | Marcinho 1+ @ 1.41 | none | lost (0 / 0, 46) | 0 | unconfirmed |
| 10 | Wallace Yan 1+ @ 1.41 | none | won (1 / **0**, 66) | 1 | unconfirmed; **discriminating** |

- **At the tipped book (bet365): 0 agree, 0 discrepancies, 10 unresolved** (5 with another book's result, 5 with none). Paper
  tips are not placed, so bet365 never settles them; only a bet365 result the maintainer can see (a placed bet, or a settled
  market shown in the account history) would count.
- **Another book (SportyBet): 5 agree, 0 disagree.** Until today they were reconciled as if they were bet365 results ("5 AGREE");
  `--reconcile` now reports them as "OTHER BOOK" and never counts them as agreement with bet365 (`replies.reconcile`,
  `Settlement.book`).
- **Mismatches investigated:** none between bookmaker and automatic results. Between data sources: TheStatsAPI's minutes were
  45 / 45 / 65 where Sportmonks has 46 / 46 / 66 (Arthur Dias, Marcinho, Wallace Yan); no result changes. Sportmonks agrees
  with Opta (FotMob) on all 8 tips where FotMob was read.
- **Tip 10 tells the two readings of bet365's wording apart:** 1 tackle attempted, 0 won. If bet365 settled it won, the market
  counts Opta's tackles (attempted, as the model prices); if lost, tackles won. Nobody has seen bet365's result.
- The sample is 10 tips on 4 matches; it cannot verify anything, and no further settled bets exist (tipping has produced no paper
  tip since 12 Oct and is now disabled).

## 9. Does the model predict the statistic the bookmaker settles?

- **The model's statistic.** The frozen League One/Two model and the pilot price `player_match_stats.tackles_attempted` and
  `fouls_committed` (Sportmonks). On one Brazil match Sportmonks matched Opta (FotMob) for 29 of 29 players on tackles and fouls
  (section 2d), and on the 8 tips above where FotMob was read; **no League One/Two match has been compared with Opta.**
- **The book's statistic.** bet365 (lead) settles on Opta. Under Opta's scheme a tackle is a ball won from an opponent in a
  ground challenge; "won" and "lost" then describe who has the ball afterwards, and Sky Bet's Opta page says both count. Read
  that way bet365's tackles = Opta tackles = Sportmonks `tackles_attempted`, which SportyBet's settlements also fit (2e). Read
  literally, bet365's wording could mean tackles won only (Sportmonks `tackles_won`, fewer), and bet365's prices already run
  far *above* the attempted count (section 2b), which a won-only count would make worse, not better. Neither reading is
  established: no bet365 settlement has been seen.
- **The void rule.** Pricing and settlement assume a bet is void only if the player does not play (`VOID_IF_NO_APPEARANCE`, set
  on every real quote in `scanner/slate.py`). bet365's rule (lead) voids a player who does not start. The engine can price that
  rule (`VOID_IF_NOT_STARTING`), but nothing uses it; changing it changes pricing and settlement semantics (AGENT.md A7) and
  waits for the rule to be verified.
- **Conclusion: not established.** Every model-versus-price "expected profit" is an **unverified estimate**, and real-price tips
  stay disabled (`market_contract.VERIFIED_CONTRACTS` empty).

## 10. What verifies a contract (proposal for the maintainer; nothing is verified until it is met and recorded here)

For one bookmaker and one market:
1. **The rule, first-hand:** the bookmaker's own page text (pasted here by the maintainer, with the URL and date) or a written
   support reply (section 5), naming the data provider, the statistic, the void rule and the period, and saying what happens
   to abandoned and postponed matches.
2. **Own-book settlements:** at least 20 settled results from that bookmaker on selections whose stored counts the project can
   grade, including at least 3 that discriminate (for tackles: attempted and won fall on different sides of the line, like tip
   10), at least 1 substitute appearance and at least 1 player who did not start; recorded with `result <tip> won|lost|void
   <book>`, `settled <id> won|lost|void <book>` or `pilot_ledger --record --book`. The bookmaker must be named: a result
   naming none reconciles as "NO BOOK NAMED" and never counts (audit M2, 2026-10-26).
3. **Reconciliation:** `pilot_ledger --reconcile` / `ledger --reconcile` report every one of them AGREE at that book, with no
   DISCREPANCY and no override; any discrepancy is investigated and resolved here before the count restarts. (Gate 3's own
   settlement-integrity check, `value_evidence.settlement_integrity`, applies the same classification to every settled
   paper bet of the Gate 3 population.)
4. **Then** the maintainer adds `(book, market): Contract(statistic, void rule, evidence file)` to `market_contract.VERIFIED_CONTRACTS` in a PR, and `tests/test_market_contract.py` checks the evidence (section 11). If the
   verified statistic or void rule differs from what the model prices, the pricing change needs a new freeze and a later lockbox
   start first (`LOCKBOX_PROTOCOL.md`).

## 11. Second pass (2026-10-25, later): criteria enforced in code; evidence still absent

**Verified: nothing.** No bookmaker or market meets section 10, and `VERIFIED_CONTRACTS` stays empty.

- **First-hand rules, attempted again and blocked:**
  - help.bet365.com (en-gb and en-us pages): HTTP 403 to a direct fetch.
  - betway.com: refused by the browser pane ("not allowed due to safety restrictions").
  - Betway's regional rules pages (betway.co.za redirects to betway.com.ng): rendered by script, so a fetch returns no text.
  - Search summaries of Betway's African pages say a player who does not start voids his bets. Those are summaries, of a
    regional site whose relation to OddsPapi's `betway` is unknown, so they do not count (section 10.1).
  - **The maintainer must supply the wording:** paste each page into the evidence file with its URL and date, or obtain a
    written support reply (section 5).
- **Same-book settlements: 0** for bet365 and Betway, in either market. Paper tips are never settled by a bookmaker. Examples
  can only come from bets the maintainer places by hand, or from settled selections the maintainer can read in a bookmaker
  account, recorded with the bookmaker named:
  - `result <tip> won|lost|void bet365` in Telegram, or `pilot_ledger --record <tip> <result> --book bet365`, for pilot tips;
  - `settled <id> <result>` for bets recorded with `placed`.
- **The criteria are now code, not prose** (`alerts/market_contract.py`):
  - A contract is `Contract(statistic, void_rule, evidence)`. `evidence_failures` checks the evidence file
    `contract_evidence/<book>_<market>.json` for:
    - first-hand wording on all five rule topics;
    - at least 20 same-book examples, deduplicated;
    - every example agreeing with the contract;
    - at least 3 discriminating examples;
    - every rival count (`RIVAL_STATISTICS`: tackles won, tackles plus challenges lost, tackles plus interceptions, tackles
      won plus interceptions; fouls plus fouls drawn) contradicted by an example;
    - a substitute appearance;
    - a named player who did not start or play.
  - `tests/test_market_contract.py` fails if a listed contract's evidence misses any criterion.
  - The gate sends a tip only when the contract settles on the very count and void rule the model priced
    (`MODEL_STATISTICS`; the pilot's `PILOT_STATISTICS`). A contract that settles on another count, or voids non-starters
    while the scans price "void only if he does not play", keeps the tips withheld until the pricing is changed under a new
    freeze and a later lockbox start.
- **Progress report:** `python scripts/contract_evidence.py --book bet365 --market tackles` gathers the same-book results in
  both ledgers, adds their stored counts and prints what is missing; `--write` saves a draft for the maintainer to complete.
  A pilot result counts only if it names the bookmaker. On 2026-10-25 all four book-market pairs had 0 examples, and every
  rule topic was missing.

## 12. Betway's own rules document (supplied by the maintainer, 2026-10-25)

**Source.**
- Document: "Betway Betting Rules", Version 42, "These terms were last updated 10/02/2022" (201 pages). Supplied by the maintainer
  as `BettingRules.pdf`; SHA-256 `a5f3e6d56e776a3a51e129fdf2328256f51ade6a620fda78b6b9a8b2683aae30`.
- Read in full as text on 2026-10-25. A copy is kept, gitignored and in the daily backup, at
  `.prop_alpha_state/analysis/bookmaker_rules/betway_betting_rules_v42_2022-02-10.pdf`.
- Recorded in `contract_evidence/betway_tackles.json` and `betway_fouls.json`, with page references and short quotes.
- This is the first first-hand bookmaker rule in this file.

| Topic | What the document says | Page | Fits the project's assumption? |
|---|---|---|---|
| Tackle | A player connects with the ball in a ground challenge and successfully takes it from the man in possession (the Opta wording); does not say whether a tackle after which the ball goes straight back to an opponent counts | 51 | Consistent with Opta "Tackles" = `tackles_attempted`; not decisive against `tackles_won` |
| Data provider | Player performance markets settle on "feed provided data", else Betway's own statistics; **no provider named** | 49 | Open |
| Player fouls | **Not defined.** Only team/match fouls are (fouls conceded, offsides and misconduct with the ball out of play excluded, penalties included) | 47 | Open |
| Extra time | Regular time with stoppage time; extra time, shoot-outs and golden goals excluded | 49 | Yes (league matches have no extra time) |
| Participation | "Propositions markets specifically, players must start for bets to stand." A player who does not start: void | 49 | **No.** The scans price, and settlement grades, "void only if he does not play" (`VOID_IF_NO_APPEARANCE`) |
| Abandoned | Void if abandoned before the end of regular time, unless already determined | 49 | Not modelled (no abandoned match yet) |
| Postponed | Void if not played on the scheduled date (local time) | 30 | Not modelled; the lockbox already drops a moved kickoff |
| Whole line | Push (stake returned) stated only for goals over/under; nothing for player markets | 33-34 | The pilot's Betway markets are "k+" ladders, which have no whole line |

**Caveats (why this verifies nothing yet):**
- The document is dated February 2022 and may not be the version in force.
- It names no licensee or website. GBP/EUR stakes and UK broadcasters point to Betway's UK/international site, but whether
  OddsPapi's `betway` is that operator is unconfirmed.
- It names no statistics provider and defines no player fouls market.
- There are **0** Betway-settled examples.

`scripts/contract_evidence.py` on 2026-10-25:
- Betway tackles: first-hand wording on 4 of 5 topics (whole-line missing), 0 of 20 examples.
- Betway fouls: 3 of 5 topics (statistic and whole-line missing), 0 of 20 examples.

**Consequence for the model.** Betway voids non-starters, so the model's probability for a Betway quote is conditioned on the
wrong event: it includes substitute appearances, which Betway would void. The project's automatic settlement would grade a
substitute's Betway bet on his count where Betway voids it.
- Even with complete evidence, a Betway contract (`VOID_IF_NOT_STARTING`) does not match what the scans price, so the gate keeps
  Betway tips withheld (`tests/test_market_contract.py`).
- Pricing Betway quotes as `VOID_IF_NOT_STARTING` (the engine supports it) changes pricing and settlement semantics. It is the
  maintainer's decision (AGENT.md A7) and has not been made.
  - In the pilot (not frozen) it needs no lockbox restart.
  - The frozen League One/Two model would need a new freeze and a later lockbox start, if a Betway League One/Two market ever
    appears.

**Second Betway document reviewed (2026-10-25): no tackles or fouls rules.**
- Document: "Betway – Betting Rules" for Malawi: limits in Kwacha; section dates 2014-2019; file created 2022-08-29; 102 pages.
  Supplied by the maintainer as `BettingRules_MW_en.pdf`, SHA-256
  `ce5f95596c565c5117c7322a2568032616e5d5f284c307775fae30da34900639`.
- Its football section has no player tackles or fouls market: no tackle definition, no player fouls, and no rule on player
  proposition participation. Nothing from it is recorded in `contract_evidence/`.
- It shows Betway's rules differ by operator. For example, a postponed league match stands for 24 hours in this document but is
  void if not played that day in v42. So the operator behind OddsPapi's `betway` must be identified before any Betway
  rule document can count.

## 13. Evidence status per target, and what the maintainer must obtain (2026-10-26)

**Targets:**
- bet365 and Betway: the bookmakers the scans price (`ladder_scan.DEFAULT_BOOKMAKERS`; `scan.DEFAULT_BOOKMAKERS` adds Sky Bet
  and William Hill, which have never returned a tackles or fouls market).
- Betfred: it quoted Championship fouls in the 2026-10-26 probe, but nothing prices it today.
- League One/Two: no target, since no bookmaker on OddsPapi quotes these markets there (probe, 2026-10-26).

One evidence file per target in `contract_evidence/` (`*_tackles.json`, `*_fouls.json`). Each records the bookmaker-authored
rules found, the access attempts that failed, what is missing and the same-book examples (none).
`python scripts/contract_evidence.py --book <book> --market <market>` lists the unmet criteria.

| Target | First-hand rules on file | Same-book examples | Criteria unmet | Blocking |
|---|---|---|---|---|
| bet365 tackles | 3 of 5 topics since 2026-10-27 (section 14; before: none, help.bet365.com refuses requests from here) | 0 | 10 | abandonment for player markets, whole-line rule, examples; settlement under its void rule |
| bet365 fouls | 3 of 5 topics since 2026-10-27 (section 14) | 0 | 7 | the same |
| Betway tackles | 4 of 5 topics, from Betway Betting Rules v42 (2022), operator and currency unconfirmed | 0 | 9 | whole-line rule, examples; its "players must start" rule is priced since 2026-10-27 (section 14), not yet settled |
| Betway fouls | 3 of 5 topics (no player fouls market defined) | 0 | 7 | statistic, whole-line rule, examples |
| Betfred fouls | none (betfred.com rules page: HTTP 403, bot challenge, not bypassed) | 0 | 10 | rules, examples; not a priced book |

Not used, and why:
- search-engine summaries of bet365's pages;
- a 2020 "Betting Rules" PDF on a content CDN that names no operator;
- Betway's Malawi rules (no tackles or fouls market);
- SportyBet's results (another book's);
- every automatic Sportmonks settlement.

**What the maintainer must obtain, per target:**

1. **The rules, first-hand.** In your own browser:
   - **bet365:** help.bet365.com → Sports Rules → Soccer: the "Settlement" page, "Match/Team/Player Statistics" and the
     "What is Opta" FAQ.
   - **Betway:** your own account's site, its football betting rules (the "Player Performance" / propositions section). Say
     which country's Betway it is, and whether it is the Betway OddsPapi quotes.
   - **Betfred:** betfred.com/help/bet-rules/football.

   For each, print the page to PDF so the URL and date show, and save it. Each must cover: the statistic and its data
   provider; extra time; a starter, a substitute and a named player who does not play; an abandoned or postponed match; and a
   count exactly on a whole line.

   Where a page is silent, ask support in writing (section 5 has the questions) and keep the reply. I then record the wording,
   URL, date and file hash in the evidence file (source `bookmaker rules document` or `bookmaker support reply`).
2. **At least 20 results settled by that bookmaker itself, per market.**
   - A bookmaker settles only bets placed with it, so these come only from bet slips or account history. Whether to place any
     bet is your decision. Paper tips never produce one, and nothing here asks you to bet.
   - Record each result naming the bookmaker: `settled <id> won|lost|void <book>`, `result <tip> won|lost|void <book>`, or
     `pilot_ledger --record <tip> <result> --book <book>`.
   - The set must include:
     - for tackles, results where Opta tackles and tackles won fall on different sides of the line, and where challenges lost
       or interceptions would change the result (each rival count contradicted at least once);
     - for fouls, a result where fouls drawn would change it;
     - a player who came on as a substitute;
     - a named player who did not start or play.
3. **Then:**
   - `python scripts/contract_evidence.py --book <book> --market <market> --write` drafts the examples with their stored
     counts.
   - Once every criterion in section 10 is met, listing the contract is your decision, in a PR.
   - A contract that voids non-starters (bet365, Betway) is priced so since 2026-10-27 (section 14); paper-bet settlement
     must read who started before any such tip could be sent.

**Checks hardened (audit 2026-10-27; nothing became verified, `VERIFIED_CONTRACTS` is still empty):**
- **Conflicts.** `evidence_failures` reports a CONFLICT when one selection (fixture, player, line, side) is recorded twice with different results or counts, and counts neither record. Before, it kept the first silently, so a contradicting same-book settlement could hide behind an agreeing one.
- **Provenance.** An example without `source` (how the bookmaker's result was read, and by whom) or `settled_on` is reported and not counted; an invalid `settled_on` date raises. Both were in `TEMPLATE.json`, but neither was checked.
- **Types.** Malformed fields raise instead of being read loosely (`started` must be true or false: `bool("false")` is true, so a substitute typed that way was read as a starter; `minutes` and the counts whole numbers of at least 0; `side` over or under; a whole or half `line`).
- **Template.** `TEMPLATE.json` now lists all three first-hand sources, `bookmaker rules document` included.

The criteria unmet, re-run 2026-10-27, are unchanged: bet365 tackles 13, bet365 fouls 10, Betway tackles 9, Betway fouls 7, Betfred fouls 10.

**Maintainer checklist, per bookmaker and market:**
1. **Rules, first-hand.** Save each rules page as a PDF with its URL and date, or keep a written support reply. It must cover all 5 topics: the statistic and data provider, extra time, participation and void (a starter, a substitute, a named non-player), abandoned or postponed matches, and whole-line settlement.
2. **Settlements.** At least 20 results settled by that bookmaker on that market.
   - Record each one naming the book: `settled <id> <result> <book>`, `result <tip> <result> <book>` or `pilot_ledger --record ... --book <book>`.
   - Each needs its `source` and `settled_on`.
   - They must include at least 3 that tell the rival counts apart, with every rival contradicted at least once, a substitute appearance and a named player who did not start or play.
   - No selection may be recorded twice with different facts.
3. **Counts.** Draft them with `python scripts/contract_evidence.py --book <book> --market <market> --write`. Never type them by hand.
4. **Listing.** Only when `evidence_failures` returns nothing: list `(book, market): Contract(...)` in `VERIFIED_CONTRACTS` in a PR. `tests/test_market_contract.py` re-checks the file and requires a lower-case key and an evidence file named `<book>_<market>.json`.
5. **Capital.** Step 2 needs bets actually settled by the bookmaker, i.e. capital at risk, which PRD §1.2 forbids before every gate has passed. That is the maintainer's decision D2 in `GATE3_DECISION_MEMO.md`.

## 14. bet365's own rules, and pricing under its void rule (2026-10-27)

**Source.** bet365's help pages "Settlement of Wagers" and Football "Match/Team/Player Statistics", read by the maintainer
in their own browser on 2026-10-27 and supplied as 7 screenshots (kept, with SHA-256, in
`.prop_alpha_state/analysis/bookmaker_rules/bet365/`; recorded in `contract_evidence/bet365_*.json`). The address bar is cut
off at `help.bet365.com/s/...`, so the exact URL and regional version are not recorded.

| Topic | bet365's wording (summary; exact text in the evidence files) |
|---|---|
| Statistic and provider | Player statistics markets settle on Opta data "according to their definitions"; the official competition website outside Opta's coverage; bet365's own statistics when the evidence conflicts. **Tackle:** a legal, ground-level challenge that successfully takes the ball from an opponent in controlled possession. **Foul conceded:** an infringement penalised by the referee with a free kick or penalty; offsides are not fouls; advantage played and a later caution is not a foul unless a free kick or penalty follows. Markets listed: Player Tackles, Alternative Player Tackles, Player Fouls Committed, Alternative Player Fouls Committed. |
| Extra time | Player match markets include extra time if played (league matches have none). |
| Participation | "If a selected player ... does not start a match, bets will be void" (rolling-substitute competitions excepted). |
| Abandoned or postponed | Only for match and team statistics bets (void if abandoned before 90 minutes unless decided): **not counted** for player markets; postponement not covered. |
| Whole line | Not stated; cannot arise on the "k+" ladders and half-point lines seen. |

**Decisions (maintainer, 2026-10-27).**
1. **Target: bet365.** SportyBet, which the maintainer also uses, cannot be priced: OddsPapi lists `sportybet`, but odds
   requests returned HTTP 400 "not supported or does not have any fixtures available" for tournaments 18, 24, 25, 36 and 325 and
   for the Premier League (17). SportyBet results stay another book's.
2. **Verification: rules plus a data check.** bet365's first-hand rules, plus a check that Sportmonks' counts equal Opta's for
   20+ player-matches (substitutes, non-starters and counts on the line among them), in place of 20+ bet365-settled bets
   (`GATE3_DECISION_MEMO.md` D2). Not built yet; `evidence_failures` still asks for settled examples.
3. **"Price the pilot as if he starts."** `ladder_scan.BOOK_VOID_RULES` gives bet365 and Betway `void_if_not_starting`; every
   real pilot quote carries its book's rule and a book with none is refused before any odds request. The model's win chance on
   such a price is the player's chance as a starter (a non-start, a sub appearance included, returns the stake). The rules are
   part of the pilot settings digest.

**Still open.** Paper-bet settlement voids only a player who did not play (`replies.grade` reads no starter flag), so
`pilot_ledger.record_tips` refuses a tip priced void if not starting before anything is recorded or sent; settlement must read
`player_match_stats.started` before any bet365 or Betway contract can be listed. `scripts/screen_replay.py` and
`scripts/edge_calibration.py` still replay under void if no appearance.
