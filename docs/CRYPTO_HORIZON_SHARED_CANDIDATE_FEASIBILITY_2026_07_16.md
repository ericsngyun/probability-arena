# CRYPTO-HORIZON-SHARED-CANDIDATE-FEASIBILITY-001 — findings (2026-07-16)

Research and measurement only. Determines, from **already-persisted local data**,
whether the current discovery / lifecycle-anchor / cohort-selection pipeline can
realistically produce two complete-state tokens with overlapping 15m planner
windows early enough to arm CANARY-004. No provider call, discovery scan, cohort
creation, observation, arming, or write occurred. Tool: `crypto-horizon-shared-
candidate-feasibility-report` (zero calls, no persistence; reuses the deployed
`_completeness_reason`, `_horizon_windows`, `_shared_windows`, `ACTIVATION_GRACE`).

## Verdict

**CURRENT DISCOVERY SOURCE IS THE BLOCKER.**

Birth-timing is *not* scarce and completeness is *not* the binding constraint —
the fatal property is that complete tokens are **persisted a median ~85 minutes
after their `first_evidence_at` anchor**, so their 15m window is already closed at
persistence for ~78% of them. If discovery surfaced complete tokens promptly, the
**197 already-overlapping complete pairs** would be usable and CANARY-004 would be
easy; instead only fleeting, luck-dependent moments exist.

Secondary (coupled) factor: initial-state completeness is only ~39% because the
DexScreener profile/boost feed captures liquidity at *profile time*, and
pre-graduation pumpfun tokens have a null liquidity field. This is entangled with
freshness (a naive freshness fix that discovers at mint would reintroduce
null-liquidity), so it is noted but is not the primary blocker.

## History coverage

- Trustworthy synced history: **~4.37 days** — `first_evidence_at` spans
  `2026-07-11T19:44:05Z … 2026-07-16T04:38:04Z`; `created_at` (persistence) spans
  `2026-07-12T00:51:35Z … 2026-07-16T04:45:57Z`. 508 solana birth anchors.
- Requested 24h/7d/14d/30d ranges: only 24h and the full window are covered; **7d,
  14d, 30d are DATA-LIMITED** (exceed available history) and collapse to the full
  508-row window. Rate estimates below are provisional on ~4 days.

## Initial-state completeness funnel

Denominator = birth anchors (full available history, n=508). Uses the exact
deployed `--require-complete` rule.

| Step | Count | % of all |
|---|---|---|
| all token anchors | 508 | 100.0% |
| valid `first_evidence_at` | 508 | 100.0% |
| deterministic initial pair | 508 | 100.0% |
| initial price present | 508 | 100.0% |
| positive initial liquidity | 198 | 39.0% |
| complete-state eligible | 198 | 39.0% |
| persisted while 15m feasible | 44 | 8.7% |
| persisted with safe due-now arm margin (grace 45s) | 41 | 8.1% |

Completeness failure reasons: `liquidity_or_initial_state_missing = 310` (the
initial liquidity **field** is null — no positive-liquidity pool captured at the
anchor), `complete = 198`. Pair and price are always present; **the only
completeness gap is initial liquidity.**

Segmentation:
- **By launch_source:** `dexscreener:profile` n=486, 37.7% complete, median
  complete-token persistence lag **5129 s (~85 min)**; `dexscreener:boost` n=22,
  68.2% complete, median lag ~3789 s (~63 min).
- **By pair venue (`first_dex_id`):** pumpfun n=432 → **29% complete** (bonding-curve,
  liquidity field frequently null); pumpswap 62/62, raydium 6/6, meteora 3/3,
  orca 1/1 → **100% complete** (graduated real pools); launchlab 0/3.

Interpretation: completeness is a *venue/lifecycle* property — a token is complete
once it has a real pool (graduated), which is also when the profile feed tends to
surface it. Fresh pumpfun mints are structurally incomplete.

## Shared-window feasibility

Complete pairs within a 15-minute neighborhood (beyond which two 15m windows
cannot intersect), using the deployed shared-window math and a 45s activation
grace:

