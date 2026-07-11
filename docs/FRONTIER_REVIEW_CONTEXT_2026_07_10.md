# FRONTIER-REVIEW-CONTEXT-002 — Probability Arena external review packet

Generated 2026-07-11 ~21:40 UTC from live EVO-X2 state (host commit `3bf417b`,
alembic `0025`). Successor to FRONTIER_REVIEW_CONTEXT_2026_07_09.md. Factual
current-state summary for external frontier-model review — documentation
only. Nothing here is advice, a recommendation, or a trading capability.

## 1. Executive summary

Probability Arena is a **read-only market-intelligence and calibration
system** over Kalshi (primary), Polymarket (observation), and Solana memecoin
surveillance, running on a single shared host (EVO-X2, SQLite, systemd
timers). Since the last packet (2026-07-09) the system: (a) **falsified its
own leading alpha hypothesis** through a pre-registered out-of-sample
protocol — all six candidate row-selection policies failed validation and
were formally retired; (b) established that even the in-sample edge was
**uneconomic under conservative friction**; (c) built and validated the
**tennis observation lane** end-to-end on the market side (synchronized tape
recorder, 30 capture runs) while measuring decisively that the current score
provider cannot supply live ITF state; and (d) executed a staged storage
reduction (OPS-014) with zero behavioral impact. **No paper trading, no live
capital, no execution capability exists anywhere in the system; MVP-005B
(paper-trading design) remains blocked.**

## 2. Current system architecture

- **Pipeline**: Kalshi scan → eligibility → enrichment → resolution →
  research packets (template + flag-gated external canaries for
  baseball/soccer) → probability forecasts (template baseline + evidence
  forecasters) → outcome sync → calibration scoring (Brier/log-loss).
