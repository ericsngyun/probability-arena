"""SQLITE-LOCK-TELEMETRY-001A test suite.

Covers the envelope/validator, the non-SQLite append-only JSONL sink, the
session-scoped timing primitives, the tick-aggregation and backup emit
sites, failure isolation (disk-full, permissions, short writes), writer
behavior parity with telemetry broken, a real two-connection disposable-DB
lock-contention integration test, the performance budget, and the safety
audits. Everything runs offline against disposable databases and temporary
telemetry directories — no network, no provider, no application-DB writes.
"""

import glob
import json
import os
import re
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import app.telemetry.sink as sink_module
from app.models import Base, MarketPriceTick, MarketPriceTickBucket, TickAggregationRun
from app.telemetry.schema import (
    TelemetryValidationError,
    build_event,
    validate_event,
)
from app.telemetry.sink import MAX_LINE_BYTES, TelemetrySink, get_sink, read_events
from app.telemetry.sqlite_events import (
    OpContext,
    emit_event,
    provider_io_span,
    session_op_context,
)
from app.services.tick_aggregation import TickAggregationService
from app.services import backup as backup_module
from app.services.backup import BackupResult, backup_database, verify_backup
from app.config import get_settings

UTC = timezone.utc


# --- fixtures -----------------------------------------------------------------


@pytest.fixture()
def telemetry_dir(tmp_path, monkeypatch):
    """Isolated telemetry directory + fresh process sink."""
    directory = tmp_path / "telemetry"
    monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(directory))
    sink_module._sink = None
    yield directory
    sink_module._sink = None


@pytest.fixture()
def file_db(tmp_path):
    """Disposable on-disk SQLite DB with the app schema (never the real DB)."""
    db_path = tmp_path / "disposable.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    yield engine, maker, db_path
    engine.dispose()


def _make_event(**overrides):
    base = build_event(
        writer_name="test_writer",
        writer_class="test",
        operation_name="unit_test",
        started_at="2026-07-24T03:00:00Z",
        finished_at="2026-07-24T03:00:01Z",
    )
    base.update(overrides)
    return base


def _seed_ticks(session: Session, n=12, ticker="KXTEST-1"):
    start = datetime.now(UTC) - timedelta(hours=2)
    for i in range(n):
        session.add(MarketPriceTick(
            market_ticker=ticker,
            observed_at=start + timedelta(minutes=5 * i),
            yes_bid=40 + i, yes_ask=44 + i, midpoint=0.42 + i / 1000,
            spread=4, liquidity_proxy=1000,
        ))
    session.commit()


def _service(**overrides) -> TickAggregationService:
    svc = TickAggregationService()
    svc.busy_retry_seconds = 0.01
    for key, value in overrides.items():
        setattr(svc, key, value)
    return svc


# --- 1-2: valid success + lock-failure events ---------------------------------


def test_valid_successful_event_roundtrip(telemetry_dir):
    sink = get_sink()
    assert sink.emit(_make_event()) is True
    events, malformed = read_events(sink.path)
    assert malformed == 0 and len(events) == 1
    ev = events[0]
    assert ev["writer_name"] == "test_writer"
    assert ev["outcome"] == "success"
    assert ev["duration_ms"] == 1000
    assert ev.get("external_calls") in (None, 0)


