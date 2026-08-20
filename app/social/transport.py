"""SOCIAL-TAPE-001 — the stream transport interface.

The collector is written against this interface and never against an HTTP
client. Two consequences, both deliberate:

1. **No test can open a socket**, because the only transports that exist in
   this repository are a fixture replayer and a null transport that refuses.
   There is no live HTTP implementation here at all. That is a stronger
   guarantee than "tests use a mock": you cannot accidentally point the
   collector at the internet, because there is nothing to point it at.

2. The seam is typed and narrow (doctrine 6): no ``*args``, no ``**kwargs``,
   no reflection, no adapter dispatch. A fault boundary on a typed direct call
   was measured free in this repo; the cost was always the varargs packing.

Fixture provenance (doctrine 9)
-------------------------------
A fixture is an executable claim about external reality. :class:`FixtureFrame`
therefore requires provenance — ``capture_id``, ``captured_at``, ``platform``,
``schema_version``, ``sanitized_frame_hash`` — and :class:`FixtureTransport`
refuses frames without it. The fixtures in this milestone are marked
``SYNTHETIC``, honestly, because no wire capture exists yet: nothing has been
connected. A later milestone that captures real frames replaces the basis
field, and the tests that certify behaviour will then be certifying venue
truth rather than our own imagination.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Iterable, Protocol, Sequence, runtime_checkable

__all__ = [
    "FrameKind",
    "FixtureBasis",
    "FrameProvenance",
    "StreamFrame",
    "TransportRule",
    "RuleSyncResult",
    "TransportError",
    "LiveTransportUnavailableError",
    "FixtureProvenanceError",
    "SocialStreamTransport",
    "FixtureTransport",
    "NullTransport",
]


class TransportError(RuntimeError):
    """Base class for transport faults."""


class LiveTransportUnavailableError(TransportError):
    """No live transport exists in this milestone, by design."""


class FixtureProvenanceError(TransportError):
    """A fixture frame could not say where it came from."""


class FrameKind(str, Enum):
    """What the stream handed us.

    ``KEEPALIVE`` is a first-class frame, not noise: a stream that is quiet
    because nothing is happening and a stream that is quiet because it is dead
    are different states, and the keepalive is the only thing that tells them
    apart.
    """

    DATA = "DATA"
    KEEPALIVE = "KEEPALIVE"
    #: The transport observed a disconnect and re-established the stream. The
    #: collector MUST treat the subscription generation as changed.
    RECONNECT = "RECONNECT"
    #: Frames the platform is re-sending to cover a gap. Their delivery timing
    #: is NOT live timing and must never be pooled with live frames.
    BACKFILL = "BACKFILL"
    #: The platform reported an error in-band.
    ERROR = "ERROR"


class FixtureBasis(str, Enum):
    """What a fixture's claim about external reality rests on."""

    #: Captured from the live wire. None exist yet in this milestone.
    WIRE_CAPTURE = "WIRE_CAPTURE"
    #: Transcribed from published protocol documentation.
    PROTOCOL_DOC = "PROTOCOL_DOC"
    #: Invented by us to exercise a code path. Certifies our behaviour, never
    #: the platform's.
    SYNTHETIC = "SYNTHETIC"


@dataclass(frozen=True)
class FrameProvenance:
    """Where a fixture frame came from. Required — never inferred."""

    capture_id: str
    captured_at: str
    platform: str
    schema_version: str
    basis: FixtureBasis
    sanitized_frame_hash: str

    def __post_init__(self) -> None:
        for name in ("capture_id", "captured_at", "platform", "schema_version"):
            if not str(getattr(self, name)).strip():
                raise FixtureProvenanceError(
                    f"fixture provenance requires {name}; a fixture that "
                    "cannot identify its basis is synthetic test data, not "
                    "venue truth"
                )
        if len(self.sanitized_frame_hash) != 64:
            raise FixtureProvenanceError(
                "sanitized_frame_hash must be a sha256 hex digest; it is the "
                "drift detector against the live platform"
            )

    @classmethod
    def synthetic(
        cls,
        capture_id: str,
        raw: bytes,
        *,
        platform: str = "X",
        schema_version: str = "social-tape-001.fixture.v1",
        captured_at: str = "1970-01-01T00:00:00.000000Z",
    ) -> "FrameProvenance":
        return cls(
            capture_id=capture_id,
            captured_at=captured_at,
            platform=platform,
            schema_version=schema_version,
            basis=FixtureBasis.SYNTHETIC,
            sanitized_frame_hash=hashlib.sha256(raw).hexdigest(),
        )


