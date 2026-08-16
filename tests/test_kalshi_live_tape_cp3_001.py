"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP3 — the collector loop, offline.

**No test in this file opens a socket.** An autouse fixture replaces the
transport module's connector and the two blocking `socket` entry points with
raisers, so "offline" is enforced rather than promised in a comment. Every
session below runs against `FixtureTransport` or a test double built on it.

What CP3 has to prove:

* the whole orchestrator runs end to end and the ARCHIVE'S OWN TOOLS agree with
  it — `kalshi-realtime-replay`'s two halves (`verify` and `replay`) report
  `records=N`, `faults=0`, integrity intact (`TestEndToEnd`);
* two runs of the same fixture produce identical per-market checksums AND
  identical normalization (`TestDeterminism`);
* trade direction is the venue's `taker_side` or a typed absence, and inference
  is structurally unreachable rather than merely absent (`TestTradeDirection`,
  `TestInferenceIsImpossible`);
* raw survives beside normalized, and no venue number reaches a `float`
  (`TestRawIsPreserved`);
* every required quantity is present or explicitly absent, always
  (`TestCoverageIsTotal`);
* the caps, the failure ladder and the recovery path behave as section 8 says
  (`TestBounds`, `TestFailureHandling`, `TestReconnectAndRecovery`);
* no forbidden channel is reachable from any configuration surface and no
  database session is reachable at all (`TestSafetySurface`).
