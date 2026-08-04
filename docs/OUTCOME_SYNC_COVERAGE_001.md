# OUTCOME-SYNC-COVERAGE-001 — outcome coverage for matured forecasts

**Status:** implemented and reviewed; production decision in §10.

Probability Arena was evaluating forecast quality on **9.5%** of its matured
forecasts and did not know it. This milestone establishes what the real
denominator is, why the other 90.5% have no label, and what it costs to fix.

**The headline finding is that this was never a provider problem.** Two
independent selection defects meant the pipeline re-did the same work forever
instead of advancing. Both are fixed here without one additional provider call.

---

## 1. Baseline (Gate 1)

Captured 2026-08-04T06:51Z (2026-08-03 23:51 PDT). Mac = origin = EVO-X2 =
`2b5a335`, Alembic `0027`, backup freshness healthy
(`backup-20260804T013626Z.db.gz`), MarketOps `ok` on every recent cycle.

| Volume | Count |
|---|---:|
| `market_forecasts` | 12,543 |
| `markets` | 100,215 |
| `market_outcomes` | 1,790 |
| `forecast_scores` | 1,631 |
| `marketops_runs` | 7,546 |
| distinct forecasted tickers | 4,903 |

## 2. The matured denominator (Gate 2)

A forecast is **matured-eligible** when its market row exists, its close time is
known and has passed by the settlement grace, and its scoring target is known.
It is emphatically **not** excluded for lacking an outcome row — that is the
thing being measured, and using the scored population as the denominator is how
a 10% sample gets mistaken for a healthy one.

**Settlement grace = 3,600 s**, a single constant. It is deliberately short: a
shorter grace makes the denominator *larger* and coverage look *worse*, which is
the conservative direction for a metric whose purpose is to stop us overstating
how much evidence we have. `estimated_probability` is `NOT NULL` in the schema,
so "scoring target known" is structurally guaranteed rather than filtered.

| Funnel step | Count |
|---|---:|
| all forecasts | 12,543 |
| **matured eligible** | **11,346** |
| outcome row present | 1,201 |
| settled yes/no (usable) | 1,083 |
| **missing outcome** | **10,145** |
| **matured coverage** | **9.5%** |

Forecasts with *any* score row: **1,000**, spanning forecast ids **1..1000**, out
of 12,543.

## 3. Missing-outcome taxonomy (Gate 3)

Every matured forecast without a usable current outcome gets exactly one reason
from a closed set of 16. Nothing collapses into `unknown` — "unknown" is what
made this milestone necessary. The classifier is a pure function with documented
precedence: structural problems, then provider-state problems, then staleness.

On production essentially the entire gap is `sync_never_attempted`: the market
was never fetched, because the selection could not reach it. The remainder is
`market_closed_unsettled` (genuinely awaiting settlement) and a small tail of
canceled/void/missing-winner markets that are permanently unscorable.

Reasons and their recoverability are declared in one table
(`outcome_coverage._RECOVERABILITY`), and a test asserts every reason appears in
it, so a new reason cannot be added without classifying it.

## 4. Call-path audit (Gate 5)

```
MarketOps stage 5 "sync_outcomes"
  → OutcomeService.sync_known_markets(limit = MARKETOPS_SYNC_OUTCOME_LIMIT)
    → select tickers  ← DEFECT A
    → KalshiRestAdapter.get_market_detail(ticker)      (read-only GET)
    → parse_market_outcome(detail)                     (sole interpreter)
    → upsert one MarketOutcomeRecord per ticker
MarketOps stage 5b "score_forecasts"
  → CalibrationService.score_unscored(limit = MARKETOPS_SCORE_LIMIT)
    → select forecasts ← DEFECT B
    → _score_target(outcome) → append one ForecastScoreRecord
```

### Defect A — the outcome selection was a frozen alphabetical prefix

```python
select(MarketForecastRecord.market_ticker).distinct()
    .order_by(MarketForecastRecord.market_ticker)      # alphabetical
...
if len(tickers) >= limit: break                        # keep the first N
```

