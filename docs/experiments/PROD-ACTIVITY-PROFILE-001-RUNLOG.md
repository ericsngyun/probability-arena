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
