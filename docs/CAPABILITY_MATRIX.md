# CAPABILITY_MATRIX

| Capability | Current status | Allowed? | Required milestone before enabling/extending | Safety notes |
|---|---|---|---|---|
| Market scanning | Live (baseline timer, EVO-X2) | ✅ | — | Read-only Kalshi GETs; MVE filter server-side; rate-bounded by limits |
| Signal detection (watcher) | Live (60s loop, EVO-X2) | ✅ | — | Informational signals only; cooldown dedup; retention bounds tick growth |
| External research | Canary only: baseball via MLB Stats API behind `ENABLE_BASEBALL_EXTERNAL_RESEARCH` (deployed code may lag; flag default false). General `ENABLE_EXTERNAL_RESEARCH` (LLM+web) exists but off | ✅ behind flags | MVP-004G-era review before widening domains | Official sources preferred; honest template fallback; provenance persisted |
| Forecasting | Template baseline default; baseball evidence canary behind `ENABLE_BASEBALL_EVIDENCE_FORECASTING`; LLM forecaster off | ✅ behind flags | Champion/challenger (MVP-004G) before broader rollout | Central confidence caps; capped ±0.25 prior shift; deterministic fallbacks |
| Calibration | Live (outcome sync + scoring in baseline runner) | ✅ | — | Read-only scoring; append-only audit; the gate for everything below |
| **EV calculation** | **Does not exist** | ❌ | MVP-005A (design + safety review), itself gated on calibration evidence of edge | See SAFETY_BOUNDARIES; no placeholder surface allowed |
| **Paper trading** | **Does not exist** | ❌ | MVP-005B, gated on MVP-005A acceptance | Simulation only, still no orders; explicit human acceptance |
| **Live trading** | **Does not exist** | ❌ | Dedicated milestone, not on roadmap | Requires limits/kill-switch design first |
| **Wallet execution** | **Does not exist** | ❌ | Dedicated custody + security review milestone | ADR-002: no private keys in this repo/DB ever |
| Crypto scouting | **Does not exist yet** | 🔜 planned read-only | CRYPTO-001 | Read-only market data only; same boundaries as Kalshi track |
| **Crypto wallet** | **Does not exist** | ❌ | Explicitly deferred ("wallet milestones later only") | Same as wallet execution |
| **Autonomous execution** | **Does not exist** | ❌ | Not planned | Would require every gate above plus standing human-in-the-loop controls |

Legend: ✅ allowed now (within documented flags/limits) · 🔜 planned, not yet built · ❌ forbidden — do not implement, scaffold, or "prepare".
