# RETENTION-COVERAGE-001 — retention coverage and policy scenarios

**Status:** measurement and design only. **Nothing is activated. No production row
was deleted.**

**Verdict:**

```text
RETENTION IS NOT THE PRIMARY GROWTH LEVER
```

The database is 1,268 MiB over its own critical gate, and **no retention policy
can bring it back under** — because deleting rows does not shrink a SQLite file.
Meanwhile the single largest consumer is not row count at all: **~1,152 MiB
(≈27% of the database) is `raw_payload` debug JSON that no application code
reads.** Details in §7.

---

## 1. How this was measured

Per-table page attribution requires walking every page of the database. On a
4.4 GB file in `journal_mode=delete`, a long read lock blocks the concurrent
writer's `COMMIT` — the exact hazard `SQLITE-BACKUP-COORDINATION-001` was built
to avoid. So the heavy analysis was run against a **decompressed copy of the
verified backup** `backup-20260803T013426Z.db.gz` (snapshot instant
`2026-08-03T01:34:26Z`), in a scratch directory that was deleted afterwards. The
live database was never scanned, and the backup artifacts were never touched.

`retention-coverage-report` ships with `--no-dbstat` for the same reason.

Growth attribution additionally uses an accident of the defect that
`DB-GROWTH-ALERT-IDENTITY-001` repaired: the 933 duplicate alerts each carried
`evidence.size_mb`, giving a 30-day time series of the database size.

## 2. Table inventory (snapshot, 2026-08-03T01:34Z)

File 4,261.7 MiB · 1,090,997 pages × 4,096 B · **freelist 171,581 pages
(15.73%, 670 MiB)**.

| Table | Rows | Data MiB | Index MiB | Total MiB | %file | B/row | Oldest | Retention |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `market_price_ticks` | 427,650 | 1,438.7 | 50.5 | **1,489.2** | 34.9 | 3,652 | 2026-08-01 | 2 d |
| `market_snapshots` | 149,474 | 520.5 | 9.9 | **530.4** | 12.4 | 3,721 | (no ts col) | **never** |
| `market_price_tick_buckets` | 1,086,344 | 244.6 | 201.3 | **445.8** | 10.5 | 430 | 2026-07-09 | 90 d |
| `crypto_token_discovery_events` | 301,880 | 290.4 | 28.0 | **318.4** | 7.5 | 1,106 | 2026-07-04 | **never** |
| `crypto_token_risk_assessments` | 232,907 | 122.3 | 17.7 | 140.0 | 3.3 | 630 | 2026-07-04 | **never** |
| `opportunity_signals` | 32,893 | 122.0 | 3.0 | 125.0 | 2.9 | 3,985 | 2026-07-03 | **never** (`signal_days=0`) |
| `crypto_price_ticks` | 149,082 | 38.4 | 36.7 | 75.1 | 1.8 | 528 | 2026-07-27 | 7 d |
| `market_detail_enrichments` | 12,118 | 61.8 | 0.6 | 62.4 | 1.5 | 5,401 | 2026-07-03 | **never** |
| `meme_catalyst_events` | 131,609 | 34.3 | 17.9 | 52.2 | 1.2 | 416 | 2026-07-20 | 14 d |
| `market_research_packets` | 12,176 | 50.3 | 0.9 | 51.2 | 1.2 | 4,413 | 2026-07-03 | **never** |
| `markets` | 96,469 | 40.9 | 9.3 | 50.2 | 1.2 | 546 | (no ts col) | **never** |
| `meme_attention_snapshots` | 54,994 | 35.9 | 10.6 | 46.5 | 1.1 | 886 | 2026-07-20 | 14 d |
| `market_forecasts` | 12,177 | 40.3 | 0.6 | 41.0 | 1.0 | 3,529 | 2026-07-03 | **never** |
| `crypto_opportunity_signals` | 67,772 | 32.0 | 7.4 | 39.4 | 0.9 | 609 | 2026-07-04 | **never** |
| `market_eligibility_assessments` | 149,474 | 26.5 | 8.3 | 34.8 | 0.8 | 244 | 2026-07-03 | **never** |
| `marketops_runs` | 7,252 | 18.5 | 0.0 | 18.5 | 0.4 | 2,668 | 2026-07-04 | **never** |
| `market_resolution_assessments` | 12,176 | 11.8 | 0.6 | 12.4 | 0.3 | 1,070 | 2026-07-03 | **never** |
| `crypto_pairs` | 20,309 | 7.2 | 3.9 | 11.1 | 0.3 | 575 | 2026-07-04 | **never** |
| `crypto_token_birth_events` | 3,992 | 8.0 | 0.7 | 8.8 | 0.2 | 2,309 | 2026-07-12 | **never** |
| `edge_precheck_snapshots` | 8,171 | 6.6 | 0.8 | 7.4 | 0.2 | 951 | 2026-07-04 | **never** |
| `market_outcomes` | 1,776 | 6.5 | 0.1 | 6.5 | 0.2 | 3,838 | 2026-07-03 | **never** |
| `watcher_runs` | 41,109 | 4.1 | 0.0 | 4.1 | 0.1 | 105 | 2026-07-04 | 30 d |
| `marketops_alerts` | 5,362 | 1.4 | 0.3 | 1.7 | 0.0 | 332 | 2026-07-04 | **never** |

