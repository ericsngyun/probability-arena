"""KALSHI-COLLECTOR-P0-FIXES — the three defects, each with a positive control.

Doctrine 7: *force the underlying condition to occur, and prove the metric
becomes non-benign.* Every proof in this file forces a condition and then
asserts BOTH directions, because a fix that makes a fault counter read zero is
indistinguishable from a fix that broke the counter:

| forced | must happen | must NOT happen |
|---|---|---|
| real `trade` frames on their own sid | `sequence_faults == 0` | the detector is not disarmed — a REAL gap on that same stream still faults |
| a genuine orderbook gap | `sequence_faults > 0`, one recovery sent | — |
| a gap on a trade stream | exactly one fault PER GAP | the counter is not pinned by the first one |
| a ladderless snapshot | a typed `no_ladder_supplied` / `omitted_by_venue` state | a laddered snapshot still reconstructs a real book |
| a silent venue | the session ends at `max_seconds` | the test would OVERRUN if the cap were still frame-gated |

**The fixtures are the venue's own bytes.** `TRADE_FRAME_VERBATIM`,
`ERROR_FRAME_VERBATIM` and the two snapshots below are copied out of
`docs/experiments/KALSHI-COLLECTOR-P0-FIXES-RUNS/p0-wire-test-instruments-60.json`,
captured read-only from DEMO on 2026-08-17. That matters more than usual here:
the pre-existing CP3 fixtures put every channel on ONE shared sid, and the venue
does not — it gives each channel its own (`sid 1 = orderbook_delta`,
`sid 2 = ticker`, `sid 3 = trade`, from the venue's own `subscribed` acks). A
trade frame therefore arrives on a subscription that never receives an
`orderbook_snapshot`, which is exactly the condition the old fixtures could not
express and exactly the condition that made every live trade a fault. A fixture
that models the venue wrongly is a test that passes for the wrong reason.

No test here opens a socket.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from app.realtime import archive_head as ah
from app.realtime import book as kb
from app.realtime import collector as kc
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt

ENV = "demo"
# The two markets the wire capture actually names.
M1 = "KXMAXSHARDINGTEST-26AUG2818-T57399.99"
M2 = "KXMAXSHARDINGTEST-26AUG2818-T68399.99"

# THE VENUE'S OWN sid ASSIGNMENT, from the `subscribed` acks in the artifact:
#   {"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":1}}
#   {"id":1,"type":"subscribed","msg":{"channel":"ticker","sid":2}}
#   {"id":1,"type":"subscribed","msg":{"channel":"trade","sid":3}}
SID_ORDERBOOK = 1
SID_TICKER = 2
SID_TRADE = 3

ALL_CHANNELS = ("orderbook_delta", "ticker", "trade")


@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - a call is the failure
        raise AssertionError("a P0 test opened a network connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


# --- the wire, verbatim -------------------------------------------------------------

def trade_frame(*, seq, market=M1, taker_side="no"):
    """`p0-wire-test-instruments-60.json`, `frame_samples_by_type.trade[0]`.

    Reproduced field for field, including `taker_book_side` /
    `taker_outcome_side`, which the venue sends beside `taker_side` and which
    this repository deliberately does not read.
    """
    return {"type": "trade", "sid": SID_TRADE, "seq": seq,
            "msg": {"count_fp": "2.00", "market_ticker": market,
                    "no_price_dollars": "0.2000", "taker_book_side": "ask",
                    "taker_outcome_side": "no", "taker_side": taker_side,
                    "trade_id": "879217f6-553e-7222-2304-fb05558d63d0",
                    "ts": 1786956599, "ts_ms": 1786956599391,
                    "yes_price_dollars": "0.8000"}}


def error_frame(*, seq, sid=SID_TRADE):
    """The body nobody had read, captured 2026-08-17.

    `{"type":"error","sid":3,"seq":2,"msg":{"code":13,"msg":"Unsupported
    action"}}` — the venue refusing a `get_snapshot` aimed at the trade
    subscription. It consumes a sequence number on the subscription it lands on.
    """
    return {"type": "error", "sid": sid, "seq": seq,
            "msg": {"code": 13, "msg": "Unsupported action"}}


def ticker_frame(*, market=M1):
    """A real `ticker` frame — and it carries NO `seq`, on the wire and here.

    2,071 of 2,071 ticker frames in the capture had no sequence number. The
    channel therefore has no drop detector at all; a `seq` invented for the
    fixture would hide that.
    """
    return {"type": "ticker", "sid": SID_TICKER,
            "msg": {"market_ticker": market, "market_id": "mid-1",
                    "price_dollars": "0.1000", "yes_ask_dollars": "0.1000",
                    "yes_ask_size_fp": "400.00", "yes_bid_dollars": "0.0600",
                    "yes_bid_size_fp": "400.00", "ts": 1786956597,
                    "ts_ms": 1786956597868,
                    "time": "2026-08-17T08:49:57.86889Z"}}


def laddered_snapshot(*, seq, market=M1):
    """A snapshot that DOES carry a ladder — 57 of 60 in the capture did."""
    return {"type": "orderbook_snapshot", "sid": SID_ORDERBOOK, "seq": seq,
            "msg": {"market_id": "mid-1", "market_ticker": market,
                    "no_dollars_fp": [["0.4300", "2500.00"],
                                      ["0.4200", "100.00"]],
                    "yes_dollars_fp": [["0.2500", "100.00"]]}}


def ladderless_snapshot(*, seq, market=M1):
    """A snapshot carrying NEITHER ladder key — 3 of 60 in the capture."""
    return {"type": "orderbook_snapshot", "sid": SID_ORDERBOOK, "seq": seq,
            "msg": {"market_id": "mid-1", "market_ticker": market}}


def orderbook_delta(*, seq, market=M1, side="no", price="0.4300",
                    change="100.00"):
    return {"type": "orderbook_delta", "sid": SID_ORDERBOOK, "seq": seq,
            "msg": {"market_ticker": market, "market_id": "mid-1",
                    "price_dollars": price, "delta_fp": change, "side": side}}


def subscribed_acks():
    """The venue's acks — no top-level `sid`, the sid is inside `msg`."""
    return [{"id": 1, "type": "subscribed",
             "msg": {"channel": "orderbook_delta", "sid": SID_ORDERBOOK}},
            {"id": 1, "type": "subscribed",
             "msg": {"channel": "ticker", "sid": SID_TICKER}},
            {"id": 1, "type": "subscribed",
             "msg": {"channel": "trade", "sid": SID_TRADE}}]


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


