"""KALSHI-TAPE-CLOSE-CALLBACK — the archive's close-observation seam, proven.

CP3.5 wired eight of the nine `CollectorMetrics` methods and left one:
`on_segment_closed`. It could not be wired from `collector.py`, because the
producer thread never runs a close and timing one from there would mean
reaching into `archive._closer`. So `segments_closed`,
`segment_close_ms_histogram` and `segment_close_ms_max` were structurally
zero — and `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` is a constant chosen to
target a **~2 second close**, which the qualification session was supposed to
retune against a measured rate it had no way to measure.

This file is the seam's proof, and it is written against the failure modes
rather than against the code:

1. **ANTI-VACUITY.** A real segment closes, the real `CollectorMetrics`
   receives exactly one close observation — and breaking the callback makes
   these tests FAIL. A test that passes when the wiring is absent certifies
   nothing; that is precisely how CP4 shipped 1,186 unreachable lines.
2. **CONTAINMENT.** An observer that raises on every close produces an archive
   whose committed bytes and checksums are IDENTICAL to a control run's. Not
   "the session survived" — identical commitment. CP3.5 set that standard for
   the frame path.
3. **MULTIPLICITY.** Exactly once per close. Not zero, not two — including
   across the idempotent re-close that `EventArchive.close()`'s drain-timeout
   path can reach.
4. **ABSENCE.** No callback means the archive of today, proven by byte
   equality against the pre-seam close path, not asserted.
5. **THE MEASUREMENT EXCLUDES THE OBSERVER.** A deliberately slow observer
   must not inflate the `close_ns` it is being handed. Telemetry inside the
   interval it measures reports its own cost as the cost of the thing.

**No test in this file opens a socket.**
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import archive_head as ah
from app.realtime import collector as kc
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt
from app.realtime.archive import EventArchive
from app.realtime.book import EventEnvelope
from app.realtime.collector_metrics import (
    CollectorMetrics,
    MetricsFlusher,
    iter_interval_records,
)
from app.realtime.segment import EVENTS_FILENAME, MANIFEST_FILENAME, SegmentState

REPO = Path(kc.__file__).resolve().parent.parent.parent
ARCHIVE_PATH = REPO / "app" / "realtime" / "archive.py"
ARCHIVE_SRC = ast.parse(ARCHIVE_PATH.read_text())
COLLECTOR_SRC = ast.parse((REPO / "app" / "realtime" / "collector.py").read_text())

ENV = "demo"
M1 = "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1"

# Everything the manifest says about WHAT was committed, as opposed to WHEN.
# `opened_at`/`closed_at` are wall clocks and `manifest_digest` covers them, so
# they cannot be equal across two runs and are excluded deliberately rather
# than by omission. What remains is the whole of the content identity: the
# record count, both chain endpoints, the ordered stream digest, the file's
# size, its sha256, and the close verdict.
COMMITMENT_FIELDS = (
    "segment_id", "record_count", "first_record_digest", "last_record_digest",
    "ordered_stream_digest", "event_file_size_bytes", "event_file_sha256",
    "close_status", "previous_segment_digest", "environment",
    "partition_identity",
)


@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - a call is the failure
        raise AssertionError("KALSHI-TAPE-CLOSE-CALLBACK opened a connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


class _FrozenGzipClock:
    """gzip stamps `time.time()` into every member header.

    Two runs a second apart therefore produce different FILE bytes for
    identical content, which would make `event_file_sha256` a clock
    measurement rather than a content checksum — and the containment proof
    would then be either flaky or forced to drop the one field that is
    literally a checksum of the committed file. Freezing the clock the gzip
    module reads (and only that one) makes the comparison what it claims to
    be: the same bytes, or not.
    """

    @staticmethod
    def time() -> float:
        return 1_786_000_000.0


@pytest.fixture
def frozen_gzip_clock(monkeypatch):
    monkeypatch.setattr(gzip, "time", _FrozenGzipClock)
    yield


# --- deterministic evidence ---------------------------------------------------------
def envelope(seq: int, *, hour: int = 0) -> EventEnvelope:
    """Byte-for-byte reproducible across runs: every field is pinned."""
    stamp = f"2026-08-14T{hour:02d}:00:00.000000Z"
    return EventEnvelope(
        schema_version=2, venue="kalshi", environment=ENV, channel="ticker",
        event_type="ticker", market_ticker=M1, market_id="m-1", sid=4, seq=seq,
        venue_time=stamp, collector_receive_time=stamp,
        normalization_time=stamp, receive_monotonic_ns=1_000 + seq,
        normalize_monotonic_ns=2_000 + seq, data_age_us=5,
        implementation_version="kalshi-tape-close-callback/test",
        connection_generation=1, subscription_generation=1,
        raw={"seq": seq}, normalized={"seq": seq})


def init_archive(root: Path):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def drive_archive(root: Path, *, observer=None, events: int = 6,
                  max_records: int = 2, **kwargs):
    """A REAL archive committing REAL segments. Two rotations and one final
    close at the defaults, so both close paths — the closer thread and
    `EventArchive.close()`'s inline commit — are exercised in one run."""
    init_archive(root)
    archive = EventArchive(root, environment=ENV,
                           max_segment_records=max_records,
                           on_segment_closed=observer, **kwargs)
    for seq in range(1, events + 1):
        archive.append(envelope(seq))
    assert archive.wait_for_rotations(60.0)
    manifests = archive.close()
    return archive, manifests


