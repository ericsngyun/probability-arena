"""The full path, end to end, with the impersonator as the headline case.

    raw artifact -> LLM claims -> authority resolution -> chain gate
                 -> semantic corroboration -> seam qualification

Unit tests prove each stage. This proves the STAGES COMPOSE: that a failure at
one layer is not silently repaired by a later one, and that the terminal
verdict traces back to the layer that actually refused.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.seam import chain_identity as CI
from app.seam import fill_seam as FS
from app.seam.chain_identity import ChainVerdict as CV
from app.seam.corroboration import decide_corroboration
from app.seam.fill_seam import SeamRefusal as RF
from app.seam.token import TokenResolutionStatus as T
from app.social.evidence_extractor import (
    Relationship as R, SourceSurface as SS, SpanOrigin as SO, extract_claims,
    to_gate2_evidence,
)
from app.social.source_authority import (
    AuthorityEvidence, AuthorityEvidenceKind as AK, AuthorityState,
    resolve_authority,
)

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
NOW = "2026-08-26T20:00:00Z"


def mint_bytes():
    b = bytearray()
    b += (1).to_bytes(4, "little") + CI.base58_decode(AUTH)
    b += (1_000_000_000).to_bytes(8, "little")
    b += bytes([6, 1])
    b += (0).to_bytes(4, "little") + b"\x00" * 32
    return bytes(b)


class Reader:
    def __init__(self, m): self.m = m
    def get_account_info(self, a): return self.m.get(a)


LIVE_CHAIN = Reader({MINT: {"owner": CI.SPL_TOKEN_PROGRAM,
                            "data": [base64.b64encode(mint_bytes()).decode(),
                                     "base64"]}})


class Stub:
    def __init__(self, c): self.c = c
    def extract(self, artifact): return self.c


def aev(kind, subject, named=None, ref="e", project="Proj"):
    return AuthorityEvidence(kind=kind, project=project,
                             subject_source_id=subject, named_source_id=named,
                             evidence_ref=ref, evidence_sha256="0" * 64,
                             observed_at_utc=NOW)


def stamp(offset_us=0, host="h1"):
    import app.seam.clock as C
    from app.seam.clock import HostBootId
    boot = HostBootId.from_json({"status": "PRESENT", "value": "b" * 36})
    base_wall = datetime(2026, 8, 26, 20, 0, 0, tzinfo=timezone.utc)
    return C.capture_observation(
        process_epoch_id="e1", boot_id=boot, host=host,
        clock=lambda: (base_wall + timedelta(microseconds=offset_us),
                       1_000_000_000_000 + offset_us * 1000))


def run_full_path(*, source_id, authority_evidence, span_origin=SO.DIRECT,
                  relationship=R.PUBLISHED_MINT,
                  surface=SS.CLAIMED_OFFICIAL_ACCOUNT):
    """Every stage, in order, exactly as production would."""
    chain = CI.verify_chain_existence(MINT, LIVE_CHAIN)
    authority = resolve_authority(project="Proj", source_id=source_id,
                                  evidence=authority_evidence)
    claims = extract_claims(
        {"artifact_id": "art-1"},
        Stub([{"candidate_mint": MINT, "relationship": relationship.value,
               "source_surface": surface.value, "span_origin": span_origin.value,
               "source_identity": source_id, "subject_entity": "Proj",
               "evidence_span": "the official contract is ..."}]),
        model="stub-1")
    gate2 = decide_corroboration(
        candidate_mint=MINT, chain_verified=chain.verified,
        evidence=[to_gate2_evidence(c, observed_at_utc=NOW, authority=authority)
                  for c in claims])
    seam = FS.qualify(
        token_status=gate2.status, social_mint=MINT, market_mint=MINT,
        delivery_mode="LIVE", social_received=stamp(0),
        quote_observed=stamp(900_000), source_can_bind=authority.can_bind,
        chain_observed=True)
    return chain, authority, gate2, seam


# --- THE headline case -------------------------------------------------------

def test_impersonator_with_a_real_mint_is_NOT_JOINABLE_and_fails_at_authority():
    """Everything except provenance is genuine and correct.

    The mint is real and initialized. The extraction is perfect -- a direct,
    source-authored PUBLISHED_MINT. The impersonator even links outward to the
    legitimate project website, which anyone can do. What it cannot produce is
    the project's own surface naming it back.
    """
    chain, authority, gate2, seam = run_full_path(
        source_id="fake_proj",
        authority_evidence=[
            aev(AK.ACCOUNT_LINKS_DOMAIN, "fake_proj", ref="fake-links-out"),
            aev(AK.SITE_LINKS_ACCOUNT, "real_proj", ref="site-names-real"),
            aev(AK.NAMES_DIFFERENT_ACCOUNT, "fake_proj", named="real_proj",
                ref="site-says-real"),
        ])

    # the token is genuinely fine -- the failure is NOT about token validity
    assert chain.verdict is CV.CHAIN_VERIFIED
    assert chain.facts.decimals == 6

    # and the extraction is genuinely fine -- NOT a semantic failure either
    # (a PUBLISHED_MINT claim was produced, direct and source-authored)

    # the refusal is at PROVENANCE
    assert authority.state is AuthorityState.IMPERSONATOR
    assert authority.can_bind is False

    assert gate2.status is T.CORROBORATION_PENDING
    assert gate2.canonically_verified is False

    assert seam.joinable is False
    assert seam.verdict is FS.SeamVerdict.SOCIAL_FILL_NOT_JOINABLE
    assert RF.SOURCE_NOT_AUTHORITATIVE in seam.refusals
    assert RF.TOKEN_NOT_CANONICAL in seam.refusals   # downstream of authority

    # and crucially NOT these -- the trace must not blame the wrong layer
    assert RF.MINT_MISMATCH not in seam.refusals
    assert RF.CLOCK_NOT_COMPUTABLE not in seam.refusals
    assert RF.CHAIN_OBSERVATION_MISSING not in seam.refusals


def test_the_same_artifact_from_the_ATTESTED_account_is_joinable():
    """The only difference is reciprocal attestation."""
    chain, authority, gate2, seam = run_full_path(
        source_id="real_proj",
        authority_evidence=[
            aev(AK.SITE_LINKS_ACCOUNT, "real_proj", ref="site"),
            aev(AK.ACCOUNT_LINKS_DOMAIN, "real_proj", ref="acct"),
        ])
    assert authority.state is AuthorityState.AUTHORITATIVE
    assert gate2.status is T.CANONICALLY_VERIFIED
    assert seam.joinable is True
    assert seam.refusals == ()


# --- no later stage repairs an earlier refusal -------------------------------

def test_a_dead_chain_cannot_be_rescued_by_perfect_provenance():
    chain = CI.verify_chain_existence(MINT, Reader({}))          # NOT_FOUND
    authority = resolve_authority(project="Proj", source_id="real_proj",
                                  evidence=[aev(AK.SITE_LINKS_ACCOUNT, "real_proj", ref="s"),
                                            aev(AK.ACCOUNT_LINKS_DOMAIN, "real_proj", ref="a")])
    assert authority.can_bind is True
    claims = extract_claims({"artifact_id": "a"}, Stub([{
        "candidate_mint": MINT, "relationship": R.PUBLISHED_MINT.value,
        "source_surface": SS.CLAIMED_OFFICIAL_ACCOUNT.value,
        "span_origin": SO.DIRECT.value, "source_identity": "real_proj",
        "evidence_span": "x"}]), model="m")
    gate2 = decide_corroboration(candidate_mint=MINT,
                                 chain_verified=chain.verified,
                                 evidence=[to_gate2_evidence(c, observed_at_utc=NOW,
                                                             authority=authority)
                                           for c in claims])
    assert gate2.status is T.TEXT_CANDIDATE
    seam = FS.qualify(token_status=gate2.status, social_mint=MINT,
                      market_mint=MINT, delivery_mode="LIVE",
                      social_received=stamp(0), quote_observed=stamp(1000),
                      source_can_bind=True, chain_observed=True)
    assert seam.joinable is False and RF.TOKEN_NOT_CANONICAL in seam.refusals


def test_quoted_content_from_an_attested_account_still_fails():
    """Authority is necessary, not sufficient: the outer account still did not
    author the span."""
    _, authority, gate2, seam = run_full_path(
        source_id="real_proj", span_origin=SO.QUOTED,
        authority_evidence=[aev(AK.SITE_LINKS_ACCOUNT, "real_proj", ref="s"),
                            aev(AK.ACCOUNT_LINKS_DOMAIN, "real_proj", ref="a")])
    assert authority.can_bind is True
    assert gate2.status is T.CORROBORATION_PENDING
    assert seam.joinable is False


def test_backfill_delivery_defeats_an_otherwise_perfect_path():
    chain, authority, gate2, _ = run_full_path(
        source_id="real_proj",
        authority_evidence=[aev(AK.SITE_LINKS_ACCOUNT, "real_proj", ref="s"),
                            aev(AK.ACCOUNT_LINKS_DOMAIN, "real_proj", ref="a")])
    assert gate2.status is T.CANONICALLY_VERIFIED
    seam = FS.qualify(token_status=gate2.status, social_mint=MINT,
                      market_mint=MINT, delivery_mode="BACKFILL",
                      social_received=stamp(0), quote_observed=stamp(1000),
                      source_can_bind=True, chain_observed=True)
    assert seam.joinable is False
    assert seam.refusals == (RF.DELIVERY_NOT_LIVE,), (
        "exactly one thing was wrong and exactly one refusal should say so")


def test_corroborated_authority_is_not_enough_end_to_end():
    """Two weak signals must not become an authoritative identity claim."""
    _, authority, gate2, seam = run_full_path(
        source_id="acct",
        authority_evidence=[aev(AK.SITE_LINKS_ACCOUNT, "acct", ref="one-way"),
                            aev(AK.LAUNCHPAD_IDENTIFIES_ACCOUNT, "acct", ref="lp")])
    assert authority.state is AuthorityState.CORROBORATED
    assert gate2.canonically_verified is False
    assert seam.joinable is False
