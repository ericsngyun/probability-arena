"""The extractor produces EVIDENCE. The gate produces verdicts.

The adversarial corpus is the specification. Each case names the extraction
that would be wrong, not just the one that is right -- because the dangerous
failure is a plausible single-mint guess, not a parse error.
"""

from __future__ import annotations

import pytest

from app.social.evidence_extractor import (
    EXTRACTOR_VERSION, EvidenceClaim, ExtractionError, Relationship as R,
    SourceSurface as SS, SpanOrigin as SO, extract_claims, normalize,
    span_hash, to_gate2_evidence,
)
from app.seam.corroboration import (
    BindingKind as K, ProvenanceScope as P, SourceAuthority as A,
    decide_corroboration,
)
from app.seam.token import TokenResolutionStatus as T

X = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
Y = "So11111111111111111111111111111111111111112"
NOW = "2026-08-25T08:00:00Z"


def raw(mint, rel, surface, origin, *, span="s", who="acct", subject="Proj"):
    return {"candidate_mint": mint, "relationship": rel.value,
            "source_surface": surface.value, "span_origin": origin.value,
            "source_identity": who, "subject_entity": subject,
            "evidence_span": span}


class Stub:
    def __init__(self, claims): self.claims = claims
    def extract(self, artifact): return self.claims


def claims_for(raws):
    return extract_claims({"artifact_id": "a1"}, Stub(raws), model="stub-1")


def verdict(claims, candidate):
    return decide_corroboration(
        candidate_mint=candidate, chain_verified=True,
        evidence=[to_gate2_evidence(c, observed_at_utc=NOW) for c in claims])


# --- the boundary: no decision may travel through a claim -------------------

@pytest.mark.parametrize("bad", ["verified", "canonical", "confidence",
                                 "trade_signal", "score", "recommendation"])
def test_a_decision_field_is_refused_at_the_boundary(bad):
    r = raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)
    r[bad] = True
    with pytest.raises(ExtractionError, match="decision field"):
        normalize(r, artifact_id="a1", model="m")


def test_the_claim_type_has_no_decision_surface():
    for name in EvidenceClaim.__dataclass_fields__:
        assert not any(b in name.lower() for b in
                       ("verified", "canonical", "confidence", "score",
                        "signal", "recommend"))


def test_a_claim_must_be_auditable_back_to_its_span():
    with pytest.raises(ExtractionError, match="hash of the span"):
        EvidenceClaim("a", X, None, R.PUBLISHED_MINT, "who",
                      SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT, "", "m")


def test_an_unknown_relationship_is_refused_not_guessed():
    r = raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)
    r["relationship"] = "PROBABLY_THE_ONE"
    with pytest.raises(ExtractionError, match="unrecognised"):
        normalize(r, artifact_id="a1", model="m")


# --- the adversarial corpus -------------------------------------------------

def test_1_official_post_publishing_one_mint():
    c = claims_for([raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)])
    assert len(c) == 1 and c[0].relationship is R.PUBLISHED_MINT
    assert verdict(c, X).status is T.CANONICALLY_VERIFIED


def test_2_x_is_fake_y_is_official_yields_TWO_claims():
    """The case that forbids a single 'best mint' answer."""
    c = claims_for([
        raw(X, R.DISAVOWED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT),
        raw(Y, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT),
    ])
    assert len(c) == 2, "one artifact, two facts -- never collapsed to one"
    assert verdict(c, X).status is T.CONFLICTING_EVIDENCE
    assert verdict(c, Y).status is T.CANONICALLY_VERIFIED


def test_3_migration_a_to_b():
    c = claims_for([
        raw(X, R.MIGRATED_MINT, SS.CLAIMED_OFFICIAL_SITE, SO.DIRECT),
        raw(Y, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_SITE, SO.DIRECT),
    ])
    assert verdict(c, X).status is T.CONFLICTING_EVIDENCE
    assert verdict(c, Y).status is T.CANONICALLY_VERIFIED


def test_4_official_account_quoting_scam_content():
    c = claims_for([raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.QUOTED)])
    assert to_gate2_evidence(c[0], observed_at_utc=NOW).scope is P.QUOTED
    assert verdict(c, X).status is T.CORROBORATION_PENDING


def test_5_official_account_discussing_a_competitor_mint():
    c = claims_for([raw(Y, R.MENTIONED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)])
    assert verdict(c, Y).status is T.CORROBORATION_PENDING
    assert verdict(c, X).status is T.CORROBORATION_PENDING


def test_6_multiple_mints_in_one_post_all_surface():
    c = claims_for([
        raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT),
        raw(Y, R.MENTIONED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT),
    ])
    assert {cl.candidate_mint for cl in c} == {X, Y}
    assert verdict(c, X).status is T.CANONICALLY_VERIFIED
    assert verdict(c, Y).status is T.CORROBORATION_PENDING


def test_7_quoted_versus_source_authored_section_of_the_same_post():
    c = claims_for([
        raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.QUOTED),
        raw(Y, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT),
    ])
    assert verdict(c, X).status is T.CORROBORATION_PENDING
    assert verdict(c, Y).status is T.CANONICALLY_VERIFIED


