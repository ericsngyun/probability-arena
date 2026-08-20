"""SOCIAL-TAPE-001 — deduplication and propagation identity.

The single most destructive mistake available in this milestone is to treat
these two situations as the same thing:

    (a) the stream handed us the same post twice
    (b) the same content was posted again, by someone else, later

(a) is a transport artefact and carries no information about the world.
(b) IS the world — it is the diffusion curve, and it is the entire object of
any future lead-lag study. A naive ``seen_ids`` set collapses them, and the
collapse is invisible: the tape simply contains fewer records, all of them
plausible.

Three identities, three verdicts
--------------------------------
``delivery_identity``  (platform, source_id, message_id, generation, seq)
``message_identity``   (platform, source_id, message_id)
``content_identity``   sha256 of whitespace-normalised text

    same delivery_identity  -> REDELIVERY. Transport noise. Recorded as a
                               distinct tape record kind so the redelivery
                               RATE stays measurable, never dropped silently.
    same message_identity,  -> RESTREAM. The stream re-sent a post we already
    new delivery_identity      have, e.g. after a reconnect/backfill. Also
                               transport, but a different mechanism, and worth
                               telling apart from a within-connection dupe.
    same message_identity,  -> REVISION. The platform changed a post under a
    different content          stable id. Never overwrite: record both.
    new message_identity,   -> PROPAGATION. Someone spread it. A first-class
    same content_identity      EVENT, always kept.
    everything new          -> NOVEL.

Note what is NOT here: no ranking of propagation events, no "virality" number,
no author weighting. The ledger reports what kind of thing happened. Deciding
whether that matters is a later, separately preregistered milestone.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from app.social.artifact import SocialArtifact

__all__ = [
    "DedupeVerdict",
    "DedupeDecision",
    "PropagationLedger",
]


class DedupeVerdict(str, Enum):
    """What kind of re-appearance this is. Never a bare boolean."""

    #: Never seen in any sense.
    NOVEL = "NOVEL"
    #: Byte-identical delivery we have already accepted, same connection.
    REDELIVERY = "REDELIVERY"
    #: Same post, delivered again on a later delivery/generation.
    RESTREAM = "RESTREAM"
    #: Same message id, different content. The platform edited it.
    REVISION = "REVISION"
    #: New post carrying content we have seen before. THIS IS AN EVENT.
    PROPAGATION = "PROPAGATION"


@dataclass(frozen=True)
class DedupeDecision:
    """The verdict plus the evidence for it."""

    verdict: DedupeVerdict
    #: The message identity this one echoes, when there is one.
    first_seen_message_identity: tuple[str, str, str] | None = None
    #: Our receipt time of the FIRST artifact carrying this content. The basis
    #: of any diffusion measurement, and our clock, not the platform's.
    first_seen_our_received_at: str | None = None
    #: How many distinct messages have carried this content before this one.
    prior_distinct_messages: int = 0

    @property
    def is_transport_duplicate(self) -> bool:
        """True only for the two transport verdicts.

        A caller may skip *analysis* of these. It must still write them to the
        tape: a redelivery rate that is never recorded cannot later be
        distinguished from a delivery gap.
        """

        return self.verdict in {DedupeVerdict.REDELIVERY, DedupeVerdict.RESTREAM}

    @property
    def is_spread(self) -> bool:
        return self.verdict is DedupeVerdict.PROPAGATION


@dataclass
class _ContentRecord:
    first_message_identity: tuple[str, str, str]
    first_our_received_at: str
    distinct_messages: int


class PropagationLedger:
    """Bounded-memory identity ledger.

    Bounded because a collector runs for months and an unbounded set is an
    unbounded memory leak. The bound is explicit and its EVICTION is visible:
    :attr:`evicted_content_keys` counts how many content identities have aged
    out, so a "PROPAGATION rate fell" observation can be checked against "the
    ledger forgot" before it is believed.

    That counter is the positive control this class exists to make possible
    (doctrine 7): force an eviction, and the metric must become non-benign.
    """

    def __init__(self, *, capacity: int = 200_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._deliveries: OrderedDict[tuple, None] = OrderedDict()
        self._messages: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._contents: OrderedDict[str, _ContentRecord] = OrderedDict()
        self.evicted_content_keys = 0
        self.evicted_message_keys = 0
        self.evicted_delivery_keys = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def classify(self, artifact: SocialArtifact) -> DedupeDecision:
        """Classify WITHOUT recording. Pure; safe to call twice."""

        if artifact.delivery_identity in self._deliveries:
            return DedupeDecision(
                verdict=DedupeVerdict.REDELIVERY,
                first_seen_message_identity=artifact.message_identity,
            )

        content_key = artifact.content_identity
        prior_content = self._contents.get(content_key)
        known_content_hash = self._messages.get(artifact.message_identity)

        if known_content_hash is not None:
            if known_content_hash != content_key:
                return DedupeDecision(
                    verdict=DedupeVerdict.REVISION,
                    first_seen_message_identity=artifact.message_identity,
                    first_seen_our_received_at=(
                        prior_content.first_our_received_at
                        if prior_content
                        else None
                    ),
                )
            return DedupeDecision(
                verdict=DedupeVerdict.RESTREAM,
                first_seen_message_identity=artifact.message_identity,
                first_seen_our_received_at=(
                    prior_content.first_our_received_at if prior_content else None
                ),
            )

        if prior_content is not None:
            return DedupeDecision(
                verdict=DedupeVerdict.PROPAGATION,
                first_seen_message_identity=prior_content.first_message_identity,
                first_seen_our_received_at=prior_content.first_our_received_at,
                prior_distinct_messages=prior_content.distinct_messages,
            )

        return DedupeDecision(verdict=DedupeVerdict.NOVEL)

    def record(self, artifact: SocialArtifact) -> DedupeDecision:
        """Classify and then remember. Returns the decision made BEFORE the
        artifact was remembered, so the verdict describes the arrival."""

        decision = self.classify(artifact)

        self._deliveries[artifact.delivery_identity] = None
        self._trim(self._deliveries, "delivery")

        content_key = artifact.content_identity
        self._messages[artifact.message_identity] = content_key
        self._trim(self._messages, "message")

        existing = self._contents.get(content_key)
        if existing is None:
            self._contents[content_key] = _ContentRecord(
                first_message_identity=artifact.message_identity,
                first_our_received_at=artifact.our_received_at.value,
                distinct_messages=1,
            )
        elif decision.verdict is DedupeVerdict.PROPAGATION:
            existing.distinct_messages += 1
            self._contents.move_to_end(content_key)
        self._trim(self._contents, "content")

        return decision

    def _trim(self, store: OrderedDict, kind: str) -> None:
        while len(store) > self._capacity:
            store.popitem(last=False)
            if kind == "content":
                self.evicted_content_keys += 1
            elif kind == "message":
                self.evicted_message_keys += 1
            else:
                self.evicted_delivery_keys += 1

    def counters(self) -> dict[str, int]:
        """Health counters. Eviction counts are NOT hidden."""

        return {
            "tracked_deliveries": len(self._deliveries),
            "tracked_messages": len(self._messages),
            "tracked_contents": len(self._contents),
            "evicted_delivery_keys": self.evicted_delivery_keys,
            "evicted_message_keys": self.evicted_message_keys,
            "evicted_content_keys": self.evicted_content_keys,
            "capacity": self._capacity,
        }

    def prime(self, artifacts: Iterable[SocialArtifact]) -> None:
        """Rebuild ledger state from tape, e.g. after a restart."""

        for artifact in artifacts:
            self.record(artifact)