Data + index totals 3,591 MiB; 3,591 + 670 freelist ≈ the 4,262 MiB file. Fully
accounted.

Everything below 1 MiB (tennis tape, cross-venue, horizon cohorts/observations,
pipeline runs, scanner runs, domain/polymarket inventory, `frontier_eval_runs`,
`alembic_version`) is operationally irrelevant to growth and is retained.

## 3. Growth attribution

**Daily ingest ≈ 810 MiB/day** at current per-row cost. **Net file growth
≈ 70 MiB/day.** The gap is retention: pruned tables churn through the freelist
without extending the file.

| Table | rows/24 h | MiB/day | Pruned? | Net contribution |
|---|---:|---:|---|---|
| `market_price_ticks` | 207,450 | **722.4** | 2 d — at steady state | **~0** (churn only) |
| `market_price_tick_buckets` | 44,503 | 18.3 | 90 d — *not yet* at steady state | **+18.3** until 2026-10-07 |
| `market_snapshots` | 4,962 | 17.6 | never | **+17.6** |
| `crypto_token_discovery_events` | 11,573 | 12.2 | never | **+12.2** |
| `opportunity_signals` | 1,531 | 5.8 | never | **+5.8** |
| `crypto_token_risk_assessments` | 9,466 | 5.7 | never | **+5.7** |
| `crypto_price_ticks` | 20,835 | 10.5 | 7 d — steady state | ~0 |
| `meme_catalyst_events` / `meme_attention_snapshots` | 13,173 | 7.0 | 14 d — steady state | ~0 |
| `market_detail_enrichments` / `_research_packets` / `_forecasts` / `_resolution` | 1,948 | 6.6 | never | **+6.6** |
| `crypto_opportunity_signals` | 2,513 | 1.5 | never | **+1.5** |
| `market_eligibility_assessments` | 4,962 | 1.2 | never | **+1.2** |
| others (runs, pairs, edge precheck) | — | 1.3 | mixed | **+1.2** |

**Unbounded total ≈ 51.8 MiB/day**, plus 18.3 MiB/day of buckets that
self-limit at 90 days. This independently reproduces the 65–85 MiB/day observed
in the alert time series — the model is validated, not assumed.

Observed daily sizes (MiB): 07-05 578 → 07-08 2,725 → 07-20 3,181 → 07-27 3,823
→ 08-02 4,259 → 08-04 4,340.

**Projection under current policy** (buckets plateau ~2026-10-07, then
51.8 MiB/day):

