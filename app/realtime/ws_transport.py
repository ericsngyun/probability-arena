"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP1 — the only socket in the package.

`app/realtime/kalshi.py` closes the set of things this repository can
CONSTRUCT: two signable routes, four subscribable channels, one HTTP method,
four frame builders. None of that constrains what an open socket can WRITE, and
a real transport is the first object in the package that owns one. This module
exists to close exactly that hole, and nothing else.

How the hole is closed — by construction, not by convention:

* **The connection object is never stored on the instance.** `connect()` opens
  it into a local, then keeps only a small frozen record of closures that
  capture it. There is no attribute, public or private, that holds the
  `websockets` connection, so no collector code can reach `conn.send`.
* **The only closure that can write to the socket validates first.** It takes a
  `dict`, runs it through `assert_sendable`, and puts the BUILDER'S output on
  the wire — not the caller's object. Reaching past `send()` into the private
  attribute therefore buys an attacker nothing: the governed path is the only
  path, so the capability restriction survives introspection.
* **There is no `send_text`, `send_bytes`, `send_raw`, or `ping` on this
  class.** Not "there is one but you shouldn't call it". There is none.
* **`assert_sendable` accepts only what the four builders emit**, verified by
  rebuilding the frame with the builder and requiring exact equality. A
  hand-rolled dict that merely resembles a subscribe command is refused, which
  is what section 4 of the milestone means by "no raw-dict send path may exist".

