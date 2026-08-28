"""SOCIAL-X-LIVE-TRANSPORT-001 — the transport's runtime state machine.

This module exists to answer one question honestly:

    N_received == 0. What does that mean?

Without it, zero observed Posts is five different facts wearing the same
number — a quiet market, a stream that never connected, a stream that wedged
open and delivered nothing, a rate limit, and our own budget stop. The
observer qualification cannot separate legitimate funnel loss from system
failure unless the transport says which one happened, and for how long.

So the machine records **dwell time per state**, not merely the current state.
Coverage is `t(RECEIVING) / t(total)`: silence is evidence only for the wall
time we were actually in a position to hear something.

## The one frozen fact

`OBSERVATION_BEARING == {RECEIVING}`.

`DEGRADED` is deliberately excluded. A rate-limited or erroring stream is
dropping Posts we cannot count, so time spent there is time we cannot claim as
observation — and cannot claim as silence either. Widening this set is the
single change that would let a broken run pass as a quiet one, which is why it
is a named constant with a mutation test rather than a condition inline.

## What this module does not do

No returns, markouts, rankings or trading decisions. It counts frames and
seconds. It also holds no credential and opens no socket: it is fed
transitions by the transport, so it can be tested exhaustively without a
network and read by the accounting layer without touching one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

__all__ = [
    "StreamState",
    "TransitionReason",
    "IllegalTransitionError",
    "Transition",
    "StreamStateMachine",
    "CoverageReport",
    "OBSERVATION_BEARING",
    "LEGAL_TRANSITIONS",
    "KEEPALIVE_STALL_S",
]


class StreamState(str, Enum):
    """Where the transport is, in terms the accounting layer can use."""

    #: No credential resolved. Nothing has been attempted.
    UNCONFIGURED = "UNCONFIGURED"
    #: A bearer token was accepted by the platform. Says nothing about the
    #: stream: authenticated-but-unavailable is NOT a quiet market.
    AUTHENTICATED = "AUTHENTICATED"
    #: The rule set the platform holds matches the frozen universe.
    RULES_RECONCILED = "RULES_RECONCILED"
    #: The socket is open, but nothing has arrived through it yet. Not yet
    #: evidence that the stream works.
    STREAM_CONNECTED = "STREAM_CONNECTED"
    #: At least one frame — a keepalive counts, because it proves liveness —
    #: has arrived and none of the degradation conditions hold. THE ONLY
    #: STATE IN WHICH SILENCE IS EVIDENCE.
    RECEIVING = "RECEIVING"
    #: Connected, but something is wrong in a way that loses Posts we cannot
    #: enumerate: rate limiting, an in-band platform error, or keepalives that
    #: stopped arriving. Not observation, and not silence.
    DEGRADED = "DEGRADED"
    #: Between connections. Contributes no observation coverage at all.
    RECONNECTING = "RECONNECTING"
    #: We stopped ourselves at the frozen Post-read cap. OUR decision, never
    #: to be reported as a provider failure.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: Terminal.
    STOPPED = "STOPPED"


#: The only state whose wall time counts as observation. See the module note.
OBSERVATION_BEARING = frozenset({StreamState.RECEIVING})


class TransitionReason(str, Enum):
    """Why the state changed. Typed so the ledger cannot hold free prose."""

    AUTHENTICATED_OK = "AUTHENTICATED_OK"
    RULES_APPLIED = "RULES_APPLIED"
    CONNECT_OK = "CONNECT_OK"
    FIRST_FRAME = "FIRST_FRAME"
    RECOVERED = "RECOVERED"
    # -- degradation, each distinguishable in the report ------------------
    KEEPALIVE_STALLED = "KEEPALIVE_STALLED"
    PLATFORM_ERROR_FRAME = "PLATFORM_ERROR_FRAME"
    RATE_LIMITED = "RATE_LIMITED"
    # -- loss of connection ------------------------------------------------
    HTTP_ERROR = "HTTP_ERROR"
    CONNECTION_DROPPED = "CONNECTION_DROPPED"
    CLEAN_EOF = "CLEAN_EOF"
    # -- our own stops -----------------------------------------------------
    POST_BUDGET_REACHED = "POST_BUDGET_REACHED"
    TIME_CAP_REACHED = "TIME_CAP_REACHED"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
    OPERATOR_STOP = "OPERATOR_STOP"
    # -- refusals before observation ---------------------------------------
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    RULE_SYNC_FAILED = "RULE_SYNC_FAILED"


#: Declared as a table rather than as conditions scattered through the
#: transport. An illegal transition raises: a state history that cannot have
#: happened is worse than a crash, because it is averaged into a report.
LEGAL_TRANSITIONS: dict[StreamState, frozenset[StreamState]] = {
    StreamState.UNCONFIGURED: frozenset({
        StreamState.AUTHENTICATED, StreamState.STOPPED}),
    StreamState.AUTHENTICATED: frozenset({
        StreamState.RULES_RECONCILED, StreamState.STOPPED}),
    StreamState.RULES_RECONCILED: frozenset({
        StreamState.STREAM_CONNECTED, StreamState.RECONNECTING,
        StreamState.STOPPED}),
    StreamState.STREAM_CONNECTED: frozenset({
        StreamState.RECEIVING, StreamState.DEGRADED,
        StreamState.RECONNECTING, StreamState.STOPPED}),
    StreamState.RECEIVING: frozenset({
        StreamState.DEGRADED, StreamState.RECONNECTING,
        StreamState.BUDGET_EXHAUSTED, StreamState.STOPPED}),
    StreamState.DEGRADED: frozenset({
        StreamState.RECEIVING, StreamState.RECONNECTING,
        StreamState.BUDGET_EXHAUSTED, StreamState.STOPPED}),
    StreamState.RECONNECTING: frozenset({
        StreamState.STREAM_CONNECTED, StreamState.BUDGET_EXHAUSTED,
        StreamState.STOPPED}),
    StreamState.BUDGET_EXHAUSTED: frozenset({StreamState.STOPPED}),
    StreamState.STOPPED: frozenset(),
}

#: Seconds of total silence — not one byte, keepalive included — after which
#: the connection is treated as wedged rather than quiet.
#:
#: X's documented filtered-stream keepalive cadence is roughly every 20s. This
#: is TRANSCRIBED FROM PROTOCOL DOCUMENTATION, not measured: the authenticated
#: smoke is the first opportunity to observe the real cadence, and this
#: constant is expected to be corrected against it. It is deliberately loose,
#: because a false KEEPALIVE_STALLED costs observation coverage while a late
#: one costs only detection latency.
KEEPALIVE_STALL_S = 45.0


class IllegalTransitionError(RuntimeError):
    """A transition the declared table does not permit."""


@dataclass(frozen=True)
class Transition:
    at_s: float
    from_state: StreamState
    to_state: StreamState
    reason: TransitionReason


@dataclass(frozen=True)
class CoverageReport:
    """What a run can honestly claim.

    `observation_coverage` is the fraction of wall time in which silence
    would have been evidence. It is REPORTED, never judged: whether a given
    coverage is sufficient is a qualification decision, not a transport one.
    """

    total_s: float
    dwell_s: dict[StreamState, float]
    transitions: tuple[Transition, ...]
    posts_received: int
    terminal_state: StreamState
    terminal_reason: TransitionReason | None

    @property
    def observed_s(self) -> float:
        return sum(self.dwell_s.get(s, 0.0) for s in OBSERVATION_BEARING)

    @property
    def observation_coverage(self) -> float:
        return self.observed_s / self.total_s if self.total_s > 0 else 0.0

    @property
    def system_failure_s(self) -> float:
        """Wall time lost to us, not to a quiet market."""
        return sum(self.dwell_s.get(s, 0.0) for s in (
            StreamState.DEGRADED, StreamState.RECONNECTING,
            StreamState.STREAM_CONNECTED))

    @property
    def zero_posts_is_interpretable(self) -> bool:
        """Whether `posts_received == 0` may be read as silence AT ALL.

        Not a threshold — a precondition. If no observation-bearing time
        elapsed, zero is not a small number; it is an absent measurement.
        """
        return self.observed_s > 0.0

    def why_zero(self) -> str:
        """The one-line answer to 'N_received == 0, what happened?'"""
        if self.posts_received > 0:
            return f"not zero: {self.posts_received} posts received"
        if not self.zero_posts_is_interpretable:
            return ("NOT SILENCE: no observation-bearing time elapsed "
                    f"(terminal {self.terminal_state.value}, "
                    f"{self.terminal_reason.value if self.terminal_reason else 'n/a'})")
        if self.terminal_state is StreamState.BUDGET_EXHAUSTED:
            return "our own budget stop, not a provider failure"
        return (f"silence over {self.observed_s:.1f}s of observation "
                f"({self.observation_coverage:.1%} coverage)")


class StreamStateMachine:
    """Records the transport's state history and the time spent in each.

    The clock is injected. A machine that reads the wall clock directly
    cannot be tested for the thing it exists to measure.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state = StreamState.UNCONFIGURED
        self._entered_at = clock()
        self._started_at = self._entered_at
        self._dwell: dict[StreamState, float] = {}
        self._transitions: list[Transition] = []
        self._posts = 0
        self._last_frame_at = self._entered_at
        self._terminal_reason: TransitionReason | None = None

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def posts_received(self) -> int:
        return self._posts

    def transition(self, to: StreamState,
                   reason: TransitionReason) -> Transition:
        if to not in LEGAL_TRANSITIONS[self._state]:
            raise IllegalTransitionError(
                f"{self._state.value} -> {to.value} is not a legal "
                f"transition (reason {reason.value})")
        now = self._clock()
        self._dwell[self._state] = (
            self._dwell.get(self._state, 0.0) + (now - self._entered_at))
        record = Transition(at_s=now - self._started_at,
                            from_state=self._state, to_state=to,
                            reason=reason)
        self._transitions.append(record)
        self._state, self._entered_at = to, now
        self._terminal_reason = reason
        return record

    def note_frame(self, *, is_post: bool) -> None:
        """A frame arrived. Keepalives count for liveness, not for N."""
        self._last_frame_at = self._clock()
        if is_post:
            self._posts += 1

    def keepalive_stalled(self, *,
                          stall_s: float = KEEPALIVE_STALL_S) -> bool:
        """True when the connection has gone wholly silent for too long.

        This is the check that separates 'wedged open' from 'quiet'. Without
        it those two are the same observation.
        """
        return (self._clock() - self._last_frame_at) > stall_s

    def report(self) -> CoverageReport:
        now = self._clock()
        dwell = dict(self._dwell)
        dwell[self._state] = (
            dwell.get(self._state, 0.0) + (now - self._entered_at))
        return CoverageReport(
            total_s=now - self._started_at,
            dwell_s=dwell,
            transitions=tuple(self._transitions),
            posts_received=self._posts,
            terminal_state=self._state,
            terminal_reason=self._terminal_reason,
        )
