# PROD-ACTIVITY-PROFILE-001 — preregistration

**Status: PREREGISTERED, NOT RUN.** Written 2026-08-19 against the amended
KALSHI-TAPE-MEASUREMENT-CONTRACT-001 (§16, L20–L22).

Read-only observation. No capital, no orders, no portfolio channels, no venue
writes except protocol-required subscription/control messages.

---

## 1. Why this exists, and why it must be preregistered

`MARKET-MICROSTRUCTURE-EDGE-001` needs a **universe**: a set of markets to
subscribe to and study. Choosing that set is not a neutral engineering step — it
is the single largest silent-overfitting surface in the whole lane.

If we pick markets *after* seeing which ones produced attractive results, every
downstream p-value is contaminated and no amount of later rigour repairs it.
EDGE-DISCOVERY-001 already cost us four failed experiments; the one thing that
survived scrutiny there was that the preregistration held. So the selection rule
is frozen **here**, before any edge measurement exists to be tempted by.

This experiment therefore answers exactly two questions and deliberately
refuses to answer a third:

* **Q1 — capacity.** How many markets can we subscribe to before the collector's
  measured limits bind?
* **Q2 — availability.** How many markets carry enough order-book activity to
  support microstructure inference at all, and how does that change with time of
  day?
* **NOT asked: which markets are profitable.** Nothing in this experiment may
  read a price direction, a return, or an outcome. It measures message activity
  only. (Doctrine 1: no signal graduates because it looks predictive.)

## 2. What we already know, and its limits

From the single P4 production window (2026-08-20T00:37–00:47Z, 84,170 records,
12 markets, one connection generation):

| quantity | measured |
|---|---|
| orderbook frames | 79,256 (94.15% of the tape) |
| mean rate, all channels | 140 f/s |
| **peak rate, sliding 1 s** | **612 f/s** (§16.2.2) |
| mean orderbook rate per market | 11.0 f/s |
| activity concentration | top market 19,741 frames; bottom 34 |
| orderbook `seq` integrity | 1 … 79,256, contiguous, 0 gaps, 0 faults |

**L20 binds and is the reason this experiment exists:** that is ONE ten-minute
overnight window. It is not a peak-capacity estimate and it is not a
time-of-day profile. Every figure above is a single draw.

## 3. Frozen design

**Windows.** Six capture windows of **25 minutes** each, at fixed local-venue
times chosen to span the venue's daily structure, run on **two separate
weekdays** (3 windows per day) so that day-effects and time-effects are not
confounded:

| slot | window (ET) | rationale |
|---|---|---|
| A | 10:00–10:25 | US morning, equities open, low sports |
| B | 14:00–14:25 | midday |
| C | 20:00–20:25 | US evening sports peak — the P4 window's neighbourhood |

Two days × three slots = **six captures, ~150 minutes total**. Fixed in advance;
no window may be re-run because its result was unattractive. A window lost to
infrastructure failure may be re-run **at the same slot on a later day**, and the
loss must be reported.

**Universe under observation.** Each capture subscribes the order-book channel to
a **fixed 40-market universe** selected by the rule in §4, plus the global
`ticker` channel for discovery (§5).

40 is chosen from §2: 40 × 51 f/s worst-case-uniform ≈ 2,040 f/s, a **3.4×**
margin under the ~6,900 f/s closer ceiling. See §6 for the hard guard.

**Capture tooling.** The P4 capture path, with the peak statistic computed as a
**sliding 1-second window** (§16.2.2). Fixed-bucket peaks understate by ~21% and
must not be used for any capacity claim in this experiment.

## 4. The selection rule, frozen before any edge measurement

A market enters the `MARKET-MICROSTRUCTURE-EDGE-001` universe if and only if,
**pooled across all six windows**:

1. it produced **≥ 1 order-book snapshot and ≥ 500 order-book deltas**; and
2. it was **publishable for ≥ 95%** of the wall-clock time it was subscribed
   (using the typed `PublicationState`, so "awaiting snapshot" is not counted as
   a fault); and
