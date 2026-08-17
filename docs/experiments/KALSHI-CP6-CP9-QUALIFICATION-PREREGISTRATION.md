# CP6–CP9 live qualification — preregistration

**Status: PREREGISTERED. Written and committed BEFORE any live session ran.**
Nothing here was chosen after seeing venue behaviour.

Authorizes **no orders, no portfolio channels, no venue writes, no capital**.
Read-only market data only, per `docs/SAFETY_BOUNDARIES.md`.

---

## 0. The standing bar — Eric's words, binding on all four checkpoints

- **A live surprise is a FINDING, not something to patch around during the
  qualification run.**
- **A missing measurement is not zero.**
- **A disconnected metric is not healthy.**
- **A session that misses the preregistered sample floor is UNDERPOWERED, not
  "close enough".**
- **Any venue behaviour that contradicts fixture assumptions must update the
  model of the venue BEFORE qualification proceeds.**

The rationale is doctrine 7: this repo's recurring defect is *a plausible benign
value produced by a broken path*. Once microstructure research begins, that
class yields clean-looking datasets and convincing statistics rather than
obvious crashes. A qualification session built on a workaround measures the
workaround.

---

## 1. Session parameters — frozen

- **Sample floor:** ≥ **2 hours** AND ≥ **100,000 archived live frames**,
  **whichever occurs later**. **4-hour maximum** for this first run.
- **Universe:** exactly **12 live tickers**, **4 high / 4 medium / 4 low**
  message-rate strata, spanning several contract/event structures.
- Manifest frozen **before** capture, recording the stratification snapshot
  **timestamp**, the ranking **statistic**, and the **full candidate
  population** — so the sampling frame is explicit and the 12 are never
  mistaken for a representative sample of the venue.
- **No ticker may be replaced because its telemetry looks cleaner.**

---

## 2. CP6 — first live session. Deliberately narrow.

Prove, on the **actual venue**, exactly these:

1. handshake
2. subscription
3. generation stamping
4. raw-frame capture
5. normalized-frame capture
6. metrics movement (**non-zero**, per doctrine 7 — absence is not health)
7. book reconstruction

**CP6 MUST NOT quietly become the long qualification run.** If the first ~10
minutes reveal a semantic mismatch with our fixtures, **stop and report**. A
mismatch discovered later contaminates everything downstream, and a tape
captured against a wrong venue model is worse than no tape.

---

## 3. CP7 — force the failure that motivated the generation epoch

Reconnect **during an active multi-market session** and prove:

> **Each market independently reacquires publishability only after ITS OWN
> new-generation snapshot.**

**No book may silently survive across a generation boundary as if nothing
happened.** This is a positive control in the doctrine-7 sense: force the
reconnect, and require the generation to change and per-market publishability to
drop and re-acquire individually.

`KALSHI-REPLAY-GENERATION-CONSISTENCY-001` (deferred, replay-side) does not
excuse the live collector from this proof.

---

## 4. CP8 — replay integrity. "Replay completed" is NOT the test.

The acceptance criterion is **state equality**:

```
live-derived terminal state  ==  replay-derived terminal state
```

for **every market where the necessary frame sequence exists**, plus:

- **frame-count conservation**
- **generation-count conservation**

**Any state that cannot be reconstructed from the durable tape is a
QUALIFICATION FAILURE**, not a caveat — the entire future microstructure
programme depends on replayability, and a tape that cannot reproduce its own
terminal state cannot support it.

---

## 5. CP9 — four-way verdict, not pass/fail

Report exactly one of:

| verdict | meaning |
|---|---|
| **QUALIFIED** | correctness properties hold AND the sample floor was met |
| **CONDITIONALLY QUALIFIED** | correctness holds; a stated limitation bounds the claim |
| **UNDERPOWERED** | correctness holds; sample insufficient for the tail estimates |
| **FAILED** | a correctness property does not hold |

**Correctness and statistical power are different dimensions and must not be
collapsed into one bit.** A run can satisfy every correctness property and still
be underpowered for p99 latency if DEMO yields, say, 30k frames — that is
**UNDERPOWERED**, and the p95/p99 figures must be **refused**, not printed with
a caveat.

Report per §7: events/sec average, p95 and p99 burst rate, event sizes, archive
append latency, rotation frequency, archive close latency, dropped events,
backpressure/lag — and a **NOT MEASURED** section that must be non-empty and
honest.

Each of the three rotation constants gets an explicit verdict against the
measured close latency. Retuning `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` against
a real rate — rather than the "~500 events/s **assumed** peak" it currently
rests on — is the milestone's stated purpose.

---

## 6. After CP9 — the fixed order

1. generation-aware `publishable_books()`
   (`KALSHI-REPLAY-GENERATION-CONSISTENCY-001`)
2. **tape schema freeze / review**
3. preregister `MARKET-MICROSTRUCTURE-EDGE-001`
4. feature derivation

The schema freeze is a **measurement contract, not a database check**. Once
feature research begins, changing event semantics, generation treatment,
trade-direction interpretation, or book-reconstruction rules risks invalidating
every downstream result.

---

## 7. Deviations

Any departure from this document must be recorded here with reason and
timestamp **before** the affected result is reported. An unlogged deviation
invalidates the affected checkpoint.

---

