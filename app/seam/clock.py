"""``ObservationTimestamp`` — a CROSS-PROCESS clock contract.

`app/social/timebase.py` protects our-clock intervals with a process epoch:
two ``OurReceivedAt`` values are subtractable only inside one process run.
That is correct and it is not enough. Once the social collector, the quote
path and the fill decoder run in **separate processes** — which is the whole
point of the join — the epoch guard refuses every interesting interval,
because the two stamps genuinely came from different processes.

The fix is not to relax the guard. It is to identify the thing that actually
makes ``time.monotonic_ns()`` comparable:

    On Linux ``CLOCK_MONOTONIC`` and on macOS ``mach_absolute_time`` are
    **boot-relative**, not process-relative. Two processes on the SAME BOOT of
    the SAME HOST read the same monotonic timeline.

So the comparability key is ``(host_id, host_boot_id)``, and ``process_epoch_id``
becomes the *fallback* key for platforms where the boot id cannot be read.
This strictly widens what is computable without weakening anything: an epoch
match still implies a boot match, and a boot mismatch still refuses.

``host_boot_id`` on Linux is ``/proc/sys/kernel/random/boot_id``. On macOS
that file does not exist. **We do not fabricate one.** A synthesised boot id
would be a value that looks like evidence of a shared timeline while being
evidence of nothing — the exact defect class this repo keeps finding. Absence
is typed (:class:`BootIdStatus`) and propagates into the interval rules.

Interval rules, enforced in :func:`interval`
--------------------------------------------

1. same ``host_id`` **and** same known ``host_boot_id``
   → monotonic interval PERMITTED, across processes.
2. same ``host_id``, boot unknown, same ``process_epoch_id``
   → monotonic interval PERMITTED (the epoch is a weaker witness of the same
   timeline; this is `app/social`'s existing rule, preserved).
3. different host, or a boot/epoch we cannot match
   → **wall interval ONLY if a synchronization error bound is known**, and the
   bound TRAVELS with the result.
4. otherwise → ``NOT_COMPUTABLE``.

Three quantities, three names
-----------------------------
Kept separate because they mean different things and pooling them is how a
lead-lag figure becomes a statement about our own downtime:

``ExternalDeliveryLatency``  ``t_received − t_created``  platform → us.
                             CROSS-CLOCK and contaminated; evidence, not a
                             latency (`app/social` already says so).
``OurResponseLatency``       ``t_quote − t_received``    ours → ours. The only
                             sound internal interval.
``CrossDomainInterval``      anything spanning platform→ours→chain. Inherits
                             the WORST fidelity in the chain and says so.

CONTAINS NO SIGNAL.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from app.realtime.book import monotonic_ns, utcnow
from app.realtime.canonical import canonical_datetime, parse_canonical_datetime

__all__ = [
    "TimeDomain",
    "ClockQuality",
    "BootIdStatus",
    "HostBootId",
    "read_host_boot_id",
    "host_id",
    "ObservationTimestamp",
    "capture_observation",
    "SyncBound",
    "IntervalBasis",
    "NotComputableReason",
    "IntervalResult",
    "ComputedInterval",
    "NotComputable",
    "interval",
    "ExternalDeliveryLatency",
    "OurResponseLatency",
    "CrossDomainInterval",
    "external_delivery_latency",
    "our_response_latency",
    "cross_domain_interval",
    "from_our_received_at",
    "ClockContractError",
    "BOOT_ID_PATH",
    "UNKNOWN_HOST",
    "legacy_wall_interval_us",
    "new_process_epoch_id",
]

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


class ClockContractError(Exception):
    """A clock rule was violated at construction time."""


class TimeDomain(str, Enum):
    """Who owns the clock. EVIDENCE-JOIN-CONTRACT-001 §3."""

    #: X / Telegram / Discord. Their clock, their semantics, uncharacterised
    #: offset from ours.
    PLATFORM = "PLATFORM"
    #: This host. The only domain in which we can measure our own pipeline.
    OURS = "OURS"
    #: Solana. `slot` is the ordering primitive; `t_confirmed` is a
    #: cluster-derived stamp and is NOT our time.
    CHAIN = "CHAIN"


class ClockQuality(str, Enum):
    """What this stamp is capable of supporting."""

    #: Wall clock AND monotonic, with a boot or epoch witness. Intervals
    #: against a matching stamp are immune to NTP steps.
    MONOTONIC_ANCHORED = "MONOTONIC_ANCHORED"
    #: Wall clock only, but the host's synchronization error is bounded and
    #: the bound is recorded.
    WALL_SYNCHRONIZED = "WALL_SYNCHRONIZED"
    #: Wall clock only, no monotonic anchor, no bound. Every legacy bare
    #: `datetime` lands here. Usable for ordering at coarse resolution and for
    #: nothing that needs sub-second honesty.
    WALL_ONLY = "WALL_ONLY"
    #: We do not know what produced it.
    UNKNOWN = "UNKNOWN"


class BootIdStatus(str, Enum):
    """Typed absence for the boot id. Never fabricated."""

    PRESENT = "PRESENT"
    #: The platform does not expose one (macOS has no
    #: `/proc/sys/kernel/random/boot_id`). A REAL, expected state.
    NOT_AVAILABLE_ON_PLATFORM = "NOT_AVAILABLE_ON_PLATFORM"
    #: The file exists and we could not read it (permissions, container).
    UNREADABLE = "UNREADABLE"
    #: Nobody asked. Distinct from "asked and there is none".
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True, slots=True)
class HostBootId:
    """The boot identity of the host that read the monotonic clock.

    Two stamps sharing a boot id share a monotonic timeline **even across
    processes**. Two stamps whose boot ids merely *fail to conflict* (because
    one or both are unknown) share nothing — :meth:`matches` returns False
    unless both are PRESENT and equal, which is the conservative direction.
    """

    status: BootIdStatus
    value: str | None = None

    def __post_init__(self) -> None:
        if self.status is BootIdStatus.PRESENT and not self.value:
            raise ClockContractError(
                "BootIdStatus.PRESENT requires the boot id itself"
            )
        if self.status is not BootIdStatus.PRESENT and self.value:
            raise ClockContractError(
                f"{self.status.value} must not carry a boot id value; a "
                "synthesised boot id is indistinguishable from a real one "
                "downstream and would certify a shared timeline that does "
                "not exist"
            )

    @property
    def is_known(self) -> bool:
        return self.status is BootIdStatus.PRESENT

    def matches(self, other: "HostBootId") -> bool:
        return (
            self.is_known and other.is_known and self.value == other.value
        )

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status.value, "value": self.value}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "HostBootId":
        return cls(
            status=BootIdStatus(payload["status"]), value=payload.get("value")
        )

    @classmethod
    def unknown(cls) -> "HostBootId":
        return cls(status=BootIdStatus.NOT_ATTEMPTED)


def read_host_boot_id(path: str = BOOT_ID_PATH) -> HostBootId:
    """Read the kernel boot id, or report typed absence.

    Never raises, never invents. macOS returns
    ``NOT_AVAILABLE_ON_PLATFORM`` — which is a fact about the platform, and a
    fact the interval rules then act on by falling back to the process epoch.
    """
    try:
        with open(path, "r", encoding="ascii") as handle:
            raw = handle.read().strip()
    except FileNotFoundError:
        return HostBootId(status=BootIdStatus.NOT_AVAILABLE_ON_PLATFORM)
    except OSError:
        return HostBootId(status=BootIdStatus.UNREADABLE)
    if not raw:
        return HostBootId(status=BootIdStatus.UNREADABLE)
    return HostBootId(status=BootIdStatus.PRESENT, value=raw)


def host_id() -> str:
    """Stable-enough identity of this machine for interval gating.

    Deliberately coarse and deliberately NOT a fingerprint: it exists so that
    "these two stamps came from different machines" is answerable. It is
    combined with the boot id, never used alone to permit a monotonic
    interval.
    """
    return os.environ.get("SEAM_HOST_ID") or socket.gethostname()


#: Sentinel host identity for a stamp that carries no host information at all
#: — every bare `datetime` migrated from `app/fills`. It never matches
#: anything, including itself, so it can never license a monotonic interval.
UNKNOWN_HOST = "unknown-host"


@dataclass(frozen=True, slots=True)
class ObservationTimestamp:
    """One reading of OUR clocks, carrying everything an interval needs.

    ``wall_utc``          canonical RFC3339 UTC. For joining to other systems.
    ``monotonic_ns``      boot-relative monotonic ns, or ``None``.
    ``host_boot_id``      typed; the cross-process comparability key.
    ``process_epoch_id``  which process run read the monotonic clock. Fallback
                          comparability key where the boot id is unavailable.
    ``host_id``           which machine.
    ``clock_quality``     what this stamp can support.
    ``domain``            OURS by construction. Platform and chain stamps do
                          NOT become this type — see the module docstring.
    """

    wall_utc: str
    host_id: str
    host_boot_id: HostBootId
    clock_quality: ClockQuality
    monotonic_ns: int | None = None
    process_epoch_id: str | None = None
    domain: TimeDomain = TimeDomain.OURS
    #: Known bound on this host's wall-clock synchronization error, if any.
    sync_bound: "SyncBound | None" = None

    def __post_init__(self) -> None:
        parse_canonical_datetime(self.wall_utc)
        if self.domain is not TimeDomain.OURS:
            raise ClockContractError(
                "ObservationTimestamp is a reading of OUR clocks. A platform "
                "claim is a SourceCreatedAt and a chain stamp belongs to the "
                "CHAIN domain; neither may be promoted into this type"
            )
        if not self.host_id:
            raise ClockContractError(
                "an ObservationTimestamp must name the host that read the "
                "clock, or no interval rule can be applied to it"
            )
        if isinstance(self.monotonic_ns, bool) or (
            self.monotonic_ns is not None
            and not isinstance(self.monotonic_ns, int)
        ):
            raise ClockContractError("monotonic_ns must be int nanoseconds")
        if self.clock_quality is ClockQuality.MONOTONIC_ANCHORED:
            if self.monotonic_ns is None:
                raise ClockContractError(
                    "MONOTONIC_ANCHORED without a monotonic reading"
                )
            if not self.host_boot_id.is_known and not self.process_epoch_id:
                raise ClockContractError(
                    "MONOTONIC_ANCHORED needs a witness of WHICH timeline the "
                    "monotonic reading belongs to: a boot id, or failing that "
                    "a process epoch. Without one the number is not "
                    "comparable to anything"
                )
        if self.clock_quality is ClockQuality.WALL_SYNCHRONIZED and (
            self.sync_bound is None
        ):
            raise ClockContractError(
                "WALL_SYNCHRONIZED asserts a bounded clock error and must "
                "carry the bound; an unquantified 'we run NTP' is not a bound"
            )

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_wall_clock(
        cls,
        value: datetime,
        *,
        host: str = UNKNOWN_HOST,
        sync_bound: "SyncBound | None" = None,
    ) -> "ObservationTimestamp":
        """Wrap a bare ``datetime``. **This is a downgrade, not a repair.**

        Used to migrate `app/fills`' legacy ``t_quote`` / ``t_submit``. The
        result is honestly typed ``WALL_ONLY`` on ``UNKNOWN_HOST``: a bare
        datetime carries no host, no boot and no monotonic anchor, so nothing
        downstream may pretend otherwise. Intervals between two of these are
        ``NOT_COMPUTABLE`` under :func:`interval`, which is exactly what
        EVIDENCE-JOIN-CONTRACT-001 §3 demands.
        """
        if value.tzinfo is None:
            raise ClockContractError(
                "a naive datetime is refused: reading it as local time "
                "canonicalises differently on two hosts"
            )
        return cls(
            wall_utc=canonical_datetime(value.astimezone(timezone.utc)),
            host_id=host,
            host_boot_id=HostBootId.unknown(),
            clock_quality=(
                ClockQuality.WALL_SYNCHRONIZED
                if sync_bound is not None
                else ClockQuality.WALL_ONLY
            ),
            sync_bound=sync_bound,
        )

    @property
    def wall_datetime(self) -> datetime:
        return parse_canonical_datetime(self.wall_utc)

    def to_json(self) -> dict[str, Any]:
        return {
            "wall_utc": self.wall_utc,
            "host_id": self.host_id,
            "host_boot_id": self.host_boot_id.to_json(),
            "clock_quality": self.clock_quality.value,
            "monotonic_ns": self.monotonic_ns,
            "process_epoch_id": self.process_epoch_id,
            "domain": self.domain.value,
            "sync_bound": (
                self.sync_bound.to_json() if self.sync_bound else None
            ),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "ObservationTimestamp":
        bound = payload.get("sync_bound")
        return cls(
            wall_utc=str(payload["wall_utc"]),
            host_id=str(payload["host_id"]),
            host_boot_id=HostBootId.from_json(payload["host_boot_id"]),
            clock_quality=ClockQuality(payload["clock_quality"]),
            monotonic_ns=payload.get("monotonic_ns"),
            process_epoch_id=payload.get("process_epoch_id"),
            sync_bound=SyncBound.from_json(bound) if bound else None,
        )


def capture_observation(
    *,
    process_epoch_id: str,
    boot_id: HostBootId | None = None,
    host: str | None = None,
    clock=None,
) -> ObservationTimestamp:
    """Read both of our clocks together. The only live factory.

    Takes no timestamp argument, by construction: there is no parameter
    through which a platform-supplied or chain-supplied time could arrive.
    """
    reader = clock or (lambda: (utcnow(), monotonic_ns()))
    wall, mono = reader()
    if wall.tzinfo is None:
        raise ClockContractError("clock returned a naive datetime")
    return ObservationTimestamp(
        wall_utc=canonical_datetime(wall.astimezone(timezone.utc)),
        host_id=host or host_id(),
        host_boot_id=boot_id if boot_id is not None else read_host_boot_id(),
        clock_quality=ClockQuality.MONOTONIC_ANCHORED,
        monotonic_ns=int(mono),
        process_epoch_id=process_epoch_id,
    )


def from_our_received_at(
    received,
    *,
    boot_id: HostBootId | None = None,
    host: str | None = None,
) -> ObservationTimestamp:
    """Lift `app/social`'s ``OurReceivedAt`` into the seam type.

    Lossless: wall, monotonic and epoch all carry across. The boot id is NOT
    invented — `OurReceivedAt` does not record one, so unless the caller
    supplies the boot id that was current for that epoch, the result falls
    back to epoch-based comparability (rule 2).
    """
    from app.social.timebase import OurReceivedAt

    if not isinstance(received, OurReceivedAt):
        raise ClockContractError(
            "from_our_received_at requires an app.social.timebase."
            "OurReceivedAt"
        )
    return ObservationTimestamp(
        wall_utc=received.value,
        host_id=host or host_id(),
        host_boot_id=boot_id if boot_id is not None else HostBootId.unknown(),
        clock_quality=ClockQuality.MONOTONIC_ANCHORED,
        monotonic_ns=int(received.monotonic_ns),
        process_epoch_id=received.epoch_id,
    )


# ---------------------------------------------------------------------------
# synchronization bounds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncBound:
    """A MEASURED bound on wall-clock synchronization error.

    Not a hope, not "we run chrony". ``method`` names how the bound was
    established and ``measured_at`` says when — a bound from last month is a
    claim about last month.
    """

    max_error_us: int
    method: str
    measured_at: str

    def __post_init__(self) -> None:
        if self.max_error_us < 0:
            raise ClockContractError("a sync bound cannot be negative")
        if not self.method:
            raise ClockContractError(
                "a sync bound must name how it was established"
            )
        parse_canonical_datetime(self.measured_at)

    def combined_with(self, other: "SyncBound | None") -> "SyncBound":
        """Two clocks, two bounds: the pair's error is bounded by the sum."""
        if other is None:
            return self
        return SyncBound(
            max_error_us=self.max_error_us + other.max_error_us,
            method=f"{self.method}+{other.method}",
            measured_at=max(self.measured_at, other.measured_at),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "max_error_us": self.max_error_us,
            "method": self.method,
            "measured_at": self.measured_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SyncBound":
        return cls(
            max_error_us=int(payload["max_error_us"]),
            method=str(payload["method"]),
            measured_at=str(payload["measured_at"]),
        )


# ---------------------------------------------------------------------------
# intervals
# ---------------------------------------------------------------------------


class IntervalBasis(str, Enum):
    #: Both stamps on one monotonic timeline, witnessed by a shared boot id.
    #: Immune to NTP steps. Valid ACROSS processes.
    MONOTONIC_SAME_BOOT = "MONOTONIC_SAME_BOOT"
    #: Both stamps on one monotonic timeline, witnessed only by a shared
    #: process epoch. `app/social`'s existing rule.
    MONOTONIC_SAME_EPOCH = "MONOTONIC_SAME_EPOCH"
    #: Wall clock, with a known synchronization bound that travels along.
    WALL_BOUNDED = "WALL_BOUNDED"
    #: Wall clock on one host with no anchor and no bound. Only reachable
    #: through the explicit legacy door; never returned by `interval()`.
    WALL_UNANCHORED = "WALL_UNANCHORED"


class NotComputableReason(str, Enum):
    DIFFERENT_HOST_NO_BOUND = "DIFFERENT_HOST_NO_BOUND"
    UNKNOWN_BOOT_NO_EPOCH_MATCH = "UNKNOWN_BOOT_NO_EPOCH_MATCH"
    NO_MONOTONIC_ANCHOR_NO_BOUND = "NO_MONOTONIC_ANCHOR_NO_BOUND"
    BOOT_MISMATCH = "BOOT_MISMATCH"
    DOMAIN_MISMATCH = "DOMAIN_MISMATCH"


@dataclass(frozen=True, slots=True)
class ComputedInterval:
    """A duration that is allowed to exist, with the terms of its licence."""

    microseconds: int
    basis: IntervalBasis
    #: Travels with the result whenever the basis is WALL_BOUNDED. Rule 3:
    #: "the bound travels with the result".
    sync_bound: SyncBound | None
    domain: TimeDomain
    notes: tuple[str, ...] = ()

    @property
    def is_computable(self) -> bool:
        return True

    def to_json(self) -> dict[str, Any]:
        return {
            "computable": True,
            "microseconds": self.microseconds,
            "basis": self.basis.value,
            "sync_bound": (
                self.sync_bound.to_json() if self.sync_bound else None
            ),
            "domain": self.domain.value,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class NotComputable:
    """The refusal. Carries no number at all — deliberately.

    There is no ``.microseconds``, no ``.value``, and no default, so a caller
    cannot accidentally read a plausible duration off a refusal.
    """

    reason: NotComputableReason
    detail: str

    @property
    def is_computable(self) -> bool:
        return False

    def to_json(self) -> dict[str, Any]:
        return {
            "computable": False,
            "reason": self.reason.value,
            "detail": self.detail,
        }


IntervalResult = ComputedInterval | NotComputable


def _wall_delta_us(start: ObservationTimestamp, end: ObservationTimestamp) -> int:
    delta = end.wall_datetime - start.wall_datetime
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def interval(
    start: ObservationTimestamp,
    end: ObservationTimestamp,
    *,
    sync_bound: SyncBound | None = None,
) -> IntervalResult:
    """Apply the four interval rules. The ONLY sanctioned door.

    ``sync_bound`` may be supplied by the caller for a cross-host pair whose
    synchronization has been characterised elsewhere; it is combined with any
    bound the stamps carry themselves.
    """
    if not isinstance(start, ObservationTimestamp) or not isinstance(
        end, ObservationTimestamp
    ):
        raise ClockContractError(
            "interval() requires two ObservationTimestamps; a bare datetime "
            "cannot state which timeline it belongs to"
        )

    same_host = (
        start.host_id == end.host_id and start.host_id != UNKNOWN_HOST
    )
    bound = sync_bound
    for stamp in (start, end):
        if stamp.sync_bound is not None:
            bound = (
                stamp.sync_bound if bound is None
                else bound.combined_with(stamp.sync_bound)
            )

    have_monotonic = (
        start.monotonic_ns is not None and end.monotonic_ns is not None
    )

    # rule 1 — same host, same known boot: monotonic, ACROSS processes.
    if same_host and have_monotonic and start.host_boot_id.matches(
        end.host_boot_id
    ):
        return ComputedInterval(
            microseconds=(end.monotonic_ns - start.monotonic_ns) // 1_000,
            basis=IntervalBasis.MONOTONIC_SAME_BOOT,
            sync_bound=None,
            domain=TimeDomain.OURS,
            notes=(
                "same host_boot_id: CLOCK_MONOTONIC is boot-relative, so the "
                "two readings are on one timeline even across processes",
            ),
        )

    # a boot mismatch is a hard refusal: the monotonic clock reset in between.
    if (
        have_monotonic
        and start.host_boot_id.is_known
        and end.host_boot_id.is_known
        and start.host_boot_id.value != end.host_boot_id.value
    ):
        if bound is not None:
            return ComputedInterval(
                microseconds=_wall_delta_us(start, end),
                basis=IntervalBasis.WALL_BOUNDED,
                sync_bound=bound,
                domain=TimeDomain.OURS,
                notes=("host rebooted between the stamps; wall clock only",),
            )
        return NotComputable(
            reason=NotComputableReason.BOOT_MISMATCH,
            detail=(
                "the host rebooted between the two stamps, so the monotonic "
                "clock reset; their difference is not a duration"
            ),
        )

    # rule 2 — same host, boot unknown, same process epoch.
    if (
        same_host
        and have_monotonic
        and start.process_epoch_id
        and start.process_epoch_id == end.process_epoch_id
    ):
        return ComputedInterval(
            microseconds=(end.monotonic_ns - start.monotonic_ns) // 1_000,
            basis=IntervalBasis.MONOTONIC_SAME_EPOCH,
            sync_bound=None,
            domain=TimeDomain.OURS,
            notes=(
                "boot id unavailable; comparability witnessed by a shared "
                "process epoch only",
            ),
        )

    # rule 3 — wall, and only with a bound that travels.
    if bound is not None:
        return ComputedInterval(
            microseconds=_wall_delta_us(start, end),
            basis=IntervalBasis.WALL_BOUNDED,
            sync_bound=bound,
            domain=TimeDomain.OURS,
            notes=(
                f"wall-clock difference; synchronization error bounded at "
                f"±{bound.max_error_us} us by {bound.method}",
            ),
        )

    # rule 4 — refuse.
    if not same_host:
        return NotComputable(
            reason=NotComputableReason.DIFFERENT_HOST_NO_BOUND,
            detail=(
                f"stamps came from {start.host_id!r} and {end.host_id!r} with "
                "no synchronization bound between their wall clocks"
            ),
        )
    if not have_monotonic:
        return NotComputable(
            reason=NotComputableReason.NO_MONOTONIC_ANCHOR_NO_BOUND,
            detail=(
                "one or both stamps are wall-clock-only with no bound; the "
                "difference is exposed to any NTP step between the readings"
            ),
        )
    return NotComputable(
        reason=NotComputableReason.UNKNOWN_BOOT_NO_EPOCH_MATCH,
        detail=(
            "monotonic readings from different process epochs on a host whose "
            "boot id could not be read; nothing witnesses a shared timeline"
        ),
    )


def legacy_wall_interval_us(
    start: ObservationTimestamp, end: ObservationTimestamp
) -> ComputedInterval:
    """The explicit legacy door: an unanchored wall difference.

    Exists so that pre-seam call sites in `app/fills` keep working while being
    LABELLED. ``WALL_UNANCHORED`` is never returned by :func:`interval`, so no
    strict consumer can receive one by accident, and the note travels on the
    record.
    """
    return ComputedInterval(
        microseconds=_wall_delta_us(start, end),
        basis=IntervalBasis.WALL_UNANCHORED,
        sync_bound=None,
        domain=TimeDomain.OURS,
        notes=(
            "unanchored wall-clock difference: no monotonic anchor and no "
            "synchronization bound; exposed to any NTP step between the two "
            "readings",
        ),
    )


# ---------------------------------------------------------------------------
# the three named quantities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalDeliveryLatency:
    """``t_received − t_created``: platform → us. **Evidence, not a latency.**

    Cross-clock by construction, so it is contaminated by the unknown offset
    between their clock and ours plus the unknown semantics of their stamp.
    Named at length so that no consumer mistakes it for
    :class:`OurResponseLatency`.
    """

    contaminated_us: int
    source_time_fidelity: str
    host_clock_offset_characterised: bool
    delivery_mode: str
    domains: tuple[TimeDomain, TimeDomain] = (
        TimeDomain.PLATFORM,
        TimeDomain.OURS,
    )

    @property
    def is_latency(self) -> bool:
        """Always False. The property exists to be read and refused."""
        return False

    def to_json(self) -> dict[str, Any]:
        return {
            "quantity": "external_delivery_latency",
            "contaminated_us": self.contaminated_us,
            "source_time_fidelity": self.source_time_fidelity,
            "host_clock_offset_characterised": (
                self.host_clock_offset_characterised
            ),
            "delivery_mode": self.delivery_mode,
        }


@dataclass(frozen=True, slots=True)
class OurResponseLatency:
    """``t_quote − t_received``: ours → ours. The one sound internal interval."""

    result: IntervalResult

    @property
    def is_computable(self) -> bool:
        return self.result.is_computable

    def to_json(self) -> dict[str, Any]:
        return {
            "quantity": "our_response_latency",
            "result": self.result.to_json(),
        }


@dataclass(frozen=True, slots=True)
class CrossDomainInterval:
    """Anything spanning platform → ours → chain.

    Inherits the WORST fidelity in the chain and says so on the record.
    EVIDENCE-JOIN-CONTRACT-001 §3.2: no interval may cross a domain boundary
    without being typed as cross-domain.
    """

    microseconds: int
    domains: tuple[TimeDomain, ...]
    worst_fidelity: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.domains)) < 2:
            raise ClockContractError(
                "CrossDomainInterval must actually cross a domain boundary; "
                "a single-domain interval belongs to interval()"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "quantity": "cross_domain_interval",
            "microseconds": self.microseconds,
            "domains": [d.value for d in self.domains],
            "worst_fidelity": self.worst_fidelity,
            "notes": list(self.notes),
        }


