# DB-GROWTH-ALERT-IDENTITY-001 — stable database-growth alert identity

**Status:** implemented and reviewed; deployment recorded in §8 below.

## 1. The defect

`MarketOpsAlertService.create()` dedupes on `(alert_type, title, status=open)`. The
database-growth alert built its title from the measurement:

```python
f"Database at {size_mb:.0f} MiB (critical)"
```

So every 1 MiB of growth produced a *different identity* and therefore a *new open
row*. Measured on EVO-X2 on 2026-08-03:

| Fact | Value |
|---|---|
| Open `db_growth_warning` rows | **933** |
| Distinct titles among them | **933** (exactly one row per title) |
| Resolved rows | 0 — there was no resolution path at all |
| Span | 2026-07-05 01:02:39Z .. 2026-08-03 00:03:37Z |
| Severity split | 577 warning (512 MiB era) / 356 critical |

The backlog pinned `marketops-report`'s recommended action to *"Investigate 933 open
warning/critical alert(s)"* and pushed every other alert type out of the newest-10 and
newest-20 operator views.

A useful accident: because each row carried `evidence.size_mb`, the backlog is also a
30-day time series of the database size. It is the basis of the growth attribution in
[`RETENTION_COVERAGE_2026_08.md`](RETENTION_COVERAGE_2026_08.md).

## 2. What changed

**One stable identity.** `ALERT_DB_GROWTH_TITLE = "Database growth above threshold"` —
no digits, no measurement. The size, thresholds, severity and observation time live in
the message and `evidence`, which are refreshed in place.

**The alert type is unchanged** (`db_growth_warning`), so all 933 rows of history stay
queryable and no migration is needed.

**Unchanged on purpose:** the size calculation (`database_size_mb`, still
`os.path.getsize`), both thresholds (1536 / 3072 MiB, still read from settings), and both
severity bands (`>= critical` → critical, `>= warning` → warning). Parity is asserted at
and around both boundaries.

**A real lifecycle**, replacing create-only:

| State | Behaviour | `alert_action` |
|---|---|---|
| unhealthy, none open | create exactly one | `created` |
| unhealthy, one open | refresh severity/message/evidence in place | `updated` |
| unhealthy, several same-title | converge to one, resolve the rest | `updated` |
| healthy | resolve the canonical row **and** strict legacy duplicates | `resolved` |
| repeated healthy | nothing written | `none` |
| size unmeasurable | change nothing — **never resolve** | `none` |
| evaluation error | record and swallow — **never resolve** | — |

The last two matter: an evaluation that could not measure the database has no business
closing a critical alert.

**Failure isolation.** The evaluation moved out of `_health_alerts` into step 7a, writing
on its own short-lived session (the pattern established by the backup-freshness hook, and
for the same reasons — see `SQLITE_BACKUP_FRESHNESS_ALERT_001.md` §8). Any exception is
caught inside the helper and recorded in `run.summary["db_growth_error"]`; it cannot fail
the cycle, including under `fail_fast`.

**The step-7 checkpoint commit is now unconditional.** It was previously guarded by
`include_backup_freshness_alert`. Both 7a and 7b write on isolated sessions, so the shared
session must release the SQLite write lock first. This is a deliberate, documented change
to the cycle's transaction contract; it is consistent with the already-documented
"checkpoint-committed, not atomic" boundary at stage 4a, and only summary/status/timings
follow it.

**Healthy-path resolution is title-scoped**, not type-scoped: an unattended 5-minute timer
may only close identities this milestone owns (canonical + strict legacy). A hand-written
`db_growth_warning` row an operator pinned is never auto-closed.

## 3. Discoverability — why the run summary, not just the alert row

Reconciliation promotes the **oldest** legacy row as canonical, so the surviving alert has
one of the *lowest* ids in the table. `marketops-report` and `marketops-alerts` both order
`id DESC` with small limits, and 4,343 open info-level `source_backed_forecast_created`
rows keep accruing higher ids.

Left alone, the repair would have made a 4.4 GB critical condition **less** visible than
the bug it replaced — `recommended_action` degrades to *"No action needed"*. So the
evaluation also publishes `run.summary["db_growth"]`, which `MarketOpsReportService`
surfaces as `MarketOpsReport.db_growth` and `_recommend` names ahead of the generic
open-alert count. `test_growth_stays_visible_after_reconciliation` pins this.

This is a workaround for a wider problem, not a fix for it — see §9.

## 4. Legacy matching

