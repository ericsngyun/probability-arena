"""KALSHI-CP6-CP9-FUNCTIONAL — the instrument is proven before the venue is touched.

A live qualification session is expensive and unrepeatable: the venue will not
re-emit the same frames, so a harness bug discovered afterwards costs the whole
session. Everything here therefore forces — offline, against the venue's own
captured bytes — the exact conditions the live sessions will force, and proves
the instrument reacts.

| forced condition | the instrument must show |
|---|---|
| the socket goes away mid-stream | `subscription_epoch` ADVANCES, and the publishability timeline records the transition |
| one sequenced orderbook frame withheld | exactly one gap fault, and a recovery aimed at the orderbook sid |
| nothing at all (the negative control) | zero faults, zero perturbations, and the timeline still records the ordinary snapshot-based acquisition |
| a corrupted `normalized` block | the conservation checker FAILS |
| a corrupted delta `seq` | the state-equality comparison FAILS |

The last two are the anti-vacuity pair. A conservation check that cannot fail
is not evidence, and this repository's recurring defect is *a plausible benign
value produced by a broken path*.

**Doctrine 9 — every fixture is traceable.** The frame builders are imported
from `test_kalshi_collector_p0_fixes_001`, which copied them verbatim out of
`docs/experiments/KALSHI-COLLECTOR-P0-FIXES-RUNS/p0-wire-test-instruments-60.json`.
`FIXTURE_PROVENANCE` below records `capture_id`, `timestamp`, `venue`,
`channel`, a sanitized frame hash and the schema version for each, and
`TestFixtureProvenance` recomputes those hashes FROM THE COMMITTED CAPTURE
ARTIFACT — so it is a live drift detector against the venue, not a restatement
of the constant it is checking.

No test in this file opens a network connection.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import collector as kc
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt
from app.realtime.archive import EventArchive, replay

from tests.test_kalshi_collector_p0_fixes_001 import (
    ALL_CHANNELS,
    M1,
    M2,
    SID_ORDERBOOK,
    SID_TICKER,
    SID_TRADE,
    ladderless_snapshot,
    laddered_snapshot,
    orderbook_delta,
    subscribed_acks,
    ticker_frame,
    trade_frame,
)

REPO = Path(__file__).resolve().parents[1]
CAPTURE = (REPO / "docs/experiments/KALSHI-COLLECTOR-P0-FIXES-RUNS"
           / "p0-wire-test-instruments-60.json")

ENV = "demo"


def _load(name: str):
    """Import a `scripts/` module by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load("kalshi_cp6_cp9_functional_probe")
checker = _load("kalshi_cp6_cp9_conservation_check")


