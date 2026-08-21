"""``delivery_mode`` as a BINDING cohort dimension.

EVIDENCE-JOIN-CONTRACT-001 §5. `SOCIAL-TAPE-001` records `delivery_mode` on
every artifact and notes that **nothing forces a consumer to condition on
it**. This module is where that obligation lands.

    `t_received − t_created` on a BACKFILL artifact is an honest number about
    a dishonest thing: it measures how long WE were absent, not how long the
    platform took. Pool it with LIVE and the latency distribution grows a long
    right tail that is a picture of our own downtime.

So:

* the **primary social-alpha cohort is LIVE only** — asserted structurally by
  :class:`LiveLeadLagCohort`, which cannot be constructed containing anything
  else;
* `BACKFILL` / `PULLED` / `UNKNOWN` remain useful and are NOT discarded —
  source reputation, semantic analysis, propagation reconstruction, and
  training resolvers all want them;
* **pooling is an error, not a convention**. :meth:`DeliveryCohort.pool`
  raises. There is no flag that turns it into a warning.

CONTAINS NO SIGNAL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, TypeVar

from app.social.artifact import DeliveryMode

__all__ = [
    "CohortError",
    "CohortPoolingError",
    "CohortPurposeViolation",
    "CohortPurpose",
    "DeliveryCohort",
    "LiveLeadLagCohort",
    "PRIMARY_ALPHA_DELIVERY_MODE",
    "LATENCY_SAFE_MODES",
    "partition_by_delivery_mode",
    "delivery_mode_breakdown",
]

T = TypeVar("T")

#: The primary social-alpha cohort. One mode. Widening this is a milestone
#: decision, and the positive controls assert it.
PRIMARY_ALPHA_DELIVERY_MODE = DeliveryMode.LIVE

#: The only modes whose `our_received_at` may enter a latency-based figure.
#: `UNKNOWN` is NOT optimistically treated as live — `SOCIAL-TAPE-001` is
#: explicit that the transport could not say.
LATENCY_SAFE_MODES = frozenset({DeliveryMode.LIVE})


class CohortError(Exception):
    """A cohort rule was violated."""


class CohortPoolingError(CohortError):
    """Two delivery modes were about to be pooled. Always fatal.

    There is deliberately no `force=True`, no `allow_pooling` setting and no
    environment variable. A pooled delivery-timing figure is not a worse
    measurement, it is a different measurement wearing the right name.
    """


class CohortPurposeViolation(CohortError):
    """A cohort was used for a purpose its delivery mode cannot support."""


class CohortPurpose(str, Enum):
    """What a cohort is allowed to be used for.

    The distinction is not bureaucratic: it is the whole §5 finding. Backfill
    is fine for *what was said and by whom*, and disqualifying for *when we
    could have known*.
    """

    #: Anything measuring an interval that involves our receipt time.
    LATENCY_LEAD_LAG = "LATENCY_LEAD_LAG"
    #: Who said it, and how good they have been. Doctrine 16.
    SOURCE_REPUTATION = "SOURCE_REPUTATION"
    #: What was said. Text, entities, sentiment.
    SEMANTIC_ANALYSIS = "SEMANTIC_ANALYSIS"
    #: The diffusion curve: who rebroadcast what, in what order.
    PROPAGATION_RECONSTRUCTION = "PROPAGATION_RECONSTRUCTION"
    #: Training or evaluating an entity resolver.
    RESOLVER_TRAINING = "RESOLVER_TRAINING"


#: Which purposes each delivery mode may serve. LATENCY_LEAD_LAG is the only
#: restricted one, and it is restricted to LIVE.
_PERMITTED_PURPOSES: dict[DeliveryMode, frozenset] = {
    DeliveryMode.LIVE: frozenset(CohortPurpose),
    DeliveryMode.BACKFILL: frozenset(CohortPurpose) - {CohortPurpose.LATENCY_LEAD_LAG},
    DeliveryMode.PULLED: frozenset(CohortPurpose) - {CohortPurpose.LATENCY_LEAD_LAG},
    DeliveryMode.UNKNOWN: frozenset(CohortPurpose) - {CohortPurpose.LATENCY_LEAD_LAG},
}


@dataclass(frozen=True)
class DeliveryCohort:
    """A set of artifacts sharing ONE delivery mode.

    Construction refuses a mixed set, so a cohort cannot become impure after
    the fact. Cohorts are combined only by explicitly asking for a breakdown
    (:func:`delivery_mode_breakdown`), never by adding them together.
    """

    delivery_mode: DeliveryMode
    members: tuple[Any, ...]
    label: str = ""

    def __post_init__(self) -> None:
        for member in self.members:
            mode = getattr(member, "delivery_mode", None)
            if mode is None:
                raise CohortError(
                    "every cohort member must carry a delivery_mode; an "
                    "artifact without one cannot be conditioned on"
                )
            if mode is not self.delivery_mode:
                raise CohortPoolingError(
                    f"cohort is {self.delivery_mode.value} but a member is "
                    f"{mode.value}; pooling delivery modes puts a fabricated "
                    "tail on every latency figure computed from this cohort"
                )

    def __len__(self) -> int:
        return len(self.members)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.members)

    def assert_purpose(self, purpose: "CohortPurpose") -> None:
        """Refuse a use this cohort's delivery mode cannot support."""
        permitted = _PERMITTED_PURPOSES.get(self.delivery_mode, frozenset())
        if purpose not in permitted:
            raise CohortPurposeViolation(
                f"a {self.delivery_mode.value} cohort may not be used for "
                f"{purpose.value}: `our_received_at` on a "
                f"{self.delivery_mode.value} artifact is honest but is NOT "
                "live delivery timing, so the resulting distribution measures "
                "our own downtime"
            )

    def pool(self, other: "DeliveryCohort") -> "DeliveryCohort":
        """Always raises. Present so the attempt has a name and a message."""
        raise CohortPoolingError(
            f"refusing to pool a {self.delivery_mode.value} cohort with a "
            f"{other.delivery_mode.value} one. If you want both, ask for a "
            "delivery_mode BREAKDOWN — EVIDENCE-JOIN-CONTRACT-001 §5: no "
            "pooled delivery-timing figure may be reported without one"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "delivery_mode": self.delivery_mode.value,
            "size": len(self.members),
            "label": self.label,
        }


