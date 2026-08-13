# CRYPTO-COVERAGE-REPAIR-002 — prospective sparse observation

Status: **implemented on branch `crypto-coverage-repair-002`. NOT MERGED, NOT
PUSHED, NOT DEPLOYED, NOT ENABLED.** The feature flag
(`ENABLE_CRYPTO_SPARSE_OBSERVATION`) is default-OFF in code; off is a clean
no-op. No systemd unit is written, installed, or armed by this milestone. No
migration. Nothing on EVO-X2 was read, copied, or touched.

Branch base: `main` @ `98cd506`.

---

## 1. Why this exists — the measurement that justifies it

Retrospective reconciliation (CRYPTO-COVERAGE-REPAIR-001) is finished, and its
own numbers closed the door on doing more of it.

| production measurement | value |
|---|---|
| finalized survival outcomes | 1,182 |
| carrying a real 24h observation | **54 (4.57%)** |
| classified `permanently_missing_evidence` | 1,026 (86.8%) |
| coverage — 15m | 80.9% |
| coverage — 1h | 81.1% |
| coverage — 6h | **16.8%** |
| coverage — 24h | **4.6%** |
| median last tick, relative to birth | **~83 minutes** |
| recoverable pool after one bounded pass | 1,043 → ~106, against an ~11,516-row backlog that is ~99% permanent write-offs |

The cliff sits between 1h and 6h. It is not pruning and it is not
reconciliation capacity — the evidence was never **collected**. Tokens simply
stop being observed roughly 83 minutes after birth, because
`crypto_scout._scan_once_unguarded` draws only from
`fetch_latest_token_profiles()` / `fetch_latest_boosted_tokens()`: ticks stop
the moment DexScreener stops promoting the token.

Reconciling harder cannot fix a denominator that was never written down. So
this milestone builds the other half:

```
birth → scheduled 6h observation → scheduled 24h observation → reconciled outcome
```

The reconciler being deployed-but-disarmed on EVO (units installed at
`98cd506`, timer deliberately not enabled pending a calibrated
`initial_per_token_cost_seconds`) makes this the live path to new evidence, not
a parallel one.

---

## 2. Design

### 2.1 Shape

One standing **rolling** cohort in the existing `crypto_horizon_cohorts` table,
marked `provenance["membership"] = "rolling_prospective_sparse"`. Every
scheduled pass does, in order:

```
gate → overlap flock → MarketOps health → resolve standing cohort →
enrol (DB only) → plan (pure) → FETCH (network, no DB write) →
WRITE (batched commits, no network)
```

### 2.2 What is reused, and what is not

**Reused unchanged** (constraint: *do not write a second scheduler*):

| reused | from | how |
|---|---|---|
| the pure planner | `crypto_horizon.plan_observations` | single source of target/window truth |
| pair selection | `crypto_horizon.select_pair` (`POLICY_QUALITY`) | deterministic highest-quality eligible pair |
| candidate diagnostics | `crypto_horizon.describe_pair` | compact per-candidate audit |
| record + honest-miss semantics | `CryptoHorizonService._record_observation` | writes the tick + audit row, or the typed miss |
| eligibility predicate | `crypto_tape._completeness_reason` | the same rule `--require-complete` and the anchor feed already use |
| MarketOps health abort | `crypto_tape._reconciliation_should_abort` | the concept reused literally, not reimplemented |
| overlap-flock mechanism + lock dir | `crypto_tape._resolve_lock_dir` | inherits the suite's lock-isolation fixture |
| tables | `crypto_horizon_cohorts` / `_cohort_members` / `_observations` | **no migration** |
| unique indexes | `ix_horizon_member_cohort_token`, `ix_horizon_obs_cohort_token_horizon` | idempotency enforced by the DB, not by bookkeeping |

**Two default-inert parameters were added to the shared lane** so the reuse is
real rather than a copy:

* `plan_observations(..., horizons=None, window_minutes=None)` — the sparse lane
  plans only 6h/24h at an absolute band. Both default to the OBS-001 behaviour
  byte-for-byte (pinned by `test_shared_planner_default_behaviour_is_unchanged`,
  mutation M23).
* `_record_observation(..., audit_candidate_limit=12, tick_source="crypto-horizon-obs")`
  — the sparse lane stores 3 candidates and stamps its own tick source.

**Deliberately NOT reused: `observe_once`'s transaction shape.** It interleaves
`session.add`/`flush` with `await fetch_pairs_for_token` and commits once at the
end. At a manual 25-token cohort pass that is tolerable; at this lane's cadence
and token count it would hold SQLite's write lock across tens of seconds of
network I/O on a shared host — precisely the single-transaction shape OPS-013
retired and CRYPTO-COVERAGE-REPAIR-001 spent five review rounds on. This lane
therefore splits the pass into a **fetch phase that opens no transaction at
all** and a **write phase that touches no network**. See §5.

### 2.3 The two schedule numbers (CHOSEN POLICY, not measured)

