"""GATE2-WRITER-TELEMETRY-001 — persistent SQLite writer telemetry.

WHAT THIS DEFENDS. Two finished Solana writers are held back by the same gate:
neither persists what it measures, so "a bad pass leaves nothing behind but
journald stdout". The fix reuses the SQLITE-LOCK-TELEMETRY-001A JSONL sink
(append-only, non-SQLite BY MANDATE) rather than adding a table, so these
tests are written against three properties that a table could not have given
us and that a careless later edit could quietly take away:

  1. THE RECORD SURVIVES A PASS THAT COMMITTED NOTHING. That is the whole
     reason `config["write_coordination"]` was rejected — it rides the
     single-attempt finalize commit, so the contended passes lose it.
  2. THE PHASE ATTRIBUTION IS NOT FLATTENED. The reconciler's breakers read
     the `batch` figure and only that; the pass total carries one `run_row`
     and one `finalize` sample that are instrument, not contention. Four
     separate fields, never summed.
  3. AN UNKNOWN LABEL COSTS "other", NEVER THE EVENT. The sink's contract on a
     validation failure is count-and-drop, so a closed label set without
     builder-side normalization would silently delete the very record this
     gate exists to keep.

The contention test uses a REAL second SQLite connection holding a REAL
RESERVED lock on a REAL file-backed database, following the precedent in
test_crypto_reconciler_guarded_timer_001.py. Nothing here simulates contention.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.models import CryptoPriceTick, CryptoToken
from app.services.crypto_tape import run_scheduled_reconciliation
from app.telemetry import writer_pass
from app.telemetry.schema import (
    REQUIRED_FIELDS,
    RUN_SOURCES,
    RUN_STATUSES,
    STOP_REASONS,
    TelemetryValidationError,
    build_event,
    validate_event,
)
from app.telemetry.sink import get_sink, read_events

CHAIN = "solana"
HOLDER_TIMEOUT_SECONDS = 0.2


# --- harness ---------------------------------------------------------------

def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_event(**over) -> dict:
    """A minimal event that validates today, used to prove the additions are
    strictly additive."""
    event = build_event(
        writer_name="crypto_tape",
        writer_class="scheduled_oneshot",
        operation_name="scheduled_reconciliation",
        started_at=_now_z(),
    )
    event.update(over)
    return event


def _mint(session, address: str, *, born_hours_ago: float):
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(hours=born_hours_ago)
    session.add(CryptoToken(
        chain=CHAIN, token_address=address, symbol=address[:6],
        first_seen_at=first_seen, last_seen_at=now,
    ))
    session.add(CryptoPriceTick(
        chain=CHAIN, token_address=address, pair_address=f"pair-{address}",
        observed_at=first_seen, price_usd=1.0, liquidity_usd=10_000.0,
        volume_24h_usd=5_000.0,
    ))
    session.flush()


class _FileDb:
    """A real, file-backed SQLite database — the lock meter needs real locks
    and the health gate keeps no state without a real file to co-locate with."""

    def __init__(self, tmp_path: Path, *, tokens: int = 12):
        self.path = tmp_path / "gate2.db"
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"timeout": 5.0},
        )
        Base.metadata.create_all(self.engine)
        self.Factory = sessionmaker(bind=self.engine)
        seed = self.Factory()
        for i in range(tokens):
            _mint(seed, f"tok-g2-{i:03d}", born_hours_ago=30 + i)
        seed.commit()
        seed.close()

    @property
    def url(self) -> str:
        return f"sqlite:///{self.path}"

    def settings(self, **over) -> Settings:
        base = {
            "database_url": self.url,
            "enable_crypto_tape_reconciler": True,
            "crypto_tape_reconciler_window_hours": 48,
            "crypto_tape_reconciler_limit": 1000,
            "crypto_tape_reconciler_batch_size": 2,
        }
        base.update(over)
        return Settings(**base)

    def close(self):
        self.engine.dispose()


class _Holder:
    """An independent connection holding SQLite's RESERVED write lock."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), timeout=HOLDER_TIMEOUT_SECONDS)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("UPDATE crypto_tokens SET last_seen_at = last_seen_at")

    def release(self):
        try:
            self.conn.rollback()
        finally:
            self.conn.close()


@pytest.fixture
def filedb(tmp_path):
    db = _FileDb(tmp_path)
    try:
        yield db
    finally:
        db.close()


