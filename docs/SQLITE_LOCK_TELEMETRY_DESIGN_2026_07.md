# SQLITE-LOCK-TELEMETRY-DESIGN-001 — nonblocking writer & lock observability (2026-07)

Implementation-ready design (not implementation) for **measurement-only** SQLite writer
and lock telemetry. **Documentation only — no code, test, config, model, schema,
migration, `.env`, systemd unit, or SQLite pragma changed; no write transaction, scan,
scoring, outcome sync, retention, backup, cohort, observation, or provider call
executed; EVO-X2 stays pinned at `3f742c9`.** Primary input:
`docs/SQLITE_WRITER_TOPOLOGY_2026_07.md` (commit `2e2b86f`). Carried-forward claims were
re-verified against that committed audit and spot-checked against live code
(`db.py:22-28`, `config.py:240`, `crypto_scout.py:589-707`, the three retry ladders) and
read-only EVO-X2 host evidence on 2026-07-17.

## Baseline confirmation (read-only, 2026-07-17)

| Fact | Audit (2e2b86f) | Live re-check | Source |
|---|---|---|---|
| Mac / origin | — | `2f9d419` | `git rev-parse` |
| EVO-X2 HEAD | `3f742c9` | `3f742c9` (pinned) | `ssh evo-x2 git rev-parse` |
| Alembic | `0027` | `0027_crypto_horizon_obs` head | `alembic/versions` |
| journal_mode | `delete` | `delete` | live `PRAGMA` |
| synchronous | `FULL` | `2` (FULL) | live `PRAGMA` |
| app busy-timeout | 30000 ms | `config.py:240` = 30000; connect_args `timeout` s (`db.py:27`) | code |
| DB size | 2.88 GB | 2,884,030,464 B | `ls -la` |
| WAL/SHM/journal files | none | none present (idle) | `ls` |
| Filesystem free | (no alerting) | 74 G free / 236 G / 68% used | `df -h` |
| Lock events / 7d | 59 | **53** (rolling window) | `journalctl grep` |
| Horizon units | none installed | none installed | `~/.config/systemd/user` |
| Active timers | marketops/watcher/meme-news/tick-agg/baseline/retention/edge-obs | same (tick-agg timer now active) | `list-timers` |
| SQLAlchemy event hooks in app | — | **none exist** (`grep event.listen` empty) | code |

No explicit `PRAGMA journal_mode`/`synchronous` is set in code — `delete`/`FULL` are
SQLite defaults; only the busy-timeout is applied (via SQLAlchemy `connect_args`). This
is why lock-hold time is not directly observable and the design leans on SQLAlchemy
events (§Timing).

## Verdict

Telemetry is **safe, additive, and the correct first move** before WAL or transaction-
ownership work — it is R5's monitoring prerequisite and the measurement baseline that
turns the WAL/ownership decisions from "obvious" into *validated*. The one hard
constraint dominates every choice: **the sink must not itself take the SQLite write lock
it is measuring.** That rules out a telemetry table in the shared DB and mandates an
append-only host file (JSONL) **on a local filesystem** outside the git tree: concurrent
writers rely on POSIX `O_APPEND` atomic seek-to-end plus the Linux local-FS inode lock
(`i_rwsem`) held across a single `write()`, so a whole-line append never interleaves —
with a ≤4096 B/line cap kept only as a defensive belt-and-suspenders, not as the
correctness basis. Because SQLite in rollback-journal mode exposes **no hook for exact file-lock hold
time**, every timing field must carry an explicit **measurement-quality** tag
(`exact`/`instrumented_estimate`/`derived_estimate`/`unknown`); the design never claims a
precision the hooks cannot deliver. Telemetry is **durable-but-nonblocking**: a telemetry
failure can never fail, slow, or roll back a writer. First slice after 2026-07-23
instruments **one low-risk maintenance writer (tick-aggregation)** plus the backup reader
for overlap — never the MarketOps hot path — and is independently `git revert`-able.
**Nothing here is implemented; all of it waits until after the 2026-07-23 candidate-
readiness checkpoint** because every writer instrumented is the frozen active runtime.

## Telemetry event model

One canonical append-only envelope per writer operation (parent) and per nested writer
(child). Flat rows correlated by ID — this **is** the parent/child model realized
append-only (§Writer attribution). Field types and nullability:

