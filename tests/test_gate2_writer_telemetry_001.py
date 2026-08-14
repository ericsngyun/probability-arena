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
from app.services import crypto_reconciler_guard as guard
from app.services.crypto_tape import (
    LockWaitAccounting,
    run_scheduled_reconciliation,
)
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
from app.telemetry.sink import TelemetrySink, get_sink, read_events

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


class _HoldMeter:
    """The minimum surface `LockWaitAccounting.record` reads off a meter.

    Deliberately hand-built rather than driving a real transaction: the point
    of A1 is the SUB-MILLISECOND hold, and a real one cannot be produced on
    demand. `record` duck-types the phase via `getattr`, so this is the same
    path a real `LockWaitMeter` takes."""

    def __init__(self, *, hold_seconds: float, lock_wait_seconds: float = 0.0,
                 phase: str = "batch"):
        self.hold_seconds = hold_seconds
        self.lock_wait_seconds = lock_wait_seconds
        self.phase = phase


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


# --- 10. A1: "not measured" and "sub-millisecond" are distinguishable --------

class TestWriteHoldIsNotZeroInflated:
    """THE SURVIVORSHIP BIAS, ARRIVING BY A SECOND ROUTE.

    Rejecting `crypto_token_lifecycle_runs` removed the ABSENCE bias: the
    passes that lose their record are the contended ones, so a store blind to
    them cannot calibrate a threshold. `write_hold_ms_max=0` re-introduced the
    same bias as ZERO-INFLATION — a contended pass, a refusal and a pre-flight
    skip all persist 0 while having measured nothing at all, and
    `int(hold_seconds * 1000)` truncates a genuine 0.4 ms hold to 0 as well.

    An operator averaging the field to derive `initial_per_token_cost_seconds`
    would pull the mean DOWN with passes that measured nothing and set the
    constant too AGGRESSIVE — larger batches, longer holds, on a live
    production writer. These tests pin the disambiguation at both ends: the
    accounting counts holds, and the emitter refuses to persist a hold figure
    it did not measure."""

    def test_a_sub_millisecond_hold_is_counted_even_though_it_truncates_to_zero(
        self,
    ):
        """The truncation itself, at the source. `write_hold_ms_max` is 0 and
        stays 0 — it is shipped schema that run rows and the CLI read — but
        `write_hold_measurements` is 1, which is the whole difference between
        "held the lock briefly" and "never opened a write transaction"."""
        acc = LockWaitAccounting()
        acc.record(_HoldMeter(hold_seconds=0.0004))
        summary = acc.as_summary()
        assert summary["write_hold_ms_max"] == 0, "0.4 ms must truncate to 0"
        assert summary["write_hold_measurements"] == 1

    def test_a_pass_that_opened_no_write_transaction_counts_no_holds(self):
        acc = LockWaitAccounting()
        acc.record(_HoldMeter(hold_seconds=0.0))
        summary = acc.as_summary()
        assert summary["write_hold_ms_max"] == 0
        assert summary["write_hold_measurements"] == 0, (
            "a sample with no hold must not be counted as a hold measurement"
        )

    def test_a_completed_pass_marks_its_hold_as_measured(self, filedb):
        summary, events = _run(filedb)
        event = events[0]
        assert summary["write_hold_measurements"] > 0
        assert event["write_hold_measured"] is True
        assert event["write_hold_ms_max"] == summary["write_hold_ms_max"]

    def test_a_refused_pass_omits_the_hold_rather_than_reporting_zero(
        self, filedb
    ):
        """THE CALIBRATION-POISONING CASE. A refusal measured nothing. If it
        persisted `write_hold_ms_max=0` an averaging query could not tell it
        from a real zero, so the field is ABSENT and `write_hold_measured` is
        an explicit false."""
        _, events = _run(filedb, limit=0)
        event = events[0]
        assert event["run_status"] == "invalid_config"
        assert event["write_hold_measured"] is False
        assert "write_hold_ms_max" not in event, (
            "an unmeasured pass must not contribute a zero to the mean"
        )

    def test_the_calibration_filter_excludes_every_unmeasured_pass(self, filedb):
        """The filter the runbook tells an operator to apply, executed. Mixing
        a refusal into the corpus is exactly what biases the constant."""
        _run(filedb, limit=0)              # refusal: measured nothing
        _run(filedb)                       # a real pass
        events = _events()
        assert len(events) == 2
        eligible = [
            e for e in events
            if e.get("run_status") in {"ok", "partial", "truncated",
                                       "backlog_expiring"}
            and (e.get("batch_count") or 0) > 0
            and e.get("write_hold_measured") is True
        ]
        assert len(eligible) == 1
        assert all("write_hold_ms_max" in e for e in eligible)


