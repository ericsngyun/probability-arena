"""GATE7-SPARSE-TELEMETRY-001 — the sparse observer (writer B) on the 001A sink.

WHAT THIS DEFENDS. GATE2-WRITER-TELEMETRY-001 built ONE persistent pass-record
surface for two Solana writers and wired only the reconciler to it. The sparse
observer measured the same quantities, threw them away at process exit, and
carried its own gate in its result payload: `persisted: false`, "install no
timer until it is". This module pins the wiring of the second writer, and the
four things a careless later edit could quietly take away:

  1. THE RECORD SURVIVES A PASS THAT COMMITTED NOTHING. Every typed refusal
     goes through the one terminal funnel, so the passes that never reach a
     durable row — the ones a calibration corpus must not be blind to — still
     leave a record.
  2. "NOT MEASURED" AND "SUB-MILLISECOND" STAY DISTINGUISHABLE. A pass that
     opened no write transaction omits `write_hold_ms_max` (and the mapped
     `commit_ms`) rather than persisting a 0 an average would absorb.
  3. THE PHASE FIELDS ARE NOT FABRICATED. Writer B times no lock WAIT and has
     no run-row or finalize phase, so those four fields are ABSENT. A derived
     stand-in would be a constant dressed as a measurement, in a field that
     feeds a governed stop condition.
  4. TELEMETRY CANNOT ABORT OR DELAY THE OBSERVATION IT MEASURES. Five
     destination failures, an audit hook proving no SQLite is touched on any
     path, and an append measured against a REAL competing writer holding a
     REAL RESERVED lock on a REAL file-backed database.

Nothing here simulates contention and nothing here enables anything: the flag
stays default-off, no timer is installed, and the sink is per-test isolated by
the `_isolate_sqlite_telemetry` autouse fixture in conftest.
"""
from __future__ import annotations

import errno
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import Base
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoPriceTick,
    CryptoTokenBirthEvent,
    MarketOpsRun,
)
from app.services import crypto_sparse_observation as sparse
from app.services.crypto_horizon import MEMBERSHIP_ROLLING, CryptoHorizonService
from app.telemetry import writer_pass
from app.telemetry.schema import (
    REQUIRED_FIELDS,
    RUN_STATUSES,
    STOP_REASONS,
    WRITER_NAMES,
)
from app.telemetry.sink import TelemetrySink, get_sink, read_events

CHAIN = "solana"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
HOLDER_TIMEOUT_SECONDS = 0.2


# --- harness ---------------------------------------------------------------


class FakeAdapter:
    """Canned pairs per token; never touches the network."""

    source_name = "dexscreener"

    def __init__(self, pairs_by_token=None):
        self.pairs_by_token = pairs_by_token or {}
        self.calls = 0

    async def fetch_pairs_for_token(self, token_address):
        self.calls += 1
        return list(self.pairs_by_token.get(token_address, []))


def token_id(n: int) -> str:
    return f"So{n:04d}" + "T" * 34


def pair(token, *, price=0.001, liq=10_000.0, address="PairA"):
    from app.adapters.dexscreener import PairData

    return PairData(
        chain=CHAIN, pair_address=address, base_token_address=token,
        base_token_symbol="TKN",
        quote_token_address="So11111111111111111111111111111111111111112",
        dex_id="raydium", price_usd=price, liquidity_usd=liq,
        volume_5m_usd=10.0, volume_1h_usd=100.0, volume_24h_usd=1000.0,
        market_cap=50_000.0, fdv=50_000.0,
        raw={"liquidity": {"usd": liq}, "txns": {"m5": {"buys": 3, "sells": 1}}},
    )


def add_birth(session, n=1, *, anchor, chain=CHAIN):
    birth = CryptoTokenBirthEvent(
        chain=chain, token_address=token_id(n), symbol=f"T{n}",
        observed_at=anchor, first_evidence_at=anchor,
        launch_source="dexscreener:profile",
        first_pair_address="Pair%04d" % n, first_dex_id="raydium",
        initial_price_usd=0.001, initial_liquidity_usd=5_000.0,
        created_at=anchor,
    )
    session.add(birth)
    session.flush()
    return birth


def settings(**over) -> Settings:
    base = dict(
        database_url="sqlite://", crypto_chain=CHAIN,
        enable_crypto_sparse_observation=True,
    )
    base.update(over)
    return Settings(**base)


def config(**over) -> sparse.SparseObservationConfig:
    base = dict(chain=CHAIN, write_batch_size=25, max_duration_seconds=600.0)
    base.update(over)
    return sparse.SparseObservationConfig(**base)


async def run_pass(session, *, adapter=None, now=NOW, s=None, cfg=None, **kw):
    s = s or settings()
    service = CryptoHorizonService(adapter=adapter or FakeAdapter(), settings=s)
    return await sparse.run_scheduled_sparse_observation(
        session, settings=s, service=service,
        config=cfg or config(chain=s.crypto_chain), now=now,
        sleeper=lambda _s: None, **kw,
    )


def seed(session, count: int = 4):
    """`count` births whose 6h band is open at NOW, plus their canned pairs."""
    for n in range(1, count + 1):
        add_birth(session, n, anchor=NOW - timedelta(hours=6, minutes=n))
    session.commit()
    return FakeAdapter({token_id(n): [pair(token_id(n))] for n in range(1, count + 1)})


def row_counts(session) -> dict:
    return {
        "cohorts": session.query(CryptoHorizonCohort).count(),
        "members": session.query(CryptoHorizonCohortMember).count(),
        "observations": session.query(CryptoHorizonObservation).count(),
        "ticks": session.query(CryptoPriceTick).count(),
    }


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def events() -> list[dict]:
    parsed, malformed = read_events(get_sink().path)
    assert malformed == 0, "the sink wrote a line it cannot read back"
    return parsed


def only_event() -> dict:
    got = events()
    assert len(got) == 1, f"expected exactly one pass record, got {len(got)}"
    return got[0]


# --- a real file-backed database, for the contention measurement ------------


