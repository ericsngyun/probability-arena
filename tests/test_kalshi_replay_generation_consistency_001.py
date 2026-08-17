"""KALSHI-REPLAY-GENERATION-CONSISTENCY-001 — the invariant, proved per market.

> Within generation `g`, a market is not publishable until THAT MARKET itself
> has received its snapshot for `g`.

CP7 measured the violation on the live venue on 2026-08-17: at both forced
generation boundaries the first snapshot of the new epoch republished **all 60
markets at once, 59 of them still carrying pre-reconnect ladders**
(`docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-QUALIFICATION-REPORT.md` §3.3).
Harm was bounded to a ~36 ms window only because the venue happened to send all
60 snapshots before any delta — a sequencing accident, not a contract.

The six proofs this file carries, in the order the milestone requires them:

| # | proof | where |
|---|---|---|
| 1 | the invariant holds at a boundary, PER MARKET | `TestTheInvariantAtAGenerationBoundary` |
| 2 | the proof FAILS if the invariant is removed | `TestAntiVacuity` |
| 3 | cold start still acquires per market | `TestColdStartIsUnchanged` |
| 4 | the fault path still re-acquires per market | `TestTheFaultPathIsUnchanged` |
| 5 | a within-generation gap still faults and unpublishes | `TestTheDropDetectorIsNotBlinded` |
| 6 | replay agrees with live, on CP7's own frames | `TestReplayAgreesWithLive` |

**Doctrine 9 — provenance.** The order-book frames here are the venue's own
bytes, lifted verbatim from the committed CP7 capture
`docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-RUNS/s2-reconnect-session.json`
(`wire.orderbook_snapshots_with_ladder` and `wire.orderbook_snapshots_without_
ladder`) — the same session in which the defect was measured. Only `seq` is
re-numbered, because a stream has to be assembled from a subset; nothing else is
touched, and `TestFrameProvenance` re-reads the artifact and fails if the frames
this file uses ever stop matching it.

No test in this file opens a network connection.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import socket
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import book as bk
from app.realtime import ws_transport as wt
from app.realtime.archive import EventArchive, replay

from tests.test_kalshi_cp6_cp9_functional_001 import (
    ENV,
    StubTransport,
    checker,
    probe,
)
from tests.test_kalshi_collector_p0_fixes_001 import (
    ALL_CHANNELS,
    SID_ORDERBOOK,
    orderbook_delta,
    subscribed_acks,
)
from app.realtime import collector as kc

REPO = Path(__file__).resolve().parents[1]
CP7_ARTIFACT = (REPO / "docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-RUNS"
                / "s2-reconnect-session.json")

#: Doctrine 9. Six fields, and a drift detector below that recomputes the hashes
#: FROM the artifact rather than restating them.
CP7_PROVENANCE = {
    "capture_id": "cp7-forced-reconnect-test-instruments-60",
    "timestamp": "2026-08-17T17:42:02.999243+00:00",
    "venue": "kalshi",
    "environment": "demo",
    "channel": "orderbook_delta",
    "schema_version": 2,
    "artifact_path": str(CP7_ARTIFACT.relative_to(REPO)),
    "artifact_keys": ("wire.orderbook_snapshots_with_ladder",
                      "wire.orderbook_snapshots_without_ladder"),
    "mutated_fields": ("seq",),
}


@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*_a, **_k):  # pragma: no cover - a call IS the failure
        raise AssertionError("a generation-consistency test opened a connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


# =====================================================================================
# the venue's own frames
# =====================================================================================

def _artifact() -> dict:
    return json.loads(CP7_ARTIFACT.read_text())


def _cp7_snapshot_frames() -> dict:
    """One verbatim `orderbook_snapshot` per market, keyed by ticker.

    Both shapes the venue sends are kept: the laddered ones and the three that
    carry NEITHER ladder key. The ladderless markets matter here specifically —
    a book with zero levels is exactly the case where "awaiting my own snapshot
    for this generation" and "based, and the venue says empty" would be
    indistinguishable if publishability were a bare boolean (doctrine 10).
    """
    wire = _artifact()["wire"]
    frames: dict = {}
    for key in ("orderbook_snapshots_with_ladder",
                "orderbook_snapshots_without_ladder"):
        for entry in wire[key]:
            frame = entry["frame"]
            frames.setdefault(frame["msg"]["market_ticker"], copy.deepcopy(frame))
    return frames


CP7_SNAPSHOTS = _cp7_snapshot_frames()
#: Deterministic order: the order the venue itself sent them in generation 1.
MARKETS = tuple(CP7_SNAPSHOTS)
LADDERLESS = tuple(t for t, f in CP7_SNAPSHOTS.items()
                   if "yes_dollars_fp" not in f["msg"]
                   and "no_dollars_fp" not in f["msg"])


def snapshot_for(market: str, *, seq: int) -> dict:
    """The venue's verbatim frame for `market`, re-sequenced and nothing else."""
    frame = copy.deepcopy(CP7_SNAPSHOTS[market])
    frame["seq"] = seq
    return frame


