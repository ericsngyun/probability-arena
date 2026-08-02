# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — Final lifecycle (captured 2026-08-02)

```text
FULL SHARED-PASS LIFECYCLE: PASS
all four horizons executed naturally · 8/8 member-horizon states · 0 duplicates
0 backfills · 0 manual triggers · 4/4 reports per horizon · all units self-cleaned
```

Final capture **2026-08-02T20:25Z**. This document supersedes the two earlier
lifecycle reports, which remain in the repository as historical evidence:

- `CRYPTO_HORIZON_ORCHESTRATOR_CANARY_004_ADAPTIVE_2026_08.md` — pair selection and arming
- `CRYPTO_HORIZON_ORCHESTRATOR_CANARY_004_FINAL_2026_08_01.md` — **superseded**; a partial
  report covering 15m + 1h only, written while 6h and 24h were still pending

Nothing was triggered, retried, regenerated, repaired, or backfilled to produce this
report. All evidence is durable and read-only.

## Adaptive pair selection (summary)

After five attempts failed to catch a qualifying pair under fixed-threshold rules, the
**adaptive dual-path** criterion qualified on the 7th of 20 allowed cycles. Its Path-A
test is geometry-relative — `slack ≥ seconds_until_window_start + 360` reduces
algebraically to `width ≥ 585 s` — where the earlier fixed 720 s gate was arithmetically
impossible for the 536–598 s shared windows that dominated the previous attempt.

Cohort **7**, created 2026-08-01T01:59:06.619535Z, armed 02:01:23.718891Z with 569.1 s of
slack. Both anchors came from discovery run 1523 and were materialized by the same
exact-cycle feed pass, giving a full 900 s intersection.

| | Token A | Token B |
| --- | --- | --- |
| Symbol | **MHGA** | **HAKUCHAN** |
| Canonical ID | `5phwy4PBmUCH8TdANwgi66JwMeuNSVjfK578XCWhGpve` | `ERyTtYneQxFcckZ8tSUcWCba33an2YygsfuYEe61pump` |
| Member ID | 20 | 21 |
| Venue | meteora | pumpswap |
| Initial price / liquidity | 1.41e-05 / 5,840.35 | 1.048e-04 / 22,221.80 |

## Per-horizon evidence

### 15m
Triggered naturally 02:02:09 (`Starting` 02:02:09 → `Started` 02:02:10).
`Result=success`, `ExecMainStatus=0`, `NRestarts=0`. `status=completed
reason=observation_attempt_complete exit_code=0`, `external_calls=2`,
`observations_recorded=2`, reports **4/4**, units self-removed.

### 1h
Triggered naturally 02:22:07 (`Started` 02:22:09, 1.185 s CPU / 73.0 M peak).
`Result=success`, `ExecMainStatus=0`, `NRestarts=0`, `exit_code=0`,
`external_calls=2`, `observations_recorded=2`, reports **4/4**, units self-removed.

### 6h
Triggered naturally **04:52:07** (`Started` 04:52:09, 1.423 s CPU / 73.7 M peak).
`Result=success`, `ExecMainStatus=0`, `NRestarts=0`. Orchestrator JSON:
`status=completed`, `reason=observation_attempt_complete`, `last_exit_code=0`,
`finished_at=04:52:08.289983Z`, `cleanup_error=None`,
`removed_units=[…c7-j3.timer, …c7-j3.service]`,
`observation_summary{due_tokens=2, due_observations=2, cap=2, external_calls=2}`.
**MarketOps health re-checked before executing:** run 6803, `healthy: True`, age 149.3 s.
Reports **4/4**. Timer and service both `LoadState=not-found`, no unit files on disk.

### 24h
Triggered naturally **13:52:07** (`Started` 13:52:10, 1.701 s CPU / 74.1 M peak).
`Result=success`, `ExecMainStatus=0`, `NRestarts=0`. Orchestrator JSON:
`status=completed`, `reason=observation_attempt_complete`, `last_exit_code=0`,
`finished_at=13:52:08.302488Z`, `cleanup_error=None`,
`removed_units=[…c7-j4.timer, …c7-j4.service]`,
`observation_summary{due_tokens=2, due_observations=2, cap=2, external_calls=2}`.
**MarketOps health re-checked before executing:** run 6893, `healthy: True`, age 210.6 s.
Reports **4/4**. Both units `LoadState=not-found`, no unit files on disk.

## Full lifecycle table