class _FileDb:
    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "gate7.db"
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"timeout": 5.0})
        Base.metadata.create_all(self.engine)
        self.Factory = sessionmaker(bind=self.engine)

    def close(self):
        self.engine.dispose()


class _Holder:
    """An independent connection holding SQLite's RESERVED write lock."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), timeout=HOLDER_TIMEOUT_SECONDS)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "UPDATE crypto_horizon_cohorts SET chain = chain")

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


# --- 1. identity: the reserved name, the reserved slice ---------------------


class TestWriterIdentity:
    def test_the_writer_name_is_the_one_the_enum_reserved(self):
        """The sink's enum reserved `crypto_horizon_observe` "for later slices
        (001B-001D)". This is that slice, so nothing about the label set
        changes — using any other name would either be rejected outright or
        make writer A's calibration corpus unfilterable."""
        assert sparse.TELEMETRY_WRITER_NAME == "crypto_horizon_observe"
        assert sparse.TELEMETRY_WRITER_NAME in WRITER_NAMES
        assert sparse.TELEMETRY_WRITER_NAME != "crypto_tape"

    @pytest.mark.asyncio
    async def test_a_completed_pass_writes_exactly_one_record(self, session):
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        assert result["status"] == "ok"
        event = only_event()
        assert event["writer_name"] == "crypto_horizon_observe"
        assert event["operation_name"] == "scheduled_sparse_observation"
        assert event["run_status"] == "ok"

    def test_every_status_this_lane_can_emit_is_in_the_bounded_set(self):
        """A status outside the set would be normalized to "other" and the
        pass's exact outcome would be lost — silently, and only in production.

        Scanned rather than listed so a status added later cannot slip in
        unnoticed. `STATUS_DUE_NOW` is excluded because it is imported from the
        horizon planner and labels a PLAN ENTRY, never a pass."""
        emitted = {
            getattr(sparse, name) for name in dir(sparse)
            if name.startswith("STATUS_") and name != "STATUS_DUE_NOW"
        }
        # the `invalid_*` config-refusal family is an open prefix that the
        # builder collapses to the single `invalid_config` label
        assert {s for s in emitted if not s.startswith("invalid_")} <= RUN_STATUSES
        for status in emitted:
            normalized = writer_pass.normalize_run_status(status)
            assert normalized != "other", f"{status} would lose its identity"

    def test_every_stop_reason_this_lane_can_emit_is_in_the_bounded_set(self):
        for reason in (sparse.STOP_DEADLINE, sparse.STOP_OBSERVE_LIMIT,
                       sparse.STOP_COMPLETE):
            assert reason in STOP_REASONS
            assert writer_pass.normalize_stop_reason(reason) == reason


# --- 2. the record survives a pass that committed nothing -------------------


class TestRefusalsAreRecorded:
    @pytest.mark.asyncio
    async def test_an_invalid_config_refusal_is_recorded(self, session):
        """The refusal families are exactly the passes a run table loses."""
        await run_pass(session, cfg=config(observe_limit=0))
        event = only_event()
        assert event["run_status"] == "invalid_config"
        assert event["outcome"] == "failed_other"

    @pytest.mark.asyncio
    async def test_a_degraded_marketops_refusal_is_recorded(self, session):
        session.add(MarketOpsRun(
            started_at=NOW - timedelta(minutes=2),
            finished_at=NOW - timedelta(minutes=1), status="error",
        ))
        session.commit()
        result = await run_pass(session)
        assert result["status"] == "marketops_degraded"
        event = only_event()
        assert event["run_status"] == "marketops_degraded"
        assert event["outcome"] == "skipped_health"

    @pytest.mark.asyncio
    async def test_an_ambiguous_cohort_refusal_is_bucketed_as_a_failure(
        self, session
    ):
        """`ambiguous_cohort` and `provider_policy_violation` were reserved in
        `RUN_STATUSES` with no coarse bucket, so both fell through to
        "unknown" — which hides the severest refusal this lane has from any
        `outcome` filter. They are `failed_other`, never `failed_lock`:
        bucketing a non-lock refusal as lock loss would inflate the contention
        rate this whole surface exists to measure."""
        for _ in range(2):
            session.add(CryptoHorizonCohort(
                chain=CHAIN, member_limit=0, window_hours=25, note="x",
                provenance={"membership": MEMBERSHIP_ROLLING}, created_at=NOW))
        session.commit()
        result = await run_pass(session)
        assert result["status"] == "ambiguous_cohort"
        event = only_event()
        assert event["run_status"] == "ambiguous_cohort"
        assert event["outcome"] == "failed_other"

    def test_the_provider_policy_violation_is_not_filed_as_unknown(self):
        assert writer_pass.outcome_for_status(
            "provider_policy_violation") == "failed_other"
        assert writer_pass.outcome_for_status("ambiguous_cohort") == "failed_other"

    @pytest.mark.asyncio
    async def test_a_dry_run_is_recorded_and_says_it_bypassed_the_gate(
        self, session
    ):
        seed(session)
        result = await run_pass(session, dry_run=True)
        assert result["status"] == "dry_run"
        event = only_event()
        assert event["run_status"] == "dry_run"
        assert event["gate_bypassed"] is True

    @pytest.mark.asyncio
    async def test_the_flag_off_path_emits_nothing_and_that_is_deliberate(
        self, session
    ):
        """Same call as `crypto_tape._finish`: a flag that is off means no pass
        happened. This lane proposes an HOURLY cadence, so recording it would
        file 24 events a day describing nothing."""
        result = await run_pass(
            session, s=settings(enable_crypto_sparse_observation=False))
        assert result["status"] == "disabled"
        assert events() == []


# --- 3. what must be preserved in the record --------------------------------


