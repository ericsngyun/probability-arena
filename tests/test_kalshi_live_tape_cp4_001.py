"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP4 — the measurement lane, offline.

CP4's *Verify* line is three claims, and the first three test classes here are
those three claims and nothing else:

1. a fixture run at a synthetic **5,000 events/s** produces intervals whose
   `events_received` sums to the fixture count;
2. **a ticker never appears in the output file**;
3. the flusher's failure paths **never raise into the loop** (proved by
   injecting an unwritable directory, and five other faults besides).

Everything after those is the closed validator, the ring, the two append
histograms and the writer — the four things §9's CP4 says this checkpoint
builds. No socket, no network, no database, pure stdlib.

The 5,000 events/s load is driven off an INJECTED nanosecond clock rather than
`sleep`. That is not only for speed: a wall-clock test of a rate is a test of
the host's scheduler, and the property under test — that no event is lost or
double-counted across an interval boundary — is a property of the delta
arithmetic, which a synthetic clock exercises exactly and a real one exercises
only by luck.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from app.realtime.collector_metrics import (
    APPEND_US_EDGES,
    APPEND_US_LABELS,
    CLOSE_MS_LABELS,
    EVENT_BYTES_LABELS,
    HISTOGRAM_KEY_RE,
    INTERVAL_FILENAME,
    MAX_HISTOGRAM_BUCKETS,
    MAX_LINE_BYTES,
    MAX_PER_SECOND_SLOTS,
    NULL_METRICS,
    SCHEMA_VERSION,
    ALLOWED_FIELDS,
    CollectorMetrics,
    IntervalWriter,
    MetricsFlusher,
    MetricsValidationError,
    NullCollectorMetrics,
    interval_path,
    iter_interval_records,
    validate_interval_record,
)

# A real Kalshi market ticker shape. Used as the needle in every no-ticker
# assertion, and never as an input the module is meant to accept.
TICKER = "KXBTCD-26AUG1417-T64999.99"
TICKERS = (TICKER, "KXETHD-26AUG1417-T3200.00", "INXD-26AUG15-B5500")


class FakeClock:
    """A monotonic nanosecond clock the test advances by hand."""

    def __init__(self, start_ns: int = 1_000_000_000_000_000) -> None:
        self.ns = start_ns

    def __call__(self) -> int:
        return self.ns

    def advance_ns(self, delta: int) -> None:
        self.ns += delta


def _metrics(**kwargs) -> CollectorMetrics:
    kwargs.setdefault("environment", "demo")
    kwargs.setdefault("markets_subscribed", len(TICKERS))
    return CollectorMetrics(**kwargs)


def _flusher(metrics, tmp_path, clock, **kwargs) -> MetricsFlusher:
    return MetricsFlusher(metrics, path=Path(tmp_path) / INTERVAL_FILENAME,
                          clock_ns=clock, **kwargs)


def _read(path) -> list[dict]:
    return list(iter_interval_records(path))


# ---------------------------------------------------------------------------------
# CP4 Verify, claim 1 — conservation at a synthetic 5,000 events/s
# ---------------------------------------------------------------------------------


