# PROBABILITY-ARENA-RESUME-2026-09-15

**Status: STOP before any new confirmation tranche, EDGE-001 run, frozen-constant
change, or August-capture restart.** This document reconciles live repository and
EVO state as of **2026-09-15**, then answers two design questions with
lifecycle/status evidence only — no confirmation alpha, no EDGE-001 outputs.

Inventory time: local Mac + read-only SSH to `mikolabs` (EVO-X2).

---

## 0. Executive verdict

| Surface | Actual state (2026-09-15) | Not this |
|---|---|---|
| Local `main` | `34da0d6` — S07 close + replacement-debt repair | Assumed still on Aug 28 tip |
| Cursor Origin `origin/main` | `6940123` — local **ahead 3, unpushed** | Synced with local |
| EVO-X2 deploy | `6940123` (2026-08-28) via GitHub remote | On the Sep 14 ledger/debt commits |
| MMEDGE confirmation | **Idle since 2026-08-28**; S01–S07 archives on disk; S08 **scheduled then never armed** | August capture still running / needs restart |
| Replacement debt | S04→S21 (`late_resolution`), S07→S22 (`live_event`) — booked in local code+ledger | Cancelled by `late_resolution` 4/4 quota |
| Solana observation | Sparse observe **live**; reconciler **latched/disabled** | Whole crypto stack dark |
| X / social | Code + frozen 18-source universe on EVO; **no bearer token; no socket** | Qualification already running |
| `_POST_ANCHOR_BINS` | Still `(late_resolution,)` only — **known measurement defect** | Already widened |

**Do not:** run EDGE-001 · inspect confirmation feature/return outcomes · restart
S08’s expired 2026-08-29 window · change frozen bin edges / series cap / universe
/ settlement-lag table without an explicit amendment.

**May do next (human decision only):** push/deploy the 3 local commits · amend
`lifecycle_compatible` to the interval-completeness rule below · then schedule a
**new** `live_event` slate under the corrected gate.

---

## 1. Repository reconciliation

### 1.1 Commits

| Ref | SHA | Tip message | Date |
|---|---|---|---|
| Local `HEAD` | `34da0d65e63a890783d9f62caec02cd276c44f2d` | Merge the S07 close and the replacement-debt repair | 2026-09-14 |
| ↑ | `20fc35e` | S07 closed: capture healthy, market already settled… | |
| ↑ | `dfc5148` | A spent slot owes a replacement, however healthy the socket was | |
| `origin/main` (Cursor Origin `esy/probability-arena`) | `69401238a35547dc32c9e5e7ce4d8e39cb4d976b` | Merge the frozen 18-source X universe… | 2026-08-28 |
| EVO `~/projects/probability-arena` | **same `6940123`** | (GitHub `ericsngyun/probability-arena`) | 2026-08-28 |

Local Mac is **3 commits ahead of both Origin and EVO**. Those commits close S07
in the tranche ledger and repair `replacement_debt` so
`CAPTURE_HEALTHY_BUT_EMPTY` sessions still consume a slot and owe a named
replacement. Until they are pushed and deployed, EVO’s code still has the
pre-repair debt accounting.

### 1.2 Phase / flags (local `agent-context`)

Phase unchanged: read-only intelligence + calibration through EVAL-001. No EV,
no trading. Local feature flags all default-off in the agent-context dump;
EVO’s `.env` differs (see §5).

---

## 2. EVO-X2 operational state

Host: `mikolabs` · deploy: `/home/miko_node_001/projects/probability-arena` ·
DB ~**4.55 GB** · last durable boot **2026-09-14 17:46 UTC** (matches the
Sep 11–14 host-outage notes in `.remember/today-2026-09-14.md`).

### 2.1 Expected timers (live)

| Unit | State |
|---|---|
| `probability-arena-watcher.service` | active (since reboot) |
| `probability-arena-marketops.timer` | active (5 min) |
| `probability-arena-baseline.timer` | active (4 h) |
| `probability-arena-retention.timer` | active |
| `probability-arena-backup.timer` | active — artifact `backup-20260915T013341Z` |
| `probability-arena-meme-news.timer` | active |
| `probability-arena-tick-aggregation.timer` | active |
| `probability-arena-crypto-sparse-observe.timer` | **active** (hourly) |
| `probability-arena-crypto-reconcile.timer` | **loaded, disabled**; health latch tripped 2026-08-19 |
| MMEDGE / kalshi-microstructure timers | **none** |

