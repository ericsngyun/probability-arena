# SQLITE-WRITER-TOPOLOGY-001 — shared-database writer ownership & contention (2026-07)

Read-only architecture + operations audit at commit `2e2b86f`, grounded in code
(`file:line`) and read-only EVO-X2 host evidence. **Documentation only — no code,
config, migration, unit, `.env`, database, or service was changed; EVO-X2 stays pinned
at `3f742c9`.** Synthesis of three independent read-only specialist audits (static
writer inventory, EVO-X2 runtime evidence, database/transaction/failure-mode
correctness).

## Executive verdict

The shared SQLite database (`data/probability_arena.db`, **2.88 GB** on EVO-X2) runs in
**`journal_mode=delete` (rollback journal — NOT WAL), `synchronous=FULL`**, with a 30 s
per-connection busy-timeout in the app (`db.py:22-28`, config default 30000 ms). Under
rollback-journal, a committing writer takes a file-wide `EXCLUSIVE` lock that blocks all
other writers **and readers**; the always-connected `watcher` daemon plus timer
schedules that align multiple writers into the same minute produce measurable
contention: **59 `database is locked` events in the last 7 days** on EVO-X2 (watcher the
dominant victim; `meme-news` hard-fails its `meme_scout_runs` insert ~4×/day;
tick-aggregation self-healed one via retry).

The single largest and least-bounded write-lock hold is **`crypto_scout._scan_once_
unguarded` holding a RESERVED write lock across sequential provider network calls
(≤10 s each) with mid-loop flushes and a single terminal commit** (`crypto_scout.py:589→
697`). Precisely: the RESERVED lock is acquired at the first mid-loop flush and then held
across `risk_engine.evaluate` (`:668`) and every subsequent token's `fetch_pairs_for_token`
(`:620`) through the terminal commit (`:697`) — the first token's fetch precedes the first
flush, but the hold spans the bulk of an N-token scan. — and it runs on the **shared MarketOps session every ~5 minutes** with
`ENABLE_CRYPTO_RISK_ENGINE=true` on EVO. This is architecturally worse than the retired
49 s OPS-013 aggregation commit (that was CPU/disk-bound and one-shot; this is
network-bound and scales with cohort size), and it is the coherent cause of lock holds
exceeding even the 30 s busy-timeout.

Correctness is largely sound (idempotent upserts, batched deletes, an online-backup API,
window-aware one-shot recovery); the exposure is **latency coupling and occasional lost
scheduled work**, not corruption. The highest-leverage remediations are cheap and do not
require leaving SQLite. **All changes are deferred until after the 2026-07-23
candidate-readiness checkpoint** (the writers analyzed here are the frozen active
runtime).

## Writer inventory

Session idiom: the CLI owns the session (`owns_session = session is None`), runs
migrations, and passes it into services; **services commit the caller's shared session**
(no `session_scope` helper exists). Only three modules add an explicit lock-retry loop;
everyone else relies on the busy-timeout.

