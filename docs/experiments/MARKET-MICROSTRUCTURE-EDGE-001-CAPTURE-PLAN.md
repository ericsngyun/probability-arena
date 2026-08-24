# MARKET-MICROSTRUCTURE-EDGE-001 — prospective capture plan

**Status: PLAN ONLY. CAPTURE NOT STARTED. M0/M1 NOT RUN.**
Written 2026-08-22, after `MARKET-MICROSTRUCTURE-EDGE-001` Amendment 2 and
before any tape is collected for it.

Read-only. No capital, no orders, no venue writes beyond protocol-required
subscription/control messages.

**Why fresh data.** The six `PROD-ACTIVITY-PROFILE-001` windows were used to
design the universe rule, the concurrency ceiling, the eligibility gate and the
event-time conditioning. They are **design evidence and are burned for
confirmation** (Amendment 2 §G). This plan defines the *separate* prospective
corpus on which M0/M1 is evaluated.

---

## 1. The sampling rule, stated exactly

This is the whole rule. It is deterministic, uses only information available at
or before the decision instant, and contains **no alpha score**.

```text
ONCE PER SESSION, at session open:
  eligible_series := the 8 profiled series
  candidate_markets := open markets in eligible_series whose
                       occurrence_datetime falls inside the session's
                       event window        # NOT close_time (settlement)

EVERY 300 s (the re-selection tick), at tick time t:
  for each candidate market i:
      Activity(i,t) := N_orderbook(i, (t-300s, t]) / 300      # lagged, sequenced
      eligible(i,t) := Activity(i,t) >= 0.10 events/s
                       AND book generation at t is valid
                       AND no sequence fault recorded for i in (t-300s, t]
                       AND TTE(i,t) > max_horizon + embargo   (= 600 s)
  panel(t) := take at most K=12 from {i : eligible(i,t)},
              ranked by Activity(i,t) descending,
              ties broken by ticker lexicographic ascending
```

**Bootstrap tick.** At session open no lagged window exists. The first 300 s is
a **warm-up**: all candidates are subscribed, no rows are emitted, and the first
panel is selected at t = open + 300 s. Warm-up frames are archived and are
excluded from the research corpus by timestamp.

**Turnover is expected and is recorded.** Every re-selection writes the panel,
each market's `Activity(i,t)`, and the admit/drop delta. A market dropping out
mid-session is normal, not a fault.

**Forbidden inputs to this rule**, restated so they cannot creep in: `ticker`
frames, contracts/min or any volume proxy, open interest, any forward-looking
activity, and any model output. Order-book wire activity only.

## 2. Session shape and coverage

| parameter | value | why |
|---|---|---|
| session length | **10,800 s (3 h)**, bounded | spans multiple TTE bins in one continuous tape |
| anchoring | centred on scheduled `occurrence_datetime` of a real event cluster | Amendment 2 §C: event time is the fundamental variable, not clock time |
| K | **12** concurrent markets | Amendment 2 §D — capacity guard, never-exceed 24 |
| re-selection tick | 300 s | equals the eligibility lookback L |
| channels | `orderbook_delta`, `trade` (+ `ticker` archived, never used for eligibility) | §5 of the prereg |
| row cadence | 1 Hz per market in `panel(t)` | as the fabric spec defines |

**Declared coverage, fixed before capture starts.** The first tranche is
**20 sessions** spanning **≥ 14 calendar days**, and must include:

* **all five TTE bins** (`far`, `approaching`, `near_event`, `live_event`,
  `late_resolution`) with ≥ 3 sessions contributing rows to each;
* **≥ 6 of the 8 series**, so no single sport dominates;
* **≥ 4 weekend sessions**, since all six profile windows were weekdays.

Coverage shortfalls are reported, never back-filled by extending into whatever
happens to be convenient.

## 3. The capacity guard, and the censoring fix

The **3,500 f/s sliding-1s hard stop is unchanged and remains authoritative.**
A breach halts the session, writes `HALTED.json`, and no later session runs on a
breached configuration until K is reduced.

