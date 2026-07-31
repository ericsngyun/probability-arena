# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — Pre-authorized bounded attempt (2026-07-31)

```text
VERDICT: NO QUALIFYING LIVE PAIR WITHIN PRE-AUTHORIZED WINDOW
MUTATIONS: ZERO
```

First attempt executed under an explicit human pre-authorization to act on the **first
naturally observed qualifying live pair**. The authorization was bounded at 15 inspected
natural MarketOps cycles / 90 minutes wall-clock / 1 pair acted upon, whichever came
first. **The 15-cycle bound was reached first, so the attempt stopped with no mutation.**

No cohort was created, nothing was armed, no observation was triggered, no unit was
installed, no provider was called, no `.env`/flag/schedule/pragma was changed, and no
manual discovery, MarketOps, or tape run was executed.

## Baseline (Gate 1)

Captured 2026-07-31T21:43:22Z (Mac) / 21:43:2xZ (EVO-X2), 14:43 PDT.

| Item | Value |
| --- | --- |
| Mac HEAD = origin/main = EVO-X2 HEAD | `e0a839c` |
| Alembic | `0027 (head)` |
| `MARKETOPS_INCLUDE_CANDIDATE_READINESS` | `true` |
| `MARKETOPS_INCLUDE_CRYPTO_TAPE_ANCHOR_FEED` | `true` |
| Horizon units installed | **0** |
| Cohorts / members / observations | 6 / 19 / 35 |
| Birth anchors / tape runs | 3,302 / 1,492 |
| Database | 4,251,414,528 B |
| Host free / used | 94,062,575,616 B / 62% |
| SQLite journal / synchronous | `delete` / `2` (FULL) |

**Baseline deviation, disclosed:** the authorizing prompt specified
`HEAD = 8d9731d`. All three repositories were in fact at `e0a839c` — `8d9731d` plus one
**documentation-only** commit (the fourteen-day checkpoint record) produced by the
immediately preceding authorized step. The substantive Gate-1 requirements
(three-way synchronization, Alembic `0027`, zero horizon units) were satisfied, so the
attempt proceeded on that basis rather than halting on a stale literal.

## Bounded qualifying-pair watch (Gate 2)

Watch opened 2026-07-31T21:43:59Z, closed 23:06:32Z — **82.5 minutes**, **15 cycles
inspected (6731–6745)**. Read-only throughout: no timer, daemon, cron entry, or
permanent watcher was created; the watch was a foreground bounded poll of the natural
readiness JSONL and `marketops_runs`.

Every inspected cycle was MarketOps-healthy (`status=ok`, `stage_errors={}`) with
`external_calls=0` on both the readiness and anchor-feed hooks.

| Cycle | Readiness state | Recorded slack (s) | Qualified |
| --- | --- | --- | --- |
| 6731 | `shared_due_now_ready` | 28.4 | no — below 300 s floor |
| 6732 | `pair_detected_not_due` | 739.5 | no — not due |
| 6733 | `pair_ready_for_manual_preparation` | 445.3 | no — not due-now |
| 6734 | `shared_due_now_ready` | 73.4 | no — below floor |
| 6735 | `shared_due_now_ready` | 88.4 | no — below floor |
| 6736 | `shared_due_now_ready` | 21.4 | no — below floor |
| 6737 | `shared_due_now_ready` | 23.9 | no — below floor |
| 6738 | `insufficient_arm_slack` | — | no |
| 6739 | `expired` | — | no |
| 6740 | `pair_detected_not_due` | 1107.4 | no — not due |
| 6741 | `pair_ready_for_manual_preparation` | 748.1 | no — not due-now |
| 6742 | `shared_due_now_ready` | **387.5** | **cleared the threshold on record, but see below** |
| 6743 | `shared_due_now_ready` | 28.7 | no — below floor |
| 6744 | `pair_detected_not_due` | 1107.9 | no — not due |
| 6745 | `pair_ready_for_manual_preparation` | 752.3 | no — not due-now |

Per Gate 2 the attempt did **not** act on `pair_ready_for_manual_preparation`,
`pair_detected_not_due`, `insufficient_arm_slack`, or `expired`, and did **not** wait for
an already-under-slack pair to become safer.

### Cycle 6742 — the one record that cleared the threshold, and why it was not acted on

Cycle 6742 (`EmyH4MA3…pump` + `F7jwo5gT…pump`, window 22:43:34→22:58:34Z) recorded
`shared_due_now_ready` with **387.5 s** of safe slack at its evaluation instant
(22:48:21.864798Z). On its recorded values it satisfied the numeric threshold.

**It was not actionable, and was correctly refused.** An SSH transport interruption
during the watch (`Read from remote host … Operation timed out`) meant the session
reconnected at 23:02:13Z and replayed the records accumulated during the gap. By the time
cycle 6742's record was *observed*, its shared window had closed at 22:58:34Z — more than
four minutes earlier. The pair was expired at observation, and creating a cohort from an
expired pair is an absolute operational boundary.

**This exposed a real methodology defect in the watch, which was corrected mid-attempt.**
The original qualification test trusted the record's stored
`remaining_safe_slack_seconds`, which is a snapshot of slack *at evaluation time*, not
*now*. The corrected test recomputes live slack as
`shared_window_close − now − 225 s` (45 s activation grace + 180 s operator prep) and
additionally reports record age. Under the corrected test, cycle 6743 re-evaluated the
same pair at a live slack of **−479.1 s**, confirming the expiry. All remaining cycles
were assessed with the corrected live-slack test.

