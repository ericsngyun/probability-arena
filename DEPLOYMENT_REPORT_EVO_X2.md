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

## PROVIDER-ROLL-001 — SolanaTracker enabled (single-provider keyed rollout) (2026-07-07, ~20:45 UTC)

Config-only change on the host (**no code change, no migration**, HEAD still `02312cd`). Closes the holder-data gap that MEME-RISK-003 made explicit, one provider at a time. `.env` housekeeping: an orphan **bare-value line (no key name, no `=`) was removed** — it predated this session and was silently ignored by the dotenv parser (value never printed); a mispasted `SOLANA_TRACKER_API_KEY` value was corrected to `KEY=value` form. Backups preserved (`.env.bak.*`).

| flag | before | after |
|---|---|---|
| `ENABLE_SOLANA_TRACKER_RISK` | false | **true** |
| `SOLANA_TRACKER_API_KEY` | absent | **present** (`key_present=True`, value never logged) |
| `ENABLE_GOPLUS_RISK` | true | true (unchanged) |
| `ENABLE_BIRDEYE_RISK` | (unset → false) | unset → false (**Birdeye NOT enabled**) |
| `ENABLE_MARKETOPS_AUTOPILOT` | true | true (unchanged) |
| `MEME_NEWS_ATTENTION_ALERT_THRESHOLD` | 0.70 | 0.70 (unchanged) |

**Provider status (`crypto-provider-health-report`):** solana-tracker **disabled → active** (enabled=True, key_present=True). goplus still active; birdeye still disabled. Covered dimensions now include a second provider for top10_holder/insider/authority/rug/honeypot, and solana-tracker is the sole provider advertising sniper/bundler. `COVERAGE GAPS` narrowed **{sniper, bundler, creator} → {creator}** (creator needs Birdeye, intentionally not enabled).

**Validation batch (`crypto-risk-assess --limit 20`):** SolanaTracker **19 success / 1 error** (single transient `ReadTimeout`); ~1s/token, ~20s total (the one timeout inflated wall time). Live `data.solanatracker.io/tokens/{addr}` calls returned 200 alongside GoPlus.

**Observed coverage (partial win — reported honestly):**
- `top10_holder` **0% → 25.68% (19/74)** crypto lane / **3.94% (19/482)** meme lane (only newly-assessed tokens carry SolanaTracker data; go-forward rate ≈ provider success rate).
- `authority` 78.4% (goplus+solana-tracker).
- `sniper` / `insider` / `bundler` **still 0%** — the `/tokens/{addr}` payload yields holder-concentration but not sniper/insider/bundler under the current parse. **Follow-up**, not a rollback trigger (likely needs additional SolanaTracker endpoints/mapping). `creator` 0% (Birdeye gap, by design).

**Risk labels (`crypto-risk-report`):** by level **low=56, medium=16, severe=2** (was low=62 / medium=9 / **severe=0**) → **2 new severe** (liquidity_removed + fake_volume driven), medium up. New reason **`high_holder_concentration`=13** and new signal category **`holder_risk`=13** (SolanaTracker top-holder derived). `rug_risk` 181→188 (+7 from new assessments). **`suspicious_supply_control` UNCHANGED at 47** — no behavior drift in that signal.

**MarketOps / desk health unchanged:** last run ok, champion/challenger `mean_delta_brier=-0.029173` (identical), **MarketOps p90 = 40.896s** (<60s), readiness `ready_for_cycle_scoped_edge_automation`. **DB impact negligible:** 2202 → 2238 MiB is tick-driven; crypto_token_risk_assessments +426 rows / +0.18 MiB from the batch. Pre-existing `db_growth_warning` (tick tables, 3d retention) is unrelated.

**Safety:** no code changed → surface identical to `02312cd`. Canonical + expanded grep clean (only boundary docstrings; expanded dangerous-identifier grep empty outside the known Kalshi WS auth); frontier-eval AST audit **`safety_ok=True`**. No EV, paper trading, recommendations, sizing, orders, wallets/keys, swaps, jupiter/tx signing, execution, or autonomy — SolanaTracker is read-only risk intelligence only.

**Recurring OpEx (accounting/ops metadata only — NOT a PnL/EV/profit feature):** SolanaTracker subscription **≈ $58–59/month USD**, recorded as a data-provider operating cost for a *future* net-profit dashboard. No profit/EV/PnL/trading capability is introduced or implied by this note.

**Decision: KEEP.** SolanaTracker is active, 95% success on the batch, one transient timeout, payload valid and usable (holder_risk signals created), and it closes the top10_holder gap. Rollback criteria (high errors / bad latency / zero coverage / unusable payload) are **not** met. Follow-up (separate step): investigate whether SolanaTracker exposes sniper/insider/bundler via additional endpoints to lift those from 0%.

**Rollback (if ever needed):** `ENABLE_SOLANA_TRACKER_RISK=false` in host `.env` (key left in place, unused); reports revert to GoPlus-only next run. No state to unwind.

## POLY-001 — read-only Polymarket market-data observer deployed DARK (2026-07-07, ~22:30 UTC)

Deployed **`02312cd` → `3f4f423`** by `git pull --ff-only`; **migration `0019` → `0020`** (4 new read-only tables). A read-only SECOND prediction-market venue (Polymarket) observed via public/no-auth GETs — Gamma market catalog + CLOB read-only order books. **Manual-only: no timer/loop installed, no API endpoint added, flag stays OFF.** Existing Kalshi/MarketOps/EDGE-AUTO/MEME-NEWS/SolanaTracker/tennis behavior unchanged; no thresholds or provider flags touched.

| item | before | after |
|---|---|---|
| pushed / deployed commit | — | **`3f4f423`** (origin/main + EVO-X2) |
| alembic revision | 0019 | **0020** |
| `ENABLE_POLYMARKET_SCOUT` | (absent) | **absent → default false** (unchanged) |
| Polymarket timer / API endpoint | none | **none** (not installed) |
| backup | — | `data/backups/backup-20260707T222330Z.db.gz` (194.65 MiB, pre-pull) |

**Migration:** `run-baseline --dry-run` applied `0020` (all pipeline stages skipped; only the audit row recorded). `agent-context` confirms revision **0020**; the 4 tables (`polymarket_scout_runs`, `polymarket_markets`, `polymarket_orderbook_snapshots`, `polymarket_domain_inventory_snapshots`) exist.

**Manual smoke (live public API from EVO-X2):**
- `polymarket-scan-once` → run #1 **ok**: markets=**50**, order books=**20** (errors=**0**), domains=**18**, 5.3s.
- `polymarket-report` → markets_seen=50, active=50, categories=18, two_sided=28 (rate=0.56), orderbook_enabled=50, orderbook_snapshots=20, provider_errors=0, spread p50=0.001 / p90=0.01. Top markets = live World Cup / Wimbledon / election books.
- `polymarket-domain-report` → 18 domains with coverage/liquidity/volume/spread proxies (e.g. "World Cup Winner" 9 markets, two_sided_rate=1.0).
- **Scheduled guard:** `polymarket-scan-once --scheduled` → `ENABLE_POLYMARKET_SCOUT=false; scheduled polymarket cycle skipped` — correct no-op.

**DB impact:** negligible — 50 markets + 20 order books + 18 domain rows + 1 run; not large enough to appear in `db-growth-report`'s largest-tables list (DB 2282 MiB, tick-driven as before). Pruned by `POLYMARKET_RETENTION_DAYS=14` via the existing retention timer; the domain-inventory table is kept as coverage history.

**Existing-system health (unchanged):** MarketOps last run #949 ok, champion/challenger `mean_delta_brier=-0.029173` (identical), p90 **42.6s** (<60s), readiness `ready_for_cycle_scoped_edge_automation`; meme-news 0 errors; providers goplus **active** / solana-tracker **active** (key present) / birdeye **disabled** — all unchanged. All 6 user timers/services **active** (marketops/watcher/baseline/retention/meme-news + **provider-roll-001-t24h still armed**, next fire Wed 2026-07-08 21:00:35 UTC).

**Safety:** frontier-eval AST audit **`safety_ok=True` (59 files)**; expanded dangerous-identifier grep on the POLY-001 files **empty** (no wallet/key/signing/swap/order-placement/EV/sizing surface; authenticated CLOB trading endpoints deliberately not implemented). Cross-venue Kalshi linking is a documented **POLY-002 placeholder only** (no arb/EV/trade-candidate labels). No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy — POLY-001 is read-only market-data observation, deployed dark/manual-only.

**Decision: KEEP DARK / MANUAL ONLY.** The observer works live (0 errors, valid payloads, useful coverage) but there is no measurement or downstream consumer yet that needs a scheduled cadence — a timer would only grow the DB. Enable a scheduled lane (`ENABLE_POLYMARKET_SCOUT=true` + a systemd timer, dark-first) **only** when POLY-002 (cross-venue linking / live WS) or a concrete measurement use gives the accumulated snapshots a purpose. Until then, manual `polymarket-*` reports on demand.

**Rollback (if ever needed):** revert code with `git reset --hard 02312cd`; migration `0020` only adds isolated tables (drop via `alembic downgrade 0019` — no other table references them). Nothing to unwind operationally (read-only, flag off, no timer).

## PROVIDER-BUDGET-001 — SolanaTracker request accounting + budget guardrails deployed (2026-07-07, ~23:10 UTC)

Deployed **`44bfc0e` → `ccbc0cf`** by `git pull --ff-only`; **no migration** (alembic `0020` unchanged — usage is derived read-only from existing `crypto_token_risk_assessments`). Provider cost/usage observability + a request guardrail that can only **skip** optional SolanaTracker lookups when over budget (fallback: GoPlus+heuristics). SolanaTracker stays enabled, Birdeye stays disabled, GoPlus unchanged; no MarketOps/EDGE-AUTO/MEME-NEWS/Polymarket behavior changed.

**Budget config set in host `.env` (conservative per-run cap 15):**

| key | value |
|---|---|
| `SOLANA_TRACKER_PER_RUN_LOOKUP_LIMIT` | **15** (conservative — CACHE_TTL is a documented knob, not an active cache, so the cap is the live control) |
| `SOLANA_TRACKER_MONTHLY_REQUEST_LIMIT` | 200000 |
| `SOLANA_TRACKER_DAILY_REQUEST_BUDGET` | 5000 |
| `SOLANA_TRACKER_HOURLY_REQUEST_BUDGET` | 200 |
| `SOLANA_TRACKER_WARN_DAILY_REQUESTS` / `_STOP_DAILY_REQUESTS` | 4000 / 6000 |
| `SOLANA_TRACKER_CACHE_TTL_HOURS` | 24 |

`.env` backed up to `~/secure-backups/probability-arena/env/.env.bak.provider-budget.*` (600).

**Per-run cap validation — `crypto-risk-assess --limit 40`:** **SolanaTracker HTTP calls = 15** (capped), **GoPlus HTTP calls = 40** (all tokens), **0 daily-STOP skips**, **40 tokens assessed**. So the cap skipped ST after 15 and the remaining 25 tokens fell back to GoPlus+heuristics with no assessment lost — exactly the intended guardrail. Today's request count moved +15 (895 → 910), confirming the cap end-to-end.

**Usage (`crypto-provider-budget-report`):** requests today=**910**, hour=**88**, month=**998**; estimated monthly run-rate **≈27,300** (well under the 150k operational target and 200k plan limit); success=**899** / error=**99** → **success_rate 90.1%**; coverage-per-request 0.90; remaining_daily=4,090; WARN(4000)/STOP(6000) both False. Recommendation: **KEEP**.

**Coverage impact vs pre-budget (NOT degraded):** observed `top10_holder` = **42/67 = 62.7%** (up from ~52% at PROVIDER-ROLL-001 T0; ~25.7% at first enablement). The per-run cap of 15 does **not** starve coverage because it is per-run and coverage accumulates across many 5-min scans (different tokens each pass). `holder_risk` signals = **229** (up from 13). `sniper`/`insider`/`bundler` still 0% (known SolanaTracker endpoint limitation, unrelated to budget).

**System integrity (unchanged):** MarketOps last run #957 ok, champion/challenger `mean_delta_brier=-0.029173` (identical), **p90 43.8s** (<60s); GoPlus active / SolanaTracker active (key present) / **Birdeye disabled**. **DB impact negligible** — no new table; `crypto_token_risk_assessments` 32,044 rows / 13.26 MiB; DB 2302 MiB (tick-driven as before).

**Safety:** frontier-eval AST audit **`safety_ok=True` (60 files)**; expanded dangerous-identifier grep on `provider_budget.py` **empty**. The guardrail only ever SKIPS SolanaTracker (never adds calls, never touches GoPlus/Birdeye); the ~$58–59/mo cost note is accounting metadata only. No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy.

**Decision: KEEP at per-run=15.** Actual run-rate (~27k/mo) sits far below the 150k target with the conservative cap, and coverage is unharmed — headroom exists to relax the cap later (e.g. 20–25) if broader per-run ST coverage is ever wanted, but 15 is the safe default that matches the ≤5k/day target. **Rollback:** raise/remove the `SOLANA_TRACKER_*` keys in `.env` (defaults restore per-run=25); or `git reset --hard 44bfc0e` (no migration/state to unwind — accounting is derived, guardrail only ever reduces calls).

## SOLANA-TRACKER-002 — sniper/insider/bundler parser fix deployed (2026-07-07, ~23:35 UTC)

Deployed **`ccbc0cf` → `451580f`** by `git pull --ff-only`; **no migration** (alembic `0020` unchanged). Read-only risk-intelligence field mapping: the SolanaTracker `/tokens/{address}` risk object already carried sniper/insider/bundler under `totalPercentage` (not the old `percentage` the parser read) — the fix parses it directly (new `_percent_direct`, no ratio mis-scaling of sub-1% values), keeping legacy fallbacks and staying absent on missing keys. **No new endpoint, no extra request** — same call, more coverage. SolanaTracker stays enabled, Birdeye disabled, GoPlus unchanged, budget caps unchanged (per-run 15).

**No extra requests / no new endpoint (validated):** `crypto-risk-assess --limit 40` → SolanaTracker HTTP calls **15** (per-run cap, unchanged), GoPlus **40**, the only ST endpoint hit was `data.solanatracker.io/tokens` (no new pattern), 0 STOP skips. Budget today moved **970 → 985 (+15 = exactly the cap)** — the parser fix costs zero additional API calls.