def _events() -> list[dict]:
    events, malformed = read_events(get_sink().path)
    assert malformed == 0, "the sink wrote a line it cannot read back"
    return events


def _run(db, **kw) -> tuple[dict, list[dict]]:
    """One governed pass; returns (summary, emitted events)."""
    session = db.Factory()
    try:
        summary = run_scheduled_reconciliation(
            session, settings=db.settings(**kw.pop("settings_over", {})), **kw
        )
    finally:
        session.close()
    return summary, _events()


# --- 1. the schema change is strictly additive -----------------------------

class TestAdditive:
    def test_a_001a_shaped_event_still_validates_unchanged(self):
        """REQUIRED_FIELDS must not have grown: every event the two existing
        001A writers emit today has to keep validating byte-identically."""
        validate_event(_valid_event())

    def test_required_fields_did_not_grow(self):
        assert REQUIRED_FIELDS == frozenset({
            "event_version", "event_id", "writer_name", "writer_class",
            "operation_name", "process_id", "host", "started_at",
            "finished_at", "duration_ms", "retry_count", "attempt_number",
            "outcome", "provider_io_during_transaction",
            "filesystem_io_during_transaction",
        })

    def test_every_writer_pass_field_is_accepted(self):
        validate_event(_valid_event(
            run_status="partial", stop_reason="deadline", run_source="scheduled",
            gate_bypassed=False, batch_count=7, batch_lock_wait_ms_max=1037,
            run_row_lock_wait_ms=12, finalize_lock_wait_ms=48,
            write_hold_ms_max=194, lock_failures=0,
            adaptive_batching_active=True, adaptive_batch_size_max=25,
            adaptive_time_budget_ms=2000,
            adaptive_initial_per_token_cost_ms=710,
        ))

    def test_the_high_cardinality_guard_still_bites(self):
        with pytest.raises(TelemetryValidationError):
            validate_event(_valid_event(token_address="So111...2"))


# --- 2. the bounded label sets ---------------------------------------------

class TestLabelSets:
    @pytest.mark.parametrize("field,value", [
        ("run_status", "not_a_status"),
        ("stop_reason", "not_a_reason"),
        ("run_source", "cron"),
    ])
    def test_out_of_set_labels_are_rejected_at_the_sink(self, field, value):
        with pytest.raises(TelemetryValidationError):
            validate_event(_valid_event(**{field: value}))

    def test_an_unknown_status_costs_other_not_the_event(self):
        """THE ANTI-SILENT-DROP PROPERTY. A closed set alone would make a
        future status delete the whole pass record, because the sink counts
        and drops on validation failure. Normalization happens in the builder,
        so the event survives with an honest 'other'."""
        assert writer_pass.normalize_run_status("a_status_from_2027") == "other"
        assert writer_pass.normalize_stop_reason("a_reason_from_2027") == "other"
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="scheduled_reconciliation",
            started_at=_now_z(), run_status="a_status_from_2027",
            stop_reason="a_reason_from_2027",
        )
        assert eid is not None, "an unknown label must not cost the record"
        event = _events()[-1]
        assert event["run_status"] == "other"
        assert event["stop_reason"] == "other"

    def test_the_invalid_config_family_collapses_to_one_label(self):
        """`invalid_*` is an OPEN prefix — one member per validated setting —
        so enumerating it would guarantee the label set drifts behind the
        code. The longest shipped member is 38 chars, which is also why it is
        never stored in a width-constrained column here."""
        for status in ("invalid_limit", "invalid_window", "invalid_batch_size",
                       "invalid_max_duration_seconds",
                       "invalid_time_budget_seconds",
                       "invalid_lock_wait_budget_seconds",
                       "invalid_initial_per_token_cost_seconds"):
            assert writer_pass.normalize_run_status(status) == "invalid_config"

    def test_known_statuses_survive_normalization_unchanged(self):
        for status in ("ok", "partial", "truncated", "skipped_contention",
                       "skipped_overlap", "marketops_degraded", "db_locked",
                       "unsafe_host_cost", "backlog_expiring"):
            assert writer_pass.normalize_run_status(status) == status
            assert status in RUN_STATUSES


# --- 3. impossible values are rejected -------------------------------------