No confirmation-capture process was running at inventory time.

### 2.2 Correction to the Sep 14 “roots missing” note

S01–S07 session roots are present under
`/home/miko_node_001/microstructure-tranche/` (165M … 248M for counted
sessions; S04 112K / S07 168K vacuous). The earlier “missing” note was a search
path error, not disk loss.

---

## 3. MARKET-MICROSTRUCTURE-EDGE-001 confirmation tranche

Source of truth: `docs/experiments/MARKET-MICROSTRUCTURE-EDGE-001-TRANCHE-LEDGER.md`
(local tip) + host `session-ledger.json` (mtime Aug 28; content agrees on
counted/vacuous).

### 3.1 Session table (status only — no alpha)

| # | Label | Bin | Series (host ledger) | Verdict |
|---:|---|---|---|---|
| 01 | `MMEDGE-S01-late_resolution-20260824` | `late_resolution` | `KXATPMATCH\|KXWTAMATCH` | CLEAN — counts |
| 02 | `MMEDGE-S02-live_event-20260824` | `live_event` | `KXATPMATCH` | CLEAN — counts |
| 03 | `MMEDGE-S03-late_resolution-20260825` | `late_resolution` | `KXMLBGAME` | CLEAN — counts |
| 04 | `MMEDGE-S04-late_resolution-20260826` | `late_resolution` | `KXMLBHR` | HEALTHY BUT EMPTY — no count |
| 05 | `MMEDGE-S05-late_resolution-20260826` | `late_resolution` | `KXATPMATCH` | CLEAN — counts |
| 06 | `MMEDGE-S06-late_resolution-20260827` | `late_resolution` | `KXWTAMATCH` | CLEAN — counts |
| 07 | `MMEDGE-S07-live_event-20260828` | `live_event` | `KXMLBHR` | HEALTHY BUT EMPTY — no count |
| 08 | *(no session root)* | `live_event` (planned) | `KXMLBHR` preferred | **`schedule-decision-08.json` only** — window was 2026-08-29; **expired, never armed** |

### 3.2 Quotas vs replacement debt (two facts)

| Bin | Target | Counted | Remaining |
|---|---:|---:|---:|
| `late_resolution` | 4 | **4** | 0 (quota met) |
| `live_event` | 4 | **1** | 3 |
| `near_event` / `approaching` / `far` | 4 each | 0 | 4 each |

| Spent vacuous slot | Owes | Bin |
|---|---|---|
| S04 | **S21** | `late_resolution` |
| S07 | **S22** | `live_event` |

`late_resolution` can read 4/4 **and** still owe S21. S05/S06 discharged their
own obligations, not S04’s. Corpus after S21 will legitimately hold **five**
counted `late_resolution` sessions.

Power/coverage floors still open: market-blocks ~1,299 / 4,000 · clusters ~78 /
150 · series represented **3 / ≥6** · weekend **1 / ≥4** · planned slots S08–S20
still notionally free, but **S08’s August decision must not be reused**.

### 3.3 What “expired August capture” means

Archives for S01–S07 remain on disk and are not deleted by age. What expired is
**S08’s schedule decision** (`decided_at_utc=2026-08-28T20:49:15Z`, selected day
ET 2026-08-29). Restarting that decision would be a new arming event under a
stale freeze — forbidden by this resume. Any next `live_event` session needs a
**fresh** schedule-anchor freeze under current rules.

---

## 4. Preregistrations and replacement-debt code

| Artifact | Status |
|---|---|
| `MARKET-MICROSTRUCTURE-EDGE-001` (+ capture plan, tranche ledger) | Confirmation tranche **authorized** 2026-08-24; **paused** operationally since S07 |
| `TRANCHE-SCHEDULE-BLOCKER.md` | Historical; Amendment 4 session-remaining gate already landed |
| `MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001` | Preregistered companion; not a reason to open alpha |
| `EDGE-DISCOVERY-001` | **STOPPED** (sports); scientific control only — **do not run** |
| `SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001` | FROZEN, **not run**; 18-source universe + budget frozen 2026-08-28 |
| `SOLANA-ALPHA-FEASIBILITY-001` | FROZEN, **not run**; blocked on qualification |
| Replacement-debt repair (`dfc5148`) | In local tree; **not on EVO/Origin** |

---

## 5. Solana / X observation stack