def sanitized_frame_hash(frame: dict) -> str:
    body = {k: v for k, v in frame.items() if k != "seq"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


class TestFrameProvenance:
    """A fixture that cannot identify its empirical basis is synthetic data."""

    def test_the_named_capture_artifact_is_present_and_is_the_one_named(self):
        assert CP7_ARTIFACT.exists(), f"CP7 artifact missing: {CP7_ARTIFACT}"
        payload = _artifact()
        assert payload["run_label"] == CP7_PROVENANCE["capture_id"]
        assert payload["environment"] == CP7_PROVENANCE["environment"]
        assert payload["started_at"] == CP7_PROVENANCE["timestamp"]
        # The session these frames came from IS the session that measured the
        # defect: three subscription epochs, two forced teardowns.
        assert payload["subscription_epoch_final"] == 3
        assert len(payload["perturbation_journal"]) == 2

    def test_every_frame_used_here_still_matches_the_artifact(self):
        """THE DRIFT DETECTOR. Re-read the artifact and compare, modulo `seq`.

        `seq` is re-numbered because a stream is assembled from a subset of the
        session's 60 markets; if anything else ever differs, these frames have
        stopped being the venue's words and this fails rather than quietly
        certifying a fixture.
        """
        wire = _artifact()["wire"]
        by_ticker: dict = {}
        for key in CP7_PROVENANCE["artifact_keys"]:
            for entry in wire[key.split(".", 1)[1]]:
                by_ticker.setdefault(entry["frame"]["msg"]["market_ticker"],
                                     entry["frame"])
        assert set(by_ticker) == set(MARKETS)
        for market in MARKETS:
            assert (sanitized_frame_hash(snapshot_for(market, seq=999))
                    == sanitized_frame_hash(by_ticker[market])), market
            assert snapshot_for(market, seq=1)["sid"] == SID_ORDERBOOK, market

    def test_both_ladder_shapes_are_represented(self):
        """Doctrine 10 needs both, or the typed-absence proof is untested."""
        assert len(MARKETS) >= 8
        assert len(LADDERLESS) == 3, LADDERLESS
        laddered = [t for t in MARKETS if t not in LADDERLESS]
        assert laddered, "no laddered market survived the extraction"
        census = _artifact()["wire"]["orderbook_snapshot_ladder_census"]
        assert "yes=absent no=absent" in census


# =====================================================================================
# harness
# =====================================================================================

def init_archive(root):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def make_config(root, **kwargs):
    params = dict(environment=ENV, archive_root=root,
                  market_tickers=MARKETS, channels=ALL_CHANNELS,
                  max_seconds=60, max_events=10_000, max_reconnects=0,
                  reconnect_backoff_base_s=0.0)
    params.update(kwargs)
    return kc.CollectorConfig(**params)


def drive(root, streams, *, tap="plain", **kwargs):
    """One ObservedSession over `streams`, one stream per connection.

    The same harness the CP7 instrument used: the shipped `_Session`, the
    shipped routers, a read-only observer that records publishability
    TRANSITIONS and the frame that caused each one.
    """
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
            "recorder": recorder, "journal": journal, "timeline": timeline}


def generation(markets, *, first_seq=1, deltas=0, acks=True):
    """One subscription generation: acks, then one snapshot per market, then
    deltas spread over those same markets — the shape the venue sends."""
    frames = list(subscribed_acks()) if acks else []
    seq = first_seq
    for market in markets:
        frames.append(snapshot_for(market, seq=seq))
        seq += 1
    for n in range(deltas):
        frames.append(orderbook_delta(seq=seq, market=markets[n % len(markets)]))
        seq += 1
    return frames


