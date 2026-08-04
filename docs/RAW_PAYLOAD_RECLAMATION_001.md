# RAW-PAYLOAD-RECLAMATION-001 — bounded historical raw-payload reclamation

**Status:** implemented and reviewed; production decision and run in §9–§11.

[`RAW-PAYLOAD-STORAGE-001`](RAW_PAYLOAD_STORAGE_001.md) stopped writing unread
provider bodies **prospectively** and deliberately touched no historical row.
This replaces eligible *historical* bodies with the same canonical envelope the
live writer produces.

**This is the only genuinely irreversible step in the sequence.** A reclaimed
body is gone; the only recovery is a backup restore. Every design choice below is
shaped by that asymmetry rather than by throughput.

---

## 1. Post-activation prospective measurement

Prospective suppression has been live since `2026-08-04T04:13Z`.

### Measured

| Fact | Value |
|---|---|
| Window | `04:13:55Z` .. `04:58:34Z` (**44 m 39 s**) |
| `market_price_ticks` rows written | 6,600 — **6,600 suppressed** |
| `crypto_token_discovery_events` | 352 written — 333 suppressed (19 kept: bodies below the 160 B monotone floor) |
| `opportunity_signals` | 1 written — 1 suppressed |
| Bytes avoided | **12.37 MiB** |
| MarketOps duration | 43,616 ms avg (20 cycles before) → **37,704 ms** (7 after) |
| `database_locked` events | **4** — unchanged |
| Alembic | `0027` |

### Projected

Extrapolating 12.37 MiB / 44 m 39 s gives **≈399 MiB/day**, consistent with the
≈404 MiB/day projection in `RAW_PAYLOAD_STORAGE_001.md` §7. **This is a
projection from a 45-minute window, not a measured daily rate.**

### Insufficiently elapsed

The **4-hourly baseline timer has not run under `none`** — it last ran at
`04:03:26Z`, ten minutes *before* activation, and next runs at `08:03Z`. So
`market_snapshots` and the three `market_detail_enrichments` columns have written
**zero** rows under suppression. Those are precisely the columns that carry the
*net growth* reduction, so the durable prospective effect is **not yet
measurable**. It is not claimed here.

## 2. Dependency re-audit

Re-verified against the `RAW-PAYLOAD-STORAGE-001` inventory. Classification:

| Column | Class |
|---|---|
| `market_snapshots.raw_payload` | **eligible_for_reclamation** |
| `crypto_token_discovery_events.raw_payload` | **eligible_for_reclamation** |
| `opportunity_signals.raw_payload` | **eligible_for_reclamation** |
| `market_detail_enrichments.raw_market_detail` / `raw_series_detail` / `raw_event_detail` | **eligible_for_reclamation** |
| `market_price_ticks.raw_payload` | **self_expiring_no_manual_action** |
| `crypto_token_risk_assessments.raw_payload` | **pinned_production_reader** |
| `market_research_packets.raw_response` | **pinned_production_reader** |
| `crypto_price_ticks.raw_payload` | **pinned_production_reader** |
| `crypto_horizon_observations.raw_payload` | **pinned_production_reader** |
| `market_forecasts.raw_response`, `market_resolution_assessments.raw_response`, `market_outcomes.raw_payload`, `crypto_opportunity_signals.raw_payload`, `edge_precheck_snapshots.raw_context`, `tennis_tape_score_snapshots.raw_payload`, `cross_venue_market_candidates.raw_context`, `crypto_token_birth_events.raw_payload` | **blocked_dependency** (ungoverned by the live writer — see below) |

**Pinned columns are structurally unreachable.** `resolve_target()` refuses any
name that is in `PINNED_FULL`, is not in `GOVERNED_COLUMNS`, or is not in the
reclamation registry. There is no table/column CLI interface at all — the command
takes a reviewed registry *name*.

**Why the ungoverned columns are blocked rather than reclaimed.** The live writer
does not suppress them, so reclaiming their history would leave those tables
permanently mixed: old rows enveloped, new rows full bodies, forever. That is a
worse state than either extreme, for ~28 MiB.

**`market_price_ticks` is excluded as self-expiring.** 877 MiB across 448,200
rows — the single largest number in the inventory, and the one it would be most
tempting to claim. But the table already carries a **2-day retention window**, so
every one of those bodies is deleted by retention within 48 h of prospective
suppression, for free. Rewriting 448,000 rows on a live 4.4 GiB database to beat
retention to work it will do anyway is all risk and no durable benefit.

## 3. Eligible historical targets

| Target | Full-body rows | MiB | Preservation |
|---|---:|---:|---|
| `market_snapshots.raw_payload` | 154,767 | **327.6** | 7 d |
| `crypto_token_discovery_events.raw_payload` | 314,816 | **224.9** | 14 d |
| `opportunity_signals.raw_payload` | 33,661 | **63.7** | 7 d |
| `market_detail_enrichments.raw_market_detail` | 12,482 | 24.4 | 7 d |
| `market_detail_enrichments.raw_series_detail` | 12,482 | 17.5 | 7 d |
| `market_detail_enrichments.raw_event_detail` | 12,482 | 6.6 | 7 d |
| **Total durable target** | | **≈664.7 MiB** | |

Excluded self-expiring `market_price_ticks`: 877.3 MiB. Full historical
inventory: 1,542.1 MiB.

## 4. Preservation windows

**Two independent floors, and the EARLIER of the two wins.**