Frame parsing is deliberately lossless-or-loud. Venue JSON is parsed with
`fixedpoint.loads_exact`, so every bare number becomes a `Decimal` — the venue
transport is the correct and only place to do that (`segment.py:585-589`: "A
Python float reaching submission is a contract violation"). A frame that is not
JSON, or is JSON but not an object, is counted as `frames_malformed` and is
never yielded, so it can never reach `make_envelope` and become a nonsense
archive record.

Library facts this module is built on were established by CP0 against the
INSTALLED `websockets` 16.0 and are recorded in section 12 of the milestone
document. The four that shape the code here:

1. the client parameter is `additional_headers`, not `extra_headers` (12.6);
2. inbound overflow applies TCP backpressure and drops nothing (12.2), so
   sequence integrity is the only drop detector needed;
3. `max_queue` is a high-water mark, and `max_queue=None` disables flow control
   entirely — this module refuses that value (12.2);
4. `max_size` overflow raises `PayloadTooBig`, which FAILS THE CONNECTION rather
   than dropping one message (12.1). See `MAX_FRAME_BYTES` below.

This module opens no socket at import time and holds no key material. It
receives a signer; it never reads a credential path.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect as _websockets_connect
from websockets.exceptions import ConnectionClosed, PayloadTooBig

from app.realtime.fixedpoint import loads_exact
from app.realtime.kalshi import (
    ENVIRONMENTS,
    RECOVERY_ACTION_GET_SNAPSHOT,
    WS_HOSTS,
    AuthPurpose,
    CapabilityError,
    Transport,
    assert_channels_allowed,
    build_get_snapshot,
    build_subscribe,
    build_unsubscribe,
)

# --- the closed outbound command set --------------------------------------------
# Mirrors the four builders in `kalshi.py` and nothing else. `build_resubscribe`
# emits an unsubscribe followed by a subscribe, so it is covered by two members
# rather than needing a third.
SENDABLE_COMMANDS = ("subscribe", "unsubscribe", "update_subscription")

# --- limits ----------------------------------------------------------------------

# `max_size` — the per-message ceiling, and a DELIBERATE choice rather than a
# default inherited by accident.
#
# The library default is 1 MiB, and exceeding it does not drop a message: it
# raises `PayloadTooBig`, which fails the whole connection with close code 1009
# (`websockets/protocol.py:625-628`, `frames.py:254`). So the cost of setting
# this too tight is not a missing frame, it is a dead session and an observation
# gap — precisely the loss class section 8.3 says must never be routine.
#
# Sizing from the only structural argument available (no Kalshi snapshot has
# ever been measured by this repo): an `orderbook_snapshot` is bounded by the
# price grid. On the cent grid that is 99 levels per side; on the 4-decimal grid
# `fixedpoint.py` admits, 9,999 levels per side. At a generous ~20 bytes per
# `[price, count]` pair that is ~400 KB for a full two-sided 4-decimal book —
# already over a third of the 1 MiB default, before any multi-market bundling.
#
# 8 MiB is therefore chosen: ~20x the arithmetic worst case for one book, 8x the
# library default, and still a finite number, so a runaway or hostile frame
# cannot exhaust memory. It is a ceiling that a correct venue should never
# approach, which makes hitting it INFORMATION rather than noise — and when it is
# hit, `FrameTooLargeError` says so by name, with the configured limit and the
# remedy, instead of surfacing as an unexplained disconnect.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# A floor with a reason: below the largest plausible single book the limit would
# be a self-inflicted disconnect generator.
MIN_FRAME_BYTES = 64 * 1024

# CP0 12.1: high-water mark, low mark derived as `high // 4`, NOT a hard cap.
# The default is the library's own; there is no evidence yet for a better one and
# inventing one would be a guess dressed as a decision.
DEFAULT_MAX_QUEUE = 16

DEFAULT_OPEN_TIMEOUT_S = 10.0
# CP0 12.7: on ping timeout the CLIENT closes with 1011 "keepalive ping timeout".
# Under the overload ladder that fuse fires before the venue reacts, which is a
# property worth keeping: the failure is local, bounded by a knob we own, and
# labelled. `_classify_close` names it so it is never mistaken for a venue close.
DEFAULT_PING_INTERVAL_S = 20.0
DEFAULT_PING_TIMEOUT_S = 20.0
# Silence is not evidence of health. A read that never completes is indistinguishable
# from a healthy quiet market unless something bounds it.
DEFAULT_READ_TIMEOUT_S = 60.0


class TransportError(RuntimeError):
    """The transport cannot proceed. Never used for a refusal.

    A refusal is a `CapabilityError` and means the caller asked for something
    outside the boundary; a `TransportError` means the socket failed. Merging
    them would let a boundary violation be retried as a network blip.
    """


class TransportReadTimeout(TransportError):
    """No frame arrived within `read_timeout_s`."""


class FrameTooLargeError(TransportError):
    """A frame exceeded `max_size`; the CONNECTION is gone, not just the frame.

    Raised instead of ending iteration quietly, because a configuration fault
    that terminates the stream looks exactly like a completed session, and the
    two must never be confused in a tape.
    """


# --- close classification --------------------------------------------------------


@dataclass(frozen=True)
class CloseRecord:
    """Why the stream ended, in terms an operator can act on.

    CP0 12.5: read the code from the `ConnectionClosed` exception's
    `rcvd`/`sent`, not from the discouraged `close_code` property. The
    `rcvd`-vs-`sent` split is the whole point — it separates "the venue dropped
    us" from "our own keepalive fuse fired", which section 8.2 rung 3a/3b needs
    and which a single close code cannot express.
    """
    cause: str            # oversize_frame | local_keepalive_timeout
                          # | remote_close | local_close | unknown
    initiator: str        # local | remote | unknown
    code: int | None
    reason: str | None
    detail: str
    at: str

    def to_dict(self) -> dict:
        return asdict(self)


CLOSE_OVERSIZE = "oversize_frame"
CLOSE_KEEPALIVE = "local_keepalive_timeout"
CLOSE_REMOTE = "remote_close"
CLOSE_LOCAL = "local_close"
CLOSE_UNKNOWN = "unknown"

_CLOSE_MESSAGE_TOO_BIG = 1009
_CLOSE_INTERNAL_ERROR = 1011
_KEEPALIVE_REASON = "keepalive ping timeout"


def _classify_close(exc: BaseException, *, max_size: int) -> CloseRecord:
    rcvd = getattr(exc, "rcvd", None)
    sent = getattr(exc, "sent", None)
    rcvd_then_sent = getattr(exc, "rcvd_then_sent", None)

    remote_first = rcvd is not None and (sent is None or rcvd_then_sent is True)
    if remote_first:
        initiator, code, reason = "remote", getattr(rcvd, "code", None), getattr(rcvd, "reason", None)
    elif sent is not None:
        initiator, code, reason = "local", getattr(sent, "code", None), getattr(sent, "reason", None)
    elif rcvd is not None:
        initiator, code, reason = "remote", getattr(rcvd, "code", None), getattr(rcvd, "reason", None)
    else:
        initiator, code, reason = "unknown", None, None

    cause = CLOSE_UNKNOWN
    detail = "connection closed"
    sent_code = getattr(sent, "code", None)
    sent_reason = getattr(sent, "reason", None) or ""

    if isinstance(getattr(exc, "__cause__", None), PayloadTooBig) or (
            sent_code == _CLOSE_MESSAGE_TOO_BIG):
        cause = CLOSE_OVERSIZE
        detail = (
            f"a venue frame exceeded max_size={max_size} bytes, so the library "
            "failed the connection (close 1009) rather than dropping the "
            "message. Remedy: subscribe to fewer markets, or raise max_size "
            "deliberately — do NOT set it to None, which removes the bound "
            "entirely.")
    elif sent_code == _CLOSE_INTERNAL_ERROR and _KEEPALIVE_REASON in sent_reason:
        cause = CLOSE_KEEPALIVE
        detail = (
            "OUR OWN client closed the connection: the keepalive pong did not "
            "arrive within ping_timeout. Under sustained overload the reader "
            "stops processing pongs, so this is the local overload fuse firing "
            "(overload ladder rung 3a), not a venue action.")
    elif initiator == "remote":
        cause = CLOSE_REMOTE
        detail = "the venue closed the connection (overload ladder rung 3b)."
    elif initiator == "local":
        cause = CLOSE_LOCAL
        detail = "this client closed the connection."

    return CloseRecord(cause=cause, initiator=initiator, code=code,
                       reason=reason, detail=detail,
                       at=datetime.now(timezone.utc).isoformat())


# --- outbound governance ---------------------------------------------------------


def _refuse(message: str) -> CapabilityError:
    return CapabilityError(f"outbound frame refused: {message}")


def _assert_sid_list(value: object) -> int:
    # The builders emit exactly one sid. Accepting a list of several would admit
    # a frame no builder can produce, which is the whole thing being prevented.
    if type(value) is not list or len(value) != 1:
        raise _refuse("`sids` must be a list holding exactly one subscription id")
    sid = value[0]
    if type(sid) is not int:
        raise _refuse("`sids` must hold an int; bool and str are not sids")
    return sid


def _assert_ticker_list(value: object) -> list:
    if type(value) is not list or not value:
        raise _refuse("`market_tickers` must be a non-empty list")
    if any(type(t) is not str for t in value):
        raise _refuse("`market_tickers` must hold plain strings")
    return value


def assert_sendable(message: object) -> dict:
    """Accept only what `kalshi.py`'s builders emit. Return the BUILDER's dict.

    Two layers, and the second is the one that matters:

    1. a closed structural check — exact top-level keys, exact per-command
       parameter keys, `type(x) is T` rather than `isinstance` so a dict or list
       subclass cannot lie about its contents (the reasoning `verify_scopes`
       already applies at `kalshi.py:202-214`);
    2. a RECONSTRUCTION check — the frame is rebuilt by calling the very builder
       that is supposed to have produced it, and must compare equal. This is how
       "send() accepts only dicts produced by build_*" becomes enforceable
       without adding a provenance token to `kalshi.py`, which section 6.1 keeps
       unmodified.

    The value returned is the rebuilt frame, so the bytes that reach the socket
    are the builders' output rather than the caller's object.
    """
    if type(message) is not dict:
        raise _refuse(
            f"expected a plain dict built by kalshi.build_*, got "
            f"{type(message).__name__}")
    if set(message) != {"id", "cmd", "params"}:
        raise _refuse(
            f"keys {sorted(message)} are not the builder shape "
            "['cmd', 'id', 'params']")

    cmd = message["cmd"]
    if type(cmd) is not str or cmd not in SENDABLE_COMMANDS:
        raise _refuse(
            f"cmd {cmd!r} is not in the closed outbound set {SENDABLE_COMMANDS}")

    command_id = message["id"]
    if type(command_id) is not int:
        raise _refuse("`id` must be an int")

    params = message["params"]
    if type(params) is not dict:
        raise _refuse("`params` must be a plain dict")

    if cmd == "subscribe":
        if set(params) != {"channels", "use_yes_price", "market_tickers"}:
            raise _refuse(
                f"subscribe params {sorted(params)} are not the builder shape "
                "['channels', 'market_tickers', 'use_yes_price']")
        channels = params["channels"]
        if type(channels) is not list:
            raise _refuse("`channels` must be a list")
        # The allowlist runs again HERE, on the wire-bound frame, not only where
        # the frame was built. A `fill` channel dies on this line.
        assert_channels_allowed(channels)
        tickers = _assert_ticker_list(params["market_tickers"])
        if params["use_yes_price"] is not True:
            raise _refuse(
                "`use_yes_price` must be exactly True; the server default is "
                "subject to migration and relying on it would silently "
                "reinterpret every NO level")
        rebuilt = build_subscribe(command_id, channels, tickers)

    elif cmd == "unsubscribe":
        if set(params) != {"sids"}:
            raise _refuse(
                f"unsubscribe params {sorted(params)} are not the builder shape "
                "['sids']")
        rebuilt = build_unsubscribe(command_id, _assert_sid_list(params["sids"]))

    else:  # update_subscription
        if set(params) != {"sids", "action", "market_tickers"}:
            raise _refuse(
                f"update_subscription params {sorted(params)} are not the "
                "builder shape ['action', 'market_tickers', 'sids']")
        action = params["action"]
        if action != RECOVERY_ACTION_GET_SNAPSHOT:
            raise _refuse(
                f"action {action!r} is not the one recovery action "
                f"{RECOVERY_ACTION_GET_SNAPSHOT!r}")
        sid = _assert_sid_list(params["sids"])
        tickers = _assert_ticker_list(params["market_tickers"])
        rebuilt = build_get_snapshot(command_id, sid, tickers)

    if rebuilt != message:
        raise _refuse(
            "the frame is not identical to what kalshi.build_* produces for "
            "these arguments; only builder output may be written to the socket")
    return rebuilt


def encode_frame(frame: dict) -> str:
    """The one encoder. `allow_nan=False` and no `default=` hook mean a value the
    builders could not have produced raises here rather than becoming a
    plausible-looking frame."""
    return json.dumps(frame, separators=(",", ":"), sort_keys=True,
                      allow_nan=False)


def encode_command(message: object) -> str:
    """Govern, then encode. The only function in the package that makes wire text."""
    return encode_frame(assert_sendable(message))


# --- counters ---------------------------------------------------------------------


@dataclass
class TransportCounters:
    """Boundary counters. Integers only; the metrics lane (CP4) owns histograms.

    `frames_malformed` and `frames_oversize` are separate on purpose: the first
    is a frame we skipped, the second is a connection we lost. A single
    "bad frames" number would hide which of those happened.

    There is no `transport_dropped`. CP0 12.4 established that the installed
    library has no drop path and no drop counter, so a zero here would be a
    fabricated measurement rather than an observation.
    """
    connects: int = 0
    closes: int = 0
    frames_received: int = 0
    frames_yielded: int = 0
    frames_malformed: int = 0
    frames_oversize: int = 0
    bytes_received: int = 0
    commands_sent: int = 0
    commands_refused: int = 0
    read_timeouts: int = 0
    reader_stall_ms_max: int = 0

    def snapshot(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _ConnectionOps:
    """The entire reachable surface of an open connection.

    Every member closes over the connection object; none exposes it. The only
    writer, `send_governed`, validates its argument before writing, so this
    record cannot be used to put unvetted bytes on the socket even by a caller
    that reaches the mangled attribute holding it.
    """
    send_governed: Callable[[object], Awaitable[dict]]
    recv: Callable[[], Awaitable[Any]]
    close: Callable[[], Awaitable[None]]
    depth: Callable[[], "int | None"]
    paused: Callable[[], "bool | None"]


class KalshiWebsocketTransport(Transport):
    """The real socket, satisfying `kalshi.Transport` (`kalshi.py:366`).

    Deliberately absent: any `url` parameter (the host comes from `WS_HOSTS`),
    any raw-send method, any credential path, any reconnect loop. Reconnection
    is the collector's job and is bounded by its `max_reconnects`; this class
    connects once and reports how the stream ended. CP0 12.6 row 5 is the reason
    the `async for ws in connect(...)` form is not used anywhere here — it
    carries its own unbounded `while True` retry policy.
    """

    def __init__(self, *, environment: str, signer,
                 open_timeout_s: float = DEFAULT_OPEN_TIMEOUT_S,
                 ping_interval_s: float | None = DEFAULT_PING_INTERVAL_S,
                 ping_timeout_s: float | None = DEFAULT_PING_TIMEOUT_S,
                 max_queue: int = DEFAULT_MAX_QUEUE,
                 max_size: int = MAX_FRAME_BYTES,
                 read_timeout_s: float | None = DEFAULT_READ_TIMEOUT_S,
                 _connector=None) -> None:
        if environment not in ENVIRONMENTS:
            raise CapabilityError(
                f"unknown environment {environment!r}; the host is resolved from "
                f"WS_HOSTS and there is no url parameter")
        if signer is None:
            raise CapabilityError(
                "a signer is required; an unauthenticated session against a "
                "live host would report success while observing nothing")
        # CP0 12.2: `max_queue=None` disables flow control entirely — no drops,
        # but unbounded memory and no backpressure. Refused, not defaulted.
        if type(max_queue) is not int or max_queue < 1:
            raise CapabilityError(
                "max_queue must be a positive int; None disables flow control "
                "entirely and is never acceptable here")
        # CP0 12.1: `max_size=None` removes the per-message ceiling.
        if type(max_size) is not int or not (
                MIN_FRAME_BYTES <= max_size <= MAX_FRAME_BYTES):
            raise CapabilityError(
                f"max_size must be an int in [{MIN_FRAME_BYTES}, "
                f"{MAX_FRAME_BYTES}]; None removes the bound and a value below "
                "the floor turns an ordinary deep book into a disconnect")
        if read_timeout_s is not None and not (
                isinstance(read_timeout_s, (int, float)) and read_timeout_s > 0):
            raise CapabilityError("read_timeout_s must be a positive number or None")

        self._environment = environment
        self._signer = signer
        self._open_timeout_s = open_timeout_s
        self._ping_interval_s = ping_interval_s
        self._ping_timeout_s = ping_timeout_s
        self._max_queue = max_queue
        self._max_size = max_size
        self._read_timeout_s = read_timeout_s
        self._connector = _connector or _websockets_connect

        self.counters = TransportCounters()
        self.last_close: CloseRecord | None = None
        # The connection lives ONLY inside the closures held here. There is no
        # `self._conn`, and that is the containment property, not an oversight.
        self.__ops: _ConnectionOps | None = None

    # --- introspection: no connection object escapes -----------------------------

    @property
    def environment(self) -> str:
        return self._environment

    @property
    def connected(self) -> bool:
        return self.__ops is not None

    @property
    def keepalive_enabled(self) -> bool:
        """False means the local overload fuse (CP0 12.7) is disarmed."""
        return self._ping_interval_s is not None

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def max_queue(self) -> int:
        return self._max_queue

    def queue_depth(self) -> int | None:
        """Inbound queue depth, or None if the library moved it.

        CP0 12.3: `len(conn.recv_messages.frames)` works today but travels an
        UNDOCUMENTED chain through a `SimpleQueue` the library calls internal.
        It is read defensively and reports None — never 0 — when the chain
        breaks, because a fabricated zero would read as "no backlog".
        """
        ops = self.__ops
        return None if ops is None else ops.depth()

    def backpressure_active(self) -> bool | None:
        ops = self.__ops
        return None if ops is None else ops.paused()

    # --- connect -------------------------------------------------------------------

    def _handshake_headers(self):
        """Signed headers for the one permitted purpose.

        A signer without `headers_for` is refused rather than tolerated: that is
        exactly `UnsignedTransportSigner`, whose seam exists for fixtures and
        replay (`kalshi.py:286-292`). Silently accepting it here would open an
        unauthenticated live session while reporting success.
        """
        headers_for = getattr(self._signer, "headers_for", None)
        if not callable(headers_for):
            raise CapabilityError(
                "the installed signer cannot sign a websocket handshake; the "
                "credential-free seam must never be used against a live host")
        return headers_for(purpose=AuthPurpose.WEBSOCKET_HANDSHAKE,
                           timestamp_ms=int(time.time() * 1000))

    async def connect(self) -> None:
        if self.__ops is not None:
            raise TransportError("already connected")
        uri = WS_HOSTS[self._environment]
        headers = self._handshake_headers()
        try:
            conn = await self._connector(
                uri,
                # CP0 12.6 row 2: the v12 spelling `extra_headers` does not
                # exist on websockets 16 and would TypeError at connect, on the
                # signed handshake.
                additional_headers=headers,
                open_timeout=self._open_timeout_s,
                ping_interval=self._ping_interval_s,
                ping_timeout=self._ping_timeout_s,
                max_size=self._max_size,
                max_queue=self._max_queue,
            )
        except Exception as exc:
            # Type name only. The URI and the signed headers must not reach a
            # log line through an exception message (SAFETY_BOUNDARIES:259,
            # the TENNIS-LIVE-FEED-002 precedent).
            raise TransportError(
                f"kalshi websocket handshake failed: {type(exc).__name__}"
            ) from None

        async def _send_governed(message: object) -> dict:
            # The inescapable check. Everything reachable from this object goes
            # through here, so governance cannot be routed around.
            frame = assert_sendable(message)
            await conn.send(encode_frame(frame))
            return frame

        async def _recv():
            # `decode=False` returns bytes for text frames too, so the byte
            # length recorded is the WIRE length with no re-encode copy, and an
            # invalidly-encoded frame becomes one malformed frame instead of a
            # library-level connection failure.
            return await conn.recv(decode=False)

        async def _close() -> None:
            await conn.close()

        def _depth() -> int | None:
            try:
                return len(conn.recv_messages.frames)
            except (AttributeError, TypeError):
                return None

        def _paused() -> bool | None:
            try:
                return bool(conn.recv_messages.paused)
            except (AttributeError, TypeError):
                return None

        self.__ops = _ConnectionOps(send_governed=_send_governed, recv=_recv,
                                    close=_close, depth=_depth, paused=_paused)
        self.counters.connects += 1

    async def close(self) -> None:
        ops = self.__ops
        if ops is None:
            return
        self.__ops = None
        self.counters.closes += 1
        try:
            await ops.close()
        except Exception:      # pragma: no cover - close is best effort
            pass

    # --- send ------------------------------------------------------------------------

    async def send(self, message: dict) -> None:
        """The only way out. Governed twice, on purpose.

        The check here lets a forbidden frame be refused even before a socket
        exists, so a misconfiguration fails at the earliest possible moment. The
        check inside the connection closure is the load-bearing one — it is the
        one that cannot be bypassed.
        """
        try:
            assert_sendable(message)
        except CapabilityError:
            self.counters.commands_refused += 1
            raise
        ops = self.__ops
        if ops is None:
            raise TransportError("not connected")
        try:
            await ops.send_governed(message)
        except CapabilityError:  # pragma: no cover - defence in depth
            self.counters.commands_refused += 1
            raise
        self.counters.commands_sent += 1

    # --- receive ----------------------------------------------------------------------

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        ops = self.__ops
        if ops is None:
            raise TransportError("not connected")
        last_read_ns = time.monotonic_ns()
        while True:
            try:
                if self._read_timeout_s is None:
                    raw = await ops.recv()
                else:
                    raw = await asyncio.wait_for(ops.recv(), self._read_timeout_s)
            except asyncio.TimeoutError:
                self.counters.read_timeouts += 1
                raise TransportReadTimeout(
                    f"no frame within {self._read_timeout_s}s; silence is not "
                    "evidence that the market is quiet") from None
            except ConnectionClosed as exc:
                record = _classify_close(exc, max_size=self._max_size)
                self.last_close = record
                self.__ops = None
                self.counters.closes += 1
                if record.cause == CLOSE_OVERSIZE:
                    self.counters.frames_oversize += 1
                    raise FrameTooLargeError(record.detail) from None
                return

            now_ns = time.monotonic_ns()
            stall_ms = (now_ns - last_read_ns) // 1_000_000
            if stall_ms > self.counters.reader_stall_ms_max:
                self.counters.reader_stall_ms_max = stall_ms
            last_read_ns = now_ns

            parsed = self._parse_frame(raw)
            if parsed is None:
                continue
            self.counters.frames_yielded += 1
            yield parsed

    def _parse_frame(self, raw: object) -> dict | None:
        """Wire bytes -> venue dict, or None having counted a malformed frame.

        Returning None rather than raising is the point: one unparseable frame
        must not end a session, and it must never be handed to `make_envelope`,
        which would archive a record whose `raw` is not a venue message.
        """
        self.counters.frames_received += 1
        if type(raw) is bytes:
            payload = raw
        elif isinstance(raw, (bytes, bytearray, memoryview)):
            payload = bytes(raw)
        elif isinstance(raw, str):
            # Strict: a str that cannot be encoded (a lone surrogate) is a
            # malformed frame, not something to repair into a valid parse.
            try:
                payload = raw.encode("utf-8")
            except UnicodeEncodeError:
                self.counters.frames_malformed += 1
                return None
        else:
            # Not even a websocket payload type.
            self.counters.frames_malformed += 1
            return None
        self.counters.bytes_received += len(payload)

        # `loads_exact`, never bare `json.loads`: every venue number becomes a
        # Decimal here, which is the only place it can be done without the
        # precision loss having already happened (`segment.py:585-589`).
        try:
            value = loads_exact(payload)
        except (ValueError, TypeError, RecursionError):
            # json.JSONDecodeError, UnicodeDecodeError and FixedPointError (the
            # NaN/Infinity refusal) are all ValueError subclasses.
            self.counters.frames_malformed += 1
            return None
        if type(value) is not dict:
            # A JSON array, string, number or null is valid JSON and is still
            # not a venue message.
            self.counters.frames_malformed += 1
            return None
        return value
