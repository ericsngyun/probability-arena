# SQLITE-BACKUP-FRESHNESS-ALERT-001 — local manifest-backed backup health

**Status:** implemented and reviewed; deployment recorded in §10 below.
**Predecessor:** [`SQLITE_BACKUP_COORDINATION_001.md`](SQLITE_BACKUP_COORDINATION_001.md)
(verified, coordinated, bounded SQLite backups — the pipeline this milestone monitors).

Backups shipped and proved recurrence. This milestone answers the question that
a working backup pipeline does *not* answer on its own:

> Does the canonical backup root **still** contain a recent, committed,
> structurally valid, manifest-backed backup artifact produced within the last
> 36 hours?

A backup system fails silently. The timer can be masked, the unit can start
failing, the volume can fill, an artifact can be truncated or removed — and
nothing in the running system notices until the day someone needs to restore.
This adds a local, measurement-only health check to the MarketOps operational
alert path so that degradation is loud.

It is **not** a second backup implementation, a retention change, a scheduler,
or a remediation path. It never runs a backup, never prunes, never modifies a
backup file or manifest, makes zero provider calls, adds no timer or daemon,
and cannot fail a MarketOps cycle.

---

## 1. Backup recurrence baseline (what this monitors)

Measured on EVO-X2 before this milestone was built:

| Fact | Value |
|---|---|
| Backup root (`BACKUP_DIR`) | `/mnt/data/probability-arena-backups` (mode 700, `miko_node_001`, not a symlink) |
| Timer | `probability-arena-backup.timer`, enabled, active/waiting, `Persistent=true` |
| Measured production backup | `2026-08-03T00:02:15Z`, verified, `online_copy_restarts=0` |
| First natural scheduled backup | `2026-08-03T01:34:25Z`, verified, `online_copy_restarts=0` |
| Latest artifact | `backup-20260803T013426Z.db.gz`, 454,984,541 B (~433.9 MiB) |
| Latest manifest | `integrity_check=ok`, 53 tables, Alembic `0027`, SHA-256 matched, counts reconciled |
| `database_locked` events, before/after backups | 4 / 4 (unchanged) |

> **Correction to the milestone brief.** The brief recorded the canonical root as
> `/mnt/data/probability-arena/backups`. The deployed value is
> `/mnt/data/probability-arena-backups`. `BACKUP_DIR` was **not** modified; the
> documentation was corrected to the deployed value.

---

## 2. Freshness contract

```python
# app/services/backup_freshness.py
BACKUP_FRESHNESS_MAX_AGE_SECONDS = 36 * 60 * 60  # 129600
```

```text
healthy when age_seconds <= 129600
stale   when age_seconds >  129600
```

**Why 36 hours.** Against the deployed timer (`OnCalendar=*-*-* 01:30:00`,
`RandomizedDelaySec=600`, `Persistent=true`), successful runs start ~24 h apart
± the 10 min jitter, so the worst-case healthy gap is ~24 h 10 m — and even a
spring-forward on a local-time `OnCalendar` only stretches that to ~25 h 10 m.
The ~11 h of headroom means normal cadence, jitter, DST and a slow run can never
false-fire, while a genuinely **late** run is tolerated ~11 h 50 m past its slot.

It does **not** wait out a fully missed day, and that is deliberate rather than
slack: after a miss the timer's own next attempt is ~48 h after the last good
backup, so 36 h fires ~11.8 h *before* that. The operator learns that last
night's backup did not happen while there is still a day left to act, instead of
after a second night has gone by too.

**Why a code constant.** It is deliberately **not** a setting. A tunable
"how stale is too stale" knob is exactly the value that gets quietly widened
until the alert stops meaning anything — and a per-host override would let
production and tests disagree about what "stale" means. The report CLI, the
MarketOps evaluator, the alert payload, the tests and this document all read the
one constant; `test_threshold_constant_does_not_drift` asserts there is exactly
one definition site and that no consumer restates the number.