### 5.1 Solana (EVO)

| Piece | State |
|---|---|
| Crypto risk engine / GoPlus / SolanaTracker risk flags | enabled on EVO |
| `ENABLE_CRYPTO_SPARSE_OBSERVATION` | **true**; hourly timer active; last ok run 2026-09-15 ~06:47Z; DexScreener only |
| Tape reconciler | flag on, timer **disabled**, health latch since 2026-08-19 (`skipped_health_latch`) |
| Candidate-readiness + anchor-feed MarketOps hooks | still enabled; 14-day window closed 2026-07-31 PASS — keep/turn-off is an open human call |
| MEME-NEWS scout timer | active (separate from X transport) |

Read-only. No wallets, swaps, or execution surface.

### 5.2 X / social

| Piece | State |
|---|---|
| Transport / state machine / events / universe modules | present at EVO `6940123` |
| Frozen 18-source universe JSON | in repo |
| `X_BEARER_TOKEN` / file on EVO | **absent** |
| systemd unit / live process | **none** |

Qualification cannot start until credential + rule registration exist. Alpha
feasibility remains downstream and frozen.

---

## 6. Lifecycle compatibility revisit

### 6.1 Current rule (too coarse)

```405:430:app/microstructure/coverage.py
_POST_ANCHOR_BINS = (TTE_LATE_RESOLUTION,)
...
    if target_bin not in _POST_ANCHOR_BINS:
        return True, "bin sits before the anchor; the market is still live"
    ...
    need_h = session_seconds / 3600.0   # defaults to 3.0 h = full capture
```

Two independent defects:

1. **Which bins:** only `late_resolution` is gated. `live_event` always returns
   compatible — S07 × `KXMLBHR` was scheduled under that lie.
2. **How much life:** requires the **entire 10,800 s** capture to remain
   unsettled after the occurrence anchor, not “≥1 complete 300 s post-warmup
   research interval wholly inside the target bin while the market is live.”

Coverage counting already uses the interval definition
(`covering_intervals` in `scripts/kalshi_microstructure_schedule_anchor.py`).
Compatibility must answer the **same** question, plus settlement reachability.

### 6.2 Correct criterion (forward; no alpha)

A pair `(series, target_bin)` is **compatible** iff, under the frozen anchor
geometry (`FIRST_TICK_TTE_S` + default session length), there exists **≥1**
complete 300 s interval such that:

1. both endpoints fall in `target_bin` via `tte_bin`, and
2. both endpoints occur **before** median settlement
   (`TTE > −SERIES_SETTLEMENT_LAG_H[series] × 3600`), using the
   2026-08-26 / n=200 lag table already frozen in `coverage.py`.

Inputs: settlement lag + bin geometry + status vocabulary (`active`/`open` vs
`finalized`/…). **Not** used: activity, frames, rows, M0/M1, returns.

S04/S07 are **illustrations** that the lag table predicted the death; they are
not used to re-estimate the table (ledger already forbids re-measurement from
those outcomes).

### 6.3 Forward series × TTE-bin matrix

Computed 2026-09-15 against current frozen constants
(`session_seconds=10800`, current `FIRST_TICK_TTE_S`). Cell = number of
complete **live** covering intervals; **Y** means ≥1.

Settlement TTE (live while `TTE > settle_tte`):

| Series | lag_h | settle_tte (s) |
|---|---:|---:|
| `KXMLBGAME` | −0.04 | +144 |
| `KXMLBHR` | −0.22 | +792 |
| `KXMLBTOTAL` | −0.39 | +1404 |
| `KXWNBAGAME` | −0.55 | +1980 |
| `KXWNBATOTAL` | −0.61 | +2196 |
| `KXNFLGAME` | +0.12 | −432 |
| `KXATPMATCH` | +3.82 | −13752 |
| `KXWTAMATCH` | +5.80 | −20880 |

| Series | `late_resolution` | `live_event` | `near_event` | `approaching` | `far` |
|---|:-:|:-:|:-:|:-:|:-:|
| `KXMLBGAME` | N (0) | **Y (2)** | Y (20) | Y (35) | Y (35) |
| `KXMLBHR` | N (0) | **N (0)** | Y (20) | Y (35) | Y (35) |
| `KXMLBTOTAL` | N (0) | **N (0)** | Y (19) | Y (35) | Y (35) |
| `KXWNBAGAME` | N (0) | **N (0)** | Y (17) | Y (35) | Y (35) |
| `KXWNBATOTAL` | N (0) | **N (0)** | Y (16) | Y (35) | Y (35) |
| `KXNFLGAME` | N (0)* | **Y (3)** | Y (20) | Y (35) | Y (35) |
| `KXATPMATCH` | **Y (35)** | **Y (3)** | Y (20) | Y (35) | Y (35) |
| `KXWTAMATCH` | **Y (35)** | **Y (3)** | Y (20) | Y (35) | Y (35) |