| Field | Type | Null? | Notes |
|---|---|---|---|
| `event_version` | int | no | schema version; starts at `1` |
| `event_id` | str (uuid4) | no | unique per emitted event |
| `parent_event_id` | str | yes | the enclosing operation's `event_id` (nested writers) |
| `writer_name` | enum str | no | canonical id (§Writer attribution) |
| `writer_class` | enum str | no | see enum |
| `writer_instance_id` | str | yes | pid+monotonic-start; distinguishes overlapping runs of one writer |
| `operation_name` | str | no | e.g. `run_once`, `commit_unit`, `sync_ticker` |
| `source_command` | str | yes | CLI subcommand that launched the process |
| `systemd_unit` | str | yes | resolved from env/`INVOCATION_ID`; null for manual |
| `marketops_cycle_id` | int | yes | correlation for in-cycle sub-writers |
| `scanner_run_id` | int | yes | when the op owns/created a `*_runs` row |
| `cohort_id` | int | yes | horizon writers |
| `job_id` | int | yes | horizon one-shot job |
| `process_id` | int | no | `os.getpid()` |
| `host` | str | no | short hostname (`mikolabs`) |
| `started_at` | ISO-8601 UTC | no | operation entry |
| `first_mutation_at` | ISO-8601 UTC | yes | first DML flush (write-lock could be acquired) |
| `commit_started_at` | ISO-8601 UTC | yes | `before_commit` |
| `finished_at` | ISO-8601 UTC | no | operation exit (commit/rollback/return) |
| `duration_ms` | int | no | `finished_at − started_at` |
| `transaction_hold_ms` | int | yes | **per transaction**: commit/rollback end − that txn's first mutation (reset each `after_commit`; see §Timing) |
| `lock_wait_ms` | int | yes | summed busy-timeout wait — **floor only** (successful delayed acquisitions are invisible; see §Timing) |
| `commit_ms` | int | yes | `after_commit − before_commit` |
| `rollback_ms` | int | yes | `after_rollback − rollback_start` |
| `retry_count` | int | no | attempts beyond the first (0 if none) |
| `retry_limit` | int | yes | ladder max for this writer |
| `attempt_number` | int | no | 1-based; child events per attempt |
| `outcome` | enum str | no | see enum |
| `exception_class` | str | yes | class name only (no message/args) |
| `exception_category` | enum str | yes | see enum |
| `sqlite_error_code` | int | yes | `sqlite3` extended code when available |
| `table_groups` | list[str] | yes | **coarse** table-family labels, never row values |
| `rows_attempted` | int | yes | writer-reported |
| `rows_committed` | int | yes | writer-reported |
| `rows_skipped` | int | yes | writer-reported |
| `partial_progress` | bool | yes | any sub-commit succeeded before failure |
| `provider_io_during_transaction` | bool | no | provider call while the session is dirty / in a transaction (SHARED or RESERVED — not a proof the write lock is held) |
| `provider_io_ms_in_txn` | int | yes | summed provider wall-time inside the write txn |
| `filesystem_io_during_transaction` | bool | no | non-DB file write while txn dirty |
| `database_bytes` | int | yes | sampled at finish (gauge writers only) |
| `filesystem_free_bytes` | int | yes | `os.statvfs` at finish (gauge writers only) |
| `journal_mode` | str | yes | sampled `PRAGMA` |
| `synchronous_mode` | str | yes | sampled `PRAGMA` |
| `external_calls` | int | yes | mirrors existing per-run ledgers |
| `measurement_quality` | object | no | per-timing-field quality map (§Timing) |

**Never emitted:** secrets, provider payloads, tokens/credentials, SQL bind parameters,
private content, raw stack traces in the primary event (a class name only), tickers,
token IDs, cohort **names**, market recommendations, or any side/size/EV/price/order/
wallet field. `table_groups` uses ~12 fixed families (`market_ticks`, `signals`,
`forecasts`, `outcomes`, `scores`, `crypto_discovery`, `crypto_horizon`, `meme`,
`polymarket`, `cross_venue`, `tennis`, `runs_audit`), never a bare table or row.

### Enums

**Outcome:** `success`, `failed_lock`, `failed_other`, `retried_success`,
`retried_failed`, `skipped_overlap`, `skipped_health`, `partial_success`, `rolled_back`,
`unknown`.

**Exception category:** `database_locked`, `database_busy`, `disk_full`,
`integrity_error`, `operational_error`, `timeout`, `process_interrupted`,
`provider_error`, `filesystem_error`, `unknown`. Classification is deterministic:
`_is_db_locked` string/`.orig` match → `database_locked`; `sqlite3` extended codes
`SQLITE_FULL`/`SQLITE_IOERR_*` → `disk_full`/`filesystem_error`; `IntegrityError` →
`integrity_error`; other `OperationalError` → `operational_error`.

**Writer class:** `scheduled_oneshot`, `continuous_daemon`, `manual_command`,
`dynamic_oneshot`, `maintenance`, `test`.

## Writer attribution

Canonical `writer_name` for all 15 writers from the topology audit (bounded label set —
safe as a metric label):

| `writer_name` | `writer_class` | Entry (`file:line`) | Session | Provider I/O in txn? | Retry class |
|---|---|---|---|---|---|
| `marketops_core` | continuous_daemon* | `marketops.py:558` | shared (parent) | via crypto child | timer/next-cycle |
| `marketops_crypto_scan` | scheduled_oneshot | `crypto_scout.py:578` | shared (child) | **YES `:620/:668`** | NO_RETRY |
| `baseline_scanner` | scheduled_oneshot | `scanner.py:188` | owns | no (fetch up-front) | next-cycle |
| `watcher` | continuous_daemon | `watcher.py:363` | fresh/iteration | no | timer/next-cycle |
| `meme_news` | scheduled_oneshot | `meme_scout.py:312` | owns | **YES `:321/:336`** | NO_RETRY |
| `tick_aggregation` | maintenance | `tick_aggregation.py:359` | owns | no | BOUNDED (any `OperationalError`, 1+3) |
| `retention` | maintenance | `retention.py:273` | owns | no | next-cycle |
| `backup` | maintenance | `backup.py:107` | reader (online-backup API) | no | reader |
| `crypto_tape` | manual_command | `crypto_tape.py:647` | owns | no | BOUNDED (string, 1+2) |
| `crypto_horizon_observe` | dynamic_oneshot | `crypto_horizon.py:791` | shared | **YES `:843`** | BOUNDED (1+1) |
| `outcome_sync` | scheduled_oneshot | `outcomes.py:33` | shared (child) | fetch-then-write | NO_RETRY |
| `forecast_scoring` | scheduled_oneshot | `calibration.py:100` | shared (child) | no | NO_RETRY |
| `polymarket` | manual_command | `polymarket.py:314` | owns | **YES** | NO_RETRY |
| `cross_venue` | manual_command | `cross_venue.py:966` | owns | no | NO_RETRY |
| `tennis_tape` | manual_command | `tennis_tape.py:334` | owns | no | NO_RETRY |

