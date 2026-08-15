"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP1 — `KalshiWebsocketTransport`, offline.

**No test in this file opens a socket.** An autouse fixture replaces the module's
default connector with one that fails the test if it is ever called, so "these
tests are offline" is enforced rather than asserted in a comment.

Most of these assert a refusal. The transport is the first object in the package
that owns a socket, and a socket that can be talked into writing an arbitrary
frame makes every allowlist in `kalshi.py` decorative.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import fields, is_dataclass
from decimal import Decimal

import pytest
from websockets.exceptions import (
    ConnectionClosedError,
    ConnectionClosedOK,
    PayloadTooBig,
)
from websockets.frames import Close

from app.realtime import book as bk
from app.realtime import kalshi as kx
from app.realtime import ws_transport as wt


# --- offline enforcement ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any test that reaches the real `websockets.connect` fails loudly."""

    async def _forbidden(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError(
            "CP1 is offline: a test attempted a real websocket connection")

    monkeypatch.setattr(wt, "_websockets_connect", _forbidden)


def test_default_connector_is_the_modern_asyncio_client():
    """Pinned so a future edit cannot quietly move to the deprecated legacy client.

    CP0 12.6: `websockets.legacy` warns on import and nothing here may depend on
    it; `websockets.asyncio.client.connect` is the modern entrypoint.
    """
    import websockets.asyncio.client as modern

    # The autouse fixture has monkeypatched the module attribute, so read the
    # pristine value from the imported library instead.
    src = inspect.getsource(wt)
    assert "from websockets.asyncio.client import connect as _websockets_connect" in src
    assert "websockets.legacy" not in src
    assert callable(modern.connect)


# --- doubles ------------------------------------------------------------------------


class FakeSigner:
    """Signs nothing. Records that the ONE permitted purpose was requested."""

    def __init__(self):
        self.calls = []

    def headers_for(self, *, purpose, timestamp_ms):
        self.calls.append((purpose, timestamp_ms))
        return {"KALSHI-ACCESS-KEY": "kid",
                "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
                "KALSHI-ACCESS-SIGNATURE": "sig"}


class FakeConnection:
    """A websockets-shaped object with no socket underneath."""

    def __init__(self, frames=(), *, end_exc=None, recv_messages=None):
        self._frames = list(frames)
        self.sent = []
        self.closed = False
        self.end_exc = end_exc or ConnectionClosedOK(
            Close(1000, ""), Close(1000, ""), True)
        if recv_messages is not None:
            self.recv_messages = recv_messages

    async def send(self, text):
        self.sent.append(text)

    async def recv(self, decode=None):
        assert decode is False, (
            "the transport must read with decode=False so the recorded byte "
            "length is the wire length")
        if self._frames:
            return self._frames.pop(0)
        raise self.end_exc

    async def close(self):
        self.closed = True


def make_transport(conn, **kw):
    seen = {}

    async def _connector(uri, **kwargs):
        seen["uri"] = uri
        seen["kwargs"] = kwargs
        return conn

    kw.setdefault("environment", kx.ENV_DEMO)
    kw.setdefault("signer", FakeSigner())
    t = wt.KalshiWebsocketTransport(_connector=_connector, **kw)
    return t, seen


def b(obj) -> bytes:
    """Wire bytes. A str is taken verbatim; anything else is JSON-encoded."""
    if isinstance(obj, str):
        return obj.encode("utf-8")
    return json.dumps(obj).encode("utf-8")


SUB = kx.build_subscribe(1, ["orderbook_delta", "ticker"], ["TICK-A"])


# --- the Verify line: send() refuses a `fill` channel --------------------------------


@pytest.mark.parametrize("channel", kx.FORBIDDEN_CHANNELS)
async def test_send_refuses_every_forbidden_channel(channel):
    """`fill` and every other private stream, refused at the wire boundary.

    The allowlist already runs in `build_subscribe`, but a transport that only
    trusted the builder would be trusting its caller. This runs it again on the
    frame that is about to be written.
    """
    frame = {"id": 1, "cmd": "subscribe",
             "params": {"channels": [channel], "use_yes_price": True,
                        "market_tickers": ["TICK-A"]}}
    t, _ = make_transport(FakeConnection())
    with pytest.raises(kx.CapabilityError) as exc:
        await t.send(frame)
    assert channel in str(exc.value)
    assert t.counters.commands_refused == 1
    assert t.counters.commands_sent == 0


async def test_fill_is_refused_even_on_a_connected_transport():
    """The refusal is not an artefact of being unconnected."""
    conn = FakeConnection()
    t, _ = make_transport(conn)
    await t.connect()
    frame = {"id": 1, "cmd": "subscribe",
             "params": {"channels": ["ticker", "fill"], "use_yes_price": True,
                        "market_tickers": ["TICK-A"]}}
    with pytest.raises(kx.CapabilityError):
        await t.send(frame)
    assert conn.sent == [], "nothing may reach the socket on a refusal"


async def test_channel_outside_the_allowlist_is_refused():
    """A channel a FUTURE venue release adds is refused by default."""
    frame = {"id": 1, "cmd": "subscribe",
             "params": {"channels": ["some_new_private_channel"],
                        "use_yes_price": True, "market_tickers": ["TICK-A"]}}
    t, _ = make_transport(FakeConnection())
    with pytest.raises(kx.CapabilityError):
        await t.send(frame)


# --- the Verify line: send() refuses an unknown `cmd` --------------------------------


@pytest.mark.parametrize("cmd", [
    "create_order", "cancel_order", "amend_order", "login", "ping",
    "get_positions", "", "SUBSCRIBE",
])
async def test_send_refuses_unknown_cmd(cmd):
    t, _ = make_transport(FakeConnection())
    frame = dict(SUB)
    frame["cmd"] = cmd
    with pytest.raises(kx.CapabilityError) as exc:
        await t.send(frame)
    assert "closed outbound set" in str(exc.value)


async def test_the_outbound_command_set_is_exactly_the_builders():
    assert wt.SENDABLE_COMMANDS == ("subscribe", "unsubscribe",
                                    "update_subscription")


# --- the Verify line: send() refuses a raw dict --------------------------------------


@pytest.mark.parametrize("raw", [
    {},
    {"foo": "bar"},
    {"cmd": "subscribe"},                                   # no id, no params
    {"id": 1, "cmd": "subscribe"},                           # no params
    {"id": 1, "cmd": "subscribe", "params": {}},             # empty params
    # A plausible hand-rolled subscribe that no builder would emit: no
    # use_yes_price, so every NO level would be reinterpreted by a server-side
    # default flip.
    {"id": 1, "cmd": "subscribe",
     "params": {"channels": ["ticker"], "market_tickers": ["T"]}},
    # use_yes_price explicitly wrong.
    {"id": 1, "cmd": "subscribe",
     "params": {"channels": ["ticker"], "use_yes_price": False,
                "market_tickers": ["T"]}},
    # An extra parameter the builder never produces.
    {"id": 1, "cmd": "subscribe",
     "params": {"channels": ["ticker"], "use_yes_price": True,
                "market_tickers": ["T"], "client_order_id": "x"}},
    # An extra top-level key.
    {"id": 1, "cmd": "subscribe", "params": SUB["params"], "auth": "token"},
    # No tickers: the venue rejects the sids-only get_snapshot with code 14,
    # and a tickerless subscribe is not a builder shape either.
    {"id": 1, "cmd": "subscribe",
     "params": {"channels": ["ticker"], "use_yes_price": True,
                "market_tickers": []}},
    # get_snapshot without the wire-confirmed required tickers.
    {"id": 1, "cmd": "update_subscription",
     "params": {"sids": [1], "action": "get_snapshot"}},
    # An action that is not the one recovery action.
    {"id": 1, "cmd": "update_subscription",
     "params": {"sids": [1], "action": "add_markets",
                "market_tickers": ["T"]}},
    # Several sids in one frame: no builder emits that.
    {"id": 1, "cmd": "unsubscribe", "params": {"sids": [1, 2]}},
    {"id": 1, "cmd": "unsubscribe", "params": {"sids": []}},
    # Types that would coerce silently if the check used isinstance/str().
    {"id": True, "cmd": "unsubscribe", "params": {"sids": [1]}},
    {"id": "1", "cmd": "unsubscribe", "params": {"sids": [1]}},
    {"id": 1, "cmd": "unsubscribe", "params": {"sids": ["1"]}},
    {"id": 1, "cmd": "subscribe",
     "params": {"channels": ["ticker"], "use_yes_price": True,
                "market_tickers": [123]}},
    {"id": 1, "cmd": "subscribe",
     "params": {"channels": "ticker", "use_yes_price": True,
                "market_tickers": ["T"]}},
])
async def test_send_refuses_raw_dicts(raw):
    """Only what `build_*` emits may reach the socket.

    Section 4 of the milestone: "the transport's send() must accept ONLY dicts
    produced by build_subscribe / build_get_snapshot / build_unsubscribe /
    build_resubscribe. No raw-dict send path may exist."
    """
    t, _ = make_transport(FakeConnection())
    with pytest.raises(kx.CapabilityError):
        await t.send(raw)
    assert t.counters.commands_sent == 0


@pytest.mark.parametrize("raw", [None, "subscribe", 1, ["subscribe"],
                                 b'{"cmd":"subscribe"}', object()])
async def test_send_refuses_non_dicts(raw):
    t, _ = make_transport(FakeConnection())
    with pytest.raises(kx.CapabilityError):
        await t.send(raw)


async def test_send_refuses_a_dict_subclass_that_lies():
    """`type(x) is dict`, not `isinstance` — a subclass can lie about its contents.

    Same reasoning `verify_scopes` already applies at `kalshi.py:202-214`.
    """

    class Sneaky(dict):
        def __iter__(self):
            return iter(("id", "cmd", "params"))

    t, _ = make_transport(FakeConnection())
    with pytest.raises(kx.CapabilityError):
        await t.send(Sneaky(SUB))


@pytest.mark.parametrize("frame", [
    kx.build_subscribe(7, ["orderbook_delta", "ticker"], ["A", "B"]),
    kx.build_subscribe(1, ["trade", "market_lifecycle_v2"], ["A"]),
    kx.build_unsubscribe(3, 42),
    kx.build_get_snapshot(4, 42, ["A"]),
    kx.build_resubscribe(5, 9, ["ticker"], ["A"])[0],
    kx.build_resubscribe(5, 9, ["ticker"], ["A"])[1],
    kx.build_resubscribe_snapshot(6, 9, ["A"]),
])
async def test_builder_output_is_accepted_and_written_verbatim(frame):
    conn = FakeConnection()
    t, _ = make_transport(conn)
    await t.connect()
    await t.send(frame)
    assert t.counters.commands_sent == 1
    assert json.loads(conn.sent[0]) == frame


async def test_the_bytes_written_are_the_builders_output_not_the_callers_object():
    """`assert_sendable` returns the REBUILT frame, so the caller's dict is never
    the thing serialized. An object that mutates between validation and encoding
    therefore cannot smuggle anything through."""
    rebuilt = wt.assert_sendable(SUB)
    assert rebuilt == SUB and rebuilt is not SUB


# --- the Verify line: the class exposes NO raw-send method ---------------------------


def test_class_exposes_no_raw_send_method():
    names = set(dir(wt.KalshiWebsocketTransport))
    forbidden = {"send_text", "send_bytes", "send_raw", "send_json", "sendall",
                 "write", "write_frame", "raw_send", "ping", "pong",
                 "connection", "conn", "websocket", "ws", "socket"}
    assert not (names & forbidden), sorted(names & forbidden)
    # Exactly one outbound method, and it is the governed one.
    senders = {n for n in names if "send" in n or "write" in n}
    assert senders == {"send"}


def test_no_url_parameter_exists_on_the_constructor():
    """The host is resolved from `WS_HOSTS`, never supplied by a caller."""
    params = set(inspect.signature(
        wt.KalshiWebsocketTransport.__init__).parameters)
    assert not (params & {"url", "uri", "host", "endpoint"})


def _attribute_reachable(root):
    """Everything findable by ordinary attribute traversal from `root`."""
    seen, out, stack = set(), [], [root]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        out.append(obj)
        for value in list(getattr(obj, "__dict__", {}).values()):
            stack.append(value)
        if is_dataclass(obj) and not isinstance(obj, type):
            for f in fields(obj):
                stack.append(getattr(obj, f.name, None))
    return out


async def test_the_connection_object_is_not_reachable_from_the_instance():
    """Containment by construction: `connect()` keeps the connection in closures.

    There is no `self._conn`. The same reasoning `auth.py` applies to key
    material (`SAFETY_BOUNDARIES:70`) applies here to the socket.
    """
    conn = FakeConnection()
    t, _ = make_transport(conn)
    await t.connect()
    reachable = _attribute_reachable(t)
    assert conn not in reachable
    assert not any(getattr(o, "__class__", None) is FakeConnection
                   for o in reachable)


async def test_reaching_the_private_ops_record_still_buys_no_raw_send():
    """Nothing in Python is truly private, so the containment must not DEPEND on
    privacy. It does not: the one writer validates before it writes, so an
    attacker holding the mangled attribute gets the governed path anyway."""
    conn = FakeConnection()
    t, _ = make_transport(conn)
    await t.connect()
    ops = getattr(t, "_KalshiWebsocketTransport__ops")
    with pytest.raises(kx.CapabilityError):
        await ops.send_governed({"id": 1, "cmd": "create_order",
                                 "params": {"sids": [1]}})
    with pytest.raises(kx.CapabilityError):
        await ops.send_governed(
            {"id": 1, "cmd": "subscribe",
             "params": {"channels": ["fill"], "use_yes_price": True,
                        "market_tickers": ["A"]}})
    with pytest.raises(kx.CapabilityError):
        await ops.send_governed("raw text")
    assert conn.sent == []


def test_no_forbidden_channel_string_is_a_default_anywhere_in_the_module():
    src = inspect.getsource(wt)
    for channel in kx.FORBIDDEN_CHANNELS:
        # The word may appear in prose; it must never appear as a quoted value.
        assert f'"{channel}"' not in src, channel
        assert f"'{channel}'" not in src, channel


# --- the Verify line: malformed frames never reach make_envelope ---------------------


MALFORMED = [
    b"not json at all",
    b"",
    b"{",
    b'{"type": "ok"',                       # truncated
    b"[1, 2, 3]",                            # JSON, not an object
    b'"a string"',
    b"12345",
    b"null",
    b"true",
    b"\xff\xfe not utf-8",
    b"NaN",                                  # loads_exact refuses non-finite
    b'{"a": Infinity}',
]


@pytest.mark.parametrize("payload", MALFORMED)
async def test_malformed_frames_are_counted_and_never_yielded(payload):
    conn = FakeConnection([payload])
    t, _ = make_transport(conn)
    await t.connect()
    got = [f async for f in t]
    assert got == []
    assert t.counters.frames_received == 1
    assert t.counters.frames_malformed == 1
    assert t.counters.frames_yielded == 0


async def test_a_non_payload_type_is_malformed_not_a_crash():
    conn = FakeConnection([object()])
    t, _ = make_transport(conn)
    await t.connect()
    assert [f async for f in t] == []
    assert t.counters.frames_malformed == 1


async def test_malformed_frames_never_reach_make_envelope(monkeypatch):
    """The collector loop, simulated: only what the transport yields is enveloped."""
    calls = []
    real = bk.make_envelope

    def spy(**kw):
        calls.append(kw["message"])
        return real(**kw)

    monkeypatch.setattr(bk, "make_envelope", spy)

    good = {"type": "ticker", "sid": 1, "seq": 1,
            "msg": {"market_ticker": "TICK-A", "ts_ms": 1786150148065}}
    conn = FakeConnection(MALFORMED + [b(good)] + MALFORMED)
    t, _ = make_transport(conn)
    await t.connect()
    async for frame in t:
        bk.make_envelope(venue="kalshi", environment="demo", channel="ticker",
                         message=frame, receive_time=bk.utcnow(),
                         receive_mono=bk.monotonic_ns())

    assert calls == [good], "exactly one frame was a venue message"
    assert t.counters.frames_malformed == 2 * len(MALFORMED)
    assert t.counters.frames_yielded == 1
    assert t.counters.frames_received == 2 * len(MALFORMED) + 1


async def test_one_bad_frame_does_not_end_the_session():
    conn = FakeConnection([b"garbage", b('{"type": "ok"}'), b"[]",
                           b('{"type": "ticker"}')])
    t, _ = make_transport(conn)
    await t.connect()
    got = [f async for f in t]
    assert got == [{"type": "ok"}, {"type": "ticker"}]


# --- no value passes through float ---------------------------------------------------


async def test_bare_numbers_parse_to_decimal_never_float():
    """`fixedpoint.loads_exact`, not `json.loads`.

    `segment.py:585-589`: "The venue transport is the correct place to produce
    Decimal... A Python float reaching submission is a contract violation."
    """
    conn = FakeConnection([b'{"type":"t","msg":{"yes_bid":0.615,"n":3}}'])
    t, _ = make_transport(conn)
    await t.connect()
    (frame,) = [f async for f in t]
    value = frame["msg"]["yes_bid"]
    assert type(value) is Decimal and value == Decimal("0.615")
    assert type(frame["msg"]["n"]) is int


async def test_a_frame_that_is_a_dict_is_yielded_as_an_exact_dict():
    conn = FakeConnection([b('{"type":"ok"}')])
    t, _ = make_transport(conn)
    await t.connect()
    (frame,) = [f async for f in t]
    assert type(frame) is dict


# --- max_size / PayloadTooBig ---------------------------------------------------------


def test_max_size_is_chosen_deliberately_not_inherited():
    assert wt.MAX_FRAME_BYTES == 8 * 1024 * 1024
    assert wt.MAX_FRAME_BYTES > 2 ** 20, "must exceed the library default"
    t, _ = make_transport(FakeConnection())
    assert t.max_size == wt.MAX_FRAME_BYTES


@pytest.mark.parametrize("bad", [None, 0, -1, 1024, wt.MAX_FRAME_BYTES + 1,
                                 "8388608", 8.0, (None, None)])
def test_max_size_rejects_unbounded_and_out_of_range_values(bad):
    """`max_size=None` removes the ceiling; a tiny ceiling manufactures disconnects."""
    with pytest.raises(kx.CapabilityError):
        make_transport(FakeConnection(), max_size=bad)


async def test_max_size_is_passed_to_the_client():
    conn = FakeConnection()
    t, seen = make_transport(conn, max_size=1_048_576)
    await t.connect()
    assert seen["kwargs"]["max_size"] == 1_048_576


async def test_payload_too_big_surfaces_as_a_named_error_not_a_mystery_disconnect():
    """CP0 12.1: `PayloadTooBig` FAILS THE CONNECTION rather than dropping a message.

    `protocol.py:625-628` calls `fail(CloseCode.MESSAGE_TOO_BIG)` and stores the
    exception in `parser_exc`; `recv()` then raises `ConnectionClosed` chained
    `from recv_exc` (`asyncio/connection.py:324`). So the operator-visible symptom
    is a close, and without classification it is indistinguishable from a venue
    hang-up. It is classified.
    """
    closed = ConnectionClosedError(None, Close(1009, "over max size"))
    closed.__cause__ = PayloadTooBig(9_000_000, 8_388_608)
    conn = FakeConnection([b('{"type":"ok"}')], end_exc=closed)
    t, _ = make_transport(conn)
    await t.connect()

    seen = []
    with pytest.raises(wt.FrameTooLargeError) as exc:
        async for frame in t:
            seen.append(frame)

    assert seen == [{"type": "ok"}], "frames before the fault are still delivered"
    assert str(wt.MAX_FRAME_BYTES) in str(exc.value)
    assert "max_size" in str(exc.value)
    assert t.counters.frames_oversize == 1
    assert t.last_close.cause == wt.CLOSE_OVERSIZE
    assert t.last_close.code == 1009
    assert t.last_close.initiator == "local"


async def test_close_1009_alone_is_enough_to_classify_oversize():
    """The chained cause is a library detail; the close code is the wire fact."""
    closed = ConnectionClosedError(None, Close(1009, "message too big"))
    conn = FakeConnection(end_exc=closed)
    t, _ = make_transport(conn)
    await t.connect()
    with pytest.raises(wt.FrameTooLargeError):
        async for _ in t:
            pass
    assert t.last_close.cause == wt.CLOSE_OVERSIZE


# --- close classification --------------------------------------------------------------


async def test_our_own_keepalive_fuse_is_named_as_ours():
    """CP0 12.7: under overload OUR client closes with 1011 before the venue acts.

    Rung 3a, not 3b. Reading the discouraged `close_code` property could not tell
    these apart; `sent` vs `rcvd` can.
    """
    closed = ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))
    conn = FakeConnection(end_exc=closed)
    t, _ = make_transport(conn)
    await t.connect()
    assert [f async for f in t] == []
    assert t.last_close.cause == wt.CLOSE_KEEPALIVE
    assert t.last_close.initiator == "local"
    assert "our own client" in t.last_close.detail.lower()


