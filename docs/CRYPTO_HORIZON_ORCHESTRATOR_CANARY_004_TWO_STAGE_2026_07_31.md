# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — Two-stage attempt (2026-07-31 → 2026-08-01)

```text
VERDICT: NO QUALIFYING PAIR-READY MOMENT WITHIN BOUNDED WINDOW
MUTATIONS: ZERO
```

First attempt under the two-stage authorization: prepare a cohort during
`pair_ready_for_manual_preparation` (Stage A, ≥720 s live slack), then arm it when the
**same** pair naturally advances to `shared_due_now_ready` (Stage B, ≥300 s live slack).

The 15-cycle bound was reached first. **No Stage-A pair qualified**, so no pair was
frozen, no cohort was created, nothing was armed, and Gates 5–11 did not execute.

## Baseline (Gate 1)

Captured 2026-07-31T23:1xZ. **`Mac HEAD = origin/main = EVO-X2 HEAD = 9b0a85b`** —
matches the required literal exactly; no deviation this time. Alembic `0027 (head)`;
both `MARKETOPS_INCLUDE_*` flags `true`; **horizon units installed 0**; cohorts /
members / observations 6 / 19 / 35; journal `delete`, synchronous `2`; host 62% used.

## Current-time rule and canonical math

Stored `remaining_safe_slack_seconds` was treated strictly as historical evidence. All
qualification used the **deployed canonical helpers**, imported directly rather than
reimplemented:

```python
from app.services.crypto_horizon_feasibility import safe_arm_deadline   # close − ACTIVATION_GRACE − margin
from app.services.crypto_horizon_readiness import OPERATOR_PREP_MARGIN_SECONDS  # 180.0
# ACTIVATION_GRACE = timedelta(seconds=45)  (crypto_horizon_orchestrator.py:61)
live_slack = (safe_arm_deadline(shared_window_close, OPERATOR_PREP_MARGIN_SECONDS) - host_now).total_seconds()
```

Live and stored values agreed to within record age throughout (e.g. cycle 6748: stored
745.0, live 742.1, age 3 s), confirming the previous attempt's defect was in the
throwaway watch script, not in deployed code.

## Bounded pair-ready watch (Gate 2)

Opened 2026-07-31T23:20:14Z, closed 2026-08-01T00:46:31Z — **86.3 minutes, 15 cycles
(6748–6762)**. Read-only; no timer, daemon, cron entry, or permanent watcher created.
All 15 cycles were MarketOps-healthy with `external_calls=0` on both hooks.

| Cycle | State | Live slack (s) | Window width (s) | Stage-A |
| --- | --- | --- | --- | --- |
| 6748 | `pair_detected_not_due` | 742.1 | 537 | no — wrong state |
| 6749 | `pair_ready_for_manual_preparation` | **377.2** | 536 | no — < 720 |
| 6750 | `shared_due_now_ready` | 28.4 | 536 | no |
| 6751 | `expired` | — | — | no |
| 6752 | `pair_detected_not_due` | 750.1 | 542 | no — wrong state |
| 6753 | `pair_ready_for_manual_preparation` | **378.1** | 542 | no — < 720 |
| 6754 | `shared_due_now_ready` | 21.2 | 542 | no |
| 6755 | `shared_due_now_ready` | **378.4** | **900** | no — due-now barred as initial state |
| 6756 | `shared_due_now_ready` | 26.2 | 900 | no |
| 6757 | `pair_detected_not_due` | 805.7 | 598 | no — wrong state |
| 6758 | `pair_ready_for_manual_preparation` | **444.6** | 598 | no — < 720 |
| 6759 | `shared_due_now_ready` | 122.1 | 598 | no |
| 6760 | `insufficient_arm_slack` | — | — | no |
| 6761 | `expired` | — | — | no |
| 6762 | `expired` | — | — | no |

Three `pair_ready` records appeared (377.2 / 378.1 / 444.6 s) — every one short of the
720 s gate, and all three on **narrow** shared windows.

## Why Stage A did not fire — the window-width geometry

The shared window is the **intersection** of two members' 15-minute windows, so its width
varies. `pair_ready_for_manual_preparation` fires in the ~225 s band *before* the window
opens, which makes the achievable preparation slack:

```text
live_slack = (window_width − 225) + (window_open − now),   with (window_open − now) ∈ (0, 225]
```

So for a given width the slack ceiling is `width − 225 + 225 = width`, and the *floor*
once ready is `width − 225`:

| Width | Slack range while `pair_ready` | Can reach 720 s? |
| --- | --- | --- |
| 900 s (full) | 675 – 900 | **yes**, if caught ≥45 s before open |
| 598 s | 373 – 598 | no |
| 542 s | 317 – 542 | no |
| 536 s | 311 – 536 | no |

**Every `pair_ready` record in this watch sat on a 536–598 s window, where 720 s is
arithmetically unreachable.** The single full-width (900 s) episode of the watch —
cycles 6755/6756 — was first observed *already* in `shared_due_now_ready` at 378.4 s,
because its window opened between two MarketOps evaluations. Gate 3 bars
`shared_due_now_ready` as an initial preparation state, so it could not be used.

That episode is worth stating plainly: **cycle 6755 would have been a valid qualifier
under the previous one-stage authorization (due-now, ≥300 s live slack) and was excluded
by this one.**

## Structural calibration (measured, not assumed)

Over all 3,654 readiness records to date:

| Gate | Records | Share of pair_ready | **Share of all cycles** | Expected cycles to first hit |
| --- | --- | --- | --- | --- |
| `pair_ready` (any) | 257 | — | 7.03% | ~14 |
| **Stage A: `pair_ready` ≥720 s** | **145** | 56.4% | **3.97%** | **~25** |
| Stage 1 (previous auth): `due_now` ≥300 s | 246 | — | 6.77% | ~15 |

`pair_ready` shared-window widths: median 900 s, and **58.0% are ≥870 s** — i.e. the
720 s gate is essentially a proxy for "full-width window", and roughly 42% of pair-ready
moments can never satisfy it.

**The central finding: the two-stage gate is _rarer_ per cycle than the one-stage gate it
was meant to improve on (3.97% vs 6.77%).** The two-stage design does deliver its
intended benefit — once triggered it gives ~12 minutes of margin instead of a
sub-6-minute race — but as specified it roughly *halves* the trigger rate, and a
15-cycle bound (~45% catch probability) is too small for it. This attempt exhausting its
budget was the more likely outcome.

Tonight's sample was additionally width-unlucky: only 1 of 4 pair episodes had a
full-width window, against 58% historically.

## Isolation evidence (Gate 12)

| Check | Result |
| --- | --- |
| Cohorts / members / observations | 6 / 19 / 35 — **unchanged**; max cohort id still 6 |
| Horizon units installed | **0** |
| MarketOps runs 6748–6762 / non-`ok` | 15 / **0** |
| Readiness & anchor-feed `external_calls` | **0** on every inspected cycle |
| Manual tape runs during watch | **0** (11 tape runs, all `exact_cycle`) |
| All-time manual tape runs | 60 — unchanged (newest is the governed 2026-07-24 canary) |
| Manual discovery / MarketOps / observation / backfill / retry | none |
| Provider policy / paid providers | untouched; none authorized |
| Alembic | `0027` unchanged |
| `.env` / flags | unchanged (both still `true`) |
| Git | `9b0a85b`, tracked-clean |
| Database | 4,276,883,456 → 4,310,507,520 B (+33.6 MB background growth) |
| Host free / used | 93,991,055,360 B / 62% |
| Telemetry JSONL | 2,909 → 2,941 lines (scheduled `tick_aggregation` only) |
| Unrelated units / untracked artifact | untouched |

## Verdict

```text
NO QUALIFYING PAIR-READY MOMENT WITHIN BOUNDED WINDOW
```

The authorization is **spent without being exercised**. No pair frozen, no cohort
created, nothing armed. A further attempt requires fresh explicit human authorization.

## Recommendation for the next authorization (not implemented)

**Authorize both entry paths under one bound**, rather than choosing between them:

- **Path A (preferred when available):** `pair_ready_for_manual_preparation` with
  ≥720 s live slack → prepare cohort now, arm on the next natural due-now cycle. Gives
  ~12 minutes of margin.
- **Path B (fallback):** `shared_due_now_ready` with ≥300 s live slack → dry-run,
  create, and arm within the same cycle, exactly as the previous authorization allowed.

The two paths are disjoint by construction (different states), so their rates add to
**~10.7% of cycles ⇒ expected ~9 cycles to first qualifier**. Combined with a bound of
**25–30 cycles (~2.5–3 hours)**, that yields roughly 94–96% probability of exercising the
canary in a single session, versus ~45% for this attempt.

Retain unconditionally: live-slack computation via the deployed canonical helpers, and
the rule that stored `remaining_safe_slack_seconds` is historical evidence only.

Nothing above was implemented — per the operational boundaries, discovered constraints
are preserved as evidence and scoped as a separate decision.