def live_states(session) -> dict:
    """Typed per-market publication state, read from the collector's own router."""
    router = session._routers[SID_ORDERBOOK]
    return router.publication_states()


def acquisitions(timeline, *, epoch):
    """`(market, causing frame's market)` for every transition to publishable."""
    out = []
    for entry in timeline:
        if entry["subscription_epoch"] != epoch:
            continue
        for change in entry["changes"]:
            if change["to"] is True:
                out.append({"market": change["market_ticker"],
                            "caused_by": entry["cause"]["market_ticker"],
                            "cause_type": entry["cause"]["event_type"],
                            "cause_seq": entry["cause"]["seq"],
                            "changes_in_this_entry": len(entry["changes"])})
    return out


def assert_each_market_acquired_on_its_own_snapshot(timeline, *, epoch, expected):
    """THE PER-MARKET ASSERTION. Never in aggregate — the aggregate is what hid
    this defect for a whole qualification session."""
    acquired = acquisitions(timeline, epoch=epoch)
    assert [a["market"] for a in acquired] == list(expected), acquired
    for entry in acquired:
        assert entry["cause_type"] == "orderbook_snapshot", entry
        assert entry["caused_by"] == entry["market"], (
            f"{entry['market']} was republished on {entry['caused_by']}'s "
            "snapshot — a sibling's snapshot says nothing about this ladder")
        assert entry["changes_in_this_entry"] == 1, (
            "one frame flipped several markets at once; that is the CP7 "
            "defect, whatever the count")


# =====================================================================================
# PROOF 1 — the invariant at a generation boundary
# =====================================================================================

RESNAPSHOTTED = MARKETS[:4]
LEFT_BEHIND = MARKETS[4:]


def boundary_session(root):
    """Generation 1 bases every market; generation 2 re-snapshots only four.

    The four continue trading in the new epoch, so this is the case CP7 could
    not rule out on the live venue: new-generation deltas arriving while other
    markets still hold ladders from the abandoned one.
    """
    gen1 = generation(MARKETS, deltas=6)
    gen2 = generation(RESNAPSHOTTED, deltas=4)
    return drive(root, [gen1, gen2], tap="close", close_budget=1,
                 after_frames=len(gen1), max_reconnects=1)