async def test_a_venue_close_is_attributed_to_the_venue():
    closed = ConnectionClosedError(Close(1001, "going away"), None)
    conn = FakeConnection(end_exc=closed)
    t, _ = make_transport(conn)
    await t.connect()
    assert [f async for f in t] == []
    assert t.last_close.cause == wt.CLOSE_REMOTE
    assert t.last_close.initiator == "remote"
    assert t.last_close.code == 1001


async def test_a_normal_close_ends_iteration_cleanly():
    conn = FakeConnection([b('{"type":"ok"}')])
    t, _ = make_transport(conn)
    await t.connect()
    assert [f async for f in t] == [{"type": "ok"}]
    assert t.last_close is not None
    assert t.connected is False
    assert t.counters.closes == 1


async def test_a_close_with_no_frames_at_all_is_unknown_not_invented():
    closed = ConnectionClosedError(None, None)
    conn = FakeConnection(end_exc=closed)
    t, _ = make_transport(conn)
    await t.connect()
    assert [f async for f in t] == []
    assert t.last_close.cause == wt.CLOSE_UNKNOWN
    assert t.last_close.initiator == "unknown"
    assert t.last_close.code is None


# --- connect: the CP0 API corrections ---------------------------------------------------


async def test_connect_uses_additional_headers_not_extra_headers():
    """CP0 12.6 row 2: the v12 spelling does not exist on 16.0 and would
    `TypeError` at connect, on the signed handshake."""
    conn = FakeConnection()
    signer = FakeSigner()
    t, seen = make_transport(conn, signer=signer)
    await t.connect()
    assert "additional_headers" in seen["kwargs"]
    assert "extra_headers" not in seen["kwargs"]
    assert seen["kwargs"]["additional_headers"]["KALSHI-ACCESS-KEY"] == "kid"
    # And it asked for the one purpose that exists for a websocket.
    assert [p for p, _ in signer.calls] == [kx.AuthPurpose.WEBSOCKET_HANDSHAKE]


