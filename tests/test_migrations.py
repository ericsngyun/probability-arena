from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db import PROJECT_ROOT, Base, run_migrations


def _columns(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {col["name"] for col in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_upgrade_head_creates_full_schema(tmp_path):
    url = f"sqlite:///{tmp_path}/migrate.db"
    run_migrations(url)

    tables = _tables(url)
    assert {"markets", "market_snapshots", "orderbook_snapshots", "scanner_runs",
            "alembic_version"} <= tables

    scanner_cols = _columns(url, "scanner_runs")
    assert {"duration_ms", "source", "error_type", "error_message"} <= scanner_cols
    assert "error" not in scanner_cols
    assert "raw_payload" in _columns(url, "market_snapshots")


def test_0004_creates_resolution_assessments_table(tmp_path):
    url = f"sqlite:///{tmp_path}/resolution.db"
    run_migrations(url)

    assert "market_resolution_assessments" in _tables(url)
    columns = _columns(url, "market_resolution_assessments")
    assert {
        "id",
        "market_ticker",
        "scanner_run_id",
        "model_name",
        "prompt_version",
        "clarity_score",
        "resolution_risk",
        "tradeability",
        "settlement_source",
        "resolution_summary",
        "ambiguity_flags",
        "rejection_reasons",
        "llm_confidence",
        "raw_response",
        "created_at",
    } <= columns

    command.downgrade(_config(url), "0003")
    assert "market_resolution_assessments" not in _tables(url)


def test_0005_creates_and_drops_detail_enrichments_table(tmp_path):
    url = f"sqlite:///{tmp_path}/enrichment.db"
    run_migrations(url)

    assert "market_detail_enrichments" in _tables(url)
    columns = _columns(url, "market_detail_enrichments")
    assert {
        "id",
        "market_ticker",
        "scanner_run_id",
        "event_ticker",
        "series_ticker",
        "title",
        "subtitle",
        "rules_text",
        "settlement_source",
        "category",
        "raw_market_detail",
        "raw_event_detail",
        "raw_series_detail",
        "created_at",
    } <= columns

    command.downgrade(_config(url), "0004")
    assert "market_detail_enrichments" not in _tables(url)


def test_0006_creates_and_drops_research_packets_table(tmp_path):
    url = f"sqlite:///{tmp_path}/research.db"
    run_migrations(url)

    assert "market_research_packets" in _tables(url)
    columns = _columns(url, "market_research_packets")
    assert {
        "id",
        "market_ticker",
        "scanner_run_id",
        "enrichment_id",
        "resolution_assessment_id",
        "collector_name",
        "collector_version",
        "domain",
        "source_queries",
        "sources",
        "key_facts",
        "missing_info",
        "research_completeness_score",
        "research_risk",
        "raw_response",
        "created_at",
    } <= columns

    command.downgrade(_config(url), "0005")
    assert "market_research_packets" not in _tables(url)


def test_0007_creates_and_drops_market_forecasts_table(tmp_path):
    url = f"sqlite:///{tmp_path}/forecasts.db"
    run_migrations(url)

    assert "market_forecasts" in _tables(url)
    columns = _columns(url, "market_forecasts")
    assert {
        "id",
        "market_ticker",
        "scanner_run_id",
        "research_packet_id",
        "resolution_assessment_id",
        "forecaster_name",
        "forecaster_version",
        "model_name",
        "prompt_version",
        "estimated_probability",
        "confidence",
        "evidence_depth",
        "forecast_risk",
        "forecast_summary",
        "bull_case",
        "bear_case",
        "skeptic_notes",
        "key_assumptions",
        "missing_info",
        "what_would_change_mind",
        "calibration_tags",
        "raw_response",
        "created_at",
    } <= columns

    command.downgrade(_config(url), "0006")
    assert "market_forecasts" not in _tables(url)


def test_migrated_schema_matches_orm_metadata(tmp_path):
    """Every ORM-mapped column must exist in the migrated schema."""
    url = f"sqlite:///{tmp_path}/parity.db"
    run_migrations(url)
    for table in Base.metadata.tables.values():
        migrated = _columns(url, table.name)
        orm = {col.name for col in table.columns}
        assert orm <= migrated, f"{table.name} missing columns: {orm - migrated}"


def test_downgrade_to_0001_restores_legacy_columns(tmp_path):
    url = f"sqlite:///{tmp_path}/downgrade.db"
    run_migrations(url)
    command.downgrade(_config(url), "0001")

    scanner_cols = _columns(url, "scanner_runs")
    assert "error" in scanner_cols
    assert "duration_ms" not in scanner_cols
    assert "raw_payload" not in _columns(url, "market_snapshots")


def test_downgrade_to_base_drops_all_tables(tmp_path):
    url = f"sqlite:///{tmp_path}/teardown.db"
    run_migrations(url)
    command.downgrade(_config(url), "base")
    assert _tables(url) == {"alembic_version"}


def test_legacy_create_all_db_is_stamped_then_upgraded(tmp_path):
    """A pre-Alembic MVP-001 database (tables, no alembic_version) must be
    stamped at 0001 and upgraded to head without erroring."""
    url = f"sqlite:///{tmp_path}/legacy.db"
    config = _config(url)
    # Build the exact MVP-001 schema via migration 0001, then remove the
    # version table to simulate a create_all-era database.
    command.upgrade(config, "0001")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()

    run_migrations(url)

    assert "alembic_version" in _tables(url)
    assert "duration_ms" in _columns(url, "scanner_runs")


def test_run_migrations_is_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path}/twice.db"
    run_migrations(url)
    run_migrations(url)
    assert "duration_ms" in _columns(url, "scanner_runs")