# --- 11. A2: the calibration quantities are PERSISTED, not merely computed ---

class TestCalibrationQuantitiesSurvive:
    def test_the_slo_counter_this_gate_exists_for_is_carried(self, filedb):
        """`write_hold_slo_violations` is the number the controller Gate 3
        builds is supposed to act on. `as_summary` computed it and the emit
        threw it away."""
        summary, events = _run(filedb)
        assert events[0]["write_hold_slo_violations"] == (
            summary["write_hold_slo_violations"]
        )

    def test_the_lock_wait_denominator_is_carried(self, filedb):
        """Every average over the lock-wait scalars needs it, and A1's
        disambiguation needs it too."""
        summary, events = _run(filedb)
        assert events[0]["lock_wait_measurements"] == (
            summary["lock_wait_measurements"]
        )

    def test_the_breaker_tallies_are_carried(self, filedb):
        summary, events = _run(filedb)
        assert events[0]["batch_lock_wait_warnings"] == (
            summary["batch_lock_wait_warnings"]
        )
        assert events[0]["batch_lock_wait_aborts"] == (
            summary["batch_lock_wait_aborts"]
        )

    def test_the_histogram_survives_the_pass(self, filedb):
        """`LockWaitAccounting`'s own docstring: a threshold must be derived
        from the tail of `lock_wait_histogram_ms`, NEVER from a scalar, because
        the per-attempt measurement bias lands almost entirely in the `1-10`
        bucket. A persisted surface that keeps only scalars therefore forces
        the gate to be calibrated from the one source its author warned
        against."""
        summary, events = _run(filedb)
        assert events[0]["lock_wait_histogram_ms"] == (
            summary["lock_wait_histogram_ms"]
        )
        assert sum(events[0]["lock_wait_histogram_ms"].values()) == (
            summary["lock_wait_measurements"]
        )

    def test_the_histogram_stays_a_bounded_bucket_map(self):
        """The one nested field. It must not become a route around the
        high-cardinality guard — a token id or ticker as a key is rejected."""
        for bad in (
            {"tok-abc123": 1},                       # a key that is not a bucket
            {"1-10": -1},                            # an impossible count
            {"1-10": True},                          # a bool is not a count
            {"1-10": 1.5},                           # a float is not a count
            {str(i): 0 for i in range(20)},          # unbounded bucket count
            ["1-10"],                                # not a map at all
        ):
            with pytest.raises(TelemetryValidationError):
                validate_event(_valid_event(lock_wait_histogram_ms=bad))
        validate_event(_valid_event(lock_wait_histogram_ms={
            "<1": 0, "1-10": 8, ">=30000": 0,
        }))

    def test_rows_committed_is_a_row_sum_across_three_tables(self, filedb):
        """NOT a committed-TOKEN count, which is what Gate 3 asks for. One
        token can produce a snapshot AND an outcome update AND a birth event,
        so this denominator is >= the token count and a per-token cost derived
        from it UNDER-estimates cost — which, inverted into a batch size, errs
        toward smaller batches. Safe, but only while it is not mistaken for a
        token count. Pinned so the arithmetic cannot drift silently."""
        summary, events = _run(filedb)
        event = events[0]
        assert event["rows_committed"] == (
            summary["snapshots_created"]
            + summary["outcomes_updated"]
            + summary["birth_events_created"]
        )
        assert event["rows_attempted"] == summary["tokens_considered"]
        assert event["rows_committed"] > event["rows_attempted"], (
            "the mismatch is the point: rows are not tokens"
        )


# --- 12. A3: the sink degrades instead of deleting ---------------------------