- **Realtime**: watcher (~5 min cadence) records `market_price_ticks` +
  deterministic informational signals; MarketOps Autopilot coordinates cycles
  (run #1906, `ok`); edge-precheck measures forecast-vs-market gaps
  (2,105 snapshots to date) with follow-through measured from ticks.
- **Analysis suite** (read-only, on a daily 15:00 UTC snapshot timer):
  edge-policy/cohort/followthrough/filter-shadow/forecast-anchor/
  trigger-timing/selection-validation/cost-shadow/retirement + frontier-eval
  + champion-challenger + storage reports.
- **Tennis lane** (manual, bounded): tick watcher, provider scaffolds
  (API-Tennis, Goalserve), synchronized tape recorder (migration 0025),
  WebSocket/live-feed probes, informative-books-first candidate ordering.
- **Scale**: DB 2,750 MiB (1.15M rows / 44 tables); tick aggregation buckets
  (5-min OHLC) at 90d retention; raw ticks at 2d (OPS-014).
- **Volumes**: 2,228 source-backed research packets; forecasts:
  baseball_evidence 2,064 / soccer_evidence 141 / template_baseline 1,246;
  champion/challenger paired n=91 (needs 100; mean ΔBrier −0.029, early).

## 3. EDGE-SELECTION post-lock failure (the headline result)

An 18-policy shadow search (EDGE-FILTER-001) produced apparently strong
cohorts (best: toward 0.539 / closure +0.42 in-sample). A pre-registration
protocol (EDGE-SELECTION-001, locked 2026-07-09T19:00Z) froze 6 candidates +
baseline + a negative control before any validating window. **First
substantial out-of-sample window (n=297 pure post-lock rows, 2026-07-11):
every candidate failed** — the primary inverted to 0.286/−1.22; follows-move
cohorts posted closures −1.2..−1.7 vs baseline −0.06 — while the **negative
control (spread_only) outperformed every candidate at 0.375/+0.33**.
Additional regime-noise evidence: the latest 24h baseline follow-through
swung positive (toward 0.404 / closure +0.167) with no system change.
Formally retired (EDGE-RETIRE-001, `docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md`):
retired policies are ineligible for any live-gate/paper/MVP discussion;
resurrection requires a new prereg + lock. **Conclusion: the search overfit;
window-to-window regime variance dominates these cohort statistics.**

## 4. COST-MODEL-001 conclusion

Independently of the validation failure, conservative friction (half-spread +
round-trip Kalshi fee 0.07·P·(1−P) at both ends + executable touch prices)
killed every positive-frictionless cohort BEFORE the out-of-sample test:
`cohorts_positive_after_costs: NONE` on both 24h and 48h windows (100%
executable-quote coverage). The apparent edge failed on both axes — direction
(out-of-sample) and economics (friction). Standing doctrine: all future
validation includes cost-adjusted gates from day one.

## 5. Tennis lane status (the active mechanism-first thesis)

- **Market tape WORKS**: TENNIS-WATCHER-001 + TENNIS-TAPE-001 (migration
  0025) captured 30 tape runs / 2,870 market snapshots / 2,870 links; live
  in-play microstructure recorded at 60s resolution (e.g. IMANAK midpoint
  0.245→0.485 across 10 minutes; 24 books repriced during a 4-minute probe
  bracket). Live ITF markets carry the volume (seven figures per match).
- **API-Tennis: mapping YES, live state NO (final)**: fixture catalog maps
  75–85% of live candidates (Challenger/ITF included; ±1-day date tolerance
  fix validated live), but REST get_livescore returned 0 rows across 25+
  probes, fixture state stayed frozen in-play, and the documented WebSocket
  emitted 0 frames in a decisive 180s in-play probe. Verdict:
  catalog/mapping-only.
- **Goalserve fallback: scaffold DEPLOYED, key PENDING**: bounded validation
  probe (≤10 calls, path-embedded-key hygiene, same linker, verdict ladder)
  is live and inert; awaiting a 30-day trial key. This is the single gating
  action for the tennis score side.
- **Candidate ordering DEPLOYED**: bounded captures now spend slots on
  active/two-sided/high-volume/moving/source-backed books first (fixes a
  measured failure where alphabetical ordering missed the live matches).
- Parked pending score side: TENNIS-TAPE-GOALSERVE / TENNIS-MICROSTRUCTURE
  (lag distributions, quote-response profiles — still no models).

## 6. Ops status

- Tick aggregation: hourly scheduled cycles, 67+ clean at last check, zero
  errors since the timer flip; coverage reached 1.0 pre-OPS-014.
- **OPS-014 EXECUTED (2026-07-11 21:29 UTC)**: raw tick retention 3d→2d,
  backup-first; 380,850 rows pruned in 9s; raw table 2,092→986 MiB (~1.1 GiB
  freed for reuse); buckets intact (90d); MarketOps healthy throughout; DB
  file flat at 2,750 MiB with internal headroom. Further reduction = new
  milestone.
- Frontier eval: `safety_ok=True` continuously; MarketOps p90 ~50–56s < 60s.

## 7. Crypto/meme lane status

Read-only surveillance unchanged: crypto signals 15,757 / tokens 2,577
tracked; meme-news scout 752 runs (0 errors), 20.7k attention snapshots,
50.4k catalyst events, 280 new tokens discovered; risk engine emits
avoid/flag verdicts only. Polymarket observation: 450 markets persisted;
cross-venue matcher (POLY-002) produces comparability verdicts + measured
probability-point differences only. No new milestones this period.

## 8. Blocked / unblocked decisions

- **MVP-005B (paper-trading design): BLOCKED** — reinforced by retirement;
  requires a new mechanism-first prereg surviving out-of-sample AND cost
  gates AND explicit human acceptance.
- **Goalserve trial key (human action)**: blocks the tennis score side — the
  single highest-leverage pending action.
- **API-Tennis pricing (~Jul 24 trial end)**: pay $40/mo for mapping-only vs
  Goalserve ($150/mo) possibly covering both roles; decide after the
  Goalserve probe.
- **EDGE-SELECTION rolling-7d (~Jul 16)**: now registry observation only.
- Unblocked/idle: champion-challenger awaits paired n≥100; further raw
  retention reduction available but not urgent.

## 9. Recommended questions for external review

1. Given the retirement (search-based cohort selection falsified, regime
   variance dominant), is the tennis latency thesis — score-event-to-quote
   lag on thin ITF books — the right next mechanism, or is there a stronger
   mechanism-first candidate in the existing data (e.g. the cost model's
   spread/fee structure itself, cross-venue Kalshi↔Polymarket timing)?
2. Is game-level (not point-level) score resolution sufficient to measure an
   exploitable-latency hypothesis on ITF books, given 60s market snapshots?
3. Are the pre-registration gates (n≥75, toward≥0.55, positive cost-adjusted
   closure, concentration guards, out-of-sample) correctly calibrated, or do
   the observed regime swings imply the gates need longer windows / multiple
   independent windows by design?
4. Champion/challenger sits at n=91 pairs (ΔBrier −0.029): what stopping rule
   should govern forecaster iteration before it becomes another overfit
   search?
5. Storage/ops: any risk in the 2d raw-tick window we have not considered,
   given buckets carry 90d aggregates?

## 10. Explicit boundaries (unchanged, enforced in code and tests)

No paper trading. No live capital. No execution of any kind. No EV
calculations, trade recommendations, or position sizing. No orders, wallets,
private keys (beyond read-only Kalshi API request-signing), swaps, or
signing. No autonomous trading. No probability/Markov models in the tennis
lane (Phase 0 measurement only). Every analysis label (promising_shadow,
validated_shadow, verdicts) is a measurement-quality statement that
authorizes nothing; advancement of any capability requires explicit human
acceptance of a named milestone.