```python
SPARSE_BAND_MINUTES    = 60.0   # absolute half-width around each horizon target
SPARSE_CADENCE_MINUTES = 60.0   # period of the host timer
```

Neither is measured on the target host and neither claims to be. They are
chosen *together*, and two invariants — asserted at import and pinned by test —
make the pair safe:

1. **BAND CONTAINMENT.** `SPARSE_BAND_MINUTES <= min(horizon_minutes ×
   HORIZON_TOLERANCE)` over the sparse horizons — 60 min against 180 min (6h)
   and 720 min (24h), a 3× and 12× margin. A tick written inside the sparse
   band is therefore **always** inside `compute_survival`'s tolerance window and
   can actually mature the label it was bought for. Violating it makes the
   module refuse to import (mutations M2, M20).
2. **MISSED-PASS TOLERANCE.** A closed interval of length `2×BAND` contains at
   least `floor(2×BAND / CADENCE)` points of a `CADENCE`-spaced lattice. At
   120/60 that is **2 scheduled passes inside every band**, so the lane still
   observes after one missed pass (unit failure, reboot, a
   `marketops_degraded` skip). Checked exhaustively against a real lattice in
   `test_at_least_two_scheduled_passes_fall_inside_every_band`, not just by the
   arithmetic (mutations M3, M3c).

A consequence worth stating: because the band is ±60 min rather than the tape's
±3h/±12h, an observation lands close to the *target*, not merely somewhere
inside the survival window. The report carries `target_distance_seconds`.

### 2.4 Why only 6h and 24h

15m and 1h production coverage is already **80.9% / 81.1%** — the background
scout observes densely for the first ~83 minutes and then stops. Buying
observations there is spend against a denominator that is nearly full. The
cliff is 6h (16.8%) and 24h (4.6%). Pinned by `test_only_6h_and_24h_are_bought`.

---

## 3. The eligibility rule, and its justification

```python
eligible(birth, now) ⟺
    birth.chain == crypto_chain
    and anchor := coalesce(first_evidence_at, observed_at) is not None
    and _completeness_reason(birth, 0.0) is None
    and now <= anchor + 24h + SPARSE_BAND_MINUTES
```

Enrolment is **oldest-anchor-first**, bounded on both sides by the enrolment
window, and excludes tokens already enrolled.

### Rule 1 — COMPLETE LIFECYCLE ANCHOR. Justified from deployed code, not intuition.

`CryptoLifecycleTapeRecorder.compute_survival` gates every horizon label on:

```python
if initial_liquidity and nearest.liquidity_usd is not None:
    ...
else:
    details["horizons"][label] = "liquidity_unmeasurable"
```

`initial_liquidity` is `birth.initial_liquidity_usd`. A birth whose initial
liquidity is NULL or ≤ 0 can therefore **never** produce a survival label at
*any* horizon, no matter how many observations are bought for it. Observing such
a birth is pure provider spend with a provably zero denominator gain.

`_completeness_reason(birth, 0.0)` is the exact predicate that already backs
`--require-complete` (CRYPTO-HORIZON-COHORT-SELECT-001) and the anchor feed's
completeness accounting — a reuse, not a new rule. Its rejection reasons
(`invalid_pair`, `missing_initial_price`,
`liquidity_or_initial_state_missing`, `null_initial_liquidity`) are reported per
pass in `enrolment_rejections`.

Mutation M1: removing this rule fails
`test_incomplete_lifecycle_anchor_is_never_enrolled`.

### Rule 2 — AT LEAST ONE BAND STILL REACHABLE.

A birth whose 24h band has already closed (`anchor + 24h + 60min < now`) has no
observable horizon left. Enrolling it could only ever manufacture scheduling
misses, corrupting the observation denominator with tokens the lane never had a
chance to observe. A birth past its **6h** band but inside its 24h band **is**
enrolled — losing one horizon must not cost the other
(`test_a_birth_past_its_6h_band_is_still_enrolled_for_its_24h_band`).

Mutation M22: removing this rule fails
`test_a_birth_whose_last_band_has_closed_is_never_enrolled`.

### What is deliberately NOT an eligibility rule

No liquidity floor, no volume floor, no risk score, no launchpad venue, no boost
state, no "interesting token" filter. The denominator this lane exists to repair
must stay the *whole eligible birth population* — any quality filter would make
the observed sample a selected one and reintroduce, on the collection side, the
exact bias the post-drain forecast baseline had to spend a milestone removing on
the scoring side. A 1-dollar initial liquidity is eligible
(`test_eligibility_applies_no_liquidity_or_quality_threshold`).

### Expected volume against measured arrivals

Measured births/day on EVO (CRYPTO-COVERAGE-REPAIR-001 B7, 2026-08-11,
`crypto_token_birth_events.observed_at`):

| window | births/day |
|---|---|
| 14d | 392.6 |
| 7d | 417.3 |
| 3d | 441.3 |
| 24h | 517.0 |

Task-supplied planning figures: ~400–500/day, p95 425, planning rate ~530.

