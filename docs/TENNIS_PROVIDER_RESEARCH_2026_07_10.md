# TENNIS-PROVIDER-001 — live-score provider research (2026-07-10)

Read-only provider research and bounded-validation plan for the tennis score
side. Goal: a legal, reliable, source-backed live-score provider that covers
the tiers Kalshi actually lists, to support future synchronized tennis tape
recording (TENNIS-TAPE-001 — parked). **This document authorizes nothing**:
no probability models, EV, paper trading, recommendations, sizing, orders,
wallets, signing, swaps, execution, or autonomy.

## 0. What our universe needs (measured, 2026-07-10)

240 live tennis candidates in 24h on EVO-X2, series mix: **KXITFMATCH 66 /
KXITF 64 / KXITFWMATCH 60 / KXATPCHALLENGERMATCH 36 / KXWTA 14**. So ~79% of
the live universe is **ITF-family** and ~15% Challenger — main-tour ATP/WTA
is a small minority. A provider without ITF + Challenger coverage is useless
for this universe (ESPN measured: `source_backed 0/176`, scoreboards carry
only 2–4 main-tour events/day).

## 1. Provider comparison

| Provider | ATP main | WTA main | Challenger | ITF | Live / point-by-point | Latency class (stated) | Pricing (public) | Trial | Notes |
|---|---|---|---|---|---|---|---|---|---|
| **API-Tennis (api-tennis.com)** | ✅ singles+doubles | ✅ | ✅ Challenger M/W | ✅ ITF M/W | livescore endpoint; fixtures include point-by-point + set scores | not stated (poll-based; likely seconds) | $40/mo Starter (8k req/day) → $120/mo Ultra (2M/day) | **14 days** | Numeric event/player/tournament keys + player names + `yyyy-mm-dd` dates → mappable to our ticker parse (players/date/tour). Coverage "included in your current subscription plan" — verify Challenger/ITF in trial tier during validation |
| **Goalserve** | ✅ | ✅ | ✅ ("all ... Challenger") | ✅ ("all ... ITF" + juniors) | point-by-point, updates **every 5s**; JSON/XML | ~5s class | **$150/mo** tennis feed; $1,200/yr | **30 days** | Strongest explicit lower-tier coverage claim; betting-feed oriented (commercial use expected); per-tier point-by-point not explicitly confirmed for ITF — validate |
| **Matchstat (tennis-api.com)** | ✅ | ✅ | ✅ ("complete") | ✅ ("complete") | live scores, point-by-point, WebSocket on MEGA | claims **sub-second updates**, 142ms avg response, 99.9% uptime | tiers FREE→MEGA (via site/RapidAPI; numbers not fully public) | free tier | Marketing claims strong but less verifiable; ID scheme not documented publicly; good second-validation candidate |
| **Sportradar Tennis API** | ✅ official ATP (incl. official Challenger since 2025) | ✅ | ✅ **official** | ❌ **ITF World Tennis Tour REMOVED from Tennis API starting 2025** (Davis/BJK Cup remain) | live + play_by_play per coverage tiers (1–5) | enterprise-grade, low | enterprise (contact sales) | dev trial keys | Best-in-class reliability/legality, but the ITF removal disqualifies it for ~79% of our live universe; candidate only for a future ATP/WTA/Challenger-quality tier |
| API-SPORTS (api-sports.io) | — | — | — | — | — | — | — | — | **No tennis endpoint** (football/NBA/etc. only) — ruled out |
| SportMonks | — | — | — | — | — | — | — | — | Tennis is an expansion plan, not an established live product — ruled out for now |
| SportsDataIO / SportDevs / AllSportsAPI | partial | partial | unverified | unverified | varies | varies | varies | varies | Credible alternates; review only if both primaries fail validation |
| Tennis Abstract | historical only | historical | partial | partial | ❌ not live | n/a | free | n/a | Useful for **historical priors** later; not a live source |
| ESPN (current scaffold) | ✅ marquee only | ✅ marquee only | ❌ (measured) | ❌ (measured) | scoreboard JSON | unknown; unofficial | free | n/a | **Measured insufficient** (0/176); also an unofficial/undocumented API — weak legal footing for sustained use |

Sources: api-tennis.com (+ /documentation), goalserve.com tennis-api
prices/coverage pages, tennis-api.com, developer.sportradar.com (overview,
data-coverage-tiers, changelog "Tennis API - Coverage Updates" 2025-01-02),
api-sports.io, sportmonks.com/products. Fetched 2026-07-10; claims are the
providers' own until validated.

## 2. Recommendation

- **Primary for first bounded validation: API-Tennis (api-tennis.com).**
  Documented Challenger M/W + ITF M/W coverage, 14-day trial, cheapest paid
  tier ($40/mo), simple REST + API key, and an ID surface (player names +
  dates + tournament names) that maps directly onto our validated ticker
  parse. Risk: docs say coverage depends on subscription plan — the
  validation run must confirm Challenger/ITF events appear on the trial plan.
- **Fallback: Goalserve.** Strongest explicit "all ATP/WTA/Challenger/ITF"
  claim, 30-day trial, 5s point-by-point; costlier ($150/mo) and XML-era
  ergonomics. If API-Tennis trial coverage disappoints, validate Goalserve
  within its trial window.
