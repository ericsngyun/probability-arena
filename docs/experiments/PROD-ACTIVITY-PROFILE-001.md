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

---

## Amendment 1 — 2026-08-19, BEFORE ANY DATA WAS COLLECTED

**Provenance, stated first because it is what makes this amendment legitimate.**
This amendment rests on exactly two things: the **frozen P4 production tape**
(captured 2026-08-20, unchanged since) and a **demonstrated defect in the peak
estimator**. It uses **no data from PROD-ACTIVITY-PROFILE-001**, because none
exists — not one window has been captured. Nothing here was chosen after seeing
a result this experiment produced.

**The metric basis is now explicit.** The capture tooling emits two figures, and
the guard in §6 is evaluated against the first:

| field | role |
|---|---|
| `peak_1s_sliding` | **PRIMARY capacity metric.** R_max,1s = max_t #{frames : τ_i ∈ [t, t+1s)} |
| `peak_1s_calendar_bucket` | secondary diagnostic, retained for comparability with earlier evidence |

The previous field name `frames_per_second_peak_1s` is **removed rather than
repointed**. Repointing it would silently change what every historical evidence
file's field means; leaving it would keep shipping the biased statistic. A stale
reader now gets a loud `KeyError`.

**The estimator defect is worse than "biased low" — it is phase-dependent.**
Measured on the same 84,170 records:

| estimator | value |
|---|---:|
| calendar bucket, monotonic-clock alignment (what the tooling shipped) | **485** |
| calendar bucket, wall-clock alignment | **565** |
| **sliding window — no free parameter** | **612** |

Two *equally legitimate* bucket alignments of one dataset disagree by 17%. A
statistic whose value depends on an arbitrary phase offset cannot bound
anything. The sliding window has no such parameter.

`tests/test_kalshi_p4_3_peak_estimator_001.py` pins the reason permanently with
a synthetic burst of 800 frames straddling a second boundary — 400 in the last
500 ms of second N, 400 in the first 500 ms of N+1. Sliding reports ~800; fixed
buckets report two quiet ~400 f/s seconds, and one test asserts the exact
consequence: **a 500 f/s guard reads as cleared by traffic that breached it.**

**No threshold in this preregistration changes.** §3 and §6 already specified a
sliding-1s window, so the guard remains **3,500 f/s** and the universe remains
**40 markets**. This amendment makes the basis explicit and names the fields; it
does not move a decision boundary. That is recorded plainly so a later reader
cannot mistake it for a threshold that was quietly relaxed.

**The 500 f/s design prior is RETIRED.** It was exceeded by 22% in the first
ten-minute production window ever captured, and that window was not selected as
a known venue peak. It may no longer be cited as a sizing assumption anywhere.

**`DEFAULT_MAX_SEGMENT_RECORDS` is deliberately NOT changed.** The observed
612 f/s peak still sits roughly 11× under the ~6,900 f/s closer ceiling and no
capacity defect has been demonstrated. The correct order is: change the
measurement basis (done), collect the six windows, then decide whether the
segment constant needs tuning. Tuning it now would be a change made on one
overnight window — the exact error that produced the retired prior.

---

## Amendment 2 — 2026-08-20, BEFORE ANY WINDOW WAS CAPTURED

**A circularity in this document, found on preflight.** §3 said the observation
universe is "selected by the rule in §4", but §4's rule is evaluated **pooled
across all six windows** — it is the *output* rule and needs the data this
experiment has not yet collected. The preregistration therefore never defined
what to subscribe to *during* the profile. This amendment fills that hole. It
changes **no threshold, no window count, and no §4 criterion.**

**Measured facts that forced the resolution** (read-only `GET /markets`, public
route, no credential, telemetry-blind):