class TestFiveThousandEventsPerSecondConservation:
    """The number this milestone exists to produce must not leak events.

    §8.4's whole argument is arithmetic on a rate. If an interval boundary can
    lose or duplicate an event, every percentile downstream is wrong by an
    unknown amount, and "we measured it" would be a worse claim than the
    honest guess it replaced.
    """

    FIXTURE_SECONDS = 30
    RATE = 5_000
    STEP_NS = 1_000_000_000 // RATE  # 200 us between events

    def _run(self, tmp_path, *, flush_every_s=10):
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        produced = 0
        for second in range(self.FIXTURE_SECONDS):
            for _ in range(self.RATE):
                clock.advance_ns(self.STEP_NS)
                metrics.on_frame(clock.ns, 400)
                metrics.on_append(150_000)
                produced += 1
            if (second + 1) % flush_every_s == 0:
                flusher.flush_now()
        flusher.stop()  # the final flush is part of the contract
        return produced, _read(flusher.path)

    def test_interval_events_received_sums_to_the_fixture_count(self, tmp_path):
        produced, records = self._run(tmp_path)
        assert produced == self.FIXTURE_SECONDS * self.RATE == 150_000
        assert len(records) >= 3
        assert sum(r["events_received"] for r in records) == produced

    def test_the_one_second_ring_also_sums_to_the_fixture_count(self, tmp_path):
        """The ring is a SECOND, independent accounting of the same events.

        §7.4 keeps it because 10 s buckets "would smooth away the burst that
        matters" — but a burst series that does not reconcile with the counter
        is a burst series nobody should believe. Two independent paths agreeing
        is the whole point of keeping both."""
        produced, records = self._run(tmp_path)
        assert sum(sum(r["events_per_second"]) for r in records) == produced

    def test_the_ring_resolves_the_burst_a_ten_second_bucket_would_hide(
            self, tmp_path):
        """One 20,000-event second inside an otherwise idle interval."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for second in range(10):
            burst = 20_000 if second == 4 else 100
            step = 1_000_000_000 // (burst + 1)
            for _ in range(burst):
                clock.advance_ns(step)
                metrics.on_frame(clock.ns, 300)
            clock.advance_ns(1_000_000_000 - step * burst)
        flusher.stop()
        records = _read(flusher.path)
        series = [count for r in records for count in r["events_per_second"]]
        assert max(series) >= 19_000, series
        # The 10 s average over the same window is ~2,000/s — an order of
        # magnitude below the peak the constants have to survive.
        interval = records[0]
        average = interval["events_received"] * 1000 / max(
            1, interval["interval_wall_ms"])
        assert average < max(series) / 5

    def test_events_received_counts_frames_not_appends(self, tmp_path):
        """A dry run (§6.7) archives nothing and must still measure the rate."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(1_000):
            clock.advance_ns(self.STEP_NS)
            metrics.on_frame(clock.ns, 256)
        flusher.stop()
        record = _read(flusher.path)[0]
        assert record["events_received"] == 1_000
        assert record["events_archived"] == 0
        assert record["append_calls"] == 0

    def test_interval_wall_ms_is_measured_never_assumed(self, tmp_path):
        """§7.4: "a stalled flusher cannot inflate a rate"."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        clock.advance_ns(37_000_000_000)  # a 37 s interval, not a 10 s one
        for _ in range(500):
            clock.advance_ns(self.STEP_NS)
            metrics.on_frame(clock.ns, 128)
        flusher.flush_now()
        record = _read(flusher.path)[0]
        assert record["interval_wall_ms"] == 37_000 + (500 * 200_000) // 1_000_000
        assert record["interval_wall_ms"] != 10_000

    def test_intervals_are_contiguous_and_indexed(self, tmp_path):
        produced, records = self._run(tmp_path)
        assert [r["interval_index"] for r in records] == list(range(len(records)))
        for earlier, later in zip(records, records[1:]):
            assert later["interval_started_at"] == earlier["interval_ended_at"]
        assert len({r["session_id"] for r in records}) == 1


# ---------------------------------------------------------------------------------
# CP4 Verify, claim 2 — a ticker never appears in the output file
# ---------------------------------------------------------------------------------


class TestNoTickerEverReachesTheFile:
    """§7.2: market identity is high-cardinality and must not enter the
    telemetry directory. The guarantee here is structural — there is no field a
    ticker fits in — so these tests attack the entry points rather than the
    output filter."""

    def test_a_full_session_writes_no_ticker_byte(self, tmp_path):
        clock = FakeClock()
        metrics = CollectorMetrics(environment="demo",
                                   markets_subscribed=len(TICKERS))
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(5_000):
            clock.advance_ns(200_000)
            metrics.on_frame(clock.ns, 512)
            metrics.on_append(90_000)
        metrics.on_sequence_fault("gap")
        metrics.on_disconnect()
        metrics.on_reconnect(2)
        flusher.stop()

        raw = flusher.path.read_bytes()
        assert raw  # the test would pass vacuously on an empty file
        for ticker in TICKERS:
            assert ticker.encode() not in raw
            assert ticker.split("-")[0].encode() not in raw
        record = _read(flusher.path)[0]
        assert record["markets_subscribed"] == len(TICKERS)

    def test_the_constructor_refuses_tickers_where_a_count_belongs(self):
        """`len()` coercion is exactly the bug this refusal prevents: a ticker
        string would have been accepted as the integer 26."""
        for bad in (TICKERS, list(TICKERS), TICKER, 3.0, True, -1, None):
            with pytest.raises(MetricsValidationError):
                CollectorMetrics(environment="demo", markets_subscribed=bad)

    def test_no_field_in_the_record_accepts_a_ticker(self, tmp_path):
        """Every field, one at a time, replaced by a ticker string."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        metrics.on_frame(clock.ns, 100)
        record = flusher._build_record(final=True)
        validate_interval_record(record)  # the untampered record is valid
        for field in sorted(ALLOWED_FIELDS):
            poisoned = dict(record)
            poisoned[field] = TICKER
            with pytest.raises(MetricsValidationError):
                validate_interval_record(poisoned)

    def test_a_ticker_smuggled_as_a_new_field_is_refused(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        record = flusher._build_record(final=True)
        for field in ("market_tickers", "ticker", "markets", "subscription"):
            poisoned = dict(record)
            poisoned[field] = TICKER
            with pytest.raises(MetricsValidationError):
                validate_interval_record(poisoned)

    def test_the_writer_refuses_a_poisoned_record_and_writes_nothing(
            self, tmp_path):
        writer = IntervalWriter(Path(tmp_path) / INTERVAL_FILENAME)
        assert writer.write({"ticker": TICKER}) is False
        assert writer.rejected == 1
        assert writer.written == 0
        assert not writer.path.exists()

    def test_the_record_has_exactly_four_string_fields(self, tmp_path):
        """The structural claim, pinned. If a fifth string field is ever added
        this fails, and whoever adds it has to argue for it."""
        clock = FakeClock()
        flusher = _flusher(_metrics(), tmp_path, clock)
        record = flusher._build_record(final=True)
        strings = {k for k, v in record.items() if isinstance(v, str)}
        assert strings == {"session_id", "environment",
                           "interval_started_at", "interval_ended_at"}


# ---------------------------------------------------------------------------------
# CP4 Verify, claim 3 — no failure path raises into the loop
# ---------------------------------------------------------------------------------


class TestNoFailurePathReachesTheCollectorLoop:
    """§8.6 and §7.1: the measurement may lose itself, never the session.

    Each test injects a real fault and asserts three things: nothing raised,
    the loss was COUNTED, and the collector's own per-frame calls still work
    afterwards."""

    def _still_usable(self, metrics) -> None:
        before = metrics.events_received
        metrics.on_frame(time.monotonic_ns(), 128)
        metrics.on_append(50_000)
        metrics.on_append_rejected(10_000)
        metrics.on_frame_malformed()
        metrics.on_segment_closed(3_000_000)
        assert metrics.events_received == before + 1
        assert metrics.observe_errors == 0

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                        reason="root ignores directory permissions")
    def test_an_unwritable_directory_is_counted_not_raised(self, tmp_path):
        """THE injection CP4 names, in the only shape that actually bites.

        A 0500 directory the process OWNS is not unwritable to it: the writer
        inherits the sink's `os.chmod(parent, 0700)` re-enforcement — which
        exists so a pre-existing loose directory cannot stay loose — and that
        chmod repairs the very permission the naive injection removed. The real
        fault is therefore an unwritable PARENT, where neither the `mkdir` nor
        the `chmod` can run at all. Pinned in this shape so nobody later
        "fixes" the injection back into one that silently passes."""
        locked = Path(tmp_path) / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            clock = FakeClock()
            metrics = _metrics()
            target = locked / "telemetry" / INTERVAL_FILENAME
            flusher = MetricsFlusher(metrics, path=target, clock_ns=clock)
            metrics.on_frame(clock.ns, 100)
            assert flusher.flush_now() is False
            assert flusher.stop() is True
            assert metrics.metric_flush_drops >= 2
            assert flusher.writer.dropped >= 2
            assert flusher.writer.last_error in {"PermissionError", "OSError"}
            assert not target.exists()
            self._still_usable(metrics)
        finally:
            os.chmod(locked, 0o700)

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                        reason="root ignores directory permissions")
    def test_the_running_thread_survives_an_unwritable_directory(self, tmp_path):
        """The same fault, against the real thread on a real cadence."""
        locked = Path(tmp_path) / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            metrics = _metrics()
            flusher = MetricsFlusher(
                metrics, path=locked / "telemetry" / INTERVAL_FILENAME,
                flush_interval_s=0.01, sample_interval_s=0.005)
            assert flusher.start() is True
            deadline = time.monotonic() + 2.0
            while metrics.metric_flush_drops < 3 and time.monotonic() < deadline:
                metrics.on_frame(time.monotonic_ns(), 64)
                metrics.on_append(20_000)
                time.sleep(0.002)
            assert flusher.alive is True, "a failing write must not kill the thread"
            assert metrics.metric_flush_drops >= 3
            assert flusher.stop() is True
            assert flusher.thread_error is None
            self._still_usable(metrics)
        finally:
            os.chmod(locked, 0o700)

    def test_a_missing_parent_that_cannot_be_created_is_counted(self, tmp_path):
        """A regular file where the directory should be. Fails for root too."""
        blocker = Path(tmp_path) / "notadir"
        blocker.write_text("i am a file\n")
        metrics = _metrics()
        flusher = MetricsFlusher(metrics, path=blocker / INTERVAL_FILENAME,
                                 clock_ns=FakeClock())
        assert flusher.flush_now() is False
        assert metrics.metric_flush_drops == 1
        assert flusher.writer.dropped == 1
        self._still_usable(metrics)

    def test_a_symlinked_output_path_is_refused_not_followed(self, tmp_path):
        """The sink's `O_NOFOLLOW` reasoning, inherited verbatim: a planted
        symlink must fail the write rather than append JSON into whatever it
        points at."""
        target = Path(tmp_path) / "victim.db"
        target.write_bytes(b"SQLite format 3\x00")
        link = Path(tmp_path) / INTERVAL_FILENAME
        link.symlink_to(target)
        metrics = _metrics()
        flusher = MetricsFlusher(metrics, path=link, clock_ns=FakeClock())
        assert flusher.flush_now() is False
        assert target.read_bytes() == b"SQLite format 3\x00"
        assert metrics.metric_flush_drops == 1
        self._still_usable(metrics)

    def test_a_short_write_is_a_drop_and_is_never_resumed(self, tmp_path,
                                                          monkeypatch):
        writer = IntervalWriter(Path(tmp_path) / INTERVAL_FILENAME)
        real_write = os.write
        calls = []

        def short_write(fd, data):
            calls.append(len(data))
            return real_write(fd, data[: len(data) // 2])

        monkeypatch.setattr(os, "write", short_write)
        flusher = MetricsFlusher(_metrics(), writer=writer, clock_ns=FakeClock())
        assert flusher.flush_now() is False
        monkeypatch.undo()
        assert writer.dropped == 1
        assert writer.last_error == "short_write"
        assert len(calls) == 1, "a short write must never be retried"
        # The half line is left behind and the reader skips it rather than
        # yielding half a measurement.
        assert _read(writer.path) == []

    def test_a_source_that_raises_is_counted_and_reports_unavailable(
            self, tmp_path):
        """CP0 12.3: an `AttributeError` from the undocumented queue-depth chain
        after a `websockets` upgrade must never reach the loop, and must never
        be laundered into a 0."""
        clock = FakeClock()
        metrics = _metrics()

        def exploding_chain():
            raise AttributeError("'ClientConnection' object has no attribute "
                                 "'recv_messages'")

        metrics.bind_reader_lag(exploding_chain)
        metrics.bind_transport_counters(exploding_chain)
        metrics.bind_archive_state(exploding_chain)
        flusher = _flusher(metrics, tmp_path, clock)
        flusher._sample()
        assert flusher.flush_now() is True
        record = _read(flusher.path)[0]
        assert record["reader_lag_frames_max"] is None  # UNAVAILABLE, not 0
        assert record["reader_stall_ms_max"] == 0
        assert metrics.source_failures >= 3
        self._still_usable(metrics)

    def test_a_broken_record_build_is_counted_not_raised(self, tmp_path,
                                                         monkeypatch):
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)

        def boom():
            raise RuntimeError("histogram snapshot failed")

        monkeypatch.setattr(metrics, "histogram_snapshot", boom)
        assert flusher.flush_now() is False
        assert flusher.thread_error == "RuntimeError"
        assert metrics.metric_flush_drops == 1
        monkeypatch.undo()
        self._still_usable(metrics)

    def test_a_dying_thread_records_how_it_died_and_stop_still_writes(
            self, tmp_path, monkeypatch):
        """Thread death is a documented degradation, not a silent one — and the
        FINAL record is written on the caller's thread precisely so that a
        session whose flusher died an hour ago still ends with a record."""
        metrics = _metrics()
        flusher = MetricsFlusher(metrics, path=Path(tmp_path) / INTERVAL_FILENAME,
                                 flush_interval_s=5.0, sample_interval_s=0.005)

        def boom():
            raise RuntimeError("sampler exploded")

        monkeypatch.setattr(flusher, "_sample", boom)
        assert flusher.start() is True
        deadline = time.monotonic() + 2.0
        while flusher.alive and time.monotonic() < deadline:
            metrics.on_frame(time.monotonic_ns(), 64)
            time.sleep(0.002)
        assert flusher.alive is False
        assert flusher.thread_error == "RuntimeError"
        monkeypatch.undo()
        assert flusher.stop() is True
        records = _read(flusher.path)
        assert len(records) == 1
        assert records[0]["events_received"] > 0
        self._still_usable(metrics)

    def test_hot_path_calls_swallow_a_broken_internal_state(self):
        """Belt and braces: even a corrupted internal makes the loop no worse
        than uninstrumented."""
        metrics = _metrics()
        metrics._event_bytes = None  # corrupt the pre-allocated histogram
        metrics.on_frame(time.monotonic_ns(), 512)
        assert metrics.observe_errors == 1
        metrics._append_us = None
        metrics.on_append(1_000)
        metrics.on_append_rejected(1_000)
        assert metrics.observe_errors == 3

    def test_an_unclassifiable_sequence_fault_is_counted_not_ignored(self):
        metrics = _metrics()
        metrics.on_sequence_fault("teleported")
        assert metrics.observe_errors == 1
        assert (metrics.sequence_gaps == metrics.sequence_regressions
                == metrics.sequence_duplicates == 0)

    def test_a_double_start_is_refused_without_raising(self, tmp_path):
        flusher = MetricsFlusher(_metrics(),
                                 path=Path(tmp_path) / INTERVAL_FILENAME,
                                 flush_interval_s=5.0)
        assert flusher.start() is True
        assert flusher.start() is False
        assert flusher.stop() is True

    def test_metric_flush_drops_is_cumulative_and_survives_into_later_records(
            self, tmp_path):
        """A record cannot report its own loss, so the honesty field has to be
        a session total or it would always read 0."""
        blocker = Path(tmp_path) / "notadir"
        blocker.write_text("x")
        metrics = _metrics()
        clock = FakeClock()
        broken = MetricsFlusher(metrics, path=blocker / INTERVAL_FILENAME,
                                clock_ns=clock)
        assert broken.flush_now() is False
        assert broken.flush_now() is False
        working = _flusher(metrics, tmp_path, clock)
        metrics.on_frame(clock.ns, 64)
        assert working.flush_now() is True
        assert _read(working.path)[0]["metric_flush_drops"] == 2


# ---------------------------------------------------------------------------------
# The closed validator
# ---------------------------------------------------------------------------------


class TestTheClosedValidator:
    """Precedent: `app/telemetry/schema.py` — "an event carrying ANY other key
    is rejected". Same rule, separate schema, for the reasons in §5.4."""

    @pytest.fixture()
    def record(self, tmp_path):
        flusher = _flusher(_metrics(), tmp_path, FakeClock())
        return flusher._build_record(final=True)

    def test_a_built_record_validates(self, record):
        assert validate_interval_record(record) is record

    def test_an_unknown_field_is_a_refusal_not_a_passthrough(self, record):
        record["events_per_hour"] = 1
        with pytest.raises(MetricsValidationError, match="unknown"):
            validate_interval_record(record)

    def test_every_field_is_required(self, record):
        for field in sorted(ALLOWED_FIELDS):
            partial = dict(record)
            partial.pop(field)
            with pytest.raises(MetricsValidationError, match="missing"):
                validate_interval_record(partial)

    def test_a_non_dict_is_refused(self):
        for bad in (None, [], "record", 7):
            with pytest.raises(MetricsValidationError):
                validate_interval_record(bad)

    def test_the_schema_version_is_pinned(self, record):
        record["schema_version"] = 2
        with pytest.raises(MetricsValidationError, match="schema_version"):
            validate_interval_record(record)

    def test_the_environment_enum_is_the_kalshi_one(self, record):
        from app.realtime.kalshi import ENVIRONMENTS
        for env in ENVIRONMENTS:
            record["environment"] = env
            assert validate_interval_record(record)
        record["environment"] = "staging"
        with pytest.raises(MetricsValidationError, match="environment"):
            validate_interval_record(record)

    def test_counts_reject_bools_negatives_and_floats(self, record):
        for bad in (True, -1, 1.5, "3", None):
            poisoned = dict(record)
            poisoned["events_received"] = bad
            with pytest.raises(MetricsValidationError):
                validate_interval_record(poisoned)

    def test_reader_lag_is_the_one_nullable_count(self, record):
        record["reader_lag_frames_max"] = None
        assert validate_interval_record(record)
        record["reader_lag_frames_max"] = 0
        assert validate_interval_record(record)
        record["reader_lag_frames_max"] = -1
        with pytest.raises(MetricsValidationError):
            validate_interval_record(record)
        # ...and it is the ONLY one.
        poisoned = dict(record)
        poisoned["reader_lag_frames_max"] = 0
        poisoned["events_received"] = None
        with pytest.raises(MetricsValidationError):
            validate_interval_record(poisoned)

    def test_a_histogram_key_must_be_a_bucket_label(self, record):
        record["append_us_histogram"] = {TICKER: 1}
        with pytest.raises(MetricsValidationError, match="bucket label"):
            validate_interval_record(record)

    def test_a_histogram_is_bounded(self, record):
        record["append_us_histogram"] = {f"{i}-{i+1}": 1
                                         for i in range(MAX_HISTOGRAM_BUCKETS + 1)}
        with pytest.raises(MetricsValidationError, match="bounded"):
            validate_interval_record(record)

    def test_a_histogram_count_must_be_a_non_negative_int(self, record):
        for bad in (True, -1, "1", 1.0):
            poisoned = dict(record)
            poisoned["append_us_histogram"] = {"<50": bad}
            with pytest.raises(MetricsValidationError):
                validate_interval_record(poisoned)

    def test_the_session_id_format_is_pinned(self, record):
        for bad in ("session-1", "", "ABCDEF", "0" * 31, 7):
            poisoned = dict(record)
            poisoned["session_id"] = bad
            with pytest.raises(MetricsValidationError):
                validate_interval_record(poisoned)

    def test_timestamps_must_be_iso_z(self, record):
        for bad in ("2026-08-14 12:00:00", "2026-08-14T12:00:00+00:00",
                    "not-a-time", 0):
            poisoned = dict(record)
            poisoned["interval_started_at"] = bad
            with pytest.raises(MetricsValidationError):
                validate_interval_record(poisoned)

    def test_impossible_timing_is_refused(self, record):
        record["interval_started_at"] = "2026-08-14T12:00:01.000Z"
        record["interval_ended_at"] = "2026-08-14T12:00:00.000Z"
        with pytest.raises(MetricsValidationError, match="impossible timing"):
            validate_interval_record(record)

    def test_the_per_second_series_is_bounded_and_integer(self, record):
        record["events_per_second"] = [0] * (MAX_PER_SECOND_SLOTS + 1)
        with pytest.raises(MetricsValidationError):
            validate_interval_record(record)
        record["events_per_second"] = [1, -1]
        with pytest.raises(MetricsValidationError):
            validate_interval_record(record)
        record["events_per_second"] = [1, True]
        with pytest.raises(MetricsValidationError):
            validate_interval_record(record)

    def test_truncation_flag_is_a_strict_bool(self, record):
        record["events_per_second_truncated"] = 1
        with pytest.raises(MetricsValidationError):
            validate_interval_record(record)

    def test_the_bucket_label_convention_is_shared_with_the_sqlite_schema(self):
        """§7.2: "Bucket labels follow the existing `_HISTOGRAM_KEY_RE`
        convention so a later reader can share one parser." The pattern is
        duplicated rather than imported (a private name is not an API), so this
        assertion is what keeps the duplicate honest."""
        from app.telemetry import schema as shared

        assert HISTOGRAM_KEY_RE.pattern == shared._HISTOGRAM_KEY_RE.pattern
        assert MAX_HISTOGRAM_BUCKETS == shared.MAX_HISTOGRAM_BUCKETS
        for labels in (APPEND_US_LABELS, EVENT_BYTES_LABELS, CLOSE_MS_LABELS):
            assert len(labels) <= MAX_HISTOGRAM_BUCKETS
            for label in labels:
                assert shared._HISTOGRAM_KEY_RE.match(label), label


# ---------------------------------------------------------------------------------
# The two append histograms (§8.4)
# ---------------------------------------------------------------------------------


class TestTheTwoAppendHistograms:
    """§8.4 point 2: rotation cost lands on ONE append, and merging it into the
    ordinary distribution makes the session maximum unattributable."""

    def test_a_rotation_append_lands_in_the_second_histogram(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(100):
            metrics.on_append(120_000)          # 120 us, ordinary
        metrics.on_append(3_000_000, rotated=True)  # 3 ms, the rotation
        assert flusher.flush_now() is True
        record = _read(flusher.path)[0]
        assert record["append_us_histogram"] == {"100-200": 100}
        assert record["append_us_rotation_histogram"] == {"2000-5000": 1}
        assert record["rotations_started"] == 1
        assert record["append_calls"] == 101

    def test_the_rotation_sample_is_excluded_from_the_ordinary_distribution(
            self, tmp_path):
        """Without the split, one 3 ms sample in a 120 us population moves the
        top of the distribution by 25x and the retune would read it as append
        cost."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(1_000):
            metrics.on_append(120_000)
        for _ in range(5):
            metrics.on_append(3_000_000, rotated=True)
        flusher.flush_now()
        record = _read(flusher.path)[0]
        ordinary = record["append_us_histogram"]
        assert set(ordinary) == {"100-200"}
        assert sum(ordinary.values()) == 1_000
        assert sum(record["append_us_rotation_histogram"].values()) == 5

    def test_a_rejected_append_is_counted_apart_from_a_malformed_frame(
            self, tmp_path):
        """§7.4: "A single 'dropped' number would hide which layer failed"."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        metrics.on_append(100_000)
        metrics.on_append_rejected(90_000)
        metrics.on_frame_malformed()
        flusher.flush_now()
        record = _read(flusher.path)[0]
        assert record["events_archived"] == 1
        assert record["events_rejected"] == 1
        assert record["frames_malformed"] == 1
        assert "transport_dropped" not in record  # CP0 12.4 deleted it

    def test_append_latency_buckets_are_half_open_and_ordered(self):
        metrics = _metrics()
        samples = {
            0: "<50", 49_000: "<50", 50_000: "50-100", 199_000: "100-200",
            200_000: "200-500", 1_000_000_000: ">=1000000",
        }
        for elapsed_ns, label in samples.items():
            fresh = _metrics()
            fresh.on_append(elapsed_ns)
            index = APPEND_US_LABELS.index(label)
            assert fresh._append_us[index] == 1, (elapsed_ns, label)
        assert len(metrics._append_us) == len(APPEND_US_EDGES) + 1

    def test_append_us_max_is_taken_and_reset_per_interval(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        metrics.on_append(4_200_000)
        flusher.flush_now()
        metrics.on_append(90_000)
        flusher.flush_now()
        first, second = _read(flusher.path)
        assert first["append_us_max"] == 4_200
        assert second["append_us_max"] == 90

    def test_close_latency_is_recorded_off_the_producer_thread(self, tmp_path):
        """§7.4: the close-latency timing is taken on the CLOSER thread, so the
        producer pays nothing for it."""
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        done = threading.Event()

        def closer():
            metrics.on_segment_closed(2_497_000_000)  # the committed 13k figure
            metrics.on_segment_closed(14_800_000_000)  # the contended one
            done.set()

        thread = threading.Thread(target=closer)
        thread.start()
        thread.join(5)
        assert done.is_set()
        flusher.flush_now()
        record = _read(flusher.path)[0]
        assert record["segments_closed"] == 2
        assert record["segment_close_ms_max"] == 14_800
        assert record["segment_close_ms_histogram"] == {"1000-2500": 1,
                                                        "5000-15000": 1}


# ---------------------------------------------------------------------------------
# The 1 s ring
# ---------------------------------------------------------------------------------


class TestTheOneSecondRing:

    def test_the_ring_is_pre_allocated_and_never_grows(self):
        metrics = _metrics(ring_slots=64)
        assert len(metrics._ring) == 64 and len(metrics._ring_sec) == 64
        clock = FakeClock()
        for _ in range(10_000):
            clock.advance_ns(1_000_000)
            metrics.on_frame(clock.ns, 10)
        assert len(metrics._ring) == 64 and len(metrics._ring_sec) == 64

    def test_a_wrapped_slot_reads_zero_not_a_stale_count(self):
        """The stamp array is what makes an idle second and a lapped second the
        same answer — a stale count would invent a burst that never happened."""
        metrics = _metrics(ring_slots=4)
        clock = FakeClock(start_ns=0)
        for second in range(4):
            clock.ns = second * 1_000_000_000
            for _ in range(second + 1):
                metrics.on_frame(clock.ns, 1)
        assert metrics.per_second_counts(0, 4) == [1, 2, 3, 4]
        # one full lap later, nothing has been written into the ring
        assert metrics.per_second_counts(4, 8) == [0, 0, 0, 0]

    def test_only_completed_seconds_are_reported_until_the_final_flush(
            self, tmp_path):
        clock = FakeClock(start_ns=10 * 1_000_000_000)
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(7):
            metrics.on_frame(clock.ns, 32)
        flusher.flush_now()  # the in-flight second is NOT reported yet
        assert _read(flusher.path)[0]["events_per_second"] == []
        flusher.flush_now(final=True)
        assert _read(flusher.path)[1]["events_per_second"] == [7]

    def test_a_long_stall_truncates_the_series_and_says_so(self, tmp_path):
        clock = FakeClock(start_ns=0)
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        clock.advance_ns((MAX_PER_SECOND_SLOTS + 50) * 1_000_000_000)
        metrics.on_frame(clock.ns, 32)
        flusher.flush_now(final=True)
        record = _read(flusher.path)[0]
        assert record["events_per_second_truncated"] is True
        assert len(record["events_per_second"]) == MAX_PER_SECOND_SLOTS
        # the counter is unaffected by the series being truncated
        assert record["events_received"] == 1

    def test_the_ring_uses_the_callers_clock_reading(self):
        """§7.1 permits two `monotonic_ns()` calls per frame, around `append`
        only. The ring must therefore ride the reading the collector already
        took, not take a third."""
        metrics = _metrics()
        metrics.on_frame(5 * 1_000_000_000, 10)
        assert metrics.per_second_counts(5, 6) == [1]
        assert metrics.per_second_counts(4, 5) == [0]


# ---------------------------------------------------------------------------------
# The hot path's cost, structurally
# ---------------------------------------------------------------------------------


class TestTheHotPathDoesNoIO:
    """§7.1's forbidden list, pinned as bytecode rather than as a comment.

    CP5 is the numeric overhead gate; this is the structural one, and it is the
    cheaper place to catch a regression — an `os.write` added to `on_frame`
    fails here in milliseconds instead of showing up as a 30% throughput loss
    in a benchmark nobody reruns."""

    HOT = ("on_frame", "on_append", "on_append_rejected", "on_frame_malformed",
           "on_sequence_fault", "on_disconnect", "on_reconnect",
           "on_segment_closed")
    FORBIDDEN = {"json", "dumps", "os", "write", "open", "format", "encode",
                 "acquire", "Lock", "print", "logging", "sleep", "monotonic",
                 "monotonic_ns", "now", "utcnow", "_utcnow_iso"}

    def test_no_hot_path_method_names_an_io_or_clock_symbol(self):
        for name in self.HOT:
            code = getattr(CollectorMetrics, name).__code__
            leaked = set(code.co_names) & self.FORBIDDEN
            assert not leaked, f"{name} reaches {sorted(leaked)}"

    def test_no_hot_path_method_takes_a_lock(self):
        for name in self.HOT:
            source_names = getattr(CollectorMetrics, name).__code__.co_names
            assert "_lock" not in source_names
            assert "_flush_lock" not in source_names

    def test_the_module_opens_no_database_session(self):
        """§10.2's rule for the collector, applied to its measurement lane."""
        source = Path("app/realtime/collector_metrics.py").read_text()
        for banned in ("get_sessionmaker", "sqlalchemy", "SessionLocal",
                       "app.db", "app.models"):
            assert banned not in source

    def test_a_frame_costs_a_bounded_number_of_allocations(self):
        """The list-growth guard: `events_received` events must not produce
        `events_received` objects."""
        import gc
        metrics = _metrics()
        gc.collect()
        before = len(gc.get_objects())
        clock = FakeClock()
        for _ in range(20_000):
            clock.advance_ns(200_000)
            metrics.on_frame(clock.ns, 512)
            metrics.on_append(150_000)
        gc.collect()
        after = len(gc.get_objects())
        assert after - before < 1_000, (before, after)


# ---------------------------------------------------------------------------------
# The null lane (CP5's other half)
# ---------------------------------------------------------------------------------


class TestTheNullLane:

    def test_null_metrics_implements_every_call_the_loop_makes(self):
        for name in TestTheHotPathDoesNoIO.HOT + (
                "bind_reader_lag", "bind_transport_counters",
                "bind_archive_state"):
            assert callable(getattr(NULL_METRICS, name)), name

    def test_null_metrics_does_nothing_and_returns_nothing(self):
        assert NULL_METRICS.on_frame(time.monotonic_ns(), 512) is None
        assert NULL_METRICS.on_append(1_000, rotated=True) is None
        assert NULL_METRICS.on_sequence_fault("gap") is None
        assert NULL_METRICS.enabled is False
        assert CollectorMetrics.enabled is True

    def test_the_null_lane_is_a_separate_type_not_a_flagged_one(self):
        assert isinstance(NULL_METRICS, NullCollectorMetrics)
        assert not isinstance(NULL_METRICS, CollectorMetrics)


# ---------------------------------------------------------------------------------
# The writer and the reader
# ---------------------------------------------------------------------------------


class TestTheIntervalWriter:

    def test_the_file_is_beside_the_shared_sink_not_inside_it(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.setenv("SQLITE_TELEMETRY_DIR", str(tmp_path))
        assert interval_path() == Path(tmp_path) / INTERVAL_FILENAME
        assert INTERVAL_FILENAME != "sqlite-writes.jsonl"

    def test_the_directory_and_file_modes_match_the_sink(self, tmp_path):
        target = Path(tmp_path) / "nested" / INTERVAL_FILENAME
        writer = IntervalWriter(target)
        flusher = MetricsFlusher(_metrics(), writer=writer, clock_ns=FakeClock())
        assert flusher.flush_now() is True
        assert oct(target.parent.stat().st_mode & 0o777) == "0o700"
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_a_loose_pre_existing_directory_is_re_tightened(self, tmp_path):
        loose = Path(tmp_path) / "loose"
        loose.mkdir(mode=0o777)
        os.chmod(loose, 0o777)
        writer = IntervalWriter(loose / INTERVAL_FILENAME)
        flusher = MetricsFlusher(_metrics(), writer=writer, clock_ns=FakeClock())
        assert flusher.flush_now() is True
        assert oct(loose.stat().st_mode & 0o777) == "0o700"

    def test_one_record_is_one_line(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(5):
            metrics.on_frame(clock.ns, 100)
            clock.advance_ns(1_000_000_000)
            flusher.flush_now()
        raw = flusher.path.read_bytes()
        assert raw.count(b"\n") == 5
        assert len(_read(flusher.path)) == 5

    def test_every_line_fits_the_cap_at_full_size(self, tmp_path):
        """A worst-case record: every histogram bucket populated and a full
        per-second series."""
        clock = FakeClock(start_ns=0)
        metrics = _metrics()
        flusher = _flusher(metrics, tmp_path, clock)
        for edge in APPEND_US_EDGES:
            metrics.on_append(edge * 1_000)
            metrics.on_append(edge * 1_000, rotated=True)
        for second in range(MAX_PER_SECOND_SLOTS - 1):
            clock.ns = second * 1_000_000_000
            for _ in range(9):
                metrics.on_frame(clock.ns, 999_999)
        clock.ns = (MAX_PER_SECOND_SLOTS - 1) * 1_000_000_000
        assert flusher.flush_now(final=True) is True
        line = flusher.path.read_bytes()
        assert len(line) <= MAX_LINE_BYTES, len(line)
        record = _read(flusher.path)[0]
        assert len(record["events_per_second"]) == MAX_PER_SECOND_SLOTS
        assert record["events_per_second_truncated"] is False

    def test_an_oversize_record_sheds_the_series_before_it_is_dropped(self,
                                                                      tmp_path):
        writer = IntervalWriter(Path(tmp_path) / INTERVAL_FILENAME)
        flusher = MetricsFlusher(_metrics(), writer=writer, clock_ns=FakeClock())
        record = flusher._build_record(final=True)
        record["events_per_second"] = [999_999] * MAX_PER_SECOND_SLOTS
        # Force the shed path by shrinking the cap for one call.
        import app.realtime.collector_metrics as module
        original = module.MAX_LINE_BYTES
        module.MAX_LINE_BYTES = 900
        try:
            assert writer.write(record) is True
        finally:
            module.MAX_LINE_BYTES = original
        written = _read(writer.path)[0]
        assert written["events_per_second"] == []
        assert written["events_per_second_truncated"] is True

    def test_the_reader_streams_and_skips_a_corrupt_line(self, tmp_path):
        import inspect
        assert inspect.isgeneratorfunction(iter_interval_records)
        path = Path(tmp_path) / INTERVAL_FILENAME
        clock = FakeClock()
        metrics = _metrics()
        flusher = MetricsFlusher(metrics, path=path, clock_ns=clock)
        metrics.on_frame(clock.ns, 64)
        flusher.flush_now()
        with path.open("a") as handle:
            handle.write("{not json\n")
            handle.write(json.dumps({"schema_version": 1}) + "\n")
        clock.advance_ns(1_000_000_000)
        flusher.flush_now()
        records = _read(path)
        assert len(records) == 2
        assert [r["interval_index"] for r in records] == [0, 1]

    def test_the_reader_on_a_missing_file_yields_nothing(self, tmp_path):
        assert _read(Path(tmp_path) / "absent.jsonl") == []


# ---------------------------------------------------------------------------------
# Bound sources (what the orchestrator supplies)
# ---------------------------------------------------------------------------------


class TestBoundSources:

    def test_transport_counters_supply_malformed_and_stall(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        counters = {"frames_malformed": 0, "reader_stall_ms_max": 0}
        metrics.bind_transport_counters(lambda: dict(counters))
        flusher = _flusher(metrics, tmp_path, clock)

        counters["frames_malformed"] = 3
        counters["reader_stall_ms_max"] = 12
        flusher.flush_now()
        counters["frames_malformed"] = 5
        counters["reader_stall_ms_max"] = 900
        flusher.flush_now()

        first, second = _read(flusher.path)
        assert first["frames_malformed"] == 3
        # DELTA, not the running total re-reported.
        assert second["frames_malformed"] == 2
        # The stall is a session watermark and is carried as-is.
        assert (first["reader_stall_ms_max"], second["reader_stall_ms_max"]) == (12, 900)

    def test_reader_lag_is_a_sampled_peak_not_a_flush_time_reading(self,
                                                                   tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        depths = iter([1, 2, 47, 3, 0])
        metrics.bind_reader_lag(lambda: next(depths, 0))
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(5):
            flusher._sample()
        flusher.flush_now()
        record = _read(flusher.path)[0]
        assert record["reader_lag_frames_max"] == 47

    def test_an_unbound_reader_lag_reports_unavailable(self, tmp_path):
        flusher = _flusher(_metrics(), tmp_path, FakeClock())
        flusher._sample()
        flusher.flush_now()
        assert _read(flusher.path)[0]["reader_lag_frames_max"] is None

    def test_closer_outstanding_is_a_sampled_peak(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        outstanding = iter([0, 1, 2, 1, 0])
        metrics.bind_archive_state(
            lambda: {"rotation_failures": 0,
                     "closer_outstanding": next(outstanding, 0)})
        flusher = _flusher(metrics, tmp_path, clock)
        for _ in range(5):
            flusher._sample()
        flusher.flush_now()
        record = _read(flusher.path)[0]
        assert record["closer_outstanding_max"] == 2
        # reset for the next interval
        flusher.flush_now()
        assert _read(flusher.path)[1]["closer_outstanding_max"] == 0

    def test_rotation_failures_are_surfaced(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        metrics.bind_archive_state(
            lambda: {"rotation_failures": 2, "closer_outstanding": 0})
        flusher = _flusher(metrics, tmp_path, clock)
        flusher.flush_now()
        assert _read(flusher.path)[0]["rotation_failures"] == 2

    def test_a_source_returning_nonsense_is_ignored_not_recorded(self, tmp_path):
        clock = FakeClock()
        metrics = _metrics()
        metrics.bind_transport_counters(lambda: "not a dict")
        metrics.bind_reader_lag(lambda: "seventeen")
        metrics.bind_archive_state(lambda: {"closer_outstanding": -1})
        flusher = _flusher(metrics, tmp_path, clock)
        flusher._sample()
        assert flusher.flush_now() is True
        record = _read(flusher.path)[0]
        assert record["reader_lag_frames_max"] is None
        assert record["reader_stall_ms_max"] == 0
        assert record["closer_outstanding_max"] == 0


# ---------------------------------------------------------------------------------
# The thread, end to end
# ---------------------------------------------------------------------------------


class TestTheFlusherThread:

    def test_a_real_threaded_session_writes_intervals_and_conserves_events(
            self, tmp_path):
        metrics = _metrics()
        flusher = MetricsFlusher(metrics, path=Path(tmp_path) / INTERVAL_FILENAME,
                                 flush_interval_s=0.05, sample_interval_s=0.01)
        assert flusher.start() is True
        produced = 0
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            metrics.on_frame(time.monotonic_ns(), 256)
            metrics.on_append(80_000)
            produced += 1
        assert flusher.stop() is True
        records = _read(flusher.path)
        assert len(records) >= 3, len(records)
        assert sum(r["events_received"] for r in records) == produced
        assert sum(r["events_archived"] for r in records) == produced
        assert metrics.metric_flush_drops == 0
        assert flusher.thread_error is None

    def test_the_context_manager_starts_and_stops(self, tmp_path):
        metrics = _metrics()
        with MetricsFlusher(metrics, path=Path(tmp_path) / INTERVAL_FILENAME,
                            flush_interval_s=5.0) as flusher:
            assert flusher.alive is True
            metrics.on_frame(time.monotonic_ns(), 64)
        assert flusher.alive is False
        assert len(_read(flusher.path)) == 1

    def test_the_thread_is_a_daemon_and_named(self, tmp_path):
        flusher = MetricsFlusher(_metrics(),
                                 path=Path(tmp_path) / INTERVAL_FILENAME,
                                 flush_interval_s=5.0)
        flusher.start()
        try:
            assert flusher._thread.daemon is True
            assert flusher._thread.name == "kalshi-tape-metrics"
        finally:
            flusher.stop()

    def test_stop_before_start_is_safe(self, tmp_path):
        flusher = MetricsFlusher(_metrics(),
                                 path=Path(tmp_path) / INTERVAL_FILENAME,
                                 clock_ns=FakeClock())
        assert flusher.stop() is True
        assert len(_read(flusher.path)) == 1
