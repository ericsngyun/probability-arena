# KALSHI-TAPE-MANIFEST-001 — findings

**Companion to the machine-generated artifacts.** `KALSHI-TAPE-MANIFEST-001.md`
and `.json` are pure tool output and are reproducible from the tool; this file
is the human analysis, the replication record, and the list of things that
could **not** be determined.

**Verdict: REFUSED.** No capture session is authorized by the frozen manifest.
Authorizes no orders, no portfolio channels, no venue writes, no capital.

---

## 0. What was asked, and what happened

Freeze the CP6–CP9 qualification session's manifest before any capture: 12 live
DEMO tickers, stratified 4 high / 4 medium / 4 low by message rate, spanning
several contract/event structures, with the snapshot timestamp, the ranking
statistic and the full candidate population recorded.

The tool was built, tested offline, and run read-only against the DEMO venue
from EVO. It **refuses**. DEMO's activity distribution has an empty middle: it
cannot supply three separable activity bands, only two.

Along the way the tool found — and corrected — **a defect in itself**, which is
reported first because it is the more transferable finding.

---

## 1. The defect this tool shipped, then caught

The first revision ranked by the venue's `volume_24h` field and gated freshness
on `updated_time`. Run against DEMO it returned **REFUSED**, having rejected
**73,057 of 73,630 markets as "stale"**, leaving 14 eligible markets that were
almost all one UFC fight card. That read as a clean, dramatic finding about the
venue.

It was an artifact of the gate.

**What exposed it.** Two census runs 2.5 minutes apart disagreed about
`volume_24h` on markets whose `updated_time` was **128 days frozen**. A frozen
market's volume does not move. So one of the two fields did not mean what its
name said, and the manifest had no way to tell which.

**The measurement that settled it.** Ten high-volume markets, re-read 180
seconds apart:

| field | moved |
|---|---|
| `updated_time` | **0 / 10** |
| lifetime volume (`volume_fp`) | **10 / 10** |
| top of book | **10 / 10** |

`updated_time` is a market-**definition** timestamp on this venue. It does not
track trading or quoting. The markets the tool was calling "stale" were trading
hundreds of thousands of contracts per minute at that instant.

**Why this is worth recording.** It is doctrine 7's failure class exactly — *a
plausible benign value produced by a broken path*. The refusal did not crash,
did not look wrong, and produced a confident, quotable number. Had it been
believed, the milestone's conclusion would have been "DEMO has ~14 tradeable
markets", which is false by a factor of forty.

It is also precisely what the CP6–CP9 preregistration anticipates: *venue
behaviour that contradicts fixture assumptions must update the model of the
venue BEFORE qualification proceeds*. The venue model is updated; the gate is
**deleted**, not defaulted-off, and `test_eligibility_policy_has_no_staleness_
knob_at_all` pins its absence so it cannot return with a permissive default.

---

## 2. The statistic, after the correction

**`traded_contracts_per_minute`** — measured, not read.

The venue's monotone **lifetime** volume counter (`volume_fp`) is read four
times at 150-second intervals; the rate is the increase divided by elapsed
minutes.

**Why the lifetime counter and not `volume_24h`:** the lifetime counter only
increases, so its difference over a window is exactly the contracts traded *in*
that window. A trailing-24h field's difference mixes arrivals with roll-off and
can go negative — observed directly (−266,639 over four hours on
`KXPGATOUR-FESJC26-JTHO`).

**Integrity control:** the counter must never decrease. Across 868 probed
markets × 4 reads, `lifetime_volume_is_monotonic = True`, 0 violations. Markets
that violate it are rejected and flagged, never clamped to a plausible rate.

**Corroborating measure:** `top_of_book_change_rate`, the fraction of
consecutive reads in which the top of book moved — a direct lower bound on
`orderbook_delta` activity. Mean 0.877; **433 of 582** eligible markets changed
at *every* read. It is **saturated** at this sampling interval and therefore
bounds the message rate only from below, uselessly (see §6).