*`marketops_core` runs as a 5-min scheduled oneshot but the enclosing daemon perspective
is a design label; classify the process as `scheduled_oneshot` at emit time from the
systemd unit.

**Nesting decision — flat correlated events (parent/child), not true spans.** The audit
established that in-cycle sub-services (`outcomes`, `calibration`, `crypto_scout`,
`signal_workflow`) **commit the caller-owned shared session**. Modeling that as nested
open spans would require span-stack state and is fragile to the non-atomic checkpoint
commits. Instead: `marketops_core` emits **one parent operation event** (`event_id` =
cycle id) at cycle start/finish; each sub-writer emits its **own flat child event**
carrying `parent_event_id` + `marketops_cycle_id`. A nested service that commits a
caller-owned session attributes the **commit** to itself (`writer_name` = the sub-writer)
but records `parent_event_id` so the report can roll child hold-time up to the cycle. This
stays strictly append-only (no open-span bookkeeping), survives process kill (each row is
self-contained), and makes "which sub-writer actually held the lock" answerable. The
parent event's `transaction_hold_ms` is left `null` (the cycle is checkpoint-committed,
not one transaction) with `measurement_quality.transaction_hold = "unknown"` — an honest
non-claim rather than a misleading whole-cycle span.

## Timing semantics

SQLite rollback-journal exposes **no API for exact file-lock acquisition/hold**. All
timing is SQLAlchemy-event-derived or service-level; each field self-discloses quality.

| Reported field | Definition | Instrumentation boundary | Quality |
|---|---|---|---|
| operation duration | entry→exit wall | handler enter/exit | `exact` |
| transaction duration | `after_begin`→`after_commit`/`after_rollback` | `SessionEvents.after_begin` + commit/rollback | `exact` (wall), but ≠ lock hold |
| **write-lock hold estimate** (per txn) | this txn's first-write → its commit/rollback end | first DML statement of the txn → `after_commit`; **reset on every `after_commit`** | `instrumented_estimate` |
| lock-wait duration | busy-timeout wait before a statement succeeds/raises | statement execute wall when it raises `database is locked`, or retry-loop sleep sum | `derived_estimate` (**floor** — see note) |
| provider-I/O-in-txn | summed provider wall-time while the session is dirty / in a transaction | wrap adapter calls; guard on `session.in_transaction()` **or** pending dirty state | `instrumented_estimate` |
| commit duration | `before_commit`→`after_commit` (incl. `EXCLUSIVE` + fsync under `synchronous=FULL`) | `SessionEvents.before_commit`/`after_commit` | `exact` (closest proxy to `EXCLUSIVE` hold) |
| rollback duration | except-block/`ConnectionEvents.rollback` entry→`after_rollback` | rollback start captured at the service except-block (no `before_rollback` hook exists) | `instrumented_estimate` |

**`transaction_hold_ms` is per-transaction, never per-operation.** A writer that commits a
`*_runs` audit row before doing its work (e.g. `crypto_scout` commits the run row at
`:589`, then runs no-lock provider fetches, then a terminal commit at `:697`) has **two
separate write-lock holds** with a lockless gap between them. Timing the whole operation
from its first flush would merge both holds plus the gap into one inflated number — on
exactly the writer R1 is about. The op-context therefore resets `first_mutation_at` on
each `after_commit`, so each committed transaction emits its own hold; the operation-level
event carries only `duration_ms`.

**`lock_wait_ms` is a floor, not a true wait.** The 30 s busy-timeout makes SQLite sleep
and retry *internally*, then usually **succeeds without raising** — a write that waited 5 s
and then acquired the lock contributes 0. Only statements that fail with `database is
locked` (elapsed execute wall) and explicit app-level retry sleeps are captured. The
report must label this metric a lower bound on contention wait, never an estimate of it.

**Measurement-quality tiers** (recorded per field in `measurement_quality`):
- `exact` — wall time between two in-process SQLAlchemy events (operation, transaction,
  commit, rollback durations).
- `instrumented_estimate` — derived from event boundaries that *approximate* a SQLite
  internal (write-lock hold ≈ first-mutation→commit-end; the RESERVED lock is acquired
  lazily by SQLite at first write, which we can only see as first flush).
- `derived_estimate` — inferred, not directly timed (lock-wait from a failing statement's
  elapsed execute time, or summed retry sleeps).
- `unknown` — not measurable with available hooks (whole-cycle transaction hold across
  checkpoint commits; exact byte-level lock acquisition instant).

**Instrumentation boundaries (SQLAlchemy events — none exist today, all additive):**
`PoolEvents.checkout`/`checkin` (connection acquisition), `SessionEvents.after_begin`
(txn begin — note DEFERRED BEGIN takes **no** lock, so this is not lock acquisition),
**`after_flush` or a `before_cursor_execute` filtered to INSERT/UPDATE/DELETE** for the
true first-write instant (never `before_flush`, which fires before any SQL is emitted and
would bias the hold long), `before_commit`/`after_commit` (commit span),
`after_rollback`/`after_soft_rollback` (rollback END; the rollback START is taken at the
service except-block since SQLAlchemy has no `before_rollback`), and
`ConnectionEvents`/`handle_error` or `_is_db_locked` at the writer boundary (busy
failure). Provider-call start/end while a transaction is dirty is captured by a thin
context manager the audit's four in-txn-provider writers already localize
(`crypto_scout`, horizon, `meme_scout`, `polymarket`). A registration helper attaches
listeners **once** to the shared engine/sessionmaker; listeners write to a thread-local
op-context, not the DB.