With 4,903 distinct forecasted tickers and a production limit of **100**, the
same ~100 alphabetically-first tickers were fetched **every six minutes,
forever** — roughly 24,000 Kalshi detail GETs per day against ~100 markets — and
the other 4,800 were **unreachable on every cycle**. This is the precise reason
coverage is ~10%, and it matters that it is a *prefix* rather than a *backlog*:
a backlog drains if you wait or raise the cap, and this never would have.

Two corroborating observations. Coverage by close age is **inverted** — 0.1% for
markets closed under 2 days, 1.3% at 2–7 days, 15.7% at 7–30 days — which is the
opposite of what a lagging-but-working sync produces. And of the ~100 reachable
markets, most already held a **terminal** outcome (settled/canceled), whose value
can never change again, so a large share of the budget bought nothing at all.

### Defect B — the scoring selection was a frozen id-ordered prefix

```python
select(MarketForecastRecord).order_by(MarketForecastRecord.id).limit(limit)
```

The LIMIT was applied *before* the already-current check, so the same oldest
1,000 forecasts were loaded every cycle and almost all immediately skipped.
Forecasts past the cap were never loaded and therefore could never be scored.
The production fingerprint is exact: **1,000 distinct scored forecasts, ids
1..1000, and nothing above** — not a slow drain, a wall.

### Defect C — an in-place yes→no flip was never re-scored

Found while testing, not from the baseline. `MarketOutcomeRecord` is upserted in
place, one row per ticker, so a market that settles `yes` and is later corrected
to `no` keeps the same `id` *and* the same `score_status` (`"scored"`). The
currency check compared only those two, so the stale score stayed "current"
forever and the forecast remained scored against an outcome that no longer
existed. `forecast_scorability` could already *detect* this; nothing *fixed* it.

## 5. Recoverability (Gate 6)

Checked before any external call, against persisted markets, prior outcome-sync
history, resolution assessments, scanner history and existing raw bodies.

**`recoverable_local` is effectively zero, and that is a real finding.** A
read-only sample of 500 `market_snapshots` raw bodies for matured, unsynced
markets returned `status="active"` and `result=""` for **all 500** — the last
snapshot of each market was taken while it was still open, because a closed
market drops out of the scan universe. `markets.status` confirms it: 4,393
matured forecasted tickers, **all still recorded `active`**, and only 465 rows in
the entire 100,215-row table say `closed`.

`market_resolution_assessments` (12,542 rows) is a *pre*-resolution clarity
assessment, not a settlement record. It is not a source of outcome truth.

So the resolution genuinely must come from the provider — but from the **same
Kalshi market-detail GET the outcome stage already makes**, not a new one.
Everything else follows from that: no new provider is required, no paid provider
is authorized, and the call budget does not move.

## 6. Fixes (Gate 8)

Three, each tied to an observed defect. Nothing speculative; no migration.

**All of it is behind `ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR`, default OFF.** With
the flag off, both selections keep their deployed behavior byte-for-byte — the
legacy alphabetical selection is preserved as a named method rather than
deleted, and a test asserts the OFF path still reproduces the defect exactly.
This exists because the operations review pointed out something true: without a
flag, landing the code *is* enabling it, and activation triggers a ~12,500-row
scoring backfill over ~13 cycles at 1,000 write transactions each, on a shared
SQLite host running `journal_mode=delete` alongside a live writer — against a
database with **4 recorded `database_locked` events in its entire history**. A
change of that size should not take effect as a side effect of a merge, and the
kill switch should not have to be `git revert`.

| Fix | Defect | File | Provider impact |
|---|---|---|---|
| Need-based outcome selection + rotation | A | `app/services/outcomes.py` | **neutral** — same cap, terminal outcomes never re-fetched |
| Need-based scoring selection | B | `app/services/calibration.py` | **none** — provider-free |
| Brier-recomputing currency check | C | `app/services/calibration.py` | **none** — provider-free |

