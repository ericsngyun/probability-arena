"""Typed absence for the realized-fill corpus (AGENTS.md doctrine 10).

> **Never encode epistemic absence as a numerical market state.**
> `None -> 0` is dangerous anywhere the zero has economic meaning.

For a fill record the zero always has economic meaning:

| | means |
|---|---|
| `tip = 0` | we inspected the transaction and it paid no MEV tip |
| `tip = unknown` | we could not determine whether it paid one |
| `markout_5m = 0.0` | the price 5 minutes after the fill equalled the fill price |
| `markout_5m = unknown` | 5 minutes have not elapsed, or we have no price there |

Collapsing those fabricates a cost basis, which is the one thing this corpus
exists to get right. So absence is **structural**, not a convention: a quantity
is either `Observed(value, source)` or `Absent(reason)`, and `Absent` supports
no arithmetic at all. Code that wants a number must ask for one explicitly and
handle the refusal.

The reason codes are a **closed set**. "Unknown" with free text is how absence
silently becomes a benign default again.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class AbsenceReason(str, Enum):
    """Closed set. Every member answers a different question, and the
    difference between them is load-bearing for the corpus."""

    #: The source was consulted and returned nothing for this field. The
    #: canonical case: an RPC `meta` object that omits `preTokenBalances`.
    NOT_PROVIDED = "not_provided"

    #: The quantity cannot exist for this record. A single-leg direct route has
    #: no intermediate pool; a transaction with no transfer to a known tip
    #: account has no tip *destination*, which is different from a zero tip.
    NOT_APPLICABLE = "not_applicable"

    #: The source spoke, but the quantity is not derivable from what it said.
    #: The canonical case: separating priority fee from base fee when the
    #: compute-budget instruction is not visible because the transaction used
    #: an address lookup table we did not resolve.
    NOT_RECONSTRUCTABLE = "not_reconstructable"

    #: The observation window has not elapsed. `markout_5m` on a fill that
    #: confirmed 40 seconds ago. This is the reason that most often gets
    #: silently written as 0.0.
    NOT_YET_OBSERVED = "not_yet_observed"

    #: The measurement requires an authorization we do not have. `eps_fill`
    #: over our OWN fills requires capital-funded calibration trades that are
    #: not authorized (see the milestone doc §9). Distinguishing this from
    #: NOT_YET_OBSERVED matters: waiting will never produce it.
    NOT_AUTHORIZED = "not_authorized"

    #: The transaction failed on chain. Its "actual output" is not zero and it
    #: is not missing — it does not exist, while its fee very much does.
    TRANSACTION_FAILED = "transaction_failed"

    #: Two sources disagreed and we refuse to pick one. A corpus that resolves
    #: contradictions by preferring the convenient side is worse than one that
    #: reports the contradiction.
    CONFLICTING_SOURCES = "conflicting_sources"


@dataclass(frozen=True, slots=True)
class Observed(Generic[T]):
    """A value that was actually observed, carrying where it came from.

    `source` is mandatory. A number without a provenance is the failure mode
    doctrine 9 exists to prevent, and in a cost basis it is unrecoverable after
    the fact.
    """

    value: T
    source: str

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Observed requires a non-empty source")

    @property
    def is_observed(self) -> bool:
        return True

    def unwrap(self) -> T:
        return self.value


@dataclass(frozen=True, slots=True)
class Absent:
    """A quantity that is not known, and why.

    Deliberately supports no arithmetic, no ordering and no implicit bool that
    could be confused with a zero. `bool(Absent(...))` is False only in the
    sense that every non-empty object is True — so we make it explicit and
    raise rather than let `x or 0` quietly work.
    """

    reason: AbsenceReason
    detail: str | None = None

    @property
    def is_observed(self) -> bool:
        return False

    def unwrap(self):
        raise AbsenceError(self.reason, self.detail)

    def __bool__(self) -> bool:  # pragma: no cover - defensive
        raise AbsenceError(
            self.reason,
            "Absent has no truth value; `x or 0` would fabricate a zero",
        )


class AbsenceError(RuntimeError):
    """Raised when code demands a value that was never observed."""

    def __init__(self, reason: AbsenceReason, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


#: A quantity that may or may not be known. Union, not Optional: `None` is
#: exactly the encoding this module exists to replace.
Maybe = Observed[T] | Absent


def observed(value: T, source: str) -> Observed[T]:
    return Observed(value=value, source=source)


def absent(reason: AbsenceReason, detail: str | None = None) -> Absent:
    return Absent(reason=reason, detail=detail)


def is_observed(m: Maybe[T]) -> bool:
    return isinstance(m, Observed)


def value_or(m: Maybe[T], default: T) -> T:
    """Explicit, greppable defaulting. Use only where the default is a
    *presentation* choice, never where the number enters a cost basis."""
    return m.value if isinstance(m, Observed) else default


def require(m: Maybe[T], what: str) -> T:
    """Demand a value. Raises `AbsenceError` naming the field, so a corrupted
    cost basis surfaces as a loud failure instead of a plausible number."""
    if isinstance(m, Observed):
        return m.value
    raise AbsenceError(m.reason, f"{what}: {m.detail}" if m.detail else what)


def combine(*terms: Maybe[Decimal], source: str) -> Maybe[Decimal]:
    """Sum terms, propagating absence.

    **A sum containing an unknown term is unknown, not the sum of the known
    ones.** This is the single most important function in the module: total
    cost = network fee + priority fee + tip, and quietly treating an
    unobserved tip as zero understates the cost basis by exactly the amount
    that matters most in a competitive block.
    """
    total = Decimal(0)
    for term in terms:
        if not isinstance(term, Observed):
            return Absent(
                reason=term.reason,
                detail=f"sum is unknown because a term is {term.reason.value}"
                + (f" ({term.detail})" if term.detail else ""),
            )
        total += term.value
    return Observed(value=total, source=source)


def as_json(m: Maybe) -> dict:
    """Serialize preserving the distinction. A corpus row written to disk must
    still be able to say `not_yet_observed` when it is re-read."""
    if isinstance(m, Observed):
        value = m.value
        if isinstance(value, Decimal):
            value = str(value)
        return {"observed": True, "value": value, "source": m.source}
    return {"observed": False, "reason": m.reason.value, "detail": m.detail}
