"""SOLANA-TOKEN-IDENTITY-VERIFICATION-001 — gate 1: chain existence.

Establishes deterministically that a base58 candidate really is the intended
Solana **token mint**, and collects the authoritative chain facts. It answers
exactly one question and refuses to answer any other:

    is this address a real, initialized SPL mint, and what are its facts?

It does **not** establish that a social artifact referred to that token. That
is gate 2 (semantic corroboration), deliberately separate, because "the string
is a real mint" defeats none of the six threats in `token.THREATS` -- a scam
post quoting a genuine competitor's mint passes this gate cleanly and must
still be rejected downstream.

**Every failure is a typed rejection, never a confidence score.** There is no
field on which a caller could threshold, because "probably the right token" is
not a state this system is allowed to hold.

Network access is injected. The decoder is pure, so the adversarial cases --
a wallet address, a token *account* rather than a mint, an uninitialized mint,
a truncated buffer -- are all exercised without a socket.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Program identities. A mint is only a mint under a token program we know.
# ---------------------------------------------------------------------------
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
KNOWN_TOKEN_PROGRAMS = frozenset({SPL_TOKEN_PROGRAM, SPL_TOKEN_2022_PROGRAM})

SYSTEM_PROGRAM = "11111111111111111111111111111111"

#: The canonical SPL mint layout. Token *accounts* are 165 bytes, so size
#: alone separates "this token" from "somebody's balance of some token" --
#: a distinction that silently ruins a join if missed.
MINT_ACCOUNT_LEN = 82
TOKEN_ACCOUNT_LEN = 165

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ChainVerdict(str, Enum):
    """Closed vocabulary. No member means "probably"."""

    CHAIN_VERIFIED = "CHAIN_VERIFIED"
    #: The address is well-formed but nothing lives there.
    NOT_FOUND = "NOT_FOUND"
    #: Something lives there, but it is not a mint -- a wallet, a program, a
    #: token account, a PDA of some other kind.
    WRONG_ACCOUNT_TYPE = "WRONG_ACCOUNT_TYPE"
    #: A mint owned by a program we do not recognise as a token program.
    UNKNOWN_TOKEN_PROGRAM = "UNKNOWN_TOKEN_PROGRAM"
    #: Mint-shaped but `is_initialized` is false.
    UNINITIALIZED_MINT = "UNINITIALIZED_MINT"
    #: The string is not a valid base58 pubkey at all.
    CHAIN_INVALID = "CHAIN_INVALID"
    #: The RPC could not answer. NOT a rejection of the address -- absence of
    #: evidence, recorded as such so it can be retried rather than cached as
    #: a verdict.
    UNAVAILABLE = "UNAVAILABLE"


class ChainIdentityError(Exception):
    pass


def base58_decode(s: str) -> bytes:
    """Pure base58 decode. Raises rather than returning a sentinel."""
    if not s or any(c not in _B58 for c in s):
        raise ChainIdentityError(f"not base58: {s[:16]!r}")
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def is_valid_pubkey(s: str) -> bool:
    try:
        return len(base58_decode(s)) == 32
    except ChainIdentityError:
        return False


@dataclass(frozen=True)
class MintFacts:
    """Authoritative facts, or nothing. Never partially guessed."""
    mint: str
    token_program: str
    decimals: int
    supply: int
    mint_authority: str | None          # None means authority RENOUNCED
    freeze_authority: str | None        # None means no freeze authority
    is_initialized: bool
    account_len: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChainVerification:
    verdict: ChainVerdict
    address: str
    facts: MintFacts | None = None
    detail: str | None = None
    #: Raw account owner as reported, kept even on rejection so a wrong-type
    #: refusal can say WHAT it actually was.
    observed_owner: str | None = None
    observed_len: int | None = None

    @property
    def verified(self) -> bool:
        """The only sanctioned read. Never branch on `verdict` directly."""
        return self.verdict is ChainVerdict.CHAIN_VERIFIED

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["verified"] = self.verified
        return d


def decode_mint_account(data: bytes) -> tuple[int, int, str | None, str | None, bool]:
    """Decode the 82-byte SPL mint layout. Raises on anything unexpected.

    Layout: COption<Pubkey> mint_authority (4 + 32), u64 supply, u8 decimals,
    u8 is_initialized, COption<Pubkey> freeze_authority (4 + 32).
    """
    if len(data) < MINT_ACCOUNT_LEN:
        raise ChainIdentityError(
            f"mint buffer is {len(data)} bytes, need {MINT_ACCOUNT_LEN}")
    ma_flag = int.from_bytes(data[0:4], "little")
    if ma_flag not in (0, 1):
        raise ChainIdentityError(f"mint_authority COption tag is {ma_flag}")
    mint_authority = base58_encode(data[4:36]) if ma_flag == 1 else None
    supply = int.from_bytes(data[36:44], "little")
    decimals = data[44]
    is_initialized = data[45] == 1
    fa_flag = int.from_bytes(data[46:50], "little")
    if fa_flag not in (0, 1):
        raise ChainIdentityError(f"freeze_authority COption tag is {fa_flag}")
    freeze_authority = base58_encode(data[50:82]) if fa_flag == 1 else None
    return supply, decimals, mint_authority, freeze_authority, is_initialized


def base58_encode(b: bytes) -> str:
    """Leading zero BYTES become leading '1's; there is no extra sentinel.

    An earlier form appended `or "1"` when the numeric part was empty, which
    for an all-zero pubkey -- the system program,
    `11111111111111111111111111111111` -- produced 33 characters instead of
    32. Leading-zero padding already encodes that case completely.
    """
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


class AccountReader(Protocol):
    """Injected read-only chain access. Returns `getAccountInfo`'s `value`."""

    def get_account_info(self, address: str) -> dict | None: ...