The new outcome selection spends the unchanged budget on markets whose outcome
can still move: matured markets with no row (oldest close first), then
non-terminal rows, then the rest, then recently-seen non-forecasted markets to
preserve prior behavior. A **terminal** row — settled with a yes/no side, or
canceled/void — is never re-fetched, and that freed budget is what pays for the
markets the prefix could never reach.

**The queue rotates, and that is not decoration.** A failed fetch writes no row,
so under a strict oldest-first order a permanently-unfetchable head — delisted
markets Kalshi no longer serves — would take the entire budget every cycle and
never advance. That is the *same defect in a different sort key*, and the
operations review caught it. Rotating by the persisted MarketOps run count means
every candidate is reached within `ceil(n / limit)` cycles regardless of how many
fetches fail. Priority still chooses where a cycle starts; it no longer decides
whether the rest is ever reached.

**Measured query cost of the new selections, read-only on production:**

| Query | Rows | Time |
|---|---:|---:|
| all outcomes | 1,790 | 6.4 ms |
| distinct forecasted tickers | 4,903 | 1.6 ms |
| close times, scoped to forecasted | 4,903 | 6.7 ms |
| *close times, unscoped (rejected)* | *100,215* | *49.7 ms* |
| all forecasts (scoring selection) | 12,544 | 60.2 ms |
| all scores (scoring selection) | 1,631 | 2.1 ms |

Outcome selection ≈15 ms, scoring selection ≈62 ms, against a MarketOps cycle
averaging ~38–44 s. The unscoped variant was measured and removed anyway: it
loaded every one of 100,215 market rows each cycle to use 5% of them, and the
fallback that genuinely needs the rest is now bounded by `LIMIT` rather than
materializing the table and breaking out of the loop.

**Rollback is a clean `git revert`.** No migration, no schema change, no data
transformation; the fixes change only which rows are *selected*.

## 7. Bounded historical synchronization (Gate 9)

`outcome-sync-backfill --dry-run | --confirm --max-markets N`

Worth stating plainly: **with Fix A deployed, this command is optional.** The
recurring stage now reaches every market that needs a fetch, so at 100 calls per
cycle and 240 cycles per day it drains the ~4,000-market backlog on its own in a
few hours, with zero additional calls. The command exists to do it once,
explicitly, under a hard cap, with an auditable record.

It fetches through the same read-only Kalshi path and the same
`parse_market_outcome` interpreter — a test asserts the module cannot import
that interpreter or assign a settlement field itself, so there is exactly one
place in the codebase that decides yes/no. `--dry-run` always beats `--confirm`.
`ABSOLUTE_MAX_MARKETS = 2000` bounds a mistyped flag. Conflicting rows are
excluded rather than overwritten: a row that disagrees with itself needs a
human, not another fetch that would erase the evidence of the disagreement.

## 8. Scoring coupling (Gate 10)

**Outcomes only.** The repository's existing behavior is two separate MarketOps
stages — sync, then score — and the milestone says to use existing behavior as
the default. The backfill therefore persists outcomes and lets canonical scoring
pick them up on the next cycle; `scores_created` is always 0 and a test asserts
it. This keeps exactly one scoring path, which is what makes score provenance
and the append-only audit trail trustworthy.

## 9. Tests and reviews (Gates 11–12)

`tests/test_outcome_sync_coverage_001.py` — 48 tests. Most assert something is
**refused or preserved**: the risk here is not failing to score, it is
manufacturing a label.

Covered: denominator independence from outcome presence; funnel monotonicity and
exact reconciliation; one reason per missing row and mutual exclusivity; closed
≠ resolved; canceled, void and missing-winner unscorable; ambiguous winner
preserved verbatim; unknown status never normalized; stale rows detected;
conflicts preserved; the frozen prefix detected and the repair reaching past it;
terminal outcomes never re-fetched; oldest-close priority; scoring advancing past
an id prefix; idempotent repeated scoring; yes/no flip creating a new row and
keeping history; no score for an unscorable outcome; forecast probabilities
unchanged; dry-run purity; provider cap and absolute cap; already-current and
conflict exclusion; provider-failure isolation; outcomes-only persistence;
text/JSON parity; zero-write and provider-free reporting; secret-free output;
invalid windows refused; no migration; AST-based trading/EV surface scan.

