from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://arena:arena@localhost:5432/probability_arena"

    redis_url: str = "redis://localhost:6379/0"
    candidates_cache_ttl_seconds: int = 30

    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_request_timeout_seconds: float = 10.0
    # Server-side filter for auto-generated multivariate/parlay markets;
    # "exclude" keeps them out of scans entirely, "" fetches everything.
    kalshi_mve_filter: str = "exclude"

    # Observer-specific credential configuration. Deliberately NOT named
    # `kalshi_api_key_id` / `kalshi_private_key_path`: a generic Kalshi
    # credential is one any Kalshi subsystem can pick up, so the blast radius
    # of a mis-scoped key would be every caller rather than one loader. These
    # names are read by `app.realtime.auth` and by nothing else, which is
    # asserted structurally in the test suite.
    #
    # The values are a KEY ID and a FILESYSTEM PATH. The PEM contents must
    # never appear in the environment: environment variables are readable from
    # /proc, leak into `docker inspect`, and survive in shell history.
    kalshi_observer_api_key_id: str = ""
    # Named `credential_path`, not `private_key_path`. The milestone suggested
    # the latter, but it puts the fragment `private_key` into config.py and so
    # requires a safety-audit allowlist entry for a field that holds a PATH and
    # never key material. Avoiding the name keeps "exactly one private-key
    # surface in the repository" literally true with zero allowlist exemptions,
    # which is the stronger form of the same guarantee.
    kalshi_observer_credential_path: str = ""
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    kalshi_ws_tickers: str = ""

    scanner_max_markets: int = 500
    candidates_default_limit: int = 25

    # Targeted game-level market scans (SCANNER-002/OPS-010) — read-only
    # supplement to the generic scan: fetch supported, measurable series
    # (game winner / totals / spreads) directly by series_ticker so they are
    # never crowded out of the first `scanner_max_markets` page by props.
    # Coverage only: no EV, no advice, no trading capability of any kind.
    enable_targeted_market_scans: bool = True
    targeted_market_series: str = (
        "KXWCGAME,KXWCTOTAL,KXWCSPREAD,KXMLBGAME,KXMLBTOTAL,KXMLBSPREAD"
    )
    targeted_market_scan_limit_per_series: int = 250
    targeted_market_scan_active_only: bool = True
    targeted_market_scan_dedup: bool = True
    # Watcher supported-universe supplement bound (game-level baseball/soccer
    # markets only; player props are excluded by market type, never unlimited)
    watcher_supported_universe_limit: int = 50

    # Resolution-criteria assessment (MVP-003B)
    enable_llm_resolution: bool = False
    resolution_model_name: str = "claude-opus-4-8"
    resolution_prompt_version: str = "v1"
    min_clarity_score: float = 0.70

    # Research packet collection (MVP-004A)
    enable_external_research: bool = False
    research_collector_name: str = "template"
    research_collector_version: str = "v1"
    research_model_name: str = "claude-opus-4-8"

    # Baseball external research canary (MVP-004E) — narrow scope: promoted
    # sports_baseball signals only; everything else stays on templates
    enable_baseball_external_research: bool = False
    baseball_research_timeout_seconds: float = 15.0
    baseball_research_max_sources: int = 8
    baseball_research_collector_version: str = "v1"

    # Soccer external research canary (SOCCER-001) — narrow scope: promoted
    # sports_soccer signals only; everything else stays on templates.
    # Provider "template" keeps the collector fallback-only even when the
    # flag is on; "espn" enables the read-only public ESPN soccer API.
    enable_soccer_external_research: bool = False
    soccer_research_provider: str = "template"
    soccer_research_timeout_seconds: float = 15.0
    soccer_research_max_sources: int = 8
    soccer_research_collector_version: str = "v1"

    # Soccer evidence-aware forecasting canary (SOCCER-002) — consumes
    # source-backed soccer packets; no external calls of its own. Forecasts
    # are measurement inputs only: no EV, no trade semantics.
    enable_soccer_evidence_forecasting: bool = False
    soccer_forecaster_version: str = "v1"
    soccer_forecast_max_confidence: float = 0.70
    soccer_forecast_min_completeness: float = 0.75

    # Baseball evidence-aware forecasting canary (MVP-004F) — consumes
    # source-backed MLB packets; no external calls of its own
    enable_baseball_evidence_forecasting: bool = False
    baseball_forecaster_version: str = "v1"
    baseball_forecast_max_confidence: float = 0.70
    baseball_forecast_min_completeness: float = 0.75

    # Tennis external research canary (TENNIS-001) — narrow scope: promoted
    # sports_tennis MATCH-WINNER signals only; everything else stays on
    # templates. Provider "template" (default) keeps the collector
    # fallback-only even when the flag is on; "espn" selects a read-only public
    # ESPN tennis client whose live payload mapping is PENDING validation (it
    # degrades to honest template fallback if the shape does not match).
    # Read-only research only: no EV, trade, sizing, order, wallet, or execution.
    enable_tennis_external_research: bool = False
    tennis_research_provider: str = "template"
    # TENNIS-PROVIDER-001: API key for the api_tennis provider scaffold.
    # Default empty = the fetcher makes NO request and reports provider_gap
    # honestly. Set only on the host .env, never committed; report
    # presence/absence only, never the value.
    tennis_provider_api_key: str = ""
    # TENNIS-GOALSERVE-001: Goalserve fallback live-state validation key.
    # Goalserve embeds the key in the URL PATH, so URLs are never logged or
    # echoed (a masked display URL exists for reports). Default empty = no
    # request. Host .env only; never committed; presence/absence only.
    goalserve_tennis_api_key: str = ""
    tennis_research_timeout_seconds: float = 15.0
    tennis_research_max_sources: int = 8
    tennis_research_collector_version: str = "v1"

    # Tennis evidence-aware forecasting canary (TENNIS-001) — consumes
    # source-backed tennis packets; no external calls of its own. Match-winner
    # markets only in v1; conservative confidence cap. Measurement inputs only.
    enable_tennis_evidence_forecasting: bool = False
    tennis_forecaster_version: str = "v1"
    tennis_forecast_max_confidence: float = 0.65
    tennis_forecast_min_completeness: float = 0.75

    # Retention / pruning (OPS-003) — operational tables only; intelligence
    # and calibration tables are never pruned
    tick_retention_days: int = 7
    watcher_run_retention_days: int = 30
    pipeline_run_retention_days: int = 90
    signal_retention_days: int = 0  # 0 = keep signals indefinitely
    retention_batch_size: int = 5000

    # OPS-012: tick aggregation (storage/durability plumbing only — aggregated
    # buckets are telemetry summaries, never trading signals; no EV/trade/
    # sizing/order/wallet/execution semantics). Raw tick retention is UNCHANGED
    # in OPS-012; a future OPS milestone may reduce it only after the
    # tick-aggregation-report proves coverage is healthy.
    tick_aggregation_bucket_seconds: int = 300   # 5-minute buckets (must divide 3600)
    tick_aggregation_max_rows: int = 200_000     # bounded raw rows read per invocation
    tick_bucket_retention_days: int = 90         # aggregated buckets kept much longer than raw

    # TENNIS-WATCHER-001: read-only tennis market tick capture (market
    # observation only — no forecasting, no signals, no trading semantics).
    # The flag gates ONLY a future scheduled path (manual runs always
    # allowed); default OFF. Ticks reuse market_price_ticks and its existing
    # raw retention window.
    enable_tennis_tick_watcher: bool = False
    tennis_tick_watch_limit: int = 200

    # COST-MODEL-001: friction assumption for the read-only cost-adjusted
    # SHADOW report (edge-cost-shadow-report) ONLY. Conservative Kalshi
    # taker-fee model: the published fee is ceil(0.07 * C * P * (1-P)) per
    # contract; the report charges rate * P * (1-P) at BOTH the trigger and
    # the horizon (full round trip, no maker rebates, no rounding down).
    # This is a measurement assumption — it is never used to compute EV,
    # recommend, size, or place anything.
    kalshi_fee_rate_assumption: float = 0.07

    # OPS-013: production-safe aggregation. Per-sub-window commits keep the
    # SQLite write lock held for seconds (an OPS-012 full-window pass held one
    # ~49s commit and collided with a MarketOps cycle). The scheduled path is
    # gated by ENABLE_TICK_AGGREGATION_TIMER (default false — the timer unit is
    # NOT auto-installed and no-ops while false); manual runs always allowed.
    # Raw tick retention is UNCHANGED — reduction stays a staged, separately
    # accepted decision informed by the readiness report.
    enable_tick_aggregation_timer: bool = False
    tick_aggregation_subwindow_hours: int = 1        # commit after each sub-window
    tick_aggregation_busy_retries: int = 3           # commit retries on a locked DB
    tick_aggregation_busy_retry_seconds: float = 2.0
    tick_aggregation_max_rows_per_subwindow: int = 100_000  # runaway guard (loud skip)
    tick_aggregation_scheduled_hours: int = 12       # window per scheduled cycle

    # OPS-011 alert calibration — advisory operational alerts only; NOT trading
    # logic. Static thresholds raised after SCANNER-002 grew the watcher/tick
    # universe (512 MiB / 150 signals-per-hour were chronically tripped by
    # normal live-slate volume). warning/critical are the active alert gates;
    # daily-rate + window are observability knobs surfaced by db-growth-report
    # (rate-based ALERTING is documented as future work — see docs/ROADMAP.md).
    db_growth_warning_mb: float = 1536.0
    db_growth_critical_mb: float = 3072.0
    db_growth_warning_daily_mb: float = 1024.0  # observability/proposed
    db_growth_window_hours: int = 24  # observability/proposed
    marketops_signal_flood_warning_per_hour: int = 400
    marketops_signal_flood_critical_per_hour: int = 800
    enable_pipeline_retention: bool = False
    enable_watcher_retention: bool = False

    # Real-time opportunity watcher (OPS-002) — informational signals only
    enable_realtime_watcher: bool = False
    watcher_poll_interval_seconds: int = 60
    watcher_market_limit: int = 100
    watcher_price_move_threshold: float = 0.07  # dollars of midpoint move
    watcher_max_spread: float = 0.15  # dollars; spread_tightened crosses into this band
    watcher_min_liquidity_proxy: int = 100  # cents of resting notional
    watcher_signal_cooldown_seconds: int = 900

    # Baseline pipeline runner (MVP-004D) — scheduled read-only measurement loop
    baseline_scan_limit: int = 500
    baseline_candidate_limit: int = 20
    baseline_fail_fast: bool = False
    baseline_sync_outcome_limit: int = 200
    baseline_score_limit: int = 1000

    # Forecast engine (MVP-004B) — probabilities and reasoning artifacts only
    enable_llm_forecasting: bool = False
    forecaster_name: str = "template_baseline"
    forecaster_version: str = "v1"
    forecast_prompt_version: str = "v1"
    forecast_model_name: str = "claude-opus-4-8"
    template_only_max_confidence: float = 0.55
    source_backed_max_confidence: float = 0.75
    missing_critical_info_max_confidence: float = 0.50

    # MarketOps Autopilot (OPS-006) — read-only coordination of existing
    # services: promote -> process -> crypto scan -> sync/score -> compare ->
    # report -> local DB alerts. No EV, no trading, no execution of any kind.
    # The flag gates ONLY the loop/timer; marketops-run-once is always allowed.
    enable_marketops_autopilot: bool = False
    marketops_promote_limit: int = 5
    marketops_process_limit: int = 5
    marketops_crypto_scan_limit: int = 100
    marketops_sync_outcome_limit: int = 500
    marketops_score_limit: int = 1000
    marketops_min_signal_age_seconds: int = 30
    marketops_max_signal_age_hours: int = 24
    # OPS-009 minute-level, domain-aware freshness. Minutes supersede the
    # hour knob (which is kept as a coarse upper bound for compatibility:
    # the effective window is min(domain minutes, hours*60)).
    marketops_max_signal_age_minutes: int = 60
    marketops_live_sports_max_signal_age_minutes: int = 20
    marketops_soccer_max_signal_age_minutes: int = 20
    marketops_baseball_max_signal_age_minutes: int = 20
    marketops_general_max_signal_age_minutes: int = 60
    # Reserved: crypto signals are NOT governed by marketops promotion; this
    # key exists for a possible later milestone and is unused in OPS-009.
    marketops_crypto_signal_age_minutes: int = 60
    marketops_include_crypto: bool = True
    marketops_include_probability_markets: bool = True
    marketops_fail_fast: bool = False
    marketops_loop_interval_seconds: int = 300
    # OPS-007: a 'running' marketops run older than this is treated as stale
    # (crashed) and no longer blocks new cycles
    marketops_lock_stale_after_minutes: int = 30

    # OPS-007 operational hardening
    sqlite_busy_timeout_ms: int = 30000  # applied to SQLite connections only
    backup_retention_days: int = 30
    backup_dir: str = "data/backups"

    # CRYPTO-COVERAGE-REPAIR-001 B10 — migration governance. Ordinary runtime
    # (roughly 100 call sites in app/cli.py, plus app/main.py's FastAPI
    # startup) used to call `run_migrations()` unconditionally, so any `git
    # pull` got its pending Alembic migrations auto-applied by the next
    # 5-minute MarketOps timer tick — ahead of any operator step, ahead of
    # any backup. This setting is the explicit, named switch between the two
    # allowed behaviours; it is NEVER inferred from a hostname, a path, or
    # any other brittle signal.
    #   guarded (default, SAFE) - runtime CHECKS the schema revision; if the
    #     database is behind the code's required head, it raises
    #     `MigrationRequiredError` (`MIGRATION_REQUIRED`) and does nothing
    #     else. Deployment owns the upgrade (backup-freshness check, explicit
    #     `alembic upgrade head`, integrity + revision verification, then
    #     runtime is permitted) — see `app.db.ensure_schema_current` and
    #     `docs/EVO_X2_RUNBOOK.md`.
    #   auto - restores the pre-B10 always-upgrade behaviour (stamp legacy
    #     `create_all` databases, then `alembic upgrade head`, on every call).
    #     Convenient for local development and first-install bootstrapping.
    #     Must be set DELIBERATELY (`MIGRATION_MODE=auto`); an unconfigured
    #     deployment stays on the safe default.
    # Any other value is treated as `guarded` (fail closed on typos too).
    migration_mode: str = "guarded"

    # RAW-PAYLOAD-STORAGE-001: whether the FULL provider response body is
    # persisted alongside the normalized columns extracted from it. Default
    # "full" preserves current behaviour exactly, so deploying the code changes
    # nothing until a host explicitly opts in.
    #   full - store the complete body (today's behaviour)
    #   none - never store the body; keep bounded provenance only
    # (An `errors_only` mode was designed and dropped: an error body is the
    #  payload class most likely to echo the request URL, and this repo sends a
    #  provider key in a query string, so no writer could be allowed to keep one
    #  without a redaction pass — which made the mode behave identically to
    #  `none` for every column. A value that cannot behave differently from
    #  another is a misconfiguration trap.)
    # Columns with a proven production reader are PINNED to full regardless of
    # this setting (app/services/raw_payload_policy.PINNED_FULL). An
    # unrecognised value fails CLOSED to "full" — never to "none".
    raw_payload_capture_mode: str = "full"

    # Edge precheck (MVP-005A) — probability-gap MEASUREMENT only. Records
    # forecast_probability - market_midpoint with validity checks. No dollar
    # EV, no trade recommendations, no sizing, no orders, no execution;
    # paper_candidate_later is a review label with zero attached behavior.
    # Thresholds are PROVISIONAL (design doc §6) pending precheck data.
    # OUTCOME-SYNC-COVERAGE-001. Default OFF so the code can land dark: with it
    # off, outcome-sync and scoring keep their deployed prefix selections
    # byte-for-byte. Turning it on switches BOTH to need-based selection, which
    # on first activation scores the entire un-scored backlog. That is a real
    # write burst, so it gets a flag rather than taking effect the moment the
    # code lands, and it gives a kill switch that is not `git revert`.
    enable_outcome_sync_coverage_repair: bool = False

    enable_edge_precheck: bool = False
    edge_precheck_min_abs_gap: float = 0.05
    edge_precheck_max_spread_cents: int = 10
    edge_precheck_min_liquidity_cents: int = 500
    edge_precheck_min_confidence: float = 0.60
    edge_precheck_max_forecast_age_seconds: int = 900
    edge_precheck_max_live_sports_forecast_age_seconds: int = 300
    edge_precheck_max_market_snapshot_age_seconds: int = 120
    edge_precheck_require_source_backed: bool = True
    edge_precheck_require_researchable: bool = True
    edge_precheck_required_persistence_snapshots: int = 3
    # MVP-005A.1: targeted modes skip a forecast measured within this window
    edge_precheck_dedupe_seconds: int = 120
    # Window/signal-based targeting selects only source-backed forecasts
    # (explicit --forecast-id requests are honored regardless — the
    # not-source-backed status records the gap honestly)
    edge_precheck_target_only_source_backed: bool = True
    marketops_include_edge_precheck: bool = False
    # CRYPTO-HORIZON-CANDIDATE-READINESS-001: default OFF. When true, an isolated,
    # non-blocking, report-only hook runs AFTER the crypto persistence stage and
    # appends one shared-pass readiness evaluation per cycle to an append-only
    # audit. It makes zero provider calls, creates no cohort/observation/unit,
    # and can never change the MarketOps cycle result or exit code. Off = the hook
    # is a complete no-op (deploying the code does not activate live persistence).
    marketops_include_candidate_readiness: bool = False
    # CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001: default OFF. When true, an
    # isolated provider-free hook runs AFTER the crypto persistence stage and
    # BEFORE the candidate-readiness evaluation, materializing canonical birth
    # anchors for EXACTLY the raw tokens newly persisted by that same natural
    # discovery cycle (existing lifecycle-tape logic; no second scan, zero
    # provider calls, bounded per-cycle token cap, idempotent, isolated
    # session). It creates no cohort/observation/unit and can never change the
    # MarketOps cycle result or exit code. Off = a complete no-op.
    marketops_include_crypto_tape_anchor_feed: bool = False
    # SQLITE-BACKUP-FRESHNESS-ALERT-001: default OFF. When true, an isolated,
    # fail-contained hook runs in the operational-health portion of the cycle
    # (adjacent to the db_growth_warning path) and evaluates whether the
    # canonical BACKUP_DIR still holds a recent, committed, structurally valid,
    # manifest-backed backup. It inspects LOCAL FILES ONLY: zero provider calls,
    # never executes or prunes a backup, never modifies a backup file or
    # manifest, adds no timer/daemon, and can never fail the MarketOps cycle or
    # change its exit code. Its only database write is the existing bounded
    # MarketOps alert lifecycle. The 36-hour threshold is a code constant
    # (app.services.backup_freshness.BACKUP_FRESHNESS_MAX_AGE_SECONDS), not a
    # setting, so it cannot be quietly widened. Off = a complete no-op.
    marketops_include_backup_freshness_alert: bool = False

    # Crypto Arena scout (CRYPTO-001) — read-only Solana memecoin
    # surveillance: discovery, price/liquidity ticks, deterministic risk
    # signals. NO wallets, NO swaps, NO transaction building/signing, NO
    # execution of any kind (see docs/SAFETY_BOUNDARIES.md).
    enable_crypto_scout: bool = False  # gates loop/timer use; manual scan always allowed
    crypto_chain: str = "solana"
    crypto_provider: str = "dexscreener"
    crypto_watcher_poll_interval_seconds: int = 60
    crypto_pair_limit: int = 100
    crypto_min_liquidity_usd: float = 5000.0
    crypto_min_volume_5m_usd: float = 1000.0
    crypto_signal_cooldown_seconds: int = 900
    enable_helius: bool = False  # reserved: no Helius adapter exists in CRYPTO-001
    enable_crypto_risk_provider: bool = False
    crypto_risk_provider: str = "mock"
    crypto_retention_days: int = 7  # crypto_price_ticks + crypto_watcher_runs only

    # CRYPTO-COVERAGE-REPAIR-001 — scheduled provider-free reconciliation.
    # DEFAULT OFF. The windowed lifecycle reconciler (CryptoLifecycleTapeRecorder
    # .run_once) already exists and is already proven, but nothing schedules it:
    # production MarketOps only runs the EXACT-CYCLE anchor feed, which by
    # construction processes each token once, at birth, when no horizon is yet
    # due. Consequence: survival horizons never mature, and the evidence needed
    # to mature them is deleted after crypto_retention_days. This flag gates a
    # bounded periodic pass that revisits already-persisted tokens whose
    # horizons have since matured. Zero external calls, zero provider budget,
    # no discovery scan, no cohort, no arming, no execution semantics of any
    # kind. Off = the CLI performs no reconciliation, no migration and no
    # write; it still opens the database to construct a session, so it is a
    # no-op in effect but not literally a process that never touches the file.
    # NOTE the pass is label-idempotent but NOT row-idempotent: each pass
    # appends ~2 rows per token considered (a lifecycle snapshot and an actor
    # observation), and neither table is pruned by retention.py.
    enable_crypto_tape_reconciler: bool = False
    # Steady-state window. Must exceed the longest horizon's closing edge
    # (24h * (1 + HORIZON_TOLERANCE) = 36h) PLUS one scheduling interval, or a
    # token can mature and leave the window between two passes; the guard in
    # run_scheduled_reconciliation enforces this and refuses otherwise. Kept
    # well under crypto_retention_days so bounded DB work, not evidence expiry,
    # is the binding constraint.
    crypto_tape_reconciler_window_hours: int = 48
    # Must cover the whole window, or the pass truncates. Truncation is
    # reported loudly (status=truncated, non-zero exit) rather than silently,
    # and selection is oldest-first so any truncation drops the UNMATURED tail
    # rather than the matured tokens the pass exists to reconcile.
    crypto_tape_reconciler_limit: int = 2000
    # CRYPTO-COVERAGE-REPAIR-001 B3/B6 — write-coordination hardening.
    # MEDIUM: every figure below is session-only evidence from an ad-hoc,
    # non-committed benchmark script — see the "evidentiary status" note in
    # docs/milestones/CRYPTO-COVERAGE-REPAIR-001.md's Write-lock defect
    # section before citing exact numbers elsewhere.
    # The measured blocker: one commit for the whole pass held SQLite's write
    # lock for 36.9s at production density, blocking a competing writer 97%
    # of a 30s busy_timeout. Bounded batches keep each individual commit's
    # lock hold to a small fraction of a second — that part is real and
    # measured (8.5-40.8s max hold -> 0.16-1.73s at 2000 tokens).
    #
    # RESTATED after a third review (NEW-H2): that per-commit hold reduction
    # does NOT mean a competing writer's worst-case wait drops proportionally.
    # SQLite's busy handler loses the lock race against ~80 back-to-back short
    # write transactions almost as easily as against one long one — measured
    # wall-clock competitor blocking was comparable between the legacy and
    # batched shapes (6.79s vs 6.75s and 8.10s vs 8.18s in two reps; in a
    # third, the BATCHED run blocked the competitor for LONGER: 13.68s vs
    # 9.88s). The honest bound on a competing writer's exposure is
    # `crypto_tape_reconciler_max_duration_seconds` (below) plus one batch,
    # not the per-commit hold — at the shipped default that is >=67% of a
    # 30s busy_timeout, not a small fraction of it. The internal deadline
    # keeps one pass from running indefinitely (the remainder simply becomes
    # next-pass backlog, never lost — see `unreconciled_backlog`).
    # `crypto_tape_reconciler_batch_size` was the MEASURED B1 profile
    # default (~1s write phase at 25 tokens/batch on a dev Mac) — NEW-HIGH-1
    # fix (third Lane-B review, SQLite coexistence, DO NOT ACTIVATE finding):
    # that measurement does not transfer to a slower host. The 20s deadline
    # cannot bound a single batch's hold (only evaluated BETWEEN batches),
    # so batch_size alone determines the worst-case write-lock hold, and
    # that hold scales with host per-token cost, not a portable constant.
    # At the reviewer's measured EVO-speed multiplier, batch_size=25
    # produced 26.3-36.5s holds (one of three trials exceeded the 30s
    # busy_timeout) and could not converge against the birth rate at all.
    # Lowered to the reviewer's measured stopgap of 5 (4.56-5.32s worst case
    # at the same host speed, unchanged competitor throughput, better duty
    # cycle) — still a COUNT-based bound, not a time-based one; see
    # `RECONCILE_BATCH_SIZE`'s comment in crypto_tape.py for the real fix
    # this stopgap defers. Change only after measuring a real batch's hold
    # on the TARGET host (`crypto-tape-reconcile --batch-size N --dry-run`),
    # never by trusting this default on an unmeasured host.
    # `crypto_tape_reconciler_max_duration_seconds` is a CHOSEN bound, NOT a
    # measured one: the milestone doc itself states the scheduled path's
    # actual end-to-end wall-clock duration on EVO-X2 has not been
    # re-measured since the batching change. Do not treat it as validated;
    # `crypto-tape-reconcile --max-duration-seconds N --dry-run` exists
    # specifically to measure a real full pass before trusting this default.
    crypto_tape_reconciler_batch_size: int = 5
    crypto_tape_reconciler_max_duration_seconds: float = 20.0

    # CRYPTO-COVERAGE-REPAIR-001 B1/B3 — the structural replacement for the
    # count-based `crypto_tape_reconciler_batch_size` invariant above. See
    # `RECONCILE_WRITE_TIME_SLO_SECONDS`/`AdaptiveBatchCostEstimate` in
    # app/services/crypto_tape.py for the full mechanism. Both default to
    # `None` (adaptive batching OFF, `crypto_tape_reconciler_batch_size`
    # keeps governing byte-for-byte as before) because
    # `crypto_tape_reconciler_initial_per_token_cost_seconds` is an
    # UNCALIBRATED value that only means something once someone has actually
    # measured a real batch's write-phase wall time on the TARGET host (EVO
    # was unreachable — expired Tailscale auth — for the whole pass that
    # built this mechanism, so no such measurement exists yet). Setting only
    # `_time_budget_seconds` without `_initial_per_token_cost_seconds` does
    # NOT activate adaptive mode — the per-token cost is the one input this
    # repo refuses to guess.
    crypto_tape_reconciler_time_budget_seconds: float | None = None
    crypto_tape_reconciler_initial_per_token_cost_seconds: float | None = None

    # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the reconciler's LOCK-WAIT
    # budget, in seconds per SQLite lock acquisition. A DIFFERENT quantity
    # from `crypto_tape_reconciler_time_budget_seconds` above: that one
    # bounds how long a reconciler transaction may HOLD the write lock; this
    # one bounds how long it may WAIT for it.
    #
    # Why this exists: `sqlite_busy_timeout_ms` (30s) is a PER-LOCK-
    # ACQUISITION timeout applied to every connection in the process, and one
    # blocked write statement performs more than one acquisition — measured
    # 1.7-2.9x the configured value before "database is locked" (see
    # RECONCILE_LOCK_WAIT_STATEMENT_OVERSHOOT in app/services/crypto_tape.py
    # for the table). On EVO that turned a `--max-duration-seconds 30` pass
    # into `duration_ms=61,047` with `blocked_ms=45,744`.
    #
    # None (the default, and what ships) means the budget is DERIVED per
    # attempt from the pass's own remaining wall-clock deadline —
    # `max(floor, remaining / 4)` — so the reconciler can never wait past its
    # own deadline. A positive value is an operator CAP on top of that
    # derived budget, never a floor under it. Deliberately no invented
    # absolute default: one production data point cannot set a percentile of
    # the contention distribution. The `lock_wait_ms` histogram this
    # milestone persists on every run row is what a real value would be
    # derived from later.
    crypto_tape_reconciler_lock_wait_budget_seconds: float | None = None

    # CRYPTO-COVERAGE-REPAIR-002 — PROSPECTIVE SPARSE OBSERVATION.
    # Reconciliation is finished and its own numbers say so: of 1,182 finalized
    # outcomes on production only 54 (4.57%) carry a real 24h observation and
    # 1,026 (86.8%) are `permanently_missing_evidence`. The cause is not
    # pruning and not reconciliation capacity — the median token's last tick is
    # ~83 minutes after birth, so 6h/24h evidence was never COLLECTED. This
    # lane collects it going forward: exactly one governed 6h observation and
    # one governed 24h observation per eligible new birth, via the existing
    # read-only DexScreener adapter (no SolanaTracker — structurally denied by
    # a run-scoped provider policy, not by convention).
    #
    # DEFAULT OFF, exactly like `enable_crypto_tape_reconciler`. Off is a clean
    # no-op: no read, no write, no external call. Activation is an operator
    # action (deploy dark -> `--dry-run` -> one `--force` pass -> inspect the
    # observation-coverage report -> flip the flag -> install the timer).
    enable_crypto_sparse_observation: bool = False
    # Per-pass ENROLMENT cap. CHOSEN with a stated margin, not measured:
    # measured EVO births/day (CRYPTO-COVERAGE-REPAIR-001 B7, 2026-08-11) are
    # 392.6 (14d) / 417.3 (7d) / 441.3 (3d) / 517.0 (24h), planning rate ~530,
    # i.e. ~22 births per hourly pass. 200 leaves ~9x headroom and drains a
    # 25h cold-start backlog in ~4 passes.
    crypto_sparse_observation_enrol_limit: int = 200
    # Per-pass OBSERVATION cap == per-pass DexScreener request cap. Each birth
    # is observed exactly twice and the 6h/24h bands never overlap, so steady
    # state is ~44 requests per hourly pass; 100 leaves ~2.3x headroom and is
    # also the horizon lane's own hard adapter cap (OBSERVE_MAX_CALLS), which
    # this value may never exceed.
    crypto_sparse_observation_observe_limit: int = 100
    # Tokens committed per write transaction in the WRITE phase. The fetch
    # phase holds NO transaction at all (network and writes are separate
    # phases, pinned by test), so this bounds only the small, network-free
    # INSERT batches: at most write_batch_size x 2 rows per commit.
    crypto_sparse_observation_write_batch_size: int = 25
    # Wall-clock deadline on the FETCH phase. CHOSEN, not measured on the
    # target host: at the 100-request cap this budgets ~0.9s per request
    # against DexScreener's documented 300 rpm token endpoint. A pass that
    # stops here reports status=partial/stop_reason=deadline and the remaining
    # member-horizons stay selectable while their band is still open.
    crypto_sparse_observation_max_duration_seconds: float = 90.0

    # Crypto risk engine (CRYPTO-002) — read-only risk INTELLIGENCE only.
    # A risk score flags danger for avoidance/review; it is never a trade
    # recommendation, and no execution capability exists anywhere. Provider
    # API keys are optional, sent as request headers only, and never printed.
    enable_crypto_risk_engine: bool = False
    enable_goplus_risk: bool = False
    goplus_api_key: str = ""
    enable_solana_tracker_risk: bool = False
    solana_tracker_api_key: str = ""
    # PROVIDER-BUDGET-001: SolanaTracker Advanced request accounting + budget
    # guardrails (cost/usage OBSERVABILITY only; plan ~$58-59/mo, 200k req/mo).
    # The guardrails can only SKIP optional SolanaTracker lookups when over
    # budget — tokens then fall back to GoPlus+heuristics (a supported mode).
    # They never add calls, never touch GoPlus/Birdeye, and attach no EV/
    # trade/sizing/order/wallet/signing/execution semantics.
    solana_tracker_monthly_request_limit: int = 200000  # official plan ceiling
    solana_tracker_daily_request_budget: int = 5000     # operational target/day
    solana_tracker_hourly_request_budget: int = 200     # operational target/hour
    solana_tracker_per_run_lookup_limit: int = 25       # max ST lookups per scan run
    solana_tracker_cache_ttl_hours: int = 24            # dedupe horizon (report/run-rate context)
    solana_tracker_warn_daily_requests: int = 4000      # log/report warning at/above
    solana_tracker_stop_daily_requests: int = 6000      # skip optional ST calls at/above
    enable_rugcheck_risk: bool = False  # reserved: no RugCheck adapter in CRYPTO-002
    crypto_risk_min_liquidity_usd: float = 5000.0
    crypto_risk_max_top_holder_pct: float = 20.0
    crypto_risk_max_sniper_pct: float = 20.0
    crypto_risk_max_insider_pct: float = 15.0
    crypto_risk_max_bundler_pct: float = 25.0
    crypto_risk_min_pair_age_seconds: int = 300
    crypto_risk_provider_timeout_seconds: float = 10.0
    crypto_risk_engine_version: str = "v1"

    # MEME-RISK-003: added holder/sniper/insider/bundler/creator coverage.
    # Birdeye is a new read-only holder-data provider (header-only key,
    # degrades gracefully without one; live payload mapping PENDING validation).
    # Helius stays reserved. creator/deployer concentration is a new heuristic
    # category (fires only when a provider supplies creator_pct). Risk
    # intelligence only — no EV/trade/sizing/orders/wallets/execution.
    enable_birdeye_risk: bool = False
    birdeye_api_key: str = ""
    crypto_risk_max_creator_pct: float = 15.0

    # MEME-NEWS-001: read-only meme/news scout + domain-expansion scout.
    # Reserved for future loop/timer use; manual meme-scan-once /
    # meme-scout-report / catalyst-report / domain-scout-report are always
    # allowed. Scouting/scoring only — no EV, trade, sizing, order, wallet,
    # swap, signing, or execution anywhere.
    enable_meme_scout: bool = False   # gates any future loop/timer; manual always allowed
    enable_domain_scout: bool = False  # gates any future loop/timer; manual always allowed
    meme_scout_limit: int = 30  # max tokens scored per scan pass
    meme_scout_version: str = "v1"
    domain_scout_version: str = "v1"

    # MEME-NEWS-002: scheduled, bounded, always-on read-only discovery lane.
    # ENABLE_MEME_NEWS_SCOUT gates the SCHEDULED runner (meme-news-run-once
    # --scheduled / the systemd timer) only; manual meme-news-run-once and all
    # reports are always allowed. Still read-only scouting — no EV, trade,
    # sizing, order, wallet, swap, signing, or execution.
    enable_meme_news_scout: bool = False
    meme_news_scout_interval_seconds: int = 300  # informational (systemd timer governs cadence)
    meme_news_max_profiles_per_run: int = 30
    meme_news_max_boosts_per_run: int = 30
    meme_news_retention_days: int = 14  # prunes meme_scout_runs/attention/catalysts (documented)
    meme_news_attention_alert_threshold: float = 0.6   # notable-event report only; no action
    meme_news_attention_jump_threshold: float = 0.15   # per-token attention delta to flag
    meme_news_severe_risk_alert: bool = True

    # POLY-001: read-only Polymarket market-DATA observer (second prediction
    # venue). Public/no-auth GETs only — Gamma market catalog + CLOB order
    # books. Market-data OBSERVATION only: no EV, arbitrage, trade
    # recommendation, position sizing, order placement/cancellation, wallet /
    # private key, signing, swap, or execution. ENABLE_POLYMARKET_SCOUT gates
    # any future loop/timer only; manual polymarket-scan-once and all reports
    # are always allowed (no timer is installed in POLY-001).
    enable_polymarket_scout: bool = False
    polymarket_market_limit: int = 50       # max markets fetched/persisted per scan
    polymarket_orderbook_limit: int = 20    # max token order books fetched per scan
    polymarket_timeout_seconds: float = 15.0
    polymarket_retention_days: int = 14     # prunes markets/orderbook/scout_runs (documented)
    polymarket_provider_version: str = "v1"

    # POLY-COVERAGE-001: bounded READ-ONLY coverage expansion of the same public
    # GETs (pagination + category/resolution-window filters + public search), so
    # the POLY-002 cross-venue matcher has comparable supply to observe. Widening
    # the observation sample only — still no EV, arbitrage label, trade
    # recommendation, position sizing, order, wallet/key, signing, or execution.
    # Defaults stay conservative; the adapter enforces hard ceilings regardless.
    polymarket_page_size: int = 100           # Gamma caps a /markets page at 100
    polymarket_max_pages: int = 5             # catalog pages per scan (ceiling 20)
    polymarket_search_limit_per_type: int = 20  # /public-search rows per page
    polymarket_search_max_pages: int = 3      # pages per search query (ceiling 5)
    polymarket_max_targeted_queries: int = 6  # Kalshi-derived queries per scan

    # Candidate hygiene / eligibility gating (MVP-003A)
    require_two_sided_quote: bool = True
    exclude_zero_quote_markets: bool = True
    min_liquidity: int = 100
    min_volume_24h: int = 25
    max_spread: float = 0.20  # dollars; 0.20 = 20 cents
    min_days_to_expiration: float = 0.25
    max_days_to_expiration: float = 45.0

    @property
    def observer_credential_configured(self) -> bool:
        """Both halves present. Neither alone is a credential.

        This reports configuration only. It authorises nothing: no code path
        connects, and the observer has no runner. Scope verification and file
        confinement happen in `app.realtime.auth` at load time.
        """
        return bool(self.kalshi_observer_api_key_id
                    and self.kalshi_observer_credential_path)

    @property
    def ws_ticker_list(self) -> list[str]:
        return [t.strip() for t in self.kalshi_ws_tickers.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