## Sink decision

| Option | Recursive-lock risk | Durability | Queryable | Simplicity | Secret exposure | Rotation | Corruption isolation | Rollback |
|---|---|---|---|---|---|---|---|---|
| 1 Append-only JSONL (host, outside git) | **none** (no shared lock; `O_APPEND`) | high (fsync-optional) | high (`jq`, report cmd) | high | controlled (schema-only) | file rotate | one bad line skipped | delete file |
| 2 Existing SQLite table | **HIGH — measures lock contention by adding a writer** | high | high (SQL) | med | low | retention | txn-scoped | migration revert |
| 3 journald structured | none | high (systemd-managed) | med (`journalctl -o json`) | med | controlled | journald | per-record | stop emitting |
| 4 Prometheus only | none | low (scrape gaps lose raw) | aggregates only | med | controlled | n/a | n/a | remove textfile |
| 5 Hybrid JSONL + Prometheus aggregation | none | high raw + high agg | high | med | controlled | file + textfile | line-level | delete file/textfile |

**Chosen: JSONL primary (raw events) + optional journald mirror for durability + a
later Prometheus textfile-collector for aggregates (slice 001E).** Option 2 is rejected
outright — it is the recursive-contention trap the whole milestone exists to avoid.
Option 4 alone loses the raw distribution needed for the baseline. The hybrid (5) is the
end state; 001A ships JSONL only.

**JSONL specification:**
- **Canonical path:** `~/probability-arena-telemetry/sqlite-writes.jsonl` (active file;
  the `<domain>-observation` convention of `~/edge-observation/`/`~/crypto-horizon-
  observation/` would suggest `~/sqlite-writer-observation/` — a cosmetic choice, either is
  acceptable), configurable via `SQLITE_TELEMETRY_DIR`. **Must be a local filesystem**
  (NFS/network `O_APPEND` is client-emulated and can interleave — unsupported). Lives
  **outside the repo tree** so `git status` stays clean.
- **Directory permissions:** `0700` (owner `miko_node_001` only); files `0600`.
- **Line schema:** exactly one JSON object per line = one event envelope; UTF-8; no
  embedded newlines.
- **Append atomicity:** each emit is **one unbuffered `os.write()` on the raw fd** opened
  `O_APPEND|O_WRONLY` (never a buffered `io`/text wrapper, which could split into multiple
  syscalls and interleave across threads/processes). POSIX atomic append + the Linux
  local-FS inode lock make a single whole-line `write()` non-interleaving regardless of
  size; the ≤4096 B cap is a defensive margin. A **short write** (partial byte count) is
  counted as a dropped event (`telemetry_dropped`), **never resumed** (resuming would let a
  second writer interleave between the two syscalls).
- **Maximum line size:** 4096 B (defensive). Oversize event → truncate `table_groups`/
  optional fields and re-serialize to valid JSON, set `truncated=true`; never split a line.
- **Rotation:** size-based at 64 MB → `sqlite-writes-YYYYMMDD-HHMMSS.jsonl`; daily roll as
  backstop. **A single owner performs rotation** (the collector/maintenance step, not the
  writers, to avoid two processes both tripping the threshold and `rename()`-ing
  concurrently). Long-lived daemons (`watcher`) that hold an fd must **reopen on SIGHUP or
  when the inode/size changes** (or open-append-close per emit) so they don't keep writing
  into the renamed inode — otherwise rotated files keep growing and their mtime never ages
  out. "Open once" applies only to short-lived one-shots.
- **Retention:** 30 days of rotated files, then delete (config `SQLITE_TELEMETRY_RETENTION_DAYS`).
- **Malformed-line handling:** the reader skips a line that fails JSON parse or schema
  validation, counts it, and reports `malformed_lines=N` — never aborts.
- **Partial-line recovery:** a trailing line without `\n` (crash mid-write) is discarded
  by the reader; `O_APPEND` guarantees earlier lines are intact.
- **Disk-growth projection:** ~600 B/event × est. events/day. Worst case at current
  cadence (marketops 5-min ≈ 288/d ×~6 sub-writers, watcher 60-s ≈ 1440/d, meme-news
  144/d, tick-agg 24/d + sub-windows, baseline 6/d) ≈ **5–8k events/day ≈ 3–5 MB/day ≈
  ~0.1 GB/month** before rotation — negligible vs the 74 G free.
- **Secret scanning:** a schema validator rejects any event whose values match the
  `AGENTS.md`/`TESTING_POLICY.md` secret grep or contain a `DATABASE_URL`; a unit test
  asserts no envelope field can carry credentials.
- **Backup policy:** telemetry is disposable measurement data — **not** backed up with the
  DB; loss is acceptable (`durable_but_nonblocking`).

## Metrics and thresholds

Derived from JSONL by the report command / a textfile collector (**pull ownership is the
report or collector, never the hot path**). All labels bounded; **prohibited high-
cardinality labels:** token IDs, tickers, cohort IDs, run/job IDs, exception messages,
`writer_instance_id`, timestamps.