Two test-quality notes, stated because they are the kind of thing that should
not pass silently:

- `tests/test_calibration.py::test_no_duplicates_unless_outcome_changes`
  asserted `skipped == 1`. That counter was an artifact of loading an
  already-current forecast and discarding it — precisely the waste being
  removed. The test now asserts the actual invariant (no duplicate score row),
  which is what it was always trying to protect.
- The trading/EV surface scan is **AST-based, not substring-based**. A substring
  scan matches each module's own safety disclaimer, which names the things it
  promises not to do; the naive version fails on correct code and would have
  trained us to weaken it.

Three independent adversarial reviews were run (resolution correctness + data
provenance + scope/safety; scoring correctness + statistical validity +
regression risk; provider governance + operations + deployment readiness).
Findings and dispositions are in §11.

## 10. Dark deployment, production run and results (Gates 14–17)

### Dark deployment

Mac = origin = EVO-X2 = `482dea3`, Alembic `0027`, flag unset (default OFF).
MarketOps cycles after deployment were byte-identical to before — `ok`,
`outcomes_synced=100`, `forecasts_scored=0`, `forecast_scores` unchanged at
1,631 rows / 1,000 distinct forecasts. No restart was required or performed.

### Production coverage report (provider-free, zero writes)

The report's own selection audit, run against production, is the finding:

> Of the 100 reachable, **100 already hold a TERMINAL outcome** and are
> re-fetched anyway. The remaining 4,854 are unreachable on EVERY cycle.

So **100% of the ~24,000 Kalshi detail GETs per day were being spent on markets
whose outcome can never change again**, while 4,854 forecasted markets were
structurally unreachable. The budget was not insufficient; it was entirely
wasted.

### Gate 15 classification: **READY FOR ONE BOUNDED OUTCOME SYNC**

Backup healthy, MarketOps healthy, dry run exact (250 selected, 0 calls, 0
writes, exit 0), conflicts excluded (0), no new provider required.

### The one authorized bounded run

`outcome-sync-backfill --confirm --max-markets 250`, 64 seconds:

| | |
|---|---:|
| provider calls | 250 |
| **settled yes/no** | **250** |
| **provider failures** | **0** |
| canceled / void | 0 |
| unrecognized status | 0 |
| outcomes created / refreshed | 236 / 14 |
| conflicts excluded | 0 |
| stop reason | `completed` |

**250 for 250, zero failures.** This settles the operations review's open
HIGH-1 question — whether Kalshi still serves detail for matured markets that
have dropped out of the scan universe — with a 100% success rate, and it
settles the milestone's provider question: **no new provider is required.**

### Coverage uplift

| Metric | Before | After | Δ |
|---|---:|---:|---:|
| matured eligible | 11,478 | 11,496 | +18 (new forecasts) |
| settled yes/no (usable) | 1,128 | **1,634** | **+506** |
| outcome row present | 1,246 | 1,720 | +474 |
| **matured coverage** | **9.83%** | **14.21%** | **+4.38 pp** |
| scored_current | 663 | 840 | +177 |
| missing outcome | 10,350 | 9,862 | −488 |
| database bytes | 4,550,623,232 | 4,550,623,232 | 0 |

250 markets recovered **506** scorable forecasts — markets carry multiple
forecasts, which is why the per-call yield is ~2x. Extrapolating to the 3,746
markets still missing suggests coverage in the high tens of percent, but that
is an extrapolation from one favourable sample and is not claimed as a result.

**One measurement artifact, stated because it looked alarming.** `forecast_scores`
moved 1,631 → 1,808 during the run, which at first reading looked like the
"dark" deployment changing behavior. It was not: the MarketOps cycle at 23:38:03
ran **concurrently** with the backfill and its scoring stage picked up outcomes
the backfill was creating in real time — 145 of the 177 new scores reference an
outcome row the backfill had just created, seconds earlier. That is the designed
sequencing (§8) working end to end, and it is also a reminder that a before/after
capture taken around a live 64-second run is not a clean boundary.

