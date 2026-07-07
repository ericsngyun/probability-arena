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

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
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

    # Edge precheck (MVP-005A) — probability-gap MEASUREMENT only. Records
    # forecast_probability - market_midpoint with validity checks. No dollar
    # EV, no trade recommendations, no sizing, no orders, no execution;
    # paper_candidate_later is a review label with zero attached behavior.
    # Thresholds are PROVISIONAL (design doc §6) pending precheck data.
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

    # Candidate hygiene / eligibility gating (MVP-003A)
    require_two_sided_quote: bool = True
    exclude_zero_quote_markets: bool = True
    min_liquidity: int = 100
    min_volume_24h: int = 25
    max_spread: float = 0.20  # dollars; 0.20 = 20 cents
    min_days_to_expiration: float = 0.25
    max_days_to_expiration: float = 45.0

    @property
    def ws_enabled(self) -> bool:
        """WebSocket snapshots run only when credentials are fully configured."""
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)

    @property
    def ws_ticker_list(self) -> list[str]:
        return [t.strip() for t in self.kalshi_ws_tickers.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
