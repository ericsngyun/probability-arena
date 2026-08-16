# KALSHI-TAPE-MANIFEST-001 — frozen session manifest

**Milestone:** KALSHI-LIVE-TAPE-COLLECTOR-001 (CP6-CP9 live DEMO qualification session)  
**Verdict:** **REFUSED**  
**Activity snapshot (canonical timestamp):** `2026-08-16T04:01:12.764730+00:00`  
**Environment:** `demo` — `https://external-api.demo.kalshi.co/trade-api/v2`

> Frozen BEFORE any capture. This document fixes the universe and the selection rule so that neither can be chosen after seeing how the tape behaved.

## VERDICT: REFUSED — the session must not run as specified

The authorized universe (12 live tickers, stratified 4 high / 4 medium / 4 low by message rate, spanning several contract/event structures) **cannot be constructed from this venue at this snapshot**. Padding the universe or blurring the strata to reach 12 was explicitly forbidden, so it was not done. The reasons:

- strata not separable at medium_over_low: ratio 1.300 < required 2.0

## 1. The activity snapshot

| field | value |
|---|---|
| **canonical timestamp** (the probe's first read) | `2026-08-16T04:01:12.764730+00:00` |
| census started | `2026-08-16T03:58:59.525553+00:00` |
| census completed | `2026-08-16T04:01:09.649911+00:00` |
| census duration | 130.124 s |
| activity probe started | `2026-08-16T04:01:12.764730+00:00` |
| activity probe completed | `2026-08-16T04:08:48.672395+00:00` |
| pages fetched | 367 |
| request | `{'limit': 200, 'mve_filter': 'exclude', 'paginated_to_exhaustion': True, 'probe_route': 'GET /markets?tickers=...', 'route': 'GET /markets', 'status': 'open'}` |

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
| `max_per_event` | 3 |
| `min_distinct_events` | 6 |
| `min_distinct_series` | 4 |
| `min_distinct_strike_types` | 2 |
| `min_seconds_to_close` | 21600 |
| `min_separation_ratio` | 2.0 |
| `min_traded_contracts_per_minute` | 0.0 |
| `probe_interval_seconds` | 150.0 |
| `probe_reads` | 4 |
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
| `screen_pool_size` | 868 |
| `screen_pool_capped_at` | 1200 |
| `screen_pool_was_truncated` | False |
| `candidates_offered_to_probe` | 868 |
| `candidates_probed` | 868 |
| `reads` | 4 |
| `span_minutes` | 7.5985 |
| `lifetime_volume_non_monotonic` | 0 |
| `lifetime_volume_is_monotonic` | True |

**Probe read timestamps:** `2026-08-16T04:01:12.764730+00:00`, `2026-08-16T04:03:44.910938+00:00`, `2026-08-16T04:06:16.605143+00:00`, `2026-08-16T04:08:48.672395+00:00`

`lifetime_volume_is_monotonic` is the probe's own integrity control: the lifetime counter must never decrease, and the statistic is a difference of that counter. If it is ever `false`, the rate derived from it is not trustworthy and the affected markets are rejected rather than clamped to a plausible-looking value.

## 5. The candidate population

| quantity | value |
|---|---|
| open markets enumerated (the frame) | 73,260 |
| eligible after the gates | 582 |
| ineligible | 72,678 |
| distinct events among eligible | 25 |
| distinct series among eligible | 23 |

**Why each rejected market was rejected** (a market can fail several gates):

| gate failed | markets |
|---|---|
| `closes` | 461 |
| `crossed_book` | 59 |
| `negative_resting_size` | 45 |
| `no_measured_trading_during_probe` | 270 |
| `no_resting_size` | 68,503 |
| `no_two_sided_quote` | 64,842 |
| `not_probed` | 72,392 |

## 6. Frame integrity — does the statistic mean anything?

Computed over the WHOLE frame, not over the survivors. A funnel that only reports its output cannot tell you its input was corrupt.

| check | value |
|---|---|
| `crossed_books` | 59 |
| `frame_size` | 73260 |
| `markets_probed` | 868 |
| `markets_that_traded_during_the_probe` | 598 |
| `markets_two_sided_quote` | 8418 |
| `markets_updated_within_24h` | 509 |
| `markets_with_nonzero_screen_statistic` | 1016 |
| `markets_with_resting_size` | 4757 |
| `negative_resting_sizes` | 45 |
| `nonzero_24h_volume_but_updated_time_older_than_24h` | 1001 |
| `updated_time_contradiction_rate` | 0.985236 |
| `updated_time_tracks_trading` | False |

### 6b. Venue-model corrections forced by this run

> The CP6–CP9 preregistration requires that venue behaviour contradicting our assumptions updates the model of the venue BEFORE qualification proceeds. These are the corrections this run forced.

- `updated_time` does NOT track trading or quoting on this venue. Ten high-volume markets were re-read 180 seconds apart: `updated_time` moved on ZERO of them, while the lifetime volume counter moved on 10/10 and the top of book moved on 10/10. `updated_time` is a market-DEFINITION timestamp. An earlier revision of this tool used it as a freshness gate and rejected 73,057 of 73,630 markets as 'stale' — including markets that were trading hundreds of thousands of contracts per minute at that instant. That gate was measuring the wrong thing and has been REMOVED; freshness is now established by direct measurement (a market must trade during the probe window), which cannot be fooled the same way. Recorded here because the CP6-CP9 preregistration requires venue behaviour that contradicts our assumptions to update the venue model BEFORE qualification proceeds.

## 7. The eligible population, complete and ranked

`eligible_ranked` is COMPLETE: it is the entire pool the twelve were drawn from, every member carrying its statistic value, rank and stratum. The full enumerated frame is larger than is sensible to commit, so it is represented by `frame_digest_sha256` plus the highest-statistic rejected markets, and can be reproduced exactly by re-running the enumeration and comparing the digest.

**Frame digest (SHA-256):** `f0604224c651ac7297d69436f7ce930f1966d963ab81a9cf8a4ec23fd7141a5a`  
*covers:* sha256 of ticker\x1f repr(statistic) \x1e, ticker-sorted, whole frame

| rank | stratum | ticker | event | series | contracts/min | ToB change rate | 24h vol (screen) | selected |
|---:|---|---|---|---|---:|---:|---:|---|
| 1 | high | `KXMLB-26-TEX` | `KXMLB-26` | `KXMLB` | 16,890.34 | 1.00 | 29,364,938 | **YES** |
| 2 | high | `KXNBA-27-TOR` | `KXNBA-27` | `KXNBA` | 13,175.99 | 1.00 | 1,684,608 | **YES** |
| 3 | high | `KXLIGAMXGAME-26AUG16SLACDG-SLA` | `KXLIGAMXGAME-26AUG16SLACDG` | `KXLIGAMXGAME` | 5,419.04 | 1.00 | 584,312 | **YES** |
| 4 | high | `KXECONSTATCPIYOY-26AUG-T3.6` | `KXECONSTATCPIYOY-26AUG` | `KXECONSTATCPIYOY` | 2,632.11 | 0.67 | 3,600,994 | **YES** |
| 5 | high | `KXUFCMOV-26AUG15MAKMGI-MAKSUB` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 26.77 | 1.00 | 27,452 | no |
| 6 | high | `KXPRESNOMR-28-ES` | `KXPRESNOMR-28` | `KXPRESNOMR` | 20.02 | 1.00 | 223,783 | no |
| 7 | high | `KXPGATOUR-FESJC26-RMCI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 19.94 | 1.00 | 23,463 | no |
| 8 | high | `KXPGATOUR-FESJC26-BCAU` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 19.08 | 1.00 | 1,690,088 | no |
| 9 | high | `KXNASCARRACE-COOO81526-KYLA` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 19.01 | 1.00 | 320,352 | no |
| 10 | high | `KXHEISMAN-27-GSTOC` | `KXHEISMAN-27` | `KXHEISMAN` | 18.74 | 1.00 | 9,354 | no |
| 11 | high | `KXLIGAMXGAME-26AUG15ATLTIG-ATL` | `KXLIGAMXGAME-26AUG15ATLTIG` | `KXLIGAMXGAME` | 18.69 | 1.00 | 46,079 | no |
| 12 | high | `KXPGATOP20-FESJC26-LABE` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 18.64 | 1.00 | 208,011 | no |
| 13 | high | `KXBOXING-26AUG15SHIELDSCOTT-SHIELD` | `KXBOXING-26AUG15SHIELDSCOTT` | `KXBOXING` | 18.58 | 1.00 | 2,220 | no |
| 14 | high | `KXPGATOUR-FESJC26-RHIS` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 18.57 | 1.00 | 23,185 | no |
| 15 | high | `KXPGATOP20-FESJC26-RHIS` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 18.55 | 1.00 | 96,025 | no |
| 16 | high | `KXPRESNOMR-28-JDV` | `KXPRESNOMR-28` | `KXPRESNOMR` | 18.52 | 1.00 | 5,841 | no |
| 17 | high | `KXNBA-27-GSW` | `KXNBA-27` | `KXNBA` | 18.51 | 1.00 | 157,230 | no |
| 18 | high | `KXPGATOUR-FESJC26-HHAL` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 18.51 | 1.00 | 23,100 | no |
| 19 | high | `KXPRESNOMR-28-BD` | `KXPRESNOMR-28` | `KXPRESNOMR` | 18.50 | 1.00 | 5,703 | no |
| 20 | high | `KXMLB-26-CHC` | `KXMLB-26` | `KXMLB` | 18.47 | 1.00 | 1,090,400 | no |
| 21 | high | `KXPGATOP20-FESJC26-RHEN` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 18.46 | 1.00 | 101,706 | no |
| 22 | high | `KXSB-27-PIT` | `KXSB-27` | `KXSB` | 18.36 | 1.00 | 113,380 | no |
| 23 | high | `KXPRESNOMR-28-BK` | `KXPRESNOMR-28` | `KXPRESNOMR` | 18.34 | 1.00 | 5,651 | no |
| 24 | high | `KXPGATOP10-FESJC26-MBRE` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 18.32 | 1.00 | 5,627 | no |
| 25 | high | `KXHEISMAN-27-JSAGA` | `KXHEISMAN-27` | `KXHEISMAN` | 18.28 | 1.00 | 65,066 | no |
| 26 | high | `KXSB-27-LAC` | `KXSB-27` | `KXSB` | 18.24 | 1.00 | 23,304 | no |
| 27 | high | `KXPGATOP10-FESJC26-NHOJ` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 18.19 | 1.00 | 5,569 | no |
| 28 | high | `KXPGATOP10-FESJC26-SLOW` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 18.16 | 1.00 | 26,143 | no |
| 29 | high | `KXPGATOP20-FESJC26-MMCN` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 18.15 | 1.00 | 8,262 | no |
| 30 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-8` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 18.14 | 1.00 | 6,159 | no |
| 31 | high | `KXPGATOUR-FESJC26-TFLE` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 18.08 | 1.00 | 23,418 | no |
| 32 | high | `KXPGATOP10-FESJC26-RFOW` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 18.00 | 1.00 | 9,567 | no |
| 33 | high | `KXHEISMAN-27-JHOOV` | `KXHEISMAN-27` | `KXHEISMAN` | 18.00 | 1.00 | 9,358 | no |
| 34 | high | `KXMLB-26-MIL` | `KXMLB-26` | `KXMLB` | 17.98 | 1.00 | 254,890 | no |
| 35 | high | `KXPGATOUR-FESJC26-MSCH` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.98 | 1.00 | 23,397 | no |
| 36 | high | `KXNBA-27-OKC` | `KXNBA-27` | `KXNBA` | 17.96 | 1.00 | 17,985 | no |
| 37 | high | `KXPGATOP20-FESJC26-APOT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.96 | 1.00 | 627,450 | no |
| 38 | high | `KXPRESNOMD-28-JCRO` | `KXPRESNOMD-28` | `KXPRESNOMD` | 17.96 | 0.00 | 23,422 | no |
| 39 | high | `KXPGATOP20-FESJC26-JSMI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.93 | 1.00 | 211,927 | no |
| 40 | high | `KXHEISMAN-27-MTONE` | `KXHEISMAN-27` | `KXHEISMAN` | 17.92 | 1.00 | 24,878 | no |
| 41 | high | `KXPGATOP10-FESJC26-SYEL` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.91 | 1.00 | 6,666 | no |
| 42 | high | `KXNASCARRACE-COOO81526-TYRE` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 17.91 | 1.00 | 83,348 | no |
| 43 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFIS-65` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 17.90 | 1.00 | 7,115 | no |
| 44 | high | `KXPGATOUR-FESJC26-SSTR` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.89 | 1.00 | 23,144 | no |
| 45 | high | `KXNCAAF-27-LSU` | `KXNCAAF-27` | `KXNCAAF` | 17.86 | 1.00 | 12,923 | no |
| 46 | high | `KXHEISMAN-27-JSAYI` | `KXHEISMAN-27` | `KXHEISMAN` | 17.85 | 1.00 | 9,449 | no |
| 47 | high | `KXPGATOP10-FESJC26-PROD` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.81 | 1.00 | 10,535 | no |
| 48 | high | `KXPGATOP10-FESJC26-KREI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.79 | 1.00 | 5,736 | no |
| 49 | high | `KXNASCARRACE-COOO81526-AUCI` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 17.79 | 1.00 | 1,581,299 | no |
| 50 | high | `KXPGATOUR-FESJC26-ECOL` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.77 | 1.00 | 23,404 | no |
| 51 | high | `KXPGATOP10-FESJC26-JSPA` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.77 | 1.00 | 5,602 | no |
| 52 | high | `KXNCAAF-27-FLA` | `KXNCAAF-27` | `KXNCAAF` | 17.74 | 1.00 | 79,124 | no |
| 53 | high | `KXSB-27-GB` | `KXSB-27` | `KXSB` | 17.73 | 1.00 | 23,334 | no |
| 54 | high | `KXMLB-26-BOS` | `KXMLB-26` | `KXMLB` | 17.72 | 1.00 | 650,152 | no |
| 55 | high | `KXHEISMAN-27-SLEA` | `KXHEISMAN-27` | `KXHEISMAN` | 17.72 | 1.00 | 9,446 | no |
| 56 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFIS-55` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 17.69 | 1.00 | 8,389 | no |
| 57 | high | `KXPGATOUR-FESJC26-ASCO` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.67 | 1.00 | 1,012,089 | no |
| 58 | high | `KXPGATOUR-FESJC26-MLEE` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.67 | 1.00 | 23,216 | no |
| 59 | high | `KXSB-27-CHI` | `KXSB-27` | `KXSB` | 17.63 | 1.00 | 23,410 | no |
| 60 | high | `KXPRESNOMR-28-DJTJR` | `KXPRESNOMR-28` | `KXPRESNOMR` | 17.57 | 1.00 | 5,740 | no |
| 61 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-13` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 17.52 | 1.00 | 9,801 | no |
| 62 | high | `KXPGATOP10-FESJC26-ASMA` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.50 | 1.00 | 4,570 | no |
| 63 | high | `KXUFCFIGHT-26AUG15MAKMGI-MGI` | `KXUFCFIGHT-26AUG15MAKMGI` | `KXUFCFIGHT` | 17.50 | 1.00 | 139,782 | no |
| 64 | high | `KXPGATOP10-FESJC26-SSTR` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.49 | 1.00 | 179 | no |
| 65 | high | `KXSB-27-JAC` | `KXSB-27` | `KXSB` | 17.49 | 1.00 | 88,650 | no |
| 66 | high | `KXPGATOP20-FESJC26-MBRE` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.49 | 1.00 | 30,672 | no |
| 67 | high | `KXHEISMAN-27-DMOOR` | `KXHEISMAN-27` | `KXHEISMAN` | 17.49 | 1.00 | 7,822 | no |
| 68 | high | `KXPGATOUR-FESJC26-JSPA` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.48 | 1.00 | 23,271 | no |
| 69 | high | `KXNBA-27-BOS` | `KXNBA-27` | `KXNBA` | 17.43 | 1.00 | 17,916 | no |
| 70 | high | `KXMLB-26-TOR` | `KXMLB-26` | `KXMLB` | 17.37 | 1.00 | 30,183,666 | no |
| 71 | high | `KXPGATOUR-FESJC26-HENG` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.34 | 1.00 | 23,314 | no |
| 72 | high | `KXSB-27-DEN` | `KXSB-27` | `KXSB` | 17.33 | 1.00 | 23,280 | no |
| 73 | high | `KXPRESNOMR-28-STE` | `KXPRESNOMR-28` | `KXPRESNOMR` | 17.33 | 1.00 | 5,901 | no |
| 74 | high | `KXMLB-26-PHI` | `KXMLB-26` | `KXMLB` | 17.32 | 1.00 | 23,404 | no |
| 75 | high | `KXMLB-26-CLE` | `KXMLB-26` | `KXMLB` | 17.32 | 1.00 | 15,134,236 | no |
| 76 | high | `KXPGATOP20-FESJC26-BCAU` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.30 | 1.00 | 8,213 | no |
| 77 | high | `KXPGATOP10-FESJC26-PCAN` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 17.30 | 1.00 | 5,232 | no |
| 78 | high | `KXPGATOUR-FESJC26-KKIT` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.28 | 1.00 | 1,640,922 | no |
| 79 | high | `KXNASCARRACE-COOO81526-JOLO` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 17.27 | 1.00 | 15,328 | no |
| 80 | high | `KXMLB-26-HOU` | `KXMLB-26` | `KXMLB` | 17.23 | 1.00 | 5,017,128 | no |
| 81 | high | `KXMLB-26-STL` | `KXMLB-26` | `KXMLB` | 17.22 | 1.00 | 245,275 | no |
| 82 | high | `KXPGATOUR-FESJC26-KREI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.21 | 1.00 | 23,212 | no |
| 83 | high | `KXSCRSENS-26-DNOR` | `KXSCRSENS-26` | `KXSCRSENS` | 17.21 | 1.00 | 71,217 | no |
| 84 | high | `KXTESTMATCH-26AUG150030INDSRI-IND` | `KXTESTMATCH-26AUG150030INDSRI` | `KXTESTMATCH` | 17.21 | 1.00 | 16,660 | no |
| 85 | high | `KXPGATOP20-FESJC26-CAME` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.10 | 1.00 | 10,590 | no |
| 86 | high | `KXUFCMOV-26AUG15MAKMGI-MGIKOTKODQ` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 17.10 | 1.00 | 34,553 | no |
| 87 | high | `KXSB-27-BAL` | `KXSB-27` | `KXSB` | 17.08 | 1.00 | 23,425 | no |
| 88 | high | `KXPGATOP20-FESJC26-PCOO` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.07 | 1.00 | 8,183 | no |
| 89 | high | `KXPGATOUR-FESJC26-NHJG` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.06 | 1.00 | 23,073 | no |
| 90 | high | `KXSB-27-SF` | `KXSB-27` | `KXSB` | 17.05 | 1.00 | 23,291 | no |
| 91 | high | `KXPGATOUR-FESJC26-SSCH` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.02 | 1.00 | 19,214 | no |
| 92 | high | `KXSB-27-TB` | `KXSB-27` | `KXSB` | 17.02 | 1.00 | 73,299 | no |
| 93 | high | `KXNASCARRACE-COOO81526-WIBY` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 17.02 | 1.00 | 231,513 | no |
| 94 | high | `KXNBA-27-LAL` | `KXNBA-27` | `KXNBA` | 17.01 | 1.00 | 17,849 | no |
| 95 | high | `KXPGATOP20-FESJC26-GWOO` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 17.00 | 1.00 | 81,183 | no |
| 96 | high | `KXPGATOUR-FESJC26-ARAI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 17.00 | 1.00 | 23,510 | no |
| 97 | high | `KXHEISMAN-27-CCARR` | `KXHEISMAN-27` | `KXHEISMAN` | 16.97 | 1.00 | 7,164 | no |
| 98 | high | `KXSB-27-KC` | `KXSB-27` | `KXSB` | 16.97 | 1.00 | 23,567 | no |
| 99 | high | `KXPRESNOMR-28-ITRU` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.96 | 1.00 | 5,688 | no |
| 100 | high | `KXNASCARRACE-COOO81526-ROCH` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 16.94 | 1.00 | 196,894 | no |
| 101 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-14` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 16.94 | 1.00 | 17,026 | no |
| 102 | high | `KXMLB-26-SF` | `KXMLB-26` | `KXMLB` | 16.93 | 1.00 | 23,403 | no |
| 103 | high | `KXPGATOP20-FESJC26-NHOJ` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.92 | 1.00 | 753,087 | no |
| 104 | high | `KXPGATOUR-FESJC26-VHOV` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.91 | 1.00 | 23,402 | no |
| 105 | high | `KXPGATOP10-FESJC26-CCON` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.90 | 1.00 | 5,609 | no |
| 106 | high | `KXPGATOUR-FESJC26-BGRI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.90 | 1.00 | 23,168 | no |
| 107 | high | `KXPGATOUR-FESJC26-GWOO` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.90 | 1.00 | 23,115 | no |
| 108 | high | `KXPGATOP20-FESJC26-MFIT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.89 | 1.00 | 8,241 | no |
| 109 | high | `KXPGATOP10-FESJC26-TFLE` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.87 | 1.00 | 4,749 | no |
| 110 | high | `KXPGATOP10-FESJC26-APOT` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.84 | 1.00 | 5,695 | no |
| 111 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFIS-51` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 16.83 | 1.00 | 11,541 | no |
| 112 | high | `KXHEISMAN-27-JMAIA` | `KXHEISMAN-27` | `KXHEISMAN` | 16.83 | 1.00 | 7,803 | no |
| 113 | high | `KXNCAAF-27-MISS` | `KXNCAAF-27` | `KXNCAAF` | 16.80 | 1.00 | 34,706 | no |
| 114 | high | `KXPRESNOMR-28-VR` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.79 | 1.00 | 5,818 | no |
| 115 | high | `KXMLB-26-LAD` | `KXMLB-26` | `KXMLB` | 16.76 | 1.00 | 609,073 | no |
| 116 | high | `KXPRESNOMR-28-TCAR` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.72 | 1.00 | 5,661 | no |
| 117 | high | `KXPGATOUR-FESJC26-ABHA` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.72 | 1.00 | 23,832 | no |
| 118 | high | `KXPGATOUR-FESJC26-ANOR` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.71 | 1.00 | 23,345 | no |
| 119 | high | `KXUFCFIGHT-26AUG15MAKMGI-MAK` | `KXUFCFIGHT-26AUG15MAKMGI` | `KXUFCFIGHT` | 16.70 | 1.00 | 113,276 | no |
| 120 | high | `KXMLB-26-WSH` | `KXMLB-26` | `KXMLB` | 16.68 | 1.00 | 356,810 | no |
| 121 | high | `KXPGATOUR-FESJC26-MMCN` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.68 | 1.00 | 23,438 | no |
| 122 | high | `KXNCAAF-27-IND` | `KXNCAAF-27` | `KXNCAAF` | 16.66 | 1.00 | 12,912 | no |
| 123 | high | `KXPRESNOMR-28-MTG` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.66 | 1.00 | 5,758 | no |
| 124 | high | `KXPGATOP10-FESJC26-MFIT` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.65 | 1.00 | 6,720 | no |
| 125 | high | `KXPGATOP10-FESJC26-JKOI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.65 | 1.00 | 27,450 | no |
| 126 | high | `KXPRESNOMR-28-MPEN` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.64 | 1.00 | 224,940 | no |
| 127 | high | `KXPRESNOMD-28-AELS` | `KXPRESNOMD-28` | `KXPRESNOMD` | 16.64 | 1.00 | 53,209 | no |
| 128 | high | `KXPGATOP20-FESJC26-SYEL` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.62 | 1.00 | 50,724 | no |
| 129 | high | `KXPGATOP20-FESJC26-SSTR` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.62 | 1.00 | 8,058 | no |
| 130 | high | `KXPGATOP20-FESJC26-RCAS` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.61 | 1.00 | 8,064 | no |
| 131 | high | `KXPRESNOMR-28-RPAU` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.60 | 1.00 | 5,709 | no |
| 132 | high | `KXPGATOUR-FESJC26-HMAT` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.58 | 1.00 | 23,229 | no |
| 133 | high | `KXNCAAF-27-USC` | `KXNCAAF-27` | `KXNCAAF` | 16.58 | 1.00 | 214,269 | no |
| 134 | high | `KXPGATOUR-FESJC26-TKIM` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.57 | 1.00 | 23,343 | no |
| 135 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-6` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 16.57 | 1.00 | 5,979 | no |
| 136 | high | `KXNBA-27-PHI` | `KXNBA-27` | `KXNBA` | 16.56 | 1.00 | 18,017 | no |
| 137 | high | `KXPGATOP20-FESJC26-ECOL` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.55 | 1.00 | 16,404 | no |
| 138 | high | `KXPGATOUR-FESJC26-XSCH` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.54 | 1.00 | 23,307 | no |
| 139 | high | `KXPRESNOMR-28-MG` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.52 | 1.00 | 224,966 | no |
| 140 | high | `KXPGATOP20-FESJC26-CCON` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.51 | 1.00 | 409,096 | no |
| 141 | high | `KXPGATOP10-FESJC26-SSTE` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.50 | 1.00 | 33,436 | no |
| 142 | high | `KXPGATOP10-FESJC26-NECH` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.45 | 1.00 | 12,718 | no |
| 143 | high | `KXPGATOUR-FESJC26-NECH` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.45 | 1.00 | 23,239 | no |
| 144 | high | `KXFEDDECISION-26SEP-C25` | `KXFEDDECISION-26SEP` | `KXFEDDECISION` | 16.45 | 1.00 | 113,288 | no |
| 145 | high | `KXPGATOP20-FESJC26-SIM` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.45 | 1.00 | 74,939 | no |
| 146 | high | `KXPGATOP10-FESJC26-SBUR` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.44 | 1.00 | 448 | no |
| 147 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-57` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 16.43 | 1.00 | 8,496 | no |
| 148 | high | `KXPGATOUR-FESJC26-CMOR` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.42 | 1.00 | 23,312 | no |
| 149 | high | `KXPRESNOMR-28-GA` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.40 | 1.00 | 5,711 | no |
| 150 | high | `KXPGATOUR-FESJC26-JBRI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.39 | 1.00 | 23,465 | no |
| 151 | high | `KXPGATOP10-FESJC26-RHEN` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.38 | 1.00 | 20,145 | no |
| 152 | high | `KXPGATOP20-FESJC26-CGOT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.38 | 1.00 | 9,618 | no |
| 153 | high | `KXPRESNOMR-28-EM` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.37 | 1.00 | 5,808 | no |
| 154 | high | `KXPGATOP20-FESJC26-HENG` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.36 | 1.00 | 8,139 | no |
| 155 | high | `KXPGATOP20-FESJC26-JKNA` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.34 | 1.00 | 66,651 | no |
| 156 | high | `KXSB-27-HOU` | `KXSB-27` | `KXSB` | 16.34 | 1.00 | 23,200 | no |
| 157 | high | `KXNCAAF-27-ORE` | `KXNCAAF-27` | `KXNCAAF` | 16.34 | 1.00 | 12,638 | no |
| 158 | high | `KXMLB-26-LAA` | `KXMLB-26` | `KXMLB` | 16.33 | 1.00 | 23,213 | no |
| 159 | high | `KXPGATOUR-FESJC26-ASMA` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.31 | 1.00 | 6,516,278 | no |
| 160 | high | `KXPGATOUR-FESJC26-PCOO` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.30 | 1.00 | 23,366 | no |
| 161 | high | `KXPGATOUR-FESJC26-MFIT` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.28 | 1.00 | 354,430 | no |
| 162 | high | `KXNASCARRACE-COOO81526-RYBL` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 16.27 | 1.00 | 20,351 | no |
| 163 | high | `KXPGATOP10-FESJC26-XSCH` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.27 | 1.00 | 5,369 | no |
| 164 | high | `KXPRESNOMR-28-TMAS` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.27 | 1.00 | 5,717 | no |
| 165 | high | `KXPRESNOMR-28-TG` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.25 | 1.00 | 472,458 | no |
| 166 | high | `KXPGATOP10-FESJC26-MHOM` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.21 | 1.00 | 292 | no |
| 167 | high | `KXPGATOUR-FESJC26-RGER` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.20 | 1.00 | 23,302 | no |
| 168 | high | `KXPGATOP20-FESJC26-JROS` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.20 | 1.00 | 8,143 | no |
| 169 | high | `KXPGATOP20-FESJC26-WKIM` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.20 | 1.00 | 92,452 | no |
| 170 | high | `KXPGATOP20-FESJC26-BGRI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.19 | 1.00 | 30,478 | no |
| 171 | high | `KXGOVFLNOMR-26-JFIS` | `KXGOVFLNOMR-26` | `KXGOVFLNOMR` | 16.16 | 1.00 | 4,427,002 | no |
| 172 | high | `KXPGATOUR-FESJC26-STHE` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.14 | 1.00 | 23,203 | no |
| 173 | high | `KXPGATOP10-FESJC26-RHIS` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.11 | 1.00 | 5,860 | no |
| 174 | high | `KXPRESNOMR-28-TC` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.11 | 1.00 | 5,728 | no |
| 175 | high | `KXPGATOP10-FESJC26-PCOO` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.10 | 1.00 | 41,554 | no |
| 176 | high | `KXPGATOUR-FESJC26-WCLA` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 16.09 | 1.00 | 23,316 | no |
| 177 | high | `KXSB-27-MIN` | `KXSB-27` | `KXSB` | 16.09 | 1.00 | 169,228 | no |
| 178 | high | `KXPGATOP20-FESJC26-RMCI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.08 | 1.00 | 595,954 | no |
| 179 | high | `KXNASCARRACE-COOO81526-AUDI` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 16.08 | 1.00 | 20,065 | no |
| 180 | high | `KXSB-27-LAR` | `KXSB-27` | `KXSB` | 16.07 | 1.00 | 489,986 | no |
| 181 | high | `KXHEISMAN-27-JSMIT` | `KXHEISMAN-27` | `KXHEISMAN` | 16.07 | 1.00 | 9,162 | no |
| 182 | high | `KXPRESNOMR-28-NMIN` | `KXPRESNOMR-28` | `KXPRESNOMR` | 16.06 | 1.00 | 116,784 | no |
| 183 | high | `KXPGATOP20-FESJC26-KMIT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.05 | 1.00 | 94,978 | no |
| 184 | high | `KXPGATOP20-FESJC26-MLEE` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 16.05 | 1.00 | 10,105 | no |
| 185 | high | `KXPRESNOMD-28-CMUR` | `KXPRESNOMD-28` | `KXPRESNOMD` | 16.04 | 1.00 | 23,303 | no |
| 186 | high | `KXNBA-27-MIA` | `KXNBA-27` | `KXNBA` | 16.03 | 1.00 | 17,959 | no |
| 187 | high | `KXPGATOP10-FESJC26-BCAU` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 16.01 | 1.00 | 421 | no |
| 188 | high | `KXPGATOP10-FESJC26-RGER` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.99 | 1.00 | 30,645 | no |
| 189 | high | `KXSB-27-DET` | `KXSB-27` | `KXSB` | 15.99 | 1.00 | 23,264 | no |
| 190 | high | `KXPGATOP10-FESJC26-CAME` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.99 | 1.00 | 21,969 | no |
| 191 | high | `KXVOTEPRIMARY-GOVFLNOMR26JFIS-52` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 15.99 | 1.00 | 5,535 | no |
| 192 | high | `KXPGATOP10-FESJC26-SIM` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.98 | 1.00 | 2,187 | no |
| 193 | high | `KXNCAAF-27-ALA` | `KXNCAAF-27` | `KXNCAAF` | 15.97 | 1.00 | 12,650 | no |
| 194 | high | `KXUFCMOV-26AUG15MAKMGI-MGISUB` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 15.97 | 1.00 | 138,540 | no |
| 195 | medium | `KXPGATOP10-FESJC26-RMCI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.96 | 1.00 | 16,715 | **YES** |
| 196 | medium | `KXTESTMATCH-26AUG122030BANAUS-TIE` | `KXTESTMATCH-26AUG122030BANAUS` | `KXTESTMATCH` | 15.93 | 1.00 | 302,697 | **YES** |
| 197 | medium | `KXMLB-26-PIT` | `KXMLB-26` | `KXMLB` | 15.91 | 1.00 | 11,023,143 | no |
| 198 | medium | `KXPGATOUR-FESJC26-RFOW` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.91 | 1.00 | 23,598 | **YES** |
| 199 | medium | `KXPGATOUR-FESJC26-SBUR` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.90 | 1.00 | 23,290 | no |
| 200 | medium | `KXPGATOUR-FESJC26-SIM` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.88 | 1.00 | 23,487 | no |
| 201 | medium | `KXPGATOP20-FESJC26-JKOI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.87 | 1.00 | 13,077 | **YES** |
| 202 | medium | `KXPGATOUR-FESJC26-SLOW` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.87 | 1.00 | 23,148 | no |
| 203 | medium | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-9` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 15.87 | 1.00 | 7,315 | no |
| 204 | medium | `KXPRESNOMR-28-JH` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.86 | 1.00 | 5,732 | no |
| 205 | medium | `KXPGATOUR-FESJC26-MKIM` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.84 | 1.00 | 23,452 | no |
| 206 | medium | `KXNASCARRACE-COOO81526-DEHA` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 15.83 | 1.00 | 479,702 | no |
| 207 | medium | `KXPGATOP20-FESJC26-KREI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.83 | 1.00 | 644,863 | no |
| 208 | medium | `KXMLB-26-CIN` | `KXMLB-26` | `KXMLB` | 15.83 | 1.00 | 23,130 | no |
| 209 | medium | `KXFEDDECISION-26SEP-H25` | `KXFEDDECISION-26SEP` | `KXFEDDECISION` | 15.81 | 1.00 | 11,737 | no |
| 210 | medium | `KXBOXING-26AUG15SHIELDSCOTT-SCOTT` | `KXBOXING-26AUG15SHIELDSCOTT` | `KXBOXING` | 15.80 | 1.00 | 2,106 | no |
| 211 | medium | `KXPGATOP10-FESJC26-HHAL` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.80 | 1.00 | 484 | no |
| 212 | medium | `KXPGATOP20-FESJC26-MKIM` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.78 | 1.00 | 13,764 | no |
| 213 | medium | `KXPGATOP10-FESJC26-JTHO` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.76 | 1.00 | 7,082 | no |
| 214 | medium | `KXSB-27-PHI` | `KXSB-27` | `KXSB` | 15.75 | 1.00 | 23,153 | no |
| 215 | medium | `KXNCAAF-27-TENN` | `KXNCAAF-27` | `KXNCAAF` | 15.74 | 1.00 | 55,836 | no |
| 216 | medium | `KXPGATOP20-FESJC26-MHOM` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.74 | 1.00 | 141,769 | no |
| 217 | medium | `KXPGATOUR-FESJC26-PCAN` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.74 | 1.00 | 8,082,869 | no |
| 218 | medium | `KXSB-27-DAL` | `KXSB-27` | `KXSB` | 15.73 | 1.00 | 1,079,714 | no |
| 219 | medium | `KXPGATOUR-FESJC26-PROD` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.73 | 1.00 | 134,416 | no |
| 220 | medium | `KXPGATOP10-FESJC26-BGRI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.73 | 1.00 | 5,924 | no |
| 221 | medium | `KXPGATOUR-FESJC26-RHEN` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.71 | 1.00 | 23,250 | no |
| 222 | medium | `KXNASCARRACE-COOO81526-BUWA` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 15.70 | 1.00 | 20,181 | no |
| 223 | medium | `KXPGATOUR-FESJC26-MHOM` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.70 | 1.00 | 23,481 | no |
| 224 | medium | `KXPGATOP20-FESJC26-RGER` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.69 | 1.00 | 132,497 | no |
| 225 | medium | `KXLIGAMXGAME-26AUG15ATLTIG-TIG` | `KXLIGAMXGAME-26AUG15ATLTIG` | `KXLIGAMXGAME` | 15.69 | 1.00 | 41,875 | no |
| 226 | medium | `KXHEISMAN-27-DWILL` | `KXHEISMAN-27` | `KXHEISMAN` | 15.66 | 1.00 | 103,890 | no |
| 227 | medium | `KXPRESNOMR-28-GY` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.66 | 1.00 | 5,830 | no |
| 228 | medium | `KXPGATOP20-FESJC26-JSPA` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.63 | 1.00 | 177,827 | no |
| 229 | medium | `KXSB-27-SEA` | `KXSB-27` | `KXSB` | 15.59 | 1.00 | 23,085 | no |
| 230 | medium | `KXPRESNOMR-28-RFK` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.58 | 1.00 | 5,654 | no |
| 231 | medium | `KXPGATOP20-FESJC26-PROD` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.58 | 1.00 | 117,676 | no |
| 232 | medium | `KXPGATOUR-FESJC26-JPOS` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.57 | 1.00 | 23,387 | no |
| 233 | medium | `KXPGATOP20-FESJC26-ARAI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.55 | 1.00 | 154,740 | no |
| 234 | medium | `KXPGATOP10-FESJC26-CMOR` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.55 | 1.00 | 28,067 | no |
| 235 | medium | `KXNCAAF-27-MICH` | `KXNCAAF-27` | `KXNCAAF` | 15.51 | 1.00 | 28,941 | no |
| 236 | medium | `KXPGATOP20-FESJC26-WCLA` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.50 | 1.00 | 8,099 | no |
| 237 | medium | `KXNCAAF-27-ND` | `KXNCAAF-27` | `KXNCAAF` | 15.49 | 1.00 | 12,733 | no |
| 238 | medium | `KXPRESNOMR-28-DJT` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.48 | 1.00 | 5,883 | no |
| 239 | medium | `KXVOTEPRIMARY-GOVFLNOMR26JFIS-60` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 15.47 | 1.00 | 12,995 | no |
| 240 | medium | `KXPGATOUR-FESJC26-JSPI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.46 | 1.00 | 23,405 | no |
| 241 | medium | `KXPRESNOMD-28-GPLA` | `KXPRESNOMD-28` | `KXPRESNOMD` | 15.45 | 1.00 | 23,452 | no |
| 242 | medium | `KXPGATOP20-FESJC26-NTAY` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.42 | 1.00 | 8,111 | no |
| 243 | medium | `KXPGATOP20-FESJC26-CMOR` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.41 | 1.00 | 124,263 | no |
| 244 | medium | `KXPGATOP20-FESJC26-KKIT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.36 | 1.00 | 8,117 | no |
| 245 | medium | `KXPGATOP20-FESJC26-ANOR` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.35 | 1.00 | 10,448 | no |
| 246 | medium | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-7` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 15.35 | 1.00 | 6,580 | no |
| 247 | medium | `KXMLB-26-AZ` | `KXMLB-26` | `KXMLB` | 15.33 | 1.00 | 923,423 | no |
| 248 | medium | `KXNCAAF-27-UGA` | `KXNCAAF-27` | `KXNCAAF` | 15.33 | 1.00 | 12,797 | no |
| 249 | medium | `KXPGATOP20-FESJC26-TFLE` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.31 | 1.00 | 8,097 | no |
| 250 | medium | `KXMLB-26-TB` | `KXMLB-26` | `KXMLB` | 15.29 | 1.00 | 1,778,342 | no |
| 251 | medium | `KXNASCARRACE-COOO81526-CHBR` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 15.29 | 1.00 | 25,912 | no |
| 252 | medium | `KXNCAAF-27-TEX` | `KXNCAAF-27` | `KXNCAAF` | 15.28 | 1.00 | 12,847 | no |
| 253 | medium | `KXPGATOP20-FESJC26-HHAL` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.28 | 1.00 | 8,116 | no |
| 254 | medium | `KXPGATOP20-FESJC26-SBUR` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.27 | 1.00 | 8,337 | no |
| 255 | medium | `KXPGATOP20-FESJC26-ABHA` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 15.24 | 1.00 | 30,409 | no |
| 256 | medium | `KXPGATOP10-FESJC26-JSMI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.24 | 1.00 | 5,575 | no |
| 257 | medium | `KXMLB-26-MIN` | `KXMLB-26` | `KXMLB` | 15.23 | 1.00 | 23,279 | no |
| 258 | medium | `KXMLB-26-MIA` | `KXMLB-26` | `KXMLB` | 15.22 | 1.00 | 23,578,749 | no |
| 259 | medium | `KXPGATOP10-FESJC26-KMIT` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.21 | 1.00 | 8,406 | no |
| 260 | medium | `KXPRESNOMR-28-NH` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.15 | 1.00 | 5,765 | no |
| 261 | medium | `KXPGATOUR-FESJC26-SYEL` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.14 | 1.00 | 23,177 | no |
| 262 | medium | `KXPRESNOMR-28-PHEG` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.14 | 1.00 | 5,607 | no |
| 263 | medium | `KXSB-27-WAS` | `KXSB-27` | `KXSB` | 15.13 | 1.00 | 161,601 | no |
| 264 | medium | `KXSB-27-CIN` | `KXSB-27` | `KXSB` | 15.12 | 1.00 | 23,332 | no |
| 265 | medium | `KXPRESNOMR-28-SHS` | `KXPRESNOMR-28` | `KXPRESNOMR` | 15.11 | 1.00 | 5,856 | no |
| 266 | medium | `KXPGATOUR-FESJC26-AFIT` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 15.11 | 1.00 | 23,336 | no |
| 267 | medium | `KXPGATOP10-FESJC26-ECOL` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.06 | 1.00 | 5,662 | no |
| 268 | medium | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-11` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 15.04 | 1.00 | 11,518 | no |
| 269 | medium | `KXTESTMATCH-26AUG150030INDSRI-TIE` | `KXTESTMATCH-26AUG150030INDSRI` | `KXTESTMATCH` | 15.04 | 1.00 | 112,127 | no |
| 270 | medium | `KXSB-27-BUF` | `KXSB-27` | `KXSB` | 15.03 | 1.00 | 23,286 | no |
| 271 | medium | `KXNBA-27-IND` | `KXNBA-27` | `KXNBA` | 15.03 | 1.00 | 18,028 | no |
| 272 | medium | `KXSB-27-NE` | `KXSB-27` | `KXSB` | 15.03 | 1.00 | 23,434 | no |
| 273 | medium | `KXPGATOP10-FESJC26-JSPI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 15.02 | 1.00 | 2,834 | no |
| 274 | medium | `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-12` | `KXVOTEPRIMARY-GOVFLNOMR26JFIS` | `KXVOTEPRIMARY` | 15.02 | 1.00 | 6,268 | no |
| 275 | medium | `KXMLB-26-SD` | `KXMLB-26` | `KXMLB` | 15.00 | 1.00 | 4,795,028 | no |
| 276 | medium | `KXPGATOP10-FESJC26-JPOS` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.99 | 1.00 | 5,630 | no |
| 277 | medium | `KXHEISMAN-27-TCHAM` | `KXHEISMAN-27` | `KXHEISMAN` | 14.97 | 1.00 | 9,580 | no |
| 278 | medium | `KXNCAAF-27-OSU` | `KXNCAAF-27` | `KXNCAAF` | 14.97 | 1.00 | 13,024 | no |
| 279 | medium | `KXNBA-27-CLE` | `KXNBA-27` | `KXNBA` | 14.95 | 1.00 | 17,595 | no |
| 280 | medium | `KXHEISMAN-27-KJEN` | `KXHEISMAN-27` | `KXHEISMAN` | 14.94 | 1.00 | 53,826 | no |
| 281 | medium | `KXPGATOUR-FESJC26-MBRE` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.94 | 1.00 | 23,396 | no |
| 282 | medium | `KXFEDDECISION-26SEP-H0` | `KXFEDDECISION-26SEP` | `KXFEDDECISION` | 14.94 | 1.00 | 265,352 | no |
| 283 | medium | `KXTESTMATCH-26AUG122030BANAUS-BAN` | `KXTESTMATCH-26AUG122030BANAUS` | `KXTESTMATCH` | 14.93 | 1.00 | 327,766 | no |
| 284 | medium | `KXNASCARRACE-COOO81526-JOBE` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 14.92 | 1.00 | 152,686 | no |
| 285 | medium | `KXNCAAF-27-TTU` | `KXNCAAF-27` | `KXNCAAF` | 14.92 | 1.00 | 12,696 | no |
| 286 | medium | `KXPGATOP20-FESJC26-ASCO` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.89 | 1.00 | 8,092 | no |
| 287 | medium | `KXPGATOUR-FESJC26-RFOX` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.89 | 1.00 | 22,933 | no |
| 288 | medium | `KXUFCMOV-26AUG15MAKMGI-MAKDEC` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 14.89 | 1.00 | 102,801 | no |
| 289 | medium | `KXUFCMOV-26AUG15MAKMGI-MGIDEC` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 14.87 | 1.00 | 21,689 | no |
| 290 | medium | `KXPGATOP20-FESJC26-STHE` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.83 | 1.00 | 8,156 | no |
| 291 | medium | `KXPGATOP10-FESJC26-LABE` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.83 | 1.00 | 468 | no |
| 292 | medium | `KXNBA-27-NYK` | `KXNBA-27` | `KXNBA` | 14.80 | 1.00 | 18,007 | no |
| 293 | medium | `KXSCRSENS-26-RNOR` | `KXSCRSENS-26` | `KXSCRSENS` | 14.79 | 1.00 | 330,732 | no |
| 294 | medium | `KXNBA-27-HOU` | `KXNBA-27` | `KXNBA` | 14.78 | 1.00 | 29,259 | no |
| 295 | medium | `KXPGATOP20-FESJC26-MTHO` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.78 | 1.00 | 50,580 | no |
| 296 | medium | `KXTESTMATCH-26AUG122030BANAUS-AUS` | `KXTESTMATCH-26AUG122030BANAUS` | `KXTESTMATCH` | 14.77 | 1.00 | 45,272 | no |
| 297 | medium | `KXPGATOP20-FESJC26-AFIT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.73 | 1.00 | 8,094 | no |
| 298 | medium | `KXPGATOP10-FESJC26-MKIM` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.71 | 1.00 | 11,512 | no |
| 299 | medium | `KXPGATOP20-FESJC26-MMCC` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.70 | 1.00 | 8,231 | no |
| 300 | medium | `KXSB-27-NYG` | `KXSB-27` | `KXSB` | 14.69 | 1.00 | 139,917 | no |
| 301 | medium | `KXHEISMAN-27-LSELL` | `KXHEISMAN-27` | `KXHEISMAN` | 14.69 | 1.00 | 19,254 | no |
| 302 | medium | `KXMLB-26-ATL` | `KXMLB-26` | `KXMLB` | 14.68 | 1.00 | 824,476 | no |
| 303 | medium | `KXHEISMAN-27-DMENS` | `KXHEISMAN-27` | `KXHEISMAN` | 14.67 | 1.00 | 9,421 | no |
| 304 | medium | `KXNBA-27-MIN` | `KXNBA-27` | `KXNBA` | 14.66 | 1.00 | 1,029,256 | no |
| 305 | medium | `KXPGATOP20-FESJC26-XSCH` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.66 | 1.00 | 18,383 | no |
| 306 | medium | `KXPGATOP20-FESJC26-JBRI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.62 | 1.00 | 574,225 | no |
| 307 | medium | `KXPRESNOMR-28-MR` | `KXPRESNOMR-28` | `KXPRESNOMR` | 14.59 | 1.00 | 25,225 | no |
| 308 | medium | `KXPGATOP10-FESJC26-WKIM` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.58 | 1.00 | 389 | no |
| 309 | medium | `KXPRESNOMR-28-JT` | `KXPRESNOMR-28` | `KXPRESNOMR` | 14.56 | 1.00 | 225,086 | no |
| 310 | medium | `KXNASCARRACE-COOO81526-CAHO` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 14.55 | 1.00 | 226,936 | no |
| 311 | medium | `KXNCAAF-27-TXAM` | `KXNCAAF-27` | `KXNCAAF` | 14.54 | 1.00 | 13,043 | no |
| 312 | medium | `KXPGATOP20-FESJC26-MSCH` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.53 | 1.00 | 16,637 | no |
| 313 | medium | `KXPGATOUR-FESJC26-JKNA` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.52 | 1.00 | 23,509 | no |
| 314 | medium | `KXPGATOP10-FESJC26-ARAI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.50 | 1.00 | 15,058 | no |
| 315 | medium | `KXSB-27-IND` | `KXSB-27` | `KXSB` | 14.48 | 1.00 | 77,514 | no |
| 316 | medium | `KXHEISMAN-27-RBEC` | `KXHEISMAN-27` | `KXHEISMAN` | 14.48 | 1.00 | 37,045 | no |
| 317 | medium | `KXPGATOP10-FESJC26-JKNA` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.48 | 1.00 | 9,965 | no |
| 318 | medium | `KXPGATOP10-FESJC26-ANOR` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.46 | 1.00 | 13,115 | no |
| 319 | medium | `KXPGATOP10-FESJC26-WCLA` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.46 | 1.00 | 4,348 | no |
| 320 | medium | `KXNASCARRACE-COOO81526-CHEL` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 14.44 | 1.00 | 108,007 | no |
| 321 | medium | `KXHEISMAN-27-WHAM` | `KXHEISMAN-27` | `KXHEISMAN` | 14.44 | 1.00 | 39,157 | no |
| 322 | medium | `KXMLB-26-CWS` | `KXMLB-26` | `KXMLB` | 14.42 | 1.00 | 7,269,923 | no |
| 323 | medium | `KXPGATOP20-FESJC26-JTHO` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.40 | 1.00 | 8,093 | no |
| 324 | medium | `KXPGATOP10-FESJC26-BHAR` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.40 | 1.00 | 18,691 | no |
| 325 | medium | `KXPGATOP20-FESJC26-ASMA` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.39 | 1.00 | 8,146 | no |
| 326 | medium | `KXPGATOUR-FESJC26-WKIM` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.38 | 1.00 | 23,122 | no |
| 327 | medium | `KXHEISMAN-27-JMAT` | `KXHEISMAN-27` | `KXHEISMAN` | 14.37 | 1.00 | 9,384 | no |
| 328 | medium | `KXNASCARRACE-COOO81526-BRKE` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 14.36 | 1.00 | 113,852 | no |
| 329 | medium | `KXMLB-26-NYM` | `KXMLB-26` | `KXMLB` | 14.33 | 1.00 | 23,440 | no |
| 330 | medium | `KXMLB-26-NYY` | `KXMLB-26` | `KXMLB` | 14.32 | 1.00 | 1,613,586 | no |
| 331 | medium | `KXLIGAMXGAME-26AUG15ATLTIG-TIE` | `KXLIGAMXGAME-26AUG15ATLTIG` | `KXLIGAMXGAME` | 14.31 | 1.00 | 759 | no |
| 332 | medium | `KXPGATOP10-FESJC26-ASCO` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.29 | 1.00 | 6,333 | no |
| 333 | medium | `KXPGATOP20-FESJC26-SSCH` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.27 | 1.00 | 23,101 | no |
| 334 | medium | `KXPGATOUR-FESJC26-JKOI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.27 | 1.00 | 23,467 | no |
| 335 | medium | `KXNASCARRACE-COOO81526-CHBE` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 14.26 | 1.00 | 50,507 | no |
| 336 | medium | `KXPGATOP20-FESJC26-RFOW` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.26 | 1.00 | 99,184 | no |
| 337 | medium | `KXNBA-27-DEN` | `KXNBA-27` | `KXNBA` | 14.26 | 1.00 | 17,764 | no |
| 338 | medium | `KXMLB-26-KC` | `KXMLB-26` | `KXMLB` | 14.24 | 1.00 | 23,457 | no |
| 339 | medium | `KXPRESNOMR-28-ETRU` | `KXPRESNOMR-28` | `KXPRESNOMR` | 14.24 | 1.00 | 116,748 | no |
| 340 | medium | `KXPGATOP20-FESJC26-RFOX` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.22 | 1.00 | 8,117 | no |
| 341 | medium | `KXPGATOP10-FESJC26-JBRI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.21 | 1.00 | 470 | no |
| 342 | medium | `KXNBA-27-DET` | `KXNBA-27` | `KXNBA` | 14.21 | 1.00 | 17,928 | no |
| 343 | medium | `KXHEISMAN-27-DMES` | `KXHEISMAN-27` | `KXHEISMAN` | 14.19 | 1.00 | 29,835 | no |
| 344 | medium | `KXNASCARRACE-COOO81526-RYPR` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 14.18 | 1.00 | 362,489 | no |
| 345 | medium | `KXPGATOP10-FESJC26-NTAY` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 14.15 | 1.00 | 479 | no |
| 346 | medium | `KXPRESNOMR-28-EKIR` | `KXPRESNOMR-28` | `KXPRESNOMR` | 14.13 | 1.00 | 5,960 | no |
| 347 | medium | `KXPGATOUR-FESJC26-KMIT` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.09 | 1.00 | 23,442 | no |
| 348 | medium | `KXPRESNOMR-28-RDS` | `KXPRESNOMR-28` | `KXPRESNOMR` | 14.09 | 1.00 | 5,925 | no |
| 349 | medium | `KXPGATOP20-FESJC26-HMAT` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 14.08 | 1.00 | 20,869 | no |
| 350 | medium | `KXPGATOUR-FESJC26-MTHO` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 14.06 | 1.00 | 23,374 | no |
| 351 | medium | `KXGOVFLNOMR-26-BD` | `KXGOVFLNOMR-26` | `KXGOVFLNOMR` | 14.02 | 1.00 | 42,981 | no |
| 352 | medium | `KXPRESNOMR-28-KNOE` | `KXPRESNOMR-28` | `KXPRESNOMR` | 14.00 | 1.00 | 5,855 | no |
| 353 | medium | `KXPGATOP20-FESJC26-SSTE` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.98 | 1.00 | 13,222 | no |
| 354 | medium | `KXPGATOP20-FESJC26-JPOS` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.97 | 1.00 | 16,125 | no |
| 355 | medium | `KXHEISMAN-27-BUNDE` | `KXHEISMAN-27` | `KXHEISMAN` | 13.92 | 1.00 | 111,392 | no |
| 356 | medium | `KXPGATOP20-FESJC26-TKIM` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.86 | 1.00 | 95,305 | no |
| 357 | medium | `KXFEDDECISION-26SEP-H26` | `KXFEDDECISION-26SEP` | `KXFEDDECISION` | 13.81 | 1.00 | 11,941 | no |
| 358 | medium | `KXHEISMAN-27-AMANN` | `KXHEISMAN-27` | `KXHEISMAN` | 13.81 | 1.00 | 9,446 | no |
| 359 | medium | `KXTESTMATCH-26AUG150030INDSRI-SRI` | `KXTESTMATCH-26AUG150030INDSRI` | `KXTESTMATCH` | 13.81 | 1.00 | 295,041 | no |
| 360 | medium | `KXPGATOP10-FESJC26-MTHO` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 13.81 | 1.00 | 5,343 | no |
| 361 | medium | `KXNCAAF-27-MIA` | `KXNCAAF-27` | `KXNCAAF` | 13.80 | 1.00 | 12,895 | no |
| 362 | medium | `KXUFCMOV-26AUG15MAKMGI-DRAW` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 13.79 | 1.00 | 71,112 | no |
| 363 | medium | `KXPGATOP20-FESJC26-BHAR` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.78 | 1.00 | 33,387 | no |
| 364 | medium | `KXHEISMAN-27-KRUS` | `KXHEISMAN-27` | `KXHEISMAN` | 13.76 | 1.00 | 81,317 | no |
| 365 | medium | `KXMLB-26-ATH` | `KXMLB-26` | `KXMLB` | 13.74 | 1.00 | 23,348 | no |
| 366 | medium | `KXPGATOUR-FESJC26-NTAY` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 13.71 | 1.00 | 22,910 | no |
| 367 | medium | `KXNBA-27-SAS` | `KXNBA-27` | `KXNBA` | 13.69 | 1.00 | 429,563 | no |
| 368 | medium | `KXPGATOUR-FESJC26-LABE` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 13.68 | 1.00 | 23,127 | no |
| 369 | medium | `KXHEISMAN-27-MREED` | `KXHEISMAN-27` | `KXHEISMAN` | 13.68 | 1.00 | 9,453 | no |
| 370 | medium | `KXMLB-26-DET` | `KXMLB-26` | `KXMLB` | 13.65 | 1.00 | 3,052,425 | no |
| 371 | medium | `KXPGATOP20-FESJC26-NECH` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.65 | 1.00 | 80,798 | no |
| 372 | medium | `KXPGATOUR-FESJC26-CAME` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 13.64 | 1.00 | 23,281 | no |
| 373 | medium | `KXPGATOP10-FESJC26-ABHA` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 13.64 | 1.00 | 5,625 | no |
| 374 | medium | `KXUFCMOV-26AUG15MAKMGI-MAKKOTKODQ` | `KXUFCMOV-26AUG15MAKMGI` | `KXUFCMOV` | 13.62 | 1.00 | 120,451 | no |
| 375 | medium | `KXPGATOUR-FESJC26-BHAR` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 13.59 | 1.00 | 8,211,362 | no |
| 376 | medium | `KXPGATOUR-FESJC26-JTHO` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 13.59 | 1.00 | 14,551,018 | no |
| 377 | medium | `KXHEISMAN-27-BBRO` | `KXHEISMAN-27` | `KXHEISMAN` | 13.56 | 1.00 | 35,935 | no |
| 378 | medium | `KXMLB-26-BAL` | `KXMLB-26` | `KXMLB` | 13.50 | 1.00 | 684,084 | no |
| 379 | medium | `KXPGATOP10-FESJC26-VHOV` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 13.49 | 1.00 | 9,213 | no |
| 380 | medium | `KXPRESNOMR-28-KB` | `KXPRESNOMR-28` | `KXPRESNOMR` | 13.45 | 1.00 | 5,696 | no |
| 381 | medium | `KXPGATOP20-FESJC26-SLOW` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.42 | 1.00 | 8,117 | no |
| 382 | medium | `KXNCAAF-27-PSU` | `KXNCAAF-27` | `KXNCAAF` | 13.37 | 1.00 | 184,734 | no |
| 383 | medium | `KXPGATOP10-FESJC26-STHE` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | 13.27 | 1.00 | 33,450 | no |
| 384 | medium | `KXPGATOP20-FESJC26-PCAN` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.23 | 1.00 | 79,189 | no |
| 385 | medium | `KXPGATOP20-FESJC26-JSPI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.11 | 1.00 | 8,156 | no |
| 386 | medium | `KXPGATOP20-FESJC26-VHOV` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | 13.03 | 1.00 | 65,724 | no |
| 387 | medium | `KXMLB-26-SEA` | `KXMLB-26` | `KXMLB` | 12.99 | 1.00 | 1,711,598 | no |
| 388 | medium | `KXNASCARRACE-COOO81526-ALBO` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 12.96 | 1.00 | 89,721 | no |
| 389 | low | `KXPGATOUR-FESJC26-JSMI` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 12.74 | 1.00 | 23,176 | no |
| 390 | low | `KXNCAAF-27-OKLA` | `KXNCAAF-27` | `KXNCAAF` | 12.21 | 1.00 | 24,148 | **YES** |
| 391 | low | `KXPRESNOMR-28-TCOT` | `KXPRESNOMR-28` | `KXPRESNOMR` | 11.95 | 1.00 | 116,953 | **YES** |
| 392 | low | `KXNASCARRACE-COOO81526-TYGI` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | 11.94 | 1.00 | 34,337 | **YES** |
| 393 | low | `KXPGATOUR-FESJC26-CGOT` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | 11.79 | 1.00 | 1,864,916 | no |
| 394 | low | `KXMAXSHARDINGTEST-26AUG2818-T68399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 6.05 | 0.33 | 4,467 | **YES** |
| 395 | low | `KXMAXSHARDINGTEST-26AUG2818-T56399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.40 | 0.67 | 4,466 | no |
| 396 | low | `KXMAXSHARDINGTEST-26AUG2818-T65399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.40 | 0.33 | 4,483 | no |
| 397 | low | `KXMAXSHARDINGTEST-26AUG2818-T69899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.26 | 0.33 | 4,362 | no |
| 398 | low | `KXMAXSHARDINGTEST-26AUG2818-T62899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.13 | 0.33 | 4,394 | no |
| 399 | low | `KXMAXSHARDINGTEST-26AUG2818-T67899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.13 | 0.67 | 4,598 | no |
| 400 | low | `KXMAXSHARDINGTEST-26AUG2818-T55599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.00 | 0.33 | 4,550 | no |
| 401 | low | `KXMAXSHARDINGTEST-26AUG2818-T63699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.00 | 0.67 | 4,426 | no |
| 402 | low | `KXMAXSHARDINGTEST-26AUG2818-T64299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 5.00 | 1.00 | 4,321 | no |
| 403 | low | `KXMAXSHARDINGTEST-26AUG2818-T61199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.87 | 0.33 | 4,483 | no |
| 404 | low | `KXMAXSHARDINGTEST-26AUG2818-T65899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.87 | 0.67 | 4,428 | no |
| 405 | low | `KXMAXSHARDINGTEST-26AUG2818-T56099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.74 | 0.67 | 4,349 | no |
| 406 | low | `KXMAXSHARDINGTEST-26AUG2818-T70099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.74 | 0.67 | 4,308 | no |
| 407 | low | `KXMAXSHARDINGTEST-26AUG2818-T72499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.74 | 0.67 | 4,381 | no |
| 408 | low | `KXMAXSHARDINGTEST-26AUG2818-T57599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.61 | 1.00 | 4,508 | no |
| 409 | low | `KXMAXSHARDINGTEST-26AUG2818-T58799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.61 | 1.00 | 4,591 | no |
| 410 | low | `KXMAXSHARDINGTEST-26AUG2818-T60499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.61 | 0.33 | 4,402 | no |
| 411 | low | `KXMAXSHARDINGTEST-26AUG2818-T73699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.61 | 0.33 | 4,502 | no |
| 412 | low | `KXMAXSHARDINGTEST-26AUG2818-T59599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.47 | 0.33 | 4,474 | no |
| 413 | low | `KXMAXSHARDINGTEST-26AUG2818-T58199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 0.67 | 4,429 | no |
| 414 | low | `KXMAXSHARDINGTEST-26AUG2818-T58999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 0.33 | 4,237 | no |
| 415 | low | `KXMAXSHARDINGTEST-26AUG2818-T60799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 0.33 | 4,536 | no |
| 416 | low | `KXMAXSHARDINGTEST-26AUG2818-T62699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 0.67 | 4,541 | no |
| 417 | low | `KXMAXSHARDINGTEST-26AUG2818-T63199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 0.67 | 4,379 | no |
| 418 | low | `KXMAXSHARDINGTEST-26AUG2818-T68299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 1.00 | 4,600 | no |
| 419 | low | `KXMAXSHARDINGTEST-26AUG2818-T68999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 0.67 | 4,492 | no |
| 420 | low | `KXMAXSHARDINGTEST-26AUG2818-T69299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.34 | 1.00 | 4,352 | no |
| 421 | low | `KXMAXSHARDINGTEST-26AUG2818-T57299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.21 | 0.67 | 4,426 | no |
| 422 | low | `KXMAXSHARDINGTEST-26AUG2818-T71699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.21 | 1.00 | 4,325 | no |
| 423 | low | `KXMAXSHARDINGTEST-26AUG2818-T55199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.33 | 4,598 | no |
| 424 | low | `KXMAXSHARDINGTEST-26AUG2818-T56199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 1.00 | 4,454 | no |
| 425 | low | `KXMAXSHARDINGTEST-26AUG2818-T56799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 1.00 | 4,463 | no |
| 426 | low | `KXMAXSHARDINGTEST-26AUG2818-T57899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.33 | 4,352 | no |
| 427 | low | `KXMAXSHARDINGTEST-26AUG2818-T58099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.67 | 4,435 | no |
| 428 | low | `KXMAXSHARDINGTEST-26AUG2818-T61899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.67 | 4,453 | no |
| 429 | low | `KXMAXSHARDINGTEST-26AUG2818-T63299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.33 | 4,446 | no |
| 430 | low | `KXMAXSHARDINGTEST-26AUG2818-T63399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.33 | 4,423 | no |
| 431 | low | `KXMAXSHARDINGTEST-26AUG2818-T64399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.33 | 4,512 | no |
| 432 | low | `KXMAXSHARDINGTEST-26AUG2818-T65699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.33 | 4,419 | no |
| 433 | low | `KXMAXSHARDINGTEST-26AUG2818-T68799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.67 | 4,352 | no |
| 434 | low | `KXMAXSHARDINGTEST-26AUG2818-T72099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 4.08 | 0.67 | 4,246 | no |
| 435 | low | `KXMAXSHARDINGTEST-26AUG2818-T56499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.95 | 0.33 | 4,654 | no |
| 436 | low | `KXMAXSHARDINGTEST-26AUG2818-T57399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.95 | 1.00 | 4,379 | no |
| 437 | low | `KXMAXSHARDINGTEST-26AUG2818-T62299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.95 | 0.67 | 4,501 | no |
| 438 | low | `KXMAXSHARDINGTEST-26AUG2818-T64499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.95 | 0.33 | 4,497 | no |
| 439 | low | `KXMAXSHARDINGTEST-26AUG2818-T66299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.95 | 0.33 | 4,438 | no |
| 440 | low | `KXMAXSHARDINGTEST-26AUG2818-T55099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.67 | 4,301 | no |
| 441 | low | `KXMAXSHARDINGTEST-26AUG2818-T60899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 1.00 | 4,284 | no |
| 442 | low | `KXMAXSHARDINGTEST-26AUG2818-T60999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.33 | 4,358 | no |
| 443 | low | `KXMAXSHARDINGTEST-26AUG2818-T61099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.33 | 4,311 | no |
| 444 | low | `KXMAXSHARDINGTEST-26AUG2818-T63899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.33 | 4,413 | no |
| 445 | low | `KXMAXSHARDINGTEST-26AUG2818-T63999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.67 | 4,344 | no |
| 446 | low | `KXMAXSHARDINGTEST-26AUG2818-T65499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.33 | 4,508 | no |
| 447 | low | `KXMAXSHARDINGTEST-26AUG2818-T65599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.67 | 4,473 | no |
| 448 | low | `KXMAXSHARDINGTEST-26AUG2818-T68599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.33 | 4,330 | no |
| 449 | low | `KXMAXSHARDINGTEST-26AUG2818-T69599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.67 | 4,604 | no |
| 450 | low | `KXMAXSHARDINGTEST-26AUG2818-T69699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 1.00 | 4,375 | no |
| 451 | low | `KXMAXSHARDINGTEST-26AUG2818-T70299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 0.67 | 4,337 | no |
| 452 | low | `KXMAXSHARDINGTEST-26AUG2818-T71099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.82 | 1.00 | 4,314 | no |
| 453 | low | `KXMAXSHARDINGTEST-26AUG2818-T66799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.68 | 1.00 | 4,510 | no |
| 454 | low | `KXMAXSHARDINGTEST-26AUG2818-T68699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.68 | 0.33 | 4,374 | no |
| 455 | low | `KXMAXSHARDINGTEST-26AUG2818-T70899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.68 | 0.33 | 4,490 | no |
| 456 | low | `KXMAXSHARDINGTEST-26AUG2818-T72399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.68 | 0.33 | 4,440 | no |
| 457 | low | `KXMAXSHARDINGTEST-26AUG2818-T72699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.68 | 0.67 | 4,377 | no |
| 458 | low | `KXMAXSHARDINGTEST-26AUG2818-T56599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 1.00 | 4,517 | no |
| 459 | low | `KXMAXSHARDINGTEST-26AUG2818-T57199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 1.00 | 4,393 | no |
| 460 | low | `KXMAXSHARDINGTEST-26AUG2818-T58599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.33 | 4,442 | no |
| 461 | low | `KXMAXSHARDINGTEST-26AUG2818-T59699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.33 | 4,458 | no |
| 462 | low | `KXMAXSHARDINGTEST-26AUG2818-T60699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.67 | 4,452 | no |
| 463 | low | `KXMAXSHARDINGTEST-26AUG2818-T62099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.33 | 4,364 | no |
| 464 | low | `KXMAXSHARDINGTEST-26AUG2818-T66199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.67 | 4,445 | no |
| 465 | low | `KXMAXSHARDINGTEST-26AUG2818-T66699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.67 | 4,541 | no |
| 466 | low | `KXMAXSHARDINGTEST-26AUG2818-T69499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.67 | 4,427 | no |
| 467 | low | `KXMAXSHARDINGTEST-26AUG2818-T73799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.55 | 0.67 | 4,444 | no |
| 468 | low | `KXMAXSHARDINGTEST-26AUG2818-T57799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 1.00 | 4,551 | no |
| 469 | low | `KXMAXSHARDINGTEST-26AUG2818-T59499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 1.00 | 4,579 | no |
| 470 | low | `KXMAXSHARDINGTEST-26AUG2818-T59999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.67 | 4,420 | no |
| 471 | low | `KXMAXSHARDINGTEST-26AUG2818-T62599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.67 | 4,452 | no |
| 472 | low | `KXMAXSHARDINGTEST-26AUG2818-T63099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.67 | 4,394 | no |
| 473 | low | `KXMAXSHARDINGTEST-26AUG2818-T67799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.67 | 4,363 | no |
| 474 | low | `KXMAXSHARDINGTEST-26AUG2818-T68499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.67 | 4,470 | no |
| 475 | low | `KXMAXSHARDINGTEST-26AUG2818-T69399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.33 | 4,429 | no |
| 476 | low | `KXMAXSHARDINGTEST-26AUG2818-T73399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.42 | 0.67 | 4,329 | no |
| 477 | low | `KXMAXSHARDINGTEST-26AUG2818-T57999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.67 | 4,527 | no |
| 478 | low | `KXMAXSHARDINGTEST-26AUG2818-T59399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 1.00 | 4,487 | no |
| 479 | low | `KXMAXSHARDINGTEST-26AUG2818-T60199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.67 | 4,499 | no |
| 480 | low | `KXMAXSHARDINGTEST-26AUG2818-T61299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.67 | 4,407 | no |
| 481 | low | `KXMAXSHARDINGTEST-26AUG2818-T62199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.67 | 4,381 | no |
| 482 | low | `KXMAXSHARDINGTEST-26AUG2818-T67599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 1.00 | 4,510 | no |
| 483 | low | `KXMAXSHARDINGTEST-26AUG2818-T68199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 1.00 | 4,332 | no |
| 484 | low | `KXMAXSHARDINGTEST-26AUG2818-T70499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.33 | 4,453 | no |
| 485 | low | `KXMAXSHARDINGTEST-26AUG2818-T70799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.33 | 4,543 | no |
| 486 | low | `KXMAXSHARDINGTEST-26AUG2818-T72899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.29 | 0.33 | 4,376 | no |
| 487 | low | `KXMAXSHARDINGTEST-26AUG2818-T55499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 1.00 | 4,585 | no |
| 488 | low | `KXMAXSHARDINGTEST-26AUG2818-T56299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 1.00 | 4,516 | no |
| 489 | low | `KXMAXSHARDINGTEST-26AUG2818-T56899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 0.67 | 4,453 | no |
| 490 | low | `KXMAXSHARDINGTEST-26AUG2818-T64199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 1.00 | 4,531 | no |
| 491 | low | `KXMAXSHARDINGTEST-26AUG2818-T64799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 1.00 | 4,574 | no |
| 492 | low | `KXMAXSHARDINGTEST-26AUG2818-T67499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 1.00 | 4,360 | no |
| 493 | low | `KXMAXSHARDINGTEST-26AUG2818-T69099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 0.33 | 4,280 | no |
| 494 | low | `KXMAXSHARDINGTEST-26AUG2818-T69999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 0.33 | 4,364 | no |
| 495 | low | `KXMAXSHARDINGTEST-26AUG2818-T70399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 0.33 | 4,478 | no |
| 496 | low | `KXMAXSHARDINGTEST-26AUG2818-T70999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.16 | 0.67 | 4,438 | no |
| 497 | low | `KXMAXSHARDINGTEST-26AUG2818-T55899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 1.00 | 4,470 | no |
| 498 | low | `KXMAXSHARDINGTEST-26AUG2818-T55999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 1.00 | 4,431 | no |
| 499 | low | `KXMAXSHARDINGTEST-26AUG2818-T56699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 1.00 | 4,476 | no |
| 500 | low | `KXMAXSHARDINGTEST-26AUG2818-T57499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 1.00 | 4,503 | no |
| 501 | low | `KXMAXSHARDINGTEST-26AUG2818-T57699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 1.00 | 4,582 | no |
| 502 | low | `KXMAXSHARDINGTEST-26AUG2818-T58899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 0.33 | 4,528 | no |
| 503 | low | `KXMAXSHARDINGTEST-26AUG2818-T59199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 0.67 | 4,614 | no |
| 504 | low | `KXMAXSHARDINGTEST-26AUG2818-T62499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 0.33 | 4,472 | no |
| 505 | low | `KXMAXSHARDINGTEST-26AUG2818-T71899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 0.67 | 4,398 | no |
| 506 | low | `KXMAXSHARDINGTEST-26AUG2818-T73199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 1.00 | 4,427 | no |
| 507 | low | `KXMAXSHARDINGTEST-26AUG2818-T73299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 3.03 | 0.67 | 4,189 | no |
| 508 | low | `KXMAXSHARDINGTEST-26AUG2818-T60399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 0.33 | 4,306 | no |
| 509 | low | `KXMAXSHARDINGTEST-26AUG2818-T61599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 1.00 | 4,423 | no |
| 510 | low | `KXMAXSHARDINGTEST-26AUG2818-T65299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 0.67 | 4,403 | no |
| 511 | low | `KXMAXSHARDINGTEST-26AUG2818-T67199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 0.67 | 4,456 | no |
| 512 | low | `KXMAXSHARDINGTEST-26AUG2818-T67399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 0.67 | 4,544 | no |
| 513 | low | `KXMAXSHARDINGTEST-26AUG2818-T72599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 0.33 | 4,481 | no |
| 514 | low | `KXMAXSHARDINGTEST-26AUG2818-T73099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.90 | 0.67 | 4,585 | no |
| 515 | low | `KXMAXSHARDINGTEST-26AUG2818-T60099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.67 | 4,251 | no |
| 516 | low | `KXMAXSHARDINGTEST-26AUG2818-T61699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.33 | 4,436 | no |
| 517 | low | `KXMAXSHARDINGTEST-26AUG2818-T61999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.33 | 4,543 | no |
| 518 | low | `KXMAXSHARDINGTEST-26AUG2818-T62799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.67 | 4,504 | no |
| 519 | low | `KXMAXSHARDINGTEST-26AUG2818-T63499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.33 | 4,489 | no |
| 520 | low | `KXMAXSHARDINGTEST-26AUG2818-T64999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.67 | 4,629 | no |
| 521 | low | `KXMAXSHARDINGTEST-26AUG2818-T65099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.67 | 4,560 | no |
| 522 | low | `KXMAXSHARDINGTEST-26AUG2818-T68099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.67 | 4,472 | no |
| 523 | low | `KXMAXSHARDINGTEST-26AUG2818-T71199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.33 | 4,479 | no |
| 524 | low | `KXMAXSHARDINGTEST-26AUG2818-T72799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.76 | 0.33 | 4,531 | no |
| 525 | low | `KXMAXSHARDINGTEST-26AUG2818-T60299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.33 | 4,514 | no |
| 526 | low | `KXMAXSHARDINGTEST-26AUG2818-T61799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.67 | 4,432 | no |
| 527 | low | `KXMAXSHARDINGTEST-26AUG2818-T62999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.67 | 4,469 | no |
| 528 | low | `KXMAXSHARDINGTEST-26AUG2818-T66499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.67 | 4,404 | no |
| 529 | low | `KXMAXSHARDINGTEST-26AUG2818-T66999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 1.00 | 4,509 | no |
| 530 | low | `KXMAXSHARDINGTEST-26AUG2818-T69799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.33 | 4,350 | no |
| 531 | low | `KXMAXSHARDINGTEST-26AUG2818-T71799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.33 | 4,406 | no |
| 532 | low | `KXMAXSHARDINGTEST-26AUG2818-T71999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.63 | 0.67 | 4,524 | no |
| 533 | low | `KXMAXSHARDINGTEST-26AUG2818-T55299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 1.00 | 4,290 | no |
| 534 | low | `KXMAXSHARDINGTEST-26AUG2818-T56999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.67 | 4,485 | no |
| 535 | low | `KXMAXSHARDINGTEST-26AUG2818-T57099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 1.00 | 4,576 | no |
| 536 | low | `KXMAXSHARDINGTEST-26AUG2818-T58399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.67 | 4,402 | no |
| 537 | low | `KXMAXSHARDINGTEST-26AUG2818-T61399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 1.00 | 4,286 | no |
| 538 | low | `KXMAXSHARDINGTEST-26AUG2818-T67099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.67 | 4,403 | no |
| 539 | low | `KXMAXSHARDINGTEST-26AUG2818-T67999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.67 | 4,415 | no |
| 540 | low | `KXMAXSHARDINGTEST-26AUG2818-T71299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.67 | 4,373 | no |
| 541 | low | `KXMAXSHARDINGTEST-26AUG2818-T72199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.67 | 4,479 | no |
| 542 | low | `KXMAXSHARDINGTEST-26AUG2818-T73599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.50 | 0.33 | 4,343 | no |
| 543 | low | `KXMAXSHARDINGTEST-26AUG2818-T59899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.37 | 0.67 | 4,460 | no |
| 544 | low | `KXMAXSHARDINGTEST-26AUG2818-T67699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.37 | 0.33 | 4,337 | no |
| 545 | low | `KXMAXSHARDINGTEST-26AUG2818-T68899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.37 | 0.67 | 4,529 | no |
| 546 | low | `KXMAXSHARDINGTEST-26AUG2818-T70699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.37 | 0.67 | 4,399 | no |
| 547 | low | `KXMAXSHARDINGTEST-26AUG2818-T71399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.37 | 0.67 | 4,348 | no |
| 548 | low | `KXMAXSHARDINGTEST-26AUG2818-T72999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.37 | 0.33 | 4,488 | no |
| 549 | low | `KXMAXSHARDINGTEST-26AUG2818-T55399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 1.00 | 4,432 | no |
| 550 | low | `KXMAXSHARDINGTEST-26AUG2818-T55699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,459 | no |
| 551 | low | `KXMAXSHARDINGTEST-26AUG2818-T58299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.33 | 4,472 | no |
| 552 | low | `KXMAXSHARDINGTEST-26AUG2818-T58499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.33 | 4,534 | no |
| 553 | low | `KXMAXSHARDINGTEST-26AUG2818-T59099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,668 | no |
| 554 | low | `KXMAXSHARDINGTEST-26AUG2818-T60599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,453 | no |
| 555 | low | `KXMAXSHARDINGTEST-26AUG2818-T63799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,469 | no |
| 556 | low | `KXMAXSHARDINGTEST-26AUG2818-T64699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,507 | no |
| 557 | low | `KXMAXSHARDINGTEST-26AUG2818-T66099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,377 | no |
| 558 | low | `KXMAXSHARDINGTEST-26AUG2818-T66899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.33 | 4,272 | no |
| 559 | low | `KXMAXSHARDINGTEST-26AUG2818-T71499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.24 | 0.67 | 4,420 | no |
| 560 | low | `KXMAXSHARDINGTEST-26AUG2818-T63599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.67 | 4,287 | no |
| 561 | low | `KXMAXSHARDINGTEST-26AUG2818-T64899.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.67 | 4,570 | no |
| 562 | low | `KXMAXSHARDINGTEST-26AUG2818-T65199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.67 | 4,437 | no |
| 563 | low | `KXMAXSHARDINGTEST-26AUG2818-T65799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.33 | 4,461 | no |
| 564 | low | `KXMAXSHARDINGTEST-26AUG2818-T66399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.67 | 4,441 | no |
| 565 | low | `KXMAXSHARDINGTEST-26AUG2818-T66599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.33 | 4,454 | no |
| 566 | low | `KXMAXSHARDINGTEST-26AUG2818-T69199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.67 | 4,495 | no |
| 567 | low | `KXMAXSHARDINGTEST-26AUG2818-T70599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.33 | 4,359 | no |
| 568 | low | `KXMAXSHARDINGTEST-26AUG2818-T72299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 2.11 | 0.33 | 4,420 | no |
| 569 | low | `KXMAXSHARDINGTEST-26AUG2818-T59299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.97 | 0.67 | 4,418 | no |
| 570 | low | `KXMAXSHARDINGTEST-26AUG2818-T59799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.97 | 1.00 | 4,352 | no |
| 571 | low | `KXMAXSHARDINGTEST-26AUG2818-T58699.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.84 | 1.00 | 4,386 | no |
| 572 | low | `KXMAXSHARDINGTEST-26AUG2818-T62399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.84 | 0.33 | 4,547 | no |
| 573 | low | `KXMAXSHARDINGTEST-26AUG2818-T71599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.84 | 1.00 | 4,519 | no |
| 574 | low | `KXMAXSHARDINGTEST-26AUG2818-T64599.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.71 | 1.00 | 4,578 | no |
| 575 | low | `KXMAXSHARDINGTEST-26AUG2818-T65999.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.71 | 0.33 | 4,345 | no |
| 576 | low | `KXMAXSHARDINGTEST-26AUG2818-T67299.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.71 | 0.67 | 4,371 | no |
| 577 | low | `KXMAXSHARDINGTEST-26AUG2818-T55799.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.58 | 1.00 | 4,338 | no |
| 578 | low | `KXMAXSHARDINGTEST-26AUG2818-T61499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.45 | 0.67 | 4,418 | no |
| 579 | low | `KXMAXSHARDINGTEST-26AUG2818-T64099.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.32 | 0.67 | 4,364 | no |
| 580 | low | `KXMAXSHARDINGTEST-26AUG2818-T73499.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.32 | 0.67 | 4,342 | no |
| 581 | low | `KXMAXSHARDINGTEST-26AUG2818-T70199.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | 1.18 | 0.33 | 4,448 | no |
| 582 | low | `KXBRASILEIRO1H-26AUG16CHABAH-CHA` | `KXBRASILEIRO1H-26AUG16CHABAH` | `KXBRASILEIRO1H` | 0.26 | 0.33 | 2 | no |

### 7b. The 250 highest-statistic REJECTED markets

These are the markets a naive 'take the top 12 by volume' rule would have selected. Their rejection reasons are the finding.

| ticker | event | 24h vol (screen) | contracts/min | bid | ask | rejected because |
|---|---|---:|---:|---:|---:|---|
| `KXPRESNOMD-28-JSTE` | `KXPRESNOMD-28` | 37,256,324 | not probed | 0.199 | 0.023 | crossed_book, not_probed |
| `KXPRESNOMD-28-HBID` | `KXPRESNOMD-28` | 27,689,973 | not probed | 0.018 | 0.006 | crossed_book, not_probed |
| `KXPRESNOMD-28-MO` | `KXPRESNOMD-28` | 20,687,736 | not probed | 0.016 | 0.012 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-RW` | `KXPRESNOMD-28` | 19,638,683 | not probed | 0.008 | 0.005 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-JTAL` | `KXPRESNOMD-28` | 10,043,906 | not probed | 0.2 | 0.014 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-MK` | `KXPRESNOMD-28` | 9,201,816 | not probed | 0.055 | 0.034 | crossed_book, not_probed |
| `KXNASCARCUPSERIES-NCS26-JLOG` | `KXNASCARCUPSERIES-NCS26` | 8,299,999 | 0.00 | 0.03 | 0.031 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-PB` | `KXPRESNOMD-28` | 5,990,323 | not probed | 0.054 | 0.054 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-AB` | `KXPRESNOMD-28` | 3,996,450 | not probed | 0.037 | 0.036 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXNASCARCUPSERIES-NCS26-CBRI` | `KXNASCARCUPSERIES-NCS26` | 3,855,145 | 0.00 | 0.049 | 0.052 | no_measured_trading_during_probe |
| `KXECONSTATCPIYOY-26AUG-T3.4` | `KXECONSTATCPIYOY-26AUG` | 2,337,729 | 0.00 | 0.36 | 0.38 | no_measured_trading_during_probe |
| `KXNASCARCUPSERIES-NCS26-CBEL` | `KXNASCARCUPSERIES-NCS26` | 2,314,288 | 0.00 | 0.07 | 0.072 | no_measured_trading_during_probe |
| `KXPRESPERSON-28-TGAB` | `KXPRESPERSON-28` | 1,757,721 | 0.00 | 0.006 | 0.007 | no_measured_trading_during_probe |
| `KXNASCARCUPSERIES-NCS26-CELL` | `KXNASCARCUPSERIES-NCS26` | 1,633,528 | 0.00 | 0.04 | 0.041 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-GN` | `KXPRESNOMD-28` | 1,510,433 | not probed | 0.21 | 0.16 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXNASCARCUPSERIES-NCS26-TRED` | `KXNASCARCUPSERIES-NCS26` | 1,503,946 | 0.00 | 0.07 | 0.08 | no_measured_trading_during_probe |
| `KXNASCARCUPSERIES-NCS26-TGIB` | `KXNASCARCUPSERIES-NCS26` | 1,306,969 | 0.00 | 0.08 | 0.081 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP10SFLAR-SF14` | `KXNFLSPREAD-26SEP10SFLAR` | 1,120,317 | 0.00 | 0.05 | 0.1 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP10SFLAR-LAR17` | `KXNFLSPREAD-26SEP10SFLAR` | 1,060,235 | 0.00 | 0.08 | 0.14 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-JF` | `KXPRESNOMD-28` | 1,022,202 | not probed | 0.002 | 0.003 | no_resting_size, negative_resting_size, not_probed |
| `KXNFLSPREAD-26SEP13ATLPIT-ATL11` | `KXNFLSPREAD-26SEP13ATLPIT` | 948,888 | 0.00 | 0.04 | 0.05 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-KH` | `KXPRESNOMD-28` | 872,947 | not probed | 0.088 | 0.077 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESPERSON-28-PBUT` | `KXPRESPERSON-28` | 820,122 | 0.00 | 0.029 | 0.03 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-RC` | `KXPRESNOMD-28` | 689,946 | not probed | 0.003 | 0.003 | crossed_book, not_probed |
| `KXPRESPERSON-28-JVAN` | `KXPRESPERSON-28` | 622,127 | 0.00 | 0.19 | 0.191 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13NYJTEN-TEN17` | `KXNFLSPREAD-26SEP13NYJTEN` | 592,161 | 0.00 | 0.07 | 0.09 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-JP` | `KXPRESNOMD-28` | 577,385 | not probed | 0.003 | 0.003 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-GR` | `KXPRESNOMD-28` | 572,648 | not probed | 0.002 | 0.003 | no_resting_size, negative_resting_size, not_probed |
| `KXNFLSPREAD-26SEP09NESEA-SEA18` | `KXNFLSPREAD-26SEP09NESEA` | 567,757 | 0.00 | 0.12 | 0.14 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13ARILAC-LAC28` | `KXNFLSPREAD-26SEP13ARILAC` | 544,351 | 0.00 | 0.08 | 0.09 | no_measured_trading_during_probe |
| `KXGOVAK-26-NDAH` | `KXGOVAK-26` | 533,333 | not probed | 0.0 | 0.005 | no_two_sided_quote, no_resting_size, not_probed |
| `KXNFLNFCCHAMP-27-TB` | `KXNFLNFCCHAMP-27` | 477,778 | 0.00 | 0.03 | 0.031 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-AYAN` | `KXPRESNOMD-28` | 467,673 | not probed | 0.003 | 0.003 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-BOBA` | `KXPRESNOMD-28` | 466,441 | not probed | 0.002 | 0.002 | crossed_book, no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-DJOH` | `KXPRESNOMD-28` | 466,194 | not probed | 0.003 | 0.003 | crossed_book, not_probed |
| `KXPRESNOMD-28-ZMAM` | `KXPRESNOMD-28` | 465,606 | not probed | 0.002 | 0.003 | no_resting_size, negative_resting_size, not_probed |
| `KXPRESPERSON-28-DTRU` | `KXPRESPERSON-28` | 419,674 | 0.00 | 0.03 | 0.031 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13NYJTEN-NYJ10` | `KXNFLSPREAD-26SEP13NYJTEN` | 384,454 | 0.00 | 0.13 | 0.14 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13BUFHOU-BUF11` | `KXNFLSPREAD-26SEP13BUFHOU` | 382,792 | 0.00 | 0.14 | 0.15 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13MIALV-LV18` | `KXNFLSPREAD-26SEP13MIALV` | 374,285 | 0.00 | 0.14 | 0.15 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13TBCIN-CIN17` | `KXNFLSPREAD-26SEP13TBCIN` | 365,079 | 0.00 | 0.14 | 0.15 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-TW` | `KXPRESNOMD-28` | 355,452 | not probed | 0.002 | 0.002 | crossed_book, not_probed |
| `KXPRESNOMD-28-HCLI` | `KXPRESNOMD-28` | 354,003 | not probed | 0.002 | 0.002 | crossed_book, not_probed |
| `KXNFLSPREAD-26SEP13WASPHI-WAS15` | `KXNFLSPREAD-26SEP13WASPHI` | 350,000 | 0.00 | 0.05 | 0.06 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13TBCIN-CIN18` | `KXNFLSPREAD-26SEP13TBCIN` | 348,523 | 0.00 | 0.1 | 0.11 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13BUFHOU-BUF17` | `KXNFLSPREAD-26SEP13BUFHOU` | 346,666 | 0.00 | 0.05 | 0.06 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13DALNYG-DAL15` | `KXNFLSPREAD-26SEP13DALNYG` | 321,212 | 0.00 | 0.1 | 0.11 | no_measured_trading_during_probe |
| `KXNFLTOTAL-26SEP13NYJTEN-57` | `KXNFLTOTAL-26SEP13NYJTEN` | 300,000 | 0.00 | 0.07 | 0.08 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13GBMIN-GB11` | `KXNFLSPREAD-26SEP13GBMIN` | 293,174 | 0.00 | 0.12 | 0.13 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP10SFLAR-SF6` | `KXNFLSPREAD-26SEP10SFLAR` | 273,039 | 0.00 | 0.15 | 0.16 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13WASPHI-WAS11` | `KXNFLSPREAD-26SEP13WASPHI` | 265,555 | 0.00 | 0.05 | 0.06 | no_measured_trading_during_probe |
| `KXPAYROLLS-26AUG-T70000` | `KXPAYROLLS-26AUG` | 259,874 | 0.00 | 0.38 | 0.39 | no_measured_trading_during_probe |
| `KXLIGAMXGAME-26AUG15MONJUA-JUA` | `KXLIGAMXGAME-26AUG15MONJUA` | 256,113 | 0.00 | 0.17 | 0.18 | no_measured_trading_during_probe |
| `KXNASCARCUPSERIES-NCS26-DHAM` | `KXNASCARCUPSERIES-NCS26` | 254,068 | 0.00 | 0.46 | 0.462 | no_measured_trading_during_probe |
| `KXPRESNOMD-28-AKLO` | `KXPRESNOMD-28` | 245,498 | not probed | 0.002 | 0.003 | no_resting_size, negative_resting_size, not_probed |
| `KXPRESNOMD-28-EWAR` | `KXPRESNOMD-28` | 243,354 | not probed | 0.01 | 0.01 | crossed_book, not_probed |
| `KXNFLGAME-26SEP13ATLPIT-ATL` | `KXNFLGAME-26SEP13ATLPIT` | 232,499 | 0.00 | 0.39 | 0.4 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP13NYJTEN-TEN18` | `KXNFLSPREAD-26SEP13NYJTEN` | 226,032 | 0.00 | 0.06 | 0.07 | no_measured_trading_during_probe |
| `KXPRESPERSON-28-SSMI` | `KXPRESPERSON-28` | 224,285 | 0.00 | 0.002 | 0.003 | no_measured_trading_during_probe |
| `KXNFLSPREAD-26SEP09NESEA-NE11` | `KXNFLSPREAD-26SEP09NESEA` | 221,111 | 0.00 | 0.05 | 0.06 | no_measured_trading_during_probe |

*(showing 60 of 250 recorded; 72,678 rejected in total — the full list is in the JSON manifest)*

## 8. The selected universe

| stratum | ticker | event | series | structure | statistic | rank |
|---|---|---|---|---|---:|---:|
| **high** | `KXMLB-26-TEX` | `KXMLB-26` | `KXMLB` | structured | 16,890.34 | 1 |
| **high** | `KXNBA-27-TOR` | `KXNBA-27` | `KXNBA` | structured | 13,175.99 | 2 |
| **high** | `KXLIGAMXGAME-26AUG16SLACDG-SLA` | `KXLIGAMXGAME-26AUG16SLACDG` | `KXLIGAMXGAME` | structured | 5,419.04 | 3 |
| **high** | `KXECONSTATCPIYOY-26AUG-T3.6` | `KXECONSTATCPIYOY-26AUG` | `KXECONSTATCPIYOY` | custom | 2,632.11 | 4 |
| **medium** | `KXPGATOP10-FESJC26-RMCI` | `KXPGATOP10-FESJC26` | `KXPGATOP10` | structured | 15.96 | 195 |
| **medium** | `KXTESTMATCH-26AUG122030BANAUS-TIE` | `KXTESTMATCH-26AUG122030BANAUS` | `KXTESTMATCH` | structured | 15.93 | 196 |
| **medium** | `KXPGATOUR-FESJC26-RFOW` | `KXPGATOUR-FESJC26` | `KXPGATOUR` | structured | 15.91 | 198 |
| **medium** | `KXPGATOP20-FESJC26-JKOI` | `KXPGATOP20-FESJC26` | `KXPGATOP20` | structured | 15.87 | 201 |
| **low** | `KXNCAAF-27-OKLA` | `KXNCAAF-27` | `KXNCAAF` | structured | 12.21 | 390 |
| **low** | `KXPRESNOMR-28-TCOT` | `KXPRESNOMR-28` | `KXPRESNOMR` | custom | 11.95 | 391 |
| **low** | `KXNASCARRACE-COOO81526-TYGI` | `KXNASCARRACE-COOO81526` | `KXNASCARRACE` | structured | 11.94 | 392 |
| **low** | `KXMAXSHARDINGTEST-26AUG2818-T68399.99` | `KXMAXSHARDINGTEST-26AUG2818` | `KXMAXSHARDINGTEST` | greater | 6.05 | 394 |

**Distinct events spanned:** 12 — `KXECONSTATCPIYOY-26AUG`, `KXLIGAMXGAME-26AUG16SLACDG`, `KXMAXSHARDINGTEST-26AUG2818`, `KXMLB-26`, `KXNASCARRACE-COOO81526`, `KXNBA-27`, `KXNCAAF-27`, `KXPGATOP10-FESJC26`, `KXPGATOP20-FESJC26`, `KXPGATOUR-FESJC26`, `KXPRESNOMR-28`, `KXTESTMATCH-26AUG122030BANAUS`

**Distinct series spanned:** 12 — `KXECONSTATCPIYOY`, `KXLIGAMXGAME`, `KXMAXSHARDINGTEST`, `KXMLB`, `KXNASCARRACE`, `KXNBA`, `KXNCAAF`, `KXPGATOP10`, `KXPGATOP20`, `KXPGATOUR`, `KXPRESNOMR`, `KXTESTMATCH`

**Distinct contract structures (`strike_type`):** 3 — `custom`, `greater`, `structured`

### Stratum boundaries

| boundary | upper stratum min | lower stratum max | ratio |
|---|---:|---:|---:|
| high_over_medium | 2,632.11 | 15.96 | 164.91x |
| medium_over_low | 15.87 | 12.21 | 1.30x |

## 9. Representativeness — read this before generalising anything

> THESE TWELVE MARKETS ARE NOT A REPRESENTATIVE SAMPLE OF THE VENUE. The sampling frame is a THREE-STAGE funnel, and every stage narrows it: (1) a census of 73260 open demo markets excluding MVE shards; (2) a SCREEN to 868 markets that already had a quoted, sized, uncrossed book and non-zero 24h volume, ordered by that screening statistic and capped at 1200; (3) a timed activity probe leaving 582 eligible. A market that was dormant during the probe, or that had a live book but no trading history, cannot appear here AT ALL. 'low' therefore means least active AMONG THE ELIGIBLE, which is nowhere near typical of the venue — the median open market on this environment has no book and trades nothing. The ranking statistic is a TRADE rate whose rank correlation with MESSAGE rate is UNMEASURED. Any statistic computed from the resulting tape describes this universe and must not be generalised to Kalshi.