class TestTheInvariantAtAGenerationBoundary:
    def test_only_the_re_snapshotted_markets_are_publishable(self, tmp_path):
        init_archive(tmp_path)
        out = boundary_session(tmp_path)
        assert out["session"].subscription_epoch == 2
        assert out["journal"][0]["event"] == "forced_socket_close"

        states = live_states(out["session"])
        assert set(states) == set(MARKETS)
        for market in RESNAPSHOTTED:
            assert states[market].publishable is True, market
            assert states[market].state == bk.PUB_PUBLISHABLE
            assert states[market].based_generation == 2
        for market in LEFT_BEHIND:
            assert states[market].publishable is False, market

    def test_the_state_of_a_left_behind_market_is_TYPED_and_carries_both_epochs(
            self, tmp_path):
        """Doctrine 10. "Awaiting my own snapshot for generation 2" is a named
        state, not an error and not a silent False — and it is NOT the same word
        the module uses for a broken book, or a consumer could not tell a
        reconnect from data loss."""
        init_archive(tmp_path)
        out = boundary_session(tmp_path)
        states = live_states(out["session"])

        for market in LEFT_BEHIND:
            state = states[market]
            assert state.state == bk.PUB_AWAITING_GENERATION_SNAPSHOT, market
            assert state.state != bk.PUB_BOOK_HALTED
            assert state.based_generation == 1, market
            assert state.subscription_generation == 2, market
            assert "abandoned" in state.reason
            # Nothing is WRONG with the book. It is simply not current.
            book = out["session"]._routers[SID_ORDERBOOK].books[market]
            assert book.integrity_reason is None, market
            assert book.synced is True, market
            assert book.stats["generation_boundaries"] == 1, market

    def test_a_zero_level_book_is_not_confused_with_an_unbased_one(self, tmp_path):
        """The three ladderless markets are the doctrine-10 case: both produce a
        book with no levels, and only one of them is an observation."""
        init_archive(tmp_path)
        out = drive(tmp_path, [generation(MARKETS, deltas=4)])
        states = live_states(out["session"])

        for market in LADDERLESS:
            assert states[market].publishable is True, market
            assert states[market].state == bk.PUB_PUBLISHABLE
            book = out["session"]._routers[SID_ORDERBOOK].books[market]
            assert len(book.yes) == 0 and len(book.no) == 0
            assert book.ladder_presence == {"yes": bk.LADDER_OMITTED,
                                            "no": bk.LADDER_OMITTED}
        # ...and the same markets after a boundary they were not re-snapshotted
        # into read differently, on the same zero levels.
        out2 = boundary_session(tmp_path)
        after = live_states(out2["session"])
        for market in LADDERLESS:
            assert market in LEFT_BEHIND
            assert after[market].state == bk.PUB_AWAITING_GENERATION_SNAPSHOT

    def test_each_market_regains_publishability_on_its_OWN_snapshot_only(
            self, tmp_path):
        """The per-market timeline: four separate entries, one change each.

        CP7's finding was a SINGLE entry carrying 60 changes. Asserting the
        shape of the transition log, not just the terminal state, is what makes
        that specific failure impossible to reproduce silently.
        """
        init_archive(tmp_path)
        out = boundary_session(tmp_path)
        assert_each_market_acquired_on_its_own_snapshot(
            out["timeline"], epoch=2, expected=RESNAPSHOTTED)
        # No market that was never re-snapshotted appears as an acquisition.
        assert not (set(a["market"] for a in acquisitions(out["timeline"], epoch=2))
                    & set(LEFT_BEHIND))

    def test_a_new_generation_delta_is_refused_rather_than_applied_to_the_old_ladder(
            self, tmp_path):
        """The serious case CP7 could not rule out, forced directly.

        A delta from generation 2 landing on a generation-1 ladder does not
        merely serve stale depth: it fabricates a book that existed at no
        instant. Here the ONLY generation-2 frame for the market is a delta.
        """
        init_archive(tmp_path)
        victim = LEFT_BEHIND[0]
        gen1 = generation(MARKETS, deltas=4)
        gen2 = ([snapshot_for(RESNAPSHOTTED[0], seq=1)]
                + [orderbook_delta(seq=2, market=victim)])
        out = drive(tmp_path, [gen1, list(subscribed_acks()) + gen2],
                    tap="close", close_budget=1, after_frames=len(gen1),
                    max_reconnects=1)

        book = out["session"]._routers[SID_ORDERBOOK].books[victim]
        assert book.stats["rejected_pre_generation_snapshot"] == 1
        assert book.stats["deltas"] == gen1_deltas_for(victim, gen1)
        # Refused, NOT halted: nothing is broken and the counters must keep
        # saying so, or a routine reconnect would file as an integrity fault.
        assert book.integrity_reason is None
        assert book.publication_state.state == bk.PUB_AWAITING_GENERATION_SNAPSHOT
        # ...and the collector counted it without ending the session or
        # answering it with a recovery command.
        assert out["result"].sequence_faults == 1
        assert out["result"].recoveries_requested == 0


def gen1_deltas_for(market, gen1) -> int:
    return sum(1 for f in gen1
               if f.get("type") == "orderbook_delta"
               and f["msg"]["market_ticker"] == market)


# =====================================================================================
# PROOF 2 — anti-vacuity: remove the invariant and the proof must go red
# =====================================================================================