"""

from __future__ import annotations

import ast
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import collector as kc
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt
from app.realtime.archive import ArchiveError, EventArchive, replay

REPO = Path(kc.__file__).resolve().parent.parent.parent
COLLECTOR_PATH = REPO / "app" / "realtime" / "collector.py"
COLLECTOR_SRC = ast.parse(COLLECTOR_PATH.read_text())

ENV = "demo"
M1 = "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1"
M2 = "KXA-SECOND-MARKET"


# --- offline enforcement ------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_socket_anywhere(monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - a call is the failure
        raise AssertionError("CP3 opened a network connection")

    monkeypatch.setattr(wt, "_websockets_connect", _explode)
    monkeypatch.setattr(socket.socket, "connect", _explode)
    monkeypatch.setattr(socket, "create_connection", _explode)
    yield


# --- fixtures: frames the venue actually sends --------------------------------------
def snapshot(market=M1, *, sid=4, seq=1, yes=(("0.4700", "5.00"),),
             no=(("0.5100", "5.00"),), ts_ms=1786150148065):
    msg = {"market_ticker": market, "market_id": "mid-1",
           "ts": "2026-08-08T00:49:08.065758Z", "ts_ms": ts_ms}
    if yes is not None:
        msg["yes_dollars_fp"] = [list(level) for level in yes]
    if no is not None:
        msg["no_dollars_fp"] = [list(level) for level in no]
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg}


def delta(market=M1, *, sid=4, seq=2, side="no", price="0.5100",
          change="201.00", ts_ms=1786150148066):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": market, "price_dollars": price,
                    "delta_fp": change, "side": side, "ts_ms": ts_ms}}


def ticker(market=M1, *, sid=4, seq=3, bid="0.4700", ask="0.5100"):
    return {"type": "ticker", "sid": sid, "seq": seq,
            "msg": {"market_ticker": market, "price_dollars": "0.5000",
                    "yes_bid_dollars": bid, "yes_ask_dollars": ask,
                    "yes_ask_size_fp": "206.00", "ts": 1786150148,
                    "ts_ms": 1786150148067,
                    "time": "2026-08-08T00:49:08.065758Z"}}


def trade(market=M1, *, sid=4, seq=4, taker_side="yes", count="1.00",
          yes_price="0.5000", no_price="0.5000", trade_id="t-1", extra=None):
    msg = {"market_ticker": market, "count_fp": count,
           "yes_price_dollars": yes_price, "no_price_dollars": no_price,
           "trade_id": trade_id, "ts_ms": 1786150148068}
    if taker_side is not None:
        msg["taker_side"] = taker_side
    msg.update(extra or {})
    return {"type": "trade", "sid": sid, "seq": seq, "msg": msg}


def lifecycle(market=M1, *, sid=4, seq=5, status="open", close_ts=1786200000):
    return {"type": "market_lifecycle_v2", "sid": sid, "seq": seq,
            "msg": {"market_ticker": market, "open_ts": 1786100000,
                    "close_ts": close_ts, "is_deactivated": False,
                    "status": status, "ts_ms": 1786150148069}}


def subscribed_ack(sid=4):
    return {"type": "subscribed", "id": 1, "sid": sid,
            "msg": {"channel": "orderbook_delta", "sid": sid}}


def venue_error(sid=4, seq=6):
    """A real wire frame. It CONSUMES a sequence number (DEMO, 2026-08-08)."""
    return {"type": "error", "sid": sid, "seq": seq,
            "msg": {"code": 14, "msg": "Market Ticker required"}}


FULL_SESSION = [subscribed_ack(), snapshot(), delta(), ticker(), trade(),
                lifecycle(), venue_error()]

ALL_CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")


# --- harness -------------------------------------------------------------------------
class RecordingFactory:
    """Hands out one `FixtureTransport` per connection and keeps them all."""

    def __init__(self, *streams):
        self.streams = [list(s) for s in streams]
        self.made: list = []

    def __call__(self):
        frames = self.streams[min(len(self.made), len(self.streams) - 1)]
        transport = kx.FixtureTransport(frames)
        self.made.append(transport)
        return transport


class FailingTransport(kx.FixtureTransport):
    """Yields, then loses the socket. The failure the reconnect driver exists for."""

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


def init_archive(root: Path):
    """The operator step (`archive-init --confirm`). A collector never mints one."""
    return ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")


def make_config(root, **kwargs):
    params = dict(environment=ENV, archive_root=root, market_tickers=(M1,),
                  channels=ALL_CHANNELS, max_seconds=60, max_events=1000,
                  max_reconnects=0, reconnect_backoff_base_s=0.0)
    params.update(kwargs)
    return kc.CollectorConfig(**params)


def run(root, frames=None, *, factory=None, **kwargs):
    factory = factory or RecordingFactory(frames if frames is not None
                                          else FULL_SESSION)
    result = kc.collect_once(make_config(root, **kwargs),
                             transport_factory=factory)
    return result, factory


def read_back(root):
    store = EventArchive(root, environment=ENV)
    records = store.read_all()
    return store.verify(), records


def replay_report(root):
    """Exactly what `kalshi-realtime-replay` does: verify, then replay."""
    integrity, records = read_back(root)
    return integrity, replay(records), records


def only(records, event_type):
    matches = [r for r in records if r["event_type"] == event_type]
    assert len(matches) == 1, (event_type, len(matches))
    return matches[0]


# --- 1-9: the checkpoint's own verify line -------------------------------------------
class TestEndToEnd:
    def test_1_the_session_runs_the_whole_loop(self, tmp_path):
        init_archive(tmp_path)
        result, factory = run(tmp_path)
        assert result.status == kc.STATUS_OK
        assert result.events_received == len(FULL_SESSION)
        assert result.events_archived == len(FULL_SESSION)
        assert result.events_rejected == 0
        assert result.frames_malformed == 0
        assert result.segments_committed >= 1
        assert result.rotation_failures == 0
        assert factory.made[0].connected is True

    def test_2_replay_reports_records_faults_and_integrity(self, tmp_path):
        """The CP3 verify line, asserted with the ARCHIVE'S tools, not new ones."""
        init_archive(tmp_path)
        run(tmp_path)
        integrity, out, records = replay_report(tmp_path)
        assert integrity["records"] == len(FULL_SESSION)
        assert integrity["intact"] is True
        assert integrity["verdict"] == "VALID"
        assert integrity["mismatched"] == []
        assert integrity["truncated_records"] == 0
        assert out["faults"] == []
        assert out["events_rejected"] == 0
        assert out["markets"] == 1
        assert out["publishable"][M1] is True
        assert len(records) == len(FULL_SESSION)

    def test_3_the_reconstructed_book_matches_the_venues_own_ticker(self, tmp_path):
        """The tape is only worth its size if replay rebuilds the venue's truth:
        bid 0.4700/5.00, ask 0.5100/206.00 after the +201.00 delta — the exact
        numbers demo validation recorded."""
        init_archive(tmp_path)
        run(tmp_path)
        _, records = read_back(tmp_path)
        out = replay(records)
        assert out["checksums"][M1] is not None
        book_top = only(records, "ticker")["normalized"]["quote"]
        assert book_top["best_yes_bid_units"] == 4700
        assert book_top["best_yes_ask_units"] == 5100
        assert book_top["spread_units"] == 400

    def test_4_every_frame_is_archived_including_control_and_error(self, tmp_path):
        """A frame we cannot route is still evidence — and on this venue an
        `error` frame consumes a sequence number, so dropping it would
        manufacture a gap on the next delta."""
        init_archive(tmp_path)
        run(tmp_path)
        _, records = read_back(tmp_path)
        types = [r["event_type"] for r in records]
        assert types == [f["type"] for f in FULL_SESSION]
        assert only(records, "error")["raw"]["msg"]["code"] == 14

    def test_5_two_markets_on_one_sid_do_not_manufacture_a_gap(self, tmp_path):
        """`seq` is subscription-global. Interleaved markets are ordinary."""
        init_archive(tmp_path)
        frames = [snapshot(M1, seq=1), snapshot(M2, seq=2),
                  delta(M2, seq=3), delta(M1, seq=4)]
        result, _ = run(tmp_path, frames, market_tickers=(M1, M2))
        integrity, out, _ = replay_report(tmp_path)
        assert result.sequence_faults == 0
        assert out["faults"] == []
        assert out["markets"] == 2
        assert integrity["records"] == 4

    def test_6_the_archive_must_already_exist(self, tmp_path):
        """`archive-init --confirm` is the operator's step. A collector that
        could bootstrap one could also certify its own deletions."""
        result, factory = run(tmp_path / "never-initialized")
        assert result.status == kc.STATUS_ARCHIVE_ERROR
        assert result.events_received == 0
        assert factory.made == []            # nothing even connected
        assert not (tmp_path / "never-initialized").exists()

    def test_7_dry_run_archives_nothing_at_all(self, tmp_path):
        result, factory = run(tmp_path, dry_run=True, archive_root=None)
        assert result.status == kc.STATUS_OK
        assert result.events_received == len(FULL_SESSION)
        assert result.events_archived == 0
        assert result.segments_committed == 0
        assert list(tmp_path.iterdir()) == []
        assert factory.made[0].connected is True

    def test_8_the_only_thing_sent_is_builder_output(self, tmp_path):
        init_archive(tmp_path)
        _, factory = run(tmp_path)
        sent = factory.made[0].sent
        assert len(sent) == 1
        # Re-runs the transport's own governance over what the loop emitted: a
        # frame that is not byte-identical to a builder's output is refused.
        assert wt.assert_sendable(sent[0]) == sent[0]
        assert sent[0]["cmd"] == "subscribe"
        assert sent[0]["params"]["channels"] == list(ALL_CHANNELS)
        assert sent[0]["params"]["market_tickers"] == [M1]
        assert sent[0]["params"]["use_yes_price"] is True

    def test_9_the_result_carries_the_boundary_note_and_a_closed_status(
            self, tmp_path):
        init_archive(tmp_path)
        result, _ = run(tmp_path)
        assert result.status in kc.SESSION_STATUSES
        assert "OBSERVE_ONLY" in result.boundary_note
        assert "no order" in result.boundary_note
        payload = result.to_dict()
        assert json.loads(json.dumps(payload, default=str))["boundary_note"]
        assert payload["markets_subscribed"] == 1
        assert "market_tickers" not in payload


# --- 10-13: determinism ---------------------------------------------------------------
VOLATILE = ("collector_receive_time", "normalization_time",
            "receive_monotonic_ns", "normalize_monotonic_ns", "data_age_us")