All times UTC. Anchor `first_evidence_at` = 2026-08-01T01:52:07.831302Z for both members.

| Horizon | Nominal target | Window start | Scheduled | Window close | Actual trigger | Planner state | Members | Obs IDs | Shared `observed_at` | Ext calls | Provider | Exit | Reports | Cleanup | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 15m | 02:07:07.831302 | 01:59:37.831302 | 02:02:09.247313 | 02:14:37.831302 | **02:02:09** | due_now | 2/2 | 36, 37 | 02:02:09.695410 | 2 | dexscreener | 0 | 4/4 | self-cleaned | **PASS** |
| 1h | 02:52:07.831302 | 02:22:07.831302 | 02:22:07.831302 | 03:22:07.831302 | **02:22:07** | due_now | 2/2 | 38, 39 | 02:22:08.349234 | 2 | dexscreener | 0 | 4/4 | self-cleaned | **PASS** |
| 6h | 07:52:07.831302 | 04:52:07.831302 | 04:52:07.831302 | 10:52:07.831302 | **04:52:07** | due_now | 2/2 | 40, 41 | 04:52:08.354404 | 2 | dexscreener | 0 | 4/4 | self-cleaned | **PASS** |
| 24h | 08-02T01:52:07.831302 | 08-01T13:52:07.831302 | 13:52:07.831302 | 08-02T13:52:07.831302 | **13:52:07** | due_now | 2/2 | 42, 43 | 13:52:08.377565 | 2 | dexscreener | 0 | 4/4 | self-cleaned | **PASS** |

```text
completed jobs        4
missed jobs           0
failed jobs           0
pending jobs          0
member-horizon states 8 of 8 expected
duplicates            0
manual triggers       0
backfills             0
```

Observation detail — every row `status=observed`, `missing_cause=None`,
`provider=dexscreener`:

| Obs | Member | Horizon | Tick | Price | Liquidity | Pair / venue |
| --- | --- | --- | --- | --- | --- | --- |
| 36 | 20 MHGA | 15m | 524512 | 9.873e-06 | 4,887.17 | `13tN4NpF…` meteora |
| 37 | 21 HAKUCHAN | 15m | 524513 | 1.029e-04 | 22,228.48 | `4hotj3uk…` pumpswap |
| 38 | 20 | 1h | 524912 | 7.722e-06 | 4,322.72 | `13tN4NpF…` meteora |
| 39 | 21 | 1h | 524913 | 1.564e-06 | 1,624.26 | `4hotj3uk…` pumpswap |
| 40 | 20 | 6h | 527255 | 9.242e-06 | 4,732.72 | `13tN4NpF…` meteora |
| 41 | 21 | 6h | 527256 | 1.527e-06 | 1,591.92 | `4hotj3uk…` pumpswap |
| 42 | 20 | 24h | 535056 | 7.429e-06 | 4,237.37 | `13tN4NpF…` meteora |
| 43 | 21 | 24h | 535057 | 1.380e-06 | 1,447.44 | `4hotj3uk…` pumpswap |

**Market-outcome note (not an orchestration finding).** HAKUCHAN fell ~98.5% between the
15m and 1h marks and continued drifting down; MHGA held far steadier. Both members were
observed cleanly at every horizon with no missing state. This lane records prices as
research data and never produces EV, a side, a size, or a recommendation.

## Shared-pass proof

The decisive query — distinct `observed_at` per horizon for cohort 7:

```text
15m  distinct_observed_at=1  shared=YES  2026-08-01 02:02:09.695410
1h   distinct_observed_at=1  shared=YES  2026-08-01 02:22:08.349234
6h   distinct_observed_at=1  shared=YES  2026-08-01 04:52:08.354404
24h  distinct_observed_at=1  shared=YES  2026-08-01 13:52:08.377565
```

Every horizon: **one timer trigger → one service execution → one cohort pass → two members
processed → one observation state per member**, with both members sharing the pass-level
timestamp. This is the property CANARY-004 existed to prove: a single bounded pass serves
the whole cohort rather than one pass per token, exactly as the deduplicated
`affected_tokens=2 affected_observations=2` job plan predicted.

Duplicate check over `(member_id, horizon)` returned **empty**.

**Research value delivered.** The 24h outcome-reconciliation report recomputes survival
with and without each observation's exact `tick_id`: `observed_with_tick=8`,
`transitioned_unknown_to_known=4`, `transition_rate=0.5` — half the member-horizon states
converted an unknown survival outcome into a known one. Final observation report gates:
15m/1h/6h/24h all `actual=1.0 pass=True`.