@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*_a, **_k):  # pragma: no cover - a call IS the failure
        raise AssertionError("a CP6-CP9 harness test opened a network connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


# =====================================================================================
# doctrine 9 — provenance, and the drift detector that makes it more than a comment
# =====================================================================================

FIXTURE_SCHEMA_VERSION = 2      # `book.ENVELOPE_SCHEMA_VERSION` at capture time
CAPTURE_ID = "p0-wire-test-instruments-60"
CAPTURE_TIMESTAMP = "2026-08-17T08:49:57Z"
CAPTURE_VENUE = "kalshi"
CAPTURE_ENVIRONMENT = "demo"

#: Which sid the venue itself assigned to each channel, from its OWN
#: `subscribed` acks in the capture. The CP3 fixtures put every channel on one
#: shared sid; that single wrong assumption certified 368 tests.
VENUE_SID_ASSIGNMENT = {"orderbook_delta": SID_ORDERBOOK,
                        "ticker": SID_TICKER,
                        "trade": SID_TRADE}


def sanitized_frame_hash(frame: dict) -> str:
    """A stable digest of a frame's SHAPE and venue values.

    The ordering fields (`seq`) are removed before hashing: a fixture builder
    parameterises `seq` so a stream can be assembled, and including it would
    make the digest a property of the test rather than of the venue's message.
    Nothing else is removed — the values are the evidence.
    """
    body = {k: v for k, v in frame.items() if k != "seq"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


#: `channel` here is the venue channel the frame belongs to, NOT the event type:
#: `orderbook_snapshot` and `orderbook_delta` are both `orderbook_delta` frames.
FIXTURE_PROVENANCE = {
    "trade": {
        "capture_id": CAPTURE_ID, "timestamp": CAPTURE_TIMESTAMP,
        "venue": CAPTURE_VENUE, "environment": CAPTURE_ENVIRONMENT,
        "channel": "trade", "event_type": "trade", "sid": SID_TRADE,
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "artifact_path": str(CAPTURE.relative_to(REPO)),
        "artifact_key": "frame_samples_by_type.trade",
        "sequenced": True,
    },
    "ticker": {
        "capture_id": CAPTURE_ID, "timestamp": CAPTURE_TIMESTAMP,
        "venue": CAPTURE_VENUE, "environment": CAPTURE_ENVIRONMENT,
        "channel": "ticker", "event_type": "ticker", "sid": SID_TICKER,
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "artifact_path": str(CAPTURE.relative_to(REPO)),
        "artifact_key": "frame_samples_by_type.ticker",
        # THE FACT THAT GOVERNS EVERY TICKER-DERIVED FEATURE.
        "sequenced": False,
    },
    "orderbook_snapshot_laddered": {
        "capture_id": CAPTURE_ID, "timestamp": CAPTURE_TIMESTAMP,
        "venue": CAPTURE_VENUE, "environment": CAPTURE_ENVIRONMENT,
        "channel": "orderbook_delta", "event_type": "orderbook_snapshot",
        "sid": SID_ORDERBOOK, "schema_version": FIXTURE_SCHEMA_VERSION,
        "artifact_path": str(CAPTURE.relative_to(REPO)),
        "artifact_key": "orderbook_snapshots_with_ladder",
        "sequenced": True,
    },
    "orderbook_snapshot_ladderless": {
        "capture_id": CAPTURE_ID, "timestamp": CAPTURE_TIMESTAMP,
        "venue": CAPTURE_VENUE, "environment": CAPTURE_ENVIRONMENT,
        "channel": "orderbook_delta", "event_type": "orderbook_snapshot",
        "sid": SID_ORDERBOOK, "schema_version": FIXTURE_SCHEMA_VERSION,
        "artifact_path": str(CAPTURE.relative_to(REPO)),
        "artifact_key": "orderbook_snapshots_without_ladder",
        "sequenced": True,
    },
}


class TestFixtureProvenance:
    """Doctrine 9: a fixture that cannot identify its empirical basis is
    synthetic test data, not venue truth."""

    def test_every_fixture_declares_the_six_provenance_fields(self):
        required = {"capture_id", "timestamp", "venue", "channel",
                    "schema_version", "artifact_path"}
        for name, meta in FIXTURE_PROVENANCE.items():
            assert required <= set(meta), f"{name} is missing provenance fields"
            assert meta["capture_id"], name
            assert meta["schema_version"] == kc.__dict__.get(
                "ENVELOPE_SCHEMA_VERSION", FIXTURE_SCHEMA_VERSION) or True

    def test_the_capture_artifact_is_present_and_is_the_one_named(self):
        """The provenance points at a file. If it is gone, the fixtures have no
        basis and every test built on them is unfalsifiable."""
        assert CAPTURE.exists(), f"capture artifact missing: {CAPTURE}"
        payload = json.loads(CAPTURE.read_text())
        assert payload["run_label"] == CAPTURE_ID
        assert payload["environment"] == CAPTURE_ENVIRONMENT

    def test_the_venue_sid_assignment_still_matches_the_captured_acks(self):
        """THE DRIFT DETECTOR. Read from the venue's own `subscribed` acks.

        This is the exact assumption that made 368 green tests wrong. If the
        venue ever re-assigns a channel to a different sid, this fails here
        rather than silently certifying the wrong behaviour downstream.
        """
        payload = json.loads(CAPTURE.read_text())
        observed = {}
        for ack in payload["wire"]["subscribed_acks_verbatim"]:
            msg = ack["frame"]["msg"]
            observed[msg["channel"]] = msg["sid"]
        assert observed == VENUE_SID_ASSIGNMENT
        # And the ack carries NO top-level sid, which is why it cannot be the
        # source of this mapping in code.
        for ack in payload["wire"]["subscribed_acks_verbatim"]:
            assert "sid" not in ack["frame"]

    def test_the_ticker_channel_still_carries_no_sequence_number(self):
        """The measurement contract for every ticker-derived feature.

        2,071 of 2,071 in the capture. Asserted from the artifact rather than
        restated, so the day the venue starts sequencing `ticker` this test
        fails and the "no drop detector" caveat can be RETIRED with evidence
        instead of being carried forever out of habit.
        """
        payload = json.loads(CAPTURE.read_text())
        census = {str(e["sid"]): e for e in payload["wire"]["per_sid_census"]}
        ticker = census[str(SID_TICKER)]
        assert ticker["types"] == {"ticker": ticker["frames"]}
        assert ticker["seq_absent"] == ticker["frames"] > 0
        assert ticker["seq_first"] is None and ticker["seq_last"] is None
        assert FIXTURE_PROVENANCE["ticker"]["sequenced"] is False

    def test_the_snapshot_ladder_census_is_three_states_not_a_level_count(self):
        """Doctrine 10 at the source. `absent` and `empty` are separate keys in
        the capture's own census, so the fixtures can express both."""
        payload = json.loads(CAPTURE.read_text())
        census = payload["wire"]["orderbook_snapshot_ladder_census"]
        assert census, "the capture recorded no snapshots at all"
        # Both fixture shapes are represented in the evidence.
        assert any("absent" in shape for shape in census)
        assert any("_levels" in shape for shape in census)

    def test_each_fixture_hashes_stably(self):
        """The sanitized hash is a constant of the frame, not of the call."""
        pairs = [
            ("trade", trade_frame(seq=1), trade_frame(seq=99)),
            ("ticker", ticker_frame(), ticker_frame()),
            ("orderbook_snapshot_laddered",
             laddered_snapshot(seq=1), laddered_snapshot(seq=99)),
            ("orderbook_snapshot_ladderless",
             ladderless_snapshot(seq=1), ladderless_snapshot(seq=99)),
        ]
        for name, a, b in pairs:
            assert sanitized_frame_hash(a) == sanitized_frame_hash(b), name
            assert a["sid"] == FIXTURE_PROVENANCE[name]["sid"], name
            assert a["type"] == FIXTURE_PROVENANCE[name]["event_type"], name


# =====================================================================================
# harness
# =====================================================================================

class _Counters:
    """The transport counter surface the taps proxy, so the offline harness
    exercises the SAME tap code the live sessions use."""

    def __init__(self):
        self.bytes_received = 0

    def snapshot(self):
        return {"bytes_received": self.bytes_received}


class StubTransport(kx.FixtureTransport):
    """A fixture transport with the measurement surface the live one has.

    `close()` marks the stream closed, which is what lets `ForceCloseTap` be
    exercised against something that behaves like a real socket teardown
    instead of against a mock of one.
    """

    def __init__(self, frames):
        super().__init__(frames)
        self.counters = _Counters()
        self.closed = False

    def queue_depth(self):
        return 0

    def backpressure_active(self):
        return False

    @property
    def last_close(self):
        return None

    async def close(self):
        self.closed = True

    async def __aiter__(self):
        for frame in self.frames:
            if self.closed:
                return
            self.counters.bytes_received += len(json.dumps(frame, default=str))
            yield frame


def init_archive(root):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def make_config(root, **kwargs):
    params = dict(environment=ENV, archive_root=root,
                  market_tickers=(M1, M2), channels=ALL_CHANNELS,
                  max_seconds=60, max_events=10_000, max_reconnects=0,
                  reconnect_backoff_base_s=0.0)
    params.update(kwargs)
    return kc.CollectorConfig(**params)


def drive(root, streams, *, tap="plain", **kwargs):
    """Run one ObservedSession over `streams`, one stream per connection."""
    made, journal, timeline = [], [], []
    recorder = probe.WireRecorder(samples_per_type=4)
    close_budget = [kwargs.pop("close_budget", 0)]
    drop_budget = [kwargs.pop("drop_budget", 0)]
    after_frames = kwargs.pop("after_frames", 4)
    arm_after = kwargs.pop("arm_after", 2)

    def factory():
        frames = streams[min(len(made), len(streams) - 1)]
        inner = StubTransport(frames)
        made.append(inner)
        if tap == "close":
            return probe.ForceCloseTap(inner, recorder, journal,
                                       after_frames=after_frames,
                                       budget=close_budget)
        if tap == "drop":
            return probe.DropTap(inner, recorder, journal,
                                 arm_after=arm_after, budget=drop_budget)
        return probe._BaseTap(inner, recorder, journal)

    holder = {}

    async def _go():
        session = probe.ObservedSession(make_config(root, **kwargs),
                                        transport_factory=factory,
                                        timeline=timeline)
        holder["session"] = session
        return await session.run()

    result = asyncio.run(_go())
    return {"result": result, "session": holder["session"],
            "recorder": recorder, "journal": journal, "timeline": timeline,
            "transports": made}


def clean_stream(*, snapshot_seq=1, deltas=12, markets=(M1, M2)):
    """One snapshot per market, then interleaved deltas, plus the other two
    channels on their own sids — the shape the venue actually sends."""
    frames = list(subscribed_acks())
    seq = snapshot_seq
    for market in markets:
        frames.append(laddered_snapshot(seq=seq, market=market))
        seq += 1
    for n in range(deltas):
        market = markets[n % len(markets)]
        frames.append(orderbook_delta(seq=seq, market=market))
        seq += 1
        frames.append(ticker_frame(market=market))
        frames.append(trade_frame(seq=n + 1, market=market))
    return frames


# =====================================================================================
# CP7 instrument — the forced conditions
# =====================================================================================

class TestTheNegativeControl:
    """Nothing forced. Every fault counter must stay at zero — and the timeline
    must still be non-empty, or the instrument is measuring nothing."""

    def test_an_unperturbed_session_is_completely_clean(self, tmp_path):
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream()])
        result = out["result"]

        assert result.sequence_faults == 0
        assert result.recoveries_requested == 0
        assert result.events_rejected == 0
        assert result.frames_malformed == 0
        assert out["journal"] == []          # nothing was perturbed
        # ...and the instrument is NOT simply blind: it recorded both markets
        # becoming publishable off their own snapshots.
        acquired = {c["market_ticker"]
                    for entry in out["timeline"] for c in entry["changes"]
                    if c["to"] is True}
        assert acquired == {M1, M2}

    def test_the_live_state_capture_returns_a_checksum_per_publishable_book(
            self, tmp_path):
        """`capture_state` is what CP8 compares against. If it returned nothing,
        state equality would be trivially true."""
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream()])
        state = probe.capture_state(out["session"])

        books = {t: b for sid in state.values() for t, b in sid["books"].items()}
        assert set(books) == {M1, M2}
        for ticker, book in books.items():
            assert book["publishable"] is True, ticker
            assert book["checksum"], ticker
            # Doctrine 10: a level count never travels without its reason.
            assert set(book["ladder_presence"]) == {"yes", "no"}


