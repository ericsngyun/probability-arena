"""SOCIAL-EVIDENCE-EXTRACTOR-001 — the semantic layer, and only that.

A model reads a social artifact and emits **typed evidence claims**. It does
not decide anything. The boundary is the whole design:

    model:                raw artifact  ->  typed claims
    deterministic policy: typed claims  ->  Gate 2 verdict

The model is allowed to say *"this span appears to be a direct publication of
mint X by source Y"*. It is not allowed to conclude *"therefore X is
canonical"*. `decide_corroboration` remains the only place that conclusion
exists, and this module has no field through which such a conclusion could
travel -- no `verified`, no `canonical`, no acceptance score, no signal.

**One artifact yields MANY claims.** "Old contract X is fake, official is Y" is
not a single mint guess; it is two claims -- `X -> DISAVOWED_MINT` and
`Y -> PUBLISHED_MINT` -- which the deterministic gate then resolves. An
extractor that returned one "best" mint would have made the decision it is
forbidden to make.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Protocol

from app.seam.corroboration import (
    BindingKind, CorroborationEvidence, ProvenanceScope, SourceAuthority,
)

EXTRACTOR_VERSION = "social-evidence-extractor-v1"


class Relationship(str, Enum):
    """What the source asserts ABOUT the mint. The load-bearing field.

    "A mint appears somewhere in the post" is not enough: the same string can
    be published, mentioned in passing, disavowed as a fake, or named as
    superseded, and those are four different facts.
    """
    PUBLISHED_MINT = "PUBLISHED_MINT"
    MENTIONED_MINT = "MENTIONED_MINT"
    DISAVOWED_MINT = "DISAVOWED_MINT"
    MIGRATED_MINT = "MIGRATED_MINT"


class SourceSurface(str, Enum):
    """WHERE the claim was made. A claim, not a finding of fact -- whether a
    surface really is official is itself evidence and is recorded as such."""
    CLAIMED_OFFICIAL_ACCOUNT = "CLAIMED_OFFICIAL_ACCOUNT"
    CLAIMED_OFFICIAL_SITE = "CLAIMED_OFFICIAL_SITE"
    LAUNCHPAD_PAGE = "LAUNCHPAD_PAGE"
    THIRD_PARTY_ACCOUNT = "THIRD_PARTY_ACCOUNT"
    UNKNOWN_SURFACE = "UNKNOWN_SURFACE"


class SpanOrigin(str, Enum):
    DIRECT = "DIRECT"
    QUOTED = "QUOTED"
    FORWARDED = "FORWARDED"


class ExtractionError(Exception):
    pass


#: Field names an extractor may never emit. Enforced at construction, so a
#: future model or prompt cannot smuggle a decision through an extra key.
FORBIDDEN_FIELDS = frozenset({
    "verified", "canonical", "canonically_verified", "is_canonical",
    "confidence", "confidence_to_accept", "score", "probability", "weight",
    "trade_signal", "signal", "recommendation", "action", "buy", "sell",
})


@dataclass(frozen=True)
class EvidenceClaim:
    """One normalized claim. Evidence, never a decision."""
    artifact_id: str
    candidate_mint: str
    subject_entity: str | None
    relationship: Relationship
    source_identity: str
    source_surface: SourceSurface
    span_origin: SpanOrigin
    evidence_span_hash: str
    extractor_model: str
    extractor_version: str = EXTRACTOR_VERSION

    def __post_init__(self) -> None:
        if not self.candidate_mint:
            raise ExtractionError("a claim must name the mint it is about")
        if not self.evidence_span_hash:
            raise ExtractionError(
                "a claim must carry the hash of the span it came from, or it "
                "cannot be audited back to the artifact")

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("relationship", "source_surface", "span_origin"):
            d[k] = getattr(self, k).value
        return d


def span_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ClaimExtractor(Protocol):
    """The injected model. Returns raw dicts; this module types them."""

    def extract(self, artifact: dict) -> list[dict]: ...


def _validate_raw(raw: dict) -> None:
    bad = FORBIDDEN_FIELDS & {k.lower() for k in raw}
    if bad:
        raise ExtractionError(
            f"extractor emitted decision field(s) {sorted(bad)}; the model "
            "produces evidence and the deterministic gate produces verdicts")


def normalize(raw: dict, *, artifact_id: str, model: str) -> EvidenceClaim:
    """Raw model output -> a typed claim. Unknown enum values are refused."""
    _validate_raw(raw)
    try:
        rel = Relationship(raw["relationship"])
        surface = SourceSurface(raw["source_surface"])
        origin = SpanOrigin(raw["span_origin"])
    except (KeyError, ValueError) as exc:
        raise ExtractionError(f"unrecognised claim shape: {exc}") from exc
    if "candidate_mint" not in raw:
        # Typed at the boundary. A bare KeyError escaping here would reach a
        # caller as a generic failure rather than as "the model did not name a
        # mint", which is a different and more useful fact.
        raise ExtractionError(
            "claim does not name a candidate_mint; the extractor must say "
            "which mint a claim is about")
    return EvidenceClaim(
        artifact_id=artifact_id,
        candidate_mint=raw["candidate_mint"],
        subject_entity=raw.get("subject_entity"),
        relationship=rel,
        source_identity=raw.get("source_identity", ""),
        source_surface=surface,
        span_origin=origin,
        evidence_span_hash=raw.get("evidence_span_hash")
                           or span_hash(raw.get("evidence_span", "")),
        extractor_model=model,
    )


def extract_claims(artifact: dict, extractor: ClaimExtractor, *,
                   model: str) -> list[EvidenceClaim]:
    """All claims in one artifact. Never collapsed to a single 'best' mint."""
    return [normalize(r, artifact_id=artifact["artifact_id"], model=model)
            for r in extractor.extract(artifact)]


# ---------------------------------------------------------------------------
# Adapter: typed claims -> Gate 2 evidence. Lossless and mechanical.
# ---------------------------------------------------------------------------

#: A CLAIMED official surface becomes an official authority only here, and the
#: mapping is deliberately narrow: a third-party account claiming to represent
#: a project is NOT an official surface, however it describes itself.
_SURFACE_TO_AUTHORITY = {
    SourceSurface.CLAIMED_OFFICIAL_ACCOUNT: SourceAuthority.OFFICIAL_PROJECT_SURFACE,
    SourceSurface.CLAIMED_OFFICIAL_SITE: SourceAuthority.OFFICIAL_PROJECT_SURFACE,
    SourceSurface.LAUNCHPAD_PAGE: SourceAuthority.OFFICIAL_PROJECT_SURFACE,
    SourceSurface.THIRD_PARTY_ACCOUNT: SourceAuthority.THIRD_PARTY,
    SourceSurface.UNKNOWN_SURFACE: SourceAuthority.UNKNOWN,
}

#: `MIGRATED_MINT` maps to DISAVOWAL, not NAMES_DIFFERENT_MINT.
#:
#: Gate 2's `NAMES_DIFFERENT_MINT` means *the authoritative surface publishes
#: some OTHER mint*, so its `subject_mint` is that other mint. A migration
#: claim is about the mint being migrated AWAY FROM -- its subject is the OLD
#: one. Mapping it to `NAMES_DIFFERENT_MINT` therefore produced
#: `subject == candidate`, which Gate 2 correctly does not treat as a conflict,
#: and a superseded mint silently failed to conflict. Caught by the
#: migration case in the adversarial corpus.
#:
#: For IDENTITY purposes "we migrated away from X" and "X is not ours" are the
#: same claim: X is not the canonical current token. The distinction between
#: fake and superseded is preserved in `detail`, so nothing is lost for a
#: future risk feature.
_RELATIONSHIP_TO_KIND = {
    Relationship.PUBLISHED_MINT: BindingKind.PUBLISHED_MINT,
    Relationship.MENTIONED_MINT: BindingKind.MENTION,
    Relationship.DISAVOWED_MINT: BindingKind.DISAVOWAL,
    Relationship.MIGRATED_MINT: BindingKind.DISAVOWAL,
}

#: FORWARDED maps to QUOTED: neither is the outer account's own words, and the
#: distinction that matters to the gate is authorship, not the mechanism.
_ORIGIN_TO_SCOPE = {
    SpanOrigin.DIRECT: ProvenanceScope.SOURCE_AUTHORED,
    SpanOrigin.QUOTED: ProvenanceScope.QUOTED,
    SpanOrigin.FORWARDED: ProvenanceScope.QUOTED,
}


def to_gate2_evidence(claim: EvidenceClaim, *,
                      observed_at_utc: str) -> CorroborationEvidence:
    return CorroborationEvidence(
        kind=_RELATIONSHIP_TO_KIND[claim.relationship],
        authority=_SURFACE_TO_AUTHORITY[claim.source_surface],
        scope=_ORIGIN_TO_SCOPE[claim.span_origin],
        subject_mint=claim.candidate_mint,
        evidence_ref=f"{claim.artifact_id}:{claim.evidence_span_hash[:12]}",
        evidence_sha256=claim.evidence_span_hash,
        observed_at_utc=observed_at_utc,
        detail=f"{claim.relationship.value} via {claim.source_surface.value} "
               f"({claim.span_origin.value}) by {claim.source_identity}",
    )
