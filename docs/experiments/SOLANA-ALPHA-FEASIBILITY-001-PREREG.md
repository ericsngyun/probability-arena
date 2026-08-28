# SOLANA-ALPHA-FEASIBILITY-001 — preregistration

**Status: FROZEN, NOT RUN.** Written 2026-08-27, before any live social count
exists. Depends on `SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001` passing first.

The Solana counterpart of `PROD-ACTIVITY-PROFILE-001`: measure whether the
lane can support an experiment **before** designing one.

---

## 1. The question

> Over a preregistered prospective window, how many trustworthy, correctly
> attributed, clock-valid social events does this system actually produce?

**No forward returns are computed.** Not once, not for a sanity check, not "just
to see". The Kalshi profile established the ordering that makes a later
preregistration honest, and the same ordering applies here.

## 2. Measured quantities, frozen now

| quantity | why it matters |
|---|---|
| joinable events **per day** | sets the window length a later experiment needs |
| **unique canonical mints** | events on one token are not independent |
| **distinct authoritative sources** | one source is a single point of failure, not a corpus |
| duplication / propagation structure | N artifacts about one event is **one** event |
| `L_delivery` distribution | contaminated; how late information reaches us |
| `L_pipeline` distribution | ours; how slowly we react |
| chain-observation availability | fraction with usable on-chain state |
| quote-observation availability | fraction with a usable quote |

`L_delivery` and `L_pipeline` are reported separately and **never summed**.
They answer different questions, and only the second is a controlled
measurement.

## 3. The collapse rule, frozen before the data

**Multiple artifacts about the same canonical mint within a window are ONE
information event.** Counting them separately would inflate the corpus with
retweets and coordinated reposts, and inflate significance later.

The frozen unit is `(canonical_mint, first_joinable_receipt)`; subsequent
artifacts on the same mint inside the window are recorded as **propagation**
against that event, not as new events. The window is declared before the run.

## 4. What the answer changes

This is a **design input**, not a finding about markets:

> 50,000 received → 12 joinable, and 5,000 received → 800 joinable, are
> different projects.

* **Few events** — the bottleneck named by the largest funnel loss is the next
  milestone. If it is authority, we need better identity data; if it is chain
  observation, the RPC layer; if it is delivery, better social infrastructure
  may be worth paying for. A 12-event corpus does not get an alpha experiment
  designed around it.
* **Many events** — `SOCIAL-LEAD-LAG-001` becomes designable, with a window
  length and clustering structure chosen from measured counts rather than hope.

## 5. Standing prohibitions during the window

* no forward return, markout or price response is computed or inspected;
* no source is added, removed or reweighted on the basis of what it produced;
* the source universe and cost envelope stay as frozen — a mid-run change makes
  the counts uninterpretable;
* no model is fitted to anything.

**The only adaptive action permitted is stopping**, under the predeclared cost
or safety rule.

## 6. What would falsify the measurement itself

* an event count that changes when the same window is recomputed;
* a joinable event whose canonical mint cannot be traced to its evidence hashes;
* propagation collapsed on ticker or name rather than canonical mint — the
  identity stack exists precisely because a symbol is not an identity;
* any latency figure pooling LIVE and BACKFILL;
* a funnel stage whose count is non-monotone against the stage above it.

## 7. Only then

`SOCIAL-LEAD-LAG-001` is preregistered afterwards, with:

> M₀ = f(price, liquidity, wallets, order flow, concentration)
> M₁ = M₀ + f(verified social event, novelty, source type, propagation)

at frozen horizons, with the social event tied to the **correct token** by the
identity stack and timed on a **trustworthy clock** by the seam. Those two
guarantees are the whole reason this sequence was built in this order.