| quantity | steady state | shipped bound | margin |
|---|---|---|---|
| enrolments per hourly pass | ~22 (530/24) | `enrol_limit=200` | **9×** |
| observations per hourly pass | ~44 (2 per birth; 6h and 24h bands never overlap) | `observe_limit=100` | **2.3×** |
| DexScreener requests/day | ≤ 1,060 (**0.74 rpm**) | endpoint documented at 300 rpm | **~400×** |
| SolanaTracker requests/day | **0** | denied by policy | n/a |

`observe_limit` is also the per-pass DexScreener request cap; it may never
exceed the horizon lane's own hard `OBSERVE_MAX_CALLS = 100`, and a larger value
is refused (`invalid_observe_limit`).

**Not measured, and stated as such:** the *complete-anchor fraction* of
production births. It is unknown from here (no production access was used), so
every figure above assumes the worst case — that 100% of births are eligible.
The real enrolment and request rates are bounded above by these numbers, never
below.

**Cold start.** The first pass after activation sees up to ~25h of eligible
backlog (~550 births). `enrol_limit=200` drains it in ~4 passes, oldest-anchor
first, so the tokens nearest to losing a band are admitted first.

### DB growth (stated, not solved)

At ~530 births/day: ~193k member rows/year and ~387k observation rows/year.
Observation `raw_payload` is capped at **3** candidate diagnostics rather than
the manual lane's 12 (RAW-PAYLOAD-STORAGE-001: raw payloads were 27% of the
production DB with zero readers), measured at **< 4 KB serialized** against a
20-candidate fixture. Ticks written by this lane are ordinary
`crypto_price_ticks` and are pruned at `crypto_retention_days`; the observation
rows are **not** pruned, deliberately — the observation-coverage record must
outlive the perishable evidence it describes. This lane adds no retention
policy; the EVO DB is already past its 3072 MB gate and that remains an open,
separately-owned problem.

---

## 4. Exactly one attempt per (token, horizon)

The constraint was "one each — not a window, not a retry storm, not a polling
loop."

`plan_observations` treats only an `observed` row as terminal and leaves a
**missed** row retryable in place. That is correct for a manual cohort pass an
operator re-runs deliberately; it is wrong for a lane that fires hourly, where a
token with no provider pair would be re-fetched at every pass for its whole
band (6 passes at 6h, 24 at 24h). So the sparse planner feeds the shared planner
an **empty** `existing` map — using it purely for window arithmetic — and
applies its own one-shot rule on top: **any** existing observation row for a
(token, horizon) is terminal.

Consequences, all pinned by test:

* provider spend per birth is bounded at exactly **two** requests;
* a miss is terminal and honest (`test_a_miss_is_terminal_and_is_never_retried`,
  mutation M4);
* a band that closes unobserved becomes a permanent, reported
  `scheduling_miss` and is **never** backfilled
  (`test_a_band_that_closes_unobserved_is_never_backfilled`);
* no interpolation and no nearest-tick substitution exist anywhere: the module
  never reads `crypto_price_ticks`. An observation is a fresh fetch inside the
  band or it does not exist. `due_fetch_order` only ever selects `due_now`, so a
  closed band is structurally unreachable.

---

## 5. Governance

| control | behaviour |
|---|---|
| **default OFF** | `enable_crypto_sparse_observation=False`. Off is a clean no-op: no read, no write, no call (`test_flag_off_is_a_clean_no_op`, mutation M12) |
| **dry run** | enrols nothing, calls nothing, writes nothing; reports exactly what it *would* enrol and observe, planning over the real member set plus transient stand-ins for the births it would admit (mutation M11) |
| **force** | one attended pass while the flag is off; the result carries `gate_bypassed` |
| **MarketOps health** | `_reconciliation_should_abort` reused literally. A degraded host aborts before any fetch or write; a dry run is exempt (mutation M7) |
| **overlap flock** | non-blocking, per-chain, own filename (`.crypto-sparse-observe-{chain}.lock`) so it never blocks the reconciler. A second instance is refused `skipped_overlap` |
| **bounded** | `enrol_limit`, `observe_limit`, `write_batch_size`, `max_duration_seconds`; every invalid bound is a typed refusal, never a silently-coerced green pass that does no work (`test_every_invalid_bound_is_refused_loudly`) |
| **typed failures** | `disabled`, `dry_run`, `ok`, `partial`, `skipped_overlap`, `lock_unavailable`, `marketops_degraded`, `db_locked`, `ambiguous_cohort`, `concurrent_write_conflict`, `provider_policy_violation`, `invalid_*`. Only `dry_run`/`ok`/`partial` exit 0 |
| **lock retry** | the tape's bounded `DB_LOCKED_MAX_ATTEMPTS`/`DB_LOCKED_RETRY_SECONDS` ladder per batch commit; a persistent lock yields `db_locked` with the already-committed batches intact |
| **no arming** | the standing cohort is refused by `build_arm_plan` — the choke point for ARMING specifically (`HorizonOrchestrator.arm` and the `crypto-horizon-arm-cohort` CLI that calls it), *not* for `build_schedule_report`/`build_reminder_plan`, which do not pass through it and are read-only anyway (mutation M8). The compensating half is tested too: a frozen cohort is still armable |

