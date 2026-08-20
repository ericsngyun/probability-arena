"""SOCIAL-TAPE-001 — the two clocks, kept apart by construction.

This module exists because of one failure mode that would silently destroy
every future lead-lag measurement built on this tape:

    treating "when the platform says the post was created" and
    "when our process first held the bytes" as the same quantity.

They are measured by different clocks, owned by different parties, and answer
different questions. This module makes them different *types*, so that
conflating them is a construction error rather than a plausible-looking number.

House alignment
---------------
Follows the vocabulary of `docs/milestones/KALSHI-TAPE-MEASUREMENT-CONTRACT-001.md`
§2 (provenance) and §8.5 (contaminated offsets), and reuses
`app.realtime.book.utcnow` / `monotonic_ns` and `app.realtime.canonical` rather
than inventing new time handling. It does not import anything that would let it
mutate the frozen Kalshi collector.

The three quantities
--------------------
``SourceCreatedAt``   VENUE_FACT.       Foreign clock. The platform's claim.
``OurReceivedAt``     COLLECTOR_FACT.   Our clock. LIVE_ONLY — a replay reader
                                        cannot re-derive it, and must never
                                        try.
``DeliveryOffset``    DERIVED_STATE.    ``our_received_at - source_created_at``,
                                        which is a *cross-clock difference*
                                        contaminated by the unknown offset
                                        between their clock and ours. It is
                                        evidence, not a latency.

Why ``source_created_at`` cannot support a latency claim about our pipeline
--------------------------------------------------------------------------
A pipeline latency is an interval between two events *we* observed, on *one*
clock, in *one* process. ``source_created_at`` is none of those:

  * it is stamped by a machine whose offset from ours is uncharacterised;
  * on several platforms it is truncated (whole seconds) or rounded, so its
    resolution is coarser than the interval being claimed;
  * on several platforms it is *assigned at request admission*, not at
    fan-out, so it excludes the platform's own internal queueing — the exact
    component a delivery-latency claim is trying to capture;
  * it can be back-dated, edited, or re-issued by the platform, and nothing on
    the wire distinguishes a re-issue from an original.

So the only sound internal-latency measurements are those built exclusively
from ``OurReceivedAt`` values *within a single process epoch*. That constraint
is enforced here: :func:`pipeline_interval_us` refuses anything else, and there
is no code path anywhere in this package that produces an ``OurReceivedAt``
from a ``SourceCreatedAt``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from app.realtime.book import monotonic_ns, utcnow
from app.realtime.canonical import canonical_datetime, parse_canonical_datetime

__all__ = [
    "ClockOwner",
    "SourceTimeFidelity",
    "SourceCreatedAt",
    "OurReceivedAt",
    "DeliveryOffset",
    "ProcessEpoch",
    "ReceiptClock",
    "SystemReceiptClock",
    "process_epoch",
    "capture_receipt",
    "delivery_offset",
    "pipeline_interval_us",
    "TimebaseError",
    "ClockConfusionError",
    "CrossEpochIntervalError",
]


class TimebaseError(Exception):
    """Base class for timebase violations."""


class ClockConfusionError(TimebaseError):
    """Raised when a value from one clock is offered where another is required.

    This is the error that the whole module exists to produce. It is raised
    eagerly and never downgraded to a warning: a silently substituted timestamp
    is indistinguishable from a correct one downstream.
    """


class CrossEpochIntervalError(TimebaseError):
    """Raised when a monotonic interval is requested across process epochs.

    ``time.monotonic_ns()`` is only comparable inside one process. Subtracting
    two stamps from different runs yields a number, and that number is noise
    wearing a duration's name.
    """


class ClockOwner(str, Enum):
    """Who read the clock. Never inferred, always recorded."""

    #: The remote platform. We did not observe this; we were told it.
    PLATFORM = "PLATFORM"
    #: Our own host, at the moment the bytes were in our process.
    COLLECTOR = "COLLECTOR"


class SourceTimeFidelity(str, Enum):
    """What the platform's own timestamp is actually capable of saying.

    Recorded per-platform in configuration rather than assumed, per AGENTS.md
    doctrine 8: a field name is not evidence of its semantics.
    """

    #: Resolution and meaning empirically verified against captured wire
    #: evidence for this platform. Nothing is UNVERIFIED-by-default's opposite
    #: until someone does that work.
    VERIFIED = "VERIFIED"
    #: We have the field, we have not verified what moves it. The default.
    UNVERIFIED = "UNVERIFIED"
    #: The platform did not supply a creation time at all.
    NOT_PROVIDED = "NOT_PROVIDED"


@dataclass(frozen=True)
class ProcessEpoch:
    """Identity of one collector process run.

    Monotonic stamps are only comparable within one of these. Written to every
    tape record so a replay reader can tell which intervals are computable and
    which are not.
    """

    epoch_id: str
    started_at: str  # canonical RFC3339 UTC

    def to_json(self) -> dict[str, str]:
        return {"epoch_id": self.epoch_id, "started_at": self.started_at}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "ProcessEpoch":
        return cls(
            epoch_id=str(payload["epoch_id"]),
            started_at=str(payload["started_at"]),
        )


def process_epoch() -> ProcessEpoch:
    """Mint a new process epoch. Called once per collector run."""

    return ProcessEpoch(
        epoch_id=uuid.uuid4().hex,
        started_at=canonical_datetime(utcnow()),
    )


@dataclass(frozen=True)
class SourceCreatedAt:
    """VENUE_FACT — what the platform *claims* about creation time.

    Trustworthy for: ordering posts *within one platform* at coarse
    resolution; joining back to the platform's own API; detecting back-dating
    when compared against our receipt.

    NOT trustworthy for: any interval involving our system, any sub-second
    claim, any cross-platform ordering, and any statement about how fast
    anything reached us.
    """

    #: Canonical RFC3339 UTC rendering of the platform's value, byte-preserved
    #: in `raw_value` so re-parsing is auditable.
    value: str
    #: Exactly what the platform sent, verbatim, before any parsing.
    raw_value: str
    #: Which platform field supplied it (§5.4: the field name that supplied a
    #: value is itself an observation).
    source_field: str
    fidelity: SourceTimeFidelity = SourceTimeFidelity.UNVERIFIED
    owner: ClockOwner = ClockOwner.PLATFORM

    def __post_init__(self) -> None:
        if self.owner is not ClockOwner.PLATFORM:
            raise ClockConfusionError(
                "SourceCreatedAt is by definition a PLATFORM clock reading; "
                f"got owner={self.owner!r}"
            )
        # Validate parseability now, not at read time.
        parse_canonical_datetime(self.value)

    @classmethod
    def from_platform(
        cls,
        raw_value: str,
        *,
        source_field: str,
        parsed: datetime,
        fidelity: SourceTimeFidelity = SourceTimeFidelity.UNVERIFIED,
    ) -> "SourceCreatedAt":
        if parsed.tzinfo is None:
            raise ClockConfusionError(
                "a naive platform timestamp is refused: reading it as local "
                "time would canonicalise differently on two hosts"
            )
        return cls(
            value=canonical_datetime(parsed.astimezone(timezone.utc)),
            raw_value=raw_value,
            source_field=source_field,
            fidelity=fidelity,
        )

    def to_json(self) -> dict[str, str]:
        return {
            "value": self.value,
            "raw_value": self.raw_value,
            "source_field": self.source_field,
            "fidelity": self.fidelity.value,
            "clock_owner": self.owner.value,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SourceCreatedAt":
        return cls(
            value=str(payload["value"]),
            raw_value=str(payload["raw_value"]),
            source_field=str(payload["source_field"]),
            fidelity=SourceTimeFidelity(payload["fidelity"]),
        )


@dataclass(frozen=True)
class OurReceivedAt:
    """COLLECTOR_FACT, LIVE_ONLY — when *our* process first held the bytes.

    This is the perishable quantity. Prices, follower counts and post bodies
    can be re-fetched later; the instant a byte arrived cannot. If this field
    is wrong, no amount of later work repairs it.

    It carries three things, not one:

    ``value``               wall clock UTC — for joining to other systems.
    ``monotonic_ns``        local monotonic — for *intervals*, and only within
                            ``epoch_id``.
    ``epoch_id``            which process run read the monotonic clock.

    A platform payload never carries a monotonic stamp or our epoch id, which
    is precisely why this type cannot be forged from one.
    """

    value: str  # canonical RFC3339 UTC
    monotonic_ns: int
    epoch_id: str
    owner: ClockOwner = ClockOwner.COLLECTOR

    def __post_init__(self) -> None:
        if self.owner is not ClockOwner.COLLECTOR:
            raise ClockConfusionError(
                "OurReceivedAt is by definition a COLLECTOR clock reading; "
                f"got owner={self.owner!r}"
            )
        if not isinstance(self.monotonic_ns, int) or isinstance(
            self.monotonic_ns, bool
        ):
            raise ClockConfusionError(
                "monotonic_ns must be an int of nanoseconds from this "
                "process's monotonic clock"
            )
        if not self.epoch_id:
            raise ClockConfusionError(
                "OurReceivedAt requires the epoch of the process that read "
                "the monotonic clock; without it the stamp cannot be used for "
                "any interval"
            )
        parse_canonical_datetime(self.value)

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "monotonic_ns": self.monotonic_ns,
            "epoch_id": self.epoch_id,
            "clock_owner": self.owner.value,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "OurReceivedAt":
        return cls(
            value=str(payload["value"]),
            monotonic_ns=int(payload["monotonic_ns"]),
            epoch_id=str(payload["epoch_id"]),
        )


#: A receipt clock reads *our* two stamps together. It takes no arguments on
#: purpose: there is no parameter through which a platform-supplied time could
#: arrive.
ReceiptClock = Callable[[], tuple[datetime, int]]


def SystemReceiptClock() -> tuple[datetime, int]:
    """The real receipt clock: wall clock and monotonic, taken together.

    Both stamps are read before any parsing work, mirroring
    `app/realtime/collector.py`, so that a derived offset is not inflated by
    our own deserialisation.
    """

    return utcnow(), monotonic_ns()


def capture_receipt(
    epoch: ProcessEpoch,
    *,
    clock: ReceiptClock = SystemReceiptClock,
) -> OurReceivedAt:
    """The ONLY factory for :class:`OurReceivedAt` used by this package.

    It reads a clock. It accepts no timestamp. There is deliberately no
    ``capture_receipt(from_source=...)`` overload, no ``default``, and no
    fallback branch: if the clock cannot be read the exception propagates and
    the item is not ingested, because an item with a fabricated receipt time is
    worse than an item we never collected.
    """

    wall, mono = clock()
    if wall.tzinfo is None:
        raise ClockConfusionError("receipt clock returned a naive datetime")
    return OurReceivedAt(
        value=canonical_datetime(wall.astimezone(timezone.utc)),
        monotonic_ns=int(mono),
        epoch_id=epoch.epoch_id,
    )


@dataclass(frozen=True)
class DeliveryOffset:
    """DERIVED_STATE — a cross-clock difference, named at length on purpose.

    ``offset_contaminated_us = our_received_at - source_created_at``
                             = true_delivery_lag + (our_clock_offset − their_clock_offset)
                             + (their_stamp_semantics − actual_creation_instant)

    Two of those three terms are uncharacterised, so this is **evidence, not a
    latency**. Negative samples are kept, never dropped: on a cross-clock hop a
    negative value *is* the offset evidence.

    What the distribution of this quantity means for a future lead-lag claim:

      * Its **spread**, not its centre, is the usable part. A constant offset
        cancels out of any within-platform comparison; a heavy right tail does
        not, and it is the tail that decides whether "post preceded price move
        by 400 ms" survives.
      * Its width is a **floor on the resolution of any lead-lag claim made
        against `source_created_at`**. If the offset's inter-quartile range is
        3 s, a 400 ms lead measured that way is unmeasurable, whatever the
        p-value says.
      * Comparing it **across platforms is meaningless** until each platform's
        stamp semantics are separately verified (doctrine 8).
      * A lead-lag claim measured against `our_received_at` instead is sound in
        *our* frame but is a statement about "when we could have known", which
        is the tradeable question anyway.
    """

    offset_contaminated_us: int
    host_clock_offset_characterised: bool = False
    source_time_fidelity: SourceTimeFidelity = SourceTimeFidelity.UNVERIFIED

    def to_json(self) -> dict[str, Any]:
        return {
            "offset_contaminated_us": self.offset_contaminated_us,
            "host_clock_offset_characterised": self.host_clock_offset_characterised,
            "source_time_fidelity": self.source_time_fidelity.value,
        }


def delivery_offset(
    source_created_at: SourceCreatedAt,
    our_received_at: OurReceivedAt,
) -> DeliveryOffset:
    """Compute the contaminated cross-clock offset.

    Integer microseconds, never a float: a float is not canonically
    representable and would break digest round-tripping (measurement contract
    §4.1).
    """

    if not isinstance(source_created_at, SourceCreatedAt):
        raise ClockConfusionError(
            "delivery_offset requires a SourceCreatedAt for the platform term"
        )
    if not isinstance(our_received_at, OurReceivedAt):
        raise ClockConfusionError(
            "delivery_offset requires an OurReceivedAt for the collector term"
        )
    src = parse_canonical_datetime(source_created_at.value)
    ours = parse_canonical_datetime(our_received_at.value)
    delta = ours - src
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return DeliveryOffset(
        offset_contaminated_us=int(micros),
        host_clock_offset_characterised=False,
        source_time_fidelity=source_created_at.fidelity,
    )


def pipeline_interval_us(start: OurReceivedAt, end: OurReceivedAt) -> int:
    """The only sound internal latency: two of OUR stamps, one epoch.

    Refuses cross-epoch pairs. Refuses a :class:`SourceCreatedAt` by type — it
    has no ``monotonic_ns`` to offer, so a platform timestamp cannot reach this
    function even by accident.
    """

    if not isinstance(start, OurReceivedAt) or not isinstance(end, OurReceivedAt):
        raise ClockConfusionError(
            "a pipeline interval may only be computed between two "
            "OurReceivedAt stamps; platform time cannot measure our pipeline"
        )
    if start.epoch_id != end.epoch_id:
        raise CrossEpochIntervalError(
            "monotonic stamps from different process epochs are not comparable"
        )
    return (end.monotonic_ns - start.monotonic_ns) // 1_000