**Age is measured from the manifest's committed `created_at`**, not from
filesystem mtime. The manifest is the pipeline's commit record and the artifact
is published in the same run, so one timestamp governs the pair; mtime can be
rewritten by unrelated tooling, `created_at` cannot.

**Timestamp handling.** UTC-aware throughout. A naive `created_at` is
interpreted as UTC (the canonical repository policy — same as
`marketops._aware`), never as local time. A manifest dated more than 5 min into
the future is `manifest_future_dated` — unhealthy, so it cannot mask staleness
forever, but only `warning`, because that is a clock problem rather than an
absent backup.

### A backup is healthy only when all of these hold

1. `BACKUP_DIR` resolves to the canonical configured root
2. the root exists and is a real directory
3. the root is not a symlink
4. the root is not group/world-writable
5. a strict-name-matched manifest exists, in canonical stamp form
6. the newest strict manifest parses
7. its `manifest_version` is within the supported range (`1..MANIFEST_VERSION`)
8. its `status` is `verified`
9. the manifest records `integrity_check=ok`
10. the manifest records an Alembic revision in the expected format
11. its `created_at` agrees with its own filename stamp (within 1 s)
12. neither stamp is more than 5 min in the future
13. it names exactly one strict-name-matched artifact, for its OWN stamp
14. the artifact exists inside the canonical root
15. the artifact is a regular file
16. the artifact is not a symlink
17. the artifact's byte size exactly matches `backup_bytes`
18. the artifact still starts with the gzip magic bytes
19. the manifest/artifact age is at most 36 h
20. no path escapes the canonical root

---

## 3. Newest-manifest semantics and the no-fallback rule

The newest strict manifest is selected by its **committed `created_at`**, with a
deterministic filename tie-break.

A manifest that cannot be parsed still competes for "newest" using its (always
present, strict) filename stamp. That is the whole point:

> **No silent fallback.** When the newest committed manifest is malformed,
> unsupported, missing its artifact, pointing at a symlink, size-mismatched or
> path-invalid, that state is reported as **unhealthy**. It is never skipped in
> favour of an older healthy backup — otherwise a broken latest backup would be
> hidden behind yesterday's good one, which is precisely the silent degradation
> this milestone exists to catch.

A strict-named manifest that is a symlink, a directory, a FIFO, or otherwise not
a confined regular file is likewise recorded as an invalid candidate — never
opened, never followed, but still visible in the verdict.

**Three bindings stop a manifest from lying about what it certifies.**
`backup.py` derives the filename stamp, `created_at` and `backup_filename` from
one variable, so genuine output always agrees. Requiring that agreement costs
three comparisons:

1. `created_at` must match the filename stamp within 1 s — otherwise the whole
   freshness verdict rests on a single mutable JSON scalar while the immutable
   corroborating value sits unused in the filename next to it.
2. `backup_filename` must be the artifact for *that same stamp* — otherwise a
   fresh manifest could vouch for an artifact from an entirely different run.
3. Both stamps must be in **canonical** form. `strptime` accepts 1-2 digit
   fields, so `backup-20260803T13426Z...` parses — to 13:42:06, i.e. *newer*
   than the canonical `01:34:26` of the same day. Such a file would win
   "newest", latch the alert at critical forever, and never be reaped, because
   retention only ever prunes `.db.gz` names. A `strftime` round-trip rejects it.

Future-dating is checked on the **filename stamp** as well as `created_at`, for
the same reason: one stray `backup-20991231T000000Z.manifest.json` would
otherwise permanently outrank every real backup.

**A failed backup that publishes no manifest is a different case.** The pipeline
writes the data file first and the manifest last, so a failed run leaves no
manifest at all and the prior verified backup legitimately remains newest. It
then goes stale naturally once verified backups stop appearing — which is the
correct signal, on the correct timescale.

---

## 4. Performance and the full-hash policy

