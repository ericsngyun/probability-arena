# CAPABILITY_MATRIX

| Capability | Current status | Allowed? | Required milestone before enabling/extending | Safety notes |
|---|---|---|---|---|
| Market scanning | Live (baseline timer, EVO-X2) | ✅ | — | Read-only Kalshi GETs; MVE filter server-side; rate-bounded by limits |
| Signal detection (watcher) | Live (60s loop, EVO-X2) | ✅ | — | Informational signals only; cooldown dedup; retention bounds tick growth |
| External research | Canaries only: baseball via MLB Stats API behind `ENABLE_BASEBALL_EXTERNAL_RESEARCH`; soccer via provider-gated ESPN API behind `ENABLE_SOCCER_EXTERNAL_RESEARCH` + `SOCCER_RESEARCH_PROVIDER` (all default off/template). General `ENABLE_EXTERNAL_RESEARCH` (LLM+web) exists but off | ✅ behind flags | Per-domain canary review before widening further | Official sources preferred; honest template fallback; provenance persisted |
| Forecasting | Template baseline default; baseball evidence canary behind `ENABLE_BASEBALL_EVIDENCE_FORECASTING`; LLM forecaster off | ✅ behind flags | Champion/challenger (MVP-004G) before broader rollout | Central confidence caps; capped ±0.25 prior shift; deterministic fallbacks |
| Calibration | Live (outcome sync + scoring in baseline runner) | ✅ | — | Read-only scoring; append-only audit; the gate for everything below |
| MarketOps coordination | Autopilot (OPS-006): auto-promote/process signals, crypto scan, sync/score, champion/challenger snapshot, local DB alerts; loop/timer behind `ENABLE_MARKETOPS_AUTOPILOT` (default false), `marketops-run-once` manual | ✅ behind flag | — | Sequences existing read-only services only; cannot trade, paper trade, calculate EV, or move money |
| **EV calculation** | **Does not exist** | ❌ | MVP-005A (design + safety review), itself gated on calibration evidence of edge | See SAFETY_BOUNDARIES; no placeholder surface allowed |
| **Paper trading** | **Does not exist** | ❌ | MVP-005B, gated on MVP-005A acceptance | Simulation only, still no orders; explicit human acceptance |
| **Live trading** | **Does not exist** | ❌ | Dedicated milestone, not on roadmap | Requires limits/kill-switch design first |
| **Wallet execution** | **Does not exist** | ❌ | Dedicated custody + security review milestone | ADR-002: no private keys in this repo/DB ever |
| Crypto scouting | Read-only Solana surveillance (CRYPTO-001): DEX Screener discovery, ticks, deterministic risk signals, reports; loop/timer use behind `ENABLE_CRYPTO_SCOUT` (default false), mock risk provider behind `ENABLE_CRYPTO_RISK_PROVIDER` | ✅ behind flags | CRYPTO-003 (paper sim) gated like MVP-005B | Public read-only GETs only; no wallets/keys/swaps/Jupiter/tx construction/signing — see SAFETY_BOUNDARIES |
| Crypto risk engine | CRYPTO-002: heuristics (always available) + optional GoPlus/SolanaTracker adapters behind `ENABLE_CRYPTO_RISK_ENGINE`/`ENABLE_GOPLUS_RISK`/`ENABLE_SOLANA_TRACKER_RISK` (all default false); composite scores/levels, activated risk signals, risk reports | ✅ behind flags | CRYPTO-003 consumes this data after acceptance | Risk intelligence only — a score is an avoid/flag verdict, never trade advice; API keys header-only, never printed |
| **Crypto wallet** | **Does not exist** | ❌ | Explicitly deferred ("wallet milestones later only") | Same as wallet execution |
| **Autonomous execution** | **Does not exist** | ❌ | Not planned | Would require every gate above plus standing human-in-the-loop controls |

Legend: ✅ allowed now (within documented flags/limits) · 🔜 planned, not yet built · ❌ forbidden — do not implement, scaffold, or "prepare".