**The frame cap must never bind again.** `--max-events` defaults to `1_000_000`
(`scripts/kalshi_prod_capture_p4.py:1037`) and the profile runner never
overrode it, which is exactly what right-censored day 2 slot C at 1,472.3 s of
its 1,500 s budget. For this capture:

> `max_events > hard_stop_fps × max_seconds`
> = 3,500 × 10,800 = 37,800,000 → **set `--max-events 40_000_000`**

so the cap is **structurally unreachable**: every session must end on the clock
(`capped_time`) or on the capacity guard, and can never end on a silent frame
cap. This is not a collector retune — `DEFAULT_MAX_SEGMENT_RECORDS` stays at
13,000, per `PROD-ACTIVITY-PROFILE-001` Amendment 3 rule 3. It removes a
demonstrated measurement defect in the *harness*, which that rule explicitly
permits.

Each session records, as the profile did: `rotation_failures`,
`sequence_faults`, `frames_malformed`, `events_rejected`, `reconnects`,
`segments_committed`, per-segment `record_count` and `close_status`, and host
load before and after.

## 4. What a research row contains

Emitted at 1 Hz for each market in `panel(t)`, every feature window ending
**at or before** `t`:

* the 12 M0 state-only features (unchanged, per prereg §3)
* the M1 flow block, Δ ∈ {1 s, 5 s, 30 s}, computed from **receive timestamps**
  and labelled **temporally associated cross-stream flow** — never causal
  ordering (Amendment 2 §H)
* `TTE(t)` and its frozen bin
* `series`, `market_ticker`, `session_id`, `panel_rank`, `Activity(i,t)`
* labels `Δmid(t, t+h)` for **h ∈ {1 s, 5 s, 30 s, 300 s}**, 30 s primary

A row is emitted only if its market was in `panel(t)` at that tick **and** the
full label horizon lies inside the session. Rows whose 300 s label would run
past session end are not emitted — truncating a label is censoring, and this
plan exists partly because censoring was not anticipated last time.

## 5. Effective sample size, computed before capture rather than after

Per session, at best: 12 markets × 10,800 s = **129,600 rows at 1 Hz**. That
number is deliberately *not* the sample size. At the 30 s primary horizon with a
≥ 300 s block, the quasi-independent unit is the 300 s market-block:

> 12 markets × (10,800 / 300) = **432 market-blocks per session**, best case

and eligibility turnover will reduce it. Across 20 sessions: **≤ 8,640
market-blocks** and **≤ 240 distinct (market, session) clusters** — markets do
not persist across sessions (L23), so each session contributes fresh clusters.

**Preregistered minimum for the tranche to be evaluable:**

* **≥ 4,000 quasi-independent 300 s market-blocks**, and
* **≥ 150 distinct (market, session) clusters**, and
* **≥ 3 sessions contributing rows in every TTE bin**.

If the realised corpus misses any of these, the experiment reports
**`UNDERPOWERED`** and either extends capture under this same rule or stops. It
may **not** proceed by loosening eligibility, widening K, or pooling the profile
windows back in. The realised counts are reported in place of the retired
"≈360,000 rows / ≈12,000 blocks" figure, which may not be quoted.

## 6. Order of operations, and what stops it

1. Eric approves this plan. **Nothing starts before that.**
2. A capture runner is built and validated end-to-end on a short session, the
   way the profile runner was — its defects are reported before real capture.
3. Tranche capture runs prospectively under §1–§3, one session at a time, each
   with an immutable archive root.
4. Coverage and §5 counts are reported. **Stop and review.**
5. Only then is M0/M1 fitted, under prereg §3–§8 unchanged.

**No M0/M1 fit, no imbalance inspection, and no alpha quantity of any kind may
be computed before step 5.** Amendment 2 exists because the ordering was kept
last time; it is kept again here.

**Binding, from prereg §8:** if M1 does not beat M0 out-of-sample at FDR 10% on
the 30 s primary horizon, order flow is declared non-additive over static book
state and this lane stops. No Hawkes, no transformer, no additional features.
Economics stay strictly downstream of `Loss(M1) < Loss(M0)`.