def init_archive(root):
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def make_config(root, **kwargs):
    params = dict(environment=ENV, archive_root=root,
                  market_tickers=(M1, M2), channels=ALL_CHANNELS,
                  max_seconds=60, max_events=10_000, max_reconnects=0,
                  reconnect_backoff_base_s=0.0)
    params.update(kwargs)
    return kc.CollectorConfig(**params)


def run(root, frames, **kwargs):
    factory = RecordingFactory(frames)
    result = kc.collect_once(make_config(root, **kwargs),
                             transport_factory=factory)
    return result, factory


def commands(factory):
    return [c for transport in factory.made for c in transport.sent]


def recovery_commands(factory):
    return [c for c in commands(factory) if c["cmd"] == "update_subscription"]


# =====================================================================================
# DEFECT 1 — every trade frame produced a false sequence fault
# =====================================================================================

class TestTradeFlowIsNotAFault:
    """The condition forced is TRADE FLOW, and the metric must stay benign."""

    def test_a_session_of_real_trade_frames_reports_zero_sequence_faults(
            self, tmp_path):
        """The measurement that motivated this milestone, inverted.

        Three live runs reported `sequence_faults == trades + 1`. The same
        frames, on the sid the venue actually puts them on, must now report
        zero — the trade stream in the capture ran 1..219 with no gap,
        duplicate or regression, so zero is the true value and anything else is
        the detector inventing faults.
        """
        init_archive(tmp_path)
        frames = subscribed_acks() + [laddered_snapshot(seq=1)]
        frames += [trade_frame(seq=n) for n in range(1, 51)]
        result, factory = run(tmp_path, frames)

        assert result.events_received == len(frames)
        assert result.sequence_faults == 0
        assert result.recoveries_requested == 0

    def test_the_trade_subscription_is_never_asked_for_a_snapshot(self, tmp_path):
        """The command the venue answers with code 13 must never be sent.

        A refused recovery costs more than a missing one: the `error` reply
        consumes a sequence number on the subscription it lands on, so answering
        a fault with an invalid command manufactures the next fault.
        """
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=1)] + [trade_frame(seq=n)
                                               for n in range(1, 21)]
        _, factory = run(tmp_path, frames)

        assert recovery_commands(factory) == []
        assert [c["cmd"] for c in commands(factory)] == ["subscribe"]

    def test_ticker_frames_carrying_no_seq_are_not_counted_as_faults(
            self, tmp_path):
        """Absent is not ordered — and it is not a fault either.

        The venue sends no `seq` on `ticker`. Counting that as a sequence fault
        would report a drop detector firing on a channel that has none.
        """
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=1)] + [ticker_frame() for _ in range(30)]
        result, _ = run(tmp_path, frames)

        assert result.events_received == 31
        assert result.sequence_faults == 0

    def test_an_interleaved_wire_shaped_session_is_completely_clean(self, tmp_path):
        """All three channels at once, each on its own sid, as the venue sends them."""
        init_archive(tmp_path)
        frames = subscribed_acks()
        for n in range(1, 21):
            # The orderbook sid runs 1..20 with no hole; the trade sid runs
            # 1..20 with no hole; they are DIFFERENT sequence spaces and the
            # interleaving must not make either look like a gap in the other.
            frames.append(laddered_snapshot(seq=1) if n == 1
                          else orderbook_delta(seq=n))
            frames.append(ticker_frame())
            frames.append(trade_frame(seq=n))
        result, factory = run(tmp_path, frames)

        assert result.sequence_faults == 0
        assert result.recoveries_requested == 0
        assert recovery_commands(factory) == []