| Horizon | Projected file |
|---|---|
| +7 d | ~4,830 MiB |
| +30 d | ~6,440 MiB |
| +90 d | ~10,100 MiB |

A note on the "flat" day: between 01:34Z and 21:04Z on 2026-08-03 the file did
not grow at all, while the freelist fell from 15.73% to 0.48%. The daily
retention run frees ~650 MiB of pages at 00:01 and the day's inserts consume
them. **File size is a ratchet that steps only when the freelist is exhausted** —
which is why a single day's file-size reading is not a growth measurement.

## 4. Preservation floors

Evidence-backed, and deliberately **not** chosen to force the database under the
size gate (`test_floors_are_evidence_backed_not_size_driven` asserts none cites it):

| Table | Floor | Why |
|---|---|---|
| `market_price_ticks` | **2 d** | Below ~2 days there is no overlap left to re-derive a bucket from its own inputs, so OPS-012 aggregation becomes unverifiable. |
| `market_price_tick_buckets` | **90 d** | The aggregated series is what survives raw-tick pruning; it *is* the long-horizon record. |
| `market_forecasts` | indefinite | An unresolved forecast deleted before its outcome arrives can never be scored. |
| `market_outcomes`, `forecast_scores` | indefinite | Settlement truth and current calibration evidence (ADR-004 gate). |
| `crypto_horizon_cohorts` / `_members` / `_observations` | indefinite | Frozen cohorts and CANARY-004 evidence. |
| `crypto_token_birth_events`, `_survival_outcomes` | indefinite | Lifecycle anchors; the tape cannot be replayed without them. |
| `marketops_runs` | **30 d** | The audit spine every deployment proof in `docs/` cites. |
| `marketops_alerts` | indefinite | Operational incident history, including this month's reconciliation record. |

## 5. Dependency map and classification

| Classification | Tables |
|---|---|
| `retain_indefinitely` | `market_forecasts`, `market_outcomes`, `forecast_scores`, `crypto_horizon_*`, `crypto_token_birth_events`, `crypto_token_survival_outcomes`, `marketops_alerts` |
| `retain_aggregated_only` | `market_price_ticks` (raw → buckets) |
| `retain_long_window` | `market_price_tick_buckets` (90 d), `watcher_runs` (30 d), `marketops_runs` (30 d), `pipeline_*` (90 d) |
| `safe_for_bounded_pruning` | `crypto_price_ticks` (7 d), `meme_*` (14 d), `polymarket_*` (14 d) — all already bounded and at steady state |
| `blocked_pending_dependency_review` | **`market_snapshots`**, **`crypto_token_discovery_events`**, `crypto_token_risk_assessments`, `opportunity_signals`, `crypto_opportunity_signals`, `market_eligibility_assessments`, `market_detail_enrichments`, `market_research_packets`, `market_resolution_assessments`, `crypto_pairs`, `edge_precheck_snapshots` |

Every table in the blocked class is in `PROTECTED_TABLES` *and* has no window —
protected as intelligence/audit history by explicit prior decision. Their readers:

- `market_snapshots` → scanner/ranking, eligibility assessment, cross-venue matcher, edge-precheck midpoint, frontier_eval
- `crypto_token_discovery_events` → crypto tape birth anchors, provider health, coverage forensics
- `crypto_token_risk_assessments` → risk engine, **provider-budget accounting** (SolanaTracker spend is derived from these rows), MEME-MAS agents, crypto retrospect
- `opportunity_signals` → signal workflow, MarketOps promotion, frontier_eval latency

Pruning any of them is a research-capability decision, not an ops decision.

## 6. Dry-run eligible counts

Measured, not extrapolated. **Under the CURRENT policy, every window is already
at steady state and essentially nothing is eligible** — 30 d cutoffs return 0
rows because the database only holds ~30 days of history. Reclamation therefore
requires *tightening* windows:

| Table | Cutoff | Eligible rows | % of table | Logical MiB freed |
|---|---:|---:|---:|---:|
| `market_price_ticks` | 1 d | 220,200 | 51.5 | **766.8** |
| `market_snapshots` | 7 d | 113,314 | 75.8 | **402.1** |
| `market_snapshots` | 14 d | 77,629 | 51.9 | 275.5 |
| `crypto_token_discovery_events` | 7 d | 223,867 | 74.2 | 236.1 |
| `crypto_token_discovery_events` | 14 d | 157,376 | 52.1 | 166.0 |
| `crypto_token_risk_assessments` | 7 d | 168,639 | 72.4 | 101.3 |
| `crypto_token_risk_assessments` | 14 d | 113,981 | 48.9 | 68.5 |
| `opportunity_signals` | 14 d | 16,530 | 50.3 | 62.8 |
| `crypto_price_ticks` | 3 d | 86,493 | 58.0 | 43.6 |
| `meme_catalyst_events` | 7 d | 66,356 | 50.4 | 26.3 |
| `meme_attention_snapshots` | 7 d | 27,272 | 49.6 | 23.0 |
| `crypto_opportunity_signals` | 14 d | 32,771 | 48.4 | 19.0 |
| `market_eligibility_assessments` | 14 d | 77,629 | 51.9 | 18.1 |
| `market_price_tick_buckets` | 30 d | 0 | 0.0 | 0.0 |

## 7. The finding that reframes this milestone

`market_price_ticks` costs **3,652 B/row** and `market_snapshots` **3,721 B/row** —
absurd for a price observation. The cause is one column:

| Table | `raw_payload` | Share of row | Total |
|---|---:|---:|---:|
| `market_price_ticks` | 2,049 B/row avg | **95%** | **835.9 MiB** |
| `market_snapshots` | 2,219 B/row avg | **96%** | **316.3 MiB** |

**≈1,152 MiB — 27% of the entire database — is raw provider JSON, and nothing
reads it.** Traced across the whole codebase: both columns are written
(`scanner.py:211`, `watcher.py:274`, `kalshi.py:180`) and never read. `schemas.py:34`
documents it as *"persisted to market_snapshots.raw_payload for debugging"*, and
`schemas.py:204` excludes it from every API response. The only `raw_payload`
*reads* anywhere are on crypto and tennis tables (`crypto_scout`, `crypto_risk_engine`,
`provider_budget`, `frontier_eval`, `crypto_provider_health`) — not these two.

Nulling `raw_payload` on rows older than ~24 h would free more logical space than
any retention scenario in §8, **while deleting no row and losing no derived
measurement** (price, spread, liquidity, volume are all separate columns). Those
two tables are also the worst-packed in the database — 61% and 63% page fill,
versus 86–90% elsewhere — so they carry ~40% fragmentation overhead on top.

This is a data-shape change, not a retention change. It deserves its own
milestone; it is not authorized here.

## 8. Scenarios

None of these is executed.

### Conservative — bound what is already unbounded, at generous windows

`crypto_token_discovery_events` 30 d · `crypto_token_risk_assessments` 30 d ·
`crypto_opportunity_signals` 30 d · `market_eligibility_assessments` 30 d.

- Eligible **today**: ~0 rows (history is only 30 days).
- Effect: caps ~19 MiB/day of unbounded growth **from 2026-09-03 onward**.
- Capability lost: none in the next 30 days; then crypto discovery/risk history
  beyond 30 days, which `provider_budget` accounting reads.
- Fresh backup required: no (nothing is deleted yet).
- Rollback: widen the window; nothing is recoverable once deleted, so the window
  must be agreed before it first bites.

### Balanced — conservative, plus 14-day windows on the crypto audit lane

Adds `crypto_token_discovery_events` 14 d · `crypto_token_risk_assessments` 14 d ·
`opportunity_signals` 14 d.

- Eligible today: **287,887 rows ≈ 297 MiB** logical.
- Effect: caps ~24 MiB/day; growth falls to ~46 MiB/day (incl. buckets).
- Capability lost: crypto tape birth anchors older than 14 days (blocks
  re-deriving lifecycle history), provider-budget accounting beyond 14 days, and
  signal-workflow history the frontier evaluation reads.