* Every sampled market from the P4 tape — `KXATPMATCH-26AUG19BORNAK-BOR`,
  `KXMLBGAME-26AUG191805MIAPHI-PHI`, `KXMLBGAME-26AUG191835NYYBAL-BAL`,
  `KXMLBTOTAL-26AUG191840SFCLE-4` — was **already NOT OPEN** ~6 h after capture.
  Same-day sports markets settle same day.
* Markets *are* listed in advance: `KXMLBGAME` had 18 open closing 26AUG20, 30
  closing 26AUG21, 30 closing 26AUG22; `KXNFLGAME` out to 26SEP13.

So a literally "fixed 40-market universe" across two weekdays is satisfiable
**only** with future-dated markets — which are open but essentially silent a day
before their event. Three of the six windows would then capture near-nothing,
and the time-of-day comparison would in fact be measuring **time-to-event**.
That answers Q2 with a confound rather than a measurement.

**RESOLUTION — the observation universe is frozen PER DAY.**

| | |
|---|---|
| day 1 | 40 markets whose `close_time` falls on profile day 1 |
| day 2 | 40 markets whose `close_time` falls on profile day 2 |
| market-level rank stability | measured **within day**, across that day's 3 slots |
| cross-day stability | measured at **series** level (`KXMLBGAME`, `KXATPMATCH`, …) |

Both days therefore carry real traffic, and time-of-day is measured rather than
entangled with time-to-event. The cost is stated plainly: **no market-level rank
statistic spans the two days**, because no market does.

**THE SELECTION RULE, written before the query that uses it runs.** §5 already
authorises `ticker` as a candidate-discovery heuristic and nothing more; this is
that, and only that.

1. **Candidate population** — every market the venue reports `status=open` whose
   `close_time` falls within profile day *d* (ET). Enumerated from REST. No
   volume, open-interest, liquidity or top-of-book field is read.
2. **Discovery** — one **5-minute global `ticker` pass** before that day's first
   window. A market is a candidate if it emitted **≥ 1 ticker frame**.
3. **Selection** — the **40** candidates with the highest ticker-frame count,
   ties broken by **ticker ascending**.
4. **Positive control (§7)** — a **41st** market closing the same day that
   emitted **zero** ticker frames during discovery, first by ticker ascending.
   It must **FAIL** §4's criteria or the run is void.

This selects the *observation* universe only. **§4's output rule is untouched**
and still governs which markets may enter `MARKET-MICROSTRUCTURE-EDGE-001`, and
its criteria are still evaluated **exclusively from order-book evidence**.

**Channels.** Each window subscribes `orderbook_delta`, `trade` and `ticker`.
`trade` is required because §2 of the deliverable asks for per-market trade
frames and `MARKET-STATE-FABRIC-v1`'s M1 block reads that channel; on the P4
tape it added 2,516 frames against 79,256 order-book frames (~3%), so its
capacity cost is immaterial. **Order-book and trade are the channels the first
experiment leans on; `ticker` remains discovery and context only**, because it
is unsequenced and its completeness is unknowable.

**The 3,500 f/s hard stop is evaluated per window, immediately on completion,
before the next window is permitted to start.** It is deliberately **not**
in-flight: an in-flight rate governor would mean editing the collector, and the
collector is frozen. A breach halts the set exactly as §6 requires — no later
window runs on a breached configuration. `peak_1s_sliding` is the sole statistic
the gate reads; `peak_1s_calendar_bucket` is recorded as a diagnostic and gates
nothing.

**Host load is recorded with every window** (load average, CPU count, memory,
disk) so venue intensity can be told apart from EVO contention. A rate figure
without it cannot distinguish a quiet venue from a busy host.

---

## Amendment 3 — the ANALYSIS stage, preregistered 2026-08-20 before any window data exists

Written while the capture is running and **before a single window has been
read**. That ordering is the point: specifying the statistics now costs nothing
and removes the possibility of choosing them once their answers are visible.

**Nothing here alters the running capture.** No threshold, universe rule,
channel, schedule or gate is touched. This section is inert until the analysis
gate opens.