def _stable(record: dict) -> dict:
    """Everything a second run must reproduce exactly.

    The five wall-clock/monotonic fields are stripped because they are the
    session's own lineage, not the venue's words; `time_to_resolution_us` is
    stripped for the same reason — it is a duration measured from OUR receive
    time, and the venue's own close stamp beside it is what is pinned.
    """
    out = {k: v for k, v in record.items() if k not in VOLATILE}
    observation = dict(out.get("normalized") or {})
    resolution = observation.get("resolution")
    if isinstance(resolution, dict):
        observation["resolution"] = {k: v for k, v in resolution.items()
                                     if k != "time_to_resolution_us"}
    out["normalized"] = observation
    return out


class TestDeterminism:
    def test_10_two_runs_produce_identical_per_market_checksums(self, tmp_path):
        """The acceptance test for the whole data path, and the reason it is
        stated in checksums rather than in 'it ran twice without crashing'."""
        first, second = tmp_path / "a", tmp_path / "b"
        for root in (first, second):
            root.mkdir()
            init_archive(root)
            run(root)
        _, out_a, records_a = replay_report(first)
        _, out_b, records_b = replay_report(second)
        assert out_a["checksums"] == out_b["checksums"]
        assert out_a["checksums"][M1] is not None
        assert out_a["publishable"] == out_b["publishable"]
        assert out_a["stats"] == out_b["stats"]
        assert out_a["subscription_stats"] == out_b["subscription_stats"]
        assert len(records_a) == len(records_b)

    def test_11_the_normalization_itself_is_reproduced_exactly(self, tmp_path):
        """Stronger than equal checksums: checksums cover the book, and most of
        the tape is not book. Every archived field except this session's own
        clock lineage must be byte-for-byte the same."""
        first, second = tmp_path / "a", tmp_path / "b"
        for root in (first, second):
            root.mkdir()
            init_archive(root)
            run(root)
        _, records_a = read_back(first)
        _, records_b = read_back(second)
        assert [_stable(r) for r in records_a] == [_stable(r) for r in records_b]

    def test_12_normalize_frame_is_pure_and_does_not_mutate_its_input(self):
        frame = trade()
        before = json.dumps(frame, sort_keys=True)
        when = kc.utcnow()
        first = kc.normalize_frame(message=frame, receive_time=when)
        second = kc.normalize_frame(message=frame, receive_time=when)
        assert first == second
        assert json.dumps(frame, sort_keys=True) == before

    def test_13_the_same_frame_normalizes_the_same_in_any_context(self):
        """No cross-frame state anywhere: a print seen after a rising book and
        the same print seen after a falling one are the same observation."""
        when = kc.utcnow()
        print_frame = trade(taker_side="no")
        alone = kc.normalize_frame(message=print_frame, receive_time=when)
        for context in (snapshot(yes=(("0.9000", "5.00"),)),
                        snapshot(no=(("0.1000", "5.00"),)),
                        ticker(bid="0.9900", ask="0.9900")):
            kc.normalize_frame(message=context, receive_time=when)
            assert kc.normalize_frame(message=print_frame,
                                      receive_time=when) == alone


# --- 14-22: trade direction -------------------------------------------------------------
class TestTradeDirection:
    def test_14_direction_comes_from_the_venues_own_field(self):
        block = kc.normalize_frame(message=trade(taker_side="yes"),
                                   receive_time=kc.utcnow())["trade"]
        assert block["direction"]["venue_field"] == "taker_side"
        assert block["direction"]["raw_taker_side"] == "yes"
        assert block["direction"]["normalized_taker_side"] == "yes"
        assert block["direction"]["source"] == kc.TRADE_DIRECTION_SOURCE
        assert block["direction"]["source"] == "venue_field:taker_side"

    def test_15_both_venue_sides_survive_the_round_trip(self, tmp_path):
        init_archive(tmp_path)
        run(tmp_path, [trade(seq=1, taker_side="yes", trade_id="a"),
                       trade(seq=2, taker_side="no", trade_id="b")])
        _, records = read_back(tmp_path)
        sides = [r["normalized"]["trade"]["direction"]["normalized_taker_side"]
                 for r in records]
        assert sides == ["yes", "no"]

    def test_16_an_absent_field_is_a_TYPED_ABSENCE_not_a_guess(self):
        """The repository's own pinned `trade` fixture
        (`tests/test_kalshi_canonical_001.py`) carries no `taker_side`, so this
        is the realistic case, not a contrived one."""
        observation = kc.normalize_frame(message=trade(taker_side=None),
                                         receive_time=kc.utcnow())
        direction = observation["trade"]["direction"]
        assert direction["normalized_taker_side"] is None
        assert direction["raw_taker_side"] is None
        assert direction["venue_field"] is None
        assert direction["source"] == kc.ABSENT_NOT_SUPPLIED
        assert observation["coverage"][kc.OBS_TRADE_DIRECTION] == \
            kc.ABSENT_NOT_SUPPLIED
        # The rest of the print is still recorded: an absent direction must not
        # cost us the price and size we DID observe.
        assert observation["coverage"][kc.OBS_TRADE_PRICE] == kc.PRESENT
        assert observation["coverage"][kc.OBS_TRADE_QUANTITY] == kc.PRESENT

    @pytest.mark.parametrize("value", ["buy", "sell", "b", "YE S", "", "1",
                                       "taker", "unknown"])
    def test_17_a_word_outside_the_venue_vocabulary_is_refused(self, value):
        observation = kc.normalize_frame(message=trade(taker_side=value),
                                         receive_time=kc.utcnow())
        direction = observation["trade"]["direction"]
        assert direction["normalized_taker_side"] is None
        assert direction["raw_taker_side"] == value        # kept verbatim
        assert observation["coverage"][kc.OBS_TRADE_DIRECTION] == \
            kc.ABSENT_UNPARSEABLE

    @pytest.mark.parametrize("value", [1, 0, True, False, ["yes"], {"side": "yes"},
                                       None])
    def test_18_a_non_string_direction_is_never_coerced(self, value):
        observation = kc.normalize_frame(message=trade(taker_side=value),
                                         receive_time=kc.utcnow())
        assert observation["trade"]["direction"]["normalized_taker_side"] is None
        assert observation["coverage"][kc.OBS_TRADE_DIRECTION] in (
            kc.ABSENT_UNPARSEABLE, kc.ABSENT_NOT_SUPPLIED)

    def test_19_case_and_padding_are_tolerated_but_recorded_raw(self):
        observation = kc.normalize_frame(message=trade(taker_side=" YES "),
                                         receive_time=kc.utcnow())
        direction = observation["trade"]["direction"]
        assert direction["normalized_taker_side"] == "yes"
        assert direction["raw_taker_side"] == " YES "

    def test_20_direction_is_not_derived_from_price_relative_to_any_quote(self):
        """A print at the ask and a print at the bid, both without the field,
        must both be absent. This is the exact case a tick or quote rule would
        answer confidently — and the case the 59-62% accuracy figure is about."""
        at_ask = trade(taker_side=None, yes_price="0.5100")
        at_bid = trade(taker_side=None, yes_price="0.4700")
        for frame in (at_ask, at_bid):
            observation = kc.normalize_frame(message=frame,
                                             receive_time=kc.utcnow())
            assert observation["trade"]["direction"]["normalized_taker_side"] is None
            assert observation["coverage"][kc.OBS_TRADE_DIRECTION] == \
                kc.ABSENT_NOT_SUPPLIED

    def test_21_the_direction_policy_rides_on_the_record_itself(self, tmp_path):
        """A boundary statement that lives only in a docstring is not on the
        artifact. A reader of a bare record can see where the sign came from."""
        init_archive(tmp_path)
        run(tmp_path, [trade(seq=1)])
        _, records = read_back(tmp_path)
        direction = records[0]["normalized"]["trade"]["direction"]
        assert "no Lee-Ready" in direction["inference_policy"]
        assert direction["venue_vocabulary"] == ["yes", "no"]

    def test_22_the_venue_price_pair_is_recorded_not_asserted(self):
        """`yes + no == 1.0000` is checkable from the tape rather than assumed
        by the parser — and when it does not hold, the record still says so."""
        good = kc.normalize_frame(message=trade(), receive_time=kc.utcnow())
        assert good["trade"]["yes_plus_no_price_units"] == 10_000
        odd = kc.normalize_frame(message=trade(yes_price="0.5000",
                                               no_price="0.4000"),
                                 receive_time=kc.utcnow())
        assert odd["trade"]["yes_plus_no_price_units"] == 9_000
        assert odd["trade"]["no_price"]["normalized_yes_price_units"] is None
        assert odd["trade"]["no_price"]["yes_scale_normalization"] == "none_applied"


