# RAW-PAYLOAD-RECLAMATION-001 — proposal (NOT authorized, NOT executed)

Successor to [`RAW_PAYLOAD_STORAGE_001.md`](RAW_PAYLOAD_STORAGE_001.md), which
stopped writing unread provider bodies **prospectively** and deliberately left
every historical row untouched.

**Nothing in this document has been run.** It exists so the next milestone starts
from measurements rather than assumptions.

## What is eligible

Only the seven **governed** columns, never the four pinned ones, and never a
column outside `raw_payload_policy.ALL_CLASSIFIED_COLUMNS`. Measured on the
2026-08-04 snapshot, historical bodies written before activation:

| Column | Rows | Historical MiB |
|---|---:|---:|
| `market_price_ticks.raw_payload` | 426,300 | 833.9 |
| `market_snapshots.raw_payload` | 153,943 | 325.9 |
| `crypto_token_discovery_events.raw_payload` | 313,445 | 224.0 |
| `opportunity_signals.raw_payload` | 33,415 | 63.2 |
| `market_detail_enrichments.raw_*` (3 cols) | 12,384 | 48.1 |
| **Total** | | **~1,495** |

`market_price_ticks` drains itself: its 2-day retention window means the
historical bodies are gone within 48 h of activation with no action at all. The
durable ~660 MiB is in the never-pruned columns.

## Required design

- **Prerequisite:** a fresh verified backup, `sqlite-backup-freshness-report`
  healthy, and the run outside 01:25–01:45 UTC.
- **Mechanism:** `UPDATE ... SET col = <envelope> WHERE <governed> AND NOT
  suppressed`, batched. `capture()` is already idempotent, so re-running is safe
  and a partially-completed batch is not a corrupt state.
- **Batching:** sized against writer contention. The db-growth reconciliation
  measured a 956-row batched UPDATE at 0.64 s; 400k rows is three orders of
  magnitude larger and must not be one transaction. Bound each batch by rowid
  and commit between batches so the 60 s watcher and 5-minute MarketOps keep
  their write windows.
- **The envelope needs the original bytes.** Reclamation must read each body to
  compute `bytes`/`digest` before replacing it — so the read cost is the full
  ~1.5 GiB regardless. Consider recording only `bytes` and dropping `digest` for
  historical rows, since there is no replayable provider response to match.
- **Rollback:** there is none. This is the one genuinely destructive step in the
  sequence, and the backup is the only recovery path. That asymmetry should
  drive the batch size and the verification between batches.
- **Verification:** row counts unchanged, normalized columns untouched, no
  pinned column written, `raw-payload-storage-report --full-scan` on a copy
  before and after.

## What it will NOT do

Free a single byte of the file. Every reclaimed page goes to the freelist. The
`db_growth_warning` alert stays critical. Only compaction changes that.

---

# SQLITE-COMPACT-COPY-001 — proposal (NOT authorized, NOT executed)

Must remain a separate milestone, and should run **after** reclamation —
compacting first would simply re-compact ~1.5 GiB of unread payload.

Preferred direction:

1. `VACUUM INTO '/mnt/data/<scratch>/compacted.db'` — builds a compacted copy
   under a read transaction **without** blocking writers, unlike in-place
   `VACUUM` which takes an exclusive lock for the whole rewrite and needs ~2x
   the database in temporary space.
2. Verify the copy independently: `integrity_check`, table counts reconciled
   against the live source, Alembic revision, and the `verify-db-backup` path.
3. **Swap under explicit approval, in a maintenance window** — stop the writers,
   re-verify, replace, restart. This is the risky step and it is the one that
   needs a human present.

Neither is authorized by RAW-PAYLOAD-STORAGE-001.