class TestDegradeRatherThanDelete:
    """FIVE REACHABLE CLASSES GAVE `rejected=1, emitted=0` THROUGH THE
    SANCTIONED ENTRY POINT — i.e. the sink silently deleted a whole pass
    record, which is precisely the failure this gate exists to end.

    Builder-side normalization is necessary but NOT SUFFICIENT: it closes the
    label doors (`outcome`, `table_groups`, unknown `**fields` keys) and
    cannot close the type doors, because a value can be the wrong type in ANY
    field. So both halves ship: the builder normalizes, and the sink retries
    once with the event stripped to REQUIRED_FIELDS + `truncated=true`."""

    def test_an_unknown_outcome_costs_the_label_not_the_record(self):
        """`outcome` is REQUIRED and enum-checked, and it was the one
        caller-supplied label reaching the envelope UN-normalized — popped out
        of `**fields` and forwarded, unlike `run_status`/`stop_reason`."""
        sink = get_sink()
        before = sink.rejected
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok", outcome="bogus",
        )
        assert eid is not None, "an unknown outcome must not delete the record"
        assert sink.rejected == before, "it must not even reach the validator"
        event = _events()[-1]
        assert event["outcome"] == "unknown"
        assert event["run_status"] == "ok", "the exact status is still carried"

    def test_an_unknown_table_group_costs_the_family_not_the_record(self):
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok",
            table_groups=["crypto_horizon", "not_a_family"],
        )
        assert eid is not None
        event = _events()[-1]
        assert event["table_groups"] == ["crypto_horizon"]

    def test_an_all_unknown_table_group_list_becomes_absent_not_empty(self):
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok", table_groups=["nope"],
        )
        assert eid is not None
        assert "table_groups" not in _events()[-1]

    def test_an_unknown_field_key_is_dropped_not_forwarded(self):
        """WRITER B'S CONCRETE CASE. `crypto_sparse_observation`'s `_WriteMeter`
        produces `commit_ms_max`, which this envelope has no field for. Under
        the old builder it rode `**fields` straight into the validator and took
        the whole record with it."""
        sink = get_sink()
        before = sink.rejected
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_horizon_observe", operation_name="p",
            started_at=_now_z(), run_status="partial",
            batch_count=3, commit_ms_max=17,
        )
        assert eid is not None, "one unknown key must not delete the record"
        assert sink.rejected == before
        event = _events()[-1]
        assert "commit_ms_max" not in event
        assert event["batch_count"] == 3, "the known fields still land"

    def test_a_wrong_typed_field_degrades_the_record_rather_than_deleting_it(
        self,
    ):
        """THE DOOR NORMALIZATION CANNOT CLOSE. A float in a duration field is
        a TYPE error, not a label error, so no builder-side mapping can catch
        it. The sink keeps the pass by shedding the optional fields."""
        sink = get_sink()
        rejected_before, degraded_before = sink.rejected, sink.degraded
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok",
            write_hold_ms_max=12.5,
        )
        assert eid is not None, "a degraded record beats a deleted one"
        assert sink.rejected == rejected_before + 1, "the full event WAS invalid"
        assert sink.degraded == degraded_before + 1
        event = _events()[-1]
        assert event["truncated"] is True, "the degradation is self-disclosed"
        assert "write_hold_ms_max" not in event
        assert REQUIRED_FIELDS <= set(event), "identity and timing survive"

    def test_a_non_bool_gate_bypassed_degrades_rather_than_deleting(self):
        eid = writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="p",
            started_at=_now_z(), run_status="ok", gate_bypassed=1,
        )
        assert eid is not None
        assert _events()[-1]["truncated"] is True

    def test_a_record_whose_identity_is_unusable_is_still_dropped(self):
        """The degradation is ONE retry on a stripped event, not a loosening.
        A required field that is itself invalid cannot be salvaged, and
        pretending otherwise would put an unattributable line on disk."""
        sink = get_sink()
        before = sink.emitted
        assert sink.emit(_valid_event(writer_name="not_a_writer")) is False
        assert sink.emitted == before

    def test_the_validator_itself_is_not_loosened(self):
        """The closed-set guarantee is the reason this surface is safe to keep
        on disk. Degradation must never become "accept it anyway"."""
        for bad in (
            {"lock_wait_ms": 1.5},
            {"batch_count": -1},
            {"gate_bypassed": 1},
            {"run_status": "not_a_status"},
            {"table_groups": ["not_a_family"]},
            {"a_field_nobody_declared": 1},
        ):
            with pytest.raises(TelemetryValidationError):
                validate_event(_valid_event(**bad))

    def test_a_degraded_record_still_carries_no_secret(self):
        """Stripping to REQUIRED_FIELDS must not be a route around the secret
        scan — the stripped event goes through the SAME validator."""
        sink = get_sink()
        before = sink.emitted
        assert sink.emit(_valid_event(
            operation_name="pass api_key=xyz", lock_wait_ms=1.5,
        )) is False
        assert sink.emitted == before