This runs on **every five-minute MarketOps cycle**, so it must be cheap and
bounded. It deliberately does **not**:

- recompute the SHA-256 of the 400+ MiB artifact
- decompress the artifact
- open the backup as a SQLite database
- traverse the backup root recursively
- retry, poll, sleep, or touch the network

**Trust boundary.** The backup pipeline already computes the SHA-256, verifies
the *compressed* artifact (decompress → header check → `integrity_check` →
required tables → counts reconciled against the live source), and publishes the
manifest **last** as its commit record. A manifest therefore *is* the assertion
that the artifact was byte-verified at publication time. Re-asserting that every
five minutes would cost minutes of CPU and hundreds of MiB of I/O per cycle to
re-derive a fact that has not changed.

What this evaluator re-checks is the set of things that can silently rot *after*
publication, cheaply and structurally: strict filename, path confinement,
regular file, no symlink, exact size match, verified manifest status, recorded
integrity result, and age.

**Full cryptographic verification stays in `verify-db-backup`** — that is the
tool for "is this artifact byte-perfect right now?", run on demand, not on
cadence. The report CLI prints a line saying so.

### Bounded inspection budget

| Bound | Value | Behaviour when exceeded |
|---|---|---|
| `MANIFEST_INSPECTION_CAP` | 64 manifests **read** | the true total is still reported; the verdict is unaffected |
| `DIRECTORY_ENTRY_CAP` | 4096 entries in one non-recursive scan | `backup_root_inspection_cap_exceeded` (unhealthy, **visible**) |
| `MAX_MANIFEST_BYTES` | 64 KiB per manifest (real ones are ~1 KiB) | that manifest is invalid |

Names are collected first, sorted newest-first, and only the newest 64 are read.
That ordering is load-bearing: reading in arbitrary `scandir` order and bailing
when the cap is hit would report a large-but-perfectly-healthy inventory as
critical "backup protection is gone". Sorting first makes the read cap unable to
change the verdict at all — the newest manifest is always inspected — while
`strict_manifest_count` still reports the true total. The directory-entry cap
still fails loudly, because a root with 4096+ entries is itself broken.

Every read is single-descriptor: `os.open(O_RDONLY|O_NOFOLLOW|O_NONBLOCK)` then
`os.fstat` on that same fd. `O_NOFOLLOW` refuses a symlink at open time,
`O_NONBLOCK` makes a planted FIFO fail instead of blocking the MarketOps cycle
forever on a read that has no timeout anywhere, and fstat-on-the-same-fd means
the thing measured is provably the thing read.

**What `healthy` does and does not mean.** It means a recent backup was
produced, is present, is the exact size its manifest committed to, and still
starts with a gzip header. It does **not** mean the bytes are still good: an
artifact overwritten in place with same-length gzip garbage reports healthy.
That is the deliberate trust boundary — `verify-db-backup` is the answer to "is
this artifact byte-perfect right now?".

Design budget: typical evaluation < 25 ms. `test_evaluator_is_bounded` asserts
< 250 ms per call against an 11-backup root — deliberately generous so CI noise
cannot flake it, while still failing loudly if the check ever starts hashing or
opening the artifact.

---

## 5. Health reasons

Deterministic and mutually exclusive. Evaluated strictly in this order, so the
first genuine fault is what gets reported:

| Reason | Meaning | Severity |
|---|---|---|
| `healthy` | recent, committed, structurally valid backup | — |
| `backup_root_unconfigured` | `BACKUP_DIR` is empty | critical |
| `backup_root_symlink` | root is a symlink (a swappable root is not canonical) | critical |
| `backup_root_missing` | root does not exist | critical |
| `backup_root_invalid` | root exists but is not a directory | critical |
| `backup_root_insecure` | root is group/world-**writable**, so manifests can be forged | critical |
| `backup_root_inspection_cap_exceeded` | >4096 entries in the root | critical |
| `no_committed_backup` | no strict manifest at all | critical |
| `manifest_invalid` | unparseable, malformed field, bad artifact name, stamp disagreement | critical |
| `manifest_unsupported` | `manifest_version` outside the supported range | critical |
| `manifest_not_verified` | `status != verified`, or `integrity_check != ok` | critical |
| `manifest_revision_invalid` | no/malformed `alembic_revision` — see below | critical |
| `manifest_future_dated` | manifest or filename stamp >5 min in the future | **warning** |
| `artifact_symlink` | named artifact is a symlink | critical |
| `artifact_missing` | named artifact is absent | critical |
| `artifact_invalid` | not a regular file, escapes the root, or is not a gzip stream | critical |
| `artifact_size_mismatch` | on-disk size ≠ `backup_bytes` | critical |
| `backup_stale` | valid pair, older than 36 h | critical |
| `evaluation_error` | the check itself failed | **warning** |

**`manifest_revision_invalid` is a deliberate divergence from the publisher.**
`backup.py` documents `alembic_version` as *optional* (create_all-based test
DBs legitimately lack it) and will publish `status="verified"` with
`alembic_revision: null`. This check is stricter: for the production database,
a backup taken from a schema with no `alembic_version` is itself the anomaly.
It gets its **own** reason rather than `manifest_invalid`, because telling an
on-call "invalid" would send them hunting for corruption in a perfectly intact
file. A cross-reference comment sits next to `EXPECTED_TABLES` in `backup.py`
so the two cannot drift. The format check is shape-only and permissive about
punctuation (`ops-001` is a legal Alembic rev-id); it never pins a value.

**`manifest_future_dated` is a warning, not a critical.** A backwards clock
step — NTP/chrony correction, VM suspend/resume, RTC drift after power loss —
would otherwise page someone about a backup that is seconds old and perfect.
It is still *unhealthy*, so it cannot mask staleness, and the evidence names
the offending manifest.

**Malformed states are never normalized into `backup_stale`.** Temporal
staleness is only ever reported once the newest manifest/artifact pair is
already structurally valid — a stale *and* broken backup reports the structural
fault, because that is the more urgent and more actionable one.

`evaluation_error` is a `warning`, not a `critical`: a broken *check* is not the
same as absent backup protection, and paging on the monitor is how monitors get
switched off. No exception text is carried into the result — an arbitrary
message is exactly the field that leaks paths and environment detail.

That asymmetry is also why **one bad file must never produce
`evaluation_error`**. `json.loads` raises `RecursionError` (a `RuntimeError`,
not a `ValueError`) on pathological nesting, so a single 40 KB file of nested
brackets — comfortably under the size cap — would otherwise abort the whole scan
and downgrade a critical "the backups are gone" into a warning "the monitor
broke". That is precisely the outcome an attacker with write access to the root
would want. `_read_candidate` therefore catches `Exception` and degrades exactly
one *candidate*.

---

## 6. Read-only report CLI

```bash
python -m app.cli sqlite-backup-freshness-report --format text
python -m app.cli sqlite-backup-freshness-report --format json
```

Zero provider calls. Opens no database. Writes no file. Creates no alert. Runs
no backup. Prunes nothing. Recomputes no hash. Text and JSON carry exactly the
same fields; both disclose the 36-hour threshold, the newest committed backup's
age, and the exact health reason. Exit code `0` = healthy, `1` = unhealthy —
the `verify-db-backup` convention, so a report that exits 0 always means
protection is actually there. (`evaluation_error` also exits 1: fail closed. A
caller that needs to distinguish a broken check from missing protection should
read `reason`, not the exit code.)

All manifest strings echoed back — `status`, `integrity_check`,
`alembic_revision` — are length-capped and stripped of control characters
first. They are attacker-influenced, and an `alembic_revision` of 40 KB
containing raw ESC is terminal-control injection against whoever runs the
report.