| Writer | Entry | Sched | Session | Tables written | Commit shape | Network in open txn? | Retry class |
|---|---|---|---|---|---|---|---|
| **MarketOps** `run_once` (`marketops.py:558-848`) | `marketops-run-once` | timer 5 min + manual | shared, threaded to all stages | runs/alerts + everything its sub-services write | run row `:589`; **incremental sub-commits**; final `:835`/error `:847`; rollback `:838` | only in crypto stage (see below) | `TIMER_RETRY_BY_NEXT_CYCLE` |
| ├ promote/process (`signal_workflow.py:87,199`) | (in cycle) | — | shared | signals, enrichments, assessments, packets, forecasts | short fetch→write→commit per item | no (fetch then write) | (cycle) |
| ├ outcomes `sync_ticker` (`outcomes.py:33-53`) | (in cycle) | — | shared | `market_outcomes` (upsert) | commit per ticker `:52` | fetch `:35` **then** write | `NO_RETRY` (busy-timeout) |
| ├ calibration `score_forecast` (`calibration.py:100-120`) | (in cycle) | — | shared | `forecast_scores` (append) | commit per forecast `:119` | none (local) | `NO_RETRY` |
| └ **crypto scan** `scan_once` (`crypto_scout.py:578-708`) | (in cycle) + `crypto-scan-once` | — | shared (in cycle) | crypto tokens/pairs/ticks/risk/signals/runs | run row `:589`; **single commit `:697`** | **YES — per-token `fetch_pairs` `:620` + `risk_engine.evaluate` `:668` inside the open txn** | `NO_RETRY` |
| **watcher** `watch_once` (`watcher.py:363-426`) | `watch-loop` | continuous daemon | **fresh per iteration**; sleep with no session | `watcher_runs`, `market_price_ticks` (append), `opportunity_signals` | run `:371`; batched write commit `:413` | **no** — single up-front fetch `:378` (cleanest pattern) | `TIMER_RETRY_BY_NEXT_CYCLE` |
| **tick-aggregation** `_commit_unit` (`tick_aggregation.py:359-382`) | `aggregate-market-ticks --scheduled` | timer hourly | owns | `market_price_tick_buckets` (upsert), `tick_aggregation_runs` | **per-sub-window** `:336`; apply+commit retryable unit | no (DB-only) | `BOUNDED_RETRY_SAFE` (3× re-apply) |
| **retention** `_delete_batched` (`retention.py:273-288`) | `prune-retention` | timer daily | owns | DELETE ticks/buckets/telemetry (`PROTECTED` never touched) | **commit per 5000-row batch** `:286` | no | `NO_RETRY` / next cycle |
| **scanner** `run_scan` (`scanner.py:188-237`) | `run-baseline` | timer 4 h | owns | `markets` (upsert), snapshots, `scanner_runs` | `:237` | fetch up-front `:262` | `NO_RETRY` / next cycle |
| **meme-news** `scan_once` (`meme_scout.py:312-355`) | `meme-news-run-once --scheduled` | timer 10 min | owns | `meme_scout_runs`, attention/catalyst | commit `:346` | **YES** — per-token network `:321,336` in open txn | `NO_RETRY` |
| **crypto-tape** `run_once` (`crypto_tape.py:647-790`) | `crypto-tape-run-once/session` | manual | owns | lifecycle tape (birth/outcome upsert) | single commit `:763` | **no** — DB-only (`external_calls=0`) | `BOUNDED_RETRY_SAFE` (3×; canonical `_is_db_locked:934`) |
| **horizon orchestrator** `run_job`→`observe_once` (`crypto_horizon_orchestrator.py:593`; `crypto_horizon.py:791-864`) | `crypto-horizon-run-job` | **armed one-shot** timer | shared | `crypto_horizon_observations` (upsert), alerts | commit `:864` | **YES** — per-token `fetch_pairs` `:843`, bounded by 2× retry | `BOUNDED_RETRY_SAFE` (2×) |
| **polymarket** `scan_once` (`polymarket.py:314-369`) | `polymarket-scan-once` | manual | owns | `polymarket_*` | commit `:357` | **YES** — pagination + orderbook fetch in open txn | `NO_RETRY` |
| **cross-venue** `match_once` (`cross_venue.py:966-1099`) | `cross-venue-match-once` | manual | owns | `cross_venue_*` | `:1087/1097` | **no** — DB-only | `NO_RETRY` |
| **tennis-tape** `run_once` (`tennis_tape.py:334-435`) | `tennis-*` | manual | owns | `tennis_tape_*` | `:435` | **no** — fetch up-front | `NO_RETRY` |
| **backup** `backup_database` (`backup.py:107-145`) | `backup-db` | **unscheduled on EVO** | — (reader) | none — online-backup API `:129` | N/A | N/A | reader (not a writer) |

## Runtime topology

`systemd --user` unit → ExecStart → CLI → service → session → tables. Verified on EVO-X2
(read-only), HEAD `3f742c9`, Alembic `0027`.

