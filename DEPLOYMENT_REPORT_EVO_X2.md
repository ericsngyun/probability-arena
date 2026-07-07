# Deployment Report — Probability Arena on EVO-X2 (`mikolabs`)

Date: 2026-07-03 (UTC) · Status: **deployed, timer enabled and scheduled**
Companion: `DEPLOYMENT_AUDIT_EVO_X2.md` (Phase 1 audit and path rationale)
**Updated 2026-07-03: OPS-003 deployed — see "OPS-003 update" section.**
**Updated 2026-07-03 (later): OPS-005 + baseball canary rollout — see below.**
**Updated 2026-07-03 (later still): MVP-004G champion/challenger deployed — see final section.**

## Deployment summary

| Item | Value |
|---|---|
| Path chosen | **Host Python venv + systemd user timer** (Docker Compose rejected: would add a redundant Postgres/Redis set to an already loaded shared host) |
| Repo path | `/home/miko_node_001/projects/probability-arena` |
| Commit deployed | `d009433` (`main`; includes MVP-004D `0cd62b0`) |
| Python / venv | Python 3.12.3 → `.venv` inside the repo (created `--without-pip` + get-pip bootstrap; host lacks `python3.12-venv` and passwordless sudo — no system packages installed) |
| Database | **SQLite** at `~/projects/probability-arena/data/probability_arena.db`, Alembic at head **`0011`**, 14 tables. Not Postgres: `master-postgres` belongs to the awaas stack, publishes no host port, and pgbouncer has no db routing (see audit). |
| Redis | Default `redis://localhost:6379/0` — intentionally unreachable on this host. The baseline CLI never uses Redis; the app's only Redis use (API candidate cache) degrades gracefully to cache-miss. |
| LLM / external flags | `ENABLE_LLM_RESOLUTION=false`, `ENABLE_EXTERNAL_RESEARCH=false`, `ENABLE_LLM_FORECASTING=false` (verified in `.env`) |
| Trading surface | **None.** Safety grep on the deployed tree found no order/wallet/execution/live-or-paper-trading/sizing/EV/recommendation implementation surface. The adjacent `~/awaas/trading/` project on this host is unrelated and shares nothing with this deployment (no env, no DB, no services). |
| Timer | `probability-arena-baseline.timer` (user unit) **enabled + active (waiting)**; every 4 h (`OnCalendar=*-*-* 00/4:00:00`, `RandomizedDelaySec=300`, `Persistent=true`). Next trigger at install time: 04:04 UTC. Lingering already enabled, so the timer survives logout/reboot. No root/system units touched. |

## Verification results (all on EVO-X2)

- **Dry run** (`run-baseline --dry-run`): pipeline run 1, `status=dry_run`, 8 skipped stage audit rows, 50 ms, no downstream rows.
- **Manual live baseline** (`--scan-limit 300 --candidate-limit 8 --sync-outcome-limit 20 --score-limit 100`): pipeline run 2, `status=completed` in 18.2 s — scan 300/300, enrich 8/8, assess 8/8, research 8/8, forecast 8/8, sync 20/20, score 8/8, report ✓. Confirms Kalshi egress from EVO-X2 works (read-only GETs; no credentials, no trading permissions).
- **systemd-triggered run** (`systemctl --user start …service`): pipeline run 3 with full `.env` defaults, `status=completed` in 54.3 s (scan 500/500, 20 candidates through the chain, 200 outcomes synced, 20 new scores with the 8 already-current scores correctly skipped by dedup). Exit `0/SUCCESS`, clean journal.
- **`pipeline-status`**: lists runs 3/2/1 with correct statuses and full stage table.
- **`calibration-report`**: works; all scores currently `pending_outcome` (forecasts are on unresolved markets — expected on day one; resolved counts accrue as markets settle and later runs re-score).
- **Timer status**: `active (waiting)`, trigger scheduled, `Triggers: probability-arena-baseline.service`.

## Caveats

1. **SQLite, not Postgres.** Deliberate (see audit). If a dedicated Postgres is provisioned later, point `DATABASE_URL` at it and rerun migrations — `run_migrations()` builds from zero; note the SQLite history would need a one-off copy if continuity matters.
2. **Pre-existing failed user units** (`arena-daily.service`, `syncthing.service`) keep the user manager `degraded`. Unrelated to this deployment; not touched.
3. **The service unit is `disabled` by design** — it is activated by the enabled timer (`TriggeredBy`), which is the correct oneshot+timer shape.
4. Scores stay `pending_outcome` until markets settle; with same-day sports dominating candidates, resolved scores should appear within ~24 h of runs.
5. `.env` currently holds no secrets (no Kalshi credentials needed for read-only data; LLM disabled). If credentials are ever added, they stay in `.env` (mode 600 recommended) and must never be committed.

## Operations

Disable the schedule:
```bash
systemctl --user disable --now probability-arena-baseline.timer
```
(Re-enable: `systemctl --user enable --now probability-arena-baseline.timer`)

Inspect logs and state:
```bash
systemctl --user status probability-arena-baseline.timer
systemctl --user status probability-arena-baseline.service
journalctl --user -u probability-arena-baseline.service -n 100 --no-pager
systemctl --user list-timers | grep probability-arena
cd ~/projects/probability-arena && .venv/bin/python -m app.cli pipeline-status
cd ~/projects/probability-arena && .venv/bin/python -m app.cli calibration-report
```

Update the deployment:
```bash
cd ~/projects/probability-arena
git pull --ff-only
.venv/bin/pip install -q -r requirements-dev.txt   # if deps changed
.venv/bin/python -m app.cli run-baseline --dry-run # migrations + audit sanity
```

---

## OPS-003 update (2026-07-03): watcher + retention live

Deployed `27a4501` (OPS-002 watcher + OPS-003 retention) following the README sequence: pull → deps → migrations via dry-run (now at Alembic **`0012`**) → two manual `watch-once` passes (36 candidates, clean) → `db-stats` sanity → **retention timer installed first** → `ENABLE_REALTIME_WATCHER=true` appended to `.env` (key predated OPS-002, so it was added rather than edited) → watcher service enabled.

| Unit | State | Schedule |
|---|---|---|
| `probability-arena-baseline.timer` | active/enabled (unchanged) | every 4 h |
| `probability-arena-retention.timer` | active/enabled (**new**) | daily (defaults: ticks 7 d, watcher runs 30 d, pipeline 90 d, signals kept forever) |
| `probability-arena-watcher.service` | active/enabled (**new**) | continuous 60 s loop over the latest scan's eligible candidates |

First-minutes verification: 11 watcher runs, 396 ticks, and **6 real signals** (3 `price_move_threshold`, 3 `spread_tightened`) from live MLB/WNBA markets — e.g. `KXMLBTOTAL-26JUL021915STLATL-17` midpoint 0.10 → 0.245. Retention dry-run on the live DB counted 0 (nothing old enough yet), as expected.

**Fix applied during deployment:** the long-running watcher's `print()` summaries were block-buffered under systemd (journal showed only stderr logging; DB rows proved the loop was healthy). Added `Environment=PYTHONUNBUFFERED=1` to the watcher unit (repo + installed copy); per-pass summaries now stream to journald.

Watcher ops:
```bash
systemctl --user status probability-arena-watcher.service
journalctl --user -u probability-arena-watcher.service -n 50 --no-pager
systemctl --user disable --now probability-arena-watcher.service   # stop the loop
systemctl --user disable --now probability-arena-retention.timer   # stop daily pruning
cd ~/projects/probability-arena && .venv/bin/python -m app.cli db-stats
```

Still read-only end to end: signals are informational; no EV, trading, orders, wallets, sizing, or execution surface exists.

---

## OPS-005 + baseball canary rollout (2026-07-03, ~04:20 UTC)

Deployed **`eeb799d` → `c35e704`** (OPS-004 signal workflow, MVP-004E baseball external research, MVP-004F evidence-aware baseball forecaster, OPS-005 canon/agent-context). Alembic **`0012` → `0013`** via `run-baseline --dry-run`. `agent-context` verified on-host (phase, redacted SQLite URL, flags, boundaries, doc paths; no secrets printed).

**Flags before → after** (one-flag-discipline, appended to `.env` which predates these keys):

| Flag | Before | After |
|---|---|---|
| `ENABLE_BASEBALL_EXTERNAL_RESEARCH` | absent (false) | **true** |
| `ENABLE_BASEBALL_EVIDENCE_FORECASTING` | absent (false) | **true** |
| `ENABLE_EXTERNAL_RESEARCH` / `ENABLE_LLM_FORECASTING` / `ENABLE_LLM_RESOLUTION` | false | false (unchanged — **no global LLM/external research**) |
| `ENABLE_REALTIME_WATCHER` | true | true |

**Verification sequence and results**

- Pre-rollout data intact: 392 outcomes, 48 forecasts / 57 scores (**9 resolved — template baseline Brier 0.1471, log-loss 0.4465**), 286 signals, 7.7k ticks, 38 MiB SQLite. All three units active; watcher journal clean.
- Default-mode control (flags false): promoted+processed signal #284 → `research=template/template_only completeness=0.65`, forecaster `template_baseline`. Correct.
- Canary mode: promoted 3 MLB total signals (SD@LAD, live at 6–10 through the middle 6th); **all 3 produced `baseball-external` source-backed packets at completeness 1.00 (0 fallbacks)** with official MLB Stats API provenance (url/title/credibility/fetched_at persisted), and **all 3 forecasts used `baseball_evidence` v1** with tags `[sports_baseball, source_backed, baseball_evidence_v1, market_type_total, early_game, live_game_state, evidence_adjusted]`. Coherent ladder: line 18 p 0.70→0.7773, line 19 p 0.59→0.6471, line 20 p 0.495→0.5238 (evidence estimates 0.84/0.69/0.55), confidence 0.60 (< 0.70 cap).
- Post-rollout: watcher restarted onto new code + env, polling cleanly (0 errors); baseline/retention timers active; canary flags visible in `agent-context`; `forecasts by forecaster: baseball_evidence=3, template_baseline=49`.
- Safety grep (incl. swap/Jupiter/wallet/EV/paper/order terms): no implementation surface. Only nuanced hit: `app/services/ws_snapshots.py` — the dormant MVP-001 read-only WebSocket *market-data* client, which signs channel subscriptions with a Kalshi API key (data-feed auth, not wallet/custody); no key configured on this host, service disabled.

**Caveats**

1. EVO-X2 cannot `git push` (anonymous HTTPS clone); this report is committed/pushed from the dev machine and pulled on the host.
2. The processed SDLAD signals were slightly stale (game had progressed since signal creation) — expected; evidence reflects packet-creation time and forecasts state that.
3. `market_price_ticks` now ~7.9k rows; the daily retention timer (first firing tonight) bounds growth at the 7-day window.
4. Calibration cohorts: `baseball_evidence` forecasts now accumulate alongside `template_baseline`; comparisons need those markets to settle first.

**Rollback (baseball canary only)**

```bash
cd ~/projects/probability-arena
sed -i 's/^ENABLE_BASEBALL_EXTERNAL_RESEARCH=.*/ENABLE_BASEBALL_EXTERNAL_RESEARCH=false/' .env
sed -i 's/^ENABLE_BASEBALL_EVIDENCE_FORECASTING=.*/ENABLE_BASEBALL_EVIDENCE_FORECASTING=false/' .env
systemctl --user restart probability-arena-watcher.service
systemctl --user status probability-arena-watcher.service
```

**Log inspection**

```bash
journalctl --user -u probability-arena-watcher.service   -n 200 --no-pager
journalctl --user -u probability-arena-baseline.service  -n 100 --no-pager
journalctl --user -u probability-arena-retention.service -n 50  --no-pager
cd ~/projects/probability-arena && .venv/bin/python -m app.cli research-canary-report
cd ~/projects/probability-arena && .venv/bin/python -m app.cli calibration-report
```

---

## MVP-004G champion/challenger deployed (2026-07-03, ~04:50 UTC)

Deployed **`71dab1d` → `918b9de`** (no migration required; Alembic stays at `0013`). `agent-context` confirms the new commit; flags unchanged (baseball canaries **true**, all global LLM/external flags **false**).

**Verification on the live DB**

- Ran the standard sweep first: `sync-outcomes --limit 60` (18 settled) → `score-forecasts` (17 newly scored, 4 pending, 31 skipped).
- `champion-challenger-report --domain sports_baseball`: **baseline n=17 scored** (Brier 0.0518, log-loss 0.2208 — many easy settlements landed this pass), **challenger n=0 scored** (coverage 3, all `pending_outcome` — the SD@LAD canary markets are for the in-progress July-2 22:10 ET game and settle within hours). Paired section correctly reports "no same-market pairs yet"; the insufficient-sample warning is displayed prominently.
- `--paired-only` variant: cleanly reports n=0/n=0 with the warning — no crash, no false signal, exactly the honest low-pair behavior required.
- Optional canary refresh: promoted + processed 2 fresh MLB total signals → 2 more source-backed packets (completeness 1.00) and `baseball_evidence` forecasts. Canary totals: **5 baseball_evidence forecasts, 5/5 source-backed packets, 0 fallbacks**; `forecasts by forecaster: baseball_evidence=5, template_baseline=49`.
- Services: baseline timer active (next fire 08:03 UTC), watcher active (run 249, zero errors in last 100 lines), retention timer active (first firing tonight 00:07 UTC Jul 4). DB 39.5 MiB, ticks ~8.5k (bounded by retention window).

**Caveats**