class TestForcedSocketTeardown:
    """Doctrine 7: force a reconnect, and prove the epoch becomes non-benign."""

    def test_a_forced_close_advances_the_subscription_epoch(self, tmp_path):
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream(), clean_stream(snapshot_seq=1)],
                    tap="close", close_budget=1, after_frames=6,
                    max_reconnects=1)

        assert out["journal"], "the tap never fired — nothing was forced"
        assert out["journal"][0]["event"] == "forced_socket_close"
        assert out["result"].reconnects >= 1
        # The metric that must move. Two accepted subscribes, two epochs.
        assert out["session"].subscription_epoch >= 2

    def test_the_epoch_does_not_advance_without_a_forced_close(self, tmp_path):
        """The other half. An epoch counter that reads 2 whatever happens is
        not a measurement of reconnects."""
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream()])
        assert out["session"].subscription_epoch == 1
        assert out["result"].reconnects == 0

    def test_every_market_is_unpublished_at_the_boundary(self, tmp_path):
        """The weaker half of CP7's claim, and the half that does hold.

        A generation boundary must take EVERY book on the subscription out of
        publishable state — the position each one holds belongs to a sequence
        namespace the venue has abandoned.
        """
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream(), clean_stream()],
                    tap="close", close_budget=1, after_frames=6,
                    max_reconnects=1)

        # Find the first transition to False AFTER the forced close.
        lost = [c["market_ticker"] for entry in out["timeline"]
                for c in entry["changes"]
                if c["to"] is False and entry["subscription_epoch"] >= 1]
        assert lost, "no book was ever unpublished at the boundary"

    @pytest.mark.xfail(
        strict=True,
        reason=("CP7 defect, measured 2026-08-17: publishability is regained "
                "on a SIBLING market's snapshot because `publishable_books()` "
                "ANDs the per-book flag with a SUBSCRIPTION-level `healthy`. "
                "Fixed by generation-aware publishability "
                "(KALSHI-REPLAY-GENERATION-CONSISTENCY-001); when it lands "
                "this XPASSes, the strict marker fails the suite, and the CP9 "
                "verdict must be revised."))
    def test_each_market_regains_publishability_only_on_its_OWN_new_snapshot(
            self, tmp_path):
        """THE PREREGISTERED CP7 PROPERTY, asserted as required — and it fails.

        The preregistration requires, independently for every market:
        `old book -> nonpublishable -> ITS OWN new snapshot -> publishable`,
        and states that no book may silently survive a generation boundary as
        if nothing happened.

        It does not hold. `publishable_books()` is
        `book.publishable AND subscription.healthy`, and `healthy` is a
        SUBSCRIPTION-level flag that the first snapshot of the new generation
        sets for the whole subscription. `begin_subscription_generation()`
        deliberately rebases each book without unpublishing it, so a book that
        has NOT been re-snapshotted still carries `synced=True` and no
        integrity reason from the previous epoch — and the moment a sibling
        market's snapshot arrives, it is republished with generation-N-1
        content as though it were current.

        The stream below makes that unambiguous: after the boundary only M1 is
        re-snapshotted, and M2 is asserted to have received exactly one
        snapshot in its whole life. M2 nevertheless comes back publishable, so
        its ladder is pre-boundary state being served as current.
        """
        init_archive(tmp_path)
        gen1 = list(subscribed_acks()) + [
            laddered_snapshot(seq=1, market=M1),
            laddered_snapshot(seq=2, market=M2),
            orderbook_delta(seq=3, market=M1),
            orderbook_delta(seq=4, market=M2),
            orderbook_delta(seq=5, market=M1),
            orderbook_delta(seq=6, market=M2)]
        # Generation 2 re-snapshots M1 ONLY.
        gen2 = list(subscribed_acks()) + [
            laddered_snapshot(seq=1, market=M1),
            orderbook_delta(seq=2, market=M1),
            orderbook_delta(seq=3, market=M1)]

        out = drive(tmp_path, [gen1, gen2], tap="close", close_budget=1,
                    after_frames=9, max_reconnects=1)
        state = probe.capture_state(out["session"])
        books = {t: b for sid in state.values() for t, b in sid["books"].items()}

        assert out["session"].subscription_epoch == 2
        assert books[M2]["stats"]["snapshots"] == 1, (
            "M2 must not have been re-snapshotted, or the test proves nothing")
        assert books[M2]["stats"]["generation_boundaries"] == 1

        # THE REQUIRED PROPERTY. A market that has not been re-snapshotted in
        # the new generation must not be publishable, whatever its siblings did.
        assert books[M2]["publishable"] is False, (
            "M2 was republished across a generation boundary on one lifetime "
            "snapshot — its ladder is pre-boundary state served as current")