**No `--backup-root` / `--now` flags, by design.** The evaluator takes both as
injectable arguments (`evaluate_backup_freshness(backup_root=..., now=...)`) and
the tests use that seam. Exposing an arbitrary operator-supplied path on a
production CLI is a strictly larger surface than this check needs, and a clock
override on a freshness monitor is a way to make it lie. Gate 6 permitted the
flags; the narrower option was taken.

---

## 7. MarketOps integration

Flag, **default off**:

```env
MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT=false
```

**Ordering.** Step **7b** of the cycle, immediately after `_health_alerts`
(step 7) — the operational-health portion, adjacent to the existing
`db_growth_warning` path, and after every market/crypto/scoring stage has
finished. It starts no scan, needs no cycle state, and nothing downstream
branches on its result.

Guarantees:

| Requirement | How |
|---|---|
| no new scan | reads local files only; never calls a scan service |
| no provider calls | the module has no HTTP client; `external_calls=0` is asserted |
| no backup execution | never calls `backup_database` |
| no backup-file mutation | AST audit proves no write-capable call exists in the module |
| one bounded directory scan | single non-recursive `os.scandir` of the canonical root |
| at most one evaluation per cycle | one call site, guarded by the flag; asserted by test |
| isolated failure handling | the whole hook body is wrapped in `try/except Exception` |
| cannot fail MarketOps, **including under `fail_fast`** | the exception is caught *inside* the helper and never re-raised, so it never reaches the stage machinery that honours `fail_fast` — and the alert write happens on its own session, so it cannot poison the shared one either (see §8) |

Summary keys:

- `run.summary["backup_freshness"]` — normal result (bounded)
- `run.summary["backup_freshness_error"]` — `"<ExceptionType>: <message[:300]>"` on failure

The bounded summary carries `status`, `healthy`, `reason`, `newest_verified_at`,
`newest_backup_filename`, `age_seconds`, `threshold_seconds`, `artifact_exists`,
`size_matches_manifest`, `invalid_manifest_count`, `external_calls`,
`duration_ms`, `alert_action`. It carries **no** full paths, no manifest bodies,
no hashes, no database contents and no environment values. Filenames are
basenames only.

---

## 8. Alert lifecycle

Reuses the deployed `marketops_alerts` table. **No new table, no migration.**

```text
alert_type: backup_freshness_warning
title:      "Backup protection unhealthy"   (STABLE — carries no varying data)
```

**Why the title is stable.** `MarketOpsAlertService.create` dedupes on
`(alert_type, title, status=open)`. Titles that embed a changing measurement
therefore stack a *new* open row every time the measurement moves —
`db_growth_warning` has accumulated 933 open rows on EVO-X2 exactly that way.
Reason, age and threshold live in the message and evidence instead.

Two methods were added to `MarketOpsAlertService` for this identity:

- `upsert_open(...) -> (alert, created)` — creates when no open row exists,
  otherwise refreshes severity/message/evidence **in place** on the one open
  row. Unlike `create`, a changed condition is not silently dropped.
- `resolve_open_by_type(session, alert_type) -> int` — resolves every open row
  of a type through the existing lifecycle (`status`, `resolved_at`).

Both **flush** rather than commit, so the write joins the caller's cycle
transaction. `resolve(alert_id)` keeps its own commit for the interactive CLI.

**The alert write runs on its OWN short-lived session**, mirroring
`_materialize_cycle_anchors`. This is load-bearing, not stylistic. `database is
locked` is a live possibility on this host — the predecessor milestone measured
4 such events — and the hook is most likely to be writing during the backup
window. Writing on the *shared* cycle session would fail two ways:

* A failed INSERT leaves the shared session in a needs-rollback state, so the
  cycle's own final `session.commit()` raises `PendingRollbackError`, the run
  row is discarded, and a monitoring hiccup has destroyed a whole cycle's audit
  record. A `SAVEPOINT` is **not** sufficient: `session.begin_nested()`
  autoflushes the session's pending state *before* emitting the savepoint, so
  the same transient lock error simply moves to that flush — and the hook would
  have *introduced* a flush point that survivable contention would otherwise
  have retried at the cycle's final commit.