class TestPreservedFields:
    @pytest.mark.asyncio
    async def test_the_pass_carries_every_field_this_gate_exists_for(
        self, session
    ):
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        event = only_event()
        for field in ("run_source", "gate_bypassed", "write_hold_measured",
                      "retry_count", "external_calls", "duration_ms",
                      "batch_count", "lock_failures"):
            assert field in event, f"{field} was dropped"
        assert event["external_calls"] == result["external_calls"] > 0
        assert event["batch_count"] == result["batches_committed"]
        assert event["stop_reason"] == result["stop_reason"] == "complete"

    @pytest.mark.asyncio
    async def test_the_measured_quantities_are_the_meters_own(self, session):
        adapter = seed(session)
        result = await run_pass(
            session, adapter=adapter, cfg=config(write_batch_size=2))
        meter = result["write_lock"]
        event = only_event()
        assert event["batch_count"] == meter["batches"] == 2
        assert event["retry_count"] == meter["retry_attempts"]
        assert event["lock_failures"] == meter["lock_failures"]
        assert event["write_hold_ms_max"] == int(meter["write_hold_ms_max"])

    @pytest.mark.asyncio
    async def test_rows_committed_is_a_row_sum_across_the_tables_written(
        self, session
    ):
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        event = only_event()
        assert event["rows_committed"] == (
            result["enrolled"] + result["observations_recorded"]
            + result["ticks_written"]
        )
        assert event["rows_attempted"] == result["due_observations"]
        assert event["rows_skipped"] == result["deferred_observations"]

    @pytest.mark.asyncio
    async def test_the_deferred_count_rides_the_observe_limit_stop(
        self, session
    ):
        adapter = seed(session, count=4)
        result = await run_pass(
            session, adapter=adapter, cfg=config(observe_limit=1))
        event = only_event()
        assert event["stop_reason"] == "observe_limit"
        assert event["rows_skipped"] == result["deferred_observations"] > 0

    @pytest.mark.asyncio
    async def test_the_fetch_write_separation_is_a_persisted_claim(
        self, session
    ):
        """This lane's headline structural property — the write phase touches no
        network and the fetch phase holds no transaction — is asserted ON the
        record, so an edit that interleaved them would have to change it."""
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        assert only_event()["provider_io_during_transaction"] is False


# --- 4. run_source is derived, gate_bypassed stays separate -----------------


