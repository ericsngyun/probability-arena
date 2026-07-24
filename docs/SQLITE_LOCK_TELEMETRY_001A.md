# SQLITE-LOCK-TELEMETRY-001A — primitives + file sink + tick-aggregation/backup (2026-07-24)

First implementation slice of `docs/SQLITE_LOCK_TELEMETRY_DESIGN_2026_07.md`,
executed after the real 2026-07-23 candidate-readiness checkpoint
(`docs/CRYPTO_HORIZON_READINESS_CHECKPOINT_7D_2026_07_23.md`, PASS WITH
OPERATIONAL FINDINGS) per the design's slice gating. **Measurement only**:
telemetry observes SQLite writers; it changes no transaction boundary, retry
ladder, journal mode, synchronous mode, busy timeout, schedule, systemd unit,
`.env`, schema, model, or provider behavior, and it is never a trading
surface of any kind.

## What shipped

| Piece | File | Notes |
|---|---|---|
| Event envelope + validation | `app/telemetry/schema.py` | closed 42-field set, bounded enums, timing-quality tags, impossible-timing rejection, secret-value scan |
| Append-only JSONL sink | `app/telemetry/sink.py` | non-SQLite by mandate; `~/probability-arena-telemetry/sqlite-writes.jsonl` (env `SQLITE_TELEMETRY_DIR`); dir `0700`, file `0600`; one unbuffered `os.write` on an `O_APPEND` fd; ≤4096 B/line with optional-field truncation; short write = dropped, never resumed; reader helper skips + counts malformed/partial lines |
| Session-scoped timing primitives | `app/telemetry/sqlite_events.py` | `OpContext` + instance-level Session listeners (`after_flush`/`before_commit`/`after_commit`/`after_rollback`), provider-I/O span primitive, gauge sampling — **never attached to the shared engine or sessionmaker** |
| Tick-aggregation emit sites | `app/services/tick_aggregation.py` | parent `aggregate` event + one child `commit_unit` event per retryable unit (outcome, retry_count/limit, commit_ms `exact`, hold `instrumented_estimate`, lock-wait `derived_estimate` floor, rollback timing); dry-run emits nothing |
| Backup emit site | `app/services/backup.py` | one reader op event per real backup (success/failure, duration, `database_bytes`, `filesystem_free_bytes`, query-form `PRAGMA` samples) for writer-overlap measurement |
| Tests | `tests/test_sqlite_lock_telemetry_001a.py` | 31 tests: envelope, rejections (impossible timing, secrets, high-cardinality), sink atomicity/concurrency/permissions/line-cap, failure isolation (broken sink, disk-full, permission, short write), parity (transaction-boundary, retry-policy, tick-aggregation, backup), parent/child correlation, provider-I/O flag, no-migration, safety audits, the **two-connection disposable-DB real lock-contention integration test**, and the emit-overhead benchmark (p99 < 1 ms) |

## Failure semantics (durable_but_nonblocking)

A telemetry failure can never fail, slow, or roll back a writer: every emit
path is wrapped, validation rejects and counts bad events, a short write is
a dropped event, the fallback is a single bounded stderr/journald line
(`telemetry_sink_unavailable`) with no retry loop, no recursion, and no DB
write. Events are emitted only **after** a commit completes or terminal
failure handling finishes — never between `before_commit` and `after_commit`.

## Documented deviations from the design text (none silent)

- **Module names**: design's `sink.py`/`schema.py`/`sqlite_events.py` layout
  is used (the milestone prompt's guessed `sqlite_writes.py` name is not);
  the test file is `tests/test_sqlite_lock_telemetry_001a.py` to match the
  milestone's validation command.
- **`measurement_quality`** is realized as flat `*_quality` fields
  (`transaction_hold_quality`, `lock_wait_quality`, `commit_quality`,
  `rollback_quality`) with the design's four tiers — same information,
  simpler flat envelope, per the milestone's field list.
- **Rotation/retention are not writer-side** (design: a single owner — the
  collector/maintenance step — rotates; writers never `rename()`). 001A ships
  no rotation; at ~a few hundred events/day from these two writers, growth is
  negligible until the 001E collector lands.
- **`marketops_cycle_id`/`scanner_run_id`/`cohort_id`/`job_id`/
  `provider_io_ms_in_txn`** are pre-registered in `ALLOWED_FIELDS` (nullable,
  unused by 001A writers) so `event_version=1` denotes ONE canonical field
  set across 001A–001D and 001A-era readers never reject later lines.
- **No fsync per event** (design default); the optional
  `SQLITE_TELEMETRY_FSYNC` knob named by the design is deferred with the
  fsync code itself. Fallback is a single stderr line (journald captures
  stderr under systemd). Extra env `SQLITE_TELEMETRY_SYSTEMD_UNIT` is an
  optional attribution aid.
- **`OpContext.parent_event_id`** holds the operation's *own* pre-generated
  event id (what children point at) — single-level correlation only; 001B's
  true nesting needs a thread-local context registry, not this closure.
- Security-review hardening applied pre-deploy: `O_NOFOLLOW` on the sink
  open (a pre-planted symlink drops the event instead of appending into its
  target), `0700` enforced on pre-existing sink directories, and all four
  session-listener bodies exception-guarded. Deferred to 001B by review:
  `source_command` subcommand allowlist + entropy-based secret heuristic
  (the strict field allowlist is the primary control today).
- Tests isolate telemetry globally: an autouse `tests/conftest.py` fixture
  points `SQLITE_TELEMETRY_DIR` at a per-test temp dir, since many
  pre-existing tests exercise the two instrumented writers.

## Validation (Mac, disposable DBs + temp telemetry dirs)

- `python -m pytest -q tests/test_sqlite_lock_telemetry_001a.py` → **31 passed**.
- Full suite green (see commit body), `git diff --check` clean.
- Proven in-suite: no network imports/calls in `app/telemetry/`; no
  application-DB writes by any telemetry path (cursor-level capture);
  commit-call sequence and retry ladder byte-identical with sink healthy vs
  broken; emit p99 < 1 ms; safety grep + AST identifier audit clean; Alembic
  head remains `0027`.

## Dark deployment (EVO-X2)

Recorded in `DEPLOYMENT_REPORT_EVO_X2.md`. Summary contract: fast-forward
pull only; MarketOps untouched (no restart, no `.env`/timer change); the
telemetry directory is created only when an instrumented operation (hourly
tick-aggregation, or a manual backup) actually emits; emitted events are
schema-valid and secret-free; no telemetry-induced lock event; the next
natural MarketOps cycle and the readiness measurement continue unchanged.

Rollback: `git revert` of the 001A commits removes the package and both
emit sites with zero residue; the JSONL under `~/probability-arena-telemetry/`
is disposable measurement data (delete at will; never backed up).

## Not in this slice

MarketOps/watcher/crypto/meme/horizon instrumentation, shared-engine
listener registration, report commands, Prometheus aggregation, rotation
ownership, alerts — all deferred to 001B–001E, with every MarketOps-hot-path
piece additionally gated behind the readiness window close (2026-07-30).