\*NFL fails `late_resolution` under the **current** first-tick anchor
(`FIRST_TICK_TTE_S = −600`): settlement at TTE=−432s means the first research
tick is already post-settlement. That is an anchor/geometry interaction, not a
claim that no shorter post-occurrence window could ever exist if anchors were
amended.

**Contrast with today’s code:** `compatible_series(live_event)` returns all 8
series. Under the corrected rule it returns only
`{KXMLBGAME, KXATPMATCH, KXWTAMATCH, KXNFLGAME}`. S07’s preferred series
`KXMLBHR` is **N**.

### 6.4 Feasibility of existing series cap, bin edges, universe

Frozen constraints revisited **without changing them**:

| Constant | Value | Feasible under corrected matrix? |
|---|---|---|
| `BIN_TARGETS` 4/4/4/4/4 | 4 per bin | **Yes** — every bin has ≥2 compatible series; `late_resolution` max slots = 2×4 = 8 ≥ 4 (+ S21 still tennis-only) |
| `TTE_BIN_EDGES_S` | far>6h … live 0–15m … late&lt;0 | **Yes** — geometry still yields covering intervals; `live_event` remains the narrow stratum (max 3 intervals / 3 h) |
| `ELIGIBLE_SERIES` (8) | sports universe | **Yes, with role split** — 4 series are `live_event`-incompatible and `late_resolution`-incompatible; they remain essential for `near_event`/`approaching`/`far` and for `MIN_SERIES_REPRESENTED=6` |
| `MAX_SESSIONS_PER_SERIES=4` | global cap | **Tight but feasible** — tennis already carries most hard-bin mass; further `late_resolution` debt (S21) **must** be ATP/WTA; remaining `live_event` should prefer `KXMLBGAME` / `KXNFLGAME` over further tennis concentration |
| `MIN_SERIES_REPRESENTED=6` / `MIN_WEEKEND_SESSIONS=4` | still open (3 and 1 today) | **Achievable** only by using pre-event bins across MLB totals / HR / WNBA / NFL — not by more `late_resolution` tennis |

**Verdict:** the existing cap, edges, and universe remain **feasible**. What is
**not** feasible is continuing to schedule `live_event` (or `late_resolution`)
as if all eight series were lifecycle-compatible. That is a gate defect, not a
reason to shrink the universe or move bin walls.

**Not done here:** widening `_POST_ANCHOR_BINS`, changing `need_h`, editing
`SERIES_SETTLEMENT_LAG_H`, or amending `FIRST_TICK_TTE_S`. Those need an
explicit amendment after this resume is accepted.

---

## 7. Operational capture-duration shortening (estimand unchanged)

### 7.1 What must stay fixed for the same estimand

Unchanged: TTE bin definitions · horizons `(1,5,30,300)` · primary horizon 30 ·
`EMBARGO_S=300` · purge/embargo/walk-forward · FDR · activity floor · K ·
coverage rule “≥1 complete 300 s post-warmup interval wholly in bin” ·
`dataset_role=CONFIRMATION` · eligibility `session_remaining > max_horizon +
embargo` (=600 s).

Session **wall-clock length** is an operational choice about how many in-bin
intervals a single capture harvests. It is not itself the estimand. Shortening
it does not change cell definitions if the session still produces ≥1 eligible
covering interval in the target bin (and labels remain protected by the 600 s
remaining gate).

### 7.2 Duration model

Let:

```text
W = WARMUP_S                         = 300
D = DECISION_TICK_S                  = 300   # one research interval
H = max(HORIZONS_S)                  = 300
E = EMBARGO_S                        = 300
R = MIN_SESSION_REMAINING_S = H + E  = 600
B = bin width (live_event = 900; …)
```

**Estimand-preserving operational minimum** (current anchors), measured
directly: for every currently compatible `(series, bin)` pair,

```text
T_min = W + R + 1 = 901 s
```