# --- 23-27: inference is structurally impossible ------------------------------------
_INFERENCE_VOCABULARY = (
    "lee_ready", "leeready", "tick_rule", "tickrule", "quote_rule",
    "infer_side", "infer_direction", "classify_side", "classify_trade",
    "guess_side", "aggressor_inferred", "inferred_side", "inferred_taker",
    "bulk_volume_classification", "emo_rule", "midpoint_rule",
)


def _app_py_files():
    return sorted((REPO / "app").rglob("*.py"))


class TestInferenceIsImpossible:
    def test_23_no_inference_vocabulary_exists_anywhere_in_app(self):
        """Identifier-level, over the whole package: a classifier cannot be
        hiding under a name in another module either."""
        hits = []
        for path in _app_py_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    name = node.name
                if name and name.lower().replace("-", "_") in _INFERENCE_VOCABULARY:
                    hits.append(f"{path.relative_to(REPO)}:{name}")
        assert hits == [], hits

    def test_24_the_trade_normalizer_takes_the_venue_message_and_nothing_else(self):
        """The structural control. With one argument and no `self`, there is no
        book, no quote and no previous print in scope to classify against."""
        fns = [n for n in ast.walk(COLLECTOR_SRC)
               if isinstance(n, ast.FunctionDef) and n.name == "_normalize_trade"]
        assert len(fns) == 1
        args = fns[0].args
        assert [a.arg for a in args.args] == ["msg"]
        assert args.posonlyargs == [] and args.kwonlyargs == []
        assert args.vararg is None and args.kwarg is None

    def test_25_the_trade_normalizer_reads_no_book_or_quote_state(self):
        """Nothing resembling a price comparison is reachable from it: the
        identifiers a classifier would need are not in the function at all."""
        fn = [n for n in ast.walk(COLLECTOR_SRC)
              if isinstance(n, ast.FunctionDef) and n.name == "_normalize_trade"][0]
        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for banned in ("book", "books", "quote", "mid", "midpoint", "best_bid",
                       "best_ask", "best_yes_bid_units", "best_yes_ask_units",
                       "spread_units", "last_price", "previous", "OrderBook",
                       "SubscriptionRouter"):
            assert banned not in names, banned
        # Anti-vacuity: the function really does read the venue's own field.
        assert "TAKER_SIDE_FIELD" in names

    def test_26_taker_side_is_read_in_exactly_one_place_in_app(self):
        """One reader means one thing to review — and it means a second,
        divergent interpretation cannot appear without showing up here."""
        readers = [str(p.relative_to(REPO)) for p in _app_py_files()
                   if "taker_side" in p.read_text()]
        assert readers == ["app/realtime/collector.py"], readers
        source = COLLECTOR_PATH.read_text()
        # The literal appears once as the constant's value. Everything else
        # refers to the constant, so there is no second spelling to drift.
        assert source.count('"taker_side"') == 1

    def test_27_the_repository_safety_grep_is_still_clean(self):
        """AGENTS.md's grep, run over the package this checkpoint touched."""
        pattern = re.compile(
            r"expected_value|kelly|position_siz|paper_trad|place_order|"
            r"submit_order|create_order|wallet|recommended_side|trade_recommend|"
            r"execute_trade", re.IGNORECASE)
        text = COLLECTOR_PATH.read_text()
        hits = [line for line in text.splitlines() if pattern.search(line)]
        # The only permitted hit is the boundary statement itself, which NAMES
        # the forbidden surfaces in order to deny them.
        assert all("wallet" in line and "no order" in line for line in hits), hits
        assert len(hits) == 1


# --- 28-34: raw beside normalized, and no float ---------------------------------------
def _walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