def segment_dirs(root: Path) -> list[Path]:
    return sorted((root / f"env={ENV}").glob("segment=*"))


def committed_state(root: Path) -> list[dict]:
    """The committed evidence, as bytes and as the archive's own checksums."""
    out = []
    for directory in segment_dirs(root):
        raw = (directory / EVENTS_FILENAME).read_bytes()
        manifest = json.loads((directory / MANIFEST_FILENAME).read_text())
        out.append({
            "file_bytes": raw,
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "record_bytes": gzip.decompress(raw),
            "commitment": {k: manifest[k] for k in COMMITMENT_FIELDS},
        })
    return out


def replay_state(root: Path) -> tuple:
    archive = EventArchive(root, environment=ENV)
    records = archive.read_verified()
    report = ar.replay(records)
    return (archive.verify()["verdict"], len(records), report["checksums"],
            report["events_applied"])


# --- a real collector session -------------------------------------------------------
def snapshot(*, sid=4, seq=1):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "msg": {"market_ticker": M1, "ts_ms": 1786150148065,
                    "yes_dollars_fp": [["0.4700", "5.00"]],
                    "no_dollars_fp": [["0.5100", "5.00"]]}}


def delta(*, sid=4, seq=2, price="0.5100"):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": M1, "price_dollars": price,
                    "delta_fp": "201.00", "side": "no", "ts_ms": 1786150148066}}


def ticker_frame(*, sid=4, seq=3):
    return {"type": "ticker", "sid": sid, "seq": seq,
            "msg": {"market_ticker": M1, "yes_bid_dollars": "0.4700",
                    "yes_ask_dollars": "0.5100", "ts_ms": 1786150148067}}


FULL_SESSION = [snapshot(), delta(), ticker_frame()]
ALL_CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")


class _Factory:
    def __init__(self, frames):
        self.frames = list(frames)

    def __call__(self):
        return kx.FixtureTransport(self.frames)


def run_session(root: Path, metrics, *, flusher=None):
    config = kc.CollectorConfig(
        environment=ENV, archive_root=root, market_tickers=(M1,),
        channels=ALL_CHANNELS, max_seconds=60, max_events=1000,
        max_reconnects=0, reconnect_backoff_base_s=0.0)
    return kc.collect_once(config, transport_factory=_Factory(FULL_SESSION),
                           metrics=metrics, flusher=flusher)


