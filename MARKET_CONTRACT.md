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
