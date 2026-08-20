# AGENTS.md — operating framework for coding/ops agents

**Read this before changing anything.** Then run `python -m app.cli agent-context`.

## Project purpose

Probability Arena is a **read-only market intelligence and calibration system** for Kalshi prediction markets. It scans, gates, enriches, assesses, researches, forecasts, and — critically — **scores its own forecasts against settled outcomes**. The strategy is deliberate: prove forecasting edge with calibration data *before* any EV or trading capability is even designed.

## Current phase

Through **EVAL-001** (see `docs/ROADMAP.md`): a frontier evaluation harness (`frontier-eval-report`) measures the whole desk and emits a conservative readiness scorecard whose labels gate further MEASUREMENT milestones only — no label authorizes live capital. Through **MVP-005A**: the ADR-004 calibration gate crossed (paired n=36, both deltas negative) and the accepted edge-precheck design is implemented — **probability-gap measurement** (forecast − market midpoint) with validity checks behind `ENABLE_EDGE_PRECHECK=false`. It is measurement, never advice: no dollar EV, no sides, no sizes, no actions; `paper_candidate_later` is a review label with zero behavior. Crypto Arena includes a read-only risk engine — deterministic heuristics plus optional GoPlus/SolanaTracker provider adapters producing composite risk scores and avoid/flag verdicts. **A risk score is risk intelligence, never a trade recommendation; "severe" means avoid/flag for review, not short/sell.** The frozen-cohort horizon lane may be explicitly armed with bounded, planner-gated **one-shot** user timers; it has no recurring timer, daemon, loop, or automatic cohort admission. **CRYPTO-COVERAGE-REPAIR-002 (branch `crypto-coverage-repair-002`, default OFF, NOT merged and NOT deployed) adds a SECOND, structurally separate cohort kind to the same tables: ONE standing cohort with ROLLING admission of new births, driven by a recurring hourly pass, buying at most two 6h and two 24h DexScreener observations per birth.** The two kinds are told apart by `provenance["membership"]` (`crypto_horizon.is_rolling_cohort`): every frozen-cohort consumer — arming, `observe_once`, `build_plan` — refuses a rolling cohort, and the rolling lane creates no timer/unit itself and installs nothing. The frozen-cohort statement above remains exactly true of the frozen lane. Otherwise through **OPS-006/007**: the full read-only loop runs scheduled on EVO-X2, a real-time watcher emits informational signals, promoted signals trigger intelligence refreshes, four narrow sport canaries (baseball external research, evidence-aware baseball forecasting, soccer external research, evidence-aware soccer forecasting — SOCCER-002, measurement inputs only) exist behind default-off flags, Crypto Arena adds a parallel read-only Solana memecoin surveillance lane (discovery, ticks, deterministic risk signals) behind its own default-off flags, and a MarketOps Autopilot coordinates all of it — auto-promote/process signals, crypto scans, outcome sync/scoring, champion/challenger snapshots, local DB alerts — behind `ENABLE_MARKETOPS_AUTOPILOT` (default false; run-once always allowed manually). The autopilot is **coordination only**: it can promote/process/research/forecast/score/report but cannot trade, paper trade, calculate EV, or move money. **No EV, no trading of any kind exists — anywhere.** In the crypto lane specifically: no wallets, no private keys, no swaps, no Jupiter/transaction construction, no signing.

## Agent roles

- **Coding agent** — implements a specified milestone in this repo. Must follow this file, `docs/TESTING_POLICY.md`, and `docs/SAFETY_BOUNDARIES.md`.
- **Ops/deployment agent** — deploys to EVO-X2 per `docs/EVO_X2_RUNBOOK.md`. Least-invasive changes only; never mutates other projects on that shared host.
- **Review agent** — checks correctness first, then architecture fit, then the safety greps below.

## Required first steps (every session)

1. `python -m app.cli agent-context` — phase, flags, DB state, boundaries.
2. Read `docs/PROJECT_CANON.md` (architecture) and `docs/SAFETY_BOUNDARIES.md` (hard limits).
3. `git log --oneline -10` — the commit messages are the milestone history.
4. `.venv/bin/python -m pytest -q` — confirm green before touching anything.
5. If the task touches deployment: read `docs/EVO_X2_RUNBOOK.md` and check what commit EVO-X2 is actually on before assuming.

## Allowed capabilities

