# KALSHI-DEMO-TRAFFIC-CAPACITY-001 — preregistration

**Status: PREREGISTERED. Committed BEFORE the probe runs.** The decision rule
and the market pool are frozen here; neither may be revised after seeing rates.

**Read-only. No orders, no portfolio channels, no venue writes, no capital, no
archive qualification run.**

---

## 1. The one question

**Is `100,000 frames in 4 hours` realistically attainable on DEMO?**

This determines whether CP6–CP9 *as currently parameterized* are feasible at
all, so it runs before any further manifest work. `KALSHI-TAPE-MANIFEST-001`
refused on strata separation and could not settle this: its
`top_of_book_change_rate` **saturates at a 150 s interval** (433 of 582 markets
changed at every read), so it bounds message rate only **from below**, uselessly.

## 2. Scope — deliberately narrow

- read-only
- **no archive qualification run**
- no orders
- **fixed small market pool, chosen BEFORE probing**
- **2–5 second** observation cadence
- direct measurement of **actual message/frame arrival**, not the saturated
  150 s proxy
- estimate expected frames over 4 hours
- **report uncertainty, not just a point estimate**
- **stop after answering the question**

## 3. The decision rule — FROZEN

$$N_{4h} = 14{,}400 \sum_i \hat\lambda_i$$

where $\hat\lambda_i$ is the measured frame arrival rate (frames/second) for
market $i$ in the pool, and 14,400 is 4 hours in seconds.

| verdict | condition |
|---|---|
| **REACHABLE** | the **conservative lower bound** is **≥ 125,000** |
| **BORDERLINE** | the **point estimate** clears 100,000 but the lower bound does not |
| **UNREACHABLE** | the **point estimate itself** is below 100,000 |

**The 125,000 threshold is deliberate**, not a rounding of 100,000: it prevents
calling a projected 102k session viable and then spending four hours
discovering that variance pushed it under.

The lower bound must be a stated interval on the *sum*, with its method named
(the rate is a count process; the interval must reflect that, and per-market
rates are not independent within an event).

## 4. The pool — frozen before probing

Chosen to include **both observed regimes**, per
`KALSHI-TAPE-MANIFEST-001-FINDING.md`:

- **all four genuinely high-activity markets** (≥ 1,000 c/min; ranks 1–4)
- **several markets from the plateau** (the 15–17 c/min band holding 30.9% of
  the eligible set)

**The pool must NOT be optimized after seeing rates.** If a chosen market turns
out to be quiet, it stays in and its rate is reported.

Venue test instruments (`KXMAXSHARDINGTEST`, `KXTESTMATCH`) are **excluded**,
per the amendment recorded in
`KALSHI-TAPE-MANIFEST-001-AMENDMENT-TEST-INSTRUMENTS.md`.

## 5. Field-semantics verification — required before measuring

Per doctrine 8, and because this milestone exists partly *because* a field
name lied: **whatever field or event is counted as a "frame" must first be
verified empirically** — re-read across a known interval, observe what moves it,
record the result. `updated_time` looked authoritative and was a definition
timestamp. Do not assume; measure.

## 6. Honesty requirements

- If the answer is **UNREACHABLE**, say so. That is a finding about DEMO and it
  reshapes the qualification design — it is not a failure of the probe.
- If the pool's rates are too variable to bound usefully, **report the interval
  and say the question is unsettled**, rather than presenting a point estimate
  as an answer.
- Record what could not be determined.
- A DEMO rate is **not** a production rate. Nothing here licenses a claim about
  production capacity.

## 7. Deviations

Any departure must be recorded here with reason and timestamp **before** the
affected result is reported.