# --- 13. A4: telemetry extraction cannot fail the pass ----------------------

class TestExtractionCannotFailThePass:
    def test_a_non_numeric_measurement_cannot_escape_into_the_writer(
        self, filedb, monkeypatch
    ):
        """PROVED BEFORE THE FIX: `emit_writer_pass` swallows everything, but
        the `int()` coercions that build its arguments run BEFORE it, on the
        writer's return path, unguarded. A non-numeric produced `ValueError:
        invalid literal for int() ... 'n/a'` escaping
        `run_scheduled_reconciliation` entirely — THE PASS FAILING BECAUSE OF
        TELEMETRY.

        No writer-A producer can do this today (every one is an int). It
        becomes reachable the moment writer B is wired, because writer B has a
        different result shape and an unmapped `commit_ms_max`. Simulated at
        the same seam a differing result shape would arrive through."""
        original = LockWaitAccounting.as_summary

        def _poisoned(self):
            summary = original(self)
            summary["write_hold_measurements"] = "n/a"
            return summary

        monkeypatch.setattr(LockWaitAccounting, "as_summary", _poisoned)
        session = filedb.Factory()
        try:
            summary = run_scheduled_reconciliation(
                session, settings=filedb.settings())
        finally:
            session.close()
        assert summary["status"] in {"ok", "partial", "truncated",
                                     "backlog_expiring"}, (
            "telemetry extraction must never decide the pass's outcome"
        )

    def test_the_pass_still_leaves_a_record_when_extraction_fails(
        self, filedb, monkeypatch
    ):
        """Same thesis as the sink's degradation: swallowing the error must
        not also delete the record. The pass HAPPENED and its status is what a
        calibration corpus must not be blind to, so the identity record lands
        without the numbers."""
        original = LockWaitAccounting.as_summary

        def _poisoned(self):
            summary = original(self)
            summary["write_hold_measurements"] = "n/a"
            return summary

        monkeypatch.setattr(LockWaitAccounting, "as_summary", _poisoned)
        session = filedb.Factory()
        try:
            summary = run_scheduled_reconciliation(
                session, settings=filedb.settings())
        finally:
            session.close()
        events = _events()
        assert len(events) == 1
        assert events[0]["run_status"] == summary["status"]
        assert "write_hold_ms_max" not in events[0]


# --- 14. A5: the two filesystem defences that survived mutation -------------

class TestFilesystemDefences:
    """BOTH OF THESE SURVIVED A MUTATION RUN WITH ALL 44 GATE-2 AND ALL 31
    001A TESTS STILL GREEN. The defences work; nothing pinned them, so a later
    edit could delete either one for free."""

    def test_a_symlinked_sink_path_is_refused_not_followed(self, tmp_path):
        """THE ONE FILESYSTEM ROUTE TO THE CORRUPTION TRAP. A symlink planted
        at `~/probability-arena-telemetry/sqlite-writes.jsonl` pointing at the
        live SQLite database would make the telemetry writer append JSON INTO
        THE DATABASE FILE: corruption, and the non-SQLite sink writing to
        SQLite, which is this milestone's one hard mandate. `O_NOFOLLOW` is
        what stops it."""
        directory = tmp_path / "tel"
        directory.mkdir()
        target = tmp_path / "pretend.db"
        payload = b"SQLite format 3\x00-- not to be appended to --\n"
        target.write_bytes(payload)
        (directory / "sqlite-writes.jsonl").symlink_to(target)

        sink = TelemetrySink(directory)
        dropped_before = sink.dropped
        assert sink.emit(_valid_event()) is False, (
            "the emit must FAIL rather than follow the symlink"
        )
        assert sink.dropped == dropped_before + 1
        assert target.read_bytes() == payload, (
            "the symlink target was written to — O_NOFOLLOW is gone"
        )

    def test_the_symlink_refusal_does_not_raise_into_the_writer(self, tmp_path):
        directory = tmp_path / "tel"
        directory.mkdir()
        target = tmp_path / "pretend.db"
        target.write_bytes(b"x")
        (directory / "sqlite-writes.jsonl").symlink_to(target)
        # returns False, never raises — the failure contract still holds
        assert TelemetrySink(directory).emit(_valid_event()) is False

    def test_directory_permissions_are_reenforced_on_a_loose_dir(self, tmp_path):
        """`mkdir(mode=..., exist_ok=True)` applies `mode` ONLY when it
        creates. A telemetry directory that already exists world-readable
        therefore stays world-readable without the explicit `chmod` — and this
        file is the one place a host's write-coordination history accumulates."""
        directory = tmp_path / "tel"
        directory.mkdir(mode=0o777)
        os.chmod(directory, 0o777)
        assert TelemetrySink(directory).emit(_valid_event()) is True
        assert (directory.stat().st_mode & 0o777) == 0o700, (
            "a pre-existing loose telemetry directory was left loose"
        )

    def test_the_file_mode_is_owner_only(self, tmp_path):
        directory = tmp_path / "tel"
        sink = TelemetrySink(directory)
        assert sink.emit(_valid_event()) is True
        assert (sink.path.stat().st_mode & 0o777) == 0o600