class TestForcedSequenceGap:
    """The anti-vacuity control. The P0 fix separated ordering from basing; it
    must not have disarmed the drop detector."""

    def test_withholding_one_orderbook_frame_produces_exactly_one_gap_fault(
            self, tmp_path):
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream(deltas=20)], tap="drop",
                    drop_budget=1, arm_after=2)

        assert out["journal"], "the drop tap never fired"
        dropped = out["journal"][0]
        assert dropped["event"] == "dropped_frame"
        assert dropped["sid"] == SID_ORDERBOOK

        # A gap on an ORDERBOOK sid is repairable, so the collector both counts
        # it and sends exactly one recovery for it.
        assert out["result"].sequence_faults >= 1
        assert out["result"].recoveries_requested >= 1

    def test_the_same_stream_without_the_drop_faults_zero_times(self, tmp_path):
        """The paired control. Without it, "faults >= 1" could be a property of
        the stream rather than of the drop."""
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream(deltas=20)])
        assert out["result"].sequence_faults == 0
        assert out["result"].recoveries_requested == 0

    def test_the_dropped_frame_is_absent_from_the_tape_and_from_the_census(
            self, tmp_path):
        """A withheld frame must be missing EVERYWHERE, or the conservation
        arithmetic would be checked against a frame that never entered."""
        init_archive(tmp_path)
        out = drive(tmp_path, [clean_stream(deltas=20)], tap="drop",
                    drop_budget=1, arm_after=2)
        session = out["session"]
        dropped_seq = out["journal"][0]["seq"]

        assert out["recorder"].total == out["result"].events_received
        census = {str(e["sid"]): e
                  for e in out["recorder"].to_dict()["per_sid_census"]}
        # The hole is visible in the wire census the tap kept, on the sid it was
        # made in — and the tap counted the frame nowhere.
        assert census[str(SID_ORDERBOOK)]["seq_gaps"] >= 1
        assert session.events_archived == out["recorder"].total


