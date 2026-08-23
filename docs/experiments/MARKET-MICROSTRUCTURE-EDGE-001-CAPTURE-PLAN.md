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
