"""SOLANA-TOKEN-IDENTITY-VERIFICATION-001 gate 1 — chain existence.

The negative cases matter more than the happy path. A gate that only proves a
real mint verifies is a gate that has not been tested: every one of the six
threats in `token.THREATS` involves an address that is *perfectly real* and
still wrong.
"""

from __future__ import annotations

import base64

import pytest

from app.seam import chain_identity as CI
from app.seam.chain_identity import ChainVerdict as V

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"      # a real-shaped mint
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


def mint_bytes(*, supply=1_000_000_000, decimals=6, mint_authority=AUTH,
               freeze_authority=None, initialized=True, length=None):
    b = bytearray()
    if mint_authority:
        b += (1).to_bytes(4, "little") + CI.base58_decode(mint_authority)
    else:
        b += (0).to_bytes(4, "little") + b"\x00" * 32
    b += supply.to_bytes(8, "little")
    b += bytes([decimals, 1 if initialized else 0])
    if freeze_authority:
        b += (1).to_bytes(4, "little") + CI.base58_decode(freeze_authority)
    else:
        b += (0).to_bytes(4, "little") + b"\x00" * 32
    out = bytes(b)
    return out[:length] if length else out


class Reader:
    def __init__(self, mapping): self.mapping = mapping
    def get_account_info(self, address): return self.mapping.get(address)


def acct(owner, data: bytes | None):
    return {"owner": owner,
            "data": [base64.b64encode(data).decode(), "base64"] if data else None}


# --- base58 and pubkey shape ------------------------------------------------

def test_base58_roundtrips():
    for s in (MINT, WALLET, AUTH, CI.SYSTEM_PROGRAM):
        assert CI.base58_encode(CI.base58_decode(s)) == s


def test_non_base58_is_chain_invalid_not_not_found():
    for bad in ("not-a-key!", "0OIl", "", "abc def"):
        r = CI.verify_chain_existence(bad, Reader({}))
        assert r.verdict is V.CHAIN_INVALID
        assert r.verified is False


def test_wrong_length_pubkey_is_refused():
    assert CI.is_valid_pubkey(MINT) is True
    assert CI.is_valid_pubkey("abc") is False


# --- the happy path, once -----------------------------------------------------

def test_a_real_initialized_mint_verifies_with_facts():
    r = CI.verify_chain_existence(
        MINT, Reader({MINT: acct(CI.SPL_TOKEN_PROGRAM, mint_bytes())}))
    assert r.verified is True
    f = r.facts
    assert f.mint == MINT and f.token_program == CI.SPL_TOKEN_PROGRAM
    assert f.decimals == 6 and f.supply == 1_000_000_000
    assert f.mint_authority == AUTH and f.freeze_authority is None
    assert f.account_len == CI.MINT_ACCOUNT_LEN


def test_token_2022_is_also_a_known_program():
    r = CI.verify_chain_existence(
        MINT, Reader({MINT: acct(CI.SPL_TOKEN_2022_PROGRAM, mint_bytes())}))
    assert r.verified is True
    assert r.facts.token_program == CI.SPL_TOKEN_2022_PROGRAM


def test_renounced_authorities_are_none_not_a_zero_pubkey():
    """A renounced authority is ABSENT, not an address of all zeros."""
    r = CI.verify_chain_existence(MINT, Reader({MINT: acct(
        CI.SPL_TOKEN_PROGRAM, mint_bytes(mint_authority=None))}))
    assert r.verified is True
    assert r.facts.mint_authority is None
    assert r.facts.mint_authority != "1" * 32


# --- the adversarial cases, which are the point -----------------------------

def test_a_valid_wallet_address_fails_the_type_gate():
    """Threat: an address copied from an unrelated source. Perfectly real."""
    r = CI.verify_chain_existence(
        WALLET, Reader({WALLET: acct(CI.SYSTEM_PROGRAM, b"")}))
    assert r.verified is False
    assert r.verdict is V.WRONG_ACCOUNT_TYPE
    assert "wallet" in (r.detail or "").lower()
    assert r.observed_owner == CI.SYSTEM_PROGRAM


def test_a_token_account_is_not_the_mint():
    """Someone's BALANCE of the token, not the token. Same program, 165 bytes.

    Asserts the DISTINGUISHING detail, not merely the verdict. A 165-byte
    account also fails the generic `!= 82` check, so a test matching only on
    the verdict -- or on the word "account", which both messages carry --
    cannot tell whether the dedicated branch exists. A mutation deleting it
    passed 17/17 against the weaker form.
    """
    r = CI.verify_chain_existence(WALLET, Reader({
        WALLET: acct(CI.SPL_TOKEN_PROGRAM, b"\x00" * CI.TOKEN_ACCOUNT_LEN)}))
    assert r.verdict is V.WRONG_ACCOUNT_TYPE
    assert r.observed_len == CI.TOKEN_ACCOUNT_LEN
    assert "balance" in (r.detail or "").lower(), (
        "a token account must be named as a BALANCE, not reported with the "
        "generic wrong-length message -- the two are different mistakes and "
        "an operator needs to know which one happened")