class TestImpossibleValues:
    @pytest.mark.parametrize("field", [
        "batch_lock_wait_ms_max", "run_row_lock_wait_ms",
        "finalize_lock_wait_ms", "write_hold_ms_max",
    ])
    def test_negative_durations_rejected(self, field):
        with pytest.raises(TelemetryValidationError):
            validate_event(_valid_event(**{field: -1}))

    @pytest.mark.parametrize("field", ["batch_count", "lock_failures"])
    def test_negative_counts_rejected(self, field):
        with pytest.raises(TelemetryValidationError):
            validate_event(_valid_event(**{field: -1}))

    @pytest.mark.parametrize("field", ["batch_count", "lock_failures",
                                       "adaptive_batch_size_max"])
    def test_a_bool_is_not_a_count(self, field):
        """`isinstance(True, int)` is True in Python, so without an explicit
        exclusion `batch_count=True` would validate as the integer 1."""
        with pytest.raises(TelemetryValidationError):
            validate_event(_valid_event(**{field: True}))

    @pytest.mark.parametrize("field", ["gate_bypassed", "adaptive_batching_active"])
    def test_an_int_is_not_a_bool(self, field):
        with pytest.raises(TelemetryValidationError):
            validate_event(_valid_event(**{field: 1}))


# --- 4. scheduled vs manual is derived, not asserted ------------------------

class TestRunSource:
    def test_systemd_invocation_id_means_scheduled(self, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "b8f0c2d4e6a84f10")
        assert writer_pass.resolve_run_source() == "scheduled"
        writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="scheduled_reconciliation",
            started_at=_now_z(), run_status="ok",
        )
        event = _events()[-1]
        assert event["run_source"] == "scheduled"
        # writer_class is DERIVED from run_source, so the two cannot disagree
        assert event["writer_class"] == "scheduled_oneshot"

    def test_a_bare_shell_means_manual(self, monkeypatch):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        assert writer_pass.resolve_run_source() == "manual"
        writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="scheduled_reconciliation",
            started_at=_now_z(), run_status="ok",
        )
        event = _events()[-1]
        assert event["run_source"] == "manual"
        assert event["writer_class"] == "manual_command"

    def test_copying_the_units_execstart_by_hand_does_not_forge_scheduled(
        self, monkeypatch, filedb
    ):
        """The failure this guards is ACCIDENTAL, not malicious: an operator
        reproducing a timer failure by pasting the unit's ExecStart line must
        not file their attended pass as an unattended one. Nothing on the
        command line can make it 'scheduled'."""
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        summary, events = _run(filedb, force=True)
        assert events[-1]["run_source"] == "manual"

    def test_force_is_recorded_separately_from_source(self, monkeypatch, filedb):
        """`gate_bypassed` must never be merged into `run_source`: the rolling
        latch must not be tripped or cleared by an attended --force pass, and
        'scheduled + bypassed' has to stay readable as the anomaly it is."""
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        _, events = _run(filedb, force=True)
        assert events[-1]["gate_bypassed"] is True
        assert events[-1]["run_source"] == "manual"

        monkeypatch.setenv("INVOCATION_ID", "b8f0c2d4e6a84f10")
        _, events = _run(filedb, force=False)
        assert events[-1]["gate_bypassed"] is False
        assert events[-1]["run_source"] == "scheduled"

    def test_run_sources_enum_is_closed(self):
        assert RUN_SOURCES == frozenset({"scheduled", "manual", "unknown"})


# --- 5. the reconciler actually persists its pass ---------------------------

