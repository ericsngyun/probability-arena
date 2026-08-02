# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — Lifecycle finalization (2026-08-01), PARTIAL

> **SUPERSEDED by `CRYPTO_HORIZON_ORCHESTRATOR_CANARY_004_FINAL_2026_08_02.md`.**
> Retained as historical evidence. This report was written while the 6h and 24h horizons
> were still pending and therefore claims no full-lifecycle verdict. Both subsequently
> executed naturally and passed; the full lifecycle verdict is **PASS**. Nothing in this
> document is retracted — it was accurate at its capture instant (2026-08-01T02:08:37Z).

```text
LIFECYCLE STATUS: INCOMPLETE — NOT YET FINALIZABLE
15m: PASS (shared pass proven)
1h:  PASS (shared pass proven)
6h:  HOST-OWNED PENDING  — fires 2026-08-01T04:52:07Z
24h: HOST-OWNED PENDING  — fires 2026-08-01T13:52:07Z
EXECUTED HORIZONS VERDICT: PASS
MUTATIONS BEYOND THE AUTHORIZED CANARY: ZERO
```

## Why this is a partial finalization

The finalization request assumed the 1h/6h/24h horizons had already run. **They had not.**
At the Gate-1 capture instant — **2026-08-01T02:08:37Z**, only ~6 minutes after the 15m
proof — all three remaining timers still showed `LastTriggerUSec=` empty and
`SubState=waiting`:

```text
1h   2026-08-01T02:22:07Z   13.5 minutes away
6h   2026-08-01T04:52:07Z    2h 43m away
24h  2026-08-01T13:52:07Z   11h 43m away
```

There was no durable evidence to capture, and triggering, backfilling, or retrying is
forbidden. The 1h job was close enough to be **observed naturally** within the session, so
it is included below. The 6h and 24h horizons remain genuinely pending and must be
finalized from durable evidence in a materially later session.

No full-lifecycle PASS is claimed here, because two of four horizons have not executed.

## Gate 1 — Baseline

`Mac HEAD = origin/main = EVO-X2 HEAD = e5ac6ac` (no deviation); Alembic `0027 (head)`;
both `MARKETOPS_INCLUDE_*` flags `true`; tracked trees clean (only the known 2026-07-15
untracked artifact, untouched); MarketOps cycles 6775–6778 all `ok`.

## 15m — PRIMARY PROOF (durable evidence re-verified)

| Item | Evidence |
| --- | --- |
| Scheduled / triggered | 02:02:09.247313Z / **02:02:09** (natural) |
| Journal | `Starting …c7-j1.service` 02:02:09 → `Started` 02:02:10 |
| Service | `Result=success`, `ExecMainStatus=0`, `NRestarts=0` |
| Self-removal | `LoadState=not-found`, **0 `c7-j1` unit files remain** |
| Job status | `status=completed reason=observation_attempt_complete exit_code=0` |
| Calls / writes | `external_calls=2`, `observations_recorded=2`, `ticks_written=2` |
| Reports | **4/4** |

| Member | Obs | Tick | `observed_at` | Status | Price / liquidity | Pair / venue |
| --- | --- | --- | --- | --- | --- | --- |
| MHGA (20) | 36 | 524512 | **02:02:09.695410** | observed | 9.873e-06 / 4,887.17 | `13tN4NpF…` meteora |
| HAKUCHAN (21) | 37 | 524513 | **02:02:09.695410** | observed | 1.029e-04 / 22,228.48 | `4hotj3uk…` pumpswap |

Report denominators: `15m due_total=2 attempted=2 observed=2 missed_attempted=0`,
completion 1.0, coverage 1.0, liq_field 1.0; target-distance 298.1 s.

## 1h — PASS (observed naturally this session)

| Item | Evidence |
| --- | --- |
| Scheduled / triggered | 02:22:07Z / **02:22:07** (natural, no manual start) |
| Journal | `Starting …c7-j2.service` 02:22:07 → `Started` 02:22:09; consumed 1.185 s CPU, 73.0 M peak |
| Service | `Result=success`, `ExecMainStatus=0`, `NRestarts=0` |
| Self-removal | `LoadState=not-found`; only `c7-j3`/`c7-j4` units remain |
| Job status | `status=completed reason=observation_attempt_complete exit_code=0` |
| Calls / writes | `external_calls=2`, `observations_recorded=2`, `ticks_written=2` |
| Reports | **4/4** |

