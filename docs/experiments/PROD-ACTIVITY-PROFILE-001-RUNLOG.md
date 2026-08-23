# PROD-ACTIVITY-PROFILE-001 — run log

**STARTED 2026-08-20.** Six windows across two weekdays. No result may be read
from an intermediate window: the universe is frozen only after all six complete.

---

## Schedule (ET slots, as preregistered)

| day | discovery | slot A | slot B | slot C |
|---|---|---|---|---|
| 1 — Thu 2026-08-20 | 09:45 ET | 10:00 | 14:00 | 20:00 |
| 2 — Fri 2026-08-21 | 09:45 ET | 10:00 | 14:00 | 20:00 |

Day 1 runs under a sequential driver (PID 1186400). Day 2 is a **guarded**
systemd user timer (`pap001-day2.timer`, Fri 02:30 UTC) that **refuses to start
if day 1 halted on the §6 gate or did not complete all three slots** — §6 halts
the *set*, not one day.

Day 1 enumerated **163 candidates** whose event occurs that day.

## Machinery validation, before any window ran

The runner was validated end-to-end at ~03:20 ET against 161 live markets. It
failed three times first, and each failure was a real defect:

1. **No genesis marker.** The archive fails closed
   (`ArchiveNotInitializedError`) — correct — but the capture script **exits 0**
   while reporting `session_result.status = archive_error`. A runner reading the
   return code would have recorded a silent empty window. The runner now mints
   the genesis, plants the B4 session claim before the socket opens, and reads
   the session's own status.
2. **`capped_time` treated as a failure.** It is the *normal* terminal status
   for a bounded window — the session stopped because it was told to. That rule
   would have marked all six windows `INVALID`.
3. **Early termination measured from frame span.** `span_seconds` is
   first-frame-to-last-frame, so a quiet 90 s window was mislabelled
   `TERMINATED_EARLY:49s_of_90s` purely because the last frame arrived early.
   **A quiet venue is a measurement, not a truncation**, and that rule would
   have voided exactly the calm morning slots this profile exists to
   characterise. It now reads the session's own duration.

Final validation: `VALID`, 234 frames / 68 s, 161/161 markets snapshotted,
order-book sid contiguous `1..190`.

## A field-semantics correction, found on preflight

The universe rule was first written against **`close_time`**, which returned
**zero candidates for both days**. `close_time` is a **settlement deadline**,
typically days after the event: `KXMLBGAME-26AUG222040MINSD-SD` has its event at
`2026-08-23T03:40Z` and a `close_time` of `2026-08-26T00:40Z`;
`KXATPMATCH-26AUG20TIRFIL-TIR` closes `2026-09-03`. The activity being profiled
follows the **event**, so the field is **`occurrence_datetime`**
(== `expected_expiration_time`). Doctrine 8, again, on a field we had not read
before.

## Live confirmation of the peak-estimator finding

Every validation run reproduced the bias, on real production traffic:

| run | `peak_1s_sliding` | `peak_1s_calendar_bucket` | understatement |
|---|---:|---:|---:|
| 1 | 156 | 108 | 44% |
| 2 | 165 | 134 | 23% |
| 3 | 167 | 158 | 6% |

The gap varies with where the burst happens to fall against the clock — which
is the phase-dependence Amendment 1 records, now visible three times in a row on
live traffic rather than only on the frozen tape.

## Standing constraints for this run

* Collector and replay stay **frozen**. No cleanup, no opportunistic refactor.
* `peak_1s_sliding` is the **sole** capacity statistic; the calendar bucket is a
  diagnostic and gates nothing.
* Activity is measured from **wire frames**, never inferred from traded volume.
* The **3,500 f/s** stop is evaluated after each window, before the next starts.
* Each window owns an **immutable** archive root; the runner refuses a non-empty one.
* **Host load recorded** with every window, so venue intensity can be told from
  EVO contention.
* **No alpha is derived from intermediate windows.**
* `MARKET-MICROSTRUCTURE-EDGE-001` does **not** start automatically when window
  six ends. The activity-profile result is reported first.

---

# RESULT — all six windows complete, analysis run 2026-08-22

Acquisition **succeeded**. Six windows, all `VALID`, §6 never fired, every
sequenced SID contiguous. Analysis run under Amendment 3 as frozen, with
Amendment 4 governing the one censored window.

| day | slot | status | frames | exposure | peak₁ₛ | segments |
|---|---|---|---:|---:|---:|---:|
| 08-20 | A | `capped_time` | 23,566 | 1502.0 s | 111 | 2 |
| 08-20 | B | `capped_time` | 739,647 | 1512.6 s | 2,214 | 57 |
| 08-20 | C | `capped_time` | 338,316 | 1510.2 s | 1,314 | 27 |
| 08-21 | A | `capped_time` | 22,342 | 1501.9 s | 219 | 2 |
| 08-21 | B | `capped_time` | 75,424 | 1502.0 s | 409 | 6 |
| 08-21 | C | **`capped_events`** | 1,000,000 | **1472.3 s** | **≥2,704** | 77 |

