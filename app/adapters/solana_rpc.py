"""Read-only Solana JSON-RPC adapter (REALIZED-FILL-CORPUS-001).

**The permitted verb set is a closed allowlist, enforced in code.** Only
historical, read-only methods appear in `PERMITTED_METHODS`, and `_call`
refuses anything else before a socket is opened. This is deliberate: the
project's hard boundary (AGENTS.md, `docs/SAFETY_BOUNDARIES.md`) forbids
transaction construction, `simulateTransaction`, signing, submission, and
fetching a blockhash / priority fee / nonce — and *every one of those is an RPC
method on the very endpoint this module talks to*. A permissive client here
would put the boundary one string literal away. So the allowlist is the
boundary, it is tested, and adding to it is a reviewable change.

Explicitly NOT permitted, and each one refused by name:
`sendTransaction` · `simulateTransaction` · `getLatestBlockhash` ·
`getRecentBlockhash` · `getFeeForMessage` · `requestAirdrop` ·
`getRecentPrioritizationFees`.

Free public endpoint only — no paid RPC (AGENTS.md, SAFETY-BOUNDARY-ROUTE-
QUOTE-001). Following the repo's adapter convention (`DexScreenerAdapter`),
every method degrades to `None` on transport, HTTP or schema failure rather
than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

#: Free public mainnet endpoint. Rate-limited and deliberately so: this
#: adapter exists to harvest a handful of pinned fixtures, not to stream.
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

SOURCE_NAME = "solana-json-rpc"

#: Closed allowlist. Read-only history and chain metadata only.
PERMITTED_METHODS: frozenset[str] = frozenset(
    {
        "getTransaction",
        "getSignaturesForAddress",
        "getSlot",
        "getBlockTime",
        "getGenesisHash",
        "getVersion",
        "getHealth",
    }
)

#: Named refusals. Membership here is not required for refusal — anything
#: outside PERMITTED_METHODS is refused — but naming them makes the boundary
#: greppable and gives the test suite something specific to assert against.
FORBIDDEN_METHODS: frozenset[str] = frozenset(
    {
        "sendTransaction",
        "simulateTransaction",
        "getLatestBlockhash",
        "getRecentBlockhash",
        "getFeeForMessage",
        "getRecentPrioritizationFees",
        "requestAirdrop",
        "getStakeMinimumDelegation",
    }
)


class ForbiddenRpcMethod(RuntimeError):
    """Raised when a caller asks for a method outside the read-only allowlist.

    This is a hard failure, not a degradation. A boundary that returns `None`
    teaches callers to retry.
    """


@dataclass(frozen=True)
class RpcResponse:
    """A successful RPC result plus the provenance needed to pin a fixture."""

    method: str
    params: list
    result: object
    endpoint: str
    #: RFC3339 UTC, recorded by the caller's clock, not the chain's.
    retrieved_at: str


class SolanaRpcAdapter:
    """Thin async read-only JSON-RPC client.

    Read-only means read-only: see `PERMITTED_METHODS`. No credentials, no
    key material, no state-mutating verb.
    """

    source_name = SOURCE_NAME

    def __init__(self, endpoint: str = DEFAULT_RPC_URL, timeout: float = 30.0):
        self.endpoint = endpoint
        self.timeout = timeout

    async def _call(self, method: str, params: list) -> object | None:
        if method not in PERMITTED_METHODS:
            raise ForbiddenRpcMethod(
                f"{method!r} is not in the read-only allowlist. This adapter "
                "may not construct, simulate, sign, submit or price a "
                "transaction (docs/SAFETY_BOUNDARIES.md)."
            )
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=body)
                if response.status_code == 429:
                    logger.warning("Solana RPC rate limit hit for %s", method)
                    return None
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Solana RPC %s failed: %s", method, exc)
            return None
        if not isinstance(payload, dict):
            logger.warning("Solana RPC %s returned a non-object body", method)
            return None
        if "error" in payload:
            logger.warning("Solana RPC %s error: %s", method, payload["error"])
            return None
        if "result" not in payload:
            # An HTTP 200 with neither result nor error is a failed request,
            # not an empty answer (the CRYPTO-COVERAGE-REPAIR-002 B1 lesson).
            logger.warning("Solana RPC %s returned no result key", method)
            return None
        return payload["result"]

    async def get_transaction(
        self,
        signature: str,
        *,
        encoding: str = "jsonParsed",
        commitment: str = "confirmed",
        max_supported_version: int = 0,
    ) -> object | None:
        """Fetch one already-confirmed transaction.

        `maxSupportedTransactionVersion` is mandatory in practice: without it
        the RPC refuses versioned (v0) transactions outright, which silently
        excludes exactly the aggregator routes this corpus is about — they use
        address lookup tables.
        """
        return await self._call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": encoding,
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": max_supported_version,
                },
            ],
        )

    async def get_signatures_for_address(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list | None:
        """Recent signatures touching an address. Used only to *discover*
        candidate fixtures; nothing downstream depends on it."""
        options: dict = {"limit": limit, "commitment": "confirmed"}
        if before:
            options["before"] = before
        result = await self._call("getSignaturesForAddress", [address, options])
        return result if isinstance(result, list) else None

    async def get_genesis_hash(self) -> str | None:
        """Chain identity. Pinned into fixture provenance so a fixture
        captured against a fork or a test cluster is detectable."""
        result = await self._call("getGenesisHash", [])
        return result if isinstance(result, str) else None