class TestAntiVacuity:
    """A proof that cannot fail is not evidence.

    `based_for_current_generation` IS the invariant — publishability and the
    delta refusal both read it and nothing else does. Forcing it to `True`
    restores the pre-fix semantics exactly: `publishable_books()` becomes
    `book.publishable AND subscription.healthy`, one flag for every market.
    """

    @staticmethod
    def _remove_the_invariant(monkeypatch):
        monkeypatch.setattr(bk.OrderBook, "based_for_current_generation",
                            property(lambda self: True))

    def test_without_the_invariant_a_sibling_snapshot_republishes_everything(
            self, tmp_path, monkeypatch):
        init_archive(tmp_path)
        self._remove_the_invariant(monkeypatch)
        out = boundary_session(tmp_path)

        states = live_states(out["session"])
        # The CP7 defect, reproduced: every market publishable, including the
        # ones still holding generation-1 ladders.
        assert all(s.publishable for s in states.values())
        assert all(states[m].publishable for m in LEFT_BEHIND)

    def test_the_per_market_proof_FAILS_without_the_invariant(
            self, tmp_path, monkeypatch):
        """The same assertion helper proof 1 passes with must now raise."""
        init_archive(tmp_path)
        self._remove_the_invariant(monkeypatch)
        out = boundary_session(tmp_path)

        with pytest.raises(AssertionError):
            assert_each_market_acquired_on_its_own_snapshot(
                out["timeline"], epoch=2, expected=RESNAPSHOTTED)

    def test_the_typed_state_proof_FAILS_without_the_invariant(
            self, tmp_path, monkeypatch):
        init_archive(tmp_path)
        self._remove_the_invariant(monkeypatch)
        out = boundary_session(tmp_path)
        states = live_states(out["session"])

        with pytest.raises(AssertionError):
            for market in LEFT_BEHIND:
                assert states[market].state == bk.PUB_AWAITING_GENERATION_SNAPSHOT

    def test_the_replay_lane_reproduces_the_defect_without_the_invariant(
            self, tmp_path, monkeypatch):
        """The replay half. CP7 reported that replay reproduced the defect
        faithfully; with the invariant removed it must still do so, or proof 6's
        mid-boundary assertion is passing for some other reason."""
        init_archive(tmp_path)
        self._remove_the_invariant(monkeypatch)
        out = boundary_session(tmp_path)
        records = EventArchive(tmp_path, environment=ENV).read_verified()

        book_indices = _orderbook_indices(records)
        boundary = min(i for i in book_indices
                       if records[i]["subscription_generation"] == 2)
        at_boundary = replay(records[:boundary + 1])["publishable"]
        # ONE market has been re-snapshotted; all of them are publishable.
        assert records[boundary]["event_type"] == "orderbook_snapshot"
        assert all(at_boundary.values())
        assert len(at_boundary) == len(MARKETS)

    def test_without_the_invariant_a_new_generation_delta_lands_on_the_old_ladder(
            self, tmp_path, monkeypatch):
        """The harm, made visible: the pre-fix code APPLIES the delta."""
        init_archive(tmp_path)
        self._remove_the_invariant(monkeypatch)
        victim = LEFT_BEHIND[0]
        gen1 = generation(MARKETS, deltas=4)
        gen2 = list(subscribed_acks()) + [
            snapshot_for(RESNAPSHOTTED[0], seq=1),
            orderbook_delta(seq=2, market=victim)]
        out = drive(tmp_path, [gen1, gen2], tap="close", close_budget=1,
                    after_frames=len(gen1), max_reconnects=1)

        book = out["session"]._routers[SID_ORDERBOOK].books[victim]
        assert book.stats["rejected_pre_generation_snapshot"] == 0
        assert book.stats["deltas"] == gen1_deltas_for(victim, gen1) + 1
        assert book.publishable is True          # ...and it would be published


# =====================================================================================
# PROOF 3 — cold start is unchanged
# =====================================================================================

class TestColdStartIsUnchanged:
    def test_every_market_acquires_separately_on_its_own_snapshot(self, tmp_path):
        """CP7 measured 60 separate acquisitions at cold start and the fix must
        not have moved that — it is the behaviour being generalised, not
        replaced."""
        init_archive(tmp_path)
        out = drive(tmp_path, [generation(MARKETS, deltas=8)])

        assert out["session"].subscription_epoch == 1
        assert out["result"].reconnects == 0
        assert out["result"].sequence_faults == 0
        assert_each_market_acquired_on_its_own_snapshot(
            out["timeline"], epoch=1, expected=MARKETS)
        states = live_states(out["session"])
        assert all(s.publishable for s in states.values())
        assert all(s.based_generation == 1 for s in states.values())

    def test_a_market_is_not_publishable_before_its_first_snapshot(self, tmp_path):
        """The cold-start half of the same invariant: generation 1 is not
        special, it is simply the generation whose base nobody has yet."""
        router = bk.SubscriptionRouter(
            bk.SubscriptionState(SID_ORDERBOOK, market_tickers=MARKETS,
                                 generation=1))
        first, second = MARKETS[0], MARKETS[1]
        router.dispatch(_record(snapshot_for(first, seq=1), generation=1))
        states = router.publication_states()
        assert states[first].publishable is True
        assert second not in states                  # no book exists yet
        router.dispatch(_record(snapshot_for(second, seq=2), generation=1))
        assert router.publishable_books() == {first: True, second: True}


