"""Gate 2 — semantic corroboration. The eight adversarial cases are the spec.

Gate 1 asks: is this a real Solana mint?
Gate 2 asks: does this social artifact actually refer to that mint canonically?

Every case below involves a mint that is REAL. That is the point: chain
existence is assumed throughout, so nothing here can pass by accident of the
address being valid.
"""

from __future__ import annotations

import pytest

from app.seam.corroboration import (
    BindingKind as K, CorroborationEvidence as E, CorroborationOutcome as O,
    ProvenanceScope as S, SourceAuthority as A, decide_corroboration,
)
from app.seam.token import JOINABLE_STATUSES, TokenResolutionStatus as T

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER = "So11111111111111111111111111111111111111112"


def ev(kind, authority, scope, subject=MINT, ref="ref-1"):
    return E(kind=kind, authority=authority, scope=scope, subject_mint=subject,
             evidence_ref=ref, evidence_sha256="0" * 64,
             observed_at_utc="2026-08-25T00:00:00Z")


def decide(evidence, *, chain=True, candidate=MINT):
    return decide_corroboration(candidate_mint=candidate, chain_verified=chain,
                                evidence=evidence)


# --- 1. official post publishes the correct mint -> PASSES ------------------

def test_case1_official_source_authored_publication_verifies():
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED)])
    assert d.outcome is O.AUTHORITATIVE_BINDING
    assert d.status is T.CANONICALLY_VERIFIED
    assert d.canonically_verified is True
    assert d.status in JOINABLE_STATUSES


def test_case1b_official_publication_reached_by_link_also_verifies():
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE, S.LINKED)])
    assert d.canonically_verified is True


# --- 2. valid mint only in QUOTED content -> does NOT pass ------------------

def test_case2_quoted_content_cannot_bind():
    """Threat 6: quote-posted scam content inside a legitimate outer post."""
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE, S.QUOTED)])
    assert d.canonically_verified is False
    assert d.status is T.CORROBORATION_PENDING
    assert "ref-1" in d.ignored


# --- 3. post discusses ANOTHER project's mint -> does NOT pass --------------

def test_case3_publication_about_a_different_mint_does_not_verify_the_candidate():
    """Threat 4: competitor token mentioned in passing."""
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE,
                   S.SOURCE_AUTHORED, subject=OTHER)])
    assert d.canonically_verified is False
    assert d.status is T.CORROBORATION_PENDING


# --- 4. old/migrated mint vs new canonical mint -> CONFLICT ----------------

def test_case4_migration_produces_conflict_not_silent_verification():
    """Threat 2: an old mint quoted in a stale post."""
    d = decide([
        ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED,
           ref="stale-post"),
        ev(K.NAMES_DIFFERENT_MINT, A.OFFICIAL_PROJECT_SURFACE,
           S.LINKED, subject=OTHER, ref="current-site"),
    ])
    assert d.outcome is O.AUTHORITATIVE_CONFLICT
    assert d.status is T.CONFLICTING_EVIDENCE
    assert d.canonically_verified is False
    assert "current-site" in d.conflict_refs


def test_case4b_a_conflict_is_never_outvoted_by_bindings():
    many = [ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED,
               ref=f"bind-{i}") for i in range(10)]
    many.append(ev(K.DISAVOWAL, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED,
                   ref="disavowal"))
    d = decide(many)
    assert d.status is T.CONFLICTING_EVIDENCE
    assert len(d.binding_refs) == 10, "the bindings are recorded, not deleted"


# --- 5. impersonator posts a real mint -> does NOT pass ---------------------

def test_case5_impersonator_cannot_bind():
    """Threat 1: a decoy address in a scam post. The mint is real."""
    d = decide([ev(K.PUBLISHED_MINT, A.THIRD_PARTY, S.SOURCE_AUTHORED,
                   ref="impersonator")])
    assert d.canonically_verified is False
    assert d.status is T.CORROBORATION_PENDING
    d2 = decide([ev(K.PUBLISHED_MINT, A.UNKNOWN, S.SOURCE_AUTHORED)])
    assert d2.canonically_verified is False


# --- 6. official warns "X is fake, Y is official" ---------------------------

def test_case6_disavowal_conflicts_x_while_y_can_verify():
    x = decide([ev(K.DISAVOWAL, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED,
                   subject=MINT, ref="warning")], candidate=MINT)
    assert x.status is T.CONFLICTING_EVIDENCE

    y = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE,
                   S.SOURCE_AUTHORED, subject=OTHER, ref="official")],
               candidate=OTHER)
    assert y.status is T.CANONICALLY_VERIFIED


# --- 7. same ticker across multiple valid mints -----------------------------

def test_case7_ticker_can_never_resolve_identity():
    d = decide([ev(K.TICKER_ONLY, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED)])
    assert d.canonically_verified is False
    assert d.status is T.CORROBORATION_PENDING
    # even many tickers from official surfaces
    d2 = decide([ev(K.TICKER_ONLY, A.OFFICIAL_PROJECT_SURFACE,
                    S.SOURCE_AUTHORED, ref=f"t{i}") for i in range(50)])
    assert d2.canonically_verified is False


# --- 8. repetition cannot substitute for authority --------------------------

def test_case8_many_low_authority_repetitions_sum_to_nothing():
    d = decide([ev(K.PUBLISHED_MINT, A.THIRD_PARTY, S.SOURCE_AUTHORED,
                   ref=f"acct-{i}") for i in range(1000)])
    assert d.canonically_verified is False
    assert d.status is T.CORROBORATION_PENDING
    assert len(d.ignored) == 1000, "every one was seen and none counted"


def test_a_mention_by_an_official_account_is_not_ownership():
    """No transitivity: 'official account mentioned this mint' != 'owns it'."""
    d = decide([ev(K.MENTION, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED)])
    assert d.canonically_verified is False


# --- structural properties --------------------------------------------------

def test_gate1_failure_short_circuits_gate2():
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE,
                   S.SOURCE_AUTHORED)], chain=False)
    assert d.status is T.TEXT_CANDIDATE
    assert d.canonically_verified is False


def test_no_evidence_at_all_is_pending_not_rejected():
    d = decide([])
    assert d.status is T.CORROBORATION_PENDING
    assert "measured absence" in d.reason


def test_there_is_no_score_anywhere_to_threshold_on():
    for name in E.__dataclass_fields__:
        assert not any(b in name.lower() for b in
                       ("score", "confidence", "weight", "probability", "rank"))
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE, S.SOURCE_AUTHORED)])
    for k in d.to_dict():
        assert not any(b in k.lower() for b in ("score", "confidence", "weight"))


def test_canonically_verified_is_still_the_only_joinable_status():
    assert JOINABLE_STATUSES == frozenset({T.CANONICALLY_VERIFIED})
    for st in (T.CHAIN_VERIFIED, T.CORROBORATION_PENDING,
               T.CONFLICTING_EVIDENCE, T.TEXT_CANDIDATE, T.AMBIGUOUS,
               T.REJECTED, T.RESOLVED_FROM_PROJECT, T.RESOLVED_FROM_ALIAS):
        assert st not in JOINABLE_STATUSES


def test_every_decision_is_auditable_to_its_evidence():
    d = decide([ev(K.PUBLISHED_MINT, A.OFFICIAL_PROJECT_SURFACE,
                   S.SOURCE_AUTHORED, ref="bind"),
                ev(K.TICKER_ONLY, A.THIRD_PARTY, S.QUOTED, ref="noise")])
    assert d.binding_refs == ("bind",)
    assert "noise" in d.ignored
    assert d.reason