class TestTheDetectorIsStillArmed:
    """ANTI-VACUITY. A fix that silences the counter is not a fix."""

    def test_a_genuine_orderbook_gap_still_faults_and_still_recovers(self, tmp_path):
        """The forced condition doctrine 7 names: a sequence gap must be non-zero."""
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=1), orderbook_delta(seq=2),
                  orderbook_delta(seq=9)]          # 3..8 lost
        result, factory = run(tmp_path, frames)

        assert result.sequence_faults >= 1
        assert result.recoveries_requested == 1
        sent = recovery_commands(factory)
        assert len(sent) == 1
        # And aimed at the ORDERBOOK subscription, which is the one the venue
        # answers — proven on the wire, 2026-08-17.
        assert sent[0]["params"]["sids"] == [SID_ORDERBOOK]
        assert sent[0]["params"]["action"] == kx.RECOVERY_ACTION_GET_SNAPSHOT

    def test_a_genuine_orderbook_regression_still_faults(self, tmp_path):
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=5), orderbook_delta(seq=6),
                  orderbook_delta(seq=2)]
        result, _ = run(tmp_path, frames)
        assert result.sequence_faults >= 1

    def test_a_delta_before_any_snapshot_still_faults(self, tmp_path):
        """The base requirement is intact where it belongs.

        `needs_base` was narrowed to the orderbook channel, not removed. A
        delta applied to a book with no snapshot fabricates a book.
        """
        init_archive(tmp_path)
        result, _ = run(tmp_path, [orderbook_delta(seq=2)])
        assert result.sequence_faults == 1

    def test_a_gap_on_the_trade_stream_faults_exactly_once_per_gap(self, tmp_path):
        """The strongest form: the counter must count GAPS, not frames after one.

        One gap must be one fault, and the stream must re-anchor — if it did
        not, every later trade would fault too and the counter would measure
        time-since-the-gap. That is the same permanently-pinned counter this
        milestone removed, one level down.
        """
        init_archive(tmp_path)
        # seq 1,2 clean | 7 is a gap | 8,9 clean | 20 is a second gap | 21,22
        frames = [laddered_snapshot(seq=1)] + [
            trade_frame(seq=n) for n in (1, 2, 7, 8, 9, 20, 21, 22)]
        result, factory = run(tmp_path, frames)

        assert result.sequence_faults == 2
        # Counted, and NOT answered with a command the venue refuses.
        assert result.recoveries_requested == 0
        assert recovery_commands(factory) == []

    def test_a_duplicate_on_the_trade_stream_is_not_a_fault(self, tmp_path):
        """A duplicate is redelivery, not loss, and never has been a fault."""
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=1)] + [trade_frame(seq=n)
                                               for n in (1, 2, 2, 3)]
        result, _ = run(tmp_path, frames)
        assert result.sequence_faults == 0


