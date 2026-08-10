from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import get_settings
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


def test_0008_0009_create_and_drop_outcome_and_score_tables(tmp_path):
    url = f"sqlite:///{tmp_path}/calibration.db"
    run_migrations(url)

    assert {"market_outcomes", "forecast_scores"} <= _tables(url)
    assert {
        "id",
        "market_ticker",
        "outcome_status",
        "resolved_probability",
        "winning_side",
        "settlement_price",
        "close_time",
        "settled_time",
        "source",
        "raw_payload",
        "created_at",
    } <= _columns(url, "market_outcomes")
    assert {
        "id",
        "forecast_id",
        "market_ticker",
        "outcome_id",
        "brier_score",
        "log_loss",
        "absolute_error",
        "was_resolved",
        "score_status",
        "score_notes",
        "score_tags",
        "created_at",
    } <= _columns(url, "forecast_scores")

    command.downgrade(_config(url), "0008")
    assert "forecast_scores" not in _tables(url)
    assert "market_outcomes" in _tables(url)
    command.downgrade(_config(url), "0007")
    assert "market_outcomes" not in _tables(url)


def test_0010_0011_create_and_drop_pipeline_tables(tmp_path):
    url = f"sqlite:///{tmp_path}/pipeline.db"
    run_migrations(url)

    assert {"pipeline_runs", "pipeline_stage_runs"} <= _tables(url)
    assert {
        "id",
        "run_type",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "config",
        "summary",
        "error_type",
        "error_message",
        "created_at",
    } <= _columns(url, "pipeline_runs")
    assert {
        "id",
        "pipeline_run_id",
        "stage_name",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "items_attempted",
        "items_succeeded",
        "items_failed",
        "summary",
        "error_type",
        "error_message",
        "created_at",
    } <= _columns(url, "pipeline_stage_runs")

    command.downgrade(_config(url), "0010")
    assert "pipeline_stage_runs" not in _tables(url)
    assert "pipeline_runs" in _tables(url)
    command.downgrade(_config(url), "0009")
    assert "pipeline_runs" not in _tables(url)


def test_0012_creates_and_drops_watcher_tables(tmp_path):
    url = f"sqlite:///{tmp_path}/watcher.db"
    run_migrations(url)

    assert {"market_price_ticks", "opportunity_signals", "watcher_runs"} <= _tables(url)
    assert {
        "id", "market_ticker", "observed_at", "yes_bid", "yes_ask", "midpoint",
        "spread", "volume_24h", "liquidity_proxy", "raw_payload", "created_at",
    } <= _columns(url, "market_price_ticks")
    assert {
        "id", "market_ticker", "signal_type", "signal_status", "observed_at",
        "old_midpoint", "new_midpoint", "price_change", "spread", "liquidity_proxy",
        "latest_forecast_id", "latest_forecast_probability", "reason", "evidence",
        "raw_payload", "created_at",
    } <= _columns(url, "opportunity_signals")
    assert {
        "id", "status", "started_at", "finished_at", "duration_ms",
        "markets_checked", "ticks_recorded", "signals_created",
        "error_type", "error_message", "created_at",
    } <= _columns(url, "watcher_runs")

    command.downgrade(_config(url), "0011")
    for table in ("market_price_ticks", "opportunity_signals", "watcher_runs"):
        assert table not in _tables(url)


def test_0013_adds_signal_workflow_fields(tmp_path):
    url = f"sqlite:///{tmp_path}/signal_workflow.db"
    run_migrations(url)

    columns = _columns(url, "opportunity_signals")
    assert {
        "promoted_at",
        "processed_at",
        "refreshed_research_packet_id",
        "refreshed_forecast_id",
        "processing_error_type",
        "processing_error_message",
    } <= columns

    command.downgrade(_config(url), "0012")
    downgraded = _columns(url, "opportunity_signals")
    assert "promoted_at" not in downgraded
    assert "refreshed_forecast_id" not in downgraded


