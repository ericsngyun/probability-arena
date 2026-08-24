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
| 1 | `late_resolution` | 4 | **1 running** |
| 2 | `live_event` | 4 | not started |
| 3 | `near_event` | 4 | not started |
| 4 | `approaching` | 4 | not started |
| 5 | `far` | 4 | not started |

A session counts toward bin *b* only if **a complete 300 s post-warmup panel
interval lies wholly within b**.

## Sessions

| # | label | primary bin | start (UTC) | length | subscribed | status |
|---:|---|---|---|---:|---:|---|
| 01 | `MMEDGE-S01-late_resolution-20260824` | `late_resolution` | 2026-08-24T00:41:58Z | 10,800 s | 24 | **RUNNING** |

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
