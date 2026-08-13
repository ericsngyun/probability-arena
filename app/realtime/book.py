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

from app.realtime.canonical import canonical_datetime
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
    # INTEGER microseconds, not float milliseconds. A float is not
    # canonically representable — writing one bare and re-reading it as
    # Decimal re-serialises differently, which is what made every record
    # carrying a venue timestamp fail its own digest and vanish on read.
    data_age_us: int | None            # venue_time -> receive, when derivable
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
    # `ts_ms` FIRST. The venue's stamping is not uniform across channels —
    # confirmed on the DEMO wire 2026-08-08:
    #   orderbook_delta : ts = "2026-08-08T00:49:08.065758Z"  (ISO string)
    #                     ts_ms = 1786150148065
    #   ticker          : ts = 1786150148  (epoch SECONDS)
    #                     ts_ms = 1786150148065, time = ISO string
    # So `ts` alone means different things on different channels, and the old
    # `int(ts)` silently produced a 1970 date for the ISO form and a
    # 1000x-inflated age for the seconds form. `ts_ms` is unambiguous wherever
    # it appears; the ISO fields are the documented fallback.
    venue_iso = None
    dt = None
    ts_ms = msg.get("ts_ms")
    if isinstance(ts_ms, int) and not isinstance(ts_ms, bool):
        try:
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            dt = None
    if dt is None:
        for key in ("time", "ts", "timestamp"):
            raw = msg.get(key)
            if isinstance(raw, str):
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    dt = None
            elif isinstance(raw, int) and not isinstance(raw, bool):
                try:
                    # Epoch seconds, per the `ticker` channel.
                    dt = datetime.fromtimestamp(raw, tz=timezone.utc)
                    break
                except (ValueError, OSError, OverflowError):
                    dt = None
    age_us = None
    if dt is not None:
        venue_iso = canonical_datetime(dt)
        # Integer microseconds throughout. Negative values are retained: they
        # are clock-offset evidence, and truncating them at zero would bias the
        # distribution optimistically.
        age_us = round((receive_time - dt).total_seconds() * 1_000_000)
    now = utcnow()
    return EventEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION, venue=venue,
        environment=environment, channel=channel,
        event_type=str(message.get("type") or "unknown"),
        market_ticker=msg.get("market_ticker"), market_id=msg.get("market_id"),
        sid=message.get("sid"), seq=message.get("seq"),
        venue_time=venue_iso,
        collector_receive_time=canonical_datetime(receive_time),
        normalization_time=canonical_datetime(now),
        receive_monotonic_ns=receive_mono,
        normalize_monotonic_ns=monotonic_ns(),
        data_age_us=age_us,
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
    The economic reading implemented here is:

        yes-side level at p  -> a resting BID for YES at p
        no-side level at p   -> a resting OFFER of YES at p   (already YES-scaled)

    **`use_yes_price=true` semantics, confirmed on the DEMO wire 2026-08-08.**
    Both ladders arrive on the YES price scale; the NO-side price IS the YES
    ask and **no complement is applied**. Ground truth from a `ticker` frame
    for KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1:

        ticker : yes_bid 0.4700 size 5.00 | yes_ask 0.5100 size 206.00
        book   : yes_dollars_fp [["0.4700","5.00"]]
                 no_dollars_fp  [["0.5100","206.00"]]   (5.00 + a +201.00 delta)

    This code previously complemented the NO ladder (`1 - 0.5100 = 0.4900`) and
    would have reported an ask two cents below the real one — uncrossed, plausible,
    and wrong, which is exactly the failure mode the reviews predicted for this
    flag. The complement is correct only when `use_yes_price` is NOT set, and it
    is always set here.

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
            # An EMPTY book legitimately omits both ladder keys — confirmed on
            # the DEMO wire 2026-08-08, seq 9:
            #   {"market_ticker": "...", "market_id": "..."}
            # arriving after deltas had removed every level. Requiring a ladder
            # key here rejected a valid snapshot, so the guard is now that the
            # message must at least identify its market.
            if not msg.get("market_ticker") and ticker is None:
                raise BookIntegrityError(
                    f"{self.market_ticker}: snapshot identifies no market")
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
        ask = min(no)          # already YES-scaled under use_yes_price=true
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
                    sid: int | None = None,
                    ordered_externally: bool = False) -> dict:
        """Apply one incremental change, refusing to continue across a gap.

        `ordered_externally` is set by `SubscriptionRouter`, which has already
        settled ordering for the whole subscription. A book must not re-check a
        per-market sequence in that case: `seq` counts messages across every
        market on the subscription, so a book's own view of it is full of holes
        that are simply its siblings' traffic.

        Fails closed on every rejection path, not only on sequence faults.
        """
        try:
            return self._apply_delta(msg, seq=seq, sid=sid,
                                     ordered_externally=ordered_externally)
        except BookIntegrityError:
            raise                       # already halted at the point of refusal
        except Exception as exc:
            self._halt(f"delta rejected: {type(exc).__name__}: {exc}")
            raise

    def _apply_delta(self, msg: dict, *, seq: int | None = None,
                     sid: int | None = None,
                     ordered_externally: bool = False) -> dict:
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

        status = SEQ_OK if ordered_externally else self.classify_seq(seq)
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

        Under `use_yes_price=true` the NO ladder is already YES-scaled, so the
        best YES offer is simply its *lowest* price. Derived from the NO side,
        never assumed to exist on the YES one.
        """
        self._require_publishable()
        if not self.no:
            return None
        return min(self.no)

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
                format_contract_units(self.no[ask])
                if ask is not None else None),
            "spread_units": (ask - bid) if (bid is not None and ask is not None)
                            else None,
            "yes_levels": len(self.yes), "no_levels": len(self.no),
        }

    def yes_scale_ladder(self) -> dict:
        """Both ladders on one YES scale, with the venue's own words preserved.

        Every level carries all four canonical fields:

            venue_side                what the venue called the side
            raw_price_string          the exact characters it sent
            raw_price_units           those characters as integer units
            normalized_yes_price_units    our YES-scale interpretation

        Normalization ADDS a reading; it never replaces the venue's. Keeping
        the raw pair means that if the `use_yes_price` convention turns out to
        differ from what is assumed here, every archived level can be
        reinterpreted after the fact instead of having to be re-collected —
        which matters precisely because that convention is the one thing this
        milestone could not verify without the demo socket.
        """
        self._require_publishable()
        bids = [{"venue_side": SIDE_YES,
                 "raw_price_string": format_price_units(p),
                 "raw_price_units": p,
                 "normalized_yes_price_units": p,
                 "price_units": p, "price": format_price_units(p),
                 "size_units": q, "raw_side": SIDE_YES,
                 "interpretation": "resting bid for YES"}
                for p, q in sorted(self.yes.items(), reverse=True)]
        asks = [{"venue_side": SIDE_NO,
                 "raw_price_string": format_price_units(p),
                 "raw_price_units": p,
                 "normalized_yes_price_units": p,
                 "price_units": p,
                 "price": format_price_units(p),
                 "size_units": q, "raw_side": SIDE_NO,
                 "interpretation": "resting economic offer of YES"}
                for p, q in sorted(self.no.items(), reverse=True)]
        asks.sort(key=lambda level: level["price_units"])
        return {"market_ticker": self.market_ticker,
                "generation": self.generation,
                # The convention this ladder assumes, recorded alongside the
                # data rather than only in a docstring.
                "use_yes_price_requested": self.use_yes_price,
                "no_side_normalization": "identity_yes_scaled",
                "bids": bids, "asks": asks}

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


# --- subscription-level sequencing ------------------------------------------------

SUB_HEALTHY = "healthy"
SUB_GAP = "sequence_gap"
SUB_REGRESSION = "sequence_regression"
SUB_WRONG_SID = "wrong_sid"
SUB_STALE_GENERATION = "stale_generation"
SUB_MISSING_SEQ = "missing_sequence"
SUB_AWAITING_SNAPSHOT = "awaiting_snapshot"


class SubscriptionError(RuntimeError):
    """A message that cannot be ordered within its subscription."""


class SubscriptionState:
    """Sequence integrity for ONE subscription, across ALL its markets.

    Kalshi assigns `seq` per **subscription**, not per market, and one
    subscription carries many `market_tickers`. Tracking `last_seq` on each
    market book therefore made every book see a subsequence with a hole at
    every message belonging to a sibling ticker: with two markets, each book
    halted on its second message and never recovered.

    So ordering lives here and routing lives below it. A book never compares
    sequence numbers at all — it applies what this object has already accepted.

    The other half of the correction is the failure mode. A `seq` hole means a
    message was lost, and nothing in the hole tells us which market it belonged
    to, so **every** book on this subscription is suspect. Repairing only the
    market named in the next message would leave the others silently wrong.
    """

    def __init__(self, sid: int, *, market_tickers=(), generation: int = 1):
        self.sid = int(sid)
        self.generation = int(generation)
        self.last_seq: int | None = None
        self.subscribed_market_tickers = tuple(market_tickers)
        self.healthy = False            # nothing is ordered until a snapshot
        self.state_reason: str | None = SUB_AWAITING_SNAPSHOT
        self.stats = {"accepted": 0, "duplicates": 0, "gaps": 0,
                      "regressions": 0, "wrong_sid": 0, "stale_generation": 0,
                      "missing_seq": 0, "recoveries": 0}

    # -- lifecycle -------------------------------------------------------------
    def _fail(self, reason: str, detail: str) -> None:
        self.healthy = False
        self.state_reason = reason
        raise SubscriptionError(f"sid {self.sid}: {detail}")

    def accept(self, *, sid, seq, generation=None, is_snapshot: bool = False) -> str:
        """Validate one message against this subscription. Ordering only.

        Order of checks matters: identity, then generation, then sequence. A
        message from a superseded generation carries a sequence from a
        different stream, so comparing it first would produce a meaningless
        verdict about a message we were going to discard anyway.
        """
        if sid is None or int(sid) != self.sid:
            self.stats["wrong_sid"] += 1
            self._fail(SUB_WRONG_SID,
                       f"message belongs to subscription {sid}, not {self.sid}")
        if generation is not None and int(generation) != self.generation:
            self.stats["stale_generation"] += 1
            self._fail(SUB_STALE_GENERATION,
                       f"message is from generation {generation}, this "
                       f"subscription is on {self.generation}")
        if seq is None:
            self.stats["missing_seq"] += 1
            self._fail(SUB_MISSING_SEQ,
                       "message carries no sequence number; absent is not ordered")
        seq = int(seq)

        if is_snapshot:
            # A snapshot re-bases the stream. It may not re-base it BACKWARDS:
            # at-least-once redelivery on reconnect otherwise discards applied
            # deltas and reports a clean, publishable result.
            if self.last_seq is not None and seq < self.last_seq:
                self._fail(SUB_REGRESSION,
                           f"snapshot seq {seq} is behind the applied position "
                           f"{self.last_seq}; refusing to rewind")
            self.last_seq = seq
            self.healthy = True
            self.state_reason = None
            self.stats["accepted"] += 1
            return SEQ_OK

        if not self.healthy:
            self._fail(self.state_reason or SUB_AWAITING_SNAPSHOT,
                       "delta received while the subscription is not healthy; "
                       "rejected rather than buffered")
        if self.last_seq is None:
            self._fail(SUB_AWAITING_SNAPSHOT,
                       "delta received before any snapshot on this subscription")
        if seq == self.last_seq:
            self.stats["duplicates"] += 1
            return SEQ_DUPLICATE
        if seq < self.last_seq:
            self.stats["regressions"] += 1
            self._fail(SUB_REGRESSION,
                       f"sequence regression: expected {self.last_seq + 1}, "
                       f"got {seq}")
        if seq > self.last_seq + 1:
            self.stats["gaps"] += 1
            self._fail(SUB_GAP,
                       f"sequence gap: expected {self.last_seq + 1}, got {seq}")
        self.last_seq = seq
        self.stats["accepted"] += 1
        return SEQ_OK

    def begin_recovery(self) -> None:
        """Mark the subscription as awaiting a fresh snapshot."""
        self.healthy = False
        self.state_reason = SUB_AWAITING_SNAPSHOT
        self.last_seq = None
        self.stats["recoveries"] += 1

    def supersede(self, *, market_tickers=None) -> int:
        """Start a new generation, e.g. after a full resubscription.

        The generation is what makes a straggler from the old stream
        identifiable: its sequence numbers are from a different namespace, and
        without this they would look like an ordinary gap.
        """
        self.generation += 1
        if market_tickers is not None:
            self.subscribed_market_tickers = tuple(market_tickers)
        self.begin_recovery()
        return self.generation


class SubscriptionRouter:
    """Dispatch: one subscription, many market books.

    Sequence integrity is settled once, at the subscription level, before any
    routing happens. This is the object that makes interleaved A/B/A traffic
    ordinary rather than a false gap on A.
    """

    def __init__(self, subscription: SubscriptionState, *, grid=None):
        self.subscription = subscription
        self.grid = grid
        self.books: dict[str, OrderBook] = {}

    def book_for(self, ticker: str) -> "OrderBook":
        book = self.books.get(ticker)
        if book is None:
            book = self.books[ticker] = OrderBook(ticker, grid=self.grid)
        return book

    def _unpublish_all(self, reason: str) -> None:
        for book in self.books.values():
            book._halt(f"subscription {self.subscription.sid}: {reason}")

    def dispatch(self, record: dict) -> dict:
        """Apply one archived/live envelope. Ordering, then routing, then apply.

        A subscription-level failure unpublishes EVERY book on this
        subscription, not just the one the failing message happened to name.
        The lost message could have belonged to any of them.
        """
        etype = record.get("event_type")
        if etype not in ("orderbook_snapshot", "orderbook_delta"):
            # NON-ORDERBOOK FRAMES STILL CONSUME A SEQUENCE NUMBER. Confirmed on
            # the DEMO wire 2026-08-08: an `error` frame arrived as
            #   {"type":"error","sid":4,"seq":4,...}
            # between deltas at seq 3 and seq 5. Skipping it without advancing
            # the position made the next delta look like a gap, which would have
            # unpublished every book on the subscription within seconds of
            # connecting. Ordering is a property of the SUBSCRIPTION, so it has
            # to account for everything the subscription carries — not just the
            # frames we happen to route.
            if record.get("seq") is not None:
                try:
                    self.subscription.accept(
                        sid=record.get("sid"), seq=record.get("seq"),
                        generation=record.get("subscription_generation"))
                except SubscriptionError:
                    self._unpublish_all(
                        self.subscription.state_reason or "sequence fault")
                    raise
            return {"action": "ignored", "event_type": etype}
        msg = (record.get("raw") or {}).get("msg") or {}
        ticker = record.get("market_ticker") or msg.get("market_ticker")
        is_snapshot = etype == "orderbook_snapshot"

        try:
            status = self.subscription.accept(
                sid=record.get("sid"), seq=record.get("seq"),
                generation=record.get("subscription_generation"),
                is_snapshot=is_snapshot)
        except SubscriptionError as exc:
            self._unpublish_all(self.subscription.state_reason or str(exc))
            raise
        if status == SEQ_DUPLICATE:
            return {"action": "duplicate_ignored", "seq": record.get("seq")}

        if not ticker:
            self._unpublish_all("message carried no market_ticker")
            raise SubscriptionError(
                f"sid {self.subscription.sid}: message at seq "
                f"{record.get('seq')} carries no market_ticker and cannot be "
                "routed; which book it belonged to is unknowable")
        if (self.subscription.subscribed_market_tickers
                and ticker not in self.subscription.subscribed_market_tickers):
            self._unpublish_all(f"unexpected market {ticker!r}")
            raise SubscriptionError(
                f"sid {self.subscription.sid}: message names {ticker!r}, which "
                "this subscription did not subscribe to")

        book = self.book_for(ticker)
        if is_snapshot:
            return book.apply_snapshot(msg, sid=self.subscription.sid,
                                       seq=record.get("seq"))
        return book.apply_delta(msg, seq=record.get("seq"),
                                sid=self.subscription.sid,
                                ordered_externally=True)

    def publishable_books(self) -> dict:
        healthy = self.subscription.healthy
        return {t: (b.publishable and healthy) for t, b in sorted(self.books.items())}