class TestRunSourceAndBypass:
    @pytest.mark.asyncio
    async def test_a_bare_shell_pass_is_manual(self, session, monkeypatch):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        event = only_event()
        assert event["run_source"] == "manual"
        assert event["writer_class"] == "manual_command"

    @pytest.mark.asyncio
    async def test_systemd_invocation_id_means_scheduled(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        event = only_event()
        assert event["run_source"] == "scheduled"
        assert event["writer_class"] == "scheduled_oneshot"

    @pytest.mark.asyncio
    async def test_copying_the_units_execstart_by_hand_does_not_forge_scheduled(
        self, session, monkeypatch
    ):
        """The reason the field is derived from `INVOCATION_ID` and not from the
        command line: an operator reproducing a timer failure by pasting
        `ExecStart` must not file their attended pass as an unattended one."""
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.setattr(sys, "argv", [
            "crypto-sparse-observe", "--scheduled", "--chain", CHAIN])
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        assert only_event()["run_source"] == "manual"

    @pytest.mark.asyncio
    async def test_scheduled_plus_bypassed_stays_readable_as_the_anomaly(
        self, session, monkeypatch
    ):
        """`gate_bypassed` is never merged into `run_source`: an unattended pass
        that ALSO bypassed the gate is a real anomaly, and it is visible only
        while the two remain separate fields."""
        monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
        adapter = seed(session)
        await run_pass(
            session, adapter=adapter, force=True,
            s=settings(enable_crypto_sparse_observation=False))
        event = only_event()
        assert event["run_source"] == "scheduled"
        assert event["gate_bypassed"] is True

    @pytest.mark.asyncio
    async def test_an_ungated_pass_is_not_marked_bypassed(self, session):
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        assert only_event()["gate_bypassed"] is False


# --- 5. A1: "not measured" and "sub-millisecond" stay distinguishable -------


class TestWriteHoldIsNotZeroInflated:
    """THE SURVIVORSHIP BIAS, ARRIVING BY WRITER B'S ROUTE.

    `_WriteMeter.record` runs once per RETURNED commit, so `write_hold_ms_max`
    and `commit_ms_max` are a structural 0.0 on every pass that opened no write
    transaction — and `int(0.0004 * 1000)` truncates a genuine sub-millisecond
    hold to the same 0. An operator averaging either field to size a batch would
    fold the passes that measured NOTHING into the mean, pull it down, and set
    the constant too aggressive on a live writer."""

    @pytest.mark.asyncio
    async def test_a_completed_pass_marks_its_hold_as_measured(self, session):
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        event = only_event()
        assert result["write_lock"]["batches"] > 0
        assert event["write_hold_measured"] is True
        assert "write_hold_ms_max" in event

    @pytest.mark.asyncio
    async def test_a_refusal_omits_the_hold_rather_than_reporting_zero(
        self, session
    ):
        await run_pass(session, cfg=config(observe_limit=0))
        event = only_event()
        assert event["write_hold_measured"] is False
        assert "write_hold_ms_max" not in event, (
            "an unmeasured pass must not contribute a zero to the mean")
        assert "commit_ms" not in event, (
            "the mapped commit figure is unmeasured on the same passes")

    @pytest.mark.asyncio
    async def test_a_pass_with_nothing_due_measured_no_hold(self, session):
        """No births, so no batch is ever staged. `ok` and honest: the record
        exists, and it says it measured nothing."""
        result = await run_pass(session)
        assert result["status"] == "ok"
        event = only_event()
        assert event["batch_count"] == 0
        assert event["write_hold_measured"] is False
        assert "write_hold_ms_max" not in event

    @pytest.mark.asyncio
    async def test_the_calibration_filter_excludes_every_unmeasured_pass(
        self, session
    ):
        """The same filter the runbook gives an operator for writer A, applied
        to writer B's records. Mixing a refusal into the corpus is exactly what
        biases the constant."""
        await run_pass(session, cfg=config(observe_limit=0))   # measured nothing
        adapter = seed(session)
        await run_pass(session, adapter=adapter)               # a real pass
        got = events()
        assert len(got) == 2
        eligible = [
            e for e in got
            if e.get("run_status") in {"ok", "partial", "truncated"}
            and (e.get("batch_count") or 0) > 0
            and e.get("write_hold_measured") is True
        ]
        assert len(eligible) == 1
        assert all("write_hold_ms_max" in e for e in eligible)


# --- 6. commit_ms_max is MAPPED, and the phases are not fabricated ----------


class TestCommitMappingAndPhaseDiscipline:
    @pytest.mark.asyncio
    async def test_commit_ms_max_lands_in_the_envelopes_commit_field(
        self, session
    ):
        """Writer B's `commit_ms_max` has no exact field in the shipped
        envelope. It is MAPPED onto `commit_ms` (whose definition — the wall
        between `before_commit` and `after_commit` — is exactly what
        `_commit_with_retry` times) rather than added, because the sink's schema
        serves five writers and is the safety boundary. `batch_count` on the
        same record names how many commits the maximum is over."""
        adapter = seed(session)
        result = await run_pass(
            session, adapter=adapter, cfg=config(write_batch_size=2))
        meter = result["write_lock"]
        event = only_event()
        assert event["commit_ms"] == int(meter["commit_ms_max"])
        assert event["commit_quality"] == "exact"
        assert event["batch_count"] == 2, "the denominator of the maximum"

    @pytest.mark.asyncio
    async def test_the_mapped_commit_can_never_exceed_the_hold_it_sits_inside(
        self, session
    ):
        adapter = seed(session)
        await run_pass(session, adapter=adapter, cfg=config(write_batch_size=1))
        event = only_event()
        assert event["commit_ms"] <= event["write_hold_ms_max"]

    @pytest.mark.asyncio
    async def test_the_lock_wait_phase_fields_are_absent_not_zero(self, session):
        """PHASE FIELDS ARE NEVER SUMMED AND NEVER INVENTED. Writer B has one
        write phase, no run row and no finalize commit, and it times no lock
        WAIT anywhere — so all four lock-wait fields are ABSENT. A zero would
        read as a measured absence of contention; a value derived from
        `retry_attempts * DB_LOCKED_RETRY_SECONDS` would be a constant dressed
        as a measurement, in a field that feeds a governed stop condition."""
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        event = only_event()
        for field in ("lock_wait_ms", "batch_lock_wait_ms_max",
                      "run_row_lock_wait_ms", "finalize_lock_wait_ms"):
            assert field not in event, (
                f"{field} was fabricated; writer B measures no lock wait")

    @pytest.mark.asyncio
    async def test_contention_is_reported_through_the_field_it_measures(
        self, session
    ):
        """`lock_failures` is the honest contention signal this lane HAS. The
        retry ladder is driven for real — a locked commit, then a rollback, then
        a re-stage — so the count is a measurement and not a sleep constant.

        NEVER `monkeypatch.undo()` IN THIS SUITE. pytest hands every fixture of
        a test the SAME `monkeypatch` instance, so `undo()` also reverts
        conftest's `_isolate_sqlite_telemetry` and the next `get_sink()` resolves
        to the operator's REAL ~/probability-arena-telemetry file. Caught here
        while writing this test (it read three of a host's own records); a
        private context is used instead."""
        adapter = seed(session, count=2)
        tripped = {"done": False}
        real_commit = Session.commit

        def flaky(self, *a, **k):
            # Only the WRITE phase's batch commit, never the enrolment commit
            # that precedes it — a lock lost during enrolment abandons the pass
            # before any batch is staged and measures nothing at all.
            staging_observations = any(
                isinstance(obj, CryptoHorizonObservation) for obj in self.new)
            if staging_observations and not tripped["done"]:
                tripped["done"] = True
                raise sqlite3.OperationalError("database is locked")
            return real_commit(self, *a, **k)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Session, "commit", flaky)
            result = await run_pass(session, adapter=adapter)
        event = only_event()
        assert tripped["done"], "the ladder was never exercised"
        assert result["status"] == "ok", "the retry recovered the batch"
        assert result["write_lock"]["lock_failures"] == 1
        assert event["lock_failures"] == 1
        assert event["retry_count"] == result["write_lock"]["retry_attempts"] == 1
        # and still no fabricated wait: the ladder was timed nowhere
        assert "lock_wait_ms" not in event


# --- 6b. the mapping itself, without a sink ---------------------------------


def _result(**over) -> dict:
    base = {
        "status": "ok", "stop_reason": "complete", "gate_bypassed": None,
        "duration_ms": 1234, "external_calls": 4, "due_observations": 8,
        "deferred_observations": 2, "band_closed_during_pass": 3,
        "enrolled": 5, "observations_recorded": 6, "ticks_written": 7,
        "batches_committed": 2,
        "write_lock": {
            "batches": 2, "retry_attempts": 1, "lock_failures": 1,
            "write_hold_ms_max": 140.75, "commit_ms_max": 9.4,
            "commit_ms_total": 12.0, "persisted": False, "note": "",
        },
    }
    base.update(over)
    return base


