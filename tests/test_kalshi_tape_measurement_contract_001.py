"""KALSHI-TAPE-MEASUREMENT-CONTRACT-001 (P3) — guards for the contract's claims.

Narrow by design. This is a contract milestone, not a test-writing exercise, so
there is exactly one test per contract claim that had no guard, and **every one
of them carries its own anti-vacuity control**: a companion assertion proving
the measurement path can reach the other answer. A guard satisfied by a
repository in which nothing works is not a guard (AGENTS.md doctrine 4).

Three of these are CHARACTERIZATION tests. They pin behaviour the contract
reports as a defect (§11 B3, §11 B4) rather than behaviour it endorses. That is
deliberate and is the repository's own pattern: pinning a limitation is what
makes it **retire on evidence** instead of being carried forever in prose. When
either defect is fixed, its test turns red, and the person fixing it is
required to delete the corresponding paragraph from the contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.realtime.archive import EventArchive, replay
from app.realtime.book import make_envelope
from app.realtime.book import (
    PUB_AWAITING_GENERATION_SNAPSHOT,
    PUB_BOOK_HALTED,
    SubscriptionRouter,
    SubscriptionState,
)
from app.realtime.collector_metrics import (
    ALLOWED_FIELDS,
    CollectorMetrics,
    MetricsFlusher,
)
from app.realtime.segment import RECORD_FIELDS

MARKET = "KXTEST-26AUG17-A"


def snapshot(seq: int, *, generation: int = 1, ticker: str = MARKET) -> dict:
    return {
        "event_type": "orderbook_snapshot", "sid": 1, "seq": seq,
        "market_ticker": ticker, "subscription_generation": generation,
        "raw": {"msg": {"market_ticker": ticker,
                        "yes_dollars_fp": [["0.4000", "5.00"]],
                        "no_dollars_fp": [["0.6000", "5.00"]]}},
    }


def delta(seq: int, *, generation: int = 1, ticker: str = MARKET) -> dict:
    return {
        "event_type": "orderbook_delta", "sid": 1, "seq": seq,
        "market_ticker": ticker, "subscription_generation": generation,
        "raw": {"msg": {"market_ticker": ticker, "side": "yes",
                        "price_dollars": "0.4000", "delta_fp": "1.00"}},
    }


def error_frame(seq: int, *, generation: int = 1) -> dict:
    """The shape captured on the DEMO wire 2026-08-08.

    `{"type":"error","sid":4,"seq":4,...}` arrived between deltas at seq 3 and
    seq 5 — an error frame on the ORDERBOOK sid, consuming a sequence number in
    that sid's space.
    """
    return {
        "event_type": "error", "sid": 1, "seq": seq, "market_ticker": None,
        "subscription_generation": generation,
        "raw": {"msg": {"code": 13, "msg": "Unsupported action"}},
    }


def drive_live(records) -> tuple[int, dict]:
    """The LIVE lane: every frame goes through `dispatch`, as the collector does."""
    router = SubscriptionRouter(
        SubscriptionState(1, market_tickers=(MARKET,), generation=1))
    faults = 0
    for record in records:
        try:
            router.dispatch(record)
        except Exception:                     # noqa: BLE001 - counted, as live does
            faults += 1
    return faults, router.publishable_books()


# =====================================================================================
# §1 — the book-replay / tape-replay boundary
# =====================================================================================


class TestReplayIsOrderbookOnly:
    """`archive.replay()` is BOOK replay. The contract says so; this pins it."""

    def test_a_non_orderbook_sid_never_becomes_a_subscription(self):
        ticker_record = {
            "event_type": "ticker", "sid": 2, "seq": None,
            "market_ticker": MARKET, "subscription_generation": 1,
            "raw": {"msg": {"market_ticker": MARKET,
                            "yes_bid_dollars": "0.4000",
                            "yes_ask_dollars": "0.6000"}},
        }
        trade_record = {
            "event_type": "trade", "sid": 3, "seq": 1,
            "market_ticker": MARKET, "subscription_generation": 1,
            "raw": {"msg": {"market_ticker": MARKET, "taker_side": "yes",
                            "yes_price_dollars": "0.5000",
                            "count_fp": "2.00"}},
        }
        out = replay([snapshot(1), ticker_record, trade_record, delta(2)])

        # The claim: three sids on the wire, ONE subscription in the output.
        assert out["subscriptions"] == 1
        assert set(out["subscription_stats"]) == {"1"}

        # ANTI-VACUITY: the orderbook sid really was processed, so the "1"
        # above is a scope limit and not a replay that did nothing at all.
        assert out["events_applied"] == 2
        assert out["publishable"] == {MARKET: True}

    def test_the_tape_itself_is_not_the_limitation(self):
        """CP8 §4.6: the same records DO re-derive the trade sid's ordering.

        The gap is in `replay()`'s scope, not in the evidence. Driving the very
        same durable records through the same router the live lane uses
        reproduces the trade sid — which is what makes §1's "the tape is not the
        limitation" an assertion rather than a hope.
        """
        trades = [{
            "event_type": "trade", "sid": 3, "seq": n,
            "market_ticker": MARKET, "subscription_generation": 1,
            "raw": {"msg": {"market_ticker": MARKET, "taker_side": "yes",
                            "yes_price_dollars": "0.5000",
                            "count_fp": "2.00"}},
        } for n in (1, 2, 3)]

        router = SubscriptionRouter(SubscriptionState(3, generation=1))
        for record in trades:
            router.dispatch(record)

        assert router.subscription.stats["accepted"] == 3
        assert router.subscription.stats["gaps"] == 0
        # ANTI-VACUITY: the detector on this sid is armed, not merely quiet.
        with pytest.raises(Exception):
            router.dispatch({**trades[0], "seq": 99})
        assert router.subscription.stats["gaps"] == 1


# =====================================================================================
# §11 B3 — CHARACTERIZATION: an error frame on the orderbook sid diverges
# =====================================================================================


class TestErrorFrameSequenceDivergence:
    """`replay()` skips non-orderbook frames BEFORE dispatch, so it never
    consumes the sequence number an `error` frame occupies. The live lane does.

    Recorded in KALSHI-REPLAY-GENERATION-CONSISTENCY-001 as STILL DEBT and
    escalated by this contract to a P4 blocker (§11 B3), because it makes replay
    **manufacture a sequence gap that never happened** — replay reports loss the
    venue never caused.

    THIS TEST PINS THE DEFECT. Fixing `replay()` to consume the sequence number
    (mirroring `SubscriptionRouter.dispatch`'s `needs_base=False` branch) turns
    it red, and §11 B3 must then be deleted from the contract.
    """

    RECORDS = [snapshot(1), delta(2), error_frame(3), delta(4)]

    def test_live_absorbs_the_error_frames_sequence_number(self):
        faults, publishable = drive_live(self.RECORDS)
        assert faults == 0
        assert publishable == {MARKET: True}

    def test_replay_manufactures_a_gap_that_never_happened(self):
        out = replay(self.RECORDS)
        assert out["events_rejected"] == 1
        assert out["publishable"] == {MARKET: False}
        assert out["publication_states"][MARKET]["state"] == PUB_BOOK_HALTED
        assert "sequence gap: expected 3, got 4" in out["faults"][0]["error"]

    def test_anti_vacuity_without_the_error_frame_the_lanes_agree(self):
        """The divergence is caused by the error frame and by nothing else."""
        clean = [snapshot(1), delta(2), delta(3), delta(4)]
        live_faults, live_publishable = drive_live(clean)
        out = replay(clean)
        assert live_faults == 0 and out["events_rejected"] == 0
        assert live_publishable == out["publishable"] == {MARKET: True}


# =====================================================================================
# §11 B4 — CHARACTERIZATION: the durable record carries no session identity
# =====================================================================================


class TestSessionBoundaryIsNotRepresentable:
    """`subscription_generation` is monotonic WITHIN one collection session and
    restarts at 1 in the next. The durable record has no session field, so two
    sessions appended to one archive are indistinguishable — and replaying
    across the boundary halts every book for the rest of the tape.

    §11 B4's remedy is operational (one archive root per session). This pins
    both halves so that adding a session identity to `RECORD_FIELDS`, or making
    the epoch cross-session monotonic, turns it red.
    """

    def test_the_durable_record_has_no_session_identity(self):
        assert not any("session" in field for field in RECORD_FIELDS)
        # ANTI-VACUITY: the fields the contract DOES claim are pinned columns
        # are actually there, so this is not passing because RECORD_FIELDS is
        # empty or renamed wholesale.
        for pinned in ("connection_generation", "subscription_generation",
                       "subscription_id", "seq", "segment_id"):
            assert pinned in RECORD_FIELDS

    def test_replaying_two_sessions_halts_every_book_at_the_boundary(self):
        session_one = [snapshot(1, generation=1), delta(2, generation=1),
                       snapshot(1, generation=2), delta(2, generation=2)]
        session_two = [snapshot(1, generation=1), delta(2, generation=1)]

        out = replay(session_one + session_two)

        # Every record of the second session is refused as a straggler from an
        # epoch the subscription has already left — which is the CORRECT
        # reading of the record schema, and the reason the schema is the defect.
        assert out["events_rejected"] == len(session_two)
        assert out["subscription_stats"]["1"]["stale_generation"] == 2
        assert out["publishable"] == {MARKET: False}

        # ANTI-VACUITY: the identical first session replays perfectly clean, so
        # the failure is the BOUNDARY and not the records.
        clean = replay(session_one)
        assert clean["events_rejected"] == 0
        assert clean["publishable"] == {MARKET: True}


# =====================================================================================
# §7 — a zero-level book is never observed emptiness unless ladder_presence says so
# =====================================================================================


class TestTypedAbsenceSurvivesReplay:
    """`NOT_PROVIDED != EMPTY != PRESENT`, and the distinction is NOT in the
    checksum — CP8 compares `ladder_presence` separately for exactly that
    reason. This asserts the two snapshots that produce an identical zero-level
    YES side are distinguishable after a replay.
    """

    def test_omitted_and_empty_ladders_are_distinguishable_after_replay(self):
        omitted = {
            "event_type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "market_ticker": MARKET, "subscription_generation": 1,
            # No `yes_dollars_fp` key at all — the venue said nothing.
            "raw": {"msg": {"market_ticker": MARKET,
                            "no_dollars_fp": [["0.6000", "5.00"]]}},
        }
        empty = {
            "event_type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "market_ticker": MARKET, "subscription_generation": 1,
            # The key is present and holds nothing — the venue said "empty".
            "raw": {"msg": {"market_ticker": MARKET, "yes_dollars_fp": [],
                            "no_dollars_fp": [["0.6000", "5.00"]]}},
        }

        def presence_after_replay(record):
            router = SubscriptionRouter(
                SubscriptionState(1, market_tickers=(MARKET,), generation=1))
            router.dispatch(record)
            return router.books[MARKET]

        a, b = presence_after_replay(omitted), presence_after_replay(empty)

        # The level counts are identical...
        assert len(a.yes) == len(b.yes) == 0
        # ...and the observations are not. THIS is the guard.
        assert a.ladder_presence["yes"] == "omitted_by_venue"
        assert b.ladder_presence["yes"] == "supplied"

        # ANTI-VACUITY: `checksum()` cannot tell them apart, which is why the
        # comparison above has to exist at all.
        assert a.checksum() == b.checksum()


# =====================================================================================
# §7.1 — publication state is typed; a reconnect boundary is not an integrity fault
# =====================================================================================


class TestReconnectIsNotAnIntegrityFault:
    """§8.3: `sequence_faults` is not synonymous with packet loss, and a
    generation boundary must never be filed as one.
    """

    SIBLING = "KXTEST-26AUG17-B"

    def test_a_market_awaiting_its_own_snapshot_is_not_halted(self):
        """The CP7 shape, and the one `rejected_pre_generation_snapshot` exists
        for: the subscription HAS been re-based for the new epoch — by a
        SIBLING's snapshot — and this market has not. A sibling's snapshot
        re-bases the sibling and says nothing about anyone else's ladder.
        """
        router = SubscriptionRouter(SubscriptionState(
            1, market_tickers=(MARKET, self.SIBLING), generation=1))
        router.dispatch(snapshot(1, generation=1))
        router.dispatch(snapshot(2, generation=1, ticker=self.SIBLING))
        assert router.publishable_books() == {MARKET: True, self.SIBLING: True}

        # The reconnect: the venue's seq restarts, and only the SIBLING is
        # re-snapshotted into the new epoch.
        router.dispatch(snapshot(1, generation=2, ticker=self.SIBLING))
        assert router.publishable_books()[self.SIBLING] is True

        # Now a delta for the market that was left behind.
        with pytest.raises(Exception):
            router.dispatch(delta(2, generation=2))

        state = router.publication_states()[MARKET]
        assert state.publishable is False
        # The benign, typed reason — NOT `book_halted`. Both epochs travel
        # with it, and their inequality IS the invariant.
        assert state.state == PUB_AWAITING_GENERATION_SNAPSHOT
        assert state.subscription_generation == 2
        assert state.based_generation == 1

        book = router.books[MARKET]
        # Counted on its own axis, never merged into `gaps` or
        # `rejected_pre_snapshot`: it is neither loss nor a cold start.
        assert book.stats["rejected_pre_generation_snapshot"] == 1
        assert book.stats["gaps"] == 0
        assert book.stats["rejected_pre_snapshot"] == 0
        assert book.integrity_reason is None
        # And the SIBLING is untouched — the refusal is per-market.
        assert router.publishable_books()[self.SIBLING] is True

        # ANTI-VACUITY: a real within-generation gap DOES halt, so the benign
        # state above is a distinction the code draws rather than a state it
        # always reports.
        router.dispatch(snapshot(3, generation=2))
        assert router.publishable_books()[MARKET] is True
        with pytest.raises(Exception):
            router.dispatch(delta(99, generation=2))
        assert router.publication_states()[MARKET].state == PUB_BOOK_HALTED
        # NOTE the object: the gap lands on the SUBSCRIPTION, never on the
        # book. Ordering is settled once, per sid, before routing — see
        # `TestPerMarketFaultCountersAreStructurallyUnreachable`.
        assert router.subscription.stats["gaps"] == 1
        assert router.books[MARKET].stats["gaps"] == 0


# =====================================================================================
# §9.1 / §9.2 — the NOT_MEASURABLE candidates, pinned as they stand today
# =====================================================================================


class TestUnboundGaugesReportZeroNotUnknown:
    """The §9 candidates. `reader_lag_frames_max` gets this RIGHT (null when
    unavailable); `reader_stall_ms_max`, `rotation_failures` and
    `closer_outstanding_max` emit a plausible 0 from a path that cannot know.

    This pins the current behaviour so the recommended retyping turns it red
    and §9.1/§9.2 are retired deliberately rather than drifting.
    """

    def test_the_three_candidates_emit_zero_while_the_correct_one_emits_null(
            self, tmp_path):
        metrics = CollectorMetrics(environment="demo", markets_subscribed=1)
        # No source bound for the transport counters or the archive state —
        # exactly the situation the fields cannot distinguish from "healthy".
        flusher = MetricsFlusher(metrics, directory=tmp_path)
        record = flusher._build_record(final=False)

        # THE CORRECT PATTERN: unavailable is null, never 0.
        assert record["reader_lag_frames_max"] is None

        # THE CANDIDATES: a benign zero from a path with no source.
        assert record["reader_stall_ms_max"] == 0
        assert record["rotation_failures"] == 0
        assert record["closer_outstanding_max"] == 0

        # ANTI-VACUITY: the record really was built and really is valid, so the
        # zeroes above are emitted values and not an empty dict.
        assert record["schema_version"] == 1
        assert set(record) == set(ALLOWED_FIELDS)

    def test_per_market_fault_counters_are_structurally_unreachable(self):
        """§9.9. `OrderBook.stats["gaps"|"regressions"|"duplicates"]` can never
        become non-zero in the ROUTED path.

        `SubscriptionRouter` settles ordering once per sid and calls
        `apply_delta(..., ordered_externally=True)`, so `classify_seq` never
        runs and the book's own fault counters are dead code on this path. They
        are the numbers `replay()["stats"]` returns per market — so a reader
        summing them across a tape with real losses gets **zero**.

        Doctrine 7: a metric that cannot become non-benign is not a healthy
        zero, it is an unmeasured one.
        """
        records = [snapshot(1), delta(9)]        # a real, four-message gap
        out = replay(records)

        # The gap unambiguously happened...
        assert out["events_rejected"] == 1
        assert out["subscription_stats"]["1"]["gaps"] == 1

        # ...and every per-market fault counter reports zero anyway.
        assert out["stats"][MARKET]["gaps"] == 0
        assert out["stats"][MARKET]["regressions"] == 0
        assert out["stats"][MARKET]["duplicates"] == 0

        # ANTI-VACUITY: the per-market block is not inert — the counters that
        # ARE reachable on this path moved.
        assert out["stats"][MARKET]["snapshots"] == 1

    def test_there_is_no_transport_dropped_field(self):
        """CP0 12.4. The library has no drop path and no drop counter, so the
        number has no source — and a zero would be a fabricated measurement.
        """
        assert not any("drop" in f and "flush" not in f for f in ALLOWED_FIELDS)
        # ANTI-VACUITY: the field set is populated and the one legitimate
        # "drop" field is present, so this is not passing on an empty schema.
        assert "metric_flush_drops" in ALLOWED_FIELDS
        assert len(ALLOWED_FIELDS) > 20


# =====================================================================================
# §10.1 — the operator command must survive the tape it exists to inspect
# =====================================================================================


WHEN = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _archive_with(tmp_path, frames) -> Path:
    """A real, initialized, committed archive holding `frames`."""
    from app.realtime import archive_head

    root = tmp_path / "tape"
    archive_head.initialize_archive(root, "demo",
                                    archive_identity="kalshi-realtime")
    store = EventArchive(root, environment="demo")
    for index, message in enumerate(frames):
        store.append(make_envelope(
            venue="kalshi", environment="demo", channel="orderbook_delta",
            message=message, receive_time=WHEN, receive_mono=1_000 + index,
            subscription_generation=1, connection_generation=1))
    store.close()
    return root


class TestOperatorReplayCommandSurvivesAHaltedBook:
    """§10.1. `replay()` sets a market's checksum to None when the book is not
    publishable — deliberately. The text output sliced it unconditionally and
    raised `TypeError` on exactly the tape it exists to report on, and ONLY in
    the interesting case: a healthy tape printed fine.
    """

    HALTED = [
        {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
         "msg": {"market_ticker": MARKET, "ts_ms": 1786000000000,
                 "yes_dollars_fp": [["0.4000", "5.00"]],
                 "no_dollars_fp": [["0.6000", "5.00"]]}},
        # A four-message hole: a genuine, unrepaired sequence gap.
        {"type": "orderbook_delta", "sid": 1, "seq": 9,
         "msg": {"market_ticker": MARKET, "ts_ms": 1786000001000,
                 "side": "yes", "price_dollars": "0.4000",
                 "delta_fp": "1.00"}},
    ]

    def test_text_output_reports_the_halt_instead_of_crashing(
            self, tmp_path, capsys):
        from app.cli import kalshi_realtime_replay

        root = _archive_with(tmp_path, self.HALTED)
        code = kalshi_realtime_replay(str(root), environment="demo",
                                      fmt="text")
        out = capsys.readouterr().out

        assert code == 1                       # a faulting tape is not a pass
        assert "NOT_PUBLISHABLE" in out
        assert "state=book_halted" in out
        # §9.9: the REAL fault numbers are printed, per sid.
        assert "sid=1" in out and "gaps=1" in out

    def test_anti_vacuity_a_healthy_tape_still_prints_a_real_checksum(
            self, tmp_path, capsys):
        from app.cli import kalshi_realtime_replay

        healthy = [self.HALTED[0],
                   {**self.HALTED[1], "seq": 2,
                    "msg": {**self.HALTED[1]["msg"]}}]
        root = _archive_with(tmp_path, healthy)
        code = kalshi_realtime_replay(str(root), environment="demo",
                                      fmt="text")
        out = capsys.readouterr().out

        assert code == 0
        assert "NOT_PUBLISHABLE" not in out
        assert "publishable=True" in out
        assert "gaps=0" in out
