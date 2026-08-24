# MARKET-MICROSTRUCTURE-EDGE-001 — confirmation tranche ledger

**BLIND CAPTURE IN PROGRESS.** 20 sessions, ≥14 days, `dataset_role=CONFIRMATION`.

Authorised 2026-08-24 after: sampling-runner qualification (12/12 invariants),
row-builder qualification (11/11 mutations), the row-v2/label-v2 seam smoke
test, Amendment 3 (`realized_vol_1s` removed) and Amendment 4 (eligibility
gates on session-remaining, five bins proven reachable).

## What may and may not be inspected during capture

**Permitted:** capture health · sequence/archive integrity · safety peak ·
row/schema validity · mechanical power accumulation · session and TTE-bin
coverage · series and weekend coverage.

**Forbidden:** returns by feature · M0/M1 losses · coefficients · feature
importance · OFI/markout relationships · flow-conditioned returns · which
sessions "look promising."

The only adaptive action permitted is **scheduling future sessions to satisfy
already-preregistered coverage constraints** — never to improve a result.

## Bin allocation — 4/4/4/4/4, hardest first

| order | primary bin | sessions | status |
|---:|---|---:|---|
| 1 | `late_resolution` | 4 | **1 of 4 complete** |
| 2 | `live_event` | 4 | **1 armed** |
| 3 | `near_event` | 4 | not started |
| 4 | `approaching` | 4 | not started |
| 5 | `far` | 4 | not started |

A session counts toward bin *b* only if **a complete 300 s post-warmup panel
interval lies wholly within b**.

## Sessions

### Scheduling record (per session, frozen BEFORE capture)

`scheduled_target_bin` · `anchor_event_id` · `anchor_occurrence_datetime` ·
`scheduled_session_start` · `selection_rule_version` ·
`candidate_count_at_freeze` · `replacement_reason`

Timing and identity only. No activity outcomes, prices, returns or feature
statistics — enforced by an AST guard over
`scripts/kalshi_microstructure_schedule_anchor.py`, which fails if the
scheduler so much as references a volume or price field.

### Hard-bin ordering

`S02 live_event → S03 late_resolution → S04 live_event`, until both difficult
strata hold ≥3 qualifying sessions. Then `near_event`, `approaching` and `far`
fill out the 4/4/4/4/4 allocation.

| # | label | primary bin | start (UTC) | length | subscribed | status |
|---:|---|---|---|---:|---:|---|
| 01 | `MMEDGE-S01-late_resolution-20260824` | `late_resolution` | 2026-08-24T00:41:58Z | 10,800 s | 24 | **CLEAN — counts** |
| 02 | `MMEDGE-S02-live_event-20260824` | `live_event` | 2026-08-24T16:40:00Z | 10,800 s | 24 | **ARMED** |

### Session 01 — pre-capture record

Candidate set frozen before capture by the Addendum 1 rule
`(occurrence_datetime ASC, ticker ASC)` over open markets in the eligible
series whose event lies in the assigned bin.

* 24 open markets, all `KXWTAMATCH` (16) and `KXATPMATCH` (8)
* events **5.69–6.69 h in the past** — genuinely `late_resolution`, i.e. past
  the nominal occurrence time and **not** settled
* exactly the concurrency ceiling, so no truncation was required
* preflight: `40,000,000 > 3,500 × 10,800 = 37,800,000` ✓

**This session is also the feasibility discovery** Amendment 4 made possible.
Whether books past their nominal event time still publish enough sequenced
activity to clear the 30-events-per-300 s floor is **not established by any
tape we hold** — every prior session was 2–3 h pre-event. If the panel comes
back persistently empty, `late_resolution` is thin at this venue, and that is a
reportable finding rather than a reason to substitute an easier bin.

### Session 01 — verdict: **OPERATIONALLY CLEAN**, counts toward `late_resolution`

**L1 capture validity — PASS.** Terminal `capped_time`.
`events_received == events_archived == 1,019,783`. `frames_malformed`,
`events_rejected`, `rotation_failures`, `sequence_faults`, `reconnects` all
**0**. 81 segments committed. `peak_1s_sliding` **1,210** against the 3,500
stop.

**L2 sampling validity — PASS.** First tick exactly at open+300 s; no warmup
tick emitted a panel; tick gaps exactly `[300.0]`; max panel size **12**, K
respected; reason vocabulary closed with no unexpected reasons.

