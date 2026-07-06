# SAFETY_BOUNDARIES — hard limits and their gates

These are project-level boundaries, not suggestions. Any task that appears to
require crossing one must stop and report back instead of building.

## Forbidden today (no implementation surface may exist)

| Boundary | Status | Gate: what must be explicitly accepted first |
|---|---|---|
| **EV calculation** | ❌ none exists | The MVP-005A gate crossed (paired n=36, both deltas negative — 2026-07-04); the accepted design was implemented as the **edge precheck**: probability-gap measurement (forecast − midpoint) with validity checks, behind `ENABLE_EDGE_PRECHECK=false`. It has no dollar-EV, side, size, or action fields by construction, and `paper_candidate_later` is a review label with zero behavior. **Dollar EV remains forbidden with no unlocking milestone defined** |
| **Trade recommendations** | ❌ none exists | Post-MVP-005B review; never from a forecaster directly — forecasts are reasoning artifacts |
| **Paper trading / simulation** | ❌ none exists | MVP-005B, gated on MVP-005A acceptance. EDGE-ANALYSIS-001 (`edge-cohort-report`) only *measures* per-cohort gap follow-through and *reports* whether the MVP-005B gate is met — it is analysis, unlocks nothing, and advancing still requires explicit human acceptance |
| **Portfolio sizing** | ❌ none exists | Post-paper-trading milestone with explicit human acceptance |
| **Order placement** | ❌ none exists | A dedicated, explicitly-accepted live-trading milestone (not currently on the roadmap) |
| **Wallet / private-key handling** | ❌ none exists (ADR-002) | A dedicated custody design + security review milestone; keys would never live in this repo/DB regardless |
| **Live trading / execution** | ❌ none exists | Same as order placement; also requires operational controls (limits, kill switches) designed first |
| **Autonomous trading** | ❌ none exists | Not planned; would require all of the above plus standing human-in-the-loop controls |
| **Crypto wallets** | ❌ none exists | CRYPTO-001 shipped read-only scouting only; wallet milestones explicitly deferred |
| **Swaps / transaction construction / signing (Jupiter or any DEX)** | ❌ none exists | WALLET-001 (policy-controlled transaction *proposal* gateway only — no signing/keys), itself gated on CRYPTO-002 (risk engine) + CRYPTO-003 (paper simulator) acceptance; much later |

## What "no implementation surface" means

- No functions, fields, tables, endpoints, or CLI commands for these capabilities — including "disabled" or "placeholder" versions.
- `paper_candidate_pending` is a human review **label** on signals with zero attached behavior; keep it that way.
- The safety grep in `AGENTS.md` / `docs/TESTING_POLICY.md` must come back clean before any milestone is declared done.

## Always true, phase-independent

- All external interaction is read-only (GETs); Kalshi credentials are not required and not stored; the optional WS client sends channel subscriptions only.
- Crypto Arena (CRYPTO-001) is read-only surveillance: public DEX data in, auditable rows out. Its signals are telemetry — no wallet code, no private keys, no swaps, no Jupiter/transaction construction, no signing, no execution, and no EV/paper-trading semantics may attach to them before the gated milestones above.
- The crypto risk engine (CRYPTO-002) produces **risk intelligence, not trade advice**: a composite risk score/level is an avoid/flag verdict for human review. "Severe" means avoid/flag — never short/sell/buy. Provider API keys are optional, header-only, and never printed or logged.
- No secrets in code, logs, or committed files; `.env` is gitignored.
- Forecast confidence is capped centrally by evidence depth; forecasters cannot self-declare certainty. Evidence-aware forecasts (baseball, soccer) are measurement inputs only — never advice, never sized, never actionable.
- Every model-assisted path has a deterministic fallback and honest degradation (template content stays labeled template).
- The meme/news + domain-expansion scout (MEME-NEWS-001) is **read-only discovery, scouting, and inventory only**: it does not trade, paper trade, compute EV, recommend trades, size positions, or place orders, and uses no wallets/private keys/swaps/signing/execution. An `attention_score` is an interest/velocity signal for human review — never a buy/trade/EV/alpha score, and it triggers no behavior. Catalyst events are informational, never a trade trigger; non-dexscreener sources (rss/x/discord/telegram) are schema placeholders added only if explicitly configured (no authenticated scraping). The domain scout adds no forecaster and changes no promotion/edge/forecast logic — `canary_priority` ranks candidate domains for future human-planned canaries only.
- The frontier evaluation harness (EVAL-001) is evaluation only: gap follow-through is market-movement analysis, not PnL; no fills or positions are simulated; readiness labels (`not_ready` … `ready_for_paper_design`) gate further MEASUREMENT milestones and never authorize live capital — no live/autonomous readiness label exists by design.