* On pysqlite, a `SAVEPOINT` opened when no transaction is in progress **commits
  on RELEASE**. Whether the alert joined the cycle transaction would then depend
  on whether an earlier stage happened to leave DML pending — a non-deterministic
  transaction boundary.

The isolated session removes both. The shared session is never touched, and the
alert's durability is deterministic: it commits independently and survives a
later cycle failure, which is the right semantics for a monitoring signal. As
with the anchor feed, the shared session is checkpoint-committed first so the
isolated session is not blocked by a write lock this same coroutine holds.
`test_real_driver_lock_error_cannot_fail_or_poison_the_cycle` proves it with a
genuine second connection holding `BEGIN EXCLUSIVE` for exactly the alert write.

| State | Behaviour | `alert_action` |
|---|---|---|
| healthy, none open | nothing written | `none` |
| healthy, one open | resolves it through the lifecycle | `resolved` |
| unhealthy, none open | creates exactly one | `created` |
| unhealthy, already open | updates the same row in place | `updated` |
| unhealthy, reason changed | updates the **same** row — never a second one | `updated` |
| check broke while an alert is open | leaves the existing row untouched | `preserved` |
| recovery | resolves, then stays quiet | `resolved` → `none` |
| regression after recovery | opens exactly one new alert | `created` |

`preserved` matters: `upsert_open` overwrites severity in place, so without it a
transient `evaluation_error` would silently downgrade an outstanding **critical**
"no committed backup" to a **warning** "the monitor hiccuped" — masking a real
outage on the one row carrying it. `upsert_open` also *converges* rather than
assuming its invariant: if several open rows of the type exist (reachable,
because the cycle overlap guard is a read-then-write check, not a lock), it
refreshes the oldest and resolves the rest.

**Discoverability.** The alert is additionally surfaced on `marketops-report`
from the latest **run summary**, not only from `open_alerts`. That is not
belt-and-braces: `open_alerts` is `ORDER BY id DESC LIMIT 10`, and the
pre-existing `db_growth_warning` mints a new open row roughly every 13 minutes
(its title embeds a changing size, defeating the `(type, title)` dedup — 933
open rows on EVO-X2 as of this milestone). A backup-protection alert surfaced
only through `open_alerts` would drop off the operator's report within ~2 hours
and never come back, so "degradation is loud" would be false within one working
day. `_recommend` also names it ahead of the generic open-alert count. Making
`db_growth_warning`'s title stable and bulk-resolving its backlog is the root
fix and belongs in its own milestone.

Repeated unhealthy cycles never stack duplicates (asserted over 12 consecutive
cycles). Repeated healthy cycles are completely silent (asserted over 10).

**No automatic remediation.** The alert path never triggers a backup, never
prunes, never touches a backup file. Tests fail the run if `backup_database`,
`apply_retention` or `prune_old_backups` is reached from it.

The evaluator itself is read-only; the only expected database write in this
whole milestone is the bounded alert lifecycle above.

---

## 9. Tests and reviews

`tests/test_sqlite_backup_freshness_alert_001.py` — 118 tests over disposable
backup roots and disposable SQLite databases. **No production backup artifact is
created, read, aged, renamed or deleted by any test.**