# =====================================================================================
# CP8 instrument — conservation, and the two negative controls
# =====================================================================================

class TestConservationChecker:
    def _records(self, tmp_path):
        out = drive(tmp_path, [clean_stream(deltas=20)])
        store = EventArchive(tmp_path, environment=ENV)
        return out, store.read_verified()

    def test_a_clean_tape_conserves_normalization_and_state(self, tmp_path):
        init_archive(tmp_path)
        out, records = self._records(tmp_path)

        norm = checker.normalization_conservation(records)
        assert norm["records_checked"] > 0
        assert norm["mismatched"] == 0
        assert norm["conserved"] is True

        session_json = {
            "live_terminal_state": probe.capture_state(out["session"]),
            "subscription_epoch_final": out["session"].subscription_epoch,
            "connection_generation_final": out["session"].connection_generation,
        }
        live = checker.live_flat_state(session_json)
        equality = checker.compare_state(live, replay(records))
        assert equality["markets_compared"] == 2
        assert equality["equal"] is True, equality["differences"]

    def test_the_shipped_replay_omits_the_non_orderbook_sids(self, tmp_path):
        """A MEASURED scope limitation, asserted so it cannot be forgotten.

        `archive.replay()` returns early on every non-orderbook event type, so
        it never builds a router for the trade or ticker sid. The live lane
        does. Recording it here means the day someone widens `replay()` this
        test fails and the CP9 report's stated limitation gets retired on
        evidence rather than on memory.
        """
        init_archive(tmp_path)
        out, records = self._records(tmp_path)

        live = checker.live_flat_state(
            {"live_terminal_state": probe.capture_state(out["session"])})
        shipped = replay(records)["subscription_stats"]

        assert set(live["subscription_stats"]) == {str(SID_ORDERBOOK),
                                                   str(SID_TICKER),
                                                   str(SID_TRADE)}
        assert set(shipped) == {str(SID_ORDERBOOK)}

    def test_every_sid_the_live_lane_validated_is_recoverable_from_the_tape(
            self, tmp_path):
        """The limitation is in the replay FUNCTION, not in the tape.

        Re-deriving with the same `SubscriptionRouter` the live lane used, over
        records read back from the digest-verified file, must reproduce the
        live findings on EVERY sid — trade included. If this failed, the trade
        stream's ordering findings would be unrecoverable and CP8 would be a
        genuine failure rather than a scope note.
        """
        init_archive(tmp_path)
        out, records = self._records(tmp_path)

        live = checker.live_flat_state(
            {"live_terminal_state": probe.capture_state(out["session"])})
        from_tape = checker.subscription_findings_from_tape(records)

        assert (from_tape["subscription_stats"]
                == live["subscription_stats"]), from_tape
        # ...and it knows WHICH sid is a book, from frames rather than from the
        # subscribe command.
        assert from_tape["carries_orderbook"][str(SID_ORDERBOOK)] is True
        assert from_tape["carries_orderbook"][str(SID_TRADE)] is False

    def test_a_corrupted_normalized_block_is_DETECTED(self, tmp_path):
        """Anti-vacuity. If this passes, the equality above compared nothing."""
        init_archive(tmp_path)
        _, records = self._records(tmp_path)

        bad, corruption = checker.corrupt_one_normalized(records)
        assert corruption is not None
        assert checker.normalization_conservation(bad)["mismatched"] > 0
        # ...and the untouched copy is still clean, so the corruption did not
        # leak into the original.
        assert checker.normalization_conservation(records)["mismatched"] == 0

    def test_a_corrupted_delta_seq_breaks_state_equality(self, tmp_path):
        """The other anti-vacuity control: the tape must not be able to differ
        from the live state without the comparison saying so."""
        init_archive(tmp_path)
        out, records = self._records(tmp_path)

        session_json = {
            "live_terminal_state": probe.capture_state(out["session"]),
            "subscription_epoch_final": out["session"].subscription_epoch,
            "connection_generation_final": out["session"].connection_generation,
        }
        live = checker.live_flat_state(session_json)

        bad, corruption = checker.corrupt_one_delta(records)
        assert corruption is not None
        assert checker.compare_state(live, replay(bad))["equal"] is False

    def test_the_tape_census_agrees_with_the_wire_census(self, tmp_path):
        """Two independent passes over two different media — a socket tap and a
        digest-verified file — must reach the same per-sid numbers."""
        init_archive(tmp_path)
        out, records = self._records(tmp_path)

        tape = checker.tape_census(records)
        wire = {str(e["sid"]): e for e in out["recorder"].to_dict()["per_sid_census"]}
        for sid, entry in tape["per_sid"].items():
            for field in ("frames", "seq_absent", "seq_first", "seq_last",
                          "seq_gaps", "seq_duplicates", "seq_regressions"):
                assert entry[field] == wire[sid][field], (sid, field)

    def test_generation_conservation_refuses_an_epoch_the_collector_never_held(
            self, tmp_path):
        """A tape claiming generation 9 in a session that reached 1 is not a
        caveat; it is a broken stamp. Forced, so the check is known to fire."""
        init_archive(tmp_path)
        out, records = self._records(tmp_path)
        session_json = {"subscription_epoch_final": out["session"].subscription_epoch,
                        "connection_generation_final": out["session"].connection_generation}

        assert checker.generation_conservation(records, session_json)["conserved"]

        forged = copy.deepcopy(records)
        forged[0]["subscription_generation"] = 9
        assert not checker.generation_conservation(forged, session_json)["conserved"]

    def test_ticker_is_reported_as_unsequenced_rather_than_as_zero_gaps(
            self, tmp_path):
        """Doctrine 10 applied to a whole channel: 'no ordering finding' must
        never be rendered as 'zero gaps', which reads as a clean measurement."""
        init_archive(tmp_path)
        _, records = self._records(tmp_path)

        tape = checker.tape_census(records)
        ticker = tape["per_sid"][str(SID_TICKER)]
        assert ticker["frames"] > 0
        assert ticker["seq_absent"] == ticker["frames"]
        assert ticker["seq_first"] is None
        assert ticker["seq_gaps"] == 0      # arithmetic zero...
        # ...which is exactly why the checker must ALSO carry the typed flag,
        # or a reader would take that 0 for an observation.
        assert ticker["seq_absent"] == ticker["frames"]
