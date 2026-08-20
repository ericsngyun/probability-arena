"""SOCIAL-TAPE-001 — the collected artifact schema.

One record per item we saw. Frozen, JSON round-trippable, digest-stable.

Three design rules, each earned elsewhere in this repo:

1. **Never encode epistemic absence as a value** (AGENTS.md doctrine 10). The
   later-populated fields — ``first_onchain_reaction``, ``first_price_reaction``
   — are structurally present and typed :data:`DeferredState.ABSENT` from the
   moment of ingestion. "We have not looked yet" is not ``None``, is not
   ``0``, and is not "no reaction".

2. **A field name is not evidence of its semantics** (doctrine 8). Every
   timestamp records which platform field supplied it and whether that field's
   behaviour has been empirically verified.

3. **Seeing something twice is not the same as it spreading.** See
   :class:`PropagationKind` and `app.social.dedupe`.

CONTAINS NO SIGNAL. Nothing here ranks, scores, or predicts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from app.social.timebase import (
    ClockConfusionError,
    DeliveryOffset,
    OurReceivedAt,
    SourceCreatedAt,
)

__all__ = [
    "INGESTION_VERSION",
    "KNOWN_INGESTION_VERSIONS",
    "Platform",
    "PropagationKind",
    "DeliveryMode",
    "ParentRef",
    "MediaRef",
    "ResolutionConfidence",
    "EntityResolution",
    "DeferredState",
    "Deferred",
    "SocialArtifact",
    "ArtifactSchemaError",
    "content_identity",
    "raw_content_digest",
]

#: Bumped whenever the meaning of any field changes. Written onto every record
#: so a replay reader never has to guess which parser produced it.
INGESTION_VERSION = "social-tape-001.v1"

#: Every version this reader understands. A record minted by an older ingestion
#: is READABLE (the tape is immutable, so old records must stay legible) but is
#: never MINTED — see `SocialArtifact.__post_init__`.
KNOWN_INGESTION_VERSIONS = frozenset({"social-tape-001.v1"})


class ArtifactSchemaError(Exception):
    """A record was asked to exist in a state the schema forbids."""


class Platform(str, Enum):
    """Closed set. A new platform is a schema change, not a free string."""

    X = "X"
    TELEGRAM = "TELEGRAM"
    DISCORD = "DISCORD"


class DeliveryMode(str, Enum):
    """HOW this item reached us — the field that protects `our_received_at`.

    ``our_received_at`` means "when our process first held the bytes". That is
    only a statement about *information arrival* when the bytes arrived because
    the platform pushed them live. A backfilled or polled item also has an
    honest ``our_received_at``, and pooling the two produces a delivery-latency
    distribution with a fabricated tail — the exact shape of finding that "we
    learn about posts 40 minutes late", when in truth 3% of them were
    recovered after an outage.

    So the mode is a REQUIRED field with no default. Every consumer must
    condition on it, and no consumer can forget it exists.
    """

    #: Pushed to us by the live stream while we were connected.
    LIVE = "LIVE"
    #: Re-sent by the platform to cover a gap we were absent for. Delivery
    #: timing is NOT live timing.
    BACKFILL = "BACKFILL"
    #: Fetched by us on purpose, e.g. hydrating a referenced parent post.
    PULLED = "PULLED"
    #: The transport could not say. Recorded, never guessed as LIVE.
    UNKNOWN = "UNKNOWN"


class PropagationKind(str, Enum):
    """How this item relates to content that already existed.

    This is the axis that must never be collapsed into "duplicate". A retweet
    is a *new observation about the world* — someone with a different audience
    amplified something at a different instant. Discarding it as a duplicate
    deletes exactly the diffusion curve a lead-lag study is trying to measure.
    """

    #: This author produced this content first, as far as we can tell.
    ORIGINAL = "ORIGINAL"
    #: Verbatim rebroadcast (retweet / repost / Telegram forward).
    REBROADCAST = "REBROADCAST"
    #: Rebroadcast with added commentary (quote-post).
    QUOTE = "QUOTE"
    #: A reply in a thread.
    REPLY = "REPLY"
    #: We can see it references something, but not what.
    UNKNOWN_PARENT = "UNKNOWN_PARENT"
    #: The platform gave us no relational information at all. Distinct from
    #: ORIGINAL: "no parent field" is not "there is no parent".
    NOT_PROVIDED = "NOT_PROVIDED"


@dataclass(frozen=True)
class ParentRef:
    """The item this one propagates from, as the platform described it."""

    kind: PropagationKind
    parent_message_id: str | None = None
    parent_author_id: str | None = None
    #: Which platform field carried the relation (§5.4 discipline).
    source_field: str | None = None

    def __post_init__(self) -> None:
        needs_parent = {
            PropagationKind.REBROADCAST,
            PropagationKind.QUOTE,
            PropagationKind.REPLY,
        }
        if self.kind in needs_parent and not self.parent_message_id:
            raise ArtifactSchemaError(
                f"{self.kind.value} asserts a parent but names none; use "
                "UNKNOWN_PARENT rather than inventing ORIGINAL"
            )
        if self.kind in {PropagationKind.ORIGINAL, PropagationKind.NOT_PROVIDED}:
            if self.parent_message_id:
                raise ArtifactSchemaError(
                    f"{self.kind.value} must not name a parent"
                )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "parent_message_id": self.parent_message_id,
            "parent_author_id": self.parent_author_id,
            "source_field": self.source_field,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "ParentRef":
        return cls(
            kind=PropagationKind(payload["kind"]),
            parent_message_id=payload.get("parent_message_id"),
            parent_author_id=payload.get("parent_author_id"),
            source_field=payload.get("source_field"),
        )


@dataclass(frozen=True)
class MediaRef:
    """A reference to media. We record the reference; we do not fetch it.

    Fetching media is a separate, separately budgeted, separately authorized
    activity. ``retrieved`` is therefore False on every record this milestone
    can produce, and is present so that a later milestone cannot pretend the
    distinction never existed.
    """

    media_key: str
    media_type: str
    url: str | None = None
    retrieved: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "media_key": self.media_key,
            "media_type": self.media_type,
            "url": self.url,
            "retrieved": self.retrieved,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "MediaRef":
        return cls(
            media_key=str(payload["media_key"]),
            media_type=str(payload["media_type"]),
            url=payload.get("url"),
            retrieved=bool(payload.get("retrieved", False)),
        )


class ResolutionConfidence(str, Enum):
    """How sure we are that this text names that on-chain entity.

    Ordinal, not numeric, and deliberately coarse. A float confidence invites
    thresholding, thresholding invites tuning, and tuning a collector is how a
    tape stops being a record of what happened.
    """

    #: Nothing address-shaped found, or found and rejected.
    UNRESOLVED = "UNRESOLVED"
    #: An address-shaped token was extracted but not confirmed against any
    #: registry. This is the ceiling of the default implementation.
    CANDIDATE = "CANDIDATE"
    #: Confirmed against an authoritative source. No such source is wired in
    #: this milestone; the state exists for a later one.
    CONFIRMED = "CONFIRMED"
    #: Multiple mutually-exclusive candidates. Explicitly NOT "pick the first".
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class EntityResolution:
    """The outcome of trying to name an on-chain entity from text."""

    confidence: ResolutionConfidence
    resolved_mint: str | None = None
    #: All address-shaped candidates found, in text order. Retained even when
    #: AMBIGUOUS so that the ambiguity is auditable rather than asserted.
    candidates: tuple[str, ...] = ()
    resolver_id: str = "unresolved"
    #: Canonical RFC3339 UTC, our clock, of the FIRST resolution attempt that
    #: produced this state. Absent until an attempt is made.
    first_entity_resolution_at: str | None = None

    def __post_init__(self) -> None:
        if self.confidence is ResolutionConfidence.UNRESOLVED and self.resolved_mint:
            raise ArtifactSchemaError("UNRESOLVED must not carry a resolved_mint")
        if self.confidence is ResolutionConfidence.AMBIGUOUS and self.resolved_mint:
            raise ArtifactSchemaError(
                "AMBIGUOUS must not collapse to a single resolved_mint; that "
                "is the failure this state exists to prevent"
            )
        if (
            self.confidence
            in {ResolutionConfidence.CANDIDATE, ResolutionConfidence.CONFIRMED}
            and not self.resolved_mint
        ):
            raise ArtifactSchemaError(
                f"{self.confidence.value} requires a resolved_mint"
            )

    @classmethod
    def unresolved(cls, *, resolver_id: str = "unresolved") -> "EntityResolution":
        return cls(
            confidence=ResolutionConfidence.UNRESOLVED, resolver_id=resolver_id
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence.value,
            "resolved_mint": self.resolved_mint,
            "candidates": list(self.candidates),
            "resolver_id": self.resolver_id,
            "first_entity_resolution_at": self.first_entity_resolution_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "EntityResolution":
        return cls(
            confidence=ResolutionConfidence(payload["confidence"]),
            resolved_mint=payload.get("resolved_mint"),
            candidates=tuple(payload.get("candidates") or ()),
            resolver_id=str(payload.get("resolver_id", "unresolved")),
            first_entity_resolution_at=payload.get("first_entity_resolution_at"),
        )


class DeferredState(str, Enum):
    """Typed absence for fields a later milestone fills in.

    ``ABSENT`` is the ONLY state a record produced by this milestone may carry
    for a deferred field. It says "no one has looked", which is a different
    claim from "we looked and there was nothing" (``OBSERVED_NONE``) and from
    "this question cannot apply here" (``NOT_APPLICABLE``).

    Collapsing these three is doctrine 10's exact failure: an unobserved
    reaction rendered as ``0`` becomes "the market did not move", which is a
    fabricated market state.
    """

    ABSENT = "ABSENT"
    OBSERVED = "OBSERVED"
    OBSERVED_NONE = "OBSERVED_NONE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class Deferred:
    """A structurally-present, typed-absent field.

    There is no default constructor that yields OBSERVED, and no ``value``
    accessor that returns ``None`` for an unobserved field — the caller must
    branch on :attr:`state`, which is the point.
    """

    state: DeferredState = DeferredState.ABSENT
    #: Our clock, canonical RFC3339 UTC, when the observation was made.
    observed_at: str | None = None
    detail: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state is DeferredState.ABSENT:
            if self.observed_at is not None or self.detail is not None:
                raise ArtifactSchemaError(
                    "ABSENT means nobody looked; it cannot carry an "
                    "observation time or a payload"
                )
        elif self.observed_at is None:
            raise ArtifactSchemaError(
                f"{self.state.value} is a claim about a moment and requires "
                "observed_at"
            )
        if self.state is DeferredState.OBSERVED and self.detail is None:
            raise ArtifactSchemaError("OBSERVED requires a detail payload")

    @property
    def is_absent(self) -> bool:
        return self.state is DeferredState.ABSENT

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "observed_at": self.observed_at,
            "detail": dict(self.detail) if self.detail is not None else None,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "Deferred":
        return cls(
            state=DeferredState(payload["state"]),
            observed_at=payload.get("observed_at"),
            detail=payload.get("detail"),
        )


def raw_content_digest(raw_content: bytes) -> str:
    """SHA-256 over the EXACT bytes received, before any decode.

    Hashing the decoded string instead would make the hash a statement about
    our parser rather than about the wire, and a later re-parse could not be
    audited against it.
    """

    if not isinstance(raw_content, (bytes, bytearray)):
        raise ArtifactSchemaError(
            "raw_content_hash must be taken over bytes, not over a decoded "
            "string: the hash exists to audit our own decoding"
        )
    return "sha256:" + hashlib.sha256(bytes(raw_content)).hexdigest()


def content_identity(text: str) -> str:
    """Identity of the *content* — the thing that spreads.

    Deliberately conservative normalisation: unicode-insensitive trimming and
    whitespace collapse only. Aggressive normalisation (stripping URLs,
    lowercasing, removing mentions) merges genuinely different posts and would
    silently manufacture propagation events that never happened.
    """

    collapsed = " ".join(text.split())
    return "sha256:" + hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SocialArtifact:
    """One collected item. Frozen and complete at construction.

    Identity, at three levels, because they answer different questions:

    ``message_identity``    (platform, source_id, message_id) — WHICH POST.
    ``content_identity``    hash of normalised text — WHAT WAS SAID.
    ``delivery_identity``   message_identity + delivery_sequence — WHICH TIME
                            THE STREAM HANDED IT TO US.

    Two artifacts sharing ``content_identity`` but not ``message_identity`` are
    a propagation event. Two sharing ``delivery_identity`` are a redelivery.
    """

    platform: Platform
    #: The configured source that this item came from (an account, a channel).
    source_id: str
    #: The platform's own id for this specific item.
    message_id: str
    author_id: str

    source_created_at: SourceCreatedAt
    our_received_at: OurReceivedAt

    raw_content: bytes
    raw_content_hash: str
    #: The human-authored text, extracted from the payload. This — NOT the raw
    #: frame — is what `content_identity` hashes.
    #:
    #: Hashing the raw frame instead would silently destroy every propagation
    #: measurement: a retweet's envelope carries a different message id,
    #: author, and reference block, so two frames carrying identical text hash
    #: differently and the spread is never seen. The extracted text is stored
    #: beside the raw bytes so the identity is auditable rather than implicit.
    content_text: str

    #: Which configured rule matched. Never a free-text description of "crypto
    #: twitter": the rule id is what makes the source universe measurable.
    matching_rule: str

    parent: ParentRef
    #: Required, no default. See :class:`DeliveryMode`.
    delivery_mode: DeliveryMode = DeliveryMode.UNKNOWN
    media: tuple[MediaRef, ...] = ()
    entity_resolution: EntityResolution = field(
        default_factory=EntityResolution.unresolved
    )

    #: Later milestones. ABSENT at ingestion, always.
    first_onchain_reaction: Deferred = field(default_factory=Deferred)
    first_price_reaction: Deferred = field(default_factory=Deferred)

    #: Monotonically-increasing per (platform, stream connection). Lets us tell
    #: "the stream sent it twice" from "two different posts".
    delivery_sequence: int = 0
    #: Which connection generation delivered it. Changes on every reconnect, so
    #: a duplicate across a reconnect is distinguishable from one within a
    #: single connection (doctrine 7: force a reconnect, this must change).
    subscription_generation: int = 0

    #: Cross-clock evidence, computed once at ingestion and stored so a replay
    #: reader is not tempted to recompute it from a re-fetched creation time.
    delivery_offset: DeliveryOffset | None = None

    ingestion_version: str = INGESTION_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.our_received_at, OurReceivedAt):
            raise ClockConfusionError(
                "our_received_at must be an OurReceivedAt produced by "
                "app.social.timebase.capture_receipt"
            )
        if not isinstance(self.source_created_at, SourceCreatedAt):
            raise ClockConfusionError(
                "source_created_at must be a SourceCreatedAt; a collector "
                "stamp is not a platform claim"
            )
        expected = raw_content_digest(self.raw_content)
        if self.raw_content_hash != expected:
            raise ArtifactSchemaError(
                "raw_content_hash does not match raw_content; the tape's "
                "audit trail for re-parsing would be void"
            )
        if not self.matching_rule:
            raise ArtifactSchemaError(
                "every artifact must name the rule that admitted it; an "
                "unattributed item cannot have its value measured"
            )
        if self.ingestion_version not in KNOWN_INGESTION_VERSIONS:
            raise ArtifactSchemaError(
                f"unknown ingestion_version {self.ingestion_version!r}; this "
                "reader cannot state what the record's fields mean"
            )

    # -- identities ---------------------------------------------------------

    @property
    def message_identity(self) -> tuple[str, str, str]:
        return (self.platform.value, self.source_id, self.message_id)

    @property
    def content_identity(self) -> str:
        return content_identity(self.content_text)

    @property
    def delivery_identity(self) -> tuple[str, str, str, int, int]:
        return self.message_identity + (
            self.subscription_generation,
            self.delivery_sequence,
        )

    @property
    def raw_text(self) -> str:
        """Best-effort decode of the raw bytes. The bytes remain canonical."""

        return bytes(self.raw_content).decode("utf-8", errors="replace")

    @property
    def is_live_delivery(self) -> bool:
        """True only for LIVE. UNKNOWN is not optimistically treated as live."""

        return self.delivery_mode is DeliveryMode.LIVE

    @property
    def is_propagation(self) -> bool:
        return self.parent.kind in {
            PropagationKind.REBROADCAST,
            PropagationKind.QUOTE,
        }

    def with_entity_resolution(
        self, resolution: EntityResolution
    ) -> "SocialArtifact":
        """Return a NEW artifact. Records are never mutated in place."""

        return replace(self, entity_resolution=resolution)

    # -- serialisation ------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        import base64

        return {
            "ingestion_version": self.ingestion_version,
            "platform": self.platform.value,
            "source_id": self.source_id,
            "message_id": self.message_id,
            "author_id": self.author_id,
            "source_created_at": self.source_created_at.to_json(),
            "our_received_at": self.our_received_at.to_json(),
            "raw_content_b64": base64.b64encode(bytes(self.raw_content)).decode(
                "ascii"
            ),
            "raw_content_hash": self.raw_content_hash,
            "content_text": self.content_text,
            "matching_rule": self.matching_rule,
            "parent": self.parent.to_json(),
            "delivery_mode": self.delivery_mode.value,
            "media": [m.to_json() for m in self.media],
            "entity_resolution": self.entity_resolution.to_json(),
            "first_onchain_reaction": self.first_onchain_reaction.to_json(),
            "first_price_reaction": self.first_price_reaction.to_json(),
            "delivery_sequence": self.delivery_sequence,
            "subscription_generation": self.subscription_generation,
            "delivery_offset": (
                self.delivery_offset.to_json()
                if self.delivery_offset is not None
                else None
            ),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "SocialArtifact":
        import base64

        from app.social.timebase import SourceTimeFidelity

        raw = base64.b64decode(payload["raw_content_b64"])
        offset_payload = payload.get("delivery_offset")
        offset = None
        if offset_payload is not None:
            offset = DeliveryOffset(
                offset_contaminated_us=int(
                    offset_payload["offset_contaminated_us"]
                ),
                host_clock_offset_characterised=bool(
                    offset_payload["host_clock_offset_characterised"]
                ),
                source_time_fidelity=SourceTimeFidelity(
                    offset_payload["source_time_fidelity"]
                ),
            )
        return cls(
            platform=Platform(payload["platform"]),
            source_id=str(payload["source_id"]),
            message_id=str(payload["message_id"]),
            author_id=str(payload["author_id"]),
            source_created_at=SourceCreatedAt.from_json(
                payload["source_created_at"]
            ),
            our_received_at=OurReceivedAt.from_json(payload["our_received_at"]),
            raw_content=raw,
            raw_content_hash=str(payload["raw_content_hash"]),
            content_text=str(payload["content_text"]),
            matching_rule=str(payload["matching_rule"]),
            parent=ParentRef.from_json(payload["parent"]),
            delivery_mode=DeliveryMode(payload["delivery_mode"]),
            media=tuple(MediaRef.from_json(m) for m in payload.get("media", [])),
            entity_resolution=EntityResolution.from_json(
                payload["entity_resolution"]
            ),
            first_onchain_reaction=Deferred.from_json(
                payload["first_onchain_reaction"]
            ),
            first_price_reaction=Deferred.from_json(payload["first_price_reaction"]),
            delivery_sequence=int(payload.get("delivery_sequence", 0)),
            subscription_generation=int(payload.get("subscription_generation", 0)),
            delivery_offset=offset,
            ingestion_version=str(payload["ingestion_version"]),
        )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Deterministic JSON for digesting. Sorted keys, no spaces, no NaN."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
