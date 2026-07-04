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

    # Baseball evidence-aware forecasting canary (MVP-004F) — consumes
    # source-backed MLB packets; no external calls of its own
    enable_baseball_evidence_forecasting: bool = False
    baseball_forecaster_version: str = "v1"
    baseball_forecast_max_confidence: float = 0.70
    baseball_forecast_min_completeness: float = 0.75

    # Retention / pruning (OPS-003) — operational tables only; intelligence
    # and calibration tables are never pruned
    tick_retention_days: int = 7
    watcher_run_retention_days: int = 30
    pipeline_run_retention_days: int = 90
    signal_retention_days: int = 0  # 0 = keep signals indefinitely
    retention_batch_size: int = 5000
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