class TestRawIsPreserved:
    def test_28_the_venue_frame_is_archived_verbatim(self, tmp_path):
        init_archive(tmp_path)
        run(tmp_path)
        _, records = read_back(tmp_path)
        for record, frame in zip(records, FULL_SESSION):
            assert record["raw"] == frame

    def test_29_every_normalized_price_sits_beside_the_venues_own_value(self,
                                                                       tmp_path):
        init_archive(tmp_path)
        run(tmp_path)
        _, records = read_back(tmp_path)
        book = only(records, "orderbook_snapshot")["normalized"]["book"]
        bid = book["bid_levels"][0]
        assert bid["venue_side"] == "yes"
        assert bid["venue_field"] == "yes_dollars_fp"
        assert bid["raw_price_value"] == "0.4700"
        assert bid["raw_price_units"] == 4700
        assert bid["normalized_yes_price_units"] == 4700
        assert bid["raw_size_value"] == "5.00"
        assert bid["contract_units"] == 500
        ask = book["ask_levels"][0]
        assert ask["venue_side"] == "no"
        assert ask["raw_price_units"] == 5100
        # The defect demo validation found: the NO ladder is ALREADY YES-scaled
        # under `use_yes_price=true`, so the ask is 0.5100 and not 0.4900.
        assert ask["normalized_yes_price_units"] == 5100
        assert book["no_side_normalization"] == "identity_yes_scaled"
        assert book["spread_units"] == 400

    def test_30_no_venue_number_ever_becomes_a_float(self, tmp_path):
        init_archive(tmp_path)
        run(tmp_path)
        _, records = read_back(tmp_path)
        floats = [v for r in records for v in _walk(r) if isinstance(v, float)]
        assert floats == []

    def test_31_a_float_in_a_frame_is_refused_not_laundered(self, tmp_path):
        """`fixedpoint.py` refuses a float outright rather than converting it,
        and this checkpoint must not soften that on the way past."""
        observation = kc.normalize_frame(
            message=trade(yes_price=0.51, count="1.00"),
            receive_time=kc.utcnow())
        assert observation["coverage"][kc.OBS_TRADE_PRICE] == kc.ABSENT_UNPARSEABLE
        assert observation["trade"]["yes_price"]["raw_price_units"] is None
        assert observation["trade"]["yes_price"]["raw_value"] == 0.51
        assert observation["trade"]["yes_price"]["parse_refusal"] == "FixedPointError"

    def test_32_an_off_contract_price_is_an_absence_with_the_raw_kept(self):
        """Five decimals is outside the authorized price contract. The value is
        not rounded to fit — rounding a venue number to make it parse is how a
        book becomes plausible and wrong."""
        observation = kc.normalize_frame(message=trade(yes_price="0.51234"),
                                         receive_time=kc.utcnow())
        assert observation["coverage"][kc.OBS_TRADE_PRICE] == kc.ABSENT_UNPARSEABLE
        assert observation["trade"]["yes_price"]["raw_value"] == "0.51234"

    def test_33_which_field_name_supplied_each_number_is_recorded(self):
        """Two candidate name sets are in play and only one is wire-verified.
        The tape says which one the venue actually used, so the first live
        session settles it from evidence."""
        mirrored = trade(taker_side="yes")
        mirrored["msg"].pop("yes_price_dollars")
        mirrored["msg"]["yes_price"] = "0.5000"
        observation = kc.normalize_frame(message=mirrored,
                                         receive_time=kc.utcnow())
        assert observation["trade"]["yes_price"]["venue_field"] == "yes_price"
        assert observation["trade"]["yes_price"]["raw_price_units"] == 5000

    def test_34_a_ladder_beyond_the_bound_keeps_raw_and_records_the_refusal(self):
        huge = [[f"0.{i:04d}", "1.00"] for i in range(kc.MAX_NORMALIZED_LEVELS + 1)]
        frame = {"type": "orderbook_snapshot", "sid": 4, "seq": 1,
                 "msg": {"market_ticker": M1, "yes_dollars_fp": huge}}
        observation = kc.normalize_frame(message=frame,
                                         receive_time=kc.utcnow())
        assert observation["coverage"][kc.OBS_BID_LEVELS] == kc.ABSENT_OVER_BOUND
        assert observation["book"]["bid_levels"] == []


