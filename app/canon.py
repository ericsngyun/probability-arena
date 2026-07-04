"""Project canon constants: the single source of truth for what this system
is, what agents may build, and what is forbidden. Consumed by
`python -m app.cli agent-context` and by tests that keep the docs honest.

Update this file (and docs/PROJECT_CANON.md) when a milestone lands."""

PROJECT_NAME = "probability-arena"

CURRENT_PHASE = (
    "Read-only intelligence + calibration accumulation (through SOCCER-001). "
    "No EV, no trading of any kind. Canary flags exist for baseball external "
    "research, evidence-aware baseball forecasting, and soccer external research."
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
)

EXPECTED_SERVICES_EVO_X2 = (
    "probability-arena-baseline.timer (systemd user, every 4h)",
    "probability-arena-retention.timer (systemd user, daily)",
    "probability-arena-watcher.service (systemd user, 60s loop)",
)

NEXT_MILESTONES = (
    "Accumulate resolved outcomes; read champion-challenger-report until the "
    "sample supports a verdict (MVP-004G shipped the comparison layer)",
    "MVP-005A EV precheck (design + safety review only; requires paired "
    "champion/challenger evidence of edge)",
    "MVP-005B paper simulator (requires MVP-005A acceptance)",
    "CRYPTO-001 read-only crypto scout (separate track, read-only)",
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
    "enable_baseball_external_research",
    "enable_soccer_external_research",
    "enable_llm_resolution",
    "enable_llm_forecasting",
    "enable_baseball_evidence_forecasting",
    "enable_realtime_watcher",
    "enable_watcher_retention",
    "enable_pipeline_retention",
)
