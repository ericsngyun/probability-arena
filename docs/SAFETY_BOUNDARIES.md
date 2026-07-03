# SAFETY_BOUNDARIES — hard limits and their gates

These are project-level boundaries, not suggestions. Any task that appears to
require crossing one must stop and report back instead of building.

## Forbidden today (no implementation surface may exist)

| Boundary | Status | Gate: what must be explicitly accepted first |
|---|---|---|
| **EV calculation** | ❌ none exists | MVP-005A (EV precheck design + safety review), which itself requires calibration evidence that a forecaster beats the market baseline on Brier over a meaningful resolved sample |
| **Trade recommendations** | ❌ none exists | Post-MVP-005B review; never from a forecaster directly — forecasts are reasoning artifacts |
| **Paper trading / simulation** | ❌ none exists | MVP-005B, gated on MVP-005A acceptance |
| **Portfolio sizing** | ❌ none exists | Post-paper-trading milestone with explicit human acceptance |
| **Order placement** | ❌ none exists | A dedicated, explicitly-accepted live-trading milestone (not currently on the roadmap) |
| **Wallet / private-key handling** | ❌ none exists (ADR-002) | A dedicated custody design + security review milestone; keys would never live in this repo/DB regardless |
| **Live trading / execution** | ❌ none exists | Same as order placement; also requires operational controls (limits, kill switches) designed first |
| **Autonomous trading** | ❌ none exists | Not planned; would require all of the above plus standing human-in-the-loop controls |
| **Crypto wallets** | ❌ none exists | CRYPTO-001 is read-only scouting only; wallet milestones explicitly deferred |

## What "no implementation surface" means

- No functions, fields, tables, endpoints, or CLI commands for these capabilities — including "disabled" or "placeholder" versions.
- `paper_candidate_pending` is a human review **label** on signals with zero attached behavior; keep it that way.
- The safety grep in `AGENTS.md` / `docs/TESTING_POLICY.md` must come back clean before any milestone is declared done.

## Always true, phase-independent

- All external interaction is read-only (GETs); Kalshi credentials are not required and not stored; the optional WS client sends channel subscriptions only.
- No secrets in code, logs, or committed files; `.env` is gitignored.
- Forecast confidence is capped centrally by evidence depth; forecasters cannot self-declare certainty.
- Every model-assisted path has a deterministic fallback and honest degradation (template content stays labeled template).