# --- 15. A6: the runbook says what an operator must actually do -------------

class TestRunbookIsActionable:
    """A TEXT PIN, DELIBERATELY, because the artifact under test is text. The
    constant `initial_per_token_cost_seconds` is derived by a human following
    this section; a runbook that omits the filter is exactly as dangerous as
    code that omits it, and nothing else in this suite can catch its removal."""

    RUNBOOK = (Path(__file__).resolve().parents[1]
               / "docs" / "EVO_X2_RUNBOOK.md")

    def test_the_calibration_filter_is_stated(self):
        text = self.RUNBOOK.read_text()
        section = text.split(
            "### Where the per-pass record now lands (GATE2-WRITER-TELEMETRY-001)"
        )[1].split("### `TimeoutStartSec`")[0]
        for clause in ("run_status", "batch_count", "write_hold_measured"):
            assert clause in section, f"the filter omits {clause}"
        assert "backlog_expiring" in section

    def test_the_self_contradictory_cli_instruction_is_gone(self):
        """It said "read the fields with `python -m app.cli` — there is no
        report command yet ... so for now this is `jq`", which is an
        instruction to run a command that does not exist."""
        text = self.RUNBOOK.read_text()
        section = text.split(
            "### Where the per-pass record now lands (GATE2-WRITER-TELEMETRY-001)"
        )[1].split("### `TimeoutStartSec`")[0]
        assert "Read the fields with `python -m app.cli`" not in section

    def test_the_growth_and_reading_hazards_are_stated(self):
        text = self.RUNBOOK.read_text()
        section = text.split(
            "### Where the per-pass record now lands (GATE2-WRITER-TELEMETRY-001)"
        )[1].split("### `TimeoutStartSec`")[0]
        assert "does not rotate" in section
        assert "read_events" in section

    def test_the_sigkill_claim_is_corrected_not_repeated(self):
        """`writer_pass.py` claimed the sink survives passes the run row loses
        "including SIGKILLed mid-finalize". It does not: the emit is the LAST
        thing a pass does, so a SIGKILL loses the JSONL line exactly as it
        loses the run row. The genuine advantage is over contended, aborted
        and refused passes."""
        doc = writer_pass.__doc__ or ""
        assert "SIGKILLed mid-finalize" not in doc
        assert "SIGKILL" in doc, "the limitation must be named, not deleted"

    def test_the_emit_is_the_last_thing_the_pass_does(self, filedb, monkeypatch):
        """The BEHAVIOURAL half of the correction above, and the reason the
        SIGKILL claim was false: by the time the append happens the health
        record is already written, so there is no window in which the JSONL
        line exists and the health/run state does not."""
        seen = {}
        original = TelemetrySink.emit

        def _watch(self, event):
            state = guard.load_state(
                guard.health_state_path(filedb.url, CHAIN), CHAIN)
            seen["health_records_at_emit"] = len(state.get("runs") or [])
            return original(self, event)

        monkeypatch.setattr(TelemetrySink, "emit", _watch)
        _run(filedb)
        assert seen, "the sink was never called at all"
        assert seen["health_records_at_emit"] >= 1, (
            "the emit ran before the health record — it is not last"
        )