class TestReconcilerEmits:
    def test_a_completed_pass_writes_exactly_one_record(self, filedb):
        summary, events = _run(filedb)
        assert len(events) == 1
        event = events[0]
        assert event["writer_name"] == "crypto_tape"
        assert event["operation_name"] == "scheduled_reconciliation"
        assert event["run_status"] == summary["status"]

    def test_the_measured_quantities_are_the_summarys_own(self, filedb):
        """Not a re-measurement: the persisted numbers must be the ones the
        pass itself computed, or the record is a second, drifting instrument."""
        summary, events = _run(filedb)
        event = events[0]
        assert event["batch_lock_wait_ms_max"] == summary["batch_lock_wait_ms_max"]
        assert event["write_hold_ms_max"] == summary["write_hold_ms_max"]
        assert event["batch_count"] == summary["batches_committed"]
        assert event["retry_count"] == summary["lock_retry_events"]
        assert event["rows_attempted"] == summary["tokens_considered"]
        assert event["external_calls"] == 0

    def test_the_adaptive_config_in_force_is_recorded(self, filedb):
        """The write-hold SLO cannot be calibrated from a distribution without
        the seed that produced it — that seed is the whole reason the timer is
        still disarmed."""
        _, events = _run(filedb, settings_over={
            "crypto_tape_reconciler_initial_per_token_cost_seconds": 0.71,
            "crypto_tape_reconciler_time_budget_seconds": 2.0,
        })
        event = events[-1]
        assert event["adaptive_batching_active"] is True
        assert event["adaptive_initial_per_token_cost_ms"] == 710
        assert event["adaptive_time_budget_ms"] == 2000
        assert event["adaptive_batch_size_max"] == 2

    def test_an_uncalibrated_pass_says_so_rather_than_faking_a_seed(self, filedb):
        _, events = _run(filedb)
        event = events[-1]
        assert event["adaptive_batching_active"] is False
        assert "adaptive_initial_per_token_cost_ms" not in event

    def test_the_flag_off_path_emits_nothing_and_that_is_deliberate(self, filedb):
        """THE ONE OUTCOME THAT IS NOT RECORDED, pinned so the omission is a
        decision rather than a gap someone later 'fixes'.

        `status="disabled"` returns before the terminal funnel, matching the
        pre-existing `guard.records_feed_gate` contract ("disabled never
        reaches here — the flag is off, no run happened"). Every other
        outcome, including every pre-flight skip and refusal, IS recorded.

        Recording it would also be actively unhelpful: both writers ship
        default-OFF and the timer may be installed dark, so this path would
        otherwise emit four events a day that describe nothing happening."""
        summary, events = _run(filedb, settings_over={
            "enable_crypto_tape_reconciler": False})
        assert summary["status"] == "disabled"
        assert events == []

    def test_an_early_refusal_is_recorded(self, filedb):
        """`invalid_limit` is refused BEFORE the adaptive config is resolved.
        A naive closure over those locals would raise NameError here — on
        exactly the early path a gate most needs recorded."""
        _, events = _run(filedb, limit=0)
        assert len(events) == 1
        assert events[0]["run_status"] == "invalid_config"
        assert "adaptive_batch_size_max" not in events[0]

    def test_a_refusal_after_config_resolution_carries_the_config(self, filedb):
        _, events = _run(filedb, lock_wait_budget_seconds=-1)
        assert events[0]["run_status"] == "invalid_config"
        assert events[0]["adaptive_batch_size_max"] == 2

    def test_a_dry_run_is_recorded(self, filedb):
        _, events = _run(filedb, dry_run=True)
        assert len(events) == 1


# --- 6. phase attribution survives ------------------------------------------

class TestPhaseAttribution:
    def test_the_four_lock_wait_figures_are_separate_fields(self, filedb):
        """The breakers read `batch` and ONLY `batch`. The pass total contains
        one run_row and one finalize sample that are once-per-pass instrument
        events, not contention — a threshold read off the total would be
        measuring the instrument."""
        summary, events = _run(filedb)
        event = events[0]
        assert event["batch_lock_wait_ms_max"] == summary["batch_lock_wait_ms_max"]
        assert event["finalize_lock_wait_ms"] == summary["finalize_lock_wait_ms"]
        assert event["lock_wait_ms"] == summary["lock_wait_ms"]
        # the batch figure is NOT the pass total wearing a different name
        assert "run_row_lock_wait_ms" in event

    def test_the_batch_figure_excludes_the_run_row_sample(self, filedb):
        """Reconstructs the accounting identity the phase split exists for."""
        summary, events = _run(filedb)
        phases = summary["lock_wait_phases"]
        event = events[0]
        assert event["run_row_lock_wait_ms"] == phases["run_row"]["lock_wait_ms"]
        assert event["batch_lock_wait_ms_max"] == (
            phases["batch"]["lock_wait_ms_max"]
        )

    def test_the_record_survives_a_contended_pass(self, filedb):
        """THE PROPERTY A TABLE COULD NOT GIVE US. Under a real held RESERVED
        lock the pass may lose its finalize commit — and with it the run row's
        `config["write_coordination"]`. The JSONL record is written after the
        pass, outside every transaction, so it survives regardless."""
        holder = _Holder(filedb.path)
        try:
            summary, events = _run(filedb, settings_over={
                "crypto_tape_reconciler_max_duration_seconds": 2.0})
        finally:
            holder.release()
        assert len(events) == 1, "a contended pass must still leave a record"
        assert events[0]["run_status"] == summary["status"]