async def test_connect_resolves_the_host_from_ws_hosts():
    conn = FakeConnection()
    t, seen = make_transport(conn, environment=kx.ENV_DEMO)
    await t.connect()
    assert seen["uri"] == kx.WS_HOSTS[kx.ENV_DEMO]


def test_unknown_environment_is_refused():
    with pytest.raises(kx.CapabilityError):
        make_transport(FakeConnection(), environment="staging")


async def test_connect_never_passes_max_queue_none():
    """CP0 12.2: `None` disables flow control entirely — unbounded memory and no
    backpressure."""
    conn = FakeConnection()
    t, seen = make_transport(conn)
    await t.connect()
    assert seen["kwargs"]["max_queue"] == wt.DEFAULT_MAX_QUEUE
    assert seen["kwargs"]["max_queue"] is not None


@pytest.mark.parametrize("bad", [None, 0, -3, "16", 16.0])
def test_max_queue_rejects_flow_control_disabling_values(bad):
    with pytest.raises(kx.CapabilityError):
        make_transport(FakeConnection(), max_queue=bad)


def test_transport_does_not_use_the_auto_reconnect_iterator_form():
    """CP0 12.6 row 5: `async for ws in connect(...)` carries its own unbounded
    `while True` retry loop, which would silently defeat `max_reconnects`.

    Checked on the AST, not on the text, so the prose that explains the trap does
    not itself trip the test.
    """
    import ast

    tree = ast.parse(inspect.getsource(wt))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFor)], (
        "the module contains an `async for`; if it ever iterates connect() "
        "directly, reconnects become unbounded")