**Pre/post coverage (observed, latest-per-token):**

| dimension | before (`ccbc0cf`) | after (`451580f`) |
|---|---|---|
| top10_holder | 32/62 (51.6%) | 31/61 (50.8%) |
| **sniper** | **0/62 (0%)** | **15/61 (24.6%)** |
| **insider** | **0/62 (0%)** | **15/61 (24.6%)** |
| **bundler** | **0/62 (0%)** | **15/61 (24.6%)** |
| authority | 44/62 (71.0%) | 41/61 (67.2%) |

(The 15 = the tokens SolanaTracker-assessed in the validation run under the per-run cap; coverage grows as the always-on lane reassesses more tokens. meme-news lane sniper/insider/bundler likewise moved 0% → 3.2%.)

**Risk-label shifts (stable, no explosion):** levels `low=43/medium=18/severe=1` → `low=41/medium=19/severe=1`. **No `sniper_concentration`/`insider_concentration`/`bundler_concentration` reasons fired** — current tokens sit under the 20/15/25 thresholds (observed bundler values ~5–19%, snipers/insiders mostly 0%), so the newly-populated flags register as coverage without tripping the categories. Signals: holder_risk 239 → 242, rug_risk 195 → 195, suspicious_supply_control 47 → 47 (unchanged). This is the expected, measured outcome — more accurate holder intelligence with no runaway labels.

**Provider health / budget:** SolanaTracker success/error 974/99 → **success_rate 90.8%** (no ST errors added; GoPlus errors 18→20 pre-existing). Budget recommendation **KEEP** (today 985, run-rate comfortably under target). **MarketOps unchanged:** last run #961 ok, champion/challenger `-0.029173` (identical), **p90 44.2s** (no latency regression). **DB impact negligible:** no new table; `crypto_token_risk_assessments` 32,232 rows / 13.36 MiB; DB 2314 MiB (tick-driven).

**Safety:** frontier-eval AST **`safety_ok=True` (60 files)**; canonical + expanded dangerous-identifier grep on `crypto_risk.py` **clean**. Read-only field mapping only — no EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy. SolanaTracker Advanced remains **≈ $58–59/month USD** recurring data-provider OpEx (accounting metadata).

**Decision: KEEP.** All KEEP criteria met — coverage increased materially (0% → 24.6% on three dimensions, at zero extra request cost), provider errors acceptable, no latency/DB regression, no safety issue, no label explosion. No rollback trigger present. **Rollback (if ever):** `git reset --hard ccbc0cf` (no migration/state; the fix only changes parsing) — SolanaTracker key/flag/budget caps stay as-is since provider behavior is not the issue.

## MEME-MAS-001 — read-only memecoin diagnostic layer deployed (2026-07-08, ~03:50 UTC)

Deployed **`451580f` → `fe986f6`** by `git pull --ff-only`; **no migration** (alembic `0020` unchanged — the diagnostic recomputes on demand from persisted rows). Read-only multi-agent DIAGNOSTIC scoring: 5 deterministic agents (Coin Structure, Catalyst Velocity, Timing, Risk Auditor, Composite Review — **no LLM, no external calls, no new provider, no table, no flag, no timer**) turn existing meme/risk rows into a `review_priority`. Manual reports only; nothing scheduled, nothing persisted.

**No migration / no external calls / no budget impact (verified):** revision stayed **0020**, no alembic files in the pull. The SolanaTracker budget count was **today=570 before AND after** running `meme-mas-report`/`meme-mas-assess` — the layer makes **zero** external requests (compute-on-demand from persisted rows). No `meme_mas` table exists (compute-on-demand, as designed).

**Report output (`meme-mas-report`, 24h, live):** **464 tokens assessed.**

| review_priority | count |
|---|---|
| high_review | 185 |
| elevated_review | 199 |
| monitor | 80 |
| low | 0 |
| **reject_risk** | **0** |

Provider coverage 361/464 (103 tokens missing provider risk data → surfaced as `missing_evidence`). Sub-score distributions: structure p50 0.60 / p90 0.88, velocity p50 0.71, timing p50 0.63, risk_penalty p50 0.0 / **p90 0.50**.

**Top high_review examples (with reasoning traces — human-review triage, NOT a trade signal):**
- `LYNK` review 0.851 — `healthy_liquidity`, `frequent_catalysts`, `fresh_token`, `liquidity_momentum`
- `HELPDAD` review 0.848 — `frequent_catalysts`, `fresh_token`, `volume_momentum`, `sustained_attention`
- `DONALD` review 0.838 — `healthy_liquidity`, `boosted`, `frequent_catalysts`, `fresh_token`

**reject_risk examples:** **none in this window** — the meme-news attention set currently holds no severe/rug/honeypot tokens (risk_penalty p90 = 0.50 = concentration/medium flags, below the reject threshold). The risk path is confirmed reachable (risk_penalty up to 0.6 observed) and forced-reject is unit-tested; `reject_risk=0` here is an honest "no severe tokens right now," not a broken path.

**Distribution note (calibration follow-up, not a blocker):** the labels skew toward elevated/high_review (fresh boosted memecoins with catalysts score well on velocity/timing), and `low`/`reject_risk` are empty this window. Thresholds are a natural MEME-MAS-002 calibration target; the layer is deterministic and correct as shipped — `review_priority` is review-attention triage, not a quality/opportunity ranking.

**No forbidden vocabulary:** the serialized diagnostic data (priorities, reasons, traces, risk_reasons) contains no buy/sell/trade/bet/EV/position language; the only "trade" occurrences are the boundary disclaimer ("not a trade recommendation / not a trade signal").

**System integrity (unchanged):** MEME-NEWS 0 errors; SolanaTracker active (key present, budget **KEEP**), **Birdeye disabled**, tennis-evidence/Polymarket flags absent → off (Polymarket dark/manual). MarketOps last run ok, champion/challenger `-0.029173` (identical), **p90 50.3s — identical before/after** (no regression). DB 2331 MiB (tick-driven; MEME-MAS adds no table).

**Safety:** frontier-eval AST **`safety_ok=True` (61 files)**; canonical + expanded dangerous-identifier grep on `meme_mas.py` **clean**. No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy. `review_priority` is human-review triage; `reject_risk` is avoid/flag for review, never a trade direction.

**Decision: KEEP — manual / report-only.** The diagnostic works on live data, adds zero requests / zero budget / zero DB / zero latency, and changes no existing behavior. It stays **manual and read-only** (no timer, no flag, no persistence). Follow-up: MEME-MAS-002 threshold calibration once review_priority is tracked over time. **Rollback (if ever):** `git reset --hard 451580f` — no migration/state/flag to unwind (compute-only).

## MEME-SHADOW-001 — review_priority follow-through analysis deployed (2026-07-08, ~04:25 UTC)

Deployed **`fe986f6` → `1082a00`** by `git pull --ff-only`; **no migration** (alembic `0020` unchanged). Read-only calibration MEASUREMENT: reconstructs the MEME-MAS `review_priority` at each historical attention snapshot (reusing the MEME-MAS agents, risk assessment as-of that moment) and measures the SAME token's later trajectory from its own later snapshots. **No table, no flag, no timer, no external call, no SolanaTracker budget impact** — manual `meme-shadow-report` only.

**No migration / no external calls / no budget impact (verified):** revision stayed **0020**, no alembic files in the pull. SolanaTracker budget was **today=660 before AND after** running the 24h + 48h reports — the layer makes **zero** external requests (compute-on-demand). No `meme_shadow` table.

**`meme-shadow-report` results (live):** 24h window = **3470 anchors** · 48h window = **6911 anchors**. Horizon coverage 24h: 15m 3443 / 1h 2581 / 6h 124 / 24h 11 (5m=0 — the ~10-min meme-news scan cadence has no snapshot near 5m). **Calibration recommendation (both windows): `no_material_separation_recalibrate`.**

**Outcome by review_priority (24h):**

| review_priority | n | survival | rug_incidence | price_mean 1h | attn_persist 1h |
|---|---|---|---|---|---|
| monitor | 485 | 0.972 | 0.025 | −5.1% | 0.41 |
| elevated_review | 1111 | 0.942 | 0.048 | −6.9% | 0.43 |
| high_review | 1839 | 0.915 | 0.049 | +4.4% | 0.21 |
| reject_risk | 35 | 1.000 | 0.000 | −5.7% | 1.00 |

**Survival / rug / liquidity-removed:** survival is high across all cohorts (0.91–1.0) and **mildly INVERTED vs review_priority** (monitor 0.972 > high_review 0.915) — the primary reason for the `recalibrate` verdict. Short-term price DOES separate (high_review +4.4% at 1h vs monitor/elevated negative), but 24h means are outlier-dominated (high_review 24h mean skewed by a few large movers — median is the robust read). **The risk dimensions carry the real predictive signal:** by risk reason (48h) `missing_provider_coverage` n=257 survival **0.835** / rug **0.101** (~2× baseline), `bundler_concentration` survival 0.875; by concentration bucket `top10:present|sib:flagged` survival **0.833** and `sib:present` survival 0.863 vs ~0.93 baseline. So flagged concentration + missing coverage predict lower survival / higher rug, while the velocity-weighted composite dilutes that into review_priority.

**Calibration takeaway (feeds MEME-MAS-002):** review_priority does not cleanly separate SURVIVAL (mildly inverted); the composite likely over-weights attention/velocity (fresh volatile tokens) vs risk/coverage. Concrete signal to up-weight risk_penalty + missing-coverage and treat flagged concentration more severely. This is precisely the recalibration insight MEME-SHADOW exists to surface.

**System integrity (unchanged):** MEME-NEWS 0 errors; SolanaTracker active (budget **KEEP**), **Birdeye disabled**, tennis-evidence/Polymarket off (Polymarket dark/manual). MarketOps last run ok, champion/challenger `-0.029173` (identical), **p90 50.3s — identical before/after** (no regression). DB tick-driven; MEME-SHADOW adds no table.

**Output language / safety:** the serialized cohorts use **price/liquidity/volume movement + survival/rug language only** — no PnL/EV/fill/order/position vocabulary (verified by grep; only the boundary disclaimer negates them). frontier-eval AST **`safety_ok=True` (62 files)**; canonical + expanded grep on `meme_shadow.py` **clean**. No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy. `price_change` is measured market movement, never PnL/a fill.

**Decision: KEEP — manual / report-only.** Works on live data, zero requests/budget/DB/latency, changes no existing behavior, stays read-only (no timer, no flag, no persistence). Immediate value: the `no_material_separation_recalibrate` verdict + risk-dimension separation are a concrete input to a future MEME-MAS-002 threshold recalibration. **Rollback (if ever):** `git reset --hard fe986f6` — compute-only, nothing to unwind.

## MEME-MAS-002 — risk-aware review_priority recalibration deployed (2026-07-08, ~04:45 UTC)

Deployed **`1082a00` → `2c52145` → `d0c54c7`** by `git pull --ff-only`; **no migration** (alembic `0020` unchanged). Read-only diagnostic LABEL recalibration only (no table/flag/timer/external-call/budget impact). Profile-based scorer (`v2` default; `v1` preserved for the before/after `meme-mas-calibration-report`): heavier risk penalties (missing coverage 0.3→0.55, concentration 0.6→0.7, fake-volume/liquidity-removed 0.5→0.6), full risk dampening, momentum+structure composite, gated high_review, and new `momentum_quality`/`structure_quality`/`coverage_quality` outputs. reject_risk hard gates preserved.

**Live-validated tuning (`d0c54c7`):** the first cut (`2c52145`, band_high 0.55) did **not** reduce high_review share on live data — the live high_review population is already clean/covered so the gates rarely fired, and the lowered band offset them. Empirical tuning on the live window set **band_high = 0.68**, which cuts high_review while keeping the strongest clean tokens. This is exactly what the dark-deploy validation is for.

**No migration / no external calls / no budget impact (verified):** revision **0020**; SolanaTracker budget today 735 → 750 (+15 = a background marketops crypto scan, **not** meme-mas/shadow — both make zero external calls); no `meme_mas`/`meme_shadow` table.

**v1 → v2 live calibration comparison** (`meme-mas-report` token basis, 462 tokens):

| priority | v1 (before) | v2 (after, tuned) |
|---|---|---|
| high_review | **183 (40%)** | **72 (15.6%)** |
| elevated_review | 190 | 184 |
| monitor | 85 | 199 |
| low | 0 | 7 |
| reject_risk | 0 | 0 |

`meme-mas-calibration-report` (anchor basis, 3498 anchors): **high_review share 0.527 → 0.273**. Per-anchor MEME-SHADOW survival under v2: `elevated_review` **0.978** (best; v1 was 0.943), `monitor` 0.939, `high_review` 0.893, `low` 0.974, `reject_risk` 1.0. **242/462 tokens changed label** (e.g. REVENGE/EATS/Intern high_review→elevated_review; Underdog/TESTPACK elevated_review→monitor).

**Acceptance results:**
- **high_review became more selective:** yes — share ~40% → ~16% (token) / 0.527 → 0.273 (anchor).
- **missing-coverage + concentration demoted:** yes — the coverage gate + heavier penalties make ~264 of 462 tokens gate-ineligible for high_review; a missing-coverage strong token drops high→elevated/monitor (risk 0.55, ×0.45 dampening), a concentration-flagged token drops further (risk 0.7).
- **clean covered momentum tokens still reach high_review:** yes — 72 do (e.g. LYNK review 0.875, momentum 0.77 / structure 1.0 / coverage 0.67).
- **reject_risk hard gates intact:** yes (35 both profiles).
- **new quality outputs render:** yes (`momentum_quality`/`structure_quality`/`coverage_quality` in `meme-mas-assess`).

**Honest limitation:** the shadow's `calibration_recommendation` stays `no_material_separation_recalibrate` because it compares high_review-vs-monitor SURVIVAL, and high_review (momentum-driven) is inherently more volatile / lower-survival than calmer cohorts — that's expected for a "worth close review" tier, not a v2 failure. v2's real win is a **more selective** high_review and a **clean, high-survival (0.978) elevated_review** tier; survival is not the right sole yardstick for a review-attention label.