def test_8_ticker_only_post_yields_no_mint_claim():
    """There is no mint, so there is nothing to claim -- not a guess."""
    assert claims_for([]) == []
    assert verdict([], X).status is T.CORROBORATION_PENDING


def test_9_screenshot_derived_mint_is_not_automatically_direct():
    c = claims_for([raw(X, R.MENTIONED_MINT, SS.UNKNOWN_SURFACE, SO.QUOTED)])
    e = to_gate2_evidence(c[0], observed_at_utc=NOW)
    assert e.authority is A.UNKNOWN and e.scope is P.QUOTED
    assert verdict(c, X).status is T.CORROBORATION_PENDING


def test_10_impersonator_copying_an_official_announcement():
    """Identical words, third-party surface. The text cannot save it."""
    c = claims_for([raw(X, R.PUBLISHED_MINT, SS.THIRD_PARTY_ACCOUNT, SO.DIRECT,
                        who="0fficial_proj")])
    assert to_gate2_evidence(c[0], observed_at_utc=NOW).authority is A.THIRD_PARTY
    assert verdict(c, X).status is T.CORROBORATION_PENDING


def test_11_third_party_claiming_to_represent_a_project():
    c = claims_for([raw(X, R.PUBLISHED_MINT, SS.THIRD_PARTY_ACCOUNT, SO.DIRECT,
                        subject="Proj (official partner)")])
    assert verdict(c, X).status is T.CORROBORATION_PENDING


def test_12_forwarded_content_is_treated_like_quoted():
    c = claims_for([raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT,
                        SO.FORWARDED)])
    assert to_gate2_evidence(c[0], observed_at_utc=NOW).scope is P.QUOTED
    assert verdict(c, X).status is T.CORROBORATION_PENDING


# --- adapter fidelity --------------------------------------------------------

def test_every_relationship_and_surface_maps_losslessly():
    for rel in R:
        for surf in SS:
            for org in SO:
                c = claims_for([raw(X, rel, surf, org)])[0]
                e = to_gate2_evidence(c, observed_at_utc=NOW)
                assert e.subject_mint == X
                assert e.evidence_sha256 == c.evidence_span_hash
                assert isinstance(e.kind, K)


def test_a_mention_never_becomes_a_publication():
    c = claims_for([raw(X, R.MENTIONED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)])
    assert to_gate2_evidence(c[0], observed_at_utc=NOW).kind is K.MENTION


def test_claims_carry_model_and_version_for_audit():
    c = claims_for([raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)])[0]
    assert c.extractor_model == "stub-1"
    assert c.extractor_version == EXTRACTOR_VERSION
    assert c.artifact_id == "a1"


def test_migration_and_disavowal_conflict_the_same_mint_but_stay_distinguishable():
    """For IDENTITY they are one claim: X is not the current token. The
    fake-vs-superseded distinction survives in `detail` for later use."""
    mig = claims_for([raw(X, R.MIGRATED_MINT, SS.CLAIMED_OFFICIAL_SITE, SO.DIRECT)])[0]
    dis = claims_for([raw(X, R.DISAVOWED_MINT, SS.CLAIMED_OFFICIAL_SITE, SO.DIRECT)])[0]
    em, ed = (to_gate2_evidence(c, observed_at_utc=NOW) for c in (mig, dis))
    assert em.kind is ed.kind is K.DISAVOWAL
    assert em.subject_mint == ed.subject_mint == X
    assert "MIGRATED_MINT" in em.detail and "DISAVOWED_MINT" in ed.detail
    assert em.detail != ed.detail


def test_a_migrated_mint_actually_conflicts():
    """The regression this mapping bug caused: a superseded mint silently
    failed to conflict because subject == candidate."""
    c = claims_for([raw(X, R.MIGRATED_MINT, SS.CLAIMED_OFFICIAL_SITE, SO.DIRECT)])
    assert verdict(c, X).status is T.CONFLICTING_EVIDENCE


@pytest.mark.parametrize("missing", ["relationship", "source_surface",
                                     "span_origin", "candidate_mint"])
def test_a_MISSING_field_is_refused_and_never_defaulted(missing):
    """An absent field must not become the strongest possible claim.

    A mutation defaulting a missing `relationship` to PUBLISHED_MINT passed
    26/26, because the suite only tested an INVALID value and never an ABSENT
    key. Silence from the model is not assent: if it did not say what the
    source asserted, there is no claim to make.
    """
    r = raw(X, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT)
    r.pop(missing)
    with pytest.raises(ExtractionError):
        normalize(r, artifact_id="a1", model="m")


def test_an_empty_claim_dict_is_refused():
    with pytest.raises(ExtractionError):
        normalize({}, artifact_id="a1", model="m")


def test_every_claim_the_model_returns_survives_to_the_gate():
    """Nothing may be silently dropped -- the gate needs all the facts."""
    rs = [raw(X, R.DISAVOWED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT, span="a"),
          raw(Y, R.PUBLISHED_MINT, SS.CLAIMED_OFFICIAL_ACCOUNT, SO.DIRECT, span="b"),
          raw(X, R.MENTIONED_MINT, SS.THIRD_PARTY_ACCOUNT, SO.QUOTED, span="c")]
    c = claims_for(rs)
    assert len(c) == len(rs) == 3
    assert len({cl.evidence_span_hash for cl in c}) == 3
