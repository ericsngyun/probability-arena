"""SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001 — the funnel, and only the funnel.

One question:

    can we prospectively acquire live social artifacts and move them through
    the full evidence / identity / clock / on-chain funnel without silently
    inventing provenance, timing, or token identity?

**Not** whether any of it predicts anything. This module cannot express a
return, a markout, a price response, a win rate, a source score, a token
ranking or a trading decision — there is no field for one and a test asserts
the code references none of them.

Every artifact terminates in **either the next stage or a typed refusal**.
Silence is not a stage: an artifact that simply stops being mentioned would be
indistinguishable from one that was never received, which is the shape of
silent-wrongness this project keeps guarding against.

The value here is the *shape* of the funnel. If 100 artifacts yield 40 mint
candidates, 30 chain-verified and 3 authoritative sources, the bottleneck is
provenance, not RPC. If canonical-verified is 20 and joinable is 2, it is the
clock or downstream observation. Either answer directs the next milestone.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum


class Stage(str, Enum):
    """Ordered. An artifact reaches a stage only by clearing every prior one."""
    RECEIVED_SOCIAL = "RECEIVED_SOCIAL"
    EVIDENCE_EXTRACTED = "EVIDENCE_EXTRACTED"
    AUTHORITY_RESOLVED = "AUTHORITY_RESOLVED"
    CHAIN_VERIFIED = "CHAIN_VERIFIED"
    CANONICALLY_VERIFIED = "CANONICALLY_VERIFIED"
    LIVE_DELIVERY = "LIVE_DELIVERY"
    CLOCK_COMPUTABLE = "CLOCK_COMPUTABLE"
    DOWNSTREAM_CHAIN_OBSERVATION = "DOWNSTREAM_CHAIN_OBSERVATION"
    QUOTE_OBSERVATION = "QUOTE_OBSERVATION"
    SOCIAL_FILL_JOINABLE = "SOCIAL_FILL_JOINABLE"


STAGE_ORDER = tuple(Stage)


class Refusal(str, Enum):
    NO_MINT_CANDIDATE = "NO_MINT_CANDIDATE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    AUTHORITY_UNVERIFIED = "AUTHORITY_UNVERIFIED"
    AUTHORITY_IMPERSONATOR = "AUTHORITY_IMPERSONATOR"
    AUTHORITY_CONFLICTING = "AUTHORITY_CONFLICTING"
    CHAIN_NOT_VERIFIED = "CHAIN_NOT_VERIFIED"
    CHAIN_UNAVAILABLE = "CHAIN_UNAVAILABLE"
    NOT_CANONICAL = "NOT_CANONICAL"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    DELIVERY_NOT_LIVE = "DELIVERY_NOT_LIVE"
    CLOCK_NOT_COMPUTABLE = "CLOCK_NOT_COMPUTABLE"
    NO_CHAIN_OBSERVATION = "NO_CHAIN_OBSERVATION"
    NO_QUOTE_OBSERVATION = "NO_QUOTE_OBSERVATION"
    SEAM_REFUSED = "SEAM_REFUSED"


@dataclass(frozen=True)
class ArtifactOutcome:
    """Where one artifact stopped, and why. Never a score."""
    artifact_id: str
    source_id: str
    reached: Stage
    refusal: Refusal | None
    #: Contaminated: `t_source_created` is a platform clock we cannot audit.
    delivery_us: int | None = None
    #: Ours end to end, only when host and boot epoch agree.
    pipeline_us: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.reached is not Stage.SOCIAL_FILL_JOINABLE and self.refusal is None:
            raise ValueError(
                f"{self.artifact_id}: stopped at {self.reached.value} with no "
                "refusal. Every artifact must terminate in the next stage or a "
                "typed refusal; silence is not a stage.")
        if self.reached is Stage.SOCIAL_FILL_JOINABLE and self.refusal is not None:
            raise ValueError(f"{self.artifact_id}: joinable AND refused")

    def to_dict(self) -> dict:
        return {**asdict(self), "reached": self.reached.value,
                "refusal": self.refusal.value if self.refusal else None}


def funnel_report(outcomes: list[ArtifactOutcome]) -> dict:
    """Counts and refusal reasons. No outcome quantity of any kind."""
    reached_at_least = {}
    idx = {s: i for i, s in enumerate(STAGE_ORDER)}
    for s in STAGE_ORDER:
        reached_at_least[s.value] = sum(
            1 for o in outcomes if idx[o.reached] >= idx[s])

    refusals = Counter(o.refusal.value for o in outcomes if o.refusal)
    stopped_at = Counter(o.reached.value for o in outcomes if o.refusal)

    # Where the funnel loses the most, as a diagnostic for the NEXT milestone.
    drops = []
    for a, b in zip(STAGE_ORDER, STAGE_ORDER[1:]):
        lost = reached_at_least[a.value] - reached_at_least[b.value]
        drops.append({"from": a.value, "to": b.value, "lost": lost})
    worst = max(drops, key=lambda d: d["lost"]) if drops else None

    delivery = [o.delivery_us for o in outcomes if o.delivery_us is not None]
    pipeline = [o.pipeline_us for o in outcomes if o.pipeline_us is not None]

    return {
        "milestone": "SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001",
        "artifacts": len(outcomes),
        "sources": len({o.source_id for o in outcomes}),
        "reached_at_least": reached_at_least,
        "stopped_at": dict(stopped_at),
        "refusals": dict(refusals),
        "stage_losses": drops,
        "largest_loss": worst,
        # Two latencies, reported separately and NEVER summed. `delivery` is
        # contaminated by a platform clock; only `pipeline` carries the strong
        # timing semantics.
        "delivery_latency_us": {
            "n": len(delivery), "contaminated": True,
            "median": sorted(delivery)[len(delivery) // 2] if delivery else None},
        "pipeline_latency_us": {
            "n": len(pipeline), "contaminated": False,
            "median": sorted(pipeline)[len(pipeline) // 2] if pipeline else None},
        "note": "counts and refusals only; no return, markout, price response, "
                "score or ranking is computed here",
    }