### Transaction shape

The fetch phase opens **no transaction**; the write phase touches **no
network**. This is pinned against a real event log built from a SQLAlchemy
`after_flush`/`after_commit` listener interleaved with the adapter's own call
log:

* no `commit` between the first and last provider call;
* no `write` between the first and last provider call;
* no provider call after the first write;
* ≥ 2 commits in the write phase (bounded batches, not one transaction).

Mutations M5 (batching collapsed) and M5b (a write injected into the fetch
phase) both fail it.

### The working set is bounded by the enrolment window, not by cohort size

The standing cohort is rolling: at the measured ~530 births/day it accrues
~193k members and ~387k observation rows per year. The first implementation
loaded the **entire** cohort in both the plan query and the write phase, so an
hourly job would have got measurably slower every day, forever — the same
unbounded-growth failure class as `_universe`'s recency starvation, and exactly
what CRYPTO-COVERAGE-REPAIR-001's capacity-vs-arrival discipline exists to
catch. It was found on re-read, before it shipped.

A member's last band closes at `anchor + 24h + BAND`. Anything older has nothing
due and never will, so both queries now exclude it **in SQL**, not in Python
after loading it. The working set is bounded at roughly
`arrival_rate × 25h ≈ 550` rows whatever the cohort's lifetime size.

Pinned by a SQLAlchemy `load` listener asserting exactly **1 of 101** members is
materialised (mutation M27), plus a convergence test that a member past both
bands drops out of the plan permanently rather than being re-walked every pass.

### Idempotency and restart survival — proven, not argued

Enforced by the database, not by bookkeeping:
`ix_horizon_member_cohort_token` makes double-enrolment impossible;
`ix_horizon_obs_cohort_token_horizon` makes double-observation impossible.

`test_a_pass_killed_mid_cycle_converges_with_no_duplicates` actually kills a
pass mid-write-phase (a `KeyboardInterrupt` raised from inside
`_record_observation` after the first batch has committed), then runs a fresh
pass and asserts: the committed batch survived, the second pass enrolled
nothing, fetched exactly the remainder, and every (token, horizon) key is
unique. Mutation M17 (no intermediate commits) fails it.

---

## 6. Two coverage surfaces, structurally separated

Conflating them is how production's real 4.57% 24h coverage stayed invisible for
months, so the separation is structural, not stylistic.

| | **observation coverage** (new) | **reconciliation coverage** (existing) |
|---|---|---|
| question | *did we look?* | *could we score it?* |
| command | `crypto-observation-coverage-report` | `crypto-tape-coverage-report` |
| builder | `crypto_sparse_observation.build_observation_coverage_report` | `crypto_coverage.build_coverage_report` |
| denominator | `member_horizons_whose_band_has_closed` | birth events with a matured horizon |
| numerator | observation rows with `status=observed` | populated survival labels |
| metric names | `observation_attempt_rate`, `observation_success_rate`, `look_completion_rate`, `scheduling_miss_rate` | `coverage_*`, `outcome_measurable`, … |
| reads survival labels? | **never** | yes, that is its subject |

States in the observation report are disjoint and exhaustive:

| state | meaning | in the rate denominator? |
|---|---|---|
| `observed` | a fetch inside the band produced usable state | yes |
| `attempted_missed` | we looked; the provider had nothing usable | yes |
| `scheduling_miss` | the band closed **while the member was enrolled** and we never looked — the number that proves the mechanism ran | yes |
| `enrolled_after_band_closed` | the band had already closed when the member was enrolled — the lane never had a chance | **no** |
| `band_open`, `band_not_open_yet` | pending | **no** |

**Pending bands are excluded from every rate** — counting a still-open band as a
miss would be the same lie as counting an absent tick as an observation
(mutation M18).

### A denominator defect the smoke run caught, and review did not

The first end-to-end CLI run over local fixtures (§10.1) reported
`scheduling_miss_rate = 0.3333` at 6h. The miss was a token born 24h earlier:
eligibility deliberately admits a birth past its 6h band so its **24h** band can
still be caught (§3, Rule 2), so that member's 6h band had closed ~17h before
the lane ever saw it.

Counting it as a `scheduling_miss` inflates the one number that is supposed to
mean *"the mechanism failed to look when it could have"* with tokens that
predate enrolment — the same denominator conflation this entire milestone exists
to stop, reintroduced inside the milestone's own report. `scheduling_miss` now
requires `member.added_at <= band_end`; everything else becomes
`enrolled_after_band_closed`, excluded from every rate and reported separately.
`scheduling_miss_examples` now carries `enrolled_at` alongside `band_closed_at`
so the distinction is auditable per row.