def verify_chain_existence(address: str, reader: AccountReader) -> ChainVerification:
    """Gate 1. One question, one typed answer."""
    if not is_valid_pubkey(address):
        return ChainVerification(ChainVerdict.CHAIN_INVALID, address,
                                 detail="not a 32-byte base58 pubkey")
    try:
        value = reader.get_account_info(address)
    except Exception as exc:
        # An RPC failure is NOT a statement about the address. Recorded as
        # unavailable so it is retried rather than cached as a rejection.
        return ChainVerification(ChainVerdict.UNAVAILABLE, address,
                                 detail=f"{type(exc).__name__}: {exc}")
    if value is None:
        return ChainVerification(ChainVerdict.NOT_FOUND, address,
                                 detail="no account at this address")

    owner = value.get("owner")
    raw = value.get("data")
    data = _decode_data(raw)
    length = len(data) if data is not None else None

    if owner not in KNOWN_TOKEN_PROGRAMS:
        # A wallet is owned by the system program; a program account by the
        # loader. Either way it is not a mint, and saying so precisely matters
        # more than saying "unverified".
        verdict = (ChainVerdict.WRONG_ACCOUNT_TYPE
                   if owner == SYSTEM_PROGRAM else
                   ChainVerdict.UNKNOWN_TOKEN_PROGRAM)
        return ChainVerification(
            verdict, address, observed_owner=owner, observed_len=length,
            detail=(f"owner {owner} is not a known token program; "
                    f"{'this is a system-owned account (wallet/PDA)' if owner == SYSTEM_PROGRAM else 'unrecognised program'}"))

    if length == TOKEN_ACCOUNT_LEN:
        return ChainVerification(
            ChainVerdict.WRONG_ACCOUNT_TYPE, address, observed_owner=owner,
            observed_len=length,
            detail="this is a token ACCOUNT (someone's balance of some token), not the mint itself")
    if data is None or length != MINT_ACCOUNT_LEN:
        return ChainVerification(
            ChainVerdict.WRONG_ACCOUNT_TYPE, address, observed_owner=owner,
            observed_len=length,
            detail=f"token-program account of {length} bytes is not a mint")

    try:
        supply, decimals, ma, fa, init = decode_mint_account(data)
    except ChainIdentityError as exc:
        return ChainVerification(ChainVerdict.CHAIN_INVALID, address,
                                 observed_owner=owner, observed_len=length,
                                 detail=str(exc))
    if not init:
        return ChainVerification(ChainVerdict.UNINITIALIZED_MINT, address,
                                 observed_owner=owner, observed_len=length,
                                 detail="mint exists but is not initialized")

    return ChainVerification(
        ChainVerdict.CHAIN_VERIFIED, address,
        facts=MintFacts(mint=address, token_program=owner, decimals=decimals,
                        supply=supply, mint_authority=ma, freeze_authority=fa,
                        is_initialized=True, account_len=length),
        observed_owner=owner, observed_len=length)


def _decode_data(raw: Any) -> bytes | None:
    """`data` arrives as [base64, "base64"] or a jsonParsed dict."""
    if isinstance(raw, (list, tuple)) and raw:
        enc = raw[1] if len(raw) > 1 else "base64"
        if enc != "base64":
            return None
        try:
            return base64.b64decode(raw[0])
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw)
        except Exception:
            return None
    return None