1. Challenger scored n is still 0 — its markets simply haven't settled; the 08:00 UTC baseline run will sync outcomes and score them, which should create the **first paired samples** (both forecasters have scored the same SDLAD tickers).
2. Baseline's Brier 0.0518 on n=17 reflects easy settlements (heavily-favored outcomes); expect it to drift toward the earlier ~0.15 as harder markets resolve. Do not compare across different resolution sets — that is exactly what the paired mode is for.
3. Retention has not yet had its first firing; glance at its journal after 00:07 UTC Jul 4.

**Next recommended step:** no code work — read `champion-challenger-report --domain sports_baseball --paired-only` daily (or after each baseline run). MVP-005A's gate opens only on negative paired deltas at ≥ `early_signal` scale.

---

## SOCCER-001 soccer canary deployed + rolled out (2026-07-04, ~01:20 UTC)

Deployed **`918b9de`/`0752c75` → `e1d3b7b`** (no migration required; Alembic stays at `0013`). Watcher restarted after the pull; all three units active (baseline next fire 04:02 UTC, retention had its first firing 00:07 UTC — journal clean).

**Two-step rollout, both validated on live World Cup signals (ARG–CPV knockout):**

1. `ENABLE_SOCCER_EXTERNAL_RESEARCH=true` + `SOCCER_RESEARCH_PROVIDER=template` (dark-launch step): promoted + processed 1 live `KXWCGOAL` signal → collector `soccer-external` selected, **honest fallback** (`provider is 'template' (no live fetcher configured)`), depth `template_only`, completeness 0.65, counted as `external_fallbacks=1`.
2. `SOCCER_RESEARCH_PROVIDER=espn`: promoted + processed 2 live `KXWCGOAL` signals → **2 source-backed packets at completeness 1.00** from live ESPN data (Argentina 3–2 Cape Verde AET, red cards none, confirmed lineups, possession/shots stats; scoreboard + match-details sources persisted with credibility/freshness). `missing_info` honestly retains team-news/recent-form/conditions.

Canary report after rollout: `soccer-external n=3 (source_backed=2, template_only=1)`, baseball canary untouched (`baseball-external n=5, source_backed=5`), `by_domain sports_soccer=18`.

**Flag state on host:** baseball canaries **true** (unchanged), `ENABLE_SOCCER_EXTERNAL_RESEARCH=true`, `SOCCER_RESEARCH_PROVIDER=espn`, all global LLM/external flags **false**.

**Caveats**

1. `KXWCGOAL` player-goal markets parse with `market_type=winner` (best-effort label; extraction and evidence are unaffected — the packet is match-context evidence for the player market). A finer market-type map can ride along in a later milestone if player props get their own forecaster.
2. Soccer packets feed the **template baseline forecaster** — there is no soccer evidence-aware forecaster yet, so `source_backed` currently only raises the confidence cap, not the estimate.

**Rollback:** flip `ENABLE_SOCCER_EXTERNAL_RESEARCH=false` (or `SOCCER_RESEARCH_PROVIDER=template`) in `.env`; no restart needed for oneshot runs, restart watcher for good measure.

---

## CRYPTO-001 Crypto Arena deployed dark (2026-07-04, ~01:36 UTC)

Deployed **`f76baaa` → `9d72237`**. Migration `0014` (7 crypto tables) applied on the first CLI command. **No new service/timer** — Crypto Arena is manual-passes-only in CRYPTO-001; `.env` has no crypto keys, so all defaults apply (`ENABLE_CRYPTO_SCOUT=false`, `ENABLE_CRYPTO_RISK_PROVIDER=false` — risk signals inactive).

**Validation pass (read-only DEX Screener GETs):** `crypto-scan-once --limit 25` → status ok, 13 tokens, 25 pairs, 25 ticks, 16 signals (13 `new_pair` on genuinely fresh pairs 0.6–2.0h old, 3 `price_momentum`), 83 discovery events, 0 risk assessments (provider off), ~1.9s. `crypto-report` and `crypto-signals-recent` render correctly. Existing units unaffected (watcher active, baseline/retention timers scheduled).

**Boundary state:** read-only surveillance only — no wallet/key/swap/Jupiter/transaction/execution surface exists (safety grep clean at commit). Next steps for this lane are gated milestones: CRYPTO-002 risk engine → CRYPTO-003 paper simulator → WALLET-001 (proposal gateway only, much later).

**Note:** crypto tables grow only when scans are run manually; retention for crypto ticks/runs (7d) rides the existing daily retention timer.

---

## OPS-006 MarketOps Autopilot deployed dark + validated (2026-07-04, ~02:15 UTC)

Deployed **`7606ca6` → `b0dd1d6`**. Migration `0015` applied on first command. `ENABLE_MARKETOPS_AUTOPILOT` stays **false** (not in host `.env`); the timer is **NOT installed** per the OPS-006 acceptance criteria — cycles are manual until the operator opts in.

**Two manual cycles, both ok (~110s each, dominated by the 500-market outcome sync):**

- **Cycle #1:** 443 signals seen → 5 promoted (all baseball — source-backed domain priority working) → 5 processed, all source-backed (completeness 1.0, info alerts raised); crypto scan 37 tokens / 38 signals (spike warning raised — expected first-scan novelty, investigated and resolved); 500 outcomes synced, 21 forecasts scored; **champion/challenger jumped 0 → 8 pairs, mean_delta_brier −0.0412** (challenger ahead; still `insufficient_sample` — no conclusions).
- **Cycle #2:** 5 *different* tickers promoted (4 World Cup player-goal + 1 baseball; refresh-cooldown and one-per-ticker rules held), all 5 source-backed via soccer-external/baseball-external; crypto signals dropped to 3 (cooldowns working, no spike re-alert after resolve); no duplicate alerts; cc pair count unchanged → no repeat sample alert.

**Alert lifecycle validated live:** raise → dedupe-while-open → `marketops-resolve-alert 6` → threshold-gated non-reraise.

**To enable the 24/7 cadence later (operator decision):**

```bash
cp infra/systemd/user/probability-arena-marketops.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now probability-arena-marketops.timer
```

**Caveats**

1. Cycle duration ~110s fits the 5-min cadence, but most of it is `sync_outcomes` over 500 markets; consider `MARKETOPS_SYNC_OUTCOME_LIMIT=100` in `.env` before installing the timer (the 4h baseline already does deep syncs).
2. The autopilot promotes aggressively while World Cup/MLB games are live — the per-ticker/hour cooldowns held in testing, but watch the first timered day via `marketops-report`.
3. DB at ~190 MiB (growth driven by watcher ticks + new crypto lane; retention windows apply). `db_growth_warning` fires at 512 MiB.

---

## OPS-006 LIVE ENABLEMENT — MarketOps Autopilot timer active (2026-07-04, ~02:27 UTC)

Host commit `a1d4393` (current main; no code change in this step — flags + timer only).

**Flags before → after** (`.env`): no `MARKETOPS_*`/`ENABLE_MARKETOPS_AUTOPILOT` keys → conservative live block:
`ENABLE_MARKETOPS_AUTOPILOT=true`, `MARKETOPS_SYNC_OUTCOME_LIMIT=100` (down from default 500 — the 4h baseline still does deep syncs), `MARKETOPS_PROMOTE_LIMIT=5`, `MARKETOPS_PROCESS_LIMIT=5`, `MARKETOPS_CRYPTO_SCAN_LIMIT=100`, `MARKETOPS_SCORE_LIMIT=1000`, `MARKETOPS_INCLUDE_CRYPTO=true`, `MARKETOPS_INCLUDE_PROBABILITY_MARKETS=true`, `MARKETOPS_FAIL_FAST=false`. All other flags unchanged (baseball canaries true, soccer canary true + espn, crypto lane dark, global LLM/external false).

**Timer installed + enabled** (`probability-arena-marketops.{service,timer}` copied to `~/.config/systemd/user/`, daemon-reload, `enable --now`): active/waiting, 5-min cadence (`OnBootSec=2min`, `OnUnitActiveSec=5min`, `RandomizedDelaySec=30`).

**Cycles observed this session (all ok, ~27–28s each — sync-limit change cut duration from 110s):**

| Run | Trigger | Seen | Promoted | Processed | Crypto tok/sig | Synced | Scored | Alerts |
|---|---|---|---|---|---|---|---|---|
| #3 | timer (first firing) | 409 | 5 | 5 | 35/6 | 100 | 4 | 5 |
| #4 | manual run-once | 404 | 5 | 5 | 35/3 | 100 | 5 | 6 |
| #5 | timer (steady-state, fired 02:31:44 as scheduled) | 397 | 5 | 5 | 35/6 | 100 | 5 | 6 |

Journal clean: 0 error/traceback lines across all marketops service runs. Existing units unaffected: baseline timer (next 04:02 UTC), watcher (active, running since 01:17), retention timer (next 00:00 UTC Jul 5) — all active.

**State snapshots at enablement:**
- DB: 191.3 MiB, 44.2k market ticks, 568 opportunity signals, 147 forecasts/packets, 1311 outcomes, marketops_runs=4.
- Champion/challenger: **9 paired samples, mean_delta_brier −0.0703** (challenger ahead; `insufficient_sample` — no conclusions until ≥30 pairs).
- Crypto lane: 40 tokens, 121 pairs, 425 ticks, 66 signals across all 5 active detector types (`new_pair=38, price_momentum=12, boost_detected=10, volume_spike=4, liquidity_removed=2`); risk detectors inactive (provider off, by design).
- Open alerts: all `info` (source-backed refreshes + cc sample updates) — the autopilot is generating exactly the audit trail intended; safe to resolve in bulk during review.
- Safety grep re-run at enablement: no implementation surface for wallet/private_key/swap/signing/order/EV/paper/sizing/trade-recommendation terms (boundary docstrings only).

**Rollback (any of, least → most):**

```bash
# stop the cadence only:
systemctl --user disable --now probability-arena-marketops.timer
# and/or turn the autopilot dark again:
sed -i 's/^ENABLE_MARKETOPS_AUTOPILOT=.*/ENABLE_MARKETOPS_AUTOPILOT=false/' ~/projects/probability-arena/.env
# full removal of the units:
rm ~/.config/systemd/user/probability-arena-marketops.{service,timer} && systemctl --user daemon-reload
```

## 24-hour readiness report — TEMPLATE (fill ~2026-07-05 02:30 UTC)

Run these and record results:

```bash
cd ~/projects/probability-arena
.venv/bin/python -m app.cli marketops-report
.venv/bin/python -m app.cli marketops-alerts --limit 50 --status open
.venv/bin/python -m app.cli db-stats
.venv/bin/python -m app.cli champion-challenger-report --domain sports_baseball --paired-only
journalctl --user -u probability-arena-marketops.service --since "-24h" --no-pager | grep -cE "status=error|Traceback"
systemctl --user list-timers | grep probability
```

| Check | Target | Actual | Pass? |
|---|---|---|---|
| Cycles completed in 24h | ~288 (5-min cadence), ≥95% status ok | | |
| Cycle duration p95 | < 60s (headroom under the 5-min window) | | |
| Stage errors / provider_error alerts | 0 sustained (transient API blips acceptable) | | |
| Signals promoted per cycle | ≤ 5, distinct tickers, no ticker >1×/hour | | |
| Source-backed packet share of processed | > 50% during live game windows | | |
| Crypto signals per cycle (steady state) | < 25 (spike alert threshold) | | |
| Open warning/critical alerts | 0 unexplained | | |
| Champion/challenger paired n | growing toward 30 (`early_signal` gate) | | |
| DB growth in 24h | < 30 MiB/day (else tighten retention) | | |
| Baseline/watcher/retention units | all still active, journals clean | | |

**Decision after 24h:** all pass → leave enabled, review weekly via `marketops-report`. Any fail → apply the matching rollback above, capture the journal, and file the finding in this report before re-enabling.

---

## CRYPTO-002 risk engine deployed dark + heuristic validation (2026-07-04, ~02:50 UTC)

Deployed **`9e6be38` → `6450194`**. Migration `0016` (11 nullable risk-engine columns) applied on first command. **All CRYPTO-002 flags at defaults** (no `.env` keys): `ENABLE_CRYPTO_RISK_ENGINE=false` — MarketOps crypto scans are unchanged; the marketops timer and watcher stayed active through the deploy.

**Manual heuristic-only validation on real accumulated lane data** (`crypto-risk-assess --limit 15`, no providers, no credentials):

- 15 real tokens assessed → **10 low, 4 medium, 1 severe** — a sane distribution, not alarm spam.
- The one severe was earned: token `58E8e4Ytwixf…` ("Elgato") hit `low_liquidity` + `liquidity_removed` + `extreme_price_movement` → composite 0.75 (severe floor) → **1 `rug_risk` signal created**. That is a real liquidity-pull signature caught from persisted tick history.
- Honest gaps everywhere: every assessment carries `provider_unknown` (no providers enabled), boosts scored as context (`boosted_token` on 6 tokens without inflating severity).

**Rollout state / next steps (operator, one at a time per runbook):**
1. (done) manual `crypto-risk-assess` heuristic-only — validated above.
2. `ENABLE_CRYPTO_RISK_ENGINE=true` → MarketOps 5-min scans assess automatically.
3. `ENABLE_GOPLUS_RISK=true` (key optional) → holder/authority facts activate holder_risk / suspicious_supply_control signals; then `ENABLE_SOLANA_TRACKER_RISK` separately.