3. its order-book sid showed **zero unexplained sequence gaps**; and
4. it was **open for the entire window** — no market that closed mid-window
   qualifies, because closure changes the process being measured.

Markets are then ranked by total delta count and the **top N** taken, N set by
the capacity rule in §6. Ties broken by ticker lexicographic order — an
arbitrary but *prespecified* rule, so it cannot be steered.

**This rule is frozen on merge of this document.** Changing any threshold after
seeing edge results invalidates the edge experiment and must be declared as a
new preregistration with a new name.

## 5. `ticker` is a discovery heuristic ONLY

The global `ticker` channel is subscribed to cheaply enumerate which markets are
moving at all, so the 40-market universe is not chosen from prior belief.

**It may not be used for anything else.** §16.4 measured on production that
`ticker` frames carry `seq = null` — all 2,395 of them, without exception. An
unsequenced channel cannot support gap detection, cannot support ordering
guarantees, and cannot be replayed deterministically. Any use of `ticker` beyond
"this ticker symbol showed signs of life" is out of contract.

Concretely: ticker output may produce a **candidate list**. The four criteria in
§4 are then evaluated **exclusively from order-book evidence**.

## 6. Capacity guard (hard, pre-committed)

Uniform scaling from §2's per-market peak against the ~6,900 f/s closer ceiling:

| universe | projected peak | headroom |
|---:|---:|---:|
| 25 | 1,275 f/s | 5.4× |
| **40** | **2,040 f/s** | **3.4×** |
| 100 | 5,100 f/s | 1.4× |
| ~135 | ~6,900 f/s | **1.0× — breach** |

Two caveats, stated because a one-sided error here is how capacity claims go
wrong. Uniform scaling is **pessimistic** in that activity is heavily
concentrated (top market 581× the bottom), so real markets 13–40 will cost far
less than 51 f/s each. It is **optimistic** in that it extrapolates from a
12-market sample of unknown representativeness, in a single overnight window.

**Guard.** If any capture's measured sliding-1s peak exceeds **3,500 f/s**
(≈ 2× the projection, ≈ 0.5× the ceiling), the capture is stopped, the universe
is halved, and the whole six-window set is restarted. This is a stopping rule,
not a judgement call.

## 7. Noise floor and positive control

**Noise floor (doctrine 4).** Every rate reported carries the **between-window
spread across the six captures**, not a point estimate. A time-of-day difference
is only reported as real if the slot-to-slot difference exceeds the day-to-day
difference within the same slot. With 2 days × 3 slots we can separate these but
cannot estimate an interaction — **stated in advance** so the writeup does not
later pretend otherwise.

**Positive control (doctrine 7).** A market known to be inactive — one selected
from the *bottom* of the ticker discovery list — is included in every capture as
a 41st subscription. If the pipeline reports it as passing the §4 criteria, the
measurement is broken and the run is void. This is the anti-vacuity arm: it
proves the criteria can fail.

**Negative control on the ceiling:** the §6 guard has never fired in any capture
we have run. If it never fires here either, that is consistent with the ceiling
being far away, not evidence that the guard works. The guard's own correctness is
tested offline against synthetic rates, not by hoping production trips it.

## 8. What this experiment cannot conclude

* Nothing about **profitability, edge, or predictability**. It reads no prices
  as signals.
* Nothing about **peak capacity beyond 40 markets** — projections in §6 are
  projections, and stay labelled as such.
* Nothing about **weekend or holiday** structure; the design is weekdays only.
* Nothing about **sub-second burst structure** beyond the sliding-1s peak.

## 9. Deliverable

A frozen `universe.json` — the ranked, criteria-passing market list with every
input count beside it — plus the six per-window rate profiles with their spread.
That file is the sole input `MARKET-MICROSTRUCTURE-EDGE-001` is permitted to
take from this experiment.
