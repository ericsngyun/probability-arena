"""Project canon constants: the single source of truth for what this system
is, what agents may build, and what is forbidden. Consumed by
`python -m app.cli agent-context` and by tests that keep the docs honest.

Update this file (and docs/PROJECT_CANON.md) when a milestone lands."""

PROJECT_NAME = "probability-arena"

CURRENT_PHASE = (
    "Read-only market intelligence + calibration/follow-through measurement "
    "(through EVAL-001). No EV, no trading of any kind. MarketOps Autopilot "
    "coordinates the read-only loop behind flags. Canary flags exist for "
    "baseball/soccer/tennis evidence forecasting and baseball/soccer external "
    "research. Crypto Arena: provider-governed read-only Solana surveillance + "
    "risk engine (a risk score is an avoid/flag verdict, never a trade "
    "recommendation), lifecycle tape, bounded frozen-cohort horizon observation "
    "and explicitly-armed one-shot orchestration, shared-candidate feasibility "
    "analysis, and a measurement-only candidate-readiness signal whose 14-day "
    "observation window CLOSED 2026-07-31 with a PASS (both checkpoints done; the "
    "hook remains enabled pending an explicit keep-on/turn-off decision). "
    "Polymarket observation, "
    "cross-venue comparability, and tennis market/tape research are read-only "
    "measurement lanes."
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
    "crypto discovery provider gate (explicit run-scoped provider policy; deny "
    "overrides flags/fallbacks; paid providers need per-provider confirmation; "
    "fail-closed before any request; read-only, never a trade action)",
    "crypto lifecycle tape (read-only replayable token lifecycle recording + "
    "survival labels, derived from already-persisted rows)",
    "crypto retrospective analysis (compute-on-demand feature/outcome "
    "separation measurement; conservative labels, never advice)",
    "crypto tape coverage forensics (compute-on-demand gap-cause decomposition "
    "+ shadow selection analysis; changes no stored label or live selection)",
    "crypto horizon observation (bounded frozen-cohort market/liquidity "
    "observations near lifecycle horizons via DexScreener)",
    "crypto horizon scheduling (compute-on-demand manual timing/reminder reports; "
    "no provider calls, persistence, timers, or automatic observation)",
    "crypto horizon one-shot orchestration (explicitly armed user-systemd jobs "
    "for existing fixed cohorts; planner-gated, bounded, no recurring timer/daemon)",
    "explicit-token horizon cohort selection (COHORT-SELECT-001/002: "
    "complete-lifecycle-anchor filter + exact canonical-token-id freeze; "
    "zero external calls, no substitution, no automatic admission)",
    "crypto shared-candidate feasibility analysis (compute-on-demand completeness "
    "funnel + shared 15m-window feasibility over persisted births; zero calls, no writes)",
    "crypto candidate-readiness measurement (local operational readiness signal for "
    "the manually-authorized shared-pass canary; isolated default-off MarketOps hook "
    "+ read-only reports; zero calls, creates no cohort/observation/arming)",
    "crypto anchor-feed measurement (exact-cycle provider-free birth-anchor "
    "materialization from the same natural discovery cycle via the existing "
    "lifecycle tape; isolated default-off MarketOps hook; zero provider calls, "
    "bounded per-cycle cap, creates no cohort/observation/arming)",
    "backup freshness measurement (local, measurement-only check that the "
    "canonical BACKUP_DIR still holds a recent (<=36h), committed, "
    "structurally valid, manifest-backed backup; isolated default-off "
    "MarketOps hook + always-available read-only report; zero provider "
    "calls, never runs/prunes/mutates a backup, cannot fail a MarketOps cycle)",
    "marketops autopilot (read-only coordination: promote/process/scan/score/report/alert)",
    "edge precheck (probability-gap measurement only; no EV, no advice, no actions)",
    "frontier evaluation (full-desk measurement quality + conservative readiness labels)",
    "Polymarket observation (read-only public Gamma catalog + CLOB books; behind flag)",
    "cross-venue semantic comparability measurement (persisted-row-only "
    "Kalshi<->Polymarket matcher + observation reports; no external calls; never EV/arbitrage)",
    "tennis market/tape measurement (read-only tennis market ticks + tape score/market "
    "snapshots; live-score side pending the Goalserve key)",
    "meme/news surveillance + MEME-MAS shadow analysis (read-only; behind default-off flags)",
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
    "probability-arena-marketops.timer (systemd user, every 5min — MarketOps "
    "Autopilot; runs the default-off candidate-readiness measurement hook when "
    "MARKETOPS_INCLUDE_CANDIDATE_READINESS is enabled; runs the default-off "
    "backup-freshness measurement hook at step 7b when "
    "MARKETOPS_INCLUDE_BACKUP_FRESHNESS_ALERT is enabled)",
    "probability-arena-watcher.service (systemd user, 60s loop)",
    "probability-arena-meme-news.timer (systemd user, every 10min)",
    "probability-arena-tick-aggregation.timer (systemd user, hourly — storage plumbing)",
    "probability-arena-retention.timer (systemd user, daily)",
    "probability-arena-backup.timer (systemd user, daily 01:30 UTC — verified "
    "SQLite backup to BACKUP_DIR on /mnt/data; SQLITE-BACKUP-COORDINATION-001)",
    "(explicitly-armed crypto-horizon one-shot user timers are transient/per-cohort "
    "and self-remove — not continuously-expected services)",
)