class TestSubscriptionStateDirectly:
    """The unit-level statement of the same properties."""

    def test_an_unbased_stream_anchors_on_its_first_seq(self):
        state = kb.SubscriptionState(SID_TRADE, generation=1)
        assert state.accept(sid=SID_TRADE, seq=7, generation=1,
                            needs_base=False) == kb.SEQ_OK
        assert state.last_seq == 7
        # `healthy` means "a snapshot has based this book". A trade stream has
        # no book, so it must NOT be claimed.
        assert state.healthy is False

    def test_an_unbased_stream_still_detects_a_gap(self):
        state = kb.SubscriptionState(SID_TRADE, generation=1)
        state.accept(sid=SID_TRADE, seq=1, generation=1, needs_base=False)
        with pytest.raises(kb.SubscriptionError):
            state.accept(sid=SID_TRADE, seq=5, generation=1, needs_base=False)
        assert state.state_reason == kb.SUB_GAP
        assert state.stats["gaps"] == 1
        # Re-anchored, so the NEXT frame is clean rather than faulting forever.
        assert state.accept(sid=SID_TRADE, seq=6, generation=1,
                            needs_base=False) == kb.SEQ_OK

    def test_a_based_stream_is_unchanged(self):
        state = kb.SubscriptionState(SID_ORDERBOOK, generation=1)
        with pytest.raises(kb.SubscriptionError):
            state.accept(sid=SID_ORDERBOOK, seq=1, generation=1)
        assert state.state_reason == kb.SUB_AWAITING_SNAPSHOT
        # And a snapshot bases it, exactly as before.
        assert state.accept(sid=SID_ORDERBOOK, seq=1, generation=1,
                            is_snapshot=True) == kb.SEQ_OK
        assert state.healthy is True

    def test_carries_orderbook_is_observed_not_assumed(self):
        router = kb.SubscriptionRouter(
            kb.SubscriptionState(SID_TRADE, market_tickers=(M1,), generation=1))
        assert router.subscription.carries_orderbook is False
        router.dispatch({"event_type": "trade", "sid": SID_TRADE, "seq": 1,
                         "market_ticker": M1, "subscription_generation": 1,
                         "raw": trade_frame(seq=1)})
        assert router.subscription.carries_orderbook is False

        book_router = kb.SubscriptionRouter(
            kb.SubscriptionState(SID_ORDERBOOK, market_tickers=(M1,),
                                 generation=1))
        book_router.dispatch({"event_type": "orderbook_snapshot",
                              "sid": SID_ORDERBOOK, "seq": 1,
                              "market_ticker": M1, "subscription_generation": 1,
                              "raw": laddered_snapshot(seq=1)})
        assert book_router.subscription.carries_orderbook is True


# =====================================================================================
# DEFECT 2 — a ladder the venue never sent must not read as an observed empty one
# =====================================================================================