### Standing rules while the six windows are live

1. **No intermediate universe optimisation.** Day 2's criteria are not changed
   because day 1 looked surprising.
2. **No preliminary alpha analysis.** Not even a glance at imbalance — a
   hypothesis designed after an informal look is no longer preregistered.
3. **No capacity retuning from one slot.** `DEFAULT_MAX_SEGMENT_RECORDS` stays
   at 13,000 unless the §6 gate fires or the *completed* profile demonstrates a
   real operational defect.
4. **Invalid windows are preserved, never silently replaced.** A semantic
   failure is part of the profile. §3 permits re-running a window lost to
   *infrastructure* failure at the same slot on a later day, and requires the
   loss to be reported; nothing else may be substituted.

### The analysis gate

Analysis begins only when **all six windows have completed**, or when the set
has **halted** under §6. A partial set is reported as partial.

### A — is production capacity healthy enough to leave the collector frozen?

Mechanical, from the six window artifacts:

* `max(peak_1s_sliding)` across all six, against the **3,500 f/s** stop and the
  ~**6,900 f/s** closer envelope.
* segment rotations per window, and records per segment against the 13,000
  constant.
* close latency and append behaviour; `rotation_failures`; `segments_committed`.
* host load (`loadavg`, memory, disk) beside each rate, so venue intensity is
  distinguishable from EVO contention.

**Default is FROZEN.** Unfreezing collector engineering requires a demonstrated
operational defect, not a large number.

### B — the smallest stable panel with enough real sequenced traffic

Descriptive only at this stage. **The universe-selection rule for the first
panel is NOT defined here** — it is preregistered separately once these
measurements exist, so that its thresholds are set against a known distribution
rather than guessed.

Per market, per window: order-book frames, trade frames, ticker frames,
sequenced order-book rate, share of venue traffic, and window coverage.

**Rank stability by channel** — the statistic that decides whether a stable
"high microstructure activity" market exists at all, or whether activity is
regime- and event-dependent:

> ρ( OB activity in window *i* , OB activity in window *j* )

computed as **Spearman rank correlation** over every window pair, and
**separately for trade activity**, because the two channels may not agree and
the first experiment leans on both.

* **within day** — market-level, across that day's three slots (3 pairs/day).
* **across days** — **series** level only, because market identity does not
  persist (L23).

Reported beside it, since a correlation alone cannot distinguish them:
**persistence vs burstiness** — for each market, the median per-window frame
count against its maximum, and the number of windows in which it cleared the §4
activity floor. A market producing 20,000 frames in one window and nothing in
five is explicitly *less* useful to the first experiment than one producing
3,000–5,000 in all six, and the report must make that visible rather than
ranking on totals.

**§7 positive control:** the known-inactive market must **FAIL** §4's criteria.
If it passes, the measurement is broken and the run is void.

### C — does `MARKET-MICROSTRUCTURE-EDGE-001` need amendment?

Checked **only** against assumptions already embedded in that document, and
**only** against production facts — never against an edge result, which does not
exist yet and must not exist when this check is made.

| embedded assumption | what the profile checks |
|---|---|
| a 40-market universe clears §4's ≥1 snapshot + ≥500 deltas | how many markets actually clear it |
| ≈360,000 rows at 1 Hz ⇒ ≈12,000 quasi-independent 30 s blocks | realised publishable-second count |
| trade features need a lag on the order of the 580 ms max interarrival | measured cross-SID interarrival |
| 30 s primary horizon carries measurable mid movement | realised mid-change distribution |
| cost floor ≈ half-spread + fees is meaningful at these spreads | realised spread distribution |
| 40 markets sit ~3.4× under the stop | realised peak against the projection |

If every assumption survives, **run the preregistration unchanged.** If one is
objectively wrong, **amend it, document why, and only then run** — with the
amendment timestamped before any M0/M1 output is produced.
