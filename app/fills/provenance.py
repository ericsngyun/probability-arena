"""Fixture provenance and drift detection (AGENTS.md doctrine 9).

> **Fixtures are executable claims about external reality.** Any fixture
> representing venue behaviour must be traceable to captured wire evidence or
> official protocol semantics. A fixture that cannot identify its empirical
> basis is synthetic test data, not venue truth.

The repo already paid for this lesson once: 368 green tests were built on
Kalshi CP3 fixtures that put every channel on one shared `sid`, which the venue
does not do. Every test was internally consistent and wrong, and the suite
certified the wrong behaviour rather than failing.

A Solana fixture has a property Kalshi's did not: **a finalized transaction is
immutable**, so the *content* cannot drift. What can and does drift is the
**representation** — RPC encodings, field presence, `jsonParsed` coverage for
programs that gain a parser, `maxSupportedTransactionVersion` behaviour, and
`meta` fields that did not exist in older ledger versions. That is precisely
the drift that would silently change what our decoder sees while every test
stays green, so it gets two hashes rather than one:

* **`content_sha256`** — over the stored bytes. Detects local mutation. A test
  asserts it on every run, offline, with no network.
* **`semantic_sha256`** — over a canonicalized subset containing exactly the
  fields the decoder reads. Compared against a live re-fetch by the gated
  drift test. This is the detector that notices the venue moving away from the
  world our tests certify.

Splitting them matters: an RPC that starts returning a new advisory field
changes `content_sha256` and must NOT be reported as venue drift, while a
change in `preTokenBalances` semantics changes `semantic_sha256` and must be.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

#: Bump when the canonical subset below changes shape. Pinned into every
#: manifest entry so an old fixture cannot be silently compared under new
#: rules.
FIXTURE_SCHEMA_VERSION = 1

#: Mainnet-beta genesis hash. A fixture whose provenance carries a different
#: one was captured against a different chain and is not venue truth for us.
MAINNET_GENESIS_HASH = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


class ProvenanceError(RuntimeError):
    """A fixture failed its own provenance claim."""


def _canonical_instruction(ix: dict) -> dict:
    """Only the parts the decoder reads. `parsed` is kept verbatim because the
    tip and transfer attribution depend on its exact shape."""
    return {
        "programId": ix.get("programId"),
        "program": ix.get("program"),
        "data": ix.get("data"),
        "parsed": ix.get("parsed"),
        "accounts": ix.get("accounts"),
        "stackHeight": ix.get("stackHeight"),
    }


def canonical_subset(payload: dict) -> dict:
    """The decoder's actual input surface, in a stable order.

    Anything the decoder reads must appear here or the drift detector is blind
    to a change in it. Anything it does not read must NOT appear here or the
    detector cries drift over cosmetics.
    """
    meta = payload.get("meta") or {}
    tx = payload.get("transaction") or {}
    message = tx.get("message") or {}
    keys = message.get("accountKeys") or []
    normalized_keys = [
        (
            {
                "pubkey": k.get("pubkey"),
                "signer": k.get("signer"),
                "writable": k.get("writable"),
                "source": k.get("source"),
            }
            if isinstance(k, dict)
            else {"pubkey": k, "signer": None, "writable": None, "source": None}
        )
        for k in keys
    ]
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "slot": payload.get("slot"),
        "blockTime": payload.get("blockTime"),
        "version": payload.get("version"),
        "signatures": tx.get("signatures"),
        "header": message.get("header"),
        "accountKeys": normalized_keys,
        "instructions": [
            _canonical_instruction(i)
            for i in (message.get("instructions") or [])
            if isinstance(i, dict)
        ],
        "meta": {
            "err": meta.get("err"),
            "fee": meta.get("fee"),
            "preBalances": meta.get("preBalances"),
            "postBalances": meta.get("postBalances"),
            "preTokenBalances": meta.get("preTokenBalances"),
            "postTokenBalances": meta.get("postTokenBalances"),
            "computeUnitsConsumed": meta.get("computeUnitsConsumed"),
            "loadedAddresses": meta.get("loadedAddresses"),
            "innerInstructions": [
                {
                    "index": g.get("index"),
                    "instructions": [
                        _canonical_instruction(i)
                        for i in (g.get("instructions") or [])
                        if isinstance(i, dict)
                    ],
                }
                for g in (meta.get("innerInstructions") or [])
                if isinstance(g, dict)
            ],
        },
    }


def _sha256_of_json(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def semantic_hash(payload: dict) -> str:
    return _sha256_of_json(canonical_subset(payload))


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class FixtureProvenance:
    """The executable claim a fixture makes about external reality."""

    capture_id: str
    venue: str
    chain_genesis_hash: str
    signature: str
    slot: int
    block_time: int | None
    #: How it was retrieved, exactly. A fixture fetched with a different
    #: encoding is a different observation of the same transaction.
    rpc_endpoint: str
    rpc_method: str
    rpc_encoding: str
    rpc_commitment: str
    rpc_max_supported_version: int
    #: When WE retrieved it (our clock), RFC3339 UTC. Not the chain's time.
    retrieved_at: str
    retrieved_by: str
    schema_version: int
    content_sha256: str
    semantic_sha256: str
    #: Which hard cases from the milestone §5 table this fixture exercises.
    hard_cases: tuple[str, ...]
    #: Free-text note on WHY this transaction was chosen.
    selection_reason: str
    relative_path: str
    #: The decoded values this fixture asserts. This is what makes it an
    #: *executable* claim rather than a stored blob.
    expected: dict = field(default_factory=dict)

    @staticmethod
    def from_json(obj: dict) -> "FixtureProvenance":
        return FixtureProvenance(
            capture_id=obj["capture_id"],
            venue=obj["venue"],
            chain_genesis_hash=obj["chain_genesis_hash"],
            signature=obj["signature"],
            slot=obj["slot"],
            block_time=obj.get("block_time"),
            rpc_endpoint=obj["rpc_endpoint"],
            rpc_method=obj["rpc_method"],
            rpc_encoding=obj["rpc_encoding"],
            rpc_commitment=obj["rpc_commitment"],
            rpc_max_supported_version=obj["rpc_max_supported_version"],
            retrieved_at=obj["retrieved_at"],
            retrieved_by=obj["retrieved_by"],
            schema_version=obj["schema_version"],
            content_sha256=obj["content_sha256"],
            semantic_sha256=obj["semantic_sha256"],
            hard_cases=tuple(obj.get("hard_cases") or ()),
            selection_reason=obj.get("selection_reason", ""),
            relative_path=obj["relative_path"],
            expected=obj.get("expected") or {},
        )

    def to_json(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "venue": self.venue,
            "chain_genesis_hash": self.chain_genesis_hash,
            "signature": self.signature,
            "slot": self.slot,
            "block_time": self.block_time,
            "rpc_endpoint": self.rpc_endpoint,
            "rpc_method": self.rpc_method,
            "rpc_encoding": self.rpc_encoding,
            "rpc_commitment": self.rpc_commitment,
            "rpc_max_supported_version": self.rpc_max_supported_version,
            "retrieved_at": self.retrieved_at,
            "retrieved_by": self.retrieved_by,
            "schema_version": self.schema_version,
            "content_sha256": self.content_sha256,
            "semantic_sha256": self.semantic_sha256,
            "hard_cases": list(self.hard_cases),
            "selection_reason": self.selection_reason,
            "relative_path": self.relative_path,
            "expected": self.expected,
        }


@dataclass(frozen=True, slots=True)
class FixtureSet:
    root: Path
    entries: tuple[FixtureProvenance, ...]

    def by_capture_id(self, capture_id: str) -> FixtureProvenance:
        for e in self.entries:
            if e.capture_id == capture_id:
                return e
        raise KeyError(capture_id)

    def payload(self, entry: FixtureProvenance) -> dict:
        return json.loads((self.root / entry.relative_path).read_bytes())


MANIFEST_NAME = "MANIFEST.json"


def load_fixture_set(root: Path) -> FixtureSet:
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    return FixtureSet(
        root=root,
        entries=tuple(
            FixtureProvenance.from_json(e) for e in manifest["fixtures"]
        ),
    )


def verify_offline(fixtures: FixtureSet) -> list[str]:
    """Check every fixture against its own recorded provenance.

    Offline, no network. Returns a list of violations; empty means clean.
    Deliberately returns rather than raises so a test can report ALL failures
    at once instead of the first.
    """
    problems: list[str] = []
    for entry in fixtures.entries:
        path = fixtures.root / entry.relative_path
        if not path.exists():
            problems.append(f"{entry.capture_id}: file missing at {path}")
            continue
        raw = path.read_bytes()
        actual_content = content_hash(raw)
        if actual_content != entry.content_sha256:
            problems.append(
                f"{entry.capture_id}: content hash {actual_content} != pinned "
                f"{entry.content_sha256} — the stored bytes were modified"
            )
        payload = json.loads(raw)
        actual_semantic = semantic_hash(payload)
        if actual_semantic != entry.semantic_sha256:
            problems.append(
                f"{entry.capture_id}: semantic hash {actual_semantic} != "
                f"pinned {entry.semantic_sha256}"
            )
        if entry.schema_version != FIXTURE_SCHEMA_VERSION:
            problems.append(
                f"{entry.capture_id}: schema version {entry.schema_version} != "
                f"current {FIXTURE_SCHEMA_VERSION}; the canonical subset "
                "changed and the pinned hash cannot be compared under new "
                "rules"
            )
        if entry.chain_genesis_hash != MAINNET_GENESIS_HASH:
            problems.append(
                f"{entry.capture_id}: genesis hash {entry.chain_genesis_hash} "
                "is not mainnet-beta"
            )
        sigs = ((payload.get("transaction") or {}).get("signatures") or [])
        if not sigs or sigs[0] != entry.signature:
            problems.append(
                f"{entry.capture_id}: stored signature {sigs[:1]} does not "
                f"match pinned {entry.signature}"
            )
    return problems


@dataclass(frozen=True, slots=True)
class DriftReport:
    capture_id: str
    drifted: bool
    pinned_semantic_sha256: str
    live_semantic_sha256: str | None
    changed_fields: tuple[str, ...]
    detail: str


def detect_drift(
    entry: FixtureProvenance, live_payload: dict | None, stored_payload: dict
) -> DriftReport:
    """Compare a live re-fetch against the pinned fixture.

    A `None` live payload is NOT drift — it is a failed fetch, and reporting a
    rate-limited RPC as venue drift would be exactly the "plausible value from
    a broken path" failure doctrine 7 targets.
    """
    if live_payload is None:
        return DriftReport(
            capture_id=entry.capture_id,
            drifted=False,
            pinned_semantic_sha256=entry.semantic_sha256,
            live_semantic_sha256=None,
            changed_fields=(),
            detail="live fetch returned nothing; drift is UNKNOWN, not absent",
        )
    live = semantic_hash(live_payload)
    if live == entry.semantic_sha256:
        return DriftReport(
            capture_id=entry.capture_id,
            drifted=False,
            pinned_semantic_sha256=entry.semantic_sha256,
            live_semantic_sha256=live,
            changed_fields=(),
            detail="semantic subset unchanged",
        )
    stored_subset = canonical_subset(stored_payload)
    live_subset = canonical_subset(live_payload)
    changed = tuple(
        sorted(
            key
            for key in set(stored_subset) | set(live_subset)
            if stored_subset.get(key) != live_subset.get(key)
        )
    )
    meta_changed: tuple[str, ...] = ()
    if "meta" in changed:
        sm = stored_subset.get("meta") or {}
        lm = live_subset.get("meta") or {}
        meta_changed = tuple(
            sorted(
                f"meta.{k}"
                for k in set(sm) | set(lm)
                if sm.get(k) != lm.get(k)
            )
        )
    return DriftReport(
        capture_id=entry.capture_id,
        drifted=True,
        pinned_semantic_sha256=entry.semantic_sha256,
        live_semantic_sha256=live,
        changed_fields=tuple(sorted(set(changed) | set(meta_changed))),
        detail=(
            "the RPC's representation of an immutable transaction changed; "
            "our decoder's input surface moved even though the chain did not"
        ),
    )