---

## Addendum 1 — 2026-08-23: the candidate-set rule the plan was missing

Found by `MARKET-MICROSTRUCTURE-CAPTURE-RUNNER-VALIDATION-001` on its first
attempt to build a real candidate set, before any tranche capture. Recorded
rather than patched silently.

**The hole.** §1 says `candidate_markets := open markets in eligible_series
whose occurrence_datetime falls inside the session's event window`, and §2 caps
concurrency at 24. It never said **which 24** when the window yields more. It
does: a live 5-hour window on 2026-08-23 returned **88 candidates**.

**Why the obvious fix is wrong.** Ticker-lexicographic truncation is
deterministic and unsteerable, so it looks safe. Applied live it selected 24
`KXATPMATCH` markets with events **17–26 hours away** — markets that would sit
below the activity floor for the whole session. The panel would never rotate,
and the session would validate nothing. Alphabetical order is uncorrelated with
the event cluster the session is supposed to be anchored to.

**The rule, frozen here:**

```text
candidate_set := markets in eligible_series whose occurrence_datetime lies in
                 [session_open + 1800 s, session_open + 5 h]
              ordered by (occurrence_datetime ASC, ticker ASC)
              truncated to the concurrency ceiling (24)
```

The lower edge guarantees `TTE > 600 s` at the final decision tick of a session
of the planned length, so no label can be truncated. Ordering by event time
ascending makes the subscribed set an actual **event cluster** — which is what
"event-anchored session" was always supposed to mean — and ticker ascending
breaks ties.

**This selects on no activity signal.** `occurrence_datetime` is a scheduled
calendar fact known before the session opens and is not a measure of activity,
volume, liquidity or expected return. It cannot be steered by anything the
venue does during the session. The activity-based rule remains confined to the
K=12 research panel, exactly as Amendment 2 §B and §E freeze it.

**Subscriptions vs the research panel, stated explicitly** — the plan implied
this but never said it, and the runner had to resolve it:

* **subscribed set** = the capacity-guarded quantity, ≤ 24, **fixed for the
  session**. This is what the 3,500 f/s stop protects.
* **research panel** = K = 12, **rotating every 300 s** by lagged order-book
  activity among the subscribed set.

The split is what makes rotation possible at all: a market that was never
subscribed has no lagged order-book activity to rank, so the panel can only
rotate within what is already being observed. It also means **the collector is
never modified** and no mid-session resubscription occurs.

---

## Addendum 2 — 2026-08-23: the tranche schedule, frozen

Supersedes §2's "20 sessions spanning ≥14 days" coverage sketch with a
mechanical plan. **Blocked on one decision** — see
[`TRANCHE-SCHEDULE-BLOCKER.md`](TRANCHE-SCHEDULE-BLOCKER.md): two of the five
TTE bins are unreachable under the frozen `TTE > 600 s` eligibility gate.
Everything below is frozen and correct either way; only the bin *allocation*
depends on that decision.

### What "a session covers bin *b*" means

Preregistered here so a session that grazes a bin for one second cannot count:

> A session counts toward TTE bin **b** only if **at least one complete 300 s
> research panel interval, after warmup, lies wholly within b**.

This reuses the existing 300 s decision block rather than inventing another
duration. The power gate is then `N_sessions,b ≥ 3` for every bin, under
exactly this definition.

### Allocation

| primary TTE anchor | sessions |
|---|---:|
| `far` | 4 |
| `approaching` | 4 |
| `near_event` | 4 |
| `live_event` | 4 |
| `late_resolution` | 4 |
| **total** | **20** |

One session of redundancy per bin over the ≥3 floor. A 3 h session naturally
contributes to several bins; that is fine. Each session has **one predeclared
primary target used only for scheduling** — never for deciding which
observations are retained.

