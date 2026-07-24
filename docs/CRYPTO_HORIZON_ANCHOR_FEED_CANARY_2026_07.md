# CRYPTO-HORIZON-ANCHOR-FEED-CANARY-001 — one governed tape session (2026-07-24)

```text
VERDICT: PASS
READINESS OUTCOME: CURRENTLY LIVE PAIR FOUND — SEPARATE CANARY-004 APPROVAL REQUESTED
```

Operational canary using only existing accepted read-only infrastructure. One
bounded `crypto-tape-run-once` execution converted fresh, already-persisted
raw discovery evidence into canonical `CryptoTokenBirthEvent` anchors, and the
candidate-readiness hook — on its next **naturally scheduled** MarketOps cycle
— classified a live complete overlapping pair for the first time. No new
anchor-production path was implemented; no discovery scan, provider call,
cohort, observation, arming, unit, backfill, flag, or `.env` change occurred.

## Preconditions (Gate A baseline, 2026-07-24 ~05:12 UTC)

Mac = origin = EVO-X2 = `57adef1`; tracked-clean (known unrelated untracked
files only); Alembic `0027`; `MARKETOPS_INCLUDE_CANDIDATE_READINESS=true`.
Birth anchors **508** (latest 2026-07-16T04:45:57Z); readiness JSONL 1,781
lines (last cycle 4877, `expired`); cohorts 6 / observations 35; horizon
units 0; DB 3,681,239,040 B; ~74.3 GB free; 0 lock events in the prior hour;
telemetry JSONL 14,955 B; SolanaTracker counters hour=30 / today=765 /
month=59,828 (budget healthy, `remaining_daily=4,235`).

## Provider call-path proof (static, zero calls made)

`crypto-tape-run-once` → `app/cli.py:1402 crypto_tape_run_once` →
`CryptoLifecycleTapeRecorder.run_once` (`app/services/crypto_tape.py:608`):

- **Module imports** (`crypto_tape.py:24-47`): stdlib + SQLAlchemy +
  `app.config` + `app.models` **only**. No adapter, no `httpx`, no provider
  module is imported anywhere in the file (verified by exhaustive grep); the
  only `await` usages are session-cadence sleepers in `run_tape_session`
  (not invoked here).
- **Data sources** (`_load_sources`, `:178-219`): persisted
  `crypto_pairs`/`crypto_price_ticks`/`crypto_token_discovery_events`/
  `crypto_token_risk_assessments`/meme attention rows only — SELECTs on the
  local database.
- **Selection** (`_universe`, `:167-176`): newest `crypto_tokens` by
  `first_seen_at` within `--hours`, capped by `--limit` — a deterministic
  bound that can pin the run to exactly one discovery cycle's tokens.
- `external_calls: 0` is a structural property (hardcoded in the summary
  because no call site exists), not a runtime assertion.
- Providers reachable: **none** — directly or indirectly (SolanaTracker,
  Birdeye, GoPlus, DexScreener are all unreachable from this path; the
  provider gate governs `crypto-scan-once`/`crypto-risk-assess`, which were
  not used). Anchors are written with **no provider access before or after**;
  there is no provider exception to swallow.
- **PATH 1 PROVEN**: anchors materialize from local persisted data with zero
  provider calls. No flag was changed to make the path safe.

The wrapper's `run_migrations()` is a no-op at Alembic head `0027`.

## Selected natural discovery cycle (Gate B)

Cycle **4878** (05:12 UTC) persisted 0 new tokens and was rejected; the
watcher then selected the first qualifying natural cycle:

- **MarketOps cycle 4881**: 05:30:03.918 → 05:30:38.127 UTC, status `ok`,
  `crypto_scan: ok`, `stage_errors={}`, readiness record `expired`
  (`external_calls=0`), **exactly one crypto scan** in the window.
- **Newly persisted raw tokens: 3** (all `first_seen_at=2026-07-24T05:30:04.268088Z`,
  launch source `dexscreener:profile`):

| Token (canonical) | Symbol | Pairs | First tick price | First tick liquidity | Complete-enough |
|---|---|---|---|---|---|
| `8aLzkFAgbULQyL6QyeuH84Pxe2EdJdmZfqWpZRYvpump` | Potato | 1 (pumpfun) | 2.83e-06 | null | no (null liquidity) |
| `2nxtQZjhEH1ukbrdQKdzkMubNoRVYie5pv1G7r1wpump` | King | 3 | 8.464e-05 | 20,681.59 | yes |
| `7z4cgsb7egGx4iWXioU5agYP2cU5tyoXZakSCxafpump` | Octen | 2 | 1.358e-04 | 26,202.17 | yes |