| Metric | Count |
|---|---|
| complete pairs ≤ 15 min apart | 197 |
| overlapping 15m windows | 197 |
| grace-compatible shared windows (≥ 45 s) | 197 |
| **usable** (both persisted before arm deadline) | **13** |
| distinct usable moments | 7 |
| days with ≥ 1 usable pair | 4 (07-12, 07-13, 07-14, 07-16) |

Operator-margin sensitivity (margin *beyond* the 45s grace, modelling dry-run +
create + arm + verify time):

| arm margin | usable pairs | distinct moments | days |
|---|---|---|---|
| 0 s | 13 | 7 | 4 |
| 60 s | 10 | 6 | 4 |
| 120 s | 10 | 6 | 4 |
| 300 s | 10 | 6 | 4 |

**Birth-timing overlap is abundant (197 grace-fit pairs); usability collapses to
~6 genuine multi-minute-margin moments across 4.37 days (~1.4/day).** Three of the
zero-margin "usable" pairs (the 07-12 BREAD/ギコ/WHO cluster) had only ~5 s of
slack — pure timing luck that any real operator action erases. The remaining ~6
moments had >8 min slack but are short (~13 min windows) and require an operator to
act at that exact time. There is no readiness detector to catch them; the CANARY-004
Gate B attempt (2026-07-16 18:11) failed precisely because no such moment was live —
the freshest complete births were hours old.

## Discovery-source findings — why Gate B returned old candidates

- `created_at` is **batch-stamped at scan time**: a single scan (e.g. 04:45:57) first-
  persisted tokens whose `first_evidence_at` spanned many hours (04:38 back to the
  prior day). So one scan surfaces a batch dominated by hours-old births.
- Therefore the ~85-min median lag is **not a persistence-pipeline delay** — it is the
  **DexScreener profile/boost graph surfacing tokens well after birth** (a token must
  first earn a profile/boost, which happens long after its 15m window). The endpoint
  is stale *by design* for a 15m horizon.
- The scan's merge/ranking did not displace fresher records — **fresh complete records
  largely do not exist in this feed** at scan time; the pool is structurally aged.
- A different already-supported source was not more appropriate: pumpfun-venue births
  (the freshest) are the *least* complete (29%).

Root cause = **discovery latency of the current source**, with a coupled
initial-state-capture effect. Multiple factors contribute, but the *binding* one is
discovery freshness: fix it and 197 existing complete pairs become usable.

## Is CANARY-004 feasible without relaxing completeness / changing the anchor / repeated scans / timing luck?

**No — not reliably.** Usable moments exist only ~1.4×/day, are short-lived, and were
only caught historically by scan-cadence luck. On-demand or scheduled CANARY-004 is
not currently feasible without either (a) a readiness detector to exploit the rare
live moments, or (b) a fresher discovery source. Relaxing completeness, changing the
anchor, or repeated scans are explicitly out of scope and were not used.

## Recommended next milestone

**CRYPTO-HORIZON-CANDIDATE-READINESS-001** (measurement-only). Turn the shared-window
feasibility logic into a **live readiness signal** over already-persisted data that
fires when two complete, still-15m-feasible tokens co-exist with adequate arm slack —
so an operator can arm CANARY-004 at one of the real >5-min-margin moments **without
repeated scans or timing luck**. It requires no new provider, no discovery-graph
change, no anchor change, and no completeness relaxation, and its accumulated
moment-rate data will determine whether the deeper structural
**CRYPTO-DISCOVERY-FRESHNESS-001** (attack the ~85-min median discovery lag) is
ultimately required. Readiness-first is the lowest-risk step and directly removes the
"timing luck" dependency for the moments the current pipeline already produces.

## Safety

Measurement only: no EV, side, size, order, recommendation, wallet, key, swap, or
execution anywhere. Zero provider calls, zero writes, no cohort/observation/unit
creation, no migration (Alembic `0027`). Full suite 1819 passed; 22 new tests;
AST + no-network + safety-grep audits clean. Cohorts 4/5/6 untouched by this
milestone (cohort 5 orphan units were removed separately under explicit approval).