def _record(frame: dict, *, generation) -> dict:
    """One archived envelope's worth of fields, as `replay()` reads them."""
    etype = frame["type"]
    return {"event_type": etype, "sid": frame.get("sid"), "seq": frame.get("seq"),
            "market_ticker": frame["msg"].get("market_ticker"),
            "subscription_generation": generation, "raw": frame}


# =====================================================================================
# PROOF 4 — the fault path is unchanged
# =====================================================================================

class TestTheFaultPathIsUnchanged:
    def test_a_recovery_re_acquires_each_market_on_its_own_snapshot(self, tmp_path):
        """CP7's `s3-drop` showed 60 separate re-acquisitions after a real gap.

        A gap unpublishes every book at once — the lost message could have
        belonged to any of them — and then each one comes back on its own
        recovery snapshot. That already held; this pins it so the generation
        work cannot have collapsed it into a single flag.
        """
        init_archive(tmp_path)
        stream = generation(MARKETS, deltas=6)
        # The venue's answer to `update_subscription`: a fresh snapshot per
        # market, continuing the same generation's sequence.
        next_seq = max(f["seq"] for f in stream if f.get("seq") is not None) + 1
        for market in MARKETS:
            stream.append(snapshot_for(market, seq=next_seq))
            next_seq += 1

        out = drive(tmp_path, [stream], tap="drop", drop_budget=1, arm_after=2)
        assert out["journal"][0]["event"] == "dropped_frame"
        assert out["result"].sequence_faults >= 1
        assert out["result"].recoveries_requested == 1

        # Every market re-acquired, each on its own snapshot, inside ONE
        # generation — so this is the fault path, not a boundary.
        assert out["session"].subscription_epoch == 1
        acquired = acquisitions(out["timeline"], epoch=1)
        recovery = acquired[len(MARKETS):]
        assert [a["market"] for a in recovery] == list(MARKETS), recovery
        for entry in recovery:
            assert entry["caused_by"] == entry["market"], entry
            assert entry["changes_in_this_entry"] == 1, entry

    def test_the_same_stream_without_the_drop_faults_zero_times(self, tmp_path):
        """The paired control, so "faults >= 1" is a property of the drop."""
        init_archive(tmp_path)
        out = drive(tmp_path, [generation(MARKETS, deltas=6)])
        assert out["result"].sequence_faults == 0
        assert out["result"].recoveries_requested == 0


# =====================================================================================
# PROOF 5 — the drop detector is not blinded
# =====================================================================================