Two tests, both mutation-proven, including the compensating half — an exclusion
that also swallowed *real* misses would be worse than the original bug (M25,
M26).

Three tests keep them apart: no metric name is shared, no `coverage_*` name
appears in the observation report, no `observation_*`/`look_*`/`scheduling_miss*`
name appears in the reconciliation report (mutation M16); and adding a
`CryptoTokenSurvivalOutcome` row must not change a single number in the
observation report (mutation M19).

---

## 7. DexScreener only — SolanaTracker structurally unreachable

Not "nobody calls it". The fetch phase runs inside a run-scoped
`ProviderPolicy`:

```python
ProviderPolicy(
    allowed=frozenset({Provider.DEXSCREENER}),
    denied=frozenset(p for p in Provider if p is not Provider.DEXSCREENER),
    caps={Provider.DEXSCREENER: len(due_tokens)},
    paid_confirmed=frozenset(),
)
```

`deny` wins over everything in `ProviderPolicy.authorization`, and
`guard_provider_request` runs at the lowest level inside
`DexScreenerAdapter._get` — *before* a client is constructed or a socket opened.
So a SolanaTracker, GoPlus or Birdeye request from anywhere inside the pass
raises `ProviderDeniedError` rather than reaching the network.

Four independent checks:

1. `test_solana_tracker_is_structurally_denied_from_this_path` — an adapter that
   actually attempts SolanaTracker mid-pass gets `ProviderDeniedError`;
   `solana_tracker_calls == 0` and `denied_provider_attempts ==
   {"solana-tracker": 1}`, both derived from the guard's own ledger, never
   hardcoded.
2. `test_a_provider_policy_violation_is_a_loud_typed_refusal` — the fetch loop
   re-raises `ProviderPolicyError` ahead of its broad handler, so a denial can
   never be degraded into an ordinary provider miss (mutation M13). The pass
   fails non-zero with `provider_policy_violation` and writes nothing.
3. `_fetch_phase` asserts at runtime that no non-DexScreener provider carries an
   `authorized`/`started`/`succeeded`/`failed` ledger count. A `blocked_policy`
   count is allowed and reported — that is the denial working.
4. `test_the_real_adapter_reaches_only_dexscreener_urls` — an end-to-end pass
   with the **real** `DexScreenerAdapter` and `httpx` intercepted: every
   requested URL starts with `https://api.dexscreener.com/token-pairs/`, and the
   pass succeeds — which also proves the policy still *authorizes* the one
   provider it needs (a policy that denied everything would be trivially safe
   and useless).

Mutation M24 (policy widened to allow everything) fails three of these.

`test_the_module_references_no_paid_provider_identifier` checks AST identifiers,
not raw text — this document and the module's own boundary docstrings name
"SolanaTracker" and "wallets" in prose, which is the AGENTS.md-acceptable kind
of hit; an identifier is not.

**No new network surface.** The lane calls exactly one adapter method that
already existed — `DexScreenerAdapter.fetch_pairs_for_token`, one address per
request. Batched 30-address fetching (floated in
CRYPTO-COVERAGE-REPAIR-001 Stage 2) was **not** built: at 0.74 requests/minute
against a 300 rpm endpoint it buys nothing and would add a new endpoint shape to
review.

---

## 8. What this mechanism deliberately does NOT do

* **No canary.** No cohort arming, no one-shot timers, no orchestrator manifest,
  no CANARY-005. The standing cohort is explicitly refused by `build_arm_plan`.
* **No second scheduler.** No new planner, no new window arithmetic, no new
  observation table, **no migration**.
* **No interpolation, no nearest-tick substitution, no backfill.** The module
  never reads `crypto_price_ticks`. Absent evidence stays absent.
* **No retries.** One attempt per (token, horizon), ever.
* **No skip-if-a-tick-already-exists.** CRYPTO-COVERAGE-REPAIR-001 Stage 2
  proposed skipping a re-tick when in-window evidence already exists. Not built,
  on the milestone's own numbers: at 6h only 6.0% of due tokens are
  observation-covered and at 24h only 1.5%, so the skip would avoid ~1.5–6% of
  requests (of a 0.74 rpm total) while adding a DB read per token and, worse, a
  *second* notion of "covered" into a milestone whose entire point is that
  "did we look?" must be one unambiguous number.
* **No SolanaTracker, no paid provider, no new provider budget.**
* **No reconciliation.** This lane never computes, updates or reads a survival
  label. Whether the tick it bought matured a label is the reconciler's job and
  the tape coverage report's number.
* **No retention policy for observation rows.** Stated as growth (§3), not
  solved here.
* **No automatic activation.** Default-OFF; no unit file is written or
  installed by this milestone.

---

## 8.1 Three defects found after the first green suite

Recorded because the *way* each was found matters more than the fix.

1. **Two tests that could not fail for the reason they existed** (§10, M14/M21)
   — found by the mutation loop, not by review.
2. **A denominator conflation inside this milestone's own report** (§6) — found
   by actually running the CLI over local fixtures, not by reading the code.
   Reviewing the report's *logic* would never have surfaced it; only a run with
   a token born before the lane started watching did.