# --- 35-40: the coverage contract -----------------------------------------------------
class TestCoverageIsTotal:
    def test_35_the_required_quantities_are_the_briefs_list(self):
        assert set(kc.REQUIRED_OBSERVATIONS) == {
            "timestamp", "sequence", "market", "bid_levels", "ask_levels",
            "trade_price", "trade_quantity", "trade_direction", "spread",
            "market_state", "time_to_resolution"}

    @pytest.mark.parametrize("frame", [subscribed_ack(), snapshot(), delta(),
                                       ticker(), trade(), lifecycle(),
                                       venue_error(),
                                       {"type": "brand_new_venue_frame"},
                                       {"type": "ticker", "msg": "not an object"},
                                       {}])
    def test_36_every_frame_kind_carries_a_full_coverage_map(self, frame):
        coverage = kc.normalize_frame(message=frame,
                                      receive_time=kc.utcnow())["coverage"]
        assert set(coverage) == set(kc.REQUIRED_OBSERVATIONS)
        assert all(v in kc.COVERAGE_VALUES for v in coverage.values()), coverage

    def test_37_every_required_quantity_is_observed_at_least_once_in_a_session(
            self, tmp_path):
        """Anti-vacuity for the whole coverage idea: a map that said `absent`
        everywhere would satisfy test 36 and prove nothing."""
        init_archive(tmp_path)
        run(tmp_path)
        _, records = read_back(tmp_path)
        seen = set()
        for record in records:
            for name, value in record["normalized"]["coverage"].items():
                if value == kc.PRESENT:
                    seen.add(name)
        assert seen == set(kc.REQUIRED_OBSERVATIONS), \
            set(kc.REQUIRED_OBSERVATIONS) - seen

    def test_38_time_to_resolution_comes_from_the_frames_own_close_stamp(self):
        observation = kc.normalize_frame(message=lifecycle(),
                                         receive_time=kc.utcnow())
        resolution = observation["resolution"]
        assert observation["coverage"][kc.OBS_TIME_TO_RESOLUTION] == kc.PRESENT
        assert resolution["venue_field"] == "close_ts"
        assert resolution["raw_value"] == 1786200000
        assert isinstance(resolution["time_to_resolution_us"], int)
        assert resolution["resolution_time"].endswith("Z")

    def test_39_a_close_stamp_in_the_wrong_unit_is_refused_not_guessed(self):
        """Milliseconds where seconds are documented would put resolution ~50,000
        years out. A unit we cannot confirm is an absence, not a conversion."""
        observation = kc.normalize_frame(
            message=lifecycle(close_ts=1786200000000), receive_time=kc.utcnow())
        assert observation["coverage"][kc.OBS_TIME_TO_RESOLUTION] == \
            kc.ABSENT_UNPARSEABLE
        assert observation["resolution"]["raw_value"] == 1786200000000
        assert observation["resolution"]["time_to_resolution_us"] is None

    def test_40_a_delta_never_pretends_to_know_the_spread(self):
        """One level on one side. The other side is not in the frame and this
        normalizer holds no book — so the answer is 'not from here', not zero."""
        observation = kc.normalize_frame(message=delta(), receive_time=kc.utcnow())
        assert observation["coverage"][kc.OBS_SPREAD] == kc.ABSENT_NOT_DERIVABLE
        assert observation["book"]["spread_units"] is None
        assert observation["coverage"][kc.OBS_ASK_LEVELS] == kc.PRESENT
        assert observation["coverage"][kc.OBS_BID_LEVELS] == kc.ABSENT_NOT_APPLICABLE
        level = observation["book"]["ask_levels"][0]
        assert level["delta_contract_units"] == 20100
        assert level["raw_delta_value"] == "201.00"

    def test_41_an_unknown_market_state_is_kept_verbatim_and_not_mapped(self):
        observation = kc.normalize_frame(
            message=lifecycle(status="reopened_after_review"),
            receive_time=kc.utcnow())
        assert observation["coverage"][kc.OBS_MARKET_STATE] == kc.ABSENT_UNPARSEABLE
        assert observation["market_state"]["raw_value"] == "reopened_after_review"
        assert observation["market_state"]["normalized_state"] is None


# --- 42-47: bounds and failure handling ------------------------------------------------
class TestBounds:
    def test_42_max_events_is_a_hard_cap(self, tmp_path):
        init_archive(tmp_path)
        result, _ = run(tmp_path, max_events=3)
        assert result.status == kc.STATUS_CAPPED_EVENTS
        assert result.events_received == 3
        assert result.events_archived == 3
        integrity, _, _ = replay_report(tmp_path)
        assert integrity["records"] == 3
        assert integrity["intact"] is True

    def test_43_a_stop_request_ends_the_session_after_the_current_frame(self,
                                                                       tmp_path):
        init_archive(tmp_path)
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] >= 2

        result = kc.collect_once(make_config(tmp_path),
                                 transport_factory=RecordingFactory(FULL_SESSION),
                                 stop_requested=stop)
        assert result.status == kc.STATUS_STOPPED
        assert result.events_received == 2
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["intact"] is True and out["faults"] == []

    def test_44_max_seconds_is_a_hard_cap(self, tmp_path):
        """The clock is not mocked: `max_seconds=0` is refused at construction,
        so the smallest honest test is that an already-elapsed budget stops the
        session on its first frame."""
        init_archive(tmp_path)
        config = make_config(tmp_path, max_seconds=1)
        session = kc._Session(config,
                              transport_factory=RecordingFactory(FULL_SESSION))
        session._started_ns = kc.monotonic_ns() - 5 * 1_000_000_000
        outcome = session._cap_check()
        assert outcome.status == kc.STATUS_CAPPED_TIME

    @pytest.mark.parametrize("bad", [{"max_seconds": 0}, {"max_events": 0},
                                     {"max_seconds": -1}, {"max_reconnects": -1},
                                     {"market_tickers": ()},
                                     {"market_tickers": ("",)},
                                     {"market_tickers": [M1]},
                                     {"environment": "prod"}])
    def test_45_a_session_without_a_bound_cannot_be_constructed(self, bad,
                                                               tmp_path):
        with pytest.raises(kx.CapabilityError):
            make_config(tmp_path, **bad)

    def test_46_an_archive_root_is_required_unless_dry_run(self, tmp_path):
        with pytest.raises(kx.CapabilityError):
            make_config(tmp_path, archive_root=None)
        assert make_config(tmp_path, archive_root=None, dry_run=True).dry_run