async def test_an_unsigned_signer_is_refused_rather_than_tolerated():
    """The credential-free seam exists for fixtures and replay; using it against
    a live host would open an unauthenticated session while reporting success."""
    t, _ = make_transport(FakeConnection(), signer=kx.UnsignedTransportSigner())
    with pytest.raises(kx.CapabilityError) as exc:
        await t.connect()
    assert "unauthenticated" in str(exc.value) or "cannot sign" in str(exc.value)


def test_a_missing_signer_is_refused():
    with pytest.raises(kx.CapabilityError):
        wt.KalshiWebsocketTransport(environment=kx.ENV_DEMO, signer=None)


async def test_handshake_failure_reports_the_exception_type_only():
    """SAFETY_BOUNDARIES:259 — the URI and the signed headers must never reach a
    log line through an exception message."""

    class Boom(RuntimeError):
        def __str__(self):
            return "wss://secret-host/?KALSHI-ACCESS-SIGNATURE=abcdef"

    async def _connector(uri, **kwargs):
        raise Boom()

    t = wt.KalshiWebsocketTransport(environment=kx.ENV_DEMO,
                                    signer=FakeSigner(), _connector=_connector)
    with pytest.raises(wt.TransportError) as exc:
        await t.connect()
    assert "Boom" in str(exc.value)
    assert "secret-host" not in str(exc.value)
    assert "SIGNATURE" not in str(exc.value)


