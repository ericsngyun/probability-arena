# MARKET-MICROSTRUCTURE-CAPTURE-RUNNER-VALIDATION-001

**Status: VALIDATION — not research data.** Its only job is to demonstrate that
the prospective sampling contract frozen in `MARKET-MICROSTRUCTURE-EDGE-001`
Amendment 2 and its capture plan is implemented **exactly as preregistered**.

No tranche capture is authorised by this milestone. No M0/M1 is run.

---

## What was built

| piece | what it is |
|---|---|
| `app/microstructure/panel.py` | the frozen sampling contract as a **pure decision core** — no socket, no model, no feature |
| `scripts/kalshi_microstructure_capture_runner.py` | the session runner: preflight, capture, replay, audit |
| `tests/test_microstructure_capture_runner_validation_001.py` | 47 tests proving the twelve invariants |

**The collector is not modified.** That was a design constraint, not an
accident: subscriptions are fixed for the session and bounded by the
never-exceed ceiling (the capacity guard), while the K=12 research panel
rotates among them. Nothing needs mid-session resubscription, so the frozen
collector stays frozen.

## Two design holes the contract had, found before any capture

Validation earns its keep by finding these *before* the tranche.

**1. Which quantity does the capacity ceiling bind?** The plan froze K=12 with a
never-exceed 24 but never said whether that bound subscriptions or research
rows. It binds **subscriptions**; the research panel is the rotating K=12. This
is forced, not chosen — a market that was never subscribed has no lagged
order-book activity to rank, so rotation can only happen inside the observed
set.

**2. Which 24, when the window yields more?** Recorded as Addendum 1 to the
capture plan. A live 5-hour window returned **88 candidates**. The obvious fix —
ticker-lexicographic truncation — is deterministic and unsteerable and still
**wrong**: applied live it selected 24 tennis markets with events 17–26 hours
away, all of which would sit below the activity floor for the entire session.
The panel would never rotate and the session would validate nothing. The rule is
now ordering by `(occurrence_datetime ASC, ticker ASC)`, which makes the
subscribed set a real event cluster and selects on a scheduled calendar fact
rather than on any activity, volume or return signal.

## The twelve invariants

| # | invariant | how it is proven |
|---|---|---|
| 1 | warmup is real | 0 rows for 300 s while counters accumulate; first tick at open+300 s |
| 2 | eligibility uses only the trailing interval | **metamorphic**: append 25,000 frames strictly after `t`, panel(`t`) byte-identical |
| 3 | activity threshold exercised both sides | 29 → ineligible, 30 → eligible, as an **integer** count |
| 4 | K means K | ≤12 always; 7 qualify → 7 selected, no backfill; deliberate tie exercises the tie-break |
| 5 | ticker/volume cannot reach eligibility | **AST guard** over the eligibility path, plus behavioural proof |
| 6 | sequence contamination excludes, then recovers | real induced gap; refusal, then recovery once it ages out |
| 7 | generation validity is *current*-generation | stale gen N−1 ladder refused in gen N, reusing the collector's typed vocabulary |
| 8 | TTE boundaries | 601/600/599 s, and all five frozen bin edges |
| 9 | panel changes only on the 300 s clock | mid-tick surge at +17 s cannot enter until the next tick |
| 10 | event cap unreachable before the safety stop | `max_events > hard_stop_fps × max_seconds` pinned as a **startup refusal** |
| 11 | the safety gate can stop the runner | synthetic breach; halted sessions can never be confirmation data |
| 12 | provenance is typed | `dataset_role` is a validated enum, not a filename convention |

### The tests have teeth — a 12-mutation campaign

47 passing tests prove nothing on their own. Each mutation below was applied to
the core, the suite re-run, and the file restored and **verified byte-identical
by checksum** afterwards.

| mutation | result |
|---|---|
| drop the `ts <= t` lookahead bound | **2 failed** |
| threshold off-by-one (`<` → `<=`) | **1 failed** |
| TTE edge off-by-one (`<=` → `<`) | **1 failed** |
| invert the tie-break | **1 failed** |
| ignore K (backfill past it) | **3 failed** |
| ignore sequence faults | **3 failed** |
| accept stale generations | **1 failed** |
| disable warmup | **1 failed** |
| open the lookback lower edge | **1 failed** |
| count `ticker` as order-book activity | **1 failed** |
| weaken the capacity inequality | **1 failed** |
| neuter the safety stop | **1 failed** |

**12 of 12 killed.** No mutation survived.

## Notes on two semantics the core encodes deliberately

**Sequence cleanliness is per-SID, not per-market.** L22 established that `seq`
is per-subscription. A gap therefore contaminates **every market riding that
subscription** for the lookback, and the core models that rather than
attributing the fault to whichever market happened to carry the frame.
Pretending faults are per-market would silently admit contaminated books.

