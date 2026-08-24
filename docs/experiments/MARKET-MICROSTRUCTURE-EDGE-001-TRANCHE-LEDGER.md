# MARKET-MICROSTRUCTURE-EDGE-001 — confirmation tranche ledger

**BLIND CAPTURE IN PROGRESS.** 20 sessions, ≥14 days, `dataset_role=CONFIRMATION`.

Authorised 2026-08-24 after: sampling-runner qualification (12/12 invariants),
row-builder qualification (11/11 mutations), the row-v2/label-v2 seam smoke
test, Amendment 3 (`realized_vol_1s` removed) and Amendment 4 (eligibility
gates on session-remaining, five bins proven reachable).

## What may and may not be inspected during capture

**Permitted:** capture health · sequence/archive integrity · safety peak ·
row/schema validity · mechanical power accumulation · session and TTE-bin
coverage · series and weekend coverage.

**Forbidden:** returns by feature · M0/M1 losses · coefficients · feature
importance · OFI/markout relationships · flow-conditioned returns · which
sessions "look promising."

The only adaptive action permitted is **scheduling future sessions to satisfy
already-preregistered coverage constraints** — never to improve a result.

## Bin allocation — 4/4/4/4/4, hardest first

| order | primary bin | sessions | status |
|---:|---|---:|---|
| 1 | `late_resolution` | 4 | **1 of 4 complete** |
| 2 | `live_event` | 4 | not started |
| 3 | `near_event` | 4 | not started |
| 4 | `approaching` | 4 | not started |
| 5 | `far` | 4 | not started |

A session counts toward bin *b* only if **a complete 300 s post-warmup panel
interval lies wholly within b**.

## Sessions

| # | label | primary bin | start (UTC) | length | subscribed | status |
|---:|---|---|---|---:|---:|---|
| 01 | `MMEDGE-S01-late_resolution-20260824` | `late_resolution` | 2026-08-24T00:41:58Z | 10,800 s | 24 | **CLEAN — counts** |

### Session 01 — pre-capture record

Candidate set frozen before capture by the Addendum 1 rule
`(occurrence_datetime ASC, ticker ASC)` over open markets in the eligible
series whose event lies in the assigned bin.

* 24 open markets, all `KXWTAMATCH` (16) and `KXATPMATCH` (8)
* events **5.69–6.69 h in the past** — genuinely `late_resolution`, i.e. past
  the nominal occurrence time and **not** settled
* exactly the concurrency ceiling, so no truncation was required
* preflight: `40,000,000 > 3,500 × 10,800 = 37,800,000` ✓

**This session is also the feasibility discovery** Amendment 4 made possible.
Whether books past their nominal event time still publish enough sequenced
activity to clear the 30-events-per-300 s floor is **not established by any
tape we hold** — every prior session was 2–3 h pre-event. If the panel comes
back persistently empty, `late_resolution` is thin at this venue, and that is a
reportable finding rather than a reason to substitute an easier bin.

### Session 01 — verdict: **OPERATIONALLY CLEAN**, counts toward `late_resolution`

**L1 capture validity — PASS.** Terminal `capped_time`.
`events_received == events_archived == 1,019,783`. `frames_malformed`,
`events_rejected`, `rotation_failures`, `sequence_faults`, `reconnects` all
**0**. 81 segments committed. `peak_1s_sliding` **1,210** against the 3,500
stop.

**L2 sampling validity — PASS.** First tick exactly at open+300 s; no warmup
tick emitted a panel; tick gaps exactly `[300.0]`; max panel size **12**, K
respected; reason vocabulary closed with no unexpected reasons.

The final two ticks returned **empty panels, correctly**: at open+10,500 s and
open+10,800 s the scheduled session-remaining was 600 s and 300 s, and the
Amendment 4 gate is *strictly* greater than 600 s. The gate is visible working
in production.

**L3 dataset validity — PASS.** `microstructure-row-v2` / `label-v2`;
13 M0 and 17 M1 columns; minimum completeness **0.978 / 0.9781**; **no
always-missing columns**; `dataset_role=CONFIRMATION`; **113,100 rows**;
0 dispatch errors.

Label coverage: **0.9780 / 0.9779 / 0.9770 / 0.9674** at 1/5/30/300 s. The 300 s
horizon holds up at 96.7%, as the 3 h projection expected.

**L4 coverage outcome — the feasibility question is answered.**

| | |
|---|---|
| covering intervals wholly in `late_resolution` | **377** |
| counts toward target bin | **yes** |
| markets clearing the 30-events/300 s floor | **24 of 24** |
| markets ever selected | 21 |
| market-blocks accumulated | **377** |
| market/session clusters | **21** |
| bins touched | `late_resolution` only |
| no-row reasons | `eligible_but_never_top_K`: 3 |

**Post-event, unsettled books are dense enough to support the panel.** Every
one of the 24 markets cleared the activity floor. **No market closed or
resolved mid-session, and none failed activity eligibility** — the only reason
any market went unsampled was losing a top-12 competition, which is the
mechanism working, not a venue limitation.

That is a real finding about Kalshi's lifecycle: `TTE < 0` is not `market no
longer observable`, which is exactly the distinction Amendment 4 was written to
preserve. Under the old `TTE > 600 s` gate this session was impossible.

**Power, measured rather than projected.** 377 market-blocks and 21 clusters
from one session, against projections of 420 and a conservative 12. At this
rate 20 sessions yield ≈7,540 blocks (floor 4,000) and well past 150 clusters.