Everything currently in the repo (see `docs/CAPABILITY_MATRIX.md`): read-only scanning, gating, enrichment, assessment, research, forecasting, outcome sync, calibration, watching, signal workflow, retention of our own operational tables. New work must stay within these unless the milestone explicitly and legitimately extends them.

## Forbidden capabilities (hard boundary — do not implement, scaffold, or "prepare")

EV calculation (any monetary or return-denominated expected value, in **any** unit — dollars, SOL, ticks, basis points, probability-weighted returns; "not denominated in dollars" is not an exemption, and the only accepted EV-adjacent surface is the existing probability-gap edge precheck) · paper trading beyond the modeled `PAPER_SIMULATION` mode described below · trade recommendations · portfolio sizing · order placement · wallet/private-key handling · live trading/execution · autonomous trading · crypto wallets · swaps/transaction construction/signing (Jupiter or any DEX) — including requesting or receiving swap instructions or transaction bytes from any endpoint (the build/swap sibling of the very API that served a quote included), constructing/encoding a transaction client-side, simulating one against an RPC node (`simulateTransaction` and equivalents), signing, submitting/broadcasting/relaying, and fetching a blockhash/priority fee/nonce · real fills, real orders, real positions, real capital. `docs/SAFETY_BOUNDARIES.md` states what milestone would have to be explicitly accepted before each could exist. If a task appears to require one of these, **stop and report back instead of building it**.

### The two narrow exceptions (SAFETY-BOUNDARY-ROUTE-QUOTE-001)

Two capability modes are **permitted with conditions**. **Neither is implemented, and the amendment authorizes no milestone** — it states what such a lane would be allowed to do if one were separately accepted. Anything outside these two descriptions is the hard boundary above and still means **stop and report back instead of building it**. Read the amendment in `docs/SAFETY_BOUNDARIES.md` before touching either; it is the source of truth and this is a summary of it.

> **BUILDING EITHER MODE TODAY STILL MEANS STOP AND REPORT BACK.** "Permitted with conditions" describes what such a lane would be *allowed to do*, not permission to *start writing it*. `PAPER_SIMULATION` requires **MVP-005B** acceptance. `READ_ONLY_ROUTE_QUOTE` requires **a separately accepted milestone that does not yet exist** — there is no such milestone today, so there is no authorization to build a quote fetcher. Both modes are **free public endpoints only**: no paid RPC, no paid trade/orderflow feed, no SolanaTracker.

- **`READ_ONLY_ROUTE_QUOTE`** — may **retrieve executable route/amount evidence**: what route a stated input size would take, and what output amount, price impact and fee a public quote endpoint reports for it. Retrieval of the quote and nothing else. It may **never** request or receive swap instructions or transaction/instruction bytes (including from the build/swap sibling of the same API — reaching a quote route grants nothing on any other route), construct/encode/serialize a transaction or instruction by any means, simulate a transaction against an RPC node, sign anything, submit/broadcast/relay, fetch a blockhash/priority fee/nonce, load/derive/generate/import/hold/reference wallet key material or seed phrases, or supply a wallet we control as the quote's user/payer. The permitted object is *what a trade of size X would cost*, never *the trade we are about to make*. A non-GET quote request may carry no key, no wallet, and nothing that mutates venue state.
- **`PAPER_SIMULATION`** — may produce **modeled fills and modeled P&L only**. Every artifact carrying such a number must carry, **on the artifact itself**, (1) an explicit **model identifier** (named and versioned) and (2) an explicit **modeled-vs-observed basis** (which inputs were OBSERVED, with timestamps, and which were MODELED). A file header, README, run-level note, docstring or column comment does **not** satisfy this. An artifact missing either field must not be produced, persisted, printed, returned, or forwarded; an aggregate/export/summary inherits both or is not produced. Real fills, real orders, real positions and real capital remain forbidden, as does presenting a modeled number as realized/actual/observed P&L, deriving a dollar EV from it, or deriving/optimizing/ranking/recommending a size from it. MVP-005B still governs whether such a lane is BUILT at all.

Both modes are **free public endpoints only**: no paid RPC, no paid trade/orderflow feed, no SolanaTracker (its CRYPTO-DISCOVERY-PROVIDER-GATE-001 authorization is scoped to the discovery/risk lanes and is not a precedent here). A quote obtainable only by paying for it is not obtainable — report no quote, never buy one.

