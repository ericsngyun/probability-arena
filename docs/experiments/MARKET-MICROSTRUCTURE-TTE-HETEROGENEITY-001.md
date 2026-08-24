# MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001 — secondary preregistration

**Status: REGISTERED, NOT RUN.**

**Timing, stated precisely rather than flatteringly.** Registered **after
confirmation capture began** — session S01 (`late_resolution`, 2026-08-24) is
complete — but **before any confirmation outcome analysis and before any
feature/label inspection of any kind**. This is **not** "preregistered before
data collection," and must never be described as such. What it is: registered
while every outcome remains unseen. The tranche ledger records the blind-capture
discipline that makes that claim checkable.

Read-only. No capital, no orders. Measures information, not profit.

---

## 1. Subordinate by construction

**This analysis is not part of the primary `MARKET-MICROSTRUCTURE-EDGE-001`
decision rule and cannot rescue a failed primary result.**

If EDGE-001 concludes that M1 does not beat M0, that conclusion **stands**. It
is then forbidden to say "but `live_event` was positive" and treat the original
hypothesis as having succeeded. A positive finding here generates a **new
prospective hypothesis**; it never retro-fits the old one.

`MARKET-MICROSTRUCTURE-EDGE-001` is untouched: **12 primary cells**, its own
Benjamini–Hochberg at FDR 10%, and the §8 stopping rule remain the sole primary
stop/go authority. This document adds **no** cell to that family.

## 2. The question

> Is the incremental value of order flow **regime-dependent**?

Not "which of sixty cells is significant." Sixty cellwise tests would dilute the
question, and several of them would be predictably weak for purely geometric
reasons (§6).

## 3. The statistic

Let, per observation,

> ΔL = L(M₀) − L(M₁)

so **ΔL > 0 means M1 is the better model**. For each already-frozen comparison
and horizon, the omnibus null is that the expected ΔL is **equal across all five
TTE bins**:

> H₀ : E[ΔL | far] = E[ΔL | approaching] = E[ΔL | near_event] = E[ΔL | live_event] = E[ΔL | late_resolution]

This is a **heterogeneity / interaction** test, not five pairwise comparisons.

## 4. The family — 12 tests, not 60

**3 comparisons × 4 horizons = 12 omnibus tests**, mirroring EDGE-001's
structure exactly. Benjamini–Hochberg at **FDR 10%** computed once over these
twelve, **as a family entirely separate from EDGE-001's twelve**. The two
corrections are never pooled and never compared.

**Primary heterogeneity cell:** *(M1 vs M0) × 30 s* — the comparison that
actually speaks to §2, at EDGE-001's own primary horizon. The other eleven are
secondary within this family and carry the same correction.

## 5. Inference

* Cluster-robust at the **event/market** level, consistent with EDGE-001 §4 and
  Amendment 2 §F.
* Block bootstrap with block length **≥ 300 s**, the same discipline as the
  primary.
* **Sessions are a nesting level, not a unit**: market/session clusters are
  reported alongside, and the omnibus may not treat 300 s blocks as independent.
* Both noise floors from EDGE-001 §5 (shuffled labels, shifted features) are run
  **within each bin** and reported beside every bin estimate. No bin effect
  smaller than its own floor may be called real.

## 6. Underpowered bins, and no adaptive merging

`live_event` is **geometrically** underrepresented: its bin is 900 s wide, so a
3 h session contributes at most 3 qualifying intervals, ~36 market-blocks at
K=12, and ~144 over four sessions — against ~1,680 for each wide bin. **That is
design geometry, not market behaviour**, and S01 already showed the analogous
post-event stratum was dense (24 of 24 markets cleared the activity floor).

Consequently:

* Every bin's realised **block and cluster counts are reported** beside its
  estimate.
* A bin whose support falls below its preregistered floor is reported
  **`UNDERPOWERED`** — never silently omitted, and never as a null result.
* **Bins are never merged, split, reweighted or dropped**, adaptively or
  otherwise. All five enter every omnibus. Merging `live_event` into
  `near_event` after seeing counts would be exactly the post-hoc move this
  document exists to prevent.
* The omnibus runs on all five bins regardless; an underpowered bin weakens the
  test, and that is reported rather than engineered away.

**Floor for an individual bin estimate to be reported as anything but
`UNDERPOWERED`:** ≥ 100 market-blocks **and** ≥ 20 event/market clusters in that
bin. Chosen from S01's measured yield (377 blocks / 21 clusters in one session),
before any outcome was seen.

## 7. What follows a surviving omnibus

If an omnibus interaction survives BH at FDR 10%:

* individual-bin estimates are reported **descriptively only**, with their
  counts and noise floors beside them;
* no individual bin may be declared a discovery on the strength of a
  post-hoc localisation;
* the finding becomes a **new prospective preregistration under a new name**,
  tested on data not yet collected.

If no omnibus survives, the honest statement is that this tranche does not show
regime dependence — **not** that regime dependence is absent.

## 8. What this cannot conclude

* Nothing about whether order flow carries information **at all** — that is
  EDGE-001's question and EDGE-001's alone.
* Nothing that licenses capital, or that converts a failed primary into a
  success.
* Nothing about regimes outside the five frozen bins, or about venues, series
  or times of day outside the tranche's declared coverage.
* Nothing causal about *why* any regime differs, should one appear to.
