"""SOLANA-READONLY-CHAIN-ADAPTER-001 — observation only, by construction.

Replaces Gate 1's injected `AccountReader` with a live implementation **without
increasing capability**. The surface is a fixed list of typed read methods, not
a generic transport:

    get_account_info · get_transaction · get_signature_status
    get_slot · get_block_time

**There is deliberately no `call(method, params)`.** A generic escape hatch is
convenient and makes capability creep unauditable: once it exists, "read-only"
becomes a property of caller discipline rather than of the module. With an
enumerated whitelist, adding a write is a visible diff in this file.

Structurally absent, and asserted by tests: any signer, private key, wallet,
transaction builder, `sendTransaction`, `simulateTransaction`, `requestAirdrop`,
swap/routing, or block-engine submission path. This module cannot move value
because it has no code that could.

Transport details stay here. Nothing above this layer sees an HTTP status, a
retry, or a JSON-RPC envelope -- Gate 1 receives account facts or a typed
failure, and **no RPC response can emit `CANONICALLY_VERIFIED`**; that decision
lives four layers up.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_ENDPOINT = "https://api.mainnet-beta.solana.com"

#: The complete set of JSON-RPC methods this adapter may ever send. Enumerated
#: rather than parameterised so that widening it is a reviewable change.
ALLOWED_METHODS = frozenset({
    "getAccountInfo", "getTransaction", "getSignatureStatuses",
    "getSlot", "getBlockTime",
})

#: Methods that must never appear. Kept explicit so the guard test has a
#: concrete list to check the source against, rather than a vague intention.
FORBIDDEN_METHODS = frozenset({
    "sendTransaction", "simulateTransaction", "requestAirdrop",
    "signTransaction", "signAllTransactions", "sendBundle",
})


class RpcUnavailable(RuntimeError):
    """Transport failed. NOT a statement about the object being queried.

    Gate 1 turns this into `UNAVAILABLE`, never `NOT_FOUND` -- an unreachable
    node must not be cached as "this token does not exist".
    """


class RpcRefused(RuntimeError):
    """The adapter refused to send. A programming error, not a network one."""


@dataclass(frozen=True)
class RpcEvidence:
    """Enough to audit a Gate 1 decision, without leaking transport upward."""
    endpoint: str
    method: str
    slot_context: int | None
    raw_sha256: str | None = None


class SolanaReadOnlyRPC:
    """Typed read surface. No generic method dispatch is exposed."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, timeout_s: int = 20,
                 opener=None) -> None:
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self._opener = opener or urllib.request.urlopen
        self.last_evidence: RpcEvidence | None = None

    # -- the only place a request is built ---------------------------------
    def _post(self, method: str, params: list) -> Any:
        if method not in ALLOWED_METHODS:
            # Belt and braces: the public methods below are the real gate, but
            # this makes a future refactor that widens them fail loudly.
            raise RpcRefused(
                f"{method!r} is not in the read-only whitelist "
                f"{sorted(ALLOWED_METHODS)}")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params}).encode()
        req = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with self._opener(req, timeout=self.timeout_s) as r:
                payload = json.load(r)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError, TimeoutError) as exc:
            raise RpcUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if "error" in payload:
            # A node-level error is still not evidence about the object.
            raise RpcUnavailable(f"rpc error: {payload['error']}")
        result = payload.get("result")
        ctx = (result or {}).get("context", {}) if isinstance(result, dict) else {}
        self.last_evidence = RpcEvidence(endpoint=self.endpoint, method=method,
                                         slot_context=ctx.get("slot"))
        return result

    # -- the enumerated read surface ---------------------------------------
    def get_account_info(self, address: str) -> dict | None:
        """Gate 1's authoritative lookup. `None` means no account exists."""
        res = self._post("getAccountInfo",
                         [address, {"encoding": "base64",
                                    "commitment": "confirmed"}])
        return (res or {}).get("value")

    def get_transaction(self, signature: str) -> dict | None:
        return self._post("getTransaction",
                          [signature, {"encoding": "json",
                                       "commitment": "confirmed",
                                       "maxSupportedTransactionVersion": 0}])

    def get_signature_status(self, signature: str) -> dict | None:
        res = self._post("getSignatureStatuses",
                         [[signature], {"searchTransactionHistory": True}])
        vals = (res or {}).get("value") or [None]
        return vals[0]

    def get_slot(self) -> int | None:
        return self._post("getSlot", [{"commitment": "confirmed"}])

    def get_block_time(self, slot: int) -> int | None:
        return self._post("getBlockTime", [slot])
