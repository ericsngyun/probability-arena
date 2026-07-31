# CRYPTO-HORIZON-ORCHESTRATOR-CANARY-004 — attempt record (2026-07-24)

## Attempt 3 (2026-07-24 06:06–06:47 UTC, bounded window): NO QUALIFYING NATURAL CYCLE WITHIN BOUNDED WINDOW

```text
VERDICT: NO QUALIFYING NATURAL CYCLE WITHIN BOUNDED WINDOW
BOUNDS: 8 natural cycles inspected / 41 min elapsed (limits: 8 cycles, 45 min)
MUTATIONS: NONE (no tape run, no cohort, no arming, no unit, no provider call)
```

Third live attempt, authorized with a bounded qualification window (max 8
naturally scheduled cycles, max 45 minutes, stop at first qualifying cycle;
no manual scan). Gate 1 baseline re-verified at `3111442` (Alembic `0027`,
flag active, anchors 511, cohorts 1–6, **horizon units 0**, DB
3,681,239,040 B, 73.85 GB free, 0 lock events, readiness JSONL 1,790 lines;
telemetry JSONL 14,955 → 29,914 B — the expected 06:00 UTC hourly
tick-aggregation emission, not a canary effect).

### Bounded qualification window (Gate 2)

Window opened 06:06 UTC. Every cycle completed naturally; none was
triggered or accelerated; all eight were status `ok` with a healthy crypto
stage and exactly one scan each; database and filesystem stayed healthy
throughout; provider counters moved only with the normal governed cadence.

| Cycle | Start → finish (UTC) | New tokens | With complete local state (pair + price + liq>0) |
|---|---|---|---|
| 4887 | 06:06:03 → 06:06:46 | 1 | 1 |
| 4888 | 06:12:03 → 06:12:53 | 1 | 1 |
| 4889 | 06:17:05 → 06:17:42 | 0 | 0 |
| 4890 | 06:23:04 → 06:24:02 | 1 | 0 |
| 4891 | 06:28:07 → 06:28:42 | 2 | 0 (neither had positive local liquidity) |
| 4892 | 06:34:04 → 06:34:50 | 1 | 0 |
| 4893 | 06:40:04 → 06:40:39 | 1 | 1 |
| 4894 | 06:46:04 → 06:46:41 | 1 | 0 |

No cycle reached the qualification bar (≥2 newly persisted tokens with
complete local initial state). The window closed at the 8-cycle bound with
41 minutes elapsed; per the authorization, **no tape session ran** and
nothing downstream (cohort/arming/observation) was attempted.

### Arrival-rate evidence for the July-30 review

Across the 13 cycles directly inspected today (4878–4881, 4885,
4887–4894): new-token counts 0,0,0,3,0,1,1,0,1,2,1,1,1; only **one** cycle
(4881) carried ≥2 complete candidates. The per-cycle probability of a
compliant two-token set in a single ~6-minute discovery slot is roughly
1/13 (~8%) on today's evidence — an 8-cycle window has perhaps a 45–50%
chance of qualifying. The mechanism itself remains fully proven
(ANCHOR-FEED-CANARY-001 converted exactly such a cycle into a live
readiness pair within 6 minutes); what CANARY-004 now needs is either
patience across more windows, a wider bounded window in a future
authorization, or acceptance that the operator arms at an
ANCHOR-FEED-style live moment when one naturally arises.

State after the attempt: identical to baseline in every canary-relevant
dimension (anchors 511, tape runs 60, cohorts 1–6, horizon units 0,
forecast PRs untouched). Epochs unchanged — **no Epoch-3 intervention
occurred**; passive readiness measurement remains uncontaminated.

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