class TestFieldMapping:
    """`_pass_telemetry_fields` is split out of the emitter precisely so the
    mapping can be pinned without a sink or a database. These are the cases a
    live pass cannot produce on demand."""

    def test_the_band_closed_count_is_not_folded_into_rows_skipped(self):
        """`deferred_observations` (never attempted, under `observe_limit` or
        the deadline) and `band_closed_during_pass` (fetched, then correctly
        refused as out of band) are different facts. Summing them would make
        neither invertible, and only one of them means "this pass was too
        small". The band-closed count has no field in the envelope and is
        DROPPED rather than smuggled into a field that means something else."""
        fields = sparse._pass_telemetry_fields(_result())
        assert fields["rows_skipped"] == 2
        assert 5 not in (fields["rows_skipped"],), "3 was folded in"

    def test_the_row_sum_spans_the_tables_this_lane_writes(self):
        fields = sparse._pass_telemetry_fields(_result())
        assert fields["rows_committed"] == 5 + 6 + 7
        assert fields["rows_attempted"] == 8

    def test_a_float_hold_truncates_to_an_int_as_the_envelope_requires(self):
        fields = sparse._pass_telemetry_fields(_result())
        assert fields["write_hold_ms_max"] == 140
        assert fields["commit_ms"] == 9
        assert isinstance(fields["write_hold_ms_max"], int)
        assert isinstance(fields["commit_ms"], int)

    def test_a_pass_with_no_batches_omits_both_timing_figures(self):
        fields = sparse._pass_telemetry_fields(
            _result(write_lock={"batches": 0, "retry_attempts": 0,
                                "lock_failures": 0, "write_hold_ms_max": 0.0,
                                "commit_ms_max": 0.0}))
        assert fields["write_hold_measured"] is False
        assert "write_hold_ms_max" not in fields
        assert "commit_ms" not in fields
        assert "commit_quality" not in fields

    @pytest.mark.parametrize("meter", [None, "not-a-dict", 7, []])
    def test_a_missing_or_malformed_meter_reads_as_measured_nothing(self, meter):
        """Refusals before the write phase have no `write_lock` key at all, and
        a duck-typed stand-in can put anything there. Neither may raise on the
        writer's return path."""
        fields = sparse._pass_telemetry_fields(_result(write_lock=meter))
        assert fields["write_hold_measured"] is False
        assert fields["retry_count"] == 0
        assert "write_hold_ms_max" not in fields

    def test_no_lock_wait_field_is_ever_produced(self):
        fields = sparse._pass_telemetry_fields(_result())
        assert not [k for k in fields if "lock_wait" in k]


# --- 7. HARD CONSTRAINT 1: the telemetry path never touches SQLite ----------


# The driver runs OUT OF PROCESS, and that is not incidental.
# `sys.addaudithook` CANNOT BE REMOVED once installed and it switches on
# CPython's audit machinery for the whole interpreter for the rest of its life.
# Installing one inside the shared suite would silently tax every later test —
# including `test_emit_overhead_within_budget`, which asserts a 1 ms p99 on this
# very sink. A subprocess is also strictly stronger evidence: the hook is armed
# in a pristine interpreter where no engine has ever been created, so a
# `sqlite3.connect` anywhere under the emit has nowhere to hide.
_AUDIT_DRIVER = r'''
import errno, json, os, sys
sys.path.insert(0, {root!r})
seen = []
def hook(name, args):
    if name.startswith("sqlite3."):
        seen.append(name)
sys.addaudithook(hook)

broken = sys.argv[1]
tmp = sys.argv[2]
over = {{}}
if broken == "happy":
    os.environ["SQLITE_TELEMETRY_DIR"] = os.path.join(tmp, "tel")
elif broken == "missing_parent":
    blocker = os.path.join(tmp, "blocker")
    open(blocker, "w").write("not a directory")
    os.environ["SQLITE_TELEMETRY_DIR"] = os.path.join(blocker, "tel")
elif broken == "permission_denied":
    d = os.path.join(tmp, "tel"); os.mkdir(d)
    f = os.path.join(d, "sqlite-writes.jsonl")
    open(f, "w").close(); os.chmod(f, 0o400)
    os.environ["SQLITE_TELEMETRY_DIR"] = d
elif broken == "symlink":
    d = os.path.join(tmp, "tel"); os.mkdir(d)
    victim = os.path.join(tmp, "pretend.db")
    open(victim, "wb").write(b"SQLite format 3\x00")
    os.symlink(victim, os.path.join(d, "sqlite-writes.jsonl"))
    os.environ["SQLITE_TELEMETRY_DIR"] = d
elif broken == "dir_is_a_file":
    f = os.path.join(tmp, "tel")
    open(f, "w").write("a regular file, not a directory")
    os.environ["SQLITE_TELEMETRY_DIR"] = f
elif broken == "disk_full":
    os.environ["SQLITE_TELEMETRY_DIR"] = os.path.join(tmp, "tel")
    real_write = os.write
    def full(fd, data):
        if b'"writer_name"' in data:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)
    os.write = full
elif broken == "unserializable_value":
    os.environ["SQLITE_TELEMETRY_DIR"] = os.path.join(tmp, "tel")
    over = {{"rows_committed": object()}}
else:
    raise SystemExit("unknown scenario " + broken)

from datetime import datetime, timezone
from app.telemetry import writer_pass

before = len(seen)
writer_pass.emit_writer_pass(
    writer_name="crypto_horizon_observe",
    operation_name="scheduled_sparse_observation",
    started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    run_status="ok", batch_count=1, write_hold_measured=True,
    write_hold_ms_max=3, commit_ms=1, commit_quality="exact",
    external_calls=4, table_groups=["crypto_horizon"], **over,
)
# only what the EMIT raised; the import above is not under test
print(json.dumps(seen[before:]))
'''