| Metric | Type | Labels | Source field | Buckets / reset |
|---|---|---|---|---|
| `sqlite_writer_operations_total` | counter | `writer_name`,`outcome` | 1/event | monotonic |
| `sqlite_writer_failures_total` | counter | `writer_name`,`exception_category` | outcome∈{failed_*,retried_failed} | monotonic |
| `sqlite_lock_failures_total` | counter | `writer_name` | exception_category=database_locked | monotonic |
| `sqlite_lock_wait_seconds` | histogram | `writer_name` | `lock_wait_ms` | 0.01,0.1,0.5,1,5,10,30,60 |
| `sqlite_transaction_hold_seconds` | histogram | `writer_name` | `transaction_hold_ms` | 0.05,0.1,0.5,1,5,10,30,60 |
| `sqlite_commit_seconds` | histogram | `writer_name` | `commit_ms` | 0.01,0.05,0.1,0.5,1,5,10,50 |
| `sqlite_retries_total` | counter | `writer_name`,`outcome` | `retry_count` | monotonic |
| `sqlite_partial_progress_total` | counter | `writer_name` | partial_progress=true | monotonic |
| `sqlite_writer_overlap_total` | counter | `writer_a`,`writer_b` (**sorted** unordered pair; ≤105) | derived overlap | monotonic |
| `sqlite_provider_io_in_txn_seconds` | histogram | `writer_name` | `provider_io_ms_in_txn` | 0.1,1,5,10,30,60 |
| `sqlite_database_bytes` | gauge | — | `database_bytes` | last-write-wins |
| `sqlite_filesystem_free_bytes` | gauge | — | `filesystem_free_bytes` | last-write-wins |
| `sqlite_journal_bytes` | gauge | — | `-journal` size sample | last-write-wins |
| `sqlite_wal_bytes` | gauge | — | `-wal` size (0 today; ready for WAL milestone) | last-write-wins |
| `sqlite_marketops_db_wait_seconds` | histogram | — | cycle Σ child `lock_wait_ms`+`commit_ms` | 0.1,0.5,1,5,10,30 |

**Provisional thresholds** (informational / warning / critical — provisional until the
7-day baseline exists; a telemetry alert **never** changes MarketOps behavior):

- **Lock failures:** info ≥1/writer/day; warn >5/hr total **or** >10%/writer failure
  rate; crit ≥3 consecutive failed scheduled runs of one writer (lost work).
- **Transaction hold:** info p50>1 s; warn p95>10 s; crit max>30 s (past busy-timeout)
  **or** any `provider_io_in_txn` p95>10 s (the crypto-scan R1 surface).
- **Disk:** info free<20% ; warn free<10% **or** <15 GB **or** DB>`DB_GROWTH_WARNING_MB`
  (1536); crit free<5% **or** <8 GB **or** projected days-to-exhaustion<14 **or**
  DB>`DB_GROWTH_CRITICAL_MB` (3072). Growth rate + journal/WAL growth tracked as trends.
  These OPS-011 constants are **absolute DB-file-size** gates (despite the `_GROWTH_`
  name), so `sqlite-storage-health-report` will show the DB **at the warning tier from day
  one** (live file 2,884 MB, ~188 MB below critical) — that is the *existing* DB state, not
  telemetry-induced: telemetry is out-of-DB JSONL and adds **zero** bytes to the database.
- **MarketOps impact:** info db-wait>5% of cycle; warn cycle>90 s **or** db-wait>20%;
  crit a stage skipped due to lock **or** a readiness record missing because a cycle
  failed.

## Reporting design

Three read-only commands, all sourced from JSONL, **zero provider calls, zero DB writes**,
bounded history, text/JSON parity, standard disclaimer. They **never** execute recovery,
kill a process, retry a job, change a schedule/pragma, or recommend any action. Report
commands **only read** JSONL — the file-mutating JSONL rotation/30-day-deletion is a
**separate maintenance step** (the collector, or a distinct `--rotate` subcommand), kept
out of the read-only report path so their read-only guarantee is clean. Any `PRAGMA`
sampling in the storage report uses the **query form only** (`PRAGMA journal_mode;`),
never an assignment — the report cannot flip a pragma.

Shared options: `--hours N` | `--since ISO` | `--until ISO` | `--writer NAME` |
`--outcome ENUM` | `--top N` | `--format text|json`.

- **`sqlite-lock-telemetry-report`** — lock-failure counts by writer/hour/category,
  retry outcomes, transaction-hold p50/p95/max per writer, provider-I/O-in-txn summary,
  consecutive-failed-run detection. Output schema: `{window, writers:[{writer_name,
  operations, failures, lock_failures, failure_pct, retry_success, retry_failed,
  hold_p50_ms, hold_p95_ms, hold_max_ms, provider_io_in_txn_p95_ms}], malformed_lines,
  disclaimer}`.
- **`sqlite-writer-overlap-report`** — wall-clock overlap between writer pairs (interval
  intersection over `started_at`/`finished_at`), ranked; flags the audited critical pairs
  (crypto-scan×watcher, marketops×meme-news, backup×any). Schema:
  `{window, overlaps:[{writer_a, writer_b, overlap_count, overlap_seconds_total,
  max_concurrent}], disclaimer}`.
- **`sqlite-storage-health-report`** — latest `database_bytes`, `filesystem_free_bytes`,
  journal/WAL bytes, growth rate, projected days-to-exhaustion, threshold state per gate.
  Schema: `{database_bytes, free_bytes, free_pct, db_growth_mb_per_day,
  projected_days_to_full, journal_bytes, wal_bytes, thresholds:{...state...}, disclaimer}`.

