"""OPS-012 tests: tick aggregation / DB pressure control.

Bucket creation, OHLC midpoint math, spread/liquidity aggregation, idempotent
rerun, missing-midpoint honesty, bounded batches with reported truncation,
dry-run writes nothing, the coverage report + STAGED (not enacted) retention
recommendation, raw ticks never deleted by aggregation, raw tick retention
window unchanged, migration 0023 up/down, and no forbidden vocabulary/fields.
Storage/durability plumbing only — buckets are telemetry summaries, never
trading signals. No live network; in-memory SQLite.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.db import Base
from app.models import MarketPriceTick, MarketPriceTickBucket
from app.services.retention import PROTECTED_TABLES, RetentionConfig, RetentionService
from app.services.tick_aggregation import (
    AGGREGATION_NOTE,
    AggregationStats,
    TickAggregationReport,
    TickAggregationReportService,
    TickAggregationService,
    bucket_start_for,
)

REPO = Path(__file__).resolve().parents[1]
# fixed, bucket-aligned base so OHLC ordering assertions are deterministic
BASE = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick(session, ticker="KXMLBGAME-1", *, at=None, bid=40, ask=44, mid=None,
         spread=None, liq=1000, vol=500):
    at = at or BASE
    if mid is None and bid is not None and ask is not None:
        mid = round((bid + ask) / 200, 4)
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid
    row = MarketPriceTick(
        market_ticker=ticker, observed_at=at, yes_bid=bid, yes_ask=ask,
        midpoint=mid, spread=spread, volume_24h=vol, liquidity_proxy=liq,
        created_at=at,
    )
    session.add(row)
    return row


def svc(**kw) -> TickAggregationService:
    return TickAggregationService(settings=Settings(_env_file=None, **kw))


def aggregate(session, **kw) -> AggregationStats:
    session.commit()
    return svc().aggregate(session, **kw)


def buckets(session) -> list[MarketPriceTickBucket]:
    return session.query(MarketPriceTickBucket).order_by(
        MarketPriceTickBucket.market_ticker, MarketPriceTickBucket.bucket_start
    ).all()


# --- bucket math ---------------------------------------------------------------


class TestBucketMath:
    def test_bucket_start_is_epoch_aligned_floor(self):
        at = datetime(2026, 7, 9, 12, 7, 33, tzinfo=timezone.utc)
        assert bucket_start_for(at, 300) == datetime(2026, 7, 9, 12, 5, tzinfo=timezone.utc)
        assert bucket_start_for(at, 900) == datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        assert bucket_start_for(at, 3600) == datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)

    def test_ohlc_midpoint_math(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        for i, mid in enumerate([0.40, 0.55, 0.35, 0.48]):  # o, h, l, c
            tick(session, at=start + timedelta(seconds=30 * (i + 1)),
                 bid=int(mid * 100) - 2, ask=int(mid * 100) + 2, mid=mid)
        stats = aggregate(session, hours=1)

        assert stats.buckets_written == 1
        b = buckets(session)[0]
        assert b.open_mid == pytest.approx(0.40)
        assert b.high_mid == pytest.approx(0.55)
        assert b.low_mid == pytest.approx(0.35)
        assert b.close_mid == pytest.approx(0.48)
        assert b.tick_count == 4

    def test_open_close_bid_ask_are_first_last(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        tick(session, at=start + timedelta(seconds=10), bid=40, ask=44)
        tick(session, at=start + timedelta(seconds=200), bid=46, ask=50)
        aggregate(session, hours=1)

        b = buckets(session)[0]
        assert (b.open_bid, b.open_ask) == (40, 44)
        assert (b.close_bid, b.close_ask) == (46, 50)

    def test_spread_and_liquidity_aggregation(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        tick(session, at=start + timedelta(seconds=10), bid=40, ask=44, liq=1000)  # spread 4
        tick(session, at=start + timedelta(seconds=60), bid=40, ask=48, liq=3000)  # spread 8
        tick(session, at=start + timedelta(seconds=90), bid=41, ask=47, liq=2000)  # spread 6
        aggregate(session, hours=1)

        b = buckets(session)[0]
        assert (b.spread_min, b.spread_max) == (4, 8)
        assert b.spread_avg == pytest.approx(6.0)
        assert (b.liquidity_min, b.liquidity_max) == (1000, 3000)
        assert b.liquidity_avg == pytest.approx(2000.0)

    def test_ticks_split_across_buckets_and_tickers(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=20), 300)
        tick(session, "KXA-1", at=start + timedelta(seconds=10))
        tick(session, "KXA-1", at=start + timedelta(seconds=400))   # next bucket
        tick(session, "KXB-1", at=start + timedelta(seconds=10))    # other ticker
        stats = aggregate(session, hours=1)

        assert stats.buckets_written == 3
        assert {(b.market_ticker, b.tick_count) for b in buckets(session)} == {
            ("KXA-1", 1), ("KXA-1", 1), ("KXB-1", 1)
        } or all(b.tick_count == 1 for b in buckets(session))

    def test_domain_is_classified_from_ticker(self, session):
        now = datetime.now(timezone.utc)
        tick(session, "KXMLBGAME-26JUL09", at=now - timedelta(minutes=10))
        aggregate(session, hours=1)
        assert buckets(session)[0].domain == "sports_baseball"

    def test_invalid_bucket_seconds_rejected(self, session):
        with pytest.raises(ValueError):
            svc().aggregate(session, bucket_seconds=7)   # doesn't divide 3600
        with pytest.raises(ValueError):
            svc().aggregate(session, bucket_seconds=0)


# --- missing data honesty --------------------------------------------------------


class TestMissingData:
    def test_missing_midpoint_yields_null_ohlc_never_fabricated(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        tick(session, at=start + timedelta(seconds=10), bid=None, ask=None,
             mid=None, spread=None, liq=2000)
        stats = aggregate(session, hours=1)

        assert stats.buckets_written == 1  # liquidity-only tick is still usable
        b = buckets(session)[0]
        assert b.open_mid is None and b.high_mid is None
        assert b.low_mid is None and b.close_mid is None
        assert b.liquidity_avg == pytest.approx(2000.0)
        assert b.tick_count == 1

    def test_partially_missing_midpoints_use_only_present_values(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        tick(session, at=start + timedelta(seconds=10), mid=0.40)
        tick(session, at=start + timedelta(seconds=60), bid=None, ask=None,
             mid=None, spread=None, liq=500)
        tick(session, at=start + timedelta(seconds=90), mid=0.44)
        aggregate(session, hours=1)

        b = buckets(session)[0]
        assert b.open_mid == pytest.approx(0.40)
        assert b.close_mid == pytest.approx(0.44)
        assert b.tick_count == 3

    def test_fully_unusable_rows_are_skipped_and_counted(self, session):
        now = datetime.now(timezone.utc)
        tick(session, at=now - timedelta(minutes=10), bid=None, ask=None,
             mid=None, spread=None, liq=0)
        stats = aggregate(session, hours=1)

        assert stats.rows_skipped_unusable == 1
        assert stats.buckets_written == 0


# --- idempotency / dry-run / bounds ----------------------------------------------


class TestIdempotencyAndBounds:
    def test_rerun_is_idempotent_no_duplicates_same_values(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        tick(session, at=start + timedelta(seconds=10), mid=0.40)
        tick(session, at=start + timedelta(seconds=60), mid=0.44)

        s1 = aggregate(session, hours=1)
        first = [(b.market_ticker, b.bucket_start, b.open_mid, b.close_mid,
                  b.tick_count) for b in buckets(session)]
        s2 = aggregate(session, hours=1)
        second = [(b.market_ticker, b.bucket_start, b.open_mid, b.close_mid,
                   b.tick_count) for b in buckets(session)]

        assert s1.buckets_inserted == 1 and s1.buckets_updated == 0
        assert s2.buckets_inserted == 0 and s2.buckets_updated == 1
        assert first == second
        assert len(second) == 1  # no duplicate rows

    def test_rerun_after_new_ticks_updates_the_bucket(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=10), 300)
        tick(session, at=start + timedelta(seconds=10), mid=0.40)
        aggregate(session, hours=1)
        tick(session, at=start + timedelta(seconds=200), mid=0.50)
        aggregate(session, hours=1)

        b = buckets(session)[0]
        assert b.close_mid == pytest.approx(0.50)
        assert b.tick_count == 2

    def test_dry_run_writes_nothing_but_reports(self, session):
        now = datetime.now(timezone.utc)
        tick(session, at=now - timedelta(minutes=10), mid=0.40)
        stats = aggregate(session, hours=1, dry_run=True)

        assert stats.dry_run is True
        assert stats.buckets_written == 1  # reported
        assert session.query(MarketPriceTickBucket).count() == 0  # written: none

    def test_row_cap_truncates_on_hour_boundary_and_reports(self, session):
        now = datetime.now(timezone.utc)
        # two ticks in each of the last 3 full hours
        for h in (3, 2, 1):
            start = bucket_start_for(now - timedelta(hours=h), 3600)
            tick(session, at=start + timedelta(minutes=1), mid=0.4)
            tick(session, at=start + timedelta(minutes=2), mid=0.5)
        session.commit()
        stats = svc().aggregate(session, hours=3, max_rows=3)

        assert stats.truncated is True
        assert stats.covered_until is not None
        # the pass stopped early; a rerun continues (idempotent)
        stats2 = svc().aggregate(session, hours=3, max_rows=10_000)
        assert stats2.truncated is False

    def test_aggregation_never_deletes_raw_ticks(self, session):
        now = datetime.now(timezone.utc)
        for i in range(20):
            tick(session, at=now - timedelta(minutes=30) + timedelta(seconds=20 * i))
        session.commit()
        before = session.query(MarketPriceTick).count()
        aggregate(session, hours=2)
        aggregate(session, hours=2)  # rerun too
        assert session.query(MarketPriceTick).count() == before


# --- retention integration --------------------------------------------------------


class TestRetention:
    def test_raw_tick_retention_window_unchanged(self):
        """OPS-012 must not shorten raw tick retention: the default and the
        settings mapping stay exactly as OPS-011 left them."""
        assert RetentionConfig().tick_days == 7
        s = Settings(_env_file=None)
        assert RetentionConfig.from_settings(s).tick_days == s.tick_retention_days

    def test_bucket_window_default_is_long(self):
        cfg = RetentionConfig.from_settings(Settings(_env_file=None))
        assert cfg.tick_bucket_days == 90

    def test_retention_prunes_only_old_buckets_never_raw_by_bucket_window(self, session):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        session.add(MarketPriceTickBucket(
            market_ticker="KXOLD", bucket_start=old, bucket_seconds=300,
            tick_count=1, created_at=old))
        session.add(MarketPriceTickBucket(
            market_ticker="KXNEW", bucket_start=now, bucket_seconds=300,
            tick_count=1, created_at=now))
        # a raw tick older than the BUCKET window but inside the raw window must survive
        tick(session, "KXRAW", at=now - timedelta(days=2))
        session.commit()

        counts = RetentionService(RetentionConfig(tick_days=7, tick_bucket_days=90)).prune(session)
        assert counts["market_price_tick_buckets"] == 1
        remaining = [b.market_ticker for b in session.query(MarketPriceTickBucket).all()]
        assert remaining == ["KXNEW"]
        assert session.query(MarketPriceTick).count() == 1  # raw untouched

    def test_prune_report_includes_bucket_table(self, session):
        session.commit()
        rows = RetentionService(RetentionConfig()).prune_report(session)
        assert any(r.table == "market_price_tick_buckets" for r in rows)
        raw = next(r for r in rows if r.table == "market_price_ticks")
        assert raw.window_days == RetentionConfig().tick_days  # unchanged

    def test_bucket_table_not_in_protected_list(self):
        assert "market_price_tick_buckets" not in PROTECTED_TABLES  # prunable at 90d


# --- report -----------------------------------------------------------------------


class TestReport:
    def test_report_renders_empty(self, session):
        r = TickAggregationReportService().build(session, settings=Settings(_env_file=None))
        assert r.bucket_total == 0
        assert "run aggregate-market-ticks first" in r.staged_recommendation
        assert r.note == AGGREGATION_NOTE

    def test_report_counts_compression_and_domains(self, session):
        now = datetime.now(timezone.utc)
        start = bucket_start_for(now - timedelta(minutes=20), 300)
        for i in range(10):
            tick(session, "KXMLBGAME-1", at=start + timedelta(seconds=10 * (i + 1)))
        aggregate(session, hours=1)

        r = TickAggregationReportService().build(session, settings=Settings(_env_file=None))
        assert r.bucket_total == 1
        assert r.compression_ratio == pytest.approx(10.0)
        assert r.buckets_by_domain == {"sports_baseball": 1}
        assert r.buckets_by_seconds == {"300": 1}

    def test_healthy_coverage_stages_but_does_not_enact(self, session):
        now = datetime.now(timezone.utc)
        for h in range(1, 4):
            start = bucket_start_for(now - timedelta(hours=h), 3600)
            tick(session, at=start + timedelta(minutes=1))
        aggregate(session, hours=4)

        r = TickAggregationReportService().build(session, settings=Settings(_env_file=None))
        assert r.coverage_healthy is True
        assert "FUTURE OPS milestone" in r.staged_recommendation
        assert "NOT enacted" in r.staged_recommendation
        assert r.retention["raw_tick_days (UNCHANGED by OPS-012)"] == 7

    def test_unhealthy_coverage_recommends_no_change(self, session):
        now = datetime.now(timezone.utc)
        for h in range(1, 5):
            start = bucket_start_for(now - timedelta(hours=h), 3600)
            tick(session, at=start + timedelta(minutes=1))
        session.commit()
        # aggregate only ONE of the four hours -> coverage < 95%
        svc().aggregate(session, hours=4, max_rows=1)

        r = TickAggregationReportService().build(session, settings=Settings(_env_file=None))
        assert r.coverage_healthy is False
        assert "keep raw tick retention unchanged" in r.staged_recommendation.lower()


# --- CLI --------------------------------------------------------------------------


class TestCLI:
    def test_aggregate_cli_parses_options(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "aggregate_market_ticks", fake)
        rc = cli.main(["aggregate-market-ticks", "--hours", "48",
                       "--bucket-seconds", "900", "--dry-run", "--max-rows", "500"])
        assert rc == 0
        assert captured == {"hours": 48, "bucket_seconds": 900,
                            "dry_run": True, "max_rows": 500,
                            "subwindow_hours": None, "scheduled": False}

    def test_aggregate_cli_runs_and_prints(self, session, capsys):
        now = datetime.now(timezone.utc)
        tick(session, at=now - timedelta(minutes=10), mid=0.4)
        session.commit()

        n = asyncio.run(cli.aggregate_market_ticks(hours=1, session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "buckets_written=1" in out
        assert "raw ticks unchanged" in out

    def test_aggregate_cli_dry_run_prints_banner(self, session, capsys):
        now = datetime.now(timezone.utc)
        tick(session, at=now - timedelta(minutes=10), mid=0.4)
        session.commit()

        asyncio.run(cli.aggregate_market_ticks(hours=1, dry_run=True, session=session))
        assert "DRY RUN — nothing written" in capsys.readouterr().out
        assert session.query(MarketPriceTickBucket).count() == 0

    def test_report_cli_runs(self, session, capsys):
        session.commit()
        n = asyncio.run(cli.tick_aggregation_report(session=session))
        out = capsys.readouterr().out
        assert n == 0
        assert "staged recommendation (NOT enacted)" in out
        assert "not advice" in out


# --- migration / safety -----------------------------------------------------------


def test_migration_0023_round_trips(tmp_path):
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/t.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0023")
    assert "market_price_tick_buckets" in inspect(create_engine(url)).get_table_names()
    command.downgrade(cfg, "0022")
    assert "market_price_tick_buckets" not in inspect(create_engine(url)).get_table_names()
    command.upgrade(cfg, "0023")  # up again in one process
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("market_price_tick_buckets")}
    assert {"open_mid", "high_mid", "low_mid", "close_mid", "tick_count"} <= cols


class TestSafety:
    def test_bucket_model_has_no_forbidden_columns(self):
        cols = set(MarketPriceTickBucket.__table__.columns.keys())
        for bad in ("side", "size", "ev", "expected_value", "action", "profit",
                    "recommendation", "order", "wallet", "arbitrage", "arb",
                    "signal", "edge", "pnl"):
            assert bad not in cols

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tick_aggregation.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("arbitrage", "opportunity", "expected_value", "paper_trad",
                    "place_order", "wallet", "private_key", "kelly",
                    "position_siz", "swap", "jupiter", "recommend_trade"):
            assert bad not in code

    def test_module_makes_no_external_calls(self):
        src = (REPO / "app" / "services" / "tick_aggregation.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_report_dataclass_has_no_forbidden_fields(self):
        for cls in (TickAggregationReport, AggregationStats):
            fields = set(cls.__annotations__)
            for bad in ("ev", "side", "size", "profit", "edge", "arbitrage",
                        "opportunity", "order", "wallet", "recommendation"):
                assert bad not in fields