Coverage: the 36 h boundary (below / exactly at / one second past), naive and
offset timestamps, every unhealthy reason, symlink and traversal rejection on
both the manifest and the artifact, deterministic newest-manifest selection,
the no-silent-fallback rule, strict filename matching, foreign files and
subdirectories ignored safely, both inspection caps, text/JSON parity, the CLI
writing nothing and calling nothing, flag-off being a complete no-op, exactly
one evaluation per cycle, hook failure under `fail_fast`, the full alert
lifecycle including recovery and regression, secret-free payloads, bounded
summaries, no migration, no new unit file, no provider/trading/capital surface,
the performance budget, an AST audit proving the evaluator has no write-capable
call (and that `os.open` can only ever be read-only), full-cycle integration
through the real `run_once`, a genuine driver-level `database is locked` failure
during the alert write, a planted FIFO, a `RecursionError`-inducing manifest, a
real `apply_retention` pass, the publication window between the two
`os.replace` calls, and an end-to-end test against `backup_database` itself so
the evaluator's view of the manifest contract cannot drift from what the
pipeline publishes.

### Independent reviews

Five independent reviews were run — backup-contract correctness, MarketOps
integration, security, operations, and scope/safety. All material findings were
fixed before deployment; the ones that changed behaviour:

| Finding | Fix |
|---|---|
| Alert write on the shared session could fail the whole cycle | isolated short-lived session (§8) |
| `begin_nested()` autoflush re-introduced the same hazard | ditto — no savepoint on the shared session |
| `RecursionError` from nested JSON collapsed the verdict to `evaluation_error` | `_read_candidate` catches `Exception`; one bad file degrades one candidate |
| Planted FIFO would block MarketOps forever | single-descriptor `O_NOFOLLOW\|O_NONBLOCK` read |
| stat-then-open TOCTOU on manifest and artifact | `fstat` on the same descriptor |
| `SUPPORTED_MANIFEST_VERSIONS` pinned to the writer's version | accept the range `1..MANIFEST_VERSION` |
| Missing `alembic_revision` reported as `manifest_invalid` | own reason + widened format + cross-ref comment in `backup.py` |
| `created_at` fully forgeable; manifest could certify another run's artifact | three stamp bindings (§3) |
| Lenient `strptime` let a junk name win "newest" permanently | canonical `strftime` round-trip |
| Manifest-read cap paged as "protection gone" on a healthy large inventory | sort-then-cap; verdict unaffected |
| Backwards clock step paged as critical corruption | `manifest_future_dated`, warning |
| Alert evidence named no file for manifest-side faults | `newest_manifest_filename` + sizes + manifest claims in the summary |
| Alert invisible behind the `db_growth_warning` flood | surfaced on the run summary and in `_recommend` |
| Unbounded manifest strings printed raw (terminal-control injection) | length-capped, non-printables stripped |
| 36 h rationale arithmetically wrong in code comment and doc | corrected (§2) |
| Canon/capability docs not updated | `app/canon.py`, `CAPABILITY_MATRIX`, `PROJECT_CANON`, `ROADMAP`, `EVO_X2_RUNBOOK`, `README` |

### Gate 11 — corrected test name

`test_backup_opens_read_only_and_outside_live_db` claimed something its body
never asserted and that the implementation does not do: `backup_database` opens
the **source read-write** (sqlite3's online-backup API requires it). What the
test actually proves is that the published artifact is a distinct file from the
live database, and that a *restored copy* of it is intact and refuses writes
when opened `mode=ro`.

Renamed to `test_published_artifact_is_separate_and_restores_intact_read_only`.
**Product behaviour was not changed to fit the old name.**

---

## 10. Deployment

*(Filled in from measured evidence at each step — nothing here is written ahead
of the event.)*

### Dark deployment

_Pending._

### Activation

_Pending._

### First natural active cycle

_Pending._

---

## 11. Rollback

```env
MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT=false
```

The next MarketOps cycle reverts the hook to a complete no-op: no evaluation, no
summary key, no alert write, no filesystem access. Nothing else needs to be
undone — there is no timer to stop, no migration to reverse, no state to clean
up, and no backup behaviour that was changed. Any alert already open can be
resolved normally with `marketops-resolve-alert <id>`.

The read-only `sqlite-backup-freshness-report` CLI keeps working regardless of
the flag.
