# Incident — crypto discovery provider-boundary breach (2026-07-15)

**Severity:** low (read-only cost only; within budget guardrails; no trading, capital, state-corruption, or safety impact)
**Status:** contained; corrective follow-up milestone proposed (`CRYPTO-DISCOVERY-PROVIDER-GATE-001`)
**Session:** `CRYPTO-HORIZON-ORCHESTRATOR-CANARY-001` preparation

## Summary

While preparing a fresh-token canary cohort, the operator was authorized to "run the existing bounded fresh-token discovery process" but was **absolutely prohibited from using SolanaTracker**. The discovery command `crypto-scan-once` inherently invokes the always-on crypto risk engine, which performs provider-backed enrichment including **SolanaTracker** lookups. A pre-flight check confirmed only the `get_risk_provider()` path was inert; it did not cover the risk-**engine** path. The scan therefore made SolanaTracker calls, breaching the boundary. This was detected immediately via the provider-budget delta, the pipeline was halted, and **no further provider calls were made** — the remaining canary was completed exclusively from already-persisted data after a full static call-path proof that every remaining command is local/DB-only.

## Breach event

| field | value |
|---|---|
| command that caused the breach | `python -m app.cli crypto-scan-once --limit 40` |
| scan ID | **`#2873`** (`ok tokens=20 pairs=40 ticks=40 signals=7 in 16917ms`) |
| start (UTC) | **2026-07-15 22:18:20** |
| completion (UTC) | ~2026-07-15 22:18:37 (16.9 s) |

### SolanaTracker counters (paid provider — budget-metered)

| counter | before (22:18:20) | after (22:18:37) | delta |
|---|---|---|---|
| `hour` | 45 | 60 | **+15** |
| `today` | 3,345 | 3,360 | **+15** |
| `month` | 29,813 | 29,828 | **+15** |
| `rolling_24h` | 3,600 | 3,615 | +15 |

The delta of **+15 equals `per_run_lookup_limit=15`** — the risk engine's per-run SolanaTracker lookup cap. Usage stayed well within guardrails (`warn_daily=4,000`, `stop_daily=6,000`; `remaining_daily` ~1,640 after). A concurrent background scan also runs on this shared host, so host-wide SolanaTracker usage drifts independently; nonetheless the scan command's own risk-engine path is SolanaTracker-backed and is the cause of the breach.

### GoPlus usage (free provider — not budget-metered)

GoPlus `token_security` GET requests were made by the same risk-engine path — approximately one per checked token (`tokens=20`), visible in the scan's `httpx` logs (e.g. `GET https://api.gopluslabs.io/api/v1/solana/token_security?...`). GoPlus is free and carries no budget counter, so no exact metered count exists; the observed pattern is ~1 lookup per checked token. GoPlus is **not** in the session prohibition list, but is recorded here for completeness.

### Persisted rows/ticks created by the breaching scan

- Scan `#2873`: `tokens=20`, `pairs=40`, `ticks=40`, `signals=7` (ordinary crypto-arena surveillance rows).
- No horizon/orchestrator artifacts were created by the scan itself.

## Root cause

`app/services/crypto_scout.py :: CryptoScoutService.scan_once` selects the risk path at ~line 581:

```python
if self.risk_engine is not None and pair_states:
    evaluation = await self.risk_engine.evaluate(session, token=..., pair=..., tick=..., ...)
    risk = evaluation.as_signal_view()
elif self.risk_provider is not None:            # <- the ONLY path the pre-flight checked
    risk = await self.risk_provider.assess(token_address)
```

The **risk-engine** branch (line 581) is gated by `ENABLE_CRYPTO_RISK_ENGINE` and performs provider-backed enrichment (GoPlus + SolanaTracker, capped at 15 SolanaTracker lookups/run). The **risk-provider** branch (line 594) is gated by `ENABLE_CRYPTO_RISK_PROVIDER` via `get_risk_provider()`.

### Flag distinction

