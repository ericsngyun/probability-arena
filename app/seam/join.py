"""The join itself: social artifact + token resolution + realized fill.

This is where the four types are actually load-bearing. It produces either a
:class:`JoinedEvidenceRow` or a :class:`JoinRefused` — never a row with a
quietly degraded field, because EVIDENCE-JOIN-CONTRACT-001 §2 requires "any
pair that does not correspond is an error rather than a best-effort match".

What the row carries, per §5 (provenance travels or the join is unauditable):

* social: ``raw_content_hash``, ``ingestion_version``, ``delivery_mode``
* fill:   ``tx_signature``, ``decoder_version``
* token:  the full :class:`TokenResolution`, confidence included — §4 forbids
  dropping confidence at the seam
* absence: BOTH vocabularies, via :class:`Measurement`'s ``origin`` tag
* time:   the three named quantities, each typed, none pooled

CONTAINS NO SIGNAL, NO SCORING, NO RANKING.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from app.seam import SEAM_VERSION
from app.seam.clock import (
    ExternalDeliveryLatency,
    ObservationTimestamp,
    OurResponseLatency,
    external_delivery_latency,
    our_response_latency,
)
from app.seam.cohort import CohortPurpose, PRIMARY_ALPHA_DELIVERY_MODE
from app.seam.measurement import Measurement, Observation
from app.seam.token import TokenResolution

__all__ = [
    "JoinRefusalReason",
    "JoinRefused",
    "JoinedEvidenceRow",
    "JoinResult",
    "join_social_to_fill",
]


class JoinRefusalReason(str, Enum):
    """Closed set. A refusal names which gate closed."""

    TOKEN_NOT_CANONICALLY_VERIFIED = "TOKEN_NOT_CANONICALLY_VERIFIED"
    MINT_MISMATCH = "MINT_MISMATCH"
    NOT_LIVE_DELIVERY = "NOT_LIVE_DELIVERY"
    NO_FILL_SIGNATURE = "NO_FILL_SIGNATURE"


@dataclass(frozen=True, slots=True)
class JoinRefused:
    """The join did not happen, and why. Carries no row.

    Deliberately not an empty/partial row: a refusal that still exposes a
    `mint` or a `latency` will be read by something.
    """

    reason: JoinRefusalReason
    detail: str

    @property
    def joined(self) -> bool:
        return False

    def to_json(self) -> dict[str, Any]:
        return {
            "joined": False,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class JoinedEvidenceRow:
    """One joined row. Every claim on it carries its own provenance."""

    seam_version: str
    # --- identity ---------------------------------------------------------
    message_identity: tuple[str, str, str]
    content_identity: str
    mint: str
    tx_signature: str
    slot: int | None
    # --- provenance -------------------------------------------------------
    raw_content_hash: str
    ingestion_version: str
    delivery_mode: str
    decoder_version: str
    token_resolution: TokenResolution
    # --- time (three quantities, three names) -----------------------------
    external_delivery: ExternalDeliveryLatency
    our_response: OurResponseLatency
    # --- measured evidence ------------------------------------------------
    #: The deferred social observations, carried as Measurements so that
    #: OBSERVED_NONE survives the join as a MEASURED NEGATIVE.
    first_onchain_reaction: Measurement
    first_price_reaction: Measurement

    @property
    def joined(self) -> bool:
        return True

    @property
    def has_measured_negative(self) -> bool:
        return (
            self.first_onchain_reaction.is_measured_negative
            or self.first_price_reaction.is_measured_negative
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "joined": True,
            "seam_version": self.seam_version,
            "message_identity": list(self.message_identity),
            "content_identity": self.content_identity,
            "mint": self.mint,
            "tx_signature": self.tx_signature,
            "slot": self.slot,
            "raw_content_hash": self.raw_content_hash,
            "ingestion_version": self.ingestion_version,
            "delivery_mode": self.delivery_mode,
            "decoder_version": self.decoder_version,
            "token_resolution": self.token_resolution.to_json(),
            "external_delivery": self.external_delivery.to_json(),
            "our_response": self.our_response.to_json(),
            "first_onchain_reaction": self.first_onchain_reaction.to_json(),
            "first_price_reaction": self.first_price_reaction.to_json(),
        }


JoinResult = JoinedEvidenceRow | JoinRefused


def join_social_to_fill(
    *,
    artifact,
    resolution: TokenResolution,
    fill,
    quote_observed_at: ObservationTimestamp,
    onchain_reaction: Measurement,
    price_reaction: Measurement,
    purpose: CohortPurpose = CohortPurpose.LATENCY_LEAD_LAG,
    received_observed_at: ObservationTimestamp | None = None,
) -> JoinResult:
    """Join one social artifact to one realized fill, or refuse.

    Gate order is deliberate — cheapest and most dangerous first:

    1. **token identity** — only ``CANONICALLY_VERIFIED`` may join (§4).
       A base58 string that happens to be a real mint is exactly the hazard.
    2. **mint agreement** — the verified mint must be the fill's mint.
    3. **delivery mode** — for a latency purpose the artifact must be LIVE
       (§5), otherwise ``t_received`` measures our downtime.
    4. **fill identity** — a fill without a signature is not authoritative.
    """
    from app.fills.absence import Observed
    from app.seam.clock import from_our_received_at

    if not resolution.is_joinable:
        return JoinRefused(
            reason=JoinRefusalReason.TOKEN_NOT_CANONICALLY_VERIFIED,
            detail=resolution.refusal_reason() or "",
        )

    if resolution.mint != getattr(fill, "mint", None):
        return JoinRefused(
            reason=JoinRefusalReason.MINT_MISMATCH,
            detail=(
                f"resolved mint {resolution.mint!r} is not the fill's mint "
                f"{getattr(fill, 'mint', None)!r}"
            ),
        )

    if purpose is CohortPurpose.LATENCY_LEAD_LAG and (
        artifact.delivery_mode is not PRIMARY_ALPHA_DELIVERY_MODE
    ):
        return JoinRefused(
            reason=JoinRefusalReason.NOT_LIVE_DELIVERY,
            detail=(
                f"artifact delivery_mode is {artifact.delivery_mode.value}; "
                f"the primary lead-lag cohort is "
                f"{PRIMARY_ALPHA_DELIVERY_MODE.value} only. A backfilled "
                "artifact's our_received_at is honest and is not live "
                "delivery timing"
            ),
        )

    signature = fill.signature
    if not isinstance(signature, Observed):
        return JoinRefused(
            reason=JoinRefusalReason.NO_FILL_SIGNATURE,
            detail=(
                "the fill carries no transaction signature; quote->fill "
                "linkage is by carried identifier only"
            ),
        )

    received = received_observed_at or from_our_received_at(
        artifact.our_received_at
    )

    return JoinedEvidenceRow(
        seam_version=SEAM_VERSION,
        message_identity=artifact.message_identity,
        content_identity=artifact.content_identity,
        mint=resolution.mint,
        tx_signature=signature.value,
        slot=(fill.slot.value if isinstance(fill.slot, Observed) else None),
        raw_content_hash=artifact.raw_content_hash,
        ingestion_version=artifact.ingestion_version,
        delivery_mode=artifact.delivery_mode.value,
        decoder_version=fill.decoder_version,
        token_resolution=resolution,
        external_delivery=external_delivery_latency(
            artifact.source_created_at,
            artifact.our_received_at,
            delivery_mode=artifact.delivery_mode.value,
        ),
        our_response=our_response_latency(received, quote_observed_at),
        first_onchain_reaction=onchain_reaction,
        first_price_reaction=price_reaction,
    )