def test_confidence_cannot_be_added_to_the_verification_surface():
    """Guards the no-threshold property against a future field."""
    for cls in (CI.ChainVerification, CI.MintFacts):
        for name, f in cls.__dataclass_fields__.items():
            assert not any(b in name.lower() for b in
                           ("score", "confidence", "probability", "likelihood"))
            assert f.type not in ("float | None",) or "score" not in name


def test_an_unknown_program_owner_is_refused_distinctly():
    r = CI.verify_chain_existence(MINT, Reader({
        MINT: acct("Stake11111111111111111111111111111111111111", mint_bytes())}))
    assert r.verdict is V.UNKNOWN_TOKEN_PROGRAM
    assert r.verdict is not V.WRONG_ACCOUNT_TYPE


def test_an_uninitialized_mint_does_not_verify():
    r = CI.verify_chain_existence(MINT, Reader({
        MINT: acct(CI.SPL_TOKEN_PROGRAM, mint_bytes(initialized=False))}))
    assert r.verdict is V.UNINITIALIZED_MINT
    assert r.verified is False
    assert r.facts is None


def test_a_missing_account_is_not_found():
    r = CI.verify_chain_existence(MINT, Reader({}))
    assert r.verdict is V.NOT_FOUND
    assert r.verified is False


def test_a_truncated_mint_buffer_is_refused_not_padded():
    r = CI.verify_chain_existence(MINT, Reader({
        MINT: acct(CI.SPL_TOKEN_PROGRAM, mint_bytes(length=40))}))
    assert r.verified is False
    assert r.verdict is V.WRONG_ACCOUNT_TYPE


def test_a_corrupt_coption_tag_is_refused():
    bad = bytearray(mint_bytes())
    bad[0:4] = (7).to_bytes(4, "little")
    r = CI.verify_chain_existence(MINT, Reader({
        MINT: acct(CI.SPL_TOKEN_PROGRAM, bytes(bad))}))
    assert r.verdict is V.CHAIN_INVALID
    assert "COption" in (r.detail or "")


# --- an RPC failure is not a rejection --------------------------------------

def test_rpc_failure_is_unavailable_not_a_verdict_about_the_address():
    class Broken:
        def get_account_info(self, address):
            raise ConnectionError("rpc down")
    r = CI.verify_chain_existence(MINT, Broken())
    assert r.verdict is V.UNAVAILABLE
    assert r.verdict is not V.NOT_FOUND, (
        "an unreachable RPC must never be cached as 'this token does not exist'")
    assert r.verified is False


# --- no confidence surface --------------------------------------------------

def test_there_is_no_score_to_threshold_on():
    r = CI.verify_chain_existence(
        MINT, Reader({MINT: acct(CI.SPL_TOKEN_PROGRAM, mint_bytes())}))
    d = r.to_dict()
    for banned in ("score", "confidence", "probability", "likelihood", "weight"):
        assert not any(banned in k.lower() for k in d), f"{banned} is exposed"
    for f in (CI.ChainVerification, CI.MintFacts):
        for name in f.__dataclass_fields__:
            assert not any(b in name.lower() for b in
                           ("score", "confidence", "probability"))


def test_verified_is_the_only_sanctioned_read():
    ok = CI.verify_chain_existence(
        MINT, Reader({MINT: acct(CI.SPL_TOKEN_PROGRAM, mint_bytes())}))
    assert ok.verified is True
    for verdict in V:
        if verdict is not V.CHAIN_VERIFIED:
            assert CI.ChainVerification(verdict, MINT).verified is False


def test_chain_existence_alone_defeats_none_of_the_named_threats():
    """The whole reason gate 2 exists, asserted here so it cannot be forgotten."""
    from app.seam.token import THREATS
    r = CI.verify_chain_existence(
        MINT, Reader({MINT: acct(CI.SPL_TOKEN_PROGRAM, mint_bytes())}))
    assert r.verified is True
    # a competitor's genuine mint quoted in a scam post passes THIS gate
    assert "decoy address in a scam post" in THREATS
    assert "competitor token mentioned in passing" in THREATS
    assert not hasattr(r, "canonically_verified")
