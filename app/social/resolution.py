"""SOCIAL-TAPE-001 — entity/mint resolution: an interface and a dull default.

The task is: given the text of a post, decide which on-chain entity (if any) it
names. That is a hard problem, and a clever resolver built at collection time
would be the worst possible place to solve it, because:

  * a resolver's mistakes become permanent facts on an immutable tape;
  * a resolver tuned against observed outcomes is a model, and a model that
    runs inside the collector has silently turned the tape into its own
    training artefact;
  * and a resolver that improves later cannot re-resolve history unless the
    raw bytes were preserved verbatim — which they are, precisely so that
    resolution can be redone.

So the default implementation here does exactly one conservative thing:
extract base58 strings of plausible mint length, and report them as
``CANDIDATE`` — never ``CONFIRMED``, because nothing authoritative is
consulted. Multiple distinct candidates yield ``AMBIGUOUS``, not "the first
one". Zero candidates yield ``UNRESOLVED``.

CONTAINS NO SIGNAL. Resolution says which entity is named. It says nothing
about whether that matters.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Protocol, runtime_checkable

from app.realtime.book import utcnow
from app.realtime.canonical import canonical_datetime
from app.social.artifact import EntityResolution, ResolutionConfidence

__all__ = [
    "EntityResolver",
    "ConservativeAddressResolver",
    "NullResolver",
    "BASE58_ALPHABET",
]

#: Base58 as used by Solana addresses: no 0, O, I, l.
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: 32-byte base58 encodes to 32–44 characters. Bounded on BOTH sides: an
#: unbounded pattern matches long words, hashes, and URL fragments, and every
#: one of those would be recorded as a candidate entity.
_ADDRESS_RE = re.compile(rf"(?<![{BASE58_ALPHABET}])([{BASE58_ALPHABET}]{{32,44}})(?![{BASE58_ALPHABET}])")

#: Strings that match the address shape but are famously not user-supplied
#: mints. Kept tiny and explicit; a growing denylist here would be the start of
#: exactly the cleverness this module refuses to build.
_KNOWN_NON_MINTS = frozenset(
    {
        "11111111111111111111111111111111",  # Solana system program
    }
)


@runtime_checkable
class EntityResolver(Protocol):
    """The seam. Typed, narrow, no varargs (doctrine 6)."""

    @property
    def resolver_id(self) -> str:
        """Stable identity, written onto every record this resolver produced.

        Required so that a later, better resolver's output is distinguishable
        from this one's rather than silently replacing it.
        """

    def resolve(self, text: str) -> EntityResolution:
        """Return a typed resolution. MUST NOT raise on ordinary input."""


class ConservativeAddressResolver:
    """Extract address-shaped strings. Confirm nothing.

    Ceiling is :data:`ResolutionConfidence.CANDIDATE`. Reaching ``CONFIRMED``
    requires consulting an authoritative registry, which is a network call,
    which is a cost, which is a separately authorized milestone.
    """

    resolver_id = "conservative-base58.v1"

    def __init__(self, *, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    def resolve(self, text: str) -> EntityResolution:
        stamp = canonical_datetime(self._now())
        found: list[str] = []
        for match in _ADDRESS_RE.finditer(text or ""):
            token = match.group(1)
            if token in _KNOWN_NON_MINTS:
                continue
            if token not in found:
                found.append(token)

        if not found:
            return EntityResolution(
                confidence=ResolutionConfidence.UNRESOLVED,
                resolver_id=self.resolver_id,
                first_entity_resolution_at=stamp,
            )
        if len(found) > 1:
            # Explicitly NOT "pick the first". A post naming two addresses is
            # ambiguous, and collapsing it would fabricate an attribution that
            # no later reader could detect.
            return EntityResolution(
                confidence=ResolutionConfidence.AMBIGUOUS,
                candidates=tuple(found),
                resolver_id=self.resolver_id,
                first_entity_resolution_at=stamp,
            )
        return EntityResolution(
            confidence=ResolutionConfidence.CANDIDATE,
            resolved_mint=found[0],
            candidates=tuple(found),
            resolver_id=self.resolver_id,
            first_entity_resolution_at=stamp,
        )


class NullResolver:
    """Resolves nothing, on purpose.

    The right default when the collector's job is only to preserve bytes:
    resolution can always be redone from the tape, so declining to do it at
    capture time costs nothing and forecloses nothing.
    """

    resolver_id = "null.v1"

    def resolve(self, text: str) -> EntityResolution:
        return EntityResolution.unresolved(resolver_id=self.resolver_id)
