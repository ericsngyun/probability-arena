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