class TestTheDropDetectorIsNotBlinded:
    """A fix that makes a fault counter read zero is indistinguishable from a
    fix that broke the counter."""

    def _router(self):
        return bk.SubscriptionRouter(
            bk.SubscriptionState(SID_ORDERBOOK, market_tickers=MARKETS,
                                 generation=1))

    def test_a_gap_inside_one_generation_still_raises_and_unpublishes(self):
        router = self._router()
        for i, market in enumerate(MARKETS, start=1):
            router.dispatch(_record(snapshot_for(market, seq=i), generation=1))
        assert all(router.publishable_books().values())        # anti-vacuity

        with pytest.raises(bk.SubscriptionError):
            router.dispatch(_record(
                orderbook_delta(seq=len(MARKETS) + 5, market=MARKETS[0]),
                generation=1))

        assert router.subscription.stats["gaps"] == 1
        assert router.subscription.stats["generation_advances"] == 0
        assert not any(router.publishable_books().values())
        # A LOSS, and it must not be dressed up as the benign boundary state.
        for market, state in router.publication_states().items():
            assert state.state == bk.PUB_BOOK_HALTED, market
            assert state.state != bk.PUB_AWAITING_GENERATION_SNAPSHOT
            assert router.books[market].integrity_reason is not None

    def test_a_gap_after_a_boundary_still_faults(self):
        """The boundary must not leave the detector disarmed behind it."""
        router = self._router()
        for i, market in enumerate(MARKETS, start=1):
            router.dispatch(_record(snapshot_for(market, seq=i), generation=1))
        for i, market in enumerate(MARKETS, start=1):
            router.dispatch(_record(snapshot_for(market, seq=i), generation=2))
        assert all(router.publishable_books().values())        # anti-vacuity

        with pytest.raises(bk.SubscriptionError):
            router.dispatch(_record(
                orderbook_delta(seq=len(MARKETS) + 9, market=MARKETS[0]),
                generation=2))
        assert router.subscription.stats["gaps"] == 1
        assert not any(router.publishable_books().values())

    def test_a_straggler_from_the_superseded_generation_still_faults(self):
        router = self._router()
        router.dispatch(_record(snapshot_for(MARKETS[0], seq=1), generation=1))
        router.dispatch(_record(snapshot_for(MARKETS[0], seq=1), generation=2))
        with pytest.raises(bk.SubscriptionError):
            router.dispatch(_record(orderbook_delta(seq=2, market=MARKETS[0]),
                                    generation=1))
        assert router.subscription.stats["stale_generation"] == 1
        assert router.publishable_books() == {MARKETS[0]: False}

    def test_a_legacy_tape_without_the_epoch_still_publishes_and_still_detects(self):
        """The `GENERATION_UNKNOWN` sentinel must not become either a permanent
        awaiting state or a licence to skip the sequence check."""
        router = self._router()
        for i, market in enumerate(MARKETS, start=1):
            router.dispatch(_record(snapshot_for(market, seq=i),
                                    generation=bk.GENERATION_UNKNOWN))
        assert all(router.publishable_books().values())
        with pytest.raises(bk.SubscriptionError):
            router.dispatch(_record(
                orderbook_delta(seq=len(MARKETS) + 5, market=MARKETS[0]),
                generation=bk.GENERATION_UNKNOWN))
        assert router.subscription.stats["gaps"] == 1


# =====================================================================================
# PROOF 6 — replay agrees with live
# =====================================================================================

def _orderbook_indices(records):
    return [i for i, r in enumerate(records)
            if r.get("event_type") in ("orderbook_snapshot", "orderbook_delta")]


def _live_publishability_by_ordinal(timeline, frames: int) -> list:
    """Replay the live TRANSITION log back into a per-frame state.

    The observer records changes, not states — a per-frame dump would be a rate
    measurement in disguise. Accumulating them recovers the state after every
    frame, which is what the replay lane can be compared against.
    """
    states, current = [{}], {}
    by_ordinal = {entry["frame_ordinal"]: entry for entry in timeline}
    for ordinal in range(1, frames + 1):
        entry = by_ordinal.get(ordinal)
        if entry is not None:
            for change in entry["changes"]:
                current[change["market_ticker"]] = change["to"]
        states.append(dict(current))
    return states


