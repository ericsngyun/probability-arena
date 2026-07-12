"""Project canon constants: the single source of truth for what this system
is, what agents may build, and what is forbidden. Consumed by
`python -m app.cli agent-context` and by tests that keep the docs honest.

Update this file (and docs/PROJECT_CANON.md) when a milestone lands."""

PROJECT_NAME = "probability-arena"

CURRENT_PHASE = (
    "Read-only intelligence + calibration accumulation (through EVAL-001). "
    "No EV, no trading of any kind. Canary flags exist for baseball external "
    "research, evidence-aware baseball and soccer forecasting, and soccer external research. "
    "Crypto Arena adds read-only Solana memecoin surveillance plus a risk "
    "engine (a risk score is an avoid/flag verdict, never a trade "
    "recommendation); MarketOps Autopilot coordinates it all behind flags."
)

ALLOWED_CAPABILITIES = (
    "market scanning (read-only Kalshi GETs)",
    "eligibility gating (deterministic)",
    "detail enrichment (market/event/series GETs)",
    "resolution assessment (rule-based; LLM behind flag)",
    "research packets (template; baseball + soccer external canaries behind flags)",
    "probability forecasting (template baseline; baseball evidence canary behind flag)",
    "outcome sync + calibration scoring (Brier / log loss)",
    "real-time watching + informational opportunity signals",
    "signal promotion + intelligence refresh workflow",
    "retention pruning of our own operational tables",
    "crypto scouting (read-only Solana token/pair surveillance + risk telemetry)",
    "crypto risk engine (heuristic + provider risk scoring; avoid/flag verdicts only)",
    "crypto lifecycle tape (read-only replayable token lifecycle recording + "
    "survival labels, derived from already-persisted rows)",
    "marketops autopilot (read-only coordination: promote/process/scan/score/report/alert)",
    "edge precheck (probability-gap measurement only; no EV, no advice, no actions)",
    "frontier evaluation (full-desk measurement quality + conservative readiness labels)",
)

FORBIDDEN_CAPABILITIES = (
    "EV calculation",
    "trade recommendations",
    "paper trading",
    "portfolio sizing",
    "order placement",
    "wallet / private-key handling",
    "live trading / execution",
    "autonomous trading",
    "crypto wallets",
    "swaps / transaction construction / signing (Jupiter or any DEX)",
)

EXPECTED_SERVICES_EVO_X2 = (
    "probability-arena-baseline.timer (systemd user, every 4h)",
    "probability-arena-retention.timer (systemd user, daily)",
    "probability-arena-watcher.service (systemd user, 60s loop)",
)

NEXT_MILESTONES = (
    "Accumulate paired outcomes toward useful_sample (n>=100) and edge-precheck "
    "measurement data (MVP-005A shipped; gate crossed at paired n=36)",
    "MVP-005B paper simulator (requires MVP-005A acceptance)",
    "CRYPTO-003 crypto paper simulator (gated like MVP-005B; requires "
    "CRYPTO-002 risk data maturity)",
    "WALLET-001 policy-controlled transaction PROPOSAL gateway only "
    "(no signing/keys; much later, dedicated security review)",
)

CANON_DOCS = (
    "AGENTS.md",
    "docs/PROJECT_CANON.md",
    "docs/SAFETY_BOUNDARIES.md",
    "docs/CAPABILITY_MATRIX.md",
    "docs/ROADMAP.md",
    "docs/EVO_X2_RUNBOOK.md",
    "docs/FEATURE_FLAGS.md",
    "docs/TESTING_POLICY.md",
    "docs/ADR/",
)

KEY_FEATURE_FLAGS = (
    "enable_external_research",
    "enable_crypto_scout",
    "enable_crypto_risk_provider",
    "enable_crypto_risk_engine",
    "enable_marketops_autopilot",
    "enable_edge_precheck",
    "enable_baseball_external_research",
    "enable_soccer_external_research",
    "enable_llm_resolution",
    "enable_llm_forecasting",
    "enable_baseball_evidence_forecasting",
    "enable_soccer_evidence_forecasting",
    "enable_realtime_watcher",
    "enable_watcher_retention",
    "enable_pipeline_retention",
)