3. **An unbounded working-set query** (§5) — found by re-reading the finished
   module against the project's own capacity-vs-arrival discipline.

None of the three would have been caught by "the tests pass". That is the
argument for keeping all three habits, not just the first.

---

## 9. Evidence discipline — what is measured vs. chosen

| value | status |
|---|---|
| 4.57% / 86.8% / 80.9/81.1/16.8/4.6% / ~83 min | **MEASURED on production**, inherited from CRYPTO-COVERAGE-REPAIR-001 and the task brief. Not re-measured here — no production access was used. |
| births/day 392.6 / 417.3 / 441.3 / 517.0 | **MEASURED on EVO** (CRYPTO-COVERAGE-REPAIR-001 B7, 2026-08-11) |
| complete-anchor fraction of births | **NOT MEASURED — open.** Every volume figure assumes the worst case (100% eligible) |
| `SPARSE_BAND_MINUTES = 60`, `SPARSE_CADENCE_MINUTES = 60` | **CHOSEN POLICY**, derived from the two stated invariants; not measured on any host |
| `enrol_limit=200`, `observe_limit=100` | **CHOSEN**, with margins stated against the measured arrival rate |
| `max_duration_seconds=90.0` | **CHOSEN**, not measured on the target host. ~0.9 s/request at the 100-request cap against a 300 rpm endpoint. A real per-request latency measurement on EVO is an activation prerequisite |
| `write_batch_size=25` | **CHOSEN.** Unlike `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` this is *not* the write-lock safety argument — the fetch phase holds no transaction at all, so this bounds only small network-free INSERT batches (≤ 50 rows per commit) |
| audit payload < 4 KB | **MEASURED on a local fixture** (20 candidates), labelled as such. Not a production byte prediction |

---

## 10. Mutation proofs

Every behavioural claim above has a test that was **proven to fail on revert**.
Each mutation was applied, the named test run, then reverted and the suite
re-confirmed green (`__pycache__` cleared between reverts).

| # | mutation | result |
|---|---|---|
| M1 | completeness eligibility rule removed | `test_incomplete_lifecycle_anchor_is_never_enrolled` FAILS |
| M2 | `SPARSE_BAND_MINUTES` 60 → 200 | import assertion (1) fires — fail-closed |
| M2b | …and the import assertion also removed | `test_band_is_contained_...` FAILS |
| M3c | `SPARSE_CADENCE_MINUTES` 60 → 90, import assertion removed | `test_at_least_two_scheduled_passes_...` FAILS |
| M4 | misses become retryable (manual-lane semantics) | `test_a_miss_is_terminal_and_is_never_retried` FAILS |
| M5 | write batching collapsed to one commit | `test_no_write_transaction_is_held_across_network_io` FAILS |
| M5b | a write injected into the fetch phase | same test FAILS |
| M6 | enrolment ordering newest-first | `test_enrolment_is_oldest_anchor_first` FAILS |
| M7 | MarketOps health abort removed | `test_a_degraded_marketops_run_aborts_before_any_write` FAILS |
| M8 | rolling-cohort arm refusal removed | `test_a_rolling_cohort_can_never_be_armed` FAILS |
| M9 | audit candidate limit 3 → 12 | `test_the_audit_payload_is_bounded_to_three_candidates` FAILS |
| M9b | `audit_candidate_limit` parameter ignored in `crypto_horizon` | same test FAILS |
| M10 | `scheduling_miss` merged into `attempted_missed` | 2 report tests FAIL |
| M11 | dry-run branch removed | `test_dry_run_enrols_nothing_...` FAILS |
| M12 | default-OFF gate removed | `test_flag_off_is_a_clean_no_op` FAILS |
| M13 | `ProviderPolicyError` swallowed as a provider miss | `test_a_provider_policy_violation_is_a_loud_typed_refusal` FAILS |
| M14 | already-enrolled exclusion removed | **SURVIVED** → test strengthened, see below |
| M15 | `observe_limit` not enforced | `test_the_observe_limit_defers_rather_than_dropping` FAILS |
| M16 | observation metric renamed `coverage_rate` | `test_..._share_no_metric_name` FAILS |
| M17 | no intermediate commits | `test_a_pass_killed_mid_cycle_converges_...` FAILS |
| M18 | pending bands counted in the denominator | `test_pending_bands_are_excluded_from_every_rate` FAILS |
| M19 | report starts reading survival labels | `test_the_observation_report_reads_no_survival_label` FAILS |
| M20 | 15m/1h added to the sparse horizons | import assertion (1) fires — fail-closed |
| M21 | tick written for a miss | **SURVIVED** → test strengthened, see below |
| M22 | unreachable-band births become eligible | `test_a_birth_whose_last_band_has_closed_...` FAILS |
| M23 | `plan_observations` default no longer all four horizons | `test_shared_planner_default_behaviour_is_unchanged` FAILS |
| M24 | provider policy widened to allow everything | 3 provider tests FAIL |
| M25 | `enrolled_after_band_closed` merged back into `scheduling_miss` | `test_a_band_that_closed_before_enrolment_is_not_a_scheduling_miss` FAILS |
| M26 | the exclusion widened so it swallows real misses too | `test_a_band_that_closed_after_enrolment_IS_a_scheduling_miss` FAILS |
| M27 | member query no longer bounded by the enrolment window | `test_the_pass_working_set_is_bounded_by_the_enrolment_window` FAILS |

