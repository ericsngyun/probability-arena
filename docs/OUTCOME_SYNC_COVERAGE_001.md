# OUTCOME-SYNC-COVERAGE-001 — outcome coverage for matured forecasts

**Status:** implemented and reviewed; production decision in §10.

Probability Arena was evaluating forecast quality on 8% of its matured
forecasts and did not know it. This milestone establishes what the real
denominator is, why the other 92% have no label, and what it costs to fix.

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

| Fix | Defect | File | Provider impact |
|---|---|---|---|
| Need-based outcome selection | A | `app/services/outcomes.py` | **neutral** — same cap, terminal outcomes never re-fetched |
| Need-based scoring selection | B | `app/services/calibration.py` | **none** — provider-free |
| Brier-recomputing currency check | C | `app/services/calibration.py` | **none** — provider-free |

The new outcome selection spends the unchanged budget on markets whose outcome
can still move: matured markets with no row (oldest close first), then
non-terminal rows, then the rest, then recently-seen non-forecasted markets to
preserve prior behavior. A **terminal** row — settled with a yes/no side, or
canceled/void — is never re-fetched, and that freed budget is what pays for the
markets the prefix could never reach.

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

## 10. Production decision (Gate 15)

_Pending dark deployment and the production report._

## 11. Review findings

_Pending._

## 12. Limitations

- `sync_never_attempted` vs `provider_market_missing` is inferred from whether an
  outcome row exists, because a fetch that returned nothing leaves no trace. The
  selection audit is what makes the distinction defensible; on its own the
  heuristic would be a guess.
- `max_attainable_coverage_pct` is an **upper bound** and is labelled as one in
  both output formats. It assumes every recoverable market settles yes/no. Some
  are genuinely still unsettled and some will return canceled or void.
- Coverage improving does **not** mean forecasts improved. A larger, less
  selected sample can easily make measured skill look *worse*, and that would be
  a more honest number, not a regression.