**Stated limitation that does not go away:** this is a **trade** rate used as a
proxy for a **message** rate. `orderbook_delta` fires on quote revisions that
never trade. The rank correlation between the two is **UNMEASURED** and can
only be measured by the capture this manifest precedes.

**On cherry-picking.** Measuring the stratification variable is not the
forbidden move. The session was authorized to stratify on activity; measuring
activity executes that instruction. What is forbidden is selecting markets by
how clean their *telemetry* looks once captured — and no gate in this tool can
be evaluated from a capture.

---

## 3. The sampling frame, stated

| stage | markets |
|---|---|
| census: open markets, MVE shards excluded, paginated to exhaustion | **73,260** |
| screen: two-sided, uncrossed, non-negatively sized, non-zero 24h volume | **868** |
| probe: appeared in ≥2 timed reads and traded during the window | **582 eligible** |

Snapshot timestamps (all UTC):

- census `2026-08-16T03:58:59.525553Z` → `04:01:09.649911Z` (367 pages, 130.1 s)
- **canonical activity-snapshot timestamp: `2026-08-16T04:01:12.764730Z`** (the
  probe's first read)
- probe reads: `04:01:12.764730Z`, `04:03:44.910938Z`, `04:06:16.605143Z`,
  `04:08:48.672395Z` (span 7.60 min)
- frame digest (SHA-256): `f0604224c651ac72…` (full value in the JSON)

The full 582-market eligible population — every ticker, with its measured rate,
rank and stratum — is in §7 of the manifest. The 73,260-market census is
committed by digest rather than by row, with the 250 highest-screen-statistic
rejected markets listed so the rejection reasons are visible.

---

## 4. THE FINDING — DEMO's activity distribution has an empty middle

| rate band (contracts/min) | eligible markets |
|---:|---:|
| ≥ 1,000 | **4** |
| 100 – 1,000 | **0** |
| 30 – 100 | **0** |
| 17 – 30 | 91 |
| 15 – 17 | **180** |
| 10 – 15 | 118 |
| 5 – 10 | 9 |
| 1 – 5 | 179 |
| < 1 | 1 |

- **A 98.3× cliff between rank 4 and rank 5** (2,632.1 → 26.8 c/min), with
  **nothing whatsoever between 30 and 1,000 c/min**.
- Ranks 5–400 span only **5.35×** across **396 markets** — a near-flat plateau.
- **180 of 582 (30.9%) sit inside the single narrow band 15.0–17.0 c/min.**

This is not a heavy-tailed market distribution. It is **four real markets plus a
synthetic plateau**, which is what a sandbox with a uniform simulated
market-maker looks like.

The consequence for the authorized design is arithmetic. Contiguous tertiles of
582 markets put the medium/low boundary inside the plateau:

| boundary | upper stratum min | lower stratum max | ratio | preregistered floor |
|---|---:|---:|---:|---:|
| high / medium | 2,632.11 | 15.96 | **164.91×** | 2.0 ✓ |
| medium / low | 15.87 | 12.21 | **1.30×** | 2.0 ✗ |

The high band is real. The medium and low bands are the same population cut in
half. Labelling them "medium message rate" and "low message rate" would be a
relabelling of an arbitrary cut through a continuum — which is the specific
false confidence the separation gate exists to prevent.

**The threshold was frozen at 2.0 before any venue data was seen** (first
commit of this branch). Lowering it now to reach twelve would be exactly the
blurring the instruction forbade, so it was not done.

---

## 5. A second finding, flagged rather than acted on

**194 of the 582 eligible markets (33%) are venue-internal test instruments** —
`KXMAXSHARDINGTEST` (188) and `KXTESTMATCH` (6). Two of them reached the
selected twelve: `KXTESTMATCH-26AUG122030BANAUS-TIE` and
`KXMAXSHARDINGTEST-26AUG2818-T68399.99`.

A Kalshi sharding load-test instrument is not a market, and its message
behaviour says nothing about market microstructure. Excluding such series is
arguably the same structural category as the MVE-shard exclusion already in the
selection rule.

**I did not add that gate, deliberately.** I noticed it only *after* seeing
which markets the rule selected, and amending a preregistered selection rule
once you can see its effect on the selection is the discipline this milestone
exists to protect. It is Eric's call. For sizing that call: excluding them
shrinks the eligible pool 582 → 388 and does **not** change the verdict, because
the plateau is the binding constraint either way.

---

## 6. What could NOT be determined

1. **Whether the 100,000-frame floor is reachable on DEMO at all.** This is the
   most consequential gap and it bears directly on CP9's power verdict. REST
   probing cannot answer it: `top_of_book_change_rate` is saturated at a
   150-second sampling interval (433/582 markets changed at every read), so it
   bounds the message rate only from below, and the bound it gives (~0.08 book
   changes/s across twelve markets) is far too weak to be useful. **Settleable
   without a capture** by re-probing a small pool at a 2–5 second interval to
   un-saturate the measure; not done here because the manifest is refused on
   independent grounds.
2. **The rank correlation between trade rate and message rate.** Structurally
   unmeasurable before a capture. The stratification rests on the assumption
   that it is positive and monotone, and that assumption is unverified.
3. **Whether DEMO's flow is simulated.** The 15–17 c/min cluster and the empty
   30–1,000 band are strongly suggestive of a synthetic market-maker, but the
   venue documents nothing and this is inference from shape, not evidence.
4. **Whether any of this transfers to production.** Nothing here is evidence
   about the production venue's activity distribution. Milestone §11 Q1 already
   flags that a demo session measures demo liquidity; this quantifies how
   severe the gap may be.
5. **Whether the four genuinely active markets stay active.** Three of the four
   are event-driven (`KXMLB-26`, `KXNBA-27`, `KXLIGAMXGAME-26AUG16SLACDG`) and
   the manifest is frozen hours before any session would run.
6. **The corrupt-book cohort's cause.** 59 crossed books and 45 negative resting
   sizes persist in the frame; two markets held an unchanged negative bid size
   across reads 180 s apart while their ask size moved. These are rejected by
   the gates, but whether they are a demo-only artifact is unknown.

---

## 7. Replication

The refusal is not a single-snapshot fluke.

- The **discredited** first revision was run twice, 2.5 minutes apart, on
  frames of 73,630 and 73,629 markets with different digests: **identical
  selection, identical five refusal reasons.**
- The **corrected** revision was run against a frame taken ~30 minutes later
  (73,260 markets) and refuses again, now on the single separation reason.
- The venue-model probe (§1) is an independent 10-market experiment with an
  unambiguous 0/10 vs 10/10 split.

---

## 8. Decisions this leaves open for Eric

Stated as options, not recommendations with one pre-picked.

1. **Amend the separation floor.** 2.0 was a judgment made blind. If a 1.3×
   medium/low separation is acceptable for a first qualification run, that is a
   preregistration amendment, made explicitly and before the run.
2. **Two strata instead of three.** DEMO cleanly supports "4 active + 8
   plateau". This abandons the 4/4/4 shape Eric froze.
3. **Accept 4 high + 8 drawn from the plateau, unstratified**, and report the
   plateau honestly as one band rather than two.
4. **Exclude venue test series** (§5) — needed regardless of which of the above
   is chosen, if any session runs.
5. **Go to production for the rate distribution**, which changes the credential
   and risk profile (milestone §11 Q1, CP10 — separate Tier-2 approval).
6. **Settle the frame-rate question first** with a short high-frequency
   re-probe (§6.1) before committing to any session length.

Doing nothing is also coherent: the manifest is frozen, the finding is
recorded, and no capture has occurred.

---

## 9. Safety

Read-only throughout. Only `GET /markets` was ever requested. **No credential
was loaded, read, copied, printed or written** — Kalshi's market-data routes are
public on both environments, so the tool has no credential code path at all,
and a static audit in the suite asserts the module contains no signing,
subscription or mutating-verb surface. No socket was opened. No capture was
started. No archive was written. Safety grep clean (boundary-statement
docstrings only).

EVO's production checkout was never modified: the tool ran from a throwaway
clone in `/tmp`, and `~/projects/probability-arena` remained at `100b5b1`,
branch `main`, working tree clean.
