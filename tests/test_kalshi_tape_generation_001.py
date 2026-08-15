"""KALSHI-TAPE-GENERATION — the subscription epoch, end to end.

The defect these tests pin down: `EventEnvelope` defined neither
`connection_id` nor `subscription_generation`, while `archive.append` wrote
both via `raw.get(...)`, so both pinned columns were permanently null — and
`SubscriptionRouter.dispatch` read that null back as "no generation
information". A reconnect (venue `seq` restarts) then looked exactly like a
sequence gap, and a sequence gap is the only drop detector this system has.

Four things have to be true at once, and the last two are what stop the fix
from being worse than the bug:

1. the epoch actually reaches the durable record — not `None`;
2. a reconnect is a GENERATION BOUNDARY, not a fault, and does not unpublish;
3. a genuine gap INSIDE a generation still faults;
4. records written before the field existed read as the documented sentinel
   and still replay.

Every negative assertion below is paired with an anti-vacuity guard (AGENTS.md
research doctrine 4): a test that proves "this faults" also proves the same
tape without the defect does NOT fault, so it cannot pass in a repository
where everything is broken.

No socket is opened anywhere in this file.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import book as bk
from app.realtime import collector as kc
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt
from app.realtime.archive import EventArchive, replay
from app.realtime.segment import EVENTS_FILENAME, read_segment_records

ENV = "demo"
M1 = "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1"
ALL_CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")


# --- offline enforcement ------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - a call is the failure
        raise AssertionError("this test opened a network connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


# --- venue frames -------------------------------------------------------------------
def snapshot(market=M1, *, sid=4, seq=1, yes=(("0.4700", "5.00"),),
             no=(("0.5100", "5.00"),)):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "msg": {"market_ticker": market, "market_id": "mid-1",
                    "ts_ms": 1786150148065,
                    "yes_dollars_fp": [list(lv) for lv in yes],
                    "no_dollars_fp": [list(lv) for lv in no]}}


def delta(market=M1, *, sid=4, seq=2, side="no", price="0.5100",
          change="201.00"):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": market, "price_dollars": price,
                    "delta_fp": change, "side": side, "ts_ms": 1786150148066}}


# --- archived-record shapes (what replay actually consumes) -------------------------
def record(frame, *, generation):
    """One envelope dict as `EventArchive._read_records` hands it to replay.

    `generation=bk.GENERATION_UNKNOWN` produces the PRE-milestone shape: the
    key is absent entirely, exactly as a v1 record reads.
    """
    rec = {"schema_version": bk.ENVELOPE_SCHEMA_VERSION, "venue": "kalshi",
           "environment": ENV, "channel": "orderbook_delta",
           "event_type": frame["type"],
           "market_ticker": frame["msg"].get("market_ticker"),
           "sid": frame["sid"], "seq": frame["seq"], "raw": frame}
    if generation is not bk.GENERATION_UNKNOWN:
        rec["subscription_generation"] = generation
    return rec


def router(*, generation=1):
    return bk.SubscriptionRouter(
        bk.SubscriptionState(4, market_tickers=(M1,), generation=generation))


# --- transports ---------------------------------------------------------------------
class FailingTransport(kx.FixtureTransport):
    """Yields, then loses the socket."""

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


class DeadOnConnect(kx.FixtureTransport):
    """The socket never comes up. A connect ATTEMPT, and nothing more."""

    def __init__(self):
        super().__init__([])

    async def connect(self) -> None:
        raise wt.TransportError("ConnectionRefusedError")


class SequenceOfTransports:
    """One transport per connection, in the order given."""

    def __init__(self, *factories):
        self._factories = list(factories)
        self.made: list = []

    def __call__(self):
        factory = self._factories[min(len(self.made),
                                      len(self._factories) - 1)]
        transport = factory()
        self.made.append(transport)
        return transport


# --- collector harness --------------------------------------------------------------
def init_archive(root: Path):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def make_config(root, **kwargs):
    params = dict(environment=ENV, archive_root=root, market_tickers=(M1,),
                  channels=ALL_CHANNELS, max_seconds=60, max_events=1000,
                  max_reconnects=0, reconnect_backoff_base_s=0.0)
    params.update(kwargs)
    return kc.CollectorConfig(**params)


def read_back(root):
    store = EventArchive(root, environment=ENV)
    return store.verify(), store.read_all()


def raw_records(root):
    """The DURABLE records, with their pinned columns — not the envelopes."""
    store = EventArchive(root, environment=ENV)
    out = []
    for directory in store._segment_dirs():
        out.extend(read_segment_records(directory / EVENTS_FILENAME))
    return out


# =====================================================================================
# PROOF 1 — the epoch reaches the archive, and is not None
# =====================================================================================
class TestTheFieldReachesTheArchive:
    def test_an_appended_envelope_lands_in_the_pinned_columns(self, tmp_path):
        init_archive(tmp_path)
        store = EventArchive(tmp_path, environment=ENV, venue="kalshi")
        store.append(bk.make_envelope(
            venue="kalshi", environment=ENV, channel="orderbook_delta",
            message=delta(), receive_time=bk.utcnow(), receive_mono=1,
            connection_generation=2, subscription_generation=7))
        store.close()

        durable = raw_records(tmp_path)
        assert len(durable) == 1
        # The whole bug in one assertion pair: these were `None` forever.
        assert durable[0]["subscription_generation"] == 7
        assert durable[0]["connection_generation"] == 2
        assert durable[0]["subscription_generation"] is not None

        # ...and it survives the read path replay actually uses.
        _, envelopes = read_back(tmp_path)
        assert envelopes[0]["subscription_generation"] == 7
        assert envelopes[0]["connection_generation"] == 2
        assert bk.subscription_generation_of(envelopes[0]) == 7

    def test_a_real_session_stamps_every_record(self, tmp_path):
        init_archive(tmp_path)
        result = kc.collect_once(
            make_config(tmp_path),
            transport_factory=lambda: kx.FixtureTransport(
                [snapshot(), delta(), delta(seq=3, change="-1.00")]))
        assert result.status == kc.STATUS_OK
        assert result.events_archived == 3

        durable = raw_records(tmp_path)
        assert len(durable) == 3
        assert [r["subscription_generation"] for r in durable] == [1, 1, 1]
        assert [r["connection_generation"] for r in durable] == [1, 1, 1]

    def test_the_stamp_matches_the_state_that_validates_it(self, tmp_path):
        """The seam: the number on the tape IS the number `accept()` checks.

        Two counters merely intended to agree is how this milestone's defect
        survived — the doc asserted the generation "rides into every
        subsequent envelope" while nothing wrote it.
        """
        init_archive(tmp_path)
        factory = SequenceOfTransports(
            lambda: FailingTransport([snapshot(), delta()], fail_after=2),
            lambda: kx.FixtureTransport([snapshot(seq=1), delta(seq=2)]))
        session = kc._Session(make_config(tmp_path, max_reconnects=1),
                              transport_factory=factory)
        import asyncio
        result = asyncio.run(session.run())
        assert result.reconnects == 1
        assert session.subscription_epoch == 2
        for router_ in session._routers.values():
            assert router_.subscription.generation == session.subscription_epoch
        stamped = {r["subscription_generation"] for r in raw_records(tmp_path)}
        assert stamped == {1, 2}


# =====================================================================================
# PROOF 2 — a reconnect is a boundary, not a sequence fault
# =====================================================================================
class TestAReconnectIsNotASequenceFault:
    def test_the_router_treats_a_new_generation_as_a_boundary(self):
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=1))
        r.dispatch(record(delta(seq=2), generation=1))
        r.dispatch(record(delta(seq=3, change="-1.00"), generation=1))
        assert r.publishable_books() == {M1: True}

        # THE RECONNECT. New generation, venue sequence restarts at 1.
        r.dispatch(record(snapshot(seq=1), generation=2))
        r.dispatch(record(delta(seq=2), generation=2))

        assert r.subscription.generation == 2
        assert r.subscription.healthy is True
        assert r.subscription.stats["generation_advances"] == 1
        # Not filed as loss. This is the distinction the epoch exists for.
        assert r.subscription.stats["gaps"] == 0
        assert r.subscription.stats["regressions"] == 0
        assert r.subscription.stats["stale_generation"] == 0
        book = r.books[M1]
        assert book.integrity_reason is None
        assert book.publishable is True
        assert book.stats["generation_boundaries"] == 1
        assert book.subscription_generation == 2
        assert r.publishable_books() == {M1: True}

    def test_books_are_not_unpublished_across_the_boundary(self):
        """Explicitly: `_unpublish_all` must not run for a reconnect."""
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=1))
        halted: list = []
        original = bk.OrderBook._halt

        def spy(self, reason):
            halted.append((self.market_ticker, reason))
            return original(self, reason)

        bk.OrderBook._halt = spy
        try:
            r.dispatch(record(snapshot(seq=1), generation=2))
            r.dispatch(record(delta(seq=2), generation=2))
        finally:
            bk.OrderBook._halt = original
        assert halted == [], halted

    def test_end_to_end_a_reconnected_session_replays_without_faults(self, tmp_path):
        init_archive(tmp_path)
        factory = SequenceOfTransports(
            lambda: FailingTransport(
                [snapshot(seq=1), delta(seq=2), delta(seq=3, change="-1.00")],
                fail_after=3),
            lambda: kx.FixtureTransport(
                [snapshot(seq=1), delta(seq=2), delta(seq=3, change="-1.00")]))
        result = kc.collect_once(make_config(tmp_path, max_reconnects=1),
                                 transport_factory=factory)
        assert result.reconnects == 1
        # The live lane saw no fault either.
        assert result.sequence_faults == 0
        assert result.events_archived == 6

        integrity, records = read_back(tmp_path)
        assert integrity["intact"] is True
        out = replay(records)
        assert out["faults"] == []
        assert out["publishable"] == {M1: True}
        assert out["events_applied"] == 6
        stats = out["subscription_stats"]["4"]
        assert stats["generation_advances"] == 1
        assert stats["gaps"] == 0 and stats["regressions"] == 0

    def test_anti_vacuity_the_same_tape_without_the_epoch_does_fault(self, tmp_path):
        """The guard that makes the test above mean something.

        Strip the generation from the very same records — the state of the
        world before this fix, where the column was null — and the reconnect
        becomes a sequence fault that unpublishes the book. If this ever stops
        failing, the test above is passing for the wrong reason.
        """
        init_archive(tmp_path)
        factory = SequenceOfTransports(
            lambda: FailingTransport(
                [snapshot(seq=1), delta(seq=2), delta(seq=3, change="-1.00")],
                fail_after=3),
            lambda: kx.FixtureTransport(
                [snapshot(seq=1), delta(seq=2), delta(seq=3, change="-1.00")]))
        kc.collect_once(make_config(tmp_path, max_reconnects=1),
                        transport_factory=factory)
        _, records = read_back(tmp_path)
        assert all(r["subscription_generation"] is not None for r in records)

        blinded = [{k: v for k, v in r.items()
                    if k != "subscription_generation"} for r in records]
        out = replay(blinded)
        assert out["faults"], "the pre-fix tape replayed clean; the proof above is vacuous"
        assert out["publishable"] == {M1: False}


# =====================================================================================
# PROOF 3 — a genuine in-generation gap STILL faults (the most important one)
# =====================================================================================
class TestAGenuineGapStillFaults:
    def test_a_gap_inside_one_generation_raises_and_unpublishes(self):
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=1))
        r.dispatch(record(delta(seq=2), generation=1))
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(record(delta(seq=9), generation=1))
        assert r.subscription.stats["gaps"] == 1
        assert r.subscription.stats["generation_advances"] == 0
        assert r.publishable_books() == {M1: False}
        assert r.books[M1].integrity_reason is not None

    def test_a_gap_still_faults_after_a_generation_boundary(self):
        """The boundary must not leave the detector disarmed behind it."""
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=1))
        r.dispatch(record(delta(seq=2), generation=1))
        r.dispatch(record(snapshot(seq=1), generation=2))    # reconnect
        r.dispatch(record(delta(seq=2), generation=2))
        assert r.publishable_books() == {M1: True}           # anti-vacuity
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(record(delta(seq=8), generation=2))
        assert r.subscription.stats["gaps"] == 1
        assert r.publishable_books() == {M1: False}

    def test_a_gap_of_exactly_one_message_still_faults(self):
        """The smallest real loss, next to the largest legitimate jump.

        Both messages are 'seq jumped'. One is a reconnect and one is a
        dropped message, and only the epoch tells them apart.
        """
        clean = router(generation=1)
        clean.dispatch(record(snapshot(seq=1), generation=1))
        clean.dispatch(record(delta(seq=2), generation=1))
        clean.dispatch(record(delta(seq=3, change="-1.00"), generation=1))
        assert clean.publishable_books() == {M1: True}       # anti-vacuity

        lossy = router(generation=1)
        lossy.dispatch(record(snapshot(seq=1), generation=1))
        lossy.dispatch(record(delta(seq=2), generation=1))
        with pytest.raises(bk.SubscriptionError):
            lossy.dispatch(record(delta(seq=4, change="-1.00"), generation=1))

    def test_a_straggler_from_a_superseded_generation_still_faults(self):
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=1))
        r.dispatch(record(snapshot(seq=1), generation=2))
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(record(delta(seq=2), generation=1))
        assert r.subscription.stats["stale_generation"] == 1
        assert r.publishable_books() == {M1: False}

    def test_a_new_generation_may_not_be_opened_by_a_delta(self):
        """A boundary explains a discontinuity; it does not order a delta.

        Without a snapshot the new generation has no base, so the delta is
        rejected rather than buffered — the same bounded rule the module
        applies everywhere else.
        """
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=1))
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(record(delta(seq=1), generation=2))
        assert r.subscription.generation == 2
        assert r.subscription.healthy is False
        assert r.publishable_books() == {M1: False}
        # ...and it recovers on the snapshot that should have led.
        r.dispatch(record(snapshot(seq=1), generation=2))
        r.dispatch(record(delta(seq=2), generation=2))
        assert r.publishable_books() == {M1: True}

    def test_the_epoch_itself_can_never_move_backwards(self):
        state = bk.SubscriptionState(4, generation=3)
        with pytest.raises(bk.SubscriptionError):
            state.supersede(generation=2)
        with pytest.raises(bk.SubscriptionError):
            state.supersede(generation=3)
        assert state.generation == 3
        assert state.supersede(generation=4) == 4            # anti-vacuity


# =====================================================================================
# PROOF 4 — records written before the field existed
# =====================================================================================
class TestLegacyRecordsReadAsTheSentinel:
    def test_an_absent_field_is_unknown_and_never_zero(self):
        assert bk.subscription_generation_of({}) is bk.GENERATION_UNKNOWN
        assert bk.subscription_generation_of(
            {"subscription_generation": None}) is bk.GENERATION_UNKNOWN
        # Unknown, never the first epoch: 0 would be a fabricated generation.
        assert bk.GENERATION_UNKNOWN is None
        assert bk.subscription_generation_of({}) != 0

    def test_a_pre_milestone_tape_still_replays(self):
        r = router(generation=1)
        for frame in (snapshot(seq=1), delta(seq=2),
                      delta(seq=3, change="-1.00")):
            r.dispatch(record(frame, generation=bk.GENERATION_UNKNOWN))
        assert r.publishable_books() == {M1: True}
        assert r.subscription.stats["stale_generation"] == 0
        assert r.subscription.stats["generation_advances"] == 0

    def test_a_pre_milestone_tape_still_detects_a_gap(self):
        """The sentinel must not become a licence to skip the check."""
        r = router(generation=1)
        r.dispatch(record(snapshot(seq=1), generation=bk.GENERATION_UNKNOWN))
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(record(delta(seq=5), generation=bk.GENERATION_UNKNOWN))
        assert r.subscription.stats["gaps"] == 1

    def test_an_envelope_built_without_epochs_round_trips_as_null(self, tmp_path):
        init_archive(tmp_path)
        store = EventArchive(tmp_path, environment=ENV, venue="kalshi")
        store.append(bk.make_envelope(
            venue="kalshi", environment=ENV, channel="orderbook_delta",
            message=snapshot(), receive_time=bk.utcnow(), receive_mono=1))
        store.close()
        durable = raw_records(tmp_path)
        assert durable[0]["subscription_generation"] is None
        assert durable[0]["connection_generation"] is None
        integrity, records = read_back(tmp_path)
        assert integrity["intact"] is True
        # The replay path does not crash on it, and does not invent an epoch.
        out = replay(records)
        assert out["faults"] == []
        assert out["publishable"] == {M1: True}

    def test_a_generation_that_is_not_an_int_is_refused_not_guessed(self):
        for bad in ("3", 3.0, True, False, -1, [3]):
            with pytest.raises(bk.SubscriptionError):
                bk.coerce_generation(bad)
        # Anti-vacuity: the permitted things ARE permitted.
        assert bk.coerce_generation(0) == 0
        assert bk.coerce_generation(9) == 9
        assert bk.coerce_generation(None) is bk.GENERATION_UNKNOWN


# =====================================================================================
# The epoch's own rule: it moves when a SUBSCRIPTION begins, not on any connect
# =====================================================================================
class TestTheEpochAdvancesOnlyOnASuccessfulSubscription:
    def test_a_failed_connect_attempt_consumes_no_epoch(self, tmp_path):
        init_archive(tmp_path)
        factory = SequenceOfTransports(
            DeadOnConnect,
            lambda: kx.FixtureTransport([snapshot(), delta()]))
        result = kc.collect_once(make_config(tmp_path, max_reconnects=1),
                                 transport_factory=factory)
        assert result.reconnects == 1
        assert result.events_archived == 2
        # The dead attempt never subscribed, so the first frame ever read is
        # still generation 1. An epoch no frame carries is a hole in the tape's
        # own numbering.
        assert {r["subscription_generation"] for r in raw_records(tmp_path)} == {1}
        assert {r["connection_generation"] for r in raw_records(tmp_path)} == {1}

    def test_the_epoch_does_not_move_per_frame(self, tmp_path):
        init_archive(tmp_path)
        kc.collect_once(
            make_config(tmp_path),
            transport_factory=lambda: kx.FixtureTransport(
                [snapshot(), delta(), delta(seq=3, change="-1.00"),
                 delta(seq=4, change="1.00")]))
        assert {r["subscription_generation"]
                for r in raw_records(tmp_path)} == {1}

    def test_each_successful_resubscription_advances_it_by_exactly_one(self, tmp_path):
        init_archive(tmp_path)
        factory = SequenceOfTransports(
            lambda: FailingTransport([snapshot(seq=1)], fail_after=1),
            lambda: FailingTransport([snapshot(seq=1)], fail_after=1),
            lambda: kx.FixtureTransport([snapshot(seq=1), delta(seq=2)]))
        result = kc.collect_once(make_config(tmp_path, max_reconnects=2),
                                 transport_factory=factory)
        assert result.reconnects == 2
        assert sorted(r["subscription_generation"]
                      for r in raw_records(tmp_path)) == [1, 2, 3, 3]
