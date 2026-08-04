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

> **Read §9 before acting on this section.** The dependency conclusion below is
> correct and was independently re-derived — but "no reader" turned out not to be
> a sufficient eligibility test for an irreversible operation. The field-level
> inventory in §9.1 is the one that decides the outcome.

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

**Pinned columns are structurally unreachable.** `resolve_target()` refuses
anything that is in `PINNED_FULL`, is not in `GOVERNED_COLUMNS`, or is not in the
reclamation registry. There is no table/column CLI interface at all — the command
takes a reviewed registry *name*.

It validates the **target the entry points at**, not the key that found it, and
that distinction was a review finding rather than the original design. Checking
the key fails *open*: a rename or copy-paste that updated the dataclass and not
the dict key would have let a pinned column through — and the pinned column with
the only real audit dependency in the repo is
`crypto_token_risk_assessments.raw_payload`, which `provider_budget` runs a SQL
`LIKE` over for SolanaTracker request accounting. A mismatched entry is now
refused outright.

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
| Rows per batch | 500 |
| Payload bytes per batch | 8 MiB |
| Batch duration | 10 s (exceeded → stop *after* committing) |
| Total run duration | 600 s |
| Max batches | 500 |
| Lock retries | 3 per RUN, then stop |
| Inter-batch pause | ≥ 0.5 s, and at least as long as the batch took |

`test_documented_bounds_match_the_code` asserts this table against the constants,
because this document is the production authorization artifact and drift in it is
a defect rather than a typo.

**Why 500 and not 2,000.** Measured, a 500-row batch holds the exclusive lock
~0.2–1.5 s on production-class hardware — comfortably inside the 30 s busy
timeout. The lock *window* was never the main risk; the **duty cycle** was.
Without a pause the loop would hold the write lock roughly half the wall time for
up to ten minutes, on a host that has recorded **4 `database_locked` events in
its entire history**. Hence the inter-batch pause: every other writer is
guaranteed a lock-free window between batches.

Each batch: select eligible primary keys → build each envelope → `UPDATE` **with
a WHERE guard re-checking the row is still non-NULL and not already suppressed**
→ one commit → verify every key is now suppressed → health check → pause.

Batches advance by a **primary-key high-water mark**. Without it every batch
would re-scan from the first row skipping everything already reclaimed —
O(n²) over a 150k-row run, re-reading payload pages each time.

Precisely on the guard: it skips a row that has become *suppressed or NULL* since
selection. It does **not** detect a row whose body was *replaced* by a different
body in that window — such a row is overwritten, with an envelope whose `bytes`
and `digest` describe the body that was selected. These tables are effectively
insert-only so the practical risk is nil, but the guarantee is stated as it is
rather than as the stronger claim an earlier draft made.

**Stop conditions**, any of which end the run with no retry in the same session:
backup precondition fails (re-checked *between batches*, not just at the start),
newest non-`running` MarketOps run not `ok` (`partial` counts as degraded),
newest watcher run not `ok`, **watcher silent for >180 s**, lock retries
exhausted, batch over its duration bound, run over its total bound, verification
failure or error, batch exception, or health-check failure.

The health gate is shaped after `crypto_horizon_orchestrator`'s rather than a
looser "is the newest row an error" test, because the looser test cannot see the
harm reclamation can actually cause. A `running` row would **mask** a prior
errored one; `partial` is the likeliest symptom of lock contention; and a starved
writer may never manage to write `status="error"` at all, because the database is
locked — so liveness is checked by recency.

A partial run is valid and resumable — that is a property of the eligibility
predicate, not of bookkeeping.

## 7. Backup prerequisite

Reclamation is irreversible, so the backup is the entire recovery story. The
precondition reuses `SQLITE-BACKUP-FRESHNESS-ALERT-001`'s evaluator rather than
inventing a second notion of "good backup", **and newer than 12 hours**.

Be precise about what that evaluator establishes, because an earlier draft of
this section overstated it. Re-verified from the filesystem, now: the manifest is
committed and marked `verified`; the artifact's size matches the manifest exactly;
the file is a real file (not a symlink) inside the backup root; it carries gzip
magic. **Inherited from the manifest, not re-proven:** `integrity_check=ok`, which
was recorded when the backup ran. The evaluator deliberately does not re-hash,
decompress, or open the artifact as SQLite — a cost decision made in that
milestone. If you want that guarantee before an irreversible run, `verify-db-backup`
does the full-hash pass and is a separate, explicit command.

**Schema agreement is now checked here and was not before.** The manifest's
Alembic revision is compared against the live `alembic_version`; a mismatch means
restoring that artifact would not reproduce the database being mutated, and the
precondition refuses. Absence of the table (a `create_all` database) is not a
mismatch.

Per the milestone boundary, no manual backup is created. If no sufficiently fresh
verified backup exists, the run stops and waits for the next scheduled one.