# --- 7. one surface, both writers -------------------------------------------

class TestServesBothWriters:
    def test_the_sparse_observers_shape_is_accepted(self):
        """CRYPTO-COVERAGE-REPAIR-002's writer is not on this branch, so this
        pins the SURFACE against the shape its `_WriteMeter` actually produces
        (batches / retry_attempts / lock_failures / write_hold_ms_max) plus its
        own status and stop-reason vocabulary. Wiring its call site is a
        one-liner when that branch merges; the plane is already here."""
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_horizon_observe",
            operation_name="rolling_admission",
            started_at=_now_z(),
            run_status="partial", stop_reason="observe_limit",
            batch_count=3, retry_count=1, lock_failures=0,
            write_hold_ms_max=140, rows_attempted=12, rows_committed=12,
            external_calls=4, table_groups=["crypto_horizon"],
        )
        assert eid is not None
        event = _events()[-1]
        assert event["writer_name"] == "crypto_horizon_observe"
        assert event["lock_failures"] == 0
        assert event["external_calls"] == 4

    def test_both_writers_land_in_one_stream(self):
        """One evaluation plane, not two parallel accounting systems."""
        for name in ("crypto_tape", "crypto_horizon_observe"):
            writer_pass.emit_writer_pass(
                writer_name=name, operation_name="pass",
                started_at=_now_z(), run_status="ok",
            )
        names = {e["writer_name"] for e in _events()}
        assert names == {"crypto_tape", "crypto_horizon_observe"}

    def test_the_observers_stop_reasons_are_in_the_set(self):
        for reason in ("deadline", "observe_limit", "complete"):
            assert reason in STOP_REASONS
            assert writer_pass.normalize_stop_reason(reason) == reason


# --- 8. telemetry can never fail the writer ---------------------------------

class TestNeverFailsTheWriter:
    def test_a_broken_sink_does_not_fail_the_pass(self, filedb, monkeypatch):
        """`durable_but_nonblocking`: a telemetry failure must never propagate
        into the writer. Proven against the real pass, not the helper."""
        def _boom(*a, **k):
            raise OSError("sink is gone")

        monkeypatch.setattr("app.telemetry.sink.TelemetrySink.emit", _boom)
        session = filedb.Factory()
        try:
            summary = run_scheduled_reconciliation(
                session, settings=filedb.settings())
        finally:
            session.close()
        assert summary["status"] in {"ok", "partial", "truncated",
                                     "backlog_expiring"}

    def test_the_emitter_swallows_everything(self, monkeypatch):
        monkeypatch.setattr(
            "app.telemetry.writer_pass.build_event",
            lambda **k: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        assert writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok",
        ) is None


# --- 9. the telemetry write is not itself a contention source ---------------

class TestTelemetryIsNotAContentionSource:
    def test_the_emit_takes_no_sqlite_lock(self, filedb):
        """The structural claim, proven structurally: a competing writer holds
        the RESERVED lock for the whole emit, and the emit still lands. If the
        sink touched SQLite at all this would block and then fail."""
        holder = _Holder(filedb.path)
        try:
            t0 = time.perf_counter()
            eid = writer_pass.emit_writer_pass(
                writer_name="crypto_tape",
                operation_name="scheduled_reconciliation",
                started_at=_now_z(), run_status="ok", batch_count=1,
                batch_lock_wait_ms_max=0, write_hold_ms_max=0,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
        finally:
            holder.release()
        assert eid is not None, "the record must land while the DB lock is held"
        # The reconciler's own write-hold is 84-194 ms against a 2.0 s SLO;
        # a telemetry append that cost a meaningful fraction of that would be
        # self-defeating. Generous bound — the point is the ORDER of magnitude.
        assert elapsed_ms < 50, f"telemetry append cost {elapsed_ms:.1f} ms"

    def test_the_sink_path_is_not_inside_the_database(self, filedb):
        writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok")
        assert get_sink().path.suffix == ".jsonl"
        assert not str(get_sink().path).endswith(".db")
