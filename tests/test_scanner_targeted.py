"""SCANNER-002/OPS-010 — targeted game-level market scan coverage.

Covers: targeted series fetching + dedupe, persistence of markets missed by
the generic first page, eligibility gating of targeted markets, the watcher
supported-universe supplement (game-level only; props never explode the
universe), partial-failure tolerance, flag-off behavior, and CLI/pipeline
count reporting. No live external calls — everything is mocked."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import cli
from app.adapters.kalshi import KalshiRestAdapter
from app.config import Settings
from app.db import Base
from app.models import Market, MarketEligibilityAssessment, MarketSnapshot
from app.services.eligibility import EligibilityThresholds
from app.services.scanner import fetch_targeted_markets, parse_targeted_series, run_scan
from app.services.watcher import (
    RealtimeWatcher,
    WatcherConfig,
    supported_game_level_market_type,
)
from tests.conftest import make_market

GAME_TICKER = "KXWCGAME-26JUL04PARFRA-FRA"
TOTAL_TICKER = "KXWCTOTAL-26JUL04PARFRA-3"
PROP_TICKER = "KXWCAST-26JUL04PARFRA-FRAKMBAPP10-1"
MLB_SPREAD_TICKER = "KXMLBSPREAD-26JUL041105PITWSH-PIT5"

THRESHOLDS = EligibilityThresholds()


def targeted_settings(**overrides) -> Settings:
    """Explicit Settings (get_settings is lru_cached and unreliable across a
    test session — the repo convention is to inject Settings instances)."""
    base = dict(
        _env_file=None,
        enable_targeted_market_scans=True,
        targeted_market_series="KXWCGAME,KXWCTOTAL",
        targeted_market_scan_limit_per_series=250,
        targeted_market_scan_active_only=True,
        targeted_market_scan_dedup=True,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


async def scan(session, adapter, **settings_overrides):
    return await run_scan(
        session,
        adapter=adapter,
        thresholds=THRESHOLDS,
        settings=targeted_settings(**settings_overrides),
    )


def game_market(ticker=GAME_TICKER, **overrides):
    return make_market(
        ticker=ticker,
        title="Paraguay vs France Winner?",
        **overrides,
    )


class TargetedFakeAdapter:
    """Generic page + per-series responses; optionally failing series."""

    def __init__(self, generic, by_series=None, failing=()):
        self.generic = generic
        self.by_series = by_series or {}
        self.failing = set(failing)
        self.series_requested: list[str] = []

    async def fetch_active_markets(self, max_markets=None):
        return self.generic[: max_markets or len(self.generic)]

    async def fetch_markets_by_series(self, series, max_markets=None, active_only=True):
        self.series_requested.append(series)
        if series in self.failing:
            raise RuntimeError(f"boom: {series}")
        return list(self.by_series.get(series, []))[: max_markets or 250]


class TestParseTargetedSeries:
    def test_parses_and_normalizes(self):
        assert parse_targeted_series(" kxwcgame , KXWCTOTAL ,, ") == ["KXWCGAME", "KXWCTOTAL"]

    def test_empty_string_yields_no_series(self):
        assert parse_targeted_series("") == []


class TestTargetedFetch:
    async def test_fetches_configured_series(self, session):
        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],
            by_series={"KXWCGAME": [game_market()], "KXWCTOTAL": []},
        )
        result = await scan(session, adapter)
        assert adapter.series_requested == ["KXWCGAME", "KXWCTOTAL"]
        assert result.targeted is not None
        assert result.targeted.by_series == {"KXWCGAME": 1, "KXWCTOTAL": 0}

    async def test_generic_and_targeted_dedupe(self, session):
        shared = game_market()
        adapter = TargetedFakeAdapter(
            generic=[shared, make_market(ticker="GENERIC-1")],
            by_series={"KXWCGAME": [shared], "KXWCTOTAL": []},
        )
        result = await scan(session, adapter)
        assert result.targeted.targeted_fetched == 1
        assert result.targeted.targeted_added == 0  # deduped against generic
        snapshots = session.execute(
            select(MarketSnapshot).join(Market).where(Market.ticker == GAME_TICKER)
        ).scalars().all()
        assert len(snapshots) == 1

    async def test_targeted_market_missed_by_generic_page_is_persisted(self, session):
        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],  # first page: props only
            by_series={"KXWCGAME": [game_market()], "KXWCTOTAL": []},
        )
        result = await scan(session, adapter)
        market = session.execute(
            select(Market).where(Market.ticker == GAME_TICKER)
        ).scalar_one()
        snapshot = session.execute(
            select(MarketSnapshot).where(MarketSnapshot.market_id == market.id)
        ).scalar_one()
        assert snapshot.scanner_run_id == result.run.id
        assert result.run.markets_fetched == 2
        assert result.targeted.targeted_added == 1

    async def test_targeted_market_still_passes_eligibility_gate(self, session):
        ineligible = game_market(yes_bid=None, yes_ask=None, volume_24h=0)
        adapter = TargetedFakeAdapter(
            generic=[],
            by_series={"KXWCGAME": [ineligible], "KXWCTOTAL": []},
        )
        result = await scan(session, adapter)
        # persisted, but hard-zeroed and not ranked — never forced into candidates
        assert GAME_TICKER not in [item.market.ticker for item in result.ranked]
        snapshot = session.execute(
            select(MarketSnapshot).join(Market).where(Market.ticker == GAME_TICKER)
        ).scalar_one()
        assert snapshot.score == 0.0
        assessment = session.execute(
            select(MarketEligibilityAssessment).where(
                MarketEligibilityAssessment.market_ticker == GAME_TICKER
            )
        ).scalar_one()
        assert assessment.is_eligible is False

    async def test_partial_series_failure_does_not_fail_scan(self, session):
        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],
            by_series={"KXWCTOTAL": [game_market(ticker=TOTAL_TICKER)]},
            failing={"KXWCGAME"},
        )
        result = await scan(session, adapter)
        assert result.run.status == "ok"
        assert result.targeted.failed_series == {"KXWCGAME": "RuntimeError"}
        assert session.execute(
            select(Market).where(Market.ticker == TOTAL_TICKER)
        ).scalar_one_or_none() is not None

    async def test_flag_off_preserves_old_behavior(self, session):
        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],
            by_series={"KXWCGAME": [game_market()]},
        )
        result = await scan(session, adapter, enable_targeted_market_scans=False)
        assert adapter.series_requested == []
        assert result.targeted is None
        assert result.run.markets_fetched == 1

    async def test_dedup_disabled_keeps_duplicates(self, session):
        shared = game_market()
        adapter = TargetedFakeAdapter(
            generic=[shared], by_series={"KXWCGAME": [shared], "KXWCTOTAL": []}
        )
        added, stats = await fetch_targeted_markets(
            adapter, [shared], settings=targeted_settings(targeted_market_scan_dedup=False)
        )
        assert stats.targeted_added == 1  # diagnostic mode: no dedupe filtering


class TestSupportedGameLevelClassification:
    def test_game_level_types_supported(self):
        assert supported_game_level_market_type(GAME_TICKER) == "winner"
        assert supported_game_level_market_type(TOTAL_TICKER) == "total"
        assert supported_game_level_market_type(MLB_SPREAD_TICKER) == "spread"

    def test_props_and_other_domains_excluded(self):
        assert supported_game_level_market_type(PROP_TICKER) is None
        assert supported_game_level_market_type("KXMLBHRR-26JUL04TORSEA-SEACYOUNG2-3") is None
        assert supported_game_level_market_type("KXATPSETWINNER-26JUL04TIABUB-4-TIA") is None


class TestWatcherSupportedUniverse:
    async def test_eligible_game_level_market_enters_universe(self, session):
        adapter = TargetedFakeAdapter(
            generic=[], by_series={"KXWCGAME": [game_market()], "KXWCTOTAL": []}
        )
        await scan(session, adapter)
        watcher = RealtimeWatcher(adapter=adapter, config=WatcherConfig())
        universe = watcher._universe_tickers(session, limit=100)
        assert GAME_TICKER in universe

    async def test_score_zero_game_level_enters_via_supplement(self, session):
        # Two-sided book but zero pre-match volume: ineligible (score 0), yet
        # game-level + unexpired => supplemented so the watcher can tick it.
        quiet_game = game_market(volume_24h=0)
        quiet_prop = game_market(ticker=PROP_TICKER, volume_24h=0)
        adapter = TargetedFakeAdapter(
            generic=[quiet_prop], by_series={"KXWCGAME": [quiet_game], "KXWCTOTAL": []}
        )
        result = await scan(session, adapter)
        assert result.ranked == []  # nothing eligible
        watcher = RealtimeWatcher(adapter=adapter, config=WatcherConfig())
        universe = watcher._universe_tickers(session, limit=100)
        assert GAME_TICKER in universe
        assert PROP_TICKER not in universe  # props never enter via the supplement

    async def test_one_sided_and_expired_game_markets_excluded(self, session):
        one_sided = game_market(yes_bid=None, volume_24h=0)
        expired = game_market(
            ticker="KXWCGAME-26JUL01OLDGAME-OLD",
            volume_24h=0,
            close_time=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        adapter = TargetedFakeAdapter(
            generic=[], by_series={"KXWCGAME": [one_sided, expired], "KXWCTOTAL": []}
        )
        await scan(session, adapter)
        watcher = RealtimeWatcher(adapter=adapter, config=WatcherConfig())
        universe = watcher._universe_tickers(session, limit=100)
        assert universe == []

    async def test_supplement_is_bounded(self, session):
        # More score-0 game-level markets than the cap: universe must not explode.
        games = [
            game_market(
                ticker=f"KXWCGAME-26JUL01AAA{chr(ord('B') + i) * 3}-AAA", volume_24h=0
            )
            for i in range(10)
        ]
        adapter = TargetedFakeAdapter(
            generic=[], by_series={"KXWCGAME": games, "KXWCTOTAL": []}
        )
        await scan(session, adapter)
        watcher = RealtimeWatcher(
            adapter=adapter, config=WatcherConfig(supported_universe_limit=3)
        )
        universe = watcher._universe_tickers(session, limit=100)
        assert len(universe) == 3

    async def test_supplement_disabled_at_zero_limit(self, session):
        adapter = TargetedFakeAdapter(
            generic=[], by_series={"KXWCGAME": [game_market(volume_24h=0)], "KXWCTOTAL": []}
        )
        await scan(session, adapter)
        watcher = RealtimeWatcher(
            adapter=adapter, config=WatcherConfig(supported_universe_limit=0)
        )
        assert watcher._universe_tickers(session, limit=100) == []


class TestCliAndPipelineReporting:
    @pytest.fixture
    def scanner_settings(self, monkeypatch):
        """cli.scan/pipeline call run_scan without a settings override, so pin
        the module-level get_settings for determinism."""
        monkeypatch.setattr(
            "app.services.scanner.get_settings", lambda: targeted_settings()
        )

    async def test_cli_scan_prints_targeted_counts(self, session, capsys, scanner_settings):
        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],
            by_series={"KXWCGAME": [game_market()], "KXWCTOTAL": []},
            failing=set(),
        )
        await cli.scan(limit=10, adapter=adapter, session=session)
        out = capsys.readouterr().out
        assert "targeted scan (SCANNER-002)" in out
        assert "added_after_dedupe=1" in out
        assert "KXWCGAME=1" in out

    async def test_cli_scan_reports_failed_series(self, session, capsys, scanner_settings):
        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],
            by_series={"KXWCTOTAL": []},
            failing={"KXWCGAME"},
        )
        await cli.scan(limit=10, adapter=adapter, session=session)
        out = capsys.readouterr().out
        assert "failed series (scan continued): KXWCGAME(RuntimeError)" in out

    async def test_pipeline_scan_stage_summary_includes_targeted_counts(
        self, session, scanner_settings
    ):
        from app.services.pipeline import BaselineConfig, PipelineRunner
        from tests.test_enrichment import FakeDetailAdapter
        from tests.test_outcomes import FakeOutcomeAdapter

        adapter = TargetedFakeAdapter(
            generic=[make_market(ticker="GENERIC-1")],
            by_series={"KXWCGAME": [game_market()], "KXWCTOTAL": []},
        )
        runner = PipelineRunner(
            scan_adapter=adapter,
            enrichment_adapter=FakeDetailAdapter(),
            outcome_adapter=FakeOutcomeAdapter({}),
        )
        result = await runner._stage_scan(
            session, BaselineConfig(scan_limit=10, candidate_limit=5)
        )
        assert result.summary["generic_fetched"] == 1
        assert result.summary["targeted_added"] == 1
        assert result.summary["targeted_by_series"] == {"KXWCGAME": 1, "KXWCTOTAL": 0}


class TestAdapterSeriesFetch:
    BASE = "https://kalshi.test/trade-api/v2"

    @respx.mock
    async def test_pages_with_cursor_and_series_params(self, sample_kalshi_market):
        page_two = dict(sample_kalshi_market, ticker="KXWCGAME-26JUL04PARFRA-PAR")
        route = respx.get(f"{self.BASE}/markets")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "markets": [dict(sample_kalshi_market, ticker=GAME_TICKER)],
                    "cursor": "page2",
                },
            ),
            httpx.Response(200, json={"markets": [page_two], "cursor": ""}),
        ]
        adapter = KalshiRestAdapter(base_url=self.BASE)
        markets = await adapter.fetch_markets_by_series("KXWCGAME", max_markets=10)
        assert [m.ticker for m in markets] == [GAME_TICKER, "KXWCGAME-26JUL04PARFRA-PAR"]
        params = route.calls[0].request.url.params
        assert params["series_ticker"] == "KXWCGAME"
        assert params["status"] == "open"
        assert params["mve_filter"] == "exclude"
        assert route.calls[1].request.url.params["cursor"] == "page2"

    @respx.mock
    async def test_active_only_false_omits_status(self, sample_kalshi_market):
        route = respx.get(f"{self.BASE}/markets").mock(
            return_value=httpx.Response(
                200, json={"markets": [sample_kalshi_market], "cursor": ""}
            )
        )
        adapter = KalshiRestAdapter(base_url=self.BASE)
        await adapter.fetch_markets_by_series("KXWCGAME", max_markets=5, active_only=False)
        assert "status" not in route.calls[0].request.url.params

    @respx.mock
    async def test_retries_429_then_succeeds(self, sample_kalshi_market, monkeypatch):
        async def instant_sleep(_):
            return None

        monkeypatch.setattr("app.adapters.kalshi.asyncio.sleep", instant_sleep)
        route = respx.get(f"{self.BASE}/markets")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"markets": [sample_kalshi_market], "cursor": ""}),
        ]
        adapter = KalshiRestAdapter(base_url=self.BASE)
        markets = await adapter.fetch_markets_by_series("KXWCGAME", max_markets=5)
        assert len(markets) == 1
        assert route.call_count == 2

    @respx.mock
    async def test_persistent_429_raises_after_bounded_retries(self, monkeypatch):
        async def instant_sleep(_):
            return None

        monkeypatch.setattr("app.adapters.kalshi.asyncio.sleep", instant_sleep)
        route = respx.get(f"{self.BASE}/markets").mock(
            return_value=httpx.Response(429)
        )
        adapter = KalshiRestAdapter(base_url=self.BASE)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.fetch_markets_by_series("KXWCGAME", max_markets=5)
        assert route.call_count == 4  # initial + RATE_LIMIT_RETRIES

    @respx.mock
    async def test_schema_drift_skips_malformed_markets(self, sample_kalshi_market):
        respx.get(f"{self.BASE}/markets").mock(
            return_value=httpx.Response(
                200,
                json={
                    "markets": [sample_kalshi_market, {"no_ticker": True}],
                    "cursor": "",
                },
            )
        )
        adapter = KalshiRestAdapter(base_url=self.BASE)
        markets = await adapter.fetch_markets_by_series("KXWCGAME", max_markets=5)
        assert len(markets) == 1  # malformed market skipped, fetch continues