# =====================================================================================
# PROOF 1 — anti-vacuity: a REAL close reaches the REAL metrics lane
# =====================================================================================
class TestAntiVacuity:
    def test_1_a_real_session_moves_the_real_close_counters(self, tmp_path):
        """The whole lane, driven by a real collector session.

        Nothing is injected but the transport. Before this milestone every
        assertion below read zero, and CP4's 81 green tests could not see it —
        which is why test 3 immediately breaks the callback and proves this
        test goes red when it is gone.
        """
        init_archive(tmp_path)
        metrics = CollectorMetrics(environment=ENV, markets_subscribed=1)
        result = run_session(tmp_path, metrics)

        assert result.status == kc.STATUS_OK
        # The session's own independent count of what it committed.
        assert result.segments_committed == 1
        assert metrics.segments_closed == result.segments_committed
        # The distribution, not merely the scalar: a counter that moves while
        # the histogram stays empty is a half-wired seam.
        assert sum(metrics.histogram_snapshot()["close_ms"]) == 1
        assert metrics.segment_close_ms_max >= 0
        assert metrics.observe_errors == 0
        assert result.metrics_errors == 0
        # And the archive it measured is real, committed evidence.
        verdict, records, _, applied = replay_state(tmp_path)
        assert verdict == "VALID"
        assert records == len(FULL_SESSION) == result.events_archived
        assert applied >= 1

    def test_2_the_close_lane_reaches_a_validated_interval_record(self, tmp_path):
        """collector -> archive closer -> CollectorMetrics -> MetricsFlusher ->
        a VALIDATED line on disk. §7.4's "archive close latency" row, end to
        end, is the deliverable — not the counter."""
        init_archive(tmp_path)
        metrics = CollectorMetrics(environment=ENV, markets_subscribed=1)
        path = tmp_path / "telemetry" / "kalshi-live-tape.jsonl"
        flusher = MetricsFlusher(metrics, path=path, flush_interval_s=60.0,
                                 sample_interval_s=0.02)
        result = run_session(tmp_path, metrics, flusher=flusher)

        assert result.status == kc.STATUS_OK
        records = list(iter_interval_records(path))
        assert len(records) == 1, records
        record = records[0]
        assert record["segments_closed"] == 1
        assert sum(record["segment_close_ms_histogram"].values()) == 1
        assert record["segment_close_ms_max"] >= 0
        assert flusher.thread_error is None

    def test_3_breaking_the_callback_makes_test_1_fail(self, tmp_path,
                                                       monkeypatch):
        """THE ANTI-VACUITY GUARD ITSELF.

        The same session, with the archive's notification broken — which is
        what "the wiring was never added" looks like from the metrics lane.
        Every close-related assertion in tests 1 and 2 must go to zero, and
        everything else must be unaffected: if this test cannot tell the two
        worlds apart, tests 1 and 2 are certifying nothing.
        """
        monkeypatch.setattr(ar.EventArchive, "_notify_segment_closed",
                            lambda self, close_ns: None)
        init_archive(tmp_path)
        metrics = CollectorMetrics(environment=ENV, markets_subscribed=1)
        result = run_session(tmp_path, metrics)

        assert result.status == kc.STATUS_OK
        assert result.segments_committed == 1          # the close still HAPPENED
        assert metrics.segments_closed == 0            # test 1 asserts == 1
        assert sum(metrics.histogram_snapshot()["close_ms"]) == 0
        assert metrics.segment_close_ms_max == 0
        assert result.events_archived == len(FULL_SESSION)

    def test_4_the_collector_passes_the_seam_and_never_times_a_close(self):
        """Structural guard, at identifier level.

        CP3.5's test 6 pins that `collector.py` NAMES every seam method. This
        one pins the two halves that test cannot see: the collector hands the
        archive the callback by KEYWORD (a positional would break silently on
        the next parameter added), and it does not reach into the private
        closer to time a close itself.

        Anti-vacuity: a keyword that does not exist and a method that does not
        exist are both asserted absent, so a scan that finds nothing at all
        cannot pass.
        """
        keywords, attributes = set(), set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Call):
                keywords |= {kw.arg for kw in node.keywords if kw.arg}
            if isinstance(node, ast.Attribute):
                attributes.add(node.attr)
        assert "on_segment_closed" in keywords
        assert "environment" in keywords                    # anti-vacuity
        assert "on_segment_closed_totally_invented" not in keywords
        # The collector holds no timer for a close and no reference to the
        # closer's queue. `_closer` appears exactly once, for §7.3's
        # `closer_outstanding` gauge, which CP3.5 already reviewed.
        assert "_timed_close" not in attributes
        assert "_notify_segment_closed" not in attributes

    def test_5_the_archive_never_imports_the_collector_or_its_metrics(self):
        """The dependency direction, asserted in the file that could break it.

        The seam is a callable passed IN. `archive.py` importing
        `collector_metrics` — or the collector — to "just call the method"
        would invert §6.1's one-way arrow and make the evidence store depend
        on the telemetry lane.

        Anti-vacuity: the imports the archive is SUPPOSED to have are asserted
        present, so an unparsed or gutted module cannot satisfy the bans.
        """
        mods = set()
        for node in ast.walk(ARCHIVE_SRC):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        assert "app.realtime.collector" not in mods
        assert "app.realtime.collector_metrics" not in mods
        assert "app.telemetry.sink" not in mods
        for banned in ("sqlalchemy", "app.db", "app.models", "app.services"):
            assert not any(m.startswith(banned) for m in mods), banned
        # Anti-vacuity: the permitted things exist.
        assert {"app.realtime.segment", "app.realtime.archive_head",
                "app.realtime.canonical"} <= mods
        # And no telemetry vocabulary crossed the boundary with the callback.
        # IDENTIFIER level, not substring: the module is allowed — required,
        # really — to explain in prose which lane consumes this and why the
        # thread ownership matters. What it may not do is CONTAIN a metric.
        identifiers = set()
        for node in ast.walk(ARCHIVE_SRC):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                identifiers.add(node.name)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
        for banned in ("CollectorMetrics", "segments_closed", "histogram",
                       "close_ms", "observe_errors", "interval_record"):
            assert banned not in identifiers, banned
        # Anti-vacuity: the seam's own identifiers ARE there.
        assert {"_timed_close", "_notify_segment_closed",
                "on_segment_closed", "close_ns"} <= identifiers

    def test_5a_the_callback_is_typed_and_takes_no_varargs(self):
        """"No generic `*args`/`**kwargs`, reflection, or telemetry-specific
        logic in the archive layer" — pinned where it could regress.

        The observer is invoked with exactly ONE positional argument and no
        keywords, and the archive never reaches the lane by name.
        """
        func = next(n for n in ast.walk(ARCHIVE_SRC)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_notify_segment_closed")
        invocations = [n for n in ast.walk(func)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "observer"]
        assert len(invocations) == 1
        call = invocations[0]
        assert len(call.args) == 1 and not call.keywords
        assert not any(isinstance(a, ast.Starred) for a in call.args)
        # No reflection anywhere near the seam.
        for node in ast.walk(ARCHIVE_SRC):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("eval", "exec", "__import__"), \
                    node.func.id
        # `getattr` is used exactly once in this module, for a documented
        # read-back on `read_segment_records`, and never on the seam.
        seam = next(n for n in ast.walk(ARCHIVE_SRC)
                    if isinstance(n, ast.FunctionDef) and n.name == "_timed_close")
        assert not [n for n in ast.walk(seam)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in ("getattr", "setattr", "hasattr")]


# =====================================================================================
# PROOF 2 — a hostile observer changes NOTHING about the commitment
# =====================================================================================
class TestContainment:
    def test_6_an_observer_that_always_raises_commits_identical_bytes(
            self, tmp_path, frozen_gzip_clock):
        """Byte-identical commitment, not "the session survived".

        Control: no observer. Hostile: an observer that raises on EVERY close,
        on the closer thread and on the inline commit alike. The archives must
        be indistinguishable — same file bytes, same sha256, same manifest
        commitment fields, same replay checksums.
        """
        control_root, hostile_root = tmp_path / "control", tmp_path / "hostile"
        control_root.mkdir()
        hostile_root.mkdir()

        raised = []

        def hostile(close_ns):
            raised.append(close_ns)
            raise RuntimeError("the metrics lane is on fire")

        control, control_manifests = drive_archive(control_root, observer=None)
        archive, manifests = drive_archive(hostile_root, observer=hostile)

        # The observer really did run, and really did fail, on every close.
        assert len(raised) == 3 == archive.rotations + len(manifests)
        assert archive.segment_close_observer_errors == 3
        # Nothing about the close was altered.
        assert archive.rotations == control.rotations == 2
        assert archive.rotation_failures == [] == control.rotation_failures
        assert len(manifests) == len(control_manifests) == 1
        assert committed_state(hostile_root) == committed_state(control_root)
        assert replay_state(hostile_root) == replay_state(control_root)

    def test_7_a_hostile_observer_does_not_end_the_session(self, tmp_path):
        """The collector-level statement of the same property: a metrics lane
        that fails cannot decide whether a tape is committed. The failure is
        COUNTED (`metrics_errors`), never silent."""
        init_archive(tmp_path)

        class Exploding(CollectorMetrics):
            def on_segment_closed(self, elapsed_ns):
                raise RuntimeError("boom")

        metrics = Exploding(environment=ENV, markets_subscribed=1)
        result = run_session(tmp_path, metrics)

        assert result.status == kc.STATUS_OK
        assert result.segments_committed == 1
        assert result.events_archived == len(FULL_SESSION)
        assert result.metrics_errors == 1            # counted, not swallowed
        assert metrics.segments_closed == 0          # the observation IS gone
        verdict, records, _, _ = replay_state(tmp_path)
        assert verdict == "VALID"
        assert records == len(FULL_SESSION)

    def test_8_an_observer_that_raises_baseexception_is_contained(self, tmp_path):
        """`except Exception` would let a `KeyboardInterrupt` raised by the
        observer escape into `EventArchive.close()`'s per-writer loop and skip
        every segment after it — healthy evidence left uncommitted by a
        telemetry failure. The trade (an interrupt absorbed one close early)
        is stated in `_notify_segment_closed`; this pins the behaviour."""
        init_archive(tmp_path)

        def hostile(close_ns):
            raise KeyboardInterrupt("Ctrl-C inside the observer")

        archive = EventArchive(tmp_path, environment=ENV,
                               max_segment_records=2, on_segment_closed=hostile)
        for seq in range(1, 7):
            archive.append(envelope(seq))
        assert archive.wait_for_rotations(60.0)
        manifests = archive.close()

        assert archive.segment_close_observer_errors == 3
        assert len(manifests) == 1
        assert archive.rotation_failures == []
        assert replay_state(tmp_path)[0] == "VALID"

    def test_9_a_non_callable_observer_is_refused_at_construction(self, tmp_path):
        """Fail at the constructor, not on the closer thread six hours in."""
        init_archive(tmp_path)
        with pytest.raises(ar.ArchiveError, match="on_segment_closed"):
            EventArchive(tmp_path, environment=ENV, on_segment_closed="metrics")


# =====================================================================================
# PROOF 3 — exactly once per close: not zero, not two
# =====================================================================================
class TestExactlyOnce:
    def test_10_one_observation_per_committed_segment(self, tmp_path):
        """Counted against DISK, not against the observer's own tally: the
        number of committed manifests is the independent ground truth."""
        seen = []
        archive, manifests = drive_archive(tmp_path, observer=seen.append,
                                           events=6, max_records=2)
        committed = [d for d in segment_dirs(tmp_path)
                     if (d / MANIFEST_FILENAME).exists()]
        assert len(committed) == 3
        assert archive.rotations == 2 and len(manifests) == 1
        assert len(seen) == len(committed)
        assert all(isinstance(ns, int) and ns > 0 for ns in seen)

    def test_11_an_idempotent_re_close_is_not_a_second_observation(self,
                                                                  tmp_path):
        """`SegmentWriter.close()` is idempotent under its own lock, and
        `EventArchive.close()` documents that its drain-timeout path can reach
        a writer the closer has already finished. That call does no close
        work; observing it would put two samples in the histogram for one
        segment and inflate `segments_closed` by exactly the number of times
        anything went slowly."""
        seen = []
        archive, manifests = drive_archive(tmp_path, observer=seen.append)
        assert len(seen) == 3
        before = committed_state(tmp_path)

        again = archive.close()                 # every writer already CLOSED
        assert len(again) == len(manifests)
        assert len(seen) == 3                   # not 4
        assert committed_state(tmp_path) == before

    def test_12_a_failed_close_is_not_observed(self, tmp_path):
        """The contract is a COMPLETED close. A close that raised is already
        reported through `close_failures`; putting it in a latency
        distribution would mix "how long a commit takes" with "how long a
        commit took before it failed"."""
        seen = []
        init_archive(tmp_path)
        archive = EventArchive(tmp_path, environment=ENV,
                               max_segment_records=100,
                               on_segment_closed=seen.append)
        archive.append(envelope(1))

        class Doomed:
            segment_id = "kalshi.2026-08-14T00.doomed"
            state = SegmentState.OPEN

            def close(self):
                raise OSError("the disk went away")

        archive._writers["doomed"] = Doomed()
        with pytest.raises(ar.ArchiveError, match="failed to close"):
            archive.close()
        assert "doomed" in archive.close_failures
        # The healthy segment was observed; the doomed one was not.
        assert len(seen) == 1

    def test_13_every_close_path_is_observed_exactly_once(self, tmp_path):
        """Both paths in one run, told apart by the thread that ran them.

        A rotation closes on `kalshi-segment-closer`; the final commit closes
        on the caller's. Observing only the first would leave a short session —
        which commits everything at `close()` — reporting nothing at all.
        """
        threads = []
        archive, manifests = drive_archive(
            tmp_path, observer=lambda ns: threads.append(
                threading.current_thread().name))
        assert len(threads) == 3
        assert threads.count("kalshi-segment-closer") == archive.rotations == 2
        assert threads[-1] == threading.current_thread().name
        assert len(manifests) == 1


# =====================================================================================
# PROOF 4 — no callback means the archive of today
# =====================================================================================
class TestAbsence:
    def test_14_the_seam_is_absent_by_default(self, tmp_path):
        init_archive(tmp_path)
        archive = EventArchive(tmp_path, environment=ENV)
        assert archive._on_segment_closed is None
        assert archive.segment_close_observer_errors == 0

    def test_15_an_unobserved_archive_is_byte_identical_to_the_old_path(
            self, tmp_path, monkeypatch, frozen_gzip_clock):
        """Proven, not asserted.

        The control run has `_timed_close` replaced by the expression this
        milestone replaced — `writer.close()`, with no state read, no clock and
        no notification — which is literally the pre-seam close path. The
        archives must be indistinguishable down to the file bytes.
        """
        seam_root, old_root = tmp_path / "seam", tmp_path / "old"
        seam_root.mkdir()
        old_root.mkdir()

        seam, seam_manifests = drive_archive(seam_root, observer=None)
        monkeypatch.setattr(ar.EventArchive, "_timed_close",
                            lambda self, writer: writer.close())
        old, old_manifests = drive_archive(old_root, observer=None)

        assert seam.rotations == old.rotations == 2
        assert seam.rotation_failures == old.rotation_failures == []
        assert len(seam_manifests) == len(old_manifests) == 1
        assert committed_state(seam_root) == committed_state(old_root)
        assert replay_state(seam_root) == replay_state(old_root)

    def test_16_an_observed_archive_commits_what_an_unobserved_one_does(
            self, tmp_path, frozen_gzip_clock):
        """The observational half of the contract: present, the callback is a
        side effect and nothing else."""
        watched_root, plain_root = tmp_path / "watched", tmp_path / "plain"
        watched_root.mkdir()
        plain_root.mkdir()
        seen = []

        drive_archive(watched_root, observer=seen.append)
        drive_archive(plain_root, observer=None)

        assert len(seen) == 3
        assert committed_state(watched_root) == committed_state(plain_root)
        assert replay_state(watched_root) == replay_state(plain_root)


# =====================================================================================
# PROOF 5 — the measured duration excludes the observer
# =====================================================================================
class TestTheMeasurementExcludesTheObserver:
    def test_17_a_slow_observer_does_not_inflate_its_own_measurement(
            self, tmp_path):
        """The ordering requirement, demonstrated.

        The observer sleeps for a second. If it were inside the measured
        interval, the reported `close_ns` would be at least that second — and
        the close-latency distribution that `DEFAULT_MAX_SEGMENT_RECORDS` is
        supposed to be retuned against would be measuring the telemetry.

        The bound is one-sided and generous on purpose: the assertion is that
        the reported number is nowhere near the sleep, not that the host is
        fast. The wall-clock assertion on the other side is what makes it
        non-vacuous — it proves the sleep really happened inside `close()`.
        """
        sleep_s = 1.0
        seen = []

        def slow(close_ns):
            seen.append(close_ns)
            time.sleep(sleep_s)

        init_archive(tmp_path)
        archive = EventArchive(tmp_path, environment=ENV,
                               max_segment_records=100,
                               on_segment_closed=slow)
        for seq in range(1, 4):
            archive.append(envelope(seq))
        started = time.monotonic_ns()
        archive.close()
        wall_ns = time.monotonic_ns() - started

        assert len(seen) == 1
        # The observer's second is really inside the close call...
        assert wall_ns >= sleep_s * 1e9
        # ...and is nowhere in the number the close reported.
        assert seen[0] < 0.5e9, seen[0]
        assert seen[0] < wall_ns - 0.5e9

    def test_18_the_reported_duration_tracks_the_close_it_measures(self,
                                                                  tmp_path):
        """Non-vacuity for the bound above: the number is a real measurement
        of the close, not a small constant. A `close_ns` that was always 0
        would pass every "excludes the observer" assertion ever written."""
        seen = []
        init_archive(tmp_path)
        archive = EventArchive(tmp_path, environment=ENV,
                               max_segment_records=100,
                               on_segment_closed=seen.append)
        for seq in range(1, 51):
            archive.append(envelope(seq))
        started = time.monotonic_ns()
        archive.close()
        wall_ns = time.monotonic_ns() - started

        assert len(seen) == 1
        assert 0 < seen[0] <= wall_ns

    def test_19_the_source_computes_the_interval_before_it_notifies(self):
        """A structural pin on the ORDER, because the property is invisible
        to a fast observer and the failure is silent: a callback moved above
        the second clock read would still pass every test that does not
        measure a slow one, and would quietly add itself to every sample."""
        func = next(n for n in ast.walk(ARCHIVE_SRC)
                    if isinstance(n, ast.FunctionDef) and n.name == "_timed_close")
        calls = [n for n in ast.walk(func)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        names = [n.func.attr for n in calls]
        assert names.count("monotonic_ns") == 2
        assert names.count("close") == 1
        assert names.count("_notify_segment_closed") == 1
        # close, both clock reads, THEN the notification.
        assert (names.index("_notify_segment_closed")
                > max(i for i, n in enumerate(names) if n == "monotonic_ns"))
        assert (names.index("close")
                < max(i for i, n in enumerate(names) if n == "monotonic_ns"))