**The policy and the automated control disagree on purpose.** The AST safety audit (`frontier-eval-report --include-safety`) still bans `swap`, `jupiter`, `paper_trad`, `expected_value`, `position_siz`, `portfolio`, `place_order`, `submit_order` and `create_order` as identifiers anywhere in `app/`, so an implementation of either mode named the obvious way will FAIL the audit. That is the correct outcome: open a separate, narrowly-reviewed change unbanning the exact fragment in the exact file when an implementation actually needs it — never rename an identifier to slip past the scan, and never broaden the allowlist past the one file that needs it.

## Research doctrine (binding — earned empirically, not asserted)

These three rules are the output of EDGE-DISCOVERY-001. Violating one has
already cost this project months.

1. **No signal graduates because it looks predictive.** Every signal must defeat
   the strongest available **contemporaneous market baseline** before any
   execution engineering or capital allocation begins. Beating a base rate, a
   naïve model, or a prior version of ourselves is level-1 evidence and bears on
   nothing. The hierarchy is:
   `beats base rate < beats naïve model < beats MARKET PRICE < survives executable price < survives fees/slippage < prospective positive expectancy`.
   Only the last two bear on capital.
2. **A signal can be real, replicating, and still uneconomic.** Always report the
   **executable cost floor beside the effect size**. EDGE-DISCOVERY-001's E2
   found a genuine, out-of-sample-replicating 2.36pt one-hour lead against a
   3.36pt floor — a real discovery and a useless trade. Never display a Sharpe,
   an accuracy, or a coefficient naked.
3. **Before declaring a dataset unavailable, exhaustively inspect raw, derived,
   aggregate, archival and observability stores.** `MarketPriceTickBucket` held
   the executable market price the whole time and had survived a pruning
   milestone; finding it turned "years of new collection" into a 90-second query.
4. **A measurement must report its own noise floor, not just its result.** An
   assertion that cannot tell you when it is meaningless is worse than no
   assertion. CP5's two-null-arm benchmark exposed a ~200,000 ns/ev floor
   against a ~900 ns signal and so caught a leaked process that had been pinning
   a core for two hours; a single-null benchmark would have reported scheduler
   noise as an overhead result and passed its gate for the wrong reason. The
   same shape applies to test guards — assert that the permitted thing EXISTS,
   or the guard is satisfied by a repository in which nothing works.

5. **A checkpoint is not complete because its implementation and tests are
   green; it is complete when its intended production path is demonstrably
   reachable.** CP4 shipped 1,186 lines and 81 passing tests that nothing in
   `app/` could call. From inside a module, everything works — so reachability
   must be asserted from *outside*, by a test that instantiates the real
   collaborator and proves observable state changes. **Seam tests stay in the
   suite permanently**, even after the subsystem matures; they are the guard,
   not scaffolding.
6. **Typed seams with explicit fault boundaries are the default for
   observational hot paths.** Measured on the collector seam: a typed direct
   call is **83 ns**, and **83 ns** again when placed inside its own narrow
   `try/except` — the boundary is free. A generic `try/except` + `**kwargs`
   seam is **292–333 ns**. The cost was never the fault handling; it was the
   varargs packing. So take the containment and pay nothing: no `*args`,
   `**kwargs`, reflection, adapter dispatch, or silent interface translation on
   these paths.

7. **Every important metric needs a POSITIVE-CONTROL test: force the underlying
   condition to occur, and prove the metric becomes non-benign.** Testing the
   healthy state only proves the healthy state. This directly targets the
   failure class that has produced every observability defect found in this
   repo — **a plausible benign value emitted by a broken path**, which does not
   crash, does not alert, and yields clean-looking datasets and convincing
   statistics.

   | force this | this must become non-benign |
   |---|---|
   | a reconnect | `subscription_generation` changes |
   | a sequence gap | the gap metric is non-zero |
   | a rotation failure | `rotation_failures` is non-zero |
   | a segment close | the close histogram moves |
   | disconnecting the metrics lane | the reachability test **fails** |

   Real instances this rule would have caught on the day they shipped: two
   archive columns permanently `None` and read as "no generation information";
   `closer_outstanding()` called on a *property*, so `rotation_failures`
   silently fell back to `0` — indistinguishable from "no rotation failed";
   `CollectorMetrics` with 81 green tests and no caller anywhere in `app/`;
   `brier_skill_vs_base_rate` reading as skill while measuring the wrong
   baseline.

   **A missing measurement is not zero. A disconnected metric is not healthy.**