At 901 s the first post-warmup decision tick has `session_remaining > 600`, and
`covering_intervals` reports ≥1 (typically 2) geometric in-bin intervals.
Settlement-filtered live counts remain ≥1 for every cell marked Y in §6.3.

Equivalent framing matching the requested pieces:

| Construction | Formula | Value | Role |
|---|---|---:|---|
| Label-safe single interval | `W + (H + E) + ε` | **~901 s** | shortest capture that still yields one eligible in-bin interval |
| Interval + explicit lookback span | `W + D + (H + E)` | **1200 s** | same estimand; one full 300 s span after warmup before the remaining gate |
| Full narrow-bin traversal | `W + B_live + (H + E)` | **1800 s** | harvest the whole 15-minute `live_event` stratum |
| Current default | fixed | **10_800 s** | maximises intervals in wide bins; overkill for `live_event` (cap 3 intervals anyway) |

### 7.3 What shortening does *not* buy

* It does **not** make `KXMLBHR` × `live_event` compatible — settlement at
  TTE≈+792 s still kills every 300 s interval inside the 0–900 s bin.
* It does **not** relax FDR, horizons, or the covering definition.
* It **does** reduce socket-hours per session and may make hard-bin scheduling
  denser once the lifecycle gate is fixed — a logistics win, not a statistical
  redesign.

**Recommendation (report only):** for future `live_event` arms, **1200–1800 s**
is sufficient to preserve the estimand; 3 h remains fine for wide bins where
interval count feeds power. No constant changed in this resume.

---

## 8. Hard stops — explicit non-actions

Per instruction, this resume **did not** and **must not** be followed by:

1. Running `EDGE-001` / `EDGE-DISCOVERY-001` evaluation paths.
2. Inspecting confirmation M0/M1 returns, coefficients, or “which sessions look
   promising.”
3. Restarting or re-arming the expired S08 / 2026-08-29 schedule.
4. Launching S08–S20 or S21/S22.
5. Modifying `_POST_ANCHOR_BINS`, `SERIES_SETTLEMENT_LAG_H`, bin edges, series
   cap, or universe without a separate accepted amendment.
6. Clearing the crypto reconciler latch or opening an X socket as a side effect
   of this document.

---

## 9. Safe next human decisions (ordered)

1. **Accept or amend** this resume’s compatibility criterion (§6.2).
2. **Push** `dfc5148`/`20fc35e`/`34da0d6` to Origin and deploy to EVO so debt
   accounting matches the ledger.
3. **Amendment draft** (separate milestone): implement interval-completeness
   `lifecycle_compatible` for all bins that can intersect settlement
   (`late_resolution` and `live_event` at minimum); keep lag table frozen;
   extend tests that currently assert “pre-anchor bins exclude nothing.”
4. Only then: **fresh** `live_event` schedule-anchor freeze restricted to
   `{KXMLBGAME, KXATPMATCH, KXWTAMATCH, KXNFLGAME}`, preferring baseball/NFL
   for series-cap headroom; optionally use 1200–1800 s session length.
5. Independently: X bearer token (qualification still frozen); reconciler latch
   clear (human); candidate-readiness flag keep/off decision.

---

## 10. Evidence index

| Claim | Evidence |
|---|---|
| Local vs Origin vs EVO SHAs | `git rev-parse` local + `ssh mikolabs` |
| Tranche quotas / debt | `docs/experiments/MARKET-MICROSTRUCTURE-EDGE-001-TRANCHE-LEDGER.md`; host `session-ledger.json` |
| S08 expired | host `schedule-decision-08.json`; no `session=MMEDGE-S08-*` |
| Lifecycle defect | `app/microstructure/coverage.py`; ledger S07 writeup; `tests/test_microstructure_coverage_001.py` |
| Matrix numbers | deterministic recomputation from `SERIES_SETTLEMENT_LAG_H` + `FIRST_TICK_TTE_S` + `tte_bin` / `covering_intervals` (2026-09-15) |
| Duration floor 901 s | same harness; `WARMUP_S` + `MIN_SESSION_REMAINING_S` + 1 |
| Solana/X EVO flags | live `.env` + `systemctl --user` on `mikolabs` |
| Preregs frozen | `docs/experiments/SOLANA-*-001-*.md` |

---

*End of resume. Stopped before tranche launch and before any EDGE-001 or frozen-constant modification.*