Age at persistence ≈ 0 s (local `first_seen` = scan instant; chain-side pair
creation 19–43 min earlier). Universe check: the newest-3-in-1h selection the
tape would make **exactly equals** this cycle's token set (verified against
the live table before running). Run-scoped provider attribution: the cycle's
readiness summary and scan ledger show the normal single governed scan only.

## Tape preview (Gate C — dry run, persisted nothing)

`crypto-tape-run-once --hours 1 --limit 3 --dry-run` at 05:32:07 UTC:
`status=dry_run`, `external_calls=0`, `tokens_considered=3`,
expected `birth_events=3`, `snapshots=3`, `actor_observations=3`,
`outcomes=3`; all three selected tokens are the cycle-4881 set (no stale, no
malformed, all with local tick/risk evidence; their 15m windows open at
05:37:34Z — not closed). Post-dry-run: anchors still 508, tape runs still 59
(`persisted=false` proven). No provider allow/deny state applies — the path
has no provider reach (PATH 1); call cap n/a.

## Tape execution (Gate D — the one authorized session)

`crypto-tape-run-once --hours 1 --limit 3` — single attempt,
05:32:07.764 → 05:32:08.462 UTC (282 ms service duration), `status=ok`,
`external_calls=0`, `tape_run_id=60`. No retry was needed; the command's
pre-existing bounded lock ladder was untouched and unused.

| Measure | Before | After |
|---|---|---|
| Birth anchors | 508 | **511** |
| Latest anchor | 2026-07-16T04:45:57Z | 2026-07-24T05:32:08Z |
| Tape lifecycle runs | 59 | 60 |
| SolanaTracker hour/today/month | 90 / 825 / 59,888 | **90 / 825 / 59,888 (unchanged)** |
| Cohorts / observations / horizon units | 6 / 35 / 0 | 6 / 35 / 0 |
| Readiness JSONL lines | 1,785 | 1,785 (next natural cycle appends) |
| Database bytes | 3,681,239,040 | 3,681,239,040 (page reuse; no growth) |
| Filesystem free | ~74.3 GB | ~73.9 GB (unrelated host activity) |
| Lock events (05:30→05:37) | — | **0** |
| Telemetry JSONL | 14,955 B | 14,955 B (tape is not an instrumented writer — expected) |

(`rolling_24h` decayed 3611→3607 during the run — window decay, not calls;
hour/today/month counters are the request evidence.)

## Anchor records created

All three anchors: source = tape run 60 over cycle-4881 tokens;
`first_evidence_at = 2026-07-24T05:30:04.268088Z`; persisted
05:32:08.064697Z; **persistence lag 123.8 s**; 15m target 05:45:04Z;
planner window open 05:37:34.268088Z, close 05:52:34.268088Z; arm deadline
(45 s grace + 180 s margin) 05:48:49.268088Z; **remaining safe arm slack at
persistence 1,001.2 s** (~16.7 min); provenance rows record the discovery
event/tick/assessment IDs.

| Anchor | Token | Pair / venue | p0 | l0 | Completeness |
|---|---|---|---|---|---|
| 509 | Potato `8aLzkF…pump` | `9YVoqz…` / pumpfun | 2.83e-06 | null | `liquidity_or_initial_state_missing` |
| 510 | King `2nxtQZ…pump` | `6AdiKx…` / pumpfun | 8.464e-05 | 20,681.59 | **COMPLETE** |
| 511 | Octen `7z4cgs…pump` | `HWpav3…` / pumpswap | 1.358e-04 | 26,202.17 | **COMPLETE** |

No price behavior or market interpretation is made or implied.

## Horizon feasibility

```text
anchors_attempted                 3
anchors_created                   3
complete_anchors                  2
15m_feasible_at_persistence       3 (all persisted 05:32:08 < window close 05:52:34)
safe_arm_slack_at_persistence     1001.2 s
provider_calls_by_provider        solana-tracker=0 birdeye=0 goplus=0 dexscreener=0
external_calls                    0
```

The same-cycle tape pass collapses the anchor persistence lag from the
historical ~85 min (batch sessions) to **~2 min**, inside the 15m window with
>16 min of slack — the property the readiness lane was starved of all week.

## Next natural readiness cycle (Gate F)