def external_delivery_latency(
    source_created_at, our_received_at, *, delivery_mode: str
) -> ExternalDeliveryLatency:
    """Compute the contaminated platform→us offset, carrying `delivery_mode`.

    `delivery_mode` is REQUIRED. EVIDENCE-JOIN-CONTRACT-001 §5: a backfilled
    artifact has an honest receipt time that is not live delivery timing, and
    pooling the two manufactures a right tail out of our own downtime.
    """
    from app.social.timebase import delivery_offset

    offset = delivery_offset(source_created_at, our_received_at)
    if not delivery_mode:
        raise ClockContractError(
            "external_delivery_latency requires the delivery_mode of the "
            "artifact; without it the figure cannot be conditioned on"
        )
    return ExternalDeliveryLatency(
        contaminated_us=offset.offset_contaminated_us,
        source_time_fidelity=offset.source_time_fidelity.value,
        host_clock_offset_characterised=offset.host_clock_offset_characterised,
        delivery_mode=delivery_mode,
    )


def our_response_latency(
    received: ObservationTimestamp,
    quote: ObservationTimestamp,
    *,
    sync_bound: SyncBound | None = None,
) -> OurResponseLatency:
    """``tau_social->quote``. Guarded by :func:`interval`, so a legacy bare
    ``datetime`` on the fills side yields ``NOT_COMPUTABLE`` rather than a
    plausible number."""
    return OurResponseLatency(
        result=interval(received, quote, sync_bound=sync_bound)
    )


#: Ordering of fidelity, worst last. A cross-domain interval takes the last
#: element present in its chain.
_FIDELITY_ORDER = ("VERIFIED", "UNVERIFIED", "NOT_PROVIDED")


def cross_domain_interval(
    microseconds: int,
    *,
    domains: tuple[TimeDomain, ...],
    fidelities: tuple[str, ...],
    notes: tuple[str, ...] = (),
) -> CrossDomainInterval:
    """Build a cross-domain figure, forcing it to inherit the worst fidelity."""
    worst = "UNVERIFIED"
    rank = -1
    for fidelity in fidelities:
        try:
            index = _FIDELITY_ORDER.index(fidelity)
        except ValueError:
            index = len(_FIDELITY_ORDER)
        if index > rank:
            rank, worst = index, fidelity
    return CrossDomainInterval(
        microseconds=microseconds,
        domains=domains,
        worst_fidelity=worst,
        notes=notes,
    )


def new_process_epoch_id() -> str:
    """Mint an epoch id for a process that is not running the social tape."""
    return uuid.uuid4().hex