class TestReplayAgreesWithLive:
    """`publishable_books()` has one caller in `app/`: `archive.replay()`.

    After the fix both lanes must reach the same per-market publishability from
    the same tape — and must do so at the same FRAME, not merely at the end,
    because the end of a tape is exactly where a boundary defect has already
    healed itself.
    """

    def _session(self, tmp_path):
        init_archive(tmp_path)
        out = boundary_session(tmp_path)
        records = EventArchive(tmp_path, environment=ENV).read_verified()
        return out, records

    def test_terminal_state_is_equal_and_the_split_is_not_trivial(self, tmp_path):
        out, records = self._session(tmp_path)
        live = checker.live_flat_state({
            "live_terminal_state": probe.capture_state(out["session"]),
            "subscription_epoch_final": out["session"].subscription_epoch,
            "connection_generation_final": out["session"].connection_generation,
        })
        replayed = replay(records)
        equality = checker.compare_state(live, replayed)

        assert equality["markets_compared"] == len(MARKETS)
        # PER-MARKET equality, which is what this milestone is about: same
        # checksum, same publishability, same stats, on every market.
        assert equality["differences"] == [], equality["differences"]
        # The one thing that still differs is CP8 §4.5's known, pre-existing
        # non-conservation: `supersede()` counts a `recoveries` on the live side
        # and the tape has no record of a collector ACTION. Asserted exactly, so
        # it can neither grow silently nor be mistaken for this defect.
        for diff in equality["subscription_stat_differences"]:
            keys = {k for k in diff["live"]
                    if diff["live"][k] != diff["replay"][k]}
            assert keys == {"recoveries"}, diff
        # ...and equality is not trivially "everything False" or "everything
        # True" — the tape ends mid-boundary on purpose.
        published = {t for t, p in replayed["publishable"].items() if p}
        assert published == set(RESNAPSHOTTED)
        assert set(replayed["publishable"]) - published == set(LEFT_BEHIND)

    def test_the_typed_state_survives_the_round_trip(self, tmp_path):
        """Doctrine 10 through the replay lane: a consumer reading the tape must
        be able to tell "awaiting its own snapshot" from "halted"."""
        out, records = self._session(tmp_path)
        replayed = replay(records)
        live = live_states(out["session"])

        for market in MARKETS:
            state = replayed["publication_states"][market]
            assert state["state"] == live[market].state, market
            assert state["publishable"] == live[market].publishable, market
            assert state["based_generation"] == live[market].based_generation
            assert (state["subscription_generation"]
                    == live[market].subscription_generation)
        for market in LEFT_BEHIND:
            assert (replayed["publication_states"][market]["state"]
                    == bk.PUB_AWAITING_GENERATION_SNAPSHOT)
            assert replayed["checksums"][market] is None

    def test_the_two_lanes_agree_frame_by_frame_over_the_boundary(self, tmp_path):
        """Prefix replay against the live transition log, per market.

        MEASURED, not assumed: the two lanes observe the UNPUBLISH at different
        frames. The live collector supersedes its subscriptions the moment the
        resubscribe is accepted (`_begin_subscription_epoch`), which is a
        `subscribed` ack; the replay lane learns of the new epoch from the first
        record STAMPED with it, and skips non-orderbook frames entirely. So the
        comparison is made where both lanes can see the same thing — on
        order-book records — and the divergence window is asserted to contain
        nothing else.
        """
        out, records = self._session(tmp_path)
        book_indices = _orderbook_indices(records)
        # Record index i is frame ordinal i+1: every frame the collector handled
        # was archived, in order, none rejected.
        assert len(records) == out["result"].events_received

        live_after = _live_publishability_by_ordinal(out["timeline"], len(records))
        compared = 0
        for i in book_indices:
            replayed = replay(records[:i + 1])["publishable"]
            live = {t: p for t, p in live_after[i + 1].items() if t in replayed}
            assert replayed == live, (
                f"lanes disagree after record {i} "
                f"({records[i]['event_type']} {records[i].get('market_ticker')}, "
                f"generation {records[i]['subscription_generation']})")
            compared += 1
        assert compared == len(book_indices) > 2 * len(MARKETS)

        # ...and the comparison is not vacuous: at the first order-book record of
        # generation 2 exactly ONE market is publishable, and it is the one whose
        # own snapshot that record is.
        boundary = min(i for i in book_indices
                       if records[i]["subscription_generation"] == 2)
        at_boundary = replay(records[:boundary + 1])["publishable"]
        assert {t for t, p in at_boundary.items() if p} == {
            records[boundary]["market_ticker"]}

        # The divergence window the docstring names, asserted rather than
        # asserted-away: between the last generation-1 record and the first
        # generation-2 record there are only non-order-book frames — which is
        # why the two lanes can be compared at all.
        previous_book = max(i for i in book_indices if i < boundary)
        between = records[previous_book + 1:boundary]
        assert between, "no ack frames between the epochs; the window is untested"
        assert all(r["event_type"] not in ("orderbook_snapshot", "orderbook_delta")
                   for r in between)
        # In that window the lanes genuinely differ, and the difference is the
        # live collector being EARLIER, never later: it supersedes on the ack.
        assert all(live_after[j + 1][t] is False
                   for j in range(previous_book + 1, boundary)
                   for t in MARKETS)

    def test_the_tape_itself_is_unchanged_by_this_milestone(self, tmp_path):
        """A RECONSTRUCTION fix. Every frame is still archived, in order, with
        its epoch — the durability contract is not what was wrong."""
        out, records = self._session(tmp_path)
        assert out["result"].events_archived == out["result"].events_received
        assert out["result"].events_rejected == 0
        assert len(records) == out["result"].events_archived
        assert {r["subscription_generation"] for r in records} == {1, 2}
        # Including the deltas that were REFUSED by the books: a refusal is a
        # reconstruction verdict, never a reason to drop evidence.
        integrity = EventArchive(tmp_path, environment=ENV).verify()
        assert integrity["intact"] is True
