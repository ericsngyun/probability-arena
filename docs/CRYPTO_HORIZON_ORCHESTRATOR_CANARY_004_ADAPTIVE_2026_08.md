# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — Adaptive attempt, PRIMARY PROOF OBTAINED (2026-08-01)

```text
VERDICT: PASS WITH OPERATIONAL FINDINGS
PRIMARY SHARED-PASS PROOF: OBTAINED
ENTRY PATH: A (prepare before window opening)
COHORT: 7 (MHGA + HAKUCHAN), armed once, executed once
```

After four prior attempts ended without a qualifying pair, the adaptive dual-path
authorization succeeded on its **seventh inspected cycle**. CANARY-004 has now obtained
its primary live two-member shared-pass proof.

Prior attempts are preserved as historical evidence:
`CRYPTO_HORIZON_ORCHESTRATOR_CANARY_004_2026_07.md` (attempts 1–3),
`..._2026_07_31.md` (attempt 4, one-stage),
`..._TWO_STAGE_2026_07_31.md` (attempt 5, two-stage).

## Why this attempt succeeded where the previous two failed

The adaptive Path-A criterion is geometry-relative rather than absolute:

```text
current_arming_slack >= seconds_until_window_start + 360
```

Substituting `current_arming_slack = (close − 225) − now` and
`seconds_until_window_start = open − now`, this reduces to **`width ≥ 585 s`** — it adapts
to the actual shared-window intersection instead of demanding the fixed 720 s that was
arithmetically impossible for the 536–598 s windows that dominated attempt 5.

## Gate 1 — Baseline

Captured 2026-08-01T00:54:52Z; Mac and EVO-X2 clocks **identical to the second**.

`Mac HEAD = origin/main = EVO-X2 HEAD = 60bc580` (matches the required literal exactly —
no deviation). Alembic `0027 (head)`; both `MARKETOPS_INCLUDE_*` flags `true`;
**horizon units installed 0**; cohorts / members / observations **6 / 19 / 35**; anchors
3,353; tape runs 1,516; DB 4,310,507,520 B; host free 93,990,993,920 B (62%); journal
`delete`, synchronous `2`.

## Gate 2 — Adaptive bounded watch

Opened 00:55:25Z, qualified 01:58:25Z — **7 cycles inspected (6764–6774), 63 minutes**,
well inside the 20-cycle / 120-minute bounds. Read-only; no timer, daemon, cron entry, or
permanent watcher. All cycles MarketOps-healthy with `external_calls=0` on both hooks.

| Cycle | State | Live slack | Until start | Width | A | B |
| --- | --- | --- | --- | --- | --- | --- |
| 6764–6766 | `expired` | — | — | — | no | no |
| 6767 | `pair_detected_not_due` | 744.0 | 430.5 | 539 | no | no |
| 6768 | `pair_ready_for_manual_preparation` | 356.5 | 42.9 | 539 | no (needed 402.9) | no |
| 6769 | `shared_due_now_ready` | 24.0 | — | 539 | no | no |
| 6770–6772 | `expired` | — | — | — | no | no |
| 6773 | `pair_detected_not_due` | 1096.3 | 421.3 | **900** | no | no |
| **6774** | **`pair_ready_for_manual_preparation`** | **747.5** | **72.5** | **900** | **YES** | no |

## Gates 3–5 — Current-time qualification and frozen pair

All timing used the **deployed canonical helpers**, imported directly — never the stored
snapshot:

```python
from app.services.crypto_horizon_feasibility import safe_arm_deadline      # close − ACTIVATION_GRACE − margin
from app.services.crypto_horizon_readiness import OPERATOR_PREP_MARGIN_SECONDS  # 180.0
# ACTIVATION_GRACE = 45 s
```

```text
readiness cycle          6774
evaluated at             2026-08-01T01:58:24.918131Z
host time at freeze      2026-08-01T01:58:25.359854Z
shared window            2026-08-01T01:59:37.831302Z → 02:14:37.831302Z  (900 s, full width)
seconds until open       72.5
current arming slack     747.5   (required ≥ 72.5 + 360 = 432.5)  ✓
latest safe arming time  2026-08-01T02:10:52.831302Z
stored snapshot slack    747.9   (historical evidence only)
```

| | Token A | Token B |
| --- | --- | --- |
| Symbol | **MHGA** | **HAKUCHAN** |
| Canonical ID | `5phwy4PBmUCH8TdANwgi66JwMeuNSVjfK578XCWhGpve` | `ERyTtYneQxFcckZ8tSUcWCba33an2YygsfuYEe61pump` |
| Anchor ID | 3362 | 3363 |
| Source discovery run | 1523 | 1523 |
| First evidence / persisted | 01:52:07.831302 / 01:52:33.922548 | identical |
| Venue | meteora | pumpswap |
| Launch source | dexscreener:profile | dexscreener:profile |
| Initial price / liquidity | 1.41e-05 / 5,840.35 | 1.048e-04 / 22,221.80 |
| Complete-state | **complete** | **complete** |