## A — capacity: **STAY FROZEN**

No observed breach. Max sliding-1s rate **2,704 f/s** against the 3,500 stop —
and per Amendment 4 that is a **lower bound**, because it comes from the
censored window.

Across all six windows: `rotation_failures=0`, `sequence_faults=0`,
`frames_malformed=0`, `events_rejected=0`, `reconnects=0`,
`recoveries_requested=0`. `events_received == events_archived` in every window.
Every segment closed `clean`; every rotation landed at exactly **13,000**
records, none over. Peak host load 1.99 on 32 cores. The heaviest window
(08-21 C) rotated 77 segments in 1,472 s — one every ~19 s, minimum
open→close 14.2 s — with zero failures.

**No operational defect exists.** Amendment 3 rule 3 applies exactly as
written: `DEFAULT_MAX_SEGMENT_RECORDS` stays at 13,000. 2,704 f/s is a large
number, not a defect, and is not grounds to retune.

## B — there is **no stable high-activity market**

This is the statistic Amendment 3 said would decide whether a persistent
"high microstructure activity" market exists. It does not.

**Rank stability, market level, within day** (Spearman ρ on exposure-normalised
rates):

| day | pair | ρ orderbook | ρ trade |
|---|---|---:|---:|
| 08-20 | A~B | 0.431 | 0.795 |
| 08-20 | A~C | 0.004 | −0.098 |
| 08-20 | B~C | **−0.648** | −0.391 |
| 08-21 | A~B | 0.772 | 0.968 |
| 08-21 | A~C | 0.226 | 0.383 |
| 08-21 | B~C | 0.279 | 0.387 |

Adjacent slots correlate; distant ones do not, and day 1's 14:00→20:00 pair is
**strongly negative**. The markets busy in the afternoon are the quiet ones at
night. Activity tracks individual event start times, not a market property.

**Rank stability, series level, across days** — ρ_orderbook **0.881**,
ρ_trade **0.905** over all 8 common series. The *series* is highly stable; the
*market* is not. That contrast is the finding.

**Persistence vs burstiness.** Median max/median frame ratio is **45.95**
(day 1) and **33.24** (day 2); 31/40 and 35/40 markets exceed 10×. Worst:
`KXMLBTOTAL-26AUG201835NYYBAL-6` at **313×** (median 63, max 19,736).

**§4 activity floor** (≥1 snapshot + ≥500 deltas):

| day | clears pooled | clears in **all three** windows | clears in only one |
|---|---:|---:|---:|
| 08-20 | 39 / 41 | **0** | 22 |
| 08-21 | 40 / 41 | **5** | 18 |

Pooling is doing all the work. A "40-market universe" is not 40 continuously
active markets — it is ~40 markets each alive in one or two windows of three.

**§7 positive control: PASS** — both controls correctly FAIL §4
(279 and 73 pooled deltas against the 500 floor). The run is not void. Noted:
both controls emitted **zero ticker frames yet produced real order-book
deltas**, which is §5's point made on production — `ticker` is a discovery
heuristic and nothing more. Day 1's control cleared 56% of the floor, so the
anti-vacuity margin is 1.8×, not a chasm.

## C — `MARKET-MICROSTRUCTURE-EDGE-001` **requires a forward amendment**

| embedded assumption | realised | verdict |
|---|---|---|
| 40-market universe clears §4 | 39/41 and 40/41 **pooled** | survives pooled only |
| ≈360,000 rows ⇒ ≈12,000 30 s blocks | rests on 40 markets × full windows; 0 and 5 markets clear the floor in all three | **unsupported** |
| trade lag ~580 ms max interarrival | not measured by this tool | **unevaluated** |
| 30 s horizon carries mid movement | needs book reconstruction | **not checkable without entering alpha** |
| cost floor ≈ half-spread + fees | needs quotes | **not checkable without entering alpha** |
| 40 markets sit ~3.4× under the stop | **2.55×** vs envelope, **1.29×** vs stop; realised peak **1.33× above** the 2,040 f/s projection | **WRONG** |

Two further production facts the edge prereg must absorb: order book (sid 1)
and trade (sid 3) are **independently sequenced** — both contiguous in all six
windows, but with no venue-guaranteed common order, so trade→book joins are
timestamp-based *association*, never causal ordering. And with activity this
regime-dependent, a pooled model risks learning market identity and activity
regime rather than microstructure.

**Recommendation: do NOT run the edge experiment unchanged.** The capacity
projection is measurably wrong and the universe-stability premise is refuted.
Amendment 3's B deliberately did not fix a panel-selection rule; the evidence
for writing one now exists, and that preregistration should be written before
any panel is chosen.