| Unit | State | Cadence | Command → writer |
|---|---|---|---|
| `probability-arena-marketops.timer` | **active** (enabled) | `OnUnitActiveSec=5min` | `marketops-run-once` → MarketOps cycle (+ crypto scan, outcomes, scores, readiness hook) |
| `probability-arena-watcher.service` | **active daemon** (up since Jul-04) | continuous 60 s | `watch-loop` → `market_price_ticks`, `opportunity_signals` |
| `probability-arena-meme-news.timer` | enabled | `OnUnitActiveSec=10min` | `meme-news-run-once --scheduled` → `meme_scout_runs`, attention |
| `probability-arena-tick-aggregation.timer` | enabled | `OnUnitActiveSec=1h` | `aggregate-market-ticks --scheduled --hours 12` → buckets |
| `probability-arena-baseline.timer` | enabled | `OnCalendar=00/4:00` (4 h) | `run-baseline` → scan/enrich/forecast/sync/score |
| `probability-arena-retention.timer` | enabled | `OnCalendar=daily` | `prune-retention` → batched DELETEs |
| `probability-arena-edge-observation.timer` | enabled | daily 15:00 | edge-observation (installed on host; not in repo `infra/`) |
| `probability-arena-backup.{service,timer}` | **NOT installed on EVO** | (repo units exist; daily 01:30) | `backup-db` — currently manual-only |
| horizon one-shot `probability-arena-horizon-c*-j*` | **dynamically armed** (none installed now) | one-shot `Persistent=true`, self-removing | `crypto-horizon-run-job` |

Classification: **currently active** = marketops, watcher, meme-news, tick-aggregation,
baseline, retention, edge-observation. **Installed-but-inactive/oneshot** = the timers
between firings. **Optional/default-off** = many stages gated by flags. **Manual-only** =
crypto-scan, crypto-tape, polymarket, cross-venue, tennis, backup. **Dynamically
installed one-shot** = horizon jobs.

## Transaction ownership

- **MarketOps is checkpoint-committed, not atomic.** The shared session is committed
  incrementally by each sub-service (`outcomes.py:52`, `calibration.py:119`,
  `crypto_scout.py:697`, `signal_workflow.py:250`), so a late-stage failure cannot roll
  back earlier stages — the final `rollback` (`marketops.py:838`) only discards work
  since the last sub-commit. This is *intentional* (keeps `EXCLUSIVE` bursts short) but
  means **nested sub-services commit a caller-owned transaction** (failure mode 14). The
  probability lane is safe (all short fetch→write→commit); the exposure is entirely the
  crypto scan.
- **Provider I/O inside an open write transaction** (RESERVED lock held across the
  network) — the contention hotspots, ranked: (1) `crypto_scout._scan_once_unguarded`
  (`:589→697`, per-token fetch + risk-provider calls, **on the shared marketops
  session**); (2) horizon `observe_once` (`crypto_horizon.py:→864`, bounded by 2× retry);
  (3) `meme_scout.scan_once` (`:319→346`); (4) `polymarket.scan_once` (`:323→357`).
- **Safe fetch-before-write** (no lock across network): watcher (batched up-front fetch),
  outcomes.sync, tennis-tape; and DB-only writers tick-aggregation, crypto-tape,
  cross-venue.
- **Commits inside loops**: outcomes per-ticker (`:52`), calibration per-forecast
  (`:119`), retention per-batch (`:286`), tick-aggregation per-sub-window (`:336`) — all
  deliberately small to minimize hold. **One terminal commit over a long loop**:
  crypto_scout (`:697`) and horizon observe (`:864`) — the problematic pattern.
- **Report/file writes vs DB commits**: the horizon orchestrator commits the observation
  (in `observe_once`) **before** generating reports (`orchestrator.py:686`) and uses
  atomic temp-file+replace for status (`:129-136`); the readiness JSONL append
  (`crypto_horizon_readiness.py:374`, `open("a")`) is a single-line append inside a
  swallow-all in the MarketOps hook (`marketops.py:850-857`) — not atomic but best-effort
  and isolated. Ordering is DB-first, so a report/file failure never implies a phantom
  DB row.

