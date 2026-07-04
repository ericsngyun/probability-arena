"""Crypto Arena (CRYPTO-001) tests: adapter parsing/error handling, upserts,
events, ticks, risk assessments, all signal detectors, cooldown dedupe, CLI,
and API. Everything is mocked — no live network calls."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cli
from app.adapters.dexscreener import (
    DEXSCREENER_API_BASE,
    DexScreenerAdapter,
    PairData,
    _parse_pair,
)
from app.db import Base, get_db
from app.main import app
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenDiscoveryEvent,
    CryptoTokenRiskAssessment,
    CryptoWatcherRun,
)
from app.services.crypto_risk import (
    MockCryptoRiskProvider,
    RiskAssessment,
    get_risk_provider,
)
from app.services.crypto_scout import (
    CryptoDiscoveryService,
    CryptoReportService,
    CryptoScoutConfig,
    CryptoSignalService,
)

NOW = datetime.now(timezone.utc)
TOKEN_A = "So1anaTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_B = "So1anaTokenBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
PAIR_A = "PairAddressAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PAIR_B = "PairAddressBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

PROFILE_PAYLOAD = [
    {"chainId": "solana", "tokenAddress": TOKEN_A, "url": "https://dexscreener.com/solana/a",
     "description": "A memecoin"},
    {"chainId": "ethereum", "tokenAddress": "0xNotSolana", "description": "filtered out"},
]

BOOST_PAYLOAD = [
    {"chainId": "solana", "tokenAddress": TOKEN_B, "totalAmount": 500,
     "url": "https://dexscreener.com/solana/b"},
]


def pair_payload(
    pair_address=PAIR_A,
    token=TOKEN_A,
    chain="solana",
    liquidity=25_000,
    volume_5m=4_000,
    change_5m=3.2,
    created_at=NOW - timedelta(hours=2),
    boosts_active=None,
):
    payload = {
        "chainId": chain,
        "pairAddress": pair_address,
        "dexId": "raydium",
        "url": f"https://dexscreener.com/solana/{pair_address}",
        "baseToken": {"address": token, "symbol": "MEME", "name": "Meme Coin"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112",
                       "symbol": "SOL"},
        "priceUsd": "0.00123",
        "liquidity": {"usd": liquidity},
        "volume": {"m5": volume_5m, "h1": 12_000, "h24": 90_000},
        "priceChange": {"m5": change_5m, "h1": 8.5},
        "marketCap": 1_200_000,
        "fdv": 1_500_000,
        "pairCreatedAt": int(created_at.timestamp() * 1000),
    }
    if boosts_active is not None:
        payload["boosts"] = {"active": boosts_active}
    return payload


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def config(**overrides) -> CryptoScoutConfig:
    defaults = dict(
        chain="solana",
        pair_limit=100,
        min_liquidity_usd=5000.0,
        min_volume_5m_usd=1000.0,
        signal_cooldown_seconds=900,
    )
    defaults.update(overrides)
    return CryptoScoutConfig(**defaults)


class FakeDexAdapter:
    """Canned DEX Screener responses; no network."""

    source_name = "dexscreener"

    def __init__(self, profiles=None, boosts=None, pairs_by_token=None):
        from app.adapters.dexscreener import _parse_profiles

        self.profiles = _parse_profiles(
            profiles if profiles is not None else PROFILE_PAYLOAD, "solana"
        )
        self.boosts = _parse_profiles(
            boosts if boosts is not None else BOOST_PAYLOAD, "solana"
        )
        raw_pairs = pairs_by_token if pairs_by_token is not None else {
            TOKEN_A: [pair_payload()],
            TOKEN_B: [pair_payload(pair_address=PAIR_B, token=TOKEN_B, boosts_active=2)],
        }
        self.pairs_by_token = {
            token: [_parse_pair(p) for p in payloads]
            for token, payloads in raw_pairs.items()
        }

    async def fetch_latest_token_profiles(self):
        return self.profiles

    async def fetch_latest_boosted_tokens(self):
        return self.boosts

    async def fetch_pairs_for_token(self, token_address):
        return self.pairs_by_token.get(token_address, [])


# --- DexScreener adapter ---


class TestDexScreenerAdapter:
    @respx.mock
    async def test_parses_token_profiles_solana_only(self):
        respx.get(f"{DEXSCREENER_API_BASE}/token-profiles/latest/v1").mock(
            return_value=httpx.Response(200, json=PROFILE_PAYLOAD)
        )
        profiles = await DexScreenerAdapter().fetch_latest_token_profiles()
        assert len(profiles) == 1
        assert profiles[0].token_address == TOKEN_A
        assert profiles[0].description == "A memecoin"

    @respx.mock
    async def test_parses_boosted_tokens(self):
        respx.get(f"{DEXSCREENER_API_BASE}/token-boosts/latest/v1").mock(
            return_value=httpx.Response(200, json=BOOST_PAYLOAD)
        )
        boosts = await DexScreenerAdapter().fetch_latest_boosted_tokens()
        assert len(boosts) == 1
        assert boosts[0].boost_amount == 500

    @respx.mock
    async def test_parses_token_pairs_with_full_metrics(self):
        respx.get(f"{DEXSCREENER_API_BASE}/token-pairs/v1/solana/{TOKEN_A}").mock(
            return_value=httpx.Response(200, json=[pair_payload(boosts_active=1)])
        )
        pairs = await DexScreenerAdapter().fetch_pairs_for_token(TOKEN_A)
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.pair_address == PAIR_A
        assert pair.base_token_symbol == "MEME"
        assert pair.price_usd == pytest.approx(0.00123)
        assert pair.liquidity_usd == 25_000
        assert pair.volume_5m_usd == 4_000
        assert pair.volume_24h_usd == 90_000
        assert pair.price_change_5m == pytest.approx(3.2)
        assert pair.market_cap == 1_200_000
        assert pair.fdv == 1_500_000
        assert pair.pair_created_at is not None
        assert pair.boosts_active == 1

    @respx.mock
    async def test_search_filters_other_chains(self):
        respx.get(url__startswith=f"{DEXSCREENER_API_BASE}/latest/dex/search").mock(
            return_value=httpx.Response(
                200,
                json={"pairs": [pair_payload(), pair_payload(chain="base")]},
            )
        )
        pairs = await DexScreenerAdapter().search_pairs("MEME")
        assert len(pairs) == 1
        assert pairs[0].chain == "solana"

    @respx.mock
    async def test_fetch_pair_single_lookup(self):
        respx.get(f"{DEXSCREENER_API_BASE}/latest/dex/pairs/solana/{PAIR_A}").mock(
            return_value=httpx.Response(200, json={"pairs": [pair_payload()]})
        )
        pair = await DexScreenerAdapter().fetch_pair(PAIR_A)
        assert pair is not None and pair.pair_address == PAIR_A

    @respx.mock
    async def test_rate_limit_and_http_errors_return_empty(self):
        respx.get(f"{DEXSCREENER_API_BASE}/token-profiles/latest/v1").mock(
            return_value=httpx.Response(429)
        )
        respx.get(f"{DEXSCREENER_API_BASE}/token-boosts/latest/v1").mock(
            return_value=httpx.Response(500)
        )
        respx.get(f"{DEXSCREENER_API_BASE}/token-pairs/v1/solana/{TOKEN_A}").mock(
            side_effect=httpx.ConnectError("boom")
        )
        adapter = DexScreenerAdapter()
        assert await adapter.fetch_latest_token_profiles() == []
        assert await adapter.fetch_latest_boosted_tokens() == []
        assert await adapter.fetch_pairs_for_token(TOKEN_A) == []

    @respx.mock
    async def test_schema_drift_is_skipped_not_fatal(self):
        drifted = [
            {"chainId": "solana"},  # no pairAddress
            {"unexpected": "shape"},
            pair_payload(),
            "not-a-dict",
        ]
        respx.get(f"{DEXSCREENER_API_BASE}/token-pairs/v1/solana/{TOKEN_A}").mock(
            return_value=httpx.Response(200, json=drifted)
        )
        pairs = await DexScreenerAdapter().fetch_pairs_for_token(TOKEN_A)
        assert len(pairs) == 1

    @respx.mock
    async def test_non_list_payload_returns_empty(self):
        respx.get(f"{DEXSCREENER_API_BASE}/token-profiles/latest/v1").mock(
            return_value=httpx.Response(200, json={"error": "unexpected object"})
        )
        assert await DexScreenerAdapter().fetch_latest_token_profiles() == []


# --- Risk provider selection ---


class TestRiskProviderSelection:
    def test_flag_off_returns_none(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_crypto_risk_provider", False)
        assert get_risk_provider() is None

    def test_mock_provider_selected_when_enabled(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_crypto_risk_provider", True)
        monkeypatch.setattr(get_settings(), "crypto_risk_provider", "mock")
        assert isinstance(get_risk_provider(), MockCryptoRiskProvider)

    def test_unknown_provider_returns_none(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "enable_crypto_risk_provider", True)
        monkeypatch.setattr(get_settings(), "crypto_risk_provider", "bogus")
        assert get_risk_provider() is None


# --- Discovery persistence ---


def discovery(adapter=None, risk_provider=None, cfg=None) -> CryptoDiscoveryService:
    cfg = cfg or config()
    return CryptoDiscoveryService(
        adapter=adapter or FakeDexAdapter(),
        risk_provider=risk_provider or MockCryptoRiskProvider(),
        signal_service=CryptoSignalService(cfg),
        config=cfg,
    )


class TestDiscovery:
    async def test_scan_once_persists_everything(self, session):
        run = await discovery().scan_once(session)

        assert run.status == "ok"
        assert run.tokens_checked == 2
        assert run.pairs_checked == 2
        assert run.ticks_recorded == 2

        tokens = session.execute(select(CryptoToken)).scalars().all()
        assert {t.token_address for t in tokens} == {TOKEN_A, TOKEN_B}
        token_a = next(t for t in tokens if t.token_address == TOKEN_A)
        assert token_a.symbol == "MEME"
        assert token_a.token_metadata["description"] == "A memecoin"
        assert token_a.token_metadata["boosted"] is False

        pairs = session.execute(select(CryptoPair)).scalars().all()
        assert {p.pair_address for p in pairs} == {PAIR_A, PAIR_B}
        assert all(p.dex_id == "raydium" and p.pair_created_at is not None for p in pairs)

        events = session.execute(select(CryptoTokenDiscoveryEvent)).scalars().all()
        types = {(e.token_address, e.event_type) for e in events}
        assert (TOKEN_A, "profile") in types
        assert (TOKEN_B, "boost") in types
        assert (TOKEN_A, "pair_seen") in types

        ticks = session.execute(select(CryptoPriceTick)).scalars().all()
        assert len(ticks) == 2
        assert all(t.price_usd and t.liquidity_usd and t.volume_5m_usd for t in ticks)

        assessments = session.execute(select(CryptoTokenRiskAssessment)).scalars().all()
        assert {a.token_address for a in assessments} == {TOKEN_A, TOKEN_B}
        assert all(a.provider == "mock" for a in assessments)

    async def test_token_and_pair_upserts_are_idempotent(self, session):
        service = discovery()
        await service.scan_once(session)
        await service.scan_once(session)

        tokens = session.execute(select(CryptoToken)).scalars().all()
        pairs = session.execute(select(CryptoPair)).scalars().all()
        assert len(tokens) == 2  # upserted, not duplicated
        assert len(pairs) == 2
        assert all(t.last_seen_at >= t.first_seen_at for t in tokens)
        # ticks append every pass
        assert len(session.execute(select(CryptoPriceTick)).scalars().all()) == 4

    async def test_no_risk_provider_records_no_assessments(self, session):
        service = CryptoDiscoveryService(
            adapter=FakeDexAdapter(),
            risk_provider=MockCryptoRiskProvider(),
            config=config(),
        )
        service.risk_provider = None  # simulate flag off
        await service.scan_once(session)
        assert session.execute(select(CryptoTokenRiskAssessment)).scalars().all() == []

    async def test_pair_limit_bounds_work(self, session):
        run = await discovery(cfg=config(pair_limit=1)).scan_once(session)
        assert run.pairs_checked == 1
        assert run.ticks_recorded == 1

    async def test_provider_failure_recorded_on_run(self, session):
        class ExplodingAdapter(FakeDexAdapter):
            async def fetch_latest_token_profiles(self):
                raise RuntimeError("provider exploded")

        with pytest.raises(RuntimeError):
            await discovery(adapter=ExplodingAdapter()).scan_once(session)
        run = session.execute(select(CryptoWatcherRun)).scalars().one()
        assert run.status == "error"
        assert run.error_type == "RuntimeError"
        assert "exploded" in run.error_message

    async def test_empty_provider_responses_yield_clean_run(self, session):
        adapter = FakeDexAdapter(profiles=[], boosts=[], pairs_by_token={})
        run = await discovery(adapter=adapter).scan_once(session)
        assert run.status == "ok"
        assert run.tokens_checked == 0
        assert run.signals_created == 0


# --- Signal detectors ---


def make_pair_row(created_hours_ago=2.0) -> CryptoPair:
    return CryptoPair(
        chain="solana",
        pair_address=PAIR_A,
        base_token_address=TOKEN_A,
        pair_created_at=NOW - timedelta(hours=created_hours_ago),
        first_seen_at=NOW,
        created_at=NOW,
    )


def make_tick(
    liquidity=25_000.0,
    volume_5m=4_000.0,
    change_5m=3.0,
    boosts_active=None,
    observed_at=NOW,
) -> CryptoPriceTick:
    return CryptoPriceTick(
        chain="solana",
        token_address=TOKEN_A,
        pair_address=PAIR_A,
        observed_at=observed_at,
        price_usd=0.001,
        liquidity_usd=liquidity,
        volume_5m_usd=volume_5m,
        price_change_5m=change_5m,
        raw_payload={"boosts_active": boosts_active},
        created_at=observed_at,
    )


def detect(pair=None, previous=None, tick=None, risk=None) -> list[str]:
    signals = CryptoSignalService(config()).detect(
        pair or make_pair_row(), previous, tick or make_tick(), risk, NOW
    )
    return [s.signal_type for s in signals]


class TestSignalDetectors:
    def test_new_pair_fires_for_young_pair_without_history(self):
        assert "new_pair" in detect(pair=make_pair_row(created_hours_ago=2))

    def test_new_pair_silent_for_old_pair_or_known_history(self):
        assert "new_pair" not in detect(pair=make_pair_row(created_hours_ago=48))
        assert "new_pair" not in detect(
            pair=make_pair_row(created_hours_ago=2), previous=make_tick()
        )

    def test_liquidity_appeared_on_threshold_cross(self):
        types = detect(previous=make_tick(liquidity=1_000), tick=make_tick(liquidity=8_000))
        assert "liquidity_appeared" in types
        # already above: no signal
        types = detect(previous=make_tick(liquidity=9_000), tick=make_tick(liquidity=10_000))
        assert "liquidity_appeared" not in types

    def test_liquidity_removed_on_halving(self):
        types = detect(previous=make_tick(liquidity=20_000), tick=make_tick(liquidity=4_000))
        assert "liquidity_removed" in types
        types = detect(previous=make_tick(liquidity=20_000), tick=make_tick(liquidity=15_000))
        assert "liquidity_removed" not in types

    def test_volume_spike_needs_multiplier_and_floor(self):
        types = detect(previous=make_tick(volume_5m=1_000), tick=make_tick(volume_5m=5_000))
        assert "volume_spike" in types
        # below absolute floor
        cfg = config(min_volume_5m_usd=10_000)
        signals = CryptoSignalService(cfg).detect(
            make_pair_row(), make_tick(volume_5m=1_000), make_tick(volume_5m=5_000), None, NOW
        )
        assert "volume_spike" not in [s.signal_type for s in signals]
        # not a 3x jump
        types = detect(previous=make_tick(volume_5m=4_000), tick=make_tick(volume_5m=6_000))
        assert "volume_spike" not in types

    def test_price_momentum_threshold(self):
        assert "price_momentum" in detect(tick=make_tick(change_5m=22.0))
        assert "price_momentum" not in detect(tick=make_tick(change_5m=9.0))

    def test_boost_detected_on_transition_only(self):
        assert "boost_detected" in detect(tick=make_tick(boosts_active=2))
        types = detect(
            previous=make_tick(boosts_active=1), tick=make_tick(boosts_active=2)
        )
        assert "boost_detected" not in types

    def test_risk_signals_fire_from_provider_flags(self):
        risk = RiskAssessment(
            provider="mock",
            token_address=TOKEN_A,
            risk_score=0.9,
            risk_level="critical",
            flags={
                "honeypot": True,
                "top10_holder_pct": 62,
                "mint_authority_enabled": True,
            },
        )
        types = detect(risk=risk)
        assert "rug_risk" in types
        assert "holder_risk" in types
        assert "suspicious_supply_control" in types

    def test_risk_signals_inactive_without_provider(self):
        assert not {"rug_risk", "holder_risk", "suspicious_supply_control"} & set(detect())

    def test_clean_risk_read_fires_nothing(self):
        risk = RiskAssessment(
            provider="mock", token_address=TOKEN_A, risk_score=0.1, risk_level="low", flags={}
        )
        assert not {"rug_risk", "holder_risk", "suspicious_supply_control"} & set(
            detect(risk=risk)
        )

    def test_signals_carry_reason_and_evidence(self):
        signals = CryptoSignalService(config()).detect(
            make_pair_row(), make_tick(liquidity=1_000), make_tick(liquidity=9_000), None, NOW
        )
        signal = next(s for s in signals if s.signal_type == "liquidity_appeared")
        assert "Liquidity rose" in signal.reason
        assert signal.evidence["new_liquidity_usd"] == 9_000
        assert signal.signal_status == "new"


class TestCooldown:
    def test_repeat_signal_suppressed_within_cooldown(self, session):
        service = CryptoSignalService(config())
        first = service.detect(
            make_pair_row(), make_tick(liquidity=1_000), make_tick(liquidity=9_000), None, NOW
        )
        assert service.persist_deduped(session, first, NOW) == 1
        repeat = service.detect(
            make_pair_row(), make_tick(liquidity=2_000), make_tick(liquidity=9_500), None, NOW
        )
        assert service.persist_deduped(session, repeat, NOW) == 0

    def test_signal_allowed_after_cooldown_expires(self, session):
        service = CryptoSignalService(config(signal_cooldown_seconds=900))
        first = service.detect(
            make_pair_row(), make_tick(liquidity=1_000), make_tick(liquidity=9_000), None, NOW
        )
        service.persist_deduped(session, first, NOW)
        later = NOW + timedelta(seconds=901)
        repeat = service.detect(
            make_pair_row(), make_tick(liquidity=2_000), make_tick(liquidity=9_500), None, later
        )
        assert service.persist_deduped(session, repeat, later) == 1


# --- End-to-end signals through discovery ---


class TestDiscoverySignals:
    async def test_first_scan_emits_new_pair_and_boost(self, session):
        run = await discovery().scan_once(session)
        types = {
            s.signal_type
            for s in session.execute(select(CryptoOpportunitySignal)).scalars().all()
        }
        assert "new_pair" in types  # fresh pairs, 2h old
        assert "boost_detected" in types  # TOKEN_B pair carries active boosts
        assert run.signals_created >= 2

    async def test_risk_signals_flow_through_with_flagged_provider(self, session):
        risky = MockCryptoRiskProvider(
            {
                TOKEN_A: RiskAssessment(
                    provider="mock",
                    token_address=TOKEN_A,
                    risk_score=0.95,
                    risk_level="critical",
                    flags={"honeypot": True, "top10_holder_pct": 80,
                           "freeze_authority_enabled": True},
                )
            }
        )
        await discovery(risk_provider=risky).scan_once(session)
        types = {
            s.signal_type
            for s in session.execute(
                select(CryptoOpportunitySignal).where(
                    CryptoOpportunitySignal.token_address == TOKEN_A
                )
            ).scalars().all()
        }
        assert {"rug_risk", "holder_risk", "suspicious_supply_control"} <= types


# --- CLI ---


class TestCli:
    async def test_crypto_scan_once_cli(self, session, capsys):
        exit_code = await cli.crypto_scan_once(services=discovery(), session=session)
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "crypto scan #1: ok" in output
        assert "tokens=2 pairs=2 ticks=2" in output

    async def test_crypto_signals_recent_cli(self, session, capsys):
        await discovery().scan_once(session)
        count = await cli.crypto_signals_recent(limit=10, session=session)
        assert count >= 2
        output = capsys.readouterr().out
        assert "crypto signal(s)" in output
        assert "new_pair" in output

    async def test_crypto_report_cli(self, session, capsys):
        await discovery().scan_once(session)
        total = await cli.crypto_report(session=session)
        assert total == 2
        output = capsys.readouterr().out
        assert "crypto report: " in output
        assert "tokens=2" in output
        assert "signals by type:" in output
        assert "risk by level: low=2" in output
        assert "latest run: #1 ok" in output

    def test_main_wires_crypto_commands(self, monkeypatch):
        captured = []

        async def fake(*args, **kwargs):
            captured.append(kwargs)
            return 0

        monkeypatch.setattr(cli, "crypto_scan_once", fake)
        monkeypatch.setattr(cli, "crypto_signals_recent", fake)
        monkeypatch.setattr(cli, "crypto_report", fake)
        assert cli.main(["crypto-scan-once", "--limit", "5"]) == 0
        assert cli.main(["crypto-signals-recent"]) == 0
        assert cli.main(["crypto-report"]) == 0
        assert len(captured) == 3


# --- API ---


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), session
    app.dependency_overrides.clear()


class TestApi:
    async def test_endpoints_serve_scanned_data_without_raw_payloads(self, client):
        test_client, session = client
        await discovery().scan_once(session)

        tokens = test_client.get("/crypto/tokens").json()
        assert {t["token_address"] for t in tokens} == {TOKEN_A, TOKEN_B}
        assert all("raw_payload" not in t for t in tokens)

        pairs = test_client.get("/crypto/pairs").json()
        assert {p["pair_address"] for p in pairs} == {PAIR_A, PAIR_B}
        assert all("raw_payload" not in p for p in pairs)

        signals = test_client.get("/crypto/signals").json()
        assert len(signals) >= 2
        assert all("raw_payload" not in s for s in signals)
        assert all(s["reason"] for s in signals)
        # rows do persist raw payloads for audit — they just never serialize
        row = session.execute(select(CryptoOpportunitySignal)).scalars().first()
        assert row.raw_payload is not None

        filtered = test_client.get("/crypto/signals?signal_type=new_pair").json()
        assert filtered and all(s["signal_type"] == "new_pair" for s in filtered)

        report = test_client.get("/crypto/report").json()
        assert report["totals"]["tokens"] == 2
        assert "new_pair" in report["signals_by_type"]
        assert report["latest_run"]["status"] == "ok"
        assert report["provider_errors"] == []

    def test_endpoints_empty_before_any_scan(self, client):
        test_client, _ = client
        assert test_client.get("/crypto/tokens").json() == []
        assert test_client.get("/crypto/pairs").json() == []
        assert test_client.get("/crypto/signals").json() == []
        report = test_client.get("/crypto/report").json()
        assert report["totals"]["tokens"] == 0
        assert report["latest_run"] is None


# --- Report service ---


class TestReportService:
    async def test_report_counts_and_errors(self, session):
        await discovery().scan_once(session)

        class ExplodingAdapter(FakeDexAdapter):
            async def fetch_latest_token_profiles(self):
                raise RuntimeError("rate limited hard")

        with pytest.raises(RuntimeError):
            await discovery(adapter=ExplodingAdapter()).scan_once(session)

        report = CryptoReportService().build(session)
        assert report.totals["tokens"] == 2
        assert report.totals["risk_assessments"] == 2
        assert report.signals_by_status.get("new", 0) >= 2
        assert report.risk_by_level == {"low": 2}
        assert len(report.provider_errors) == 1
        assert report.provider_errors[0].error_type == "RuntimeError"
        assert report.latest_run.status == "error"
