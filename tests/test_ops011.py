"""OPS-011 — DB growth observability, retention dry-run detail, alert calibration.

All hermetic: in-memory SQLite, explicit Settings injection (never mutates the
shared get_settings singleton), no live API calls. OPS-011 is ops/observability
only — these tests assert reporting/threshold behavior, never trading logic."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import CryptoPriceTick, MarketPriceTick
from app.services.db_growth import build_growth_report, domain_for_ticker
from app.services.retention import RetentionConfig, RetentionService


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _tick(ticker: str, age_days: float) -> MarketPriceTick:
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    return MarketPriceTick(
        market_ticker=ticker, observed_at=ts, created_at=ts, volume_24h=0, liquidity_proxy=0
    )


def seed_ticks(session: Session):
    session.add_all([
        _tick("KXMLBGAME-26JUL04AB-A", 0.2),     # baseball, <1d
        _tick("KXMLBTOTAL-26JUL04AB-9", 2.0),    # baseball, 1-3d
        _tick("KXWCGAME-26JUL04PARFRA-FRA", 0.5),  # soccer, <1d
        _tick("KXATPSETWINNER-26JUL04AB-A", 5.0),  # tennis, 3-7d
        _tick("KXFEDDECISION-26JUL-A", 9.0),     # macro, >7d (eligible for prune)
    ])
    session.commit()


class TestDomainClassification:
    def test_prefix_domains(self):
        assert domain_for_ticker("KXMLBGAME-X") == "sports_baseball"
        assert domain_for_ticker("KXWCGAME-X") == "sports_soccer"
        assert domain_for_ticker("KXATPSETWINNER-X") == "sports_tennis"
        assert domain_for_ticker("KXFED-X") == "macro"
        assert domain_for_ticker("SOMETHING-ELSE") == "general"


class TestGrowthReport:
    def test_row_counts_and_tick_totals(self, session):
        seed_ticks(session)
        r = build_growth_report(session, settings=Settings(_env_file=None))
        assert r.tick_total == 5
        tables = {t.name: t.rows for t in r.tables}
        assert tables["market_price_ticks"] == 5
        # every declared table is present in the report
        assert "edge_precheck_snapshots" in tables
        assert "crypto_price_ticks" in tables

    def test_tick_age_buckets(self, session):
        seed_ticks(session)
        r = build_growth_report(session, settings=Settings(_env_file=None))
        assert r.tick_by_age["<1d"] == 2
        assert r.tick_by_age["1-3d"] == 1
        assert r.tick_by_age["3-7d"] == 1
        assert r.tick_by_age[">7d"] == 1

    def test_tick_by_domain(self, session):
        seed_ticks(session)
        r = build_growth_report(session, settings=Settings(_env_file=None))
        assert r.tick_by_domain["sports_baseball"] == 2
        assert r.tick_by_domain["sports_soccer"] == 1
        assert r.tick_by_domain["sports_tennis"] == 1
        assert r.tick_by_domain["macro"] == 1

    def test_oldest_newest_and_last_hour(self, session):
        seed_ticks(session)
        session.add(_tick("KXMLBGAME-FRESH", 0.001))  # within the last hour
        session.commit()
        r = build_growth_report(session, settings=Settings(_env_file=None))
        assert r.tick_last_hour == 1
        assert r.tick_oldest is not None and r.tick_newest is not None
        assert r.tick_oldest <= r.tick_newest

    def test_thresholds_reflect_settings(self, session):
        settings = Settings(
            _env_file=None,
            db_growth_warning_mb=1536.0,
            db_growth_critical_mb=3072.0,
            marketops_signal_flood_warning_per_hour=400,
            marketops_signal_flood_critical_per_hour=800,
        )
        r = build_growth_report(session, settings=settings)
        assert r.thresholds["db_growth_warning_mb"] == 1536.0
        assert r.thresholds["db_growth_critical_mb"] == 3072.0
        assert r.thresholds["signal_flood_warning_per_hour"] == 400
        assert r.thresholds["signal_flood_critical_per_hour"] == 800

    def test_largest_tables_ordering(self, session):
        seed_ticks(session)
        session.add_all([
            CryptoPriceTick(
                pair_address="p", token_address="tok", chain="solana",
                created_at=datetime.now(timezone.utc),
            ),
        ])
        session.commit()
        r = build_growth_report(session, settings=Settings(_env_file=None))
        # market_price_ticks (5 rows) is the largest by row count
        assert r.largest_tables[0].name == "market_price_ticks"


class TestRetentionDryRunDetail:
    def test_prune_report_projects_eligible_rows(self, session):
        seed_ticks(session)
        service = RetentionService(RetentionConfig(tick_days=7))
        reports = {r.table: r for r in service.prune_report(session)}
        ticks = reports["market_price_ticks"]
        assert ticks.window_days == 7
        assert ticks.total_rows == 5
        assert ticks.eligible_rows == 1  # only the 9-day-old macro tick
        assert ticks.remaining_rows == 4
        assert ticks.oldest is not None and ticks.newest is not None

    def test_signals_kept_forever_when_disabled(self, session):
        service = RetentionService(RetentionConfig(signal_days=0))
        reports = {r.table: r for r in service.prune_report(session)}
        sig = reports["opportunity_signals"]
        assert sig.window_days is None       # keep-forever
        assert sig.eligible_rows == 0

    def test_dry_run_deletes_nothing(self, session):
        seed_ticks(session)
        service = RetentionService(RetentionConfig(tick_days=7))
        service.prune_report(session)
        # prune_report is projection-only; the rows are still present
        r = build_growth_report(session, settings=Settings(_env_file=None))
        assert r.tick_total == 5

    def test_tighter_window_makes_more_rows_eligible(self, session):
        seed_ticks(session)
        service = RetentionService(RetentionConfig(tick_days=3))
        reports = {r.table: r for r in service.prune_report(session)}
        # 3d window: the 5d tennis + 9d macro ticks are now eligible
        assert reports["market_price_ticks"].eligible_rows == 2


class TestRetentionUnchanged:
    def test_existing_prune_behavior_intact(self, session):
        """prune() still deletes exactly the rows past their window (OPS-011
        adds reporting, not new deletion behavior)."""
        seed_ticks(session)
        service = RetentionService(RetentionConfig(tick_days=7))
        counts = service.prune(session, dry_run=True)
        assert counts["market_price_ticks"] == 1  # unchanged from OPS-003 semantics
        # protected tables are still never in the prune set
        assert "market_snapshots" not in counts
        assert "edge_precheck_snapshots" not in counts


class TestCliDbGrowthReport:
    async def test_cli_prints_sections(self, session, capsys, monkeypatch):
        from app import cli

        seed_ticks(session)
        await cli.db_growth_report(session=session)
        out = capsys.readouterr().out
        assert "largest tables" in out
        assert "market_price_ticks" in out
        assert "by age:" in out
        assert "by domain:" in out
        assert "retention windows" in out
        assert "alert thresholds (OPS-011)" in out

    async def test_cli_retention_dry_run_detail(self, session, capsys):
        from app import cli

        seed_ticks(session)
        await cli.prune_retention(dry_run=True, session=session)
        out = capsys.readouterr().out
        assert "retention detail (OPS-011" in out
        assert "market_price_ticks" in out
        assert "eligible=" in out
