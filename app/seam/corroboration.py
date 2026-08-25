"""SOLANA-TOKEN-IDENTITY-VERIFICATION-001 — gate 2: semantic corroboration.

Gate 1 proves the address is a real Solana mint. It proves nothing about
whether *this artifact* referred to *that* token: every one of the six threats
in `token.THREATS` involves an address that is perfectly real and still wrong.
Gate 2 is that second, independent question.

**The division of labour is the design.** A model may *extract and normalize*
evidence -- read a page, find a published contract address, tell quoted content
from source-authored content. A model may **not decide**. Only
`decide_corroboration` emits `CANONICALLY_VERIFIED`, from a rule with no
numeric input:

    CANONICALLY_VERIFIED  <=>  CHAIN_VERIFIED
                               AND authoritative binding
                               AND NOT authoritative conflict

Three things are structurally impossible here, not merely discouraged:

* **no confidence threshold.** `CorroborationEvidence` carries no score, so
  there is nothing to compare against a cutoff.
* **no ticker identity.** A symbol is not an identity -- every scam copies a
  name -- so `TICKER_ONLY` can never contribute to a binding.
* **no transitivity, and no strength in numbers.** "An official account
  mentioned this mint" is not "this account owns this mint", and a thousand
  low-authority repetitions of a real mint sum to nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from app.seam.token import TokenResolutionStatus


class ProvenanceScope(str, Enum):
    """WHERE in the artifact the evidence came from. Load-bearing."""

    #: The account itself authored this text.
    SOURCE_AUTHORED = "SOURCE_AUTHORED"
    #: Inside quoted, retweeted or forwarded content. The outer account did
    #: not say it, and a legitimate account quoting a scam is threat six.
    QUOTED = "QUOTED"
    #: Reached by following a link OUT of the artifact to another surface.
    LINKED = "LINKED"


class SourceAuthority(str, Enum):
    """WHO said it. Only one level can bind an identity."""

    #: A surface the project itself controls: its verified account, its site,
    #: its launchpad page. Establishing this is the extractor's job and is
    #: itself evidence that must be recorded.
    OFFICIAL_PROJECT_SURFACE = "OFFICIAL_PROJECT_SURFACE"
    THIRD_PARTY = "THIRD_PARTY"
    UNKNOWN = "UNKNOWN"


class BindingKind(str, Enum):
    """WHAT was said about the mint."""

    #: An authoritative surface publishes this mint AS its token.
    PUBLISHED_MINT = "PUBLISHED_MINT"
    #: An authoritative surface says this mint is NOT theirs.
    DISAVOWAL = "DISAVOWAL"
    #: An authoritative surface publishes a DIFFERENT mint as its token --
    #: the migration/stale-mint case.
    NAMES_DIFFERENT_MINT = "NAMES_DIFFERENT_MINT"
    #: A symbol or name matched. NEVER an identity.
    TICKER_ONLY = "TICKER_ONLY"
    #: Somebody referred to the mint. Not a claim of ownership.
    MENTION = "MENTION"


class CorroborationOutcome(str, Enum):
    AUTHORITATIVE_BINDING = "AUTHORITATIVE_BINDING"
    AUTHORITATIVE_CONFLICT = "AUTHORITATIVE_CONFLICT"
    NO_AUTHORITATIVE_EVIDENCE = "NO_AUTHORITATIVE_EVIDENCE"


@dataclass(frozen=True)
class CorroborationEvidence:
    """One normalized observation. Extracted by a model, judged by nobody.

    Carries no score, no confidence and no weight -- by construction, so a
    future caller cannot thresh01d its way to a verification.
    """
    kind: BindingKind
    authority: SourceAuthority
    scope: ProvenanceScope
    #: The mint this evidence speaks about -- NOT necessarily the candidate.
    subject_mint: str | None
    #: Stable reference to the raw material, so a decision is auditable.
    evidence_ref: str
    evidence_sha256: str
    observed_at_utc: str
    detail: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("kind", "authority", "scope"):
            d[k] = getattr(self, k).value
        return d


@dataclass(frozen=True)
class CorroborationDecision:
    outcome: CorroborationOutcome
    status: TokenResolutionStatus
    candidate_mint: str
    binding_refs: tuple = ()
    conflict_refs: tuple = ()
    ignored: tuple = ()
    reason: str = ""

    @property
    def canonically_verified(self) -> bool:
        return self.status is TokenResolutionStatus.CANONICALLY_VERIFIED

    def to_dict(self) -> dict:
        return {**asdict(self), "outcome": self.outcome.value,
                "status": self.status.value,
                "canonically_verified": self.canonically_verified}


def _binds(e: CorroborationEvidence, candidate: str) -> bool:
    """Does this single piece of evidence bind the CANDIDATE mint?

    Every clause is necessary and each corresponds to a named threat:
    the kind must be a publication (not a mention, not a ticker), the
    authority must be the project's own surface (not a third party, however
    many), the scope must not be quoted content (threat six), and the subject
    must be the candidate itself rather than some other mint.
    """
    return (e.kind is BindingKind.PUBLISHED_MINT
            and e.authority is SourceAuthority.OFFICIAL_PROJECT_SURFACE
            and e.scope is not ProvenanceScope.QUOTED
            and e.subject_mint == candidate)


def _conflicts(e: CorroborationEvidence, candidate: str) -> bool:
    """Does this evidence contradict the binding?

    An authoritative disavowal of the candidate, or an authoritative
    publication naming a DIFFERENT mint. Quoted content cannot conflict for
    the same reason it cannot bind: the outer account did not say it.
    """
    if e.authority is not SourceAuthority.OFFICIAL_PROJECT_SURFACE:
        return False
    if e.scope is ProvenanceScope.QUOTED:
        return False
    if e.kind is BindingKind.DISAVOWAL and e.subject_mint == candidate:
        return True
    if (e.kind is BindingKind.NAMES_DIFFERENT_MINT
            and e.subject_mint is not None and e.subject_mint != candidate):
        return True
    return False


def decide_corroboration(
    *,
    candidate_mint: str,
    chain_verified: bool,
    evidence: list[CorroborationEvidence],
) -> CorroborationDecision:
    """The ONLY door to `CANONICALLY_VERIFIED`. Deterministic, unweighted."""
    if not chain_verified:
        return CorroborationDecision(
            CorroborationOutcome.NO_AUTHORITATIVE_EVIDENCE,
            TokenResolutionStatus.TEXT_CANDIDATE, candidate_mint,
            reason="gate 1 did not pass; gate 2 is not consulted for an "
                   "address that is not a known mint")

    binds = [e for e in evidence if _binds(e, candidate_mint)]
    conflicts = [e for e in evidence if _conflicts(e, candidate_mint)]
    ignored = [e for e in evidence if e not in binds and e not in conflicts]

    if conflicts:
        # A conflict DOMINATES a binding. If an authoritative surface both
        # published this mint and disavowed it, the honest state is that we
        # do not know -- not that one of the two wins.
        return CorroborationDecision(
            CorroborationOutcome.AUTHORITATIVE_CONFLICT,
            TokenResolutionStatus.CONFLICTING_EVIDENCE, candidate_mint,
            binding_refs=tuple(e.evidence_ref for e in binds),
            conflict_refs=tuple(e.evidence_ref for e in conflicts),
            ignored=tuple(e.evidence_ref for e in ignored),
            reason="an authoritative surface contradicts this mint; a "
                   "conflict is never outvoted by a binding")

    if binds:
        return CorroborationDecision(
            CorroborationOutcome.AUTHORITATIVE_BINDING,
            TokenResolutionStatus.CANONICALLY_VERIFIED, candidate_mint,
            binding_refs=tuple(e.evidence_ref for e in binds),
            ignored=tuple(e.evidence_ref for e in ignored),
            reason="chain-verified, published by an official project surface "
                   "outside quoted content, and uncontradicted")

    return CorroborationDecision(
        CorroborationOutcome.NO_AUTHORITATIVE_EVIDENCE,
        TokenResolutionStatus.CORROBORATION_PENDING, candidate_mint,
        ignored=tuple(e.evidence_ref for e in ignored),
        reason="gate 2 ran and found no authoritative binding; this is a "
               "measured absence, not a refusal to look")