| Member | Obs | Tick | `observed_at` | Status | Price / liquidity | Pair / venue |
| --- | --- | --- | --- | --- | --- | --- |
| MHGA (20) | 38 | 524912 | **02:22:08.349234** | observed | 7.722e-06 / 4,322.72 | `13tN4NpF…` meteora |
| HAKUCHAN (21) | 39 | 524913 | **02:22:08.349234** | observed | 1.564e-06 / 1,624.26 | `4hotj3uk…` pumpswap |

Report denominators after the 1h pass: `15m attempted=2 observed=2`,
`1h due_total=2 attempted=2 observed=2 missed_attempted=0` (completion 1.0, coverage 1.0),
`6h not_due=2`, `24h not_due=2`; target-distance p50 298.1 s / max 1799.5 s.

**Market-outcome note (not an orchestration finding).** HAKUCHAN's price fell ~98.5%
(1.029e-04 → 1.564e-06) with liquidity 22,228 → 1,624 between the 15m and 1h marks, and
MHGA drifted 9.873e-06 → 7.722e-06. Both members were nonetheless observed cleanly with
`missing_cause=None`. Per the canary's own rule, market outcome does not determine
orchestration success — and this lane never produces EV, a side, a size, or a
recommendation.

## 6h — HOST-OWNED PENDING

`probability-arena-horizon-c7-j3.timer` / `.service` — `LoadState=loaded`,
`ActiveState=active`, `SubState=waiting`, **`LastTriggerUSec=` empty**,
`NextElapseUSecRealtime=2026-08-01 04:52:07 UTC` (2026-07-31 21:52:07 PDT). Not triggered,
not backfilled, not disarmed.

## 24h — HOST-OWNED PENDING

`probability-arena-horizon-c7-j4.timer` / `.service` — `LoadState=loaded`,
`ActiveState=active`, `SubState=waiting`, **`LastTriggerUSec=` empty**,
`NextElapseUSecRealtime=2026-08-01 13:52:07 UTC` (2026-08-01 06:52:07 PDT). Not triggered,
not backfilled, not disarmed.

## Full lifecycle reconciliation

| Horizon | Nominal target | Window | Scheduled | Actual trigger | Planner state | Members | Obs IDs | Exit | Reports | Cleanup | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 15m | 02:07:07.831302Z | 01:59:37→02:14:37Z | 02:02:09.247313Z | **02:02:09** | due_now | 2/2 | 36, 37 | 0 | 4/4 | self-cleaned | **PASS** |
| 1h | 02:52:07.831302Z | 02:22:07→03:22:07Z | 02:22:07.831302Z | **02:22:07** | due_now | 2/2 | 38, 39 | 0 | 4/4 | self-cleaned | **PASS** |
| 6h | 07:52:07.831302Z | 04:52:07→10:52:07Z | 04:52:07.831302Z | — | not_due | — | — | — | — | units installed | **pending** |
| 24h | 2026-08-02T01:52:07.831302Z | 08-01T13:52:07→08-02T13:52:07Z | 13:52:07.831302Z | — | not_due | — | — | — | — | units installed | **pending** |

- Intended member-horizon observation states: **8**; recorded so far: **4** (`15m: 2`, `1h: 2`)
- Duplicate `(member_id, horizon)` check: **empty**
- Backfill: **none** — only 15m and 1h rows exist
- Manual trigger / retry loop: **none**; every execution was timer-driven
- Missed: **0**; completed: **2 of 4 horizons**
- Cohort-7 units removed after completion: j1 ✓, j2 ✓; j3/j4 intentionally still installed

## Shared-pass confirmation

Both executed horizons satisfy the full shared-pass criterion:

```text
one timer trigger → one service execution → one cohort pass
→ two members processed → one observation state per member
```

The proof in each case is the **identical pass-level `observed_at`** across both members —
`02:02:09.695410` for 15m and `02:22:08.349234` for 1h. Two members were served by a
single bounded pass, not by two separate passes, exactly as the deduplicated
`affected_tokens=2 affected_observations=2` job plan predicted.

## Provider governance

| Metric | 15m | 1h |
| --- | --- | --- |
| `external_calls` | 2 | 2 |
| Provider | DexScreener only | DexScreener only |
| Requests | one bounded `token-pairs/v1/solana/<mint>` GET per member | same |
| SolanaTracker / Birdeye / GoPlus | **0 / 0 / 0** | **0 / 0 / 0** |