class TestFailureHandling:
    def test_47_a_malformed_frame_is_counted_and_never_archived(self, tmp_path):
        init_archive(tmp_path)
        frames = [snapshot(), "not a dict", 42, None, [1, 2], delta()]
        result, _ = run(tmp_path, frames)
        assert result.status == kc.STATUS_OK
        assert result.events_received == 6
        assert result.frames_malformed == 4
        assert result.events_archived == 2
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["records"] == 2
        assert out["faults"] == []

    def test_48_a_rejected_record_is_counted_and_the_session_continues(self,
                                                                      tmp_path):
        """Section 8.6: one pathological payload must not end a session. This
        also PINS the message prefix the collector uses to tell a per-record
        rejection from a broken partition — it drives a real rejection through
        a real archive rather than asserting the string."""
        init_archive(tmp_path)
        poison = {"type": "ticker", "sid": 4, "seq": 2,
                  "msg": {"market_ticker": M1, "volume": 1.5}}   # a float
        result, _ = run(tmp_path, [snapshot(), poison, delta()])
        assert result.status == kc.STATUS_OK
        assert result.events_received == 3
        assert result.events_rejected == 1
        assert result.events_archived == 2
        assert result.rejection_reasons[0].startswith(kc.RECORD_REJECTION_PREFIX)
        assert "not_canonical" in result.rejection_reasons[0]
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["records"] == 2 and out["faults"] == []

    def test_49_a_partition_level_failure_stops_the_session(self, tmp_path,
                                                            monkeypatch):
        """The other half of the same fork: continuing past a broken partition
        would produce a tape with a hole nothing records."""
        init_archive(tmp_path)

        def _broken(self, envelope):
            raise ArchiveError("could not open segment 'kalshi.2026-08-08T00'")

        monkeypatch.setattr(EventArchive, "append", _broken)
        result, _ = run(tmp_path)
        assert result.status == kc.STATUS_ARCHIVE_ERROR
        assert result.events_received == 1
        assert result.events_archived == 0
        assert "could not open segment" in result.detail

    def test_50_the_archive_is_closed_even_when_the_session_fails(self, tmp_path,
                                                                  monkeypatch):
        """`close()` is the commit point and it runs in a `finally`: an
        unclosed segment is explicitly not evidence."""
        init_archive(tmp_path)
        closed = {"n": 0}
        real_close = EventArchive.close

        def _counting_close(self):
            closed["n"] += 1
            return real_close(self)

        monkeypatch.setattr(EventArchive, "close", _counting_close)

        def _boom():
            raise RuntimeError("the loop exploded")

        with pytest.raises(RuntimeError):
            kc.collect_once(make_config(tmp_path),
                            transport_factory=lambda: _ExplodingTransport(_boom))
        assert closed["n"] == 1

    def test_51_a_metrics_hook_that_raises_cannot_take_the_tape_down(self,
                                                                    tmp_path):
        """CP3.5: the hostile object implements the TYPED seam and raises from
        every method of it. Before CP3.5 this test passed against an object
        with the old method names, so it would have passed even if the seam had
        no caller at all — the `AttributeError` counted the same as a raise."""
        init_archive(tmp_path)

        class Hostile(kc.NullCollectorMetrics):
            def on_frame(self, received_mono_ns, wire_bytes=0):
                raise RuntimeError("metrics exploded")

            def on_append(self, elapsed_ns, rotated=False):
                raise RuntimeError("metrics exploded")

            def on_append_rejected(self, elapsed_ns=0):
                raise RuntimeError("metrics exploded")

            def on_frame_malformed(self):
                raise RuntimeError("metrics exploded")

            def on_sequence_fault(self, kind):
                raise RuntimeError("metrics exploded")

            def on_disconnect(self):
                raise RuntimeError("metrics exploded")

            def on_reconnect(self, subscription_generation=None):
                raise RuntimeError("metrics exploded")

            def on_subscription_generation(self, generation):
                raise RuntimeError("metrics exploded")

        result = kc.collect_once(make_config(tmp_path),
                                 transport_factory=RecordingFactory(FULL_SESSION),
                                 metrics=Hostile())
        assert result.status == kc.STATUS_OK
        assert result.events_archived == len(FULL_SESSION)
        assert result.metrics_errors > 0          # counted, never silent
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["intact"] is True and out["faults"] == []

    def test_52_the_metrics_seam_is_never_handed_a_market_ticker(self, tmp_path):
        """Every argument that crosses the seam, captured through the WIRED
        path. §7.2's no-ticker guarantee is structural — there is no parameter
        a market identifier could ride in — so the assertion is about the whole
        argument list, not about one field name."""
        init_archive(tmp_path)
        seen: list = []

        class Recorder(kc.NullCollectorMetrics):
            def on_frame(self, received_mono_ns, wire_bytes=0):
                seen.append(("on_frame", received_mono_ns, wire_bytes))

            def on_append(self, elapsed_ns, rotated=False):
                seen.append(("on_append", elapsed_ns, rotated))

            def on_frame_malformed(self):
                seen.append(("on_frame_malformed",))

            def on_sequence_fault(self, kind):
                seen.append(("on_sequence_fault", kind))

            def on_subscription_generation(self, generation):
                seen.append(("on_subscription_generation", generation))

            def on_disconnect(self):
                seen.append(("on_disconnect",))

        kc.collect_once(make_config(tmp_path),
                        transport_factory=RecordingFactory(FULL_SESSION),
                        metrics=Recorder())
        assert [call for call in seen if call[0] == "on_frame"]
        blob = json.dumps(seen, default=str)
        assert M1 not in blob and "KX" not in blob
        # Anti-vacuity: the needle really is findable when it IS present.
        assert M1 in json.dumps([("on_frame", M1)])
        for call in seen:
            for argument in call[1:]:
                assert isinstance(argument, (int, bool)) or argument in (
                    "gap", "regression", "duplicate"), call


class _ExplodingTransport(kx.FixtureTransport):
    def __init__(self, boom):
        super().__init__([])
        self._boom = boom

    async def __aiter__(self):
        self._boom()
        yield {}