async def test_connect_twice_is_refused():
    t, _ = make_transport(FakeConnection())
    await t.connect()
    with pytest.raises(wt.TransportError):
        await t.connect()


async def test_send_before_connect_is_a_transport_error_not_a_silent_noop():
    t, _ = make_transport(FakeConnection())
    with pytest.raises(wt.TransportError):
        await t.send(SUB)


async def test_iterating_before_connect_is_refused():
    t, _ = make_transport(FakeConnection())
    with pytest.raises(wt.TransportError):
        async for _ in t:
            pass


# --- keepalive and timeouts --------------------------------------------------------------


async def test_keepalive_settings_are_passed_and_visible():
    conn = FakeConnection()
    t, seen = make_transport(conn)
    await t.connect()
    assert seen["kwargs"]["ping_interval"] == wt.DEFAULT_PING_INTERVAL_S
    assert seen["kwargs"]["ping_timeout"] == wt.DEFAULT_PING_TIMEOUT_S
    assert t.keepalive_enabled is True


def test_disarming_the_keepalive_fuse_is_visible():
    t, _ = make_transport(FakeConnection(), ping_interval_s=None)
    assert t.keepalive_enabled is False


async def test_read_timeout_is_bounded_and_named():
    """Silence is not evidence that the market is quiet."""

    class Hanging(FakeConnection):
        async def recv(self, decode=None):
            await asyncio.sleep(3600)

    t, _ = make_transport(Hanging(), read_timeout_s=0.01)
    await t.connect()
    with pytest.raises(wt.TransportReadTimeout):
        async for _ in t:
            pass
    assert t.counters.read_timeouts == 1


