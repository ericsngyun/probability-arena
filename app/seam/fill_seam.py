"""SOCIAL-FILL-SEAM-QUALIFICATION-001 — the join contract.

A social observation enters the future lead-lag corpus only if ALL of:

    CANONICALLY_VERIFIED
  ^ delivery_mode == LIVE
  ^ clock interval is COMPUTABLE
  ^ mint_social == mint_market

Every refusal is typed. **Nothing here reads a price, a return or a markout** --
this milestone establishes only whether an event is ELIGIBLE for a future
experiment, never whether the token subsequently moved.

## Two latencies, never one

    L_delivery = t_our_received - t_source_created
    L_pipeline = t_quote        - t_our_received

They answer different questions and are never summed. `L_delivery` is
contaminated evidence: `t_source_created` comes from a platform clock we do not
control and cannot audit. `L_pipeline` is ours end to end and is measurable on
one host across processes, which the seam's falsifier 3 confirmed on Linux.

Adding them would produce a number that is part measurement and part hearsay,
and would hide *which* of the two a poor result came from -- information
arriving late, or our own pipeline reacting slowly. That distinction decides
whether paying for better social infrastructure is economically justified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from app.seam.clock import ComputedInterval, ObservationTimestamp, interval
from app.seam.token import JOINABLE_STATUSES, TokenResolutionStatus


class SeamVerdict(str, Enum):
    SOCIAL_FILL_JOINABLE = "SOCIAL_FILL_JOINABLE"
    SOCIAL_FILL_NOT_JOINABLE = "SOCIAL_FILL_NOT_JOINABLE"


class SeamRefusal(str, Enum):
    TOKEN_NOT_CANONICAL = "TOKEN_NOT_CANONICAL"
    SOURCE_NOT_AUTHORITATIVE = "SOURCE_NOT_AUTHORITATIVE"
    DELIVERY_NOT_LIVE = "DELIVERY_NOT_LIVE"
    CLOCK_NOT_COMPUTABLE = "CLOCK_NOT_COMPUTABLE"
    BOOT_EPOCH_MISMATCH = "BOOT_EPOCH_MISMATCH"
    HOST_MISMATCH = "HOST_MISMATCH"
    MINT_MISMATCH = "MINT_MISMATCH"
    CHAIN_OBSERVATION_MISSING = "CHAIN_OBSERVATION_MISSING"
    QUOTE_PRECEDES_SOCIAL_RECEIPT = "QUOTE_PRECEDES_SOCIAL_RECEIPT"


DELIVERY_LIVE = "LIVE"


@dataclass(frozen=True)
class Latencies:
    """Two quantities, deliberately not one.

    `delivery_us` is None when the platform gave us no creation time; that is a
    measured absence, not a zero.
    """
    delivery_us: int | None
    delivery_is_contaminated: bool
    pipeline_us: int | None
    pipeline_basis: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SeamResult:
    verdict: SeamVerdict
    refusals: tuple
    mint: str | None
    latencies: Latencies | None
    detail: str = ""

    @property
    def joinable(self) -> bool:
        return self.verdict is SeamVerdict.SOCIAL_FILL_JOINABLE

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value,
                "refusals": [r.value for r in self.refusals],
                "mint": self.mint, "joinable": self.joinable,
                "latencies": self.latencies.to_dict() if self.latencies else None,
                "detail": self.detail}


def qualify(*, token_status: TokenResolutionStatus, social_mint: str | None,
            market_mint: str | None, delivery_mode: str,
            social_received: ObservationTimestamp | None,
            quote_observed: ObservationTimestamp | None,
            source_can_bind: bool = True,
            source_created_us: int | None = None,
            chain_observed: bool = True) -> SeamResult:
    """The join contract. Collects ALL refusals rather than short-circuiting.

    Reporting every reason matters operationally: a session that fails on four
    grounds is a different problem from one that fails on a single fixable one.
    """
    refusals: list[SeamRefusal] = []

    if token_status not in JOINABLE_STATUSES:
        refusals.append(SeamRefusal.TOKEN_NOT_CANONICAL)
    if not source_can_bind:
        refusals.append(SeamRefusal.SOURCE_NOT_AUTHORITATIVE)
    if delivery_mode != DELIVERY_LIVE:
        refusals.append(SeamRefusal.DELIVERY_NOT_LIVE)
    if not chain_observed:
        refusals.append(SeamRefusal.CHAIN_OBSERVATION_MISSING)

    # Mint identity must AGREE, and absence is not agreement.
    if social_mint is None or market_mint is None or social_mint != market_mint:
        refusals.append(SeamRefusal.MINT_MISMATCH)

    pipeline_us, basis = None, None
    if social_received is None or quote_observed is None:
        refusals.append(SeamRefusal.CLOCK_NOT_COMPUTABLE)
    else:
        if social_received.host_id != quote_observed.host_id:
            refusals.append(SeamRefusal.HOST_MISMATCH)
        elif (social_received.host_boot_id.value
              != quote_observed.host_boot_id.value):
            refusals.append(SeamRefusal.BOOT_EPOCH_MISMATCH)
        res = interval(social_received, quote_observed)
        if isinstance(res, ComputedInterval):
            pipeline_us, basis = res.microseconds, res.basis.value
            if pipeline_us < 0:
                # A quote cannot precede the social receipt it is a reaction
                # to. This is an ordering violation, not a small negative.
                refusals.append(SeamRefusal.QUOTE_PRECEDES_SOCIAL_RECEIPT)
        elif SeamRefusal.HOST_MISMATCH not in refusals and \
                SeamRefusal.BOOT_EPOCH_MISMATCH not in refusals:
            refusals.append(SeamRefusal.CLOCK_NOT_COMPUTABLE)

    delivery_us = None
    if social_received is not None and source_created_us is not None:
        recv_us = int(social_received.wall_datetime.timestamp() * 1_000_000)
        delivery_us = recv_us - source_created_us

    lat = Latencies(delivery_us=delivery_us, delivery_is_contaminated=True,
                    pipeline_us=pipeline_us, pipeline_basis=basis)

    if refusals:
        return SeamResult(SeamVerdict.SOCIAL_FILL_NOT_JOINABLE,
                          tuple(sorted(set(refusals), key=lambda r: r.value)),
                          social_mint, lat,
                          detail=f"{len(set(refusals))} refusal(s)")
    return SeamResult(SeamVerdict.SOCIAL_FILL_JOINABLE, (), social_mint, lat,
                      detail="all four join conditions hold")
