"""The canonical realized-fill record (REALIZED-FILL-CORPUS-001).

One row = one attempted route execution, from the quote that motivated it to
the markouts that judge it. Every field is typed for **absence** (doctrine 10)
and every field carries a **reconstructability class** so a consumer can tell a
venue fact from something we inferred.

Nothing in this module executes anything. A record is written from a
transaction that already confirmed, or it is not written.

Units policy — deliberate, and the reason the record is verbose:

* On-chain amounts are **integer base units** (`lamports`, token base units).
  Floats are banned in the cost basis. A 9-decimal SPL amount does not survive
  a float round trip, and a cost basis that drifts in the 9th decimal is a cost
  basis that cannot resolve a 4 bps hurdle.
* Prices and ratios are `Decimal`.
* Durations are integer milliseconds.
* Every amount is paired with its `mint` and its `decimals`, and `decimals` is
  itself a `Maybe` — RPC `meta` usually supplies it and sometimes does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from app.fills.absence import Absent, Maybe, as_json

#: The native mint. Wrapped SOL uses this address as an SPL mint, which is
#: exactly why wrapped-SOL accounting is a hard case: the same economic asset
#: appears in BOTH the lamport ledger and the token ledger, and double counting
#: it is a live way to corrupt an input amount.
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class Reconstructability(str, Enum):
    """What kind of claim a field is. Mirrors the measurement contract."""

    #: The venue/chain asserted it directly. `fee`, `slot`, `signature`.
    VENUE_FACT = "venue_fact"
    #: We computed it from venue facts by a stated rule. Balance deltas,
    #: realized price, realized slippage.
    DERIVED = "derived"
    #: We computed it from venue facts by a rule that provably loses
    #: information. Priority fee when compute-unit price is inferred rather
    #: than read; "cancelled vs executed" style inferences.
    DERIVED_LOSSY = "derived_lossy"
    #: Not obtainable from the available evidence at all.
    NOT_RECONSTRUCTABLE = "not_reconstructable"


class Side(str, Enum):
    """Direction in the token being measured, not in the quote asset."""

    ACQUIRE = "acquire"  # base token in, quote asset out
    DISPOSE = "dispose"  # base token out, quote asset in


class FillStatus(str, Enum):
    CONFIRMED = "confirmed"
    #: On-chain failure. Fees were still paid. Output is
    #: `TRANSACTION_FAILED`, never zero.
    FAILED = "failed"
    #: Confirmed, but the decoder refuses to assert amounts. A record in this
    #: state is retained; it is evidence about the decoder, and dropping it
    #: would bias the corpus toward transactions we happen to understand.
    UNDECODABLE = "undecodable"


class PriceSource(str, Enum):
    """Where a markout price came from. **This is not cosmetic.**

    A markout computed against a different venue is not the same measurement as
    one computed against the pool we actually traded, and a corpus that mixes
    them without saying so will report a venue basis as adverse selection.
    """

    #: Reserves of the exact pool(s) traded, read at the markout slot.
    SAME_POOL_RESERVES = "same_pool_reserves"
    #: A trade that actually occurred in the same pool at/near the horizon.
    SAME_POOL_TRADE = "same_pool_trade"
    #: A different venue or aggregator price for the same pair.
    OTHER_VENUE = "other_venue"
    #: An aggregate price feed (e.g. a pair-level API snapshot).
    AGGREGATOR_SNAPSHOT = "aggregator_snapshot"
    #: Interpolated between two observations that bracket the horizon.
    INTERPOLATED = "interpolated"
    #: No price at the horizon.
    NONE_AVAILABLE = "none_available"


#: Ranking used only to *report* measurement quality, never to silently
#: substitute a worse source for a better one.
PRICE_SOURCE_QUALITY = {
    PriceSource.SAME_POOL_TRADE: 0,
    PriceSource.SAME_POOL_RESERVES: 1,
    PriceSource.OTHER_VENUE: 2,
    PriceSource.AGGREGATOR_SNAPSHOT: 3,
    PriceSource.INTERPOLATED: 4,
    PriceSource.NONE_AVAILABLE: 99,
}


@dataclass(frozen=True, slots=True)
class TokenAmount:
    """An integer amount of one mint. Never a float."""

    mint: str
    base_units: int
    decimals: Maybe[int]

    def to_decimal(self) -> Maybe[Decimal]:
        """Human-scaled amount. Absent when decimals are absent — a base-unit
        count without its scale is not a quantity."""
        from app.fills.absence import Observed, observed

        if not isinstance(self.decimals, Observed):
            return self.decimals
        return observed(
            Decimal(self.base_units) / (Decimal(10) ** self.decimals.value),
            source="TokenAmount.to_decimal",
        )

    def as_json(self) -> dict:
        return {
            "mint": self.mint,
            "base_units": self.base_units,
            "decimals": as_json(self.decimals),
        }


@dataclass(frozen=True, slots=True)
class RouteLeg:
    """One hop. `pool` is the AMM account actually touched, when we can name
    it; `program_id` is the on-chain program that owned the instruction."""

    index: int
    program_id: Maybe[str]
    pool: Maybe[str]
    input_mint: Maybe[str]
    output_mint: Maybe[str]

    def as_json(self) -> dict:
        return {
            "index": self.index,
            "program_id": as_json(self.program_id),
            "pool": as_json(self.pool),
            "input_mint": as_json(self.input_mint),
            "output_mint": as_json(self.output_mint),
        }


@dataclass(frozen=True, slots=True)
class RouteDescriptor:
    """The route as a whole.

    `legs` may be `Absent`: a balance-delta decoder can measure the *endpoints*
    of a multi-hop route with full confidence while knowing nothing reliable
    about the middle. That is a legitimate and common state, and it must not be
    reported as a single-leg direct route.
    """

    legs: Maybe[tuple[RouteLeg, ...]]
    hop_count: Maybe[int]
    aggregator: Maybe[str]

    def as_json(self) -> dict:
        legs = self.legs
        legs_json: object
        if isinstance(legs, Absent):
            legs_json = as_json(legs)
        else:
            legs_json = [leg.as_json() for leg in legs.value]
        return {
            "legs": legs_json,
            "hop_count": as_json(self.hop_count),
            "aggregator": as_json(self.aggregator),
        }


@dataclass(frozen=True, slots=True)
class QuoteRecord:
    """What we were told the route would do, BEFORE it happened.

    Recorded verbatim. It is the counterfactual half of `eps_fill` and it must
    never be back-filled from the outcome — a quote reconstructed after the
    fill is not a quote, it is a fit.
    """

    t_quote: Maybe[datetime]
    quoted_input: Maybe[TokenAmount]
    quoted_output: Maybe[TokenAmount]
    #: output per unit input, in human units
    quoted_price: Maybe[Decimal]
    #: fraction, e.g. Decimal("0.0042") for 42 bps. NOT percent.
    quoted_price_impact: Maybe[Decimal]
    #: the slippage-protected floor the quote carried
    quoted_min_output: Maybe[TokenAmount]
    quote_source: Maybe[str]
    #: verbatim provider payload identifier, for audit
    quote_capture_id: Maybe[str]

    def as_json(self) -> dict:
        def amt(m):
            return as_json(m) if isinstance(m, Absent) else m.value.as_json()

        t = self.t_quote
        return {
            "t_quote": as_json(t) if isinstance(t, Absent) else t.value.isoformat(),
            "quoted_input": amt(self.quoted_input),
            "quoted_output": amt(self.quoted_output),
            "quoted_price": as_json(self.quoted_price),
            "quoted_price_impact": as_json(self.quoted_price_impact),
            "quoted_min_output": amt(self.quoted_min_output),
            "quote_source": as_json(self.quote_source),
            "quote_capture_id": as_json(self.quote_capture_id),
        }


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """The three cost terms, kept apart on purpose.

    Confusing them corrupts the cost basis, which is the entire point of the
    corpus:

    * **network fee** — the base transaction fee, `5000 lamports x signatures`
      today. A protocol constant.
    * **priority fee** — `compute_unit_price (micro-lamports/CU) x compute
      units CONSUMED`, ceil-divided by 1e6. Paid to the leader. Discretionary,
      moves with congestion, and is the term an execution-quality argument can
      actually touch.
    * **tip** — a plain SOL transfer to a block-engine tip account. **Not a
      protocol fee at all.** It never appears in `meta.fee`. A cost model that
      reads `meta.fee` and stops has omitted it entirely, and in a competitive
      block the tip is routinely larger than both other terms combined.

    All three in lamports. `total()` propagates absence.
    """

    network_fee_lamports: Maybe[Decimal]
    priority_fee_lamports: Maybe[Decimal]
    tip_lamports: Maybe[Decimal]
    compute_units_consumed: Maybe[int]
    compute_unit_price_micro_lamports: Maybe[int]
    #: rent debited for ATA creation, and rent credited back on close. Not a
    #: trading cost, but it moves the lamport balance and will be misread as
    #: one if it is not separated.
    rent_lamports_net: Maybe[Decimal]
    tip_destinations: Maybe[tuple[str, ...]]

    def total_lamports(self) -> Maybe[Decimal]:
        from app.fills.absence import combine

        return combine(
            self.network_fee_lamports,
            self.priority_fee_lamports,
            self.tip_lamports,
            source="CostBreakdown.total_lamports",
        )

    def as_json(self) -> dict:
        dests = self.tip_destinations
        return {
            "network_fee_lamports": as_json(self.network_fee_lamports),
            "priority_fee_lamports": as_json(self.priority_fee_lamports),
            "tip_lamports": as_json(self.tip_lamports),
            "compute_units_consumed": as_json(self.compute_units_consumed),
            "compute_unit_price_micro_lamports": as_json(
                self.compute_unit_price_micro_lamports
            ),
            "rent_lamports_net": as_json(self.rent_lamports_net),
            "tip_destinations": (
                as_json(dests) if isinstance(dests, Absent) else list(dests.value)
            ),
            "total_lamports": as_json(self.total_lamports()),
        }


@dataclass(frozen=True, slots=True)
class Markout:
    """One post-fill price observation.

    `price` is in the SAME units as `actual_price` — output asset per unit of
    input asset — or the difference is meaningless.
    """

    horizon_seconds: int
    price: Maybe[Decimal]
    source: PriceSource
    #: how far the observation actually was from the horizon. A "5s markout"
    #: taken 41s late is a different measurement and must say so.
    observation_offset_ms: Maybe[int]

    def as_json(self) -> dict:
        return {
            "horizon_seconds": self.horizon_seconds,
            "price": as_json(self.price),
            "source": self.source.value,
            "observation_offset_ms": as_json(self.observation_offset_ms),
        }


@dataclass(frozen=True, slots=True)
class StateLabels:
    """Regime labels at decision time, supplied by their owners.

    Per `VOLATILITY-STATE-ENGINE-001` §3 a regime is *"a typed label with an
    owner"*, computed once and consumed by everything downstream. This record
    stores the label; it never computes one. `volatility_state` is expected to
    be `NOT_COMPUTABLE:no_fill_history` for R4 until this very corpus exists,
    and storing that string is the correct behaviour, not a gap.
    """

    liquidity_state: Maybe[str]
    volatility_state: Maybe[str]
    social_state: Maybe[str]

    def as_json(self) -> dict:
        return {
            "liquidity_state": as_json(self.liquidity_state),
            "volatility_state": as_json(self.volatility_state),
            "social_state": as_json(self.social_state),
        }


@dataclass(frozen=True, slots=True)
class RealizedFill:
    """The canonical record.

    Field groups map 1:1 onto the measurement contract's table, and the
    contract is the normative description of what each one may be claimed to
    mean.
    """

    # --- identity -----------------------------------------------------------
    #: The decision or observation this execution answers to. Absent for a
    #: fixture harvested from the public chain: it answers to no decision of
    #: ours, and pretending otherwise would poison the linkage statistics.
    decision_id: Maybe[str]
    observation_id: Maybe[str]
    #: The token being measured (base). Distinct from the quote asset.
    mint: str
    side: Side
    #: Trade size expressed in the QUOTE asset, human units. The denominator
    #: everything economic is quoted against.
    notional_quote_units: Maybe[Decimal]
    quote_asset_mint: Maybe[str]

    # --- route --------------------------------------------------------------
    route: RouteDescriptor

    # --- quote (pre-trade) --------------------------------------------------
    quote: QuoteRecord

    # --- submission ---------------------------------------------------------
    t_submit: Maybe[datetime]
    signature: Maybe[str]

    # --- confirmation -------------------------------------------------------
    slot: Maybe[int]
    t_confirmed: Maybe[datetime]
    status: FillStatus

    # --- realized -----------------------------------------------------------
    actual_input: Maybe[TokenAmount]
    actual_output: Maybe[TokenAmount]
    costs: CostBreakdown
    #: realized output per unit input, human units
    actual_price: Maybe[Decimal]
    #: (quoted_price - actual_price) / quoted_price. Positive = we did worse
    #: than quoted. Sign convention is stated once, here, and nowhere else.
    realized_slippage: Maybe[Decimal]

    # --- latency ------------------------------------------------------------
    quote_to_submit_ms: Maybe[int]
    submit_to_confirm_ms: Maybe[int]

    # --- labels -------------------------------------------------------------
    markouts: tuple[Markout, ...]
    states: StateLabels
    #: Identifier of the model/version that produced the decision. Required by
    #: the PAPER_SIMULATION artifact rule and by ALPHA-FACTORY-001 §7.8
    #: (data/feature versioning against silent look-ahead).
    model_version: Maybe[str]

    # --- provenance ---------------------------------------------------------
    decoder_version: str
    #: Per-field reconstructability, so a consumer never has to guess.
    reconstructability: dict[str, Reconstructability] = field(default_factory=dict)
    #: Everything the decoder refused to assert, and why. A non-empty list is
    #: normal and is the honest state for most real routes.
    decoder_notes: tuple[str, ...] = ()

    def markout(self, horizon_seconds: int) -> Markout | None:
        for m in self.markouts:
            if m.horizon_seconds == horizon_seconds:
                return m
        return None

    def as_json(self) -> dict:
        def amt(m):
            return as_json(m) if isinstance(m, Absent) else m.value.as_json()

        def ts(m):
            return as_json(m) if isinstance(m, Absent) else m.value.isoformat()

        return {
            "decision_id": as_json(self.decision_id),
            "observation_id": as_json(self.observation_id),
            "mint": self.mint,
            "side": self.side.value,
            "notional_quote_units": as_json(self.notional_quote_units),
            "quote_asset_mint": as_json(self.quote_asset_mint),
            "route": self.route.as_json(),
            "quote": self.quote.as_json(),
            "t_submit": ts(self.t_submit),
            "signature": as_json(self.signature),
            "slot": as_json(self.slot),
            "t_confirmed": ts(self.t_confirmed),
            "status": self.status.value,
            "actual_input": amt(self.actual_input),
            "actual_output": amt(self.actual_output),
            "costs": self.costs.as_json(),
            "actual_price": as_json(self.actual_price),
            "realized_slippage": as_json(self.realized_slippage),
            "quote_to_submit_ms": as_json(self.quote_to_submit_ms),
            "submit_to_confirm_ms": as_json(self.submit_to_confirm_ms),
            "markouts": [m.as_json() for m in self.markouts],
            "states": self.states.as_json(),
            "model_version": as_json(self.model_version),
            "decoder_version": self.decoder_version,
            "reconstructability": {
                k: v.value for k, v in self.reconstructability.items()
            },
            "decoder_notes": list(self.decoder_notes),
        }


#: The horizons the corpus commits to. Fixed at registration so a horizon
#: cannot be chosen after the data is seen (ALPHA-FACTORY-001 §7.1).
MARKOUT_HORIZONS_SECONDS = (1, 5, 30, 300)
