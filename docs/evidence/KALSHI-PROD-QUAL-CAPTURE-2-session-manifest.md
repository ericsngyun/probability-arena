# KALSHI-TAPE-MANIFEST-001 — frozen session manifest

**Milestone:** KALSHI-LIVE-TAPE-COLLECTOR-001 (CP6-CP9 live DEMO qualification session)  
**Verdict:** **QUALIFIED**  
**Activity snapshot (canonical timestamp):** `2026-08-20T00:26:42.325152+00:00`  
**Environment:** `production` — `https://api.elections.kalshi.com/trade-api/v2`

> Frozen BEFORE any capture. This document fixes the universe and the selection rule so that neither can be chosen after seeing how the tape behaved.

## 1. The activity snapshot

| field | value |
|---|---|
| **canonical timestamp** (the probe's first read) | `2026-08-20T00:26:42.325152+00:00` |
| census started | `2026-08-20T00:23:11.020429+00:00` |
| census completed | `2026-08-20T00:26:39.083665+00:00` |
| census duration | 208.063 s |
| activity probe started | `2026-08-20T00:26:42.325152+00:00` |
| activity probe completed | `2026-08-20T00:34:19.042832+00:00` |
| pages fetched | 487 |
| request | `{'route': 'GET /markets', 'status': 'open', 'limit': 200, 'mve_filter': 'exclude', 'paginated_to_exhaustion': True, 'probe_route': 'GET /markets?tickers=...'}` |

Four timestamps, not one, because the stratification rests on a measurement rather than a reading. The CENSUS establishes the frame; the PROBE measures the activity every rank is derived from. The canonical snapshot timestamp is therefore the probe's FIRST read — dating the stratification to the census would attribute the ranking to a measurement the ranking does not use. Every probe read timestamp is listed in section 4b so the window is fully reconstructible.

## 2. The ranking statistic

**`traded_contracts_per_minute`** — sourced from `['volume', 'volume_fp']`.

traded_contracts_per_minute — the market's MEASURED trading rate, obtained by reading the venue's LIFETIME traded-contract counter (`volume_fp`) repeatedly over a timed probe window and dividing the increase by the elapsed wall-clock minutes: (volume_last − volume_first) / minutes. It is a measurement taken by this tool, not a field the venue reports.

**Why it is a reasonable proxy for message rate.** It is a CURRENT RATE rather than an aggregate, and it is measured rather than assumed. The lifetime counter is monotonically non-decreasing, so its increase over a window is exactly the contracts traded IN that window, with none of the roll-off contamination that makes a trailing-24-hour field ambiguous. Message rate on the subscribed channels is driven by participants acting, and participants who trade also quote, cancel and replace — so a market trading N contracts per minute right now is the best orderable evidence obtainable without opening the socket. The probe also records how often the top of book CHANGED between reads (`top_of_book_change_rate`), which is a direct lower bound on orderbook_delta activity and is reported beside the primary statistic as a corroborating measure.

**Limitations — read these before using any number derived from this tape.**

- It counts TRADES, not MESSAGES. The tape's volume is dominated by orderbook_delta, which fires on every quote revision — including cancels and replaces that never trade. A market quoted by a churning maker can emit a high message rate at near-zero trading, and a market that trades in occasional blocks can emit few book messages. The rank correlation between this statistic and actual message rate is UNMEASURED, and can only be measured by the very capture this manifest precedes.
- The probe window is minutes long and the session is hours long. A rate measured over a short window is a noisy estimate of the session's rate, and event-driven markets (sports especially) reprice around scheduled events. The manifest is frozen anyway — a universe rechosen at capture time is not a preregistration — so a ranking that inverts between the freeze and the capture is an accepted cost, not a defect.
- The probe pool is a SCREENED subset, not the whole venue. Re-reading 70,000 markets several times is not a bounded read-only query, so the probe is run only on markets that a single census pass showed to be quoted, sized and non-zero-volume. A market that was dormant at census time and woke during the probe is invisible to this statistic. The screen is stated so the sampling frame stays explicit.
- It says nothing about the DEPTH of a book, and orderbook_delta cost scales with the number of price levels that move, not with contracts traded.
- Cross-environment transfer is unestablished. A DEMO rate is evidence about DEMO. Nothing here licenses treating it as a production rate, and DEMO flow may be simulated rather than human.

**The stronger statistic that was deliberately not used.** The only statistic that is NOT a proxy is the message rate itself, obtained by subscribing to each candidate and counting frames. That was deliberately NOT done: it requires opening the socket, which is the very thing this manifest must precede. Note the distinction that makes this measurement legitimate — the probe measures the STRATIFICATION VARIABLE the session was authorized to stratify on, which is preregistered; it does not measure how clean a ticker's telemetry looks, which would be the cherry-pick the selection rule exists to prevent.

## 3. The selection rule (frozen)

**Frame.** Every market returned by GET /markets with status=open and mve_filter=exclude, paginated to exhaustion. Multivariate-event (MVE) combinatorial shards are excluded because they are generated near-identical permutations of the same underlying legs — precisely the 'twelve near-identical markets' the universe requirement forbids.

**Eligibility gates.** A market is a candidate only if all hold:

- yes_bid > 0 AND yes_ask > 0 (a one-sided or empty book has no resting orders to delta)
- yes_bid < yes_ask (a crossed book cannot be the output of a live matching engine; it is corrupt or synthetic)
- yes_bid_size > 0 AND yes_ask_size > 0, both non-negative (a negative resting size is physically impossible)
- the market was PROBED — it appeared in at least two timed reads, so a rate could be measured at all
- traded_contracts_per_minute > 0.0 (MEASURED during the probe window: the market actually traded while we watched. This replaces the discredited updated_time freshness gate)
- the lifetime volume counter did not DECREASE across the probe (if it did, the field is not what we believe and the rate derived from it is not trustworthy)
- close_time is at least 6.0h after the snapshot, so the market survives the session's 4-hour maximum

> Every gate is STRUCTURAL — it asks whether a market can emit book messages at all. None can be evaluated from a capture, so no gate can be tuned toward tickers that produce cleaner telemetry.

**Ranking.** Eligible markets sorted by traded_contracts_per_minute DESCENDING, ties broken by ticker ASCENDING (lexicographic). The tie-break carries no behavioural information.

**Stratification.** Contiguous tertiles of the ranked ELIGIBLE set: ranks [1, n/3) = high, [n/3, 2n/3) = medium, [2n/3, n] = low. 4 markets taken from each.

**Within-stratum pick.** Two deterministic passes in rank order: first accept only markets whose event_ticker is unclaimed anywhere in the selection, then fill any shortfall from the remainder.

**Replacement rule.** If a selected market becomes unusable before the session, it is replaced by the NEXT market in frozen rank order within the same stratum whose event_ticker is unclaimed. Replacements must NOT be chosen by observed telemetry quality, message volume, or how clean the resulting tape looks — that is cherry-picking one level down. Any replacement is recorded as an amendment to this manifest, with its reason, BEFORE the session starts.

**Thresholds.**

| threshold | value |
|---|---|
| `min_separation_ratio` | 2.0 |
| `min_distinct_events` | 6 |
| `min_distinct_series` | 4 |
| `min_distinct_strike_types` | 2 |
| `max_per_event` | 3 |
| `min_traded_contracts_per_minute` | 0.0 |
| `min_seconds_to_close` | 21600 |
| `probe_reads` | 4 |
| `probe_interval_seconds` | 150.0 |
| `screen_pool_max` | 1200 |

## 4. Authorized session parameters (frozen by Eric, not alterable here)

| parameter | value |
|---|---|
| minimum duration | 2 h |
| minimum archived live frames | 100,000 |
| maximum duration | 4 h |
| universe size | 12 (4 per stratum) |

**Stop rule.** Run until BOTH the 2-hour minimum AND the 100,000-archived-live-frame minimum are satisfied — whichever occurs LATER — and stop unconditionally at the 4-hour maximum even if the frame minimum has not been reached. Reaching the 4-hour cap short of 100,000 frames is a FINDING about DEMO message rates, not a reason to extend the session.

### 4b. The activity probe (how the statistic was measured)

| field | value |
|---|---|
| `screen_pool_size` | 1200 |
| `screen_pool_capped_at` | 1200 |
| `screen_pool_was_truncated` | True |
| `candidates_offered_to_probe` | 1200 |
| `candidates_probed` | 1200 |
| `reads` | 4 |
| `span_minutes` | 7.612 |
| `lifetime_volume_non_monotonic` | 0 |
| `lifetime_volume_is_monotonic` | True |

**Probe read timestamps:** `2026-08-20T00:26:42.325152+00:00`, `2026-08-20T00:29:14.570381+00:00`, `2026-08-20T00:31:46.896033+00:00`, `2026-08-20T00:34:19.042832+00:00`

`lifetime_volume_is_monotonic` is the probe's own integrity control: the lifetime counter must never decrease, and the statistic is a difference of that counter. If it is ever `false`, the rate derived from it is not trustworthy and the affected markets are rejected rather than clamped to a plausible-looking value.

## 5. The candidate population

| quantity | value |
|---|---|
| open markets enumerated (the frame) | 97,392 |
| eligible after the gates | 648 |
| ineligible | 96,744 |
| distinct events among eligible | 271 |
| distinct series among eligible | 119 |

**Why each rejected market was rejected** (a market can fail several gates):

| gate failed | markets |
|---|---|
| `not_probed` | 96,192 |
| `no_resting_size` | 35,277 |
| `no_two_sided_quote` | 30,984 |
| `closes` | 3,002 |
| `no_measured_trading_during_probe` | 524 |

## 6. Frame integrity — does the statistic mean anything?

Computed over the WHOLE frame, not over the survivors. A funnel that only reports its output cannot tell you its input was corrupt.

| check | value |
|---|---|
| `frame_size` | 97392 |
| `markets_with_nonzero_screen_statistic` | 17954 |
| `markets_updated_within_24h` | 23875 |
| `markets_two_sided_quote` | 66408 |
| `markets_with_resting_size` | 62115 |
| `crossed_books` | 0 |
| `negative_resting_sizes` | 0 |
| `markets_probed` | 1200 |
| `markets_that_traded_during_the_probe` | 676 |
| `nonzero_24h_volume_but_updated_time_older_than_24h` | 12703 |
| `updated_time_contradiction_rate` | 0.70753 |
| `updated_time_tracks_trading` | False |

### 6b. Venue-model corrections forced by this run

> The CP6–CP9 preregistration requires that venue behaviour contradicting our assumptions updates the model of the venue BEFORE qualification proceeds. These are the corrections this run forced.

- `updated_time` does NOT track trading or quoting on this venue. Ten high-volume markets were re-read 180 seconds apart: `updated_time` moved on ZERO of them, while the lifetime volume counter moved on 10/10 and the top of book moved on 10/10. `updated_time` is a market-DEFINITION timestamp. An earlier revision of this tool used it as a freshness gate and rejected 73,057 of 73,630 markets as 'stale' — including markets that were trading hundreds of thousands of contracts per minute at that instant. That gate was measuring the wrong thing and has been REMOVED; freshness is now established by direct measurement (a market must trade during the probe window), which cannot be fooled the same way. Recorded here because the CP6-CP9 preregistration requires venue behaviour that contradicts our assumptions to update the venue model BEFORE qualification proceeds.

## 7. The eligible population, complete and ranked

`eligible_ranked` is COMPLETE: it is the entire pool the twelve were drawn from, every member carrying its statistic value, rank and stratum. The full enumerated frame is larger than is sensible to commit, so it is represented by `frame_digest_sha256` plus the highest-statistic rejected markets, and can be reproduced exactly by re-running the enumeration and comparing the digest.

**Frame digest (SHA-256):** `c601fcf11de39fed2527f2eae3c289e5091a597dbe8da116571e3d8e0a7cc1c0`  
*covers:* sha256 of ticker\x1f repr(statistic) \x1e, ticker-sorted, whole frame

| rank | stratum | ticker | event | series | contracts/min | ToB change rate | 24h vol (screen) | selected |
|---:|---|---|---|---|---:|---:|---:|---|
| 1 | high | `KXATPMATCH-26AUG19BORNAK-BOR` | `KXATPMATCH-26AUG19BORNAK` | `KXATPMATCH` | 29,328.09 | 1.00 | 1,614,285 | **YES** |
| 2 | high | `KXATPMATCH-26AUG19BORNAK-NAK` | `KXATPMATCH-26AUG19BORNAK` | `KXATPMATCH` | 26,700.88 | 1.00 | 2,421,055 | no |
| 3 | high | `KXMLBGAME-26AUG191940SEAMIL-MIL` | `KXMLBGAME-26AUG191940SEAMIL` | `KXMLBGAME` | 23,582.81 | 1.00 | 1,331,742 | **YES** |
| 4 | high | `KXMLBGAME-26AUG191805MIAPHI-PHI` | `KXMLBGAME-26AUG191805MIAPHI` | `KXMLBGAME` | 19,534.93 | 1.00 | 1,001,064 | **YES** |
| 5 | high | `KXMLBGAME-26AUG191835NYYBAL-BAL` | `KXMLBGAME-26AUG191835NYYBAL` | `KXMLBGAME` | 18,865.45 | 1.00 | 666,077 | **YES** |
| 6 | high | `KXWTAMATCH-26AUG19KEYWAN-WAN` | `KXWTAMATCH-26AUG19KEYWAN` | `KXWTAMATCH` | 14,804.21 | 1.00 | 857,110 | no |
| 7 | high | `KXWTAMATCH-26AUG19KEYWAN-KEY` | `KXWTAMATCH-26AUG19KEYWAN` | `KXWTAMATCH` | 13,687.57 | 1.00 | 421,549 | no |
| 8 | high | `KXMLBGAME-26AUG191940SEAMIL-SEA` | `KXMLBGAME-26AUG191940SEAMIL` | `KXMLBGAME` | 13,362.81 | 1.00 | 545,458 | no |
| 9 | high | `KXMLSGAME-26AUG19PHIMIA-MIA` | `KXMLSGAME-26AUG19PHIMIA` | `KXMLSGAME` | 13,097.97 | 1.00 | 1,518,037 | no |
| 10 | high | `KXATPMATCH-26AUG19FRIOCO-FRI` | `KXATPMATCH-26AUG19FRIOCO` | `KXATPMATCH` | 12,907.39 | 1.00 | 296,448 | no |
| 11 | high | `KXMLSGAME-26AUG19SEAATX-SEA` | `KXMLSGAME-26AUG19SEAATX` | `KXMLSGAME` | 11,051.80 | 1.00 | 40,691 | no |
| 12 | high | `KXMLBGAME-26AUG191835NYYBAL-NYY` | `KXMLBGAME-26AUG191835NYYBAL` | `KXMLBGAME` | 10,208.84 | 1.00 | 1,145,441 | no |
| 13 | high | `KXMLBGAME-26AUG192005WSHTEX-WSH` | `KXMLBGAME-26AUG192005WSHTEX` | `KXMLBGAME` | 9,537.65 | 1.00 | 342,579 | no |
| 14 | high | `KXMLBGAME-26AUG191840TORTB-TOR` | `KXMLBGAME-26AUG191840TORTB` | `KXMLBGAME` | 9,283.21 | 1.00 | 1,037,944 | no |
| 15 | high | `KXMLBRFI-26AUG192040LADCOL` | `KXMLBRFI-26AUG192040LADCOL` | `KXMLBRFI` | 7,664.09 | 1.00 | 67,517 | no |
| 16 | high | `KXATPCHALLENGERMATCH-26AUG19KWONMOC-MOC` | `KXATPCHALLENGERMATCH-26AUG19KWONMOC` | `KXATPCHALLENGERMATCH` | 6,752.32 | 1.00 | 581,880 | no |
| 17 | high | `KXATPCHALLENGERMATCH-26AUG19KWONMOC-KWON` | `KXATPCHALLENGERMATCH-26AUG19KWONMOC` | `KXATPCHALLENGERMATCH` | 6,700.90 | 1.00 | 337,502 | no |
| 18 | high | `KXMLBGAME-26AUG191840STLCIN-CIN` | `KXMLBGAME-26AUG191840STLCIN` | `KXMLBGAME` | 6,433.71 | 1.00 | 621,399 | no |
| 19 | high | `KXMLBGAME-26AUG191805MIAPHI-MIA` | `KXMLBGAME-26AUG191805MIAPHI` | `KXMLBGAME` | 6,261.37 | 1.00 | 1,412,204 | no |
| 20 | high | `KXMLBGAME-26AUG191840TORTB-TB` | `KXMLBGAME-26AUG191840TORTB` | `KXMLBGAME` | 6,213.52 | 1.00 | 544,952 | no |
| 21 | high | `KXMLBSPREAD-26AUG192040LADCOL-LAD2` | `KXMLBSPREAD-26AUG192040LADCOL` | `KXMLBSPREAD` | 5,559.60 | 1.00 | 108,250 | no |
| 22 | high | `KXMLBGAME-26AUG191840STLCIN-STL` | `KXMLBGAME-26AUG191840STLCIN` | `KXMLBGAME` | 5,421.92 | 1.00 | 428,357 | no |
| 23 | high | `KXATPCHALLENGERMATCH-26AUG19PACBAR-BAR` | `KXATPCHALLENGERMATCH-26AUG19PACBAR` | `KXATPCHALLENGERMATCH` | 4,810.69 | 1.00 | 207,478 | no |
| 24 | high | `KXATPCHALLENGERMATCH-26AUG19GALPRI-PRI` | `KXATPCHALLENGERMATCH-26AUG19GALPRI` | `KXATPCHALLENGERMATCH` | 4,753.06 | 1.00 | 222,692 | no |
| 25 | high | `KXBRASILEIROBGAME-26AUG19AVAREC-AVA` | `KXBRASILEIROBGAME-26AUG19AVAREC` | `KXBRASILEIROBGAME` | 4,662.70 | 1.00 | 47,635 | no |
| 26 | high | `KXMLBGAME-26AUG191940ATHKC-ATH` | `KXMLBGAME-26AUG191940ATHKC` | `KXMLBGAME` | 4,310.51 | 1.00 | 535,488 | no |
| 27 | high | `KXMLBGAME-26AUG192040LADCOL-LAD` | `KXMLBGAME-26AUG192040LADCOL` | `KXMLBGAME` | 4,231.01 | 1.00 | 607,305 | no |
| 28 | high | `KXMLBGAME-26AUG192005WSHTEX-TEX` | `KXMLBGAME-26AUG192005WSHTEX` | `KXMLBGAME` | 4,199.98 | 1.00 | 227,207 | no |
| 29 | high | `KXWNBASPREAD-26AUG19TORWSH-WSH19` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 4,007.39 | 1.00 | 91,961 | no |
| 30 | high | `KXATPMATCH-26AUG19FRIOCO-OCO` | `KXATPMATCH-26AUG19FRIOCO` | `KXATPMATCH` | 3,675.56 | 1.00 | 719,976 | no |
| 31 | high | `KXWNBAGAME-26AUG19TORWSH-WSH` | `KXWNBAGAME-26AUG19TORWSH` | `KXWNBAGAME` | 3,621.33 | 1.00 | 675,758 | no |
| 32 | high | `KXMLBGAME-26AUG191940ATHKC-KC` | `KXMLBGAME-26AUG191940ATHKC` | `KXMLBGAME` | 3,552.28 | 1.00 | 581,955 | no |
| 33 | high | `KXBRASILEIROBGAME-26AUG19FORSBE-TIE` | `KXBRASILEIROBGAME-26AUG19FORSBE` | `KXBRASILEIROBGAME` | 3,417.34 | 1.00 | 50,626 | no |
| 34 | high | `KXMLSGAME-26AUG19CINNYC-CIN` | `KXMLSGAME-26AUG19CINNYC` | `KXMLSGAME` | 2,998.35 | 1.00 | 931,929 | no |
| 35 | high | `KXATPCHALLENGERMATCH-26AUG19UGOMEJ-MEJ` | `KXATPCHALLENGERMATCH-26AUG19UGOMEJ` | `KXATPCHALLENGERMATCH` | 2,903.01 | 1.00 | 94,531 | no |
| 36 | high | `KXMLBTOTAL-26AUG191805MIAPHI-6` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 2,834.07 | 1.00 | 94,961 | no |
| 37 | high | `KXMLBSPREAD-26AUG191805MIAPHI-PHI4` | `KXMLBSPREAD-26AUG191805MIAPHI` | `KXMLBSPREAD` | 2,699.11 | 1.00 | 36,633 | no |
| 38 | high | `KXMLBGAME-26AUG191840SFCLE-SF` | `KXMLBGAME-26AUG191840SFCLE` | `KXMLBGAME` | 2,697.20 | 1.00 | 417,473 | no |
| 39 | high | `KXMLBGAME-26AUG192010LAAHOU-HOU` | `KXMLBGAME-26AUG192010LAAHOU` | `KXMLBGAME` | 2,690.84 | 1.00 | 254,855 | no |
| 40 | high | `KXMLBSPREAD-26AUG191840TORTB-TB2` | `KXMLBSPREAD-26AUG191840TORTB` | `KXMLBSPREAD` | 2,410.13 | 1.00 | 305,178 | no |
| 41 | high | `KXMLBSPREAD-26AUG192040LADCOL-LAD3` | `KXMLBSPREAD-26AUG192040LADCOL` | `KXMLBSPREAD` | 2,304.40 | 1.00 | 43,218 | no |
| 42 | high | `KXMLSGAME-26AUG19DCUNE-NE` | `KXMLSGAME-26AUG19DCUNE` | `KXMLSGAME` | 2,277.76 | 1.00 | 37,086 | no |
| 43 | high | `KXMLBSPREAD-26AUG191835NYYBAL-NYY2` | `KXMLBSPREAD-26AUG191835NYYBAL` | `KXMLBSPREAD` | 2,266.66 | 1.00 | 138,343 | no |
| 44 | high | `KXWNBATOTAL-26AUG19TORWSH-167` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 2,242.33 | 1.00 | 51,949 | no |
| 45 | high | `KXNWSLGAME-26AUG19RACREI-RAC` | `KXNWSLGAME-26AUG19RACREI` | `KXNWSLGAME` | 2,214.55 | 1.00 | 63,544 | no |
| 46 | high | `KXMLSGAME-26AUG19DCUNE-DCU` | `KXMLSGAME-26AUG19DCUNE` | `KXMLSGAME` | 2,167.81 | 1.00 | 182,290 | no |
| 47 | high | `KXBRASILEIROBGAME-26AUG19AVAREC-TIE` | `KXBRASILEIROBGAME-26AUG19AVAREC` | `KXBRASILEIROBGAME` | 2,136.79 | 1.00 | 28,141 | no |
| 48 | high | `KXMLBGAME-26AUG192040LADCOL-COL` | `KXMLBGAME-26AUG192040LADCOL` | `KXMLBGAME` | 2,135.44 | 1.00 | 61,669 | no |
| 49 | high | `KXATPCHALLENGERMATCH-26AUG19GALPRI-GAL` | `KXATPCHALLENGERMATCH-26AUG19GALPRI` | `KXATPCHALLENGERMATCH` | 2,086.59 | 1.00 | 88,256 | no |
| 50 | high | `KXMLBTOTAL-26AUG191840TORTB-16` | `KXMLBTOTAL-26AUG191840TORTB` | `KXMLBTOTAL` | 1,974.68 | 1.00 | 37,041 | no |
| 51 | high | `KXMLSGAME-26AUG19NYRBNSH-NSH` | `KXMLSGAME-26AUG19NYRBNSH` | `KXMLSGAME` | 1,961.21 | 1.00 | 420,606 | no |
| 52 | high | `KXMLBGAME-26AUG192010LAAHOU-LAA` | `KXMLBGAME-26AUG192010LAAHOU` | `KXMLBGAME` | 1,878.92 | 1.00 | 117,961 | no |
| 53 | high | `KXMLBTOTAL-26AUG191805MIAPHI-9` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 1,867.68 | 1.00 | 434,117 | no |
| 54 | high | `KXMLBGAME-26AUG191840SFCLE-CLE` | `KXMLBGAME-26AUG191840SFCLE` | `KXMLBGAME` | 1,863.31 | 1.00 | 573,944 | no |
| 55 | high | `KXDIMAYORGAME-26AUG19CUCINT-INT` | `KXDIMAYORGAME-26AUG19CUCINT` | `KXDIMAYORGAME` | 1,785.82 | 1.00 | 43,623 | no |
| 56 | high | `KXWNBAGAME-26AUG19TORWSH-TOR` | `KXWNBAGAME-26AUG19TORWSH` | `KXWNBAGAME` | 1,775.93 | 1.00 | 671,005 | no |
| 57 | high | `KXATPCHALLENGERMATCH-26AUG19ONCSKA-ONC` | `KXATPCHALLENGERMATCH-26AUG19ONCSKA` | `KXATPCHALLENGERMATCH` | 1,775.59 | 1.00 | 273,645 | no |
| 58 | high | `KXWNBAGAME-26AUG19MINGS-MIN` | `KXWNBAGAME-26AUG19MINGS` | `KXWNBAGAME` | 1,659.01 | 1.00 | 245,046 | no |
| 59 | high | `KXMLBHR-26AUG192040LADCOL-LADSOHTANI17-1` | `KXMLBHR-26AUG192040LADCOL` | `KXMLBHR` | 1,575.03 | 1.00 | 137,137 | no |
| 60 | high | `KXATPCHALLENGERMATCH-26AUG19UGOMEJ-UGO` | `KXATPCHALLENGERMATCH-26AUG19UGOMEJ` | `KXATPCHALLENGERMATCH` | 1,572.54 | 1.00 | 71,962 | no |
| 61 | high | `KXBRASILEIROBTOTAL-26AUG19AVAREC-4` | `KXBRASILEIROBTOTAL-26AUG19AVAREC` | `KXBRASILEIROBTOTAL` | 1,530.46 | 1.00 | 12,963 | no |
| 62 | high | `KXCONMEBOLSUDGAME-26AUG19SFERIV-RIV` | `KXCONMEBOLSUDGAME-26AUG19SFERIV` | `KXCONMEBOLSUDGAME` | 1,523.35 | 1.00 | 34,254 | no |
| 63 | high | `KXVALORANTGAME-26AUG192000G2M80-M80` | `KXVALORANTGAME-26AUG192000G2M80` | `KXVALORANTGAME` | 1,521.02 | 1.00 | 85,155 | no |
| 64 | high | `KXMLBSPREAD-26AUG191940ATHKC-ATH3` | `KXMLBSPREAD-26AUG191940ATHKC` | `KXMLBSPREAD` | 1,516.42 | 1.00 | 26,236 | no |
| 65 | high | `KXMLBTOTAL-26AUG191940ATHKC-8` | `KXMLBTOTAL-26AUG191940ATHKC` | `KXMLBTOTAL` | 1,463.09 | 1.00 | 18,183 | no |
| 66 | high | `KXCPLMATCH-26AUG191900GAWSTL-STL` | `KXCPLMATCH-26AUG191900GAWSTL` | `KXCPLMATCH` | 1,413.49 | 1.00 | 426,836 | no |
| 67 | high | `KXMLBSPREAD-26AUG192040LADCOL-LAD4` | `KXMLBSPREAD-26AUG192040LADCOL` | `KXMLBSPREAD` | 1,382.53 | 1.00 | 9,878 | no |
| 68 | high | `KXMLSGAME-26AUG19PHIMIA-TIE` | `KXMLSGAME-26AUG19PHIMIA` | `KXMLSGAME` | 1,351.53 | 1.00 | 146,441 | no |
| 69 | high | `KXMLBTOTAL-26AUG191940SEAMIL-9` | `KXMLBTOTAL-26AUG191940SEAMIL` | `KXMLBTOTAL` | 1,345.58 | 1.00 | 146,683 | no |
| 70 | high | `KXPGATOUR-BMC26-RFOW` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 1,327.35 | 0.33 | 128,280 | no |
| 71 | high | `KXMLBTOTAL-26AUG191835NYYBAL-9` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 1,307.59 | 1.00 | 101,016 | no |
| 72 | high | `KXDIMAYORGAME-26AUG19CUCINT-CUC` | `KXDIMAYORGAME-26AUG19CUCINT` | `KXDIMAYORGAME` | 1,282.71 | 1.00 | 26,979 | no |
| 73 | high | `KXMLBTOTAL-26AUG192040LADCOL-12` | `KXMLBTOTAL-26AUG192040LADCOL` | `KXMLBTOTAL` | 1,267.42 | 1.00 | 133,448 | no |
| 74 | high | `KXMLSTOTAL-26AUG19PHIMIA-5` | `KXMLSTOTAL-26AUG19PHIMIA` | `KXMLSTOTAL` | 1,212.70 | 1.00 | 27,009 | no |
| 75 | high | `KXMLBTOTAL-26AUG191840SFCLE-3` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 1,173.20 | 1.00 | 13,707 | no |
| 76 | high | `KXMLSGAME-26AUG19CINNYC-NYC` | `KXMLSGAME-26AUG19CINNYC` | `KXMLSGAME` | 1,138.57 | 1.00 | 91,408 | no |
| 77 | high | `KXMLSGAME-26AUG19SKCSTL-STL` | `KXMLSGAME-26AUG19SKCSTL` | `KXMLSGAME` | 1,137.29 | 1.00 | 50,640 | no |
| 78 | high | `KXBRASILEIROBGAME-26AUG19FORSBE-FOR` | `KXBRASILEIROBGAME-26AUG19FORSBE` | `KXBRASILEIROBGAME` | 1,089.20 | 1.00 | 72,446 | no |
| 79 | high | `KXMLBTOTAL-26AUG192005WSHTEX-7` | `KXMLBTOTAL-26AUG192005WSHTEX` | `KXMLBTOTAL` | 1,056.66 | 1.00 | 12,721 | no |
| 80 | high | `KXMLBSPREAD-26AUG191940ATHKC-ATH2` | `KXMLBSPREAD-26AUG191940ATHKC` | `KXMLBSPREAD` | 1,044.82 | 1.00 | 43,694 | no |
| 81 | high | `KXPGATOUR-BMC26-MTHO` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 1,026.85 | 1.00 | 219,620 | no |
| 82 | high | `KXWTASETWINNER-26AUG19KEYWAN-2-WAN` | `KXWTASETWINNER-26AUG19KEYWAN-2` | `KXWTASETWINNER` | 1,010.15 | 1.00 | 12,022 | no |
| 83 | high | `KXMLBTOTAL-26AUG191805MIAPHI-7` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 997.46 | 1.00 | 161,587 | no |
| 84 | high | `KXPGAR1LEAD-BMC26-LABE` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 913.36 | 0.67 | 26,369 | no |
| 85 | high | `KXATPCHALLENGERMATCH-26AUG19PACBAR-PAC` | `KXATPCHALLENGERMATCH-26AUG19PACBAR` | `KXATPCHALLENGERMATCH` | 909.51 | 1.00 | 149,047 | no |
| 86 | high | `KXATPMATCH-26AUG19TIAAUG-TIA` | `KXATPMATCH-26AUG19TIAAUG` | `KXATPMATCH` | 877.35 | 1.00 | 57,775 | no |
| 87 | high | `KXMLBSPREAD-26AUG191840STLCIN-STL2` | `KXMLBSPREAD-26AUG191840STLCIN` | `KXMLBSPREAD` | 875.80 | 1.00 | 22,537 | no |
| 88 | high | `KXMLBTOTAL-26AUG192010LAAHOU-9` | `KXMLBTOTAL-26AUG192010LAAHOU` | `KXMLBTOTAL` | 868.98 | 1.00 | 64,765 | no |
| 89 | high | `KXATPCHALLENGERMATCH-26AUG19ONCSKA-SKA` | `KXATPCHALLENGERMATCH-26AUG19ONCSKA` | `KXATPCHALLENGERMATCH` | 850.95 | 1.00 | 175,055 | no |
| 90 | high | `KXMLS1H-26AUG19PHIMIA-MIA` | `KXMLS1H-26AUG19PHIMIA` | `KXMLS1H` | 846.29 | 1.00 | 49,316 | no |
| 91 | high | `KXMLBTOTAL-26AUG191840STLCIN-6` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 841.01 | 1.00 | 47,505 | no |
| 92 | high | `KXMLBSPREAD-26AUG191940SEAMIL-SEA5` | `KXMLBSPREAD-26AUG191940SEAMIL` | `KXMLBSPREAD` | 810.68 | 1.00 | 24,469 | no |
| 93 | high | `KXBRASILEIROBTOTAL-26AUG19AVAREC-3` | `KXBRASILEIROBTOTAL-26AUG19AVAREC` | `KXBRASILEIROBTOTAL` | 806.12 | 1.00 | 25,784 | no |
| 94 | high | `KXATPMATCH-26AUG19TIAAUG-AUG` | `KXATPMATCH-26AUG19TIAAUG` | `KXATPMATCH` | 800.85 | 1.00 | 115,093 | no |
| 95 | high | `KXMLBTEAMTOTAL-26AUG192040LADCOL-LAD7` | `KXMLBTEAMTOTAL-26AUG192040LADCOL` | `KXMLBTEAMTOTAL` | 790.48 | 1.00 | 27,259 | no |
| 96 | high | `KXMLSTOTAL-26AUG19CLBMTL-5` | `KXMLSTOTAL-26AUG19CLBMTL` | `KXMLSTOTAL` | 778.84 | 1.00 | 13,866 | no |
| 97 | high | `KXWNBATOTAL-26AUG19TORWSH-182` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 759.81 | 1.00 | 70,419 | no |
| 98 | high | `KXMLBHR-26AUG192040LADCOL-LADSOHTANI17-2` | `KXMLBHR-26AUG192040LADCOL` | `KXMLBHR` | 745.58 | 1.00 | 85,639 | no |
| 99 | high | `KXMLBTEAMTOTAL-26AUG192040LADCOL-LAD3` | `KXMLBTEAMTOTAL-26AUG192040LADCOL` | `KXMLBTEAMTOTAL` | 731.85 | 1.00 | 18,518 | no |
| 100 | high | `KXMLBRFI-26AUG192010LAAHOU` | `KXMLBRFI-26AUG192010LAAHOU` | `KXMLBRFI` | 730.69 | 0.67 | 157,597 | no |
| 101 | high | `KXMLSTOTAL-26AUG19SKCSTL-3` | `KXMLSTOTAL-26AUG19SKCSTL` | `KXMLSTOTAL` | 722.31 | 1.00 | 11,045 | no |
| 102 | high | `KXMLBTOTAL-26AUG191835NYYBAL-10` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 718.48 | 1.00 | 368,479 | no |
| 103 | high | `KXPGATOUR-BMC26-JKNA` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 718.06 | 0.67 | 152,643 | no |
| 104 | high | `KXMLBTOTAL-26AUG191940SEAMIL-8` | `KXMLBTOTAL-26AUG191940SEAMIL` | `KXMLBTOTAL` | 715.94 | 1.00 | 810,334 | no |
| 105 | high | `KXMLSGAME-26AUG19CLBMTL-CLB` | `KXMLSGAME-26AUG19CLBMTL` | `KXMLSGAME` | 711.01 | 1.00 | 260,966 | no |
| 106 | high | `KXDIMAYORGAME-26AUG19CUCINT-TIE` | `KXDIMAYORGAME-26AUG19CUCINT` | `KXDIMAYORGAME` | 707.59 | 1.00 | 14,950 | no |
| 107 | high | `KXMLSGAME-26AUG19PHIMIA-PHI` | `KXMLSGAME-26AUG19PHIMIA` | `KXMLSGAME` | 697.09 | 1.00 | 261,608 | no |
| 108 | high | `KXBRASILEIROBGAME-26AUG19FORSBE-SBE` | `KXBRASILEIROBGAME-26AUG19FORSBE` | `KXBRASILEIROBGAME` | 690.51 | 1.00 | 81,890 | no |
| 109 | high | `KXMLBTOTAL-26AUG191840STLCIN-9` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 665.91 | 1.00 | 72,815 | no |
| 110 | high | `KXMLBTOTAL-26AUG191940SEAMIL-10` | `KXMLBTOTAL-26AUG191940SEAMIL` | `KXMLBTOTAL` | 664.00 | 1.00 | 84,423 | no |
| 111 | high | `KXMLBTOTAL-26AUG192005WSHTEX-8` | `KXMLBTOTAL-26AUG192005WSHTEX` | `KXMLBTOTAL` | 637.29 | 1.00 | 92,322 | no |
| 112 | high | `KXMLS1H-26AUG19CINNYC-TIE` | `KXMLS1H-26AUG19CINNYC` | `KXMLS1H` | 636.13 | 1.00 | 7,520 | no |
| 113 | high | `KXMLSGAME-26AUG19SKCSTL-SKC` | `KXMLSGAME-26AUG19SKCSTL` | `KXMLSGAME` | 635.44 | 1.00 | 20,200 | no |
| 114 | high | `KXMLSGAME-26AUG19MINATL-MIN` | `KXMLSGAME-26AUG19MINATL` | `KXMLSGAME` | 629.67 | 1.00 | 41,099 | no |
| 115 | high | `KXMLBSPREAD-26AUG192010LAAHOU-HOU2` | `KXMLBSPREAD-26AUG192010LAAHOU` | `KXMLBSPREAD` | 622.98 | 1.00 | 129,290 | no |
| 116 | high | `KXMLBTOTAL-26AUG192010LAAHOU-7` | `KXMLBTOTAL-26AUG192010LAAHOU` | `KXMLBTOTAL` | 618.91 | 1.00 | 8,138 | no |
| 117 | high | `KXMLSSPREAD-26AUG19PHIMIA-MIA3` | `KXMLSSPREAD-26AUG19PHIMIA` | `KXMLSSPREAD` | 596.17 | 1.00 | 19,024 | no |
| 118 | high | `KXMLBTOTAL-26AUG191840SFCLE-5` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 594.99 | 1.00 | 60,695 | no |
| 119 | high | `KXMLSTOTAL-26AUG19PHIMIA-6` | `KXMLSTOTAL-26AUG19PHIMIA` | `KXMLSTOTAL` | 587.93 | 1.00 | 54,072 | no |
| 120 | high | `KXMLBOUTS-26AUG192040LADCOL-COLKFREELAND21-18` | `KXMLBOUTS-26AUG192040LADCOL` | `KXMLBOUTS` | 571.56 | 1.00 | 49,583 | no |
| 121 | high | `KXDIMAYORTOTAL-26AUG19CUCINT-3` | `KXDIMAYORTOTAL-26AUG19CUCINT` | `KXDIMAYORTOTAL` | 569.38 | 1.00 | 16,191 | no |
| 122 | high | `KXMLSGAME-26AUG19DCUNE-TIE` | `KXMLSGAME-26AUG19DCUNE` | `KXMLSGAME` | 566.11 | 1.00 | 14,352 | no |
| 123 | high | `KXMLBSPREAD-26AUG191805MIAPHI-PHI3` | `KXMLBSPREAD-26AUG191805MIAPHI` | `KXMLBSPREAD` | 556.54 | 1.00 | 103,318 | no |
| 124 | high | `KXCONMEBOLSUDGAME-26AUG19SFERIV-SFE` | `KXCONMEBOLSUDGAME-26AUG19SFERIV` | `KXCONMEBOLSUDGAME` | 552.72 | 1.00 | 24,692 | no |
| 125 | high | `KXWNBATOTAL-26AUG19TORWSH-179` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 532.50 | 1.00 | 100,717 | no |
| 126 | high | `KXMLSGAME-26AUG19ORLCHI-CHI` | `KXMLSGAME-26AUG19ORLCHI` | `KXMLSGAME` | 529.80 | 1.00 | 202,974 | no |
| 127 | high | `KXCONMEBOLLIBADVANCE-26AUG19FLACRU-CRU` | `KXCONMEBOLLIBADVANCE-26AUG19FLACRU` | `KXCONMEBOLLIBADVANCE` | 522.79 | 1.00 | 8,422 | no |
| 128 | high | `KXCPLMATCH-26AUG191900GAWSTL-GAW` | `KXCPLMATCH-26AUG191900GAWSTL` | `KXCPLMATCH` | 515.71 | 1.00 | 280,773 | no |
| 129 | high | `KXCONMEBOLLIBGAME-26AUG19FLACRU-CRU` | `KXCONMEBOLLIBGAME-26AUG19FLACRU` | `KXCONMEBOLLIBGAME` | 513.11 | 1.00 | 11,103 | no |
| 130 | high | `KXMLSGAME-26AUG19CLBMTL-MTL` | `KXMLSGAME-26AUG19CLBMTL` | `KXMLSGAME` | 508.15 | 1.00 | 58,217 | no |
| 131 | high | `KXMLSGAME-26AUG19MINATL-ATL` | `KXMLSGAME-26AUG19MINATL` | `KXMLSGAME` | 504.37 | 1.00 | 27,997 | no |
| 132 | high | `KXMLBTOTAL-26AUG191840STLCIN-3` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 490.93 | 1.00 | 10,179 | no |
| 133 | high | `KXMLSTOTAL-26AUG19PHIMIA-4` | `KXMLSTOTAL-26AUG19PHIMIA` | `KXMLSTOTAL` | 477.07 | 1.00 | 179,990 | no |
| 134 | high | `KXMLBSPREAD-26AUG191840SFCLE-SF3` | `KXMLBSPREAD-26AUG191840SFCLE` | `KXMLBSPREAD` | 473.94 | 1.00 | 14,284 | no |
| 135 | high | `KXMLS1H-26AUG19TORCLT-CLT` | `KXMLS1H-26AUG19TORCLT` | `KXMLS1H` | 450.86 | 1.00 | 18,385 | no |
| 136 | high | `KXBRASILEIROBTOTAL-26AUG19FORSBE-3` | `KXBRASILEIROBTOTAL-26AUG19FORSBE` | `KXBRASILEIROBTOTAL` | 445.95 | 1.00 | 21,139 | no |
| 137 | high | `KXMLSTOTAL-26AUG19NYRBNSH-2` | `KXMLSTOTAL-26AUG19NYRBNSH` | `KXMLSTOTAL` | 445.00 | 1.00 | 10,587 | no |
| 138 | high | `KXNWSLGAME-26AUG19RACREI-TIE` | `KXNWSLGAME-26AUG19RACREI` | `KXNWSLGAME` | 441.24 | 1.00 | 33,046 | no |
| 139 | high | `KXMLBSPREAD-26AUG191940SEAMIL-MIL2` | `KXMLBSPREAD-26AUG191940SEAMIL` | `KXMLBSPREAD` | 440.62 | 1.00 | 176,996 | no |
| 140 | high | `KXMLS1HTOTAL-26AUG19NYRBNSH-1` | `KXMLS1HTOTAL-26AUG19NYRBNSH` | `KXMLS1HTOTAL` | 440.44 | 1.00 | 10,126 | no |
| 141 | high | `KXMLSTOTAL-26AUG19NYRBNSH-5` | `KXMLSTOTAL-26AUG19NYRBNSH` | `KXMLSTOTAL` | 433.36 | 1.00 | 18,472 | no |
| 142 | high | `KXMLBTOTAL-26AUG191940SEAMIL-12` | `KXMLBTOTAL-26AUG191940SEAMIL` | `KXMLBTOTAL` | 432.81 | 1.00 | 97,223 | no |
| 143 | high | `KXMLBF5-26AUG191940ATHKC-KC` | `KXMLBF5-26AUG191940ATHKC` | `KXMLBF5` | 432.06 | 1.00 | 6,972 | no |
| 144 | high | `KXVALORANTGAME-26AUG192000G2M80-G2` | `KXVALORANTGAME-26AUG192000G2M80` | `KXVALORANTGAME` | 430.22 | 1.00 | 48,940 | no |
| 145 | high | `KXCONMEBOLLIBGAME-26AUG19FLACRU-FLA` | `KXCONMEBOLLIBGAME-26AUG19FLACRU` | `KXCONMEBOLLIBGAME` | 427.97 | 1.00 | 14,651 | no |
| 146 | high | `KXMLBHR-26AUG192040LADCOL-LADAPAGES44-1` | `KXMLBHR-26AUG192040LADCOL` | `KXMLBHR` | 418.65 | 1.00 | 16,652 | no |
| 147 | high | `KXPGATOUR-BMC26-LABE` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 417.18 | 1.00 | 268,679 | no |
| 148 | high | `KXMLBTOTAL-26AUG191805MIAPHI-8` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 413.67 | 1.00 | 174,671 | no |
| 149 | high | `KXDIMAYORTOTAL-26AUG19CUCINT-4` | `KXDIMAYORTOTAL-26AUG19CUCINT` | `KXDIMAYORTOTAL` | 410.31 | 1.00 | 10,185 | no |
| 150 | high | `KXPGATOUR-BMC26-MMCC` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 408.93 | 0.33 | 411,596 | no |
| 151 | high | `KXMLBSPREAD-26AUG191940ATHKC-KC2` | `KXMLBSPREAD-26AUG191940ATHKC` | `KXMLBSPREAD` | 408.53 | 1.00 | 265,056 | no |
| 152 | high | `KXMLBTOTAL-26AUG191840STLCIN-8` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 397.12 | 1.00 | 49,032 | no |
| 153 | high | `KXMLBSPREAD-26AUG191835NYYBAL-BAL2` | `KXMLBSPREAD-26AUG191835NYYBAL` | `KXMLBSPREAD` | 391.27 | 1.00 | 39,327 | no |
| 154 | high | `KXMLSGAME-26AUG19ORLCHI-ORL` | `KXMLSGAME-26AUG19ORLCHI` | `KXMLSGAME` | 385.56 | 1.00 | 72,704 | no |
| 155 | high | `KXNWSLTOTAL-26AUG19RACREI-4` | `KXNWSLTOTAL-26AUG19RACREI` | `KXNWSLTOTAL` | 383.99 | 1.00 | 6,805 | no |
| 156 | high | `KXPGATOUR-BMC26-TKIM` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 368.61 | 0.67 | 159,572 | no |
| 157 | high | `KXMLBTOTAL-26AUG191840SFCLE-2` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 359.44 | 1.00 | 15,257 | no |
| 158 | high | `KXWNBATOTAL-26AUG19MINGS-161` | `KXWNBATOTAL-26AUG19MINGS` | `KXWNBATOTAL` | 358.70 | 1.00 | 9,225 | no |
| 159 | high | `KXBTCMAXMON-BTC-26AUG31-7000000` | `KXBTCMAXMON-BTC-26AUG31` | `KXBTCMAXMON` | 355.79 | 1.00 | 328,075 | no |
| 160 | high | `KXMLSGAME-26AUG19NYRBNSH-NYRB` | `KXMLSGAME-26AUG19NYRBNSH` | `KXMLSGAME` | 337.77 | 1.00 | 47,248 | no |
| 161 | high | `KXWNBASPREAD-26AUG19TORWSH-WSH22` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 328.26 | 1.00 | 32,356 | no |
| 162 | high | `KXPRIMARYMOV-SCRSENS262-RNOR-P62` | `KXPRIMARYMOV-SCRSENS262` | `KXPRIMARYMOV` | 327.12 | 0.67 | 10,692 | no |
| 163 | high | `KXTESTMATCH-26AUG190600PAKENG-PAK` | `KXTESTMATCH-26AUG190600PAKENG` | `KXTESTMATCH` | 317.32 | 1.00 | 290,092 | no |
| 164 | high | `KXMLBTOTAL-26AUG192010LAAHOU-8` | `KXMLBTOTAL-26AUG192010LAAHOU` | `KXMLBTOTAL` | 317.05 | 1.00 | 12,891 | no |
| 165 | high | `KXMLBTOTAL-26AUG191835NYYBAL-8` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 312.19 | 1.00 | 15,842 | no |
| 166 | high | `KXPGATOUR-BMC26-XSCH` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 311.61 | 0.33 | 72,402 | no |
| 167 | high | `KXMLBTOTAL-26AUG191805MIAPHI-10` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 301.75 | 1.00 | 72,232 | no |
| 168 | high | `KXMLBSPREAD-26AUG191840TORTB-TB4` | `KXMLBSPREAD-26AUG191840TORTB` | `KXMLBSPREAD` | 300.15 | 1.00 | 36,771 | no |
| 169 | high | `KXMLBHR-26AUG192040LADCOL-LADMBETTS50-1` | `KXMLBHR-26AUG192040LADCOL` | `KXMLBHR` | 297.99 | 1.00 | 14,429 | no |
| 170 | high | `KXMLBSPREAD-26AUG191805MIAPHI-PHI2` | `KXMLBSPREAD-26AUG191805MIAPHI` | `KXMLBSPREAD` | 287.19 | 1.00 | 164,049 | no |
| 171 | high | `KXWTAMATCH-26AUG19SABBEJ-SAB` | `KXWTAMATCH-26AUG19SABBEJ` | `KXWTAMATCH` | 286.24 | 1.00 | 60,376 | no |
| 172 | high | `KXMLBF5-26AUG192040LADCOL-LAD` | `KXMLBF5-26AUG192040LADCOL` | `KXMLBF5` | 285.15 | 1.00 | 53,161 | no |
| 173 | high | `KXMLSGAME-26AUG19COLLAFC-LAFC` | `KXMLSGAME-26AUG19COLLAFC` | `KXMLSGAME` | 282.91 | 1.00 | 42,509 | no |
| 174 | high | `KXMLBKS-26AUG191940ATHKC-KCSLUGO67-5` | `KXMLBKS-26AUG191940ATHKC` | `KXMLBKS` | 279.78 | 1.00 | 22,588 | no |
| 175 | high | `KXMLBTOTAL-26AUG192040LADCOL-8` | `KXMLBTOTAL-26AUG192040LADCOL` | `KXMLBTOTAL` | 277.76 | 1.00 | 8,193 | no |
| 176 | high | `KXMLBSPREAD-26AUG191840STLCIN-CIN3` | `KXMLBSPREAD-26AUG191840STLCIN` | `KXMLBSPREAD` | 277.72 | 1.00 | 9,334 | no |
| 177 | high | `KXMLBTOTAL-26AUG191840STLCIN-7` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 276.27 | 1.00 | 45,829 | no |
| 178 | high | `KXMLBSPREAD-26AUG191805MIAPHI-MIA2` | `KXMLBSPREAD-26AUG191805MIAPHI` | `KXMLBSPREAD` | 271.87 | 1.00 | 34,636 | no |
| 179 | high | `KXMLBF5TOTAL-26AUG191940ATHKC-6` | `KXMLBF5TOTAL-26AUG191940ATHKC` | `KXMLBF5TOTAL` | 270.20 | 1.00 | 7,441 | no |
| 180 | high | `KXPRESNOMD-28-WM` | `KXPRESNOMD-28` | `KXPRESNOMD` | 269.76 | 0.67 | 6,667 | no |
| 181 | high | `KXDPWORLDTOUR-NEC26-MSOU` | `KXDPWORLDTOUR-NEC26` | `KXDPWORLDTOUR` | 266.14 | 0.33 | 69,902 | no |
| 182 | high | `KXBTCD-26AUG2117-T71499.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 262.74 | 1.00 | 21,005 | no |
| 183 | high | `KXMLSGAME-26AUG19VANHOU-HOU` | `KXMLSGAME-26AUG19VANHOU` | `KXMLSGAME` | 259.29 | 1.00 | 72,213 | no |
| 184 | high | `KXWNBATOTAL-26AUG19TORWSH-176` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 259.05 | 1.00 | 47,874 | no |
| 185 | high | `KXBTCD-26AUG2117-T69999.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 258.80 | 1.00 | 61,461 | no |
| 186 | high | `KXMLBTOTAL-26AUG191940ATHKC-9` | `KXMLBTOTAL-26AUG191940ATHKC` | `KXMLBTOTAL` | 254.24 | 1.00 | 114,384 | no |
| 187 | high | `KXMLBF5-26AUG192010LAAHOU-LAA` | `KXMLBF5-26AUG192010LAAHOU` | `KXMLBF5` | 253.00 | 1.00 | 14,158 | no |
| 188 | high | `KXNWSLGAME-26AUG19RACREI-REI` | `KXNWSLGAME-26AUG19RACREI` | `KXNWSLGAME` | 250.64 | 0.67 | 53,215 | no |
| 189 | high | `KXMLSSPREAD-26AUG19PHIMIA-MIA2` | `KXMLSSPREAD-26AUG19PHIMIA` | `KXMLSSPREAD` | 248.54 | 1.00 | 53,959 | no |
| 190 | high | `KXWNBAGAME-26AUG19MINGS-GS` | `KXWNBAGAME-26AUG19MINGS` | `KXWNBAGAME` | 247.92 | 1.00 | 141,627 | no |
| 191 | high | `KXSB-27-NE` | `KXSB-27` | `KXSB` | 246.36 | 0.33 | 18,378 | no |
| 192 | high | `KXPGATOUR-BMC26-JPOS` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 245.41 | 0.33 | 73,268 | no |
| 193 | high | `KXPGATOUR-BMC26-RHIS` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 245.41 | 0.33 | 82,119 | no |
| 194 | high | `KXMLSTOTAL-26AUG19DCUNE-3` | `KXMLSTOTAL-26AUG19DCUNE` | `KXMLSTOTAL` | 243.06 | 1.00 | 17,376 | no |
| 195 | high | `KXPGATOUR-BMC26-WKIM` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 242.08 | 0.67 | 139,548 | no |
| 196 | high | `KXMLBTOTAL-26AUG191835NYYBAL-11` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 241.47 | 1.00 | 78,798 | no |
| 197 | high | `KXMLBSPREAD-26AUG191840TORTB-TB3` | `KXMLBSPREAD-26AUG191840TORTB` | `KXMLBSPREAD` | 240.87 | 1.00 | 33,276 | no |
| 198 | high | `KXPGATOUR-BMC26-HMAT` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 237.88 | 0.67 | 239,240 | no |
| 199 | high | `KXMLSGAME-26AUG19CINNYC-TIE` | `KXMLSGAME-26AUG19CINNYC` | `KXMLSGAME` | 235.15 | 1.00 | 28,751 | no |
| 200 | high | `KXWNBATOTAL-26AUG19TORWSH-170` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 233.88 | 1.00 | 113,934 | no |
| 201 | high | `KXBTCD-26AUG2017-T68749.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 232.34 | 1.00 | 10,635 | no |
| 202 | high | `KXATPMATCH-26AUG19FARMUS-FAR` | `KXATPMATCH-26AUG19FARMUS` | `KXATPMATCH` | 232.17 | 1.00 | 137,674 | no |
| 203 | high | `KXMLBF5-26AUG192010LAAHOU-HOU` | `KXMLBF5-26AUG192010LAAHOU` | `KXMLBF5` | 227.92 | 1.00 | 21,277 | no |
| 204 | high | `KXMLSGAME-26AUG19ORLCHI-TIE` | `KXMLSGAME-26AUG19ORLCHI` | `KXMLSGAME` | 227.89 | 1.00 | 45,790 | no |
| 205 | high | `KXMLBKS-26AUG192040LADCOL-LADRSASAKI11-6` | `KXMLBKS-26AUG192040LADCOL` | `KXMLBKS` | 226.56 | 1.00 | 15,300 | no |
| 206 | high | `KXMLBF5-26AUG191940ATHKC-ATH` | `KXMLBF5-26AUG191940ATHKC` | `KXMLBF5` | 225.33 | 1.00 | 8,841 | no |
| 207 | high | `KXMLS1H-26AUG19CLBMTL-MTL` | `KXMLS1H-26AUG19CLBMTL` | `KXMLS1H` | 224.09 | 1.00 | 7,713 | no |
| 208 | high | `KXBRASILEIROBGAME-26AUG19CUIFER-FER` | `KXBRASILEIROBGAME-26AUG19CUIFER` | `KXBRASILEIROBGAME` | 222.56 | 1.00 | 6,664 | no |
| 209 | high | `KXBTCMAXMON-BTC-26AUG31-7750000` | `KXBTCMAXMON-BTC-26AUG31` | `KXBTCMAXMON` | 218.73 | 1.00 | 26,452 | no |
| 210 | high | `KXWNBASPREAD-26AUG19TORWSH-WSH7` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 218.22 | 1.00 | 17,780 | no |
| 211 | high | `KXNWSLGAME-26AUG19HDACHI-CHI` | `KXNWSLGAME-26AUG19HDACHI` | `KXNWSLGAME` | 217.67 | 1.00 | 9,717 | no |
| 212 | high | `KXMLBTOTAL-26AUG191835NYYBAL-13` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 215.51 | 1.00 | 9,169 | no |
| 213 | high | `KXPGATOUR-BMC26-MFIT` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 213.46 | 0.33 | 84,665 | no |
| 214 | high | `KXMLSTOTAL-26AUG19PHIMIA-7` | `KXMLSTOTAL-26AUG19PHIMIA` | `KXMLSTOTAL` | 211.85 | 1.00 | 12,350 | no |
| 215 | high | `KXPGATOUR-BMC26-ABHA` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 204.51 | 0.67 | 115,243 | no |
| 216 | high | `KXMLS1H-26AUG19NYRBNSH-NSH` | `KXMLS1H-26AUG19NYRBNSH` | `KXMLS1H` | 202.44 | 1.00 | 7,396 | no |
| 217 | medium | `KXMLBTOTAL-26AUG191840SFCLE-4` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 202.24 | 1.00 | 32,154 | **YES** |
| 218 | medium | `KXATPMATCH-26AUG19FARMUS-MUS` | `KXATPMATCH-26AUG19FARMUS` | `KXATPMATCH` | 197.05 | 1.00 | 159,413 | **YES** |
| 219 | medium | `KXMLS1H-26AUG19TORCLT-TIE` | `KXMLS1H-26AUG19TORCLT` | `KXMLS1H` | 197.02 | 1.00 | 10,904 | **YES** |
| 220 | medium | `KXMLSGAME-26AUG19NYRBNSH-TIE` | `KXMLSGAME-26AUG19NYRBNSH` | `KXMLSGAME` | 196.82 | 1.00 | 31,023 | **YES** |
| 221 | medium | `KXCONMEBOLLIBADVANCE-26AUG19FLACRU-FLA` | `KXCONMEBOLLIBADVANCE-26AUG19FLACRU` | `KXCONMEBOLLIBADVANCE` | 195.36 | 1.00 | 16,372 | no |
| 222 | medium | `KXBRASILEIROBGAME-26AUG19AVAREC-REC` | `KXBRASILEIROBGAME-26AUG19AVAREC` | `KXBRASILEIROBGAME` | 193.71 | 1.00 | 88,114 | no |
| 223 | medium | `KXSB-27-SEA` | `KXSB-27` | `KXSB` | 187.11 | 0.33 | 60,218 | no |
| 224 | medium | `KXMLBF5SPREAD-26AUG192040LADCOL-LAD2` | `KXMLBF5SPREAD-26AUG192040LADCOL` | `KXMLBF5SPREAD` | 184.87 | 1.00 | 13,305 | no |
| 225 | medium | `KXPGATOUR-BMC26-WCLA` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 181.95 | 0.67 | 535,233 | no |
| 226 | medium | `KXMLS1H-26AUG19CINNYC-CIN` | `KXMLS1H-26AUG19CINNYC` | `KXMLS1H` | 177.89 | 0.67 | 6,746 | no |
| 227 | medium | `KXWNBASPREAD-26AUG19TORWSH-WSH13` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 176.50 | 1.00 | 247,630 | no |
| 228 | medium | `KXMLBTB-26AUG192040LADCOL-LADSOHTANI17-3` | `KXMLBTB-26AUG192040LADCOL` | `KXMLBTB` | 176.34 | 1.00 | 9,640 | no |
| 229 | medium | `KXPGATOUR-BMC26-BCAU` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 175.30 | 0.33 | 32,308 | no |
| 230 | medium | `KXPGAR1LEAD-BMC26-JROS` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 172.86 | 0.33 | 18,508 | no |
| 231 | medium | `KXMLBF5-26AUG192005WSHTEX-WSH` | `KXMLBF5-26AUG192005WSHTEX` | `KXMLBF5` | 172.24 | 1.00 | 95,732 | no |
| 232 | medium | `KXPGATOUR-BMC26-GWOO` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 171.97 | 0.33 | 223,513 | no |
| 233 | medium | `KXMLBSPREAD-26AUG191840SFCLE-SF2` | `KXMLBSPREAD-26AUG191840SFCLE` | `KXMLBSPREAD` | 170.02 | 1.00 | 38,843 | no |
| 234 | medium | `KXMLS1H-26AUG19ORLCHI-CHI` | `KXMLS1H-26AUG19ORLCHI` | `KXMLS1H` | 169.72 | 1.00 | 7,007 | no |
| 235 | medium | `KXCONMEBOLLIBTOTAL-26AUG19FLACRU-3` | `KXCONMEBOLLIBTOTAL-26AUG19FLACRU` | `KXCONMEBOLLIBTOTAL` | 169.69 | 1.00 | 27,063 | no |
| 236 | medium | `KXMLSGAME-26AUG19TORCLT-TOR` | `KXMLSGAME-26AUG19TORCLT` | `KXMLSGAME` | 166.62 | 1.00 | 90,307 | no |
| 237 | medium | `KXPGAR1LEAD-BMC26-SSCH` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 166.51 | 1.00 | 109,462 | no |
| 238 | medium | `KXMLS1HSPREAD-26AUG19PHIMIA-MIA2` | `KXMLS1HSPREAD-26AUG19PHIMIA` | `KXMLS1HSPREAD` | 166.37 | 0.67 | 9,919 | no |
| 239 | medium | `KXMLBTOTAL-26AUG191840TORTB-15` | `KXMLBTOTAL-26AUG191840TORTB` | `KXMLBTOTAL` | 165.72 | 1.00 | 18,159 | no |
| 240 | medium | `KXATPEXACTMATCH-26AUG19FRIOCO-FRI20` | `KXATPEXACTMATCH-26AUG19FRIOCO` | `KXATPEXACTMATCH` | 164.68 | 1.00 | 6,915 | no |
| 241 | medium | `KXPGATOUR-BMC26-JROS` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 164.22 | 1.00 | 102,189 | no |
| 242 | medium | `KXMLBSPREAD-26AUG191840STLCIN-STL3` | `KXMLBSPREAD-26AUG191840STLCIN` | `KXMLBSPREAD` | 162.87 | 1.00 | 34,871 | no |
| 243 | medium | `KXMLSTOTAL-26AUG19CINNYC-3` | `KXMLSTOTAL-26AUG19CINNYC` | `KXMLSTOTAL` | 161.67 | 1.00 | 13,737 | no |
| 244 | medium | `KXMLS1HTOTAL-26AUG19CINNYC-3` | `KXMLS1HTOTAL-26AUG19CINNYC` | `KXMLS1HTOTAL` | 161.60 | 1.00 | 13,289 | no |
| 245 | medium | `KXMLSGAME-26AUG19TORCLT-CLT` | `KXMLSGAME-26AUG19TORCLT` | `KXMLSGAME` | 156.36 | 1.00 | 170,592 | no |
| 246 | medium | `KXMLSTOTAL-26AUG19NYRBNSH-3` | `KXMLSTOTAL-26AUG19NYRBNSH` | `KXMLSTOTAL` | 156.20 | 1.00 | 33,069 | no |
| 247 | medium | `KXATPGSPREAD-26AUG19FRIOCO-FRI5` | `KXATPGSPREAD-26AUG19FRIOCO` | `KXATPGSPREAD` | 155.90 | 1.00 | 75,202 | no |
| 248 | medium | `KXMLBSPREAD-26AUG192005WSHTEX-WSH2` | `KXMLBSPREAD-26AUG192005WSHTEX` | `KXMLBSPREAD` | 154.69 | 1.00 | 11,788 | no |
| 249 | medium | `KXPGATOUR-BMC26-JBRI` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 153.45 | 0.33 | 31,249 | no |
| 250 | medium | `KXMLBTOTAL-26AUG191940ATHKC-12` | `KXMLBTOTAL-26AUG191940ATHKC` | `KXMLBTOTAL` | 153.34 | 1.00 | 8,618 | no |
| 251 | medium | `KXMLBF5-26AUG192005WSHTEX-TEX` | `KXMLBF5-26AUG192005WSHTEX` | `KXMLBF5` | 152.93 | 1.00 | 14,931 | no |
| 252 | medium | `KXWNBASPREAD-26AUG19TORWSH-WSH16` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 150.18 | 1.00 | 194,877 | no |
| 253 | medium | `KXMLSGAME-26AUG19TORCLT-TIE` | `KXMLSGAME-26AUG19TORCLT` | `KXMLSGAME` | 146.39 | 1.00 | 59,594 | no |
| 254 | medium | `KXMLSGAME-26AUG19PORSD-POR` | `KXMLSGAME-26AUG19PORSD` | `KXMLSGAME` | 145.35 | 1.00 | 10,111 | no |
| 255 | medium | `KXMLSGAME-26AUG19SEAATX-ATX` | `KXMLSGAME-26AUG19SEAATX` | `KXMLSGAME` | 144.20 | 1.00 | 31,233 | no |
| 256 | medium | `KXBTCY-27JAN0100-B57500` | `KXBTCY-27JAN0100` | `KXBTCY` | 143.52 | 1.00 | 15,997 | no |
| 257 | medium | `KXMLBTOTAL-26AUG192040LADCOL-11` | `KXMLBTOTAL-26AUG192040LADCOL` | `KXMLBTOTAL` | 143.44 | 1.00 | 65,263 | no |
| 258 | medium | `KXWNBASPREAD-26AUG19TORWSH-WSH4` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 142.94 | 0.67 | 33,168 | no |
| 259 | medium | `KXMLBTOTAL-26AUG191840SFCLE-6` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 141.28 | 1.00 | 31,865 | no |
| 260 | medium | `KXMLBTOTAL-26AUG191940SEAMIL-11` | `KXMLBTOTAL-26AUG191940SEAMIL` | `KXMLBTOTAL` | 140.28 | 1.00 | 88,354 | no |
| 261 | medium | `KXMLBSPREAD-26AUG192005WSHTEX-WSH3` | `KXMLBSPREAD-26AUG192005WSHTEX` | `KXMLBSPREAD` | 139.65 | 1.00 | 7,124 | no |
| 262 | medium | `KXMLSTOTAL-26AUG19TORCLT-4` | `KXMLSTOTAL-26AUG19TORCLT` | `KXMLSTOTAL` | 139.37 | 0.67 | 34,329 | no |
| 263 | medium | `KXMLBSPREAD-26AUG191840SFCLE-CLE2` | `KXMLBSPREAD-26AUG191840SFCLE` | `KXMLBSPREAD` | 138.25 | 1.00 | 360,338 | no |
| 264 | medium | `KXPGAR1LEAD-BMC26-WCLA` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 131.37 | 0.33 | 40,982 | no |
| 265 | medium | `KXPGAR1LEAD-BMC26-JTHO` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 130.97 | 0.33 | 7,176 | no |
| 266 | medium | `KXIMPEACH-27-JAN01` | `KXIMPEACH` | `KXIMPEACH` | 129.40 | 1.00 | 8,557 | no |
| 267 | medium | `KXWNBASPREAD-26AUG19MINGS-MIN2` | `KXWNBASPREAD-26AUG19MINGS` | `KXWNBASPREAD` | 126.33 | 1.00 | 63,724 | no |
| 268 | medium | `KXMLBF5TOTAL-26AUG191940ATHKC-5` | `KXMLBF5TOTAL-26AUG191940ATHKC` | `KXMLBF5TOTAL` | 125.64 | 1.00 | 16,341 | no |
| 269 | medium | `KXMLSSPREAD-26AUG19DCUNE-NE2` | `KXMLSSPREAD-26AUG19DCUNE` | `KXMLSSPREAD` | 122.74 | 1.00 | 13,218 | no |
| 270 | medium | `KXNBA-27-PHI` | `KXNBA-27` | `KXNBA` | 116.91 | 0.67 | 26,839 | no |
| 271 | medium | `KXMLSTOTAL-26AUG19DCUNE-5` | `KXMLSTOTAL-26AUG19DCUNE` | `KXMLSTOTAL` | 114.78 | 1.00 | 19,627 | no |
| 272 | medium | `KXMLS1H-26AUG19PHIMIA-TIE` | `KXMLS1H-26AUG19PHIMIA` | `KXMLS1H` | 114.04 | 1.00 | 28,732 | no |
| 273 | medium | `KXTESTMATCH-26AUG190600PAKENG-ENG` | `KXTESTMATCH-26AUG190600PAKENG` | `KXTESTMATCH` | 110.88 | 1.00 | 282,401 | no |
| 274 | medium | `KXMLSGAME-26AUG19CLBMTL-TIE` | `KXMLSGAME-26AUG19CLBMTL` | `KXMLSGAME` | 110.68 | 1.00 | 23,656 | no |
| 275 | medium | `KXMLBTOTAL-26AUG191840TORTB-14` | `KXMLBTOTAL-26AUG191840TORTB` | `KXMLBTOTAL` | 110.49 | 1.00 | 7,564 | no |
| 276 | medium | `KXBRASILEIROBTOTAL-26AUG19FORSBE-4` | `KXBRASILEIROBTOTAL-26AUG19FORSBE` | `KXBRASILEIROBTOTAL` | 110.20 | 0.67 | 28,460 | no |
| 277 | medium | `KXMLBTOTAL-26AUG192005WSHTEX-6` | `KXMLBTOTAL-26AUG192005WSHTEX` | `KXMLBTOTAL` | 110.09 | 1.00 | 7,077 | no |
| 278 | medium | `KXPGAR1LEAD-BMC26-STHE` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 109.46 | 0.33 | 14,767 | no |
| 279 | medium | `KXPGAR1LEAD-BMC26-MMCC` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 109.06 | 0.33 | 28,009 | no |
| 280 | medium | `KXITFMATCH-26AUG18TAMJAS-JAS` | `KXITFMATCH-26AUG18TAMJAS` | `KXITFMATCH` | 105.89 | 0.67 | 134,611 | no |
| 281 | medium | `KXBTCD-26AUG2117-T64999.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 105.10 | 1.00 | 66,258 | no |
| 282 | medium | `KXMLBTEAMTOTAL-26AUG192040LADCOL-LAD6` | `KXMLBTEAMTOTAL-26AUG192040LADCOL` | `KXMLBTEAMTOTAL` | 104.82 | 0.67 | 7,155 | no |
| 283 | medium | `KXNASCARRACE-DOLT26-CHBE` | `KXNASCARRACE-DOLT26` | `KXNASCARRACE` | 104.15 | 0.67 | 11,161 | no |
| 284 | medium | `KXMLBF5TOTAL-26AUG192040LADCOL-7` | `KXMLBF5TOTAL-26AUG192040LADCOL` | `KXMLBF5TOTAL` | 101.44 | 1.00 | 7,060 | no |
| 285 | medium | `SENATEFLS-26-D` | `SENATEFLS-26` | `SENATEFLS` | 100.28 | 0.67 | 26,690 | no |
| 286 | medium | `KXMLSTOTAL-26AUG19NYRBNSH-1` | `KXMLSTOTAL-26AUG19NYRBNSH` | `KXMLSTOTAL` | 100.10 | 1.00 | 29,525 | no |
| 287 | medium | `KXMLSSPREAD-26AUG19SKCSTL-STL2` | `KXMLSSPREAD-26AUG19SKCSTL` | `KXMLSSPREAD` | 98.92 | 1.00 | 10,832 | no |
| 288 | medium | `KXWNBASPREAD-26AUG19TORWSH-WSH10` | `KXWNBASPREAD-26AUG19TORWSH` | `KXWNBASPREAD` | 97.18 | 1.00 | 81,095 | no |
| 289 | medium | `KXSCRSENS-26-RNOR` | `KXSCRSENS-26` | `KXSCRSENS` | 93.82 | 0.67 | 597,807 | no |
| 290 | medium | `KXMLBSPREAD-26AUG192005WSHTEX-TEX2` | `KXMLBSPREAD-26AUG192005WSHTEX` | `KXMLBSPREAD` | 93.59 | 1.00 | 34,513 | no |
| 291 | medium | `KXWNBA-26-IND` | `KXWNBA-26` | `KXWNBA` | 90.12 | 0.33 | 23,086 | no |
| 292 | medium | `KXWNBASPREAD-26AUG19MINGS-MIN4` | `KXWNBASPREAD-26AUG19MINGS` | `KXWNBASPREAD` | 90.11 | 1.00 | 14,414 | no |
| 293 | medium | `KXPGAR1LEAD-BMC26-TFLE` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 88.28 | 0.67 | 9,704 | no |
| 294 | medium | `KXCANPLGAME-26AUG19FORSUP-FOR` | `KXCANPLGAME-26AUG19FORSUP` | `KXCANPLGAME` | 87.14 | 1.00 | 18,111 | no |
| 295 | medium | `KXPGAR1LEAD-BMC26-VHOV` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 85.65 | 0.33 | 17,152 | no |
| 296 | medium | `KXBTCMAXMON-BTC-26AUG31-7500000` | `KXBTCMAXMON-BTC-26AUG31` | `KXBTCMAXMON` | 85.39 | 1.00 | 149,888 | no |
| 297 | medium | `KXMLSTOTAL-26AUG19CINNYC-5` | `KXMLSTOTAL-26AUG19CINNYC` | `KXMLSTOTAL` | 84.56 | 1.00 | 11,159 | no |
| 298 | medium | `KXMLSTOTAL-26AUG19TORCLT-5` | `KXMLSTOTAL-26AUG19TORCLT` | `KXMLSTOTAL` | 83.78 | 1.00 | 28,238 | no |
| 299 | medium | `KXMLBSPREAD-26AUG191835NYYBAL-NYY4` | `KXMLBSPREAD-26AUG191835NYYBAL` | `KXMLBSPREAD` | 83.66 | 1.00 | 6,799 | no |
| 300 | medium | `KXMLBTOTAL-26AUG191805MIAPHI-11` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 83.13 | 1.00 | 16,022 | no |
| 301 | medium | `KXMLS1HTOTAL-26AUG19ORLCHI-2` | `KXMLS1HTOTAL-26AUG19ORLCHI` | `KXMLS1HTOTAL` | 82.58 | 1.00 | 17,934 | no |
| 302 | medium | `KXPGAR1LEAD-BMC26-HMAT` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 81.19 | 0.67 | 18,351 | no |
| 303 | medium | `KXMLBTOTAL-26AUG191940ATHKC-11` | `KXMLBTOTAL-26AUG191940ATHKC` | `KXMLBTOTAL` | 80.10 | 1.00 | 22,395 | no |
| 304 | medium | `KXBTCMAXMON-BTC-26AUG31-7250000` | `KXBTCMAXMON-BTC-26AUG31` | `KXBTCMAXMON` | 78.77 | 1.00 | 140,550 | no |
| 305 | medium | `KXMLBTOTAL-26AUG191835NYYBAL-7` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 76.34 | 1.00 | 6,982 | no |
| 306 | medium | `KXMLSGAME-26AUG19COLLAFC-COL` | `KXMLSGAME-26AUG19COLLAFC` | `KXMLSGAME` | 74.87 | 1.00 | 10,433 | no |
| 307 | medium | `KXMLBKS-26AUG192040LADCOL-LADRSASAKI11-5` | `KXMLBKS-26AUG192040LADCOL` | `KXMLBKS` | 73.17 | 0.67 | 14,128 | no |
| 308 | medium | `KXPRESNOMD-28-GN` | `KXPRESNOMD-28` | `KXPRESNOMD` | 72.98 | 0.33 | 21,596 | no |
| 309 | medium | `KXMLSGAME-26AUG19RSLDAL-DAL` | `KXMLSGAME-26AUG19RSLDAL` | `KXMLSGAME` | 72.34 | 1.00 | 14,502 | no |
| 310 | medium | `KXMLSSPREAD-26AUG19DCUNE-DCU2` | `KXMLSSPREAD-26AUG19DCUNE` | `KXMLSSPREAD` | 70.02 | 0.33 | 7,259 | no |
| 311 | medium | `KXMLBKS-26AUG192040LADCOL-COLKFREELAND21-4` | `KXMLBKS-26AUG192040LADCOL` | `KXMLBKS` | 69.54 | 0.67 | 13,195 | no |
| 312 | medium | `KXPGATOP10-BMC26-SBUR` | `KXPGATOP10-BMC26` | `KXPGATOP10` | 69.08 | 0.67 | 9,922 | no |
| 313 | medium | `KXPGAR1LEAD-BMC26-RFOW` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 68.29 | 0.33 | 58,983 | no |
| 314 | medium | `KXWNBATOTAL-26AUG19TORWSH-161` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 67.96 | 1.00 | 13,483 | no |
| 315 | medium | `SENATEMI-26-D` | `SENATEMI-26` | `SENATEMI` | 67.25 | 0.67 | 49,742 | no |
| 316 | medium | `KXMLBTOTAL-26AUG191940ATHKC-10` | `KXMLBTOTAL-26AUG191940ATHKC` | `KXMLBTOTAL` | 66.71 | 1.00 | 26,884 | no |
| 317 | medium | `KXPGATOUR-BMC26-RMAC` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 66.34 | 0.33 | 104,013 | no |
| 318 | medium | `KXPGATOP10-BMC26-WCLA` | `KXPGATOP10-BMC26` | `KXPGATOP10` | 65.69 | 0.33 | 18,635 | no |
| 319 | medium | `KXMLBHR-26AUG192040LADCOL-LADFFREEMAN5-1` | `KXMLBHR-26AUG192040LADCOL` | `KXMLBHR` | 64.56 | 1.00 | 14,386 | no |
| 320 | medium | `KXWTAMATCH-26AUG19SABBEJ-BEJ` | `KXWTAMATCH-26AUG19SABBEJ` | `KXWTAMATCH` | 63.65 | 1.00 | 28,822 | no |
| 321 | medium | `KXMLBSPREAD-26AUG191940SEAMIL-SEA2` | `KXMLBSPREAD-26AUG191940SEAMIL` | `KXMLBSPREAD` | 63.65 | 1.00 | 25,772 | no |
| 322 | medium | `KXMLBHR-26AUG192010LAAHOU-HOUYALVAREZ44-1` | `KXMLBHR-26AUG192010LAAHOU` | `KXMLBHR` | 62.21 | 1.00 | 34,009 | no |
| 323 | medium | `KXMLBTEAMTOTAL-26AUG191840TORTB-TOR7` | `KXMLBTEAMTOTAL-26AUG191840TORTB` | `KXMLBTEAMTOTAL` | 61.49 | 1.00 | 14,337 | no |
| 324 | medium | `KXPGATOUR-BMC26-CMOR` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 57.91 | 0.33 | 98,492 | no |
| 325 | medium | `KXMLSBTTS-26AUG19DCUNE-BTTS` | `KXMLSBTTS-26AUG19DCUNE` | `KXMLSBTTS` | 57.56 | 1.00 | 7,592 | no |
| 326 | medium | `KXBTCMAXY-26DEC31-99999.99` | `KXBTCMAXY-26DEC31` | `KXBTCMAXY` | 55.96 | 0.33 | 24,143 | no |
| 327 | medium | `KXMLBF5-26AUG191940SEAMIL-SEA` | `KXMLBF5-26AUG191940SEAMIL` | `KXMLBF5` | 55.30 | 1.00 | 23,737 | no |
| 328 | medium | `KXMLBTOTAL-26AUG191805MIAPHI-12` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 55.17 | 1.00 | 17,562 | no |
| 329 | medium | `KXPGAR1LEAD-BMC26-MTHO` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 53.66 | 1.00 | 14,115 | no |
| 330 | medium | `KXBTCD-26AUG2117-T69499.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 53.14 | 1.00 | 26,012 | no |
| 331 | medium | `KXWNBATOTAL-26AUG19TORWSH-173` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 52.35 | 1.00 | 41,342 | no |
| 332 | medium | `KXSCRSENS-26-DNOR` | `KXSCRSENS-26` | `KXSCRSENS` | 51.68 | 1.00 | 513,792 | no |
| 333 | medium | `KXMLSGAME-26AUG19PORSD-SD` | `KXMLSGAME-26AUG19PORSD` | `KXMLSGAME` | 51.57 | 1.00 | 21,805 | no |
| 334 | medium | `KXNFLGAME-26AUG21NYJPIT-PIT` | `KXNFLGAME-26AUG21NYJPIT` | `KXNFLGAME` | 51.33 | 1.00 | 26,609 | no |
| 335 | medium | `KXMLBTOTAL-26AUG191840STLCIN-4` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 50.11 | 1.00 | 20,016 | no |
| 336 | medium | `KXMLBTOTAL-26AUG191940ATHKC-14` | `KXMLBTOTAL-26AUG191940ATHKC` | `KXMLBTOTAL` | 50.09 | 1.00 | 13,133 | no |
| 337 | medium | `KXRETIREMM-26` | `KXRETIREMM-26` | `KXRETIREMM` | 49.28 | 0.67 | 53,133 | no |
| 338 | medium | `KXMLBHR-26AUG191940SEAMIL-SEAJRODRGUEZ44-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 47.34 | 1.00 | 8,687 | no |
| 339 | medium | `KXMLBHR-26AUG191840STLCIN-CINSSTEWART27-1` | `KXMLBHR-26AUG191840STLCIN` | `KXMLBHR` | 47.03 | 1.00 | 10,912 | no |
| 340 | medium | `KXMLBTOTAL-26AUG191840STLCIN-10` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 46.82 | 1.00 | 15,375 | no |
| 341 | medium | `KXMLBF5TOTAL-26AUG192010LAAHOU-5` | `KXMLBF5TOTAL-26AUG192010LAAHOU` | `KXMLBF5TOTAL` | 46.77 | 1.00 | 8,020 | no |
| 342 | medium | `KXMLBSPREAD-26AUG191840TORTB-TB5` | `KXMLBSPREAD-26AUG191840TORTB` | `KXMLBSPREAD` | 46.30 | 1.00 | 21,111 | no |
| 343 | medium | `KXMLBTOTAL-26AUG192040LADCOL-13` | `KXMLBTOTAL-26AUG192040LADCOL` | `KXMLBTOTAL` | 43.99 | 1.00 | 13,400 | no |
| 344 | medium | `KXMLBSPREAD-26AUG191840SFCLE-CLE3` | `KXMLBSPREAD-26AUG191840SFCLE` | `KXMLBSPREAD` | 43.98 | 1.00 | 18,268 | no |
| 345 | medium | `KXPGATOUR-BMC26-MMCN` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 43.89 | 0.33 | 42,024 | no |
| 346 | medium | `KXMLSSPREAD-26AUG19DCUNE-DCU3` | `KXMLSSPREAD-26AUG19DCUNE` | `KXMLSSPREAD` | 43.22 | 1.00 | 13,427 | no |
| 347 | medium | `KXMLBF5TOTAL-26AUG192005WSHTEX-5` | `KXMLBF5TOTAL-26AUG192005WSHTEX` | `KXMLBF5TOTAL` | 42.98 | 1.00 | 8,476 | no |
| 348 | medium | `KXCONMEBOLSUDADVANCE-26AUG19TORTIG-TIG` | `KXCONMEBOLSUDADVANCE-26AUG19TORTIG` | `KXCONMEBOLSUDADVANCE` | 41.56 | 1.00 | 15,539 | no |
| 349 | medium | `KXATPGSPREAD-26AUG19BORNAK-NAK5` | `KXATPGSPREAD-26AUG19BORNAK` | `KXATPGSPREAD` | 40.84 | 1.00 | 27,968 | no |
| 350 | medium | `KXMLSSCORE-26AUG19CINNYC-CIN4NYC1` | `KXMLSSCORE-26AUG19CINNYC` | `KXMLSSCORE` | 39.41 | 0.33 | 11,545 | no |
| 351 | medium | `KXMLSSCORE-26AUG19PHIMIA-PHI3MIA2` | `KXMLSSCORE-26AUG19PHIMIA` | `KXMLSSCORE` | 39.28 | 1.00 | 13,460 | no |
| 352 | medium | `KXMLBHR-26AUG192010LAAHOU-LAAMTROUT27-1` | `KXMLBHR-26AUG192010LAAHOU` | `KXMLBHR` | 39.14 | 1.00 | 25,012 | no |
| 353 | medium | `KXCS2GAME-26AUG201000VITFAZE-FAZE` | `KXCS2GAME-26AUG201000VITFAZE` | `KXCS2GAME` | 39.07 | 0.67 | 9,334 | no |
| 354 | medium | `KXBSNGAME-26AUG192000AGSVAQ-AGS` | `KXBSNGAME-26AUG192000AGSVAQ` | `KXBSNGAME` | 38.70 | 0.67 | 8,380 | no |
| 355 | medium | `KXPGATOUR-BMC26-SSCH` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 38.69 | 1.00 | 438,798 | no |
| 356 | medium | `KXMLBF5TOTAL-26AUG192040LADCOL-6` | `KXMLBF5TOTAL-26AUG192040LADCOL` | `KXMLBF5TOTAL` | 38.69 | 1.00 | 52,802 | no |
| 357 | medium | `KXBIGBROTHERELIMINATION-26AUG20-ANG` | `KXBIGBROTHERELIMINATION-26AUG20` | `KXBIGBROTHERELIMINATION` | 38.66 | 0.67 | 26,991 | no |
| 358 | medium | `KXPGAR1LEAD-BMC26-SBUR` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 38.52 | 0.67 | 38,969 | no |
| 359 | medium | `KXMLSTOTAL-26AUG19CINNYC-4` | `KXMLSTOTAL-26AUG19CINNYC` | `KXMLSTOTAL` | 37.69 | 1.00 | 106,377 | no |
| 360 | medium | `KXMLSTOTAL-26AUG19ORLCHI-2` | `KXMLSTOTAL-26AUG19ORLCHI` | `KXMLSTOTAL` | 37.37 | 0.67 | 10,819 | no |
| 361 | medium | `KXNFLGAME-26AUG21GBDEN-GB` | `KXNFLGAME-26AUG21GBDEN` | `KXNFLGAME` | 37.27 | 1.00 | 7,433 | no |
| 362 | medium | `KXCS2GAME-26AUG200700LGCNAVI-NAVI` | `KXCS2GAME-26AUG200700LGCNAVI` | `KXCS2GAME` | 36.76 | 0.67 | 10,061 | no |
| 363 | medium | `KXPGAR1LEAD-BMC26-GWOO` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 36.46 | 0.67 | 76,334 | no |
| 364 | medium | `KXMLBF5-26AUG192005WSHTEX-TIE` | `KXMLBF5-26AUG192005WSHTEX` | `KXMLBF5` | 36.32 | 1.00 | 16,985 | no |
| 365 | medium | `KXMLBSPREAD-26AUG191940ATHKC-KC3` | `KXMLBSPREAD-26AUG191940ATHKC` | `KXMLBSPREAD` | 36.25 | 1.00 | 9,624 | no |
| 366 | medium | `KXPRESNOMD-28-AOC` | `KXPRESNOMD-28` | `KXPRESNOMD` | 36.25 | 0.67 | 25,880 | no |
| 367 | medium | `KXPGATOUR-BMC26-SBUR` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 36.00 | 0.67 | 231,593 | no |
| 368 | medium | `KXMLBTOTAL-26AUG191840STLCIN-11` | `KXMLBTOTAL-26AUG191840STLCIN` | `KXMLBTOTAL` | 35.36 | 1.00 | 7,363 | no |
| 369 | medium | `KXMLSTOTAL-26AUG19CLBMTL-6` | `KXMLSTOTAL-26AUG19CLBMTL` | `KXMLSTOTAL` | 35.32 | 1.00 | 6,831 | no |
| 370 | medium | `KXMLBF5-26AUG191940SEAMIL-MIL` | `KXMLBF5-26AUG191940SEAMIL` | `KXMLBF5` | 34.62 | 1.00 | 14,503 | no |
| 371 | medium | `KXMLSSPREAD-26AUG19TORCLT-TOR3` | `KXMLSSPREAD-26AUG19TORCLT` | `KXMLSSPREAD` | 34.42 | 0.67 | 10,446 | no |
| 372 | medium | `KXMLBHR-26AUG191940ATHKC-KCVPASQUANTINO9-1` | `KXMLBHR-26AUG191940ATHKC` | `KXMLBHR` | 34.14 | 1.00 | 25,937 | no |
| 373 | medium | `CONTROLH-2026-R` | `CONTROLH-2026` | `CONTROLH` | 33.07 | 0.33 | 293,529 | no |
| 374 | medium | `KXMLSSPREAD-26AUG19PHIMIA-PHI3` | `KXMLSSPREAD-26AUG19PHIMIA` | `KXMLSSPREAD` | 33.05 | 1.00 | 22,181 | no |
| 375 | medium | `KXMLBOUTS-26AUG192040LADCOL-LADRSASAKI11-17` | `KXMLBOUTS-26AUG192040LADCOL` | `KXMLBOUTS` | 33.02 | 0.67 | 7,280 | no |
| 376 | medium | `KXMLSSPREAD-26AUG19MINATL-ATL2` | `KXMLSSPREAD-26AUG19MINATL` | `KXMLSSPREAD` | 32.46 | 1.00 | 7,274 | no |
| 377 | medium | `KXMLBSPREAD-26AUG192005WSHTEX-WSH4` | `KXMLBSPREAD-26AUG192005WSHTEX` | `KXMLBSPREAD` | 31.79 | 1.00 | 9,949 | no |
| 378 | medium | `KXMLBSPREAD-26AUG191840TORTB-TOR4` | `KXMLBSPREAD-26AUG191840TORTB` | `KXMLBSPREAD` | 30.77 | 1.00 | 12,314 | no |
| 379 | medium | `KXMLSBTTS-26AUG19NYRBNSH-BTTS` | `KXMLSBTTS-26AUG19NYRBNSH` | `KXMLSBTTS` | 30.62 | 1.00 | 11,422 | no |
| 380 | medium | `KXWNBATOTAL-26AUG19TORWSH-158` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 30.40 | 1.00 | 16,863 | no |
| 381 | medium | `KXTESTMATCH-26AUG190600PAKENG-TIE` | `KXTESTMATCH-26AUG190600PAKENG` | `KXTESTMATCH` | 30.35 | 1.00 | 145,411 | no |
| 382 | medium | `KXMLBTEAMTOTAL-26AUG191940SEAMIL-MIL4` | `KXMLBTEAMTOTAL-26AUG191940SEAMIL` | `KXMLBTEAMTOTAL` | 30.26 | 1.00 | 17,920 | no |
| 383 | medium | `KXPGATOUR-BMC26-ANOR` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 30.10 | 0.33 | 150,882 | no |
| 384 | medium | `KXPRESNOMD-28-ZMAM` | `KXPRESNOMD-28` | `KXPRESNOMD` | 30.08 | 1.00 | 50,362 | no |
| 385 | medium | `KXMLSTOTAL-26AUG19ORLCHI-4` | `KXMLSTOTAL-26AUG19ORLCHI` | `KXMLSTOTAL` | 29.88 | 1.00 | 36,441 | no |
| 386 | medium | `KXMLSSPREAD-26AUG19ORLCHI-CHI2` | `KXMLSSPREAD-26AUG19ORLCHI` | `KXMLSSPREAD` | 29.47 | 0.33 | 15,249 | no |
| 387 | medium | `KXPGAHOLEINONE-BMC26-2` | `KXPGAHOLEINONE-BMC26` | `KXPGAHOLEINONE` | 29.35 | 0.67 | 8,691 | no |
| 388 | medium | `KXMLBTOTAL-26AUG191805MIAPHI-13` | `KXMLBTOTAL-26AUG191805MIAPHI` | `KXMLBTOTAL` | 28.64 | 1.00 | 28,844 | no |
| 389 | medium | `KXWNBAGAME-26AUG20INDDAL-IND` | `KXWNBAGAME-26AUG20INDDAL` | `KXWNBAGAME` | 28.55 | 0.67 | 71,387 | no |
| 390 | medium | `KXMLBSPREAD-26AUG191940SEAMIL-SEA4` | `KXMLBSPREAD-26AUG191940SEAMIL` | `KXMLBSPREAD` | 28.52 | 1.00 | 54,644 | no |
| 391 | medium | `KXMLBSPREAD-26AUG191840STLCIN-CIN2` | `KXMLBSPREAD-26AUG191840STLCIN` | `KXMLBSPREAD` | 26.67 | 1.00 | 160,397 | no |
| 392 | medium | `KXMLSSPREAD-26AUG19CINNYC-CIN3` | `KXMLSSPREAD-26AUG19CINNYC` | `KXMLSSPREAD` | 26.44 | 1.00 | 16,156 | no |
| 393 | medium | `KXPGAR1LEAD-BMC26-SSTR` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 26.27 | 0.00 | 9,208 | no |
| 394 | medium | `KXRT-INS-55` | `KXRT-INS` | `KXRT` | 25.60 | 0.67 | 16,606 | no |
| 395 | medium | `KXMLBHR-26AUG191940ATHKC-KCBWITT7-1` | `KXMLBHR-26AUG191940ATHKC` | `KXMLBHR` | 25.51 | 1.00 | 11,729 | no |
| 396 | medium | `KXMLBTEAMTOTAL-26AUG191840STLCIN-CIN2` | `KXMLBTEAMTOTAL-26AUG191840STLCIN` | `KXMLBTEAMTOTAL` | 25.35 | 1.00 | 14,565 | no |
| 397 | medium | `KXMLSBTTS-26AUG19ORLCHI-BTTS` | `KXMLSBTTS-26AUG19ORLCHI` | `KXMLSBTTS` | 25.00 | 1.00 | 7,780 | no |
| 398 | medium | `KXPGAR1LEAD-BMC26-CGOT` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 23.63 | 0.33 | 28,813 | no |
| 399 | medium | `KXBRASILEIROBSPREAD-26AUG19VINPON-VIN2` | `KXBRASILEIROBSPREAD-26AUG19VINPON` | `KXBRASILEIROBSPREAD` | 23.43 | 0.67 | 27,279 | no |
| 400 | medium | `KXRT-MUT-60` | `KXRT-MUT` | `KXRT` | 23.38 | 0.67 | 25,815 | no |
| 401 | medium | `KXDOTA2GAME-26AUG200100BETVSN-BET` | `KXDOTA2GAME-26AUG200100BETVSN` | `KXDOTA2GAME` | 23.25 | 0.67 | 10,243 | no |
| 402 | medium | `KXPGATOUR-BMC26-TFLE` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 22.67 | 0.67 | 66,038 | no |
| 403 | medium | `KXBTCD-26AUG2117-T68499.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 22.60 | 1.00 | 32,000 | no |
| 404 | medium | `KXMLBGAME-26AUG201410SEAMIL-MIL` | `KXMLBGAME-26AUG201410SEAMIL` | `KXMLBGAME` | 22.60 | 0.67 | 26,910 | no |
| 405 | medium | `KXLIGAMXGAME-26AUG21JUAAME-AME` | `KXLIGAMXGAME-26AUG21JUAAME` | `KXLIGAMXGAME` | 22.37 | 0.67 | 18,857 | no |
| 406 | medium | `KXMLSGAME-26AUG19RSLDAL-RSL` | `KXMLSGAME-26AUG19RSLDAL` | `KXMLSGAME` | 22.03 | 1.00 | 8,338 | no |
| 407 | medium | `KXMLBTOTAL-26AUG191840SFCLE-7` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 21.76 | 1.00 | 31,699 | no |
| 408 | medium | `KXMLBHIT-26AUG192005WSHTEX-WSHJYOUNG30-1` | `KXMLBHIT-26AUG192005WSHTEX` | `KXMLBHIT` | 21.28 | 0.67 | 9,308 | no |
| 409 | medium | `KXMLBTOTAL-26AUG191835NYYBAL-12` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 21.21 | 1.00 | 47,762 | no |
| 410 | medium | `KXBTCMAXMON-BTC-26AUG31-8250000` | `KXBTCMAXMON-BTC-26AUG31` | `KXBTCMAXMON` | 21.02 | 1.00 | 25,080 | no |
| 411 | medium | `KXMLBHR-26AUG191940SEAMIL-MILCYELICH22-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 20.70 | 1.00 | 20,610 | no |
| 412 | medium | `KXETHD-26AUG2117-T2089.99` | `KXETHD-26AUG2117` | `KXETHD` | 20.63 | 0.33 | 41,149 | no |
| 413 | medium | `KXMLSGAME-26AUG19LAGSJ-SJ` | `KXMLSGAME-26AUG19LAGSJ` | `KXMLSGAME` | 20.57 | 1.00 | 14,971 | no |
| 414 | medium | `KXBTCD-26AUG2117-T67999.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 20.49 | 1.00 | 39,002 | no |
| 415 | medium | `KXMLBTOTAL-26AUG192005WSHTEX-9` | `KXMLBTOTAL-26AUG192005WSHTEX` | `KXMLBTOTAL` | 19.81 | 1.00 | 8,367 | no |
| 416 | medium | `GOVPARTYFL-26-R` | `GOVPARTYFL-26` | `GOVPARTYFL` | 19.74 | 1.00 | 19,359 | no |
| 417 | medium | `KXBTCD-26AUG2017-T67249.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 19.71 | 0.33 | 7,548 | no |
| 418 | medium | `KXETHD-26AUG2017-T2179.99` | `KXETHD-26AUG2017` | `KXETHD` | 19.71 | 0.67 | 14,017 | no |
| 419 | medium | `KXMLSTOTAL-26AUG19NYRBNSH-4` | `KXMLSTOTAL-26AUG19NYRBNSH` | `KXMLSTOTAL` | 19.61 | 1.00 | 27,022 | no |
| 420 | medium | `KXDOTA2GAME-26AUG192200TSIRO-TS` | `KXDOTA2GAME-26AUG192200TSIRO` | `KXDOTA2GAME` | 19.35 | 1.00 | 28,940 | no |
| 421 | medium | `KXMLSTOTAL-26AUG19CLBMTL-3` | `KXMLSTOTAL-26AUG19CLBMTL` | `KXMLSTOTAL` | 19.04 | 1.00 | 10,456 | no |
| 422 | medium | `KXMLSGAME-26AUG19LAGSJ-LAG` | `KXMLSGAME-26AUG19LAGSJ` | `KXMLSGAME` | 18.53 | 1.00 | 76,085 | no |
| 423 | medium | `KXMLBKS-26AUG192005WSHTEX-TEXKROCKER80-5` | `KXMLBKS-26AUG192005WSHTEX` | `KXMLBKS` | 18.26 | 1.00 | 37,240 | no |
| 424 | medium | `KXMLBSPREAD-26AUG191840TORTB-TOR2` | `KXMLBSPREAD-26AUG191840TORTB` | `KXMLBSPREAD` | 17.98 | 1.00 | 10,932 | no |
| 425 | medium | `KXMLSTOTAL-26AUG19CLBMTL-4` | `KXMLSTOTAL-26AUG19CLBMTL` | `KXMLSTOTAL` | 17.84 | 1.00 | 9,540 | no |
| 426 | medium | `KXMLBHR-26AUG191940ATHKC-ATHLBUTLER4-1` | `KXMLBHR-26AUG191940ATHKC` | `KXMLBHR` | 17.62 | 1.00 | 18,930 | no |
| 427 | medium | `KXPGATOUR-BMC26-RMCI` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 17.50 | 0.33 | 104,622 | no |
| 428 | medium | `KXMLSTOTAL-26AUG19TORCLT-6` | `KXMLSTOTAL-26AUG19TORCLT` | `KXMLSTOTAL` | 17.20 | 1.00 | 11,580 | no |
| 429 | medium | `KXMLSTOTAL-26AUG19DCUNE-4` | `KXMLSTOTAL-26AUG19DCUNE` | `KXMLSTOTAL` | 16.82 | 1.00 | 7,353 | no |
| 430 | medium | `KXATPEXACTMATCH-26AUG19BORNAK-NAK21` | `KXATPEXACTMATCH-26AUG19BORNAK` | `KXATPEXACTMATCH` | 16.76 | 1.00 | 10,577 | no |
| 431 | medium | `KXMLSSPREAD-26AUG19CLBMTL-CLB2` | `KXMLSSPREAD-26AUG19CLBMTL` | `KXMLSSPREAD` | 16.70 | 1.00 | 11,280 | no |
| 432 | medium | `KXITFMATCH-26AUG18TAMJAS-TAM` | `KXITFMATCH-26AUG18TAMJAS` | `KXITFMATCH` | 16.35 | 1.00 | 346,011 | no |
| 433 | low | `KXMLBHR-26AUG191940SEAMIL-MILJBAUERS9-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 16.16 | 1.00 | 11,071 | **YES** |
| 434 | low | `KXMLSTOTAL-26AUG19ORLCHI-6` | `KXMLSTOTAL-26AUG19ORLCHI` | `KXMLSTOTAL` | 15.83 | 0.67 | 12,145 | **YES** |
| 435 | low | `KXMLBHR-26AUG191805MIAPHI-MIAGCONINE18-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | 15.80 | 1.00 | 27,944 | **YES** |
| 436 | low | `KXWNBATOTAL-26AUG19TORWSH-164` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | 15.74 | 1.00 | 12,643 | **YES** |
| 437 | low | `KXITFMATCH-26AUG18ICHWUX-ICH` | `KXITFMATCH-26AUG18ICHWUX` | `KXITFMATCH` | 15.37 | 0.67 | 88,777 | no |
| 438 | low | `KXMLBHR-26AUG191940SEAMIL-MILJCHOURIO11-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 15.04 | 0.67 | 10,249 | no |
| 439 | low | `KXMLSSCORE-26AUG19PHIMIA-PHI1MIA2` | `KXMLSSCORE-26AUG19PHIMIA` | `KXMLSSCORE` | 14.91 | 1.00 | 16,430 | no |
| 440 | low | `KXMLBKS-26AUG192005WSHTEX-WSHCCAVALLI24-6` | `KXMLBKS-26AUG192005WSHTEX` | `KXMLBKS` | 14.80 | 1.00 | 36,711 | no |
| 441 | low | `KXMLSTOTAL-26AUG19PHIMIA-8` | `KXMLSTOTAL-26AUG19PHIMIA` | `KXMLSTOTAL` | 14.71 | 0.67 | 7,120 | no |
| 442 | low | `KXPGATOP5-BMC26-CGOT` | `KXPGATOP5-BMC26` | `KXPGATOP5` | 14.60 | 0.33 | 6,761 | no |
| 443 | low | `KXBIGBROTHER-26DEC31-ANG` | `KXBIGBROTHER-26DEC31` | `KXBIGBROTHER` | 14.58 | 0.67 | 6,845 | no |
| 444 | low | `KXMLBHR-26AUG191835NYYBAL-NYYTGRISHAM12-1` | `KXMLBHR-26AUG191835NYYBAL` | `KXMLBHR` | 14.09 | 1.00 | 25,235 | no |
| 445 | low | `KXMLBTEAMTOTAL-26AUG191805MIAPHI-MIA4` | `KXMLBTEAMTOTAL-26AUG191805MIAPHI` | `KXMLBTEAMTOTAL` | 14.02 | 1.00 | 11,938 | no |
| 446 | low | `KXMLBHR-26AUG192005WSHTEX-WSHAORTIZ81-1` | `KXMLBHR-26AUG192005WSHTEX` | `KXMLBHR` | 13.73 | 1.00 | 42,161 | no |
| 447 | low | `KXNFLGAME-26AUG20LVHOU-HOU` | `KXNFLGAME-26AUG20LVHOU` | `KXNFLGAME` | 13.65 | 0.67 | 53,696 | no |
| 448 | low | `KXSB-27-LAR` | `KXSB-27` | `KXSB` | 13.63 | 1.00 | 46,428 | no |
| 449 | low | `KXMLB-26-MIL` | `KXMLB-26` | `KXMLB` | 13.63 | 1.00 | 70,113 | no |
| 450 | low | `KXLALIGAGAME-26AUG23ELCBAR-BAR` | `KXLALIGAGAME-26AUG23ELCBAR` | `KXLALIGAGAME` | 13.61 | 1.00 | 17,105 | no |
| 451 | low | `KXPGATOUR-BMC26-RHEN` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 13.17 | 0.33 | 55,058 | no |
| 452 | low | `KXBTCD-26AUG2017-T68249.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 13.14 | 1.00 | 13,474 | no |
| 453 | low | `KXETHD-26AUG2017-T2219.99` | `KXETHD-26AUG2017` | `KXETHD` | 13.14 | 0.33 | 8,589 | no |
| 454 | low | `KXPGAH2H-BMC26R1RFOWRMAC-RFOW` | `KXPGAH2H-BMC26R1RFOWRMAC` | `KXPGAH2H` | 13.14 | 0.33 | 7,243 | no |
| 455 | low | `KXPRESPERSON-28-DTRU` | `KXPRESPERSON-28` | `KXPRESPERSON` | 12.85 | 0.33 | 16,877 | no |
| 456 | low | `KXATPGSPREAD-26AUG19BORNAK-NAK2` | `KXATPGSPREAD-26AUG19BORNAK` | `KXATPGSPREAD` | 12.82 | 1.00 | 37,011 | no |
| 457 | low | `KXETHD-26AUG2017-T2419.99` | `KXETHD-26AUG2017` | `KXETHD` | 12.74 | 1.00 | 9,333 | no |
| 458 | low | `KXBTCD-26AUG2117-T72999.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 12.61 | 1.00 | 8,020 | no |
| 459 | low | `KXMLBHR-26AUG191940ATHKC-ATHJMCNEIL22-1` | `KXMLBHR-26AUG191940ATHKC` | `KXMLBHR` | 12.61 | 1.00 | 19,057 | no |
| 460 | low | `KXLIGAMXGAME-26AUG22CDGTIJ-TIJ` | `KXLIGAMXGAME-26AUG22CDGTIJ` | `KXLIGAMXGAME` | 12.41 | 0.67 | 24,233 | no |
| 461 | low | `KXMLSSCORE-26AUG19NYRBNSH-NYRB1NSH2` | `KXMLSSCORE-26AUG19NYRBNSH` | `KXMLSSCORE` | 12.36 | 0.67 | 6,990 | no |
| 462 | low | `KXSB-27-PHI` | `KXSB-27` | `KXSB` | 12.30 | 0.33 | 19,719 | no |
| 463 | low | `KXMLBTEAMTOTAL-26AUG191840SFCLE-SF8` | `KXMLBTEAMTOTAL-26AUG191840SFCLE` | `KXMLBTEAMTOTAL` | 12.22 | 1.00 | 6,811 | no |
| 464 | low | `KXATP-26USO-SIN` | `KXATP-26USO` | `KXATP` | 11.85 | 0.33 | 55,390 | no |
| 465 | low | `KXMLSTOTAL-26AUG19CINNYC-6` | `KXMLSTOTAL-26AUG19CINNYC` | `KXMLSTOTAL` | 11.80 | 1.00 | 9,578 | no |
| 466 | low | `KXPGAR1LEAD-BMC26-CAME` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 11.71 | 1.00 | 11,294 | no |
| 467 | low | `KXPRESNOMD-28-MK` | `KXPRESNOMD-28` | `KXPRESNOMD` | 11.62 | 0.67 | 7,053 | no |
| 468 | low | `KXMLB-26-TB` | `KXMLB-26` | `KXMLB` | 11.60 | 0.67 | 26,666 | no |
| 469 | low | `KXPGATOUR-BMC26-SIM` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 11.17 | 0.33 | 149,062 | no |
| 470 | low | `KXMLBSPREAD-26AUG191840SFCLE-SF4` | `KXMLBSPREAD-26AUG191840SFCLE` | `KXMLBSPREAD` | 10.90 | 1.00 | 9,887 | no |
| 471 | low | `KXTIME-26-ZOH` | `KXTIME-26` | `KXTIME` | 10.89 | 1.00 | 75,694 | no |
| 472 | low | `KXMLSSCORE-26AUG19PHIMIA-PHI1MIA3` | `KXMLSSCORE-26AUG19PHIMIA` | `KXMLSSCORE` | 10.31 | 1.00 | 20,945 | no |
| 473 | low | `KXMLB-26-LAD` | `KXMLB-26` | `KXMLB` | 10.18 | 0.67 | 67,736 | no |
| 474 | low | `KXUFCFIGHT-26AUG22BARKUS-KUS` | `KXUFCFIGHT-26AUG22BARKUS` | `KXUFCFIGHT` | 9.89 | 0.33 | 13,621 | no |
| 475 | low | `KXETHD-26AUG2017-T2139.99` | `KXETHD-26AUG2017` | `KXETHD` | 9.85 | 0.67 | 9,153 | no |
| 476 | low | `KXPGATOUR-BMC26-STHE` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 9.85 | 0.33 | 155,830 | no |
| 477 | low | `KXBTCD-26AUG2017-T71499.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 9.59 | 1.00 | 11,070 | no |
| 478 | low | `KXATP-26CINCIN-NAK` | `KXATP-26CINCIN` | `KXATP` | 9.40 | 0.67 | 9,183 | no |
| 479 | low | `KXMLBTOTAL-26AUG192040LADCOL-10` | `KXMLBTOTAL-26AUG192040LADCOL` | `KXMLBTOTAL` | 9.39 | 1.00 | 8,956 | no |
| 480 | low | `KXMLBHR-26AUG191840TORTB-TBRVILADE26-1` | `KXMLBHR-26AUG191840TORTB` | `KXMLBHR` | 9.23 | 1.00 | 19,079 | no |
| 481 | low | `KXHEISMAN-27-JMAIA` | `KXHEISMAN-27` | `KXHEISMAN` | 9.23 | 0.33 | 11,920 | no |
| 482 | low | `KXBTCD-26AUG2117-T70499.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 9.06 | 1.00 | 17,461 | no |
| 483 | low | `KXATPGSPREAD-26AUG19FARMUS-MUS4` | `KXATPGSPREAD-26AUG19FARMUS` | `KXATPGSPREAD` | 8.96 | 1.00 | 17,514 | no |
| 484 | low | `KXMLBTOTAL-26AUG191840SFCLE-8` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 8.89 | 1.00 | 99,109 | no |
| 485 | low | `KXFEDDECISION-26SEP-H0` | `KXFEDDECISION-26SEP` | `KXFEDDECISION` | 8.83 | 0.33 | 161,583 | no |
| 486 | low | `KXMLBHR-26AUG191940SEAMIL-SEATWARD3-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 8.81 | 0.67 | 17,414 | no |
| 487 | low | `KXLIGAMXGAME-26AUG21TIGALA-ALA` | `KXLIGAMXGAME-26AUG21TIGALA` | `KXLIGAMXGAME` | 8.76 | 0.67 | 24,618 | no |
| 488 | low | `KXMLBHR-26AUG191840TORTB-TBJARANDA8-1` | `KXMLBHR-26AUG191840TORTB` | `KXMLBHR` | 8.75 | 1.00 | 14,457 | no |
| 489 | low | `KXPGATOUR-BMC26-JSPA` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 8.72 | 0.33 | 66,655 | no |
| 490 | low | `KXWNBATOTAL-26AUG19MINGS-152` | `KXWNBATOTAL-26AUG19MINGS` | `KXWNBATOTAL` | 8.61 | 1.00 | 7,210 | no |
| 491 | low | `KXRT-INS-65` | `KXRT-INS` | `KXRT` | 8.51 | 1.00 | 12,887 | no |
| 492 | low | `KXNWSLGAME-26AUG19HDACHI-HDA` | `KXNWSLGAME-26AUG19HDACHI` | `KXNWSLGAME` | 8.47 | 1.00 | 7,993 | no |
| 493 | low | `KXNFLGAME-26AUG20SFLAC-LAC` | `KXNFLGAME-26AUG20SFLAC` | `KXNFLGAME` | 8.28 | 0.67 | 19,676 | no |
| 494 | low | `KXMLBALCENT-26-DET` | `KXMLBALCENT-26` | `KXMLBALCENT` | 8.28 | 0.67 | 6,586 | no |
| 495 | low | `KXMLBHR-26AUG192005WSHTEX-TEXCSEAGER5-1` | `KXMLBHR-26AUG192005WSHTEX` | `KXMLBHR` | 8.14 | 1.00 | 12,348 | no |
| 496 | low | `KXPGATOUR-BMC26-MLEE` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 8.13 | 0.33 | 168,418 | no |
| 497 | low | `KXMLBHR-26AUG192005WSHTEX-TEXEDURAN20-1` | `KXMLBHR-26AUG192005WSHTEX` | `KXMLBHR` | 7.91 | 1.00 | 17,126 | no |
| 498 | low | `KXRAIN-26AUG19-PHX` | `KXRAIN-26AUG19` | `KXRAIN` | 7.87 | 1.00 | 11,533 | no |
| 499 | low | `KXIPHONERELEASE-IPHONE18-26OCT01` | `KXIPHONERELEASE-IPHONE18` | `KXIPHONERELEASE` | 7.71 | 0.67 | 42,830 | no |
| 500 | low | `KXMLBRFI-26AUG201310SFCLE` | `KXMLBRFI-26AUG201310SFCLE` | `KXMLBRFI` | 7.56 | 0.33 | 11,746 | no |
| 501 | low | `KXPGATOUR-BMC26-CGOT` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 7.44 | 0.33 | 209,610 | no |
| 502 | low | `KXBTCD-26AUG2117-T67499.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 7.36 | 1.00 | 55,232 | no |
| 503 | low | `SENATEAK-26-D` | `SENATEAK-26` | `SENATEAK` | 7.34 | 0.33 | 35,889 | no |
| 504 | low | `KXAKSENATE-26NOV03-MPEL` | `KXAKSENATE-26NOV03` | `KXAKSENATE` | 7.12 | 0.67 | 11,611 | no |
| 505 | low | `KXMLBHR-26AUG191805MIAPHI-PHITTURNER7-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | 7.09 | 1.00 | 11,374 | no |
| 506 | low | `KXMLBHR-26AUG191940ATHKC-KCJCAGLIANONE14-1` | `KXMLBHR-26AUG191940ATHKC` | `KXMLBHR` | 7.01 | 1.00 | 19,889 | no |
| 507 | low | `KXGOVAKPRIMARY-26-TTAY` | `KXGOVAKPRIMARY-26` | `KXGOVAKPRIMARY` | 6.99 | 0.33 | 10,110 | no |
| 508 | low | `KXCS2GAME-26AUG201000TSB8-B8` | `KXCS2GAME-26AUG201000TSB8` | `KXCS2GAME` | 6.96 | 0.33 | 7,904 | no |
| 509 | low | `KXPGATOUR-BMC26-VHOV` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 6.83 | 0.67 | 202,694 | no |
| 510 | low | `KXMLBHR-26AUG191805MIAPHI-MIAHHERNNDEZ13-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | 6.83 | 1.00 | 9,445 | no |
| 511 | low | `KXUFCFIGHT-26AUG22HERROD-HER` | `KXUFCFIGHT-26AUG22HERROD` | `KXUFCFIGHT` | 6.70 | 0.33 | 7,464 | no |
| 512 | low | `KXMLBSPREAD-26AUG191940SEAMIL-SEA3` | `KXMLBSPREAD-26AUG191940SEAMIL` | `KXMLBSPREAD` | 6.69 | 1.00 | 37,984 | no |
| 513 | low | `KXPRESNOMD-28-JOSS` | `KXPRESNOMD-28` | `KXPRESNOMD` | 6.61 | 0.33 | 130,971 | no |
| 514 | low | `KXBTCD-26AUG2017-T68499.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 6.57 | 1.00 | 14,313 | no |
| 515 | low | `KXBTCD-26AUG2017-T66999.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 6.55 | 0.33 | 11,562 | no |
| 516 | low | `KXLALIGAGAME-26AUG22ESPRMA-RMA` | `KXLALIGAGAME-26AUG22ESPRMA` | `KXLALIGAGAME` | 6.17 | 0.67 | 45,009 | no |
| 517 | low | `KXMLBHR-26AUG191840TORTB-TORKOKAMOTO7-1` | `KXMLBHR-26AUG191840TORTB` | `KXMLBHR` | 6.17 | 0.33 | 6,587 | no |
| 518 | low | `KXDOTA2GAME-26AUG192200TSIRO-IRO` | `KXDOTA2GAME-26AUG192200TSIRO` | `KXDOTA2GAME` | 6.04 | 0.67 | 21,662 | no |
| 519 | low | `KXNFLGAME-26AUG22NOLAR-LAR` | `KXNFLGAME-26AUG22NOLAR` | `KXNFLGAME` | 6.04 | 0.33 | 10,043 | no |
| 520 | low | `KXMLBHR-26AUG191940SEAMIL-MILAVAUGHN28-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 5.84 | 1.00 | 6,668 | no |
| 521 | low | `KXNFLGAME-26AUG21CARJAC-CAR` | `KXNFLGAME-26AUG21CARJAC` | `KXNFLGAME` | 5.74 | 0.67 | 37,608 | no |
| 522 | low | `KXWNBASPREAD-26AUG20INDDAL-IND2` | `KXWNBASPREAD-26AUG20INDDAL` | `KXWNBASPREAD` | 5.65 | 1.00 | 6,933 | no |
| 523 | low | `KXDOTA2GAME-26AUG200400TYLIQUID-LIQUID` | `KXDOTA2GAME-26AUG200400TYLIQUID` | `KXDOTA2GAME` | 5.59 | 0.33 | 14,049 | no |
| 524 | low | `KXMLB-26-NYY` | `KXMLB-26` | `KXMLB` | 5.45 | 1.00 | 21,124 | no |
| 525 | low | `KXMLBRFI-26AUG201835NYYBAL` | `KXMLBRFI-26AUG201835NYYBAL` | `KXMLBRFI` | 5.43 | 1.00 | 10,590 | no |
| 526 | low | `KXPGATOP20-BMC26-SBUR` | `KXPGATOP20-BMC26` | `KXPGATOP20` | 5.42 | 0.67 | 7,059 | no |
| 527 | low | `KXMLSSPREAD-26AUG19MINATL-MIN2` | `KXMLSSPREAD-26AUG19MINATL` | `KXMLSSPREAD` | 5.36 | 1.00 | 7,426 | no |
| 528 | low | `KXMLSSPREAD-26AUG19CLBMTL-CLB3` | `KXMLSSPREAD-26AUG19CLBMTL` | `KXMLSSPREAD` | 5.29 | 1.00 | 12,780 | no |
| 529 | low | `KXPGAR1LEAD-BMC26-RMCI` | `KXPGAR1LEAD-BMC26` | `KXPGAR1LEAD` | 5.25 | 0.33 | 22,310 | no |
| 530 | low | `KXWNBAPTS-26AUG19TORWSH-TORMMABREY3-20` | `KXWNBAPTS-26AUG19TORWSH` | `KXWNBAPTS` | 5.25 | 1.00 | 18,202 | no |
| 531 | low | `KXDOTA2GAME-26AUG200100BETVSN-VSN` | `KXDOTA2GAME-26AUG200100BETVSN` | `KXDOTA2GAME` | 5.18 | 1.00 | 39,069 | no |
| 532 | low | `KXMLBHR-26AUG191940SEAMIL-SEARAROZARENA56-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 5.15 | 1.00 | 7,736 | no |
| 533 | low | `KXBTCY-27JAN0100-B67500` | `KXBTCY-27JAN0100` | `KXBTCY` | 5.05 | 0.33 | 18,067 | no |
| 534 | low | `KXMISENATE-26-AELS` | `KXMISENATE-26` | `KXMISENATE` | 4.97 | 0.67 | 61,541 | no |
| 535 | low | `KXWNBAGAME-26AUG20ATLLA-LA` | `KXWNBAGAME-26AUG20ATLLA` | `KXWNBAGAME` | 4.92 | 0.33 | 14,266 | no |
| 536 | low | `KXMLBALWEST-26-SEA` | `KXMLBALWEST-26` | `KXMLBALWEST` | 4.86 | 0.67 | 9,687 | no |
| 537 | low | `KXMLSSPREAD-26AUG19SKCSTL-STL3` | `KXMLSSPREAD-26AUG19SKCSTL` | `KXMLSSPREAD` | 4.76 | 1.00 | 6,711 | no |
| 538 | low | `KXPGATOUR-BMC26-PCAN` | `KXPGATOUR-BMC26` | `KXPGATOUR` | 4.73 | 0.33 | 65,988 | no |
| 539 | low | `KXNFLGAME-26AUG20LVHOU-LV` | `KXNFLGAME-26AUG20LVHOU` | `KXNFLGAME` | 4.66 | 0.67 | 95,107 | no |
| 540 | low | `KXDOTA2GAME-26AUG200700FLCNGX-FLC` | `KXDOTA2GAME-26AUG200700FLCNGX` | `KXDOTA2GAME` | 4.59 | 0.33 | 12,701 | no |
| 541 | low | `KXWNBATOTAL-26AUG19MINGS-164` | `KXWNBATOTAL-26AUG19MINGS` | `KXWNBATOTAL` | 4.40 | 1.00 | 47,991 | no |
| 542 | low | `KXMLBTOTAL-26AUG192010LAAHOU-10` | `KXMLBTOTAL-26AUG192010LAAHOU` | `KXMLBTOTAL` | 4.16 | 1.00 | 15,061 | no |
| 543 | low | `KXMLBF5SPREAD-26AUG191940SEAMIL-SEA3` | `KXMLBF5SPREAD-26AUG191940SEAMIL` | `KXMLBF5SPREAD` | 4.07 | 1.00 | 6,894 | no |
| 544 | low | `KXATPGSPREAD-26AUG19TIAAUG-AUG3` | `KXATPGSPREAD-26AUG19TIAAUG` | `KXATPGSPREAD` | 4.06 | 1.00 | 10,121 | no |
| 545 | low | `KXAKSENADVANCE-26AUG18-GHEI` | `KXAKSENADVANCE-26AUG18` | `KXAKSENADVANCE` | 4.04 | 0.33 | 7,016 | no |
| 546 | low | `KXETHD-26AUG2117-T2049.99` | `KXETHD-26AUG2117` | `KXETHD` | 3.94 | 0.33 | 19,786 | no |
| 547 | low | `KXUFCFIGHT-26AUG22JUDCHA-JUD` | `KXUFCFIGHT-26AUG22JUDCHA` | `KXUFCFIGHT` | 3.94 | 0.33 | 9,739 | no |
| 548 | low | `KXF1RACE-DUTGP26-ANT` | `KXF1RACE-DUTGP26` | `KXF1RACE` | 3.87 | 0.67 | 10,267 | no |
| 549 | low | `KXMLBTOTAL-26AUG191835NYYBAL-14` | `KXMLBTOTAL-26AUG191835NYYBAL` | `KXMLBTOTAL` | 3.83 | 1.00 | 12,589 | no |
| 550 | low | `KXIPHONERELEASE-IPHONE18-27JAN01` | `KXIPHONERELEASE-IPHONE18` | `KXIPHONERELEASE` | 3.81 | 0.67 | 13,537 | no |
| 551 | low | `KXNFLGAME-26AUG20SFLAC-SF` | `KXNFLGAME-26AUG20SFLAC` | `KXNFLGAME` | 3.75 | 1.00 | 13,412 | no |
| 552 | low | `KXMLBTEAMTOTAL-26AUG191940ATHKC-KC4` | `KXMLBTEAMTOTAL-26AUG191940ATHKC` | `KXMLBTEAMTOTAL` | 3.68 | 1.00 | 15,588 | no |
| 553 | low | `KXBIGBROTHERELIMINATION-26AUG20-KAM` | `KXBIGBROTHERELIMINATION-26AUG20` | `KXBIGBROTHERELIMINATION` | 3.56 | 1.00 | 12,389 | no |
| 554 | low | `KXMLBHR-26AUG191805MIAPHI-PHIABOHM28-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | 3.55 | 0.67 | 10,909 | no |
| 555 | low | `KXMLBHR-26AUG191805MIAPHI-PHIKSCHWARBER12-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | 3.55 | 1.00 | 42,563 | no |
| 556 | low | `KXMLBHR-26AUG192005WSHTEX-WSHCABRAMS5-1` | `KXMLBHR-26AUG192005WSHTEX` | `KXMLBHR` | 3.55 | 1.00 | 20,826 | no |
| 557 | low | `KXBTCD-26AUG2017-T69499.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 3.48 | 1.00 | 17,604 | no |
| 558 | low | `KXLIGAMXGAME-26AUG22CRAATL-ATL` | `KXLIGAMXGAME-26AUG22CRAATL` | `KXLIGAMXGAME` | 3.45 | 0.33 | 12,288 | no |
| 559 | low | `KXMLSSPREAD-26AUG19CINNYC-CIN2` | `KXMLSSPREAD-26AUG19CINNYC` | `KXMLSSPREAD` | 3.45 | 1.00 | 24,535 | no |
| 560 | low | `KXMLSSPREAD-26AUG19PHIMIA-PHI2` | `KXMLSSPREAD-26AUG19PHIMIA` | `KXMLSSPREAD` | 3.32 | 1.00 | 22,013 | no |
| 561 | low | `KXMLSTOTAL-26AUG19NYRBNSH-6` | `KXMLSTOTAL-26AUG19NYRBNSH` | `KXMLSTOTAL` | 3.30 | 1.00 | 8,732 | no |
| 562 | low | `KXMLSSCORE-26AUG19CINNYC-CIN2NYC1` | `KXMLSSCORE-26AUG19CINNYC` | `KXMLSSCORE` | 3.29 | 1.00 | 17,232 | no |
| 563 | low | `KXMLBHR-26AUG192005WSHTEX-TEXJPEDERSON3-1` | `KXMLBHR-26AUG192005WSHTEX` | `KXMLBHR` | 3.09 | 1.00 | 9,407 | no |
| 564 | low | `KXLALIGAGAME-26AUG20RVCALA-RVC` | `KXLALIGAGAME-26AUG20RVCALA` | `KXLALIGAGAME` | 3.01 | 0.33 | 7,750 | no |
| 565 | low | `KXMLBF5-26AUG192040LADCOL-COL` | `KXMLBF5-26AUG192040LADCOL` | `KXMLBF5` | 2.76 | 0.67 | 8,803 | no |
| 566 | low | `KXNFLSPREAD-26AUG20LVHOU-LV2` | `KXNFLSPREAD-26AUG20LVHOU` | `KXNFLSPREAD` | 2.63 | 0.33 | 7,343 | no |
| 567 | low | `KXNFLTOTAL-26AUG20LVHOU-38` | `KXNFLTOTAL-26AUG20LVHOU` | `KXNFLTOTAL` | 2.63 | 0.67 | 7,071 | no |
| 568 | low | `KXNEXTTEAMNBA-26SCUR-GSW` | `KXNEXTTEAMNBA-26SCUR` | `KXNEXTTEAMNBA` | 2.46 | 0.33 | 7,169 | no |
| 569 | low | `KXBTC50VS100-BTC-26DEC31` | `KXBTC50VS100-BTC-26DEC31` | `KXBTC50VS100` | 2.29 | 0.33 | 15,095 | no |
| 570 | low | `KXBTCD-26AUG2117-T70999.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 2.25 | 1.00 | 35,371 | no |
| 571 | low | `KXBTCD-26AUG2017-T71249.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 2.23 | 1.00 | 6,718 | no |
| 572 | low | `KXBTCD-26AUG2117-T68999.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 2.23 | 1.00 | 38,389 | no |
| 573 | low | `KXMLBTB-26AUG192040LADCOL-LADSOHTANI17-2` | `KXMLBTB-26AUG192040LADCOL` | `KXMLBTB` | 2.12 | 0.67 | 7,793 | no |
| 574 | low | `KXMLBHIT-26AUG191835NYYBAL-NYYGLOMBARD96-1` | `KXMLBHIT-26AUG191835NYYBAL` | `KXMLBHIT` | 2.11 | 1.00 | 8,476 | no |
| 575 | low | `KXMLBNLMVP-26-SOHT` | `KXMLBNLMVP-26` | `KXMLBNLMVP` | 2.09 | 1.00 | 35,070 | no |
| 576 | low | `KXMLBHR-26AUG191940SEAMIL-SEACRALEIGH29-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | 2.07 | 1.00 | 13,368 | no |
| 577 | low | `KXMLBTEAMTOTAL-26AUG191805MIAPHI-PHI5` | `KXMLBTEAMTOTAL-26AUG191805MIAPHI` | `KXMLBTEAMTOTAL` | 1.91 | 1.00 | 10,853 | no |
| 578 | low | `KXMLSTOTAL-26AUG19ORLCHI-3` | `KXMLSTOTAL-26AUG19ORLCHI` | `KXMLSTOTAL` | 1.90 | 1.00 | 14,548 | no |
| 579 | low | `KXPGATOP5-BMC26-SSCH` | `KXPGATOP5-BMC26` | `KXPGATOP5` | 1.84 | 0.33 | 19,342 | no |
| 580 | low | `KXMLBTOTAL-26AUG192040LADCOL-16` | `KXMLBTOTAL-26AUG192040LADCOL` | `KXMLBTOTAL` | 1.71 | 1.00 | 6,806 | no |
| 581 | low | `KXNFLGAME-26AUG23SEATEN-SEA` | `KXNFLGAME-26AUG23SEATEN` | `KXNFLGAME` | 1.71 | 0.33 | 17,356 | no |
| 582 | low | `KXMLB-26-BOS` | `KXMLB-26` | `KXMLB` | 1.58 | 1.00 | 27,945 | no |
| 583 | low | `KXMLBHR-26AUG191805MIAPHI-MIAJMARSEE87-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | 1.58 | 1.00 | 15,139 | no |
| 584 | low | `KXMLBNLEAST-26-PHI` | `KXMLBNLEAST-26` | `KXMLBNLEAST` | 1.58 | 1.00 | 7,225 | no |
| 585 | low | `KXEPLGAME-26AUG21ARSCOV-ARS` | `KXEPLGAME-26AUG21ARSCOV` | `KXEPLGAME` | 1.55 | 1.00 | 10,780 | no |
| 586 | low | `KXATPCHALLENGERMATCH-26AUG19MELKOU-KOU` | `KXATPCHALLENGERMATCH-26AUG19MELKOU` | `KXATPCHALLENGERMATCH` | 1.48 | 1.00 | 7,105 | no |
| 587 | low | `KXBTCD-26AUG2117-T64499.99` | `KXBTCD-26AUG2117` | `KXBTCD` | 1.42 | 0.33 | 94,664 | no |
| 588 | low | `KXMLBRFI-26AUG202005WSHTEX` | `KXMLBRFI-26AUG202005WSHTEX` | `KXMLBRFI` | 1.39 | 1.00 | 9,469 | no |
| 589 | low | `KXMLSSCORE-26AUG19PHIMIA-PHI2MIA3` | `KXMLSSCORE-26AUG19PHIMIA` | `KXMLSSCORE` | 1.32 | 1.00 | 19,995 | no |
| 590 | low | `KXBTCD-26AUG2017-T69999.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 1.31 | 1.00 | 25,130 | no |
| 591 | low | `KXMLBTEAMTOTAL-26AUG191840SFCLE-CLE2` | `KXMLBTEAMTOTAL-26AUG191840SFCLE` | `KXMLBTEAMTOTAL` | 1.31 | 1.00 | 14,211 | no |
| 592 | low | `KXMLSTOTAL-26AUG19COLLAFC-3` | `KXMLSTOTAL-26AUG19COLLAFC` | `KXMLSTOTAL` | 1.31 | 0.33 | 19,116 | no |
| 593 | low | `KXMLBGAME-26AUG201240STLCIN-CIN` | `KXMLBGAME-26AUG201240STLCIN` | `KXMLBGAME` | 1.27 | 1.00 | 12,087 | no |
| 594 | low | `KXBTCD-26AUG2017-T69249.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 1.23 | 1.00 | 29,802 | no |
| 595 | low | `KXMLBRFI-26AUG201410ATLCWS` | `KXMLBRFI-26AUG201410ATLCWS` | `KXMLBRFI` | 1.20 | 0.67 | 9,265 | no |
| 596 | low | `KXRT-MUT-50` | `KXRT-MUT` | `KXRT` | 1.20 | 0.67 | 15,326 | no |
| 597 | low | `KXEPLGAME-26AUG23NEWLFC-LFC` | `KXEPLGAME-26AUG23NEWLFC` | `KXEPLGAME` | 1.18 | 0.67 | 7,255 | no |
| 598 | low | `KXMLBRFI-26AUG201310TORTB` | `KXMLBRFI-26AUG201310TORTB` | `KXMLBRFI` | 1.18 | 0.33 | 9,449 | no |
| 599 | low | `KXALIENS-27` | `KXALIENS-27` | `KXALIENS` | 1.17 | 0.33 | 38,740 | no |
| 600 | low | `SENATETX-26-R` | `SENATETX-26` | `SENATETX` | 1.06 | 0.33 | 133,350 | no |
| 601 | low | `KXRT-MUT-65` | `KXRT-MUT` | `KXRT` | 1.05 | 0.33 | 21,865 | no |
| 602 | low | `SENATETX-26-D` | `SENATETX-26` | `SENATETX` | 1.01 | 0.33 | 38,095 | no |
| 603 | low | `KXUFCFIGHT-26AUG22HERROD-ROD` | `KXUFCFIGHT-26AUG22HERROD` | `KXUFCFIGHT` | 0.99 | 0.33 | 18,584 | no |
| 604 | low | `KXMLBHR-26AUG192010LAAHOU-LAAZNETO9-1` | `KXMLBHR-26AUG192010LAAHOU` | `KXMLBHR` | 0.92 | 1.00 | 9,090 | no |
| 605 | low | `KXBSNGAME-26AUG192000AGSVAQ-VAQ` | `KXBSNGAME-26AUG192000AGSVAQ` | `KXBSNGAME` | 0.85 | 1.00 | 9,002 | no |
| 606 | low | `KXETHMINY-27JAN01-1250` | `KXETHMINY-27JAN01` | `KXETHMINY` | 0.85 | 0.33 | 30,215 | no |
| 607 | low | `KXLIGAMXGAME-26AUG22LEOMON-LEO` | `KXLIGAMXGAME-26AUG22LEOMON` | `KXLIGAMXGAME` | 0.83 | 1.00 | 9,299 | no |
| 608 | low | `KXBTCMINMON-BTC-26AUG31-6000000` | `KXBTCMINMON-BTC-26AUG31` | `KXBTCMINMON` | 0.82 | 0.67 | 41,321 | no |
| 609 | low | `KXBTCMAXY-26DEC31-119999.99` | `KXBTCMAXY-26DEC31` | `KXBTCMAXY` | 0.79 | 0.00 | 6,750 | no |
| 610 | low | `KXMLBKS-26AUG192005WSHTEX-WSHCCAVALLI24-7` | `KXMLBKS-26AUG192005WSHTEX` | `KXMLBKS` | 0.78 | 1.00 | 13,419 | no |
| 611 | low | `KXMLBKS-26AUG192005WSHTEX-WSHCCAVALLI24-5` | `KXMLBKS-26AUG192005WSHTEX` | `KXMLBKS` | 0.75 | 1.00 | 9,422 | no |
| 612 | low | `KXUFCFIGHT-26AUG22YOUDOR-YOU` | `KXUFCFIGHT-26AUG22YOUDOR` | `KXUFCFIGHT` | 0.71 | 0.67 | 12,184 | no |
| 613 | low | `KXBTCD-26AUG2017-T69749.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 0.71 | 1.00 | 45,475 | no |
| 614 | low | `KXMLSTOTAL-26AUG19ORLCHI-5` | `KXMLSTOTAL-26AUG19ORLCHI` | `KXMLSTOTAL` | 0.70 | 1.00 | 15,743 | no |
| 615 | low | `KXMLSSPREAD-26AUG19TORCLT-CLT3` | `KXMLSSPREAD-26AUG19TORCLT` | `KXMLSSPREAD` | 0.69 | 1.00 | 9,129 | no |
| 616 | low | `KXWNBASPREAD-26AUG19MINGS-GS4` | `KXWNBASPREAD-26AUG19MINGS` | `KXWNBASPREAD` | 0.68 | 0.33 | 7,844 | no |
| 617 | low | `KXPRESNOMD-28-REMA` | `KXPRESNOMD-28` | `KXPRESNOMD` | 0.68 | 0.33 | 106,075 | no |
| 618 | low | `KXJOINCLUB-26OCT02JALVAREZ-ATM` | `KXJOINCLUB-26OCT02JALVAREZ` | `KXJOINCLUB` | 0.66 | 0.67 | 7,113 | no |
| 619 | low | `KXMLBKS-26AUG191835NYYBAL-BALCBASSITT40-6` | `KXMLBKS-26AUG191835NYYBAL` | `KXMLBKS` | 0.66 | 1.00 | 7,968 | no |
| 620 | low | `KXMLBTOTAL-26AUG191840SFCLE-10` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | 0.66 | 1.00 | 9,990 | no |
| 621 | low | `KXRT-INS-50` | `KXRT-INS` | `KXRT` | 0.66 | 0.67 | 33,926 | no |
| 622 | low | `KXMLBKS-26AUG192005WSHTEX-WSHCCAVALLI24-8` | `KXMLBKS-26AUG192005WSHTEX` | `KXMLBKS` | 0.61 | 1.00 | 7,668 | no |
| 623 | low | `SENATEMI-26-R` | `SENATEMI-26` | `SENATEMI` | 0.61 | 0.67 | 59,955 | no |
| 624 | low | `KXITFMATCH-26AUG19COUTRE-COU` | `KXITFMATCH-26AUG19COUTRE` | `KXITFMATCH` | 0.53 | 0.67 | 69,602 | no |
| 625 | low | `KXNEXTPRESSEC-29JAN21-SJEN` | `KXNEXTPRESSEC-29JAN21` | `KXNEXTPRESSEC` | 0.53 | 0.33 | 35,122 | no |
| 626 | low | `KXMLSSPREAD-26AUG19NYRBNSH-NYRB2` | `KXMLSSPREAD-26AUG19NYRBNSH` | `KXMLSSPREAD` | 0.44 | 1.00 | 17,817 | no |
| 627 | low | `KXMLBSPREAD-26AUG191840STLCIN-STL4` | `KXMLBSPREAD-26AUG191840STLCIN` | `KXMLBSPREAD` | 0.39 | 1.00 | 8,124 | no |
| 628 | low | `KXWNBAGAME-26AUG20ATLLA-ATL` | `KXWNBAGAME-26AUG20ATLLA` | `KXWNBAGAME` | 0.39 | 0.33 | 92,382 | no |
| 629 | low | `KXMLSSCORE-26AUG19PHIMIA-PHI2MIA2` | `KXMLSSCORE-26AUG19PHIMIA` | `KXMLSSCORE` | 0.38 | 0.67 | 6,950 | no |
| 630 | low | `KXNETFLIXRANKSHOWRUNNERUP-26AUG24-TIR` | `KXNETFLIXRANKSHOWRUNNERUP-26AUG24` | `KXNETFLIXRANKSHOWRUNNERUP` | 0.37 | 0.67 | 7,243 | no |
| 631 | low | `KXETHMAXMON-ETH-26AUG31-275000` | `KXETHMAXMON-ETH-26AUG31` | `KXETHMAXMON` | 0.31 | 1.00 | 12,012 | no |
| 632 | low | `KXRT-MUT-45` | `KXRT-MUT` | `KXRT` | 0.29 | 1.00 | 13,617 | no |
| 633 | low | `GOVPARTYIL-26-D` | `GOVPARTYIL-26` | `GOVPARTYIL` | 0.26 | 0.33 | 13,045 | no |
| 634 | low | `KXPGATOP20-BMC26-CAME` | `KXPGATOP20-BMC26` | `KXPGATOP20` | 0.26 | 0.33 | 7,259 | no |
| 635 | low | `KXMLBHRR-26AUG191805MIAPHI-PHIKSCHWARBER12-2` | `KXMLBHRR-26AUG191805MIAPHI` | `KXMLBHRR` | 0.23 | 0.33 | 7,347 | no |
| 636 | low | `KXOSCARPIC-27-ODY` | `KXOSCARPIC-27` | `KXOSCARPIC` | 0.21 | 0.33 | 7,540 | no |
| 637 | low | `KXMLSSPREAD-26AUG19NYRBNSH-NSH2` | `KXMLSSPREAD-26AUG19NYRBNSH` | `KXMLSSPREAD` | 0.20 | 1.00 | 7,630 | no |
| 638 | low | `KXMLBTEAMTOTAL-26AUG191940ATHKC-KC5` | `KXMLBTEAMTOTAL-26AUG191940ATHKC` | `KXMLBTEAMTOTAL` | 0.18 | 1.00 | 8,734 | no |
| 639 | low | `SENATEFLS-26-R` | `SENATEFLS-26` | `SENATEFLS` | 0.14 | 0.33 | 11,014 | no |
| 640 | low | `KXBOXING-26AUG23ROMEROLOPEZ-ROMERO` | `KXBOXING-26AUG23ROMEROLOPEZ` | `KXBOXING` | 0.13 | 0.67 | 19,406 | no |
| 641 | low | `KXBTCD-26AUG2017-T70499.99` | `KXBTCD-26AUG2017` | `KXBTCD` | 0.13 | 1.00 | 9,178 | no |
| 642 | low | `KXGOVAKPRIMARY-26-DBRO` | `KXGOVAKPRIMARY-26` | `KXGOVAKPRIMARY` | 0.13 | 0.67 | 41,251 | no |
| 643 | low | `KXMLBTEAMTOTAL-26AUG191805MIAPHI-PHI8` | `KXMLBTEAMTOTAL-26AUG191805MIAPHI` | `KXMLBTEAMTOTAL` | 0.13 | 0.67 | 7,075 | no |
| 644 | low | `KXMLBTEAMTOTAL-26AUG191840SFCLE-SF3` | `KXMLBTEAMTOTAL-26AUG191840SFCLE` | `KXMLBTEAMTOTAL` | 0.13 | 1.00 | 9,398 | no |
| 645 | low | `KXMLBTEAMTOTAL-26AUG191840SFCLE-SF6` | `KXMLBTEAMTOTAL-26AUG191840SFCLE` | `KXMLBTEAMTOTAL` | 0.13 | 1.00 | 13,128 | no |
| 646 | low | `KXMLSSPREAD-26AUG19TORCLT-TOR2` | `KXMLSSPREAD-26AUG19TORCLT` | `KXMLSSPREAD` | 0.13 | 1.00 | 20,276 | no |
| 647 | low | `KXPGATOP20-BMC26-WKIM` | `KXPGATOP20-BMC26` | `KXPGATOP20` | 0.13 | 0.33 | 9,829 | no |
| 648 | low | `KXMLBSPREAD-26AUG191835NYYBAL-NYY3` | `KXMLBSPREAD-26AUG191835NYYBAL` | `KXMLBSPREAD` | 0.03 | 1.00 | 44,479 | no |

### 7b. The 250 highest-statistic REJECTED markets

These are the markets a naive 'take the top 12 by volume' rule would have selected. Their rejection reasons are the finding.

| ticker | event | 24h vol (screen) | contracts/min | bid | ask | rejected because |
|---|---|---:|---:|---:|---:|---|
| `KXBTC15M-26AUG192030-30` | `KXBTC15M-26AUG192030` | 764,746 | 68,501.51 | 0.92 | 0.921 | closes_in_0.1h |
| `KXPGATOUR-BMC26-ARAI` | `KXPGATOUR-BMC26` | 730,934 | 0.00 | 0.005 | 0.006 | no_measured_trading_during_probe |
| `KXMLBRFI-26AUG192005WSHTEX` | `KXMLBRFI-26AUG192005WSHTEX` | 367,444 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXSB-27-BUF` | `KXSB-27` | 307,712 | 0.00 | 0.07 | 0.08 | no_measured_trading_during_probe |
| `KXATPSETWINNER-26AUG19BORNAK-2-NAK` | `KXATPSETWINNER-26AUG19BORNAK-2` | 281,463 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXVOTEPRIMARY-GOVFLNOMR26JFIS-60` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | 261,958 | not probed | 0.0 | 0.001 | no_two_sided_quote, no_resting_size, not_probed |
| `KXWTASETWINNER-26AUG19KEYWAN-1-KEY` | `KXWTASETWINNER-26AUG19KEYWAN-1` | 259,670 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXVOTEPRIMARY-GOVFLNOMR26JFIS-65` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | 241,204 | not probed | 0.0 | 0.001 | no_two_sided_quote, no_resting_size, not_probed |
| `KXATPSETWINNER-26AUG19BORNAK-2-BOR` | `KXATPSETWINNER-26AUG19BORNAK-2` | 224,091 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXPRESNOMD-28-CMUR` | `KXPRESNOMD-28` | 222,525 | 0.00 | 0.006 | 0.007 | no_measured_trading_during_probe |
| `KXVOTEPRIMARY-GOVFLNOMR26JFIS-55` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | 211,750 | 0.00 | 0.998 | 0.999 | no_measured_trading_during_probe |
| `KXATP-26USO-COB` | `KXATP-26USO` | 201,217 | 0.00 | 0.01 | 0.02 | no_measured_trading_during_probe |
| `KXBTCD-26AUG1921-T69599.99` | `KXBTCD-26AUG1921` | 180,247 | 6,122.53 | 0.59 | 0.6 | closes_in_0.6h |
| `KXPGATOUR-BMC26-RFOX` | `KXPGATOUR-BMC26` | 177,056 | 0.00 | 0.006 | 0.007 | no_measured_trading_during_probe |
| `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-11` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | 173,497 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXPGATOUR-BMC26-ECOL` | `KXPGATOUR-BMC26` | 150,964 | 0.00 | 0.003 | 0.004 | no_measured_trading_during_probe |
| `KXHIGHLAX-26AUG19-B80.5` | `KXHIGHLAX-26AUG19` | 149,468 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXWTASETWINNER-26AUG19KEYWAN-1-WAN` | `KXWTASETWINNER-26AUG19KEYWAN-1` | 147,010 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `CONTROLH-2026-D` | `CONTROLH-2026` | 144,241 | 0.00 | 0.84 | 0.85 | no_measured_trading_during_probe |
| `KXVALORANTMAP-26AUG192000G2M80-1-M80` | `KXVALORANTMAP-26AUG192000G2M80-1` | 140,677 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXBTCD-26AUG1921-T69499.99` | `KXBTCD-26AUG1921` | 133,707 | 5,315.06 | 0.73 | 0.74 | closes_in_0.6h |
| `KXPGATOUR-BMC26-MBRE` | `KXPGATOUR-BMC26` | 133,148 | 0.00 | 0.012 | 0.013 | no_measured_trading_during_probe |
| `KXFEDDECISION-26SEP-C25` | `KXFEDDECISION-26SEP` | 132,948 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXHIGHLAX-26AUG19-T83` | `KXHIGHLAX-26AUG19` | 129,102 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXPRIMARYTURNOUT-GOVFLNOMR26-1700000` | `KXPRIMARYTURNOUT-GOVFLNOMR26` | 128,092 | 0.00 | 0.12 | 0.14 | no_measured_trading_during_probe |
| `KXVALORANTMAP-26AUG192000G2M80-1-G2` | `KXVALORANTMAP-26AUG192000G2M80-1` | 123,641 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXPRESNOMD-28-AKLO` | `KXPRESNOMD-28` | 121,679 | 0.00 | 0.001 | 0.002 | no_measured_trading_during_probe |
| `KXBTC2026200-27JAN01-200000` | `KXBTC2026200-27JAN01` | 120,763 | 0.00 | 0.03 | 0.04 | no_measured_trading_during_probe |
| `KXPGATOUR-BMC26-CAME` | `KXPGATOUR-BMC26` | 116,827 | 0.00 | 0.042 | 0.043 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-JS` | `KXPRESNOMD-28` | 107,932 | 0.00 | 0.045 | 0.046 | no_measured_trading_during_probe |
| `KXCONMEBOLSUDADVANCE-26AUG19ATLRBB-RBB` | `KXCONMEBOLSUDADVANCE-26AUG19ATLRBB` | 107,931 | not probed | 0.0 | 0.01 | no_two_sided_quote, no_resting_size, not_probed |
| `KXBTCD-26AUG1921-T69399.99` | `KXBTCD-26AUG1921` | 107,765 | 2,642.63 | 0.81 | 0.82 | closes_in_0.6h |
| `KXGOVCA-26-SHIL` | `KXGOVCA-26` | 101,734 | 0.00 | 0.035 | 0.037 | no_measured_trading_during_probe |
| `KXITFMATCH-26AUG19COUTRE-TRE` | `KXITFMATCH-26AUG19COUTRE` | 95,905 | 0.00 | 0.27 | 0.28 | no_measured_trading_during_probe |
| `KXMLBTOTAL-26AUG191940SEAMIL-7` | `KXMLBTOTAL-26AUG191940SEAMIL` | 95,723 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXPGATOUR-BMC26-JTHO` | `KXPGATOUR-BMC26` | 94,297 | 0.00 | 0.02 | 0.021 | no_measured_trading_during_probe |
| `KXITFMATCH-26AUG18ICHWUX-WUX` | `KXITFMATCH-26AUG18ICHWUX` | 93,502 | 0.00 | 0.96 | 0.97 | no_measured_trading_during_probe |
| `KXNCAAF-27-MISS` | `KXNCAAF-27` | 92,477 | 0.00 | 0.02 | 0.03 | no_measured_trading_during_probe |
| `KXBTCD-26AUG1921-T69699.99` | `KXBTCD-26AUG1921` | 90,720 | 4,650.09 | 0.44 | 0.45 | closes_in_0.6h |
| `KXITFMATCH-26AUG18SHISIN-SIN` | `KXITFMATCH-26AUG18SHISIN` | 89,983 | 0.00 | 0.02 | 0.03 | no_measured_trading_during_probe |
| `KXBTCD-26AUG2117-T65999.99` | `KXBTCD-26AUG2117` | 89,534 | 0.00 | 0.94 | 0.95 | no_measured_trading_during_probe |
| `KXMLBNLROTY-26-NMCL` | `KXMLBNLROTY-26` | 88,526 | 0.00 | 0.06 | 0.07 | no_measured_trading_during_probe |
| `KXMAYORLA-26-KBAS` | `KXMAYORLA-26` | 86,958 | 0.00 | 0.63 | 0.64 | no_measured_trading_during_probe |
| `KXFEDDECISION-26SEP-H25` | `KXFEDDECISION-26SEP` | 82,770 | 0.00 | 0.27 | 0.28 | no_measured_trading_during_probe |
| `KXATPGTOTAL-26AUG19BORNAK-25` | `KXATPGTOTAL-26AUG19BORNAK` | 82,441 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXPGATOUR-BMC26-SSTR` | `KXPGATOUR-BMC26` | 81,804 | 0.00 | 0.004 | 0.005 | no_measured_trading_during_probe |
| `KXMAYORLA-26-NRAM` | `KXMAYORLA-26` | 80,997 | 0.00 | 0.36 | 0.37 | no_measured_trading_during_probe |
| `KXMISENATE-26-MROG` | `KXMISENATE-26` | 77,647 | 0.00 | 0.4 | 0.41 | no_measured_trading_during_probe |
| `KXDPWORLDTOUR-NEC26-FCEL` | `KXDPWORLDTOUR-NEC26` | 76,833 | 0.00 | 0.014 | 0.015 | no_measured_trading_during_probe |
| `KXCONMEBOLLIBADVANCE-26AUG19COQPLA-PLA` | `KXCONMEBOLLIBADVANCE-26AUG19COQPLA` | 75,011 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXCONMEBOLSUDADVANCE-26AUG19ATLRBB-ATL` | `KXCONMEBOLSUDADVANCE-26AUG19ATLRBB` | 74,843 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXMLSTOTAL-26AUG19TORCLT-3` | `KXMLSTOTAL-26AUG19TORCLT` | 74,076 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXPGATOUR-BMC26-KKIT` | `KXPGATOUR-BMC26` | 72,514 | 0.00 | 0.015 | 0.016 | no_measured_trading_during_probe |
| `KXHIGHLAX-26AUG19-B82.5` | `KXHIGHLAX-26AUG19` | 72,224 | not probed | 0.99 | 1.0 | no_resting_size, not_probed |
| `KXBTCD-26AUG1921-T69999.99` | `KXBTCD-26AUG1921` | 70,769 | 1,906.99 | 0.11 | 0.12 | closes_in_0.6h |
| `KXPRESNOMR-28-TCAR` | `KXPRESNOMR-28` | 69,480 | 0.00 | 0.03 | 0.032 | no_measured_trading_during_probe |
| `KXMLBHR-26AUG191840TORTB-TBJCAMINERO13-1` | `KXMLBHR-26AUG191840TORTB` | 67,630 | 0.00 | 0.06 | 0.09 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-MC` | `KXPRESNOMD-28` | 66,013 | 0.00 | 0.007 | 0.009 | no_measured_trading_during_probe |
| `KXPGATOUR-BMC26-ASCO` | `KXPGATOUR-BMC26` | 65,628 | 0.00 | 0.01 | 0.011 | no_measured_trading_during_probe |
| `KXDPWORLDTOUR-NEC26-PREE` | `KXDPWORLDTOUR-NEC26` | 65,334 | 0.00 | 0.055 | 0.062 | no_measured_trading_during_probe |

*(showing 60 of 250 recorded; 96,744 rejected in total — the full list is in the JSON manifest)*

## 8. The selected universe

| stratum | ticker | event | series | structure | statistic | rank |
|---|---|---|---|---|---:|---:|
| **high** | `KXATPMATCH-26AUG19BORNAK-BOR` | `KXATPMATCH-26AUG19BORNAK` | `KXATPMATCH` | structured | 29,328.09 | 1 |
| **high** | `KXMLBGAME-26AUG191940SEAMIL-MIL` | `KXMLBGAME-26AUG191940SEAMIL` | `KXMLBGAME` | structured | 23,582.81 | 3 |
| **high** | `KXMLBGAME-26AUG191805MIAPHI-PHI` | `KXMLBGAME-26AUG191805MIAPHI` | `KXMLBGAME` | structured | 19,534.93 | 4 |
| **high** | `KXMLBGAME-26AUG191835NYYBAL-BAL` | `KXMLBGAME-26AUG191835NYYBAL` | `KXMLBGAME` | structured | 18,865.45 | 5 |
| **medium** | `KXMLBTOTAL-26AUG191840SFCLE-4` | `KXMLBTOTAL-26AUG191840SFCLE` | `KXMLBTOTAL` | greater | 202.24 | 217 |
| **medium** | `KXATPMATCH-26AUG19FARMUS-MUS` | `KXATPMATCH-26AUG19FARMUS` | `KXATPMATCH` | structured | 197.05 | 218 |
| **medium** | `KXMLS1H-26AUG19TORCLT-TIE` | `KXMLS1H-26AUG19TORCLT` | `KXMLS1H` | structured | 197.02 | 219 |
| **medium** | `KXMLSGAME-26AUG19NYRBNSH-TIE` | `KXMLSGAME-26AUG19NYRBNSH` | `KXMLSGAME` | structured | 196.82 | 220 |
| **low** | `KXMLBHR-26AUG191940SEAMIL-MILJBAUERS9-1` | `KXMLBHR-26AUG191940SEAMIL` | `KXMLBHR` | structured | 16.16 | 433 |
| **low** | `KXMLSTOTAL-26AUG19ORLCHI-6` | `KXMLSTOTAL-26AUG19ORLCHI` | `KXMLSTOTAL` | greater | 15.83 | 434 |
| **low** | `KXMLBHR-26AUG191805MIAPHI-MIAGCONINE18-1` | `KXMLBHR-26AUG191805MIAPHI` | `KXMLBHR` | structured | 15.80 | 435 |
| **low** | `KXWNBATOTAL-26AUG19TORWSH-164` | `KXWNBATOTAL-26AUG19TORWSH` | `KXWNBATOTAL` | greater | 15.74 | 436 |

**Distinct events spanned:** 12 — `KXATPMATCH-26AUG19BORNAK`, `KXATPMATCH-26AUG19FARMUS`, `KXMLBGAME-26AUG191805MIAPHI`, `KXMLBGAME-26AUG191835NYYBAL`, `KXMLBGAME-26AUG191940SEAMIL`, `KXMLBHR-26AUG191805MIAPHI`, `KXMLBHR-26AUG191940SEAMIL`, `KXMLBTOTAL-26AUG191840SFCLE`, `KXMLS1H-26AUG19TORCLT`, `KXMLSGAME-26AUG19NYRBNSH`, `KXMLSTOTAL-26AUG19ORLCHI`, `KXWNBATOTAL-26AUG19TORWSH`

**Distinct series spanned:** 8 — `KXATPMATCH`, `KXMLBGAME`, `KXMLBHR`, `KXMLBTOTAL`, `KXMLS1H`, `KXMLSGAME`, `KXMLSTOTAL`, `KXWNBATOTAL`

**Distinct contract structures (`strike_type`):** 2 — `greater`, `structured`

### Stratum boundaries

| boundary | upper stratum min | lower stratum max | ratio |
|---|---:|---:|---:|
| high_over_medium | 18,865.45 | 202.24 | 93.28x |
| medium_over_low | 196.82 | 16.16 | 12.18x |

## 9. Representativeness — read this before generalising anything

> THESE TWELVE MARKETS ARE NOT A REPRESENTATIVE SAMPLE OF THE VENUE. The sampling frame is a THREE-STAGE funnel, and every stage narrows it: (1) a census of 97392 open production markets excluding MVE shards; (2) a SCREEN to 1200 markets that already had a quoted, sized, uncrossed book and non-zero 24h volume, ordered by that screening statistic and capped at 1200; (3) a timed activity probe leaving 648 eligible. A market that was dormant during the probe, or that had a live book but no trading history, cannot appear here AT ALL. 'low' therefore means least active AMONG THE ELIGIBLE, which is nowhere near typical of the venue — the median open market on this environment has no book and trades nothing. The ranking statistic is a TRADE rate whose rank correlation with MESSAGE rate is UNMEASURED. Any statistic computed from the resulting tape describes this universe and must not be generalised to Kalshi.