## Retry and lock policy

| Writer | Attempts | Backoff | Rollback→retry | Re-applies work | Error match | Class |
|---|---|---|---|---|---|---|
| tick-aggregation `_commit_unit` | 1+3 | 2.0 s | yes | **yes** (apply_fn re-invoked) | **any `OperationalError`** | `BOUNDED_RETRY_SAFE` |
| crypto-tape `run_once`/`session` | 1+2 | 3.0 s | yes | retries whole `run_once` | string `database is locked` (`_is_db_locked:934`) | `BOUNDED_RETRY_SAFE` |
| horizon orchestrator `run_job` | **1+1** | 3.0 s | yes | retries `observe_once` | `_is_db_locked` (imported) | `BOUNDED_RETRY_SAFE` (weakest) |
| MarketOps / watcher / retention / scanner / meme / polymarket / cross-venue / tennis / crypto-scan(manual) | 0 | — | — | — | — | `NO_RETRY` / `TIMER_RETRY_BY_NEXT_CYCLE` |

Three **divergent hand-rolled ladders** (3 vs 2 vs 4 total tries; 3.0 s vs 2.0 s;
string-match vs any-`OperationalError`) with **no shared constant** — a lock surfaced
with different wording is retried by aggregation but **not** by tape/orchestrator. The
most operationally sensitive path (the self-removing one-shot horizon job that cannot
re-fire on its own schedule) gets the **weakest** ladder (1 retry). No
`POTENTIALLY_UNBOUNDED` loops exist (retention's batch loop is unbounded in iterations
but each iteration commits monotonic progress → terminates). App connection busy-timeout
is 30 s (`config.py:240`); the EVO probe's 5 s reading was the probe's own Python default,
not the app's.

## Schedule-overlap findings

Rollback-journal means *any* two writers touching the file contend on one lock;
shared-table writes are not required. Severity reflects observed evidence + hold length.

| Pair | Cadence | Shared table? | Contention | Evidence |
|---|---|---|---|---|
| **MarketOps (crypto scan) × watcher** | 5 min × continuous | no (file lock) | **critical** | watcher is the dominant victim of the 59 lock events; crypto-scan long hold is the writer |
| **MarketOps × meme-news** | :00/:05/:10 × :00/:10 | no | **high** | meme-news hard-fails `meme_scout_runs` insert ~4×/day; Jul-16 22:49 watcher+meme-news collision confirmed |
| baseline × meme-news × retention | all fire in the 00:05–00:06 minute | no | **high** | schedule alignment stacks 3 writers against the always-open watcher |
| MarketOps × tick-aggregation | 5 min × hourly | no | moderate | one soft hit Jul-14 23:34 recovered via retry (`retries 1/3`) |
| MarketOps × baseline | 5 min × 4 h | markets/forecasts/outcomes | moderate | baseline 51.5 s wall (network `sync_outcomes`), few in-txn writes |
| MarketOps × horizon observe | 5 min × armed one-shot | crypto_price_ticks | moderate | both hold lock-across-network; horizon bounded by 2× retry |
| retention × tick-aggregation/watcher | daily × hourly/continuous | ticks/buckets (diff tables, same file) | low | batched deletes, short bursts; idempotent |
| **backup × any writer** | (unscheduled on EVO) × — | — | low (correctness) / starvation risk | online-backup API is consistent; residual = backup *starvation* under continuous writes |
| crypto-tape × MarketOps | manual × 5 min | none (tape tables) | low | tape is DB-only, short local hold |
| scoring/outcome-sync × MarketOps | (in cycle) | outcomes/scores | none | same session, serialized within the cycle |

## Table ownership

Append-only unless noted; "concurrent" = >1 service may write the table (contending on
the shared file). Full map from `app/models.py`.

- **Single-writer, append-only**: `market_snapshots`/`orderbook_snapshots` (scanner),
  `market_forecasts` (forecasting), `forecast_scores` (calibration),
  `market_price_ticks` (watcher — deleted only by retention), `edge_precheck_snapshots`,
  `meme_*`, `polymarket_*`, `cross_venue_*`, `tennis_tape_*`, crypto discovery
  events/ticks/signals, crypto lifecycle snapshots.