## Provider governance

| Horizon | External calls | Provider | SolanaTracker | Birdeye | GoPlus | Denied-provider requests |
| --- | --- | --- | --- | --- | --- | --- |
| 15m | 2 | DexScreener | 0 | 0 | 0 | 0 |
| 1h | 2 | DexScreener | 0 | 0 | 0 | 0 |
| 6h | 2 | DexScreener | 0 | 0 | 0 | 0 |
| 24h | 2 | DexScreener | 0 | 0 | 0 | 0 |
| **Total** | **8** | DexScreener only | **0** | **0** | **0** | **0** |

Each job log shows exactly two `GET https://api.dexscreener.com/token-pairs/v1/solana/<mint>`
requests — one bounded request per member — and nothing else. Cohort creation and arming
were themselves `external_calls=0` (manifest records `external_calls: 0`).

**Attribution.** Global SolanaTracker counters continued moving throughout, driven by the
ongoing background MarketOps crypto stage via `ENABLE_CRYPTO_RISK_ENGINE`
(`per_run_lookup_limit=15` per natural scan). **That activity is not attributable to this
canary** — the horizon observation lane is statically DexScreener-only.

## Cleanup and isolation

| Check | Result |
| --- | --- |
| Cohort-7 timers/services loaded | **none** — all four `LoadState=not-found` |
| Cohort-7 unit files on disk | **none** |
| Retained evidence | `job-1..4.json`, `job-1..4.log`, `job-1..4-reports/`, `manifest.json` (evidence convention) |
| Unrelated user units | md5 `147d2c40b9fdcb089bd96b92c9aeb11a` — **identical to the pre-arm hash**; 22 files |
| MarketOps cycles 02:00→14:00Z | **120**, non-`ok` **0**, cycles with `stage_errors` **0** |
| `crypto_watcher_runs` in window | **120** = exactly 1 scan/cycle — no second scan |
| Readiness / anchor-feed `external_calls` violations | **0** |
| Manual discovery / tape / MarketOps trigger | **0** |
| Cohorts created in window | **0** (cohort 7 predates it at 01:59:06) |
| All-time cohorts / members / observations | 7 / 21 / 43 |
| Other cohorts | untouched — 1:15, 2:6, 3:4, 5:6, 6:4 (cohort 4 still 0, permanently unarmed) |
| `.env` / flags / systemd schedules / SQLite pragmas | unchanged (`delete`, `synchronous=2`) |
| Migration | none — Alembic `0027` throughout |
| Trading / capital action | none |

Storage across the whole canary remained bounded: database 4,386,648,064 B with host at
**61% used** (94.3 GB free) — actually one point *better* than at arming.

## Final verdict

```text
PASS
```

Every PASS criterion is satisfied: four horizons executed naturally, one shared pass each,
both members processed at every horizon, 8/8 member-horizon states, zero duplicates, zero
backfills, zero manual triggers, `exit_code=0` throughout, 4/4 reports per horizon, all
units self-cleaned, and provider and isolation boundaries held.

## Operational findings

1. **Carried forward, not new — render-preview ordering.** During arming,
   `systemd-analyze verify` was run against the *installed* units rather than a zero-install
   pre-arm render, because `render_service`/`render_timer` live on `SystemdUserManager`
   without a plan-builder and reconstructing job dicts would have consumed the arming
   slack. It returned rc=0 on byte-identical content. This concerned the arming procedure
   only; the four executions below it were clean. See the adaptive report.
2. **No new findings** arose during the 6h or 24h horizons.

## Render-preview follow-up

**`IMPLEMENT CRYPTO-HORIZON-ORCHESTRATOR-RENDER-PREVIEW-001`.** `render_service(job, store)`
and `render_timer(job)` are pure string functions whose output embeds only `project_root`,
`python_path`, and `store.log_path()` — none dependent on write location — so a temp-dir
render is provably byte-identical to the installed unit, and `install_jobs()` is the sole
writer/enabler. A `--render-dir` on the dry-run path is small, additive, and
semantics-preserving.

## Next operational priorities

Ranked separately in this session's backlog assessment. Headline: backup protection first
(canon declares a timer that live inspection must confirm), then storage/retention against
the breached app-level DB gate, with render-preview and test-clock hygiene as small
cleanups.