**The activity floor is an integer.** 0.10 events/s over 300 s is exactly 30
events. Comparing the float rate would put floating-point rounding directly on
the eligibility boundary, so the rule compares counts and a test asserts the
float constant is absent from the eligibility path.

---

## The live validation session — 2026-08-23

`MMCRV001-validation-20260823`, session `s-20260823T231228Z-b96fc1bed4f1`, at
commit `4fba8c3`. **`VALIDATION_ONLY`, `dataset_role=VALIDATION`,
`usable_as_confirmation=false`.** Permanently excluded from
`MARKET-MICROSTRUCTURE-EDGE-001`.

24 subscribed markets (WNBA game/total, WTA match) with events 2–3 h out;
1,200 s; K=12.

| | |
|---|---|
| frames | **350,391** (`orderbook_delta` 332,847 · `ticker` 11,160 · `trade` 6,357 · `orderbook_snapshot` 24) |
| terminal status | **`capped_time`** — the clock bound, not the frame cap |
| peak₁ₛ sliding | **1,493 f/s** vs the 3,500 stop |
| segments committed | 27 |
| `events_received` = `events_archived` | 350,391 = 350,391 |
| rotation failures · sequence faults · malformed · rejected · reconnects | **0 · 0 · 0 · 0 · 0** |
| decision ticks | 3, gaps of **exactly 300.0 s** |
| panel size | **12 at every tick**, 24 audit rows per tick |

**Rotation is real, not theoretical.** Six of twelve markets swapped between
tick 0 and tick 1, two more between tick 1 and tick 2 — an entire game's markets
(`LVTOR`) displaced by another's (`INDCHI`), then tennis entering as the WNBA
tip-offs approached. This is exactly the event-time dependence the profile
found, now visible inside a single 20-minute session.

**The metamorphic anti-lookahead proof, on live tape.** Each tick was recomputed
from a tape truncated at that instant — 58,491 / 170,643 / 246,752 of 350,391
frames — and every panel came back **byte-identical** to the full-tape run.

**Ticker isolation, on live tape.** 11,160 real ticker frames, 3.2% of the tape,
contributed **zero** to eligibility.

**The censoring fix worked.** The session ended `capped_time`. Preflight
asserted `40,000,000 > 3,500 × 1,200 = 4,200,000` before the socket opened.

## Verdict

| check | verdict | basis |
|---|---|---|
| warmup | **QUALIFIED** | live: first decision exactly open+300 s, none earlier |
| lookahead | **QUALIFIED** | unit metamorphic **and** live metamorphic over 350k frames |
| threshold boundaries | **QUALIFIED** | unit 29/30 + mutation — *not exercised live* |
| K / tie-break | **QUALIFIED** | unit incl. a deliberate tie; live 12/12 with real rotation |
| source isolation | **QUALIFIED** | AST guard + unit + live (11,160 ticker frames, zero effect) |
| fault exclusion | **QUALIFIED** | unit refusal-and-recovery + mutation — *not exercised live* |
| generation safety | **QUALIFIED** | unit stale-generation refusal + mutation — *not exercised live* |
| TTE | **QUALIFIED** | unit 601/600/599 and all five bin edges; live exercised 2 of 5 bins |
| decision cadence | **QUALIFIED** | live gaps exactly 300.0 s; mid-tick surge test in unit |
| capacity relationship | **QUALIFIED** | startup refusal proven in unit; live preflight passed, `capped_time` |
| safety stop | **QUALIFIED** | live below-threshold path (1,493 < 3,500); breach path **synthetic by design** |
| provenance | **QUALIFIED** | typed enum, commit stamped, `usable_as_confirmation=false` |

### What the live session did NOT prove, stated plainly

Production in a 20-minute window simply did not produce the negative
conditions. Every one of the 24 markets sat far above the activity floor
(minimum count **212** against a floor of **30**), every book was `publishable`,
there were **zero** sequence faults, and no market approached the TTE edge. So
the live run exercised the *positive* path only.

The five negative paths — activity-floor exclusion, sequence-fault exclusion and
recovery, stale-generation refusal, TTE exclusion, and the safety stop firing —
rest on unit tests plus the mutation campaign, **not** on live occurrence. That
is the correct trade: a sequence fault cannot be induced on production, and
deliberately breaching 3,500 f/s against a live venue to watch the stop fire
would be reckless. It is recorded here rather than left implicit.

### Two operational findings for the tranche

1. **Expect N−1 ticks.** The 4th tick fell **0.365 s** past the last frame and
   was correctly excluded. A 10,800 s session should be planned for ~35 ticks,
   not 36.
2. **The 1 Hz feature/label extractor does not exist yet.** `research_rows_emitted:
   36` counts *panel memberships* (3 ticks × 12), not 1 Hz research rows. The
   sampling contract is validated; the row builder that turns a panel into M0/M1
   features and `Δmid` labels is the next build, and no tranche should start
   before it exists — otherwise the tape would be collected without the thing
   that consumes it being proven.
