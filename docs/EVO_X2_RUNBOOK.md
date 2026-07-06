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

Crypto Arena (CRYPTO-001) has **no service/timer** — validate with manual passes only:
`crypto-scan-once --limit 25` → `crypto-report` → `crypto-signals-recent`. The
migration (`0014`) applies on the first command. `ENABLE_CRYPTO_SCOUT` stays
false (it only reserves future loop/timer use); a crypto timer would be its own
deliberate rollout step in a later milestone. Read-only DEX Screener GETs; no
wallets/swaps/execution exist anywhere.

## Soccer evidence forecasting (SOCCER-002) rollout

One flag: `ENABLE_SOCCER_EVIDENCE_FORECASTING=true` (soccer research canary
must already be on with `provider=espn`). Validate during a World Cup window:
promote/process 1–3 soccer signals, check `signal-report` /
`research-canary-report` for `soccer_evidence` forecasts, then run
`edge-precheck --latest-marketops-run` — soccer forecasts (confidence ≥0.60)
are now measurable. Forecasts remain measurement inputs only.

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
suite (edge-policy/edge-cohort/frontier-eval/champion-challenger/db-growth/
prune-retention --dry-run); it changes no flag, gate, threshold, or live
service. Note: a cloud/routine scheduler cannot reach this private Tailscale
host — this on-host timer is the reliable mechanism.

```bash
cat ~/edge-observation/latest.log                 # newest snapshot
systemctl --user status probability-arena-edge-observation.timer
systemctl --user start probability-arena-edge-observation.service   # run now
systemctl --user disable --now probability-arena-edge-observation.timer  # stop
# fully remove: rm ~/.config/systemd/user/probability-arena-edge-observation.{service,timer} && systemctl --user daemon-reload && rm -rf ~/edge-observation
```

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

## DB backup (OPS-007)

Consistent snapshots via the sqlite3 online backup API (safe while all
services run), gzipped into `data/backups/` with `BACKUP_RETENTION_DAYS`
pruning:

```bash
cd ~/projects/probability-arena
.venv/bin/python -m app.cli backup-db
.venv/bin/python -m app.cli list-db-backups
.venv/bin/python -m app.cli verify-db-backup data/backups/backup-<stamp>.db.gz
```

Optional daily timer (NOT auto-installed; install commands in the unit file):
`infra/systemd/user/probability-arena-backup.{service,timer}`.

Restore drill: `gunzip -k backup-<stamp>.db.gz`, point a scratch
`DATABASE_URL` at the extracted file, run `db-stats` against it.
TODO (later OPS milestone): scheduled off-host copies.

## MarketOps overlap guard (OPS-007)

Concurrent cycles cannot collide anymore: a second `marketops-run-once` (or a
timer firing during a manual run) records a graceful `skipped`
(`already_running`) run instead of a SQLite lock error, and a 'running' row
older than `MARKETOPS_LOCK_STALE_AFTER_MINUTES` is treated as crashed. SQLite
connections also carry a `SQLITE_BUSY_TIMEOUT_MS` wait. Manual cycles no
longer need to dodge timer firings.