@pytest.mark.parametrize("bad", [0, -1, "5"])
def test_read_timeout_must_be_positive_or_explicitly_none(bad):
    with pytest.raises(kx.CapabilityError):
        make_transport(FakeConnection(), read_timeout_s=bad)


# --- counters and the defensive queue-depth read -------------------------------------------


async def test_wire_bytes_are_counted_at_the_transport_boundary():
    payload = b('{"type":"ok"}')
    conn = FakeConnection([payload, b"garbage"])
    t, _ = make_transport(conn)
    await t.connect()
    [f async for f in t]
    assert t.counters.bytes_received == len(payload) + len(b"garbage")


async def test_no_transport_dropped_counter_is_invented():
    """CP0 12.4: the library has no drop path and no drop counter, so a zero here
    would be a fabricated measurement rather than an observation."""
    snap = wt.TransportCounters().snapshot()
    assert "transport_dropped" not in snap
    assert {"frames_malformed", "frames_oversize"} <= set(snap)


async def test_reader_stall_is_measured_without_library_support():
    conn = FakeConnection([b('{"a":1}'), b('{"a":2}')])
    t, _ = make_transport(conn)
    await t.connect()
    [f async for f in t]
    assert t.counters.reader_stall_ms_max >= 0


def test_queue_depth_is_none_when_disconnected():
    t, _ = make_transport(FakeConnection())
    assert t.queue_depth() is None
    assert t.backpressure_active() is None


