"""Who gets to say a mint is official — resolved deterministically.

Gate 2 verifies a mint when an **official project surface** publishes it. Until
now the extractor's *claim* that a surface was official became that authority
directly, which is a loophole with a name:

    a convincing impersonator publishes a real mint.
    Gate 1 passes. The text extractor is perfectly correct.
    Gate 2 verifies the wrong token.

Gate 2 is only ever as good as the provenance classification feeding it. So
authority is now its own resolution stage with its own typed states, and the
same division of labour applies one level up:

    model:  artifact/surface  ->  typed AUTHORITY EVIDENCE
    policy: authority evidence -> AuthorityState

The model may observe *"this site's footer links to @proj"*. It may not
conclude *"@proj is the official account"*. Only `resolve_authority` does that,
and it has no numeric input to threshold on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class AuthorityState(str, Enum):
    """Closed vocabulary. Only the first can bind a mint identity."""

    #: A mutual link between two surfaces the project controls -- the site
    #: names the account AND the account names the domain. Neither direction
    #: alone is enough: anyone can link to a project's website, and a hijacked
    #: site can name anyone.
    AUTHORITATIVE = "AUTHORITATIVE"
    #: One-directional linkage supported by an independent surface (launchpad
    #: page, on-chain metadata). Real evidence, but not mutual attestation.
    CORROBORATED = "CORROBORATED"
    #: Nothing was found. A measured absence, not a judgement.
    UNVERIFIED = "UNVERIFIED"
    #: An authoritative surface names a DIFFERENT identity for this project.
    IMPERSONATOR = "IMPERSONATOR"
    #: Authoritative surfaces disagree with each other.
    CONFLICTING = "CONFLICTING"


#: The only state that may become an official project surface downstream.
#: Widening this is a milestone decision, not a code change, and it is asserted
#: in the tests.
BINDING_STATES = frozenset({AuthorityState.AUTHORITATIVE})


class AuthorityEvidenceKind(str, Enum):
    #: The project's site links to this account.
    SITE_LINKS_ACCOUNT = "SITE_LINKS_ACCOUNT"
    #: This account's profile links back to the project's domain.
    ACCOUNT_LINKS_DOMAIN = "ACCOUNT_LINKS_DOMAIN"
    #: A launchpad/registry page identifies this account as the project's.
    LAUNCHPAD_IDENTIFIES_ACCOUNT = "LAUNCHPAD_IDENTIFIES_ACCOUNT"
    #: On-chain metadata references this domain or account.
    ONCHAIN_METADATA_REFERENCES = "ONCHAIN_METADATA_REFERENCES"
    #: An authoritative surface names a DIFFERENT account for this project.
    NAMES_DIFFERENT_ACCOUNT = "NAMES_DIFFERENT_ACCOUNT"
    #: A check ran and found nothing. Recorded, because silence is not evidence.
    NEGATIVE_CHECK = "NEGATIVE_CHECK"


@dataclass(frozen=True)
class AuthorityEvidence:
    """One observation about who controls a surface. Carries no score."""
    kind: AuthorityEvidenceKind
    project: str
    #: The account/surface this evidence is about.
    subject_source_id: str
    #: For NAMES_DIFFERENT_ACCOUNT, the identity the surface actually named.
    named_source_id: str | None
    evidence_ref: str
    evidence_sha256: str
    observed_at_utc: str
    detail: str | None = None

    def to_dict(self) -> dict:
        return {**asdict(self), "kind": self.kind.value}


@dataclass(frozen=True)
class AuthorityResolution:
    state: AuthorityState
    project: str
    source_id: str
    supporting_refs: tuple = ()
    conflicting_refs: tuple = ()
    resolver_version: str = "source-authority-v1"
    reason: str = ""

    @property
    def can_bind(self) -> bool:
        """The only sanctioned read. Never branch on `state` directly."""
        return self.state in BINDING_STATES

    def to_dict(self) -> dict:
        return {**asdict(self), "state": self.state.value,
                "can_bind": self.can_bind}


K = AuthorityEvidenceKind


def resolve_authority(*, project: str, source_id: str,
                      evidence: list[AuthorityEvidence]) -> AuthorityResolution:
    """Deterministic. No thresholds, no counting-as-strength."""
    mine = [e for e in evidence
            if e.project == project and e.subject_source_id == source_id]
    others = [e for e in evidence
              if e.project == project and e.kind is K.NAMES_DIFFERENT_ACCOUNT
              and e.named_source_id not in (None, source_id)]

    site_to_acct = [e for e in mine if e.kind is K.SITE_LINKS_ACCOUNT]
    acct_to_site = [e for e in mine if e.kind is K.ACCOUNT_LINKS_DOMAIN]
    independent = [e for e in mine if e.kind in
                   (K.LAUNCHPAD_IDENTIFIES_ACCOUNT, K.ONCHAIN_METADATA_REFERENCES)]

    mutual = bool(site_to_acct and acct_to_site)

    # An authoritative surface naming someone else DOMINATES. If the project's
    # own site says the official account is @a, then @b is an impersonator
    # however convincingly @b links to the domain -- anyone may link outward.
    if others:
        state = (AuthorityState.CONFLICTING if mutual
                 else AuthorityState.IMPERSONATOR)
        return AuthorityResolution(
            state, project, source_id,
            supporting_refs=tuple(e.evidence_ref for e in mine),
            conflicting_refs=tuple(e.evidence_ref for e in others),
            reason=("an authoritative surface names a different account for "
                    "this project" + ("; and this account also has mutual "
                                      "linkage, so the surfaces disagree"
                                      if mutual else "")))
    if mutual:
        return AuthorityResolution(
            AuthorityState.AUTHORITATIVE, project, source_id,
            supporting_refs=tuple(e.evidence_ref
                                  for e in site_to_acct + acct_to_site),
            reason="mutual attestation: the project's surface names this "
                   "account and the account names the project's domain")
    if (site_to_acct or acct_to_site) and independent:
        return AuthorityResolution(
            AuthorityState.CORROBORATED, project, source_id,
            supporting_refs=tuple(e.evidence_ref
                                  for e in mine if e in site_to_acct
                                  or e in acct_to_site or e in independent),
            reason="one-directional linkage supported by an independent "
                   "surface; real evidence, but not mutual attestation")
    return AuthorityResolution(
        AuthorityState.UNVERIFIED, project, source_id,
        supporting_refs=tuple(e.evidence_ref for e in mine),
        reason="no mutual attestation and no independent corroboration; a "
               "measured absence rather than a judgement")