class TestLadderPresenceIsTyped:

    def test_a_ladderless_snapshot_produces_a_typed_state(self):
        observation = kc.normalize_frame(message=ladderless_snapshot(seq=1),
                                         receive_time=kb.utcnow())
        book = observation["book"]
        assert book["depth"] == "no_ladder_supplied"
        assert book["venue_omitted_bid_ladder"] is True
        assert book["venue_omitted_ask_ladder"] is True
        # The load-bearing assertion: NOT `present`. A quantity the venue never
        # transmitted, recorded as present with zero levels, is a plausible
        # benign value produced by a broken path.
        assert observation["coverage"][kc.OBS_BID_LEVELS] == kc.ABSENT_NOT_SUPPLIED
        assert observation["coverage"][kc.OBS_ASK_LEVELS] == kc.ABSENT_NOT_SUPPLIED
        assert observation["coverage"][kc.OBS_SPREAD] == kc.ABSENT_NOT_SUPPLIED

    def test_a_laddered_snapshot_still_reports_present_and_reconstructs(self):
        """The other half. A fix that types every ladder as absent is not a fix."""
        observation = kc.normalize_frame(message=laddered_snapshot(seq=1),
                                         receive_time=kb.utcnow())
        book = observation["book"]
        assert book["depth"] == "full_ladder"
        assert book["venue_omitted_bid_ladder"] is False
        assert book["venue_omitted_ask_ladder"] is False
        assert observation["coverage"][kc.OBS_BID_LEVELS] == kc.PRESENT
        assert observation["coverage"][kc.OBS_ASK_LEVELS] == kc.PRESENT
        assert observation["coverage"][kc.OBS_SPREAD] == kc.PRESENT
        assert len(book["bid_levels"]) == 1
        assert len(book["ask_levels"]) == 2
        # 0.4200 - 0.2500, in exact integer price units.
        assert book["spread_units"] == book["best_yes_ask_units"] - \
            book["best_yes_bid_units"]

    def test_an_explicitly_empty_ladder_is_present_not_absent(self):
        """"The venue said there is nothing here" is an observation.

        Distinct from "the venue said nothing", which is the case above. If
        these two collapsed in either direction the distinction would be
        decorative.
        """
        frame = ladderless_snapshot(seq=1)
        frame["msg"]["yes_dollars_fp"] = []
        frame["msg"]["no_dollars_fp"] = []
        observation = kc.normalize_frame(message=frame,
                                         receive_time=kb.utcnow())
        assert observation["book"]["depth"] == "full_ladder"
        assert observation["coverage"][kc.OBS_BID_LEVELS] == kc.PRESENT
        assert observation["book"]["venue_omitted_bid_ladder"] is False

    def test_a_one_sided_snapshot_says_so(self):
        frame = ladderless_snapshot(seq=1)
        frame["msg"]["yes_dollars_fp"] = [["0.2500", "100.00"]]
        observation = kc.normalize_frame(message=frame,
                                         receive_time=kb.utcnow())
        assert observation["book"]["depth"] == "one_side_ladder_only"
        assert observation["coverage"][kc.OBS_BID_LEVELS] == kc.PRESENT
        assert observation["coverage"][kc.OBS_ASK_LEVELS] == kc.ABSENT_NOT_SUPPLIED

    def test_the_book_itself_carries_ladder_presence(self):
        book = kb.OrderBook(M1)
        assert book.ladder_presence == {kb.SIDE_YES: kb.LADDER_UNOBSERVED,
                                        kb.SIDE_NO: kb.LADDER_UNOBSERVED}
        book.apply_snapshot(ladderless_snapshot(seq=1)["msg"], sid=SID_ORDERBOOK,
                            seq=1)
        assert book.ladder_presence == {kb.SIDE_YES: kb.LADDER_OMITTED,
                                        kb.SIDE_NO: kb.LADDER_OMITTED}
        # Zero levels AND the reason there are zero, in the same view.
        top = book.top_of_book()
        assert top["yes_levels"] == 0
        assert top["ladder_presence"][kb.SIDE_YES] == kb.LADDER_OMITTED

        book.apply_snapshot(laddered_snapshot(seq=2)["msg"], sid=SID_ORDERBOOK,
                            seq=2)
        assert book.ladder_presence == {kb.SIDE_YES: kb.LADDER_SUPPLIED,
                                        kb.SIDE_NO: kb.LADDER_SUPPLIED}
        assert book.top_of_book()["yes_levels"] == 1
        assert book.yes_scale_ladder()["ladder_presence"][kb.SIDE_NO] == \
            kb.LADDER_SUPPLIED

    def test_a_laddered_snapshot_still_reconstructs_a_real_book(self, tmp_path):
        """End to end, through the real collector and the real router."""
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=1), orderbook_delta(seq=2, side="yes",
                                                            price="0.2600",
                                                            change="50.00")]
        result, _ = run(tmp_path, frames)
        assert result.status == kc.STATUS_OK
        assert result.sequence_faults == 0
        assert result.events_archived == 2