### Scoring is now the binding constraint

`scored_current` is 840 of 11,496 (7.31%) and `distinct scored forecasts` is
still exactly **1,000, max forecast_id 1,000** — because scoring runs on the
legacy id prefix while the flag is off. Outcome coverage improved; scorability
barely moved. **Defect B is now the bottleneck, and only the flag fixes it.**

## 10b. Production decision (Gate 15/18)

## 11. Review findings

An independent operations / provider-governance review verified the two claims
it was explicitly told not to trust, and both held:

- the pre-change selection's reachability was a pure function of alphabetical
  rank — no cursor, no rotation, no persisted position, so waiting or raising
  the cap would never have reached rank > N;
- the recurring stage remains provider-call-neutral: at most `limit` GETs to the
  same read-only endpoint, no new provider, and `provider_budget.py` is
  SolanaTracker-scoped and untouched.

It also confirmed by measurement that scoring **converges** — 12,543 rows over
~13 cycles, then 0 writes at cycle 14, no unbounded growth — and that no restart
is required, because MarketOps and baseline are `Type=oneshot` timer units and
the long-running watcher imports none of these paths.

Blocking findings, all applied:

| # | Finding | Resolution |
|---|---|---|
| HIGH-1 | New selection had no failure memory; an unfetchable oldest-close head would monopolise the budget forever — the same defect in a new sort key | Queue rotates by the persisted MarketOps run count; every candidate reached within `ceil(n/limit)` cycles |
| HIGH-2 | `audit_selection` hard-coded the alphabetical prefix while claiming to replay the deployed one, so the tool built to *validate* the repair would have reported the repair absent. Verdict also latched | Replays whichever selection is running and names it; verdict gated on the selection actually being a frozen prefix |
| HIGH-3 | The dry run exited **1** — the documented, default, safe invocation reported failure | `dry_run` added to the success set |
| HIGH-4 | No flag, so this was not a dark deploy and had no kill switch but `git revert` | `ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR`, default off, with an OFF-path test |
| MED-2 | A run where every fetch failed reported `completed` and exited 0 | `all_fetches_failed`, exit 1 — also the primary signal for HIGH-1 |
| MED-3/6 | Recurring path materialized every forecast's JSON columns and full research packets to read one field (~90–100 MB heap) | `load_only` / two-column selects |
| MED-5 | Six reasons had no signal, and `provider_market_missing` was **structurally unreachable**, so `requires_new_provider` could only ever be 0 — a constant presented as a measurement | Unmeasurable reasons declared as such; the new-provider question now names the bounded probe that answers it |
| L2 | Forecasts on markets with no `close_time` were excluded invisibly | Counted in `data_quality` |

Environment checks the review required: EVO-X2 SQLite is **3.45.1** (variable
limit 32,766, so the `IN`-list cliff is far from current volume) and
`MARKETOPS_FAIL_FAST` is unset. Both clear.

Deferred with reasons: MED-1 (the write burst) is now controlled by the flag and
by `MARKETOPS_SCORE_LIMIT` rather than by code; MED-4's chunking is unnecessary
at 4,903 bindings against a 32,766 limit but is recorded as a future cliff; L1
(`provider_cap` unreachable because the list is pre-capped) is dead but harmless.

### Second review — resolution correctness, scoring, statistical validity

It confirmed by probe that there is exactly one status interpreter, that no
outcome is ever inferred from price, and — the thing I most wanted checked —
that scoring **converges**: six consecutive cycles over adversarial
probabilities produced 8 rows then `(0 scored, 0 skipped)` forever. The float
equality in `_score_is_current` is safe (`round(x, 6)` → SQLite REAL is an exact
double round-trip) and the NaN attack is blocked upstream by `app/schemas.py`.

Then it falsified three claims with executable probes:

| # | Falsified claim | Resolution |
|---|---|---|
| H1 | "conflicts are preserved **unscored**" — false. `_score_target` read `winning_side` alone, so a row saying `yes` with `resolved_probability=0.0` **was scored** and given a Brier value, while the coverage report excluded it. The funnel was not monotonic | One shared rule in both classifiers |
| H2 | "the denominator does not depend on outcome presence" — false when `Market.close_time` is NULL, where maturity fell back to the *outcome's* close time. Biased **optimistically** | Maturity comes from the market only |
| H3 | "every candidate reached within `ceil(n/limit)` cycles no matter how many fetches fail" — true under total failure, false under partial success | Docstring states what was measured |
| H4 | `audit_selection` hard-coded `unreachable = 0` when the repair was on, so it could never report the repair **insufficient** | Measures `candidate_pool`, `full_sweep_cycles`, `full_sweep_hours`; can return `SELECTION_SWEEP_PERIOD_TOO_LONG` |
| M5 | Rotation counter used `COUNT(marketops_runs)`; `retention_coverage` already recommends a 30-day prune on that table, which would silently restore a fixed prefix | `MAX(id)` |
| M3 | The flag was not exactly dark — the brier check ran regardless, so merging would re-score flipped outcomes | Gated |

H1 is the one worth dwelling on. My first fix over-reached: it also treated a
*missing* `resolved_probability` as a contradiction, which broke 26 existing
tests. That was the right signal. `parse_market_outcome` always writes both
fields together, so an absent probability is an older or synthetic row and
`winning_side` is still the source-backed field — calling that a conflict would
have silently unscored a large legitimate population to fix a small corrupt one.
Only a **present and disagreeing** value is evidence of corruption.

H4 stings, because the module already contains a paragraph indicting exactly
this pattern — a constant presented as a measurement — and I wrote the defect
into the function three screens below it.

Also applied: M1 (a tight uplift bound excluding markets merely awaiting
settlement, reported alongside the loose one, because the loose bound counts a
market that closed 61 minutes ago as recoverable *uplift*), M2, M6, M7, L5.

Deferred with reasons: M4 — `provider_failures` cannot distinguish 404 from a
network outage, because `kalshi.py` collapses every error into `None`. The
probe is still the right instrument but it answers the question only if the
failures are 404s; splitting the exception is a separate change. M8 is in §12.

**One finding I disagreed with and resolved differently.** The review suggested
using the "currently reachable" set as evidence that a fetch had been attempted,
which would have made `provider_market_missing` reachable. That is wrong:
*reachable* means "would be selected next cycle", not "was already tried", so a
freshly forecast market would be labelled a provider gap. I implemented it,
saw it mislabel exactly that case in a test, and removed it. The honest answer
is that this repository cannot distinguish never-selected from
selected-and-failed, because a failed fetch leaves no trace — so the report
declares it unmeasurable and names the probe that measures it.

## 12. Limitations

- `sync_never_attempted` vs `provider_market_missing` is inferred from whether an
  outcome row exists, because a fetch that returned nothing leaves no trace. The
  selection audit is what makes the distinction defensible; on its own the
  heuristic would be a guess.
- `max_attainable_coverage_pct` is an **upper bound** and is labelled as one in
  both output formats. It assumes every recoverable market settles yes/no. Some
  are genuinely still unsettled and some will return canceled or void.
- **Flipping the flag breaks comparability of the calibration series.**
  `CalibrationService.summary()` has no window and no cohort filter, so scored
  forecasts go from ~1,000 (ids 1..1000, the *oldest*) to ~12,500 within ~13
  cycles, and `mean_brier` will move for a **population** reason. The prior
  ADR-004 evidence was computed on an id-prefix sample; it is a *different*
  population, not a smaller one. Capture a `calibration-report` immediately
  before activation, or the old cohort is only reconstructible by hand.
- Coverage improving does **not** mean forecasts improved. A larger, less
  selected sample can easily make measured skill look *worse*, and that would be
  a more honest number, not a regression.