This is a defect in the checkpoint's own monitoring script, **not** in any deployed
Probability Arena code, and nothing was implemented in response to it beyond correcting
the throwaway watch logic.

## Structural finding — the 300 s threshold is rarer than the headline slack figure suggests

The fourteen-day checkpoint reported median safe arm slack of ~378 s across *live*
evaluations. That figure pools two states and is misleading for arming purposes.
Restricted to `shared_due_now_ready` — the only state this authorization may act on —
the distribution across all 663 such records is:

```text
n = 663      >= 300 s: 246 (37.1%)
median 30.4 s    p75 384.7 s    p90 389.3 s    max 451.7 s
```

The distribution is **sharply bimodal**, not centred near 378 s: a due-now evaluation
either lands early in the shared window (~385 s) or late (~30 s), with little between.
Since `shared_due_now_ready` occurs on ~18% of cycles and only 37.1% of those clear
300 s, a qualifying cycle arises on roughly **6.8% of all cycles** — an expected
**~14.8 cycles** to the first qualifier.

**The authorized 15-cycle bound was therefore sized at approximately a coin flip.** This
attempt exhausting its budget is the expected outcome slightly more often than not, and
is not evidence of any regression: pair *arrival* remains abundant (six `shared_due_now_ready`
records in 15 cycles), but arrival at ≥300 s of slack is the scarce event.

Contributing geometry: several observed shared windows were ~9 minutes wide rather than
the full 15 (e.g. 21:56:34→22:05:34Z, 22:07:34→22:16:37Z), because the shared window is
the *intersection* of two members' 15-minute windows. With a ~6-minute MarketOps cadence,
a 9-minute intersection frequently yields its first due-now evaluation with under 300 s
remaining.

## State at bound exhaustion (Gate 2 stop)

At 23:06:32Z the watch stopped on the cycle bound with cycle 6745 showing a **healthy
pair one cycle away from qualifying**: `5ti3D9iB…rns` + `BTfe3RG7…pump`,
`pair_ready_for_manual_preparation`, live slack **747.7 s**, shared window
23:07:37→23:22:37Z — a full 15-minute intersection on track to present
`shared_due_now_ready` at roughly 23:11–23:13Z with ~385 s of slack.

It was **not** acted upon: the authorization's cycle bound had been reached, and acting
past a bound is outside the authorization regardless of how favourable the pair looks.

## Provider, MarketOps, and mutation isolation (Gate 10)

| Check | Result |
| --- | --- |
| Cohorts / members / observations | 6 / 19 / 35 — **unchanged**, max cohort id still 6 |
| Horizon units installed | **0** |
| MarketOps failures during watch (6731–6745) | **0** |
| Readiness `external_calls` | 0 on every inspected cycle |
| Anchor-feed `external_calls` | 0 on every inspected cycle |
| Manual tape runs today / during watch | **0 / 0** (185 tape runs today, all `exact_cycle`) |
| All-time manual tape runs | 60 — unchanged; newest is run 60, the governed 2026-07-24 canary |
| Manual discovery / MarketOps / observation / backfill | none executed |
| Alembic | `0027` unchanged |
| `.env` / flags | unchanged (both `MARKETOPS_INCLUDE_*` still `true`) |
| Provider policy / paid providers | untouched; none authorized |
| Database | 4,251,414,528 → 4,276,883,456 B (+25.5 MB, normal background growth) |
| Host free / used | 94,033,510,400 B / 62% — unchanged |
| Telemetry JSONL | 2,893 → 2,909 lines (scheduled `tick_aggregation` only) |
| Unrelated units | untouched |
| Untracked artifact (2026-07-15 typo file) | untouched |

## Verdict

```text
NO QUALIFYING LIVE PAIR WITHIN PRE-AUTHORIZED WINDOW
```

The pre-authorization is **spent without being exercised**: no pair was selected, so
Gates 3–9 did not execute. Per the authorization's terms this does not roll over — a
further attempt requires fresh explicit human authorization.

## Recommendations for the next authorization (not implemented)

1. **Widen the bound, or the attempt is a coin flip.** At ~6.8% qualifying cycles, a
   ~30-cycle / ~3-hour bound gives roughly 87% probability of catching a qualifier
   versus ~65% for 15 cycles. This is the smallest possible change and needs no code.

2. **Preferred: make the trigger two-stage, acting at `pair_ready_for_manual_preparation`
   instead of racing `shared_due_now_ready`.** The evidence is unambiguous — in every
   episode observed here (6732→6733→6734, 6740→6741→6742, 6744→6745→…),
   `pair_ready_for_manual_preparation` preceded `shared_due_now_ready` by exactly one
   cycle and carried **~745–750 s** of slack, versus a median of 30.4 s once due-now.
   Preparing and dry-running the cohort at `pair_ready`, then arming on the following
   due-now cycle, converts a sub-6-minute race into a ~12-minute two-stage operation and
   would have succeeded on at least three separate pairs during this 82-minute watch.
   This is a change to the canary *procedure*, not to any deployed code or to arming
   safety, but it materially changes what is being authorized and therefore requires
   explicit approval.

3. **Keep the live-slack test.** Any future watch must qualify on
   `shared_window_close − now − 225 s`, never on the stored slack field, so that a
   transport stall can never surface an expired pair as actionable.

Neither recommendation was implemented; per the operational boundaries, a discovered
defect is preserved as evidence and scoped as a separate milestone.
