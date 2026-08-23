"""M0 and M1 features, exactly as MARKET-STATE-FABRIC-v1 §3–§4 freeze them.

**This module must never import `labels`.** The dependency direction is the
structural guarantee that a future price cannot reach a feature: nothing here
can see past time `t` because nothing here is ever handed a post-`t` value.
A test asserts the import graph, so the guarantee survives refactoring.

Prices are probabilities. `PRICE_SCALE = 10_000` units per dollar and a Kalshi
contract settles in [0, 1], so probability = `price_units / 10_000`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

PRICE_UNITS_PER_PROB = 10_000
CONTRACT_UNITS_PER_CONTRACT = 100
FIVE_CENTS_UNITS = 500                      # 0.05 probability

#: Trailing windows for the flow block (fabric §4).
FLOW_WINDOWS_S = (1, 5, 30)

#: The pre-declared trade safety margin. §16.4 measured a 580 ms max
#: cross-sid interarrival on the P4 tape; 1,000 ms respects that scale with
#: margin. The preregistration requires M1 be reported at this lag AND at
#: double it, so a result that depends on the lag is visible as the timing
#: artefact it is.
TRADE_LAG_MS = 1_000
TRADE_LAG_DOUBLE_MS = 2 * TRADE_LAG_MS

#: The fabric's §3 table has twelve numbered rows, but row 9 names TWO fields
#: (`levels_bid` / `levels_ask`), so the M0 block is thirteen columns under
#: twelve definitions. Recorded rather than silently reconciled.
M0_FEATURES = (
    "mid", "spread", "depth_bid_l1", "depth_ask_l1", "imbalance_l1",
    "depth_bid_5c", "depth_ask_5c", "imbalance_5c", "levels_bid",
    "levels_ask", "microprice", "micro_minus_mid", "dist_to_bound",
)

#: Controls, NOT predictive features (fabric §3). Carried for stratification.
M0_CONTROLS = ("seconds_to_close", "book_age_ms")

M1_FLOW_BASE = ("delta_count", "signed_depth_flow", "quote_reversal",
                "realized_vol", "trade_count", "signed_trade_flow")
M1_FLOW_FEATURES = tuple(f"{n}_{w}s" for w in FLOW_WINDOWS_S
                         for n in M1_FLOW_BASE)
M1_FEATURES = M0_FEATURES + M1_FLOW_FEATURES

#: A value the venue did not provide. NEVER collapses to 0.0 -- "no ask side"
#: and "an ask side of zero" are different observations.
NOT_PROVIDED = None


@dataclass(frozen=True)
class BookView:
    """The ladder at one instant, already YES-scaled. Immutable by design."""
    market_ticker: str
    bids: dict          # price_units -> contract_units
    asks: dict          # price_units -> contract_units
    last_modified_ms: float | None
    sample_time_ms: float

    @property
    def best_bid_units(self):
        return max(self.bids) if self.bids else None

    @property
    def best_ask_units(self):
        return min(self.asks) if self.asks else None


def _prob(units):
    return None if units is None else units / PRICE_UNITS_PER_PROB


def _contracts(units):
    return None if units is None else units / CONTRACT_UNITS_PER_CONTRACT


def compute_m0(view: BookView, *, seconds_to_close: float | None) -> dict:
    """The thirteen state columns plus the two controls.

    Every field is `NOT_PROVIDED` rather than zero when the ladder cannot
    support it. A one-sided book has no mid, and saying `mid = 0` would be a
    fabricated observation at the bottom of the probability range.
    """
    bid_u, ask_u = view.best_bid_units, view.best_ask_units
    out = {name: NOT_PROVIDED for name in M0_FEATURES}

    out["levels_bid"] = len(view.bids)
    out["levels_ask"] = len(view.asks)
    out["depth_bid_l1"] = _contracts(view.bids.get(bid_u)) if bid_u is not None else NOT_PROVIDED
    out["depth_ask_l1"] = _contracts(view.asks.get(ask_u)) if ask_u is not None else NOT_PROVIDED

    if out["depth_bid_l1"] is not None and out["depth_ask_l1"] is not None:
        b, a = out["depth_bid_l1"], out["depth_ask_l1"]
        out["imbalance_l1"] = (b - a) / (b + a) if (b + a) > 0 else NOT_PROVIDED

    if bid_u is None or ask_u is None:
        # No two-sided book: mid, spread, microprice and everything derived
        # from them are genuinely unobserved.
        return {**out, "seconds_to_close": seconds_to_close,
                "book_age_ms": _book_age(view)}

    mid_u = (bid_u + ask_u) / 2
    out["mid"] = _prob(mid_u)
    out["spread"] = _prob(ask_u - bid_u)
    out["dist_to_bound"] = min(out["mid"], 1.0 - out["mid"])

    lo, hi = mid_u - FIVE_CENTS_UNITS, mid_u + FIVE_CENTS_UNITS
    bid5 = sum(q for p, q in view.bids.items() if lo <= p <= hi)
    ask5 = sum(q for p, q in view.asks.items() if lo <= p <= hi)
    out["depth_bid_5c"] = _contracts(bid5)
    out["depth_ask_5c"] = _contracts(ask5)
    out["imbalance_5c"] = ((bid5 - ask5) / (bid5 + ask5)
                           if (bid5 + ask5) > 0 else NOT_PROVIDED)

    bsz, asz = view.bids[bid_u], view.asks[ask_u]
    if (bsz + asz) > 0:
        micro_u = (bid_u * asz + ask_u * bsz) / (bsz + asz)
        out["microprice"] = _prob(micro_u)
        out["micro_minus_mid"] = _prob(micro_u - mid_u)

    return {**out, "seconds_to_close": seconds_to_close,
            "book_age_ms": _book_age(view)}


def _book_age(view: BookView):
    if view.last_modified_ms is None:
        return NOT_PROVIDED
    return max(0.0, view.sample_time_ms - view.last_modified_ms)


@dataclass
class FlowAccumulator:
    """Order-book and trade events, kept only so windows can be summed.

    Events are appended as they are replayed. `window()` reads a half-open
    trailing interval; it cannot see anything the replay has not reached.
    """
    deltas: list = field(default_factory=list)   # (ms, signed_units, side, price_u)
    trades: list = field(default_factory=list)   # (ms, signed_contracts)
    best_prices: list = field(default_factory=list)  # (ms, best_bid_u, best_ask_u)
    mids: list = field(default_factory=list)     # (ms, mid_prob) at 1 Hz samples

    def add_delta(self, ms: float, signed_units: float, side: str, price_u: int):
        self.deltas.append((ms, signed_units, side, price_u))

    def add_trade(self, ms: float, signed_contracts: float):
        self.trades.append((ms, signed_contracts))

    def add_best(self, ms: float, bid_u, ask_u):
        self.best_prices.append((ms, bid_u, ask_u))

    def add_mid(self, ms: float, mid):
        self.mids.append((ms, mid))


def compute_m1_flow(acc: FlowAccumulator, t_ms: float, *,
                    trade_lag_ms: int = TRADE_LAG_MS) -> dict:
    """The flow block over each Δ, from events STRICTLY before `t`.

    Order-book windows are `(t-Δ, t]`. **Trade windows end at `t -
    trade_lag_ms`**, because trades cross a sid boundary and carry no
    venue-guaranteed ordering against the book — only receive timestamps
    relate them. The lag is the pre-declared safety margin, not a tuning knob.
    """
    out = {}
    for w in FLOW_WINDOWS_S:
        lo = t_ms - w * 1000
        win = [d for d in acc.deltas if lo < d[0] <= t_ms]
        out[f"delta_count_{w}s"] = len(win)
        # side "yes" is a resting BID, side "no" is a YES-scaled OFFER.
        out[f"signed_depth_flow_{w}s"] = sum(
            (u if side == "yes" else -u) for _ms, u, side, _p in win
        ) / CONTRACT_UNITS_PER_CONTRACT

        bests = [b for b in acc.best_prices if lo < b[0] <= t_ms]
        out[f"quote_reversal_{w}s"] = _quote_reversals(bests)

        mids = [m for m in acc.mids if lo < m[0] <= t_ms and m[1] is not None]
        diffs = [b - a for (_, a), (_, b) in zip(mids, mids[1:])]
        out[f"realized_vol_{w}s"] = (statistics.stdev(diffs)
                                     if len(diffs) >= 2 else NOT_PROVIDED)

        # LAGGED trade window -- ends before t, never at it.
        t_trade = t_ms - trade_lag_ms
        lo_trade = t_trade - w * 1000
        tw = [x for x in acc.trades if lo_trade < x[0] <= t_trade]
        out[f"trade_count_{w}s"] = len(tw)
        out[f"signed_trade_flow_{w}s"] = sum(c for _ms, c in tw)
    return out


def _quote_reversals(bests) -> int:
    """Direction changes in the best-price mid across the window."""
    seq = [(b, a) for _ms, b, a in bests if b is not None and a is not None]
    if len(seq) < 3:
        return 0
    mids = [(b + a) / 2 for b, a in seq]
    signs = [0 if x == y else (1 if y > x else -1)
             for x, y in zip(mids, mids[1:])]
    signs = [s for s in signs if s != 0]
    return sum(1 for x, y in zip(signs, signs[1:]) if x != y)


def m0_is_subset_of_m1() -> bool:
    """Frozen containment: M1 = M0 + exactly the preregistered flow set."""
    return (set(M0_FEATURES) < set(M1_FEATURES)
            and set(M1_FEATURES) - set(M0_FEATURES) == set(M1_FLOW_FEATURES))