- **Not now: Sportradar** (ITF removed — disqualifying for this universe;
  reconsider if the strategy narrows to ATP/WTA/Challenger only, where its
  official coverage + enterprise reliability would lead). **Ruled out:**
  API-SPORTS (no tennis), SportMonks (not established). **ESPN**: retire from
  consideration for coverage (measured 0/176) and legal footing (unofficial
  API).

## 3. Known coverage gaps and risks

- ITF point-by-point granularity is not explicitly confirmed by either
  primary candidate — set/game-level may be the floor at ITF tier (still
  sufficient for TENNIS-TAPE score-to-market lag at game resolution).
- Trial tiers may restrict tournament types (API-Tennis docs imply this) —
  the validation plan measures exactly this before any payment decision.
- Player-name → Kalshi player-code mapping (3–4-letter codes like CAS/AMB)
  must be validated empirically; our `_find_event`-style matching needs the
  provider's names, not abbreviations, so the scaffold matches on
  last-name-prefix — measured, never assumed.
- Latency claims (5s Goalserve, sub-second Matchstat) are marketing until
  measured against market ticks — that measurement IS the point of the tape.

## 4. Expected latency class

- API-Tennis: poll-based REST; effective class ~5–30s at reasonable poll
  cadence within request quotas (8k/day Starter ≈ one poll ~every 11s
  sustained — adequate for game-level lag studies, not point-level).
- Goalserve: stated 5s point-by-point class.
- Matchstat: claims sub-second/WebSocket at top tier.
- All adequate for the first tape question (does the market move before the
  score updates at game resolution?); point-level microstructure would need
  the higher tiers later.

## 5. Cost estimate (public numbers)

- Validation phase: **$0** (API-Tennis 14-day trial; Goalserve 30-day trial).
- First paid month if validation succeeds: **$40/mo** (API-Tennis Starter) or
  **$150/mo** (Goalserve). Sportradar: enterprise quote only.

## 6. ToS / legal notes (as visible)

- API-Tennis and Goalserve are commercial sports-data vendors whose paid
  plans are sold for app/site use including betting products; standard
  subscription terms apply — **review the actual ToS at signup** (neither
  page publishes full commercial-use text). No scraping involved; keyed API
  access only. Sportradar is the gold standard legally (enterprise contract)
  if/when that tier is justified.

## 7. Bounded validation plan (requires explicit approval + a trial key)

1. Obtain an API-Tennis trial key (no card required per their site); store
   ONLY on EVO-X2 `.env` as `TENNIS_PROVIDER_API_KEY` (never committed;
   report presence/absence only).
2. Set `TENNIS_RESEARCH_PROVIDER=api_tennis` inline (env var scoped to the
   command, `.env` untouched — same pattern as the ESPN run).
3. Run `tennis-live-source-report --top 50 --hours 24` — the scaffold maps
   `get_fixtures(date)` to the internal scoreboard shape; the report then
   measures, per current live candidate: `source_backed` vs
   `provider_no_match`, with the same bounded fetch cap (≤6 scoreboard-days).
   Request volume: ≤10 REST calls total. Persistence: none.
4. Decision gates: **useful** if source_backed ≥ ~50% of live Challenger+ITF
   candidates (name-mapping tuning allowed for one iteration); **fallback to
   Goalserve** if < ~25% after tuning; in between → one more live-window run.
5. No forecasting, no persistence of provider data (validation only), no
   trading surface of any kind. TENNIS-TAPE-001 remains parked until a
   provider passes AND a tape design milestone is explicitly accepted.

## 7b. VALIDATION RESULTS (2026-07-10, bounded run — 5 of ≤10 calls, no persistence)

- **Tier check (`get_events`, 1 call): PASSED** — the 14-day trial plan
  includes all 27 tournament types, explicitly **Challenger Men/Women
  Singles+Doubles and ITF Men/Women Singles+Doubles**. No tier lockout.
- **Coverage (`get_fixtures`, 2 dates × 2 passes): PASSED the ≥50% gate.**
  435 + 251 fixtures returned for the two live dates. First (untuned) pass:
  84/176 = 47.7% — depressed by the ESPN-era `KXITF*→atp` tour mapping
  excluding ITF-women fixtures. Allowed tuning pass (tour filter neutral,
  player codes disambiguate): **130/176 = 73.9% source_backed** —
  KXATPCHALLENGERMATCH 32/36, KXWTACHALLENGERMATCH 12/14, KXITFWMATCH 46/60,
  KXITFMATCH 40/66. The tuning is codified in `ApiTennisFetcher.fetch_scoreboard`.
- Remaining ~26% unmatched: last-name-code edge cases (multi-word/hyphenated
  names, diacritics) — further headroom, not a blocker.
- **VERDICT: API-Tennis is USEFUL per §7 gates. TENNIS-TAPE-001 becomes
  designable** (still parked until a tape design milestone is explicitly
  accepted). Goalserve fallback not needed.

## 8. Can the recommended provider support TENNIS-TAPE-001?

Likely yes, pending validation: API-Tennis exposes fixtures + livescore with
event status, set/game scores, player names, and dates for Challenger/ITF —
enough to timestamp score changes at game resolution and pair them with our
tennis market ticks (TENNIS-WATCHER-001, validated live). Point-level tape
would need Goalserve/Matchstat tiers. The two halves would meet in a future,
explicitly-accepted TENNIS-TAPE-001 design.
