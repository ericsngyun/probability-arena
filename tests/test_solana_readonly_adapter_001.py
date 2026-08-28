"""The adapter may observe. It must be incapable of anything else.

Capability is checked STRUCTURALLY -- over the source and the import graph --
not by trusting callers. "Read-only" should be a property of the module, not of
everyone who ever uses it.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import urllib.error

import pytest

from app.seam import chain_identity as CI
from app.seam import solana_rpc as RPC
from app.seam.chain_identity import ChainVerdict as V

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


# --- capability, structurally ------------------------------------------------

def test_there_is_no_generic_call_method():
    """A generic escape hatch makes read-only a caller property, not a module one.

    The class surface is EXACTLY the five reads -- `endpoint`, `timeout_s` and
    `last_evidence` are instance attributes and do not appear on the class,
    which makes this a tighter assertion than listing them would.
    """
    public = {n for n in dir(RPC.SolanaReadOnlyRPC) if not n.startswith("_")}
    assert public == {"get_account_info", "get_transaction",
                      "get_signature_status", "get_slot", "get_block_time"}
    # EXACT names, not substrings: "sign" matches `get_signature_status`,
    # which is a read. The equality assertion above is the real guard; this
    # names the specific escape hatches so a reader sees what is excluded.
    for banned in ("call", "request", "rpc", "post", "execute", "invoke",
                   "send", "sign", "send_transaction", "sign_transaction"):
        assert banned not in public, banned


def test_no_write_method_appears_anywhere_in_the_source():
    src = inspect.getsource(RPC)
    tree = ast.parse(src)
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    # the forbidden list itself is allowed to name them; nothing else is
    sendable = literals - RPC.FORBIDDEN_METHODS - {"jsonrpc"}
    for m in RPC.FORBIDDEN_METHODS:
        assert m not in sendable


def test_the_whitelist_is_exactly_the_five_read_methods():
    assert RPC.ALLOWED_METHODS == {
        "getAccountInfo", "getTransaction", "getSignatureStatuses",
        "getSlot", "getBlockTime"}
    assert not (RPC.ALLOWED_METHODS & RPC.FORBIDDEN_METHODS)


def test_no_signer_wallet_or_execution_import_is_reachable():
    tree = ast.parse(inspect.getsource(RPC))
    mods = {n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    for banned in ("solders", "solana.keypair", "nacl", "ed25519", "wallet",
                   "signer", "jito", "jupiter", "swap", "anchorpy"):
        assert not any(banned in m.lower() for m in mods), f"imports {banned}"


def test_sending_a_forbidden_method_is_refused_not_attempted():
    """Belt and braces: even if a future refactor widens the public surface."""
    r = RPC.SolanaReadOnlyRPC(opener=lambda *a, **k: pytest.fail("network!"))
    with pytest.raises(RPC.RpcRefused, match="whitelist"):
        r._post("sendTransaction", [])
    with pytest.raises(RPC.RpcRefused):
        r._post("simulateTransaction", [])


def test_no_keypair_or_secret_field_exists():
    r = RPC.SolanaReadOnlyRPC()
    for attr in vars(r):
        assert not any(b in attr.lower() for b in
                       ("key", "secret", "signer", "wallet", "seed"))


# --- transport does not leak upward ------------------------------------------

def _resp(payload):
    class R:
        def __enter__(self): return io.BytesIO(json.dumps(payload).encode())
        def __exit__(self, *a): return False
    return lambda *a, **k: R()


def test_an_rpc_error_becomes_unavailable_not_a_null_result():
    r = RPC.SolanaReadOnlyRPC(opener=_resp({"error": {"code": -32005}}))
    with pytest.raises(RPC.RpcUnavailable, match="rpc error"):
        r.get_account_info(MINT)


def test_a_transport_failure_becomes_unavailable():
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    r = RPC.SolanaReadOnlyRPC(opener=boom)
    with pytest.raises(RPC.RpcUnavailable):
        r.get_account_info(MINT)


def test_evidence_is_recorded_without_leaking_the_envelope():
    r = RPC.SolanaReadOnlyRPC(opener=_resp(
        {"result": {"context": {"slot": 12345}, "value": None}}))
    assert r.get_account_info(MINT) is None
    ev = r.last_evidence
    assert ev.method == "getAccountInfo" and ev.slot_context == 12345
    assert not hasattr(ev, "http_status") and not hasattr(ev, "body")


# --- THE positive control: RPC failure != NOT_FOUND --------------------------

def test_gate1_maps_an_unreachable_node_to_UNAVAILABLE_never_NOT_FOUND():
    """The single most important invariant in the whole chain."""
    def boom(*a, **k):
        raise urllib.error.URLError("node down")
    reader = RPC.SolanaReadOnlyRPC(opener=boom)
    res = CI.verify_chain_existence(MINT, reader)
    assert res.verdict is V.UNAVAILABLE
    assert res.verdict is not V.NOT_FOUND
    assert res.verified is False


def test_gate1_maps_a_genuinely_absent_account_to_NOT_FOUND():
    """And the contrast still works, so UNAVAILABLE is not swallowing both."""
    reader = RPC.SolanaReadOnlyRPC(opener=_resp(
        {"result": {"context": {"slot": 1}, "value": None}}))
    res = CI.verify_chain_existence(MINT, reader)
    assert res.verdict is V.NOT_FOUND


def test_the_adapter_satisfies_the_AccountReader_protocol_gate1_expects():
    r = RPC.SolanaReadOnlyRPC(opener=_resp(
        {"result": {"context": {"slot": 1}, "value": None}}))
    assert hasattr(r, "get_account_info")
    assert CI.verify_chain_existence(MINT, r).verdict is V.NOT_FOUND


# --- no RPC response can emit a semantic verdict ------------------------------

def test_the_adapter_cannot_express_a_canonical_verdict():
    """Checked over CODE, not prose.

    The module docstring says "no RPC response can emit CANONICALLY_VERIFIED",
    which a substring scan reads as a violation of the very thing it promises.
    """
    from tests.astguard import assert_never_references, imported_modules
    assert_never_references(RPC, ("canonical", "corroborat", "tokenresolution",
                                  "joinable", "verified"))
    mods = imported_modules(RPC)
    for banned in ("corroboration", "token", "fill_seam", "evidence_extractor"):
        assert not any(banned in m for m in mods), f"adapter imports {banned}"
