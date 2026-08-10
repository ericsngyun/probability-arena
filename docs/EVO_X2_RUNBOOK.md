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

```bash
cd ~/projects/probability-arena
git status --short                      # must be clean; stop and report if dirty
git pull --ff-only
.venv/bin/pip install -q -r requirements-dev.txt     # if deps changed
.venv/bin/python -m app.cli run-baseline --dry-run   # applies migrations, audit-only
.venv/bin/python -m app.cli db-stats                 # sanity
# restart long-running services after code changes:
systemctl --user restart probability-arena-watcher.service
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
03/09/15/21:20 UTC, running `crypto-tape-reconcile`) on branch
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
- It is a **SQLite writer**, but not a single long transaction: the scheduled
  path commits in bounded batches (`crypto_tape_reconciler_batch_size`,
  default 25 tokens) instead of one transaction for the whole pass, and stops
  at an internal wall-clock deadline
  (`crypto_tape_reconciler_max_duration_seconds`, default 20s). A 25-token
  batch's own write phase measured well under one second at production
  density in the profiling session that set this default; the scheduled
  path's end-to-end wall-clock time on EVO-X2, with this batching in place,
  has **not yet been measured** — measure it before relying on the 20s
  deadline being generous. (Earlier, pre-fix figure for reference only, NOT
  current behaviour: one single-transaction dry run measured 105.2s for 819
  tokens on EVO-X2 — that shape is what this fix replaced.) It runs
  `Nice=10 IOWeight=20` and aborts when the latest MarketOps run errored.
  Exit code is non-zero on a refused, truncated, partial, locked, or
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
  and an actor observation). Neither table is pruned by `retention.py`. Budget
  roughly 2 MB/day at the shipped defaults and revisit retention before raising
  the cadence.

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