# --- 53-58: reconnect, supersede, recovery ----------------------------------------------
class TestReconnectAndRecovery:
    def test_53_a_socket_failure_reconnects_within_the_bound(self, tmp_path):
        init_archive(tmp_path)
        made: list = []

        def factory():
            transport = FailingTransport(
                [snapshot(), delta()] if not made else [ticker(seq=3)],
                fail_after=2 if not made else 99)
            made.append(transport)
            return transport

        result = kc.collect_once(make_config(tmp_path, max_reconnects=2),
                                 transport_factory=factory)
        assert result.reconnects == 1
        assert result.status == kc.STATUS_OK
        assert result.events_archived == 3
        assert len(made) == 2
        # The new stream's `seq` lives in a different namespace, so the
        # subscription is superseded rather than compared across the break.
        assert result.subscription_generations >= 2

    def test_54_exhausting_the_bound_ends_the_session_and_says_so(self, tmp_path):
        init_archive(tmp_path)
        result = kc.collect_once(
            make_config(tmp_path, max_reconnects=2),
            transport_factory=lambda: FailingTransport([snapshot()],
                                                       fail_after=1))
        assert result.status == kc.STATUS_CAPPED_RECONNECTS
        assert result.reconnects == 2
        assert "max_reconnects=2 exhausted" in result.detail
        # Rung 5 is a design commitment: a collector that cannot keep up stops.
        # What it collected before stopping is still committed evidence.
        integrity, out, _ = replay_report(tmp_path)
        assert integrity["intact"] is True and out["faults"] == []
        assert integrity["records"] == 3

    def test_55_zero_reconnects_means_the_first_failure_ends_it(self, tmp_path):
        init_archive(tmp_path)
        result = kc.collect_once(
            make_config(tmp_path, max_reconnects=0),
            transport_factory=lambda: FailingTransport([snapshot()],
                                                       fail_after=1))
        assert result.status == kc.STATUS_CAPPED_RECONNECTS
        assert result.reconnects == 0

    def test_56_a_sequence_gap_triggers_exactly_one_snapshot_request(self,
                                                                    tmp_path):
        """The existing model is DRIVEN, not re-invented: `begin_recovery` plus
        `get_snapshot` WITH tickers (the venue rejects the sids-only form with
        code 14, and that error costs a sequence slot). Once per fault, not once
        per frame — after a gap every later delta faults too."""
        init_archive(tmp_path)
        frames = [snapshot(seq=1), delta(seq=9), delta(seq=10), delta(seq=11)]
        result, factory = run(tmp_path, frames)
        assert result.sequence_faults == 3
        assert result.recoveries_requested == 1
        sent = factory.made[0].sent
        assert [f["cmd"] for f in sent] == ["subscribe", "update_subscription"]
        recovery = sent[1]
        assert recovery["params"]["action"] == kx.RECOVERY_ACTION_GET_SNAPSHOT
        assert recovery["params"]["market_tickers"] == [M1]
        assert wt.assert_sendable(recovery) == recovery

    def test_57_a_gap_never_costs_the_record(self, tmp_path):
        """Archive first, interpret second. Every frame is on the tape even
        though three of them could not be ordered."""
        init_archive(tmp_path)
        frames = [snapshot(seq=1), delta(seq=9), delta(seq=10), delta(seq=11)]
        result, _ = run(tmp_path, frames)
        assert result.events_archived == 4
        integrity, out, records = replay_report(tmp_path)
        assert integrity["records"] == 4
        assert integrity["intact"] is True
        # Replay sees the same fault the live loop saw — the two agree because
        # both route through `SubscriptionRouter`, grouped by sid.
        assert out["faults"]
        assert out["publishable"][M1] is False
        assert out["checksums"][M1] is None

    def test_58_a_fresh_snapshot_re_arms_recovery(self, tmp_path):
        """After the venue answers, a LATER gap must be able to ask again."""
        init_archive(tmp_path)
        frames = [snapshot(seq=1), delta(seq=9),          # gap -> ask
                  snapshot(seq=10), delta(seq=20)]        # recovered, then gap
        result, factory = run(tmp_path, frames)
        assert result.recoveries_requested == 2
        assert [f["cmd"] for f in factory.made[0].sent] == [
            "subscribe", "update_subscription", "update_subscription"]


# --- 59-64: the safety surface ------------------------------------------------------
class TestSafetySurface:
    @pytest.mark.parametrize("channel", kx.FORBIDDEN_CHANNELS)
    def test_59_no_forbidden_channel_is_reachable_from_a_config(self, channel,
                                                                tmp_path):
        """A `fill` in a config file, an env var or a CLI argument dies before
        any object exists that could open a socket."""
        with pytest.raises(kx.CapabilityError) as exc:
            make_config(tmp_path, channels=(channel,))
        assert "forbidden" in str(exc.value)

    @pytest.mark.parametrize("channel", ["fills", "orderbook_delta_v2", "",
                                         "ticker_v2", "market_positions"])
    def test_60_the_channel_allowlist_is_closed_not_merely_screened(self,
                                                                    channel,
                                                                    tmp_path):
        with pytest.raises(kx.CapabilityError):
            make_config(tmp_path, channels=(channel,))

    def test_61_the_default_channel_set_is_the_two_recommended_ones(self):
        assert kc.DEFAULT_CHANNELS == ("orderbook_delta", "ticker")
        assert set(kc.DEFAULT_CHANNELS) <= set(kx.ALLOWED_CHANNELS)

    def test_62_the_collector_opens_no_database_session(self):
        """Run in a FRESH interpreter: importing the collector must not drag in
        SQLAlchemy at all. Section 3 keeps this lane entirely outside the SQLite
        contention and backup-coordination stories."""
        probe = (
            "import sys;"
            "import app.realtime.collector as c;"
            "print('collector' if 'app.realtime.collector' in sys.modules else 'no');"
            "print('sqlalchemy' if 'sqlalchemy' in sys.modules else 'clean')")
        out = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO),
                             capture_output=True, text=True, timeout=180)
        assert out.returncode == 0, out.stderr
        lines = out.stdout.split()
        # Anti-vacuity: the module really was imported in that interpreter.
        assert lines == ["collector", "clean"], out.stdout

    def test_63_no_session_maker_is_named_anywhere_in_the_module(self):
        names = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for banned in ("get_sessionmaker", "SessionLocal", "sessionmaker",
                       "get_db", "engine", "execute", "commit", "cursor"):
            assert banned not in names, banned

    def test_64_the_module_opens_no_socket_and_names_no_host(self):
        source = COLLECTOR_PATH.read_text()
        # No URL literal: the host is resolved from `WS_HOSTS` inside the
        # transport and there is no url parameter anywhere on this path.
        for banned in ("wss://", "https://", "urlopen"):
            assert banned not in source, banned
        # Identifier level for the rest, so the module may still EXPLAIN in
        # prose why it holds no socket — a raw substring scan would fail on its
        # own docstring, a false failure this repository has produced before.
        names = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for banned in ("socket", "create_connection", "getaddrinfo", "connect_ex",
                       "aiohttp", "urlopen"):
            assert banned not in names, banned
        mods = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        assert not any(m.startswith(("websockets", "http", "urllib", "requests"))
                       for m in mods), mods

    def test_65_the_transport_is_a_seam_and_the_loop_holds_no_credential(self):
        """`run_session` takes a factory. The signer never appears in the loop:
        the credential lane is `load_observer_signer` and stops there."""
        fns = {n.name: n for n in ast.walk(COLLECTOR_SRC)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        session_names = set()
        for name in ("run_session", "collect_once"):
            for node in ast.walk(fns[name]):
                if isinstance(node, ast.Name):
                    session_names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    session_names.add(node.attr)
        for banned in ("load_observer_signer", "signer", "credential_path",
                       "key_id", "headers_for"):
            assert banned not in session_names, banned
        assert "transport_factory" in session_names