```python
LEGACY_DB_GROWTH_TITLE_RE = re.compile(r"Database at [0-9]+ MiB(?: \(critical\))?")
```

Anchored `fullmatch`, always paired with `alert_type == ALERT_DB_GROWTH`, and never a
substring or SQL `LIKE`. `[0-9]` rather than `\d`, because `\d` matches every Unicode
decimal digit — `Database at ٤٢٦٢ MiB` would otherwise match a title our code cannot
produce. A non-string title (SQLite's dynamic typing permits one) returns `False` rather
than raising mid-read. Both legacy spellings that ever existed are covered.

## 5. Reconciliation

```bash
python -m app.cli marketops-reconcile-db-growth-alerts --dry-run --format json
python -m app.cli marketops-reconcile-db-growth-alerts --confirm
```

Dry-run is the default; `--dry-run --confirm` together is safe (dry-run wins).

- Only rows with `alert_type = 'db_growth_warning'` **and** a canonical or strict-legacy
  title are ever written. Everything else is counted as `excluded_unmatched` and reported.
- **Duplicates are RESOLVED, never deleted.** No row is hard-deleted; the full history
  stays queryable.
- Canonical selection: an existing open row already carrying the canonical title (lowest
  id); otherwise the **oldest** legacy row is promoted, so the alert keeps the `created_at`
  at which the condition was first observed. The earliest observation across all matched
  rows is preserved in `evidence.condition_first_observed_at`, and the cycle hook carries
  it forward on every later refresh.
- One atomic transaction; any failure rolls back.
- The report carries `resolved_at` and the complete `resolve_ids`, so the operation has a
  precise inverse (§7).

## 6. Runbook

1. Confirm `sqlite-backup-freshness-report` is **healthy** and a recent verified artifact
   exists.
2. Confirm `alembic current` is at head — the CLI runs `run_migrations()` as a side effect,
   as every command in this repo does.
3. **Do not run `--confirm` between 01:25 and 01:45 UTC.** The backup's stepped online copy
   restarts whenever another connection writes; a write inside that window would force a
   restart of a 4.4 GB copy. MarketOps contention itself is not a concern — the
   reconciliation is a single batched `UPDATE` measured at tens of milliseconds, against a
   30 s busy timeout.
4. Run `--dry-run --format json` and **save the output**.
5. Verify `excluded_unmatched == 0`, `matched_legacy` equals the expected backlog, and
   `remaining_open_after` is what you intend.
6. Run `--confirm`. Save the output — it contains `resolved_at` and `resolve_ids`.
7. Verify: exactly one open canonical row, zero open legacy rows, backup-freshness alert
   untouched, unrelated types untouched, row count unchanged (no deletes).
8. Observe the next natural MarketOps cycle and confirm it **refreshes** the canonical row
   rather than creating another.

**Order matters slightly.** Deploy first and let one cycle run, so the cycle mints the
canonical row and reconciliation prefers it (`canonical_source=existing_canonical`); or
reconcile first and the oldest legacy row is promoted. Either converges to one row; the
first keeps the alert nearer the top of `id DESC` views, the second keeps the true
first-observation `created_at`. §3 makes visibility independent of that choice.

## 7. Rollback

`--confirm` only flips `status`/`resolved_at`, so the inverse is exact and does not require
restoring a backup:

```sql
UPDATE marketops_alerts
   SET status = 'open', resolved_at = NULL
 WHERE alert_type = 'db_growth_warning'
   AND resolved_at = '<resolved_at from the report>';
```

The CLI prints this statement, fully populated, after a confirmed run.

Reverting the *code* restores the old create-only behaviour; the canonical row would then
simply stop being refreshed and a new per-MiB row would be minted again.

## 8. Deployment

*(Filled in from measured evidence, after the event.)*

_Pending._

## 9. Known remaining problem — this is not the whole swamp

`marketops-alerts` is still dominated by other alert identities:

| Alert type | Open rows | Cause |
|---|---|---|
| `source_backed_forecast_created` | 4,343 | per-ticker identity by design, **no resolution path at all** |
| `too_many_signals` | 56 | title embeds the hourly count — the same identity bug |
| `champion_challenger_sample_update` | 32 | title embeds `X -> Y` — the same identity bug |

Fixing database growth alone does not restore the operator's view; it removes the one alert
that was still forcing the growth condition into it, which is why §3 exists. Auto-resolving
info-level alerts after N days, adding a `--severity` filter to `marketops-alerts`, and
applying this milestone's title pattern to `too_many_signals` and
`champion_challenger_sample_update` are the follow-up.