Both job logs show exactly two `api.dexscreener.com` GETs and nothing else.

**Counter attribution (important, and a repeat of a documented gotcha).** The global
`crypto-provider-budget-report` shows SolanaTracker `hour=30 today=345`. This is **not**
attributable to the canary: it is the ongoing background MarketOps crypto stage via
`ENABLE_CRYPTO_RISK_ENGINE` (~15 lookups per natural scan, `per_run_lookup_limit=15`),
which runs every cycle independent of this lane. The horizon observation lane is
statically DexScreener-only. No paid provider was authorized or requested by this canary,
and no denied-provider request occurred.

## Cleanup and isolation

| Check | Result |
| --- | --- |
| Cohorts / members / observations | 7 / 21 / **39** (35 → 39 = exactly the 4 authorized rows) |
| Cohort 7 by horizon | `15m: 2`, `1h: 2` — nothing else |
| Horizon units | j1, j2 **self-removed**; j3, j4 pending by design |
| MarketOps | 6775–6778 all `ok`; no failure caused by the canary |
| Manual discovery / tape / MarketOps trigger | **none** (0 manual tape runs) |
| Second scan caused by canary | **none** |
| `.env` / flags / systemd schedules / SQLite pragmas | **unchanged** |
| Migration | none — Alembic `0027` throughout |
| Unrelated units | unchanged |
| Database bytes | 4,310,507,520 → 4,310,507,520 (unchanged) |
| Host free / used | 93,980,266,496 / **62%** |
| Telemetry JSONL | 2,941 → 2,973 lines, scheduled `tick_aggregation` only; no sink failure |
| Trading / capital behaviour | none — lane is observation-only by construction |

## Verdict

```text
EXECUTED HORIZONS (15m, 1h): PASS
FULL LIFECYCLE: INCOMPLETE — 6h and 24h not yet executed
```

Gate 6's PASS requires *all four* horizons triggered naturally. Two have. Nothing failed,
so `FAIL` would misrepresent a healthy canary; but `PASS` would assert something untrue
about the 6h and 24h horizons. The lifecycle is therefore reported as **incomplete and
pending**, with the two executed horizons passing cleanly on every criterion.

## Gate 7 — Render-preview follow-up decision

```text
IMPLEMENT CRYPTO-HORIZON-ORCHESTRATOR-RENDER-PREVIEW-001
```

Assessment from the deployed code (`app/services/crypto_horizon_orchestrator.py`):

- `SystemdUserManager.render_service(job, store)` and `render_timer(job)` are **pure
  string functions**. They touch no filesystem and call no `systemctl`.
- The rendered text embeds only `self.project_root`, `self.python_path`, and
  `store.log_path(cohort_id, job_id)` — none of which depend on *where* the text is
  written. A temp-directory render is therefore **provably byte-identical** to the
  installed unit.
- `install_jobs()` is the sole writer/enabler, and the dry-run path already computes the
  same `jobs` list it consumes.

A `--render-dir` (or equivalent) on the `arm-cohort` dry-run path is consequently a small,
additive change that reuses the two existing renderers, performs zero installs, and
changes no runtime semantics (`persisted=false`, `installed=false` already). It would
close the sole operational finding of the adaptive canary — that `systemd-analyze verify`
had to be run post-install because reconstructing job dicts would have consumed the
arming slack that lost attempts 4 and 5.

**Not implemented in this session**, per the boundaries.

## Next operational priorities

1. **Finalize 6h and 24h from durable evidence** in a genuinely later session (after
   ~2026-08-01T14:00Z). No intervention needed meanwhile — both are host-owned and healthy.
2. **SQLITE-STORAGE-GROWTH-001** — still the selected next operational milestone from the
   fourteen-day checkpoint; the DB remains ~132% of its 3072 MiB app-level gate.
3. **CRYPTO-HORIZON-ORCHESTRATOR-RENDER-PREVIEW-001** — small, closes the canary's only
   operational finding.
4. **Forecast PR stack** — PR#2 still carries the absolute-date time-bomb
   (`NOW = 2026-07-16T12:00Z` vs a real-clock `hours=240` lookback); fix, then run the
   Gate-19 merge sequence. Consider a hygiene sweep for this defect class (third
   occurrence).
5. **`probability-arena-backup.timer`** — declared in canon as expected but never
   installed, against a 4.3 GB production database.
