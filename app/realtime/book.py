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

import hashlib
import json

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
SEQ_MISSING = "missing_sequence"


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
        if seq is None:
            # Absent is not ordered. A missing seq made every subsequent delta
            # classify OK forever, gaps included, while the book still reported
            # itself publishable — the exact reasoning `verify_scopes` applies
            # to an absent scopes field, applied inconsistently here.
            return SEQ_MISSING
        if self.last_seq is None:
            return SEQ_OK
        if seq == self.last_seq:
            return SEQ_DUPLICATE
        if seq < self.last_seq:
            return SEQ_REGRESSION
        if seq > self.last_seq + 1:
            return SEQ_GAP
        return SEQ_OK

    # -- snapshot / delta --------------------------------------------------------

    def _halt(self, reason: str) -> None:
        """Unpublish. Every refusal in this class goes through here.

        Previously only `classify_seq` faults and the negative-level check set
        `integrity_reason`, so a parse rejection, an off-grid price or a
        missing field left `synced=True` and the book kept serving its
        pre-rejection top of book as current. The venue told us a level
        changed, we dropped the message, and nothing recorded that.
        """
        self.synced = False
        self.integrity_reason = reason

    def apply_snapshot(self, msg: dict, *, sid: int | None = None,
                       seq: int | None = None,
                       receive_time: datetime | None = None) -> dict:
        """Replace the book wholesale and clear any integrity fault.

        Fails closed: anything that prevents the snapshot from being applied in
        full leaves the book unpublishable. A snapshot is the resync signal, so
        a silently dropped one leaves the previous book published indefinitely.
        """
        try:
            ticker = msg.get("market_ticker")
            if ticker is not None and ticker != self.market_ticker:
                raise BookIntegrityError(
                    f"{self.market_ticker}: snapshot is labelled {ticker!r}; "
                    "a book must never absorb another market's data")
            if seq is not None and self.last_seq is not None and seq < self.last_seq:
                # Older state arriving with a higher generation, published as
                # current, is the one thing generation numbering exists to
                # prevent. At-least-once redelivery on reconnect makes this
                # ordinary, not exotic.
                raise BookIntegrityError(
                    f"{self.market_ticker}: snapshot seq {seq} is behind the "
                    f"applied position {self.last_seq}; refusing to rewind")
            if "yes_dollars_fp" not in msg and "no_dollars_fp" not in msg:
                # `.get(...) or []` turned an unrecognised payload into an
                # empty book that reported itself synced and publishable —
                # indistinguishable from a genuinely empty market.
                raise BookIntegrityError(
                    f"{self.market_ticker}: snapshot carries neither "
                    "'yes_dollars_fp' nor 'no_dollars_fp'; an unparsed "
                    "snapshot must not mark the book synced")
            yes_levels = self._parse_levels(msg.get("yes_dollars_fp") or [], "yes")
            no_levels = self._parse_levels(msg.get("no_dollars_fp") or [], "no")
            yes = {p: q for p, q in yes_levels if q > 0}
            no = {p: q for p, q in no_levels if q > 0}
            self._require_uncrossed(yes, no)
        except Exception as exc:
            self._halt(f"snapshot rejected: {type(exc).__name__}: {exc}")
            raise
        self.yes, self.no = yes, no
        if sid is not None and self.sid is not None and sid != self.sid:
            self.stats["resyncs"] += 1
        self.sid = sid if sid is not None else self.sid
        self.last_seq = seq
        self.generation += 1
        self.synced = seq is not None
        self.integrity_reason = None if seq is not None else (
            "snapshot carried no sequence number; ordering cannot be verified")
        self.snapshot_receive_time = receive_time or utcnow()
        self.stats["snapshots"] += 1
        return {"action": "snapshot", "generation": self.generation,
                "yes_levels": len(self.yes), "no_levels": len(self.no)}

    def _require_uncrossed(self, yes: dict, no: dict) -> None:
        """A crossed book is the observable symptom of a wrong NO mapping.

        It is the cheapest invariant available and the one this design most
        needs, because the YES/NO complement is the single most likely way to
        build a plausible, wrong book.
        """
        if not yes or not no:
            return
        bid = max(yes)
        ask = ONE_DOLLAR_UNITS - max(no)
        if ask < bid:
            raise BookIntegrityError(
                f"{self.market_ticker}: crossed book — best YES bid "
                f"{format_price_units(bid)} exceeds derived best YES ask "
                f"{format_price_units(ask)}")

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
            if any(price == seen for seen, _ in out):
                raise FixedPointError(
                    f"{self.market_ticker}: {side} level "
                    f"{format_price_units(price)} appears twice in one "
                    "snapshot; last-write-wins would silently lose depth")
            out.append((price, qty))
        return out

    def apply_delta(self, msg: dict, *, seq: int | None = None,
                    sid: int | None = None) -> dict:
        """Apply one incremental change, refusing to continue across a gap.

        Fails closed on every rejection path, not only on sequence faults.
        """
        try:
            return self._apply_delta(msg, seq=seq, sid=sid)
        except BookIntegrityError:
            raise                       # already halted at the point of refusal
        except Exception as exc:
            self._halt(f"delta rejected: {type(exc).__name__}: {exc}")
            raise

    def _apply_delta(self, msg: dict, *, seq: int | None = None,
                     sid: int | None = None) -> dict:
        if not self.synced:
            self.stats["rejected_pre_snapshot"] += 1
            # Documented bounded rule: pre-snapshot deltas are REJECTED, not
            # buffered. Buffering would require guessing how far back the
            # snapshot will reach, and a wrong guess silently double-applies.
            raise BookIntegrityError(
                f"{self.market_ticker}: delta received before any snapshot; "
                "rejected rather than buffered")

        ticker = msg.get("market_ticker")
        if ticker is not None and ticker != self.market_ticker:
            self._halt(f"delta is labelled {ticker!r}, not {self.market_ticker}")
            raise BookIntegrityError(
                f"{self.market_ticker}: delta is labelled {ticker!r}; a book "
                "must never absorb another market's data")
        if sid is not None and self.sid is not None and sid != self.sid:
            # Kalshi's seq is per SUBSCRIPTION. A delta from a superseded
            # subscription carries a seq from a different namespace, so
            # comparing it against this book's position is meaningless.
            self._halt(f"delta belongs to sid {sid}, book is on sid {self.sid}")
            raise BookIntegrityError(
                f"{self.market_ticker}: delta belongs to subscription {sid}, "
                f"this book is on {self.sid}; sequence numbers from a "
                "superseded subscription are not comparable")

        status = self.classify_seq(seq)
        if status == SEQ_DUPLICATE:
            self.stats["duplicates"] += 1
            return {"action": "duplicate_ignored", "seq": seq}
        if status in (SEQ_GAP, SEQ_REGRESSION, SEQ_MISSING):
            self.stats["gaps" if status == SEQ_GAP else "regressions"] += 1
            if status == SEQ_MISSING:
                self._halt("delta carried no sequence number; ordering cannot "
                           "be verified and absent is not ordered")
            else:
                self._halt(f"sequence {status}: expected "
                           f"{(self.last_seq or 0) + 1}, got {seq}")
            # Never silently continue. The caller must resynchronise.
            raise BookIntegrityError(
                f"{self.market_ticker}: {self.integrity_reason}")

        side = str(msg.get("side") or "").lower()
        if side not in (SIDE_YES, SIDE_NO):
            raise FixedPointError(f"{self.market_ticker}: unknown side {side!r}")
        if "price_dollars" not in msg or "delta_fp" not in msg:
            # Direct subscription raised KeyError, which replay did not catch,
            # so one malformed record made the whole archive unreplayable.
            raise FixedPointError(
                f"{self.market_ticker}: delta is missing 'price_dollars' or "
                "'delta_fp'")
        price = parse_price_units(msg["price_dollars"], field="price_dollars")
        self.grid.validate(price, field="price_dollars")
        # A delta MAY be negative: that is how a level is decremented.
        change = parse_contract_units(msg["delta_fp"], field="delta_fp",
                                      allow_negative=True)

        ladder = self.yes if side == SIDE_YES else self.no
        current = ladder.get(price, 0)
        updated = current + change
        if updated < 0:
            self._halt(
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
        """Digest of the book's full state.

        Gated like every other derived view: it was the one that was not, so a
        halted book still produced a confident digest, and `replay()` emitted
        it as the acceptance test for the whole data path.

        Generation, sequence and sid are included because two books with the
        same ladders at different positions are not the same observation, and
        checksum equality is exactly what replay determinism is asserted on.
        """
        self._require_publishable()
        payload = {
            "market_ticker": self.market_ticker,
            "generation": self.generation,
            "last_seq": self.last_seq,
            "sid": self.sid,
            "yes": {str(k): v for k, v in sorted(self.yes.items())},
            "no": {str(k): v for k, v in sorted(self.no.items())},
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")).hexdigest()[:16]

        payload = {
            "market_ticker": self.market_ticker,
            "yes": sorted(self.yes.items()), "no": sorted(self.no.items()),
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