- **Single-writer, mutable-in-place (upsert)**: `markets` (scanner+enrichment upsert,
  `ticker` unique), `market_outcomes` (outcomes sync, `market_ticker` unique),
  `market_price_tick_buckets` (aggregation, unique ticker+bucket+seconds),
  `crypto_tokens`/`crypto_pairs` (scout, unique chain+addr), lifecycle
  births/outcomes (upsert), `crypto_horizon_observations` (unique cohort+token+horizon),
  cohort members (unique cohort+token).
- **Cross-writer (concurrent) — watch these**: `opportunity_signals` (watcher INSERTs +
  MarketOps promotion/processing mutate status; cooldown dedup, **no DB unique**);
  `crypto_price_ticks` (crypto_scout + horizon observe both append); `marketops_alerts`
  (MarketOps AlertService + horizon health); `markets` (scanner insert + enrichment
  upsert). All rely on busy-timeout + idempotency, not row locks (SQLite has none).
- **Run/audit tables** (`*_runs`): each single-writer, mutable status.
- **Retention deletes** high-churn tables only; `PROTECTED_TABLES` (intelligence:
  forecasts/outcomes/scores/packets/assessments) are never pruned unless explicitly
  configured (signals kept indefinitely unless `signal_days>0`).

## Historical evidence

- **59 `database is locked` events / 7 days** on EVO-X2 (journal): watcher ~20+
  (some autoflush-during-query), meme-news hard failures Jul-16 07:18/14:42/20:50/22:49
  (`INSERT INTO meme_scout_runs`), tick-aggregation 1 soft (recovered).
- Confirmed concurrent collision Jul-16 22:49:40–22:50:02 (watcher PID 1919798 +
  meme-news PID 1384335 both throw within ~20 s).
- Two persistent DB file holders: MarketOps (current run) + the watcher daemon.
- Live pragmas: `journal_mode=delete`, `synchronous=FULL`, no `-wal`/`-shm` file.
- DB file **2.88 GB** — approaching the `db_growth_critical_mb=3072` gate (`config.py:174`).
- ROADMAP: **OPS-012** full-window aggregation commit (~49 s) collided with MarketOps run
  #1215 → `PendingRollbackError`; **OPS-013** fixed it into per-sub-window commits + the
  3× re-apply retry; **OPS-014** reduced tick retention; **CRYPTO-TAPE-CADENCE-002** added
  lock-safe capture retries. So contention is a known, recurring theme already partly
  mitigated — the crypto-scan hold is the remaining large, unmitigated surface.

## Failure-mode analysis

