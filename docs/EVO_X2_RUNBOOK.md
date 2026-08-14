# EVO_X2_RUNBOOK

Host `mikolabs` (Tailscale alias `evo-x2`), user `miko_node_001`, repo at
`~/projects/probability-arena`, `.venv` inside, SQLite at `data/probability_arena.db`.
**Shared production host** — user-level systemd only, never touch other projects'
services or the awaas Docker stack. See `DEPLOYMENT_AUDIT_EVO_X2.md` /
`DEPLOYMENT_REPORT_EVO_X2.md` for history.

## Deployed services (systemd --user; lingering enabled)

| Unit | Cadence | Purpose |
|---|---|---|
| `probability-arena-baseline.timer` | every 4 h | full read-only measurement loop |
| `probability-arena-retention.timer` | daily | prune operational tables |
| `probability-arena-watcher.service` | continuous, 60 s | ticks + informational signals |

> Deployment lag is normal: check `git -C ~/projects/probability-arena log --oneline -1`
> on the host before assuming main is deployed (as of OPS-005 the host is on
> `eeb799d`; OPS-004/MVP-004E/MVP-004F are pending rollout).

## Status

```bash
systemctl --user list-timers | grep probability
systemctl --user status probability-arena-baseline.timer probability-arena-retention.timer probability-arena-watcher.service
cd ~/projects/probability-arena && .venv/bin/python -m app.cli pipeline-status
cd ~/projects/probability-arena && .venv/bin/python -m app.cli db-stats
```

## Logs

```bash
journalctl --user -u probability-arena-baseline.service  -n 100 --no-pager
journalctl --user -u probability-arena-retention.service -n 50  --no-pager
journalctl --user -u probability-arena-watcher.service   -n 100 --no-pager   # PYTHONUNBUFFERED set in unit
```

## Disable / re-enable

```bash
systemctl --user disable --now probability-arena-watcher.service    # stop 60s loop
systemctl --user disable --now probability-arena-baseline.timer     # stop 4h loop
systemctl --user disable --now probability-arena-retention.timer    # stop daily pruning
# re-enable: systemctl --user enable --now <unit>
```

## Deployment update sequence

CRYPTO-COVERAGE-REPAIR-001 B10 — migration governance. `MIGRATION_MODE`
defaults to `guarded` (never overridden on this host): every ordinary runtime
call — `run-baseline`, every other CLI command, the watcher/marketops timers,
the FastAPI service — now CHECKS the schema revision and fails closed with
`MIGRATION_REQUIRED` (`SCHEMA_BEHIND_CODE`) if the database is behind the
code's head, instead of silently applying Alembic. This is deliberate: it
stops a `git pull` with a pending migration from getting auto-upgraded by the
next 5-minute MarketOps timer tick, ahead of a backup or this explicit step.
If a migration is pending after `git pull`, apply it explicitly BEFORE
restarting/resuming any service or timer.

**CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B7 correction — the sequence
below used to be non-executable and is now repository-owned end to end.**
`$DB_PATH` was referenced but defined NOWHERE in this repo — it was never a
real environment variable — and `sqlite3 "$DB_PATH" "PRAGMA integrity_check"`
therefore expanded to `sqlite3 "" "PRAGMA integrity_check"`, which opens a
**temporary, throwaway** database (SQLite's `""` filename convention) and
prints `ok` with exit `0` having inspected **nothing at all** — a green
result carrying zero diagnostic content. Worse: **the `sqlite3` binary is not
installed on EVO-X2** (`ssh mikolabs which sqlite3` → nothing), so this line
could never have run there even with a real path. Every command below is a
real, tested `app.cli` subcommand using this application's own database
resolution — none of it depends on a shell variable that doesn't exist or a
binary that isn't installed:

```bash
cd ~/projects/probability-arena
git status --short                      # must be clean; stop and report if dirty
git pull --ff-only
.venv/bin/pip install -q -r requirements-dev.txt     # if deps changed

# 1) verify version + resolved DB path (read-only; never applies a migration)
.venv/bin/python -m app.cli db-schema-report
#   prints: resolved database path, current revision, head revision, status
#   status == SCHEMA_MATCH -> nothing pending, skip to "run-baseline --dry-run" below
#   status == SCHEMA_BEHIND_CODE -> continue with steps 2-7
#   status == SCHEMA_AHEAD_OF_CODE -> STOP. Do NOT run `alembic upgrade head`
#     (it is a no-op here and will not resolve this) — see "AHEAD vs BEHIND"
#     below for the required operator decision.

# Only if SCHEMA_BEHIND_CODE (a migration is genuinely pending):
# 2) verify backup freshness, THEN take one more immediately before the
#    upgrade — do not rely on a backup that may be up to 36h old for a
#    schema change (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B9)
.venv/bin/python -m app.cli sqlite-backup-freshness-report
.venv/bin/python -m app.cli backup-db
# 3) pause affected writers if the pending migration touches a table an
#    active writer holds open across a long span (check the migration's own
#    docstring / alembic/versions/<rev>_*.py for a note); ordinary additive
#    migrations do not require this — the batch-alter-table ones (e.g. 0028)
#    complete in a few seconds and do not need a manual pause
# 4) apply the migration explicitly
.venv/bin/alembic upgrade head
# 5) verify the revision actually landed at head
.venv/bin/python -m app.cli db-schema-report   # must now print status: SCHEMA_MATCH
# 6) POST-migration integrity check — run it against a fresh backup
#    ARTIFACT, not the live file. `backup-db`'s own verification step IS a
#    full `PRAGMA integrity_check` (app/services/backup.py `_inspect_sqlite`,
#    opened mode=ro on the decompressed copy), so this single command both
#    proves post-migration integrity AND leaves you a known-good restore
#    point taken AFTER the schema change. Read `status=ok`.
#    See "Step 6: integrity without blocking writers" below for why this
#    does not run against the live database, and for the real measured
#    duration (the old "~7m18s" here was wrong by ~61x).
.venv/bin/python -m app.cli backup-db
# 7) rebuild planner statistics for whatever this migration rebuilt (see
#    "Migration 0028: `ANALYZE` after a table rebuild" below for the
#    concrete per-migration step — do NOT run a blanket ANALYZE after every
#    migration; only migrations that rebuild a table need one)
# 8) verify a critical reconciliation query plan is still what's expected
#    (see docs/CRYPTO_QUERY_PLAN_AND_DENOMINATOR_RECOVERY_001.md for the
#    EXPLAIN QUERY PLAN fingerprint this checks against)
#
#    *** EXPECTED: status=backlog_expiring, truncated=True, EXIT CODE 1. ***
#    *** THIS IS A PASS. Do not read it as a failed deploy.             ***
#    See "Step 8: why a healthy run exits 1" below before running it, and
#    do NOT run this step under `set -e` without the `|| true` guard shown
#    there — it will halt the deploy on a successful check.
.venv/bin/python -m app.cli crypto-tape-reconcile --dry-run --force --hours 48 --max-duration-seconds 30 || true

.venv/bin/python -m app.cli run-baseline --dry-run   # audit-only; fails closed if a migration is still pending
.venv/bin/python -m app.cli db-stats                 # sanity
# 9) resume services / restart long-running services after code changes:
systemctl --user restart probability-arena-watcher.service
# 10) observe health for the next few cycles:
.venv/bin/python -m app.cli marketops-report
journalctl --user -u probability-arena-watcher.service -n 50 --no-pager
```

### Step 6: integrity without blocking writers (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R3, and the duration correction R0)

**The duration was wrong by ~61x. `~7m18s` was never measured.** The
milestone doc records it as "carried forward from the review-supplied
measurement in the task brief, not independently re-measured here". The
repository's own on-host evidence artifact contradicts it outright:
`docs/evidence/crypto-query-plan-and-denominator-recovery-001/analyze_live.json`
is the record of the live ANALYZE session run against **this exact database**
(`file_bytes = 4 550 623 232`, `page_count = 1 110 992`) on EVO-X2:

| | |
|---|---|
| `started_at` | 2026-08-11T07:04:43.950430Z |
| `finished_at` | 2026-08-11T07:04:51.156672Z |
| **whole session** | **7.206 s** |
| of which `analyze_seconds` | 0.4597 s |
| `integrity_check` | `ok` — a **full** `PRAGMA integrity_check` on the **live** file |

The entire session — preflight, `before` snapshot, `ANALYZE`, `after`
snapshot, a full live-file `integrity_check`, and the delta — completed in
**7.206 seconds**. A 438-second (`7m18s`) integrity check cannot fit inside
it. The true full-`integrity_check` cost on the live 4.55 GB database is
therefore **under ~6.7 s** warm, consistent with the 4.8 s recorded for the
same check on a byte-faithful copy in
`docs/CRYPTO_QUERY_PLAN_AND_DENOMINATOR_RECOVERY_001.md`. **`7m18s` is
almost certainly a unit transcription of a ~7.2-second measurement.**

**Warm vs cold is the whole story, and EVO is always warm.** EVO has 92 GB
RAM and ~21 GB of page cache against a 4.55 GB database, so the file is
fully resident and every EVO figure is a warm figure — this is stated
explicitly in `CRYPTO_QUERY_PLAN_AND_DENOMINATOR_RECOVERY_001.md`
§"Cold-disk behaviour". On a memory-constrained host the same check is
orders of magnitude slower because it degrades into random 4 KiB reads; that
is a property of the measuring host, **not of EVO**. Do not import a
cold-cache figure from a laptop into this runbook.

**Measured, cold, on a full-size file (R0 re-measurement).** Two reviewers
reported 6.59 s and 669.34 s for this same check and disagreed by 100x, so
it was re-measured directly. Method: a synthetic database built on this
repo's **real schema** (`alembic upgrade head`; 53 tables, 129 indexes,
`page_size=4096`) and filled to production scale — **4 612 046 848 bytes,
1 125 988 pages, 351 426 free** — i.e. slightly *larger* and with
**774 562 in-use pages vs EVO's 691 912**, so it is a strictly harder file
than production. No production data was copied. Host: MacBook Air M2, 8 GB
RAM, `journal_mode=delete`. Cold = page cache evicted by streaming a 6.4 GB
unrelated file through the buffer cache before each run (`sudo purge` was
unavailable). Load average is reported because other work shared the host.

| check | engine | cache | duration | load |
|---|---|---|---|---|
| `quick_check` | sqlite3 CLI 3.51.0 | **cold** | **50.10 s** | 2.55 |
| `integrity_check` | sqlite3 CLI 3.51.0 | **cold** | **873.83 s** | 2.71 |
| `integrity_check` | app engine (SQLAlchemy / py-sqlite 3.45.1) | **cold** | **820.49 s** | 13.18 |
| `integrity_check` | live EVO-X2, from `analyze_live.json` | **warm** | **≤ 6.75 s** | — |

Three conclusions, each of which settles part of the dispute:

1. **Cache state is the entire 100x.** ~820-874 s cold versus ≤6.75 s warm
   on EVO is a **~130x** spread on the same operation. Reviewer A's 6.59 s
   is a *warm* measurement and is correct for EVO. Reviewer B's 669.34 s is
   a *cold* (or memory-starved) measurement and is correct for the host it
   was taken on — it is simply not EVO's number, and it does not make
   "~7m18s" an underestimate.
2. **"App engine ~50 % slower" is refuted.** At matched cache state the app
   engine took **820.49 s** and the raw `sqlite3` binary **873.83 s** — the
   engine was *faster*, and it ran under 5x the system load. The
   CLI-versus-SQLAlchemy factor is negligible; it cannot explain a 100x gap
   and it does not even have the claimed sign.
3. **`quick_check` is ~17x cheaper than `integrity_check`** cold (50.10 s vs
   873.83 s), which is the other candidate explanation — but neither
   reviewer's figure matches a `quick_check`, so this is not what they
   measured.

**Warm at production scale is NOT reproducible on a laptop, and that is the
point.** At the time of measurement this 8 GB host had ~0.38 GB of
reclaimable page cache against a 4.6 GB file, so it can never hold the
database resident; EVO holds all 4.55 GB of it resident permanently. Any
"warm" number taken on a memory-constrained machine is really a cold number.
**The authoritative warm figure is EVO's own artifact, not a re-measurement
elsewhere.**

**Writer contention, measured.** With a competing writer using this app's
declared 30 s busy timeout against the same full-size
`journal_mode=delete` file: a 54.01 s reader hold produced a **46.53 s**
maximum writer wait — the writer is blocked for essentially the entire
duration of the check, confirming the mechanism. (5 write attempts, 0
failures, so the reviewer-reported *failure* count was not reproduced at
this sample size; the wait-tracks-duration relationship is the robust
finding.) **Writer wait ≈ check duration, roughly 1:1** — which is exactly
why the duration error mattered: at ~7 s writers wait ~7 s and the 30 s
busy timeout absorbs it, while at the mistakenly-documented 438 s they
would exhaust it and fail.

**Why it still runs against a copy.** Even at ~7 s the mechanism is real:
EVO is `journal_mode=delete`, so a reader holding SHARED prevents any writer
from reaching EXCLUSIVE to commit. Step 6 sits between step 3 ("ordinary
additive migrations do not require a manual pause") and step 9 (resume
services), so MarketOps (5 min), meme-news (5 min), tick-aggregation
(hourly) and the continuously-writing watcher are **all live throughout**.
At ~7 s every blocked writer is comfortably inside the 30 s
`sqlite_busy_timeout_ms` and simply waits; at the mistakenly-documented
438 s they would have exhausted it and failed. **Running the check against
the artifact removes the exposure entirely rather than relying on that
margin**, and costs nothing extra because `backup-db` already performs a
full `integrity_check` during verification.