The final two ticks returned **empty panels, correctly**: at open+10,500 s and
open+10,800 s the scheduled session-remaining was 600 s and 300 s, and the
Amendment 4 gate is *strictly* greater than 600 s. The gate is visible working
in production.

**L3 dataset validity — PASS.** `microstructure-row-v2` / `label-v2`;
13 M0 and 17 M1 columns; minimum completeness **0.978 / 0.9781**; **no
always-missing columns**; `dataset_role=CONFIRMATION`; **113,100 rows**;
0 dispatch errors.

Label coverage: **0.9780 / 0.9779 / 0.9770 / 0.9674** at 1/5/30/300 s. The 300 s
horizon holds up at 96.7%, as the 3 h projection expected.

**L4 coverage outcome — the feasibility question is answered.**

| | |
|---|---|
| covering intervals wholly in `late_resolution` | **377** |
| counts toward target bin | **yes** |
| markets clearing the 30-events/300 s floor | **24 of 24** |
| markets ever selected | 21 |
| market-blocks accumulated | **377** |
| market/session clusters | **21** |
| bins touched | `late_resolution` only |
| no-row reasons | `eligible_but_never_top_K`: 3 |

**Post-event, unsettled books are dense enough to support the panel.** Every
one of the 24 markets cleared the activity floor. **No market closed or
resolved mid-session, and none failed activity eligibility** — the only reason
any market went unsampled was losing a top-12 competition, which is the
mechanism working, not a venue limitation.

