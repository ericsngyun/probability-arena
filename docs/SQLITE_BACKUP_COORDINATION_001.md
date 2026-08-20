# SQLITE-BACKUP-COORDINATION-001 — verified, coordinated SQLite backups

Read-only research infrastructure. This milestone protects the production
SQLite database; it changes no schema, no pragma, no transaction boundary, no
provider policy, and no forecasting or scoring behaviour.

## Why this was urgent

Live inspection of EVO-X2 on 2026-08-02 found:

- `probability-arena-backup.timer` **had never been installed** — 0 unit files,
  `is-enabled` returned `not-found`. Canon listed it as an expected service.
- The newest backup on disk was `backup-20260710T042831Z.db.gz` — **23 days
  stale**, predating the readiness measurement window, the exact-cycle anchor
  feed, the entire CANARY-004 lifecycle, and the forecast stack.
- Backups were written to `data/backups`, i.e. **the same pressured root volume**
  as the 4.19 GiB live database, while `/mnt/data` sat at 5% used.
- The backup directory was mode **0755** (world-readable).

The unit templates existed in `infra/systemd/user/` and were documented as
"Optional daily timer (NOT auto-installed)" — so canon was not fabricating a
service, the install step had simply never been performed.

## Audit of the pre-existing backup path

`backup-db` / `list-db-backups` / `verify-db-backup` (OPS-007) already did the
single most important thing correctly: **it used sqlite3's online backup API**
(`source.backup(snapshot)`), which is consistent under concurrent writers. It is
not a raw `cp` of a live database. That core is retained unchanged.

The gaps, each a real exposure:

| Gap | Consequence |
| --- | --- |
| gzip written straight to the final filename | an interrupted run published a truncated file that looked like a valid backup |
| no verification before publication | a corrupt artifact was reported as success |
| no manifest | no hash, no provenance, no way to detect silent bit-rot |
| no capacity gate | a full disk produced a partial artifact |
| no overlap prevention | two runs could interleave on the same destination |
| dir 0755, files 0644 | world-readable database contents |
| uncompressed snapshot in the system temp dir | a ~4 GiB write onto an unrelated, possibly pressured volume |
| retention could delete the **only** backup | latent data-loss bug (see below) |

## Backup correctness contract

```text
capacity gate  ->  exclusive backup lock  ->  online-backup snapshot
  ->  gzip to <target>.part  ->  verify  ->  manifest  ->  atomic os.replace
  ->  bounded tiered retention  ->  success telemetry
```

A backup is **not successful until verification passes**. Verification checks:
artifact exists, non-zero, not a symlink; gzip readable; `SQLite format 3`
header; `PRAGMA integrity_check == ok`; required core tables present
(`markets`, `market_forecasts`, `crypto_tokens`, `marketops_runs`); Alembic
revision readable; bounded table counts queryable; opens read-only; path outside
the live database; sha256 recorded and matched in the manifest. **Migrations are
never run against a backup.**

Publication is atomic: the artifact is built as `<name>.part` in the destination
directory and moved into place with `os.replace` (same directory, therefore same
filesystem). A failed verification unlinks the partial and publishes nothing.

## Artifact and manifest

Repository convention is retained — `backup-<UTC stamp>.db.gz` — rather than the
milestone's *suggested* `probability-arena-<stamp>.sqlite3`, because eight
existing backups already use it and `list_backups` matches on it. The manifest is
a sibling `backup-<UTC stamp>.manifest.json` (mode 0600) recording:
`manifest_version`, `created_at`, `source_database_path_redacted`,
`source_database_bytes`, `backup_filename`, `backup_bytes`, `sha256`,
`alembic_revision`, `sqlite_version`, `journal_mode`, `integrity_check`,
`required_tables_present`, `selected_table_counts`, `backup_duration_ms`,
`verification_duration_ms`, `host`, `application_commit`, `status`.

The source path is redacted to a bare filename. No secrets, credentials,
provider keys or payloads, environment variables, or row contents are recorded —
enforced by test.

## Destination, permissions, capacity

Destination is configurable via `BACKUP_DIR`. On EVO-X2 it is set to
`/mnt/data/probability-arena-backups`: a **distinct ext4 filesystem** with
712 GiB free (5% used), persistent in `/etc/fstab`, owned by `<REMOTE_USER>`.
This deliberately moves backups **off** the pressured root volume that already
holds the 4.19 GiB database and is at 61%.

Permissions: directory `0700`, artifacts and manifests `0600`. A symlinked backup
root is rejected outright.

Capacity is checked **before writing** and fails closed:

```text
required = 2 x database_bytes + 512 MiB margin
```

(2× because the uncompressed snapshot and its gzip coexist briefly.) A shortfall
returns `skipped_capacity` — it deletes nothing, never retries, and never touches
the live database or the newest backup.

## Concurrency and writer coordination

Overlap is prevented by a dedicated **`flock`** on a coordination-only file
(`.backup.lock`, opened `O_NOFOLLOW`) inside the backup root. It never touches
SQLite's own locking. It is **non-blocking**, so there is no unbounded wait and
no retry loop; a contended run returns `skipped_overlap` immediately, which is
explicitly *not* counted as a successful backup. Because the kernel releases
`flock` when the holding process dies, a crashed run cannot leave a stale lock —
there is no PID file and no TOCTOU check.