**This pins the operating window, which is worth stating explicitly.** The backup
runs daily at 01:30 UTC and the age is measured from the manifest's `created_at`
(the run's *start*), so the 12-hour rule means reclamation can only run between
roughly **01:40 and 13:30 UTC**. Within that, avoid 01:25–01:45 (the backup's own
coordinated copy) and the top of each hour (tick aggregation). A run outside the
window is refused by the precondition, not merely discouraged.

**Do not start two runs concurrently.** The UPDATE guard makes it safe — the
second run's updates match zero rows — but it doubles the lock pressure for no
benefit.

## 8. Tests and reviews

`tests/test_raw_payload_reclamation_001.py` — 98 tests over disposable databases
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

Added after the third review: the three classification sets partition
`GOVERNED_COLUMNS` exactly (a governed column can no longer be silently neither
reclaimable nor excluded); the discovery-event envelope's `source` is pinned to
the live adapter's `source_name`; the marker match is literal rather than `LIKE`
(the marker is full of `_`, which is a LIKE wildcard, so a body containing
`rawXpayloadXsuppressed` used to count as already-suppressed); a schema-mismatched
backup is refused; the read-only report still projects when no backup exists while
the mutating path still fails closed; reclamation refuses a session carrying
unrelated pending work, because it commits; and an unknown target is a clean
refusal with exit code 2 rather than a traceback.

**Three independent adversarial reviews** (security, operations, dependency/data
correctness) were run and their findings applied. The third review re-derived the
dependency conclusion independently rather than trusting the earlier audit, and it
held: no production reader exists for any of the six columns. It also proved, over
the schema rather than by string-matching the source, that every registry UPDATE
writes exactly one column, that no target table carries an `onupdate` column or a
trigger, and that every deviation in the eligibility predicate errs toward skipping
rather than destroying.

The finding that changed the outcome was **not** a correctness defect. See §9.

## 9. Production authorization decision

**RECOMMENDATION: DO NOT AUTHORIZE A RECLAMATION RUN.**

The implementation is complete, reviewed three times, and safe. That is not the
question. The question is whether the rows are *conclusively eligible*, and a
measurement taken at the last gate says they are not.

### 9.1 What was measured

The third review observed that "no runtime reader" is the right test for a
**reversible** prospective policy and a weaker test for **irreversible**
destruction, and that no field-level inventory of body *contents* had ever been
done — only of body *readers*. That gap was real, so the inventory was done:
a read-only, `PRAGMA query_only=ON`, bounded sample (300–400 rows per column, at
the oldest, middle and newest of each table) against production.

| Column | Body size | Distinct top-level keys | Keys captured by a normalized column |
|---|---:|---:|---:|
| `market_snapshots.raw_payload` | ~2.0–2.4 KB | 42–45 | ~15 |
| `opportunity_signals.raw_payload` | ~2.0–2.3 KB | 42–45 | ~15 |
| `market_detail_enrichments.raw_market_detail` | — | 45 | 2 |
| `market_detail_enrichments.raw_series_detail` | — | 14 | 2 |
| `market_detail_enrichments.raw_event_detail` | — | 13 | 4 |
| `crypto_token_discovery_events.raw_payload` | ~550–660 B | 13 | 0 |

The shape is stable across the full age range of every table, so this is not an
artifact of sampling recent rows.

### 9.2 What would actually be destroyed

The normalized columns are a genuine superset of what any *current* consumer
reads — the dependency audit is correct and I am not walking it back. But they
are a strict *subset* of what the bodies contain, and the difference is not
decoration. Two classes:

**Per-observation quantitative state that exists nowhere else and can never be
re-fetched.** For the two Kalshi bodies: `previous_price_dollars`,
`previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `yes_bid_size_fp`,
`yes_ask_size_fp`, `price_ranges`, `price_level_structure`,
`notional_value_dollars`, and the per-snapshot `status`/`result`. Top-of-book
**size** is the sharpest example: `market_snapshots` stores `liquidity`, and
`app/adapters/kalshi.py:84-87` only consults the size fields to *derive*
liquidity when Kalshi omits it. The depth itself is never stored. For a
probability-research repository, per-snapshot top-of-book depth and previous-tick
prices are research data, not debugging residue. For
`crypto_token_discovery_events`, the same applies to `amount` / `totalAmount` —
the DexScreener **boost magnitude** at discovery, which the table's columns
(`chain`, `event_type`, `pair_address`, `source`, `token_address`, `observed_at`)
do not carry at all, and which is squarely in the subject matter of the crypto
tape and birth-anchor work.

**Static market metadata that is re-derivable only while the market is live.**
`strike_type`, `floor_strike`, `custom_strike`, `settlement_timer_seconds`,
`can_close_early`, `early_close_condition`, `rules_secondary`, `market_type`,
`expiration_value`, `response_price_units`. §4 justified the enrichment floor as
"re-derivable by re-fetching the market". That is true for a live market and
false for a settled or delisted one — which, on a 7-day-old row, is much of the
population.

### 9.3 Why this changes the answer rather than being a caveat

The milestone authorizes reclamation of "only conclusively eligible columns and
rows", and separately forbids reclaiming "unresolved dependency columns". Applied
honestly to the measurement above, **no target in the registry clears that bar**
today. Not because a reader was missed, but because "no reader" was never the
same claim as "no value", and the gap between them was never measured until now.

Reclaiming would free ≈665 MiB logically, which does not shrink the file at all
(§12) — the payoff is deferred entirely to a future compaction milestone. Trading
permanently unrecoverable research fields for headroom that only materializes
after a *separate* unapproved step is a bad trade, and it is a trade that cannot
be un-made.

### 9.4 What is genuinely needed instead

This is a judgement call about research value, and it is Eric's, not mine. The
options, cheapest first:

1. **Decide the unnormalized fields have no research value**, record that, and
   authorize the run as specified. Everything is built and dry-runnable.
2. **Normalize what matters first** — promote `yes_bid_size` / `yes_ask_size` /
   `previous_*` to real columns on `market_snapshots`, and boost `amount` /
   `totalAmount` on `crypto_token_discovery_events` — then reclaim the bodies
   with nothing lost. A separate, reversible, additive milestone.
3. **Reclaim only where the delta is genuinely inert.** Judged narrowly, the
   strongest candidate is `market_detail_enrichments.raw_event_detail`
   (6.6 MiB) — the least valuable and the smallest. That is not worth an
   irreversible production run on its own.
4. **Do nothing here and go straight to compaction.** `SQLITE-COMPACT-COPY-001`
   reclaims the freelist that prospective suppression is already generating,
   destroys nothing, and is fully reversible by keeping the source file. Note
   that **prospective suppression is already doing the durable work**: ~399
   MiB/day of bodies are no longer being written, and `market_price_ticks`
   self-clears 877 MiB every 48 h through retention.

**Recommended: 4, then 2 if the fields prove worth keeping.** Reclamation buys
logical space the file does not yet give back, at a permanent and now-measured
cost.

## 10. Production run

**Not performed.** No historical row has been modified. The only production
access taken under this milestone was read-only: `PRAGMA query_only=ON` samples
for §9.1, and the dry run below, which writes nothing.

### Production dry run — 2026-08-04T06:38:06Z, EVO-X2 at `f6184ee`

`external_calls=0  persisted=false  rows_changed=0`
Backup prerequisite **OK** — `backup-20260804T013626Z.db.gz`, 18,100 s old,
alembic `0027` matching live.

| Target | Rows | Eligible | MiB now | MiB envelope | **MiB net** |
|---|---:|---:|---:|---:|---:|
| `crypto_token_discovery_events.raw_payload` | 315,928 | 156,970 | 118.8 | 17.7 | **101.2** |
| `market_snapshots.raw_payload` | 154,767 | 119,355 | 251.1 | 13.4 | **237.6** |
| `opportunity_signals.raw_payload` | 33,665 | 25,324 | 47.7 | 2.8 | **44.9** |
| `market_detail_enrichments.raw_market_detail` | 12,484 | 9,452 | 18.4 | 1.1 | **17.4** |
| `market_detail_enrichments.raw_series_detail` | 12,484 | 9,452 | 13.3 | 1.1 | **12.3** |
| `market_detail_enrichments.raw_event_detail` | 12,484 | 9,452 | 5.0 | 1.1 | **3.9** |
| **Total logical bytes reclaimable** | | | | | **417.2** |

**These numbers supersede §3.** §3's ≈664.7 MiB was the whole-column inventory
from `RAW-PAYLOAD-STORAGE-001`; 417.2 MiB is what is actually eligible once the
preservation floors, the already-suppressed rows and the envelope's own cost are
applied. The gap is the point: a third of the headline figure was never
reclaimable.

It is also 417.2 MiB of *logical* space in a 4.4 GiB file that does not shrink
(§12) — set against the permanently destroyed fields inventoried in §9.2.

## 11. Verification

Not applicable — nothing was applied. The tooling is deployed dark and inert:
`raw-payload-reclamation-report` and `raw-payload-reclaim` (dry run) are
read-only, and `--confirm` is the only mutating path.

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
2. Restore the verified backup that satisfied the precondition — recorded as
   `authorizing_backup` in the run result and the audit line, and guaranteed by
   the backup floor to predate every reclaimed row.
3. Verify and restart.

**Be clear about what that costs.** Restoring discards *every* write to *every*
table since that backup — up to 12 hours of ticks, snapshots, MarketOps runs and
crypto tape state — not just the reclaimed column. The restore is effectively
unusable as a remedy for a partial mistake, which is exactly why the batches are
small, why each verifies before the next begins, and why any stop condition ends
the session rather than retrying.

Every confirmed run appends one bounded, payload-free, secret-free JSONL line to
`~/probability-arena-reclamation/runs.jsonl` carrying the target, row count,
primary-key range, effective cutoff, authorizing backup and stop reason. Terminal
scrollback is not a record for an irreversible operation, and a killed process
would otherwise leave none at all.

That asymmetry is why the batches are small, why every batch verifies before the
next begins, why the backup is re-checked *between* batches rather than only at
the start, and why a stop condition ends the session rather than retrying.

If reclamation produces an unexpected result, **stop and do not re-run in the
same session** — the run is idempotent, so nothing is lost by pausing to
investigate.