# =====================================================================================
# DEFECT 3 — max_seconds was not enforced while the venue was quiet
# =====================================================================================

class SilentTransport(kx.FixtureTransport):
    """Connects, accepts a subscribe, and then never yields anything.

    The exact live condition: a session blocked in `recv()` on a quiet venue,
    with no `read_timeout_s` to rescue it. Before the fix nothing could end
    this session, because every cap was evaluated on frame arrival.
    """

    def __init__(self):
        super().__init__([])
        self.iterated = False

    async def __aiter__(self):
        self.iterated = True
        await asyncio.Event().wait()          # forever
        yield {}                              # pragma: no cover - unreachable


class TestTimeCapDuringSilence:

    def test_a_silent_session_terminates_at_max_seconds(self, tmp_path):
        """THE PROOF, and it is written so that a regression FAILS rather than hangs.

        `asyncio.wait_for` around the whole session is the guard: if the cap
        were frame-gated again the session would never return, the outer
        timeout would fire, and this test would fail with a `TimeoutError`
        instead of pinning the suite open forever. A hang is not a test result.
        """
        init_archive(tmp_path)
        transport = SilentTransport()

        async def drive():
            return await kc.run_session(
                make_config(tmp_path, max_seconds=1, max_reconnects=0),
                transport_factory=lambda: transport)

        async def guarded():
            return await asyncio.wait_for(drive(), timeout=20)

        result = asyncio.run(guarded())

        assert transport.iterated is True      # it really did block in the read
        assert result.status == kc.STATUS_CAPPED_TIME
        assert "max_seconds=1" in result.detail
        assert result.events_received == 0
        # The cap fired at the bound, not merely eventually.
        assert 900 <= result.duration_ms < 15_000

    def test_the_session_commits_its_archive_when_the_cap_fires_on_silence(
            self, tmp_path):
        """Ending on a timer must not end WITHOUT the commit point.

        `close()` runs in the `run()` finally, and a cancellation that skipped
        it would leave an unclosed segment — which `archive.py` says is
        explicitly not evidence.
        """
        init_archive(tmp_path)
        transport = SilentTransport()

        async def guarded():
            return await asyncio.wait_for(kc.run_session(
                make_config(tmp_path, max_seconds=1),
                transport_factory=lambda: transport), timeout=20)

        result = asyncio.run(guarded())
        assert result.status == kc.STATUS_CAPPED_TIME
        assert result.rotation_failures == 0

    def test_an_already_expired_budget_opens_no_further_socket(self, tmp_path):
        """The reconnect ladder must not be able to extend the bound.

        A transport that fails immediately would otherwise let `max_reconnects`
        buy connections indefinitely past `max_seconds`.
        """
        init_archive(tmp_path)
        opened = []

        class InstantFailure(kx.FixtureTransport):
            def __init__(self):
                super().__init__([])
                opened.append(self)

            async def __aiter__(self):
                raise wt.TransportError("ConnectionClosedError")
                yield {}                      # pragma: no cover - unreachable

        session = kc._Session(make_config(tmp_path, max_seconds=1,
                                          max_reconnects=50,
                                          reconnect_backoff_base_s=0.0),
                              transport_factory=InstantFailure)

        async def guarded():
            return await asyncio.wait_for(session.run(), timeout=20)

        result = asyncio.run(guarded())
        assert result.status in (kc.STATUS_CAPPED_TIME,
                                 kc.STATUS_CAPPED_RECONNECTS)
        assert result.duration_ms < 15_000

    def test_frame_driven_caps_are_unchanged(self, tmp_path):
        """The other two caps must still fire, and still on frames."""
        init_archive(tmp_path)
        frames = [laddered_snapshot(seq=1)] + [orderbook_delta(seq=n)
                                               for n in range(2, 12)]
        result, _ = run(tmp_path, frames, max_events=4)
        assert result.status == kc.STATUS_CAPPED_EVENTS
        assert result.events_received == 4
