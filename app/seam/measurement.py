"""``Measurement[T]`` — absence as TWO independent dimensions.

EVIDENCE-JOIN-CONTRACT-001 §2 found that `app/fills` `AbsenceReason` and
`app/social` `DeferredState` agree on exactly one member and encode *different
distinctions*:

* `app/fills` answers **why we cannot have it** — never provided, cannot be
  rebuilt, not yet seen, not authorized.
* `app/social` answers **what looking achieved** — nobody looked, looked and
  found nothing, looked and found something.

Merging them into one enum destroys information in both directions. So this
module does not merge them. It carries both axes at once:

    availability   AVAILABLE | NOT_PROVIDED | NOT_RECONSTRUCTABLE
                   | NOT_YET_OBSERVED | NOT_AUTHORIZED | NOT_APPLICABLE
    observation    NOT_ATTEMPTED | OBSERVED_NONE | OBSERVED_VALUE

The combination the whole seam exists to protect:

    availability=AVAILABLE, observation=OBSERVED_NONE

  = "we watched the window and the event did not occur". A **real negative
    label**. `P(wallet_confirmation = 0 | social event)` is only meaningful
    when we actually watched.

versus

    availability=NOT_PROVIDED, observation=NOT_ATTEMPTED

  = "we have no measurement". Not a zero, not a negative, not evidence.

Those two must never collapse, and here they cannot: they differ on both
axes, they serialize differently, and neither will yield a number.

**Illegal combinations are unconstructible, not discouraged.** The legality
table is enforced in ``__post_init__`` and there is no bypass:

| observation | permitted availability | value |
|---|---|---|
| ``OBSERVED_VALUE`` | ``AVAILABLE`` only | required |
| ``OBSERVED_NONE``  | ``AVAILABLE`` only | must be ``None``; **window required** |
| ``NOT_ATTEMPTED``  | any | must be ``None`` |

The window requirement on ``OBSERVED_NONE`` is not decoration. A negative
label without the window it was measured over cannot be compared to anything,
cannot be pooled, and cannot state its own noise floor (doctrine 4). "We
looked and saw nothing" is a claim about an interval or it is not a claim.

**Losslessness.** Both source vocabularies map in and back out exactly. The
coarse ``availability`` axis is a projection, so the ORIGINAL source term
travels on the record as :class:`OriginTag` — which is also EVIDENCE-JOIN-
CONTRACT-001 §2's requirement that "a joined row carries both vocabularies or
neither". ``tests/test_social_fill_seam_001.py`` proves the round trip for
every member of both enums.

CONTAINS NO SIGNAL.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Generic, Mapping, TypeVar

T = TypeVar("T")

__all__ = [
    "Availability",
    "Observation",
    "ObservationWindow",
    "OriginTag",
    "Measurement",
    "MeasurementError",
    "IllegalMeasurementError",
    "UnmappableAbsenceError",
    "MeasurementAbsentError",
    "FILLS_VOCABULARY",
    "SOCIAL_VOCABULARY",
    "from_fills_absence",
    "from_fills_maybe",
    "to_fills_maybe",
    "from_social_deferred",
    "to_social_deferred",
    "FILLS_AVAILABILITY_TABLE",
    "FILLS_OBSERVATION_TABLE",
    "SOCIAL_AVAILABILITY_TABLE",
    "SOCIAL_OBSERVATION_TABLE",
]


class MeasurementError(Exception):
    """Base class for seam measurement violations."""


class IllegalMeasurementError(MeasurementError):
    """A combination the schema forbids was requested.

    Raised at construction so that an impossible measurement cannot exist in
    memory, be serialized, or reach a join.
    """


class UnmappableAbsenceError(MeasurementError):
    """A source vocabulary term has no image in the seam, or vice versa.

    EVIDENCE-JOIN-CONTRACT-001 §2: "any pair that does not correspond is an
    error rather than a best-effort match".
    """


class MeasurementAbsentError(MeasurementError):
    """Code demanded a value from a measurement that does not carry one."""

    def __init__(self, measurement: "Measurement") -> None:
        self.measurement = measurement
        super().__init__(
            f"availability={measurement.availability.value} "
            f"observation={measurement.observation.value}: no value"
        )


class Availability(str, Enum):
    """**Why the quantity can or cannot be had.** Independent of whether
    anybody looked."""

    #: The quantity is obtainable by us. This is the ONLY availability under
    #: which an observation may have taken place.
    AVAILABLE = "AVAILABLE"

    #: The source was consulted (or would be) and supplies nothing for this
    #: field. Mirrors `fills.AbsenceReason.NOT_PROVIDED`.
    NOT_PROVIDED = "NOT_PROVIDED"

    #: The source spoke but the quantity is not derivable from what it said.
    NOT_RECONSTRUCTABLE = "NOT_RECONSTRUCTABLE"

    #: The observation window has not elapsed. Waiting WILL produce it.
    NOT_YET_OBSERVED = "NOT_YET_OBSERVED"

    #: An authorization we do not have is required. Waiting will NEVER produce
    #: it — this is `eps_fill`'s state, and conflating it with
    #: NOT_YET_OBSERVED would suggest the corpus fills itself over time.
    NOT_AUTHORIZED = "NOT_AUTHORIZED"

    #: The quantity cannot exist for this record at all.
    #:
    #: DEVIATION, DELIBERATE, AND FLAGGED: the milestone brief named five
    #: members and this is a sixth. `NOT_APPLICABLE` is the ONE member
    #: `fills.AbsenceReason` and `social.DeferredState` already agree on
    #: (EVIDENCE-JOIN-CONTRACT-001 §2), and it is the most common absence in
    #: `app/fills/corpus.py`. Omitting it would have forced every adapter to
    #: invent one of the other five for it — inventing information at the
    #: exact seam built to stop that.
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Observation(str, Enum):
    """**What looking achieved.** Independent of why the quantity exists."""

    #: Nobody looked. Says nothing whatsoever about the world.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"

    #: We watched a stated window and the event did not occur. A MEASUREMENT,
    #: and the negative case every lead-lag study needs.
    OBSERVED_NONE = "OBSERVED_NONE"

    #: We looked and there was a value.
    OBSERVED_VALUE = "OBSERVED_VALUE"


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    """The interval a negative observation was measured over.

    Required for ``OBSERVED_NONE``. Both ends are canonical RFC3339 UTC
    strings from OUR clock; the window is a statement about when *we* were
    watching, which is the only window we can honestly assert.

    ``basis`` names the timestamp discipline behind the ends — a window whose
    ends are bare wall clock is a weaker negative than one anchored to a
    monotonic clock, and the difference must be legible downstream rather than
    inferred.
    """

    start_utc: str
    end_utc: str
    basis: str
    #: What was watched: "solana-tx-for-mint", "price-feed", ... Free text is
    #: refused; an unnamed watcher cannot be re-run.
    watcher_id: str

    def __post_init__(self) -> None:
        if not self.start_utc or not self.end_utc:
            raise IllegalMeasurementError(
                "an observation window needs both ends; a negative label with "
                "an open end cannot be compared to anything"
            )
        if not self.watcher_id:
            raise IllegalMeasurementError(
                "an observation window must name the watcher that produced "
                "it, or the negative cannot be reproduced"
            )
        if self.end_utc < self.start_utc:
            raise IllegalMeasurementError(
                f"observation window ends before it starts: "
                f"{self.start_utc} .. {self.end_utc}"
            )

    def to_json(self) -> dict[str, str]:
        return {
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "basis": self.basis,
            "watcher_id": self.watcher_id,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "ObservationWindow":
        return cls(
            start_utc=str(payload["start_utc"]),
            end_utc=str(payload["end_utc"]),
            basis=str(payload["basis"]),
            watcher_id=str(payload["watcher_id"]),
        )


#: Vocabulary identifiers used by :class:`OriginTag`.
FILLS_VOCABULARY = "app.fills.absence.AbsenceReason"
SOCIAL_VOCABULARY = "app.social.artifact.DeferredState"


@dataclass(frozen=True, slots=True)
class OriginTag:
    """The exact source-vocabulary term this measurement came from.

    The two axes are a *projection* of the source vocabularies, and a
    projection loses the terms the target enum does not have (`app/fills`
    `TRANSACTION_FAILED` and `CONFLICTING_SOURCES` have no seam member).
    Carrying the original term makes every adapter lossless by construction
    and satisfies §2's "a joined row carries both vocabularies or neither".
    """

    vocabulary: str
    code: str

    def to_json(self) -> dict[str, str]:
        return {"vocabulary": self.vocabulary, "code": self.code}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "OriginTag":
        return cls(
            vocabulary=str(payload["vocabulary"]), code=str(payload["code"])
        )


@dataclass(frozen=True, slots=True)
class Measurement(Generic[T]):
    """One quantity, on two independent axes.

    Supports no arithmetic, no ordering, and no truthiness. ``bool(m)`` raises
    rather than letting ``m or 0`` fabricate a zero — the same defence
    `app/fills/absence.py` already applies, kept here so that passing through
    the seam never weakens it.
    """

    value: T | None
    availability: Availability
    observation: Observation
    #: Where the claim came from. Mandatory for anything OBSERVED (doctrine 9);
    #: a number without provenance is unrecoverable after the fact.
    source: str | None = None
    #: Required for OBSERVED_NONE. See :class:`ObservationWindow`.
    window: ObservationWindow | None = None
    #: Free-form explanation. Never load-bearing; never parsed.
    detail: str | None = None
    #: The source-vocabulary term, if this came from an adapter.
    origin: OriginTag | None = None

    # -- legality ----------------------------------------------------------

    def __post_init__(self) -> None:
        av, ob = self.availability, self.observation

        if ob is Observation.OBSERVED_VALUE:
            if av is not Availability.AVAILABLE:
                raise IllegalMeasurementError(
                    f"OBSERVED_VALUE requires availability=AVAILABLE, got "
                    f"{av.value}: a value cannot have been observed for a "
                    "quantity we could not have"
                )
            if self.value is None:
                raise IllegalMeasurementError(
                    "OBSERVED_VALUE with value=None is exactly the None->0 "
                    "shape doctrine 10 forbids; use OBSERVED_NONE if the "
                    "event did not occur"
                )
            if not self.source:
                raise IllegalMeasurementError(
                    "OBSERVED_VALUE requires a source (doctrine 9)"
                )
            return

        if ob is Observation.OBSERVED_NONE:
            if av is not Availability.AVAILABLE:
                raise IllegalMeasurementError(
                    f"OBSERVED_NONE requires availability=AVAILABLE, got "
                    f"{av.value}: you cannot have watched a window you could "
                    "not watch"
                )
            if self.value is not None:
                raise IllegalMeasurementError(
                    "OBSERVED_NONE asserts the event did NOT occur; it must "
                    f"not carry a value (got {self.value!r})"
                )
            if self.window is None:
                raise IllegalMeasurementError(
                    "OBSERVED_NONE is a measured negative and requires the "
                    "window it was measured over; without one it is "
                    "indistinguishable from 'nobody looked'"
                )
            if not self.source:
                raise IllegalMeasurementError(
                    "OBSERVED_NONE requires a source (doctrine 9)"
                )
            return

        # NOT_ATTEMPTED
        if self.value is not None:
            raise IllegalMeasurementError(
                "NOT_ATTEMPTED cannot carry a value; nobody looked"
            )
        if self.window is not None:
            raise IllegalMeasurementError(
                "NOT_ATTEMPTED cannot carry an observation window; a window "
                "is the record of having watched"
            )

    # -- constructors ------------------------------------------------------

    @classmethod
    def observed_value(
        cls,
        value: T,
        *,
        source: str,
        detail: str | None = None,
        window: ObservationWindow | None = None,
        origin: OriginTag | None = None,
    ) -> "Measurement[T]":
        return cls(
            value=value,
            availability=Availability.AVAILABLE,
            observation=Observation.OBSERVED_VALUE,
            source=source,
            window=window,
            detail=detail,
            origin=origin,
        )

    @classmethod
    def observed_none(
        cls,
        *,
        source: str,
        window: ObservationWindow,
        detail: str | None = None,
        origin: OriginTag | None = None,
    ) -> "Measurement[T]":
        """The measured negative. THIS is the label a lead-lag study needs."""
        return cls(
            value=None,
            availability=Availability.AVAILABLE,
            observation=Observation.OBSERVED_NONE,
            source=source,
            window=window,
            detail=detail,
            origin=origin,
        )

    @classmethod
    def not_attempted(
        cls,
        availability: Availability,
        *,
        detail: str | None = None,
        source: str | None = None,
        origin: OriginTag | None = None,
    ) -> "Measurement[T]":
        """Nobody looked. ``availability`` still says why (or why not)."""
        return cls(
            value=None,
            availability=availability,
            observation=Observation.NOT_ATTEMPTED,
            source=source,
            detail=detail,
            origin=origin,
        )

    # -- accessors ---------------------------------------------------------

    @property
    def is_observed_value(self) -> bool:
        return self.observation is Observation.OBSERVED_VALUE

    @property
    def is_measured_negative(self) -> bool:
        """True only for the real negative label. **Not** true for absence.

        Any consumer counting negatives must call this and nothing else.
        """
        return self.observation is Observation.OBSERVED_NONE

    @property
    def is_measurement(self) -> bool:
        """True when we actually looked, either way."""
        return self.observation in (
            Observation.OBSERVED_VALUE,
            Observation.OBSERVED_NONE,
        )

    def unwrap(self) -> T:
        if self.observation is Observation.OBSERVED_VALUE:
            assert self.value is not None
            return self.value
        raise MeasurementAbsentError(self)

    def __bool__(self) -> bool:
        raise MeasurementAbsentError(self)

    def with_origin(self, origin: OriginTag) -> "Measurement[T]":
        return replace(self, origin=origin)

    # -- serialisation -----------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "availability": self.availability.value,
            "observation": self.observation.value,
            "value": self.value,
            "source": self.source,
            "window": self.window.to_json() if self.window else None,
            "detail": self.detail,
            "origin": self.origin.to_json() if self.origin else None,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "Measurement":
        window = payload.get("window")
        origin = payload.get("origin")
        return cls(
            value=payload.get("value"),
            availability=Availability(payload["availability"]),
            observation=Observation(payload["observation"]),
            source=payload.get("source"),
            window=ObservationWindow.from_json(window) if window else None,
            detail=payload.get("detail"),
            origin=OriginTag.from_json(origin) if origin else None,
        )


# ---------------------------------------------------------------------------
# adapters — `app/fills`
# ---------------------------------------------------------------------------
#
# The fills vocabulary answers "why can we not have it". It therefore
# DETERMINES `availability` and, for most members, does NOT determine
# `observation`. Where it does not, the caller must say. There is no default.

def _fills_tables():
    from app.fills.absence import AbsenceReason

    availability = {
        AbsenceReason.NOT_PROVIDED: Availability.NOT_PROVIDED,
        AbsenceReason.NOT_APPLICABLE: Availability.NOT_APPLICABLE,
        AbsenceReason.NOT_RECONSTRUCTABLE: Availability.NOT_RECONSTRUCTABLE,
        AbsenceReason.NOT_YET_OBSERVED: Availability.NOT_YET_OBSERVED,
        AbsenceReason.NOT_AUTHORIZED: Availability.NOT_AUTHORIZED,
        # Coarsenings. The exact term survives on `Measurement.origin`, so the
        # inverse adapter reproduces it byte for byte; without that tag these
        # two would be genuinely lossy and would be refused instead.
        AbsenceReason.TRANSACTION_FAILED: Availability.NOT_APPLICABLE,
        AbsenceReason.CONFLICTING_SOURCES: Availability.NOT_RECONSTRUCTABLE,
    }
    # Which members determine the OBSERVATION axis on their own. A member
    # absent from this table leaves `observation` undetermined and the caller
    # MUST supply it. Guessing here is the whole defect the seam prevents.
    observation = {
        # The window has not elapsed, so nothing can have been observed yet.
        AbsenceReason.NOT_YET_OBSERVED: Observation.NOT_ATTEMPTED,
        # We are not permitted to look, so we did not.
        AbsenceReason.NOT_AUTHORIZED: Observation.NOT_ATTEMPTED,
        # The quantity cannot exist, so there was nothing to attempt.
        AbsenceReason.NOT_APPLICABLE: Observation.NOT_ATTEMPTED,
    }
    return availability, observation


class _LazyTable(Mapping):
    """Module-level view of a table that needs a deferred import."""

    def __init__(self, index: int) -> None:
        self._index = index

    def _table(self):
        return _fills_tables()[self._index]

    def __getitem__(self, key):
        return self._table()[key]

    def __iter__(self):
        return iter(self._table())

    def __len__(self):
        return len(self._table())


#: Published so the mapping is inspectable (and testable) as a table, per
#: EVIDENCE-JOIN-CONTRACT-001 §2's "one explicit, lossless mapping declared as
#: a table".
FILLS_AVAILABILITY_TABLE: Mapping = _LazyTable(0)
FILLS_OBSERVATION_TABLE: Mapping = _LazyTable(1)


def from_fills_absence(
    reason,
    *,
    observation: Observation | None = None,
    detail: str | None = None,
) -> Measurement:
    """Adapt a `fills.AbsenceReason` into a :class:`Measurement`.

    ``observation`` is REQUIRED unless the reason determines it (see
    :data:`FILLS_OBSERVATION_TABLE`). The fills vocabulary is silent about
    whether anyone looked; filling that silence with a plausible default is
    precisely the failure this seam exists to prevent.
    """
    availability_table, observation_table = _fills_tables()
    if reason not in availability_table:
        raise UnmappableAbsenceError(
            f"{reason!r} has no image in the seam vocabulary"
        )
    determined = observation_table.get(reason)
    if determined is not None:
        if observation is not None and observation is not determined:
            raise UnmappableAbsenceError(
                f"{reason.value} determines observation="
                f"{determined.value}; caller asserted {observation.value}"
            )
        resolved = determined
    else:
        if observation is None:
            raise UnmappableAbsenceError(
                f"{reason.value} does not determine whether anyone looked; "
                "pass observation= explicitly. The fills vocabulary answers "
                "'why can we not have it', not 'what did looking achieve'"
            )
        resolved = observation

    if resolved is not Observation.NOT_ATTEMPTED:
        raise UnmappableAbsenceError(
            f"an AbsenceReason cannot carry observation={resolved.value}; "
            "an absent fills quantity was never observed"
        )

    return Measurement(
        value=None,
        availability=availability_table[reason],
        observation=resolved,
        detail=detail,
        origin=OriginTag(vocabulary=FILLS_VOCABULARY, code=reason.value),
    )


def from_fills_maybe(maybe, *, observation: Observation | None = None) -> Measurement:
    """Adapt `fills.Maybe` (``Observed`` | ``Absent``) into a Measurement."""
    from app.fills.absence import Observed

    if isinstance(maybe, Observed):
        return Measurement.observed_value(maybe.value, source=maybe.source)
    return from_fills_absence(
        maybe.reason, observation=observation, detail=maybe.detail
    )


def to_fills_maybe(measurement: Measurement):
    """Inverse adapter. Exact for anything this module produced.

    Refuses a measured negative: `app/fills` has no way to say "we watched and
    it did not happen", so writing one back would silently become
    "NOT_PROVIDED" — the §2 collapse.
    """
    from app.fills.absence import AbsenceReason, Absent, Observed

    if measurement.observation is Observation.OBSERVED_VALUE:
        return Observed(
            value=measurement.unwrap(),
            source=measurement.source or "seam",
        )
    if measurement.observation is Observation.OBSERVED_NONE:
        raise UnmappableAbsenceError(
            "app.fills.AbsenceReason cannot express a MEASURED NEGATIVE; "
            "writing OBSERVED_NONE back would become NOT_PROVIDED, which is "
            "EVIDENCE-JOIN-CONTRACT-001 §2's forbidden collapse"
        )
    origin = measurement.origin
    if origin is not None and origin.vocabulary == FILLS_VOCABULARY:
        return Absent(
            reason=AbsenceReason(origin.code), detail=measurement.detail
        )
    if origin is not None:
        raise UnmappableAbsenceError(
            f"measurement originated in {origin.vocabulary}; converting it "
            "into a fills AbsenceReason would assert a provenance it does "
            "not have"
        )
    reverse = {
        Availability.NOT_PROVIDED: AbsenceReason.NOT_PROVIDED,
        Availability.NOT_APPLICABLE: AbsenceReason.NOT_APPLICABLE,
        Availability.NOT_RECONSTRUCTABLE: AbsenceReason.NOT_RECONSTRUCTABLE,
        Availability.NOT_YET_OBSERVED: AbsenceReason.NOT_YET_OBSERVED,
        Availability.NOT_AUTHORIZED: AbsenceReason.NOT_AUTHORIZED,
    }
    if measurement.availability not in reverse:
        raise UnmappableAbsenceError(
            f"availability={measurement.availability.value} has no "
            "AbsenceReason"
        )
    return Absent(
        reason=reverse[measurement.availability], detail=measurement.detail
    )


# ---------------------------------------------------------------------------
# adapters — `app/social`
# ---------------------------------------------------------------------------
#
# The social vocabulary answers "what did looking achieve". It therefore
# DETERMINES `observation` and, for `ABSENT`, does NOT determine
# `availability` — "nobody looked" says nothing about whether they could have.

def _social_tables():
    from app.social.artifact import DeferredState

    observation = {
        DeferredState.ABSENT: Observation.NOT_ATTEMPTED,
        DeferredState.OBSERVED: Observation.OBSERVED_VALUE,
        DeferredState.OBSERVED_NONE: Observation.OBSERVED_NONE,
        DeferredState.NOT_APPLICABLE: Observation.NOT_ATTEMPTED,
    }
    availability = {
        DeferredState.OBSERVED: Availability.AVAILABLE,
        DeferredState.OBSERVED_NONE: Availability.AVAILABLE,
        DeferredState.NOT_APPLICABLE: Availability.NOT_APPLICABLE,
        # DeferredState.ABSENT is deliberately absent from this table.
    }
    return availability, observation


class _LazySocialTable(_LazyTable):
    def _table(self):
        return _social_tables()[self._index]


SOCIAL_AVAILABILITY_TABLE: Mapping = _LazySocialTable(0)
SOCIAL_OBSERVATION_TABLE: Mapping = _LazySocialTable(1)


def from_social_deferred(
    deferred,
    *,
    availability: Availability | None = None,
    window: ObservationWindow | None = None,
    source: str | None = None,
) -> Measurement:
    """Adapt `social.Deferred` into a :class:`Measurement`.

    ``availability`` is REQUIRED for ``ABSENT``: the social vocabulary records
    that nobody looked and is silent about whether anybody could have.

    ``window`` is REQUIRED for ``OBSERVED_NONE``. **This is a real gap in
    `app/social` today**, recorded in the milestone doc: ``Deferred`` demands
    an ``observed_at`` instant but no window, so the tape cannot currently say
    how long it watched. The seam refuses to invent one.
    """
    from app.social.artifact import DeferredState

    availability_table, observation_table = _social_tables()
    state = deferred.state
    ob = observation_table[state]
    origin = OriginTag(vocabulary=SOCIAL_VOCABULARY, code=state.value)
    resolved_source = source or f"social.Deferred@{deferred.observed_at}"

    if state is DeferredState.ABSENT:
        if availability is None:
            raise UnmappableAbsenceError(
                "DeferredState.ABSENT means nobody looked; it does not say "
                "whether the quantity was obtainable. Pass availability= "
                "explicitly rather than defaulting to AVAILABLE (which would "
                "manufacture a watched window) or NOT_PROVIDED (which would "
                "manufacture a dead source)"
            )
        return Measurement.not_attempted(
            availability, detail="nobody looked", origin=origin
        )

    if availability is not None and availability is not availability_table[state]:
        raise UnmappableAbsenceError(
            f"{state.value} determines availability="
            f"{availability_table[state].value}; caller asserted "
            f"{availability.value}"
        )

    if state is DeferredState.NOT_APPLICABLE:
        return Measurement.not_attempted(
            Availability.NOT_APPLICABLE,
            detail="the question does not apply to this record",
            origin=origin,
        )

    if state is DeferredState.OBSERVED_NONE:
        if window is None:
            raise UnmappableAbsenceError(
                "OBSERVED_NONE is a measured negative and the seam requires "
                "the window it was measured over; app.social.Deferred does "
                "not carry one, so the caller must supply it"
            )
        return Measurement.observed_none(
            source=resolved_source,
            window=window,
            detail="watched the window; the event did not occur",
            origin=origin,
        )

    return Measurement.observed_value(
        dict(deferred.detail) if deferred.detail is not None else {},
        source=resolved_source,
        window=window,
        origin=origin,
    )


def to_social_deferred(measurement: Measurement, *, observed_at: str | None = None):
    """Inverse adapter. Refuses where `app/social` cannot express the state."""
    from app.social.artifact import Deferred, DeferredState

    origin = measurement.origin
    if origin is not None and origin.vocabulary not in (SOCIAL_VOCABULARY,):
        raise UnmappableAbsenceError(
            f"measurement originated in {origin.vocabulary}; converting it "
            "into a social DeferredState would assert a provenance it does "
            "not have"
        )

    ob = measurement.observation
    if ob is Observation.NOT_ATTEMPTED:
        if measurement.availability is Availability.NOT_APPLICABLE:
            stamp = observed_at or (
                measurement.window.end_utc if measurement.window else None
            )
            if stamp is None:
                raise UnmappableAbsenceError(
                    "social NOT_APPLICABLE requires observed_at; pass one"
                )
            return Deferred(
                state=DeferredState.NOT_APPLICABLE, observed_at=stamp
            )
        if measurement.availability is not Availability.AVAILABLE and origin is None:
            # `app/social` has exactly one way to say "nobody looked", so the
            # availability distinction is LOST going this direction. Refuse
            # rather than drop it silently.
            raise UnmappableAbsenceError(
                f"app.social.DeferredState cannot record availability="
                f"{measurement.availability.value}; it has only ABSENT, "
                "which means 'nobody looked'. The distinction would be "
                "dropped at the seam"
            )
        return Deferred(state=DeferredState.ABSENT)

    stamp = observed_at or (
        measurement.window.end_utc if measurement.window else None
    )
    if stamp is None:
        raise UnmappableAbsenceError(
            "a social observation is a claim about a moment and requires "
            "observed_at"
        )
    if ob is Observation.OBSERVED_NONE:
        return Deferred(state=DeferredState.OBSERVED_NONE, observed_at=stamp)
    detail = measurement.value
    if not isinstance(detail, Mapping):
        detail = {"value": detail}
    return Deferred(
        state=DeferredState.OBSERVED, observed_at=stamp, detail=dict(detail)
    )