1. **The age floor** — per-table, about research and debugging value.
2. **The backup floor** — universal and non-negotiable: a row written *after* the
   newest verified backup is in **no** backup, so reclaiming it has no recovery
   path whatsoever. This is not a per-table judgement call.

| Target | Age floor | Evidence |
|---|---|---|
| `market_snapshots` | 7 d | The body's stated purpose is scan debugging, and that value lives in the last few baseline cycles — 7 days is ~42 cycles. |
| `crypto_token_discovery_events` | 14 d | The tape reads structured columns, not this body, but it does copy it into `crypto_token_birth_events`. 14 days covers the tape's full 24 h survival-horizon set with wide margin plus the readiness measurement epochs. |
| `opportunity_signals` | 7 d | MarketOps promotion only considers signals inside a **60-minute** freshness window. 7 days is two orders of magnitude beyond any live read. |
| `market_detail_enrichments.*` | 7 d | Enrichment is re-derivable by re-fetching the market; the normalized columns carry everything any consumer reads. |

Also never eligible, independent of age: rows already carrying the envelope, rows
whose payload is absent (SQL `NULL` *or* the JSON literal `null` — SQLAlchemy's
JSON type stores Python `None` as the latter, so `IS NULL` alone finds none of
them), and bodies at or below the 160-byte monotone floor, where the envelope
would make the row *bigger*.

## 5. Canonical replacement contract

The **same** `provenance_envelope()` the live writer calls — there is no
historical-only representation, and a test asserts the module never constructs
the marker itself:

```json
{"raw_payload_suppressed": true, "mode": "none",
 "source": "kalshi_rest", "bytes": 2049, "digest": "a1b2c3d4e5f60718"}
```

`bytes` and `digest` describe the **original** body, computed before it is
replaced. The full body is never preserved or reintroduced.

**Idempotency.** A reclaimed row no longer matches the eligibility predicate, so
re-running finds zero. Proven by test, including a partial-then-resume sequence.

## 6. Batch and stop policy

| Bound | Value |
|---|---|
| Rows per batch | 2,000 |
| Payload bytes per batch | 8 MiB |
| Batch duration | 10 s (exceeded → stop *after* committing) |
| Total run duration | 600 s |
| Max batches | 500 |
| Lock retries | 3, then stop |

Each batch: select eligible primary keys → build each envelope → `UPDATE` **with
a WHERE guard re-checking the row still holds a full body** (so a row a writer
changed since selection is skipped, not overwritten) → one commit → verify every
key is now suppressed → health check before the next batch.

**Stop conditions**, any of which end the run with no retry in the same session:
backup precondition fails (re-checked *between batches*, not just at the start),
latest MarketOps run in `error`, latest watcher run in `error`, lock retries
exhausted, batch over its duration bound, run over its total bound, verification
failure, batch exception, or health-check failure.

A partial run is valid and resumable — that is a property of the eligibility
predicate, not of bookkeeping.

## 7. Backup prerequisite

Reclamation is irreversible, so the backup is the entire recovery story. The
precondition reuses `SQLITE-BACKUP-FRESHNESS-ALERT-001`'s evaluator rather than
inventing a second notion of "good backup": healthy (which already proves
manifest-backed, structurally valid, `integrity_check=ok`, exact size match,
strict path confinement, expected Alembic revision) **and newer than 12 hours**.

Per the milestone boundary, no manual backup is created. If no sufficiently fresh
verified backup exists, the run stops and waits for the next scheduled one.

## 8. Tests and reviews

`tests/test_raw_payload_reclamation_001.py` — 68 tests over disposable databases
with representative multi-KiB payloads. Most assert something is *refused*.

Coverage includes: only registry names resolve and every pinned/ungoverned/
excluded name is rejected; both preservation floors, including the backup floor
overriding the age floor; already-suppressed, absent and too-small rows excluded;
the canonical envelope reused with the original byte count and digest; normalized
fields, other tables and row counts unchanged; the NOT NULL column staying valid;
row and byte batch limits; max-rows, batch-duration and total-runtime stops; lock
retry exhaustion leaving the database untouched; rollback changing nothing;
verification failure halting; health degradation stopping between batches; a row
changed since selection not being overwritten; idempotency and resumability; no
deletion, compaction, pragma, migration, provider call, timer or capital surface;
text/JSON parity; and secret-free, payload-free output.

## 9. Production authorization decision

_Pending dark deployment._

## 10. Production run

_Pending._

## 11. Verification

_Pending._

## 12. Logical versus physical

Reclaimed bytes become **reusable freelist pages**. The SQLite file length is a
high-water mark and **does not shrink**; `db_growth_warning` stays critical.

Only `SQLITE-COMPACT-COPY-001` changes the file size, and it is **not
authorized** by this milestone. Preferred future direction, unchanged:

```text
VACUUM INTO a new database on a separate volume
  → full verification
  → explicit maintenance-window swap
  → preserve the original database for rollback
```

## 13. Rollback and incident handling

There is **no in-place undo**. A reclaimed body is gone. Recovery is:

1. Stop the writers.
2. Restore the verified backup that satisfied the precondition — which, by the
   backup floor, is guaranteed to predate every reclaimed row.
3. Verify and restart.

That asymmetry is why the batches are small, why every batch verifies before the
next begins, why the backup is re-checked *between* batches rather than only at
the start, and why a stop condition ends the session rather than retrying.

If reclamation produces an unexpected result, **stop and do not re-run in the
same session** — the run is idempotent, so nothing is lost by pausing to
investigate.