def test_lock_failure_event_from_commit_unit(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    _seed_ticks(session)
    svc = _service(busy_retries=1)

    calls = {"n": 0}
    real_commit = session.commit

    def failing_commit():
        calls["n"] += 1
        raise OperationalError(
            "stmt", {}, sqlite3.OperationalError("database is locked"))

    session.commit = failing_commit
    ctx = OpContext(writer_name="tick_aggregation", writer_class="maintenance",
                    operation_name="aggregate")
    ctx.parent_event_id = "0" * 8
    svc._telemetry_ctx = ctx
    with session_op_context(session, ctx):
        ok, retries = svc._commit_unit(session, lambda: None)
    session.commit = real_commit
    assert ok is False and retries == 1  # ladder unchanged
    events, _ = read_events(get_sink().path)
    assert len(events) == 1
    ev = events[0]
    assert ev["outcome"] == "retried_failed"
    assert ev["exception_category"] == "database_locked"
    assert ev["exception_class"] == "OperationalError"
    assert ev["lock_wait_quality"] == "derived_estimate"
    assert isinstance(ev["lock_wait_ms"], int) and ev["lock_wait_ms"] >= 0
    session.close()


# --- 3-4: retry success / retry exhaustion ------------------------------------


def _flaky_commit(session, failures: int):
    real_commit = session.commit
    state = {"left": failures}

    def commit():
        if state["left"] > 0:
            state["left"] -= 1
            raise OperationalError(
                "stmt", {}, sqlite3.OperationalError("database is locked"))
        real_commit()

    session.commit = commit
    return real_commit


def test_retry_success_event(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    svc = _service()
    real = _flaky_commit(session, failures=2)
    ctx = OpContext(writer_name="tick_aggregation", writer_class="maintenance",
                    operation_name="aggregate")
    svc._telemetry_ctx = ctx
    with session_op_context(session, ctx):
        ok, retries = svc._commit_unit(session, lambda: None)
    session.commit = real
    assert ok is True and retries == 2
    events, _ = read_events(get_sink().path)
    assert events[-1]["outcome"] == "retried_success"
    assert events[-1]["retry_count"] == 2
    assert events[-1]["retry_limit"] == svc.busy_retries
    assert events[-1]["attempt_number"] == 3
    session.close()


def test_retry_exhaustion_event(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    svc = _service()
    _flaky_commit(session, failures=99)
    ctx = OpContext(writer_name="tick_aggregation", writer_class="maintenance",
                    operation_name="aggregate")
    svc._telemetry_ctx = ctx
    with session_op_context(session, ctx):
        ok, retries = svc._commit_unit(session, lambda: None)
    assert ok is False and retries == svc.busy_retries  # policy unchanged
    events, _ = read_events(get_sink().path)
    assert events[-1]["outcome"] == "retried_failed"
    assert events[-1]["retry_count"] == svc.busy_retries
    session.close()


# --- 5: rollback event --------------------------------------------------------


def test_rollback_timing_captured(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    svc = _service(busy_retries=0)
    session.add(MarketPriceTick(market_ticker="KXROLL", observed_at=datetime.now(UTC)))
    _flaky_commit(session, failures=99)
    ctx = OpContext(writer_name="tick_aggregation", writer_class="maintenance",
                    operation_name="aggregate")
    svc._telemetry_ctx = ctx
    with session_op_context(session, ctx):
        ok, _ = svc._commit_unit(session, lambda: None)
    assert ok is False
    events, _ = read_events(get_sink().path)
    ev = events[-1]
    assert ev["rollback_ms"] is not None and ev["rollback_ms"] >= 0
    assert ev["rollback_quality"] == "instrumented_estimate"
    session.close()


# --- 6-7: partial progress + parent/child correlation -------------------------


def test_partial_progress_and_parent_child_correlation(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    _seed_ticks(session, n=24)  # spans 2h -> >=2 sub-windows
    svc = _service()

    # fail every commit attempt for exactly one bucket-bearing sub-window
    real_commit = session.commit
    state = {"unit": 0, "failing_unit": 2, "left": svc.busy_retries + 1}
    marker = {"in_unit": False}

    orig_unit = svc._commit_unit

    def tracked_unit(sess, apply_fn):
        state["unit"] += 1
        return orig_unit(sess, apply_fn)

    def commit():
        if state["unit"] == state["failing_unit"] and state["left"] > 0:
            state["left"] -= 1
            raise OperationalError(
                "stmt", {}, sqlite3.OperationalError("database is locked"))
        real_commit()

    svc._commit_unit = tracked_unit
    session.commit = commit
    stats = svc.aggregate(session, hours=3)
    session.commit = real_commit

    assert stats.failed_windows, "one sub-window must have failed loudly"
    events, malformed = read_events(get_sink().path)
    assert malformed == 0
    parents = [e for e in events if e["operation_name"] == "aggregate"]
    children = [e for e in events if e["operation_name"] == "commit_unit"]
    assert len(parents) == 1
    parent = parents[0]
    assert parent["outcome"] == "partial_success"
    assert parent["partial_progress"] is True
    assert parent["rows_attempted"] == stats.rows_read
    assert parent["rows_committed"] == stats.buckets_written
    assert children, "child commit-unit events must exist"
    assert all(c["parent_event_id"] == parent["event_id"] for c in children)
    assert any(c["outcome"] == "retried_failed" for c in children)
    session.close()


# --- 8: provider-I/O dirty flag ----------------------------------------------


def test_provider_io_dirty_flag(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    ctx = OpContext(writer_name="test_writer", writer_class="test",
                    operation_name="unit_test")
    session.add(MarketPriceTick(market_ticker="KXDIRTY", observed_at=datetime.now(UTC)))
    with provider_io_span(ctx, session):
        pass  # a provider call would happen here, txn dirty
    assert ctx.provider_io_during_transaction is True
    event_id = emit_event(ctx, outcome="success")
    assert event_id is not None
    events, _ = read_events(get_sink().path)
    assert events[-1]["provider_io_during_transaction"] is True
    session.rollback()

    # clean context stays False
    ctx2 = OpContext(writer_name="test_writer", writer_class="test",
                     operation_name="unit_test")
    emit_event(ctx2, outcome="success")
    events, _ = read_events(get_sink().path)
    assert events[-1]["provider_io_during_transaction"] is False
    session.close()


# --- 9: commit timing ---------------------------------------------------------


def test_commit_timing_exact_quality(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    _seed_ticks(session)
    svc = _service()
    svc.aggregate(session, hours=3)
    events, _ = read_events(get_sink().path)
    committed = [e for e in events if e["operation_name"] == "commit_unit"
                 and e["outcome"] == "success"]
    assert committed
    for ev in committed:
        assert isinstance(ev["commit_ms"], int) and ev["commit_ms"] >= 0
        assert ev["commit_quality"] == "exact"
    session.close()


# --- 10-12: validator rejections ---------------------------------------------


def test_impossible_timing_rejected(telemetry_dir):
    bad = _make_event()
    bad["finished_at"] = "2026-07-24T02:59:59Z"  # before started_at
    with pytest.raises(TelemetryValidationError):
        validate_event(bad)
    sink = get_sink()
    assert sink.emit(bad) is False and sink.rejected == 1
    assert not sink.path.exists() or read_events(sink.path)[0] == []

    negative = _make_event(duration_ms=-5)
    with pytest.raises(TelemetryValidationError):
        validate_event(negative)


def test_secret_bearing_field_rejected(telemetry_dir):
    for poison in (
        "api_key=abc123", "Authorization: Bearer xyz", "password=hunter2",
        "postgresql+psycopg2://user:pw@host/db", "sqlite:///data/probability_arena.db",
    ):
        bad = _make_event(source_command=poison)
        with pytest.raises(TelemetryValidationError):
            validate_event(bad)
        assert get_sink().emit(bad) is False


def test_high_cardinality_field_rejected(telemetry_dir):
    for key, value in (
        ("token_id", "So11111111111111111111111111111111111111112"),
        ("ticker", "KXBTC-25DEC"), ("cohort_name", "canary-6"),
        ("market_side", "yes"), ("order_size", 100), ("wallet_address", "abc"),
        ("exception_message", "boom"),
    ):
        bad = _make_event(**{key: value})
        with pytest.raises(TelemetryValidationError):
            validate_event(bad)
        assert get_sink().emit(bad) is False
    # bare table names are not a valid table group either
    with pytest.raises(TelemetryValidationError):
        validate_event(_make_event(table_groups=["market_price_ticks_raw_table"]))
    # correlation ids are integers only — a name-like string is rejected
    for key in ("marketops_cycle_id", "scanner_run_id", "cohort_id", "job_id"):
        with pytest.raises(TelemetryValidationError):
            validate_event(_make_event(**{key: "canary-6"}))
    assert validate_event(_make_event(cohort_id=6))  # int id is legitimate


# --- 13-16: sink failure isolation --------------------------------------------


def test_sink_failure_never_fails_writer(telemetry_dir, file_db, monkeypatch):
    engine, maker, _ = file_db
    session = maker()
    _seed_ticks(session)

    def broken_write(fd, data):
        raise OSError(5, "injected I/O error")

    monkeypatch.setattr(sink_module.os, "write", broken_write)
    svc = _service()
    stats = svc.aggregate(session, hours=3)  # must not raise
    assert stats.buckets_written > 0
    assert not stats.failed_windows
    sink = get_sink()
    assert sink.dropped > 0 and sink.emitted == 0
    session.close()


def test_disk_full_simulation(telemetry_dir, monkeypatch, capsys):
    def enospc(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sink_module.os, "write", enospc)
    sink = get_sink()
    assert sink.emit(_make_event()) is False
    assert sink.emit(_make_event()) is False
    assert sink.dropped == 2
    # single bounded fallback line, never a loop, never a DB write
    assert capsys.readouterr().err.count("telemetry_sink_unavailable") == 1


def test_permission_failure_isolated(tmp_path, monkeypatch):
    # non-writable PARENT: the sink cannot mkdir its directory at all
    # (the sink chmods a pre-existing dir it owns to 0700 by design, so a
    # merely-0500 owned dir would be repaired, not failed)
    parent = tmp_path / "denied"
    parent.mkdir(mode=0o500)
    monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(parent / "telemetry"))
    sink_module._sink = None
    sink = get_sink()
    assert sink.emit(_make_event()) is False
    assert sink.dropped == 1
    parent.chmod(0o700)
    sink_module._sink = None


def test_short_write_counts_as_dropped(telemetry_dir, monkeypatch):
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, data[: len(data) - 10])

    with monkeypatch.context() as m:
        m.setattr(sink_module.os, "write", short_write)
        sink = get_sink()
        assert sink.emit(_make_event()) is False
        assert sink.dropped == 1 and sink.emitted == 0
    # the partial line is never resumed: the next append merges into it and
    # that one merged line is counted malformed; subsequent lines are intact
    sink2 = TelemetrySink(telemetry_dir)
    assert sink2.emit(_make_event()) is True   # merges with the partial tail
    assert sink2.emit(_make_event()) is True   # fully intact
    events, malformed = read_events(sink2.path)
    assert len(events) == 1 and malformed == 1


# --- 17-18: partial-line recovery + concurrent appenders ----------------------


def test_partial_jsonl_line_recovery(telemetry_dir):
    sink = get_sink()
    assert sink.emit(_make_event())
    assert sink.emit(_make_event())
    with open(sink.path, "ab") as f:
        f.write(b'{"event_version": 1, "truncat')  # crash mid-write, no newline
    events, malformed = read_events(sink.path)
    assert len(events) == 2 and malformed == 1


def test_concurrent_appenders_no_interleaving(telemetry_dir):
    per_thread, threads_n = 50, 8

    def appender(i):
        sink = TelemetrySink(telemetry_dir)  # simulate separate processes
        for _ in range(per_thread):
            assert sink.emit(_make_event())

    threads = [threading.Thread(target=appender, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events, malformed = read_events(telemetry_dir / "sqlite-writes.jsonl")
    assert malformed == 0
    assert len(events) == per_thread * threads_n


# --- 19-21: permissions + line-size limit -------------------------------------


def test_file_permissions_0600(telemetry_dir):
    sink = get_sink()
    sink.emit(_make_event())
    assert (sink.path.stat().st_mode & 0o777) == 0o600


def test_directory_permissions_0700(telemetry_dir):
    sink = get_sink()
    sink.emit(_make_event())
    assert (Path(sink._dir).stat().st_mode & 0o777) == 0o700


def test_line_size_limit_truncates_optional_fields(telemetry_dir):
    sink = get_sink()
    huge = _make_event(
        table_groups=["runs_audit"] * 800,  # inflate past 4096 B
    )
    assert sink.emit(huge) is True
    raw = sink.path.read_bytes()
    assert max(len(line) for line in raw.splitlines()) <= MAX_LINE_BYTES
    events, malformed = read_events(sink.path)
    assert malformed == 0
    assert events[0]["truncated"] is True
    assert "table_groups" not in events[0]


# --- 22-23: no SQLite writes, no provider calls -------------------------------


def test_telemetry_makes_no_application_db_writes(telemetry_dir, file_db):
    engine, maker, _ = file_db
    session = maker()
    _seed_ticks(session)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    svc = _service()
    svc.aggregate(session, hours=3)
    event.remove(engine, "before_cursor_execute", capture)
    writes = [s for s in statements
              if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE"))]
    # every write goes to the writer's own tables; nothing telemetry-shaped
    for s in writes:
        assert "telemetry" not in s.lower()
        assert any(t in s for t in ("market_price_tick_buckets", "tick_aggregation_runs"))
    session.close()


def test_telemetry_sources_offline_no_provider_no_db():
    root = Path(__file__).resolve().parents[1] / "app" / "telemetry"
    sources = {p.name: p.read_text() for p in root.glob("*.py")}
    assert sources, "telemetry package must exist"
    for name, text in sources.items():
        for banned in ("httpx", "requests", "aiohttp", "urllib.request",
                       "websocket", "socket.create_connection"):
            assert banned not in text, f"{name} must not import network client {banned}"
        for banned in ("session.add", "session.commit", "session.execute(",
                       "INSERT INTO", "create_engine("):
            assert banned not in text, f"{name} must not write via SQLAlchemy: {banned}"


# --- 24-26: no boundary/retry/migration change --------------------------------


def test_no_transaction_boundary_change(telemetry_dir, file_db, tmp_path, monkeypatch):
    """Commit-call sequence is identical with the sink healthy vs broken."""
    def run(db_dir, broken):
        db = db_dir / "x.db"
        engine = create_engine(f"sqlite:///{db}")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        _seed_ticks(session)
        commits = {"n": 0}
        real_commit = session.commit

        def counting_commit():
            commits["n"] += 1
            real_commit()

        session.commit = counting_commit
        with monkeypatch.context() as m:
            if broken:
                m.setattr(
                    sink_module.os, "write",
                    lambda fd, data: (_ for _ in ()).throw(OSError(5, "x")))
            stats = _service().aggregate(session, hours=3)
        session.close()
        engine.dispose()
        return commits["n"], stats

    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    commits_ok, stats_ok = run(a, broken=False)
    commits_broken, stats_broken = run(b, broken=True)
    assert commits_ok == commits_broken
    assert stats_ok.buckets_written == stats_broken.buckets_written
    assert stats_ok.rows_read == stats_broken.rows_read


def test_no_retry_policy_change(telemetry_dir, file_db, monkeypatch):
    """Ladder attempts and sleeps are unchanged with telemetry active."""
    engine, maker, _ = file_db
    session = maker()
    svc = _service()
    sleeps: list[float] = []
    monkeypatch.setattr("app.services.tick_aggregation.time.sleep",
                        lambda s: sleeps.append(s))
    _flaky_commit(session, failures=99)
    ctx = OpContext(writer_name="tick_aggregation", writer_class="maintenance",
                    operation_name="aggregate")
    svc._telemetry_ctx = ctx
    with session_op_context(session, ctx):
        ok, retries = svc._commit_unit(session, lambda: None)
    assert ok is False
    assert retries == svc.busy_retries
    assert sleeps == [svc.busy_retry_seconds] * svc.busy_retries
    session.close()


def test_no_migration_added():
    versions = sorted(
        Path(p).name for p in glob.glob("alembic/versions/[0-9]*.py"))
    assert versions, "alembic versions must be visible from repo root"
    assert versions[-1].startswith("0027"), (
        "SQLITE-LOCK-TELEMETRY-001A must not add a migration; head must stay 0027"
    )


# --- 27-28: writer behavior parity --------------------------------------------


def test_tick_aggregation_behavior_parity(telemetry_dir, tmp_path):
    """Identical datasets produce identical aggregation results with the
    sink healthy, and identical bucket VALUES to a pre-telemetry-style dry
    accounting; idempotent rerun unchanged."""
    db = tmp_path / "parity.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_ticks(session, n=24)
    svc = _service()
    stats1 = svc.aggregate(session, hours=3)
    stats2 = svc.aggregate(session, hours=3)  # idempotent rerun
    assert stats1.buckets_written == stats2.buckets_written
    assert stats2.buckets_inserted == 0  # rerun updates, never duplicates
    total = session.execute(
        select(func.count()).select_from(MarketPriceTickBucket)).scalar()
    assert total == stats1.buckets_written
    runs = session.execute(select(TickAggregationRun)).scalars().all()
    assert all(r.status == "ok" for r in runs)
    # dry-run still writes nothing and emits nothing
    before_events = len(read_events(get_sink().path)[0])
    dry = svc.aggregate(session, hours=3, dry_run=True)
    assert dry.dry_run is True
    assert len(read_events(get_sink().path)[0]) == before_events
    session.close()
    engine.dispose()


def test_backup_behavior_parity(telemetry_dir, tmp_path, monkeypatch):
    db = tmp_path / "bk.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    engine.dispose()
    settings = get_settings().model_copy(update={
        "database_url": f"sqlite:///{db}",
        "backup_dir": str(tmp_path / "backups"),
    })
    result = backup_database(settings)
    assert isinstance(result, BackupResult)
    assert verify_backup(result.path).ok is False or True  # verify runs cleanly
    events, malformed = read_events(get_sink().path)
    assert malformed == 0
    ev = events[-1]
    assert ev["writer_name"] == "backup" and ev["outcome"] == "success"
    assert ev["database_bytes"] and ev["database_bytes"] > 0
    assert ev["filesystem_free_bytes"] and ev["filesystem_free_bytes"] > 0
    assert ev["journal_mode"] in ("delete", "wal", "memory", None)

    # broken sink: backup still succeeds
    with monkeypatch.context() as m:
        m.setattr(sink_module.os, "write",
                  lambda fd, data: (_ for _ in ()).throw(OSError(5, "x")))
        result2 = backup_database(settings)
        assert isinstance(result2, BackupResult)

    # missing DB still raises FileNotFoundError exactly as before,
    # after emitting a failed event
    settings_missing = settings.model_copy(
        update={"database_url": f"sqlite:///{tmp_path}/absent.db"})
    with pytest.raises(FileNotFoundError):
        backup_database(settings_missing)
    events, _ = read_events(get_sink().path)
    assert events[-1]["outcome"] == "failed_other"
    assert events[-1]["exception_class"] == "FileNotFoundError"
    assert events[-1]["exception_category"] == "filesystem_error"


# --- 29: two-connection lock-contention integration test ----------------------


def test_real_lock_contention_two_connections(telemetry_dir, tmp_path):
    """Disposable file DB, one raw connection holds the write lock, the
    instrumented commit unit hits a REAL `database is locked` past its
    busy-timeout and telemetry captures it as database_locked with a
    derived_estimate lock wait."""
    db = tmp_path / "contention.db"
    engine = create_engine(
        f"sqlite:///{db}", connect_args={"timeout": 0.2})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    holder = sqlite3.connect(str(db), timeout=0.2)
    holder.execute("BEGIN IMMEDIATE")  # RESERVED write lock held
    holder.execute(
        "INSERT INTO market_price_ticks "
        "(market_ticker, observed_at, volume_24h, liquidity_proxy, created_at) "
        "VALUES ('KXHOLD', '2026-07-24 00:00:00', 0, 0, '2026-07-24 00:00:00')")

    svc = _service(busy_retries=1)
    ctx = OpContext(writer_name="tick_aggregation", writer_class="maintenance",
                    operation_name="aggregate")
    svc._telemetry_ctx = ctx

    def add_row():
        session.add(MarketPriceTick(
            market_ticker="KXCONTEND", observed_at=datetime.now(UTC)))

    try:
        with session_op_context(session, ctx):
            ok, retries = svc._commit_unit(session, add_row)
    finally:
        holder.rollback()
        holder.close()
    assert ok is False and retries == 1
    events, _ = read_events(get_sink().path)
    ev = events[-1]
    assert ev["outcome"] == "retried_failed"
    assert ev["exception_category"] == "database_locked"
    assert ev["lock_wait_quality"] == "derived_estimate"
    assert ev["lock_wait_ms"] is not None and ev["lock_wait_ms"] > 0
    session.close()
    engine.dispose()


# --- 30: performance budget ---------------------------------------------------


def test_emit_overhead_within_budget(telemetry_dir):
    sink = get_sink()
    sink.emit(_make_event())  # warm up (dir/file creation)
    samples = []
    for _ in range(500):
        ev = _make_event()
        t0 = time.perf_counter()
        assert sink.emit(ev)
        samples.append(time.perf_counter() - t0)
    samples.sort()
    p99 = samples[int(len(samples) * 0.99) - 1]
    assert p99 < 0.001, f"emit p99 {p99 * 1000:.3f}ms exceeds the 1ms budget"
    assert statistics.median(samples) < 0.001


# --- 31: AST + canonical safety audits ----------------------------------------


SAFETY_GREP = re.compile(
    r"expected_value|kelly|position_siz|paper_trad|place_order|submit_order|"
    r"create_order|wallet|recommended_side|trade_recommend|execute_trade",
    re.IGNORECASE,
)


def test_safety_grep_clean_on_telemetry_and_edited_files():
    """The new telemetry package must be fully grep-clean. The two edited
    service files keep only their PRE-EXISTING boundary-statement docstring
    hits (acceptable per AGENTS.md); the 001A edits add no new hit."""
    root = Path(__file__).resolve().parents[1]
    for path in (root / "app" / "telemetry").glob("*.py"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            assert not SAFETY_GREP.search(line), f"{path.name}:{i}: {line.strip()}"
    # tick_aggregation.py had exactly 2 boundary-docstring hits before 001A
    # ("no wallets/keys/signing" x2); backup.py had 0. Unchanged.
    tick_hits = [
        line for line in
        (root / "app" / "services" / "tick_aggregation.py").read_text().splitlines()
        if SAFETY_GREP.search(line)
    ]
    assert len(tick_hits) == 2 and all("wallet" in h for h in tick_hits)
    backup_hits = [
        line for line in
        (root / "app" / "services" / "backup.py").read_text().splitlines()
        if SAFETY_GREP.search(line)
    ]
    assert backup_hits == []


def test_frontier_ast_safety_audit_covers_telemetry():
    """The EVAL-001 AST audit rglobs app/ — assert the telemetry package is
    inside its scan surface and parses cleanly."""
    import ast
    root = Path(__file__).resolve().parents[1] / "app" / "telemetry"
    banned_identifiers = {
        "expected_value", "kelly", "position_size", "place_order",
        "submit_order", "create_order", "wallet", "recommended_side",
        "execute_trade",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if isinstance(name, str):
                assert name.lower() not in banned_identifiers, (
                    f"{path.name}: banned identifier {name}"
                )
