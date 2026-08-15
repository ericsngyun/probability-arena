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