class LiveLeadLagCohort(DeliveryCohort):
    """The PRIMARY social-alpha cohort. LIVE only, by construction.

    A backfilled artifact cannot enter this type — not by flag, not by
    override, not by a later mutation, because the check runs in
    ``__post_init__`` and the record is frozen.
    """

    def __init__(self, members: Iterable[Any], label: str = "primary-alpha"):
        super().__init__(
            delivery_mode=PRIMARY_ALPHA_DELIVERY_MODE,
            members=tuple(members),
            label=label,
        )

    def __post_init__(self) -> None:
        if self.delivery_mode is not PRIMARY_ALPHA_DELIVERY_MODE:
            raise CohortPoolingError(
                "the primary lead-lag cohort is "
                f"{PRIMARY_ALPHA_DELIVERY_MODE.value} only"
            )
        super().__post_init__()
        self.assert_purpose(CohortPurpose.LATENCY_LEAD_LAG)


def partition_by_delivery_mode(
    artifacts: Iterable[Any],
) -> dict[DeliveryMode, DeliveryCohort]:
    """Split into pure cohorts. The only sanctioned way to hold a mixed set."""
    buckets: dict[DeliveryMode, list[Any]] = {}
    for artifact in artifacts:
        mode = getattr(artifact, "delivery_mode", None)
        if mode is None:
            raise CohortError(
                "cannot partition an artifact with no delivery_mode"
            )
        buckets.setdefault(mode, []).append(artifact)
    return {
        mode: DeliveryCohort(delivery_mode=mode, members=tuple(items))
        for mode, items in buckets.items()
    }


def delivery_mode_breakdown(
    artifacts: Iterable[Any],
) -> Mapping[str, int]:
    """The breakdown §5 requires beside any pooled delivery-timing figure."""
    counts: dict[str, int] = {}
    for artifact in artifacts:
        mode = getattr(artifact, "delivery_mode", None)
        key = mode.value if mode is not None else "MISSING"
        counts[key] = counts.get(key, 0) + 1
    return counts