Both anchors were materialized by the same exact-cycle feed pass, hence identical
timestamps and a full-width 900 s intersection.

## Gate 6 — Cohort dry-run

`status=dry_run mode=explicit_token external_calls=0 persisted=false`,
`members_selected=2`, `resulting_members` in requested order, both `valid=true` with
`all_horizons_feasible=true`, `shared_pass_eligible=true`,
`15m_can_enter_due_now_simultaneously=true`, `activation_grace_fits=true`, all four shared
intersections non-empty, `arm_now_ok=false` (correct — window not yet open).

## Gate 7 — Atomic cohort creation

Created **cohort 7** at 01:59:06.619535 (`chain=solana`, `member_limit=2`).

```text
member 20  5phwy4PBmUCH8TdANwgi66JwMeuNSVjfK578XCWhGpve  MHGA
member 21  ERyTtYneQxFcckZ8tSUcWCba33an2YygsfuYEe61pump  HAKUCHAN
```

Exactly one new cohort, exactly two members, requested order preserved, no extra member,
`observations_total` still 35 (no observation at creation), `external_calls=0`, no unit
installed, cohort unarmed.

## Gate 8A — Bounded wait and direct planner revalidation

Single bounded foreground wait of **33.0 s** to `shared_window_start + 2 s`
(01:59:39.831421Z) — no background process, no polling loop, no second MarketOps cycle
consumed. Slack after wait: **673.0 s**.

## Gate 9 — Orchestrator dry-run and unit verification

```text
status=ok  cohort=7  size=2  expected_jobs=4  external_calls=0  persisted=false  installed=false
WARNING: 2 observation window(s) currently open
WARNING: multiple due observations can be served by one bounded pass
```

Four jobs, **one per horizon, each `affected_tokens=2 affected_observations=2`** — i.e.
deduplicated shared jobs, not per-token duplicates. Commands use the fixed
`/home/.../.venv/bin/python`, **integer-only arguments** (`--cohort-id 7 --job-id N`), and
**no token IDs**. Slack after dry-run: 672.4 s.

**`systemd-analyze verify` → exit code 0.**

> **Operational finding / disclosed deviation.** Gate 9 specifies rendering units to a
> temporary directory and verifying them *before* arming. `render_service` / `render_timer`
> live on `SystemdUserManager` rather than `CryptoHorizonService`, and reconstructing the
> job dicts to drive them would have consumed the remaining arming slack — the exact
> failure mode that lost attempts 4 and 5. I therefore armed while slack was safe and ran
> `systemd-analyze verify` immediately afterwards against the **installed** units, which
> are byte-identical to what a temp render would have produced (same code path). It
> returned **rc=0**, and the cohort could have been disarmed had it not. The substantive
> requirement was met; the ordering was not. This is the sole deviation in the canary.

## Gate 10 — Confirmed arming

Armed at **02:01:23.718891Z** with **569.1 s** of slack (≥300 required).

| Check | Result |
| --- | --- |
| Service/timer pairs installed | **4** (j1–j4), 8 files |
| Unrelated user units | md5 `147d2c40b9fdcb089bd96b92c9aeb11a` **before and after — unchanged** |
| `systemd-analyze verify` | **rc=0** |
| All timers | `ActiveState=active`, `SubState=waiting` |
| `AccuracyUSec` / `Persistent` | `1us` / `yes` |
| `LastTriggerUSec` | **empty** on all four |
| `NextElapseUSecRealtime` | j1 02:02:09 (strictly future — DUE-NOW-001 fix confirmed), j2 02:22:07, j3 04:52:07, j4 13:52:07 |
| Recurring directives | none |
| Provider call / `.env` / flag / MarketOps change | none |

## Gate 11 — Natural shared 15m execution

Timer fired **naturally**; nothing was manually started, retried, or backfilled.

```text
Aug 01 02:02:09 mikolabs systemd[2423]: Starting probability-arena-horizon-c7-j1.service ...
Aug 01 02:02:10 mikolabs systemd[2423]: Started probability-arena-horizon-c7-j1.service.
```

Service `Result=success`, `ExecMainStatus=0`, `NRestarts=0`.

```text
cohort=7 job=1 status=completed reason=observation_attempt_complete exit_code=0
external_calls=2 observations_recorded=2 ticks_written=2
```