**`live_event` and `late_resolution` are scheduled FIRST.** `far`,
`approaching` and `near_event` are obtainable simply by starting earlier. The
in-play bins depend on the venue keeping markets open, on exact event timing,
and on whether useful book activity continues at all — none of which we have
evidence for. If a bin proves structurally impossible for a series, that must
be discovered at session 2, not session 18.

### The scheduling rule

```text
eligible event/session candidate
  -> compute session start from occurrence_datetime and the frozen bin edges
  -> choose the start that places a complete post-warmup 300 s panel interval
     inside the assigned bin
  -> freeze event and start time BEFORE capture
```

**The scheduling variable is event time and TTE coverage. Nothing else.**
Explicitly not: current price movement, spread, order flow, social or news
signal, preliminary alpha, or "this game looks active."

### Deterministic replacement

> If a scheduled event is unavailable, or its markets are closed before capture
> begins, take the next eligible event with the **same primary bin target**,
> ordered `(occurrence_datetime ASC, ticker ASC)`.

No substituting a more interesting game. The replacement is recorded with the
reason.

### Series spread

Beyond §2's ≥6-of-8 floor, operationally:

* target all **8** series where feasible;
* **no series may hold more than 4** primary sessions;
* **≥6 series represented before session 15**;
* **≥4 weekend sessions** (all profile and validation sessions were weekdays);
* each TTE bin spread across **more than one series** where feasible.

This is what stops "M1 works" from quietly meaning "one sport dominated one TTE
regime." Series is a sampling stratum, never an alpha-selection variable.

### Blind-capture discipline

During the tranche, inspect only: process health, the safety gate, sequence
integrity, archive conservation, row counts, schema/version correctness,
power/coverage progress, and per-bin session counts.

Do **not** inspect: feature/return correlations, M0 or M1 coefficients, feature
importance, directional markouts, "interesting" markets, or preliminary loss
differences.

**The only adaptive action permitted is scheduling future sessions to satisfy
already-preregistered coverage constraints** — never to improve a result.

---

## Addendum 3 — 2026-08-24: coverage deficit → replacement session

**Frozen before any target-bin failure had occurred.** After S02 the tranche
has **18 bin obligations across 18 remaining sessions** — zero slack. One
mechanically valid session that fails to earn its target bin would create a
coverage shortfall with nowhere to absorb it.

### The rule

A session that is **operationally valid but fails its target-bin coverage**:

1. **remains in the corpus.** Its rows passed L1–L3; they are good rows and
   discarding them would trade real data for tidy bookkeeping.
2. **is never relabelled** to another bin, however many complete intervals it
   happened to land there. The session was scheduled against one obligation and
   is scored against that one.
3. **leaves its obligation outstanding**, discharged by a **deterministic
   replacement session for the same bin**, appended after the planned tranche
   as **S21+**, chosen by the same frozen coverage and anchor schedulers.
4. **never takes quota from another bin**, never widens a TTE definition, and
   never retroactively retargets a completed session.

**"20 sessions" remains the planned tranche; the experiment may require more
sessions purely to satisfy already-frozen coverage floors.**

### Why this changes no hypothesis

The cells, horizons, comparisons, family size, FDR level, embargo, clustering
and §8 verdicts are all untouched. This addendum governs **how many sessions
are collected**, not what is computed from them. The power floors (≥4,000
market-blocks, ≥150 clusters, ≥3 sessions per TTE bin) are unchanged and are
what the replacement sessions exist to satisfy.

`PLANNED_SESSIONS = 20` is now explicitly **not a cap** in `coverage.py`, and
`SessionRecord` carries **two independent facts** rather than one merged flag:

| field | meaning |
|---|---|
| `operationally_clean` | L1–L3 passed → the rows belong in the corpus |
| `counted` | L4 earned the target bin → the obligation is discharged |

Merging them would have forced a choice between discarding good rows and faking
coverage. `coverage_deficit()` reports outstanding obligations, sessions that
were clean but missed their bin, and how many appended sessions the frozen
floors still require; `replacement_obligations()` lists them in the frozen
hard-bins-first order.