That is a real finding about Kalshi's lifecycle: `TTE < 0` is not `market no
longer observable`, which is exactly the distinction Amendment 4 was written to
preserve. Under the old `TTE > 600 s` gate this session was impossible.

**Power, measured rather than projected.** 377 market-blocks and 21 clusters
from one session, against projections of 420 and a conservative 12. At this
rate 20 sessions yield ≈7,540 blocks (floor 4,000) and well past 150 clusters.

### S02 — deferred to a fuller slate, deliberately

Not launched on the night of 2026-08-23. The frozen design schedules around
event timing, so there is no reason to spend a `live_event` session on a
thinning late-Sunday card. **S01 already established that the hard strata are
available**, so nothing is at risk in waiting: all 24 post-event markets
cleared the activity floor, and the only exclusions were legitimate top-K
competition.

The anchor is chosen by `kalshi_microstructure_schedule_anchor.py`, not by
hand. It enumerates open markets in the eligible series, computes the required
start per bin, and takes the **earliest feasible occurrence time** — it cannot
prefer a busier-looking game because it never sees activity.

### Anchor arithmetic, per bin (3 h session)

| target bin | session start | complete intervals inside the bin |
|---|---|---:|
| `far` | event − 545 min | 35 |
| `approaching` | event − 365 min | 35 |
| `near_event` | event − 125 min | 20 |
| **`live_event`** | **event − 20 min** | **3** |
| `late_resolution` | event + 5 min | 35 |

`event − 20 min` puts the first post-warmup tick at TTE ≈ 900 s, the top of the
stratum.

**`live_event` is structurally the thinnest stratum**, and always will be: the
bin is 900 s wide, so a session can contribute at most three complete 300 s
intervals no matter how long it runs. Four sessions therefore yield ~12
covering intervals against ~140 for each of the wide bins. That is a
consequence of the frozen bin boundaries, not a defect, and it is **not** a
reason to widen them after the fact — but per-bin block counts will be sharply
unbalanced and the analysis must not read that imbalance as a property of the
markets.

---

## A note for the future evaluator: unequal block counts are geometric

Per-bin block counts will be **sharply unbalanced**, and the cause is the
frozen bin widths, not the markets:

| bin | TTE width | time-intervals / session | market-blocks / session (K=12) | over 4 sessions |
|---|---:|---:|---:|---:|
| `far` | unbounded | 35 | 420 | ~1,680 |
| `approaching` | 14,400 s | 35 | 420 | ~1,680 |
| `near_event` | 6,300 s | 20 | 240 | ~960 |
| **`live_event`** | **900 s** | **3** — `900/300` | **36** | **~144** |
| `late_resolution` | unbounded | 35 | 420 | ~1,680 |

**Correction (2026-08-23):** an earlier revision of this note said four
`live_event` sessions yield "~12 blocks against ~140". That conflated
*time-intervals* with *market-blocks* — each qualifying interval yields up to
K=12 market-blocks. The corrected figures are **~144 against ~1,680**. The
**ratio** was right (~12×); the absolute counts were an order of magnitude low.
S01 bears the scale out: 377 market-blocks in one `late_resolution` session,
90% of the 420 ceiling. **That is geometry, not evidence.** It
says nothing about whether live-event markets are sparse, quiet or
uninformative — S01 already showed the analogous post-event stratum was dense,
with 24 of 24 markets clearing the activity floor.

The hazard is pooling: weighting every block equally and reading the result as
"the experiment's average regime" lets the wide bins dominate purely because
they contain more clock time.

**No weights are changed here.** This is documentation so the imbalance is
never mistaken for market behaviour.

### ⚠ The TTE-stratified view is NOT currently a preregistered cell

Checked rather than assumed, and it matters:

* **§7 freezes exactly twelve cells** — 3 comparisons × 4 horizons —
  Benjamini–Hochberg at FDR 10% "computed once, on the pre-declared set", and
  states plainly: *"No cell may be added after seeing results; a cell that
  looks interesting later starts a new preregistration with a new name."*
* **Amendment 2 §F** names **series** as strata/evaluation groups/covariates
  and clusters uncertainty at event/market level. It does not name TTE.
* **Amendment 2 §C** froze the TTE bins and observes they *allow* asking
  `E[r(t+h) | OFI(t), TTE(t)]` later — but never registers that as a cell.

So "M0 vs M1 by TTE stratum" is currently **an unregistered analysis**. Running
it after the tranche would violate §7; registering it now is legitimate but
multiplies the cell count (12 × 5 = 60) and materially changes the FDR
correction.

**This needs a decision before the tranche completes, not at analysis time** —
by then the only honest options are to skip the stratified view entirely or to
start a separate preregistration under a new name. Recorded here so the choice
is made with a clear head and no results in view.

### Session 02 — scheduling record, frozen before capture

Chosen by `kalshi_microstructure_schedule_anchor.py`, not by hand.

| field | value |
|---|---|
| `scheduled_target_bin` | `live_event` |
| `anchor_occurrence_datetime` | `2026-08-24T17:00:00Z` |
| `scheduled_session_start` | `2026-08-24T16:40:00Z` (occurrence − 20 min) |
| `session_seconds` | 10,800 |
| `selection_rule_version` | `capture-plan-addendum-1+2 / edge-amendment-4` |
| `candidate_count_at_freeze` | 24 (all 24 on the anchor slate) |
| `pool_size_at_freeze` | 509 markets, 23 distinct occurrence times |
| `feasible_anchors` | 23 — the earliest was taken |
| `projected_covering_intervals` | 3 (the `live_event` maximum) |
| `replacement_reason` | none |
| `frozen_at_utc` | `2026-08-24T15:55:46Z` |
| scheduler commit | `73a7017` |

The winner is `KXATPMATCH` — 23 anchors were feasible and the rule took the
earliest, with no reference to activity, price or any S01-derived information.
Launch is armed by a timer that waits for the frozen start and then runs the
session unchanged, so the start time cannot drift by however long a human takes
to notice the clock.

### ⚠ Series concentration is accumulating on the hard bins

| session | bin | series |
|---|---|---|
| S01 | `late_resolution` | `KXWTAMATCH` ×16, `KXATPMATCH` ×8 — tennis |
| S02 | `live_event` | `KXATPMATCH` ×24 — tennis |

Both hard-bin sessions are tennis, and that is not coincidence: the hard bins
need anchors whose *timing* qualifies, and tennis has many more distinct
occurrence times per day than baseball. The deterministic
earliest-feasible-anchor rule will therefore keep selecting tennis for
`live_event` and `late_resolution`.

This pulls against the frozen spread constraints — **≥6 of 8 series**, **no
series more than 4 primary sessions**, and **each bin across more than one
series where feasible**. If S03 and S04 also land on tennis, the tennis budget
is spent on two strata and the wide bins must carry all remaining series
diversity.

**Nothing is changed on this basis now.** Adjusting the scheduler to prefer a
different series would make it activity-adjacent and defeat the AST guard.
Recorded so the tension is visible while it is still cheap to address inside
the frozen rules — e.g. by taking the earliest feasible anchor *of the target
bin* on a day whose slate is not tennis-dominated, which is a scheduling input,
not a selection signal.