8. **A field name is not evidence of its semantics. Before using any venue field
   as an experimental variable, empirically verify what causes it to change.**

   The concrete case: the tape-manifest tool gated market freshness on Kalshi's
   `updated_time` and reported that **73,057 of 73,630 markets were stale**.
   Precise, dramatic, reproducible — and **wrong**. `updated_time` is a market
   *definition* timestamp. Ten markets re-read 180 s apart moved it **0/10**
   while lifetime volume moved **10/10** and top-of-book moved **10/10**; the
   "stale" markets were trading hundreds of thousands of contracts per minute.

   The failure was not noise. Noise is obvious. This was a confident false
   finding produced by assuming a field meant what its name suggested.

   **Apply this before any of these becomes an experimental variable:**
   `timestamp` · `sequence` · `trade side` · `size` · `volume` ·
   `open interest` · `book update` · `market status` · `liquidity`.

   The verification is cheap: re-read the field across a known interval and
   observe what moves it. Do that *before* it enters a statistic, not after the
   statistic looks surprising.

9. **Fixtures are executable claims about external reality.** Any fixture
   representing venue behaviour must be traceable to **captured wire evidence or
   official protocol semantics**. A fixture that cannot identify its empirical
   basis is **synthetic test data, not venue truth**.

   Earned: 368 green tests were built on CP3 fixtures that put every channel on
   **one shared `sid`**. The venue does not — it assigns sid 1 =
   `orderbook_delta`, sid 2 = `ticker`, sid 3 = `trade`. Every test built on
   those fixtures was internally consistent and wrong, and the suite certified
   the wrong behaviour rather than failing. This is the most dangerous form of
   the repo's recurring failure class, because fixtures are what everything else
   is checked against.

   **Critical protocol fixtures must carry provenance:**
   `capture_id` · `timestamp` · `venue` · `channel` ·
   `sanitized raw frame hash` · `schema version`.
   That is also the drift detector — it is how we notice the live venue moving
   away from the world our tests certify.

   **Sequence is a property of stream identity, not of the market.** The domain
   is `(connection/session, subscription generation, sid)`, and within a
   sequenced sid `seq_{n+1} = seq_n + 1` unless documented venue behaviour says
   otherwise. Assuming `seq = f(market)` is what produced 219 false faults on a
   stream that ran 1..219 perfectly clean.

10. **Never encode epistemic absence as a numerical market state.**
    `None → 0` is dangerous anywhere the zero has economic meaning.

    | | means |
    |---|---|
    | `depth = 0` | the venue said the book is empty |
    | `depth = unknown` | the venue said nothing |

    Collapsing those fabricates market state. The concrete case: an **omitted**
    snapshot ladder was normalized as `present` with zero levels, making "the
    venue said nothing" and "the venue said empty" one record — which would have
    corrupted spread, depth imbalance, OFI, microprice, resilience and
    liquidity-regime labels, and every model built on them.

    Absence must be **structurally** representable, not remembered by
    convention: `LadderState = NOT_PROVIDED | EMPTY | PRESENT(levels)`.

    **Every source carries a data-quality capability, and features inherit it:**

    | source | ordering | gap detection | completeness claim |
    |---|---|---|---|
    | `orderbook` | sequenced | yes | measurable |
    | `trade` | sequenced (own sid) | yes | measurable |
    | `ticker` | **unsequenced** | **no** | **unknown** |

    Ticker data is not unusable — but it must **never be silently described as
    lossless**. A feature like `rolling_ticker_volume_30s` must inherit "no
    sequence-based loss detection" from its source.

11. **Mathematical arbitrage is not executable arbitrage.** A payoff identity
    is a hypothesis, not an edge. Between "these two prices are inconsistent"
    and "we can capture the difference" sit gates that each kill independently:

    > semantic relationship → payoff identity → book depth → fees →
    > **capital lockup** → **venue transformation support** → executable edge

    The last two are where prediction markets differ from equities and where
    naive analyses die. Capital is locked until resolution, so a 3% spread held
    for four months is not 3% — quote the **annualised** figure or quote
    nothing. And the venue must actually support the transformation the
    identity requires; an identity the exchange will not let you assemble
    before settlement is arithmetic, not a trade.