## 8. AMENDMENT — rescoped to DEMO FUNCTIONAL-ONLY (2026-08-17)

**Recorded as a forward amendment, before any CP6–CP9 session runs.**

`KALSHI-DEMO-TRAFFIC-CAPACITY-001` returned **UNREACHABLE by 9.2×**, and the
scale control settled it: all **388** non-test eligible markets — 32× the
preregistered universe — reach only **55,344** projected frames. **98.3% of the
frames DEMO's eligible population emits come from 194 venue test instruments.**
DEMO is an engineering sandbox, not an empirical proxy for production activity.

**The ≥100,000-frame floor is withdrawn for DEMO**, and with it every rate,
latency-tail and capacity claim. Those move to
`KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001`. Do not keep extracting
conclusions from DEMO that it cannot provide.

### CP6 — live semantics
Does the collector correctly understand actual venue messages? Prove: channel/
sid assignments · sequence domains · subscription generations · snapshot forms ·
normalized representation · **zero unexplained recovery errors**.

### CP7 — reconnect correctness
Force reconnects. Prove `generation_after > generation_before`, and
**independently for every market**: `old book → nonpublishable → its own new
snapshot → publishable`. Also prove a genuine **within-generation gap still
faults**.

### CP8 — deterministic replay
`State_live^terminal == State_replay^terminal`, plus **raw-frame conservation**,
**normalized-frame conservation where normalization is defined**, **generation
conservation**, and **per-sid sequence findings conserved**. Where a frame class
cannot support conservation because the venue does not sequence it (`ticker`),
the report must say **explicitly what can and cannot be established** rather
than omitting it.

### CP9 — functional qualification, and nothing more

Output exactly this shape, so that "CP9 PASSED" can never be read six months
later as "collector performance was validated under realistic trading
conditions":

```
Collector semantics      QUALIFIED / FAILED
Reconnect behavior       QUALIFIED / FAILED
Archive conservation     QUALIFIED / FAILED
Replay equality          QUALIFIED / FAILED
Fault isolation          QUALIFIED / FAILED

DEMO throughput          NOT QUALIFIED
Production latency       NOT MEASURED
Production capacity      NOT MEASURED
Microstructure realism   NOT ESTABLISHED
```

The four lower lines are **not** caveats to be dropped when the top five pass.
They are the scope of the claim.

---

## 9. Deviations actually taken (2026-08-17) — §7's required record

The CP6–CP9 sessions ran on 2026-08-17. Five departures from §1–§5, each forced
by the §8 rescope or by DEMO's nature, are recorded in full in
`KALSHI-CP6-CP9-FUNCTIONAL-QUALIFICATION-REPORT.md` §6 and summarised here so
this document is not silent about them:

1. **§1's ≥100,000-frame floor** — withdrawn by §8. Three sessions totalling
   24,396 frames. No rate, tail or capacity claim is made from them.
2. **§1's "12 live tickers, 4/4/4 message-rate strata"** — not constructible:
   `KALSHI-TAPE-MANIFEST-001` REFUSED the manifest because DEMO has an empty
   middle. 60 venue test instruments were used instead — the P0 capture's set,
   frozen before capture, legitimate for a functional proof and worthless for a
   microstructure one.
3. **CP6 and CP8 share one unperturbed session**, which also serves as the
   negative control for the two perturbed ones.
4. **CP7's reconnect is forced by tearing down the real socket**, not by
   waiting for a venue-initiated disconnect. The teardown is genuine; the cause
   is ours, and whether a venue-initiated disconnect behaves identically is not
   established.
5. **CP7's live sequence gap is forced by withholding a frame**, not by
   observing a natural drop. No natural gap has ever been observed on any DEMO
   subscription.

**Outcome:** CP6 QUALIFIED, **CP7 FAILED** (per-market re-acquisition across a
generation boundary), CP8 QUALIFIED with two named non-reconstructible
collector-action counters, fault isolation QUALIFIED. The four lower lines of
the §8 block stand unchanged.

**AMENDED FORWARD 2026-08-17 — CP7 re-run live, and now QUALIFIED.** Recorded
here rather than by editing the line above, because the outcome of the sessions
that ran is not changed by a later run. `KALSHI-REPLAY-GENERATION-CONSISTENCY-
001` fixed the defect and `KALSHI-CP7-LIVE-RERUN-001` re-measured **all three
CP7 properties on the venue** — two forced teardowns, per-market re-acquisition
in 60 separate transition entries at each boundary, and a within-generation gap
that still faults and is still typed `book_halted`. A sixth deviation joins the
five above:

6. **The re-run's universe is not the original 60.** Three of the 2026-08-17
   instruments had closed, so 57 were retained in their original order and 3
   were topped up by a **telemetry-blind** rule (ticker ascending), frozen with
   its full candidate population **before** any socket opened. §1's prohibition
   — no ticker may be replaced because its telemetry looks cleaner — is
   satisfied by construction: the freeze script reads no activity field at all.

**Still not established, in either run:** the delta-refusal path
(`rejected_pre_generation_snapshot`) has **never been exercised live**. Both
times the venue sent every snapshot before any delta for the affected markets.
The re-run measures that absence rather than inferring it, and reports it as
NOT EXERCISED rather than as a pass. The four lower lines of §8 still stand
unchanged.
