"""KALSHI-P4-1-REPLAY-REQUAL — B3 closure: a sequenced control frame is a
sequence event.

THE INVARIANT UNDER TEST
------------------------
    A sequenced frame must affect the relevant sequence domain even if that
    frame is not an order-book state mutation.

`archive.replay()` used to `continue` past every non-orderbook `event_type`
*before* any sequence processing (`archive.py`, the B3 branch). The venue's
`seq` counts **frames on a subscription**, not book mutations, so an `error`
frame consumes a number in its sid's space. Skipping it left `replay()`
expecting that number for the next delta and **manufacturing a gap that never
happened**: live 0 faults / publishable, replay 1 fault / `book_halted`.

WHAT THIS FILE DOES NOT CLAIM
-----------------------------
It does not claim `replay()` now models non-orderbook *subscriptions*. It does
not. A sid that never carries an orderbook frame is still outside book replay's
scope, exactly as `test_the_shipped_replay_omits_the_non_orderbook_sids`
(CP6-CP9) and `test_a_non_orderbook_sid_never_becomes_a_subscription`
(TAPE-MEASUREMENT-CONTRACT §1) assert. Widening that scope is a different
decision with its own guards; B3 is only about the sequence domain of a
subscription replay is **already** reconstructing. `test_the_fix_did_not_widen_
replay_s_scope` pins that boundary so this fix cannot be mistaken for that one.

PROVENANCE (AGENTS.md doctrine 9)
---------------------------------
The `error` frame is not invented. It is read at test time out of the committed
P0 capture and its canonical digest is pinned, so if the artifact moves these
tests fail rather than silently certifying a shape the venue never sent.

    capture_id   p0-wire-test-instruments-60
    timestamp    2026-08-17T08:49:57Z .. 2026-08-17T08:51:57Z
    venue        Kalshi, environment "demo", credential proven read-only
    channel      the frame arrived on sid 3 (`trade`); see §"WHICH SID"
    frame hash   sha256 of the canonical JSON, pinned in ERROR_FRAME_SHA256
    schema       RECORD_SCHEMA_VERSION == 1 (the durable record it is wrapped in)

WHICH SID — and why this is not an invented semantic
----------------------------------------------------
Two independent, committed observations combine, and neither is extrapolated:

1. **A control frame consumes a sequence number in the sid it lands on.**
   Proven by arithmetic on the P0 per-sid census, not by assertion. sid 3
   carried 219 frames — 218 `trade` and 1 `error` — over `seq` 1..219 with
   0 gaps, 0 duplicates and 0 absent. 219 numbers, 219 frames, one of them the
   `error`: the error frame occupies exactly one `seq`. This is asserted in
   `test_the_wire_says_a_control_frame_consumes_a_sequence_number`.

2. **An `error` frame can land on the ORDERBOOK sid.** The 2026-08-08 DEMO
   capture recorded `{"type":"error","sid":4,"seq":4}` between deltas at seq 3
   and seq 5, sid 4 being the orderbook channel that day
   (KALSHI-TAPE-MEASUREMENT-CONTRACT-001 §3.1). Sids are assigned in ack order,
   so the orderbook channel is whichever sid the acks name — nothing here
   hard-codes it.

(1) gives the *semantic*, (2) gives the *placement*. The fixture below is (1)'s
verbatim frame body positioned per (2). Nothing else about `error` frames is
encoded — not their effect on a book (none), not a recovery, not a meaning.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.realtime.archive import replay
from app.realtime.book import (
    PUB_BOOK_HALTED,
    SubscriptionRouter,
    SubscriptionState,
)
from app.realtime.segment import RECORD_SCHEMA_VERSION

# --- provenance ---------------------------------------------------------------

CAPTURE = (Path(__file__).resolve().parents[1] / "docs" / "experiments"
           / "KALSHI-COLLECTOR-P0-FIXES-RUNS"
           / "p0-wire-test-instruments-60.json")
CAPTURE_ID = "p0-wire-test-instruments-60"
CAPTURE_MILESTONE = "KALSHI-COLLECTOR-P0-FIXES"
#: sha256 over `json.dumps(frame, sort_keys=True, separators=(",", ":"))` of the
#: single `error` frame in the capture. The drift detector for doctrine 9.
ERROR_FRAME_SHA256 = (
    "16b0a6039d1afca4a558b8fda304f341d6c612eee9028fbc95df6ccd687eb9c2")

MARKET = "KXTESTMATCH-26AUG150030INDSRI-IND"   # a ticker from the same capture
SID_ORDERBOOK = 1


def _capture() -> dict:
    return json.loads(CAPTURE.read_text())


def wire_error_frame() -> dict:
    """The verbatim `error` frame body, read from the capture, hash-checked."""
    frames = _capture()["wire"]["error_frames_verbatim"]
    assert len(frames) == 1, f"capture no longer holds exactly one error frame: {frames}"
    frame = frames[0]["frame"]
    canonical = json.dumps(frame, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    assert digest == ERROR_FRAME_SHA256, (
        f"the committed error frame changed: {canonical} -> {digest}. Either the "
        "artifact moved or the venue's shape did; re-derive before trusting any "
        "test in this file.")
    return frame


# --- record builders ----------------------------------------------------------
# Durable-record envelopes, the shape `replay()` consumes.

def snapshot(seq: int, *, generation: int = 1, ticker: str = MARKET,
             sid: int = SID_ORDERBOOK) -> dict:
    return {
        "event_type": "orderbook_snapshot", "sid": sid, "seq": seq,
        "market_ticker": ticker, "subscription_generation": generation,
        "schema_version": RECORD_SCHEMA_VERSION,
        "raw": {"msg": {"market_ticker": ticker,
                        "yes_dollars_fp": [["0.4000", "5.00"]],
                        "no_dollars_fp": [["0.6000", "5.00"]]}},
    }


def delta(seq: int, *, generation: int = 1, ticker: str = MARKET,
          sid: int = SID_ORDERBOOK) -> dict:
    return {
        "event_type": "orderbook_delta", "sid": sid, "seq": seq,
        "market_ticker": ticker, "subscription_generation": generation,
        "schema_version": RECORD_SCHEMA_VERSION,
        "raw": {"msg": {"market_ticker": ticker, "side": "yes",
                        "price_dollars": "0.4000", "delta_fp": "1.00"}},
    }


def wire_error_record(seq: int, *, generation: int = 1,
                      sid: int = SID_ORDERBOOK) -> dict:
    """The captured `error` frame, wrapped as the durable record replay reads.

    Only `seq` and `sid` are repositioned — onto the orderbook subscription, the
    placement the 2026-08-08 capture observed. The frame BODY is byte-identical
    to the committed wire evidence, and `market_ticker` is `None` because an
    `error` frame names no market: that is what makes it unroutable to a book
    and therefore purely a sequence event.
    """
    frame = wire_error_frame()
    return {
        "event_type": frame["type"], "sid": sid, "seq": seq,
        "market_ticker": None, "subscription_generation": generation,
        "schema_version": RECORD_SCHEMA_VERSION,
        "raw": {"msg": dict(frame["msg"])},
    }


def drive_live(records) -> tuple[int, dict, object]:
    """The LIVE lane: every frame through `dispatch`, as the collector does."""
    router = SubscriptionRouter(
        SubscriptionState(SID_ORDERBOOK, market_tickers=(MARKET,), generation=1))
    faults = 0
    for record in records:
        try:
            router.dispatch(record)
        except Exception:                     # noqa: BLE001 - counted, as live does
            faults += 1
    return faults, router.publishable_books(), router.subscription.last_seq


# =====================================================================================
# The wire evidence itself — before any claim is made about code
# =====================================================================================


class TestWireProvenance:
    """Doctrine 9: the fixture is venue truth, and it is checked, not asserted."""

    def test_the_capture_is_the_one_this_file_claims(self):
        capture = _capture()
        assert capture["milestone"] == CAPTURE_MILESTONE
        assert capture["run_label"] == CAPTURE_ID
        assert capture["environment"] == "demo"
        assert capture["credential_audit"]["proven_read_only"] is True
        assert capture["started_at"].startswith("2026-08-17T08:49:57")

    def test_the_error_frame_matches_its_pinned_digest(self):
        frame = wire_error_frame()
        # The shape the collector's own comments quote.
        assert frame["type"] == "error"
        assert frame["seq"] == 2
        assert frame["msg"] == {"code": 13, "msg": "Unsupported action"}

    def test_the_wire_says_a_control_frame_consumes_a_sequence_number(self):
        """THE load-bearing evidence. Arithmetic on the census, not an opinion.

        sid 3 carried 218 `trade` frames and 1 `error` frame — 219 frames — over
        `seq` 1..219 with zero gaps, zero duplicates and zero absent. 219
        contiguous numbers consumed by 219 frames means the `error` frame
        consumed one of them. If control frames did NOT consume a sequence
        number, 218 trades would have had to span 1..219, which requires a gap,
        and the census reports none.
        """
        census = {s["sid"]: s for s in _capture()["wire"]["per_sid_census"]}
        trade_sid = census[3]
        assert trade_sid["types"] == {"trade": 218, "error": 1}
        assert trade_sid["frames"] == 219
        assert (trade_sid["seq_first"], trade_sid["seq_last"]) == (1, 219)
        assert trade_sid["seq_gaps"] == 0
        assert trade_sid["seq_duplicates"] == 0
        assert trade_sid["seq_absent"] == 0
        # 219 distinct numbers, 219 frames, only 218 of them trades.
        span = trade_sid["seq_last"] - trade_sid["seq_first"] + 1
        assert span == trade_sid["frames"] == sum(trade_sid["types"].values())
        assert trade_sid["types"]["trade"] < span

        # ANTI-VACUITY: the census can express a hole. The orderbook sid ran
        # 5,886 frames over seq 1..5886 and the ticker sid carries no `seq` at
        # all (2,071 of 2,071 absent) — so these fields are populated by a real
        # measurement with real variety, not by a stub that writes zeros.
        assert census[1]["frames"] == 5886 and census[1]["seq_last"] == 5886
        assert census[2]["seq_absent"] == 2071 and census[2]["seq_first"] is None


# =====================================================================================
# B3 — the defect, closed
# =====================================================================================


class TestSequencedControlFrameIsASequenceEvent:
    """B3. Replay must consume the `seq` an `error` frame occupies."""

    #: The 2026-08-08 shape, carrying the 2026-08-17 verbatim body.
    def records(self):
        return [snapshot(1), delta(2), wire_error_record(3), delta(4)]

    def test_replay_does_not_manufacture_a_gap(self):
        out = replay(self.records())
        assert out["events_rejected"] == 0, out["faults"]
        assert out["faults"] == []
        assert out["publishable"] == {MARKET: True}
        assert out["checksums"][MARKET] is not None

    def test_replay_and_live_reach_the_same_verdict(self):
        """The replay-equality claim, on the exact input that used to break it."""
        records = self.records()
        live_faults, live_publishable, live_last_seq = drive_live(records)
        out = replay(records)

        assert (live_faults, live_publishable) == (0, {MARKET: True})
        assert out["events_rejected"] == live_faults
        assert out["publishable"] == live_publishable
        # The positions agree, which is the actual subject of the defect.
        assert out["subscription_stats"]["1"]["gaps"] == 0
        assert live_last_seq == 4

    def test_the_error_frames_actual_sequence_is_consumed(self):
        """Its ACTUAL number — not "one more", not "skip ahead"."""
        out = replay([snapshot(1), wire_error_record(2)])
        assert out["subscription_stats"]["1"]["accepted"] == 2
        assert out["subscription_stats"]["1"]["gaps"] == 0

        # ANTI-VACUITY: the consumed number is 2 and nothing else. A frame
        # arriving at 4 after the error at 2 is still a hole at 3.
        holed = replay([snapshot(1), wire_error_record(2), delta(4)])
        assert holed["events_rejected"] == 1
        assert "sequence gap: expected 3, got 4" in holed["faults"][0]["error"]


class TestTheLadderIsNotTouched:
    """Requirement 5: a sequence event, not a book event."""

    def test_the_error_frame_advances_the_sequence_and_nothing_else(self):
        before = replay([snapshot(1), delta(2)])
        after = replay([snapshot(1), delta(2), wire_error_record(3)])

        # The book is bit-identical...
        assert after["checksums"][MARKET] == before["checksums"][MARKET]
        assert after["checksums"][MARKET] is not None
        assert after["stats"] == before["stats"]
        # ...and no book event was counted for it...
        assert after["events_applied"] == before["events_applied"] == 2
        assert after["markets"] == before["markets"] == 1
        # ...while the SUBSCRIPTION did move.
        assert (after["subscription_stats"]["1"]["accepted"]
                == before["subscription_stats"]["1"]["accepted"] + 1)

        # ANTI-VACUITY: an orderbook frame in the same position DOES change the
        # checksum, so "identical" above is a real property of the error frame
        # and not of a replay that stopped early.
        moved = replay([snapshot(1), delta(2), delta(3)])
        assert moved["checksums"][MARKET] != before["checksums"][MARKET]

    def test_an_unsequenced_control_frame_is_passed_over(self):
        """2,071 of 2,071 `ticker` frames carried no `seq` (P0 census, sid 2).

        A frame the venue never ordered must not be counted as a fault for a
        number it never had.
        """
        unsequenced = {**wire_error_record(3), "seq": None}
        out = replay([snapshot(1), delta(2), unsequenced, delta(3)])
        assert out["events_rejected"] == 0, out["faults"]
        assert out["publishable"] == {MARKET: True}


class TestARealDropStillFaults:
    """Requirement 4: the fix must not blind the only drop detector we have."""

    def test_a_missing_orderbook_frame_still_halts_the_book(self):
        out = replay([snapshot(1), delta(2), delta(4)])
        assert out["events_rejected"] == 1
        assert out["publishable"] == {MARKET: False}
        assert out["publication_states"][MARKET]["state"] == PUB_BOOK_HALTED
        assert "sequence gap: expected 3, got 4" in out["faults"][0]["error"]

    def test_a_drop_ADJACENT_to_an_error_frame_still_halts_the_book(self):
        """The dangerous case: the error frame must absorb its own number only.

        If the fix absorbed "whatever comes next" instead of `seq` 3, this real
        loss at 4 would be silently swallowed — which is the precise way a
        sequence fix turns into a sequence blindfold.
        """
        out = replay([snapshot(1), delta(2), wire_error_record(3), delta(5)])
        assert out["events_rejected"] == 1
        assert out["publishable"] == {MARKET: False}
        assert out["publication_states"][MARKET]["state"] == PUB_BOOK_HALTED
        assert "sequence gap: expected 4, got 5" in out["faults"][0]["error"]

    def test_a_gap_CARRIED_BY_the_error_frame_still_halts_the_book(self):
        """The loss can be the frame before the error frame."""
        out = replay([snapshot(1), delta(2), wire_error_record(5)])
        assert out["events_rejected"] == 1
        assert out["publishable"] == {MARKET: False}
        assert "sequence gap: expected 3, got 5" in out["faults"][0]["error"]

    def test_the_live_lane_agrees_about_every_one_of_those(self):
        """Parity is the point: replay must not be a second opinion."""
        for records in ([snapshot(1), delta(2), delta(4)],
                        [snapshot(1), delta(2), wire_error_record(3), delta(5)],
                        [snapshot(1), delta(2), wire_error_record(5)]):
            live_faults, live_publishable, _ = drive_live(records)
            out = replay(records)
            assert out["events_rejected"] == live_faults == 1, records
            assert out["publishable"] == live_publishable == {MARKET: False}


class TestDeterminismAndScope:

    def test_replay_terminal_state_is_deterministic(self):
        """Requirement 6. Same records, same everything — twice."""
        records = [snapshot(1), delta(2), wire_error_record(3), delta(4)]
        first, second = replay(records), replay(list(records))
        assert first == second
        for key in ("checksums", "publishable", "publication_states", "stats",
                    "subscription_stats", "events_applied", "events_rejected"):
            assert first[key] == second[key]

        # ANTI-VACUITY: the terminal state is a function of the records, so a
        # DIFFERENT tape reaches a different one. Equality above is not the
        # trivial equality of two empty results.
        assert replay(records[:2]) != first
        assert first["checksums"][MARKET] is not None

    def test_the_fix_did_not_widen_replay_s_scope(self):
        """B3 is not "replay now models every sid". It still does not.

        A control frame advances a subscription replay is ALREADY
        reconstructing. It never brings a new subscription into existence —
        that is the separate, deliberately-guarded decision recorded in
        `test_the_shipped_replay_omits_the_non_orderbook_sids` (CP6-CP9).
        """
        trade = {
            "event_type": "trade", "sid": 3, "seq": 1,
            "market_ticker": MARKET, "subscription_generation": 1,
            "raw": {"msg": {"market_ticker": MARKET, "taker_side": "yes",
                            "yes_price_dollars": "0.5000", "count_fp": "2.00"}},
        }
        error_on_a_bookless_sid = wire_error_record(2, sid=3)
        out = replay([snapshot(1), trade, error_on_a_bookless_sid, delta(2)])

        assert out["subscriptions"] == 1
        assert set(out["subscription_stats"]) == {"1"}

        # ANTI-VACUITY: on the sid that DOES carry books the very same error
        # frame is consumed, so the "1" above is a scope boundary rather than
        # the fix failing to run at all.
        on_book_sid = replay([snapshot(1), wire_error_record(2), delta(3)])
        assert on_book_sid["events_rejected"] == 0, on_book_sid["faults"]
        assert on_book_sid["subscription_stats"]["1"]["accepted"] == 3


class TestNoInventedVenueSemantics:
    """Requirement 3. The fix encodes ordering and NOTHING about what an error means."""

    def test_the_error_frame_triggers_no_recovery_and_no_halt_by_itself(self):
        out = replay([snapshot(1), wire_error_record(2)])
        assert out["publishable"] == {MARKET: True}
        assert out["subscription_stats"]["1"]["recoveries"] == 0
        assert out["subscription_stats"]["1"]["gaps"] == 0
        assert out["external_calls"] == 0 and out["persisted"] is False

    def test_an_unknown_control_type_is_treated_identically(self):
        """Ordering is a property of the SUBSCRIPTION, not of the frame's name.

        Nothing special-cases the string "error". A frame type we have never
        seen, carrying a `seq` on a sid we are reconstructing, consumes it the
        same way — which is what keeps this a sequence rule instead of a
        hard-coded list of venue vocabulary.
        """
        unknown = {**wire_error_record(3), "event_type": "some_future_frame"}
        out = replay([snapshot(1), delta(2), unknown, delta(4)])
        assert out["events_rejected"] == 0, out["faults"]
        assert out["publishable"] == {MARKET: True}