| # | Failure | Effect | Recovery | Auto-safe? | Mitigation |
|---|---|---|---|---|---|
| 1 | two writers contend past busy-timeout | `database is locked`; losing write raises | busy-timeout + 3 ladders or next cycle | idempotent lanes: yes | shorten crypto-scan hold; unify retry; WAL |
| 2 | dies after partial commits | partial cycle persisted (not corrupt) | next cycle re-derives (idempotent) | yes | already resilient; document non-atomic contract |
| 3 | dies before final commit | open txn lost; hot `-journal` left | SQLite hot-journal recovery on next open | yes | inherent to rollback-journal; fine |
| 4 | **provider stalls with open txn** | write lock held for provider-timeout × tokens | 10 s per-call cap + stage try/except | yes | **gather provider I/O outside the write span (top fix)** |
| 5 | backup during active writer | slower/restarted copy | online-backup API keeps it **consistent** | yes | add max-duration/lease if starvation observed |
| 6 | retention deletes while aggregation reads | transient lock wait; no deadlock (single-writer) | busy-timeout; small batches | yes | keep batch size small |
| 7 | report file OK, DB commit fails | reports describe uncommitted state | `post_report_error`; ordering is DB-first | yes | acceptable (DB-then-report) |
| 8 | DB commit OK, report/JSONL file fails | file/DB divergence, no DB loss | atomic status replace; readiness append swallowed | yes | re-run report CLI |
| 9 | timer reruns after partial failure | duplicate attempt → **no duplication** (upserts keyed) | idempotent by construction | yes | solid (OPS-012 "inserted=0/updated=43,564") |
| 10 | reboot during one-shot horizon job | missed window | `Persistent=true` catch-up + DUE-NOW window guard → honest `missed` | yes (window-aware) | correct |
| 11 | WAL grows | **N/A — no WAL**; rollback-journal is bounded by txn size | hot-journal recovery | — | if WAL enabled: add `-wal` size + `wal_autocheckpoint` monitoring |
| 12 | disk near capacity | writes fail `disk full`; aborted txn | **GAP — no free-disk check** (`db_growth` only checks DB *file* size) | no | **add filesystem `statvfs` alerting** |
| 13 | duplicate process bypasses app lock | two cycles | guard is **DB-based** (`_active_run:539`) not in-process, but **non-atomic check-then-insert (TOCTOU)**, no unique constraint | mostly (busy-timeout serializes inserts) | add `UNIQUE` partial index on `MarketOpsRun(status='running')` |
| 14 | nested service commits caller session | MarketOps cycle non-atomic | intentional; per-stage try/except; idempotent re-derive | yes | document "checkpoint-committed, not atomic" contract |
| 15 | test vs prod pragma divergence | tests use in-memory `sqlite://`, no busy-timeout → **real lock contention untestable**; only synthetic `OperationalError` injection | — | — | **add one two-connection shared-file busy-timeout integration test** |

## Risk register

| ID | Risk | Evidence | Likelihood | Impact | Detect | Residual | Milestone | Defer past 07-23? |
|---|---|---|---|---|---|---|---|---|
| R1 | Crypto scan holds write lock across provider I/O on the shared 5-min cycle | crypto_scout.py:589-697; 59 lock events; EVO `ENABLE_CRYPTO_RISK_ENGINE=true` | **high** | **high** | journal `database is locked` | **high** | SQLITE-TRANSACTION-OWNERSHIP-001 | **yes (frozen hot path)** |
| R2 | Reader/writer blocking under rollback-journal (no WAL) | `journal_mode=delete`, `synchronous=FULL`, 2.88 GB; watcher victim | high | medium | lock-error rate | medium | SQLITE-WAL-HEALTH-001 | yes (Tier-3 on-disk change) |
| R3 | meme-news silently drops scheduled runs on lock | 4 hard `meme_scout_runs` INSERT failures/day, NO_RETRY | high | medium | journal + missing runs | medium | SQLITE-WRITER-GUARD-001 (shared retry) | yes |
| R4 | Divergent retry ladders; weakest on the one-shot horizon path | 3/2/4 attempts, string-vs-any match | medium | medium | code | medium | RUNTIME-UTIL-001 | yes |
| R5 | No free-disk alerting; DB at 2.88 GB vs 3.07 GB gate | db_growth.py absolute-size only; no statvfs | medium | high | none today | **high** | SQLITE-LOCK-TELEMETRY-001 (adds statvfs) | partially — add monitoring first |
| R6 | MarketOps overlap guard TOCTOU (no unique constraint) | marketops.py:539-556 non-atomic | low | medium | duplicate runs | low | SQLITE-WRITER-GUARD-001 | yes |
| R7 | Backup unscheduled on EVO; manual-only | no backup timer installed | medium | high (no recent recovery point) | none | **high** | SQLITE-BACKUP-COORDINATION-001 | assess at 07-23 |
| R8 | No real lock-contention integration test | tests use in-memory sqlite | medium | low | — | low | (part of each fix) | yes |

## Proposed future architecture (design only — not implemented)

1. **Explicit transaction ownership + short write spans.** A single `session_scope()`
   context manager in `app/db.py`; a convention that pure/analysis functions never
   commit and repository functions own their commit; and, crucially, **provider I/O
   outside the write transaction** — gather all network results first, then a short write
   burst (fixes R1, the longest hold). Applies to `crypto_scout`, horizon `observe_once`,
   `meme_scout`, `polymarket`.
