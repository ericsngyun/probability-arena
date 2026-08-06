"""KALSHI-REALTIME-OBSERVATION-001A — event envelope and order-book state machine.

Two things live here and they are deliberately separate:

* the **envelope**, which carries full time lineage and the RAW venue fields
  alongside the normalized interpretation, so normalization is never destructive;
* the **book**, which reconstructs a canonical YES-denominated ladder from a
  snapshot plus ordered deltas and refuses to publish across a sequence gap.

The rule that shapes the whole file: *a book whose sequence integrity is
unresolved must not be published as current*. Continuing across a gap produces a
book that looks fine and is quietly missing levels — the failure mode that is
hardest to notice and most expensive to have trusted.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.realtime.fixedpoint import (
    ONE_DOLLAR_UNITS,
    FixedPointError,
    PriceGrid,
    complement_price_units,
    format_contract_units,
    format_price_units,
    parse_contract_units,
    parse_price_units,
)

SIDE_YES = "yes"
SIDE_NO = "no"

# Book generation advances on every (re)synchronisation. A consumer that holds a
# stale generation knows its view is not merely old but discontinuous.
GEN_INITIAL = 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def monotonic_ns() -> int:
    """Durations only. Wall clock is for timestamps, never for elapsed time."""
    return time.monotonic_ns()


@dataclass
class EventEnvelope:
    """One normalized market event with its full time lineage.

    `raw` is retained verbatim. Normalization adds an interpretation; it never
    replaces the venue's own words, so a later question about what the venue
    actually said is answerable from the archive rather than reconstructed.
    """
    schema_version: int
    venue: str
    environment: str                  # demo | production — never mixed
    channel: str
    event_type: str
    market_ticker: str | None
    market_id: str | None
    sid: int | None
    seq: int | None
    venue_time: str | None            # venue-stamped, when supplied
    collector_receive_time: str        # wall clock at receive
    normalization_time: str            # wall clock after normalization
    receive_monotonic_ns: int          # for exact durations
    normalize_monotonic_ns: int
    data_age_ms: float | None          # venue_time -> receive, when derivable
    implementation_version: str
    raw: dict = field(default_factory=dict)
    normalized: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


IMPLEMENTATION_VERSION = "kalshi-realtime-observer/001A"
ENVELOPE_SCHEMA_VERSION = 1


def make_envelope(*, venue: str, environment: str, channel: str, message: dict,
                  receive_time: datetime, receive_mono: int,
                  normalized: dict | None = None) -> EventEnvelope:
    msg = message.get("msg") or {}
    venue_time = msg.get("ts") or msg.get("timestamp")
    venue_iso = None
    age_ms = None
    if venue_time is not None:
        try:
            # Kalshi stamps seconds or milliseconds depending on channel; both
            # are handled explicitly rather than guessed from magnitude alone.
            ts = int(venue_time)
            seconds = ts / 1000 if ts > 10**11 else ts
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
            venue_iso = dt.isoformat()
            age_ms = round((receive_time - dt).total_seconds() * 1000, 3)
        except (TypeError, ValueError, OSError):
            venue_iso = None
    now = utcnow()
    return EventEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION, venue=venue,
        environment=environment, channel=channel,
        event_type=str(message.get("type") or "unknown"),
        market_ticker=msg.get("market_ticker"), market_id=msg.get("market_id"),
        sid=message.get("sid"), seq=message.get("seq"),
        venue_time=venue_iso,
        collector_receive_time=receive_time.isoformat(),
        normalization_time=now.isoformat(),
        receive_monotonic_ns=receive_mono,
        normalize_monotonic_ns=monotonic_ns(),
        data_age_ms=age_ms,
        implementation_version=IMPLEMENTATION_VERSION,
        raw=message, normalized=normalized or {},
    )


# --- sequence integrity ---------------------------------------------------------

SEQ_OK = "ok"
SEQ_DUPLICATE = "duplicate"
SEQ_GAP = "gap"
SEQ_REGRESSION = "regression"


class BookIntegrityError(RuntimeError):
    """The book cannot be trusted and must not be published."""


@dataclass
class BookLevel:
    price_units: int
    contract_units: int


class OrderBook:
    """Canonical YES-denominated book for one market.

    Kalshi publishes two ladders, YES and NO, and they are NOT a bid/ask pair.
    With `use_yes_price=true` both arrive on the YES price scale, and the
    economic reading is:

        yes-side level  -> a resting BID for YES at p
        no-side level   -> a resting economic OFFER of YES at p

    Both raw sides are kept. `best_yes_bid` and `best_yes_ask` are derived, and
    the ask is derived from the NO ladder rather than assumed to exist on the
    YES one — assuming conventional bid/ask semantics here is the single most
    likely way to build a plausible, wrong book.
    """

    def __init__(self, market_ticker: str, *, grid: PriceGrid | None = None,
                 use_yes_price: bool = True):
        self.market_ticker = market_ticker
        self.grid = grid or PriceGrid([])
        self.use_yes_price = use_yes_price
        self.yes: dict[int, int] = {}
        self.no: dict[int, int] = {}
        self.sid: int | None = None
        self.last_seq: int | None = None
        self.generation = GEN_INITIAL
        self.synced = False
        self.integrity_reason: str | None = "awaiting snapshot"
        self.snapshot_receive_time: datetime | None = None
        self.stats = {"snapshots": 0, "deltas": 0, "duplicates": 0, "gaps": 0,
                      "regressions": 0, "resyncs": 0, "rejected_pre_snapshot": 0}

    # -- integrity ---------------------------------------------------------------

    @property
    def publishable(self) -> bool:
        """A book is publishable only when synced AND sequence-clean."""
        return self.synced and self.integrity_reason is None

    def _require_publishable(self) -> None:
        if not self.publishable:
            raise BookIntegrityError(
                f"{self.market_ticker}: book is not publishable "
                f"({self.integrity_reason})")

    def classify_seq(self, seq: int | None) -> str:
        if seq is None or self.last_seq is None:
            return SEQ_OK
        if seq == self.last_seq:
            return SEQ_DUPLICATE
        if seq < self.last_seq:
            return SEQ_REGRESSION
        if seq > self.last_seq + 1:
            return SEQ_GAP
        return SEQ_OK

    # -- snapshot / delta --------------------------------------------------------

    def apply_snapshot(self, msg: dict, *, sid: int | None = None,
                       seq: int | None = None,
                       receive_time: datetime | None = None) -> dict:
        """Replace the book wholesale and clear any integrity fault."""
        yes_levels = self._parse_levels(msg.get("yes_dollars_fp") or [], "yes")
        no_levels = self._parse_levels(msg.get("no_dollars_fp") or [], "no")
        self.yes = {p: q for p, q in yes_levels if q > 0}
        self.no = {p: q for p, q in no_levels if q > 0}
        self.sid = sid if sid is not None else self.sid
        self.last_seq = seq
        self.generation += 1
        self.synced = True
        self.integrity_reason = None
        self.snapshot_receive_time = receive_time or utcnow()
        self.stats["snapshots"] += 1
        return {"action": "snapshot", "generation": self.generation,
                "yes_levels": len(self.yes), "no_levels": len(self.no)}

    def _parse_levels(self, raw_levels, side: str):
        out = []
        for entry in raw_levels:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise FixedPointError(
                    f"{self.market_ticker}: {side} level {entry!r} is not a "
                    "[price, count] pair")
            price = parse_price_units(entry[0], field=f"{side}.price_dollars")
            self.grid.validate(price, field=f"{side}.price_dollars")
            # Snapshot quantities are resting sizes: never negative.
            qty = parse_contract_units(entry[1], field=f"{side}.contract_count_fp",
                                       allow_negative=False)
            out.append((price, qty))
        return out

    def apply_delta(self, msg: dict, *, seq: int | None = None) -> dict:
        """Apply one incremental change, refusing to continue across a gap."""
        if not self.synced:
            self.stats["rejected_pre_snapshot"] += 1
            # Documented bounded rule: pre-snapshot deltas are REJECTED, not
            # buffered. Buffering would require guessing how far back the
            # snapshot will reach, and a wrong guess silently double-applies.
            raise BookIntegrityError(
                f"{self.market_ticker}: delta received before any snapshot; "
                "rejected rather than buffered")

        status = self.classify_seq(seq)
        if status == SEQ_DUPLICATE:
            self.stats["duplicates"] += 1
            return {"action": "duplicate_ignored", "seq": seq}
        if status in (SEQ_GAP, SEQ_REGRESSION):
            self.stats["gaps" if status == SEQ_GAP else "regressions"] += 1
            self.synced = False
            self.integrity_reason = (
                f"sequence {status}: expected {(self.last_seq or 0) + 1}, "
                f"got {seq}")
            # Never silently continue. The caller must resynchronise.
            raise BookIntegrityError(
                f"{self.market_ticker}: {self.integrity_reason}")

        side = str(msg.get("side") or "").lower()
        if side not in (SIDE_YES, SIDE_NO):
            raise FixedPointError(f"{self.market_ticker}: unknown side {side!r}")
        price = parse_price_units(msg["price_dollars"], field="price_dollars")
        self.grid.validate(price, field="price_dollars")
        # A delta MAY be negative: that is how a level is decremented.
        change = parse_contract_units(msg["delta_fp"], field="delta_fp",
                                      allow_negative=True)

        ladder = self.yes if side == SIDE_YES else self.no
        current = ladder.get(price, 0)
        updated = current + change
        if updated < 0:
            self.synced = False
            self.integrity_reason = (
                f"delta drove {side} {format_price_units(price)} negative "
                f"({current} {change:+d}); the local book disagrees with the venue")
            raise BookIntegrityError(
                f"{self.market_ticker}: {self.integrity_reason}")
        if updated == 0:
            ladder.pop(price, None)
        else:
            ladder[price] = updated
        self.last_seq = seq if seq is not None else self.last_seq
        self.stats["deltas"] += 1
        return {"action": "delta", "side": side, "price_units": price,
                "delta_units": change, "level_units": updated,
                "deleted": updated == 0}

    def mark_resynchronised(self) -> None:
        self.stats["resyncs"] += 1

    # -- derived views -----------------------------------------------------------

    @property
    def best_yes_bid_units(self) -> int | None:
        """Highest resting YES bid."""
        self._require_publishable()
        return max(self.yes) if self.yes else None

    @property
    def best_yes_ask_units(self) -> int | None:
        """Lowest YES offer, derived from the NO ladder.

        The best NO bid is the *highest* NO price; as a YES offer that is the
        *lowest* complement. Derived, never assumed to exist on the YES side.
        """
        self._require_publishable()
        if not self.no:
            return None
        return complement_price_units(max(self.no))

    def top_of_book(self) -> dict:
        self._require_publishable()
        bid, ask = self.best_yes_bid_units, self.best_yes_ask_units
        return {
            "market_ticker": self.market_ticker,
            "generation": self.generation,
            "last_seq": self.last_seq,
            "best_yes_bid": format_price_units(bid) if bid is not None else None,
            "best_yes_bid_units": bid,
            "best_yes_bid_size": (format_contract_units(self.yes[bid])
                                  if bid is not None else None),
            "best_yes_ask": format_price_units(ask) if ask is not None else None,
            "best_yes_ask_units": ask,
            "best_yes_ask_size": (
                format_contract_units(self.no[complement_price_units(ask)])
                if ask is not None else None),
            "spread_units": (ask - bid) if (bid is not None and ask is not None)
                            else None,
            "yes_levels": len(self.yes), "no_levels": len(self.no),
        }

    def yes_scale_ladder(self) -> dict:
        """Both ladders on one YES scale, with the raw side preserved."""
        self._require_publishable()
        bids = [{"price_units": p, "price": format_price_units(p),
                 "size_units": q, "raw_side": SIDE_YES,
                 "interpretation": "resting bid for YES"}
                for p, q in sorted(self.yes.items(), reverse=True)]
        asks = [{"price_units": complement_price_units(p),
                 "price": format_price_units(complement_price_units(p)),
                 "size_units": q, "raw_side": SIDE_NO,
                 "raw_price_units": p,
                 "interpretation": "resting economic offer of YES"}
                for p, q in sorted(self.no.items(), reverse=True)]
        asks.sort(key=lambda level: level["price_units"])
        return {"market_ticker": self.market_ticker,
                "generation": self.generation, "bids": bids, "asks": asks}

    def checksum(self) -> str:
        """Deterministic digest of book state, for replay equality."""
        import hashlib
        import json

        payload = {
            "market_ticker": self.market_ticker,
            "yes": sorted(self.yes.items()), "no": sorted(self.no.items()),
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