async def test_queue_depth_reads_the_documented_chain_when_it_is_there():
    from websockets.asyncio.messages import Assembler

    assembler = Assembler(16)
    conn = FakeConnection(recv_messages=assembler)
    t, _ = make_transport(conn)
    await t.connect()
    assert t.queue_depth() == 0
    assert t.backpressure_active() is False


async def test_queue_depth_reports_none_not_zero_when_the_chain_breaks():
    """CP0 12.3: `len(conn.recv_messages.frames)` travels an UNDOCUMENTED chain
    through a `SimpleQueue` the library calls internal. A library upgrade that
    moves it must never surface as "no backlog", and must never let an
    `AttributeError` reach the reader loop."""

    class Moved:
        pass

    conn = FakeConnection(recv_messages=Moved())
    t, _ = make_transport(conn)
    await t.connect()
    assert t.queue_depth() is None
    assert t.backpressure_active() is None

    plain = FakeConnection()          # no recv_messages attribute at all
    t2, _ = make_transport(plain)
    await t2.connect()
    assert t2.queue_depth() is None


async def test_the_undocumented_chain_still_exists_on_the_installed_websockets():
    """Fails loudly on the upgrade that moves it, instead of silently reporting
    no backlog forever."""
    from websockets.asyncio.messages import Assembler

    assembler = Assembler(16)
    assert hasattr(assembler, "frames")
    assert len(assembler.frames) == 0
    assert assembler.paused is False