@dataclass(frozen=True)
class StreamFrame:
    """One frame off the stream. ``raw`` is the exact bytes, undecoded."""

    kind: FrameKind
    raw: bytes = b""
    #: Transport-assigned, monotonically increasing within one connection.
    #: Resets across reconnects, which is why ``generation`` exists.
    delivery_sequence: int = 0
    #: Increments on every reconnect. Forcing a reconnect MUST move this — the
    #: positive control from doctrine 7.
    subscription_generation: int = 0
    #: Which configured rules the platform says matched this frame.
    matched_rule_ids: tuple[str, ...] = ()
    provenance: FrameProvenance | None = None


@dataclass(frozen=True)
class TransportRule:
    """A rule as the platform holds it, which may differ from ours."""

    remote_id: str
    tag: str
    value: str


@dataclass(frozen=True)
class RuleSyncResult:
    """Outcome of reconciling our universe against the platform's rule set."""

    added: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    #: Rules the platform holds that our universe does not name. NOT deleted
    #: automatically: a foreign rule may belong to another tenant of the same
    #: credential, and silently deleting it would be a cross-tenant mutation.
    foreign: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.deleted)


@runtime_checkable
class SocialStreamTransport(Protocol):
    """The complete surface the collector is allowed to touch."""

    async def list_rules(self) -> Sequence[TransportRule]:
        """Return the rules the platform currently holds."""

    async def apply_rules(
        self, add: Sequence[TransportRule], delete: Sequence[str]
    ) -> RuleSyncResult:
        """Reconcile the platform's rule set toward ours."""

    def frames(self) -> AsyncIterator[StreamFrame]:
        """Yield frames until the stream ends or the caller stops consuming."""

    async def aclose(self) -> None:
        """Release the transport."""


class FixtureTransport:
    """Replays a recorded/synthetic frame sequence. Opens nothing.

    Every frame must carry :class:`FrameProvenance`, so a test cannot quietly
    certify behaviour against a frame nobody can trace.
    """

    def __init__(
        self,
        frames: Iterable[StreamFrame],
        *,
        remote_rules: Sequence[TransportRule] = (),
    ) -> None:
        materialised = list(frames)
        for index, frame in enumerate(materialised):
            if frame.provenance is None:
                raise FixtureProvenanceError(
                    f"fixture frame {index} has no provenance; see doctrine 9"
                )
        self._frames = materialised
        self._remote_rules = list(remote_rules)
        self.applied: list[RuleSyncResult] = []
        self.closed = False
        self.frames_yielded = 0

    async def list_rules(self) -> Sequence[TransportRule]:
        return tuple(self._remote_rules)

    async def apply_rules(
        self, add: Sequence[TransportRule], delete: Sequence[str]
    ) -> RuleSyncResult:
        existing = {r.remote_id: r for r in self._remote_rules}
        for remote_id in delete:
            existing.pop(remote_id, None)
        for rule in add:
            existing[rule.remote_id] = rule
        self._remote_rules = list(existing.values())
        result = RuleSyncResult(
            added=tuple(r.tag for r in add),
            deleted=tuple(delete),
        )
        self.applied.append(result)
        return result

    async def frames(self) -> AsyncIterator[StreamFrame]:
        for frame in self._frames:
            self.frames_yielded += 1
            yield frame

    async def aclose(self) -> None:
        self.closed = True


class NullTransport:
    """The default. Refuses everything.

    A collector constructed without an explicit transport gets this one, so the
    failure mode of a misconfiguration is a loud refusal rather than a silent
    live connection.
    """

    async def list_rules(self) -> Sequence[TransportRule]:
        raise LiveTransportUnavailableError(
            "no transport is configured; SOCIAL-TAPE-001 ships no live "
            "transport and activates nothing"
        )

    async def apply_rules(
        self, add: Sequence[TransportRule], delete: Sequence[str]
    ) -> RuleSyncResult:
        raise LiveTransportUnavailableError(
            "no transport is configured; rule management is inert"
        )

    async def frames(self) -> AsyncIterator[StreamFrame]:
        raise LiveTransportUnavailableError(
            "no transport is configured; there is no stream to consume"
        )
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        return None