### Two tests that could not fail for the reason they existed

Both were found by the mutation loop, not by review, and both are the failure
classes this project's own evidence discipline names.

* **M14 — a test that could not distinguish success from a caught crash.**
  `test_rerunning_the_pass_double_enrols_and_double_observes_nothing` asserted
  only that row counts were unchanged and `enrolled == 0`. With the
  already-enrolled exclusion removed, the second pass raises `IntegrityError`,
  is caught, rolled back into `concurrent_write_conflict` — and *every original
  assertion still passed*. It now asserts the second pass is **healthy**
  (`status == ok`, `due_observations == 0`), not merely harmless. Fails on
  revert.
* **M21 — a fixture that exercised only one of two branches.**
  `test_no_tick_is_written_for_a_miss` used a provider returning no pairs at
  all, so it never reached the "pairs exist but none eligible" branch. It is now
  parametrized over both miss shapes and additionally pins `tick_id` and
  `liquidity_usd` NULL on the observation row. Fails on revert (M21b: a tick
  injected into the no-pair branch).

---

## 10.1 Live smoke run (local fixtures, no network, no production data)

Run against a throwaway SQLite file in the session scratchpad, with `httpx`
intercepted so no packet leaves the machine. Six seeded births: two due at 6h,
one due at 24h, one with an incomplete anchor, one past all bands, one too
young.

```
$ crypto-sparse-observe                       # flag OFF (the shipped default)
status=disabled  external_calls=0  no-op (flag enable_crypto_sparse_observation is off)
exit=0

$ crypto-sparse-observe --dry-run
status=dry_run  external_calls=0  provider=dexscreener  solana_tracker_calls=0
cohort_id=None  cohort_created=False  births_considered=5
enrolment_rejections={'incomplete_lifecycle_anchor': 1}
would_create_cohort=True  would_enrol=4  due_observations=3  would_fetch_tokens=3
plan_status_counts={'6h': {'overdue_unobserved': 1, 'due_now': 2, 'not_due': 1},
                    '24h': {'due_now': 1, 'not_due': 3}}
nothing was enrolled, fetched, or written
exit=0
```

Row counts after the disabled pass, the dry run **and** the coverage report:
`crypto_horizon_cohorts=0, _cohort_members=0, _observations=0,
crypto_price_ticks=0`. The 30h-old birth never even reaches the rejection
histogram — the enrolment window excludes it in SQL, which is why
`births_considered` is 5 and not 6.

Then one attended `--force` pass with the **real** `DexScreenerAdapter`:

```
status=ok  external_calls=3  provider=dexscreener  solana_tracker_calls=0  gate_bypassed=force
cohort_id=1  cohort_created=True  enrolled=4  due_observations=3
observations_recorded=3  ticks_written=3  outcome_counts={'observed': 3}
batches_committed=1  deferred=0  stop_reason=complete
provider_ledger={'dexscreener': {'authorized': 3, 'started': 3, 'succeeded': 3,
                                 'failed': 0, 'blocked_policy': 0, 'skipped_cap': 0}}

URLs requested:
  https://api.dexscreener.com/token-pairs/v1/solana/So0003TTTT...
  https://api.dexscreener.com/token-pairs/v1/solana/So0001TTTT...
  https://api.dexscreener.com/token-pairs/v1/solana/So0002TTTT...
```

Three requests, one host, one endpoint. `target_distance_seconds` came out
`p50=64.7, max=784.7` against a `band_half_width_seconds=3600` — observations
landing within ~13 minutes of target, comfortably inside both the sparse band
and the tape's own tolerance.

This run is also what surfaced the `scheduling_miss` denominator defect in §6.

---

## 10.2 Full-suite validation

```
4,320 collected
5 failed, 4,305 passed, 6 skipped, 4 xfailed, 3 warnings in 700.75s (11:40)
load average at start 5.64 / 8.55 / 11.79   at end 3.31 / 4.44 / 7.52
```

All 5 failures are in the **known wall-clock-sensitive cluster** named in this
project's own testing notes, and none is a regression from this branch:

| failing test | file touched by this branch? |
|---|---|
| `test_crypto_horizon_obs_001::TestCohort::test_window_filters_on_first_evidence_not_observed_at` | no |
| `test_crypto_horizon_obs_001::TestCohort::test_hours_1_never_returns_token_older_than_60_minutes` | no |
| `test_crypto_horizon_obs_001::TestCohort::test_timezone_naive_first_evidence_handled` | no |
| `test_live_market_001::TestEndToEnd::test_market_freshness_measured` | no |
| `test_live_market_001::TestEndToEnd::test_volatile_market_surfaces_in_examples` | no |