def _audit_emit(scenario: str, tmp_path: Path) -> list[str]:
    import json
    import subprocess

    root = str(Path(__file__).resolve().parents[1])
    driver = tmp_path / "driver.py"
    driver.write_text(_AUDIT_DRIVER.format(root=root))
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, str(driver), scenario, str(work)],
        capture_output=True, text=True, cwd=root)
    assert proc.returncode == 0, (
        f"the emit raised out of a pristine process:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestTelemetryNeverTouchesSqlite:
    """THE ONE HARD MANDATE OF THE 001A SINK, proved for writer B the way it
    was proved for writer A: an audit hook that would see a connect or an
    execute, across the happy path and every error path the emit has.

    `sqlite3.connect` and `sqlite3.execute` are CPython audit events raised by
    the stdlib driver itself — which is the driver SQLAlchemy uses here — so a
    database opened anywhere under the emit, by any layer, lands in this list."""

    def test_the_happy_path_opens_no_database(self, tmp_path):
        assert _audit_emit("happy", tmp_path) == []

    @pytest.mark.parametrize("broken", [
        "missing_parent", "permission_denied", "symlink", "dir_is_a_file",
        "disk_full", "unserializable_value",
    ])
    def test_no_error_path_opens_a_database(self, tmp_path, broken):
        seen = _audit_emit(broken, tmp_path)
        assert seen == [], f"the {broken} path touched SQLite: {seen}"

    def test_the_audit_harness_would_actually_see_a_database(self, tmp_path):
        """THE HARNESS'S OWN NEGATIVE CONTROL. Six clean results mean nothing
        unless the hook can fail — a typo in the event prefix would produce the
        same six empty lists."""
        import json
        import subprocess

        root = str(Path(__file__).resolve().parents[1])
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import json, sqlite3, sys\n"
            "seen = []\n"
            "sys.addaudithook(lambda n, a: "
            "seen.append(n) if n.startswith('sqlite3.') else None)\n"
            f"sqlite3.connect({str(tmp_path / 'probe.db')!r}).execute('select 1')\n"
            "print(json.dumps(seen))\n")
        proc = subprocess.run([sys.executable, str(probe)],
                              capture_output=True, text=True, cwd=root)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout.strip()), (
            "the audit hook sees nothing even when SQLite IS used — the six "
            "clean results above prove nothing")

    def test_the_sink_path_is_not_a_database(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(tmp_path / "tel"))
        import app.telemetry.sink as sink_mod
        sink_mod._sink = None
        assert writer_pass.emit_writer_pass(
            writer_name=sparse.TELEMETRY_WRITER_NAME,
            operation_name=sparse.TELEMETRY_OPERATION_NAME,
            started_at=datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            run_status="ok",
        ) is not None
        assert get_sink().path.suffix == ".jsonl"
        assert not str(get_sink().path).endswith(".db")


# --- 8. HARD CONSTRAINT 2: the destination-failure matrix -------------------


def _break_destination(kind: str, tmp_path: Path, monkeypatch):
    """Five real ways the telemetry destination is unusable. Each is created on
    the filesystem (or in the syscall) rather than by patching the sink, so the
    emit takes exactly the path it would take on a host."""
    import app.telemetry.sink as sink_mod

    if kind == "missing_path":
        # the configured directory's parent is a FILE, so `mkdir` cannot even
        # create it — the "somebody deleted /mnt/data" shape
        blocker = tmp_path / "gone"
        blocker.write_text("x")
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(blocker / "deep" / "tel"))
    elif kind == "permission_denied":
        directory = tmp_path / "tel"
        directory.mkdir()
        target = directory / "sqlite-writes.jsonl"
        target.touch()
        os.chmod(target, 0o400)
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(directory))
    elif kind == "symlink":
        directory = tmp_path / "tel"
        directory.mkdir()
        victim = tmp_path / "pretend.db"
        victim.write_bytes(b"SQLite format 3\x00-- must not be appended to --\n")
        (directory / "sqlite-writes.jsonl").symlink_to(victim)
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(directory))
    elif kind == "malformed":
        # the configured "directory" is a regular file
        destination = tmp_path / "tel"
        destination.write_text("not a directory")
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(destination))
    elif kind == "disk_full":
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(tmp_path / "tel"))
        real_write = os.write

        def full(fd, data):
            if b'"writer_name"' in data:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", full)
    else:  # pragma: no cover - guarded by the parametrize list
        raise AssertionError(kind)
    sink_mod._sink = None


class TestTelemetryCannotAbortTheObservation:
    """THE MATRIX. Under every unusable destination the observation must
    complete UNCHANGED: same status, same durable rows, same provider spend.
    A telemetry surface that can fail the work it measures is worse than no
    telemetry surface."""

    DESTINATIONS = ["disk_full", "permission_denied", "missing_path",
                    "symlink", "malformed"]

    @pytest.mark.asyncio
    async def test_the_healthy_baseline(self, session):
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        assert result["status"] == "ok"
        assert row_counts(session)["observations"] > 0
        assert len(events()) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", DESTINATIONS)
    async def test_a_broken_destination_changes_nothing_about_the_pass(
        self, session, tmp_path, monkeypatch, kind
    ):
        adapter = seed(session)
        _break_destination(kind, tmp_path, monkeypatch)
        result = await run_pass(session, adapter=adapter)
        assert result["status"] == "ok", (
            f"the {kind} destination changed the pass's status")
        counts = row_counts(session)
        assert counts["observations"] == result["observations_recorded"] > 0
        assert counts["ticks"] == result["ticks_written"] > 0
        assert counts["members"] == result["enrolled"] > 0
        assert result["external_calls"] == adapter.calls > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", DESTINATIONS)
    async def test_a_broken_destination_never_claims_the_record_landed(
        self, session, tmp_path, monkeypatch, kind
    ):
        """FAIL-SOFT MAY DROP THE RECORD; IT MAY NOT FABRICATE SUCCESS. The
        result's own `persisted` flag is set from the append's actual outcome,
        so an operator reading the receipt is never told a record exists that
        does not."""
        adapter = seed(session)
        _break_destination(kind, tmp_path, monkeypatch)
        result = await run_pass(session, adapter=adapter)
        assert result["write_lock"]["persisted"] is False

    @pytest.mark.asyncio
    async def test_the_symlinked_destination_is_not_followed_into_a_database(
        self, session, tmp_path, monkeypatch
    ):
        """The one filesystem route to the corruption trap: a symlink at the
        sink path pointing at a live SQLite database would make the telemetry
        append write JSON INTO THE DATABASE FILE."""
        victim = tmp_path / "pretend.db"
        adapter = seed(session)
        _break_destination("symlink", tmp_path, monkeypatch)
        payload = victim.read_bytes()
        result = await run_pass(session, adapter=adapter)
        assert result["status"] == "ok"
        assert victim.read_bytes() == payload, (
            "the symlink target was appended to — O_NOFOLLOW is gone")

    @pytest.mark.asyncio
    async def test_a_sink_that_raises_outright_does_not_fail_the_pass(
        self, session, monkeypatch
    ):
        """`durable_but_nonblocking`, proved against the real pass rather than
        the helper: even an exception from inside the sink is swallowed."""
        monkeypatch.setattr(
            "app.telemetry.sink.TelemetrySink.emit",
            lambda self, event: (_ for _ in ()).throw(OSError("sink is gone")))
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        assert result["status"] == "ok"
        assert result["observations_recorded"] > 0
        assert result["write_lock"]["persisted"] is False