NEXT_MILESTONES = (
    "Crypto candidate-readiness measurement window CLOSED 2026-07-31: both the "
    "7-day and 14-day checkpoints PASSED (docs/CRYPTO_HORIZON_READINESS_CHECKPOINT_"
    "14D_2026_07_30.md). Both MARKETOPS_INCLUDE_CANDIDATE_READINESS and "
    "MARKETOPS_INCLUDE_CRYPTO_TAPE_ANCHOR_FEED are still true on EVO-X2 and still "
    "measurement-only (no cohort creation/arming) — whether to keep them on now that "
    "the window has closed is an OPEN human decision, not an assumption",
    "CANARY-004 (shared-pass horizon canary) is the RECOMMENDED next step per the "
    "14-day verdict, but arming still requires explicit human approval given AT a "
    "live readiness moment, never retroactively. Pair scarcity is no longer the "
    "constraint: Epoch 4 saw 496 distinct live pairs (~66/day) with 62% persisting "
    "across >=2 consecutive cycles — the binding constraint is now approval timing",
    "CRYPTO-DISCOVERY-FRESHNESS-001 SUPERSEDED — close or re-scope before building. "
    "Its premise (SHARED-CANDIDATE-FEASIBILITY-001: 'the discovery source is the "
    "blocker') was overturned by the 14-day evidence: the real blocker was "
    "birth-anchor production, now fixed by the exact-cycle anchor feed (lag ~85min "
    "-> median 17.2s; 100% of anchors persist while 15m-feasible, vs 8.7% before)",
    "Storage gate wants its own OPS milestone: the DB is at ~132% of the 3072MiB "
    "app-level gate (295 open critical db_growth_warning alerts), growth dominated "
    "by market_price_ticks (~2.1GB). Pre-existing since 2026-07-05, NOT attributable "
    "to the crypto lanes; host itself is fine (62% used). Retention/aggregation "
    "decision, not a horizon decision",
    "Freeze-deferred merges are now eligible (the window they waited on has closed): "
    "SQLITE-LOCK-TELEMETRY-001B, the CLI decomposition, and forecast PRs #1/#2. "
    "PR#2 is stacked on PR#1's branch — merge PR#1 to main first, then rebase/"
    "retarget PR#2. None merged yet; order is the operator's call",
    "Measurement-only forecast reports (scorability audit, reliability decomposition) "
    "may be developed separately — read-only, no forecast/gate/label change",
    "Goalserve-backed tennis live-state work blocked pending the API key",
    "Accumulate paired outcomes toward useful_sample (n>=100); retired EDGE-SELECTION "
    "policies remain retired (EDGE-RETIRE-001; resurrection needs a new prereg+lock)",
    "EV, paper trading, portfolio sizing, wallet/key handling, transaction "
    "construction/signing, swaps, and live execution remain UNAUTHORIZED and require "
    "explicit, separately-accepted milestones before any surface may exist "
    "(see docs/SAFETY_BOUNDARIES.md) — none are a current next step",
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
    "marketops_include_candidate_readiness",
    "marketops_include_crypto_tape_anchor_feed",
    "enable_crypto_tape_reconciler",
    "marketops_include_backup_freshness_alert",
    "marketops_include_edge_precheck",
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
    "migration_mode",
)
