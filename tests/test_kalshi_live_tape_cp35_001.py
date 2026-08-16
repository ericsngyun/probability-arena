"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP3.5 — THE SEAM, PROVEN THROUGH.

This file exists because two green suites and strict file ownership produced a
false "complete": CP3 (`collector.py`) defined `observe_frame(**kwargs)` /
`observe_event(name)` and CP4 (`collector_metrics.py`) defined `on_frame` /
`on_append` / a typed method per event class, both exactly as instructed, and
**nothing called either**. 1,186 lines and 81 passing tests of unreachable
measurement code. `AGENTS.md` now states the general rule; this file is the
specific guard for this seam, and every test in it is written against the
failure mode rather than against the code.

The four things it proves, in order:

1. **REACHABILITY.** The REAL collector drives the REAL `CollectorMetrics` and
   the REAL `MetricsFlusher`, and a validated interval record with the session's
   own numbers lands on disk. No mock, no stub, no hand-driven call sequence —
   a unit suite cannot catch an unreachable module, because from inside the
   module everything works.
2. **MULTIPLICITY.** Exactly one observation per intended event, per path.
   Double-counting a frame is as wrong as missing it and neither is visible in a
   counter you only ever compare against itself, so every count here is
   asserted against the session's own independent total.
3. **CONTAINMENT.** A metrics lane that fails — in any method, in the flusher's
   lifecycle, in the bindings, or intermittently — cannot end the session, and
   cannot change one byte of the tape. The comparison is against a clean run's
   own replay checksums, not against an assertion that nothing bad happened.
4. **SHAPE.** The seam is typed, direct and individually guarded: no `**kwargs`,
   no `*args`, no reflection, no dispatch table, and every metrics call inside
   its own narrow exception boundary. CP5's numbers only hold for that shape, so
   the shape is pinned in the source rather than trusted.

**No test in this file opens a socket.**
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import collector as kc
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt
from app.realtime.archive import EventArchive, replay
from app.realtime.collector_metrics import (
    APPEND_US_LABELS,
    CollectorMetrics,
    MetricsFlusher,
    iter_interval_records,
)

REPO = Path(kc.__file__).resolve().parent.parent.parent
COLLECTOR_PATH = REPO / "app" / "realtime" / "collector.py"
COLLECTOR_SRC = ast.parse(COLLECTOR_PATH.read_text())

ENV = "demo"
M1 = "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1"

# Every method of the seam, named once. The list is the contract: a method that
# leaves this list has to leave `collector.py` and `collector_metrics.py` too,
# and a method that joins it has to be wired before this file goes green.
#
# AMENDED BY KALSHI-TAPE-CLOSE-CALLBACK: `on_segment_closed` joined the list.
# It was the one seam method CP3.5 could not wire — the producer never runs a
# close, and timing one from the collector would have meant reaching into
# `archive._closer`. `EventArchive` now takes a typed
# `on_segment_closed=callable(close_ns: int) -> None`, so the collector's half
# is a one-line forward and the method has a caller like every other. Its
# proofs live in `tests/test_kalshi_tape_close_callback_001.py`.
SEAM_METHODS = (
    "on_frame", "on_frame_malformed", "on_append", "on_append_rejected",
    "on_sequence_fault", "on_disconnect", "on_reconnect",
    "on_subscription_generation", "on_segment_closed",
)
SEAM_BINDINGS = ("bind_transport_counters", "bind_reader_lag",
                 "bind_archive_state")