Re-run in isolation: `test_crypto_horizon_obs_001.py` **27 passed**,
`test_live_market_001.py` **28 passed**, and the 5 named tests together
**5 passed**.

**The mechanism was reproduced deterministically rather than assumed.** These
tests bind `NOW = datetime.now(timezone.utc)` at module import and then seed a
row at `NOW - 59 minutes` against a live `--hours 1` filter that uses the real
clock. Importing the module, sleeping 90 seconds, and then running the single
test reproduces the exact failure (`members_selected == 2`, not 3) with no code
change at all — the 59-minute token is simply 60.5 minutes old by the time the
assertion runs. In a 700-second suite the module is imported minutes before the
test executes, so the boundary row falls out. `git diff main..HEAD` touches
neither test file, and the only change this branch makes anywhere near
`create_cohort` is one extra `provenance["membership"]` key plus the
`audit_candidate_limit` default — neither can move `members_selected`.

This is a pre-existing fixture-drift defect worth its own fix (bind the clock
per test, not per module), but fixing it is not in this milestone's scope and
would be a change to two unrelated lanes.

### One invalidated run, recorded

A first full-suite attempt on this tree was **discarded, not reported**: the
host filled to 0 bytes free mid-run (a 228 GiB volume already at 192 GiB used
before this session, plus 2.4 GiB of accumulated `pytest-of-*` tmp directories),
and from ~17% onward essentially every test errored on `ENOSPC`. Those errors
were an artefact of the host, not of the tree, and a run in that state cannot
distinguish the two — so it was thrown away, the tmp directories were cleared
(freeing 4.9 GiB), and the suite was re-run clean from the start. The numbers
above are from that clean run, with disk headroom monitored throughout
(4.9 GiB at start, 4.3 GiB at the low point).

---

## 11. Surface added

* `app/services/crypto_sparse_observation.py` — the mechanism and the
  observation-coverage report.
* `app/services/crypto_horizon.py` — two default-inert parameters on
  `plan_observations` and `_record_observation`; `MEMBERSHIP_FROZEN` /
  `MEMBERSHIP_ROLLING` / `is_rolling_cohort`.
* `app/services/crypto_horizon_orchestrator.py` — `build_arm_plan` refuses a
  rolling cohort (`rolling_cohort_not_armable`).
* `app/config.py`, `app/canon.py`, `.env.example`, `docs/FEATURE_FLAGS.md` — the
  flag and its four bounds.
* `app/cli.py` — `crypto-sparse-observe` (`--dry-run`, `--force`,
  `--enrol-limit`, `--observe-limit`, `--write-batch-size`,
  `--max-duration-seconds`) and `crypto-observation-coverage-report`.
* `tests/test_crypto_coverage_repair_002.py` — 57 tests.

No migration. No systemd unit. No provider adapter change.

---

## 12. Activation order (NOT performed)

1. Deploy dark at the default-OFF flag; confirm `crypto-sparse-observe` prints
   `status=disabled external_calls=0`.
2. `crypto-sparse-observe --dry-run` on the target host — read
   `births_considered`, `enrolment_rejections`, `would_enrol`,
   `due_observations`. This is where the **complete-anchor fraction** (§9, open)
   gets measured for the first time.
3. One attended `crypto-sparse-observe --force --observe-limit 5` — measure real
   per-request latency and pass wall time, and set
   `CRYPTO_SPARSE_OBSERVATION_MAX_DURATION_SECONDS` from it rather than from the
   chosen 90.0.
4. `crypto-observation-coverage-report` — confirm the denominator is what step 2
   predicted and that `scheduling_miss` is 0.
5. Only then flip the flag and install an hourly user timer. The cadence in the
   unit **must** match `SPARSE_CADENCE_MINUTES`; invariant (2) is stated against
   that number, and nothing in the code can enforce a systemd `OnCalendar` it
   cannot see. **This coupling is the weakest joint in the design.** It is not
   unmitigated, though: a timer that is slower than `SPARSE_CADENCE_MINUTES`,
   or that stops, shows up directly as a rising `scheduling_miss_rate` in the
   observation-coverage report — that metric exists precisely to detect "the
   mechanism failed to look when it could have", and after the
   `enrolled_after_band_closed` split (§6) it means nothing else. Watch it; a
   sustained non-zero value at 6h is a cadence problem, not a provider problem.
   What is *not* mitigated is the first pass after a misconfiguration: nothing
   fails loudly at install time, and the signal only appears one band later
   (≥7h at 6h, ≥25h at 24h).
6. Re-measure DB growth against the §3 projection after 7 days.

Free denominator first (the reconciler), purchased observations second — but the
reconciler's timer is disarmed pending its own calibration, so in practice this
lane is the live path to new evidence and should be watched on its own merits.