# --- 9. the extraction prelude degrades rather than escaping ----------------


class TestExtractionCannotFailThePass:
    """THE TRAP WRITER A'S REVIEW PROVED AND WRITER B MAKES REACHABLE.
    `emit_writer_pass` swallows everything, but the field extraction runs
    BEFORE it, on the writer's return path. Writer A could not actually reach
    it (every producer is an int); writer B's meter carries FLOATS, its
    `write_lock` block is absent on most refusal paths, and a duck-typed
    stand-in can put anything in either."""

    @pytest.mark.asyncio
    async def test_a_non_numeric_measurement_cannot_escape_into_the_writer(
        self, session, monkeypatch
    ):
        def poisoned(self):
            return {"batches": 2, "write_hold_ms_max": "n/a",
                    "commit_ms_max": 0.5, "retry_attempts": 0,
                    "lock_failures": 0, "persisted": False, "note": ""}

        monkeypatch.setattr(sparse._WriteMeter, "snapshot", poisoned)
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        assert result["status"] == "ok", (
            "telemetry extraction must never decide the pass's outcome")
        assert result["observations_recorded"] > 0

    @pytest.mark.asyncio
    async def test_the_pass_still_leaves_an_identity_record_without_the_numbers(
        self, session, monkeypatch
    ):
        """A bare `except: pass` around the extraction would drop the WHOLE
        record. The pass happened and its status is the fact a calibration
        corpus most needs, so the identity record lands without the numbers."""
        def poisoned(self):
            return {"batches": 2, "write_hold_ms_max": "n/a",
                    "commit_ms_max": 0.5, "retry_attempts": 0,
                    "lock_failures": 0, "persisted": False, "note": ""}

        monkeypatch.setattr(sparse._WriteMeter, "snapshot", poisoned)
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        event = only_event()
        assert event["run_status"] == result["status"]
        assert event["writer_name"] == "crypto_horizon_observe"
        assert "write_hold_ms_max" not in event
        assert "batch_count" not in event

    @pytest.mark.asyncio
    async def test_a_missing_write_lock_block_is_not_an_error(self, session):
        """Every refusal before the write phase has no `write_lock` key at all.
        The extraction must read that as "measured nothing", not as a failure."""
        await run_pass(session, cfg=config(write_batch_size=0))
        event = only_event()
        assert event["run_status"] == "invalid_config"
        assert event["write_hold_measured"] is False
        assert event["retry_count"] == 0


# --- 10. degradation may drop fields, never fabricate an outcome -----------


class TestDegradeRatherThanDelete:
    def test_writer_b_inherits_the_sinks_degradation(self, tmp_path):
        """The sink strips to REQUIRED_FIELDS + truncated=true rather than
        deleting a record whose optional field failed validation. Writer B
        inherits it unchanged; nothing here loosens the validator."""
        sink = TelemetrySink(tmp_path / "tel")
        event = writer_pass.build_event(
            writer_name="crypto_horizon_observe",
            writer_class="manual_command",
            operation_name="scheduled_sparse_observation",
            started_at=datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            outcome="failed_lock",
            run_status="db_locked",
            commit_ms=1.5,   # a float where an int is required
        )
        assert sink.emit(event) is True
        assert sink.degraded == 1
        landed, _ = read_events(sink.path)
        assert landed[0]["truncated"] is True
        assert set(landed[0]) == REQUIRED_FIELDS | {"truncated"}

    def test_a_degraded_record_cannot_report_a_success_the_pass_did_not_have(
        self, tmp_path
    ):
        """`outcome` is a REQUIRED field, so it survives the strip verbatim.
        Degradation sheds optional numbers; it can never turn a lost pass into
        a landed one."""
        sink = TelemetrySink(tmp_path / "tel")
        event = writer_pass.build_event(
            writer_name="crypto_horizon_observe",
            writer_class="manual_command",
            operation_name="scheduled_sparse_observation",
            started_at=datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            outcome="failed_lock",
            run_status="db_locked",
            batch_count=-1,   # impossible count
        )
        assert sink.emit(event) is True
        landed, _ = read_events(sink.path)
        assert landed[0]["outcome"] == "failed_lock"
        assert landed[0]["rows_committed"] if False else True  # not resurrected
        assert "rows_committed" not in landed[0]

    @pytest.mark.asyncio
    async def test_a_lost_record_is_never_reported_as_a_landed_one(
        self, session, tmp_path, monkeypatch
    ):
        adapter = seed(session)
        _break_destination("disk_full", tmp_path, monkeypatch)
        await run_pass(session, adapter=adapter)
        assert events() == []


# --- 11. the append's own cost, under a real competing writer --------------


