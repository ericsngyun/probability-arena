# LIFECYCLE-COMPATIBILITY-AMENDMENT-002

**Status: FROZEN 2026-09-15.** Amends confirmation-tranche scheduling for
`MARKET-MICROSTRUCTURE-EDGE-001`. Does **not** reopen any statistical cell.

Resume baseline (pre-amendment bookkeeping tip): `6cd23bc`.

---

## 1. What changed

The old `_POST_ANCHOR_BINS` proxy required the market to remain tradable for the
**entire 10,800-second capture** following the occurrence anchor (and only for
`late_resolution`). S04 / S07 and the pre-outcome lifecycle analysis showed that
this is neither necessary nor sufficient for observability of the frozen
estimand.

**Compatibility is henceforth defined by the availability of complete
feature-and-label observation intervals inside the target TTE stratum:**

\[
\mathrm{Compatible}(s,b)
=
N_{\text{complete observable intervals}}(s,b) \ge 1
\]

A complete unit requires:

1. pre-feature history (`LOOKBACK_S = 300`);
2. time inside the target TTE bin for a full decision interval;
3. tradable lifecycle state at feature time (stop-vs-anchor evidence);
4. forward observation through the frozen max label horizon (`300 s`).

Evidence (`SeriesLifecycleEvidence`) is stored separately from policy
(`SeriesBinCompatibility`). Implementation:
`app/microstructure/lifecycle_compatibility.py`.

Kalshi lifecycle semantics used here: `active` = tradable; `closed` /
`determined` / `finalized` = not; `close_time` is not settlement time.

---

## 2. What did **not** change

| Frozen surface | Status |
|---|---|
| M0 feature set | unchanged |
| M1 feature set | unchanged |
| Horizons `{1s, 5s, 30s, 300s}` | unchanged |
| Panel `K=12` | unchanged |
| Decision cadence `300 s` | unchanged |
| TTE bin edges | unchanged |
| Eligible series universe (8) | unchanged |
| `MAX_SESSIONS_PER_SERIES = 4` | unchanged |
| Primary BH / FDR family | unchanged |
| Confirmation blindness rules | unchanged |
| Stop-vs-anchor evidence table (2026-08-26, n=200) | **not re-measured** from S04/S07 |

---

## 3. Ops contracts introduced (not statistical constants)

| Contract | Value |
|---|---|
| `SESSION_SECONDS_BY_BIN[live_event]` | **1_800** |
| `SESSION_SECONDS_BY_BIN[near_event]` | **7_200** |
| `SESSION_SECONDS_BY_BIN[approaching]` / `[far]` / `[late_resolution]` | **10_800** |
| Target-bin reachability preflight | `TARGET_BIN_REACHABLE` / refuse otherwise |
| Whole-tranche feasibility gate | `tranche_feasibility.plan_remaining_tranche` |

Session duration is a **capture-plan property derived from target bin**, not a
hypothesis parameter.

---

## 4. Forward scheduling consequences

Under the interval rule (default per-bin durations):

| Bin | Compatible series |
|---|---|
| `late_resolution` | `KXATPMATCH`, `KXWTAMATCH` |
| `live_event` | `KXMLBGAME`, `KXATPMATCH`, `KXWTAMATCH`, `KXNFLGAME` |
| `near_event` / `approaching` / `far` | all 8 |

`KXMLBHR` is refused for `late_resolution` and `live_event`; it remains allowed
for pre-event bins.

---

## 5. Tests that lock the defect

`tests/test_lifecycle_compatibility_002.py`:

* KXMLBHR × late_resolution / live_event refuse; approaching allows
* ATP/WTA × live_event allow
* 3 h session crossing the anchor still allows when intervals complete
* mutation: `lag >= SESSION_SECONDS` is not observationally equivalent
* mutation: rejecting any anchor-crossing bin would kill tennis late_resolution
* max label horizon must appear in the completeness source
* whole-tranche feasibility after S07 is solvable without relaxing constants

---

## 6. Authorization boundary

This amendment authorizes:

* scheduling and preflight under the new compatibility module;
* per-bin capture durations above;
* dry-run tranche feasibility reports.

It does **not** authorize:

* inspecting confirmation alpha;
* changing M0/M1 / horizons / bins / universe / FDR;
* resurrecting the expired S08 schedule artifact;
* launching a session without a fresh freeze under the resume baseline.

---

*End of amendment.*