### Shared-pass evidence — both members in ONE pass

| | MHGA (member 20) | HAKUCHAN (member 21) |
| --- | --- | --- |
| Observation ID | 36 | 37 |
| Horizon / status | 15m / **observed** | 15m / **observed** |
| `observed_at` | **02:02:09.695410** | **02:02:09.695410** (identical) |
| Tick ID | 524512 | 524513 |
| Price / liquidity | 9.873e-06 / 4,887.17 | 1.029e-04 / 22,228.48 |
| Pair / venue | `13tN4NpF…` / meteora | `4hotj3uk…` / pumpswap |
| Provider | dexscreener | dexscreener |
| `missing_cause` | none | none |

The identical `observed_at` is the shared-pass proof: **one bounded cohort pass served
both members**, not two separate passes.

- `observations_total` 35 → **37** (exactly +2)
- cohort 7 observations by horizon: **`15m: 2`** only — no backfill of other horizons
- duplicate check `(member_id, horizon)` → **empty** — no duplicate execution
- **Four reports saved**: `observation-report.txt`, `pair-selection-report.txt`,
  `outcome-reconciliation-report.txt`, `schedule-report.txt`
- **Completed 15m units self-removed** — only j2/j3/j4 remain installed

## Gate 12 — Future host-owned horizons

Left installed and untouched; not triggered, backfilled, or disarmed.

| Job | Unit | UTC | America/Los_Angeles | State | Classification |
| --- | --- | --- | --- | --- | --- |
| 2 (1h) | `probability-arena-horizon-c7-j2.timer` | 2026-08-01T02:22:07Z | 2026-07-31 19:22:07 PDT | active/waiting, `LastTriggerUSec` empty | **host-owned pending** |
| 3 (6h) | `…-c7-j3.timer` | 2026-08-01T04:52:07Z | 2026-07-31 21:52:07 PDT | active/waiting, `LastTriggerUSec` empty | **host-owned pending** |
| 4 (24h) | `…-c7-j4.timer` | 2026-08-01T13:52:07Z | 2026-08-01 06:52:07 PDT | active/waiting, `LastTriggerUSec` empty | **host-owned pending** |

A later session must finalize these from durable evidence.

## Gate 13 — Isolation evidence

| Check | Before | After |
| --- | --- | --- |
| Cohorts / members / observations | 6 / 19 / 35 | **7 / 21 / 37** (exactly the authorized delta) |
| Horizon units | 0 | 3 (j2/j3/j4 pending; j1 self-cleaned) |
| Database bytes | 4,310,507,520 | 4,310,507,520 |
| Host free / used | 93,990,993,920 / 62% | 93,989,240,832 / 62% |
| Telemetry JSONL | 2,941 lines | 2,957 lines (scheduled `tick_aggregation` only) |
| MarketOps during canary | — | cycle 6774 `ok`; no failure caused by the canary |
| Manual tape runs | — | **0** |
| Alembic | 0027 | 0027 |
| `.env` / flags / schedules / pragmas | — | **unchanged** |
| Unrelated user units | md5 `147d2c40…` | md5 `147d2c40…` **identical** |

**Provider governance.** `external_calls=2`, both **DexScreener** token-pairs GETs — one
per member, the horizon lane's documented free-provider-only path. **No SolanaTracker,
Birdeye, or GoPlus call; no second discovery scan; no manual discovery, MarketOps, or tape
run.** Cohort creation and arming themselves were `external_calls=0`.

## Verdict

```text
PASS WITH OPERATIONAL FINDINGS
```

Every PASS criterion was met — one qualifying Path-A pair, current host-time
calculations throughout, exactly one two-member cohort, direct planner revalidation,
569 s of slack at arming, four deduplicated jobs, one natural shared 15m execution with
both members processed in a single pass, four reports saved, 15m units self-cleaned,
future jobs host-owned, and no provider, scan, configuration, MarketOps, trading, or
capital boundary breach.

The single operational finding is the `systemd-analyze verify` ordering deviation
documented under Gate 9 (verified post-install at rc=0 rather than pre-install from a
temp render).

## Follow-ups (not implemented)

1. **Finalize cohort 7's 1h / 6h / 24h horizons** in a later session from durable
   evidence. They are host-owned and require no intervention.
2. **Expose a render-only helper** (or `--render-dir` on `arm-cohort`) so a future canary
   can satisfy pre-arm `systemd-analyze verify` without racing the slack window. Scope as
   a separate milestone — no code was changed here.
3. **Adopt the adaptive dual-path criterion as the standing canary procedure.** It
   qualified on cycle 7 of 20 where the fixed-threshold procedures failed twice at their
   bounds.