Checking the artifact is equivalent to checking the live file: `backup-db`
uses SQLite's online backup API to produce a byte-faithful copy, so
corruption present in the live file is present in the copy. This is the same
reasoning the capacity milestone already used ("`PRAGMA integrity_check` =
ok on the byte-faithful online copy ... run off-host-path so production was
not asked to read 4.55 GB before the decision").

**Chosen: check the artifact.** Rejected alternatives, and why:

| Option | Why not |
|---|---|
| quiesce writers (stop watcher, mask timers) around step 6 | introduces a real service outage and two extra failure modes (forgetting to unmask, a timer firing mid-window) to avoid a ~7 s wait that the busy timeout already absorbs |
| check the **step-2** backup | that artifact is taken *before* `alembic upgrade head`; it cannot verify post-migration integrity, which is the entire point of step 6 |
| check the live file and accept the wait | works today at ~7 s, but couples a production-writer stall to a number nobody re-measures |

**If you specifically need the LIVE file checked** (e.g. investigating
suspected on-disk corruption rather than verifying a migration), run it
deliberately and knowingly:

```bash
.venv/bin/python -m app.cli db-integrity-check     # ~7s warm at 4.55 GB; holds SHARED for that time
```

### Step 8: why a healthy run exits 1 (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R4)

Step 8 is a **mandatory verification step that exits non-zero on success**.
Both reviewers of this runbook hit this independently, which is exactly how an
operator will hit it at 2am: a red exit code on a prescribed deploy step reads
as a failed deploy, and under `set -e` it silently halts the rest of the
sequence before services are resumed.

**Expected output on EVO-X2's real data:**

```
status=backlog_expiring  external_calls=0  window=48h  ...
tokens_considered=...  truncated=True  ...
recoverable_backlog_count=...  oldest_recoverable_age_seconds=...  ...
WARNING: ...the oldest RECOVERABLE unreconciled token is <N>h old, >= the
162.0h frontier threshold...
```
**Exit code: 1.** This is the documented, expected result.

**Why.** `app/cli.py` returns `-1` (shell exit 1) for any status other than
`ok`/`dry_run`, deliberately: "eligible work remains unmeasured" must never
look healthy. `backlog_expiring` is set by
`run_scheduled_reconciliation` whenever the oldest *recoverable* token is
older than `crypto_retention_days*24 − RECONCILER_CADENCE_HOURS` (162h at
the shipped 7d/6h defaults), and it overrides the dry-run status. On EVO
that condition is currently TRUE, so a dry run reports it. `--dry-run`
writes nothing either way — the non-zero exit is a report about the
*backlog*, not about this command having failed.

**What this step actually verifies** is that the command runs to completion
against the post-migration schema and emits a plausible pass summary — i.e.
that the migration did not break the reconciliation query path. It is not a
health assertion about the backlog.

**What a GENUINE step-8 failure looks like** — any of these means stop and
investigate, and none of them is `backlog_expiring`:

| Symptom | Meaning |
|---|---|
| a Python traceback / non-zero exit with **no** `status=` line | the command did not run — schema or import breakage from the migration |
| `status=error` (`external_calls=0  error=...`) | the pass refused or crashed internally |
| `status=migration_required` | step 4/5 did not actually land; go back |
| `external_calls` **≠ 0** | a provider was contacted; this path must be provider-free |
| `status=skipped_contention` | never got the DB lock — a writer was not quiesced |
| `status=unsafe_host_cost` | adaptive batching refused the host cost estimate |
| `tokens_considered=0` **and** `work_available` > 0 | selection is broken — real work exists but none was selected |

**Scripting it.** Because success is exit 1, assert on the status string, not
the exit code:

```bash
out=$(.venv/bin/python -m app.cli crypto-tape-reconcile --dry-run --force \
        --hours 48 --max-duration-seconds 30 2>&1) || true
echo "$out"
grep -qE 'status=(ok|dry_run|dry_run_partial|truncated|partial|backlog_expiring)' <<<"$out" \
  && grep -q 'external_calls=0' <<<"$out" \
  || { echo "STEP 8 GENUINELY FAILED"; exit 1; }
```

### AHEAD vs BEHIND (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B8)

`db-schema-report` distinguishes three states instead of the old binary
"current == head" check, which raised the identical `MIGRATION_REQUIRED`
whether the database was behind OR ahead of the code — with remediation text
("run `alembic upgrade head`") that is a no-op when ahead, so an operator
following it loops forever. This is exactly the state after applying `0028`
and then reverting the merge that added it.

- **`SCHEMA_MATCH`** — nothing to do.
- **`SCHEMA_BEHIND_CODE`** — the normal case after a `git pull` that added a
  migration. Follow steps 1-8 above.
- **`SCHEMA_AHEAD_OF_CODE`** — the database's stamped revision is not an
  ancestor of the running code's head (the code doesn't even have a script
  for it, or it's simply newer). **Never auto-downgrade.** This requires an
  explicit operator decision between exactly two options:
  1. **Redeploy the newer code** this database revision belongs to
     (`git pull --ff-only` to the commit whose Alembic head matches the
     database's current revision, then re-run `db-schema-report`); or
  2. **Downgrade the database** to this code's head — see the `0028`
     rollback procedure immediately below for the one case this repo has
     actually measured. Confirm a fresh backup exists first.

### Migration `0028` rollback procedure (measured, CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B8)

`0028` widens `crypto_token_lifecycle_runs.status` from `VARCHAR(16)` to
`VARCHAR(32)` via `batch_alter_table` (SQLite cannot `ALTER COLUMN` in
place — batch mode rebuilds the table). The downgrade re-narrows it back to
`VARCHAR(16)`, which is lossy IF any stored value exceeds 16 characters
(`"skipped_contention"` is 18 chars, `"dry_run_partial"` is 16 and fits with
zero headroom). Measured independently on a production copy before this was
documented as safe to reverse:

- **Downgrade wall time: 3.25s.**
- **Write-lock hold during the rebuild: 0.157s** — well under any writer's
  busy-timeout budget; a concurrent MarketOps cycle is not expected to fail
  because of this downgrade alone.
- **Losslessness, and the real reason for it:** an 18-character value
  (`"skipped_contention"`) written before the downgrade was verified intact
  and unmodified after downgrading and re-upgrading.
  CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R8 — this bullet used to
  attribute that survival to "SQLite does not truncate ... when nothing in
  the actual row data exceeds the new declared width", which is simply
  wrong and self-contradictory: the 18-character value **does** exceed the
  16-character declared width, and it survived anyway. The correct reason is
  the one the next sentence already gives — **SQLite never enforces
  `VARCHAR(n)` at all**, so a narrowing `batch_alter_table` cannot truncate
  regardless of what the data contains. The observation is real; the stated
  mechanism was not, and believing it would predict data loss for exactly
  the case that is safe. **This is NOT a length-enforcement guarantee going
  forward** — SQLite never
  enforces `VARCHAR(n)` length, so nothing stops a value written AFTER the
  downgrade (while running old code with the wide-status vocabulary
  disabled) from exceeding 16 characters again; the downgrade only proves
  existing data survives the round trip, not that future writes are
  constrained.
- **Foreign-key dependency:** `0028`'s `batch_alter_table` rebuild depends on
  `PRAGMA foreign_keys=OFF` being in effect for the duration of the table
  swap (SQLite's batch-mode default). **Production carries 7,345
  pre-existing dangling FK rows** (documented, not new) — if a downgrade or
  upgrade of this migration is ever run with foreign key enforcement
  deliberately turned ON, expect it to fail loudly on those pre-existing
  rows, not on anything this migration itself introduces.

```bash
# Confirm a fresh backup exists FIRST.
.venv/bin/python -m app.cli backup-db
.venv/bin/alembic downgrade 0027
.venv/bin/python -m app.cli db-schema-report     # must show current revision 0027, status SCHEMA_MATCH
.venv/bin/python -m app.cli db-integrity-check   # ~7s warm at 4.55 GB (NOT 7m18s — see "Step 6" above)
# Statistics: downgrading rebuilds crypto_token_lifecycle_runs AGAIN — see
# the ANALYZE step below; run it the same way after a downgrade as after
# the original upgrade.
```

### Migration `0028`: `ANALYZE` after a table rebuild (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B5)

`0028`'s `batch_alter_table` rebuild of `crypto_token_lifecycle_runs`
(`CREATE` new table, copy rows, `DROP` old, rename) does not carry that
table's `sqlite_stat1` row across the rebuild — **measured 130 → 129 rows**
applying `0028` to a production copy that had already been `ANALYZE`d. This
is an intentional, explicit, per-migration operator step — **this repo does
NOT auto-rerun `ANALYZE` after every migration**; most migrations here are
additive (`CREATE TABLE`/`ADD COLUMN`) and never touch existing statistics at
all, so a blanket "always ANALYZE after upgrade" policy would run an
unnecessary ~0.46s (whole-database) to potentially much longer (at larger
scale) statistics rebuild for migrations that don't need it. Only migrations
that rebuild a table (any `batch_alter_table` on SQLite) need this step, and
`0028` is currently the only one:

```bash
.venv/bin/python -m app.cli db-integrity-check   # confirm ok before touching statistics
.venv/bin/python -c "
from app.db import get_engine
from sqlalchemy import text
with get_engine().connect() as conn:
    conn.execute(text('ANALYZE crypto_token_lifecycle_runs'))
    conn.commit()
"
```
**Use the per-table `ANALYZE crypto_token_lifecycle_runs`. Do not substitute a
full-database `ANALYZE` here** (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001
R10). This used to say a full-database `ANALYZE` was "also acceptable given
the measured ~0.46s cost". That 0.46s is a **warm-cache, host-specific**
figure — EVO has 92 GB RAM and ~21 GB of page cache against a 4.55 GB
database, so every table was already resident (see
`docs/CRYPTO_QUERY_PLAN_AND_DENOMINATOR_RECOVERY_001.md` §"Cold-disk
behaviour", which states plainly that *every figure there is warm-cache*).
It is not a bound on what the command costs elsewhere, or on EVO after a
reboot: a reviewer measured **5m39s cold** for the full-database form against
**0.203s** for the per-table form. `ANALYZE` also takes a **write lock** for
its whole duration, so the full-database form blocks every production writer
for that time — the same failure mode as step 6 (see the writer-quiescing
note there). The per-table form rebuilds exactly what `0028` invalidated and
nothing else, which is both correct and three orders of magnitude cheaper.

**Post-migration acceptance criteria** (all four must pass before declaring
the migration complete):
1. `db-schema-report` reports the expected revision and `status:
   SCHEMA_MATCH`.
2. `sqlite_stat1` contains a row for `crypto_token_lifecycle_runs` again.
   CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R9 — this used to say to
   run that query "via `db-integrity-check`'s engine", but
   `db-integrity-check` **takes no SQL**: it runs exactly one hardcoded
   `PRAGMA integrity_check` and accepts no query argument, so the
   instruction was not executable. Use the same read-only snippet form the
   restore section uses (and the same existence gate — after a rebuild the
   table exists, but this stays correct if it does not):
   ```bash
   .venv/bin/python -c "
   from app.db import get_engine
   from sqlalchemy import text
   with get_engine().connect() as conn:
       present = conn.execute(text(
           \"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'\"
       )).scalar()
       rows = conn.execute(text(
           \"SELECT * FROM sqlite_stat1 WHERE tbl='crypto_token_lifecycle_runs'\"
       )).fetchall() if present else []
       print(f'crypto_token_lifecycle_runs stat rows: {len(rows)}')
       for r in rows:
           print('   ', tuple(r))
   "
   ```
   Do not use the bare `sqlite3` binary — it is not installed on EVO-X2.
3. `db-integrity-check` reports `PASS`.
4. MarketOps health and watcher health are both green for at least one full
   cycle after resuming services (`marketops-report`, `journalctl --user -u
   probability-arena-watcher.service`).

### Restoring a backup: planner state is NOT integrity (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B6)

**Every backup artifact taken before 2026-08-11T07:04:51Z predates the live
`ANALYZE`** (see the "Live ANALYZE" entry in `DEPLOYMENT_REPORT_EVO_X2.md`) —
restoring one of those restores an **UNANALYSED** planner state silently,
even though the schema and every row of data are otherwise fully intact.
"Integrity restored" (`db-integrity-check` reports `PASS`) is NOT the same
claim as "planner state restored" — do not equate the two. Backup artifacts
themselves are never modified to fix this; the restore procedure adds an
explicit post-restore step instead:

```bash
# ... normal restore procedure (see docs/SQLITE_BACKUP_COORDINATION_001.md) ...
.venv/bin/python -m app.cli db-integrity-check    # 1) integrity restored?
# CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R5 — this MUST gate on the
# table's existence first. An un-ANALYZEd database has no `sqlite_stat1`
# TABLE at all (SQLite creates it only when `ANALYZE` first runs), and an
# un-ANALYZEd database is precisely the case this whole procedure exists to
# catch — every backup artifact taken before 2026-08-11T07:04:51Z. A bare
# `SELECT count(*) FROM sqlite_stat1` therefore raises
# `OperationalError: no such table: sqlite_stat1` and the "if 0" branch
# below is unreachable for the only input that matters.
.venv/bin/python -c "
from app.db import get_engine
from sqlalchemy import text
with get_engine().connect() as conn:
    present = conn.execute(text(
        \"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'\"
    )).scalar()
    n = conn.execute(text('SELECT count(*) FROM sqlite_stat1')).scalar() if present else 0
    print(f'sqlite_stat1 rows: {n}' + ('' if present else '  (table absent: never ANALYZEd)'))
"
# 2) if 0 (or fewer rows than expected — 130 as of the 2026-08-11 baseline,
#    see DEPLOYMENT_REPORT_EVO_X2.md), the restored backup predates the
#    live ANALYZE. Rebuild planner statistics explicitly before declaring
#    the environment fully operational:
.venv/bin/python -c "
from app.db import get_engine
from sqlalchemy import text
with get_engine().connect() as conn:
    conn.execute(text('ANALYZE'))
    conn.commit()
"
# 3) only NOW is the environment fully operational — integrity AND planner
#    state both verified, not just integrity.
```

## Feature flag rollout sequence (one flag at a time)

1. Deploy dark (flags unchanged), verify template-mode behavior.
2. Edit `~/projects/probability-arena/.env` — flip exactly one flag (append the key if it predates the `.env`; it was created before newer flags existed).
3. Exercise the smallest possible path (e.g. `process-promoted-signals --limit 3`).
4. Inspect: `research-canary-report`, `signal-report`, journals.
5. Restart the watcher service if the flag affects it (`systemctl --user restart probability-arena-watcher.service` — oneshot timers pick up `.env` next run automatically).
6. Roll back = flip the flag back; no code change needed.

Soccer canary (SOCCER-001) is a two-step rollout: flip `ENABLE_SOCCER_EXTERNAL_RESEARCH=true` first with `SOCCER_RESEARCH_PROVIDER=template` (collector selected, honest fallbacks, zero external calls), inspect `research-canary-report`, then set `SOCCER_RESEARCH_PROVIDER=espn` as its own step.

MarketOps Autopilot (OPS-006) rollout is **dark → run-once → optional timer**:
deploy with `ENABLE_MARKETOPS_AUTOPILOT=false`, run `marketops-run-once`
manually 1–3 times and inspect `marketops-report` / `marketops-alerts`, then —
only if wanted — install `infra/systemd/user/probability-arena-marketops.{service,timer}`
(5-min cadence, NOT auto-installed; install commands are in the timer file).
The `marketops-loop` CLI additionally refuses to start unless the flag is true.
It coordinates existing read-only services only — it cannot trade, paper
trade, calculate EV, or move money.

Crypto risk engine (CRYPTO-002) rollout: run `crypto-risk-assess --limit 25` +
`crypto-risk-report` manually first (heuristic-only, no flags needed), then flip
`ENABLE_CRYPTO_RISK_ENGINE=true` so MarketOps crypto scans assess automatically,
then enable providers one at a time (`ENABLE_GOPLUS_RISK` / 
`ENABLE_SOLANA_TRACKER_RISK`; keys optional, never printed). A risk level is an
avoid/flag verdict for review — never a trade direction.

Holder-risk coverage (MEME-RISK-003): `crypto-provider-health-report` shows which
providers are active, which risk dimensions they cover, and the **explicit
coverage gaps** (GoPlus-only leaves sniper/bundler/creator uncovered);
`meme-risk-coverage-report` shows the same for the meme-news lane. To close the
gaps: enable `ENABLE_SOLANA_TRACKER_RISK` (sniper/insider/bundler; needs
`SOLANA_TRACKER_API_KEY`) and/or `ENABLE_BIRDEYE_RISK` (top-holder + creator;
`BIRDEYE_API_KEY` optional — but validate the Birdeye payload against real
responses first, as its mapping is pending validation and will degrade to honest
absence until confirmed). Read-only intelligence; no EV/trade/sizing/orders/
wallets/execution. No migration.

SolanaTracker request budget (PROVIDER-BUDGET-001): `crypto-provider-budget-report`
shows SolanaTracker usage against its plan (SolanaTracker Advanced **≈ $58–59/month
USD**, 200k requests/month) — requests today/hour/month, estimated monthly
run-rate, remaining daily/monthly budget, success/error rate, coverage-per-request,
and a keep/tune recommendation. Usage is derived read-only from existing
assessments (**no new table, no migration**). The guardrail can only **skip**
optional SolanaTracker lookups when a scan hits `SOLANA_TRACKER_PER_RUN_LOOKUP_LIMIT`
(25) or the day reaches `SOLANA_TRACKER_STOP_DAILY_REQUESTS` (6000) — skipped tokens
fall back to GoPlus+heuristics; GoPlus/Birdeye are never affected. Defaults sit far
above current usage, so nothing is skipped under normal load (the STOP is a cost
circuit breaker). To re-tune, edit the `SOLANA_TRACKER_*` budget keys in `.env`.
Cost note is accounting/ops metadata only — no EV/trade/sizing/orders/wallets/
signing/swaps/execution.

Crypto Arena (CRYPTO-001) discovery has **no service/timer** — validate with
manual passes only: `crypto-scan-once --limit 25` → `crypto-report` →
`crypto-signals-recent`. The migration (`0014`) applies on the first command.
`ENABLE_CRYPTO_SCOUT` stays false (it only reserves future loop/timer use).
Read-only DEX Screener GETs; no wallets/swaps/execution exist anywhere.

**CRYPTO-COVERAGE-REPAIR-001 — the deliberate rollout step this doc anticipated.**
This milestone ships a crypto timer unit
(`infra/systemd/user/probability-arena-crypto-reconcile.{service,timer}`,
03/09/15/21:07 UTC, running `crypto-tape-reconcile`) on branch
`worktree/crypto-coverage-repair`. **As of this writing that branch is not
merged to `main` and NOTHING is installed on EVO-X2** — there is still zero
crypto timers running in production. The steps below are what an operator
must do, in order, to change that; do not read this section as describing
current host state. It is **provider-free** — zero external calls, zero
provider budget — and exists because survival horizons never matured:
production MarketOps only runs the exact-cycle anchor feed, which by
construction sees each token once at age ~0 when no horizon is due, while the
windowed reconciler that would revisit matured tokens was CLI-only and
unscheduled.

- Prerequisite: the branch must be reviewed, merged to `main`, and deployed
  (`git pull` + restart the app per the normal deploy step) before any of the
  following applies on EVO-X2.
- Gate: `ENABLE_CRYPTO_TAPE_RECONCILER` (default **false**). While false the
  command reconciles nothing, applies no migration, and writes nothing.
- The unit deliberately does **not** run Alembic; deploy migrations through the
  normal runbook step, never through this timer.
- Install (only after the branch is merged and deployed):
  `cp infra/systemd/user/probability-arena-crypto-reconcile.{service,timer}
  ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user
  enable --now probability-arena-crypto-reconcile.timer`
- Verify dark: `journalctl --user -u probability-arena-crypto-reconcile.service
  -n 20 --no-pager` should show `status=disabled  external_calls=0  no-op`.
- **DO NOT ACTIVATE without first completing the batch-size measurement step
  below (third Lane-B review, SQLite coexistence, NEW-HIGH-1).** The 20s
  internal deadline does NOT bound a single batch's write-lock hold — it is
  only evaluated BETWEEN batches — so the hold is governed entirely by
  `crypto_tape_reconciler_batch_size x per-token cost on the target host`,
  and per-token cost is not portable across hosts. The shipped default was
  originally 25 (a dev-Mac measurement); at the reviewer's measured EVO-speed
  multiplier the SAME value produced 26.3-36.5s holds — one of three trials
  exceeded the 30s `sqlite_busy_timeout_ms` outright — and a full pass
  converged only ~25 tokens per 6h tick against ~405 new births/day, so the
  reconciler could never converge on that host at all. The shipped default
  is now 5 (the reviewer's measured stopgap: 4.56-5.32s worst-case hold at
  the same host speed, unchanged competitor throughput, better duty cycle) —
  this is still a COUNT-based bound, not a time-based one, and is NOT a
  substitute for measuring on the actual target host before enabling.
- It is a **SQLite writer**, but not a single long transaction: the scheduled
  path commits in bounded batches (`crypto_tape_reconciler_batch_size`,
  default 5 tokens) instead of one transaction for the whole pass, and stops
  at an internal wall-clock deadline
  (`crypto_tape_reconciler_max_duration_seconds`, default 20s).
  **Two different metrics, do not conflate them (third review, NEW-H2).
  MEDIUM: every figure in this bullet is session-only evidence from an
  ad-hoc, non-committed benchmark script — see docs/milestones/
  CRYPTO-COVERAGE-REPAIR-001.md's Write-lock defect section for the full
  evidentiary-status note before citing exact numbers:**
  - The per-commit **write-lock hold** genuinely collapsed: measured
    8.5-40.8s max hold in the legacy single-transaction shape down to
    0.16-1.73s at 2000 tokens with batching.
  - A competing writer's **worst-case wait does NOT track that hold** — it
    tracks the reconciler's **pass wall time**. Measured wall-clock
    competitor blocking was comparable between legacy and batched (6.79s vs
    6.75s, 8.10s vs 8.18s in two reps), and in a third rep the BATCHED run
    blocked the competitor LONGER than the legacy comparison (13.68s vs
    9.88s). All of that wait was in `BEGIN IMMEDIATE`, never in `COMMIT`:
    ~80 back-to-back short write transactions give SQLite's sleeping busy
    handler ~80 chances to lose the lock race — classic writer starvation,
    not a hold-duration problem. (Control: a read-only `dry_run` of 9.12s
    produced a max competitor wait of only 0.076s, ruling out the read span
    as the cause.)
  - The honest bound on a competing writer's exposure is therefore
    `crypto_tape_reconciler_max_duration_seconds` (20s) **plus one batch**,
    i.e. **>=67% of the 30s `sqlite_busy_timeout_ms`** — not the sub-second
    per-batch hold, and not a small percentage of the busy timeout. The
    scheduled path's actual end-to-end wall-clock time on EVO-X2 has **not
    yet been re-measured** since this fix — do that before relying on the
    20s deadline being generous in practice. (Earlier, pre-fix figure for
    reference only, NOT current behaviour: one single-transaction dry run
    measured 105.2s for 819 tokens on EVO-X2 — that shape is what this fix
    replaced.)
  - **Required pre-activation measurement, and how to actually run it
    (HIGH-2 fix):** `run_scheduled_reconciliation` always supplies a
    `batch_size`, so `crypto-tape-reconcile --dry-run` is chunked and trips
    the SAME 20s deadline a real pass would — on EVO-X2 that means a plain
    `--dry-run` cannot measure a full, untruncated pass; it reports
    `status=dry_run_partial` (exit 1) once it hits the deadline. Use the
    `--max-duration-seconds` flag added for exactly this to raise the
    deadline for one measurement invocation only (it does not change the
    `.env` default):
    ```bash
    .venv/bin/python -m app.cli crypto-tape-reconcile --force --dry-run \
      --hours 48 --max-duration-seconds 600
    # status=dry_run (not dry_run_partial) proves the pass completed
    # end-to-end; read its real wall time from duration_ms and use that,
    # not the 20s default, to judge whether the deadline is generous.
    ```
    `--batch-size` is also available (`crypto-tape-reconcile --batch-size N`)
    for measuring alternate batch shapes without touching `.env`. Both flags
    plus the underlying `CRYPTO_TAPE_RECONCILER_{WINDOW_HOURS,LIMIT,
    BATCH_SIZE,MAX_DURATION_SECONDS}` settings are documented in
    `docs/FEATURE_FLAGS.md` and `.env.example`.
  - **Required batch-size-hold measurement (NEW-HIGH-1, mandatory before
    enabling on any host, including EVO-X2):** the deadline measurement
    above tells you the PASS's wall time; it says nothing about a single
    BATCH's write-lock hold, which is the number that actually matters for
    a competing writer's worst-case wait when the deadline can't help (one
    batch in flight). Time one real batch commit on the target host (e.g.
    instrument `_process_batch`'s commit with a `perf_counter` pair, or run
    `--batch-size N --limit N --max-duration-seconds 600` so the whole pass
    IS one batch and read `duration_ms`), and set
    `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` so that measured hold stays
    comfortably under `sqlite_busy_timeout_ms` — never trust the shipped
    default on an unmeasured host.
    **Doc-drift correction (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001
    B9):** `sqlite_busy_timeout_ms` is **30s**, but that is a PER-STATEMENT
    timeout (SQLite's `busy_timeout` re-arms on every statement that hits
    `SQLITE_BUSY`), not a bound on the whole transaction/batch. One batch
    commit issues several statements (birth upsert, snapshot insert, actor
    insert, outcome upsert, plus the run-row bookkeeping), and each one gets
    its own up-to-30s wait if it lands on the lock — so the REAL worst-case
    hold a single stalled batch can impose is closer to **~47s**, not 30s.
    Anywhere this doc says "the 30s busy bound", read that as the
    per-statement figure, not the batch's total worst case.
  It runs `Nice=10 IOWeight=20` and aborts when the latest MarketOps run
  errored. Exit code is non-zero on a refused, truncated, partial, locked, or
  overlap-skipped pass — a green unit that reconciled nothing is exactly the
  failure this milestone removes.
- Result statuses to watch for in the journal, beyond plain `ok`/`disabled`:
  - `truncated` — the window plus backlog exceeded `CRYPTO_TAPE_RECONCILER_LIMIT`
    and work was dropped; raise the limit.
  - `partial` (with `stop_reason=deadline` or `stop_reason=contention`) — the
    pass stopped early; already-committed batches are durable and nothing is
    duplicated, but the run row's own status is not "clean ok". Repeated
    `stop_reason=deadline` on every run means the deadline is too tight for
    the actual per-pass work; repeated `stop_reason=contention` means another
    writer is holding the DB lock past the retry budget.
  - `skipped_overlap` — another reconciliation pass (a second scheduled tick,
    a manual `crypto-tape-session`, or a stray manual `crypto-tape-run-once`)
    already held the per-chain overlap lock; this pass read and wrote
    nothing. The lock is a coordination-only flock file, one per chain, at
    `.crypto-tape-reconcile-{chain}.lock` next to the SQLite database file
    (or the system temp dir for a non-SQLite `DATABASE_URL`). It is
    kernel-released automatically when the holding process exits or crashes
    — there is no PID file and no manual "clear the lock" step; if this
    status repeats across every scheduled tick, look for a manual pass left
    running in a stray tmux session (`crypto-tape-session`), not a stuck
    lock file.
  - `skipped_contention` — the very first write of the pass (the run row)
    never acquired the DB lock even after the bounded retry ladder; nothing
    was written. Distinct from `partial`/`stop_reason=contention`, which
    means some batches DID get through before contention stopped it.
- Row cost: each pass appends ~2 rows per token considered (a lifecycle snapshot
  and an actor observation) — `skip_redundant_when_final` does NOT reduce this
  on the scheduled path (it is structurally inert there; see the milestone doc's
  "MEDIUM, fixed" note on `skip_redundant_when_final` — LOW fix, fourth
  re-review: this used to cite "HIGH-3", which is the unrelated
  `record_discovery_run`/IntegrityError item). Neither table is pruned by
  `retention.py`. `tokens_considered`
  per pass is bounded by `crypto_tape_reconciler_max_duration_seconds`
  (deadline-capped, not just selection-capped), and the milestone's earlier
  "1.048 MiB per pass ≈ 4.19 MiB/day" figure predates both that deadline and
  the HIGH-1 age-exclusion fix — it is NOT current. Do not budget against it;
  re-measure real per-pass row growth once the flag is flipped, against a real
  4x/day cadence, before revisiting retention or raising the cadence.
- **One-off write-off memorialization cost — expect it, it is not a leak**
  (CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R11). B1's reserved
  write-off sub-budget exists to *drain* the permanent write-offs
  (`RETENTION_LOST` / `MISSING_REQUIRED_INITIAL_STATE`) that were burying
  recoverable work. Draining them means **visiting and memorialising each
  one exactly once** (`final=True`, so it never returns — proven idempotent
  by B4). At the measured backlog composition that is a bounded, one-time
  cost, and it should be **expected rather than discovered halfway through**:

  | | |
  |---|---|
  | write-offs to memorialise | **~11 101** |
  | drain rate | **~590/pass** (the reserved sub-budget actually used) |
  | time to drain | **~4.7 days** at the 4 passes/day cadence |
  | rows written per write-off | **~4** (outcome + lifecycle snapshot + actor observation + run accounting) |
  | **permanent rows added** | **~44 000** |

  Those rows land in `crypto_token_lifecycle_snapshots` and
  `crypto_token_actor_observations` — **neither of which `retention.py`
  prunes** (its crypto coverage is `crypto_tokens`, `crypto_pairs`,
  `crypto_price_ticks`, `crypto_token_discovery_events`,
  `crypto_token_risk_assessments`, `crypto_opportunity_signals`,
  `crypto_watcher_runs`; the survival/lifecycle/actor tables are absent).
  This was a **deliberate design choice** — a write-off is a permanent
  finding about a token whose evidence is gone, and re-deriving it every
  pass forever is strictly worse than storing it once — but it is a
  permanent addition, so it is recorded here rather than being noticed later
  as unexplained growth. It is one-time: once drained, the lane's steady
  state is the arrival rate, not the backlog.

**Provider gate (CRYPTO-DISCOVERY-PROVIDER-GATE-001).** `crypto-scan-once` and
`crypto-risk-assess` are now **fail-closed**: a bare command prints a zero-call
provider plan and does NOT execute. Always preview first with `--provider-plan`
(zero calls, zero writes). To execute you must pass `--yes` **plus** an explicit
provider selection; any paid provider (SolanaTracker/Birdeye) additionally
requires its own `--confirm-paid-provider <name>` — generic `--yes` never
authorizes a paid provider. Examples:
```bash
.venv/bin/python -m app.cli crypto-scan-once --provider-plan                                   # inspect providers; zero calls
.venv/bin/python -m app.cli crypto-scan-once --allow-provider dexscreener --yes                # DexScreener-only (honest, no paid)
.venv/bin/python -m app.cli crypto-scan-once --deny-provider solana-tracker --yes              # deny ST (overrides flags/fallback)
.venv/bin/python -m app.cli crypto-scan-once --confirm-paid-provider solana-tracker --yes      # explicit paid opt-in
```
`--deny-provider` overrides feature flags, adapter enablement, and fallbacks. A
completed run prints a true per-provider request ledger. MarketOps runs under an
explicit behavior-equivalent policy (unchanged provider set/caps/output).

## Soccer evidence forecasting (SOCCER-002) rollout

One flag: `ENABLE_SOCCER_EVIDENCE_FORECASTING=true` (soccer research canary
must already be on with `provider=espn`). Validate during a World Cup window:
promote/process 1–3 soccer signals, check `signal-report` /
`research-canary-report` for `soccer_evidence` forecasts, then run
`edge-precheck --latest-marketops-run` — soccer forecasts (confidence ≥0.60)
are now measurable. Forecasts remain measurement inputs only.

## Tennis evidence canary (TENNIS-001) rollout

Two-step, dark-first (mirrors the soccer canary). **Research:** flip
`ENABLE_TENNIS_EXTERNAL_RESEARCH=true` first with `TENNIS_RESEARCH_PROVIDER=template`
(collector selected, honest fallbacks, zero external calls), inspect
`research-canary-report` for the `tennis-external` collector, then — only after
validating the ESPN tennis payload mapping against real responses — set
`TENNIS_RESEARCH_PROVIDER=espn` as its own step. **Forecasting:** flip
`ENABLE_TENNIS_EVIDENCE_FORECASTING=true` (research canary must be on and
producing source-backed match-winner packets); promote/process 1–3 tennis
signals, check `research-canary-report` for `tennis_evidence` forecasts, then
`edge-precheck --latest-marketops-run`. v1 handles MATCH-WINNER markets only
(everything else falls back honestly); tightly-capped ±0.20 shift, conf cap
0.65. Read-only measurement — no EV/trade/paper/sizing/orders/wallets/execution.

## Edge precheck (MVP-005A) rollout

Dark first: deploy with `ENABLE_EDGE_PRECHECK=false`, run one manual
measurement pass (`edge-precheck --limit 25 --force-readonly` — still
read-only, creates measurement rows only), inspect `edge-precheck-report`.
Then flip `ENABLE_EDGE_PRECHECK=true` as its own step.

**Prefer targeted runs during live windows** (MVP-005A.1):
`edge-precheck --latest-marketops-run` measures exactly the forecasts the
last autopilot cycle refreshed — run it within ~2 minutes of a cycle
finishing (`journalctl --user -u probability-arena-marketops.service -n 3`)
so freshness checks can pass. Broad `--limit` sweeps are diagnostics only
and will be dominated by stale-forecast noise by design.

Only after targeted manual sessions during prime live windows (World Cup
afternoon UTC / MLB evening ET) produce sane watchlist behavior, consider
`MARKETOPS_INCLUDE_EDGE_PRECHECK=true` — the autopilot stage is strictly
cycle-scoped (≤5 forecasts/cycle, the ones it just refreshed; never a
sweep). All outputs are gaps and labels; nothing here is a trade
instruction, and no downstream behavior branches on the results.

## Targeted game-level scans (SCANNER-002/OPS-010) rollout

Defaults ship enabled (`ENABLE_TARGETED_MARKET_SCANS=true` — same read-only GETs).
After `git pull`: run `.venv/bin/python -m app.cli scan --limit 500` manually once and
inspect the new `targeted scan (SCANNER-002)` output line (generic/targeted/added counts,
per-series breakdown, failed series). Confirm game-level rows exist
(`KXWCGAME`/`KXMLBTOTAL`-class tickers in `markets`), then restart the watcher
(`systemctl --user restart probability-arena-watcher.service`) and check its journal for
the `Watcher universe: N tickers (...)` composition line — game-level soccer/baseball
markets should appear even before their volume qualifies them as candidates. Rollback:
`ENABLE_TARGETED_MARKET_SCANS=false` in `.env` (exact old behavior), restart watcher.

## Promotion freshness (OPS-009)

Minute-level windows govern promotion (sports 20m / general 60m by default;
`min(minutes, hours*60)` for compat). During quiet hours the probability lane
idles by design (`signals seen=0`); during live windows promoted ages should
be minutes, visible in `marketops-report` ("promotion (OPS-009)" line) and
`frontier-eval-report` latency metrics.

## Frontier evaluation (EVAL-001)

`.venv/bin/python -m app.cli frontier-eval-report --hours 24 --include-crypto
--include-safety [--save-run]` — desk-wide quality + readiness over the
window. Run after live sessions and before considering any flag escalation;
the scorecard is deliberately conservative (no watchlist rows → not_ready).
The `edge-precheck runtime` section distinguishes evidence readiness from the
current double-gated configuration. For
`ready_for_cycle_scoped_edge_automation`, an already-enabled stage should say
to continue accumulating measurements, not suggest enabling the flag again.
If runtime values are unavailable, verify the host `.env`/resolved settings
before changing configuration. The recommendation is report text only and
never changes either flag or the evidence-derived readiness label.

## Edge cohort analysis (EDGE-ANALYSIS-001)

`.venv/bin/python -m app.cli edge-cohort-report --hours 24` — read-only cohort
follow-through slicing of the watchlist/candidate population (which market
types/conditions show the midpoint moving toward the forecast). Analysis only:
no flag, no threshold, no logic change; use it to decide which cohorts warrant
more observation and whether the MVP-005B-design gate is met (it reports
`blocked: True/False` and never unlocks anything itself). Safe to run anytime.

## Scheduled edge-observation snapshots (read-only)

A `systemd --user` timer `probability-arena-edge-observation.timer` runs
daily at **15:00 UTC** (after overnight settlements + the 00:08 retention
prune) and writes a timestamped report snapshot to `~/edge-observation/`.
The runner (`~/edge-observation/run_report.sh`) and its logs live **outside**
the git tree, so the repo stays clean. It only runs the read-only report
suite (edge-policy/edge-cohort/edge-followthrough-diagnostic/edge-filter-shadow/forecast-anchor-diagnostic/trigger-timing-shadow/edge-selection-validation (candidates RETIRED per EDGE-RETIRE-001 — registry observation only)/edge-cost-shadow/frontier-eval/champion-challenger/db-growth/
prune-retention --dry-run); it changes no flag, gate, threshold, or live
service. `live-market-state-report` (LIVE-MARKET-001),
`tennis-live-source-report` (TENNIS-LIVE-SOURCE-001), the
TENNIS-WATCHER-001 pair (`tennis-watch-scan-once [--dry-run]`,
`tennis-watch-report`), and the TENNIS-TAPE-001 pair
(`tennis-tape-capture-once [--dry-run]`, `tennis-tape-report` — bounded
manual captures; needs TENNIS_RESEARCH_PROVIDER=api_tennis inline + the
host-only key, else it skips honestly), and `tennis-api-livefeed-probe`
(TENNIS-LIVE-FEED-002 — one bounded WebSocket validation, ≤300s, key
host-only, persists nothing), and `tennis-goalserve-probe`
(TENNIS-GOALSERVE-001 — bounded Goalserve fallback validation, ≤10 calls,
GOALSERVE_TENNIS_API_KEY host-only, persists nothing), and
`tennis-tape-capture-session` (TENNIS-CAPTURE-SESSION-001 — repeated bounded
captures in ONE invocation, max 60 min, aborts on errors; the preferred way
to run live-window tape sessions) are deliberately NOT on the daily timer — they are
manual on-demand observation tools whose value is real-time
freshness/coverage during live slates, not daily snapshots (the tennis source
report only fetches when a provider is explicitly configured, and the tennis
tick scan's scheduled path no-ops unless ENABLE_TENNIS_TICK_WATCHER=true;
manual bounded runs are always allowed). Note: a cloud/routine scheduler cannot reach this private Tailscale
host — this on-host timer is the reliable mechanism.

```bash
cat ~/edge-observation/latest.log                 # newest snapshot
systemctl --user status probability-arena-edge-observation.timer
systemctl --user start probability-arena-edge-observation.service   # run now
systemctl --user disable --now probability-arena-edge-observation.timer  # stop
# fully remove: rm ~/.config/systemd/user/probability-arena-edge-observation.{service,timer} && systemctl --user daemon-reload && rm -rf ~/edge-observation
```

## Meme/news + domain scout (MEME-NEWS-001, read-only)

```bash
.venv/bin/python -m app.cli meme-scan-once --limit 30   # read-only DexScreener attention pass
.venv/bin/python -m app.cli meme-scout-report           # attention aggregates + top tokens
.venv/bin/python -m app.cli catalyst-report             # catalyst-event stream
.venv/bin/python -m app.cli domain-scout-report         # market-domain inventory + canary priority
```

All read-only discovery/scouting: `attention_score` is an interest signal, never
a buy/trade/EV score; the domain scout adds no forecaster and changes no
promotion/edge/forecast logic. `ENABLE_MEME_SCOUT`/`ENABLE_DOMAIN_SCOUT` (default
false) are reserved for future loop/timer use — the manual commands are always
allowed. `meme-scan-once` hits the public DexScreener GETs already in scope; no
new authenticated sources. No EV/paper/sizing/orders/wallets/keys/swaps/signing/
execution anywhere.

### MEME-NEWS-002 scheduled discovery lane (read-only, NOT auto-installed)

```bash
.venv/bin/python -m app.cli meme-news-run-once      # one bounded cycle (manual: always allowed)
.venv/bin/python -m app.cli meme-news-report        # windowed report
.venv/bin/python -m app.cli meme-news-alerts        # derived notable events (informational)
```

To go live as a timer (**dark-first, two-step**): (1) set `ENABLE_MEME_NEWS_SCOUT=true`
in `.env` (the `--scheduled` command no-ops while false, so the timer is safe to
install dark first); (2) install the units:

```bash
cp infra/systemd/user/probability-arena-meme-news.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now probability-arena-meme-news.timer   # 10-min cadence
# inspect: systemctl --user list-timers | grep meme-news
# disable: systemctl --user disable --now probability-arena-meme-news.timer
```

Independent of MarketOps/EDGE-AUTO (own oneshot unit — cannot affect them).
Retention (`MEME_NEWS_RETENTION_DAYS=14`, via the existing retention timer)
prunes `meme_scout_runs`/`meme_attention_snapshots`/`meme_catalyst_events`;
domain-scout inventory kept. `db-growth-report` now reports the meme row counts.
`attention_score`/alerts are informational only — no EV/recommendation/order/
wallet/swap/signing/execution/sizing/paper trading.

### CRYPTO-TAPE-001 crypto lifecycle tape (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli crypto-tape-run-once --limit 25 --hours 48 --dry-run  # compute + report, persists NOTHING
.venv/bin/python -m app.cli crypto-tape-run-once --limit 25 --hours 48            # persists ONLY lifecycle tape rows
.venv/bin/python -m app.cli crypto-tape-report --hours 24 --top 5                 # coverage, survival labels, actor patterns
.venv/bin/python -m app.cli crypto-tape-session --duration-hours 6 --interval-min 30 --limit 25 [--dry-run]
   # CRYPTO-TAPE-CADENCE-001: bounded repeated passes to mature horizons — one
   # invocation, hard caps (<=36h, 15-120min, <=144 captures), aborts on
   # abnormal status/MarketOps error; NOT a timer; dry-run persists nothing.
   # Deployed dark 2026-07-12 (b5da6d7; dry-run session validated live in
   # 1.03s — no sleeping, nothing persisted, ST budget unchanged).
   # CRYPTO-TAPE-CADENCE-002: lock-safe — a capture that hits "database is
   # locked" is rolled back and retried (<=3 attempts, ~3s apart); a
   # persistent lock aborts CLEANLY (aborted=True abort_reason=database_locked
   # failed_capture_index=N rows_written_before_abort=N), no crash. Deployed
   # dark 2026-07-13 (2f9aa2c, no migration; 15 lock-safe tests pass ON HOST,
   # dry-run persists nothing, ST budget unchanged).
   # Real sessions require explicit approval per invocation (long-lived
   # foreground process on a shared host — run inside tmux/screen).
```

**NEW-MEDIUM-4 note (third Lane-B review, SQLite coexistence): net host
exposure to monolithic manual-path transactions went UP with this change, not
down.** Each manual `crypto-tape-run-once` capture inside a `crypto-tape-session`
still runs `run_once` with `batch_size=None` (the LEGACY, unbatched,
single-transaction shape — see `_assemble_pass_locked`'s docstring; only the
SCHEDULED reconciler opts into batching), so at production density each
capture is still a single multi-second-to-minutes-long write transaction. This
milestone's CRYPTO-COVERAGE-REPAIR-001 fix made `run_tape_session` no longer
ABORT the whole session on the first `status="skipped_overlap"` capture (a
collision with the scheduled reconciler's per-chain lock) — instead that one
capture is skipped and the session continues. Before that fix, a session that
collided with the reconciler on its first capture died immediately (1
monolithic transaction attempted, 0 completed); after it, a full 6h/12-capture
session now runs potentially all 12 monolithic transactions to completion. This
is the correct behavioural fix for the session's own stated purpose (a
transient collision with a legitimate concurrent pass should not kill an
otherwise-healthy session), but it means a manual session now holds MORE total
write-lock time against the host, not less, than before this milestone. At
EVO-X2 speed these individual captures can be minutes long (see the
`crypto-tape-run-once --dry-run` pre-activation measurement above for how to
gauge real capture duration on this host). Prefer the scheduled, batched
reconciler for routine reconciliation; reserve manual `crypto-tape-session` for
targeted windows (e.g. the World Cup/prime-live-window use noted above), run
it inside tmux/screen, and budget for the FULL planned session duration's
worth of monolithic-transaction exposure, not just the first capture's.

**Lock contention guidance (CRYPTO-TAPE-CADENCE-002).** The host's
baseline/watcher/MarketOps writers share the SQLite write lock, so a capture's
run-row INSERT can occasionally hit `database is locked` past the DB busy
timeout. The session now retries that capture (bounded) and, if the lock
persists, aborts loudly and cleanly instead of crashing with
`PendingRollbackError`. When a session reports `abort_reason=database_locked`:
(1) **always run sessions inside tmux/screen** so an abort never orphans the
shell; (2) check for a heavy concurrent writer — `marketops-report` (a stuck/
long cycle) and `tick-aggregation-report` (a big aggregation window holding the
lock, see OPS-013); (3) **only rerun after those settle** — the tape is
idempotent, so a rerun simply resumes maturing horizons; no data is lost or
double-written on an abort (the aborted capture wrote nothing;
`rows_written_before_abort` counts only the captures that fully committed).

One DERIVED assembly pass consolidating already-persisted rows (crypto
tokens/pairs/ticks/discovery events/risk assessments + meme attention/
catalysts) into lifecycle tape tables (migration 0026: runs / birth events /
snapshots / actor observations / survival outcomes — NOT retention-pruned).
**Zero external calls, zero SolanaTracker budget impact, no flag, no timer,
no scheduled path; MarketOps unchanged.** Rollout sequence when asked: deploy
dark → migrate → `--dry-run` (verify persists nothing) → one real bounded run
→ inspect `crypto-tape-report`. A survival label is measured token behavior,
never PnL/EV/recommendation/sizing/order. **Deployed dark 2026-07-12
(`b4362c8`, migration 0026, tape_run_id=1 validated live — see
DEPLOYMENT_REPORT_EVO_X2.md); manual/report-only, no timer.**

### Crypto horizon observation + bounded one-shot orchestration

```bash
.venv/bin/python -m app.cli crypto-horizon-cohort-create --limit 25 --hours 48 --dry-run   # preview a fixed cohort
.venv/bin/python -m app.cli crypto-horizon-cohort-create --limit 25 --hours 48             # freeze it (returns cohort_id)
.venv/bin/python -m app.cli crypto-horizon-cohort-create --hours 1 --limit 2 --require-complete --dry-run  # COHORT-SELECT-001: only complete-liquidity-anchor births (excludes null-liquidity fresh tokens that sort first); optional --min-liquidity N
.venv/bin/python -m app.cli crypto-horizon-cohort-create --token <ID> --token <ID> --require-complete --require-shared-horizon-windows --dry-run  # COHORT-SELECT-002: exact explicit membership; preview validation + shared_pass_eligible; zero calls, persists nothing
.venv/bin/python -m app.cli crypto-horizon-cohort-create --token <ID> --token <ID> --require-complete --require-shared-horizon-windows --confirm   # atomically freeze exactly those tokens (or nothing)
.venv/bin/python -m app.cli crypto-horizon-schedule-report --cohort-id N                    # exact UTC/PT windows; no calls/writes
.venv/bin/python -m app.cli crypto-horizon-reminder-plan --cohort-id N                      # static plan only; installs nothing
.venv/bin/python -m app.cli crypto-horizon-arm-cohort --cohort-id N --dry-run               # exact one-shot preview; installs/writes/calls nothing
.venv/bin/python -m app.cli crypto-horizon-arm-cohort --cohort-id N --confirm               # explicit install; existing fixed cohort only
.venv/bin/python -m app.cli crypto-horizon-orchestrator-report --cohort-id N                # pending/completed/failed/missed + logs/health/counts
.venv/bin/python -m app.cli crypto-horizon-disarm-cohort --cohort-id N                       # exact isolated cleanup preview
.venv/bin/python -m app.cli crypto-horizon-disarm-cohort --cohort-id N --confirm             # remove only that cohort's units
.venv/bin/python -m app.cli crypto-horizon-observation-report --cohort-id N --shadow        # coverage-gain + provider-load estimate FIRST
.venv/bin/python -m app.cli crypto-horizon-observe-once --cohort-id N --limit 25 --dry-run   # plan preview, ZERO calls, nothing persisted
.venv/bin/python -m app.cli crypto-horizon-observe-once --cohort-id N --limit 25             # ONE bounded pass (DexScreener; requires approval per run)
.venv/bin/python -m app.cli crypto-horizon-observation-report --cohort-id N --top 5          # completion/liquidity rates + success gates
.venv/bin/python -m app.cli crypto-horizon-pair-selection-report --cohort-id N --top 5       # OBS-002: why did observations fail? shadow pair policies
.venv/bin/python -m app.cli crypto-horizon-outcome-reconciliation-report --cohort-id N       # OBS-002: proof an observation flipped an outcome unknown->known
```

Manual workflow remains available: schedule report → reminder plan → operator
checks `observe-once --dry-run` → explicit bounded `observe-once` → reports.
The schedule/reminder commands remain report-only and must not themselves be
placed in systemd, cron, MarketOps, or another runner.

**CRYPTO-HORIZON-ORCHESTRATOR-001 workflow:** deploy code dark, choose an
already-created cohort, and review `arm-cohort --dry-run`. It must print every
deduplicated action in UTC/PT, exact fixed-path command, cohort-size limit, and
expected job count while creating no directory/unit/state and making no provider
call. Only a separately approved `--confirm` may write units. Unit names are
`probability-arena-horizon-cN-jM.{service,timer}` under
`~/.config/systemd/user/`; operational manifests, statuses, logs, and the four
post-pass reports live under `~/crypto-horizon-observation/cohort-N/`.

Generated timers contain one exact `OnCalendar`, `Persistent=true`,
`AccuracySec=1us`, and no recurrence. Services use the fixed project directory
and `.venv/bin/python` directly with validated integer cohort/job IDs; no token
name, provider key, or arbitrary shell string enters a unit. Every wakeup first
rechecks the existing planner. `due_now` may make one cohort-size-bounded
DexScreener pass; already-observed, early, inconsistent, or overdue windows make
no call, and overdue windows are never forced/backfilled. A reboot can wake a
persisted timer, but the same planner check still governs it. MarketOps must have
a recent successful completed run; otherwise the job skips and creates a local
warning alert. Database locking gets one retry only; provider failure is recorded
without a retry loop. Each terminal worker exits and removes its own unit pair.

Before any approved arming, verify isolation:

```bash
systemctl --user list-timers --all --no-pager
.venv/bin/python -m app.cli crypto-horizon-orchestrator-report --cohort-id N
```

Never hand-edit generated units, use sudo, modify `.env`, create a cohort as part
of arming, or substitute a recurring timer. Rollback is the cohort-specific
`disarm-cohort` preview followed by explicit `--confirm`; then verify unrelated
Probability Arena timers are unchanged. No migration or feature flag is needed.

**OBS-002 note:** pair selection is now deterministic `active_pair_quality_score`
(picks the highest-quality *eligible* pair — valid price + positive liquidity —
over all candidates, never fabricating liquidity from FDV/mcap/volume). Failed
`no_liquidity_state` observations are **retried in place** on a later
`observe-once` (observed rows stay frozen), so to fix cohort-1's 3 failures you
re-run the observe pass (it now captures per-candidate diagnostics + better
selection), then read `crypto-horizon-pair-selection-report` to see if the
failure was avoidable and `crypto-horizon-outcome-reconciliation-report` to
prove which observations matured a survival label. No migration in OBS-002.
**OBS-002 deployed dark 2026-07-13 (`8ffa4fb`, no migration; dry-run persists
nothing, denominators reconciled, outcome-reconciliation proved 1 unknown→known
transition (Cyy7Mdet5H9i6Vsv 6h), ST budget unchanged). NO real retry run —
cohort-1 6h windows have closed; next productive real pass is cohort-1 24h
(8 due_now) or a fresh cohort, on explicit approval. See DEPLOYMENT_REPORT.**

Fills the UPSTREAM tick-coverage gap CRYPTO-COVERAGE-001 identified: fetches
market/liquidity state for a FROZEN cohort near each 15m/1h/6h/24h mark and
persists ordinary `crypto_price_ticks` (so tape survival horizons mature) plus
audit rows. **Manual or explicitly armed one-shot only — no repeating timer,
daemon, loop, automatic cohort creation, or flag.**
Uses **DexScreener only (free, no key) → ZERO SolanaTracker impact**; each pass
is bounded (≤100 calls, only `due_now` horizons, one fetch/token, a horizon
observed once); dry-run makes ZERO calls and persists nothing; misses recorded
honestly, never fabricated. Real manual passes or cohort arming require explicit
approval. To actually mature horizons: create a cohort of freshly-born tokens,
then manually observe or explicitly arm its planner-derived windows so each
window gets a tick; check the success gates in the
report (measurement only). Rollout: deploy dark → migrate → `--dry-run` cohort
+ observe → `--shadow` → one small approved real pass → report. Migration 0027
(3 additive tables). **Deployed dark 2026-07-13 (`1d20392`, migration 0027;
backup verified; cohort_id=1 created live — 10 members, 8 tokens 6h `due_now`,
zero external calls, nothing persisted by dry-run/shadow; NO real observe pass
run yet — awaits approval). See DEPLOYMENT_REPORT_EVO_X2.md. Note: fresh <2h
cohorts need a discovery/cadence pass first (host's newest birth was 7.4h old).**

### CRYPTO-COVERAGE-001 tape coverage forensics (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli crypto-tape-coverage-report --hours 168 --top 5 --limit 25
   # why do survival horizons stay unmeasurable? decompose gaps + shadow selection
```

Decomposes every unmeasurable 15m/1h/6h/24h survival horizon into an explicit
cause and reports a coverage funnel, an upstream-tick-coverage-vs-revisit-policy
bottleneck verdict, a selection/starvation analysis, and a SHADOW-ONLY selection
comparison (recent / due-first / fixed-cohort / mixed). **The load-bearing
finding it exists to surface:** survival matures only from background-scout
`crypto_price_ticks`, and the recorder selects recent-first, so old cohorts
whose long horizons are due rank below the per-run limit and starve — read the
`bottleneck_verdict` and `shadow_selection` sections to decide whether the next
crypto milestone should change selection (its own accepted milestone) or lift
upstream tick coverage. **No table/migration, persists nothing, no external
call, no SolanaTracker budget impact, no timer; changes no stored label or live
selection.** Diagnostic only, never advice.
**Deployed dark 2026-07-13 (`452fe79`, no migration; 72h+168h validated live,
nothing persisted, ST budget unchanged). LIVE VERDICT: 6h/24h bottleneck =
`upstream_tick_coverage` (0.94 / 0.71), NOT revisit policy — shadow shows
due-horizon-first matures 0 vs recent-first's 5, so DO NOT change recorder
selection; the ceiling is upstream scout tick density near long horizons. See
DEPLOYMENT_REPORT_EVO_X2.md.**

### CRYPTO-RETROSPECT-001 retrospective analysis (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli crypto-retrospect-report --hours 48 --top 5   # which features separate tape outcomes? (measurement)
.venv/bin/python -m app.cli crypto-retrospect-report --hours 72 --cohort tape-backed   # RETROSPECT-002: mature-tape lens only
```

Joins persisted features (concentration/risk/liquidity/volume/boost/attention/
social/venue/coverage/missing-info buckets) to the CRYPTO-TAPE-001 survival
outcomes over the recent token universe, with conservative labels (`too_thin`
/ `provider_gap_dominates` / `no_separation` / `weak_separator` /
`strong_risk_separator` / `strong_survival_separator`). **RETROSPECT-002** adds
`--cohort {all,tape-backed,derived-only}` plus always-on `data_source_mix` and
per-dimension `source_stratification` (source labels + a dilution warning) so
mature tape-backed evidence reads apart from fresh derived-only noise — use
`--cohort tape-backed` after each cadence session to see whether a pattern is
real or just fresh-token dilution. **No table/migration, persists nothing, no
external call, no SolanaTracker budget impact, no timer.** A separation/source
label is feature-quality evidence, never advice.
**RETROSPECT-001 deployed dark 2026-07-12 (`18a6a93`); RETROSPECT-002
deployed dark 2026-07-13 (`c434fd7`, no migration; all six cohort×window
reports validated live, nothing persisted, ST budget unchanged — every
dimension still tape_too_thin, needs a second cadence session; see
DEPLOYMENT_REPORT_EVO_X2.md). Manual/report-only.**

### MEME-SHADOW-001 label follow-through (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli meme-shadow-report --lookback-hours 48   # does review_priority predict later behavior? (calibration measurement)
```

Reconstructs MEME-MAS `review_priority` at historical attention snapshots and
measures each token's later trajectory (price/liq/vol at 5m/15m/1h/6h/24h,
survival, rug incidence, attention persistence, risk transition) → outcome
cohorts by review_priority / sub-score / risk reason / concentration + a
calibration recommendation. **No table/migration, no external call, no
SolanaTracker budget impact, no timer.** Market-movement MEASUREMENT (like edge
follow-through) — never PnL/EV/paper/recommendation/sizing/order. Deploy is
code-only, dark-by-default; **do not deploy unless explicitly asked.**

### MEME-MAS-001 memecoin diagnostic (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli meme-mas-report --hours 24 --top 10   # multi-agent review-priority triage (not advice)
.venv/bin/python -m app.cli meme-mas-assess --limit 20            # per-token diagnostic traces
```

Five deterministic agents recompute sub-scores on demand from persisted
`meme_attention_snapshots` / `crypto_token_risk_assessments` /
`meme_catalyst_events` → a `review_priority` (low/monitor/elevated_review/
high_review/reject_risk). **No table/migration, no external request, no
SolanaTracker budget impact, no timer** — manual reports only. `review_priority`
is human-review triage, never a trade recommendation/EV/sizing/order; `reject_risk`
is avoid/flag for review. Deploy is code-only and dark-by-default (no flag to
flip); **do not deploy unless explicitly asked.**

### POLY-002 Kalshi↔Polymarket cross-venue observation (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli cross-venue-match-once     # default now recency-aware + representative (XVENUE-OPS-001: kalshi 4000 / polymarket 500, most-recently-seen first)
.venv/bin/python -m app.cli cross-venue-match-once --recent-hours 48   # drop stale 'active' rows (host had ~12k stale-active); prints stale_skipped
.venv/bin/python -m app.cli cross-venue-match-once --domain sports --market-type winner   # narrow the sample
.venv/bin/python -m app.cli cross-venue-report         # comparables, midpoint-difference distribution, spread/liquidity, freshness
.venv/bin/python -m app.cli cross-venue-candidates --label comparable_market_candidate
```

> **XVENUE-OPS-001:** the no-arg default is now representative — it loads Kalshi
> markets most-recently-seen first (was rowid/oldest-first, which returned
> days-stale `active` rows) and every run prints a sample-composition report
> (domain/market-type breakdown, stale/no-snapshot counts, overlap, low-overlap
> note). Selection/usability only; the matcher, labels, and precision gates are
> unchanged. The prior deploy's "default under-covers the data" follow-up is
> resolved by this milestone.

### XVENUE-OBS-001 observation windows (read-only, on-demand, NO timer)

```bash
.venv/bin/python -m app.cli xvenue-observation-report   # one-screen window verdict: clean vs flagged comparables, overlap assessment
```

For high-overlap slates (World Cup semifinal/final, MLB slates, election
windows) follow **`docs/XVENUE_OBSERVATION_RUNBOOK.md`**: targeted scan →
coverage census → match → report → candidates-by-label →
`xvenue-observation-report`. The report composes persisted rows only (no
external call, nothing persisted) and warns when the latest match run predates
the latest scan. A clean comparable is a coverage fact for human review — never
an opportunity/arb/EV/trade signal.

Deterministic semantic matcher over **already-persisted** Kalshi markets/snapshots
+ POLY-001 polymarket markets → candidate labels + measured `observed_difference`
(0–1 probability midpoint gap). **No external call, no timer, no flag.** New tables
from migration `0021` (`cross_venue_observation_runs`, `cross_venue_market_candidates`).
Deploy needs a migration (0020→0021) but is otherwise dark/manual. OBSERVATION only
— never EV/arbitrage/trade/side/size/order/wallet/execution; ambiguous data →
`unresolved_semantic_match`. **Do not deploy unless explicitly asked.**

### POLY-001 Polymarket market-data observer (read-only, NO timer installed)

```bash
.venv/bin/python -m app.cli polymarket-scan-once --limit 50   # one bounded read-only scan (manual: always allowed)
.venv/bin/python -m app.cli polymarket-report                 # windowed market-data report
.venv/bin/python -m app.cli polymarket-domain-report          # per-category inventory (latest scan)
```

Read-only SECOND venue: public/no-auth GETs against the Gamma market catalog +
CLOB read-only order books (no API key/wallet/signing; authenticated trading
endpoints not implemented). **No systemd timer is installed in POLY-001** — the
lane is manual-only; `ENABLE_POLYMARKET_SCOUT` merely reserves a future
`--scheduled` path (which no-ops while false). New tables from migration `0020`
(`polymarket_markets`/`polymarket_orderbook_snapshots`/`polymarket_scout_runs`/
`polymarket_domain_inventory_snapshots`). Deploy is dark-by-default and requires
no flag flip to use the manual reports; **do not deploy unless explicitly asked.**
Retention (`POLYMARKET_RETENTION_DAYS=14`, via the existing retention timer)
prunes markets/orderbook/scout-run rows; the domain-inventory coverage table is
kept. Prices/order books are informational quotes only — no EV/arbitrage/
recommendation/order/wallet/swap/signing/execution/sizing/paper trading;
cross-venue Kalshi linking shipped in POLY-002 (no arb/EV labels).

### POLY-PRECISION-001 cross-venue matcher precision (read-only, NO timer)

No flag, no setting, no migration, no external call — the precision fixes are
unconditional matcher behavior. **Required before POLY-COVERAGE-001 may deploy.**
Re-run `cross-venue-match-once` after deploying; expect materially FEWER
`comparable_market_candidate` rows (9 → 2 on the validated sample) because
mis-aligned and cross-sport pairs now degrade to `unresolved_semantic_match` or
`incompatible_outcome`. That drop is the fix working, not a regression.

* A Polymarket midpoint and any `observed_difference` exist ONLY when the outcome
  side is aligned to the Kalshi YES proposition; otherwise both are absent and the
  row carries `outcome_side_uncertain` / `midpoint_side_uncertain`.
* New mismatch reasons to expect in `cross-venue-report`: `market_type_mismatch`,
  `threshold_mismatch`, `entity_mismatch`, `sport_or_game_mismatch`,
  `outcome_side_uncertain`, `midpoint_side_uncertain`, and the REVIEW flag
  `large_observed_difference_requires_review`.
* That flag means the MATCH is suspicious (or a Kalshi quote is stale). It is
  **never an opportunity, edge, arbitrage, or action**, and never rejects a pair.
* Identifies no arbitrage, computes no EV, recommends no trade, paper trades
  nothing, sizes nothing, places no orders, uses no wallets/keys/signing/execution.

### POLY-COVERAGE-001 Polymarket coverage expansion (read-only, NO timer)

```bash
# broader: bounded pagination + category / resolution-window filters
.venv/bin/python -m app.cli polymarket-scan-once --limit 400 --orderbook-limit 20 \
    --end-date-min 2026-07-08T00:00:00Z --end-date-max 2026-07-22T00:00:00Z

# targeted: search queries derived deterministically from persisted Kalshi titles/tickers (no LLM)
.venv/bin/python -m app.cli polymarket-scan-once --targeted --limit 400 --orderbook-limit 20

.venv/bin/python -m app.cli polymarket-coverage-report --top 20   # per-domain SUPPLY census
.venv/bin/python -m app.cli cross-venue-match-once --polymarket-limit 600   # rerun POLY-002
```

Read-only coverage expansion of the SAME public/no-auth GETs. Requires migration
`0022` (additive columns on `polymarket_scout_runs`: `scan_mode`, `pages_fetched`,
`market_fetch_errors`, `duplicates_dropped`, `queries_used`). **No systemd timer is
installed**; `ENABLE_POLYMARKET_SCOUT` **remains false** and still gates only the
future `--scheduled` path. **Do not deploy unless explicitly asked.**

Operational notes:

* A broadened scan writes up to `--limit` rows into `polymarket_markets` per run.
  The host DB is already near the OPS-011 growth **warning** tier — check
  `db-growth-report` before running large scans, and prefer `--limit`/
  `--orderbook-limit` over the ceilings. Retention prunes these rows after 14 days.
* Bounded by construction: page size ≤100 (server cap), ≤20 catalog pages, ≤5
  search pages per query, ≤1000 markets per scan, order books capped by
  `--orderbook-limit`. Skipped queries, fair-share caps, and Kalshi census
  truncation are **logged, never silent**.
* `queries_used` on the audit row records the queries actually **sent**, not the
  queries planned — a query starved by the market budget is never claimed as coverage.
* Coverage expansion identifies no arbitrage, computes no EV, recommends no trade,
  paper trades nothing, sizes nothing, places no orders, and uses no
  wallets/private keys/signing/swaps/execution. `comparable_supply` in the coverage
  report means *a comparison could be attempted*, never *this is an opportunity*.

## DB growth & alert calibration (OPS-011)

`db-growth-report` is the read-only storage view: file size, per-table row
counts + est MiB (SQLite `dbstat` when compiled in), largest tables, tick age
buckets, ticks-by-domain, edge-precheck/crypto growth, backups, retention
windows, and the calibrated alert thresholds. `prune-retention --dry-run` now
also prints a per-table projection (window, total, eligible, remaining,
oldest/newest ticks) — run it before adjusting any retention window.

```bash
cd ~/projects/probability-arena
.venv/bin/python -m app.cli db-growth-report
.venv/bin/python -m app.cli prune-retention --dry-run
```

Alert thresholds were raised after SCANNER-002 (the 512 MiB / 150-signals-per-
hour advisories tripped on normal live-slate volume). Defaults:
`DB_GROWTH_WARNING_MB=1536` / `DB_GROWTH_CRITICAL_MB=3072`,
`MARKETOPS_SIGNAL_FLOOD_WARNING_PER_HOUR=400` /
`..._CRITICAL_PER_HOUR=800`. To re-tune, edit `.env` (oneshot timer picks up
next run; restart the watcher only if a watcher-affecting flag changed). This
is ops/observability only — no forecasting, edge, or trading behavior changes.

## Tick aggregation (OPS-012 — manual, NO timer)

`market_price_ticks` dominates the SQLite file (~62%: raw rows carry
`raw_payload` JSON at ~2.8 KB/row). OPS-012 rolls raw ticks into fixed-interval
`market_price_tick_buckets` (OHLC midpoint, open/close bid/ask, spread/liquidity
ranges, tick counts — migration `0023`) so history survives at a fraction of the
storage. **Buckets are telemetry summaries, never trading signals.**

```bash
.venv/bin/python -m app.cli aggregate-market-ticks --hours 24 --dry-run   # preview; writes nothing
.venv/bin/python -m app.cli aggregate-market-ticks --hours 24             # idempotent upsert (rerun-safe)
.venv/bin/python -m app.cli tick-aggregation-report                       # coverage + staged recommendation
.venv/bin/python -m app.cli db-growth-report                              # now shows buckets + steady-state projection
```

Operational rules:

* **Aggregation never deletes raw ticks.** Only `prune-retention` prunes, on its
  own windows; **raw tick retention (`TICK_RETENTION_DAYS`) is UNCHANGED by
  OPS-012.** The tick-aggregation-report STAGES (never enacts) the future option
  of reducing raw retention toward 24-48h once coverage is proven healthy —
  enacting that is a separate, explicitly-accepted milestone.
* Buckets age out on their own `TICK_BUCKET_RETENTION_DAYS=90` window (via the
  existing retention timer's prune).
* Bounded: `TICK_AGGREGATION_MAX_ROWS=200000` raw rows per pass; a cap stop
  lands on an hour boundary and is printed (rerun to continue — never silent).
* Expected scale (validated on a 24h copy of real host ticks): ~203k raw →
  ~43.5k five-minute buckets in ~35s; hour coverage 100%; rerun updates in
  place with identical values.
* Manual only — no timer is installed for aggregation. If regular runs are
  wanted later, that is a separate deploy decision.

### OPS-013 hardening + gated timer

```bash
.venv/bin/python -m app.cli aggregate-market-ticks --hours 24                      # per-hour commits (default)
.venv/bin/python -m app.cli aggregate-market-ticks --hours 24 --subwindow-hours 2  # coarser commit unit
.venv/bin/python -m app.cli aggregate-market-ticks --scheduled --hours 12          # timer path: NO-OPS unless ENABLE_TICK_AGGREGATION_TIMER=true
.venv/bin/python -m app.cli tick-aggregation-report                                # coverage + READINESS gates
```

* **Per-sub-window commits**: the SQLite write lock is held for seconds per
  window (the OPS-012 full-window pass held one ~49s commit and produced the
  MarketOps #1215 transient). Per-window rows/buckets/commit_ms/retries are
  printed; a failed commit is retried bounded times as an apply+commit unit,
  then recorded LOUDLY (audit row + nonzero exit) — reruns repair it.
* **Timer rollout (two-step, like meme-news; do NOT enable unless asked):**
  1. `cp infra/systemd/user/probability-arena-tick-aggregation.{service,timer} ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now probability-arena-tick-aggregation.timer`
     — safe dark: the service runs `--scheduled` which no-ops while
     `ENABLE_TICK_AGGREGATION_TIMER=false`.
  2. Set `ENABLE_TICK_AGGREGATION_TIMER=true` in `.env` to go live (hourly,
     `--hours 12` overlap so cycles self-heal).
* **Raw-retention reduction stays staged**: check
  `tick-aggregation-report` — all readiness gates (coverage_72h ≥ 0.98,
  ≥ 5 clean scheduled cycles, no recent run errors, raw feed fresh) must pass
  before proposing the 3d → 24-48h change as its own explicitly-accepted
  milestone. `tick_aggregation_runs` (migration 0024) is the evidence trail.

## DB backup (OPS-007, hardened by SQLITE-BACKUP-COORDINATION-001)

Consistent snapshots via the sqlite3 online backup API (safe while all
services run), published atomically **only after verification**, with a
manifest and bounded tiered retention. Full design and the restore runbook:
`docs/SQLITE_BACKUP_COORDINATION_001.md`.

```bash
cd ~/projects/probability-arena
.venv/bin/python -m app.cli backup-db --dry-run   # capacity + retention plan only
.venv/bin/python -m app.cli backup-db             # verified backup (what the timer runs)
.venv/bin/python -m app.cli list-db-backups
.venv/bin/python -m app.cli verify-db-backup <BACKUP_DIR>/backup-<stamp>.db.gz
.venv/bin/python -m app.cli prune-db-backups --dry-run   # --confirm required to delete
```

**Destination:** `BACKUP_DIR=/mnt/data/probability-arena-backups` (set in `.env`).
Deliberately on `/mnt/data` — a separate ext4 volume with ~712 GiB free — rather
than the root volume that already holds the ~4.2 GiB database at 61% use.
Directory `0700`, artifacts and manifests `0600`.

**Scheduled daily** at `01:30 UTC` (18:30 America/Los_Angeles) with up to 10 min
jitter, via `probability-arena-backup.{service,timer}`. That slot avoids the
00:00–00:06 retention/baseline cluster and the hourly tick-aggregation slot at
`:22`. Install / uninstall:

```bash
cp infra/systemd/user/probability-arena-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now probability-arena-backup.timer
systemctl --user list-timers | grep backup

# uninstall
systemctl --user disable --now probability-arena-backup.timer
rm ~/.config/systemd/user/probability-arena-backup.{service,timer}
systemctl --user daemon-reload
```

Safety properties worth knowing before touching this: the newest backup is
**never** pruned; retention keeps 7 daily / 4 weekly / 3 monthly plus a floor of
the newest 7 backups; overlap returns `skipped_overlap` and capacity shortfall
returns `skipped_capacity`, neither of which counts as a successful backup and
neither of which deletes anything.

**Freshness check (SQLITE-BACKUP-FRESHNESS-ALERT-001).** A working backup
pipeline doesn't answer whether it's *still* producing recent backups — this
adds a local, read-only health check on top of it.

```bash
.venv/bin/python -m app.cli sqlite-backup-freshness-report --format text
.venv/bin/python -m app.cli sqlite-backup-freshness-report --format json
```

Zero provider calls, opens no database, writes/prunes/mutates nothing. Exit
code `0` = healthy, `1` = unhealthy (the `verify-db-backup` convention). Both
formats disclose the 36h threshold, the newest committed backup's age, and the
exact health reason (`healthy`, `backup_stale`, `manifest_invalid`,
`artifact_missing`, etc. — see `docs/SQLITE_BACKUP_FRESHNESS_ALERT_001.md` §5
for the full list). Age is read from the manifest's `created_at`, not
filesystem mtime.

An isolated MarketOps hook runs the same check at step 7b of every cycle
behind `MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT` (default **false**, not yet
enabled on EVO-X2). When enabled, an unhealthy result opens/updates one
deduplicated `backup_freshness_warning` alert in `marketops_alerts`, which
self-resolves once a fresh verified backup reappears; the hook cannot fail a
MarketOps cycle, even under `MARKETOPS_FAIL_FAST`. No new timer/daemon/table.

Restore drill (non-destructive): `gunzip -c <backup> > /tmp/scratch.db`, point a
scratch `DATABASE_URL` at it, run `db-stats`. A real restore is a
human-authorised destructive procedure — follow the runbook in
`docs/SQLITE_BACKUP_COORDINATION_001.md`, which preserves the damaged database
rather than deleting it.
TODO (later OPS milestone): scheduled off-host copies.

## MarketOps overlap guard (OPS-007)

Concurrent cycles cannot collide anymore: a second `marketops-run-once` (or a
timer firing during a manual run) records a graceful `skipped`
(`already_running`) run instead of a SQLite lock error, and a 'running' row
older than `MARKETOPS_LOCK_STALE_AFTER_MINUTES` is treated as crashed. SQLite
connections also carry a `SQLITE_BUSY_TIMEOUT_MS` wait. Manual cycles no
longer need to dodge timer firings.

## Reconciler lock-wait telemetry (CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001)

The crypto reconciler carries an explicit **lock-wait budget** — how long ONE
SQLite lock acquisition may WAIT — distinct from the write-**hold** SLO. This
section is for whoever eventually sets a threshold from the telemetry, because
several of these numbers do not mean what their names suggest.

**Gate context.** There is no recurring reconciler timer, and there must not be
one until a real `lock_wait_ms` distribution has been shown to stay bounded
across several `--force` passes. The instrument below IS the gate; read it
before running counted passes, not after.

### Read the threshold off the histogram tail, never off the scalar

`lock_wait_ms` (the pass total) is **biased high**. `LockWaitMeter` cannot split
a *succeeding* statement's duration into "slept in SQLite's busy handler" and
"did work" — Python's `sqlite3` exposes no busy-handler callback — so every
attempt's reported wait also contains that attempt's own DML and its commit
fsync. Measured on a pass with **zero contention** (no competing writer at all,
so every millisecond is bias):

```
lock_wait_ms 3380   lock_wait_measurements 402   lock_wait_ms_max 21
blocked_ms   9578   histogram {'1-10': 372, '10-100': 30, '>=100': 0}
```

~8.4 ms per attempt against a true wait of exactly zero: 35 % of `blocked_ms`
under no contention, and it scales with batch count (~340 s of phantom wait
across 100 passes).

The bias lands almost entirely in the `1-10` bucket, so:

* **Derive thresholds from `lock_wait_decision_tail(lock_wait_histogram_ms)`,
  never from pass-total `lock_wait_ms`.** The histogram's edges are fixed, so
  per-pass rows add up across passes into a real distribution.
* `lock_wait_ms_baseline_per_attempt` is the pass's own in-band estimate of the
  per-attempt bias and `lock_wait_ms_net` is the total with
  `baseline x measurements` removed. Use the net if you must use a scalar —
  but read the next two subsections first, because both the estimator and the
  bucket this section originally nominated were wrong.

#### The decision bucket is `>=1000 ms`, not `>=100 ms`

An earlier version of this section named the `>=100 ms` buckets as the
threshold source. **They are contaminated by the pass's own fsync.** Measured
on two genuinely uncontended passes:

```
lock_wait_ms_max 485   write_hold_ms_max 484
lock_wait_ms_max 480   write_hold_ms_max 479
```

One fsync stall, counted once as a **hold** and once again as a "lock wait",
puts a phantom sample in `100-1000` on a pass whose true wait is exactly zero.
Over ~100 counted passes that is ~100 phantom samples in the bucket the
threshold would come from.

**Chosen fix: move the discriminator to `>=1000 ms`** — i.e. the
`1000-5000`, `5000-15000`, `15000-30000` and `>=30000` buckets, which
`lock_wait_decision_tail()` sums for you. Why this rather than subtracting an
uncontended reference pass's `>=100` count:

* it needs no reference pass to be captured, stored, kept current per host, or
  remembered by whoever reads the histogram months from now;
* `>=1000` was **0 on all four** measured uncontended passes;
* the contamination is bounded by `write_hold_ms_max` — but the margin is
  **~1.8x, measured, not the "order of magnitude" an earlier version of this
  section claimed.** That claim rested on the two samples above (484/479 ms).
  Four uncontended passes on EVO copies moved the peak up 12 %:

  ```
  write_hold_ms_max : 479, 544, 92, 532
  lock_wait_ms_max  : 480, 530, 20, 521
  ```

  **Peak 544 ms is 54 % of the 1000 ms edge — a margin of ~1.8x.** The edge is
  still the right choice and it is verified clean: the decision tail was 0 on
  all four passes, while `100-1000` collected 1/2/0/1 samples, which is exactly
  the fsync contamination the edge was moved to avoid. But the separation is a
  measured margin on this host, not a comfortable property of the mechanism,
  and EVO is not a permanently quiet host: 61 GB is held by unrelated
  co-tenants with 4 GB of swap in use, so its fsync tail is not fixed. A host
  with roughly **2x slower fsync puts *uncontended* stalls straight into the
  decision bucket.**

  **Revisit trigger, numeric rather than qualitative: re-examine this edge if
  any counted pass reports `write_hold_ms_max > 700`.** ("Approaches 1000 ms",
  the wording this replaces, is already half-true at 544.)

Reading a single pass: a `100-1000` sample within a few ms of that pass's
`write_hold_ms_max` is that pass's own fsync, not a wait.

#### Reading the decision tail: per phase, never the total

**(CRYPTO-RECONCILER-LOCK-WAIT-PHASE-ATTRIBUTION-001.)** Three production
`--force` passes on EVO at load 0.4-0.8, after the deadline fix:

| tape_run_id | duration_ms | lock_wait_ms | max | measurements | `>=1000` tail | batches |
|---|---|---|---|---|---|---|
| 3813 | 30,126 | 4,615 | 1,191 | 326 | **1** | 323 |
| 3814 | 30,148 | 4,461 | 1,334 | 389 | **1** | 387 |
| 3815 | 30,117 | 4,502 | 1,337 | 383 | **1** | 381 |

The decision bucket holds **exactly one sample on every pass** — never zero,
never two — with maxima clustered just above the 1000 ms edge. Random
co-tenant contention does not produce that shape. A once-per-pass *systematic*
event does, and a pass has **two** of them:

* **`run_row`** — the run row's CREATION commit, the pass's first write (cold
  page cache, first journal creation);
* **`finalize`** — the run row's FINALIZE commit, bounded differently from the
  batch ladder (`RECONCILE_FINALIZE_MAX_LOCK_ATTEMPTS = 1`, inheriting the
  connection's 30 s busy timeout rather than the tight derived budget).

The census confirms it exactly on all three passes:
`measurements = batches + 1 (run row) + 1 (finalize) + retries`
(326 = 323+1+1+1, 389 = 387+1+1+0, 383 = 381+1+1+0).

So the **pass-total tail has a systematic floor of ~1 that is not contention at
all**, and a threshold derived from it would be measuring the instrument rather
than the host. Same defect class as the fsync counted once as a hold and once
as a lock wait (subsection above), one layer further out.

**The fix is attribution, not subtraction.** Every recorded attempt now carries
its phase, and the tail is reported per phase:

```
lock_wait_decision_tail            whole pass  (kept for reconciliation only)
lock_wait_decision_tail_batch      THE TIMER'S BASIS
lock_wait_decision_tail_finalize   tracked against TimeoutStartSec, not this
finalize_lock_wait_ms              the finalize's own wait, kept in its own right
lock_wait_phases                   {phase: {lock_wait_ms, lock_wait_ms_max,
                                            measurements, decision_tail}}
```

`lock_wait_phase_decision_tail(source, phase)` is the executable predicate —
give it either a pass summary or a run row's `config.write_coordination` blob.
The CLI prints all of the above on every pass, and on the `db_locked` refusal
path too.

**On the run row**, the container is persisted as
`lock_wait_phases_before_finalize` (staged inside the finalize commit, same
caveat as `blocked_ms_before_finalize`). Two consequences:

* its `batch` entry is **complete** and is what the timer decision reads;
* its `finalize` entry is **structurally zero** — the closure runs inside the
  very commit it would be measuring. The flat `*_finalize` mirrors are
  therefore *omitted* from the run row rather than persisted as misleading
  zeros. **Read the finalize's wait off the pass summary / CLI output, never
  off the run row.**

The **prelude** is bounded but not metered and is therefore *not* a bucket here
— see "The prelude is bounded but not metered" below. Phase attribution does
not fix the fsync-shaped bias either (`lock_wait_ms` remains a tight upper
bound, median-corrected via `lock_wait_ms_baseline_per_attempt`, erring high
and never negative); it only makes the residual **attributable**.

#### The bias baseline is the MEDIAN attempt, and the net does not go to zero

This section used to say the corrected scalar "goes to ~0 on a zero-contention
pass, which is the correct answer". **Measured, it did not.** On a genuine
zero-contention pass (true wait exactly 0) the min-estimated baseline left
`lock_wait_ms_net = 2250 ms`:

```
lock_wait_ms 3810   measurements 390   mean bias 9.77 ms/attempt
min-estimated baseline 4 ms  ->  net = 3810 - 4*390 = 2250 ms   (41 % recovered)
```

Reproduced across two independent runs (3810/390 and 3809/392 attempts). The
cause is that the per-attempt bias is **right-skewed**, and a min-estimator
under-corrects a right-skewed distribution.

`lock_wait_ms_baseline_per_attempt` is therefore now the **median** retained
attempt, not the minimum. Measured on this repo's dev Mac, four zero-contention
passes (`batch_size=5`, no competing writer anywhere, so the correct net is 0):

| attempts | total ms | mean ms/att | min | median | net via min | net via median |
|---------:|---------:|------------:|----:|-------:|------------:|---------------:|
| 56       | 132      | 2.36        | 1   | 2      | 76 (42 % recovered) | 20 (85 %) |
| 146      | 823      | 5.64        | 2   | 3      | 531 (36 %)  | 385 (53 %)     |
| 146      | 537      | 3.68        | 2   | 3      | 246 (54 %)  | 100 (81 %)     |
| 146      | 460      | 3.15        | 2   | 2      | 168 (64 %)  | 168 (64 %)     |

The median is never worse than the min and is materially better in three of
four; the dev Mac's own per-attempt bias is only 2-6 ms, so millisecond
quantisation flattens the difference there far more than it would on EVO.

**What the corrected scalar converges to, stated as measured rather than
asserted: not zero.** It converges to `(mean − median) × attempts`, the
residual right-skew of the bias distribution — 15-47 % of the raw scalar on the
dev Mac. It is an **upper bound** on the true wait (a median sits below a
right-skewed mean, so the subtraction is deliberately conservative), never an
estimate of it. **The residual on EVO is unknown until a counted pass measures
it there, and capturing it is a job for the first counted `--force` pass: run
one with no competing writer and record `lock_wait_ms`, `measurements`,
`lock_wait_ms_min`, `lock_wait_ms_baseline_per_attempt` and `lock_wait_ms_net`.
That row is the host's zero-contention reference.**

Two further limits, stated rather than discovered later, and **each with its
sign spelled out** — "under-reports" without a sign is the one thing a reader
cannot act on.

**1. The sample cap makes the median a PREFIX median, and that errs
conservative.** The baseline is the median of the first
`RECONCILE_LOCK_WAIT_SAMPLE_CAP` (256) *retained* attempts — a bounded-memory
choice, not a statistical one. Measured passes sit at 250-262 attempts (right
at the cap) and earlier 30 s passes at 390-392 (so the median came from the
first ~65 %). Early attempts in a pass are the **cheapest** (smaller journal,
warm-up), so a prefix median sits **below** the pass's true median, the
subtracted baseline is **too small**, and the correction therefore
**under-corrects**. Direction, explicitly: **`lock_wait_ms_net` errs HIGH, never
low, and this effect can never drive it negative** (the reported net is also
clamped at 0). An over-reported net is a false alarm an operator can
investigate; an under-reported one would be a missed one.

**2. A majority-contended pass errs the other way.** If more than half the
retained attempts are genuinely contended, the median contains real waiting,
the subtracted baseline is **too large**, and the net **under**-reports the
true wait. That direction — the dangerous one — is exactly why the decision
basis is the histogram tail and not the scalar.

### Fields, and where to find them

Every pass reports the whole contract, including refusals — a `db_locked`
abandon reports what it had measured before giving up rather than a row of
`None`s. Per-pass scalars plus the histogram are persisted on the run row under
`config.write_coordination` (suffixed `_before_finalize` where the value is
staged before the finalize commit).

**Key on `stop_reason`, not `status`.** On lock-wait-budget expiry the returned
`status` can be `backlog_expiring` rather than `partial`, because the frontier
override runs after the lock-wait status assignment. `stop_reason` is
`lock_wait_budget` in that case, and it is the discoverable field.

### What the residual bound is, and is not

The advertised residual (`deadline + ATTEMPT_MULTIPLIER x budget`) is a **model
of the idle host, not a guarantee**:

* `RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT = 2.0` is a **chosen safety factor**,
  not a measurement. Re-derived on EVO (SQLite 3.45.1, idle) the true
  per-acquisition overshoot is ~**1.01x**; re-measured on the dev Mac at load
  average ~5-6, same SQLite version, it reaches **5.80x**. The factor tracks
  host load, not SQLite, so no constant can bound it.
* If the EVO histogram shows passes failing into `partial` /
  `stop_reason=lock_wait_budget` on contention they had deadline left to
  absorb, **lower** the constant toward the measured ~1.0x — do not raise the
  deadline.

### The prelude is bounded but not metered

The selection **prelude** (`backlog_size`, `classify_backlog`, `_universe`,
`universe_size`, `unreconciled_backlog`, the frontier queries) and the governed
path's MarketOps-health read are reads, and reads block behind an EXCLUSIVE
holder. They now run inside the same derived budget as the write loop — before
that fix a 30 s-deadline pass was measured at 60.11 s with
`lock_retry_events=0`, two successive read acquisitions each burning the full
30 s process timeout. But the accounting object lives inside the write phase,
so **prelude waits do not appear in `lock_wait_ms` or the histogram**. The
histogram describes the WRITE path's wait distribution, which is the quantity
the gate is about.

`prelude_ms` is the wall time of the **whole** budgeted prelude block;
`classify_ms` is only its `classify_backlog` step. `prelude_ms - classify_ms`
is the part that used to be unattributable — the other five queries sat outside
`classify_ms` but inside the budget, so a block in any of them inflated
`duration_ms` alone. Both print on every pass.

### Prelude-blocked passes: excluded from the distribution, counted separately

A pass abandoned **before** it reached the write phase reports a full contract
of real zeros (that is deliberate — the alternative was a row of `None`s). Real
zeros are honest per pass and **wrong to aggregate**: summed into a histogram
they are indistinguishable from a healthy pass. A zero row averaged in as
benign is worse for the distribution than a missing row was.

**The diagnostic this runbook used to give does not fire.** It said "a pass
whose wall time exceeds the model with an empty tail was most likely blocked in
the prelude" — but both measured prelude-blocked passes came in at **15.04 s
against a 42 s model and a 30 s deadline**, i.e. comfortably *under* the model.

The signature that IS unambiguous, observed on both:

```
status == "db_locked"  AND  lock_wait_measurements == 0  AND  duration_ms > 0
```

`db_locked` says contention ended the pass; `measurements == 0` says the write
phase — the only place the accounting object lives — was never reached;
`duration_ms > 0` says the process nevertheless ran. Nothing else produces that
combination: a pass abandoned *inside* the write phase carries
`measurements >= 1`, and a validation refusal is not `db_locked`.

Every governed result carries the verdict as **`lock_wait_distribution_eligible`**
(computed by `lock_wait_distribution_eligible()`; the CLI prints it, including
on the refusal path). **Rule for the counted passes:**

* `lock_wait_distribution_eligible=false` rows are **excluded** from the wait
  distribution, and
* **counted separately** as a running "blocked before it started" tally.
  How often the reconciler cannot even begin is itself a result the gate wants
  — it is not a censored sample and must not be silently dropped either.

### Orphaned `status='running'` rows: excluded, and counted separately

Same rule, one layer down. A SIGKILL mid-finalize (the `TimeoutStartSec`
outcome derived below) leaves the committed token batches durable and the run
row at **`status='running'` forever**. Nothing jams — the overlap guard is a
flock released on process death — but **nothing reconciles those rows either**,
and the counted-passes analysis reads run rows, so an orphan would otherwise be
counted as a pass that finished.

`lock_wait_run_row_orphaned(status, config)` is the executable classifier. The
signature is exact rather than heuristic, because `status` and
`config.write_coordination` are written by the **same** finalize commit:

```
status == "running"  AND  no `config.write_coordination`
```

A row that finalized carries both; a row that did not carries neither. Caveat
the caller owns: a pass **currently in flight** matches this too, so run the
analysis when no reconciler pass is running — which is the only authorised
shape today anyway (attended `--force`, no recurring timer).

**Rule for the counted passes:** orphaned rows are **excluded** from the
`lock_wait_ms` distribution (they carry no `write_coordination` to sum, and
counting them among completed passes inflates the denominator) and **counted
separately**. The rate at which a SIGKILLed finalize leaves an orphan is itself
a result the gate wants — identical reasoning to the prelude-blocked tally
above, and a zero row averaged in as healthy is worse than a missing row.

Note the in-process cousin: a finalize that merely **loses the lock race**
(rather than being SIGKILLed) also leaves `status='running'`, but it *does*
return a full summary with the whole lock-wait accounting, reported as
`partial` / `skipped_contention`. That pass is a normal, eligible sample; only
the row on disk looks orphaned. This is why the tally is a property of the run
rows and not a substitute for reading the pass output.

### Guarded recurring reconciler timer (CRYPTO-RECONCILER-GUARDED-TIMER-001)

Eric **approved a guarded recurring 6-hourly timer** on 2026-08-12. Nothing in
this milestone is installed on EVO-X2: the unit files are repo templates and
activation is an operator action, in the order below. **The disable procedure
comes first on purpose** — an operator reaching for this section is usually
reaching for the brake.

#### Disable the guarded reconciler timer

Three levels, cheapest first. Each is complete on its own; you do not need the
next one.

```bash
# 1. STOP FUTURE PASSES (keeps the unit installed, keeps all state).
#    The pass becomes a clean no-op that writes nothing and exits 0.
sed -i 's/^ENABLE_CRYPTO_TAPE_RECONCILER=.*/ENABLE_CRYPTO_TAPE_RECONCILER=false/' \
  ~/projects/probability-arena/.env
grep ENABLE_CRYPTO_TAPE_RECONCILER ~/projects/probability-arena/.env   # verify

# 2. STOP THE SCHEDULE ITSELF (also stops the dark no-op firing at all).
systemctl --user disable --now probability-arena-crypto-reconcile.timer
systemctl --user list-timers | grep crypto-reconcile      # expect: nothing

# 3. STOP A PASS THAT IS RUNNING RIGHT NOW.
systemctl --user stop probability-arena-crypto-reconcile.service
```

Level 3 is safe by construction and needs no cleanup: batches committed before
the stop are durable, the interrupted batch is rolled back and neither
duplicated nor lost, and the unworked tokens return to the backlog. If the stop
lands mid-finalize the run row is left at `status='running'` with no
`config.write_coordination` — the already-classified orphan signature
(`lock_wait_run_row_orphaned()`); it is excluded from the lock-wait
distribution and tallied separately. **Nothing is corrupted and nothing needs
repairing by hand.**

There is also an AUTOMATIC disable: the rolling health gate (below) can latch
the timer off without a human. `crypto-reconciler-health` shows whether that
has happened.

#### Enable the guarded reconciler timer

Do these in order; each step's check must pass before the next.

1. **The pre-existing preconditions still apply** — see "Recurring-timer
   preconditions" below. In particular precondition 3: a **measured**
   `CRYPTO_TAPE_RECONCILER_INITIAL_PER_TOKEN_COST_SECONDS` for this host, with
   adaptive batching on. It has no default by design; never guess it. This one
   is deliberately **not** enforced as a per-run pre-flight skip: it is a
   settings value that cannot change between two runs six hours apart, so it
   is an enable-time check, and making it a per-run skip would turn the flag
   into a silent no-op for every caller that does not set it.
2. **Confirm the guard has somewhere to keep state.** The health gate is inert
   without a file-backed SQLite database:
   ```bash
   .venv/bin/python -m app.cli crypto-reconciler-health
   # REQUIRED: a line starting `state_path=` and `latch=CLEAR`.
   # `state=inert` means the gate would watch NOTHING — do not enable.
   ```
3. **Install the units dark** (flag still false):
   ```bash
   cp infra/systemd/user/probability-arena-crypto-reconcile.{service,timer} \
      ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now probability-arena-crypto-reconcile.timer
   journalctl --user -u probability-arena-crypto-reconcile.service -n 20 --no-pager
   # expect: status=disabled  external_calls=0  no-op
   ```
4. **Flip the flag** and let exactly one natural tick run:
   ```bash
   sed -i 's/^ENABLE_CRYPTO_TAPE_RECONCILER=.*/ENABLE_CRYPTO_TAPE_RECONCILER=true/' \
     ~/projects/probability-arena/.env
   systemctl --user list-timers | grep crypto-reconcile     # next tick
   ```
5. **Read the first unattended pass** before trusting the second:
   ```bash
   journalctl --user -u probability-arena-crypto-reconcile.service -n 60 --no-pager
   .venv/bin/python -m app.cli crypto-reconciler-health
   ```
   Expected on a healthy pass: `batch_lock_wait_aborts=0`,
   `batch_lock_wait_warnings=0`, `batch_lock_wait_ms_max` inside the measured
   1.0-1.3 s band (well under the 2,000 ms review line), `review_required=False`,
   `health_gate_state=active`, `health_gate_tripped=False`, `latch=CLEAR`,
   `skips_total=0`, and `adaptive_batching_active=True` (precondition 3 —
   if this reads `False`, step 1 did not take effect).
   `status=partial (stop_reason=deadline)` plus `truncated` is **normal** at
   production density and exits non-zero by design — that is the pass telling
   you work remains, not a fault.

#### The guard rails, and the fact that every threshold is a CHOICE

None of these numbers is derived or measured. They are **operational policy**,
chosen against measured evidence, and they should be re-chosen if the evidence
moves. The evidence they were chosen against: six counted production passes
(`tape_run_id` 3813-3815 plus three phase-attributed) with duration
29,730-30,164 ms against a 30 s deadline, `lock_wait_ms` 3,238-4,615, **batch**
maxima 16/514/1037 ms, finalize 8-12 ms, `run_row` 9-191 ms,
`write_hold_ms_max` 84-194 ms, 0-1 retries, **zero** lock failures and **zero**
write-hold SLO violations. Eric's framing, which the whole design follows:
*"occasional 1-1.3s waits are acceptable, escalating/repeated waits are not."*

| Layer | Trigger | Chosen value | Effect |
|---|---|---|---|
| Pre-flight skip | MarketOps unhealthy (`_reconciliation_should_abort`, reused) | — | `status=marketops_degraded`, nothing read or written, recorded |
| Pre-flight skip | health latch tripped | — | `status=skipped_health_latch`, nothing read or written, recorded |
| Per-run | **batch-phase** lock wait | `>= 2000 ms` | run marked for review; feeds the gate (this is the sub-threshold trend line) |
| Per-run | **batch-phase** lock wait | `>= 5000 ms` | warning: recorded and logged, run continues |
| Per-run | **batch-phase** lock wait | `>= 7000 ms` | abort: ladder stops via the existing `stop_reason=lock_wait_budget` path; committed batches durable |
| Post-run review | `write_hold_ms_max` | `> 700 ms` | run marked for review; feeds the gate |
| Post-run review | `wall_time_model_exceeded` | `true` | run marked for review; feeds the gate |
| Post-run review | any lock failure / write-hold SLO violation | — | run marked for review; feeds the gate |
| Rolling gate | severe waits (`>=5000 ms`) | 2 of the last 4 runs | **latch: auto-disable** |
| Rolling gate | contention stops (`contention`/`lock_wait_budget`) | 3 consecutive | **latch: auto-disable** |
| Rolling gate | `marketops_degraded` skips | 2 consecutive | **latch: auto-disable** |
| Rolling gate | runs marked for review | 3 of the last 4 runs | **latch: auto-disable** |

Four things a reader must not misread:

* **The lock-wait figure is the PHASE-ATTRIBUTED `batch` one**, never the pass
  total. The total carries a ~1-sample-per-pass systematic floor from the
  `run_row` and `finalize` bookkeeping commits; a threshold read off it would
  be measuring the instrument. See "Reading the decision tail: per phase, never
  the total".
* **A bare `partial` is NOT a contention signal.** At production density every
  pass ends `partial`/`deadline`/`truncated`; the gate's contention rule
  deliberately requires `stop_reason in {contention, lock_wait_budget}` or a
  lock-failure status. A rule that counted plain `partial` would trip on the
  first three healthy runs.
* **The budget caps the wait; the breaker caps the ladder.** `7000 ms` is
  **not** a ceiling on how long any single acquisition may block. A wait is
  classified only *after* it completes and its duration is known — a measured
  `abort_ms=24517` against the 7,000 ms line is a 24.5 s wait that already
  happened and is now being judged. The only thing bounding one acquisition is
  the **lock-wait budget** (`derive_lock_wait_budget_seconds`, ~5 s at the
  shipped 20 s deadline, times the statement overshoot), which this milestone
  does not touch. What the abort line prevents is the **next rung** — retrying
  into the same holder.
* **`>= 2000 ms` is a mark, not a brake.** One run at 2 s does nothing. It is
  the *trend* line: without it the gate implemented only the first half of
  *"occasional 1-1.3s waits are acceptable, escalating/repeated waits are
  not"*, and a host sitting at 4,999 ms — ~5x the worst healthy batch wait ever
  observed, ~3,800x the healthy median — could run **eight consecutive passes,
  two full unattended days, with nothing firing**, as could a monotonic climb
  from 1.0 s to 4.9 s. Marked runs feed the existing 3-of-4 review rule, so any
  of those sequences now latches on the third marked run.

#### The escape boundary: what this gate deliberately does NOT catch

Stated as policy so it is never mistaken for an oversight. A sweep of sustained
contention **frequency** — every run's worst batch wait held at 1,037 ms, the
worst healthy maximum ever measured — evaluating after every run. (The
orthogonal **severity** sweep is the next subsection; do not read one table for
the other.)

| sustained contention rate | latches? |
|---|---|
| 25%, 33% | never |
| **50%** (`CH`, `CHCH`, `CCHH`) | **never, in 40 runs** |
| 67% | at run 4 |
| 75% | at run 3 |
| 100% | at run 3 |

**A host on which half of every pass stops on contention, indefinitely, never
latches on the contention rule.** That is accepted, and these are the reasons —
if any stops holding, the rule must be re-chosen:

* every contended pass still commits its durable batches and returns unworked
  tokens to the backlog: nothing is lost, nothing is corrupted;
* each such run is individually marked `review=True` and is visible in
  `crypto-reconciler-health`, so it is reported even when it is not disabled;
* at 50% alternation **only the severe-wait rule (`>= 5000 ms`, 2 in 4) catches
  anything** — see the severity sweep immediately below.

##### The residual band, measured: 2.0-4.999 s at <= 50% frequency

An earlier version of this section claimed the severe-wait rule *and* the
`>= 2000 ms` review line "independently catch the alternating cases". **That
was wrong**, and the correction matters because the accepted band is larger
than the wrong claim implied. A second sweep — the contended run at each wait,
genuinely *clean* interleaved runs (1,037 ms, inside the accepted band),
evaluated after every run:

| contended wait, alternating 50% | latches? |
|---|---|
| 1,200 / 2,000 / 2,500 / 3,800 / **4,999 ms** | **never, in 40 runs** |
| 5,000 / 5,500 / 7,000 ms | at run 3 (severe-wait rule) |

**At 50% the `>= 2000 ms` review line contributes nothing.** It marks the
contended half only, so the window holds 2 marked runs of 4 and never reaches
the 3-of-4 review rule; the alternation is caught only once the wait crosses
the 5,000 ms severe line. The residual accepted region is therefore a bounded
rectangle in (severity x frequency): **waits of 2.0-4.999 s occurring on at
most half of passes**. Above 5,000 ms, or above 50% frequency, the gate latches.

##### The lever that would close it — considered and DECLINED

A reviewer verified the 50% escape **is** closable, just not by a threshold:
tightening `HEALTH_REVIEW_RUNS` from 3 to 2 catches benign 50% alternation at
run 3 at every wait in the table above (1,200 ms included), and it does **not**
fire on the six measured healthy passes (sustained 0/16/514/1,037/1,300/1,999
ms all stay clear, because a healthy pass is not a contention stop and is not
marked at all). So no threshold can close this boundary; the **count** lever
can. It is declined, on the record, for two reasons:

* it would close far more than the residual band. It latches a 50%-contended
  host at **1,037 ms** — squarely inside the 1.0-1.3 s band Eric explicitly
  accepted — so it does not narrow the accepted rectangle, it deletes the whole
  50% policy that this section exists to state;
* 2-of-4 would also latch on **two 2.0 s runs in a day**, which is arguably
  still "occasional", and it would make the severe-wait rule (2 in 4) largely
  redundant.

If the residual band ever needs closing, this is the lever — do not
re-discover it as a gap, and do not reach for a lower threshold, which cannot
work.

A 50%-contended host that is not actually hurting anyone is a host to report,
not a host to auto-disable — and a boundary an operator cannot see would be a
blind spot rather than a policy, so `crypto-reconciler-health` prints
`contention_total` / `contention_rate` beside the skip rate (below). Because
**no latch will ever summon anyone for the 2.0-4.999 s at <= 50% band**,
`contention_rate` is a HUMAN control on an automated system: reading it is a
scheduled operator step, not merely an available field. See "Weekly: read
`contention_rate`" below. **Read the rate; do not wait for a latch that is not
coming.**

#### The auto-disable latch, and how a human clears it

State lives in `.crypto-tape-reconciler-health-solana.json` **next to the
SQLite file** — not in the database, because the outcomes the gate most needs
to count (`marketops_degraded`, prelude `db_locked`, `skipped_contention`) are
exactly the ones that never write a run row.

```bash
# read it (read-only, safe any time)
.venv/bin/python -m app.cli crypto-reconciler-health

# clear a tripped latch — only after actually looking at why it tripped
.venv/bin/python -m app.cli crypto-reconciler-health \
  --clear --operator eric --note "co-tenant backup ran long; MarketOps green"
```

* Tripping is **loud**: `logger.error`, a `!! HEALTH GATE TRIPPED` block in the
  pass output, and a **non-zero exit even when the pass itself was `ok`**, so
  the unit goes red in `systemctl --user list-units`.
* Nothing clears it automatically. Healthy runs do not clear it; time does not
  clear it; only `--clear --operator <name>` does. Clearing keeps the trip in
  `latch_history` and restarts the rolling window past the records that tripped
  it (otherwise the same history would re-trip on the next run).
* The latch blocks the **timer**, not the operator: `--force` and `--dry-run`
  still run and report `health_latched=True`, because a human running a pass by
  hand is already the human the latch was trying to summon.
* An unreadable or corrupt state file **fails closed** (scheduled runs are
  blocked) and is never overwritten — it is evidence.
* **The raw records age out; the reasons do not.** Latched runs are admitted to
  the history and are inert there (not contention, not a MarketOps skip,
  `review=False`), so at 4 latched runs a day against
  `HEALTH_HISTORY_MAX_RECORDS = 40` they overwrite the raw records that
  produced the trip within ~10 days. The *reasons* survive in `latch` /
  `latch_history` (`HEALTH_LATCH_HISTORY_MAX = 10`), which is what an operator
  reads. Accepted, and noted so a reader is not surprised: if you need the raw
  window that tripped a latch, read it within ~10 days.

##### Read the SKIP RATE, not just the latch

`crypto-reconciler-health` prints **three differently-scoped lines, each
carrying its own denominator**:

```
retained_history skips_total=9  skip_rate=0.3214  runs_total=28  skips_by_status={'marketops_degraded': 9}  contention_total=0  contention_rate=0.0  (every retained run, bounded at 40 runs)
since_clear      skips_total=9  skip_rate=0.3214  runs_total=28  skips_by_status={'marketops_degraded': 9}  (every run since the last --clear; the records the gate may evaluate)
last_4 runs_evaluated=4  severe_wait_runs=0  consecutive_contention_runs=0  consecutive_marketops_skips=0  review_runs=1  (…)
distribution_excluded_total=9  distribution_excluded_by_status={'marketops_degraded': 9}
```

**Never read one line's rate against another line's denominator.** These are
three different spans and they were once *two adjacent lines both labelled
`gate_window`* — a reviewer could reasonably have read `skip_rate=0.3214` as
"of the last 4 runs" and been wrong; on that state the last-4 skip rate was
0.25. The spans are:

| line | span | what it is for |
|---|---|---|
| `retained_history` | every retained record, bounded at `HEALTH_HISTORY_MAX_RECORDS` = 40 | the operator's rate — is this a healthy *week*? |
| `since_clear` | every record after `gate_window_start_seq` | the records the gate is allowed to evaluate at all |
| `last_4` | the last `HEALTH_WINDOW_RUNS` of those | the window the 2-in-4 / 3-in-4 rules actually read |

On a host that has **never been cleared**, `retained_history` and `since_clear`
are the same runs and the two lines are identical — expected, not a bug; they
diverge the moment someone runs `--clear`. Two fields on the `last_4` line are
a fourth span again and say so inline: `consecutive_contention_runs` and
`consecutive_marketops_skips` are *trailing* counts that run back through
`since_clear`, not only through the last 4.

**A clean latch is not a clean week.** The gate's own
`consecutive_marketops_skips` is a *trailing* count: a week in which 9 of 28
passes were skipped for `marketops_degraded` — a 32% skip rate, a third of the
week doing no work — reads `consecutive_marketops_skips=0` whenever the last
pass happened to be healthy, and the whole reading is `latch=CLEAR
review_runs=0 consecutive_marketops_skips=0`. That is why the rate exists. The
rule that excludes skips from the lock-wait distribution is only defensible
because they are **counted** somewhere, and this is where.

`distribution_excluded_*` is the wider tally: pre-flight skips **plus** the
prelude-blocked `db_locked` passes (see "Prelude-blocked passes"). A pass
blocked in the prelude did try and a skip did not, so they are reported
alongside each other rather than merged.

Each run line also prints `adaptive_batching_active`, so a drifted
precondition 3 (a measured `initial_per_token_cost_seconds`, adaptive batching
on — an **enable-time** check, and enable-time checks are human steps that
drift) is visible in the gate history instead of invisible. It is recorded
only; nothing enforces it per run, deliberately.

##### Weekly: read `contention_rate` — the one control nothing automates

**This is a recurring operator step, not an available field.** Everything else
in this section eventually summons a human by latching. The residual accepted
band — waits of **2.0-4.999 s on at most half of passes** (see "The residual
band, measured") — never will: no rule fires there, by design. `contention_rate`
is therefore a **human** control on an otherwise automated system, and a control
nobody is scheduled to read is not a control.

Once a week, on EVO:

```bash
.venv/bin/python -m app.cli crypto-reconciler-health
```

Read, in this order:

1. `latch=` — `CLEAR` or `TRIPPED`. If `CLEAR`, the automation has nothing to
   tell you and the remaining steps are the whole point of the read.
2. `retained_history … contention_rate=` — the operator's number. `0.0` on a
   healthy host. **`> 0.25` sustained across weeks is a finding to report**,
   even with `latch=CLEAR`, because the 50% escape boundary is deliberate: a
   rate that keeps climbing toward 0.5 is exactly the shape the gate has
   agreed not to act on.
3. `retained_history … skip_rate=` — a third of the week doing no work reads as
   `latch=CLEAR` too (see above).
4. The per-run lines' `batch_lock_wait_ms_max`. Values parked in the
   **2,000-4,999 ms** band alongside a non-zero `contention_rate` are the
   residual rectangle itself, observed. Nothing will latch; you are the alarm.

Take it to a milestone decision rather than tightening a constant in place —
the lever that would close this band is `HEALTH_REVIEW_RUNS`, and it is
declined for stated reasons, not unnoticed.

##### NEVER DELETE THE HEALTH STATE FILE — clear the latch with the CLI

This is an instruction, not a description. Measured behaviour: a latch present
**blocks**; a **corrupt** file **blocks** (fails closed — correct); a
**deleted** file **does not block**. The realistic path to a deleted file is an
operator "fixing" the corruption warning by removing the file — the natural
response — or a cleanup/restore sweeping up a dotfile.

```bash
# WRONG — this silently clears the latch and erases the trip.
rm ~/…/.crypto-tape-reconciler-health-solana.json

# RIGHT — a named human decision, and the trip is retained in latch_history.
.venv/bin/python -m app.cli crypto-reconciler-health \
  --clear --operator eric --note "what you actually checked"
```

If the file is corrupt, **leave it and clear it with the CLI**; if the CLI
cannot read it either, move it aside (`mv <file> <file>.corrupt-$(date +%s)`)
so it stays as evidence, and record what you did here. Never `rm`.

**Cross-check before trusting `latch=CLEAR`.** A deleted state file reads
exactly like a host that never tripped, so the state file is not the only place
to look: every trip also writes a `logger.error` line naming the state path and
exits the unit non-zero.

```bash
journalctl --user -u probability-arena-crypto-reconcile.service \
  --since "-14 days" | grep "HEALTH GATE TRIPPED"
# A hit here beside `latch=CLEAR` = the state file went missing after a trip.
```

That check covers **every** trip path, including the pre-flight skips
(`marketops_degraded`, `skipped_health_latch`) that write no run row at all.

### Recurring-timer preconditions

These are the conditions the *guarded* timer above is enabled under; they were
written when there was no recurring timer at all and remain the enable
checklist. "Model, not guarantee" is sufficient for the
attended `--force` phase — a longer-than-modelled pass still terminates and
rolls back cleanly with a human watching — but it is **not** sufficient
unattended, and the answer is not a better constant (there isn't one: the
overshoot term tracks host load, 1.01x on idle EVO and 5.80x on a dev Mac at
load 5-6).

1. **Observed wall-time non-exceedance.** Every pass now records
   `wall_time_model_ms` (`modelled_pass_wall_seconds`: data deadline + one
   in-flight batch's whole retry ladder + the finalize's single attempt) and
   `wall_time_model_exceeded` next to its own `duration_ms`. The precondition
   is `wall_time_model_exceeded=false` on **every** counted pass — observed,
   not derived. One exceedance resets the count and is a finding, not noise.
2. **A bounded decision tail — on the BATCH phase.** The threshold is derived
   from `lock_wait_decision_tail_batch` (equivalently
   `lock_wait_phase_decision_tail(row, "batch")`), i.e. from **batch/read-path
   contention**, and **explicitly not** from the pass total. The total carries a
   known systematic floor of ~1 sample per pass produced by the two
   once-per-pass bookkeeping commits (`run_row`, `finalize`) — measured on
   tape_run_id 3813/3814/3815 — and a threshold read off it would be measuring
   the instrument rather than the host. See "Reading the decision tail: per
   phase, never the total".

   The **finalize tail is tracked separately, against `TimeoutStartSec`** (next
   section), not against the contention threshold. It is reported, not
   discarded: `finalize_lock_wait_ms` and `lock_wait_decision_tail_finalize` are
   on every pass summary and in the CLI print block. A `run_row` sample in the
   decision bucket is likewise not contention and does not count toward this
   precondition — but a *rising* `run_row` or `finalize` tail is a real finding
   about the host and must be reported, never quietly dropped.

   `lock_wait_distribution_eligible=false` rows and
   `lock_wait_run_row_orphaned()` rows are both excluded and tallied
   separately. Re-examine the `>=1000 ms` edge itself if any counted pass
   reports `write_hold_ms_max > 700` — **this numeric revisit trigger is
   unchanged.**
3. **A calibrated `initial_per_token_cost_seconds`, with adaptive batching
   enabled.** It has no default by design, and until a measured EVO value is
   set the write-hold SLO is *recorded but not enforced* — a fixed token count
   is not a safety invariant. A recurring timer without adaptive batching is a
   fixed-batch writer on a shared host.

   This stays an **enable-time** check, not a per-run pre-flight skip: a
   settings value cannot change between two runs six hours apart, and a per-run
   skip would silently no-op the flag for every caller that does not set it.
   But enable-time checks are human steps and human steps drift, so **every run
   records `adaptive_batching_active`** into the gate history
   (`crypto-reconciler-health` prints it per run). Observation only — nothing
   branches on it — but a drifted precondition is now visible instead of
   invisible.

   **The calibrated value is only valid at the batch size it was derived
   against.** Gate 3's `0.15` is derived and margined for a **5-token** first
   batch, which is what `min(8, 5)` produces on today's constants. **Raising
   `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` above 5 requires re-deriving the seed
   first** — see "HARD PRECONDITION for Gate 6" in the Gate 3 subsection below,
   and the no-partial-batch precondition any re-derivation must satisfy.
4. **A `TimeoutStartSec` that covers the derivation below**, or an explicit
   accepted decision to live with the documented SIGKILL outcome. "Below" means
   "`TimeoutStartSec` vs the finalize ladder" — the RECONCILER's derivation.
   GATE7-SPARSE-UNITS-001 inserted a second, unrelated `TimeoutStartSec`
   derivation between here and there, for the sparse observer; the two share a
   directive name and nothing else.

### Sparse observer timer (GATE7-SPARSE-UNITS-001)

A **different lane from the reconciler** and a different unit pair:
`probability-arena-crypto-sparse-observe.{service,timer}`. The reconciler is
provider-free and revisits rows already on disk; this lane **spends real
DexScreener requests** to buy one 6h and one 24h observation per enrolled
birth. Nothing about the reconciler's numbers transfers, and the two timers are
enabled, disabled and read independently.

**Nothing is installed on EVO-X2 by GATE7-SPARSE-UNITS-001.** The unit files
are repo templates; installation and activation are operator actions, in the
order below. **The disable procedure comes first on purpose** — an operator
reaching for this section is usually reaching for the brake.

#### Disable the sparse observer timer

Three levels, cheapest first. Each is complete on its own; you do not need the
next one.

```bash
# 1. STOP FUTURE PASSES (keeps the unit installed, keeps all state).
#    The pass becomes a clean no-op: no read, no write, no provider call, and
#    no telemetry record. Exits 0.
sed -i 's/^ENABLE_CRYPTO_SPARSE_OBSERVATION=.*/ENABLE_CRYPTO_SPARSE_OBSERVATION=false/' \
  ~/projects/probability-arena/.env
grep ENABLE_CRYPTO_SPARSE_OBSERVATION ~/projects/probability-arena/.env   # verify

# 2. STOP THE SCHEDULE ITSELF (also stops the dark no-op firing at all).
systemctl --user disable --now probability-arena-crypto-sparse-observe.timer
systemctl --user list-timers | grep crypto-sparse-observe   # expect: nothing

# 3. STOP A PASS THAT IS RUNNING RIGHT NOW.
systemctl --user stop probability-arena-crypto-sparse-observe.service
```

Level 3 is safe by construction and needs no cleanup. The pass writes in
batches through `_commit_with_retry(prepare=…)`, and every counter advances
only after a commit has **returned**:

* **Durable**: every enrolment batch and every observation batch that committed.
* **Rolled back**: the in-flight batch's staged rows — neither duplicated nor
  half-written, because `prepare()` re-stages from scratch on every attempt.
* **Not left inconsistent**: the overlap guard is a kernel-held `flock`, so it
  is released on process death and no stale lock survives; double-enrolment is
  impossible (`ix_horizon_member_cohort_token`) and double-observation is
  impossible (`ix_horizon_obs_cohort_token_horizon`), so the next pass re-plans
  from persisted state and picks up exactly what has no row yet. **There is no
  `running` row to orphan — this lane has no run table.**
* **Actually paid**: the provider requests already spent on the interrupted
  batch's tokens, plus any member-horizon whose band closes before the next
  pass, which becomes a permanent, never-backfilled `scheduling_miss`.

**A stop or a `TimeoutStartSec` overrun leaves no trace in this lane's own
telemetry.** `_emit_pass_telemetry` runs at the end of the pass, so a killed
pass appends nothing to the JSONL sink. Unlike the reconciler — whose overrun
signature is an orphaned `status='running'` row — the evidence here is
`journalctl` (`Failed with result 'timeout'`) and, one band later, a rising
`scheduling_miss_rate`. If you suspect an overrun, read the journal; the corpus
will not tell you.

There is **no automatic disable** for this lane. The reconciler's rolling
health latch is its own and does not gate this timer.

#### Enable the sparse observer timer

Do these in order; each step's check must pass before the next. Steps 1–4 are
the CRYPTO-COVERAGE-REPAIR-002 deployment plan and are **not optional** — in
particular step 3, which is the only place `MAX_DURATION_SECONDS` gets a value
measured on this host.

1. **Apply migration 0029.** The command deliberately does not run Alembic, so
   an un-migrated database puts the planner on the slow, un-indexed path
   (measured 416 ms cold against 25 ms):
   ```bash
   cd ~/projects/probability-arena && .venv/bin/alembic upgrade head
   ```
2. **Dark check and dry run** (flag still false):
   ```bash
   .venv/bin/python -m app.cli crypto-sparse-observe
   # expect: status=disabled  external_calls=0  no-op
   .venv/bin/python -m app.cli crypto-sparse-observe --dry-run
   # read: births_considered, enrolment_rejections, would_enrol,
   #       due_observations, working_set_index_present=True
   ```
3. **One attended pass, then set the fetch budget from it.**
   ```bash
   .venv/bin/python -m app.cli crypto-sparse-observe --force --observe-limit 5
   ```
   Read `duration_ms` and `write_lock`. Set
   `CRYPTO_SPARSE_OBSERVATION_MAX_DURATION_SECONDS` from the measured
   per-request latency rather than from the chosen 90.0. **Changing it
   invalidates `TimeoutStartSec`** — that value is derived from the 90 s
   default (see "`TimeoutStartSec` for the sparse observer" below), and
   `tests/test_gate7_sparse_units_001.py` fails until the unit is re-derived.
   Do that re-derivation before step 5, not after.
4. **Read the coverage report** and confirm the denominator matches what step 2
   predicted and that `scheduling_miss` is 0:
   ```bash
   .venv/bin/python -m app.cli crypto-observation-coverage-report
   ```
   Note the line it prints: `expected timer line: OnCalendar=…`. **Copy that
   string into the unit rather than typing a cadence.** Nothing in the code can
   see a systemd `OnCalendar`, and this is the weakest joint in the lane.
5. **Install the units dark** (flag still false):
   ```bash
   cp infra/systemd/user/probability-arena-crypto-sparse-observe.{service,timer} \
      ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now probability-arena-crypto-sparse-observe.timer
   systemctl --user list-timers | grep crypto-sparse-observe   # next tick, <= 1h
   journalctl --user -u probability-arena-crypto-sparse-observe.service -n 20 --no-pager
   # expect: status=disabled  external_calls=0  no-op
   ```
   Confirm the installed `OnCalendar` is the string step 4 printed:
   ```bash
   grep OnCalendar ~/.config/systemd/user/probability-arena-crypto-sparse-observe.timer
   ```
6. **Flip the flag** and let exactly one natural tick run:
   ```bash
   sed -i 's/^ENABLE_CRYPTO_SPARSE_OBSERVATION=.*/ENABLE_CRYPTO_SPARSE_OBSERVATION=true/' \
     ~/projects/probability-arena/.env
   ```
7. **Read the first unattended pass** before trusting the second:
   ```bash
   journalctl --user -u probability-arena-crypto-sparse-observe.service -n 60 --no-pager
   .venv/bin/python -m app.cli crypto-observation-coverage-report
   ```
   Expected on a healthy pass: `status=ok`, `solana_tracker_calls=0`,
   `gate_bypassed=None`, `stop_reason=complete`,
   `working_set_index_present=True`, and a `write_lock` block with
   `lock_failures=0` and `persisted=True`. **`gate_bypassed=True` on a
   scheduled run means `--force` reached the `ExecStart` — remove it.**
   `status=partial` with `stop_reason=deadline` or `observe_limit` is the pass
   telling you work remains, not a fault.

#### Expected timer inventory after install

So a later audit has a list to diff against rather than a memory. Run:

```bash
systemctl --user list-timers --all --no-pager | grep probability
```

| Unit | Expected after this install | Source of that expectation |
|---|---|---|
| `probability-arena-baseline.timer` | present, every 4 h | "Deployed services" table at the top of this file |
| `probability-arena-retention.timer` | present, daily | same |
| `probability-arena-backup.timer` | present, daily 01:30 UTC | SQLITE-BACKUP-COORDINATION-001, recurrence proven |
| `probability-arena-marketops.timer` | present, 5 min | referenced throughout as the live 5-minute writer |
| `probability-arena-tick-aggregation.timer` | present, gated by `ENABLE_TICK_AGGREGATION_TIMER` | OPS-013 hardening section |
| `probability-arena-meme-news.timer` | **absent** unless its own section was followed | MEME-NEWS-002 is "NOT auto-installed" |
| `probability-arena-crypto-reconcile.timer` | **absent** unless "Enable the guarded reconciler timer" was followed | that section |
| `probability-arena-crypto-sparse-observe.timer` | **present, hourly** — the only unit this procedure adds | this section |

**The audit-proof statement is the delta, not the table**: this procedure adds
**exactly one** timer and **exactly one** oneshot service, and changes nothing
else. The table above is assembled from this runbook's own sections rather than
from a live query, so treat a mismatch as a question to answer, not as a
failure — but a unit present that is on **neither** list is a finding.

`probability-arena-watcher.service` is a continuous service, not a timer, and
does not appear in `list-timers`.

#### `TimeoutStartSec` for the sparse observer

The full derivation, its inputs, and the exceedance conditions live **in the
unit file** — `infra/systemd/user/probability-arena-crypto-sparse-observe.service`
— because that is where the next person to change the number will be. In brief:
`5min` covers the measured-regime model (~157 s at the worst load factor this
repo has measured, L = 5.80) with a ~1.9× margin, and that margin is spent
entirely on the per-lock-acquisition wait, which the measurement does not
bound. **No value could cover the arithmetic ceiling** — at L = 5.80 with every
lock acquisition waiting the full 30 s busy timeout it is ~124 minutes, longer
than the lane's own cadence — so the number is chosen against measurement and
the exceedance conditions are written down instead of a guarantee.

### Where the per-pass record now lands (GATE2-WRITER-TELEMETRY-001)

Precondition 3 needs a **distribution**, and until this milestone a pass left
nothing behind but journald stdout once the unit's log rotated. Every governed
pass now appends **one JSONL line** to the SQLITE-LOCK-TELEMETRY-001A sink —
`~/probability-arena-telemetry/sqlite-writes.jsonl`, overridable with
`SQLITE_TELEMETRY_DIR`.

**Not a database table, on purpose.** The sink is non-SQLite by mandate, so it
cannot take the write lock it is measuring, and it survives a pass that
committed nothing — which the run row's `config["write_coordination"]` does
not, because that rides the single-attempt finalize commit. The contended
passes are exactly the ones whose numbers matter and exactly the ones the run
row loses. **This milestone adds no migration**: nothing to apply, nothing to
break if it is not applied, and the Alembic head stays `0028`.

**There is no report command.** The collector/rotation step is owned by 001E
and is still unbuilt, so reading this file is `jq` over it — see the growth and
reading warning below before you read it on EVO. What to read, and the traps:

* `batch_lock_wait_ms_max` is **the only** figure the breakers act on. The
  pass total (`lock_wait_ms`) carries one `run_row` and one `finalize` sample
  per pass that are once-per-pass bookkeeping, not contention — a threshold
  read off the total measures the instrument. `run_row_lock_wait_ms` and
  `finalize_lock_wait_ms` are recorded separately and must not be summed into
  the batch figure.
* **Derive a threshold from `lock_wait_histogram_ms`, not from a scalar.** The
  histogram is now persisted per pass for exactly this reason
  (`LockWaitAccounting`'s own docstring: the per-attempt measurement bias lands
  almost entirely in the `1-10` bucket, so the tail survives the bias and the
  scalars do not).
* `write_hold_ms_max` against the 2.0 s SLO, alongside
  `write_hold_slo_violations` (the SLO counter itself) and
  `adaptive_initial_per_token_cost_ms` — a recorded hold distribution means
  nothing without the seed that produced it.
* `run_source` is derived from systemd's own `INVOCATION_ID`, not from the
  command line, so an attended pass cannot be filed as a timer run by copying
  the unit's `ExecStart`. `gate_bypassed` (`--force`) is a **separate** field:
  filter unattended passes on `run_source="scheduled"`, never on the absence
  of `--force`. **`run_source` is nonetheless forgeable and is not evidence**:
  a caller may pass it directly to `emit_writer_pass`, and `export
  INVOCATION_ID=anything` satisfies the derivation. Nothing consumes this file
  today and the health latch does not read it, so nothing is at risk — but do
  not build an enforcement decision on the field without adding a check first.
* `run_status="disabled"` is deliberately never emitted (no run happened).
  Every other outcome, including every pre-flight skip and refusal, is.
* **The file now carries a second writer (GATE7-SPARSE-TELEMETRY-001).** The
  sparse observer appends under `writer_name="crypto_horizon_observe"`,
  `operation_name="scheduled_sparse_observation"` — **0.8–0.9 KB per record**
  (measured on the dev Mac: 825 B for an ordinary completed pass, 882 B for the
  fullest record the lane can build). Treat it as ~0.9 KB, **not as a
  constant**: it moves with which optional fields the pass produced and with
  the length of `host`, `systemd_unit` and `source_command`, so a single-byte
  figure will not reproduce on EVO. The 4,096 B line cap is far above both, so
  nothing sheds fields. It is default-off with no timer, so nothing arrives
  on EVO today. **This is why the calibration filter below is scoped to one
  `writer_name`, and it must stay scoped**: the two writers have different
  phase vocabularies, and averaging
  them together produces a per-token cost that belongs to neither. Read writer
  B's records with the same predicate and its own name, e.g.
  `jq -c 'select(.writer_name=="crypto_horizon_observe")' …`. Four differences
  to expect: it emits **no** `lock_wait_ms`, `batch_lock_wait_ms_max`,
  `run_row_lock_wait_ms` or `finalize_lock_wait_ms` — it has one write phase,
  no run row, no finalize commit, and it times no lock wait anywhere, so those
  fields are ABSENT rather than zero; its contention signal is `lock_failures`
  plus `retry_count`. `commit_ms` on its records is its per-pass **maximum**
  batch commit (`batch_count` is the denominator), not one transaction's value.
  **`commit_ms` must be filtered by `writer_name` before ANY cross-writer
  aggregation** — the reduction differs per writer (`tick_aggregation` records
  its LAST sub-window commit) and both are stamped `commit_quality="exact"`,
  because that tier says how a sample was measured, not how samples were
  reduced. That is a hard rule, not a preference; it is stated in full in
  `docs/SQLITE_LOCK_TELEMETRY_001A.md` and in `app/telemetry/schema.py`'s
  docstring. `rows_committed` is
  `enrolled + observations_recorded + ticks_written`. `write_hold_measured`
  works exactly as it does for writer A, and `write_hold_ms_max`/`commit_ms`
  are both omitted when it is `false`.
* **What the append costs, measured — and the number NOT to use.** One emit is
  a single `os.write()` of that ~0.9 KB, outside every transaction the lane
  owns. Two independent runs on the dev Mac, 400 emits per arm, with a second
  connection holding SQLite's RESERVED write lock on a real file-backed
  database for the contended arm:

  | run | p50 idle | p50 under a held RESERVED lock | delta |
  |---|---|---|---|
  | review of GATE7-SPARSE-TELEMETRY-001 | 0.096 ms | 0.130 ms | +34 µs |
  | follow-up round, load ~12 | 0.131 ms | 0.128 ms | −2.5 µs |

  Both say the same thing, and it is the only cost claim to rely on: **the
  append does not see the write lock, because it never touches SQLite.** The
  absolute p50 tracks host load; the delta is tens of microseconds in either
  direction, i.e. nothing. **Do not re-derive this from a whole-PASS
  before/after delta.** A ~1,500 ms pass cannot resolve a 0.1 ms append: an
  earlier round of this branch reported a "+2.02 ms median whole-pass delta"
  that was interleaving noise at load ~25, and the same experiment returned a
  LOWER p90 with telemetry ON.

#### Deriving `initial_per_token_cost_seconds` — the mandatory filter

`write_hold_ms_max=0` **does not mean the hold was zero.** A contended pass, a
refused pass and a pre-flight skip all record no hold at all, and
`int(hold_seconds * 1000)` truncates a genuine 0.4 ms hold to 0 as well.
Averaging the raw field mixes passes that measured *nothing* into the mean,
pulls it down, and yields a constant that is **too aggressive** — larger
batches and longer holds on a live production writer. That is the same
survivorship bias that got `crypto_token_lifecycle_runs` rejected, arriving as
zero-inflation instead of absence.

Two fields resolve it, and both must be used:

* `write_hold_measured` (bool) — `false` means no write transaction was opened.
  `write_hold_ms_max` is **omitted entirely** from such a record.
* `write_hold_ms_max = 0` **with** `write_hold_measured = true` is a real,
  sub-millisecond hold. Keep it.

**Filter every calibration query on all of:**

```
run_status ∈ {ok, partial, truncated, backlog_expiring}
AND batch_count > 0
AND write_hold_measured == true
```

```sh
jq -c 'select(.writer_name=="crypto_tape"
       and (.run_status|IN("ok","partial","truncated","backlog_expiring"))
       and (.batch_count // 0) > 0
       and .write_hold_measured == true)
       | {write_hold_ms_max, batch_count, rows_committed, rows_attempted,
          adaptive_initial_per_token_cost_ms}' \
  ~/probability-arena-telemetry/sqlite-writes.jsonl
```

**`rows_committed` is not a token count.** It is
`snapshots_created + outcomes_updated + birth_events_created` — rows summed
across three tables, and one token can contribute to all three. Gate 3 asks for
"per-batch write-hold and committed-**token** count", so a per-token cost
derived as `write_hold_ms_max / rows_committed` uses a denominator that is
**≥** the token count and therefore **under-estimates** per-token cost; once
inverted into a batch size that errs toward *smaller* batches, which is the
safe direction. If you want a token-grain denominator instead, use
`rows_attempted` (`tokens_considered`), and note the reverse caveat: it counts
tokens the pass looked at, so on a truncated pass it is an over-estimate.
Whichever you pick, `write_hold_ms_max` is a **max** over batches while both
denominators are **sums** over the pass — state which pairing you used when you
record the constant.

#### Gate 3 — the calibration session and the chosen constant

**CHOSEN: `CRYPTO_TAPE_RECONCILER_INITIAL_PER_TOKEN_COST_SECONDS = 0.15`.**

It is written into `.env.example` **commented out**, exactly as it was before,
and is set **nowhere that takes effect**. Activation is Gate 6, and it is a
separate, deliberate decision. Nothing in this subsection turns anything on.

```text
CALIBRATION-GATE3-001 — Gate 3 derivation (EVO-X2, ef92b4d)
  samples (n=8)  per-token ms = write_hold_ms_max / batch_size, batch_size=5
  sorted         14.8  17.8  18.0  18.8  18.8  19.6  19.8  105.4
  median         18.8 ms/token
  warm max       19.8 ms/token   (the 7-sample warm cluster)
  cold start     105.4 ms/token  (n=1, the first write after a fresh checkout)
  margin         0.15 = 0.1054 x 1.423
  CHOSEN         initial_per_token_cost_seconds = 0.15

  AS SHIPPED, end to end — what the code does with 0.15
  bias 1.5                             -> conservative estimate 0.225 s/token
  next_adaptive_batch_size(2.0, 0.15)  -> 8 tokens    (NOT 2.0/0.15 = 13)
      at cold 105.4 ms/token -> 0.843 s   (42.2% of the 2.0 s SLO)
  B11 ceiling CRYPTO_TAPE_RECONCILER_BATCH_SIZE = 5
  min(8, 5)                            -> 5 tokens    <- THE FIRST BATCH,
      at cold 105.4 ms/token -> 0.527 s   (26.3% of SLO)   identical to today
      at warm  19.8 ms/token -> 0.099 s
```

**Read the second half of that block before acting on the first.** `2.0 / 0.15
≈ 13` is budget arithmetic and is not a batch size the code ever issues. On
today's constants the calibrated first batch is **5 tokens — exactly what ships
today**, so setting this constant changes reconciler behaviour not at all.

##### The session

Eight **attended `--force`** passes on EVO-X2 at branch `CALIBRATION-GATE3-001`
(`ef92b4d`), with `ENABLE_CRYPTO_TAPE_RECONCILER` **off** throughout — so every
pass recorded `gate_bypassed=force` and no timer existed. Host load 0.3–0.6,
with the ordinary co-tenants running: the crypto watcher, MarketOps on its
5-minute cycle, meme-news on its 5-minute cycle, and hourly tick-aggregation.

All eight passed the mandatory filter above — `writer_name == "crypto_tape"`,
the four-status set, `batch_count > 0`, `write_hold_measured == true` — with
**zero passes filtered out**, and every one of the eight recorded
`write_hold_slo_violations = 0`.

| # | status | batches | hold_ms_max | batch_lock_wait_ms_max | rows_committed |
|---|--------|---------|-------------|------------------------|----------------|
| 1 | partial | 350 | 527 | 516 | 3500 |
| 2 | partial | 391 | 99 | 938 | 3910 |
| 3 | partial | 394 | 94 | 514 | 3940 |
| 4 | partial | 181 | 98 | 4038 | 1810 |
| 5 | ok | 135 | 94 | 1015 | 1350 |
| 6–8 | ok | — | — | — | — |

Pass 1 is the cold start (first write after a fresh checkout). Pass 4 is the
deadline overshoot. Passes 6–8 each returned `ok` on an identical
`tokens_considered = 590` in ~11 s.

##### Which pairing was used — the section above requires this be stated

`per-token cost = write_hold_ms_max / batch_size`, with **`batch_size = 5`
tokens**. This is neither of the two pass-level denominators the section above
warns about: it is the **per-batch token count**, and `write_hold_ms_max` is a
max **over batches**, so numerator and denominator are at the same grain. The
pairing was verified on every pass — `batches_committed x 5 == tokens_considered`
held throughout.

> **HARD PRECONDITION for any future re-calibration: no batch may be partial.**
> Check `batches_committed x batch_size == tokens_considered` on **every** pass
> in the sample, and record the result. This is not a note about the Gate 3
> session — it is the difference between a valid and an invalid derivation.
>
> The numerator is a **max over batches**; the denominator is **fixed**. If any
> batch was short, the worst batch's true per-token cost is
> `write_hold_ms_max / (tokens in THAT batch)`, which is *larger* than the
> arithmetic reports. **The failure direction is an UNDER-estimate of per-token
> cost** — a seed too small, a first batch too large, and a write-lock hold
> longer than the SLO was checked against. That is the unsafe direction, and
> nothing else on such a pass looks wrong.
>
> A short batch is ordinary: any pass whose `tokens_considered` is not an exact
> multiple of `batch_size` has one, and a **deadline-stopped** session is the
> standard way to produce one. If the identity does not hold on every pass, the
> session has not derived a constant — re-run it, do not adjust the arithmetic.

**`rows_committed` was NOT used, and the table above shows why.** 3,500 rows
over 350 batches is 10 rows per 5-token batch: it is
`snapshots_created + outcomes_updated + birth_events_created` summed across
three tables, roughly 2x the token count here. Dividing by it would have
under-estimated per-token cost by about half.

##### Why the single cold observation governs

The seed is consumed **once, at process start — the coldest moment in the
pass.** That is the whole reason the cold sample cannot be discarded as an
outlier: it is the only observation taken under the conditions the seed is
actually used in, and it is a *recurring* condition, not a freak one — every
deploy and every reboot produces it.

A controller seeded from the warm cluster demonstrates the failure directly.
`2.0 / 0.0198 ≈ 101` tokens in the first batch; at the observed cold cost of
105.4 ms/token that batch holds the write lock for **10.6 s — 5x the 2.0 s
`RECONCILE_WRITE_TIME_SLO_SECONDS`** — before a single measurement has been fed
back into the estimator. The median (18.8) is worse still. So the seed is set
from the cold tail, not from the centre of the distribution.

`0.15 = worst observed (0.1054) x 1.423`. The seed is then biased high by 1.5
to 0.225 s/token, `next_adaptive_batch_size(2.0, 0.15)` returns **8** tokens
(**0.843 s at the cold cost, 42.2% of the SLO**), and the B11 ceiling clamps
that to `min(8, 5) = 5` — **0.527 s at the cold cost (26.3% of the SLO)**,
0.099 s at the warm cost, with the controller free to grow from there on real
measurements.

##### On today's constants the effect is NO CHANGE; the loosening is latent

**The as-shipped first batch is 5 tokens — exactly what ships today.** On
today's other constants, setting this seed changes reconciler behaviour not at
all. Two readings are wrong in opposite directions:

* **It is not a tightening.** Neither intermediate figure is below current
  behaviour — 13 tokens on the budget arithmetic, 8 after the bias, both
  **larger** than the fixed `CRYPTO_TAPE_RECONCILER_BATCH_SIZE = 5`. Nothing
  here constrains the first batch, so there is no throughput to "recover"
  elsewhere.
* **It is not a loosening either — not yet.** The loosening is *latent*: it is
  realised only if Gate 6 also raises the B11 ceiling above 5, at which point
  the first batch becomes 8 tokens (0.843 s, 42.2% of the SLO at the cold
  cost). That is a separate decision, with its own evidence and its own hard
  precondition (below), deliberately not taken here.

##### The two clamps, in the order the code applies them

Neither was changed here; both are named because the end-to-end figure above is
unreadable without them.

* **`AdaptiveBatchCostEstimate` biases the seed HIGH** by
  `bias_multiplier = 1.5` before it is ever used to size a batch. The
  conservative estimate from a 0.15 seed is 0.225 s/token, so
  `next_adaptive_batch_size(2.0, ...)` returns **8** — a hold of **0.843 s at
  the cold cost (42.2% of the SLO)**. The same bias applies to the warm-seed
  counterfactual: a conservative 0.0297 s/token, **67 tokens, 7.06 s, still
  3.5x the SLO**. The bias does not rescue a warm seed.
* **The B11 sanity ceiling is `batch_size`**, i.e. the shipped
  `CRYPTO_TAPE_RECONCILER_BATCH_SIZE = 5`. Once adaptive batching is active
  `batch_size` becomes a maximum only, and `min(8, 5) = 5` — the shipped first
  batch, and the reason the effect above is no change at all.

##### HARD PRECONDITION for Gate 6 — raising the B11 ceiling re-opens this derivation

**The calibration risk did not resolve; it transferred.** `0.15` is inert today
only because `min(8, 5) = 5`. Every margin recorded in this subsection is a
statement about a **5-token** batch and about nothing else.

> **Precondition (Gate 6), alongside preconditions 1-4 above.** Raising
> `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` above **5** requires
> `initial_per_token_cost_seconds` to be **re-derived first**. The ceiling is
> not an independent throughput knob, and a re-derivation is itself subject to
> the no-partial-batch precondition in "Which pairing was used" above.

It is a precondition and not a note because the cold tail is bounded by
**n = 1**: the only thing between an unmeasured worse cold start and an SLO
breach is the margin, and the margin suffices only at 5 tokens. At a ceiling of
8 the cold-cost hold is already 42.2% of the SLO on the single cold observation
that exists — a cold start twice as slow as the one measured puts 8 tokens at
**84%** and 13 tokens **past** the SLO.

##### Caveats — these are part of the derivation, not footnotes

* **n = 1 for the cold case.** The cold tail is **not bounded by this data**. A
  worse cold start is entirely possible; the 1.42x margin exists for exactly
  that reason and is not a proof of anything. A second cold observation is the
  cheapest thing that would improve this constant.
* **The margin is a judgement, not a measurement.** It is exactly
  `0.15 / 0.1054 = 1.423x` over the single worst observation, and no
  distributional claim is made about it. **State what it buys against the
  as-shipped batch, not the budget arithmetic:** at the shipped 5-token first
  batch the cold-case hold is **26.3% of the SLO**; at an 8-token batch (the
  B11 ceiling raised) **42.2%**. An earlier form of this caveat anchored on
  "under 70% of the SLO" from the `2.0/0.15 ≈ 13`-token figure — a batch size
  the code never issues. That anchor is superseded by the block above.
* **Pass 4 overshot its 30 s deadline by 37%** (`duration_ms = 41035`) on a
  4,038 ms `batch_lock_wait_ms_max`. This is the documented limit that
  `max_duration_seconds` **cannot interrupt a statement already blocked inside
  SQLite** — the deadline is only evaluated between batches. It is **not a
  calibration input** (the hold was a normal 98 ms; the wait was the lock, not
  the write) but it belongs in this record, and it is the same class of event
  precondition 1 asks to be *observed* rather than modelled.
* **Passes 6–8 returned `ok` on an identical `tokens_considered = 590`.** The
  historical backlog has drained to steady state, so the warm cluster is a
  steady-state measurement. A backlog surge is a different population and this
  constant was not measured against one.
* **Eight passes, one host, one session, one branch.** The constant is not
  portable to a different host, and re-deriving it is the required step if the
  host changes — the same rule `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` already
  carries.

#### Growth, rotation and reading this file

**The file does not rotate, has no size bound, no retention and no alert**, and
it is **shared** with the SQLITE-LOCK-TELEMETRY-001A writers rather than being
this milestone's alone. Rotation is owned by 001E, which is unbuilt.

Gate 2's own contribution is genuinely small — measured **1,168 B per pass,
~4.6 KB/day** at the 6-hourly cadence (it was 906 B before the SLO counter,
the two denominators and the histogram were added) — but growth is monotonic
and the 001A writers add their own. **Check the file's size before you read
it**:

```sh
ls -lh ~/probability-arena-telemetry/sqlite-writes.jsonl
```

Do **not** use `app.telemetry.sink.read_events` on EVO. It calls
`path.read_bytes()` on the whole file and holds every parsed event: measured
**420 MB peak Python heap on a 60 MB file** (7.0x amplification, 8.5 s) in the
GATE2 session, and **453 MB on a 60.8 MB file** (7.2x) in the Gate 4 paired
measurement below. It is a test helper. Stream with `jq` instead, as above.

**`_lock_tally` reads the same way, and unlike `read_events` it is a consumer
that actually runs on EVO.** `scripts/sqlite_analyze_maintenance.py::
_lock_tally` does `path.read_text()` on the whole file. **Its own measured cost
is 204 MB peak heap on a 60.8 MB file — 3.4x amplification**, from the Gate 4
review, which ran both whole-file readers against that one file. It is about
half `read_events` because it discards each line after counting it instead of
holding every parsed event. **The 406 MB / 58 MB figure previously recorded
here for `_lock_tally` was `read_events`' cost, not the tally's** — the
script's own docstring attributed the same number to `read_events`, and the two
artifacts disagreed about whose it was; the paired measurement replaces it in
both. Cheaper is not cheap: 3.4x of a file with no rotation is still a second
and independent reason to check the size above, and `read_events` is a test
helper you can decline to run while this one sits on the ANALYZE maintenance
path. **Not fixed** — recorded so the size check is understood as protecting a
real consumer.

**`lock_events` is cumulative and monotonic.** `_lock_tally` counts every line
in the file with no time window, and nothing rotates the file before 001E, so
the tally only ever grows. The two consumers differ in exactly this: the
ANALYZE record takes a **bounded before/after delta** (`delta.lock_events`),
while the Kalshi DEMO session's `current_lock_events > 6` stop condition in
`docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md` reads the **cumulative**
number — so once it crosses, it stays crossed for the life of the file. Only
the delta is bounded. Owned by 001E along with rotation; recorded here so it is
not rediscovered mid-session.

**The `writer_name` scoping did NOT fix this, and was not meant to.** It fixed
*which* population is counted, not *over what interval*: `lock_events` is
cumulative and monotonic **for the in-scope writers too**. Measured: **90 days
of hourly `tick_aggregation` at a benign 0.5% genuine-contention rate produces
11 in-scope events — past the `> 6` stop condition** with nothing whatever
wrong. That is not a cosmetic ageing problem, because `> 6` governs exactly the
attended-session stop conditions the `initial_per_token_cost_seconds`
calibration depends on. **NAMED FOLLOW-UP, not built here:** give `_lock_tally`
a `--since` argument, or report a `lock_events_last_24h` figure beside the
cumulative count. Owned by 001E with rotation.

Measured cost of the append itself, against a real competing SQLite writer on
this repo's dev Mac: **p50 0.10 ms, p95 0.32 ms** while a co-tenant *held* the
RESERVED lock — statistically identical to the idle-host figure, which is the
structural point (it takes no lock).

**The sink is not crash-durable.** It survives a pass that *returned* having
committed nothing — contended, aborted, refused, skipped — which is the whole
point, because those are the passes the run row loses. It does **not** survive a
SIGKILL: the append is the last thing a pass does, so a kill mid-finalize loses
the JSONL line exactly as it loses the run row.

#### BEFORE YOU ENABLE THE RECONCILER: this file already has a consumer

`scripts/sqlite_analyze_maintenance.py::_lock_tally` reads this same JSONL file
and reports `lock_events`, counting **any** event with `lock_wait_ms > 0` or
`retry_count > 0`. That predicate was written when only the two 001A writers
(`tick_aggregation`, `backup`) appended here, and for them a non-zero lock wait
genuinely is a lock event. It is a governed number: the ANALYZE record carries
it as a before/after **delta**, and `current_lock_events > 6` is a documented
session stop condition.

**A healthy `crypto_tape` reconciler pass routinely reports `lock_wait_ms > 0`**
— measured 12 ms on an uncontended 12-token pass, from once-per-pass `run_row`
and `finalize` bookkeeping samples that are instrument, not contention. So the
moment `enable_crypto_tape_reconciler` is turned on with a timer, `lock_events`
starts climbing by roughly one per pass (~4/day at the 6-hourly cadence) and
will cross 6 within two days — reading as new contention when nothing has
contended.

**RESOLVED — the count is now scoped by `writer_name`.** `lock_events` counts
only `tick_aggregation` and `backup` (`LOCK_EVENT_WRITERS` in
`scripts/sqlite_analyze_maintenance.py`), which is the population the `> 6`
constant was read off. **The threshold was NOT raised.** Raising a limit to
accommodate a miscount destroys the safeguard; the defect was never the limit,
it was the population being counted under it.

**Why scoping and not `batch_lock_wait_ms_max`.** Moving the count onto the
phase-attributed field was the other candidate fix, and on the measured numbers
it does not work as stated: the same eight healthy, uncontended passes measure
`batch_lock_wait_ms_max` of 3/3/2/1/1/1/6/2 ms — **the
GATE2-WRITER-TELEMETRY-001 session** — so `> 0` on that field counts them
exactly as the flat predicate did.

**Name the session whenever you quote an eight-value series.** Three are in
circulation and they are different populations, not restatements of one:
`3/3/2/1/1/1/6/2` is GATE2-WRITER-TELEMETRY-001's uncontended passes and is the
series this argument stands on; the CALIBRATION-GATE3-001 per-pass table
(EVO-X2, `ef92b4d`, Gate 3 subsection above) records passes that *did* wait —
516/938/514/**4038**/1015/... — and would not support this argument; and a
`6/5/1/1/2/1/2/2` series quoted in review is **not recorded anywhere in this
repository** and should be treated as unsourced until a session is attached to
it. The scoping conclusion survives all three. The provenance does not, which
is why the label travels with the numbers. Making it work would need a millisecond
threshold, and the only one available is the reconciler's own breaker constant
— importing that into the count that governs the reconciler would let guarded
code widen its own boundary, the same inversion the histogram validation was
deliberately kept structural to avoid. Scoping needs no constant at all.

**The reconciler is scoped out of the governed count, not hidden.** The tally
also reports `lock_event_scope`, `out_of_scope_events` and
`out_of_scope_flat_predicate_hits`, so a `crypto_tape` pass that genuinely
contends is still visible in the ANALYZE record — it simply cannot move a
number calibrated on a different population.

**THE DEADLINE WAS THE CALIBRATION SESSION, NOT THE TIMER.** An earlier
statement of this hazard said it had to be resolved "before the flag is
flipped". That was late by one step: the session that derives
`initial_per_token_cost_seconds` is itself about eight attended `--force`
passes, and under the old predicate those alone crossed `> 6` before any timer
existed. The scoping had to land first, and did.

### `TimeoutStartSec` vs the finalize ladder

The run row's finalize inherits the **connection's** busy timeout (30 s in
production), not the batch loop's tight derived budget — that is deliberate
(it is what persists `write_coordination`, i.e. the gate's evidence, on
contended passes). Against a holder that never releases, a *3-attempt* finalize
ladder would cost `3 x 30 s x overshoot + 2 x 3 s`:

| overshoot | source | finalize ladder |
|-----------|--------|-----------------|
| 1.01x | EVO, idle, measured | ~97 s |
| 2.0x  | the shipped constant | ~186 s |
| 5.80x | dev Mac at load 5-6, measured | **~528 s** |

**The finalize's measured wait now has its own number**, per pass:
`finalize_lock_wait_ms` (with `lock_wait_decision_tail_finalize` and
`lock_wait_phases["finalize"]`). It is separated from the contention signal
precisely so it can be checked *here* — against this budget and against
`TimeoutStartSec` — instead of being subtracted out and lost. It is on the pass
summary and the CLI output only; the run row cannot carry it (see the phase
subsection).

A real blocked pass has been measured at `lock_wait_ms=206284` (~206 s). **The
finalize therefore runs a single attempt**
(`RECONCILE_FINALIZE_MAX_LOCK_ATTEMPTS = 1`): it is bookkeeping, a second
attempt 3 s later against the same 20-45 s holder rarely helps, and the
accounting still reaches the operator through the returned summary even when
the commit is lost. Whole-unit derivation with that change, as a function of
the load-dependent overshoot L (`26 s + 60L`):

```
20 s (deadline) + 66 s (one in-flight batch ladder, 3 x 4 x 5 s + 2 x 3 s)
                + 60 s (finalize, 1 x 2.0 x 30 s)          = ~146 s   at L=2.0
                                                              ~87 s   at L=1.01
                                                             ~374 s   at L=5.80
```

**`TimeoutStartSec` was raised from 5min to 7min (420 s) by
CRYPTO-RECONCILER-GUARDED-TIMER-001**, because the ~374 s ceiling at the
measured 5.80x exceeded the old 300 s value and the timer is no longer
attended-only.

**The load factor at which 420 s is exceeded is `L = 6.57`** — written down
here so nobody re-derives it: `26 + 60L > 420` ⟺ `L > 394/60 = 6.566…`, and at
`L = 6.6` the ceiling is **422 s**, 2 s past the chosen value. The gap between
the worst measured L (5.80) and the crossing L (6.57) is ~13% of *load*
headroom, not a safety margin.

**The signature of an overrun is the orphaned `running`-row tally.** A SIGKILL
mid-finalize is the only thing that leaves a run row at `status='running'` with
no `config.write_coordination`, so a rising `lock_wait_run_row_orphaned()`
count *is* the host reporting `L > 6.57`. That is how an exceedance gets
noticed rather than assumed — check it whenever the unit shows a timeout.

420 s covers the worst overshoot ever measured in this repo,
with ~12% headroom — **not a guarantee**: L tracks host load and is unbounded
above, so no constant fixes it, which is precisely why precondition 1 is
*observed* non-exceedance and why `wall_time_model_exceeded=true` is one of the
post-run review triggers feeding the auto-disable gate. **The failure mode is
non-corrupting**: a SIGKILL mid-finalize leaves committed batches durable and
the run row at `status='running'` — the same orphaned row a lost finalize
produces, plus a failed unit. Nothing reconciles those rows, so the
counted-passes analysis excludes and tallies them via
`lock_wait_run_row_orphaned()` (see the subsection above). If a future
measurement makes that outcome
frequent, the fix is to re-derive `TimeoutStartSec` against
`3 x busy_timeout x overshoot` on the *observed* host overshoot — a unit-file
change, and therefore a deliberate deployment step recorded here, never a
silent edit.

### Operator knob

`CRYPTO_TAPE_RECONCILER_LOCK_WAIT_BUDGET_SECONDS` (unset by default) is a
tighter **cap**, never a floor. Set it from the histogram above once a real
distribution exists; never guess it. CLI override:
`--lock-wait-budget-seconds`.