def _column_specs(url: str, table: str) -> dict:
    engine = create_engine(url)
    try:
        return {col["name"]: col for col in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _type_family(sa_type) -> str:
    """CRYPTO-COVERAGE-REPAIR-001 B12 — a coarse, dialect-tolerant type
    family used to catch a genuinely wrong migrated column type (e.g. a
    model's Integer column landing as TEXT in the migrated schema) without
    being brittle to SQLite's affinity-based reflection (DATETIME/TIMESTAMP,
    BOOLEAN/INTEGER-affinity, etc. are legitimately different concrete
    SQLAlchemy classes for the "same" declared intent). `None` means "cannot
    classify" (a custom TypeDecorator such as RawJSON) — callers must skip
    the family check in that case rather than guess."""
    try:
        py = sa_type.python_type
    except NotImplementedError:
        return None
    if py is bool:
        return "bool"
    if py in (int,):
        return "int"
    if py in (float,):
        return "float"
    if py is str:
        return "str"
    if py.__name__ in ("datetime", "date", "time"):
        return "temporal"
    return py.__name__


# CRYPTO-COVERAGE-REPAIR-001 B12 — pre-existing nullable drift this
# strengthened test DISCOVERED (not introduced): 13 columns across several
# unrelated milestones (MEME-MAS, Polymarket observation, cross-venue
# comparability, crypto horizon cohorts) declare `nullable=False` on the ORM
# model but the deployed migration never added the matching NOT NULL
# constraint. Fixing 13 migrations across unrelated lanes — each needing its
# own backfill/data-safety review before tightening a live column — is a
# separate, dedicated milestone's work, not an in-scope shotgun edit here.
# This allowlist keeps today's real gaps from silently regressing further
# (nothing may be ADDED to this set without justification) while still
# making the check strict for every other column, including any new one:
# a freshly introduced nullable mismatch anywhere else still fails loudly.
_KNOWN_NULLABLE_DRIFT = {
    ("meme_attention_snapshots", "chain"),
    ("meme_attention_snapshots", "token_address"),
    ("meme_catalyst_events", "source"),
    ("meme_catalyst_events", "subject_type"),
    ("meme_catalyst_events", "subject_ref"),
    ("meme_catalyst_events", "catalyst_type"),
    ("domain_market_inventory_snapshots", "domain"),
    ("polymarket_markets", "market_id"),
    ("polymarket_orderbook_snapshots", "token_id"),
    ("polymarket_domain_inventory_snapshots", "domain"),
    ("cross_venue_market_candidates", "match_label"),
    ("crypto_horizon_cohort_members", "cohort_id"),
    ("crypto_horizon_observations", "cohort_id"),
}


def test_migrated_schema_matches_orm_metadata(tmp_path):
    """Every ORM-mapped column must exist in the migrated schema, with a
    compatible type, length/width, and nullability — not just the same name.

    CRYPTO-COVERAGE-REPAIR-001 B12 fix: the original version of this test
    compared column NAMES only (`orm_names <= migrated_names`), so a model
    declaring `String(32)` over a migrated `VARCHAR(16)` — a real
    truncation-risk defect an application could hit in production without
    ever seeing a migration-time error — passed silently. This version also
    asserts: nullability matches (except `_KNOWN_NULLABLE_DRIFT`'s
    pre-existing, out-of-scope debt — see its docstring); a model's declared
    String/Text length is <= the migrated column's length (narrower storage
    than the model promises is a real defect; migrated columns are allowed
    to be AT LEAST as wide, matching how this repo's own migrations have
    widened columns over time, e.g. migration 0028's
    CryptoTokenLifecycleRun.status); and, where classifiable, the two
    columns' coarse type family (bool/int/float/str/temporal) agrees.
    Custom TypeDecorator columns (`python_type` raises
    `NotImplementedError`, e.g. `RawJSON`) skip the family check — there is
    no dialect-neutral way to classify them from reflection alone — but
    still get the name/nullability/length checks."""
    url = f"sqlite:///{tmp_path}/parity.db"
    run_migrations(url)
    for table in Base.metadata.tables.values():
        migrated = _column_specs(url, table.name)
        for col in table.columns:
            assert col.name in migrated, (
                f"{table.name}.{col.name} missing from migrated schema"
            )
            db_col = migrated[col.name]
            if (table.name, col.name) not in _KNOWN_NULLABLE_DRIFT:
                assert bool(db_col["nullable"]) == bool(col.nullable), (
                    f"{table.name}.{col.name}: nullable mismatch — "
                    f"model={col.nullable} migrated={db_col['nullable']}"
                )
            model_len = getattr(col.type, "length", None)
            if model_len is not None:
                db_len = getattr(db_col["type"], "length", None)
                assert db_len is not None and db_len >= model_len, (
                    f"{table.name}.{col.name}: model declares length "
                    f"{model_len} but migrated column is {db_col['type']!r} "
                    f"(length {db_len}) — narrower migrated storage than "
                    f"the model contract promises"
                )
            model_family = _type_family(col.type)
            db_family = _type_family(db_col["type"])
            if model_family is not None and db_family is not None:
                assert model_family == db_family, (
                    f"{table.name}.{col.name}: type family mismatch — "
                    f"model={col.type!r} ({model_family}) "
                    f"migrated={db_col['type']!r} ({db_family})"
                )


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


def test_0014_creates_and_drops_crypto_arena_tables(tmp_path):
    url = f"sqlite:///{tmp_path}/crypto.db"
    run_migrations(url)

    crypto_tables = {
        "crypto_tokens",
        "crypto_pairs",
        "crypto_token_discovery_events",
        "crypto_token_risk_assessments",
        "crypto_price_ticks",
        "crypto_opportunity_signals",
        "crypto_watcher_runs",
    }
    assert crypto_tables <= _tables(url)

    assert {"chain", "token_address", "symbol", "name", "decimals", "metadata",
            "first_seen_at", "last_seen_at"} <= _columns(url, "crypto_tokens")
    assert {"pair_address", "base_token_address", "quote_token_address", "dex_id",
            "url", "pair_created_at"} <= _columns(url, "crypto_pairs")
    assert {"price_usd", "liquidity_usd", "volume_5m_usd", "volume_1h_usd",
            "volume_24h_usd", "price_change_5m", "price_change_1h", "market_cap",
            "fdv"} <= _columns(url, "crypto_price_ticks")
    assert {"signal_type", "signal_status", "reason", "evidence"} <= _columns(
        url, "crypto_opportunity_signals"
    )
    assert {"risk_score", "risk_level", "flags", "provider"} <= _columns(
        url, "crypto_token_risk_assessments"
    )
    assert {"tokens_checked", "pairs_checked", "ticks_recorded",
            "signals_created"} <= _columns(url, "crypto_watcher_runs")

    command.downgrade(_config(url), "0013")
    assert not (crypto_tables & _tables(url))


def test_0015_creates_and_drops_marketops_tables(tmp_path):
    url = f"sqlite:///{tmp_path}/marketops.db"
    run_migrations(url)

    assert {"marketops_runs", "marketops_alerts"} <= _tables(url)
    assert {"status", "config", "summary", "signals_seen", "signals_promoted",
            "signals_processed", "crypto_tokens_seen", "crypto_signals_created",
            "outcomes_synced", "forecasts_scored", "alerts_created", "error_type",
            "error_message"} <= _columns(url, "marketops_runs")
    assert {"alert_type", "severity", "status", "title", "message", "evidence",
            "resolved_at"} <= _columns(url, "marketops_alerts")

    command.downgrade(_config(url), "0014")
    assert not ({"marketops_runs", "marketops_alerts"} & _tables(url))


def test_0016_adds_and_drops_risk_engine_columns(tmp_path):
    url = f"sqlite:///{tmp_path}/riskengine.db"
    run_migrations(url)

    engine_columns = {
        "liquidity_risk_score",
        "holder_risk_score",
        "authority_risk_score",
        "market_structure_risk_score",
        "manipulation_risk_score",
        "provider_risk_score",
        "composite_risk_score",
        "composite_risk_level",
        "risk_reasons",
        "provider_names",
        "heuristic_version",
    }
    assert engine_columns <= _columns(url, "crypto_token_risk_assessments")

    command.downgrade(_config(url), "0015")
    remaining = _columns(url, "crypto_token_risk_assessments")
    assert not (engine_columns & remaining)
    assert {"provider", "risk_score", "flags"} <= remaining  # CRYPTO-001 intact


def test_0017_creates_and_drops_edge_precheck_snapshots(tmp_path):
    url = f"sqlite:///{tmp_path}/edge.db"
    run_migrations(url)

    assert "edge_precheck_snapshots" in _tables(url)
    assert {"market_ticker", "forecast_id", "signal_id", "market_snapshot_id",
            "resolution_assessment_id", "forecaster_name", "evidence_depth",
            "forecast_probability", "forecast_confidence", "forecast_risk",
            "market_midpoint", "yes_bid", "yes_ask", "spread_cents",
            "liquidity_proxy_cents", "probability_gap", "abs_probability_gap",
            "status", "invalidation_reasons", "forecast_age_seconds",
            "market_snapshot_age_seconds", "persistence_count", "thresholds",
            "tags", "raw_context"} <= _columns(url, "edge_precheck_snapshots")

    command.downgrade(_config(url), "0016")
    assert "edge_precheck_snapshots" not in _tables(url)


def test_0018_creates_and_drops_frontier_eval_runs(tmp_path):
    url = f"sqlite:///{tmp_path}/eval.db"
    run_migrations(url)
    assert "frontier_eval_runs" in _tables(url)
    assert {"status", "started_at", "finished_at", "duration_ms", "window_start",
            "window_end", "summary", "warnings"} <= _columns(url, "frontier_eval_runs")
    command.downgrade(_config(url), "0017")
    assert "frontier_eval_runs" not in _tables(url)


def test_migration_under_a_real_blocking_lock_waits_the_declared_policy_then_fails_cleanly(
    tmp_path, monkeypatch,
):
    """CRYPTO-COVERAGE-REPAIR-001 B11 — alembic/env.py:40's engine used to be
    built with NO `connect_args`, so a migration hitting a real writer lock
    waited at Python's INCIDENTAL `sqlite3` default (5s), not this app's
    deliberately reviewed `sqlite_busy_timeout_ms` policy (30s in
    production; every other SQLite writer in this codebase goes through
    `app.db.connect_args_for`, which reads that same setting). The same gap
    existed at app/db.py:66's inspection engine. Fixed by threading
    `connect_args_for(url)` into both.

    Proof, with a REAL second connection holding `BEGIN EXCLUSIVE` (blocks
    readers too, not just writers — see the crypto-tape reconciler's own
    real-lock tests for why `BEGIN IMMEDIATE` alone is not enough to prove
    this): with the declared busy timeout overridden (via
    `SQLITE_BUSY_TIMEOUT_MS`, which both `alembic/env.py` and `app/db.py`
    read through `get_settings()`) to a value BELOW Python's 5s incidental
    default, `run_migrations` against a real held lock must fail in
    ROUGHLY that declared window, not silently pad out to 5s. That is the
    exact signal that would go undetected if `connect_args_for` were ever
    dropped again: this test's assertion on the observed wait window is the
    thing that actually distinguishes "used the declared policy" from
    "used Python's incidental default" — a test that only checked
    "eventually raises OperationalError" would pass under either.

    Also asserts no partial-upgrade: the DB was already at `head` before the
    lock was taken, so a failed re-upgrade attempt must leave the schema
    revision exactly where it was — never a half-applied migration."""
    import sqlite3
    import time

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy.exc import OperationalError

    url = f"sqlite:///{tmp_path}/locked_migration.db"
    db_path = tmp_path / "locked_migration.db"
    run_migrations(url)  # bring to head while unlocked

    config = _config(url)
    script = ScriptDirectory.from_config(config)
    head_before = script.get_current_head()

    def _current_rev() -> str | None:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                row = conn.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                return row[0] if row else None
        finally:
            engine.dispose()

    assert _current_rev() == head_before

    declared_timeout_seconds = 1.2
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", str(int(declared_timeout_seconds * 1000)))
    # `get_settings` is `@lru_cache`d (module-global, persists across the
    # whole test session) — the FIRST `run_migrations(url)` call above
    # already resolved and cached a `Settings()` (default
    # `sqlite_busy_timeout_ms=30000`, or whatever an earlier test in this
    # session cached first). Without clearing it here, the env override
    # above would be silently ignored and this test would validate nothing
    # (it would still "pass" against the real, uncontrolled default —
    # exactly the kind of false-positive this milestone's tests exist to
    # avoid). Restore the cache afterward so this test cannot poison any
    # other test's settings for the rest of the session.
    get_settings.cache_clear()

    holder = sqlite3.connect(str(db_path), timeout=0.2)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.perf_counter()
        raised = None
        try:
            run_migrations(url)
        except OperationalError as exc:
            raised = exc
        elapsed = time.perf_counter() - t0
    finally:
        holder.rollback()
        holder.close()
        monkeypatch.undo()
        get_settings.cache_clear()

    assert raised is not None, (
        "run_migrations must fail cleanly under a real held lock, not "
        "silently proceed or hang"
    )
    assert "locked" in str(raised).lower()
    # The bound this test exists to prove: the observed wait tracks the
    # DECLARED policy (1.2s), not Python's incidental 5s default. A
    # generous but still discriminating window: comfortably above the
    # declared timeout (retry/inspection overhead) yet well short of the
    # 5s default this fix removes.
    assert declared_timeout_seconds <= elapsed < 4.0, (
        f"elapsed={elapsed:.2f}s did not track the declared "
        f"{declared_timeout_seconds}s busy-timeout policy — if this creeps "
        f"toward 5s, `connect_args_for` has stopped being threaded into "
        f"the migration engine(s) again"
    )

    # No partial upgrade: revision unchanged (already at head; the failed
    # attempt touched nothing).
    assert _current_rev() == head_before