@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - a call is the failure
        raise AssertionError("CP3.5 opened a network connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


# --- the venue's own frames ---------------------------------------------------------
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


def trade_frame(*, sid=4, seq=4):
    return {"type": "trade", "sid": sid, "seq": seq,
            "msg": {"market_ticker": M1, "count_fp": "1.00",
                    "yes_price_dollars": "0.5000", "taker_side": "yes",
                    "trade_id": "t-1", "ts_ms": 1786150148068}}


def poison_frame(*, sid=4, seq=5):
    """A float in the payload. The writer refuses it — a REAL rejection."""
    return {"type": "ticker", "sid": sid, "seq": seq,
            "msg": {"market_ticker": M1, "volume": 1.5}}


FULL_SESSION = [snapshot(), delta(), ticker_frame(), trade_frame()]
ALL_CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")


# --- harness ------------------------------------------------------------------------
class RecordingFactory:
    def __init__(self, *streams):
        self.streams = [list(s) for s in streams]
        self.made: list = []

    def __call__(self):
        frames = self.streams[min(len(self.made), len(self.streams) - 1)]
        transport = kx.FixtureTransport(frames)
        self.made.append(transport)
        return transport


class CountingTransport(kx.FixtureTransport):
    """A fixture transport that keeps CP1's real counter block.

    The live transport counts `bytes_received` inside `_parse_frame`, and the
    collector reads that counter's DELTA across each yield as the frame's wire
    size. A fixture without counters proves the seam survives their absence; it
    cannot prove the byte lane carries anything, which is how a permanently
    empty histogram would go unnoticed.
    """

    def __init__(self, frames):
        super().__init__(frames)
        self.counters = wt.TransportCounters()
        self.sizes = [len(json.dumps(f).encode()) for f in frames]

    async def __aiter__(self):
        for frame, size in zip(self.frames, self.sizes):
            self.counters.bytes_received += size
            self.counters.frames_yielded += 1
            yield frame

    def queue_depth(self) -> int | None:
        return 3


class CountingFactory:
    def __init__(self, frames):
        self.frames = list(frames)
        self.made: list = []

    def __call__(self):
        transport = CountingTransport(self.frames)
        self.made.append(transport)
        return transport


class FailingTransport(kx.FixtureTransport):
    def __init__(self, frames, *, fail_after: int):
        super().__init__(frames)
        self._fail_after = fail_after

    async def __aiter__(self):
        for index, frame in enumerate(self.frames):
            if index >= self._fail_after:
                raise wt.TransportError("ConnectionClosedError")
            yield frame
        if self._fail_after <= len(self.frames):
            raise wt.TransportError("ConnectionClosedError")


class CallCounter(CollectorMetrics):
    """The REAL lane, plus a tally of how many times each method was entered.

    Subclassed rather than mocked on purpose: every assertion below is about
    both the number of CALLS and the value of the real counter they moved, and
    a stub could satisfy the first while the second stayed dead — which is
    exactly the failure this checkpoint exists to close.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls: dict = {name: 0 for name in SEAM_METHODS + SEAM_BINDINGS}
        self.args: list = []

    def _tally(self, name, *args):
        self.calls[name] += 1
        self.args.append((name,) + args)

    def on_frame(self, received_mono_ns, wire_bytes=0):
        self._tally("on_frame", received_mono_ns, wire_bytes)
        super().on_frame(received_mono_ns, wire_bytes)

    def on_frame_malformed(self):
        self._tally("on_frame_malformed")
        super().on_frame_malformed()

    def on_append(self, elapsed_ns, rotated=False):
        self._tally("on_append", elapsed_ns, rotated)
        super().on_append(elapsed_ns, rotated=rotated)

    def on_append_rejected(self, elapsed_ns=0):
        self._tally("on_append_rejected", elapsed_ns)
        super().on_append_rejected(elapsed_ns)

    def on_sequence_fault(self, kind):
        self._tally("on_sequence_fault", kind)
        super().on_sequence_fault(kind)

    def on_disconnect(self):
        self._tally("on_disconnect")
        super().on_disconnect()

    def on_reconnect(self, subscription_generation=None):
        self._tally("on_reconnect", subscription_generation)
        super().on_reconnect(subscription_generation)

    def on_subscription_generation(self, generation):
        self._tally("on_subscription_generation", generation)
        super().on_subscription_generation(generation)

    def on_segment_closed(self, elapsed_ns):
        # Entered on the ARCHIVE's closer thread (or, for the final commit,
        # on whichever thread called `archive.close()`), never on the loop.
        self._tally("on_segment_closed", elapsed_ns)
        super().on_segment_closed(elapsed_ns)

    def bind_transport_counters(self, source):
        self._tally("bind_transport_counters")
        super().bind_transport_counters(source)

    def bind_reader_lag(self, source):
        self._tally("bind_reader_lag")
        super().bind_reader_lag(source)

    def bind_archive_state(self, source):
        self._tally("bind_archive_state")
        super().bind_archive_state(source)


def init_archive(root: Path):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def make_config(root, **kwargs):
    params = dict(environment=ENV, archive_root=root, market_tickers=(M1,),
                  channels=ALL_CHANNELS, max_seconds=60, max_events=1000,
                  max_reconnects=0, reconnect_backoff_base_s=0.0)
    params.update(kwargs)
    return kc.CollectorConfig(**params)


def metrics_for(markets=1, **kwargs):
    return CallCounter(environment=ENV, markets_subscribed=markets, **kwargs)


def run(root, metrics=None, *, frames=None, factory=None, flusher=None,
        **kwargs):
    factory = factory or RecordingFactory(frames if frames is not None
                                          else FULL_SESSION)
    result = kc.collect_once(make_config(root, **kwargs),
                             transport_factory=factory, metrics=metrics,
                             flusher=flusher)
    return result, factory


def replay_report(root):
    store = EventArchive(root, environment=ENV)
    records = store.read_all()
    return store.verify(), replay(records), records


def histogram_total(metrics: CollectorMetrics) -> int:
    snap = metrics.histogram_snapshot()
    return sum(snap["append_us"]) + sum(snap["append_us_rotation"])


# =====================================================================================
# PROOF 1 — the real collector reaches the real metrics lane
# =====================================================================================
class TestReachability:
    def test_1_a_real_session_moves_the_real_counters(self, tmp_path):
        """The anti-'1,186 lines of unreachable green code' test.

        Nothing here is injected except the transport. If the seam were
        unwired, every assertion below would read zero — which is precisely
        what CP4's own 81 green tests could not detect.
        """
        init_archive(tmp_path)
        metrics = metrics_for()
        result, _ = run(tmp_path, metrics)

        assert result.status == kc.STATUS_OK
        assert metrics.events_received == len(FULL_SESSION) > 0
        assert metrics.events_archived == len(FULL_SESSION)
        assert metrics.append_calls == len(FULL_SESSION)
        assert metrics.observe_errors == 0
        assert result.metrics_errors == 0
        # The lane's totals are the session's totals, independently arrived at.
        assert metrics.events_received == result.events_received
        assert metrics.events_archived == result.events_archived
        # And the latency distribution is populated, not merely the scalars.
        assert histogram_total(metrics) == len(FULL_SESSION)
        assert metrics.append_us_max >= 0

    def test_2_the_per_second_ring_carries_the_session(self, tmp_path):
        """`events_per_second` is the field §7.4's burst row is built on, and
        it is filled from the collector's OWN step-1 monotonic stamp. A ring
        that never leaves the process measures nothing."""
        init_archive(tmp_path)
        metrics = metrics_for()
        run(tmp_path, metrics)
        occupied = [count for count, stamp
                    in zip(metrics._ring, metrics._ring_sec) if stamp >= 0]
        assert sum(occupied) == len(FULL_SESSION)

    def test_3_wire_bytes_reach_the_size_histogram(self, tmp_path):
        """The byte lane, end to end: CP1's counter -> the collector's delta ->
        CP4's histogram. A fixture without counters is the other half of this
        (test 4) and the two together are why `wire_bytes` is not permanently
        zero the way `measurement_path` was permanently `None`."""
        init_archive(tmp_path)
        metrics = metrics_for()
        factory = CountingFactory(FULL_SESSION)
        result, _ = run(tmp_path, metrics, factory=factory)
        expected = sum(factory.made[0].sizes)
        assert result.status == kc.STATUS_OK
        assert metrics.event_bytes_total == expected > 0
        assert sum(metrics._event_bytes) == len(FULL_SESSION)
        assert metrics.calls["bind_transport_counters"] == 1
        assert metrics.calls["bind_reader_lag"] == 1
        assert metrics.calls["bind_archive_state"] == 1

    def test_4_a_transport_without_counters_is_not_a_failure(self, tmp_path):
        """`FixtureTransport` has no counter block and the live transport's
        chain is documented as breakable (CP0 12.3). Absent must mean zero
        BYTES, never a raised seam or a fabricated size."""
        init_archive(tmp_path)
        metrics = metrics_for()
        result, _ = run(tmp_path, metrics)
        assert result.metrics_errors == 0
        assert metrics.event_bytes_total == 0
        assert sum(metrics._event_bytes) == 0
        assert metrics.events_received == len(FULL_SESSION)
        assert metrics.calls["bind_transport_counters"] == 0

    def test_5_a_real_interval_record_lands_on_disk(self, tmp_path):
        """collector -> CollectorMetrics -> MetricsFlusher -> a VALIDATED line
        in `kalshi-live-tape.jsonl`. The whole lane, driven by a session."""
        init_archive(tmp_path)
        metrics = metrics_for()
        path = tmp_path / "telemetry" / "kalshi-live-tape.jsonl"
        flusher = MetricsFlusher(metrics, path=path, flush_interval_s=60.0,
                                 sample_interval_s=0.02)
        result, _ = run(tmp_path, metrics, flusher=flusher)

        assert result.status == kc.STATUS_OK
        assert result.measurement_path == str(path)
        records = list(iter_interval_records(path))
        assert len(records) == 1, records
        record = records[0]
        assert record["events_received"] == len(FULL_SESSION)
        assert record["events_archived"] == len(FULL_SESSION)
        assert record["events_rejected"] == 0
        assert record["markets_subscribed"] == 1
        assert record["environment"] == ENV
        assert record["session_id"] == metrics.session_id
        assert sum(record["append_us_histogram"].values()) == len(FULL_SESSION)
        assert set(record["append_us_histogram"]) <= set(APPEND_US_LABELS)
        assert record["metric_flush_drops"] == 0
        assert flusher.thread_error is None

    def test_6_the_seam_is_called_from_app_not_only_from_tests(self):
        """The structural guard against the seam quietly becoming unreachable
        again. `collector.py` must NAME every method of the contract.

        Anti-vacuity is the point of the second half: a method that does not
        exist is asserted absent — if this test can find that, its findings
        are real.

        AMENDED BY KALSHI-TAPE-CLOSE-CALLBACK, and kept NET-STRONGER.
        `on_segment_closed` used to be asserted ABSENT here, as the visible
        marker of the one unwired method. An audit that pins an interface as
        unwired certifies unreachable code the moment the wiring lands, so the
        marker moves rather than disappears: the method is now required in
        `SEAM_METHODS` like the other eight, AND the seam it arrives through is
        pinned too — `collector.py` must hand `EventArchive` the callback by
        KEYWORD, because that is the coupling that can silently rot (a
        positional would break on the next parameter `EventArchive` gains, and
        a dropped kwarg would return the counters to zero with every test in
        this file still green). The full proof set is
        `tests/test_kalshi_tape_close_callback_001.py`.
        """
        called = set()
        archive_kwargs = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                         ast.Attribute):
                called.add(node.func.attr)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "EventArchive"):
                archive_kwargs |= {kw.arg for kw in node.keywords if kw.arg}
        for name in SEAM_METHODS + SEAM_BINDINGS:
            assert name in called, f"{name} has no caller in collector.py"
        assert "on_frame_totally_invented" not in called
        # The close seam's own arrival point, held to the same standard.
        assert "on_segment_closed" in archive_kwargs, archive_kwargs
        assert "environment" in archive_kwargs                 # anti-vacuity
        assert "on_segment_closed_totally_invented" not in archive_kwargs
        # The interfaces CP3 defined and CP3.5 replaced are GONE, not shimmed.
        # Identifier level, not substring: the module is allowed — required,
        # really — to EXPLAIN in prose what it replaced and why.
        assert "observe_frame" not in called
        assert "observe_event" not in called


# =====================================================================================
# PROOF 2 — exactly one observation per intended event, per path
# =====================================================================================
class TestExactlyOnce:
    def test_7_one_on_frame_per_received_frame(self, tmp_path):
        init_archive(tmp_path)
        metrics = metrics_for()
        result, _ = run(tmp_path, metrics)
        assert metrics.calls["on_frame"] == len(FULL_SESSION)
        assert metrics.calls["on_frame"] == result.events_received
        assert metrics.events_received == result.events_received

    def test_8_one_append_observation_per_append_and_never_both(self, tmp_path):
        """A rejected append is observed once as a REJECTION and never also as
        an accepted one. §7.4 keeps the two apart forever; a frame counted in
        both would make `events_received = archived + rejected + malformed`
        stop holding."""
        init_archive(tmp_path)
        metrics = metrics_for()
        frames = [snapshot(), poison_frame(), delta()]
        result, _ = run(tmp_path, metrics, frames=frames)

        assert result.events_rejected == 1 and result.events_archived == 2
        assert metrics.calls["on_append"] == 2
        assert metrics.calls["on_append_rejected"] == 1
        assert metrics.events_archived == 2
        assert metrics.events_rejected == 1
        assert metrics.append_calls == 3          # both kinds cost writer time
        assert histogram_total(metrics) == 3
        assert metrics.calls["on_frame"] == 3
        # The conservation property, asserted rather than assumed.
        assert (metrics.events_received
                == metrics.events_archived + metrics.events_rejected
                + metrics.frames_malformed)

    def test_9_a_malformed_frame_is_observed_once_on_each_axis(self, tmp_path):
        init_archive(tmp_path)
        metrics = metrics_for()
        frames = [snapshot(), "not a dict", 42, delta()]
        result, _ = run(tmp_path, metrics, frames=frames)

        assert result.frames_malformed == 2 and result.events_archived == 2
        assert metrics.calls["on_frame"] == 4     # the denominator counts them
        assert metrics.calls["on_frame_malformed"] == 2
        assert metrics.calls["on_append"] == 2
        assert metrics.frames_malformed == 2
        assert metrics.events_received == 4

    def test_10_a_duplicate_is_observed_once_and_is_not_a_gap(self, tmp_path):
        """The venue re-sent something already applied. Nothing was lost, so
        `sequence_faults` does not move — but the interval record has its own
        counter for it, and that counter is the only place it is visible."""
        init_archive(tmp_path)
        metrics = metrics_for()
        frames = [snapshot(seq=1), delta(seq=2), delta(seq=2)]
        result, _ = run(tmp_path, metrics, frames=frames)

        assert result.sequence_faults == 0
        assert metrics.sequence_duplicates == 1
        assert metrics.sequence_gaps == 0 and metrics.sequence_regressions == 0
        assert metrics.calls["on_sequence_fault"] == 1
        assert metrics.args.count(("on_sequence_fault", "duplicate")) == 1
        assert metrics.observe_errors == 0

    def test_11_a_gap_is_observed_once_per_faulting_frame(self, tmp_path):
        """After a gap every following delta faults too — which is why recovery
        is requested once per fault and not once per frame. The metrics lane
        must see the same shape: one observation per faulting frame, and every
        one of them classified as a GAP rather than as something adjacent."""
        init_archive(tmp_path)
        metrics = metrics_for()
        frames = [snapshot(seq=1), delta(seq=9)]
        result, _ = run(tmp_path, metrics, frames=frames)

        assert result.sequence_faults == 1
        assert metrics.sequence_gaps == 1
        assert metrics.calls["on_sequence_fault"] == 1
        assert metrics.args.count(("on_sequence_fault", "gap")) == 1
        assert metrics.observe_errors == 0
        assert metrics.sequence_regressions == metrics.sequence_duplicates == 0

    def test_12_a_fault_with_no_bucket_is_not_laundered_into_one(self, tmp_path):
        """A delta before any snapshot is `awaiting_snapshot`, which the closed
        interval schema has no field for. The wrong answers are: call it a gap
        (fabricates a loss that did not happen) and pass the raw reason
        (`on_sequence_fault` counts an unknown kind as an OBSERVE ERROR, i.e. a
        claim that the metrics lane malfunctioned). The right answer is the
        session result, where it is counted and true."""
        init_archive(tmp_path)
        metrics = metrics_for()
        result, _ = run(tmp_path, metrics, frames=[delta(seq=2)])

        assert result.sequence_faults == 1
        assert metrics.calls["on_sequence_fault"] == 0
        assert metrics.sequence_gaps == 0
        assert metrics.sequence_regressions == 0
        assert metrics.sequence_duplicates == 0
        assert metrics.observe_errors == 0        # nothing was laundered
        assert result.metrics_errors == 0

    def test_13_one_disconnect_per_connection_the_venue_ended(self, tmp_path):
        """Two connections, both ended from the far side: the first by a
        transport failure, the second by the stream running out. Two
        disconnects, one reconnect. A cap or an operator stop is not a
        disconnect and is asserted separately (test 15)."""
        init_archive(tmp_path)
        metrics = metrics_for()
        made: list = []

        def factory():
            transport = FailingTransport(
                [snapshot(), delta()] if not made else [ticker_frame()],
                fail_after=2 if not made else 99)
            made.append(transport)
            return transport

        result, _ = run(tmp_path, metrics, factory=factory, max_reconnects=2)

        assert result.reconnects == 1 and result.status == kc.STATUS_OK
        assert metrics.calls["on_disconnect"] == 2
        assert metrics.disconnects == 2
        assert metrics.calls["on_reconnect"] == 1
        assert metrics.reconnects == 1

    def test_14_the_reconnect_carries_the_epoch_and_the_epoch_is_reported(
            self, tmp_path):
        """The defect this seam was asked to fix: `on_reconnect`'s
        `subscription_generation` parameter had no channel to arrive through
        and was never once supplied, so the gauge sat at 0 while the tape
        stamped 1, 2, 3. Both halves are asserted — the epoch the session left,
        and the epoch it arrived in."""
        init_archive(tmp_path)
        metrics = metrics_for()
        made: list = []

        def factory():
            transport = FailingTransport(
                [snapshot(), delta()] if not made else [ticker_frame()],
                fail_after=2 if not made else 99)
            made.append(transport)
            return transport

        run(tmp_path, metrics, factory=factory, max_reconnects=2)

        # One epoch observation per SUCCESSFUL subscribe, not one per router.
        assert metrics.calls["on_subscription_generation"] == 2
        assert ("on_subscription_generation", 1) in metrics.args
        assert ("on_subscription_generation", 2) in metrics.args
        # The reconnect carried the epoch it was leaving, not a `None`.
        assert ("on_reconnect", 1) in metrics.args
        # THE GAUGE EQUALS WHAT THE TAPE SAYS. Read back from the archive, not
        # from the collector's own bookkeeping, which is the number that was
        # permanently disagreeing.
        _, _, records = replay_report(tmp_path)
        stamped = {r["subscription_generation"] for r in records}
        assert stamped == {1, 2}
        assert metrics.subscription_generation == max(stamped) == 2

    def test_15_an_operator_cap_is_not_a_disconnect(self, tmp_path):
        init_archive(tmp_path)
        metrics = metrics_for()
        result, _ = run(tmp_path, metrics, max_events=2)
        assert result.status == kc.STATUS_CAPPED_EVENTS
        assert metrics.calls["on_disconnect"] == 0
        assert metrics.disconnects == 0
        assert metrics.calls["on_frame"] == 2

    def test_16_the_frame_that_breaks_the_partition_is_still_observed_once(
            self, tmp_path, monkeypatch):
        """A partition-level failure ends the session, and the frame that hit
        it was still received. Observing it keeps `events_received` conserved
        across the one path where the session is about to stop — the interval
        that explains a failure is the one that must not have a hole in it."""
        init_archive(tmp_path)
        metrics = metrics_for()

        def _broken(self, envelope):
            from app.realtime.archive import ArchiveError
            raise ArchiveError("could not open segment 'kalshi.2026-08-08T00'")

        monkeypatch.setattr(EventArchive, "append", _broken)
        result, _ = run(tmp_path, metrics)

        assert result.status == kc.STATUS_ARCHIVE_ERROR
        assert result.events_received == 1
        assert metrics.calls["on_frame"] == 1
        assert metrics.events_received == 1
        # No append happened, so neither append observation was made.
        assert metrics.calls["on_append"] == 0
        assert metrics.calls["on_append_rejected"] == 0
        assert metrics.append_calls == 0


# =====================================================================================
# PROOF 3 — a metrics failure cannot interrupt or corrupt the tape
# =====================================================================================
class _Hostile(kc.NullCollectorMetrics):
    """Every seam method raises. CP4 proves its own methods never do; this is
    the belt to that pair of braces, and it is what makes the boundary's
    behaviour observable rather than merely argued."""

    def on_frame(self, received_mono_ns, wire_bytes=0):
        raise RuntimeError("metrics exploded")

    def on_frame_malformed(self):
        raise RuntimeError("metrics exploded")

    def on_append(self, elapsed_ns, rotated=False):
        raise RuntimeError("metrics exploded")

    def on_append_rejected(self, elapsed_ns=0):
        raise RuntimeError("metrics exploded")

    def on_sequence_fault(self, kind):
        raise RuntimeError("metrics exploded")

    def on_disconnect(self):
        raise RuntimeError("metrics exploded")

    def on_reconnect(self, subscription_generation=None):
        raise RuntimeError("metrics exploded")

    def on_subscription_generation(self, generation):
        raise RuntimeError("metrics exploded")

    def bind_transport_counters(self, source):
        raise RuntimeError("metrics exploded")

    def bind_reader_lag(self, source):
        raise RuntimeError("metrics exploded")

    def bind_archive_state(self, source):
        raise RuntimeError("metrics exploded")


class _HostileFlusher:
    def start(self):
        raise RuntimeError("the flusher exploded")

    def stop(self, timeout_s=5.0):
        raise RuntimeError("the flusher exploded")

    @property
    def path(self):
        raise RuntimeError("the flusher exploded")


class _OnceHostile(CollectorMetrics):
    """Fails once, mid-session, then behaves. The intermittent case: a lane
    that stops measuring after its first bad moment is a lane that reports a
    quiet session instead of a broken one."""

    def __init__(self, *, fail_on: int, **kwargs):
        super().__init__(**kwargs)
        self._fail_on = fail_on
        self._seen = 0

    def on_frame(self, received_mono_ns, wire_bytes=0):
        self._seen += 1
        if self._seen == self._fail_on:
            raise RuntimeError("metrics exploded once")
        super().on_frame(received_mono_ns, wire_bytes)


class TestContainment:
    def test_17_a_totally_hostile_lane_leaves_the_tape_byte_identical(
            self, tmp_path):
        """The strong form. Not 'the session survived' — the EVIDENCE is
        identical to what a clean run produced, market checksum for market
        checksum, and the archive's own verifier says so."""
        clean_root, hostile_root = tmp_path / "clean", tmp_path / "hostile"
        clean_root.mkdir()
        hostile_root.mkdir()
        init_archive(clean_root)
        init_archive(hostile_root)
        frames = [snapshot(), delta(), poison_frame(), ticker_frame(),
                  "not a dict", trade_frame()]

        clean, _ = run(clean_root, metrics_for(), frames=frames)
        hostile, _ = run(hostile_root, _Hostile(), frames=frames)

        assert hostile.status == clean.status == kc.STATUS_OK
        assert hostile.events_archived == clean.events_archived == 4
        assert hostile.events_rejected == clean.events_rejected == 1
        assert hostile.frames_malformed == clean.frames_malformed == 1
        # Counted, never silent: one per attempted observation.
        assert hostile.metrics_errors > 0
        assert clean.metrics_errors == 0

        clean_integrity, clean_out, clean_records = replay_report(clean_root)
        h_integrity, h_out, h_records = replay_report(hostile_root)
        assert h_integrity["intact"] is True
        assert h_integrity["verdict"] == "VALID"
        assert h_integrity["records"] == clean_integrity["records"] == 4
        assert h_out["faults"] == clean_out["faults"] == []
        assert h_out["checksums"] == clean_out["checksums"]
        assert [r["event_type"] for r in h_records] == [
            r["event_type"] for r in clean_records]

    def test_18_the_boundary_counts_one_error_per_attempted_observation(
            self, tmp_path):
        """`metrics_errors` is a measurement of the measurement lane. It must
        not merge failures either: one raise, one count."""
        init_archive(tmp_path)
        result, _ = run(tmp_path, _Hostile(), frames=[snapshot(), delta()])
        # 3 bindings are attempted (only `bind_archive_state` on a fixture
        # transport), 1 epoch, 2 frames x (on_frame + on_append), 1 disconnect.
        assert result.metrics_errors == 1 + 1 + 4 + 1
        assert result.events_archived == 2

    def test_19_a_flusher_that_cannot_start_or_stop_does_not_end_the_session(
            self, tmp_path):
        init_archive(tmp_path)
        metrics = metrics_for()
        result, _ = run(tmp_path, metrics, flusher=_HostileFlusher())
        assert result.status == kc.STATUS_OK
        assert result.events_archived == len(FULL_SESSION)
        assert result.measurement_path is None
        assert result.metrics_errors == 3          # start, stop, path
        # The lane itself still measured; only its writer was broken.
        assert metrics.events_received == len(FULL_SESSION)
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["intact"] is True and out["faults"] == []

    def test_20_a_lane_that_fails_once_keeps_measuring(self, tmp_path):
        init_archive(tmp_path)
        metrics = _OnceHostile(fail_on=2, environment=ENV,
                               markets_subscribed=1)
        result, _ = run(tmp_path, metrics)
        assert result.status == kc.STATUS_OK
        assert result.metrics_errors == 1
        assert result.events_archived == len(FULL_SESSION)
        # One frame's observation was lost. Every other frame's was not, and
        # the appends never stopped being observed at all.
        assert metrics.events_received == len(FULL_SESSION) - 1
        assert metrics.events_archived == len(FULL_SESSION)
        integrity, _, _ = replay_report(tmp_path)
        assert integrity["records"] == len(FULL_SESSION)

    def test_21_the_writer_thread_cannot_reach_the_tape(self, tmp_path):
        """A real flusher whose destination is unwritable. The interval record
        is lost and counted; the tape is not affected, because the two files
        share nothing but a session."""
        init_archive(tmp_path)
        metrics = metrics_for()
        blocked = tmp_path / "telemetry"
        blocked.write_text("not a directory")
        flusher = MetricsFlusher(metrics, path=blocked / "kalshi-live-tape.jsonl",
                                 flush_interval_s=60.0, sample_interval_s=0.02)
        result, _ = run(tmp_path, metrics, flusher=flusher)

        assert result.status == kc.STATUS_OK
        assert result.events_archived == len(FULL_SESSION)
        assert flusher.writer.written == 0
        assert flusher.writer.dropped >= 1
        assert metrics.metric_flush_drops >= 1
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["intact"] is True and out["faults"] == []

    def test_22_no_market_ticker_reaches_the_lane_or_its_file(self, tmp_path):
        """§7.2 through the WIRED path. CP4 proves its module cannot hold a
        ticker; this proves the collector does not hand it one — the two
        together are the guarantee, and only one of them was previously
        testable."""
        init_archive(tmp_path)
        metrics = metrics_for()
        path = tmp_path / "telemetry" / "kalshi-live-tape.jsonl"
        flusher = MetricsFlusher(metrics, path=path, flush_interval_s=60.0,
                                 sample_interval_s=0.02)
        run(tmp_path, metrics, flusher=flusher)

        assert M1 not in json.dumps(metrics.args, default=str)
        assert M1 not in json.dumps(
            {k: v for k, v in vars(metrics).items()
             if not k.startswith("_bind")}, default=str)
        blob = path.read_bytes()
        assert M1.encode() not in blob
        assert b"KX" not in blob
        # Anti-vacuity: the ticker IS in the evidence store, so its absence
        # from the telemetry file is a fact about the seam and not about the
        # session having no markets.
        _, _, records = replay_report(tmp_path)
        assert any(r.get("market_ticker") == M1 for r in records)


# =====================================================================================
# PROOF 4 — the shape CP5's numbers were measured against
# =====================================================================================
class TestSeamShape:
    """CP5 priced a `try/except` + `**kwargs` wrapper at +250 ns p50 and a
    direct call at 83 ns. CP3.5's shape — typed direct call, own inline
    boundary — measures +0 ns p50 over the direct call, because the kwargs
    packing rather than the exception handler was the cost. Those numbers only
    describe the code while the code keeps that shape, so the shape is pinned
    here.
    """

    def _metrics_calls(self):
        names = set(SEAM_METHODS + SEAM_BINDINGS)
        found = []
        for node in ast.walk(COLLECTOR_SRC):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in names):
                found.append(node)
        return found

    def test_23_no_metrics_call_packs_varargs_or_kwargs(self):
        calls = self._metrics_calls()
        assert len(calls) >= len(SEAM_METHODS)
        for call in calls:
            assert not any(isinstance(a, ast.Starred) for a in call.args), \
                ast.dump(call)
            assert all(kw.arg is not None for kw in call.keywords), \
                ast.dump(call)
            # Every positional argument is a plain value, not a packed one.
            for argument in call.args:
                assert not isinstance(argument, (ast.Dict, ast.DictComp)), \
                    ast.dump(call)

    def test_24_every_metrics_call_sits_inside_its_own_boundary(self):
        """Not one wrapper around many calls: one boundary per call, so a
        method that raises cannot suppress the observation after it.

        AMENDED BY KALSHI-TAPE-CLOSE-CALLBACK, and kept NET-STRONGER.
        `on_segment_closed` is the one seam call the collector does not make
        from its own thread: the ARCHIVE calls it, on the thread that ran the
        close, from inside `_notify_segment_closed`'s own boundary — which
        already wraps the collector's forward and everything under it. A
        second `try` here would catch the same failure first and leave
        `archive.segment_close_observer_errors` permanently zero, hiding a
        metrics failure from the counter that exists to show it.

        So the exemption is not "this one is allowed to be unguarded". It is
        three assertions that the boundary is somewhere BETTER, all checked
        below: the call is the entire body of `_on_segment_closed` and nothing
        else rides with it; `archive.py` contains it with `BaseException`
        (stronger than the `Exception` this test requires everywhere else);
        and the collector FOLDS the archive's error count into
        `metrics_errors`, so the honesty field still moves.
        """
        guarded = set()
        for node in ast.walk(COLLECTOR_SRC):
            if not isinstance(node, ast.Try):
                continue
            handled = any(
                h.type is not None and getattr(h.type, "id", None) == "Exception"
                for h in node.handlers)
            if not handled:
                continue
            calls = [c for stmt in node.body for c in ast.walk(stmt)
                     if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute)]
            # ONE metrics call per boundary. A `try` holding two of them would
            # let the first one's failure hide the second one entirely.
            metrics_calls = [c for c in calls
                             if c.func.attr in set(SEAM_METHODS + SEAM_BINDINGS)]
            assert len(metrics_calls) <= 1, ast.dump(node)
            guarded.update(id(c) for c in metrics_calls)

        # --- the ONE call whose boundary lives in `archive.py` ---------------
        forwarder = next(n for n in ast.walk(COLLECTOR_SRC)
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "_on_segment_closed")
        body_calls = [c for c in ast.walk(forwarder)
                      if isinstance(c, ast.Call)
                      and isinstance(c.func, ast.Attribute)]
        assert [c.func.attr for c in body_calls] == ["on_segment_closed"]
        assert len(forwarder.body) == 2                # the docstring, and it
        exempt = {id(c) for c in body_calls}
        for call in self._metrics_calls():
            assert id(call) in guarded or id(call) in exempt, ast.dump(call)
        # The exemption is only sound if the archive really does contain it.
        archive_src = ast.parse(
            (REPO / "app" / "realtime" / "archive.py").read_text())
        notify = next(n for n in ast.walk(archive_src)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "_notify_segment_closed")
        handlers = [h for t in ast.walk(notify) if isinstance(t, ast.Try)
                    for h in t.handlers]
        assert [getattr(h.type, "id", None) for h in handlers] == \
            ["BaseException"], ast.dump(notify)
        # ...and only honest if the count reaches `metrics_errors`.
        folded = [n for n in ast.walk(COLLECTOR_SRC)
                  if isinstance(n, ast.AugAssign)
                  and getattr(n.target, "attr", None) == "metrics_errors"
                  and getattr(n.value, "attr", None)
                  == "segment_close_observer_errors"]
        assert len(folded) == 1, "the archive's observer failures are dropped"

    def test_25_there_is_no_generic_metrics_dispatcher_left(self):
        """The shape CP5 measured and Eric refused: a wrapper that takes
        `**kwargs`, or a name-to-method lookup. Both are absent by identifier,
        so the module can still EXPLAIN them in prose."""
        names = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for banned in ("_metric_frame", "_metric_event", "observe_frame",
                       "observe_event", "METRIC_EVENTS"):
            assert banned not in names, banned
        # Anti-vacuity: the scan finds identifiers that ARE there.
        assert {"on_frame", "metrics_errors", "_metrics"} <= names
        # No function in the module takes `**kwargs` or `*args` at all, so no
        # generic forwarder can exist even under a different name.
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.args.vararg is None, node.name
                assert node.args.kwarg is None, node.name
        # And nothing looks a metrics method up by name.
        for node in ast.walk(COLLECTOR_SRC):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "setattr", "eval",
                                         "exec", "globals")):
                # `getattr` survives for the transport's optional surface
                # (`close`, `counters`, `queue_depth`), which is a capability
                # probe, not a metrics dispatch. Nothing may look up a seam
                # method that way.
                assert not any(isinstance(a, ast.Constant)
                               and a.value in set(SEAM_METHODS + SEAM_BINDINGS)
                               for a in node.args), ast.dump(node)

    def test_26_the_null_lane_has_typed_no_ops_not_a_varargs_sink(self):
        """CP5 §13.1.1: a null lane that packs varargs is SLOWER than no lane
        at all, which understated the true overhead by 125 ns/event. Both arms
        of the gate have to be honest or the gate measures its own scaffolding.
        """
        import inspect

        from app.realtime.collector_metrics import NULL_METRICS

        for name in SEAM_METHODS + SEAM_BINDINGS:
            signature = inspect.signature(getattr(NULL_METRICS, name))
            kinds = [p.kind for p in signature.parameters.values()]
            assert inspect.Parameter.VAR_POSITIONAL not in kinds, name
            assert inspect.Parameter.VAR_KEYWORD not in kinds, name
            real = inspect.signature(getattr(CollectorMetrics, name))
            assert list(signature.parameters) == [
                p for p in real.parameters if p != "self"], name