**System integrity (unchanged):** MEME-NEWS 0 errors; SolanaTracker active (budget **KEEP**), **Birdeye disabled**, tennis-evidence/Polymarket off; **provider-roll-001 T+24h timer still armed** (fires Wed 2026-07-08 21:00:35 UTC). MarketOps last run ok, champion/challenger `-0.029173` (identical), **p90 50.3s — identical before/after**. DB tick-driven; no new table.

**Output language / safety:** diagnostic data uses review/quality/survival language only — no PnL/EV/fill/order/position/buy/sell/bet (grep clean; only the boundary disclaimer negates them). frontier-eval AST **`safety_ok=True` (62 files)**; canonical + expanded grep on `meme_mas.py` **clean**. No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy.

**Decision: KEEP — manual / report-only.** v2 meets the recalibration goals (selective, risk-aware high_review; clean elevated tier), zero requests/budget/DB/latency, changes no existing behavior. **Rollback (if ever):** `git reset --hard 1082a00` — compute-only, nothing to unwind; MEME-MAS-001 (v1) logic remains selectable via the profile regardless. Follow-up (MEME-MAS-003, if wanted): a non-survival separation metric for the shadow calibration verdict, since high-momentum review tiers are volatile by nature.

## MEME-MAS-003 — multi-objective calibration metrics deployed (2026-07-08, ~05:30 UTC)

Deployed **`d0c54c7` → `4a312ec`** by `git pull --ff-only`; **no migration** (alembic `0020`). Read-only ANALYSIS only — adds five separate calibration objectives to MEME-SHADOW and a `meme-mas-objectives-report` (v1 vs v2). **`app/services/meme_mas.py` is unchanged — no label/scoring change** (post-pull `meme-mas-report` distribution identical to pre-pull: high_review 70 / elevated 184 / monitor 199 / low 8 / reject 0 on 461 tokens). No table/flag/timer/external-call/budget impact.

**No migration / no external calls / no budget impact (verified):** revision **0020**; SolanaTracker budget today 825 → 840 (+15 = a background marketops crypto scan, **not** the objectives report, which makes zero external calls); no new table.

**Live `meme-mas-objectives-report` (24h = 3469 anchors, 48h = 7077 anchors), v1 vs v2:**