Cycle **4882** (naturally scheduled; started 05:36:03, finished 05:36:49,
`ok`, exit 0, `Result=success`; exactly one crypto scan; `stage_errors={}`):

- Exactly **one** readiness record appended (line 1,786; no duplicates),
  `external_calls=0`.
- New anchors visible: `complete_candidates` 198 → **200**;
  `overlapping_pairs` 197 → **198**; `usable_pairs` 0 → **1**.
- **Readiness state: `pair_ready_for_manual_preparation`** — the first
  non-expired live state in 1,786 records:

```text
candidate_token_a = 2nxtQZjhEH1ukbrdQKdzkMubNoRVYie5pv1G7r1wpump  (King)
candidate_token_b = 7z4cgsb7egGx4iWXioU5agYP2cU5tyoXZakSCxafpump  (Octen)
shared_window_open  = 2026-07-24T05:37:34.268088Z
shared_window_close = 2026-07-24T05:52:34.268088Z
evaluated_at        = 2026-07-24T05:36:29.633090Z
remaining_safe_slack_seconds = 739.6
```

- No cohort, observation, arming, or unit action occurred (6 / 35 / 0
  unchanged). **Nothing was created or armed from this pair** — CANARY-004
  requires separate explicit human approval, which is hereby requested for a
  *future* live pair window; this specific window closes at 05:52:34Z and
  must not be armed retroactively after it expires.

## Readiness result

`pair_ready_for_manual_preparation` (King+Octen). The evaluator behaved
exactly per its documented classification: at evaluation (05:36:29) the
shared window opened within the 180 s operator margin (open 05:37:34) with
739.6 s of safe slack remaining.

**Pair persistence at the following natural cycle:** cycle 4883 (evaluated
05:42:31, record 1,787, `external_calls=0`) advanced the same pair to
**`shared_due_now_ready`** with 378.1 s of safe slack — the full state
ladder (`expired` → `pair_ready_for_manual_preparation` →
`shared_due_now_ready`) executed live across consecutive natural cycles,
with cohorts/observations/units still 6 / 35 / 0. The window then closes
naturally at 05:52:34Z (subsequent records are expected to return to
`insufficient_arm_slack`/`expired` — correct behavior, not a failure).

## Measurement epoch split

```text
Epoch 1 — anchor feed inactive:
  2026-07-16T19:56:26Z → 2026-07-24T05:32:07Z
  cycles 3097–4881 (readiness records 1–1,785): 100% expired; catch-rate
  vacuous (zero-denominator; anchor starvation per the 7-day checkpoint).

Epoch 2 — anchor feed deliberately activated once:
  from 2026-07-24T05:32:08Z (tape run 60)
  cycle 4882 onward: first live pair classified.
```

The July-30 report must analyze these epochs separately; a combined
undifferentiated catch rate would be meaningless. **One governed session does
not establish long-term cadence sufficiency** — it proves the mechanism, not
the operating cadence; any recurring anchor-feed cadence is a separate,
explicitly approved milestone.

## SQLite and storage impact

The DB remains past its 3,072 MB absolute internal gate (3,681,239,040 B —
pre-existing condition). The tape session added 3 birth + 3 snapshot +
3 actor + 3 outcome + 1 run row with **zero measurable file growth** (page
reuse), zero lock events, zero telemetry growth, and no scheduled-writer
failure during the session window. Free space ~73.9 GB (well above any
critical threshold). No retention/WAL/schedule/transaction change was made.

## Verdict

**PASS.** Every Gate E criterion held: ≥1 new canonical anchor (3, two
complete); anchors traceable to natural cycle 4881 (tape run 60 provenance);
complete initial state preserved from persisted evidence; zero provider
requests of any kind (counters flat, path statically provider-free); no
second scan; no cohort/observation/unit; no lifecycle-semantic change; no
MarketOps behavior change; single bounded attempt; no unexpected database or
telemetry effect.

## July 30 implications

1. The readiness measurement's zero-denominator problem is a **feed-cadence
   problem, not an evaluator problem** — proven live end-to-end.
2. Epoch-2 evidence now exists; the 14-day report must segment epochs.
3. Any decision on a *recurring* anchor-feed cadence (or
   CRYPTO-DISCOVERY-FRESHNESS-001) belongs to the July-30 review, informed by
   this canary; nothing recurring was installed here.
4. A live pair can and did appear with ~12 min of actionable window —
   CANARY-004 execution remains gated on separate explicit human approval
   *at such a moment*, never retroactively.
