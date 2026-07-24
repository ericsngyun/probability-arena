# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — attempt record (2026-07-24)

## Attempt 2 (2026-07-24 ~05:50–05:56 UTC): NO COMPLIANT TWO-TOKEN SET IN SELECTED NATURAL CYCLE

```text
VERDICT: NO COMPLIANT TWO-TOKEN SET IN SELECTED NATURAL CYCLE
MUTATIONS: NONE (no tape run, no cohort, no arming, no unit, no provider call)
```

Second live attempt at the shared-pass canary (the first, 2026-07-16 ~18:11
UTC, is recorded in `DEPLOYMENT_REPORT_EVO_X2.md` — it too found no compliant
pair, from an authorized governed scan). This attempt used the
ANCHOR-FEED-CANARY-001 method (single-shot natural-cycle selection + one
provider-free tape pass) under an authorization that explicitly forbade
waiting through repeated cycles and forbade any extra scan.

### Gate 1 baseline (all preconditions held)

Mac = origin = EVO-X2 = `a10faca`; tracked-clean; Alembic `0027`;
`MARKETOPS_INCLUDE_CANDIDATE_READINESS=true`. Birth anchors 511 (latest
2026-07-24T05:32:08Z, tape run 60); readiness JSONL 1,788 lines (last record
cycle 4884 — the Epoch-2 King/Octen pair still tracked pre-expiry);
cohorts 1–6 only; observations 15/6/4/–/6/4; **horizon units 0**; DB
3,681,239,040 B; 73.85 GB free (70% used — above critical); 0 lock events in
the prior hour; telemetry JSONL 14,955 B (unchanged); SolanaTracker
hour=135 / today=870 / month=59,933. King/Octen expired naturally at
05:52:34Z and were never armed (cycle 4885's readiness record correctly
returned to `expired`).

### Gate 2 — selected natural cycle: 4885 (single shot)

The next naturally scheduled cycle after baseline was **4885**
(timer 05:53:18; run 05:54:03.938 → 05:55:01.183 UTC): status `ok`, exit 0,
`crypto_scan: ok`, `stage_errors={}`, exactly one crypto scan (run 4884,
05:54:04 → 05:54:32), provider attribution normal (counters moved exactly
one cycle's worth), database healthy, no lock degradation.

**Newly persisted raw tokens in cycle 4885: 0** (38 tokens processed — all
re-seen updates of previously persisted tokens; the newest first-seen tokens
on the host predate this cycle). Plausible complete candidates: 0 (< 2
required).

Per the gate rules — membership must come from this one cycle; no waiting
through repeated cycles; no extra scan — the canary **stopped before Gate 3**
with zero mutations of any kind.

### Operational context for the July-30 review

New-token arrival per ~6-minute cycle is bursty: of the recent observed
cycles, 4878/4879/4880/4885 persisted 0 new tokens while 4881 persisted 3
(2 complete). A single-shot cycle selection therefore fails whenever the
DexScreener profile/boost feed happens to surface nothing new in that
~6-minute slot — an arrival-process property, not an orchestrator, tape,
selector, or readiness defect. Nothing in this attempt contradicts the
ANCHOR-FEED-CANARY-001 PASS: the mechanism (cycle → tape → anchors →
readiness pair) remains proven; what this attempt adds is evidence that the
*compliant-pair arrival rate per single cycle* is well under 1.

Measurement epochs remain as defined (Epoch 1: feed inactive; Epoch 2: one
anchor-feed session, live pair expired unarmed). **No Epoch-3 intervention
occurred** — this attempt changed nothing, so passive readiness measurement
continues uncontaminated.

### State after the attempt

Identical to baseline: anchors 511, tape runs 60, cohorts 1–6, observations
unchanged, horizon units 0, readiness JSONL appending normally (expired),
provider counters moving only with the normal governed MarketOps cadence, DB
byte-size unchanged, telemetry unchanged. Forecast PRs #1/#2 untouched.

A future CANARY-004 attempt requires a fresh explicit authorization; if that
authorization again uses single-shot cycle selection, expect repeated
attempts to be necessary, or authorize a bounded multi-cycle selection
window explicitly.