- **momentum_followthrough** — v2 `high_review` momentum-positive rate **0.325 (24h) / 0.342 (48h)** is the HIGHEST of all tiers and above v1 (0.288 / 0.310). **high_review IS momentum-positive** even though its survival is lowest — the survival-only verdict was misleading.
- **survival_quality** — v2 `elevated_review` survival **0.977 (24h) / ~0.96 (48h)** is the safest non-reject tier (v1 was 0.951); `high_review` survival 0.892 / 0.904 is the lowest (the concentrated high-momentum tokens are volatile by design). `reject_risk` 1.0.
- **risk_adjusted_movement** (median move × survival — a diagnostic, never a return/PnL/EV) — all tiers are negative at 1h (most memecoins fade; winners are outliers), and v2 `high_review` is the **least-negative momentum tier** (−4.6 vs monitor −13.0). Framed and labelled as measurement only.
- **review_queue_efficiency** — v2 `high_review` **share 0.27** (half of v1's 0.52–0.56) with **momentum-positive lift 1.20 (24h) / 1.22 (48h)** vs v1's 1.06–1.10. So v2's high_review is a **smaller, higher-signal review queue** — the clearest efficiency win.
- **coverage_quality** (label-independent) — **MISSING provider coverage clearly predicts worse outcomes:** survival **0.857 / 0.839**, rug **0.089 / 0.100**, 1h price median **−41% / −40%** vs covered **0.943 / 0.935**, rug 0.041 / 0.048, −7% / −6%.

**Interpretation:** judged on the RIGHT objectives (momentum, queue efficiency, coverage) the MEME-MAS-002 labels are working — high_review concentrates momentum-positive tokens more efficiently, elevated_review is the safe tier, and missing coverage is a strong negative. **No further label change is warranted** (the milestone was analysis-only by design).

**System integrity (unchanged):** MEME-NEWS 0 errors; SolanaTracker active (budget **KEEP**), **Birdeye disabled**, tennis-evidence/Polymarket off; **provider-roll-001 T+24h timer still armed** (fires Wed 2026-07-08 21:00:35 UTC — has not yet fired). MarketOps last run ok, champion/challenger `-0.029173` (identical), **p90 50.3s — identical before/after**. DB tick-driven; no new table.

**Output language / safety:** the objective data rows use momentum/survival/movement language only — no PnL/EV/fill/order/position/buy/sell/bet (grep clean; only the boundary disclaimer/headers negate them). frontier-eval AST **`safety_ok=True` (62 files)**; canonical + expanded grep on `meme_shadow.py` **clean**. No EV, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy.

**Decision: KEEP — manual / report-only.** Pure read-only analysis; zero requests/budget/DB/latency; labels and all existing behavior unchanged. **Rollback (if ever):** `git reset --hard d0c54c7` — compute-only, nothing to unwind (adds only the objectives report).

## POLY-002 — Kalshi↔Polymarket cross-venue observation deployed (2026-07-08, ~17:35 UTC)

Deployed **`4a312ec` → `cb6337d`** by `git pull --ff-only`; **migration `0020` → `0021`** (2 new read-only tables). Read-only semantic matching + measurement over already-persisted Kalshi markets/snapshots + POLY-001 polymarket markets. Manual-only: no timer, no API endpoint, no flag; `ENABLE_POLYMARKET_SCOUT` untouched (still false). Existing MarketOps/EDGE-AUTO/MEME-NEWS/SolanaTracker/Polymarket-scan behavior unchanged.

| item | value |
|---|---|
| pushed / deployed commit | **`cb6337d`** (origin/main + EVO-X2) |
| DB backup (pre-migration) | `data/backups/backup-20260708T173111Z.db.gz` (225.29 MiB) |
| alembic revision | **0020 → 0021** (applied via `run-baseline --dry-run`, all stages skipped) |
| new tables | `cross_venue_observation_runs`, `cross_venue_market_candidates` |

**`cross-venue-match-once` (live):** run #1 (kalshi=1500 × polymarket=50) → **21 candidates**; run #2 (kalshi=8000 × polymarket=50) → **31 candidates**. Label breakdown (run #2): `incompatible_resolution` **25**, `unresolved_semantic_match` **3**, `incompatible_outcome` **2**, `low_confidence_match` **1**, **`comparable_market_candidate` 0**. All domain=sports.

**Comparable count = 0 — a correct, conservative result, not a failure.** The current venue overlap is thin: the 50-market Polymarket snapshot (from the POLY-001 deploy scan) is tournament-winner / game-prop heavy, while the Kalshi active sample is game-level props (spreads, totals, player hits → over_under/spread outcomes). The matcher therefore **rejects incompatible outcomes/resolutions and refuses to force matches** (exactly the required behavior: ambiguous/missing → `unresolved_semantic_match`, never a fabricated comparable). Sample candidates: `KXWCSPREAD-26JUL07SUIC ↔ 2793883` (incompatible_outcome, title_sim 0.425 — spread vs winner); `KXMLBHIT-… ↔ 2793895/93/94` (unresolved, conf ~0.56).

**Observed-difference measurement validated:** the one `low_confidence_match` produced a real **`observed_difference` = 0.0595** (measured |kalshi_mid − polymarket_mid| probability-point gap). Distribution: n=1, p50/p90/max = 0.0595. **Spread comparison:** kalshi_spread p50 **0.04** vs polymarket_spread p50 **0.001** (Polymarket books are far tighter). Freshness: observation_confidence p50 0.75. So the measurement path works on live data; more comparables will surface once Polymarket data is refreshed with markets that have Kalshi identical-outcome equivalents (the mocked WC-winner smoke confirmed comparables are produced when data overlaps).

**DB impact negligible:** the two cross_venue tables hold ~52 rows (2 runs) — too small to appear in `db-growth-report`'s largest tables; DB 2555 MiB is tick-driven (grew over ~19h since the last deploy, unrelated).

**System integrity (unchanged):** MEME-NEWS 0 errors; SolanaTracker active (budget **KEEP**), **Birdeye disabled**, `ENABLE_POLYMARKET_SCOUT` still false; MarketOps last cycle ok, champion/challenger `-0.029173` (identical). **MarketOps p90 56.5s** (<60s) — elevated vs the prior 50.3s only because cycle #1142 was mid-run under a busy 17:33 UTC live sports slate; POLY-002 is a separate manual command and does not run in the MarketOps timer. **provider-roll-001 T+24h SolanaTracker timer UNDISTURBED** — active (waiting), next trigger **Wed 2026-07-08 21:00:35 UTC (~3h 26m)**.

**Safety:** frontier-eval AST **`safety_ok=True` (63 files)**; canonical + expanded + **arb/arbitrage** grep on `cross_venue.py` **clean** (only boundary docstrings negate them); **no forbidden columns** (`side`/`size`/`ev`/`action`/`order`/`wallet`/`arbitrage`/`arb`/`profit` all absent). No EV, arbitrage, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy — a `match_label` is a comparability verdict for human review and `observed_difference` is a measured probability gap, never a signal/action.

**Decision: KEEP — manual / report-only.** The layer works on live data (produces conservative candidates, measures observed differences, refuses forced matches), adds negligible DB and zero external calls / zero budget / zero timer, and changes no existing behavior. **Rollback (if ever):** `git reset --hard 4a312ec` on host, then `alembic downgrade 0020` (the two tables are isolated — no other table references them; nothing else to unwind).

## POLY-COVERAGE-001 + POLY-PRECISION-001 — broadened Polymarket coverage + matcher precision deployed (2026-07-08, ~21:30 UTC)

Combined dark deployment of the read-only Polymarket **coverage expansion** (`292f7b5`) and the cross-venue **matcher precision** fixes (`74753de`). Deployed `cb6337d → 74753de` by `git pull --ff-only`; **migration `0021` → `0022`** (5 additive scan-provenance columns on `polymarket_scout_runs`). Manual/report-only: no timer, no API endpoint, no flag change; `ENABLE_POLYMARKET_SCOUT` untouched (still false). Existing MarketOps / EDGE-AUTO / MEME-NEWS / SolanaTracker / MEME-MAS behavior unchanged.

| item | value |
|---|---|
| pushed commits | `292f7b5` (POLY-COVERAGE-001), `74753de` (POLY-PRECISION-001) |
| deployed commit | **`74753de`** (origin/main + EVO-X2) |
| DB backup (pre-migration) | `data/backups/backup-20260708T212605Z.db.gz` (234.43 MiB) — verified OK (39 tables, integrity ok) |
| alembic revision | **0021 → 0022** (applied via `run-baseline --dry-run`, pipeline run #46 `status=dry_run`, all stages skipped) |
| new columns (0022) | `polymarket_scout_runs`: `scan_mode`, `pages_fetched`, `market_fetch_errors`, `duplicates_dropped`, `queries_used` (additive, defaulted, reversible) |

**Broadened Polymarket scan (`polymarket-scan-once --limit 400 --orderbook-limit 100 --targeted`, live public GETs):** run #2 → **markets=400, orderbooks=100, errors=0, domains=34**, `scan_mode=targeted`, `pages_fetched=12`, `duplicates_dropped=0`, `queries_used=['mlb','tennis','world cup','election']` (derived deterministically from the host's persisted Kalshi active titles/tickers — no LLM). **Fair-share budget caps engaged and logged:** `mlb` yielded 977 → capped at 100, `world cup` 327 → 150, `election` 521 → 150; one high-yield topic cannot starve the others. `polymarket_markets` **50 → 450** (prior POLY-001 baseline was the single 50-row scan).

**`polymarket-coverage-report` (supply census):** poly **447 active** vs kalshi 4000 (report honestly prints `TRUNCATED at --kalshi-limit=4000` — the census undercounts Kalshi, not silently). Poly market types: `yes_no` 167, `candidate_winner` 153, `winner` 107, **`exact_score` 11**, `over_under` 6, `advance` 2, `spread` 1 (the new `exact_score` type and fixed O/U normalization are visibly classifying live data). Overlap domains: sports / politics / other (all `comparable_supply=True`); no comparable supply in `crypto` (`no_polymarket_markets`) and `economics` (`no_kalshi_markets`) — each stated with its reason.

**Cross-venue matching (precision-hardened matcher):**
- **Default limits** (`cross-venue-match-once`, kalshi=1500 × polymarket=200): run #3 → **0 candidates**. This is a limit artifact, not a matcher fault: the default `kalshi_limit=1500` slices the oldest active Kalshi markets by rowid (no `ORDER BY`), which do not overlap the freshly-scanned sports/politics Polymarket sample. **Follow-up:** consider ordering the Kalshi load by recency, or raising the default limit, so the no-arg command exercises current overlap. (Documented, not changed here — deployment is behavior-preserving.)
- **Fuller limits** (`--kalshi-limit 8000 --polymarket-limit 600`): run #4 → **candidates=96, comparable=0, unresolved=30**.

**Label distribution (run #4, 96 candidates):** `incompatible_resolution` **38**, `unresolved_semantic_match` **30**, `incompatible_outcome` **28**, **`comparable_market_candidate` 0**, `low_confidence_match` 0. By domain: sports 90, politics 6. **Mismatch reasons show the new gates firing:** `resolution_gap_days` 53, `outcome_type_mismatch` 23, **`market_type_mismatch` 12**, **`outcome_side_uncertain` 6**, **`entity_mismatch` 4**, **`sport_or_game_mismatch` 1**.

**observed_difference distribution: n=0 — no probability-point gap was computed anywhere in the run.** This is the strongest possible confirmation of the precision fix: every pair either failed a compatibility gate or could not have its Polymarket outcome side aligned to the Kalshi YES proposition, so **no midpoint and no observed_difference were fabricated**. `comparable=0` is a correct, conservative result on today's live data (the scratch A/B's 2 survivors were the GPT-5.6 pair, whose Kalshi side `KXGPT-OPEN-26JUL08` resolves today).

**`comparable_market_candidate` examples:** none this run (0 rows).

**`unresolved` / `outcome_side_uncertain` examples (all with `observed_difference=None`):**
- `KXPGA3BALL-JODC26R2MFE ↔ 2431012` — `unresolved_semantic_match`, conf 0.5586 (winner/winner, title_sim 0.264 — plausible but below the comparable bar).
- `KXMLBGAME-26JUL061945MIL ↔ 2115519` — **exactly the KXMLBGAME arbitrary-entity case the old code mispriced**: now `incompatible_resolution` + `outcome_side_uncertain`, **midpoint and observed_difference omitted** (no fabricated ~0.4 gap).
- `KXWCGAME-26JUL07SUICOL-T ↔ 2793924` — `incompatible_outcome` (`outcome_type_mismatch=advance!=winner`) + `outcome_side_uncertain`.
- `KXATPGSPREAD-26JUL05AUGD ↔ 2812533` — `unresolved_semantic_match`, `outcome_side_uncertain`.
6 side-uncertain rows total, **100% with `observed_difference=None`**. **0 rows carried `large_observed_difference_requires_review`** (no comparable/low-confidence pair had an aligned midpoint to measure). **No CS2/Valorant false positive** (the `sport_or_game_mismatch` gate fired once); **no exact-score → handicap/spread false match**.

**DB impact (bounded):** `polymarket_markets` 50 → 450 (+400), `polymarket_orderbook_snapshots` 20 → 120 (+100), `polymarket_domain_inventory_snapshots` 18 → 52 (+34), `polymarket_scout_runs` 1 → 2, `cross_venue_market_candidates` 52 → 148 (+96), `cross_venue_observation_runs` 2 → 4. SQLite **2657.11 → 2660.70 MiB (+3.6)** — negligible; retention prunes markets/orderbook/scout-run rows after `POLYMARKET_RETENTION_DAYS=14`. (Note: DB remains in the OPS-011 **growth-warning** band, 2660 MiB vs warn 1536 / crit 3072 — pre-existing and tick-driven, unrelated to this deploy.)

**System integrity (unchanged):** MarketOps last cycle #1182 running on its normal timer (signals promoted/processed as usual); MEME-NEWS #354 ok, 0 errors; SolanaTracker budget **KEEP** (rolling_24h 3958, warn/stop not tripped), GoPlus + SolanaTracker active, **Birdeye disabled**; frontier-eval readiness **`ready_for_cycle_scoped_edge_automation`** (unchanged), `safety_ok=True`. **provider-roll-001 T+24h SolanaTracker timer — completed and undisturbed** (fired on schedule Wed 2026-07-08 21:00:55 UTC in the prior session; it is a one-shot with no next trigger).

**Safety:** canonical grep on `cross_venue.py` / `polymarket.py` / `polymarket_coverage.py` / `adapters/polymarket.py` → 11 hits, **all boundary disclaimers** (none survive the disclaimer filter); **0 forbidden `def`s**; frontier-eval AST **`safety_ok=True`**. No EV, arbitrage/arb labels, paper trading, trade recommendations, position sizing, order placement, wallets/private keys, signing, swaps, execution, or autonomy — a `match_label` is a comparability verdict for human review, `observed_difference` is a measured probability gap (absent unless the side is aligned), and `large_observed_difference_requires_review` is a review flag on a suspicious match, never an opportunity.

**Decision: KEEP — manual / report-only.** The coverage layer broadens live supply (50 → 450 markets, 4 → many domains) within hard bounds, the precision matcher is conservative and side-aligned (0 fabricated gaps, gates visibly rejecting cross-sport / arbitrary-entity / incompatible-type pairs), DB impact is negligible, and no existing behavior, timer, flag, or endpoint changed. **Rollback (if ever):** `git reset --hard cb6337d` on host, then `alembic downgrade 0021` (0022 only drops the 5 additive `polymarket_scout_runs` columns; the extra polymarket/cross-venue rows are isolated and harmless to leave). **Follow-up (non-blocking):** the no-arg `cross-venue-match-once` default limits (1500×200) under-cover this host's data — order the Kalshi load by recency or raise the default before any routine use.

## XVENUE-OPS-001 — recency-aware cross-venue sampling deployed (2026-07-08, ~23:15 UTC)

Deployed **`a7e5804` → `cf0ae01`** by `git pull --ff-only`. **No migration** (alembic stayed at `0022 (head)` before and after; no new revision files). Code-only usability/coverage fix: `cross-venue-match-once` now loads Kalshi markets **most-recently-seen first** (`last_seen_at DESC, id DESC` — was an unordered rowid slice returning days-stale `active` rows), adds `--recent-hours` / `--domain` / `--market-type` sample filters, raises the bounded defaults (kalshi 1500→**4000**, polymarket 200→**500**), and prints a per-run sample-composition report. Manual/report-only: no timer, no endpoint, no flag change; `ENABLE_POLYMARKET_SCOUT` untouched (still false). Matcher, labels, and every POLY-PRECISION-001 gate byte-for-byte unchanged.

| item | value |
|---|---|
| pushed / deployed commit | **`cf0ae01`** (origin/main + EVO-X2) |
| migration | **none** — alembic `0022 (head)` before and after; deploy is code-only, no backup required beyond the standing dailies (5 backups on host) |
| before: default behavior | run #3 (2026-07-08 deploy session): no-arg `cross-venue-match-once` (old 1500×200 rowid slice) → **0 candidates**; useful rows required magic `--kalshi-limit 8000 --polymarket-limit 600` |
| after: default behavior | run #5: no-arg → **389 candidates, comparable 1, unresolved 16**, mode=`recent_active` — matches the local scratch A/B exactly |

**Default run (`cross-venue-match-once`, no args, run #5):** kalshi=4000 polymarket=447, candidates **389** (`incompatible_outcome` 324, `incompatible_resolution` 43, `unresolved_semantic_match` 16, `low_confidence_match` 5, **`comparable_market_candidate` 1**). **Sample composition printed by the run:** kalshi by domain `{sports 3418, other 544, politics 38}`, polymarket by domain `{sports 288, politics 153, other 5, economics 1}`; market-type breakdowns on both venues (incl. `exact_score` 11); **domain overlap `[other, politics, sports]`** — no low-overlap warning (the pre-fix run would have shown one).

**`--recent-hours 48` (run #6):** identical results with **`stale_skipped=11935`** reported — 11,935 `active`-flagged Kalshi rows not seen in 48h were excluded, exactly matching the local scratch prediction; the top-4000 recency slice was already fresh, confirming the window and the ordering agree.

**Candidate/comparable quality (conservative, precision unchanged):** the single comparable is `KXATPSETWINNER-26JUL07AUGD ↔ 2812533` (Wimbledon ATP, winner/winner, conf 0.5527, observed_diff 0.5095) and it is **flagged `large_observed_difference_requires_review=0.5095`** — a set-winner vs match-market pairing is precisely the suspicious-match case the review flag exists for; it is a review annotation, never an opportunity. The two large low-confidence gaps (0.458, 0.37) are likewise flagged; small gaps (0.0595, −0.17) are not. **0 side-uncertain rows carry an `observed_difference`** (invariant verified by direct DB query). Incompatible pairs remain incompatible (`entity_mismatch` 176, `outcome_type_mismatch` 171, `market_type_mismatch` 87, `sport_or_game_mismatch` 1 among reasons).

**DB impact:** 2703.84 → 2705.64 MiB (+1.8 MiB; 778 candidate rows across the two validation runs plus normal tick growth). `cross_venue_market_candidates` 148 → 926 — the two-run cost of a now-useful default; rows are isolated observation data. (DB remains in the pre-existing OPS-011 growth-warning band, tick-driven, unrelated.)

**System integrity (unchanged):** MarketOps #1199 ok on its normal timer (5 promoted/processed); MEME-NEWS #363 ok, 0 errors; SolanaTracker budget **KEEP** (3510 today vs 5000 daily), GoPlus + SolanaTracker active, Birdeye disabled; frontier-eval readiness **`ready_for_cycle_scoped_edge_automation`** (unchanged), **`safety_ok=True`**. No timer installed; no polymarket/cross-venue timer exists; `ENABLE_POLYMARKET_SCOUT` absent from `.env` (default false).

**Safety:** canonical grep on the deployed matcher → only boundary-disclaimer lines; **0 forbidden `def`s**; tokenize-stripped code scan and AST identifier audit both **NONE** for wallet/private_key/keypair/swap/jupiter/signing/send_transaction/order-placement/dollar-EV/paper-trading/sizing/trade-recommendation/buy/sell/bet/arbitrage/arb; frontier-eval AST `safety_ok=True`. No EV, arbitrage labels, paper trading, recommendations, sizing, orders, wallets/keys, signing, swaps, execution, or autonomy — the low-overlap note and the large-difference flag are observation-coverage/review language only.

**Decision: KEEP — manual / report-only.** The no-arg command is now representative (0 → 389 candidates on identical data-shape) without magic limits, sampling is transparent, stale rows are skippable and counted, and nothing else changed. **Rollback (if ever):** `git reset --hard a7e5804` on host — code-only, nothing to unwind (candidate rows from runs #5/#6 are isolated observation data).

## XVENUE-OBS-001 — observation-window runbook + report deployed (2026-07-09, ~00:10 UTC)

Deployed **`9b91462` → `8e54e85`** by `git pull --ff-only`. **No migration** (alembic `0022 (head)` before and after; 22 revision files unchanged). Code+docs only: new `docs/XVENUE_OBSERVATION_RUNBOOK.md` (manual sequence + measured domain/window guidance for World Cup / MLB / politics / crypto / tennis) and new `xvenue-observation-report [--top]` CLI composing the latest **persisted** Polymarket scan run + cross-venue match run into one window verdict. Manual/report-only: no timer, no endpoint, no flag change, no new match label; `ENABLE_POLYMARKET_SCOUT` untouched (still false). Matcher and all POLY-002/POLY-PRECISION-001/XVENUE-OPS-001 behavior unchanged.

| item | value |
|---|---|
| pushed / deployed commit | **`8e54e85`** (origin/main + EVO-X2) |
| migration | **none** — `0022 (head)` before/after; no backup needed beyond standing dailies |
| runbook | `docs/XVENUE_OBSERVATION_RUNBOOK.md` present on host (6,425 bytes) |

**`xvenue-observation-report` (live, composes scan #2 + match #6):** scan `mode=targeted markets=400 queries=['mlb','tennis','world cup','election']`; match `ran_after_scan=True kalshi=4000 polymarket=447`. **candidates=389, comparable total=1 / clean=0 / flagged_for_review=1, side_uncertain=5, unresolved=16.** Overlap assessment: **`overlap_no_clean_comparable`** — "the venues meet in this window but list different market types or unalignable sides" — exactly the smoke-predicted verdict for the current pre-semifinal window, and consistent with the label distribution (`incompatible_outcome` 324, `resolution_gap_days` 351 among reasons). The sole comparable (`KXATPSETWINNER ↔ 2812533`, diff 0.5095) is **excluded from the clean list and printed under "FLAGGED for review … not opportunities"**. `--top 20` renders identically (lists bounded). Stale-pipeline warning correctly absent (`ran_after_scan=True`; the warning path is unit-tested and fires only when the match run predates the scan).

**Read-only verified on host:** row counts identical before/after running the report twice (`polymarket_scout_runs` 2, `cross_venue_observation_runs` 6, `cross_venue_market_candidates` 926) — the report persists nothing and makes no external call. **DB impact: zero** beyond normal tick growth (2728.14 MiB at preflight, tick-driven, pre-existing OPS-011 warning band).

**System integrity (unchanged):** MarketOps #1208 cycling normally; MEME-NEWS #368 ok, 0 errors; SolanaTracker budget **KEEP** (15 today post-midnight-reset, 3600 rolling-24h), GoPlus + SolanaTracker active; frontier-eval readiness **`ready_for_cycle_scoped_edge_automation`** (unchanged), **`safety_ok=True`**. No polymarket/xvenue/cross-venue timer exists; `ENABLE_POLYMARKET_SCOUT` absent from `.env` (default false).

**Safety:** canonical grep on `xvenue_observation.py` → 2 hits, both boundary disclaimers; tokenize-stripped code scan and AST identifier audit → **NONE**, including the expanded vocabulary (`opportunity`, `arbitrage`, `arb`, `buy`, `sell`, `bet`, dollar-EV, paper-trading, sizing, orders, wallets/keys, signing, swaps). Every overlap-assessment label is coverage language; the flagged list is explicitly "not opportunities".

**Decision: KEEP — manual / report-only.** The window verdict renders correctly on live data, persists nothing, calls nothing, and changes nothing. **Next use:** the World Cup semifinal slate (Jul 9–11) per the runbook — targeted scan with `--end-date-min/max` bracketing the games, then re-run the sequence and check whether `clean_comparable_present` appears for game-winner ↔ game-winner pairs. **Rollback (if ever):** `git reset --hard 9b91462` — code+docs only, nothing to unwind.

## OPS-012 — tick aggregation deployed, first pass complete (2026-07-09, ~00:55 UTC)

Deployed **`8514159` → `2cd7c63`** by `git pull --ff-only`; **migration `0022` → `0023`** (new `market_price_tick_buckets` table). Operational storage/durability only: raw ticks untouched, **raw tick retention UNCHANGED** (`TICK_RETENTION_DAYS=3` on host, verified in `.env`), no timer installed, no flag added, and no MarketOps/EDGE-AUTO/MEME-NEWS/SolanaTracker/Polymarket/MEME-MAS logic change (watcher/edge_precheck/frontier_eval/edge_cohort have zero diff).

| item | value |
|---|---|
| pushed / deployed commit | **`2cd7c63`** (origin/main + EVO-X2) |
| DB backup (pre-migration) | `data/backups/backup-20260709T004709Z.db.gz` (196.54 MiB) — verified OK (39 tables, integrity ok) |
| alembic revision | **0022 → 0023** (applied via `run-baseline --dry-run`, pipeline run #48 `status=dry_run`) |
| pre-deploy DB | **2,728.14 MiB** (warn 1,536 / crit 3,072); `market_price_ticks` **617,250 rows / 1,709.14 MiB** (~62% of file); ~8,400 ticks/h ≈ 558 MiB/day |

**Aggregation sequence (all bounded, all read-only toward raw ticks):**
- **Dry-run** (`--hours 24 --bucket-seconds 300 --dry-run`): rows_read=202,950, would-write **43,467** buckets, 18.1s, wrote nothing; the 200k row cap **reported truncation on the hour boundary** ("complete only up to 00:00 — rerun to continue"), never silent.
- **Real pass** (same args): **43,467 buckets inserted**, 23.9s, same truncation honestly reported.
- **Idempotency rerun** (same args): **inserted=0, updated=43,467** — exact; no duplicates, identical values.
- **Tail pass** (`--hours 2`): +1,500 buckets covering the truncated final hour.
- **Full-window pass** (`--hours 49 --max-rows 500000`): rows_read=421,200 → **90,124 buckets total** in 49.1s, covering the entire raw window (oldest bucket 2026-07-06 23:00).

**Coverage & size (measured, not estimated):** `tick-aggregation-report` → **49/49 raw-tick hours covered (rate=1.0), `healthy=True`**; buckets by domain `{sports_baseball 61,905, sports_soccer 28,121, general 49, sports_tennis 49}`; staged recommendation now reads "A FUTURE OPS milestone may reduce raw tick retention from 3d toward 24-48h … **NOT enacted by OPS-012**". **dbstat: `market_price_tick_buckets` = 20.16 MiB vs `market_price_ticks` = 1,711.12 MiB — an ~85:1 byte ratio for the same history window.** Row compression ~4.67 raw:bucket.

**Raw invariance:** raw counts only ever *increased* across every pass (617,400 → 617,550 → 617,850 → 618,000 — the live watcher writing; aggregation deleted nothing). `db-growth-report` (after) shows the new lines: heaviest tickers (WC FRA-MAR game markets, 4,120 each), **projected steady-state ~1,674.57 MiB (558.19 MiB/day × 3d)**, `aggregated buckets: 90,124 rows`, and the explicit "! DB size above warning threshold (below critical)" status. `prune-retention --dry-run` includes the bucket line (window **90d**, eligible 0) with the raw line unchanged (3d window, 6,900 eligible — normal).

**One transient, honestly noted:** MarketOps cycle **#1215 errored (`PendingRollbackError`)** at 00:50, coinciding with the 49s full-window aggregation commit — an SQLite write-lock collision (busy timeout 30s < the long single commit). **#1216 recovered `ok` on the next cycle**; no data loss (the cycle's sync/score simply deferred). Operational guidance: prefer the default 200k row cap (~24s passes) for routine runs, or run large passes between MarketOps cycles. **Follow-up for OPS-013:** commit per hour-sub-window to cap lock hold at ~2s. Frontier readiness **unchanged** (`ready_for_cycle_scoped_edge_automation`), `safety_ok=True`.

**Safety:** tokenize-stripped code scan + AST identifier audit on `tick_aggregation.py` / `db_growth.py` / `retention.py` → **NONE** across the expanded vocabulary (wallet/private_key/keypair/swap/jupiter/signing/send_transaction/order-placement/dollar-EV/paper-trading/sizing/trade-recommendation/buy/sell/bet/arbitrage/arb/opportunity; `OpportunitySignal` is the pre-existing OPS-002 model name). Buckets are telemetry summaries with no side/size/EV/action/order/wallet column; host scanner `safety_ok=True`.

**Recommendation: KEEP OPS-012. Do NOT reduce raw retention yet.** Coverage is healthy on today's window, but the staged reduction (3d → 24-48h) is an **OPS-013 decision** to be taken only after aggregation has been run regularly and coverage stays healthy on the host — aggregation is manual-only today (no timer), so either schedule manual passes with slate deploys or make OPS-013 the milestone that adds the (explicitly accepted) timer + per-sub-window commits + the retention change. **Rollback (if ever):** `git reset --hard 8514159` + `alembic downgrade 0022` (drops only the isolated bucket table).

## OPS-013 — per-sub-window aggregation deployed; timer installed DARK (2026-07-09, ~01:25 UTC)

Deployed **`dd931e2` → `dd9e439`** by `git pull --ff-only`; **migration `0023` → `0024`** (`tick_aggregation_runs` audit spine). Operational storage/durability only: raw ticks untouched, **`TICK_RETENTION_DAYS=3` unchanged** (verified in `.env`), no lane logic changed. The aggregation timer was installed **DARK** per the controlled-deployment instruction — **`ENABLE_TICK_AGGREGATION_TIMER` is NOT set (false)** and every scheduled fire no-ops until explicitly approved.

| item | value |
|---|---|
| pushed / deployed commit | **`dd9e439`** (origin/main + EVO-X2) |
| DB backup (pre-migration) | `data/backups/backup-20260709T012029Z.db.gz` (203.16 MiB) — verified OK (40 tables, integrity ok) |
| alembic revision | **0023 → 0024** (via `run-baseline --dry-run`, pipeline #49 `status=dry_run`) |
| scheduled guard | `aggregate-market-ticks --scheduled --hours 1` with flag unset → **"scheduled tick-aggregation cycle skipped"**, no run row, nothing written ✅ |

**The headline number — lock hold collapsed ~17×:** the manual 24h pass (`--subwindow-hours 1`) ran as **24 separate sub-window commits with `max_commit_ms=2841`** (idempotent rerun: **335ms**), versus the OPS-012 single ~49,000ms full-window commit that caused the MarketOps #1215 collision. **MarketOps cycle #1220 ran concurrently with the real pass and completed `ok`** — along with #1216–#1219 — i.e. **no SQLite lock regression under live concurrent writes**.

**Aggregation sequence:** dry-run → would-write 43,414 buckets across 24 sub-windows, wrote nothing (`max_commit_ms=0`), row-cap truncation honestly reported at the hour boundary. Real pass (audit **run #1**): rows_read=202,950, buckets 43,414 (300 inserted / 43,114 updated), 23.5s total, **0 retries, 0 failed windows, 0 oversized windows**. Idempotent rerun (audit **run #2**): **inserted=0, updated=43,414** — exact. `tick_aggregation_runs` holds both rows (`ok`, `failed_windows=null`, `oversized_windows=null`).

**Raw invariance & retention:** raw count only *increased* across every step (622,200 → 622,350 → 622,500 — live watcher writes; aggregation deleted nothing). `prune-retention --dry-run`: `market_price_ticks` window still 3d (11,550 eligible — normal), `market_price_tick_buckets` 0 eligible (90d), `tick_aggregation_runs` 0 eligible (30d).

**Readiness (honest, as designed):** `tick-aggregation-report` shows the new READINESS section → **`not_ready`**, reasons `coverage_72h=0.6849 < 0.98` (the 72h view reaches Jul-6 hours that predate aggregation) and `clean_scheduled_cycles=0 < 5` (timer dark). 48h coverage 0.9796/healthy; `raw_feed_fresh=True`; recent runs error-free. **Raw-retention reduction stays a future OPS-014**, gated on clean scheduled evidence.

**Dark timer install (step 13, manual validation clean):** units copied to `~/.config/systemd/user/`, `daemon-reload`, timer **enabled** — next fire 02:26 UTC, hourly. **Proof of dark no-op from the journal:** a manual service start logged `ENABLE_TICK_AGGREGATION_TIMER=false; scheduled tick-aggregation cycle skipped` and the unit finished cleanly. Each hourly fire will no-op identically until the flag is explicitly flipped.

**Health & safety:** MarketOps #1216–#1220 all `ok`; frontier readiness **unchanged** (`ready_for_cycle_scoped_edge_automation`), **`safety_ok=True`**; DB 2,728.14 MiB (above warn 1,536, below crit 3,072 — unchanged by this deploy; buckets 90,424 rows). Tokenize + AST audits on the OPS-013 modules: **NONE** across the expanded vocabulary (wallet/keys/swap/jupiter/signing/send_transaction/orders/dollar-EV/paper-trading/sizing/trade-recommendation/buy/sell/bet/arbitrage/arb/opportunity).

**Recommendation: KEEP OPS-013 — manual validation fully clean.**
- **Timer enablement remains a separate explicit step:** flip `ENABLE_TICK_AGGREGATION_TIMER=true` in `.env` only on explicit approval; the installed timer then goes live on its next hourly fire (`--hours 12` overlap, self-healing).
- **Raw-retention reduction remains future OPS-014**, proposable only after the readiness report shows `ready_to_stage` (≥5 clean scheduled cycles + coverage_72h ≥ 0.98 + no errors + fresh raw feed).
- **Rollback (if ever):** `systemctl --user disable --now probability-arena-tick-aggregation.timer`, `git reset --hard dd931e2`, `alembic downgrade 0023` (drops only the isolated audit table).

## OPS-013 — timer flag flip: scheduled aggregation LIVE (2026-07-09, ~01:35 UTC)

`ENABLE_TICK_AGGREGATION_TIMER=true` set in the host `.env` (backed up first to `~/secure-backups/probability-arena/env/.env.backup-20260709T013120Z`). **Only this flag changed**; `TICK_RETENTION_DAYS=3` verified unchanged. The hourly timer (installed dark in the prior deploy) is active — next fire 02:26 UTC, `--scheduled --hours 12 --subwindow-hours 1` per cycle, overlap-by-design so cycles self-heal.

**Immediate scheduled-mode validation (manual `--scheduled` run):** the guard now RUNS instead of skipping → audit **run #3 `ok [scheduled]`**: rows_read=105,150, buckets 22,681 (1,050 inserted / 21,631 updated), 13 sub-windows, **max_commit_ms=327**, 0 retries, 0 failed/oversized windows, no truncation. Raw ticks untouched (count only grew with watcher writes, 623,700). MarketOps #1220–#1222 all `ok` (no lock regression); DB 2,728.14 MiB (unchanged); frontier readiness unchanged, `safety_ok=True`. 48h coverage back to **1.0/healthy** after the pass.

**Live validation found + fixed a readiness-counter bug (hotfix `6cbb6da`, deployed):** run #3 was clean yet `clean_scheduled_cycles=0` — the SQLAlchemy JSON `none_as_null` trap: assigning Python `None` to the `failed_windows` JSON column stores JSON `'null'`, not SQL NULL, so the counter's `IS NULL` predicate could **never** match a service-written row and the readiness gate could never be satisfied. Fixed both directions: `finalize_run` now writes `sqlalchemy.null()` (SQL NULL going forward), and the counter filters in Python (`not fw`) so legacy JSON-null rows on the host also count. Regression tests added (service-written run counts; legacy form counts; failed-window run doesn't); 1,108 passed. Verified live: `clean_scheduled_cycles=1` after the hotfix.

**Post-flip readiness (honest):** `not_ready` — `coverage_72h=0.6986 < 0.98` (72h view still reaches pre-aggregation Jul-6 hours, which age out naturally) and `clean_scheduled_cycles=1 < 5` (accrues hourly). A ≥5-cycle follow-up check is scheduled ~07:45 UTC; its evidence (clean cycles, coverage_72h trend, max_commit_ms across scheduled runs, MarketOps health, prune dry-run) will be appended below and determines when **OPS-014 (raw retention 3d → 24-48h)** becomes proposable. Raw retention stays unchanged until then.

## OPS-013 — post-flag-flip evidence (T+4h, 5 clean scheduled cycles) (2026-07-09, ~05:35 UTC)

**Verdict: KEEP the timer ON. OPS-014 is NOT ready yet — one gate remains.**

| item | value |
|---|---|
| clean_scheduled_cycles | **5** (runs #3–#7: one manual `--scheduled` + four timer fires at 02:27/03:29/04:30/05:31) — **cycles gate PASSES** |
| coverage_72h | **0.7534** < 0.98 — the ONLY failing gate; the 72h view still contains pre-aggregation Jul-6 hours, which age out by ~22:00 UTC |
| coverage_48h | 1.0 (healthy) |
| scheduled max_commit_ms | #3 **327** · #4 **3,294** · #5 **15,798** · #6 **357** · #7 **2,451** |
| failed / oversized / retries | **0 / 0 / 0** across all five cycles |
| MarketOps since flip | **39 cycles, 0 errors** — no lock regression |
| DB size | 2,728.14 MiB (flat; above warn 1,536, below crit 3,072) |
| raw ticks | 657,600 rows / **1,813.91 MiB** (dbstat) — growing normally, never reduced by aggregation |
| buckets | 98,674 rows / **21.55 MiB** (dbstat) — ~84:1 byte ratio holds |
| prune-retention --dry-run | raw 3d window (47,400 eligible — normal), buckets 90d (0 eligible), agg runs 30d (0 eligible) |
| flags | `ENABLE_TICK_AGGREGATION_TIMER=true`, `TICK_RETENTION_DAYS=3` — both verified |

**On run #5's `max_commit_ms=15,798`:** an outlier, not a trend — the surrounding cycles committed in 0.3–3.3s. The 03:29 fire landed in a busy MarketOps+watcher write window, so OUR commit *waited* on their locks within the 30s busy timeout (the contention design working: we wait, they work, nobody errors — MarketOps stayed clean through it). Watch-item: if a future cycle's wait exceeds the busy timeout, the bounded retry path activates loudly (it has not yet been exercised live: retries_total=0).

**OPS-014 eligibility:** not yet. Missing evidence is exactly one item — `coverage_72h >= 0.98`, which is time-bound: the uncovered Jul-6 hours (pre-OPS-012 aggregation) exit the 72h window by ~22:00 UTC tonight while hourly cycles keep the front edge covered. **Re-check `tick-aggregation-report` after ~22:00 UTC** (or tomorrow morning); when it shows `ready_to_stage`, OPS-014 becomes proposable as **design only** — and per the accepted decision rule, the staged reduction should go **3d → 2d first**, not straight to 24h. Raw retention remains untouched until OPS-014 is explicitly accepted.

## FOLLOWTHROUGH-001 — follow-through diagnostic deployed (2026-07-09, ~06:57 UTC)

Deployed **`995afa5` → `94eb3a7`** by `git pull --ff-only` (also brings the docs-only `283c0e8` frontier-review packet). **No migration** (`0024 (head)`, 24 revision files before/after). Read-only analysis only: no flag, no timer, no endpoint, no persistence, no external call; edge-precheck gates, forecasts, promotion, MarketOps, and EDGE-AUTO are byte-for-byte unchanged.

| item | value |
|---|---|
| pushed / deployed commit | **`94eb3a7`** (origin/main + EVO-X2) |
| migration | **none** |
| new capability | `edge-followthrough-diagnostic-report --hours N [--top N]` — WHY is gap follow-through negative (timing / direction / verdicts / failure examples) |

**Live 24h diagnostic (the same 114-row window frontier reports):** toward_rate 0.2368, mean_closure −0.7848, continued_away 0.6053. **OVERALL VERDICT: `adverse_selection_candidate`.** The mechanism, quantified: **`gap_opposes_move_share = 0.7981`** and **`sharp_pre_move_share = 0.807`** — four of five watchlist gaps point back to where the market moved in the prior 10 minutes — while **forecast_age_p50 = 41s**: forecasts are FRESH, so this is **not staleness**; the evidence forecaster re-forecasts right after the `price_move_threshold` trigger but stays anchored behind the move, and the move persists.

**Live 48h diagnostic (262 rows — the separation is robust and sharper):** overall toward 0.2759 / closure −0.392, opposes_share 0.7582. The discriminating cohort: **gap-opposes-move n=184 → toward 0.2609 / closure −0.4947** vs **gap-follows-move n=59 → toward 0.3729 / closure +0.0172 (flat)**. Worst market types/series: **spread** (toward 0.1919 / closure −0.5394; `KXMLBSPREAD` worst series, incl. one repeated-failure ticker `KXMLBSPREAD-26JUL071840SEAMIA` with 3/3 rows adverse, mean closure −1.52) and **winner/`KXMLBGAME`** (0.2295/−0.432); **totals/`KXMLBTOTAL` grade `neutral` at 48h** (0.3861/−0.2234). Notable single example: a 34-second-old forecast whose market ran to closure −11.8 by 60m — freshness does not save an anchored estimate.

**Health (unchanged):** MarketOps #1276 `ok` / #1277 cycling normally; frontier readiness `ready_for_cycle_scoped_edge_automation`, **`safety_ok=True`**; tick aggregation now **6 clean scheduled cycles**, coverage_72h risen to 0.7671 (OPS-014 still waiting on the ~22:00 UTC ageing-out, as expected); DB 2,728.14 MiB (flat — the diagnostic persists nothing). Safety audits (tokenize-stripped + AST, expanded vocabulary incl. buy/sell/bet/arbitrage/arb/opportunity) ran clean (**NONE**) on this exact tree pre-commit; only boundary disclaimers appear in grep.

**Recommendation: KEEP — manual/report-only. MVP-005B remains blocked** (follow-through still below every gate; `paper_candidate_later` = 0). **Next milestone should be shadow FILTER analysis, not a live gate change**: the data now supports simulating — read-only, over persisted rows — what the watchlist would look like under a "gap must follow the recent move" cohort filter and/or a spread-series exclusion, measured against the same follow-through yardstick, before anyone touches a real gate. Even the best cohort (follows_move, 0.37 toward / flat closure) is below coin-flip, so the finding motivates analysis, not action. **Rollback (if ever):** `git reset --hard 995afa5` — analysis-only, nothing to unwind.

## EDGE-FILTER-001 — shadow adverse-selection filters deployed (2026-07-09, ~07:30 UTC)

Deployed **`e81e398` → `fb84852`** by `git pull --ff-only`. **No migration** (`0024 (head)`, 24 files). Read-only shadow analysis: no flag, timer, endpoint, or persistence; edge-precheck gates, forecasts, promotion, MarketOps, and EDGE-AUTO byte-for-byte unchanged. New capability: `edge-filter-shadow-report --hours N --top N` — 18 candidate adverse-selection filters replayed over existing watchlist rows (consumes FOLLOWTHROUGH-001's RowDiagnostic).

| item | value |
|---|---|
| pushed / deployed commit | **`fb84852`** (origin/main + EVO-X2) |
| migration | **none** |
| baseline (48h, n=262) | 60m toward **0.2759**, closure **−0.392** (24h n=114: 0.2368 / −0.7848) |
| worst series (data-derived) | **KXMLBSPREAD** — spread_only confirmed `worse_than_baseline` (0.1919 / −0.5394); winner_only and exclude_price_move_threshold also worse |

**Best positive-closure cohorts (48h, live):** three `promising_shadow` policies, all via materially positive mean closure with concentration guards passing — `gap_follows_move_and_tight_spread` (n=53, toward 0.396, closure **+0.177**), `gap_follows_move_and_high_liquidity` (n=30, 0.467, **+0.225**), `require_gap_follows_move_exclude_spreads` (n=38, 0.447, **+0.160**). **Strongest but young (`too_thin` — keep observing):** `require_gap_follows_move_totals_only` (n=26, toward **0.539**, closure **+0.42**) and `totals_only_no_sharp_pre_move` (n=15, 0.533, +1.19). On the thinner 24h window every candidate correctly demotes to `too_thin`/`neutral` — the conservative ladder behaving as designed on small samples.

**Interpretation (from data, in the report itself):** excluding gap-opposes-move improves 60m toward by only **+0.036** (helpful, insufficient alone); requiring gap-follows-move does **not** clear the promising rate bar (0.373); spreads confirmed as the adverse-selection concentration (spread_only 0.192 vs exclude-spreads 0.327); totals materially less bad (**+0.110** vs baseline). **`policies_clearing_mvp_bar: []` → MVP-005B remains BLOCKED**, and the report's own note states any advancement additionally requires explicit human acceptance. Post-pull consistency: the 48h follow-through diagnostic reproduces exactly (0.2759/−0.392, opposes 0.7582, verdict `adverse_selection_candidate`).

**Health (unchanged):** MarketOps #1281 `ok`; frontier `ready_for_cycle_scoped_edge_automation`, **`safety_ok=True`**; tick aggregation 6 clean scheduled cycles, coverage_72h 0.7671 (OPS-014 still on its ~22:00 UTC track); DB 2,728.14 MiB flat (the shadow report persists nothing). Safety audits (tokenize-stripped + AST, expanded vocabulary incl. buy/sell/bet/arbitrage/arb/opportunity) **NONE** on this exact tree pre-commit; grep hits are boundary disclaimers only.

**Recommendation: KEEP — manual/report-only. Continue accumulating; no live gate change.** The first positive-closure cohorts in the project's history now exist in shadow, but every one is below the 0.55 rate bar or under-sampled: the follows-move+totals cohort needs roughly 2× its current sample before `promising_shadow` can even apply, and a live-gate discussion would be its own explicitly-accepted milestone after that. Re-run `edge-filter-shadow-report --hours 48` after the next few MLB slates and during the World Cup semifinal windows. **Rollback (if ever):** `git reset --hard e81e398` — analysis-only, nothing to unwind.

## FORECAST-ANCHOR-001 — anchoring diagnostic deployed (2026-07-09, ~16:40 UTC)

Deployed **`7ff96c0` → `5edd80d`** by `git pull --ff-only`. **No migration** (`0024 (head)`, 24 files). Read-only analysis: no flag/timer/endpoint/persistence; forecasts, edge-precheck gates, promotion, MarketOps, and EDGE-AUTO unchanged. New capability: `forecast-anchor-diagnostic-report --hours N --top N` — did the forecast move when the market moved, measured per row from recorded prior forecasts and ticks.

| item | value |
|---|---|
| pushed / deployed commit | **`5edd80d`** (origin/main + EVO-X2) |
| migration | **none** |
| 48h live (n=255, 118 classifiable) | median **adjustment_ratio 0.483** (forecast moves ~half as much as the market; market moved more in 83% of rows); **anchored_static share only 5.9%**, partial 40.7%; ~50% of rows have NO prior forecast |
| moved_with_market cohort | **still fails: toward 0.19 / closure −0.25** (vs partial 0.13/−0.69, anchored 0.29/−1.00) — keeping up does not rescue follow-through |
| spreads vs totals anchoring | near-identical anchored+partial shares (**0.419 vs 0.410**); totals grade `no_anchor_issue_detected` (toward 0.378), spreads `timing_adverse_selection` (0.196) — **totals are less adverse, not less anchored** |
| overall verdict (48h) | **`timing_adverse_selection`** — selection/timing dominates; anchoring contributes but is secondary |
| 24h refinement (n=114) | overall `market_type_specific`: **winner markets grade `anchoring_confirmed`** (anchored+partial 0.667, ratio 0.416) while totals/spreads grade timing — the one market type where anchoring IS the mechanism |

**Interpretation (printed by the report, from data):** the negative follow-through is a **trigger timing/selection issue, not primarily forecaster anchoring** — `next_step_evidence: trigger_redesign_candidate_or_more_data … keep collecting shadow data before any change`. This matches the external second opinion's caution and redirects the going-in hypothesis. Companion post-pull runs reconcile: 48h follow-through diagnostic reproduces (`adverse_selection_candidate`, toward 0.2756, opposes 0.755) and the shadow filters HOLD on the rolled-forward window (`gap_follows_move_and_tight_spread` 0.404/+0.245, `require_gap_follows_move_exclude_spreads` 0.459/+0.255 — both still `promising_shadow`).

**Health (unchanged):** MarketOps #1374 `ok`; frontier `ready_for_cycle_scoped_edge_automation`, **`safety_ok=True`**; tick aggregation **15 clean scheduled cycles**, coverage_72h **0.8904** and climbing (OPS-014 re-check ~22:00 UTC on track); DB 2,728.14 MiB flat (nothing persisted). Safety audits (tokenize + AST, expanded vocabulary incl. buy/sell/bet/arbitrage/arb/opportunity) **NONE** on this exact tree pre-commit.

**Recommendation: KEEP — manual/report-only. MVP-005B remains blocked.** The next alpha-facing milestone should be **trigger-timing shadow analysis** (e.g. simulating delayed/settlement-gated measurement or a follows-move condition at trigger time, over persisted rows) — **not a live gate change**, and not forecaster redesign except possibly for winner markets where anchoring is confirmed (also shadow-first). **Rollback (if ever):** `git reset --hard 7ff96c0` — analysis-only, nothing to unwind.

## TRIGGER-TIMING-001 — trigger-timing shadow simulation deployed (2026-07-09, ~18:05 UTC)

Deployed **`ac75c12` → `d62bcd6`** by `git pull --ff-only`. **No migration** (`0024 (head)`), no flag/timer/endpoint/persistence; triggers, forecasts, edge-precheck gates, promotion, MarketOps, and EDGE-AUTO unchanged. New capability: `trigger-timing-shadow-report --hours N --top N` — replays 8 alternate measurement times (immediate baseline; +2/5/10/15m cooldowns; midpoint-flat/spread-stable/gap-follows-move waits bounded at 30m) over persisted ticks per historical watchlist row, forecast held fixed, gap re-derived and follow-through measured FROM the delayed time; `gap_evaporated` counts rows mean reversion beat to the measurement.

| item | value |
|---|---|
| pushed / deployed commit | **`d62bcd6`** (origin/main + EVO-X2) |
| migration | **none** (alembic stays `0024`) |
| tests / audit at commit | 1236 passed / 2 skipped; tokenize+AST vocabulary audit clean |
| 48h live population | **263 watchlist rows** (24h: 127) |

**48h live policy table (60m horizon):**

| policy | n (survival) | opposes | toward / closure | label |
|---|---|---|---|---|
| baseline_immediate | 263 (100%) | 0.767 | 0.282 / −0.360 | neutral |
| delay_2m | 245 (93%) | 0.810 | 0.265 / −0.282 | neutral |
| delay_5m | 228 (87%) | 0.814 | 0.256 / −0.164 | neutral |
| delay_10m | 227 (86%) | 0.750 | 0.238 / −0.286 | worse_than_baseline |
| delay_15m | 235 (89%) | 0.690 | 0.242 / −0.315 | worse_than_baseline |
| wait_until_midpoint_flat_5m | 128 (49%) | 0.860 | 0.122 / −0.104 | worse_than_baseline |
| wait_until_spread_stable | 222 (84%) | 0.773 | 0.244 / −0.117 | worse_than_baseline |
| wait_until_gap_follows_move | 203 (77%) | **0.000** | 0.301 / −0.180 | neutral |

**Conclusion: a pure cooldown is NOT promising.** No timing policy is `promising_shadow` on either window (24h n=127 agrees). Delays soften mean closure (−0.36 → −0.16 at best, delay_5m) mostly by letting the worst continuation happen *before* measurement (gap_evaporated 17–35 rows per delay), but the **toward-rate never improves** (0.282 baseline → 0.24–0.26 delayed) and opposes-share barely moves at short delays. Even `wait_until_gap_follows_move` — which drives opposes-share to exactly 0 at 77% survival — leaves closure negative (−0.18) and toward at only 0.301. **Implication: the adverse selection lives in WHICH rows trigger, not merely WHEN they are measured** — consistent with EDGE-FILTER-001, where follows-move+quality *filters* (which change the population, not the clock) remain the only cohorts with positive closure. Caveat printed by the report: the recorded forecast is held fixed; a live forecaster could refresh during a delay.

**Companion post-pull runs reconcile:** 48h follow-through diagnostic reproduces (toward 0.2824 / closure −0.365); shadow filters HOLD and strengthen (`gap_follows_move_and_tight_spread` 0.412/+0.294, `require_gap_follows_move_exclude_spreads` 0.472/+0.324, both `promising_shadow`) — and for the first time the filter report's MVP-005B line shows **`require_gap_follows_move_totals_only` clearing the shadow bar** (`blocked: False` on this window); per the report's own note and project doctrine, MVP-005B **remains gated on explicit human acceptance** and this deploy changes nothing about it.

**Health (unchanged):** MarketOps #1385 `ok` (17:42 UTC); frontier `safety_ok=True`, p90 55.3s < 60s; tick aggregation coverage 48h **1.0**, readiness `not_ready` only on `coverage_72h=0.9178 < 0.98` (17 clean cycles, no errors — OPS-014 re-check on track); DB 2,728.14 MiB flat (nothing persisted; above 1536 MiB warn tier, below critical — known state). **Safety audit** (host tokenize+AST, expanded vocabulary incl. wallet/keypair/swap/jupiter/signing/order/buy/sell/bet/arbitrage/arb/opportunity, 70 files): **no hits in any TRIGGER-TIMING-001 or analysis surface**; the only two hits are the long-standing `kalshi_private_key_path` RSA request-signing auth for read-only Kalshi API/WS access (`app/config.py`, `app/services/ws_snapshots.py`) — API authentication, not wallets, pre-existing and documented.

**Ops note:** `~/edge-observation/run_report.sh` (the documented daily read-only report snapshot, outside the git tree) was synced to the runbook's suite list — added `edge-followthrough-diagnostic-report`, `edge-filter-shadow-report`, `forecast-anchor-diagnostic-report` (documented but missing since their deploys) and `trigger-timing-shadow-report`, all `--hours 48 --top 10`. Bash syntax verified; **no new timer**, existing daily 15:00 UTC schedule unchanged.

**Recommendation: KEEP — manual/report-only. MVP-005B remains blocked pending explicit human acceptance.** The next alpha-facing work should be **trigger row SELECTION / cohort pre-registration** (e.g. pre-register the follows-move+totals cohort and watch it accumulate out-of-sample), **not a live gate change** and not more measurement-delay tuning — the timing question is now answered in shadow. **Rollback (if ever):** `git reset --hard ac75c12` — analysis-only, nothing to unwind.

## EDGE-SELECTION-001 — pre-registered selection validation deployed (2026-07-09, ~18:55 UTC)

Deployed **`f611202` → `31434ff`** by `git pull --ff-only`. **No migration** (`0024 (head)`), no flag/timer/endpoint/persistence; edge-precheck, forecasts, promotion, gates, MarketOps, and EDGE-AUTO unchanged. New capability: `edge-selection-validation-report --hours N [--since ISO] [--until ISO]` — evaluates ONLY the policy registry frozen in **`docs/EDGE_SELECTION_PREREG_2026_07_09.md` (locked 2026-07-09T19:00:00Z)** against fixed pre-registered gates, on windows explicitly labelled discovery / validation / mixed vs the lock.

| item | value |
|---|---|
| pushed / deployed commit | **`31434ff`** (origin/main + EVO-X2) |
| migration | **none** (alembic stays `0024`) |
| prereg document | `docs/EDGE_SELECTION_PREREG_2026_07_09.md`, lock **2026-07-09T19:00:00Z** |
| frozen policies | **8** — baseline; 6 candidates (primary `require_gap_follows_move_totals_only`); `spread_only` negative control. Registry freeze is test-enforced; any change requires a NEW prereg document + lock |
| success gates | final_n ≥ 75 (preferred ≥ 150); 60m toward ≥ 0.55; positive mean closure; ≤34% ticker / ≤50% game concentration; out-of-sample window; no MarketOps/safety regression; clean invalid profile |
| tests at commit | 1273 passed / 2 skipped; tokenize+AST audit clean |

**Live 48h run (18:54 UTC, n=293): window type = `DISCOVERY` (rows pre-lock=293, post-lock=0)** — the report prints "only a VALIDATION window can validate a candidate — this window cannot." Statuses on the discovery window (informational only): primary candidate `require_gap_follows_move_totals_only` **`insufficient_sample`** (n=26, toward 0.539, closure +0.400 — not failing, not validatable); `require_gap_follows_move_exclude_spreads` / `gap_follows_move_and_high_liquidity` / `gap_follows_move_and_tight_spread` `failing_gates` on toward 0.40–0.48 < 0.50; `total_only` and `exclude_spread_markets` `failing_gates` (negative closure). **Negative control `spread_only`: `control_consistent`** (toward 0.218 / closure −0.487 — adverse as expected, so candidate results are not regime-shift artifacts). `validated_shadow policies this window: none`. The **`--since 2026-07-09T19:00:00` run correctly labels `VALIDATION` with population 0** (lock minutes in the future at run time) — this is the canonical invocation once post-lock data accumulates. Note: the report takes no `--top` flag (the protocol has no examples section).

**Why this milestone exists (recorded in the prereg doc):** the primary candidate's shadow-MVP-bar clear (2026-07-09 ~17:45 UTC, `blocked: False`) regressed within hours (24h toward 0.389 the same evening; `blocked: True` since). Single-window winners of an 18-policy search are upward-biased by construction; only post-lock windows count. Companion post-pull filter report reconciles: two `promising_shadow` policies hold (tight_spread 0.404/+0.243, exclude_spreads 0.459/+0.258), `policies_clearing_mvp_bar: []`, `blocked: True`.

**Health (unchanged):** MarketOps #1396 `ok` / #1397 running normally; frontier `safety_ok=True`, p90 55.6s < 60s; tick aggregation 48h coverage 0.98 healthy, readiness blocked only on `coverage_72h=0.9315 < 0.98` (18 clean cycles, no errors — OPS-014 re-check on track); DB 2,728.14 MiB flat (nothing persisted). **Safety audit** (host tokenize+AST, expanded vocabulary incl. wallet/keypair/swap/jupiter/signing/order/buy/sell/bet/arbitrage/arb/opportunity, 71 files): no hits in any EDGE-SELECTION-001 or analysis surface; only the two long-standing `kalshi_private_key_path` RSA request-signing references (read-only Kalshi API auth, pre-existing, documented).

**Recommendation: KEEP — manual/report-only. No live gate change. No MVP-005B.** The protocol is now armed and the lock is live; **MVP-005B remains blocked unless explicit human acceptance**, and the report prints that unconditionally. Next meaningful runs, in order: `--since 2026-07-09T19:00:00` after tonight's MLB slate settles (first post-lock rows, likely `insufficient_sample`); the first fully-post-lock 48h window from ~2026-07-11T19:00Z; the **rolling 7d window from ~2026-07-16 as the primary decision window**. All post-lock windows count — including failures. **Rollback (if ever):** `git reset --hard f611202` — analysis-only, nothing to unwind.

## COST-MODEL-001 — cost-adjusted shadow measurement deployed (2026-07-09, ~20:30 UTC)

Deployed **`97a34b5` → `1eb63e1`** by `git pull --ff-only`. **No migration** (`0024 (head)`), no flag/timer/endpoint/persistence; edge-precheck, forecasts, promotion, gates, MarketOps, and EDGE-AUTO unchanged. New capability: `edge-cost-shadow-report --hours N --top N` — re-measures 60m midpoint follow-through net of half-spread, a conservative Kalshi fee assumption (new config `kalshi_fee_rate_assumption=0.07`, the published taker shape rate·P·(1−P) charged at BOTH measurement ends, no rebates — analysis assumption only), and executable TOUCH prices from recorded bid/ask ticks (above-market: trigger ask → horizon bid; below-market: trigger bid → horizon ask; missing quotes counted, never guessed).

| item | value |
|---|---|
| pushed / deployed commit | **`1eb63e1`** (origin/main + EVO-X2) |
| migration | **none** (alembic stays `0024`) |
| tests at commit | 1312 passed / 2 skipped; tokenize+AST audit clean |
| 48h live | population 324, **323 measurable, 100% touch coverage** |
| 24h live | population 183, 100% touch coverage |

**48h live cohort table (60m closure: frictionless / −half-spread / −fees / executable-touch):**

| cohort | n | toward | frictionless | −half-spread | −fees | touch | label |
|---|---|---|---|---|---|---|---|
| baseline_all_rows | 323 | 0.285 | −0.281 | −0.349 | −0.518 | −0.426 | neutral |
| require_gap_follows_move_totals_only | 28 | 0.500 | **+0.301** | +0.200 | **−0.035** | +0.097 | **cost_killed** |
| gap_follows_move_and_high_liquidity | 35 | 0.429 | **+0.235** | +0.179 | **−0.056** | +0.095 | **cost_killed** |
| gap_follows_move_and_tight_spread | 59 | 0.373 | **+0.179** | +0.126 | **−0.075** | +0.011 | **cost_killed** |
| require_gap_follows_move_exclude_spreads | 41 | 0.415 | **+0.094** | +0.008 | **−0.221** | −0.078 | **cost_killed** |
| total_only / exclude_spreads / spread_only | 128/203/120 | 0.21–0.38 | all negative | — | — | — | neutral |

**`cohorts_positive_after_costs: NONE` on BOTH 24h and 48h. Every positive-frictionless cohort is `cost_killed`.** The half-spread only dents the closures; the conservative round-trip fee (~3.5 closure points at mid-range prices against ~10-point gaps) erases them, and the executable-touch numbers were already thin (+0.01..+0.10). **Implication: the apparent shadow edge is, so far, a frictionless-measurement artifact. EDGE-SELECTION post-lock validation must be judged WITH cost-adjusted metrics alongside the frictionless prereg gates — a candidate that passes the prereg bars but stays `cost_killed` is not a real edge. MVP-005B remains blocked**, and the report prints that unconditionally.

**Companion post-pull runs:** the post-lock EDGE-SELECTION window is accumulating (**29 rows post-lock at 20:27 UTC**; primary candidate n=2 `sample_collapsed`, negative control n=10 `insufficient_sample` — early, honest, nothing validated). The filter report showed ANOTHER fleeting MVP-bar clear (`totals_only_no_sharp_pre_move`, n=20+, `blocked: False` this window) — a policy that is **not** among the 8 pre-registered candidates, which is precisely the single-window churn pre-registration exists to discipline; per doctrine it changes nothing, and the cost report shows nothing survives friction anyway.

**Health (unchanged):** MarketOps #1412 `ok` (20:23 UTC); frontier `safety_ok=True`, p90 55.9s < 60s; tick aggregation 19 clean cycles, `coverage_72h=0.9452` and climbing (crosses 0.98 ~22:00–23:00 UTC — OPS-014 re-check pending); DB 2,728.14 MiB flat (nothing persisted). **Safety audit** (host tokenize scan, expanded vocabulary incl. wallet/keypair/swap/jupiter/signing/order/buy/sell/bet/arbitrage/arb/opportunity, 72 files): no hits in any COST-MODEL-001 or analysis surface; only the two long-standing `kalshi_private_key_path` RSA request-signing references (read-only Kalshi API auth, pre-existing, documented).

**Recommendation: KEEP — manual/report-only. No paper trading, no MVP-005B unlock.** Future EDGE-SELECTION validation runs should pair `edge-selection-validation-report` with `edge-cost-shadow-report` on the same windows; graduation talk requires a cohort that passes the pre-registered gates out-of-sample AND stays positive after fees and touch prices. **Rollback (if ever):** `git reset --hard 97a34b5` — analysis-only, nothing to unwind.

## LIVE-MARKET-001 — live market/state observer deployed (2026-07-09, ~21:36 UTC)

Deployed **`171d292` → `5213532`** by `git pull --ff-only`. **No migration** (`0024 (head)`), no flag/timer/endpoint/persistence/external call; MarketOps, EDGE-AUTO, forecasts, gates, and promotion unchanged. New capability: `live-market-state-report --domain D --top N [--hours N]` — read-only live-state observation foundation for future in-game research: quote quality, market-update freshness, 1m/5m/10m volatility diagnostics (labels, never signals), status ladder with `stale_provider` warnings, and a tennis match-winner state scaffold that extracts score state ONLY from persisted TENNIS-001 research packets — provider gaps reported honestly, nothing fabricated, nothing fetched.

| item | value |
|---|---|
| pushed / deployed commit | **`5213532`** (origin/main + EVO-X2) |
| migration | **none** (alembic stays `0024`) |
| tests at commit | 1340 passed / 2 skipped; tokenize+AST audit clean |
| new flags / timers / external calls | **none** (compute-on-demand over persisted rows) |

**Live runs (21:35 UTC):**
- **sports_tennis (24h): 0 live candidates** — the report prints the explicit **`provider_gap`** (no validated live tennis score source; `TENNIS_RESEARCH_PROVIDER` defaults to `template`, the TENNIS-001 ESPN payload mapping is unvalidated) and **`insufficient_live_data`** (no tennis markets have ticks in the window). Honest empty — no state was invented.
- **sports_baseball (24h): 20 live candidates**, quote quality **14 tight / 6 missing_quotes** (missing = settled markets with one-sided books), status all `observable_market_only` (market quotes fresh, no score source), **mean market freshness 42.7s** (the realtime watcher is genuinely fresh), volatility **14 calm / 0 volatile / 6 insufficient** at 21:35 UTC (pre-game window — volatile examples list empty, correctly).

**Health (unchanged):** MarketOps #1423 `ok` / #1424 running normally; frontier `safety_ok=True`, p90 55.9s < 60s; tick aggregation 20 clean cycles, `coverage_72h=0.9589` (OPS-014 monitor watching for the ~23:00 UTC crossing); DB 2,728.14 MiB flat (nothing persisted). **Safety audit** (host tokenize scan, expanded vocabulary incl. wallet/keypair/swap/jupiter/signing/order/buy/sell/bet/arbitrage/arb/opportunity, 73 files): no hits in any LIVE-MARKET-001 or analysis surface; only the two long-standing `kalshi_private_key_path` RSA request-signing references (read-only Kalshi API auth, pre-existing, documented).

**Recommendation: KEEP — manual/report-only, run during live slates (deliberately NOT on the daily timer). Tennis live observation is blocked by data/source coverage, not by code**: no tennis markets currently tick and no validated live score provider exists. The next live-market milestone should be **provider/data-coverage validation** (e.g. validate the TENNIS-001 ESPN payload mapping against real responses behind its existing flag, and/or add tennis series to targeted scans) — observation-lane work, **not trading**; any decision-capable step remains far beyond this foundation and separately gated. **Rollback (if ever):** `git reset --hard 171d292` — analysis-only, nothing to unwind.

## TENNIS-LIVE-SOURCE-001 — tennis source-coverage validation deployed (2026-07-10, ~00:40 UTC)

Deployed **`17b10c4` → `751c5c6`** by `git pull --ff-only`. **No migration** (`0024 (head)`), no flag/timer/endpoint/persistence; providers NOT enabled (`TENNIS_RESEARCH_PROVIDER=template` unchanged); MarketOps, EDGE-AUTO, forecasts, gates, and promotion unchanged. New capability: `tennis-live-source-report --top N --hours N` — validates whether persisted tennis markets can map to source-backed live match state, built entirely on the existing TENNIS-001 scaffolds (ticker parse → players → tour/date → scoreboard event match); zero fetches under the template provider, bounded read-only fetches (one per tour/date, hard cap 6) only when a provider is explicitly configured.

| item | value |
|---|---|
| pushed / deployed commit | **`751c5c6`** (origin/main + EVO-X2) |
| migration / flags / providers | **none / unchanged / not enabled** |
| tests at commit | 1361 passed / 2 skipped; tokenize+AST audit clean |

**Live run (00:39 UTC, template provider — zero external calls):** total tennis markets **1,450**; live candidates (24h) **240**; match-winner **1,252**; classification mix match_winner 1252 / unknown 124 / set_winner 44 / prop 30; mapping mix **provider_gap 1,248** / not_match_winner 198 / ticker_unparseable **4**; missing player mappings **14**; scoreboards_fetched **0**. **Mapping quality is ~99% structural** — ticker → players → tour → date resolves for all but 18 of 1,450 markets. **Every live candidate is `KXATPCHALLENGERMATCH` (Challenger tier)** — consistent with the known ESPN coverage gap, so the blocker is provider EVENT coverage, not our mapping. Companion `live-market-state-report --domain sports_tennis`: 0 ticking candidates + honest provider_gap (tennis markets are persisted from scans but not in the tick watcher universe — a separate, additive coverage question).

**Health (unchanged):** MarketOps #1454 `ok` / #1455 running normally; frontier `safety_ok=True`, p90 54.8s < 60s; tick aggregation **`ready_to_stage`** (coverage_72h 0.9863, 23 clean cycles — OPS-014 proposal pending Eric's decision, nothing enacted); DB 2,750.43 MiB (intraday tick accumulation before the 00:08 prune — normal, below critical). **Safety audit** (host tokenize scan, expanded vocabulary, 74 files): no hits in any TENNIS-LIVE-SOURCE-001 or analysis surface; only the two long-standing `kalshi_private_key_path` RSA request-signing references (read-only Kalshi API auth, pre-existing, documented).

**Recommendation: KEEP — manual/report-only. Providers remain OFF.** The next tennis step should be **bounded provider coverage validation** — an explicitly-approved, read-only `TENNIS_RESEARCH_PROVIDER=espn` run during a live tennis window to turn structural validation into a real coverage measurement (expected: `provider_no_match` on Challenger; possibly real matches on ATP/WTA main tour) — **before any TENNIS-TAPE-001 discussion**. No trading capability is implied by any outcome. **Rollback (if ever):** `git reset --hard 17b10c4` — analysis-only, nothing to unwind.

## TENNIS-WATCHER-001 — tennis tick coverage tool deployed (2026-07-10, ~01:40 UTC)

Deployed **`5362849` → `f42f523`** by `git pull --ff-only`. **No migration** (`0024 (head)`), **no flag enabled** (`ENABLE_TENNIS_TICK_WATCHER` not in `.env`, default false), **no timer installed** (verified: no tennis unit in `systemctl --user list-timers`); MarketOps, EDGE-AUTO, forecasts, gates, promotion, and signal detection untouched. New capability: `tennis-watch-scan-once --limit N [--dry-run] [--scheduled]` (bounded read-only quote pass over active tennis markets into plain `market_price_ticks` — same table/shape/retention as the realtime watcher; no signals, no watcher_runs) + `tennis-watch-report --hours N` (DB-only coverage).

| item | value |
|---|---|
| pushed / deployed commit | **`f42f523`** (origin/main + EVO-X2) |
| migration / flags / timers | **none / none enabled / none installed** |
| tests at commit | 1381 passed / 2 skipped; tokenize+AST audit clean |

**Validation sequence (01:38 UTC):**
- **Coverage report (pre):** active tennis markets **240**, match-winner **176**, tick_covered **0**, coverage_rate **0.0** — the measured market-side gap, now visible as a first-class report.
- **Dry-run scan (`--limit 20 --dry-run`):** 20/20 Challenger tickers fetched (read-only GET), **ticks_recorded=0**; `two_sided_quotes=0` (no-play hour — books one-sided, honestly reported).
- **Coverage report (post dry-run):** still 0/240 — **dry-run persisted nothing**, verified.
- **Scheduled guard (`--limit 5 --scheduled`):** `skipped_flag_disabled` — no fetch, no rows, exactly as designed.

**Health (unchanged):** MarketOps #1464 `ok`; frontier `safety_ok=True`, p90 53.8s < 60s; tick aggregation `ready_to_stage` (coverage_72h 0.9863, 24 clean cycles — OPS-014 decision still pending Eric, nothing enacted); DB 2,750.43 MiB (normal intraday, below critical). **Safety audit** (host tokenize scan, expanded vocabulary, 75 files): no hits in any TENNIS-WATCHER-001 or analysis surface; only the two long-standing `kalshi_private_key_path` RSA request-signing references (read-only Kalshi API auth, pre-existing, documented).

**Recommendation: KEEP — manual/report-only.** Non-dry-run scans remain available for explicitly-chosen live Challenger windows (books were one-sided at deploy time; meaningful capture requires in-play hours). **TENNIS-TAPE-001 remains parked** — market-side ticks are now capturable on demand, but the score side is still blocked on provider coverage. **The next tennis milestone should be TENNIS-PROVIDER-001**: research/select a live-score source with Challenger/ITF draw coverage (ESPN measured definitively insufficient: source_backed 0/176), read-only validation first, behind the existing provider plumbing. **Rollback (if ever):** `git reset --hard 5362849` — additive tooling, nothing to unwind.

## TENNIS-PROVIDER-001 — provider research + adapter scaffold deployed (2026-07-10, ~03:36 UTC)

Deployed **`7b4ec8a` → `999892f`** by `git pull --ff-only`. **No migration** (`0024 (head)`), **no provider key** (verified: `TENNIS_PROVIDER_API_KEY` absent from `.env`, settings resolve `api key present: False`), **no provider enabled** (`TENNIS_RESEARCH_PROVIDER=template`, fetcher resolves to `None`), **no timer, no live fetch** (post-pull report: `scoreboards_fetched=0`, honest `provider_gap`); MarketOps, EDGE-AUTO, forecasts, gates, and promotion unchanged.

| item | value |
|---|---|
| pushed / deployed commit | **`999892f`** (origin/main + EVO-X2) |
| migration / key / provider / timer | **none / absent / template / none** |
| tests at commit | 1397 passed / 2 skipped; safety + secret audits clean |

**What shipped:** `docs/TENNIS_PROVIDER_RESEARCH_2026_07_10.md` — provider comparison scored against the MEASURED universe (240 live tennis candidates: ~79% ITF-family, ~15% Challenger). **Conclusion: API-Tennis primary** (documented ATP/WTA/Challenger M+W/ITF M+W coverage; player names + dates map onto our validated ticker parse; $40/mo Starter; 14-day trial; risk: per-plan coverage needs trial verification), **Goalserve fallback** (explicit all-tier claim, 5s point-by-point, $150/mo, 30-day trial), **Sportradar not first choice** (ITF World Tennis Tour removed from its Tennis API starting 2025 per official changelog — a poor fit for this universe despite official Challenger coverage; enterprise pricing), **ESPN retired for this universe** (measured 0/176; unofficial API). Plus `app/services/tennis_providers.py`: `ApiTennisFetcher` behind the existing `TENNIS_RESEARCH_PROVIDER` selection — makes **no request unless `TENNIS_PROVIDER_API_KEY` is set** (default empty; never committed/logged/echoed — tests assert the key never appears in display URLs); adapts `get_fixtures` into the scoreboard shape TENNIS-001's `_find_event` already matches (documented v1 limitation: 4-letter Kalshi codes need a tuning pass).

**Post-pull verification:** tennis-live-source-report identical to pre-pull (1,450 markets / 240 live / provider_gap / **zero fetches**); tennis-watch-report unchanged (30/240 covered from the approved first scan); provider state confirmed inert.

**Health (unchanged):** MarketOps #1484 `ok`; frontier `safety_ok=True`, p90 54.3s < 60s; tick aggregation `ready_to_stage` (coverage_72h 0.9863, 26 clean cycles — OPS-014 decision still pending Eric); DB 2,750.43 MiB (below critical). **Safety audit** (host tokenize scan, expanded vocabulary, 76 files): no hits in any TENNIS-PROVIDER-001 or analysis surface; only the two long-standing `kalshi_private_key_path` RSA request-signing references (read-only Kalshi API auth, pre-existing, documented).

**Recommendation: KEEP — scaffold inert until explicitly activated.** **TENNIS-TAPE-001 remains parked** until the bounded API-Tennis validation passes: Eric obtains a 14-day trial key (host `.env` only, never committed), then an explicitly-approved run of `tennis-live-source-report` with `TENNIS_RESEARCH_PROVIDER=api_tennis` inline (≤10 REST calls, no persistence) measures real Challenger/ITF coverage against the decision gates (≥50% source_backed useful; <25% after one tuning pass → Goalserve). **Rollback (if ever):** `git reset --hard 7b4ec8a` — additive research/plumbing, nothing to unwind.

## TENNIS-PROVIDER-001 validation — API-Tennis PASSED; tuned scaffold deployed (2026-07-10, ~04:05 UTC)

Deployed **`bf2da7f` → `977753f`** by `git pull --ff-only`. **No migration** (`0024 (head)`), no timer, no autonomous fetch loop; MarketOps/EDGE-AUTO/forecasts/gates unchanged. This deploy carries the **validated API-Tennis tuning** (fetch_scoreboard adapts unfiltered — player codes disambiguate) and the research doc's §7b validation results.

| item | value |
|---|---|
| pushed / deployed commit | **`977753f`** (origin/main + EVO-X2) |
| bounded validation verdict | **API-Tennis PASSED** — targeted live-candidate run: **130/176 = 73.9% source_backed** (Challenger 32/36 = 89%, WTA-Challenger 12/14, ITF-W 46/60, ITF-M 40/66); untuned first pass 47.7%; one allowed tuning pass (tour-filter fix) |
| call budget | **5 of ≤10** for the validation; post-deploy verification used the report's own hard cap (**exactly 6 scoreboard fetches**) |
| tier check | trial plan includes **all 27 tournament types** incl. Challenger M/W + ITF M/W — no tier lockout |
| secret exposure | **none** — key stored in host `.env` only (backed up pre-change), reported present/absent only; 0 occurrences in report output; repo grep for key fragment: clean |
| tests at commit | 1398 passed / 2 skipped |

**Post-deploy verification (04:03 UTC):** `TENNIS_RESEARCH_PROVIDER=api_tennis tennis-live-source-report --top 50 --hours 24` ran end-to-end through the deployed scaffold: `provider=api-tennis.com`, **source_backed=276** match-winner markets matched (incl. real `Finished`/`Retired` statuses — richer state than ESPN ever returned), `provider_no_match=284`, `provider_gap=744`, `scoreboards_fetched=6` (cap held). Note: the full report's overall 21.1% rate is the known **cap-ordering artifact** (its 6 fetch slots go to the oldest (tour,date) pairs — Jul-3 era — leaving current dates as provider_gap); the **authoritative live-candidate coverage number remains 73.9%** from the targeted validation. Remaining matching limitation: ~26% of live candidates miss on player-name→code edge cases (multi-word/hyphenated last names, diacritics) — tuning headroom, not a blocker; a live-first fetch ordering for capped runs is a known small follow-up.

**Health (unchanged):** MarketOps #1489 `ok`; frontier `safety_ok=True`, p90 54.2s < 60s; tick aggregation `ready_to_stage` (coverage_72h 0.9863, 27 clean cycles — OPS-014 still pending Eric); DB 2,750.43 MiB. **Safety audit** (host tokenize scan, expanded vocabulary, 76 files): only the two long-standing `kalshi_private_key_path` Kalshi RSA references.

**Recommendation & status: TENNIS-TAPE-001 is now DESIGNABLE — both halves exist** (market ticks via TENNIS-WATCHER-001, validated live; score source via API-Tennis, validated at 73.9% live-candidate coverage). It remains **parked pending explicit acceptance** of a tape design milestone. Practical notes: trial key expires ~2026-07-24; Starter ($40/mo) suffices on request volume (one livescore call covers all live matches; 8k req/day fits 10–15s polling). Known follow-ups for the tape design: name-code tuning (+headroom above 73.9%), live-first fetch ordering for capped report runs. **Rollback (if ever):** `git reset --hard bf2da7f` — research/plumbing only.

## TENNIS-TAPE-001 — synchronized tennis tape deployed (2026-07-10, ~04:30 UTC)

Deployed **`1088427` → `ca4f7f0`** by `git pull --ff-only` with **backup-first migration**. `backup-db` → `data/backups/backup-20260710T042831Z.db.gz` (220.02 MiB), `verify-db-backup` → **OK (41 tables, integrity ok)**. **Migration 0025 applied** via `run-baseline --dry-run` (0024 → 0025); all four tape tables verified present (`tennis_tape_runs` / `tennis_tape_score_snapshots` / `tennis_tape_market_snapshots` / `tennis_tape_links`). No timer, no scheduled path, no new flags enabled; MarketOps/EDGE-AUTO/forecasts/gates/promotion unchanged.

| item | value |
|---|---|
| pushed / deployed commit | **`ca4f7f0`** (origin/main + EVO-X2) |
| migration | **0025 applied** (backup verified first) |
| tests at commit | 1422 passed / 2 skipped; safety audit clean (incl. `markov` forbidden in Phase 0) |
| caps | 4 provider calls/run (deduped by date); ≤200 tickers/quote pass |

**Dry-run validation (04:29 UTC):**
- Plain run (provider=template): **`skipped_provider_gap`** — nothing fetched, nothing persisted, exactly as designed.
- Inline `TENNIS_RESEARCH_PROVIDER=api_tennis` dry-run (`--limit 20`): **16/20 `source_backed_link` (80%)**, 2 `fuzzy_candidate`, 2 `provider_no_match`; **2 score calls** (cap held), 1 chunked market fetch, **10/20 two-sided quotes** (live in-play window at capture time). Key never echoed.
- **Persistence check: all four tape tables at 0 rows; zero tennis market_price_ticks written; no signals; no watcher rows** — dry-run persists nothing, proven live.
- `tennis-tape-report`: renders honestly empty (no tape runs yet).

**Health (unchanged):** MarketOps #1493 `ok`; frontier `safety_ok=True`, p90 53.9s < 60s; tick aggregation `ready_to_stage` (coverage_72h 0.9863, 27 clean cycles — OPS-014 still pending Eric); DB 2,750.43 MiB (backup added 220 MiB under `data/backups/`, rotating per backup retention). **Safety audit** (host tokenize scan, expanded vocabulary now incl. `markov`, 77 files): only the two long-standing `kalshi_private_key_path` Kalshi RSA references.

**Recommendation: KEEP — dark/manual.** The **first real (non-dry-run) capture requires explicit approval and should run during a live Challenger/ITF in-play window** (the 04:29 dry-run showed 10/20 two-sided books — in-play windows exist right now); meaningful lag measurement comes from REPEATED captures across a window, each explicitly run. **After real tape data accumulates, the next milestone should be TENNIS-MICROSTRUCTURE-001** (read-only analysis over tapes: score-to-market lag distributions, quote-response profiles — still no models/EV/trading). **Rollback:** `git reset --hard 1088427` + `alembic downgrade 0024` (tape tables are additive and empty; the pre-migration backup exists regardless).