The backup coexists with MarketOps, the watcher daemon, meme-news, tick
aggregation, retention, the baseline scanner, and horizon observations. Nothing
is stopped, paused, or put into a maintenance mode; no pragma, transaction
boundary, or retry behaviour changes. The online backup API is the coordination
mechanism.

## Retention policy — two deliberate behaviour changes

**1. The newest backup is never deleted.** The previous mtime-based policy would
delete the sole remaining backup once it aged past `BACKUP_RETENTION_DAYS`,
leaving zero backups. That was a latent data-loss bug.

**2. Tiered, with a recency floor.** Retention is now driven by the filename
stamp (not mtime): **7 daily / 4 weekly / 3 monthly**, plus a floor that always
keeps the **newest 7 backups whatever day they fall on**. Without the floor, an
operator's manual backup taken immediately before a risky change would be pruned
by that night's scheduled run.

Deletion is confined to the backup root; strict-name-matched (prefix + parseable
UTC stamp + suffix); rejects symlinks, traversal, and non-regular files; removes
a backup's manifest alongside it; and runs **only after a verified publication**.
Anything unrecognised is reported as `skipped`, never deleted.

Both changed OPS-007 tests were updated in place with comments explaining the
change — nothing was silently altered.

## CLI contract

```bash
python -m app.cli backup-db --dry-run     # capacity + retention plan; creates and deletes nothing
python -m app.cli backup-db               # verified backup (unchanged default; the shipped unit uses this)
python -m app.cli backup-db --confirm     # explicit form, same behaviour
python -m app.cli list-db-backups
python -m app.cli verify-db-backup <path>
python -m app.cli prune-db-backups --dry-run   # reports retained/deleted/skipped
python -m app.cli prune-db-backups --confirm   # only this deletes
```

`backup-db` bare is deliberately unchanged so the shipped systemd unit keeps
working. Destructive pruning requires `--confirm`.

## Systemd design

`infra/systemd/user/probability-arena-backup.{service,timer}` — user-level only,
no root, no sudo. `Type=oneshot`, no `Restart=always`, no loop inside the
service; the timer owns recurrence with `Persistent=true`,
`OnCalendar=*-*-* 01:30:00`, `RandomizedDelaySec=600`.

Schedule chosen against live timer evidence: it avoids the crowded
00:00–00:06 window (retention 00:00:32, baseline 00:04) and the hourly
tick-aggregation slot at `:22` — tick-aggregation being the only writer that has
ever produced a database-locked event. 01:30 UTC = 18:30 America/Los_Angeles.

Units are rendered to a temporary directory and checked with
`systemd-analyze verify` (exit 0 required) before installation.

## Telemetry

Reuses the SQLITE-LOCK-TELEMETRY-001A backup instrumentation unchanged: one
reader event per real backup, with outcome, durations, `database_bytes`,
`filesystem_free_bytes`, journal/synchronous samples, and `external_calls=0`.
Telemetry goes to the JSONL sink and is **never written into the application
database**. Instrumentation is not broadened to any other writer.

## Restore runbook (procedure only — do not execute without explicit authorisation)

Restoring is a destructive, human-authorised operation. This milestone does not
perform one.

1. **Stop the writers.** `systemctl --user stop probability-arena-marketops.timer
   probability-arena-meme-news.timer probability-arena-tick-aggregation.timer
   probability-arena-retention.timer probability-arena-baseline.timer
   probability-arena-watcher.service` — timers first, then the daemon.
2. **Confirm no active SQLite process:** `fuser data/probability_arena.db` (or
   `lsof`) returns nothing, and no horizon one-shot units are pending.
3. **Select a verified backup:** `list-db-backups`, then read its manifest.
4. **Re-verify before trusting it:** `verify-db-backup <path>` must print `OK`,
   and the artifact sha256 must match `sha256` in the manifest.
5. **Preserve the damaged database** — rename, never delete:
   `mv data/probability_arena.db data/probability_arena.db.incident-<UTC stamp>`.
6. **Restore to a temporary path first:**
   `gunzip -c <backup> > data/restore-candidate.db`.
7. **Verify the candidate:** `PRAGMA integrity_check`, confirm the required
   tables, compare `selected_table_counts` against the manifest, and read
   `alembic_version` — it must match the application's expected revision.
   **Do not run migrations against a restored backup as part of recovery.**
8. **Atomically replace only under explicit human authorisation:**
   `mv data/restore-candidate.db data/probability_arena.db`.
9. **Restart services** in the reverse order they were stopped.
10. **Verify health:** one natural MarketOps cycle `ok` with `stage_errors={}`,
    watcher active, `db-stats` sane, Alembic revision as expected.
11. **Retain incident evidence:** keep the preserved damaged database, the
    manifest, the telemetry JSONL slice, and the journal for the window.

**Rollback / uninstall of this milestone:**

```bash
systemctl --user disable --now probability-arena-backup.timer
rm ~/.config/systemd/user/probability-arena-backup.{service,timer}
systemctl --user daemon-reload
# optionally remove the BACKUP_DIR line from .env to restore the default
```

Removing the units stops all scheduled backups and touches nothing else; existing
backup artifacts are inert files and are never deleted by uninstalling.
Reverting the code is `git revert` of this milestone's commits — the backup path
returns to the OPS-007 behaviour, and existing artifacts remain readable because
the filename convention is unchanged.
