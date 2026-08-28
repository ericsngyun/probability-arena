"""SOCIAL-X-LIVE-TRANSPORT-001 — what the transport OBSERVES, not what it means.

The transport reports wire facts. It does not decide what they imply about
observation coverage, and it cannot: it has no budget, no cap, no notion of
whether silence is evidence. Those are session policy, and session policy
lives in `observer_session.py`.

This module is the vocabulary between them. Keeping it separate is what makes
the separation enforceable rather than aspirational -- `x_transport` imports
this and never imports `x_stream_state`, and a test asserts it.

Why events rather than the transport driving the machine directly: a transport
that mutates stream state owns two jobs, and the second one is invisible.
Every future edit to reconnect handling would silently be an edit to
observation accounting, and the accounting would be untestable without a
socket. As events, the whole lifecycle replays from a list.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "EventKind",
    "TransportEvent",
    "AuthenticationAccepted",
    "RulesReconciled",
    "StreamOpened",
    "FrameObserved",
    "PlatformErrorObserved",
    "RateLimited",
    "HttpErrorObserved",
    "ConnectionEnded",
    "RetryBudgetExhausted",
]


class EventKind(str, Enum):
    AUTHENTICATION_ACCEPTED = "AUTHENTICATION_ACCEPTED"
    RULES_RECONCILED = "RULES_RECONCILED"
    STREAM_OPENED = "STREAM_OPENED"
    FRAME_OBSERVED = "FRAME_OBSERVED"
    PLATFORM_ERROR_OBSERVED = "PLATFORM_ERROR_OBSERVED"
    RATE_LIMITED = "RATE_LIMITED"
    HTTP_ERROR_OBSERVED = "HTTP_ERROR_OBSERVED"
    CONNECTION_ENDED = "CONNECTION_ENDED"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class TransportEvent:
    """Base. Frozen, so an event cannot be edited after it is reported."""

    kind: EventKind


@dataclass(frozen=True)
class AuthenticationAccepted(TransportEvent):
    kind: EventKind = EventKind.AUTHENTICATION_ACCEPTED


@dataclass(frozen=True)
class RulesReconciled(TransportEvent):
    added: int = 0
    deleted: int = 0
    unchanged: int = 0
    #: Rules the platform holds that our universe does not name. Reported so
    #: the session can record them; never deleted.
    foreign: int = 0
    kind: EventKind = EventKind.RULES_RECONCILED


@dataclass(frozen=True)
class StreamOpened(TransportEvent):
    subscription_generation: int = 0
    kind: EventKind = EventKind.STREAM_OPENED


@dataclass(frozen=True)
class FrameObserved(TransportEvent):
    """One frame off the wire.

    `is_post` is the transport's report of frame TYPE, not of value. A
    keepalive is `is_post=False` and still proves liveness; conflating the two
    is what makes a wedged stream look like a quiet market.
    """

    is_post: bool = False
    delivery_sequence: int = 0
    subscription_generation: int = 0
    kind: EventKind = EventKind.FRAME_OBSERVED


@dataclass(frozen=True)
class PlatformErrorObserved(TransportEvent):
    kind: EventKind = EventKind.PLATFORM_ERROR_OBSERVED


@dataclass(frozen=True)
class RateLimited(TransportEvent):
    kind: EventKind = EventKind.RATE_LIMITED


@dataclass(frozen=True)
class HttpErrorObserved(TransportEvent):
    status: int = 0
    kind: EventKind = EventKind.HTTP_ERROR_OBSERVED


@dataclass(frozen=True)
class ConnectionEnded(TransportEvent):
    """The connection is over.

    `clean` distinguishes an orderly end-of-stream from a drop. The session
    treats them identically for coverage -- both stop observation -- but the
    distinction is recorded because it is the only signal that separates a
    platform closing us out from a network fault.
    """

    clean: bool = True
    kind: EventKind = EventKind.CONNECTION_ENDED


@dataclass(frozen=True)
class RetryBudgetExhausted(TransportEvent):
    attempts: int = 0
    kind: EventKind = EventKind.RETRY_BUDGET_EXHAUSTED