2. **One standardized bounded lock-retry helper** (attempts, backoff, rollback→re-apply,
   `OperationalError`-based match) replacing the three divergent ladders (R4) — a shared
   `app/util` primitive (ties to RUNTIME-UTIL-001 from the portfolio audit).
3. **WAL** (`journal_mode=WAL`, `synchronous=NORMAL`, `wal_autocheckpoint`, raised
   busy-timeout) so readers proceed against the last checkpoint during a writer's commit
   — directly unblocks the watcher (R2). Single-host precondition holds (EVO-X2 only).
   Add `-wal`/`-shm` size + checkpoint-backlog monitoring at the same time.
4. **Writer telemetry**: per-writer lock-failure counters, commit-hold p50/p95/max,
   writer-overlap detection, and **filesystem free-space** alerting (R5) — the
   measurement baseline that justifies and validates 1–3.
5. **A DB-level overlap guard** — `UNIQUE` partial index on `MarketOpsRun(status='running')`
   (or a transaction-scoped guard) closing the TOCTOU (R6).
6. **Backup coordination**: schedule backup on EVO with a max-duration/abort + light
   lease, and **stagger timer offsets** (phase `OnUnitActiveSec` so marketops/meme-news/
   tick-aggregation/retention don't align on the same minute).

**Do not migrate to PostgreSQL merely because concurrent writers exist** — the
busy-timeout + idempotent retries already serialize them correctly, and WAL is a
one-pragma, same-engine fix for the observed reader-blocking. Migrate off SQLite **only
after WAL is enabled and instrumented and** the measured ceiling is still hit:
- lock-failure rate stays `>1%` of write attempts after WAL + retry unification;
- genuine *simultaneous* multi-writer commit is required (SQLite serializes writers even
  under WAL) and the serialized latency is unacceptable;
- DB size/write-throughput past the single-file envelope (file past ~3 GB *after*
  aggregation/retention do their job, with WAL checkpoints unable to keep up);
- repeated recovery events or unresolvable backup starvation;
- a need for concurrent connections from multiple hosts (not the case today).

## Recommended milestone sequence

1. **SQLITE-LOCK-TELEMETRY-001** — write telemetry (lock-failure counters, commit-hold
   durations, writer overlap) + filesystem free-space alerting. Measurement-only,
   additive, low-risk; establishes the baseline and validation harness for everything
   below.
2. **SQLITE-WAL-HEALTH-001** — enable WAL + `synchronous=NORMAL` + raised busy-timeout +
   checkpoint/`-wal` monitoring; backup-aware. Tier-3; gated on the telemetry baseline.
3. **SQLITE-TRANSACTION-OWNERSHIP-001** — move provider I/O outside the write span in
   `crypto_scout`/horizon/meme/polymarket (gather-then-write). Removes the longest hold.
4. **RUNTIME-UTIL-001 / SQLITE-WRITER-GUARD-001** — unify the three retry ladders into one
   shared helper; add the `MarketOpsRun(status='running')` unique partial index.
5. **SQLITE-BACKUP-COORDINATION-001** — schedule + coordinate backup on EVO; stagger timer
   offsets.

### Recommended first implementation milestone (after July 23)

**SQLITE-LOCK-TELEMETRY-001.** It is the disciplined first move: measurement-only and
additive (no on-disk/journal/pragma change, no frozen-hot-path refactor), it establishes
the lock-failure-rate and commit-hold baseline that turns the WAL and transaction-
ownership decisions from "obvious" into *measured and validated*, and it closes the most
dangerous unmonitored gap today — **no filesystem free-space alerting while the DB sits
at 2.88 GB near its 3.07 GB gate**. WAL-HEALTH-001 follows immediately once the baseline
confirms the rate. None of this is implemented now; all of it waits until after the
2026-07-23 candidate-readiness checkpoint, because every writer analyzed here is the
frozen active runtime.
