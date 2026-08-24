# The 4/4/4/4/4 TTE schedule is currently unsatisfiable — decision required

**Found 2026-08-23, before any confirmation capture. No alpha was inspected.**

The instruction was to freeze a balanced four-sessions-per-bin schedule across
the five frozen TTE bins. **It cannot be frozen as specified**, because two of
the five bins are unreachable under the eligibility rule already frozen in
`MARKET-MICROSTRUCTURE-EDGE-001` Amendment 2 §B.

## The contradiction

Eligibility requires `TTE > 600 s`. The bins are defined on the same `TTE`:

| bin | TTE range | eligible sub-range | width |
|---|---|---|---:|
| `far` | > 21,600 | > 21,600 | ∞ |
| `approaching` | 7,200 – 21,600 | 7,200 – 21,600 | 14,400 s |
| `near_event` | 900 – 7,200 | 900 – 7,200 | 6,300 s |
| `live_event` | 0 – 900 | **600 – 900** | **300 s** |
| `late_resolution` | < 0 | **none** | **0 — UNREACHABLE** |

Verified against the real decision core, not by reading the constants:

```
bin=late_resolution  TTE= -1200  eligible=False  reason=tte_at_or_below_600s
bin=live_event       TTE=   300  eligible=False  reason=tte_at_or_below_600s
bin=live_event       TTE=   750  eligible=True
bin=near_event       TTE=  3000  eligible=True
```

**`late_resolution` can never produce a single row.** `live_event` admits a
300 s sliver — exactly one panel interval, and only if a tick lands precisely at
TTE = 900. Under the preregistered "≥ 3 sessions per bin" floor, and under the
proposed 4-per-bin plan, both bins fail structurally no matter how sessions are
scheduled.

## Root cause — a defect in the capture plan, not in the bins

Capture plan §1 wrote the gate as:

> `TTE(i,t) > max_horizon + embargo (= 600 s)`

That conflates **event proximity** with **label availability**. The constraint
that actually protects a label is that its endpoint lies inside the observed
session with a published mid — and `labels.py` already enforces exactly that,
independently, via `REASON_PAST_SESSION_END` and `REASON_NO_ENDPOINT`. The TTE
gate is therefore both **redundant** and **wrong**: it silently deletes the two
bins where in-play markets are most interesting, for a reason that has nothing
to do with whether a label can be computed.

A market does not stop publishing because its event started. Live in-game
markets are precisely the high-volatility states the five-bin scheme was
written to reach.

## Recommended fix (mechanical, pre-alpha) — needs Eric's decision

Replace the TTE horizon gate with the constraint that was always meant:

```text
-  TTE(i,t) > max_horizon + embargo          (600 s)
+  session_end - t > max_horizon + embargo   (600 s)
```

and let `TTE` range freely. Then:

* all five bins become reachable, and the 4/4/4/4/4 plan is satisfiable;
* labels remain fully protected — by the rule that actually governs them;
* nothing else in the contract moves: the activity floor, K, rotation, the
  tie-break, the safety stop and the bins themselves are untouched.

**Not applied unilaterally.** It changes which data the tranche collects, which
is a scope decision rather than a defect repair.

### The alternative, if the gate is to stay

Reduce the scheme to the three reachable bins and schedule **7 / 7 / 6** across
`far` / `approaching` / `near_event`, recording explicitly that the experiment
says nothing about in-play or post-event microstructure. That is a real loss:
those are the states most likely to carry flow information.

## One open empirical question either way

Whether Kalshi books keep publishing usefully during and after an event is
**not established by any tape we hold** — every market in both validation
sessions was 2–3 h pre-event. If the gate is relaxed, the first `live_event`
session should be treated as discovering that, and a bin that turns out to be
structurally empty at the venue should be reported as such rather than
back-filled from an easier bin.

## Everything else from the pre-tranche decisions is done

* `realized_vol_1s` removed, no replacement (fabric Amendment 3), schema at
  `v2`, and the `window_seconds × sampling_hz ≥ 2` invariant is permanent and
  tested.
* The "a session covers bin *b* only if a complete 300 s post-warmup panel
  interval lies wholly within *b*" definition is adopted below and is
  unaffected by this blocker.
* Deterministic replacement, the series-spread constraints, and
  scheduling-variable discipline are recorded in the capture plan addendum and
  are likewise unaffected.