# --- the interface is not broken -----------------------------------------------------------


def test_the_transport_satisfies_the_declared_interface():
    for name in ("connect", "send", "__aiter__"):
        assert hasattr(wt.KalshiWebsocketTransport, name)
    assert issubclass(wt.KalshiWebsocketTransport, kx.Transport)


async def test_fixture_transport_still_satisfies_the_same_interface():
    """CP1 must not break the credential-free path the replay lane depends on."""
    frames = [{"type": "ok"}, {"type": "ticker"}]
    ft = kx.FixtureTransport(frames)
    await ft.connect()
    assert ft.connected is True
    await ft.send(SUB)
    assert ft.sent == [SUB]
    assert [f async for f in ft] == frames
    assert list(ft.iter_frames()) == frames


async def test_both_transports_are_drivable_by_one_loop():
    """The collector must not need to know which transport it holds."""

    async def drive(transport):
        await transport.connect()
        return [f async for f in transport]

    conn = FakeConnection([b('{"type":"ok"}')])
    live, _ = make_transport(conn)
    assert await drive(live) == [{"type": "ok"}]
    assert await drive(kx.FixtureTransport([{"type": "ok"}])) == [{"type": "ok"}]


async def test_close_is_idempotent_and_releases_the_connection():
    conn = FakeConnection()
    t, _ = make_transport(conn)
    await t.connect()
    await t.close()
    assert conn.closed is True
    assert t.connected is False
    await t.close()