12. **Statistics proposes, the agent screens, deterministic code decides.**
    The permitted direction is:

    > numerical screen finds a candidate → agent judges whether a *mechanism*
    > is plausible → deterministic evaluator tests the economics

    The forbidden direction is an LLM inventing a correlation and statistics
    being used to confirm it. An agent supplies **semantics, hypotheses,
    interpretations and critiques** — never an action. Anything that selects a
    trade must be deterministic and inspectable, and **no agent may sit in the
    synchronous market path**, ever.

13. **`NO_TRADE` is a first-class action, not the absence of one.** A system
    that must always choose a side has no way to express "the edge is real and
    still not worth taking here". Action is a function of more than the signal:

    > Action = f(alpha, volatility regime, liquidity, execution cost,
    > uncertainty, portfolio state)

    The same +2% signal is a trade in one regime and a refusal in another. A
    strategy that cannot abstain will be adversely selected precisely when
    conditions are worst, because that is when its signal looks strongest.

14. **Size against the adverse end of the uncertainty interval, not the point
    estimate.** Forecast `[σ⁻, σ⁻⁺]`, not `σ̂`. A point estimate of 18% with a
    90% interval of 12–39% is not an 18% risk — the position must survive 39%.
    Point estimates encode false precision exactly where the tails matter, and
    a model is most confidently wrong during the regime shifts that hurt most.

15. **A bounding statistic must not depend on a free parameter.** A peak is an
    upper bound, so an estimator that can be moved by an arbitrary choice
    cannot bound anything. Measured on one 84,170-record tape, the *same*
    calendar-second peak estimator returned **485** under monotonic-clock
    alignment and **565** under wall-clock alignment, while the sliding window —
    which has no alignment to choose — returned **612**. Three live production
    samples reproduced the bias at 44%, 23% and 6%. Fixed buckets do not merely
    add noise; they are **phase-sensitive**, and they err **low**, which is the
    one direction capacity work cannot absorb.

16. **A source's reputation is its forward return NET of the state-conditional
    expectation, never its hit rate.** For a source `i` and horizon `h`:

    > `α_i(h) = r_{t,t+h} − E[r_{t,t+h} | S_t]`

    Subtracting the state term is the whole measurement. A caller who posts
    *after* a token is already moving can show a 70% "win rate" while
    contributing exactly zero information — the move was already in `S_t`, and
    the reputation score is measuring the market, not the source. This is
    doctrine 1 in another domain, and it is the same error the sports
    forecaster taught us: **the model was the market, blurred.**

    Reputation is therefore a **vector, not a number** — lead time, novelty,
    α at several horizons, rug/adverse-outcome rate, and the sample size behind
    each. A single score hides which of those is carrying it.

17. **Infrastructure must earn its cost from a measured constraint, not from
    sounding serious.** Do not buy latency before proving expectancy at a
    slower timescale. The ladder is:

    | tier | when it is justified |
    |---|---|
    | **0** — standard RPC / WSS, public endpoints | always start here |
    | **1** — managed streaming with replay | only once completeness or latency is **demonstrably** blocking a validated experiment |
    | **2** — preconfirmation / shred-level / MEV-class access | only once a latency-sensitive edge has actually been established |

    The test for moving up a tier is a *measurement showing the current tier is
    the binding constraint* — not a hypothesis that it might be. Optimising
    microseconds before demonstrating positive expectancy over seconds or
    minutes is spending money to make an unproven edge arrive faster.

## Parallel-agent composition (binding)

> **Every parallel-agent milestone with a shared runtime path must have an
> explicitly owned integration-seam checkpoint.**

**File ownership prevents collisions. It does not guarantee composition.** When
workstream A and workstream B exchange an interface, assign a third checkpoint —
or the orchestrator — to *prove the seam* before either is called complete.

Earned the hard way: KALSHI-LIVE-TAPE-COLLECTOR-001 ran CP3 (orchestrator) and
CP4 (metrics) in parallel under strict file ownership. CP3 was told to define a
hook and not implement the lane; CP4 was told to define its expected interface
and not wire it. **Both complied exactly.** The result was 1,186 lines and 81
green tests of *unreachable* code — `CollectorMetrics` had no caller anywhere in
`app/`, and the two interfaces did not match. Two green reports produced a false
"complete" status.