- **Blocked** pending an explicit decision on those three readers.
- Fresh backup required: **yes**.

### Aggressive — balanced, plus `market_snapshots` 7 d and raw ticks 1 d

- Eligible today: **621,401 rows ≈ 1,466 MiB** logical.
- Effect: growth falls to ~28 MiB/day.
- Capability lost: **severe.** `market_snapshots` at 7 days breaks the
  cross-venue matcher, eligibility history and edge-precheck midpoint
  reconstruction; raw ticks at 1 day drops below the aggregation-validation floor
  in §4. Both are `PROTECTED_TABLES`.
- **Not recommended.** It buys 1.4 GiB of *reusable pages* that still do not
  shrink the file (§9), at the cost of the measurement capability this project
  exists to produce.

## 9. Logical versus physical

This is the crux.

- Deleting rows in `journal_mode=delete` moves pages to the **freelist**. They are
  reused by later inserts. The file length is a **high-water mark** and only ever
  ratchets upward — **it does not shrink.**
- The database is 4,340 MiB against a 3,072 MiB gate — **1,268 MiB over**. Even the
  aggressive scenario's 1,466 MiB of freed pages leaves the *file* at 4,340 MiB
  and the alert still critical.
- `VACUUM` rewrites the file compactly but takes an exclusive lock for the whole
  rewrite and needs ~2× the database in temporary space — unacceptable against a
  5-minute writer and a nightly backup.
- `VACUUM INTO 'copy.db'` builds a compacted copy under a read transaction
  without blocking writers, but **swapping it in is a separate, riskier
  maintenance operation** (stop writers, verify, replace, restart) that must be
  approved on its own terms.

**No compaction is authorized by this milestone.** Given §7, a compaction
milestone should be *preceded* by the `raw_payload` decision — compacting first
would just re-compact 1.15 GiB of unread JSON.

## 10. Recommendation

1. **First: `raw_payload`.** ~1,152 MiB, 27% of the database, zero readers. Null
   it on aged rows (or stop writing it). Highest value, lowest capability cost,
   and no retention window changes. **Own milestone.**
2. **Second: the conservative scenario.** Bound the four unbounded operational
   tables at 30 days. Nothing is eligible today, so it can be agreed now and
   takes effect gradually — the safest possible way to introduce a window.
3. **Third: compaction**, once (1) has removed the bulk. `VACUUM INTO` plus a
   separately-approved swap is the only path that moves the *file* below the gate.
4. **Do not** pursue the aggressive scenario. It trades the project's core
   measurement capability for pages that do not shrink the file.
5. Consider whether the 3,072 MiB gate is still the right number for a system
   whose steady state is legitimately several GiB. The gate has been breached
   since 2026-07-20 and the alert has been correct every day since.

## 11. Activation gate

Bounded pruning may be activated only when **all** hold:

- [x] The `raw_payload` decision is made — **RAW-PAYLOAD-STORAGE-001**
      (`docs/RAW_PAYLOAD_STORAGE_001.md`) adds a prospective capture
      policy over 1,495 MiB of unread provider bodies. Note it does NOT
      reclaim the existing ones; that is RAW-PAYLOAD-RECLAMATION-001.
- [ ] Each window in the chosen scenario has a named owner who accepts the
      capability loss for the tables in §5's blocked class.
- [ ] `retention-coverage-report` is re-run and its eligible counts reviewed
      **on the day of activation** (they move daily).
- [ ] A fresh verified backup exists and `sqlite-backup-freshness-report` is
      healthy.
- [ ] Activation runs outside 01:25–01:45 UTC.
- [ ] `prune-retention --dry-run` output is saved as the pre-flight record.
- [ ] It is understood and written down that **the file will not shrink**.

Until then, `retention-coverage-report` is read-only and there is no `--confirm`.
