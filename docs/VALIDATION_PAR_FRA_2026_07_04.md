# VALIDATION — PAR–FRA prime window (2026-07-04, 19:30–21:45 UTC)

**Session type:** read-only measurement validation on EVO-X2 (live production data).
**Question under test (armed by the SOCCER-002/CAN–MAR session):** do main soccer
markets (KXWCGAME winner / KXWCTOTAL totals) fire fresh signals during a live
World Cup window, producing `soccer_evidence` forecasts at ≥0.60 confidence and
the first valid watchlist rows?

**Answer: no — and the blocker is upstream of every mechanism validated so far.
Main-market soccer never entered the scanner universe, so the watcher never
ticked it, so no signal could exist, regardless of promotion/forecast/precheck
quality.** This document is the motivating evidence for SCANNER-002 / OPS-010.

## Session facts

- PAR–FRA (World Cup, Paraguay vs France) kicked off 21:00 UTC; passes ran live
  at 19'–36' of the first half (0–0 throughout the session).
- Host healthy: load ~0.04, 79G free disk, all four user units active, repo
  clean on `d7f21b9` → `c2c562a` (docs commit during session).
- No code changes, no flag changes. `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`
  before, during, after. `ENABLE_EDGE_PRECHECK=true` (manual targeted runs).

## Measurement passes (cycle-scoped `edge-precheck --latest-marketops-run`)

Three passes rode scheduled 5-minute MarketOps timer cycles, each precheck
seconds after the cycle finished (per MVP-005A.1 targeted-mode discipline):

| Pass | Cycle | Precheck timing | Targeted | Outcome |
|---|---|---|---|---|
| A | #209 (21:27 UTC) | 37 s after finish | 0 | 10 seen / 0 promoted (ticker refresh-cooldown after #208) → zero noise rows |
| B | #210 (21:33 UTC) | 36 s after promotion | 3 | 3 promoted, all `KXMLBHRR` player props (live TOR–SEA). Two on 1¢-spread books, ~59,000¢ liquidity proxy, fresh snapshots, gaps −0.665 / −0.480 → sole failure `invalid_low_confidence` (0.50 prop cap). One one-sided book → +`invalid_wide_spread`. persist=1 |
| C | #211 (21:39 UTC) | 1 s after finish | 0 | 15 seen / 0 promoted (cooldown anti-thrash) → zero noise rows |

Watchlist = 0 and candidate_labels = 0 in every pass. Three earlier same-evening
manual cycles (#187 19:30, #194 20:08, #208 21:23) match: every targeted row was
`baseball_evidence`, source-backed, confidence 0.50; the only other failures were
one-sided books and `invalid_stale_market_snapshot` on tickers orphaned by the
20:01 universe rotation.

## Exact root-cause chain (verified, in order)

1. **The measurable markets existed and were ideal.** `KXWCGAME-26JUL04PARFRA-{FRA,PAR,TIE}`
   was live on Kalshi at 82/83¢ (1¢ spread) with ~3.5M contracts of in-play 24h
   volume (`volume_24h_fp`), verified by direct read-only GET during the match.
2. **The scanner never fetched them.** `scanner_max_markets=500` stops after the
   first 500 open markets in default API page order. That page was saturated by
   props (182 `KXWCSTART` lineup props + MLB player props). Verified **not** the
   `mve_filter`: PARFRA GAME markets return fine with `mve_filter=exclude`.
3. **The PARFRA markets that did get scanned were all props** (90 across
   `KXWCAST`/`KXWCFIRSTGOAL`/`KXWCGOAL`/`KXWCSOA`/`KXWCTEAMFIRSTGOAL`), all with
   `volume_24h=0` pre-match → eligibility `volume_24h_below_min` → score 0.
4. **The watcher universe requires score > 0** from the latest ok scan, so no
   PARFRA ticker was ever ticked. The 20:01 UTC scan rotation produced a
   19-ticker universe (11 `KXMLBHRR` props, 4 ATP, 4 soccer props for Jul 6–7
   matches), frozen until the next 4-hourly scan at 00:01 UTC — after full time.
5. **Confirmed live during the match:** 0 PARFRA price ticks, 0 PARFRA signals
   ever recorded; the 4 future-match soccer props ticked every 60 s (the watcher
   loop itself is healthy).
6. **What was promoted instead were player props, which cannot pass the
   confidence gate by design:** evidence forecasters cap confidence at 0.50 when
   no evidence-based estimate is computable (team/game-level evidence cannot
   price a player). All 251 `sports_baseball`+`sports_soccer` prop forecasts to
   date sit at 0.50 (soccer max ever = 0.50, n=18); edge-precheck's
   `EDGE_PRECHECK_MIN_CONFIDENCE=0.60` correctly rejects them.
7. **The 0.60 gate is demonstrably reachable on game-level markets:**
   `KXMLBSPREAD-26JUL041105PITWSH` forecasts hit 0.60/0.65 confidence during the
   day — but they refreshed via the 4-hourly baseline pipeline, not via signals,
   so they were `invalid_stale_forecast` by the time any measurement ran.

## What is healthy (validated live, unchanged)

- OPS-009 promotion freshness: promoted ages minutes-level (mean 205–487 s
  across cycles; 12h p50 ≈ 358 s), `skipped_stale` working, cooldowns preventing
  re-promotion thrash.
- Cycle-scoped measurement: `forecast_to_edge_precheck_s_p50` fell 349 s →
  ~80 s during the session; promotion→measurement 36 s in pass B.
- Honest invalidation: `invalid_explainable_rate=1.0`; zero-promotion cycles
  produce zero measurement rows.
- Source-backed targeting: tennis template refreshes correctly excluded.
- Champion/challenger (baseball): paired n=53, mean_delta_brier −0.041.
- Safety audit (EVAL-001 AST scan): 48 files, `safety_ok: true`.

## Boundaries

No forbidden capability was touched or approached: no EV, no trade
recommendations, no paper trading, no sizing, no orders, no wallets/keys, no
swaps, no signing, no execution, no autonomy. All outputs were probability gaps
and validity labels — measurement only, never advice.

## Conclusion → SCANNER-002 / OPS-010

The root cause of zero watchlist rows is **scanner/watcher universe coverage**,
not edge-precheck and not the forecasters. Fix direction: supplement the generic
first-N scan with targeted, read-only fetches of supported game-level series
(`KXWCGAME`, `KXWCTOTAL`, `KXMLBGAME`, `KXMLBTOTAL`, `KXMLBSPREAD`, …) so
measurable markets reliably enter the scan universe and — when eligible — the
watcher universe. Player-prop promotion tuning (CAN–MAR proposal) remains
worthwhile but is insufficient alone: with today's scan, excluding props leaves
soccer with zero promotable markets.