**Boundary:** risk output is avoid/flag intelligence for review — never a trade direction. Safety grep clean at commit; API keys (none set) are header-only and never printed (`agent-context` redaction is unit-tested).

---

## CRYPTO-002 LIVE ENABLEMENT — heuristic-only risk engine on (2026-07-04, ~03:42 UTC)

Host commit `0e613b1` (current main; flags-only change — no code deployed in this step).

**Flags before → after** (`.env`): no CRYPTO-002 keys → `ENABLE_CRYPTO_RISK_ENGINE=true` with providers explicitly pinned off (`ENABLE_GOPLUS_RISK=false`, `ENABLE_SOLANA_TRACKER_RISK=false`, `ENABLE_RUGCHECK_RISK=false`); **no API keys added, none printed**. MarketOps is a timer-triggered oneshot, so no restart was needed — each firing reads `.env` fresh.

**Manual validation cycle (run #17, engine on):** ok in 28.1s — 277 signals seen, 5 promoted/processed, crypto scan 34 tokens → **34 automatic risk assessments** (heuristics add negligible cycle time), 3 crypto signals, 100 synced, 4 scored, 0 new alerts.

**Risk state after enablement:**
- `crypto-risk-report`: **engine=heuristic-only (v1), providers=none**; 49 assessments across 39 tokens; latest-per-token levels **low=21, medium=18** (+ the earlier manual severe superseded); common reasons: `provider_unknown=39` (honest — no providers), `low_liquidity=19`, `fake_volume_suspected=12`, `boosted_token=8`, `extreme_price_movement=2`.
- **Risk signals: still exactly 1 `rug_risk`** (the Elgato liquidity pull from the manual smoke) — zero false fires from the automatic assessments; `holder_risk`/`suspicious_supply_control` correctly inactive without provider holder/authority data.
- **Zero provider errors** (GoPlus/SolanaTracker/RugCheck disabled — never attempted).
- Journal: 0 error/traceback lines; all four units active; DB 199.3 MiB (crypto lane now 1.7k ticks, 1.2k discovery events under the 7-day crypto retention window).

**First scheduled timer cycle with engine on (run #18, fired 03:44):** ok in 29.6s — 34 tokens scanned and auto-assessed (assessments 49 → 83), latest-per-token levels stable (low=21/medium=18), still exactly 1 rug_risk, 0 alerts. Cadence headroom intact.

**Rollback:** `sed -i 's/^ENABLE_CRYPTO_RISK_ENGINE=.*/ENABLE_CRYPTO_RISK_ENGINE=false/' ~/projects/probability-arena/.env` (providers already false), then `marketops-run-once` or wait one timer firing; verify with `crypto-risk-report` (mode returns to disabled; manual `crypto-risk-assess` remains available).

**Caveats**
1. Heuristic-only mode cannot see holder concentration or mint/freeze authority — `holder_risk`/`suspicious_supply_control` stay dormant until a provider flag is enabled (next rollout step, one at a time, keys optional).
2. Assessment volume ≈ tokens-per-scan (~35) per 5-min cycle ≈ 10k rows/day; assessments are audit history (not pruned by design) — watch table growth in the 24h readiness review and consider a retention window for them in a later OPS milestone if needed.
3. `provider_unknown` in every reason list is by design (honest absence), not an error.

---

## CRYPTO-002B — GoPlus provider rollout, provider-backed mode live (2026-07-04, ~04:15 UTC)

Host commit `df81a17` (flags-only change). **Flags before → after:** `ENABLE_GOPLUS_RISK false → true`; unchanged: `ENABLE_CRYPTO_RISK_ENGINE=true`, `ENABLE_SOLANA_TRACKER_RISK=false`, `ENABLE_RUGCHECK_RISK=false`. **No API key added — GoPlus works keyless at current volume** (~34 sequential lookups/scan); no secrets exist or were printed.

**Pre-rollout state (heuristic-only had kept working autonomously):** 250 assessments / 45 tokens, low=22 / medium=21 / severe=2, `rug_risk=3` — the engine had caught a **second real liquidity pull** (`EcJKubCHMXYB…`) unattended overnight.

**Manual provider-backed batch (`crypto-risk-assess --limit 20`):** 20/20 GoPlus reads succeeded, 0 errors. `provider_unknown` disappeared from all 20 reason lists; **authority facts went live** (mint/freeze authority verified *disabled* on all 20 — honest clean reads, `authority_risk_score=0.0`). Two active liquidity-pull tokens (SQUAD, Pepe/EcJK…) re-confirmed severe. 0 new risk signals (nothing warranted one).

**Manual MarketOps cycle (run #24):** ok in 37.4s (GoPlus adds ~8s/cycle; ~4.3min headroom). 33 tokens scanned → all auto-assessed; **goplus=31 tokens with provider data, provider_errors=3** (rate-limit/unknown-token misses recorded per assessment; heuristics covered those tokens — exactly the designed degradation). First scheduled cycle after (run #25, fired 04:20): ok in 38.5s.

**Risk state after:** engine=**provider-backed (goplus)**; 303 assessments / 49 tokens; latest-per-token levels low=40 / medium=9 / severe=0. Signal counts unchanged and honest: `rug_risk=3`, `holder_risk=0`, `suspicious_supply_control=0` — zero false fires; severe/high did not explode (it *tightened*: provider corroboration dilutes unknown-risk weight for clean tokens).

**DB:** 207.7 MiB, 303 risk assessments, 24 marketops runs. Journal: 0 errors across 200 lines. All four units active.

**Caveats**
1. **GoPlus Solana payloads did not include a parseable top-10 holder rate** for the assessed tokens — authority checks are live, but the holder-concentration dimension (and therefore `holder_risk`) stays data-dormant until GoPlus returns holder rates or SolanaTracker (which exposes sniper/insider/bundler/top10 percentages) is enabled as its own rollout step.
2. Transition categories (`liquidity_removed`) are point-in-time: a token that already rugged reassesses lower later because the drop is no longer *observed between ticks*. The severe assessment + `rug_risk` signal remain the durable audit record — read history, not just latest, when reviewing.
3. ~3 provider misses per ~34-token scan at keyless volume; acceptable and self-healing. If miss rate grows, a GOPLUS_API_KEY can be added to `.env` (header-only, never printed) without any code change.

**Rollback:** `sed -i 's/^ENABLE_GOPLUS_RISK=.*/ENABLE_GOPLUS_RISK=false/' ~/projects/probability-arena/.env` (engine stays on, heuristic-only), then `marketops-run-once` or wait one firing; verify `crypto-risk-report` shows heuristic-only.

---

## CRYPTO-002C — SolanaTracker rollout attempted: requires API key; degraded path validated, flag reverted (2026-07-04, ~04:30 UTC)

Host commit `ad79fde` (flags-only session). **Shell/process inspection first** (per ops request): the only probability-arena process on the host is the systemd watcher loop (PID 292836, healthy). All other python/shell processes belong to unrelated projects on this shared host (awaas stack, published http.server, one long-lived interactive bash) — untouched per AGENTS.md. The previously reported "2 shells" were local session poll-waiters, already exited. **Nothing killed.**

**Flags:** `ENABLE_SOLANA_TRACKER_RISK false → true → false` (reverted, see below). Unchanged throughout: `ENABLE_CRYPTO_RISK_ENGINE=true`, `ENABLE_GOPLUS_RISK=true`, `ENABLE_RUGCHECK_RISK=false`. No API keys added or printed.

**Result: SolanaTracker's data API requires an `x-api-key` — keyless is a hard 0%** (HTTPStatusError on every call: 0/20 manual batch, 0/34 at scan volume; every miss recorded per-assessment as `provider_errors: {'solana-tracker': 'no usable data'}`). Per the rollout rule ("do not invent a workaround"), no key was fabricated.

**Degraded path fully validated before reverting:**
- Manual batch (20 tokens): completed normally on GoPlus + heuristics; 0 unwarranted signals.
- Scheduled cycle #27 at full scan volume with the failing provider: **ok in 41.2s** (fast 401s add ~3s vs GoPlus-only); manual cycle #28 ok in 42.9s. MarketOps never failed; risk levels stable (low=43/medium=9, `rug_risk=3`, holder/supply still 0 false fires).
- Reverted `ENABLE_SOLANA_TRACKER_RISK=false`: mode back to provider-backed (goplus), keeping a permanently-failing provider off saves ~34 futile calls/cycle.

**Incident noted (unrelated to SolanaTracker):** one manual `marketops-run-once` at 04:32 crashed CLI-side with `sqlite3.OperationalError: database is locked` — it collided with the concurrently-firing scheduled timer cycle at the initial run-row INSERT (before any stage ran; the scheduled cycle won the lock and completed ok; no service-side errors; no data loss). **Operational guidance:** run manual cycles between timer firings (check `systemctl --user list-timers`). **Follow-up candidate for a future OPS milestone:** add a SQLite `busy_timeout`/overlap guard to marketops runs, mirroring the baseline pipeline's overlap lock.

**DB:** 458 risk assessments, ~208 MiB. All four units active; service journal error-free.

**To enable SolanaTracker later:** provision a key into `.env` as `SOLANA_TRACKER_API_KEY=…` (mode 600, never committed/printed), then flip `ENABLE_SOLANA_TRACKER_RISK=true` — no code change needed. Until then, `holder_risk`/`suspicious_supply_control` remain data-dormant (GoPlus supplies authority facts but no holder rates for these tokens).

**Rollback state:** already applied (flag false); GoPlus-backed mode confirmed post-revert via a 3-token assess.

---

## OPS-007 deployed + validated: overlap guard, busy timeout, backups (2026-07-04, ~05:03 UTC)

Deployed **`e2d8ae9` → `a1d4ff6`** (no migration; code + config defaults only — no `.env` changes needed, all OPS-007 knobs use defaults).

**Gate check performed first (Phase 1):** baseball champion/challenger **paired n=29** (threshold 30), `d_brier=−0.0222`, `d_log_loss=−0.0627`, wins 9 / losses 3 / ties 17. **MVP-005A not yet formally unlocked — one paired settlement away.** Deltas are negative on both metrics; if the next settlement keeps them negative at n≥30, the design-review gate opens.

**Overlap guard validated live by recreating the original collision:** started the timer service manually, then ran `marketops-run-once` 3s later — result: `marketops run #35: skipped (already_running, active run #34)`, exit 0, while run #34 completed ok in 39.3s. The exact scenario that previously crashed with `sqlite3.OperationalError: database is locked` is now a graceful skip. SQLite connections additionally carry a 30s busy timeout (`SQLITE_BUSY_TIMEOUT_MS`).

**First live backup:** `backup-db` → `data/backups/backup-20260704T050258Z.db.gz` (209 MiB DB → **15.38 MiB** gzip via the sqlite3 online backup API, taken while all services ran); `verify-db-backup` → OK (26 tables, integrity ok). Retention 30d. Daily timer artifacts exist (`probability-arena-backup.{service,timer}`) but are **not installed** — install commands in the unit file when wanted.

All four units active; journal clean. Tests at OPS-007: 515 passing.

**Rollback:** none needed for the guard/timeout (pure hardening, no behavior change on the happy path); backups are additive. To disable backups just don't install the timer; to loosen the lock, raise `MARKETOPS_LOCK_STALE_AFTER_MINUTES`.

---

## MVP-005A deployed dark — edge precheck measurement layer (2026-07-04, ~05:30 UTC)

Deployed **`19370c2`… → `1bd134a`**. Migration `0017` (edge_precheck_snapshots) applied on first command. **All flags at defaults** (`ENABLE_EDGE_PRECHECK=false`, `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`) — autopilot behavior unchanged.

**Force-readonly measurement pass on live data (25 forecasts):** the honest-invalidation design worked exactly as intended — **all 25 snapshots invalid, zero watchlist, zero candidates**: `invalid_not_source_backed=20` (template forecasts), `invalid_stale_forecast=4`, `invalid_stale_market_snapshot=1` as primary statuses, with every failing check collected (`stale_market_snapshot=25`, `low_confidence=25`, `wide_spread=17`, `low_liquidity=13` among reasons — measured after the night's games ended, when quotes are stale and books thin). Mean |gap| 0.028, largest +0.095 — correctly rejected for validity. **The layer refuses to manufacture edge from bad inputs, which is its whole job.**

**Rollout ladder (per runbook):** (1) done — dark deploy + force-readonly pass; (2) flip `ENABLE_EDGE_PRECHECK=true` when ready for on-demand measurement; (3) much later, `MARKETOPS_INCLUDE_EDGE_PRECHECK=true` for a measurement pass per 5-min cycle (double-gated). The interesting data will come from measuring **during live games**, when source-backed forecasts are fresh and the watcher quotes are seconds old.

**Boundary:** measurement only — no dollar EV, no sides, sizes, orders, wallets, or execution; `paper_candidate_later` is a review label with zero behavior. Safety grep (incl. word-boundary buy/sell/bet sweep) clean at commit. Tests: 549 passing.

---

## MVP-005A LIVE ENABLEMENT — manual edge precheck on; honest invalidation on live data (2026-07-04, ~06:20 UTC)

Host commit `1f0639a` (flags-only change). **Flags before → after:** no EDGE keys → `ENABLE_EDGE_PRECHECK=true`, `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` (explicitly pinned — **MarketOps integration remains disabled**).

**Process inspection (again requested):** only the systemd watcher loop (PID 292836) runs on the host; "2 shells" were local session poll-waiters, exited. Nothing killed.

**Manual measurement sequence (3 passes, ~06:12/06:14/06:16 UTC, during live late-night MLB — MIL–AZ, MIA–ATH, TOR–SEA in late innings):**

- Pass timing matters and the mechanics work: measuring **seconds after autopilot cycle #47**, the 5 just-refreshed tickers passed *source-backed* (✓ 0.65 confidence ✓), *forecast age 47–51s* (✓ under the 300s sports limit), and *quote age 26s* (✓ under 120s) — the four checks that failed on stale data earlier in the night.
- They failed **only** on `invalid_wide_spread` + `invalid_low_liquidity`: at ~2am ET in late innings, the deciding books are one-sided/empty (`spread=None, liquidity=0`). That is a true statement about the market, not a defect — the layer refuses to compute a gap against a midpoint that doesn't exist.
- 175 total snapshots; statuses: `invalid_not_source_backed=101`, `invalid_stale_forecast=62`, `invalid_stale_market_snapshot=7`, `invalid_wide_spread=5`. **Watchlist=0, paper_candidate_later=0** — zero manufactured edge. Reason frequencies: stale_snapshot=151, low_confidence=139, stale_forecast=138, wide_spread=128, low_liquidity=103, not_source_backed=101 (all failures collected per row).
- Persistence behaved correctly: invalid rows never accrue a streak (all persist=1). Valid-row persistence needs a live two-sided-book window (unit-tested; live validation pending prime hours).

**Champion/challenger (unchanged):** paired n=36, d_brier −0.0493, d_log_loss −0.1525. **DB:** 235.2 MiB, 175 edge snapshots, 1 backup (15.4 MiB). All four units active; journal clean.

**Caveats / next observation window:**
1. The valid-measurement window per ticker is the ~5 minutes after its autopilot refresh (300s sports staleness) once per hour (ticker refresh cooldown) — and requires a two-sided book. **Prime windows: World Cup afternoon UTC and MLB evening ET**, when books are active. Run `edge-precheck --limit 50` a few times, minutes apart, during those windows to observe the first valid watchlist rows and live persistence.
2. Late-night measurements will be dominated by microstructure invalidations — expected, and useful evidence that thresholds are doing their job.
3. Snapshot volume is manual-only for now (~50/run); no retention pressure yet.

**Rollback:** `sed -i 's/^ENABLE_EDGE_PRECHECK=.*/ENABLE_EDGE_PRECHECK=false/' ~/projects/probability-arena/.env` (MarketOps key already false); verify with `edge-precheck-report` and service statuses.

---

## MVP-005A.1 deployed — targeted edge-precheck modes (2026-07-04, ~06:50 UTC)

Deployed **`fa0ac34` → `5324046`** (no migration; no flag changes — `ENABLE_EDGE_PRECHECK=true`, `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` unchanged).

**Live validation of the cycle-targeted mode:** `edge-precheck --latest-marketops-run` measured exactly **4** forecasts (the latest cycle's refreshed, source-backed ones) instead of a 50-row sweep — all honestly invalid (`stale_forecast` + one-sided books; measured minutes after the cycle on ended games). Signal-to-noise is the point: targeted runs produce only rows about the cycle's actual work.

**Usage guidance now in the runbook:** during prime live windows (World Cup afternoon UTC / MLB evening ET), run `edge-precheck --latest-marketops-run` within ~2 minutes of a cycle finishing. The MarketOps stage, if ever enabled, is now strictly cycle-scoped (≤5 forecasts/cycle) — the sweep-noise concern that kept it off is resolved, but it stays off pending manual live-window sessions with sane watchlist behavior.

---

## MVP-005A.1 validation session — mechanics verified; prime window not yet reached (2026-07-04, 06:41–06:55 UTC)

Host commit `bd1a4c7`; flags confirmed `ENABLE_EDGE_PRECHECK=true`, `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` (unchanged this session — **no flags touched**).

**Session timing caveat, stated up front:** 06:41–06:55 UTC = ~2:45am ET — *past* the prime window. The night's MLB games had just ended (MIL–AZ's `newly_two_sided` signals were transient flickers; direct tick inspection showed every book one-sided: `bid=None, ask=100¢, liquidity=0`). No live liquid market existed anywhere during the session, so **valid watchlist rows were structurally impossible** — and correctly, none were manufactured.

**Two cycle-scoped sessions run (each seconds after an autopilot cycle):**

| Cycle | Run | Targeted | Source-backed | Result |
|---|---|---|---|---|
| 1 (06:46) | #52 | 1 (WC player-goal, yesterday's match) | 1 | invalid: stale_snapshot + low_confidence + wide_spread + low_liquidity |
| 2 (06:52) | #53 | 2 (TOR–SEA player-HR, game over) | 2 | invalid: stale_snapshot (+ one-sided book reasons) |
| 2-rerun (immediate) | #53 | **0** | — | **dedupe window validated live** (both skipped, measured <120s ago) |

- Watchlist=0, paper_candidate_later=0, persistence all =1 (invalid rows never accrue streaks — correct).
- Cumulative: 182 snapshots, statuses/reasons consistent with honest invalidation throughout.
- **Structural finding:** soccer source-backed forecasts fail `invalid_low_confidence` — they come from the template baseline forecaster (no soccer evidence-aware forecaster exists), whose confidence sits below the 0.60 gate. Until SOCCER-002-style evidence forecasting exists (or thresholds are deliberately retuned), **valid watchlist rows can only come from live-MLB windows via `baseball_evidence` forecasts (conf 0.65)**.

**Services:** all four active; marketops journal 0 errors. Safety greps clean (boundary docstrings only).

**Recommendation: KEEP `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`.** All mechanics are now live-validated (cycle targeting, source-backed filtering, dedupe, honest invalidation, persistence hygiene) — but the acceptance bar ("valid watchlist rows with sane persistence in a prime window") is unmet because no prime window occurred during the session. **Next session (operator or agent):** during World Cup afternoon (~14:00–22:00 UTC today) or MLB evening (~23:00 UTC+), run 2–3 times, minutes apart:

```bash
cd ~/projects/probability-arena
journalctl --user -u probability-arena-marketops.service -n 3 --no-pager | grep "marketops run"   # wait for a cycle
.venv/bin/python -m app.cli edge-precheck --latest-marketops-run    # within ~2 min of it
.venv/bin/python -m app.cli edge-precheck-report
```

If watchlist rows appear with correct persistence accrual, cycle-scoped automation (≤5 rows/cycle) can be enabled as a one-flag step. **No change and no rollback required from this session.**

---

## SOCCER-002 deployed dark — soccer evidence-aware forecaster (2026-07-04, ~07:20 UTC)

Deployed **`bd47715` → `2d2cf10`** (no migration; `ENABLE_SOCCER_EVIDENCE_FORECASTING` not in host `.env` — defaults false, behavior unchanged). MarketOps timer and watcher stayed active through the deploy.

**Why this matters for the measurement track:** soccer source-backed packets previously fed the template baseline (confidence < 0.60), so World Cup markets could never pass edge-precheck. `soccer_evidence` forecasts carry 0.65–0.70 confidence — once the flag is flipped, World Cup windows become measurable.

**Rollout (one flag, during a World Cup window — next window ~14:00 UTC today):**
1. `ENABLE_SOCCER_EVIDENCE_FORECASTING=true` in `.env` (soccer research canary already on with `espn`).
2. Let the autopilot process 1–3 live soccer signals; verify `soccer_evidence` in `research-canary-report` forecaster breakdown.
3. `edge-precheck --latest-marketops-run` within ~2 min of a cycle — first measurable soccer watchlist rows.
4. As outcomes settle: `champion-challenger-report --domain sports_soccer --challenger soccer_evidence_v1`.

Boundary restated: forecasts are measurement inputs only — no dollar EV, no advice, no actions. Tests at SOCCER-002: 583 passing; safety greps clean.

---

## SOCCER-002 LIVE ENABLEMENT — flag on; pipeline validated end-to-end; watchlist validation scheduled for today's matches (2026-07-04, ~07:15 UTC)

Host commit `ebb560a` (flags-only change). **Flags before → after:** `ENABLE_SOCCER_EVIDENCE_FORECASTING` absent → **true**. Confirmed unchanged: `ENABLE_SOCCER_EXTERNAL_RESEARCH=true`, `SOCCER_RESEARCH_PROVIDER=espn`, `ENABLE_EDGE_PRECHECK=true`, `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`. Nothing forbidden enabled.

**Session timing, honestly:** 07:15 UTC is a dead zone — zero promotable soccer signals remain (the autopilot consumed yesterday's within its cooldowns) and no live books exist. Full watchlist validation therefore could not run; the **pipeline itself was validated end-to-end live** instead:

- **Forecaster selection live:** a real source-backed soccer packet (`KXWCGOAL-…ARGNGONZA11-1`, completeness 1.0) put through `ForecastingService` selected `soccer_evidence` (forecast #428 persisted — forecaster breakdown now shows `soccer_evidence=1`).
- **Player-goal conservatism live:** the market is a player-goal type, and the forecaster correctly refused to price it from team data — internal fallback with `market_type_player_goal` tag, confidence 0.5, anchored to mid. Exactly the designed behavior.
- **Edge-precheck measured the soccer_evidence forecast** (explicit `--forecast-id` mode): honestly invalid — `invalid_stale_market_snapshot` + low_confidence/wide_spread/low_liquidity (yesterday's finished match, dead book). Watchlist=0, candidates=0, persistence=1 — no manufactured edge.

**Today's World Cup window (from the live ESPN scoreboard): CAN–MAR 17:00 UTC and PAR–FRA 21:00 UTC.** Validation runbook for that window (operator or agent): during a live half, wait for an autopilot cycle to finish, then within ~2 minutes run `edge-precheck --latest-marketops-run`, 2–3 times minutes apart. Winner/total soccer markets processed in those cycles should now produce `soccer_evidence` forecasts at 0.65 confidence → the first valid soccer watchlist rows if books are two-sided.

**Recommendation: keep `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`** until the 17:00/21:00 UTC sessions produce watchlist rows with sane persistence. Everything upstream of that is now proven live.

**Rollback:** `sed -i 's/^ENABLE_SOCCER_EVIDENCE_FORECASTING=.*/ENABLE_SOCCER_EVIDENCE_FORECASTING=false/' ~/projects/probability-arena/.env` — soccer reverts to template-baseline forecasts; nothing else changes.

Safety greps clean (boundary docstrings only). All four services active throughout.

---

## EVAL-001 deployed — first live frontier evaluation (2026-07-04, ~07:50 UTC)

Deployed **`b928a24` → `57e8369`**; migration `0018` applied on first command; `--save-run` persisted eval run #1. No flags (EVAL-001 is always-available read-only evaluation).

**Verdict: `not_ready` — exactly the conservative call the design requires** (0 watchlist rows in 24h; 187 gap measurements, all honestly invalid, invalid_explainable_rate=1.0). The harness refused to inflate anything.

**Real findings from the first report (this is why EVAL-001 exists):**
1. **Champion/challenger window view:** baseball paired n=36, d_brier −0.0493 (unpaired in-window: baseline 0.165 vs challenger 0.092 Brier). The `soccer_evidence_v1` cohort has begun: paired n=1.
2. **Microstructure by domain:** two-sided rates — general 98%, baseball 79.6%, soccer 69.7%, tennis 42.8%. Sports books are the hard part; the spread p50 is only 2¢ where books exist.
3. **Latency:** MarketOps p50 38.3s / p90 42.0s (**under the 60s automation threshold**) / p99 108.5s (the SolanaTracker-attempt cycles). Watcher tick age 22s.
4. **Signal freshness insight:** signal age at promotion p50 ≈ **5 hours** — the autopilot's 24h promotion window plus per-ticker cooldowns mean it often promotes stale signals, which then produce forecasts for already-moved game states. **Tuning candidate: tighten MARKETOPS_MAX_SIGNAL_AGE_HOURS (e.g. 2–4h) so fresh forecasts chase fresh signals.**
5. **Crypto insight:** post-risk-signal liquidity change averages **+40%** across 24 samples — liquidity often *returns* after `liquidity_removed` fires (pull/re-add patterns), a CRYPTO-002 threshold-tuning datapoint. Provider error rate 19.3% (GoPlus keyless misses + the SolanaTracker window).

All four services active; journal clean. Tests at EVAL-001: 606 passing; AST safety scan clean across 47 app files (live, part of the report).

---

## OPS-008 — signal freshness tuning applied (2026-07-04, ~08:00 UTC)

**EVAL-001 finding applied:** signal age at promotion had p50 ≈ 5 hours — the autopilot's 24h promotion window meant it routinely promoted stale signals whose game states had already moved, producing forecasts that could never pass edge-precheck freshness.

**Flag before → after:** `MARKETOPS_MAX_SIGNAL_AGE_HOURS` default 24 → **1** (config is integer-typed, so the optional 0.5 variant is not supported — 1h is the floor without a code change). Unchanged and verified: `ENABLE_EDGE_PRECHECK=true`, `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`, `ENABLE_SOCCER_EVIDENCE_FORECASTING=true`, all crypto/safety flags.

**Validation (dead-zone hour, ~4am ET — which is itself the proof):**
- Manual cycle #66 and scheduled cycle #68: `signals seen=0, promoted=0, processed=0` — the 1h window correctly **starves promotion of stale signals** (previous cycles were promoting 5/cycle from a pool of 150–380 stale ones). Crypto lane unaffected (scans/sync/score normal); durations 34–38s.
- `edge-precheck --latest-marketops-run`: 0 targeted — no noise rows created from nothing.
- 6h frontier report: `not_ready` (correct), MarketOps p90 42s, all services active, no journal errors.

**Expected effect in live windows (CAN–MAR 17:00 UTC / PAR–FRA 21:00 UTC):** signals promoted will be <1h old (typically minutes — the watcher emits them within 60s of a move), so refreshed forecasts describe *current* game state and can pass the 300s live-sports freshness gate at measurement time. The scheduled 17:17 UTC validation session will observe this directly.

**Caveats:** (1) during quiet hours the probability lane now idles — by design; the `no_recent_signals` health alert may fire on long dead stretches with the watcher running (informational); (2) if live-window sessions show the 1h window is still too loose (or too tight for slower markets), the knob is one line in `.env`.

**Rollback:** `sed -i 's/^MARKETOPS_MAX_SIGNAL_AGE_HOURS=.*/MARKETOPS_MAX_SIGNAL_AGE_HOURS=24/' ~/projects/probability-arena/.env` (or remove the key).

---

## OPS-009 deployed — promotion quality: minute windows + readiness scoring (2026-07-04, ~08:41 UTC)

Deployed **`35890e9` → `7746ef9`** (no migration; no `.env` changes — the new minute knobs use defaults: sports 20m, general 60m, with the existing `MARKETOPS_MAX_SIGNAL_AGE_HOURS=1` surviving as a coarse upper bound, so nothing got looser).

**What changed in promotion:** candidates now pass DOMAIN-specific minute windows (baseball/soccer/live-sports 20m, general 60m) and are ordered by a deterministic **measurement-readiness score** — freshness, source-backed capability, market-type measurability (player props lowest), signal-type priority, live book quality vs the edge-precheck thresholds. The score orders promotion only; it is never an EV/value/trade quantity. Run summaries now record promoted ages, domain/market-type/signal-type breakdowns, skipped-stale and unmeasurable counts.

**Quiet-window validation (cycle #75, ~4:40am ET):** `signals seen=0, promoted=0, skipped_stale=0` — no signal in the whole DB is fresher than an hour at this dead hour, so the probability lane idles with **zero stale promotions and zero edge-precheck noise** (0 rows via `--latest-marketops-run`). Crypto/sync/score lanes normal; 33.7s duration; all four units active. The `promotion (OPS-009)` line renders in `marketops-report`.

**Live-window expectations (CAN–MAR 17:00 UTC / PAR–FRA 21:00 UTC + MLB tonight):** promoted ages should drop from the pre-OPS-008 ~5h / pre-OPS-009 ~67min p50 to **minutes** (the watcher emits within 60s of a move); promoted mix should skew to spread/total/winner markets on fresh two-sided books — precisely the ones edge-precheck can validate. The scheduled 17:17 UTC session measures this directly; `marketops-report`'s promotion line and `frontier-eval-report`'s `signal_age_at_promotion_s_p50` are the before/after evidence.

**Champion/challenger meanwhile:** paired n=44, d_brier −0.0498, d_log_loss −0.1448 (`early_signal`) — steadily strengthening.

**Rollback:** the minute knobs are defaults in code; to revert behavior set all `MARKETOPS_*_MAX_SIGNAL_AGE_MINUTES` keys high (e.g. 1440) in `.env`, or revert the commit.

---

## OPS-009 live-supply validation — promoted signal age collapses to ~4 minutes (2026-07-04, 08:49–09:00 UTC)

**Window honesty first:** the specified prime windows (CAN–MAR 17:00 UTC, PAR–FRA 21:00 UTC, MLB evening) had not opened. However, **live ITF tennis was genuinely trading** (overnight tournaments), providing real fresh-signal supply — enough to validate the OPS-009 promotion mechanics live, though NOT the watchlist outcome (tennis has no evidence forecaster, so source-backed forecasts are structurally impossible in this window).

**Session 1 (run #77, 08:49):** 2 seen → **1 promoted at age 266.5s (~4.4 min)** — sports_tennis, market type `winner`, `skipped_stale=0`, `unmeasurable=0` (the tennis book was live). One-per-ticker dedupe collapsed the two same-ticker signals correctly. Processed=1 → template forecast (tennis has no evidence path) → cycle-scoped edge-precheck targeted **0 rows** (source-backed filter): no noise row was created for an unmeasurable forecast. Duration 33.4s.

**Session 2 (run #80, 08:58, after intermediate timer cycles):** 1 seen → **0 promoted** — the refresh-cooldown correctly refused to re-promote the just-processed ticker. Zero stale promotions, zero measurement noise.

**The freshness trajectory, now measured live:** promoted signal age p50 ≈ **5 hours** (pre-OPS-008) → ≈ **67 minutes** (post-OPS-008) → **~4.4 minutes** (OPS-009, live). This is the number that had to move for edge-precheck's 300s live-sports freshness gate to be reachable, and it moved.

**Frontier readiness:** `not_ready` — blocked solely on watchlist rows, which require a source-backed domain (baseball/soccer) live window. Champion/challenger: paired n=44, d_brier −0.0498 (early_signal). Safety audit clean (in-report AST scan). All four services active.

**Decision per the validation rules:** rule 3/insufficient-supply variant applies to the watchlist question (the live supply was tennis-only — measurable-domain supply was insufficient); **do NOT loosen freshness**, and **keep `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`**. The 17:00/21:00 UTC World Cup sessions (scheduled agent at 17:17 UTC) are the real watchlist test — every upstream mechanism they depend on has now been validated live, including the 4-minute promotion ages they'll inherit.

---

## SOCCER-002 prime-window validation session — CAN–MAR (ran late, 19:02–19:20 UTC; kickoff window mostly missed) + live MLB passes (2026-07-04)

**Timing honesty:** the scheduled 17:17 UTC session fired at ~19:02 UTC (host machine asleep at trigger time); CAN–MAR was at 90'+8' (0–2) on arrival. However the autopilot worked the window autonomously all day, and a live MLB window (MIN–NYY, July-4 afternoon slate) was open — three measurement passes ran against it.

**What the autopilot did with CAN–MAR unattended:** `soccer_evidence` forecasts 1 → **18**; source-backed packets 199 → 262; promotion metrics live and healthy (cycle #181: 46 seen, 4 promoted at **age mean 541s ≈ 9 min**, skipped_stale=78 — OPS-009 working in a real window).

**Measurement passes (MLB live):**

| Pass | Cycle | Timing | Targeted | Result |
|---|---|---|---|---|
| 1 | #181 | ~7 min after cycle | 4 | all invalid: `stale_forecast` (pass timing) + `low_confidence` (player props); **real gaps measured on two-sided books** (+0.145…+0.205) |
| 2 | #183 | seconds after | 0 | 0 promoted that cycle — all 23 candidates in ticker refresh-cooldown (anti-thrash during signal flood; correct) |
| 3 | #184 | seconds after | 2 | **`stale_forecast` eliminated** — only `low_confidence` (+`wide_spread` on thin player books) remains |

Persistence: all rows persist=1 (invalid rows never accrue — correct). Watchlist=0, candidates=0.

**The decisive finding (decision rules 4+5):** the remaining blocker is a single structural fact — **the live signal supply is overwhelmingly player-prop markets**. Every CAN–MAR soccer promotion was a player series (`KXWCAST` assists, `KXWCSOA` shots-on-target, `KXWCTEAMFIRSTGOAL` first scorer): all 18 soccer_evidence forecasts correctly fell back (12 `unknown`, 6 `player_goal`, all 0.5 confidence). Same in MLB: HR/hit/TB props dominate. Evidence forecasters correctly refuse to price players from team data → 0.5 confidence → `invalid_low_confidence`, always. Additionally discovered: **KXWCAST/KXWCSOA classify as `unknown` (+5) rather than `player` (0)** in the OPS-009 promotion scorer — soccer's spec parser runs before the generic player-segment check. Tuning gap recorded, NOT fixed (no code this session).

**Session verdicts:**
- Freshness chain: **fully proven live** (9-min promotion ages; stale_forecast vanishes with cycle-timed passes).
- Books: main markets two-sided with measurable gaps; player books thin/wide.
- Watchlist: still 0 — structurally blocked by player-prop dominance, not by any mechanism failure.
- **Recommendation: keep `MARKETOPS_INCLUDE_EDGE_PRECHECK=false`** (rule 5: player props dominate → promotion-tuning pass warranted later, not now). Proposed **OPS-010** scope for operator review: exclude player markets from promotion (or zero them harder), fix the KXWCAST/KXWCSOA classification, and consider watcher attention on main-market series.
- **PAR–FRA 21:00 UTC is the main-market soccer shot** — a follow-up session is armed for 21:25 UTC (in-session scheduler; machine must be awake). Its question: do KXWCGAME/KXWCTOTAL-style PARFRA markets fire fresh signals that produce 0.65-confidence soccer_evidence forecasts and the first valid watchlist rows?

Safety greps clean. All four services active throughout; `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` unchanged.

---

## PAR–FRA prime-window validation session — main-market soccer never reached the scanner universe (2026-07-04, 21:23–21:40 UTC)

**Window honesty:** this session ran genuinely live — PAR–FRA kicked off 21:00 UTC; passes ran at 19'–36' of the first half (0–0 throughout). Host healthy (load 0.04, 79G free), all four units active, host and local clean on `d7f21b9`. No code, no flag changes; `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` verified before and after.

**The armed question is answered, and the answer is upstream of promotion.** Do KXWCGAME/KXWCTOTAL PARFRA markets fire fresh signals → 0.65-confidence soccer_evidence forecasts → first watchlist rows? **No — they never had the chance.** The main markets exist and are ideal measurement targets: `KXWCGAME-26JUL04PARFRA-{FRA,PAR,TIE}` verified live on Kalshi at 82/83¢ (1¢ spread) with **3.5M contracts** of in-play 24h volume. But:

- The scanner's single sweep (`scanner_max_markets=500`, API default page order) is saturated by prop series — the first 500 open markets included **182 `KXWCSTART` lineup props** plus MLB player props. The 20:01 UTC scan ingested **90 PARFRA markets, all props** (`KXWCAST`/`KXWCFIRSTGOAL`/`KXWCGOAL`/`KXWCSOA`/`KXWCTEAMFIRSTGOAL`); `KXWCGAME`/`KXWCTOTAL` sit past the cutoff. Verified **not** the `mve_filter` (PARFRA GAME returns fine with `mve_filter=exclude`).
- All 90 ingested PARFRA props carried `volume_24h=0` pre-match (some one-sided books) → eligibility score 0 → excluded from the watcher universe (score>0 required).
- The 20:01 rotation left a **19-ticker universe** (11 `KXMLBHRR` TOR–SEA props, 4 ATP, 4 soccer props for the *Jul 6–7* matches), frozen until the 00:01 UTC scan — after full time. Confirmed live during the match: **0 PARFRA ticks, 0 PARFRA signals ever** (the 4 future-match soccer props ticked every 60s; the watcher itself is fine).

**Measurement passes (cycle-scoped, seconds after scheduled marketops timer cycles, ~5–6 min apart, PAR–FRA live):**

| Pass | Cycle | Timing | Targeted | Result |
|---|---|---|---|---|
| A | #209 (21:27) | 37s after finish | 0 | 10 seen, 0 promoted (refresh-cooldown after #208) → no noise rows |
| B | #210 (21:33) | 36s after promotion | 3 | 23 seen, 3 promoted — all `KXMLBHRR` player props (live TOR–SEA). Two on **1¢-spread books, ~59,000¢ liquidity, gaps −0.665 / −0.480, fresh snapshots** — sole failure `invalid_low_confidence` (0.5 prop cap). One book one-sided → +`wide_spread`. persist=1 all |
| C | #211 (21:39) | 1s after finish | 0 | 15 seen, 0 promoted (cooldown anti-thrash, as in CAN–MAR) → no noise rows |

Watchlist=0, candidate_labels=0 in every pass. Earlier same-evening manual cycles (#187 19:30, #194 20:08, #208 21:23) match the pattern: every targeted row `baseball_evidence`, source-backed, confidence 0.5, only-blocker confidence except where books were one-sided or the 20:01 universe rotation orphaned MIN–NYY tickers (`invalid_stale_market_snapshot` — promoted signals referencing tickers the watcher had just stopped ticking).

**Everything below the confidence gate is now proven live:** promotion freshness (p50 358s in the 6h window; promotion→measurement 36s in pass B — `forecast_to_edge_precheck_s_p50` fell 349s → **79.6s** during the session), honest invalidation (`invalid_explainable_rate` 1.0), cooldown/noise discipline (0-promoted cycles produce 0 rows), source-backed targeting (tennis template refreshes correctly excluded). And the 0.60 gate **is reachable**: `KXMLBSPREAD-26JUL041105PITWSH` game-level forecasts hit **0.60/0.65 confidence** today — but via the 4-hourly baseline pipeline, not signals, so they were stale by measurement time. Champion/challenger meanwhile: paired n=50, d_brier −0.0432. Safety audit clean (48 files, `safety_ok: true`).

**Recommendations (recommend-only, per rules):**
- **`MARKETOPS_INCLUDE_EDGE_PRECHECK`: keep OFF.** Zero valid rows exist; automation would only inscribe invalid measurements on a cadence.
- **OPS-010 is warranted — but the CAN–MAR scope is necessary, not sufficient.** Player-prop exclusion + the KXWCAST/KXWCSOA classification fix would today leave soccer with *zero* promotable markets, because the main markets never enter the scan. OPS-010 should add **scanner coverage for supported-domain main markets** (targeted `series_ticker` sweeps for KXWCGAME/KXWCTOTAL-class series, or deeper paging / domain-aware universe injection), and consider cycle-scoped measurement of baseline-refreshed **game-level** forecasts (the 0.65-confidence PIT–WSH spreads were measurable tonight; nothing measured them within 300s).
- Verdict on watchlist evidence: **observe-more after OPS-010 lands.** Enable is not on the table until valid rows exist.

Outputs remain gaps and labels — measurement only, never advice. No EV, no sides, no sizes, no actions.

---

## SCANNER-002/OPS-010 deployed — first valid watchlist row (2026-07-04 23:56 – 2026-07-05 00:05 UTC)

Deployed **`c2c562a` → `00e169b`** (no migration, no `.env` changes — targeted scans ship enabled by default; `ENABLE_TARGETED_MARKET_SCANS=false` is the one-line rollback). Watcher restarted; baseline/retention/marketops timers untouched.

**Smoke scan (manual, 23:56 UTC):** `generic=500 targeted_fetched=560 added_after_dedupe=546`, all six series returned (`KXMLBTOTAL=250, KXMLBSPREAD=140, KXMLBGAME=92, KXWCTOTAL=36, KXWCSPREAD=24, KXWCGAME=18`), zero failed series, 2.6 s duration. Eligible candidates jumped **19 → 357**, now dominated by game-level series; the ranking top became live MLB `KXMLBGAME` winner markets at 0.947. KXWCGAME rows now exist for every upcoming World Cup match with 0.92–0.947 scores (tight books, `volume_24h` correctly parsed from `_fp` fields).

**Watcher:** universe is now **150 tickers** (100 top-score + 50 supported-universe supplement), composition logged each pass: `soccer:winner=11 soccer:total=15 soccer:spread=13 baseball:winner=34 baseball:total=44 baseball:spread=28 soccer:other=5`. 450 ticks in the first 3 minutes across all six game-level series. Signals immediately shifted from props to game-level: first 3 minutes produced `KXMLBGAME=4, KXMLBTOTAL=3, KXMLBSPREAD=3` and zero prop signals.

**The chain, end to end (timer cycle #235, 00:02 UTC, live MLB night games):** 11 seen → 5 promoted, **all game-level** (2 winner, 1 total, 2 spread) → 5 `baseball_evidence` refreshes → cycle-scoped `edge-precheck --latest-marketops-run` 66 s later measured all 5:

| Ticker | Status | Gap | Confidence | Spread |
|---|---|---|---|---|
| KXMLBTOTAL-26JUL041910TBHOU-12 | **watchlist** | +0.054 | 0.60 | 1¢ |
| KXMLBSPREAD-…TBHOU-TB3 | no_gap (valid) | −0.003 | 0.60 | 1¢ |
| KXMLBGAME-…BALCIN-BAL | no_gap (valid) | −0.019 | 0.60 | 1¢ |
| KXMLBGAME-…BALCIN-CIN | no_gap (valid) | −0.020 | 0.60 | 2¢ |
| KXMLBSPREAD-…TBHOU-TB4 | invalid_stale_market_snapshot | — | — | — |

**That watchlist row is the first valid one in project history** — every gate passed on a live market: source-backed, confidence 0.60, 1¢ spread, 132,253¢ liquidity proxy, fresh forecast + snapshot, |gap| ≥ 0.05. The three `no_gap` rows are equally important: fully valid measurements whose gaps were honestly below the 0.05 floor. Frontier readiness moved for the first time: `not_ready` → **`observe_more`** ("watchlist sample too thin (1 < 10)"). Safety audit on deployed code: 48 files, `safety_ok: true`.

**Boundaries unchanged:** `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` (all measurement remains manual/cycle-scoped); no EV, no advice, no trading surface anywhere — this milestone is scanner/watcher coverage only.

**Next:** accumulate watchlist samples during live windows (MLB nightly; POR–ESP Jul 6 ~19:00 UTC, ARG–EGY Jul 7 — KXWCGAME markets for both are already in the universe). At watchlist n≥10 with sane behavior, revisit `MARKETOPS_INCLUDE_EDGE_PRECHECK` per the runbook. Soccer-side confidence ≥0.60 still needs a live soccer window to prove (props are gone from promotion, but a live KXWCGAME signal hasn't occurred yet this deploy).

---

## Watchlist accumulation validation — decision-rule thresholds met (2026-07-05, 00:10–00:45 UTC, live MLB night slate)

Three manual cycle-scoped sessions (no code, no flag changes; `MARKETOPS_INCLUDE_EDGE_PRECHECK=false` throughout):

| Session | Cycles | Seen → promoted | Measured | watchlist | no_gap (valid) | invalid |
|---|---|---|---|---|---|---|
| 1 (00:10) | #239 manual (0 promoted, cooldown) + timer #238 measured in-freshness | 9→5 (#238) | 5 | 3 | 1 | 1 stale_snapshot |
| 2 (00:25) | #242 manual | 29→3 | 3 | 0 | 2 | 1 wide_spread |
| 3 (00:38) | timer #245 + #246 manual | 32→1, 46→5 | 6 | 5 | 1 | 0 |

All promotions game-level baseball (winner/total/spread across TB–HOU, BAL–CIN, CWS–CLE, PHI–KC, NYM–ATL); promoted age mean fell to **81 s** in #246. Every promoted forecast measured within seconds-to-~70s (`forecast_to_edge_precheck_s_p50` = **36.3 s**).

**Cumulative (6h window): watchlist = 10, no_gap = 11, valid_measurement_rate = 0.55, invalid_explainable_rate = 1.0, persistence all 1 (correct — 1h ticker refresh-cooldown means no forecast re-measured yet; invalid rows never accrue), paper_candidate_later = 0 (requires persistence ≥ 3).** Confidence-0.6 bucket: 36 forecasts (was 4 pre-SCANNER-002).

**Gap follow-through (market movement, not PnL; n=10):** 5m toward-rate 0.3, 15m 0.4, **30m 0.7, 60m 0.7** with mean gap closure ≈ 100% at 30–60m. Small sample; not clearly negative — clears the decision rule.

**Frontier readiness moved again: `observe_more` → `ready_for_cycle_scoped_edge_automation`** ("valid + watchlist rows exist, invalid rows fully explainable, MarketOps p90 37.6s < 60s, safety clean").

**Decision-rule status (all five met):** watchlist ≥ 10 ✓ · follow-through not clearly negative ✓ · persistence correct ✓ · safety audit clean (48 files) ✓ · p90 37.6 s < 60 s ✓. **Recommendation: `MARKETOPS_INCLUDE_EDGE_PRECHECK=true` is now justified as its own deliberate one-flag rollout step** (cycle-scoped stage only, ≤5 forecasts/cycle). Caveats for the operator: follow-through n=10 is early; soccer confidence ≥0.60 remains unproven (all 10 watchlist rows are baseball — POR–ESP Jul 6 ~19:00 UTC is the soccer proof window, KXWCGAME markets already in the universe).

No EV, no paper trading, no recommendations-to-trade, no sizing, no orders, no wallets, no swaps, no execution — outputs remain gaps and labels.

---

## MVP-005A.2 / EDGE-AUTO-001 — MARKETOPS_INCLUDE_EDGE_PRECHECK flipped true (2026-07-05, 01:40–01:50 UTC)

**Readiness evidence (before flip):** frontier label `ready_for_cycle_scoped_edge_automation`; watchlist=10, valid-measurement rate 0.55, invalid rows 100% explainable, follow-through n=10 (30m/60m toward-rate 0.7), MarketOps p90 38.9s < 60s, safety audit clean, all four units active, repo clean on `f05f5cf`.

**Flag change (the only change):** `MARKETOPS_INCLUDE_EDGE_PRECHECK` **false → true** (`sed` on `.env`; oneshot timer picks it up per-run, no restarts). Unchanged and verified: `ENABLE_EDGE_PRECHECK=true`, `ENABLE_SOCCER_EVIDENCE_FORECASTING=true`, `ENABLE_SOCCER_EXTERNAL_RESEARCH=true`, `ENABLE_CRYPTO_RISK_ENGINE=true`, `ENABLE_GOPLUS_RISK=true`. Nothing else touched.

**Validation cycles (live MLB night slate):**
- **#257 (manual, 01:40):** 51 seen → 5 promoted/processed (all game-level) → stage summary `edge_prechecks_created=5, watchlist=4, candidate_labels=0, invalid=0, no_gap=1`; 38.0s. Exactly the cycle's own refreshed forecasts — no sweep.
- **#258 (scheduled timer, 01:44):** 44 seen → 3 promoted (all `total`) → `created=3, watchlist=1, candidate_labels=0, invalid=1, no_gap=1`; 38.4s. First fully-autonomous measurement cycle.

**Post-rollout state (6h window):** watchlist **15**, no_gap 13, paper_candidate_later **0** (still requires persistence ≥3 — correct), valid-measurement rate **0.72**, invalid_explainable_rate 1.0. **First persistence increment observed and correct:** `KXMLBSPREAD-26JUL042008NYMATL-ATL3` re-measured watchlist with the same gap direction → persist=2 (distribution `{1: 38, 2: 1}`). Invalidation reasons in-window: low_confidence 7, wide_spread 5, stale_snapshot 4, low_liquidity 1 — all expected classes.

**Gap follow-through (market movement, not PnL; n=14):** 5m 0.50 → 15m 0.57 → **30m 0.786 / 60m 0.786** toward-rate, mean gap closure 78–88% at 30–60m. Directionally positive; still an early sample.

**Service health:** all four units active; 0 errors in the last 250 marketops journal lines; durations p50 34.2s / p90 38.9s / p99 40.4s (no measurable stage cost). DB 530.1 → 532.7 MiB across the rollout window (tick growth from the 150-ticker universe; `db_growth_warning` alerts are the 512 MiB advisory — retention prunes ticks at 7 days; consider raising `DB_GROWTH_WARNING_MB` or accepting the larger steady-state in a later OPS pass). Safety audit: 48 files, `safety_ok: true`; canonical + expanded greps on host: boundary docstrings only.

**Caveats:** (1) all 15 watchlist rows are baseball — soccer ≥0.60 confidence remains unproven until a live soccer window (POR–ESP Jul 6 ~19:00 UTC; KXWCGAME markets already in the universe); (2) follow-through n=14 is early — keep reading it as market-movement telemetry, never as PnL; (3) `paper_candidate_later` remains a zero-behavior filing label; MVP-005B stays gated on explicit acceptance.

**Rollback (one line):** `sed -i 's/^MARKETOPS_INCLUDE_EDGE_PRECHECK=true/MARKETOPS_INCLUDE_EDGE_PRECHECK=false/' ~/projects/probability-arena/.env` then `marketops-run-once` + `marketops-report`/`edge-precheck-report` to confirm the stage is gone.

No EV, no paper trading, no trade recommendations, no sizing, no orders, no wallets/keys, no swaps, no signing, no execution, no autonomy — the autopilot gained one measurement stage, strictly cycle-scoped (≤5 same-cycle forecasts), and nothing else.

---

## OPS-011 — DB growth observability + alert calibration (2026-07-05, ~21:00 UTC)

Deployed **`134e401` → `36aa08a`** (no migration). Ops/observability only — no forecasting, edge, promotion, or trading logic changed; `MARKETOPS_INCLUDE_EDGE_PRECHECK` stays **true**.

**Live DB breakdown (`db-growth-report`, dbstat compiled in on host):** file **1086.9 MiB**; `market_price_ticks` is **903 MiB / 83%** of the DB at 291,992 rows (next: market_snapshots 43.5 MiB, crypto_token_discovery_events 24.9 MiB, opportunity_signals 13.6 MiB, crypto_price_ticks 11.5 MiB). Ticks by domain: baseball 173,919 / soccer 81,439 / general 27,591 / tennis 9,043. Age buckets: `<1d`=187,918, `1-3d`=104,074, `3-7d`=0, `>7d`=0 — **the 7d window had not yet pruned anything** (oldest tick 2026-07-03). Observed raw-tick rate ≈ **317 MiB/day** average (peak live-slate ≈ 645 MiB/day est).

**Growth estimate → retention decision:** at 7d retention, tick steady-state ≈ 2.2 GB (~2.4 GB total DB), which would chronically trip even a raised warning. Raw ticks are pure telemetry (the watcher only compares consecutive ticks; edge-precheck freshness is ≤120s; follow-through uses ≤60m). **Applied `TICK_RETENTION_DAYS` 7 → 3** on the host (reversible; prunes 0 rows right now since the oldest tick is 2.85d — a purely forward-looking cap). At 3d, tick steady-state ≈ 0.95 GB → total ~1.15 GB, safely under the 1536 MiB warning. Note: the SQLite file won't shrink from the current 1087 MiB without a `VACUUM` (locks the DB — deferred to a maintenance window; freed pages are reused so growth stays capped meanwhile).

**Alert calibration (config-driven, warning + critical tiers):**
- `DB_GROWTH_WARNING_MB` 512 → **1536**, `DB_GROWTH_CRITICAL_MB` → **3072**.
- `MARKETOPS_SIGNAL_FLOOD_WARNING_PER_HOUR` 150 → **400**, `..._CRITICAL_PER_HOUR` → **800**.
- Verified live: last `db_growth_warning` fired 03:20 UTC at the old 512 gate ("578 MiB"); DB is now 1087 MiB but **no new db_growth alert fires** (< 1536). Last `too_many_signals` fired 03:14 UTC at old 150 ("153"); signal volume is now 237/h but **no new alert fires** (< 400). Chronic advisories silenced; genuine anomalies (critical tiers, watcher-stale, no-signal) still fire.

**New observability:** `db-growth-report` (size, per-table rows + est MiB, largest tables, tick age/domain buckets, edge/crypto growth, backups, retention windows, thresholds) and `prune-retention --dry-run` now prints a per-table projection (window, total, eligible, remaining, oldest/newest ticks).

**Health post-deploy (cycles #451, #452, +manual):** edge-precheck batches all **5 rows (≤5, strictly cycle-scoped — no sweep)**; MarketOps p50/p90/p99 **32.4 / 38.2 / 40.4 s** (unchanged); readiness `ready_for_cycle_scoped_edge_automation`; safety audit **49 files, safety_ok true**. All four units active.

**Rollback:** `sed -i 's/^TICK_RETENTION_DAYS=3/TICK_RETENTION_DAYS=7/' .env` (retention); remove the `DB_GROWTH_*` / `MARKETOPS_SIGNAL_FLOOD_*` keys to fall back to config defaults (which are the same calibrated values). No code rollback needed for the alert change.

**Follow-up (OPS-012, proposed):** roll raw ticks into hourly OHLC/spread/liquidity aggregates, retain raw ticks shorter + aggregates longer, and move DB-growth alerting from absolute-size gates to a rate-based (MiB/day) signal. Build only when small and explicitly safe.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets, no swaps, no execution, no autonomy — OPS-011 is storage/alert measurement and tuning only.

## EDGE-ANALYSIS-001 — edge cohort follow-through analysis deployed (2026-07-05, ~23:00 UTC)

Deployed **`dd99146` → `d20ca56`** by `git pull --ff-only` (clean fast-forward; **no migration** — no alembic/model changes, only a new service + CLI command + tests + docs). Read-only **reporting only**: no flag, threshold, promotion, edge, forecast, or service change. `MARKETOPS_INCLUDE_EDGE_PRECHECK` stays **true**; `ENABLE_EDGE_PRECHECK` stays **true**. **No services restarted** (a read-only CLI needs none; oneshot timers already run the new code from disk). All four units remain active.

**New capability:** `edge-cohort-report --hours N` — slices watchlist / `paper_candidate_later` snapshots into 10 cohort dimensions and measures per-cohort gap follow-through (market movement, not PnL), labelling each `too_thin` / `promising` / `neutral` / `weak` / `exclude_candidate`.

**Live output summary (host, `--hours 24`):** 348 snapshots, 222 follow-through rows. Overall moved-toward rate **0.464 / 0.432 / 0.369 / 0.324** at 5/15/30/60m (cross-checks the frontier-eval follow-through exactly). Cohort labels:
- **exclude_candidate** (deprioritize in future gating): `market_type=winner`, `confidence=0.65+`, `game_phase=late`, `persistence=2`, `abs_gap>0.15`, `liquidity=1M-10M`.
- **weak**: `total`, `spread`, both gap signs, baseball overall, `spread=1`, `game_phase=early`, `persistence=1`, `price_move_threshold`.
- **neutral** (observe more): small-gap buckets (0.05–0.10), `spread=2–5c`, `liquidity<100k`.
- **too_thin**: soccer (n=8), non-`price_move_threshold` signal types, `persistence=3+` (n=7).

**Any cohort promising?** **No** — zero cohorts cleared the `promising` bar; several are actively `exclude_candidate`.

**MVP-005B-design gate:** **BLOCKED** — no cohort clears both the sample floor (n≥20) and toward-rate (≥0.55); overall toward-rate **0.398** over n=222. The report unlocks nothing; advancing would still require explicit human acceptance.

**Health post-deploy:** readiness `ready_for_cycle_scoped_edge_automation` (unchanged); safety audit **50 files, safety_ok true** (new module scanned, 0 violations); MarketOps p90 **38.5 s** (< 60 s); run #471 ok. DB **1137.8 MiB** (< 1536 warn); `market_price_ticks` still dominant (~308k rows, oldest 2026-07-03 — 3d retention not yet matured, `3-7d`=0). No new `db_growth`/`signal_flood` alerts.

**Rollback:** none needed operationally (read-only, no flag/service change). To remove the command: `git revert d20ca56` (or `git reset --hard dd99146` on host) — no state to unwind.

**Next recommendation:** **keep collecting.** Follow-through remains neutral-to-negative across every cohort; MVP-005B stays blocked. Use `edge-cohort-report` to track whether any cohort (e.g. the small-gap/tighter-spread `neutral` buckets) firms up as samples grow, and to justify future edge-gating deprioritization (winner markets, 0.65+ confidence, late-game, persistence-2). Do not start MVP-005B-design. OPS-012 tick aggregation remains the standing roadmap item once the 3d retention plateau is observed.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets, no swaps, no signing, no execution, no autonomy — EDGE-ANALYSIS-001 is measurement/reporting only.

## EDGE-POLICY-001 — read-only shadow cohort-filter policy analysis deployed (2026-07-06, ~00:00 UTC)

Deployed **`447e7ae` → `debdfda`** by `git pull --ff-only` (clean fast-forward; **no migration** — new service + CLI command + tests + docs only). Read-only **shadow analysis**: it re-slices existing rows and changes **no** flag, threshold, promotion, forecaster, edge-precheck, MarketOps, or service behavior. `MARKETOPS_INCLUDE_EDGE_PRECHECK` stays **true**; `ENABLE_EDGE_PRECHECK` stays **true**. **No services restarted.** All four units active. (First on-host invocation hit a transient SQLite `database is locked` — a concurrent MarketOps/watcher write; retried immediately and succeeded. Expected on this single-writer SQLite host; harmless for a read-only report.)

**New capability:** `edge-policy-report --hours N` — simulates 13 candidate cohort filters over the watchlist / `paper_candidate_later` population, with per-policy follow-through, distributions, and a settlement-conditioned forecast-vs-market Brier block on resolved outcomes. Analysis only (not PnL, not EV, not a trade).

**Live output summary (host, `--hours 24`):** population 233, **52 resolved markets** for settlement. Every policy labels **neutral** — none reaches `promising_shadow`. Blended moved-toward rate by policy (baseline **0.388**):
- `exclude_all_current_bad_cohorts` **0.561** (n=45, 30m=0.51, 60m=0.49) and `conservative_candidate_policy` **0.583** (n=15, 30m=0.47) are the strongest lifts but **neither clears** the gate (30m/60m ≥ 0.55 at n≥20).
- Mild improvers over baseline: `spread_2_5c_only` 0.484, `small_gap_only_005_010` 0.474, `liquidity_lt_100k_only` 0.471, the single-exclusions ~0.42–0.44.

**Any shadow policy promising?** **No.** Zero policies clear n≥20 with moved-toward ≥0.55 at 30m or 60m while improving over baseline. `exclude_all_current_bad_cohorts` is the closest (30m 0.51) but still short.

**Settlement — any narrow cohort worth tracking?** **Yes, one:** `small_gap_only_005_010` — short-horizon follow-through is weak (blended 0.474) yet on **n=12 resolved** the forecast Brier **beats** the market midpoint by **0.017** (forecast 0.252 vs market 0.269). This is the only resolved-outcome disagreement flagged; worth continued tracking. Context: the **baseline** forecast is *worse* than market at settlement (Brier 0.202 vs 0.120; beats-market only 0.115 over 52 resolved), so any edge is narrow and cohort-specific — calibration only, **not** EV/PnL/trade.

**MVP-005B-design gate:** **BLOCKED** — no shadow policy clears the gate; the filters re-slice the same weak population. The report unlocks nothing; advancing would still require explicit human acceptance.

**Health post-deploy:** readiness `ready_for_cycle_scoped_edge_automation` (unchanged); safety audit **51 files, safety_ok true** (new module scanned, 0 violations); MarketOps p90 **38.6 s** (< 60 s); run #481 ok. DB **1163.8 MiB** (< 1536 warn); `market_price_ticks` ~317k rows, oldest 2026-07-03 (3d retention not yet matured, `3-7d`=0). No new `db_growth`/`signal_flood` alerts. Champion/challenger baseball paired n=91 (early_signal).

**Rollback:** none needed operationally (read-only, no flag/service change). To remove the command: `git revert debdfda` (or `git reset --hard 447e7ae` on host) — no state to unwind.

**Next recommendation:** **keep collecting.** The interesting threads — `exclude_all_current_bad_cohorts` lifting follow-through toward ~0.56, and the small-gap cohort narrowly beating market at settlement — are suggestive but sub-gate and thin. Re-run `edge-policy-report` as samples grow to see whether either firms up past the 30m/60m ≥ 0.55 gate at n≥20. Do not start MVP-005B-design. OPS-012 tick aggregation remains the standing roadmap item once the 3d retention plateau is observed.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets/private keys, no swaps, no signing, no execution, no autonomy — EDGE-POLICY-001 is shadow measurement/reporting only.

## MEME-NEWS-001 — read-only meme/news + domain-expansion scout deployed (2026-07-06, ~05:36 UTC)

Deployed **`efd7e2d` → `8239510`** by `git pull --ff-only`; **migration 0018 → 0019** applied via the safe path (`run-baseline --dry-run`, pipeline run #27 `status=dry_run`). **Pre-migration backup:** `data/backups/backup-20260706T053419Z.db.gz` (107.15 MiB). Read-only discovery/scouting: **no flag changed, no service restarted, no timer/loop enabled**; `MARKETOPS_INCLUDE_EDGE_PRECHECK` and `ENABLE_EDGE_PRECHECK` stay **true**; `ENABLE_MEME_SCOUT`/`ENABLE_DOMAIN_SCOUT` remain **default false** (manual commands always allowed). Migration 0019 adds 5 empty audit tables (meme_scout_runs, meme_attention_snapshots, meme_catalyst_events, domain_scout_runs, domain_market_inventory_snapshots) — no EV/trade/order/wallet/swap/execution columns.

| | before | after |
|---|---|---|
| commit | `efd7e2d` | `8239510` |
| alembic revision | `0018` | `0019` |
| DB size | 1322.27 MiB | 1323.40 MiB (+~1 MiB, scan audit rows only) |

**Part A — `meme-scan-once` (live DexScreener, on-host):** run #1 ok — **28 profiles + 30 boosts → 30 tokens scored, 65 catalysts.** `meme-scout-report`: attention p50 **0.346** / p90 **0.464**, `provider_confidence_avg=1.0` (host crypto-lane risk data present, unlike a cold DB), risk levels low=29 / severe=1 (the one severe token correctly penalized). Top: HAHA 0.500, DONALT 0.499, POST 0.466, LEVI 0.464. `attention_score` is an interest signal only — no action attached.

**Part B — `catalyst-report`:** **65 events** — `profile_seen`=30, `social_present`=29, `boost`=6; all `source=dexscreener`, `subject=token`. rss/x/discord/telegram remain unconfigured placeholders.

**Part C — `domain-scout-report`:** **10,316 markets across 8 domains.** Candidate priorities (ranked):

| domain | mkts | active | 2-sided | clarity | forecaster | canary_priority |
|---|---|---|---|---|---|---|
| sports_baseball | 5692 | 5692 | 0.86 | 1.0 | yes | 0.866 |
| **sports_tennis** | 528 | 528 | **1.0** | **1.0** | **NO** | **0.625** |
| sports_soccer | 714 | 714 | 0.80 | 0.95 | yes | 0.547 |
| general | 2965 | 2965 | 0.67 | 0.95 | NO | 0.545 |
| politics | 9 | 9 | 1.0 | — | NO | 0.450 |
| macro | 17 | 17 | 0.88 | — | NO | 0.421 |
| crypto | 7 | 7 | 0.71 | — | NO | 0.379 |
| weather | 384 | 384 | 0.57 | — | NO | 0.360 |

**Top forecaster-gap expansion candidate: `sports_tennis`** — 528 fully two-sided markets, clarity 1.0, known ESPN/ATP/WTA public source, and no evidence forecaster yet. (basketball/golf/esports did not surface — no such series in the current scanned universe; they'd need targeted scan coverage first, like SCANNER-002 did for game-level markets.)

**Existing EDGE-AUTO / MarketOps health (unchanged):** MarketOps run #538 ok (32.6s, clean journal, quiet overnight window promoted=0); edge-policy gate **BLOCKED** (0.4043, unchanged); readiness `ready_for_cycle_scoped_edge_automation`; champion/challenger baseball paired n=91; all four units active; no errors/warnings in the last 150 marketops journal lines.

**DB growth impact:** negligible (+~1 MiB — the 5 tables hold only this run's scan audit rows). `market_price_ticks` 3-7d bucket now 9860 (3-day retention plateau maturing; first substantial prune ~Jul 7). Under the 1536 MiB warn.

**Safety:** canonical + expanded grep clean (only boundary docstrings); frontier-eval AST audit **53 files, safety_ok=True, 0 violations** (new meme_scout/domain_scout modules scanned).

**Flags changed:** **none.** No services restarted, no loop/timer enabled, no API endpoints added.

**Rollback:** operationally none needed (read-only, no flag/service change). To remove: `git reset --hard efd7e2d` on host + `alembic downgrade 0018` (drops the 5 empty tables); backup above restores pre-migration state if ever needed.

**Next recommendation:** **keep collecting.** The domain scout gives a concrete, data-backed signal: **`sports_tennis` is the strongest next-canary candidate** (real two-sided supply + clear resolution + public data source + no forecaster). A future **docs-only tennis-canary design** could be justified — but that is a separate, explicitly-accepted milestone; nothing here builds a forecaster or changes live behavior. `meme-scan-once` is manual-only for now (no timer enabled per instructions). OPS-012 tick aggregation remains the standing item once the 3-day retention plateau is observed.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets/private keys, no swaps, no signing, no execution, no autonomy — MEME-NEWS-001 is read-only discovery/scouting only.

## MEME-NEWS-002 — scheduled read-only discovery lane deployed + ENABLED (2026-07-06, ~06:06 UTC)

Deployed **`778469c` → `eb3e103`** by `git pull --ff-only`; **no migration** (reuses MEME-NEWS-001 schema `0019` — alembic revision unchanged). Then enabled as a controlled 10-minute `systemd --user` timer. Read-only scheduled discovery: **existing MarketOps/EDGE-AUTO behavior unchanged**, no API added, `MARKETOPS_INCLUDE_EDGE_PRECHECK` / `ENABLE_EDGE_PRECHECK` still true.

| item | value |
|---|---|
| 1. pushed commit | `eb3e103` |
| 2. deployed commit | `778469c` → `eb3e103` |
| 3. flag before/after | absent (default **false**) → **`ENABLE_MEME_NEWS_SCOUT=true`** |
| 4. migration | **none** — alembic `0019` unchanged (no migration/model files in diff) |

**5. Manual run (flag still off):** `meme-news-run-once` → run #3 ok, 29 profiles + 30 boosts, **30 scored, 63 catalysts** (manual path works flag-independent). `meme-news-report`/`meme-news-alerts`/`db-growth-report` all worked.

**6. Scheduled guard while disabled:** `meme-news-run-once --scheduled` → `ENABLE_MEME_NEWS_SCOUT=false; scheduled meme-news cycle skipped` (correct no-op).

**7. Timer status:** `probability-arena-meme-news.timer` **active (waiting)**, next trigger 06:16:13 UTC, 10-min cadence, Triggers the service. Service is a oneshot (`disabled`/TriggeredBy the timer — the correct shape, mirroring marketops).

**8. Forced scheduled service run:** `systemctl --user start probability-arena-meme-news.service` → **Result=success, exit 0/SUCCESS**, journal `meme-news run #5: ok profiles=29 boosts=30 scored=30 catalysts=63`, clean finish, 1.3s CPU. (After the flag flip the `--scheduled` command runs instead of skipping.)

**9. meme-news-report (post-enable):** 5 runs (**0 errors**), 150 attention snapshots, 317 catalysts, attention **p50 0.357 / p90 0.465 / max 0.745**, `provider_confidence_avg=0.985` (host crypto-lane risk data present), `missing_holder_coverage=3`.

**10. meme-news-alerts:** informational notable events firing correctly — observed `high_attention` (×2, ≥0.6), `attention_jump` (×2), and a `severe_risk` **warn** (avoid/flag verdict — never a trade direction). Local, derived, no push, no recommendation.

**11. DB size/growth:** 1335.70 → **1336.87 MiB** (+~1 MiB). `meme_attention_snapshots` 150 rows; at the 10-min cadence (~30 snapshots + ~60 catalysts per run) growth is modest and bounded by `MEME_NEWS_RETENTION_DAYS=14` (prunes runs/snapshots/catalysts; domain inventory kept). Under the 1536 MiB warn.

**12. MarketOps/EDGE-AUTO health (unchanged):** MarketOps run #543 ok; readiness `ready_for_cycle_scoped_edge_automation`; no marketops journal errors; all four prior units active + the new meme-news timer active. The lane is a separate oneshot unit and cannot affect MarketOps.

**13. Safety:** canonical grep clean (only a boundary docstring in `meme_news.py`); frontier-eval AST audit **54 files, safety_ok=True, 0 violations**.

**14. Rollback:**
```bash
sed -i 's/^ENABLE_MEME_NEWS_SCOUT=true/ENABLE_MEME_NEWS_SCOUT=false/' ~/projects/probability-arena/.env
systemctl --user disable --now probability-arena-meme-news.timer
systemctl --user stop probability-arena-meme-news.service
# (optional full removal) rm ~/.config/systemd/user/probability-arena-meme-news.{service,timer} && systemctl --user daemon-reload
```
Even the flag flip alone neutralizes it: the `--scheduled` command no-ops while false, so the timer becomes a harmless empty tick.

**15. Recommendation:** **keep the timer on, observe for the first ~24h.** Watch three things via `meme-news-report`/`meme-news-alerts`/`db-growth-report`: (a) `meme_attention_snapshots`/`meme_catalyst_events` growth vs the 14-day retention plateau (confirm it caps), (b) alert volume/noise (tune `MEME_NEWS_ATTENTION_ALERT_THRESHOLD` if `high_attention` is too chatty), (c) that MarketOps p90 and the DB warn threshold stay clear. Roll back per §14 if errors, runaway growth, or noise appear.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets/private keys, no swaps, no signing, no execution, no autonomy — MEME-NEWS-002 is read-only scheduled discovery only.

## TENNIS-001 — dark-first deployment; template canary on, evidence forecasting OFF (2026-07-06, ~07:40 UTC)

Deployed **`47babb5` → `7f23186`** by `git pull --ff-only`; **no migration** (alembic `0019` unchanged — no migration/model files in the diff). Dark-first rollout: read-only tennis evidence canary. Existing MarketOps/EDGE-AUTO/meme-news behavior **unchanged**; `MARKETOPS_INCLUDE_EDGE_PRECHECK` / `ENABLE_EDGE_PRECHECK` stay **true**; no services restarted (oneshot timers read `.env` next run).

| | before | after |
|---|---|---|
| commit | `47babb5` | `7f23186` |
| alembic revision | `0019` | `0019` (no migration) |
| `ENABLE_TENNIS_EXTERNAL_RESEARCH` | (unset → false) | **true** (dark canary) |
| `TENNIS_RESEARCH_PROVIDER` | (unset → template) | **template** |
| `ENABLE_TENNIS_EVIDENCE_FORECASTING` | (unset → false) | **false** (unchanged) |

**Template dark-canary result:** `marketops-run-once` #559 ok (quiet overnight window, 0 signals promoted/processed) — **no tennis packets, no `tennis_evidence` forecasts, no behavior created.** `research-canary-report` unchanged (baseball-external 750, soccer-external 70, template 499; forecasters baseball_evidence 750 / soccer_evidence 46 / template_baseline 524; no tennis yet). With `provider=template` the tennis collector, if selected for a tennis signal, wraps the template collector and falls back honestly to `template_only` — behaviorally identical to no-canary.

**Real ticker + ESPN provider validation (read-only probe, forecasting left off):**
- **Parser: 528/528 real tennis-prefixed markets parsed, 0 failures** (468 winner-markets). Handles the real Kalshi shapes `KXATPCHALLENGERMATCH-…`, `KXITFMATCH-…`, `KXITFWMATCH-…` (matchup splits to two player codes; ticker suffix identifies the subject player). **Ticker format fully validated.**
- **ESPN provider: 0/5 winner tickers produced source-backed packets — all fell back honestly** (`no scoreboard match for <matchup>`). The ESPN tennis endpoint responds, but the currently-listed markets are all **ATP Challenger / ITF futures with lower-tier players that ESPN's tennis scoreboard does not cover** (and the Kalshi player codes don't align with ESPN athlete abbreviations). Template provider: all fell back honestly, as required.

**Tennis evidence forecasting: REMAINS OFF.** Real-provider validation did not yield source-backed packets, so per the rollout gate `ENABLE_TENNIS_EVIDENCE_FORECASTING` stays false. (Even if enabled it would have nothing source-backed to act on.)

**Existing health (unchanged):** all five user timers active (marketops, watcher, baseline, retention, meme-news); MarketOps run #559 ok, no journal errors; readiness `ready_for_cycle_scoped_edge_automation`; champion/challenger n=91. **MEME-NEWS-002 24h observation healthy** — meme-news runs #11–13 ok, 0 errors, 30 scored/run. DB ~1377 MiB (under the 1536 warn).

**Safety:** canonical + expanded grep clean (only boundary docstrings); frontier-eval AST audit **56 files, safety_ok=True, 0 violations**.

**Rollback:** `sed -i 's/^ENABLE_TENNIS_EXTERNAL_RESEARCH=true/ENABLE_TENNIS_EXTERNAL_RESEARCH=false/' .env` (or remove the TENNIS block) — read-only, no state to unwind; `git reset --hard 47babb5` on host removes the code.

**Next recommendation:** **Keep the template dark canary on (harmless honest fallback); keep `ENABLE_TENNIS_EVIDENCE_FORECASTING` OFF.** The parser is fully validated, but `provider=espn` cannot serve the current market set (all Challenger/ITF). Revisit the ESPN provider only when **main-tour ATP/WTA match-winner markets** appear (ESPN covers those) — re-run the read-only probe then, and only flip `provider=espn` + `ENABLE_TENNIS_EVIDENCE_FORECASTING=true` if the probe yields source-backed packets. A future refinement to map Kalshi player codes → ESPN athlete abbreviations may also be needed.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets/private keys, no swaps, no signing, no execution, no autonomy — TENNIS-001 is read-only evidence/research, deployed dark with forecasting off.

## MEME-RISK-003 — holder-risk coverage reporting deployed DARK; providers OFF (2026-07-07, ~06:35 UTC)

Deployed **`81ae060` → `00d5db6`** by `git pull --ff-only`; **no migration** (alembic `0019` unchanged — holder/creator percentages live in the existing `flags` JSON). Dark: the new coverage *reporting* is live, but **no risk provider was enabled**. Existing MarketOps/EDGE-AUTO/MEME-NEWS behavior unchanged; `MARKETOPS_INCLUDE_EDGE_PRECHECK`/`ENABLE_EDGE_PRECHECK` untouched.

| flag | before | after |
|---|---|---|
| `ENABLE_CRYPTO_RISK_ENGINE` | true | true (unchanged) |
| `ENABLE_GOPLUS_RISK` | true | true (unchanged) |
| `ENABLE_SOLANA_TRACKER_RISK` | false | **false** (dark) |
| `ENABLE_BIRDEYE_RISK` | (unset → false) | **unset → false** (dark) |

**Provider absence is EXPLICIT (`crypto-provider-health-report`):** goplus **active** (covers top10_holder/insider/authority/rug/honeypot); solana-tracker **disabled** (would cover sniper/insider/bundler); birdeye **disabled** (would cover top10_holder/creator); helius/rugcheck **reserved**. **`COVERAGE GAPS (no active provider): sniper, bundler, creator`.** Keys reported present/absent only.

**Notable finding — the gap is wider than assumed:** observed coverage over recent assessments is **0/50 for *every* holder dimension including top10_holder** — GoPlus is returning authority/rug/honeypot verdicts for these memecoins but **no holder-concentration data at all**. `meme-risk-coverage-report`: 464 tokens, 364 with goplus data, 100 missing — but 0/464 for all five holder dimensions. So closing the holder/sniper/insider/bundler/creator gap genuinely requires enabling SolanaTracker (needs key) and/or Birdeye (creator/holder; payload pending validation). MEME-RISK-003 now makes this explicit instead of silent.

**GoPlus behavior unchanged:** `crypto-risk-report` still `engine=provider-backed providers=goplus`, `by level low=50`, `goplus=45` uses / 5 errors — same as pre-deploy, plus the new holder-coverage overlay + gap line.

**Health:** all 6 user timers active (marketops/watcher/baseline/retention/meme-news/edge-observation); meme-news 140 runs, **0 errors**; MarketOps p90 **40.0s** (<60s); readiness `ready_for_cycle_scoped_edge_automation`. **Note: `edge-policy-report` gate flipped to `blocked: False`** — a shadow policy cleared the n≥20 & ≥0.55 @30m/60m bar over the current 24h window. This is a MEASUREMENT signal only (advancing to MVP-005B-design still needs explicit human acceptance — nothing is unlocked); flagged for review, not acted on. DB **1848 MiB** — tick-driven (`market_price_ticks` 1547 MiB), under the 3072 critical; meme footprint negligible.

**Safety:** grep clean (only boundary docstrings); frontier-eval AST audit **57 files, safety_ok=True, 0 violations**.

**Rollback:** none needed operationally (read-only reporting, providers off). To remove: `git reset --hard 81ae060` on host — no state to unwind.

**Recommendations:**
- **Enable a real holder-data provider next** (separate step): `ENABLE_SOLANA_TRACKER_RISK=true` with its key closes sniper/insider/bundler; `ENABLE_BIRDEYE_RISK=true` adds creator/holder — but validate the Birdeye payload against live responses first (mapping pending). Until then the reports honestly show the gap.
- **MEME_NEWS_ATTENTION_ALERT_THRESHOLD tune (RECOMMENDATION ONLY — not applied):** raise `0.6 → 0.70`. Over 24h, `high_attention` fired 72× at 0.6 vs 44× at 0.65 and 21× at 0.70; attention p90 is 0.499, so 0.6 flags the whole top decile while 0.70 keeps only the genuinely notable ~top-2% spikes (~1/h), cutting `high_attention` volume ~70% without losing the strongest signals. Applying it is a live meme-news config change, left to explicit approval.

No EV, no paper trading, no recommendations, no sizing, no orders, no wallets/private keys, no swaps, no signing, no execution, no autonomy — MEME-RISK-003 is read-only risk-coverage intelligence, deployed dark with all providers off.