Disclaimer (all three): *"Read-only measurement. No recovery, retry, scheduling, pragma,
or trading action. Thresholds provisional until baseline."* Sample (text):
```
sqlite-lock-telemetry-report  window=24h
writer                 ops   fail  lock  fail%  retry_ok  hold_p95_ms  prov_io_p95_ms
tick_aggregation        24     0     0   0.0%       1          420            -
watcher              1440    18    16   1.1%       0          180            -
meme_news             144     4     4   2.8%       0          260          1900
marketops_crypto_scan  288     0     0   0.0%       0        14300         12800
malformed_lines=0
```

## Rollout slices

| Slice | Instruments | Files (likely) | Hot-path? | Test focus | Overhead | Gate | Rollback | Natural cycle? |
|---|---|---|---|---|---|---|---|---|
| **001A** primitives + file sink | tick_aggregation (+ backup reader) | +`app/telemetry/sqlite_events.py`,`sink.py`,`schema.py`; edit `tick_aggregation.py`,`backup.py` (emit only) | **no** (listeners **scoped to the instrumented sessions**, not the shared engine) | envelope, sink atomicity, isolation, overhead | <1 ms/event | Mac suite + dark EVO | revert 1 commit | recommended (observe tick-agg) |
| **001B** MarketOps + watcher | marketops_core, crypto_scan, watcher, in-cycle children | edit `marketops.py`,`crypto_scout.py`,`watcher.py`,`signal_workflow.py`,`outcomes.py`,`calibration.py`; shared-engine listener registration lands here | **yes** | parent/child, provider-in-txn, per-cycle db-wait, **cycle byte-parity (#24)** | <2 ms/cycle | 001A baseline + **≥N cycles / short soak** (not a single cycle) | revert per file | **required** |
| **001C** scheduled maintenance | retention, baseline_scanner, meme_news, tick-agg (full) | edit `retention.py`,`scanner.py`,`meme_scout.py` | no | lock-failure capture (meme hard-fails), retry | <1 ms | 001B green | revert | recommended |
| **001D** horizon + manual | crypto_horizon_observe, crypto_tape, polymarket, cross_venue, tennis_tape | edit those services | dynamic/manual | dynamic-oneshot correlation, dry-run zero-emit | <1 ms | 001C green | revert | n/a (manual) |
| **001E** reports + alerts | (read-only) | +3 report cmds, +Prometheus textfile collector, alert thresholds | no | report grouping, threshold math, text/JSON parity | none (read) | 001A–D data present | revert | no |

**Slice gating vs the readiness window.** The candidate-readiness measurement hook runs
**inside** the MarketOps cycle and its observation window closes **2026-07-30** (7-day
checkpoint 07-23, 14-day 07-30). Therefore any instrumentation that touches the MarketOps
hot path — the shared-engine listener registration and 001B's MarketOps/watcher emit-sites
— must wait until **after the 07-30 window closes**, to honor the one-variable rule
(§Baseline, T11): landing it mid-window would inject a concurrent runtime change into the
active readiness measurement. **001A is the exception**: its listeners are scoped to the
tick-aggregation and backup sessions only (never the shared engine / MarketOps), so it adds
zero code to the readiness path and may proceed after 07-23. 001B–E follow after 07-30.

**Interaction with CLI decomposition:** telemetry emit-sites live inside service modules,
not `cli.py`, so CLI decomposition (which preserves handler/session behavior) is
orthogonal — but **do not bundle** the two in one PR (§Interactions). **Interaction with
WAL:** 001A–E must land and collect a baseline **before** any WAL milestone; the
`sqlite_wal_bytes`/`sqlite_journal_bytes` gauges and `journal_mode`/`synchronous` fields
are the instruments that will later prove WAL's effect.

## Performance budget

- **Per event:** <1 ms (build dict, `json.dumps`, one `O_APPEND` write; optional deferred
  fsync). No network, no DB.
- **Per transaction:** ≤2 SQLAlchemy-event callbacks + 1 emit ≈ sub-ms.
- **Per MarketOps cycle:** <2 ms added across parent + ~6 children; target <0.2% of a
  ~5–50 s cycle.
- **JSONL growth:** ~3–5 MB/day (§Sink); ~0.1 GB/month pre-rotation.
- **CPU/memory:** negligible; thread-local op-context is a few small dicts; no buffering
  beyond the current line.
- **Filesystem sync:** default **no explicit fsync per event** (rely on page cache +
  periodic flush) to avoid adding fsync contention; a config `SQLITE_TELEMETRY_FSYNC=false`
  default. Loss window on crash = unsynced tail (acceptable, `best_effort` for the very
  last lines).

## Failure semantics

Telemetry is **`durable_but_nonblocking`** (preferred). Contract:
- **Telemetry failure must not fail the writer** — every emit is wrapped in `except
  Exception` (never a bare `except:`, which would swallow `KeyboardInterrupt`/`SystemExit`
  and interfere with daemon shutdown) that logs at debug and increments an in-process
  `telemetry_dropped` counter; the writer proceeds exactly as today.
- **Malformed telemetry must not block persistence** — serialization errors are caught;
  the DB commit already happened or proceeds independently.
- **Disk-full telemetry failure** falls back to a single stderr/journald line
  (`telemetry_sink_unavailable`) — **never a DB write** (that would add contention) and
  **never a retry loop**.
- **No infinite retry:** at most one fallback attempt (JSONL→journald/stderr), then drop.
- **No recursive emission:** the emit path and fallback path never call back into
  instrumented code; the sink module imports nothing that emits telemetry.
- **No DB write from the fallback** — hard invariant, tested.

Loss classification: raw JSONL is `durable_but_nonblocking` (survives writer/process
issues, may lose the unsynced tail on host crash); the fallback line is `best_effort`;
nothing is `mandatory` (measurement never gates the runtime).

## Testing strategy

Unit/behavioral (offline, mocked providers — per `TESTING_POLICY`): (1) success event;
(2) lock-failure event (`database_locked`); (3) retry success; (4) retry exhaustion;
(5) rollback; (6) partial progress; (7) nested parent/child correlation; (8) provider I/O
while txn dirty → `provider_io_during_transaction=true`; (9) clean txn → false; (10) commit
timing bounds; (11) negative/impossible timing **rejected** (finish<start ⇒ validation
error, not emitted); (12) sink-failure isolation (writer still succeeds); (13) disk-full
simulation (patched `write` raising `SQLITE_FULL`/`ENOSPC` → fallback, no DB write);
(14) partial JSONL line recovery (trailing partial discarded, prior lines parsed);
(15) concurrent appenders (N processes/threads append ≤4096 B lines → all lines intact,
no interleave); (16) **secret-free** event validation (no field matches the secret grep /
`DATABASE_URL`); (17) metric-label cardinality limits (only bounded label sets;
token/ticker/id rejected); (18) report grouping; (19) writer-overlap detection from
synthetic intervals; (20) free-space threshold calculation; (21) DB-growth projection;
(22) **no DB writes** by any telemetry path (assert connection unused); (23) **no provider
calls**; (24) **no MarketOps semantic change** (cycle output byte-identical with telemetry
on/off); (25) **no retry-policy change** (ladders untouched — telemetry observes, never
retries); (26) **no migration** (no schema delta); (27) **no automatic alert action**
(reports return text only); (28) AST + canonical safety audits clean (`frontier-eval-report
--include-safety` rglobs new `app/telemetry/*.py`; secret grep clean);
(29) **production-SQLite lock-contention integration test** — closes audit gap #15: a
**temporary disposable** file DB, two real connections with the app busy-timeout, one
holds a write txn while the other attempts a write past the timeout → asserts a real
`database is locked` is captured as `exception_category=database_locked` with a
`derived_estimate` `lock_wait_ms`; (30) performance-overhead benchmark (emit p99<1 ms;
cycle overhead<0.2%).

**Flaky-output normalization** (shared normalizer used by generator and asserts):
timestamps→`<TS>`, durations/`*_ms`→`<DUR>`, absolute paths→`<PATH>`, `database_bytes`/
`free_bytes`→`<BYTES>`, pids→`<PID>`, `event_id`/`parent_event_id`→`<UUID>`, host→`<HOST>`.

## Baseline protocol

**Baseline period — ≥7 days on EVO-X2, current pragmas unchanged (`delete`/`FULL`), no
WAL, no transaction-boundary change, no schedule staggering** (so the baseline reflects
today's contention). Report: operations by writer; lock-failure rate (per writer + total);
transaction-hold p50/p95/max by writer; overlap frequency (the audited critical pairs);
retry outcomes; lost scheduled work (consecutive failures); DB-size growth/day; free-space
trend. Capture the raw JSONL for the whole window (archived, not pruned during baseline).

**Post-change comparison** (after a later WAL or ownership milestone): identical
`writer_name` labels, identical report windows and comparable schedule periods (same
weekday/live-window mix), identical success definitions, and **no simultaneous unrelated
runtime change** (one variable at a time). **Minimum evidence to declare improvement:**
(a) lock-failure rate drop is statistically material over ≥7 comparable days (not a quiet
window); (b) watcher lock-victim events fall; (c) no regression in transaction-hold p95 or
commit p95; (d) no new failure category appears; (e) the change is the *only* runtime
delta in the compared window. Declaring WAL a success requires the lock-failure-rate ceiling
in the topology audit (`>1%` of write attempts) to be measured *below* threshold post-WAL.

## Interactions with pending work

- **PR #1 (`FORECAST-SCORABILITY-AUDIT-001`, ed5805e)** / **PR #2
  (`FORECAST-RELIABILITY-DECOMP-001`, b0ab073)**: both add read-only report commands, no
  writers — no telemetry emit-site. Unaffected; not modified; not retargeted.
- **CLI decomposition (`CLI_DECOMPOSITION_DESIGN_2026_07.md`)**: emit-sites are in service
  modules, not `cli.py`; orthogonal. **Telemetry must not be bundled with CLI
  decomposition** — separate PRs, separate slices.
- **Candidate-readiness measurement**: telemetry is additive and behavior-preserving to
  that lane; the readiness JSONL hook is untouched. 001A (session-scoped, off the MarketOps
  path) may proceed after the **2026-07-23** checkpoint; any MarketOps-hot-path / shared-
  engine instrumentation (001B+) waits until the readiness window **closes 2026-07-30**.
- **Future WAL milestone**: **no WAL change in the telemetry baseline deployment**;
  baseline precedes WAL and provides its validation instruments.
- **Future transaction-ownership refactor**: **no transaction-boundary change in the
  baseline deployment**; the `session_scope()`/gather-then-write work is a *later*
  milestone that telemetry will measure.
- **Runtime utility module (RUNTIME-UTIL-001)**: the unified retry helper is a *separate*
  milestone; telemetry only **observes** retries, changing no ladder.
- **Backup coordination (SQLITE-BACKUP-COORDINATION-001)**: the backup reader is
  instrumented (001A) so overlap/starvation is measured before scheduling backup.
- **No schedule staggering during baseline collection** (staggering is a coordination
  milestone that would confound the WAL comparison).

Required order: **2026-07-23 checkpoint → 001A (session-scoped, off hot path) → readiness
window closes 2026-07-30 → forecast PRs integrate → (CLI decomposition and 001B+ telemetry
proceed independently, unbundled) → telemetry baseline ≥7 d → WAL → ownership**.

This design is **measurement-only and behavior-preserving**, but it is *additive runtime
code* (engine/session listeners, service-module emit calls, provider-timing context
managers), not zero new code — "no runtime change" throughout means **no runtime
behavioral/semantic change** (byte-identical cycle output, unchanged retry ladders, no
migration), verified by tests #24–#26.

## Risk register

| ID | Risk | Sev | Control / rollback |
|---|---|---|---|
| T1 | **Recursive contention** — sink takes the SQLite lock it measures | **critical** | non-SQLite sink mandated; JSONL `O_APPEND`; test #2/#22 assert no DB write; revert |
| T2 | High-cardinality metrics explode Prometheus | high | bounded label enums only; test #17 rejects token/ticker/id labels; collector allowlist |
| T3 | Secret leakage into JSONL | **critical** | schema-only envelope; secret-scan validator; test #16; `0700`/`0600` perms |
| T4 | Telemetry disk growth unbounded | medium | size+daily rotation, 30-day retention, ≤4096 B/line; growth projection ~0.1 GB/mo |
| T5 | Measurement overhead slows the hot path | high | <1 ms/event, no fsync-per-event, swallow-all; benchmark test #30; cycle-parity test #24 |
| T6 | **Incorrect transaction-boundary inference** (mislabeled hold) | high | measurement-quality tags; `instrumented_estimate`/`unknown` where honest; never claim exact lock hold |
| T7 | Duplicate events (retry re-emit) | medium | one child event per attempt with `attempt_number`; dedup by `event_id` in reports |
| T8 | Missing events on process kill (SIGKILL mid-op) | medium | each row self-contained; open-op has no close row → report infers `unknown` outcome; no span state to corrupt |
| T9 | Misleading lock-hold estimates drive wrong WAL conclusion | high | quality tags surfaced in reports; baseline compares only `exact` commit p95 + lock-failure **rate**, not estimated hold |
| T10 | Journal mode changes mid-comparison | medium | `journal_mode`/`synchronous` recorded per event; comparison rejects windows with mixed modes |
| T11 | Multiple simultaneous runtime changes confound baseline | high | one-variable rule (§Baseline); no staggering/WAL/ownership during baseline |
| T12 | Fallback recursion (fallback re-emits telemetry) | medium | sink module imports nothing instrumented; single fallback attempt; test asserts no re-entry |
| T13 | Filesystem permission failure on sink dir | low | create `0700` dir at startup; on failure → journald fallback, writer unaffected |
| T14 | Emit adds latency inside the commit callback | medium | emit happens **after** `after_commit`/handler exit, never between `before_commit` and `after_commit` |

## Recommended first implementation slice

**SQLITE-LOCK-TELEMETRY-001A — primitives + file sink + one maintenance writer** (after
2026-07-23). It introduces the event schema, uses a **non-SQLite** sink, instruments
**one low-risk writer (tick_aggregation)** plus the **backup reader** for overlap, changes
**no transaction boundary, no retry ladder, no journal mode, no schedule**, and is
independently `git revert`-able.

- **Files added:** `app/telemetry/__init__.py`, `app/telemetry/schema.py` (envelope +
  enums + validation, incl. negative-timing rejection + secret scan),
  `app/telemetry/sink.py` (JSONL `O_APPEND`, ≤4096 B, rotation, fallback),
  `app/telemetry/sqlite_events.py` (SQLAlchemy listener registration + op-context),
  `tests/test_sqlite_telemetry_001a.py` (envelope, sink atomicity/concurrency,
  isolation, secret-free, overhead benchmark, the two-connection disposable-DB
  lock-contention integration test).
- **Files edited (emit only — no behavior change):** `app/services/tick_aggregation.py`
  (emit success/retry/commit-timing events around the existing `_commit_unit`),
  `app/services/backup.py` (emit a reader op event for overlap). Listeners are attached
  **only to the tick-aggregation/backup sessions** (via `event.listen(session, ...)` on
  those service-owned sessions), **not** the shared engine — so 001A adds no callback to
  the MarketOps hot path and can run during the 07-23→07-30 readiness window. The
  shared-engine registration is deferred to 001B (after 07-30).
- **Not touched:** `cli.py` dispatch semantics, MarketOps, crypto scan/horizon, watcher,
  provider gate, `.env` values, models, migrations, systemd units, SQLite pragmas, retry
  constants.
- **Acceptance:** full Mac suite green; new tests pass incl. the disposable-DB contention
  integration test and the overhead benchmark (emit p99<1 ms, cycle-parity byte-identical);
  AST + secret audits clean; JSONL written only under `~/probability-arena-telemetry/`
  (repo `git status` clean); `git revert` of the single commit removes all emit-sites and
  the package with zero residue; dark EVO deploy shows tick-aggregation events accumulating
  with **zero** new lock events attributable to telemetry.

Nothing above is implemented in this milestone. 001A (session-scoped, off the MarketOps
path) waits until after the 2026-07-23 candidate-readiness checkpoint; every hot-path slice
(001B+) additionally waits until the readiness observation window closes 2026-07-30, since
every writer instrumented is the frozen active runtime.