| flag | value on EVO-X2 | gates | pre-flight covered it? |
|---|---|---|---|
| `ENABLE_CRYPTO_RISK_PROVIDER` | **False** | `get_risk_provider()` → the `risk_provider.assess` branch | yes → returned `None` (inert) |
| `ENABLE_CRYPTO_RISK_ENGINE` | **True** | `risk_engine.evaluate` → GoPlus **+ SolanaTracker** enrichment | **no — missed** |

### Why the pre-flight was insufficient

The pre-flight reasoned only about `get_risk_provider()` (the `risk_provider` branch) and concluded the scan would be DexScreener-only. It did **not** grep the scout for the independent, always-on `risk_engine.evaluate` path, which is the actual SolanaTracker caller and is gated by a *different* flag (`ENABLE_CRYPTO_RISK_ENGINE=True`). A correct pre-flight would have statically traced **every** provider/enrichment call in `scan_once`, not just the one abstraction. This is exactly the gap the proposed follow-up milestone closes.

## Containment

- Breach detected immediately from the provider-budget delta (+15); the discovery pipeline was halted.
- **No further provider calls occurred after detection.** Before continuing, a complete static call-path inspection proved the entire remaining path (`crypto-tape-run-once`, `crypto-horizon-cohort-create`, all four horizon reports, `crypto-horizon-arm-cohort --dry-run`, `crypto-horizon-orchestrator-report`, `crypto-provider-budget-report`) is local/DB-only — none construct or reach `DexScreenerAdapter`, `SolanaTracker`, `GoPlus`, any HTTP client, or any fallback/refresh path. SolanaTracker counters were snapshotted immediately before and after **every** subsequent command; each showed **before == after** (zero self-attributable calls).
- **At the moment of detection: no horizon cohort, unit, timer, manifest, or arming existed.** Cohort `4` (SBULL) was created only afterwards, under the zero-call regime with explicit human approval; it made zero provider calls.

## Final provider-counter comparison

| checkpoint (UTC) | ST `hour` / `today` / `month` | self-attributable ST calls |
|---|---|---|
| pre-scan 22:18:20 | 45 / 3,345 / 29,813 | — |
| post-scan 22:18:37 | 60 / 3,360 / 29,828 | **+15 (breach)** |
| tape-run-once (before==after) | 90 / 3,390 / 29,858 | 0 |
| cohort-create (before==after) | 105 / 3,405 / 29,873 | 0 |
| reports/schedule (before==after) | 105 / 3,405 / 29,873 | 0 |
| arm --dry-run (before==after) | 105 / 3,405 / 29,873 | 0 |
| orchestrator-report + reports (before==after) | 120 / 3,420 / 29,888 | 0 |

(Absolute values rise between checkpoints because a background scan on this shared host consumes SolanaTracker independently; the invariant that matters — **before == after around each of my commands** — held for every command after the breach.)

## Proposed follow-up milestone (NOT implemented in this session)

**`CRYPTO-DISCOVERY-PROVIDER-GATE-001` — explicit, fail-closed provider gating for discovery.** Scope proposal:

1. A **provider execution plan / pre-flight** that enumerates exactly which providers each discovery run will call (DexScreener / GoPlus / SolanaTracker), derived from the actual call graph — not from a single flag.
2. **Fail closed**: discovery aborts before any network call unless the resolved provider set is explicitly permitted for the invocation.
3. **Explicit confirmation required for paid providers** (SolanaTracker) — a `--dry-run`/plan mode that prints the provider set and makes zero calls, plus an explicit opt-in to proceed.
4. A **genuinely provider-free discovery path** (or a clearly-labelled "compose-from-persisted-only" mode) if technically supportable, so a fresh cohort can be sourced without paid enrichment.
5. Regression tests asserting the pre-flight's provider set matches the real call graph, and that fail-closed blocks an un-permitted paid provider.

## Lesson

Never certify a command as provider-free from a single abstraction (`get_risk_provider()`) or a single flag. Statically trace **every** enrichment/provider/HTTP call in the command's call graph before running it. When an authorization ("run discovery") conflicts with an absolute prohibition ("no SolanaTracker"), the **prohibition wins** and the conflict must be surfaced *before* execution, not after.