The guard against this is a test that instantiates the **real** collaborator,
drives the **real** path, and proves observable state actually changes. A unit
suite cannot catch an unreachable module, because from inside the module
everything works. The worked example is
`tests/test_kalshi_live_tape_cp35_001.py` (CP3.5, the seam checkpoint that
closed this one): reachability, exactly-one-observation-per-event, containment
of a hostile collaborator, and the shape the cost was measured against.

**Two corollaries that seam checkpoint earned:**

* **An audit that forbids the wiring certifies unreachable code.** The
  dependency-direction test had `collector_metrics` off the collector's
  permitted-import list, so the module was structurally unimportable by the one
  caller it existed for. When an integration seam is opened, its audit is part
  of the seam — amend it explicitly, keep it net-stronger, and say so.
* **A parameter nothing ever passes is not an interface, it is a comment.**
  `on_reconnect(subscription_generation=…)` had no channel to arrive through for
  a whole milestone. Prefer proving a parameter's *caller* exists over proving
  its *handler* works.

**Metric-naming rule:** `brier_skill_vs_base_rate` is level-1 evidence and must
never be presented as market-relative skill. Its rename, and the addition of
`brier_skill_vs_market`, are deferred to a **declared amendment window** because
`app/services/forecast_reliability.py` is pinned at `experiment_registry.py:422`
— editing it is a drift event against a live registered experiment.

### Current research status (2026-08-15)

**Sports forecasting: STOPPED, not deleted.** All four preregistered
EDGE-DISCOVERY-001 experiments failed
(`docs/experiments/EDGE-DISCOVERY-001-VERDICT.md`). Current forecasts have **zero
authorization path to capital**. The models are retained at low frequency as
**scientific controls and regression benchmarks**. **Do not spend effort making
them more accurate.** The mechanism is understood: `logit(p) = −0.094 +
0.568·logit(q)`, R² = 0.661 — the model is the market, blurred.

**Scope of that verdict:** it covers **sports**, the domain markets are already
best-calibrated in (measured: `β_q = 1.013`). It does **not** show that LLMs
cannot have prediction-market edge, and must not be generalised that way.

**Next programmes, in order:** implement `KALSHI-LIVE-TAPE-COLLECTOR-001` →
preregister `MARKET-MICROSTRUCTURE-EDGE-001` (target future market movement, not
settlement) → collect prospective tape → simple state baselines before any
sophisticated model → `STRUCTURAL-PROBABILITY-EDGE-001` → information-arrival →
and only after a net edge exists, the quantitative decision kernel.

**Solana** continues as an **execution-science laboratory**, not a P&L venue:
`QUOTE → EXPECTED FILL → REALIZED FILL → MODEL ERROR`. Judge it by
`Ĉ(s) − C_realized(s)`, not by capacity.

## Testing expectations

`docs/TESTING_POLICY.md` in one line: everything green, no live LLM/web calls in unit tests (mock every provider), gated live tests skip by default, migrations get up/down tests, and run the safety grep before declaring done:

```bash
grep -rinE "expected_value|kelly|position_siz|paper_trad|place_order|submit_order|create_order|wallet|recommended_side|trade_recommend|execute_trade" app/ --include="*.py"
```

Expected result: no implementation surface. Acceptable hits: boundary-statement docstrings, `app/canon.py` declarations, the pre-existing Kalshi WS auth (`ws_snapshots.py` / `kalshi_private_key_path`), and the EVAL-001 scanner's own vocabulary constants in `app/services/frontier_eval.py` (string literals the AST-based safety audit uses to detect banned identifiers — `frontier-eval-report --include-safety` runs that stricter identifier-level check).

## Deployment expectations

EVO-X2 is a **shared production host**. User-level systemd only; own directory/venv/SQLite; nothing global; flags roll out per the documented sequences (deploy dark → validate template mode → flip one flag → process 1–3 items → inspect). Update the runbook and deployment report when state changes.

## Report-back format

End milestone work with: what was built (mapped to requirements) · validation (test counts, live-smoke evidence) · safety confirmation (grep + boundary statement) · deployment state (what is/isn't on EVO-X2) · risks/follow-ups. Commit as `<MILESTONE-ID>: <summary>` with a body that documents decisions — the git log is the project's memory.
