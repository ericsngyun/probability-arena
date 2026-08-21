"""``TokenResolution`` — **mint equality is not token identity.**

EVIDENCE-JOIN-CONTRACT-001 §4. `app/social` produces a `resolved_mint` by
pulling plausible-length base58 strings out of free text. `app/fills` produces
a mint read off a confirmed transaction. Joining those on string equality
treats two different kinds of object as the same kind.

The harmless failure is a base58 string that never existed on chain: it simply
does not match. The dangerous failure is a string that matches a **real but
wrong** mint, because it joins successfully and attributes on-chain activity
to a social event that never referenced it. The named threats:

* a **decoy address** pasted into a scam post;
* an **old mint** quoted in a stale post;
* a **copied address** from somewhere else entirely;
* a **competitor mention**;
* a contract visible in a **screenshot** and unrelated to the text;
* **quote-posted scam content**, where the outer post is legitimate.

A string match turns every one of those into ground truth.

So the join into the primary alpha cohort is permitted at exactly one status:
:data:`TokenResolutionStatus.CANONICALLY_VERIFIED`. A syntactically valid
base58 string is nowhere near enough, and neither is "the mint exists".

The escalation is an INTERFACE, not a resolver
----------------------------------------------
Building a clever resolver here would repeat the mistake `app/social/
resolution.py` already refuses: a resolver's mistakes become permanent facts
on an immutable tape, and a resolver tuned against outcomes is a model living
inside the collector. So this module ships:

    post contains a mint          TextCandidateStage      (offline, default)
      → is it a live mint         ChainExistenceStage     (protocol only)
        → does project context    ProjectContextStage     (protocol only)
           corroborate
          → does a linked site    LinkedProfileStage      (protocol only)
             reference the token
            → canonical identity  CanonicalIdentityStage  (protocol only)

Only the first stage has an implementation, and it can never rise above
``TEXT_CANDIDATE``. Every later stage is a Protocol with an explicit
"not wired" implementation that returns its input unchanged and records why.
**No stage in this module performs network I/O, and the tests never do.**

CONTAINS NO SIGNAL. Resolution says which entity is named. It says nothing
about whether that matters.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "Chain",
    "TokenResolutionStatus",
    "EvidenceKind",
    "ResolutionEvidence",
    "TokenResolution",
    "TokenResolutionError",
    "JOINABLE_STATUSES",
    "ResolverStage",
    "TextCandidateStage",
    "ChainExistenceStage",
    "ProjectContextStage",
    "LinkedProfileStage",
    "CanonicalIdentityStage",
    "NotWiredStage",
    "EscalationLadder",
    "default_ladder",
    "from_entity_resolution",
    "THREATS",
]


class TokenResolutionError(Exception):
    """A resolution was asked to exist in a state the schema forbids."""


class Chain(str, Enum):
    """Closed set. A mint is only an identity WITHIN a chain — the same
    base58 string on two chains is two different assets."""

    SOLANA = "SOLANA"


class TokenResolutionStatus(str, Enum):
    """The escalation ladder as a closed, ordered vocabulary."""

    #: A base58-shaped string was found in text. Nothing was consulted. This
    #: is the ceiling of any offline resolver and it is NOT joinable.
    TEXT_CANDIDATE = "TEXT_CANDIDATE"

    #: The mint exists on chain AND independent context corroborates that
    #: THIS post refers to THAT token. The only joinable status.
    CANONICALLY_VERIFIED = "CANONICALLY_VERIFIED"

    #: Identified via the posting project's own account/context rather than
    #: from the text. Strong, but the post-to-token link is inferred.
    RESOLVED_FROM_PROJECT = "RESOLVED_FROM_PROJECT"

    #: Identified through a ticker/name alias table. Aliases are contested by
    #: design — every scam copies a name — so this is never joinable.
    RESOLVED_FROM_ALIAS = "RESOLVED_FROM_ALIAS"

    #: Several mutually exclusive candidates. Explicitly NOT "pick the first".
    AMBIGUOUS = "AMBIGUOUS"

    #: Actively ruled out (denylisted, known decoy, wrong chain).
    REJECTED = "REJECTED"


#: **The join gate.** One member. Widening this set is a milestone decision,
#: not a code change — it is asserted in the positive controls.
JOINABLE_STATUSES = frozenset({TokenResolutionStatus.CANONICALLY_VERIFIED})


#: The named threats from EVIDENCE-JOIN-CONTRACT-001 §4, kept in code so a
#: reviewer of a future resolver sees what it has to defeat.
THREATS = (
    "decoy address in a scam post",
    "old mint quoted in a stale post",
    "address copied from an unrelated source",
    "competitor token mentioned in passing",
    "screenshot containing an unrelated contract",
    "quote-posted scam content inside a legitimate outer post",
)


class EvidenceKind(str, Enum):
    """What kind of check produced a piece of corroboration."""

    #: A base58-shaped string appeared in the post text. Shape only.
    TEXT_MATCH = "TEXT_MATCH"
    #: The mint account exists on chain and is an SPL mint.
    CHAIN_MINT_EXISTS = "CHAIN_MINT_EXISTS"
    #: The posting account is the project's own account, or is linked to it.
    PROJECT_ACCOUNT_LINK = "PROJECT_ACCOUNT_LINK"
    #: A site or profile linked from the post names the same token.
    LINKED_PROFILE_REFERENCE = "LINKED_PROFILE_REFERENCE"
    #: A ticker/name alias table mapped a symbol to this mint.
    ALIAS_TABLE = "ALIAS_TABLE"
    #: A registry we treat as canonical asserts this identity.
    CANONICAL_REGISTRY = "CANONICAL_REGISTRY"
    #: A check ran and found NOTHING. Recorded, because "we looked and found
    #: no corroboration" is evidence and silence is not.
    NEGATIVE_CHECK = "NEGATIVE_CHECK"


#: Kinds that corroborate the post→token LINK, as opposed to merely
#: establishing that the mint exists. CANONICALLY_VERIFIED requires at least
#: one, because "the string is a real mint" defeats none of the six threats.
_CORROBORATING = frozenset(
    {
        EvidenceKind.PROJECT_ACCOUNT_LINK,
        EvidenceKind.LINKED_PROFILE_REFERENCE,
        EvidenceKind.CANONICAL_REGISTRY,
    }
)


@dataclass(frozen=True, slots=True)
class ResolutionEvidence:
    """One check, its outcome, and where it came from."""

    kind: EvidenceKind
    #: Which stage produced it. Stable, so a later, better stage's output is
    #: distinguishable from this one's rather than silently replacing it.
    stage_id: str
    detail: str
    #: Canonical RFC3339 UTC, our clock.
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.stage_id:
            raise TokenResolutionError(
                "evidence must name the stage that produced it"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "stage_id": self.stage_id,
            "detail": self.detail,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "ResolutionEvidence":
        return cls(
            kind=EvidenceKind(payload["kind"]),
            stage_id=str(payload["stage_id"]),
            detail=str(payload["detail"]),
            observed_at=payload.get("observed_at"),
        )


@dataclass(frozen=True, slots=True)
class TokenResolution:
    """A claim that a piece of text refers to a specific on-chain token."""

    chain: Chain
    status: TokenResolutionStatus
    resolver_version: str
    mint: str | None = None
    #: Every candidate seen, in text order. Retained even when AMBIGUOUS, so
    #: the ambiguity is auditable rather than asserted.
    candidates: tuple[str, ...] = ()
    evidence: tuple[ResolutionEvidence, ...] = ()
    #: Ordinal and coarse, never a float. A float confidence invites
    #: thresholding, thresholding invites tuning, and a tuned collector stops
    #: being a record of what happened. Mirrors
    #: `app.social.artifact.ResolutionConfidence`.
    confidence: str = "CANDIDATE"

    def __post_init__(self) -> None:
        if not self.resolver_version:
            raise TokenResolutionError(
                "every resolution must name the resolver that produced it"
            )
        if self.status is TokenResolutionStatus.AMBIGUOUS:
            if self.mint is not None:
                raise TokenResolutionError(
                    "AMBIGUOUS must not collapse to a single mint; that is "
                    "the failure the state exists to prevent"
                )
            if len(self.candidates) < 2:
                raise TokenResolutionError(
                    "AMBIGUOUS asserts several mutually exclusive candidates "
                    "and must carry them"
                )
        if self.status is TokenResolutionStatus.REJECTED and self.mint is not None:
            raise TokenResolutionError(
                "REJECTED must not carry a mint; a rejected candidate that "
                "still exposes one will be joined on by something"
            )
        needs_mint = {
            TokenResolutionStatus.TEXT_CANDIDATE,
            TokenResolutionStatus.CANONICALLY_VERIFIED,
            TokenResolutionStatus.RESOLVED_FROM_PROJECT,
            TokenResolutionStatus.RESOLVED_FROM_ALIAS,
        }
        if self.status in needs_mint and not self.mint:
            raise TokenResolutionError(
                f"{self.status.value} requires a mint"
            )

        if self.status is TokenResolutionStatus.CANONICALLY_VERIFIED:
            kinds = {e.kind for e in self.evidence}
            if EvidenceKind.CHAIN_MINT_EXISTS not in kinds:
                raise TokenResolutionError(
                    "CANONICALLY_VERIFIED requires CHAIN_MINT_EXISTS "
                    "evidence: a base58 string that was never confirmed to "
                    "exist on chain is not a token"
                )
            if not (kinds & _CORROBORATING):
                raise TokenResolutionError(
                    "CANONICALLY_VERIFIED requires evidence corroborating "
                    "that THIS POST refers to THAT TOKEN (project link, "
                    "linked profile, or canonical registry). 'The mint "
                    "exists' defeats none of the six named threats — a decoy "
                    "address in a scam post is a real mint too"
                )

    # -- the gate ----------------------------------------------------------

    @property
    def is_joinable(self) -> bool:
        """True only at ``CANONICALLY_VERIFIED``. Read this, never ``status``."""
        return self.status in JOINABLE_STATUSES

    def refusal_reason(self) -> str | None:
        if self.is_joinable:
            return None
        return (
            f"token resolution status {self.status.value} is not joinable "
            f"into the primary alpha cohort; only "
            f"{sorted(s.value for s in JOINABLE_STATUSES)} is"
        )

    def with_evidence(
        self, *evidence: ResolutionEvidence
    ) -> "TokenResolution":
        return replace(self, evidence=self.evidence + tuple(evidence))

    def to_json(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "status": self.status.value,
            "resolver_version": self.resolver_version,
            "mint": self.mint,
            "candidates": list(self.candidates),
            "evidence": [e.to_json() for e in self.evidence],
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "TokenResolution":
        return cls(
            chain=Chain(payload["chain"]),
            status=TokenResolutionStatus(payload["status"]),
            resolver_version=str(payload["resolver_version"]),
            mint=payload.get("mint"),
            candidates=tuple(payload.get("candidates") or ()),
            evidence=tuple(
                ResolutionEvidence.from_json(e)
                for e in payload.get("evidence") or ()
            ),
            confidence=str(payload.get("confidence", "CANDIDATE")),
        )


def from_entity_resolution(
    entity, *, chain: Chain = Chain.SOLANA
) -> TokenResolution:
    """Adapt `app.social.artifact.EntityResolution` into a seam resolution.

    **Deliberately cannot produce a joinable result.** The social side's
    ceiling is `CANDIDATE` (nothing authoritative is consulted) and its
    `CONFIRMED` state is defined as "confirmed against an authoritative
    source" with no such source wired — so it maps to
    ``RESOLVED_FROM_ALIAS``, which is not joinable, rather than to
    ``CANONICALLY_VERIFIED``, which would let an unverified registry claim
    into the alpha cohort. The confidence is CARRIED, never dropped (§4).
    """
    from app.social.artifact import ResolutionConfidence

    confidence = entity.confidence
    evidence = ()
    if entity.candidates:
        evidence = (
            ResolutionEvidence(
                kind=EvidenceKind.TEXT_MATCH,
                stage_id=entity.resolver_id,
                detail=(
                    f"{len(entity.candidates)} base58-shaped candidate(s) "
                    "extracted from post text; shape only"
                ),
                observed_at=entity.first_entity_resolution_at,
            ),
        )

    if confidence is ResolutionConfidence.UNRESOLVED:
        return TokenResolution(
            chain=chain,
            status=TokenResolutionStatus.REJECTED,
            resolver_version=entity.resolver_id,
            candidates=tuple(entity.candidates),
            evidence=evidence,
            confidence=confidence.value,
        )
    if confidence is ResolutionConfidence.AMBIGUOUS:
        return TokenResolution(
            chain=chain,
            status=TokenResolutionStatus.AMBIGUOUS,
            resolver_version=entity.resolver_id,
            candidates=tuple(entity.candidates),
            evidence=evidence,
            confidence=confidence.value,
        )
    if confidence is ResolutionConfidence.CANDIDATE:
        return TokenResolution(
            chain=chain,
            status=TokenResolutionStatus.TEXT_CANDIDATE,
            resolver_version=entity.resolver_id,
            mint=entity.resolved_mint,
            candidates=tuple(entity.candidates),
            evidence=evidence,
            confidence=confidence.value,
        )
    return TokenResolution(
        chain=chain,
        status=TokenResolutionStatus.RESOLVED_FROM_ALIAS,
        resolver_version=entity.resolver_id,
        mint=entity.resolved_mint,
        candidates=tuple(entity.candidates),
        evidence=evidence
        + (
            ResolutionEvidence(
                kind=EvidenceKind.ALIAS_TABLE,
                stage_id=entity.resolver_id,
                detail=(
                    "social-side CONFIRMED; the authoritative source it "
                    "names is not identified on the record, so this is NOT "
                    "canonical verification"
                ),
                observed_at=entity.first_entity_resolution_at,
            ),
        ),
        confidence=confidence.value,
    )


# ---------------------------------------------------------------------------
# the escalation ladder — an interface with a conservative default
# ---------------------------------------------------------------------------


@runtime_checkable
class ResolverStage(Protocol):
    """One rung. Typed, narrow, no varargs (doctrine 6).

    A stage takes a resolution and returns a resolution. It MAY add evidence
    and MAY raise the status. It MUST NOT lower an existing status silently
    and MUST NOT raise on ordinary input.
    """

    @property
    def stage_id(self) -> str:
        """Stable identity, written onto every piece of evidence."""

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution:
        ...


class TextCandidateStage:
    """Rung 1 — post contains a mint. Offline, and the only implemented rung.

    Reuses `app.social.resolution.ConservativeAddressResolver` rather than
    growing a second base58 extractor: two extractors would drift, and the
    drift would look like a resolution improvement.
    """

    stage_id = "seam-text-candidate.v1"

    def __init__(self, resolver=None) -> None:
        if resolver is None:
            from app.social.resolution import ConservativeAddressResolver

            resolver = ConservativeAddressResolver()
        self._resolver = resolver

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution:
        if resolution.status is not TokenResolutionStatus.TEXT_CANDIDATE and (
            resolution.candidates
        ):
            return resolution
        entity = self._resolver.resolve(text)
        return from_entity_resolution(entity, chain=resolution.chain)


class NotWiredStage:
    """A rung that exists as a contract and does nothing.

    Returns its input unchanged plus a NEGATIVE_CHECK note. It never calls the
    network, so it is safe in tests by construction — there is nothing to
    stub, nothing to accidentally leave enabled, and no environment in which
    it behaves differently.
    """

    def __init__(self, stage_id: str, reason: str) -> None:
        self.stage_id = stage_id
        self._reason = reason

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution:
        return resolution.with_evidence(
            ResolutionEvidence(
                kind=EvidenceKind.NEGATIVE_CHECK,
                stage_id=self.stage_id,
                detail=self._reason,
            )
        )


@runtime_checkable
class ChainExistenceStage(Protocol):
    """Rung 2 — is it a live mint? Requires an RPC call. NOT implemented."""

    @property
    def stage_id(self) -> str: ...

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution: ...


@runtime_checkable
class ProjectContextStage(Protocol):
    """Rung 3 — does the project/account context corroborate? NOT implemented."""

    @property
    def stage_id(self) -> str: ...

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution: ...


@runtime_checkable
class LinkedProfileStage(Protocol):
    """Rung 4 — does a linked site/profile name the same token? NOT implemented."""

    @property
    def stage_id(self) -> str: ...

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution: ...


@runtime_checkable
class CanonicalIdentityStage(Protocol):
    """Rung 5 — canonical identity. NOT implemented."""

    @property
    def stage_id(self) -> str: ...

    def escalate(
        self, resolution: TokenResolution, *, text: str
    ) -> TokenResolution: ...


@dataclass(frozen=True, slots=True)
class EscalationLadder:
    """Runs stages in order. A stage may only ever ADD evidence.

    The ladder does not decide joinability; :attr:`TokenResolution.is_joinable`
    does, from the status the stages actually reached. So a ladder with four
    unwired rungs produces a non-joinable result, which is the correct and
    safe default state of this system today.
    """

    stages: tuple[ResolverStage, ...]

    def resolve(self, text: str, *, chain: Chain = Chain.SOLANA) -> TokenResolution:
        resolution = TokenResolution(
            chain=chain,
            status=TokenResolutionStatus.REJECTED,
            resolver_version="seam-ladder.v1",
            confidence="UNRESOLVED",
        )
        for stage in self.stages:
            resolution = stage.escalate(resolution, text=text)
        return resolution


def default_ladder() -> EscalationLadder:
    """The conservative default: one real rung, four honest refusals."""
    return EscalationLadder(
        stages=(
            TextCandidateStage(),
            NotWiredStage(
                "chain-existence.not-wired",
                "chain existence check requires an RPC call; not authorized "
                "in this milestone, so the mint is unconfirmed",
            ),
            NotWiredStage(
                "project-context.not-wired",
                "project/account corroboration not wired; the post->token "
                "link is unsupported",
            ),
            NotWiredStage(
                "linked-profile.not-wired",
                "linked site/profile check not wired; no independent "
                "reference to this token",
            ),
            NotWiredStage(
                "canonical-identity.not-wired",
                "canonical identity registry not wired; CANONICALLY_VERIFIED "
                "is unreachable and the join gate stays shut",
            ),
        )
    )