class TestTelemetryIsNotAContentionSource:
    def test_the_append_lands_and_is_cheap_while_a_writer_holds_the_lock(
        self, filedb, tmp_path, monkeypatch
    ):
        """MEASURED, NOT ASSUMED. A second connection holds SQLite's RESERVED
        write lock for the whole emit. If the sink touched SQLite at all this
        would block for the busy timeout and then fail.

        The sparse pass's own write phase is ~110 ms of a ~1,569 ms pass with a
        27 ms peak co-tenant stall; a telemetry append costing a meaningful
        fraction of that would be self-defeating. The bound is generous on
        purpose — the claim is the ORDER OF MAGNITUDE, and it must hold on a
        loaded laptop as well as on EVO."""
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(tmp_path / "tel"))
        import app.telemetry.sink as sink_mod
        sink_mod._sink = None

        holder = _Holder(filedb.path)
        try:
            samples = []
            for _ in range(20):
                t0 = time.perf_counter()
                eid = writer_pass.emit_writer_pass(
                    writer_name=sparse.TELEMETRY_WRITER_NAME,
                    operation_name=sparse.TELEMETRY_OPERATION_NAME,
                    started_at=datetime.now(timezone.utc)
                    .isoformat().replace("+00:00", "Z"),
                    run_status="ok", stop_reason="complete",
                    batch_count=2, retry_count=0, lock_failures=0,
                    write_hold_measured=True, write_hold_ms_max=110,
                    commit_ms=4, commit_quality="exact",
                    rows_attempted=8, rows_committed=16, rows_skipped=0,
                    external_calls=4, table_groups=["crypto_horizon"],
                )
                samples.append((time.perf_counter() - t0) * 1000)
                assert eid is not None, (
                    "the record must land while the DB write lock is held")
        finally:
            holder.release()
        samples.sort()
        p50 = samples[len(samples) // 2]
        assert p50 < 5.0, f"telemetry append p50 {p50:.3f} ms under contention"
        assert max(samples) < 50.0, f"worst append {max(samples):.3f} ms"

    def test_one_record_is_a_bounded_line(self, tmp_path, monkeypatch):
        """The sink caps a line at 4096 B and sheds optional fields above it.
        Writer B's fullest record must be nowhere near that — the file has no
        rotation before 001E."""
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(tmp_path / "tel"))
        import app.telemetry.sink as sink_mod
        sink_mod._sink = None
        writer_pass.emit_writer_pass(
            writer_name=sparse.TELEMETRY_WRITER_NAME,
            operation_name=sparse.TELEMETRY_OPERATION_NAME,
            started_at=datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            run_status="partial", stop_reason="observe_limit",
            gate_bypassed=True, batch_count=4, retry_count=2, lock_failures=1,
            write_hold_measured=True, write_hold_ms_max=180, commit_ms=9,
            commit_quality="exact", rows_attempted=200, rows_committed=400,
            rows_skipped=8, external_calls=100,
            table_groups=["crypto_horizon"],
        )
        size = get_sink().path.stat().st_size
        assert 0 < size < 1500, f"one writer-B record is {size} B"
        assert read_events(get_sink().path)[0][0].get("truncated") is None


# --- 12. both writers, one stream ------------------------------------------


class TestOneEvaluationPlane:
    @pytest.mark.asyncio
    async def test_writer_b_lands_in_the_same_file_as_writer_a(self, session):
        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        writer_pass.emit_writer_pass(
            writer_name="crypto_tape", operation_name="scheduled_reconciliation",
            started_at=datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            run_status="ok",
        )
        names = [e["writer_name"] for e in events()]
        assert names == ["crypto_horizon_observe", "crypto_tape"]

    @pytest.mark.asyncio
    async def test_writer_b_is_out_of_scope_for_the_governed_lock_tally(
        self, session
    ):
        """`scripts/sqlite_analyze_maintenance.py` scopes its `lock_events`
        stop condition to `{tick_aggregation, backup}` because that is the
        population the `> 6` limit was calibrated on. Writer B's records must
        land OUTSIDE that scope — a new writer must not be able to move a
        governed threshold by existing."""
        import importlib.util

        adapter = seed(session)
        await run_pass(session, adapter=adapter)
        path = (Path(__file__).resolve().parents[1] / "scripts"
                / "sqlite_analyze_maintenance.py")
        spec = importlib.util.spec_from_file_location("_gate7_maint", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert sparse.TELEMETRY_WRITER_NAME not in module.LOCK_EVENT_WRITERS
        tally = module._lock_tally(get_sink().path)
        assert tally["lock_events"] == 0
        assert tally["out_of_scope_events"] == 1
        assert tally["out_of_scope_flat_predicate_hits"] == 0


# --- 13. the gate text says only what is now true ---------------------------


class TestTheGateTextIsTrue:
    @pytest.mark.asyncio
    async def test_the_payload_no_longer_carries_the_run_table_instruction(
        self, session
    ):
        """The lane's own gate was `persisted: false` plus "install no timer
        until it is". The run-record half is now satisfied — by the JSONL sink,
        which needed no table and no migration. The CALIBRATION half is not, so
        the note still refuses the timer and the flag is still default-off."""
        adapter = seed(session)
        result = await run_pass(session, adapter=adapter)
        note = result["write_lock"]["note"]
        assert result["write_lock"]["persisted"] is True
        assert "install no timer until it is" not in note
        assert "no timer is installed" in note
        assert "not calibrated" in note.lower()

    def test_the_flag_is_still_default_off(self):
        default = Settings(database_url="sqlite://")
        assert default.enable_crypto_sparse_observation is False

    def test_no_timer_unit_was_installed(self):
        units = list((Path(__file__).resolve().parents[1] / "infra" / "systemd"
                      ).rglob("*sparse*"))
        assert units == [], f"a timer/service unit was installed: {units}"

    def test_the_runbook_tells_an_operator_how_writer_b_differs(self):
        """A TEXT PIN, deliberately: the shared sink now has two grains and two
        vocabularies in one file, and an operator who reads writer B's records
        with writer A's expectations gets a per-token cost that belongs to
        neither. Nothing else in the suite can catch this paragraph's removal."""
        runbook = (Path(__file__).resolve().parents[1] / "docs"
                   / "EVO_X2_RUNBOOK.md").read_text()
        section = runbook.split(
            "### Where the per-pass record now lands "
            "(GATE2-WRITER-TELEMETRY-001)")[1].split("### `TimeoutStartSec`")[0]
        assert 'writer_name="crypto_horizon_observe"' in section
        # the four fields whose ABSENCE a reader must not read as a zero
        for field in ("lock_wait_ms", "batch_lock_wait_ms_max",
                      "run_row_lock_wait_ms", "finalize_lock_wait_ms"):
            assert field in section
        assert "ABSENT rather than zero" in section
        assert "per-pass **maximum**" in section, (
            "the commit_ms aggregation caveat is gone — a reader would take a "
            "max for a single transaction's commit")
