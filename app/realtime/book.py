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

Its per-market half, earned the same way (KALSHI-REPLAY-GENERATION-CONSISTENCY,
CP7 live evidence): *within a subscription generation, a market is not
publishable until THAT MARKET has received its own snapshot for that
generation*. A sibling's snapshot re-bases the sibling and says nothing about
anyone else's ladder.
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

# --- capture-time epochs ----------------------------------------------------------
#
# KALSHI-TAPE-GENERATION. Two monotonic epochs are stamped onto every envelope
# AT CAPTURE TIME, because neither can be recovered afterwards from the tape:
#
#   connection_generation    advances when a socket is established
#   subscription_generation  advances when a new SUCCESSFUL subscription
#                            generation begins — not on every connect attempt,
#                            not on every frame
#
# Only `subscription_generation` has ordering semantics. The venue's `seq`
# restarts with each new subscription, so sequence continuity is only meaningful
# WITHIN one generation; a generation boundary EXPLAINS a discontinuity instead
# of being a fault. Without the field, a routine reconnect and real data loss
# are the same observation — and since a sequence hole is the only drop detector
# this system has (CP0: the websockets library never silently drops and exposes
# no drop counter), that ambiguity is not recoverable later. The epoch is never
# inferred at read time for exactly that reason.
#
# THE SENTINEL. `None` — never 0 — means "generation unknown". A record written
# before these fields existed (`normalized_event.schema_version == 1`) carries
# neither key, so `.get()` yields `None`, and every reader below treats that as
# unknown: the generation check is skipped, ordering falls back to pure `seq`
# continuity, and nothing pretends the record was generation 0. 0 would be a
# fabricated epoch, and a fabricated epoch would let an old record silently
# supersede a live one.
GENERATION_UNKNOWN = None


def coerce_generation(value):
    """One place that decides what a generation field MEANS.

    Absent/`None` is the documented unknown sentinel. A real generation is a
    non-negative `int`; `bool` is excluded because `True == 1` would make a
    corrupt record read as generation 1. Anything else is a malformed record
    and raises, rather than being silently rounded into a plausible epoch.
    """
    if value is GENERATION_UNKNOWN:
        return GENERATION_UNKNOWN
    if isinstance(value, bool) or not isinstance(value, int):
        raise SubscriptionError(
            f"generation {value!r} is neither an int nor the documented "
            "unknown sentinel (absent/None); a record whose epoch cannot be "
            "read is refused rather than assigned a plausible one")
    if value < 0:
        raise SubscriptionError(
            f"generation {value!r} is negative; epochs are monotonic from 0")
    return value


def subscription_generation_of(record: dict):
    """The subscription epoch a stored/live record was captured under.

    Returns `GENERATION_UNKNOWN` for pre-KALSHI-TAPE-GENERATION records, which
    is exactly how those records must read: unknown, not zero.
    """
    return coerce_generation(record.get("subscription_generation"))


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
    # CAPTURE-TIME EPOCHS. Defaulted to `GENERATION_UNKNOWN` rather than 0 so an
    # envelope built by something that genuinely has no epoch (a fixture, a
    # legacy import) says "unknown" instead of claiming the first generation.
    # `archive.py` writes both into the record's pinned columns; before this
    # existed it read `raw.get("connection_id")` and
    # `raw.get("subscription_generation")` off an envelope that defined
    # NEITHER, so both columns were permanently null and `dispatch` read that
    # null straight back as "no generation information".
    connection_generation: int | None = GENERATION_UNKNOWN
    subscription_generation: int | None = GENERATION_UNKNOWN
    raw: dict = field(default_factory=dict)
    normalized: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


IMPLEMENTATION_VERSION = "kalshi-realtime-observer/001A"
# 1 -> 2 (KALSHI-TAPE-GENERATION): the envelope gained `connection_generation`
# and `subscription_generation`. Bumped DELIBERATELY, because the version is
# what makes the sentinel legible: in a v1 record the absence of
# `subscription_generation` means "written before the field existed, generation
# unknown", and in a v2 record it means the writer explicitly had no epoch. A
# reader that could not tell those apart would have to guess, which is the
# after-the-fact reconstruction this tape exists to make unnecessary. The
# durable RECORD schema (`segment.RECORD_FIELDS`) is untouched: it has always
# had both columns, and this change only stops writing null into them.
ENVELOPE_SCHEMA_VERSION = 2


def make_envelope(*, venue: str, environment: str, channel: str, message: dict,
                  receive_time: datetime, receive_mono: int,
                  normalized: dict | None = None,
                  connection_generation=GENERATION_UNKNOWN,
                  subscription_generation=GENERATION_UNKNOWN) -> EventEnvelope:
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
        # Validated here, at capture, so a nonsense epoch can never reach the
        # durable tape and be indistinguishable from a real one later.
        connection_generation=coerce_generation(connection_generation),
        subscription_generation=coerce_generation(subscription_generation),
        raw=message, normalized=normalized or {},
    )


# --- sequence integrity ---------------------------------------------------------

SEQ_OK = "ok"
SEQ_DUPLICATE = "duplicate"
SEQ_GAP = "gap"
SEQ_REGRESSION = "regression"
SEQ_MISSING = "missing_sequence"

# --- ladder presence --------------------------------------------------------------
#
# "The venue sent an empty ladder" and "the venue sent no ladder key at all" are
# DIFFERENT OBSERVATIONS and are never merged. Measured on the DEMO wire
# 2026-08-17 (`docs/experiments/KALSHI-COLLECTOR-P0-FIXES-RUNS/`): of 60
# `orderbook_snapshot` frames, 57 carried non-empty ladders — up to eight YES
# levels and seven NO levels — and 3 carried NEITHER key. So the venue does send
# ladders, and it omits the key for a side holding nothing, which this file
# already recorded as legitimate from a 2026-08-08 capture (`apply_snapshot`).
#
# What was missing is any way for a reader to tell "observed, and empty" apart
# from "never transmitted". Both produce a book with zero levels; only one of
# them is an observation, and reading the other as depth is how a microstructure
# statistic acquires liquidity that never existed.
LADDER_SUPPLIED = "supplied"
LADDER_OMITTED = "omitted_by_venue"
LADDER_UNOBSERVED = "no_snapshot_applied"

# --- publication state ------------------------------------------------------------
#
# KALSHI-REPLAY-GENERATION-CONSISTENCY-001. Publishability is a TYPED state, not a
# bare boolean, because three different situations produce "False" and a consumer
# that cannot tell them apart cannot act on any of them:
#
#   PUB_BOOK_HALTED                    integrity is broken — a gap, a rejection,
#                                      a crossed book. Something is WRONG.
#   PUB_AWAITING_GENERATION_SNAPSHOT   nothing is wrong. The subscription moved to
#                                      a new generation and THIS market has not yet
#                                      received its own snapshot for it, so the
#                                      ladder it holds belongs to an abandoned
#                                      epoch. Not an error; not "based and empty".
#   PUB_SUBSCRIPTION_UNHEALTHY         the subscription itself is awaiting a base.
#
# Doctrine 10: "not yet based for this generation" travels with its own name and
# with BOTH generation numbers, so a reader never has to infer why a book went
# quiet — and can never read a zero-level book as observed emptiness when it is
# really an un-re-snapshotted book from the previous epoch.
PUB_PUBLISHABLE = "publishable"
PUB_BOOK_HALTED = "book_halted"
PUB_AWAITING_GENERATION_SNAPSHOT = "awaiting_snapshot_for_generation"
PUB_SUBSCRIPTION_UNHEALTHY = "subscription_unhealthy"


@dataclass(frozen=True)
class PublicationState:
    """Why one market is, or is not, publishable right now.

    `publishable` is the answer; `state` is the reason in the closed vocabulary
    above; `reason` is the human detail. `subscription_generation` and
    `based_generation` are carried on every instance — equal means this market
    holds a base for the epoch the subscription is in, and that identity IS the
    invariant this milestone enforces.
    """
    publishable: bool
    state: str
    reason: str | None
    subscription_generation: int | None
    based_generation: int | None

    def to_dict(self) -> dict:
        return asdict(self)


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
        # The SUBSCRIPTION epoch this book's position belongs to — a different
        # thing from `self.generation`, which counts this book's own
        # resynchronisations. Unknown until a router tells it one.
        self.subscription_generation = GENERATION_UNKNOWN
        # The subscription epoch whose OWN snapshot based this book. The pair
        # (`subscription_generation`, `based_generation`) is the whole of
        # KALSHI-REPLAY-GENERATION-CONSISTENCY-001: when they differ, the
        # subscription has moved to a new epoch and THIS market has not yet been
        # re-snapshotted, so its ladder is pre-boundary state and must not be
        # served as current no matter what its siblings did.
        self.based_generation = GENERATION_UNKNOWN
        self.synced = False
        self.integrity_reason: str | None = "awaiting snapshot"
        self.snapshot_receive_time: datetime | None = None
        # Which ladders the LAST APPLIED SNAPSHOT actually carried. Typed, and
        # carried on every derived view, so a zero-level side is never read as
        # observed emptiness when the venue simply did not transmit it.
        self.ladder_presence: dict = {SIDE_YES: LADDER_UNOBSERVED,
                                      SIDE_NO: LADDER_UNOBSERVED}
        self.stats = {"snapshots": 0, "deltas": 0, "duplicates": 0, "gaps": 0,
                      "regressions": 0, "resyncs": 0, "rejected_pre_snapshot": 0,
                      # Deltas from a NEW generation refused because this
                      # market has not been re-snapshotted into it. Counted on
                      # its own and never merged into `gaps` or
                      # `rejected_pre_snapshot`: it is neither loss nor a cold
                      # start, and merging it would make the boundary
                      # indistinguishable from both.
                      "rejected_pre_generation_snapshot": 0,
                      # A reconnect is counted, never merged into `gaps`: they
                      # are different phenomena and the whole point of the
                      # epoch is that they stay distinguishable.
                      "generation_boundaries": 0}

    # -- integrity ---------------------------------------------------------------

    @property
    def based_for_current_generation(self) -> bool:
        """Has THIS market received its own snapshot for the epoch it is in?

        `GENERATION_UNKNOWN == GENERATION_UNKNOWN` is True by design: a book
        nobody ever told an epoch to (a direct caller, a legacy tape) is not
        put into a permanent awaiting state by a check about epochs it does not
        participate in. The moment a router names an epoch, the comparison
        becomes real.
        """
        return self.based_generation == self.subscription_generation

    @property
    def publication_state(self) -> PublicationState:
        """The typed answer, with both epochs attached. See `PublicationState`.

        Order matters. A halted book is halted whatever its epoch — reporting
        it as merely "awaiting the new generation" would hide an integrity
        fault behind a benign word, which is this repository's recurring
        failure class rather than a hypothetical one.
        """
        if not self.synced or self.integrity_reason is not None:
            return PublicationState(
                False, PUB_BOOK_HALTED,
                self.integrity_reason or "book is not synced",
                self.subscription_generation, self.based_generation)
        if not self.based_for_current_generation:
            return PublicationState(
                False, PUB_AWAITING_GENERATION_SNAPSHOT,
                (f"{self.market_ticker}: based in subscription generation "
                 f"{self.based_generation!r}, the subscription is on "
                 f"{self.subscription_generation!r}; this market has not "
                 "received its own snapshot for the current generation and "
                 "its ladder belongs to the epoch the venue abandoned"),
                self.subscription_generation, self.based_generation)
        return PublicationState(True, PUB_PUBLISHABLE, None,
                                self.subscription_generation,
                                self.based_generation)

    @property
    def publishable(self) -> bool:
        """Publishable only when synced, sequence-clean AND based in this epoch.

        The third clause is KALSHI-REPLAY-GENERATION-CONSISTENCY-001. It is a
        property of THIS market: a sibling's snapshot re-bases the sibling, and
        says nothing whatsoever about the ladder held here.
        """
        return self.publication_state.publishable

    def _require_publishable(self) -> None:
        state = self.publication_state
        if not state.publishable:
            raise BookIntegrityError(
                f"{self.market_ticker}: book is not publishable "
                f"[{state.state}] ({state.reason})")

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
            # PRESENCE IS READ BEFORE THE VALUES, and `None` counts as absent
            # for the reason `_first_field` gives: a JSON null is the venue
            # declining to answer, not an answer. `or []` below still guards the
            # parse; what it no longer does is erase the distinction.
            raw_yes = msg.get("yes_dollars_fp")
            raw_no = msg.get("no_dollars_fp")
            presence = {
                SIDE_YES: LADDER_SUPPLIED if raw_yes is not None else LADDER_OMITTED,
                SIDE_NO: LADDER_SUPPLIED if raw_no is not None else LADDER_OMITTED,
            }
            yes_levels = self._parse_levels(raw_yes or [], "yes")
            no_levels = self._parse_levels(raw_no or [], "no")
            yes = {p: q for p, q in yes_levels if q > 0}
            no = {p: q for p, q in no_levels if q > 0}
            self._require_uncrossed(yes, no)
        except Exception as exc:
            self._halt(f"snapshot rejected: {type(exc).__name__}: {exc}")
            raise
        self.ladder_presence = presence
        self.yes, self.no = yes, no
        if sid is not None and self.sid is not None and sid != self.sid:
            self.stats["resyncs"] += 1
        self.sid = sid if sid is not None else self.sid
        self.last_seq = seq
        # THIS market is now based for the epoch it is positioned in. The
        # router has already moved `subscription_generation` forward
        # (`_rebase_all` runs between accepting a frame and applying it), so
        # taking the value from the book rather than from a parameter keeps one
        # source of truth for the epoch instead of two that must agree.
        self.based_generation = self.subscription_generation
        self.generation += 1
        self.synced = seq is not None
        self.integrity_reason = None if seq is not None else (
            "snapshot carried no sequence number; ordering cannot be verified")
        self.snapshot_receive_time = receive_time or utcnow()
        self.stats["snapshots"] += 1
        return {"action": "snapshot", "generation": self.generation,
                "yes_levels": len(self.yes), "no_levels": len(self.no),
                # On the RESULT too, not only on the object: the dispatch
                # return value is what a caller reads to decide whether a
                # snapshot told it anything, and `yes_levels: 0` alone cannot
                # answer that.
                "ladder_presence": dict(self.ladder_presence)}

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

        if not self.based_for_current_generation:
            # KALSHI-REPLAY-GENERATION-CONSISTENCY-001, the serious half. A
            # delta from generation g applied on top of generation g-1's ladder
            # does not merely serve stale depth — it FABRICATES a book that
            # never existed at any instant, and one that a later snapshot would
            # overwrite without leaving a trace that it ever happened.
            #
            # Deliberately NOT `_halt`: nothing is broken. The book is already
            # non-publishable through its typed state, the refusal is counted
            # on its own axis, and the market recovers the moment ITS OWN
            # snapshot for this generation arrives. Halting here would file a
            # routine reconnect as an integrity fault, which is exactly the
            # conflation `_rebase_all` exists to prevent.
            #
            # It runs AFTER the identity checks on purpose: "this message
            # belongs to another market/subscription" is the stronger statement
            # and must keep its halt, or a mislabelled frame arriving during a
            # boundary would be filed as a benign wait.
            self.stats["rejected_pre_generation_snapshot"] += 1
            raise BookIntegrityError(
                f"{self.market_ticker}: delta belongs to subscription "
                f"generation {self.subscription_generation!r}; this book was "
                f"last based in {self.based_generation!r} and has not received "
                "its own snapshot for the current generation — rejected rather "
                "than applied to an abandoned ladder")

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

    def begin_subscription_generation(self, generation) -> None:
        """A NEW subscription generation began. Rebase, do not halt.

        Clears the per-book sequence POSITION and nothing else — no ladder is
        touched and the book is NOT HALTED, because a reconnect is a legitimate
        discontinuity rather than evidence of loss.

        It does, however, stop being PUBLISHABLE, and that is
        KALSHI-REPLAY-GENERATION-CONSISTENCY-001: `based_generation` stays where
        it was, so until this market's own snapshot for the new epoch arrives,
        `publication_state` reads `awaiting_snapshot_for_generation` — a named,
        benign state, distinct from every integrity fault. Before this, the
        book kept `synced=True` with no integrity reason and was republished
        the instant any SIBLING market re-snapshotted; measured live on
        2026-08-17, that republished 59 of 60 markets on ladders from the epoch
        the venue had just abandoned (CP7).

        What must go is the position: after a reconnect the venue's `seq`
        restarts, so
        `apply_snapshot`'s anti-rewind guard would compare two numbers from
        different namespaces and refuse the very snapshot that re-bases the
        book. That guard stays fully armed WITHIN a generation, which is where
        at-least-once redelivery can actually rewind applied state.

        Only a genuine, strictly-increasing epoch stamped at capture time
        reaches here (`SubscriptionState.accept`), and the tape's digest chain
        is what makes that stamp trustworthy on replay.
        """
        self.subscription_generation = coerce_generation(generation)
        self.last_seq = None
        self.stats["generation_boundaries"] += 1

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
            # Beside every level count, always. `yes_levels: 0` with
            # `ladder_presence.yes == "omitted_by_venue"` is a book with no
            # observation on that side; the same zero with `"supplied"` is a
            # side the venue said was empty. A consumer that treats those the
            # same is inventing depth information it does not have.
            "ladder_presence": dict(self.ladder_presence),
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
                "ladder_presence": dict(self.ladder_presence),
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

    **ONE sid IS ONE CHANNEL, and only the orderbook channel needs a base.**
    Measured on the DEMO wire 2026-08-17: a single `subscribe` naming three
    channels produced three acks, `sid 1 = orderbook_delta`, `sid 2 = ticker`,
    `sid 3 = trade`, each carrying its own `seq` namespace. `healthy` means "a
    snapshot has based this book" — a requirement that exists so a DELTA is
    never applied to a book that has no base, and which is meaningless for a
    stream of trade prints. Applying it anyway is what put the collector in a
    permanent fault state: the trade sid never receives an `orderbook_snapshot`,
    so it could never become healthy, so all 218 of its frames faulted while its
    sequence ran 1..219 with **zero** gaps, duplicates or regressions.

    So ordering and basing are separated. `needs_base=False` validates sequence
    CONTINUITY only, which is the whole of what a non-orderbook stream can be
    wrong about, and the drop detector stays fully armed on both.
    """

    def __init__(self, sid: int, *, market_tickers=(), generation: int = 1):
        self.sid = int(sid)
        self.generation = int(generation)
        self.last_seq: int | None = None
        self.subscribed_market_tickers = tuple(market_tickers)
        self.healthy = False            # nothing is ordered until a snapshot
        # Whether this subscription has ever carried an orderbook frame.
        # OBSERVED, never assumed: it is what decides whether `get_snapshot` is
        # a recovery this subscription can even accept (the venue answers
        # `{"code":13,"msg":"Unsupported action"}` when it is not).
        self.carries_orderbook = False
        self.state_reason: str | None = SUB_AWAITING_SNAPSHOT
        self.stats = {"accepted": 0, "duplicates": 0, "gaps": 0,
                      "regressions": 0, "wrong_sid": 0, "stale_generation": 0,
                      "missing_seq": 0, "recoveries": 0,
                      # Reconnects, counted separately from every fault
                      # counter beside them.
                      "generation_advances": 0}

    # -- lifecycle -------------------------------------------------------------
    def _fail(self, reason: str, detail: str) -> None:
        self.healthy = False
        self.state_reason = reason
        raise SubscriptionError(f"sid {self.sid}: {detail}")

    def _reanchor_if_unbased(self, seq: int, *, needs_base: bool) -> None:
        """After a discontinuity on an unbased stream, move to the new position.

        The fault has already been COUNTED when this runs; what it prevents is
        counting it forever. An orderbook subscription recovers by requesting a
        snapshot, and `begin_recovery()` clears the position for it. A trade or
        ticker stream has no such repair — a lost print is lost — so if the
        anchor stayed where it was, every later frame would compare against a
        position the venue has moved past and fault too. The counter would then
        measure TIME SINCE the discontinuity rather than the NUMBER of them,
        which is the permanently-pinned-counter defect this fix exists to
        remove, reintroduced one level down.

        Untouched when `needs_base` is set: an orderbook stream must not quietly
        adopt a position it never based.
        """
        if not needs_base:
            self.last_seq = seq

    def note_orderbook_frame(self) -> None:
        """This subscription carries the orderbook channel — observed, not assumed.

        Set by the router on the first `orderbook_snapshot`/`orderbook_delta`
        routed through it, which on the wire is the very first frame the
        orderbook sid delivers. It is the only thing that makes a `get_snapshot`
        recovery applicable, and it is deliberately derived from FRAMES rather
        than from the subscribe command: the live lane and the replay lane see
        the same frames, so both reach the same verdict, whereas only the live
        lane ever saw the command. (The `subscribed` ack does name the channel,
        but it carries no top-level `sid` — the sid is inside `msg` — so it is
        not routable to a subscription and cannot serve as the source here.)
        """
        self.carries_orderbook = True

    def accept(self, *, sid, seq, generation=None, is_snapshot: bool = False,
               needs_base: bool = True) -> str:
        """Validate one message against this subscription. Ordering only.

        Order of checks matters: identity, then generation, then sequence. A
        message from a superseded generation carries a sequence from a
        different stream, so comparing it first would produce a meaningless
        verdict about a message we were going to discard anyway.

        **Sequence continuity is evaluated WITHIN a generation.** The three
        cases are different phenomena and are kept apart:

        * `generation < self.generation` — a straggler from a stream we have
          already left. Still a fault: its `seq` is from a dead namespace.
        * `generation > self.generation` — a RECONNECT. The venue's `seq`
          restarts, so the boundary EXPLAINS the discontinuity instead of
          being one. Not a fault, and it HALTS nothing. It does take every
          book out of publishable state until that book's own snapshot for the
          new epoch arrives — see `OrderBook.begin_subscription_generation`.
        * `GENERATION_UNKNOWN` — a record written before the epoch existed.
          The check is skipped and ordering falls back to pure `seq`
          continuity, exactly as it behaved before the field.

        The one thing this must not do is blind the drop detector: a gap
        *inside* a generation still raises, and only a strictly-greater,
        capture-time epoch re-bases the stream.

        `needs_base` is the ORDERBOOK requirement and only the orderbook
        requirement. Set — the default, and what an `orderbook_delta` passes —
        a message arriving before any snapshot is a fault, because a delta
        applied to an unbased book fabricates a book. Clear — a `trade` print,
        a `ticker` update, an `error` frame — the first `seq` seen anchors the
        stream and continuity is checked from there. A gap still faults; there
        is simply no snapshot to wait for, and the drop detector is armed on
        the first frame instead of never.
        """
        if sid is None or int(sid) != self.sid:
            self.stats["wrong_sid"] += 1
            self._fail(SUB_WRONG_SID,
                       f"message belongs to subscription {sid}, not {self.sid}")
        generation = coerce_generation(generation)
        if generation is not GENERATION_UNKNOWN and generation != self.generation:
            if generation < self.generation:
                self.stats["stale_generation"] += 1
                self._fail(SUB_STALE_GENERATION,
                           f"message is from generation {generation}, this "
                           f"subscription is on {self.generation}")
            self._begin_generation(generation)
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

        if needs_base:
            if not self.healthy:
                self._fail(self.state_reason or SUB_AWAITING_SNAPSHOT,
                           "delta received while the subscription is not healthy; "
                           "rejected rather than buffered")
            if self.last_seq is None:
                self._fail(SUB_AWAITING_SNAPSHOT,
                           "delta received before any snapshot on this subscription")
        elif self.last_seq is None:
            # ANCHOR, not a fault. This stream has no snapshot to wait for, so
            # the first `seq` it delivers is the position everything after it is
            # measured against. `healthy` is deliberately NOT set: it means "a
            # snapshot has based this book", the books are the only thing that
            # reads it, and a trade stream has none.
            self.last_seq = seq
            self.stats["accepted"] += 1
            return SEQ_OK
        if seq == self.last_seq:
            self.stats["duplicates"] += 1
            return SEQ_DUPLICATE
        expected = self.last_seq + 1
        if seq < self.last_seq:
            self.stats["regressions"] += 1
            self._reanchor_if_unbased(seq, needs_base=needs_base)
            self._fail(SUB_REGRESSION,
                       f"sequence regression: expected {expected}, got {seq}")
        if seq > self.last_seq + 1:
            self.stats["gaps"] += 1
            self._reanchor_if_unbased(seq, needs_base=needs_base)
            self._fail(SUB_GAP,
                       f"sequence gap: expected {expected}, got {seq}")
        self.last_seq = seq
        self.stats["accepted"] += 1
        return SEQ_OK

    def begin_recovery(self) -> None:
        """Mark the subscription as awaiting a fresh snapshot."""
        self.healthy = False
        self.state_reason = SUB_AWAITING_SNAPSHOT
        self.last_seq = None
        self.stats["recoveries"] += 1

    def _begin_generation(self, generation: int) -> None:
        """Move to a new epoch and await its first snapshot.

        Deliberately NOT `begin_recovery()`: a reconnect is not a recovery
        from a fault, and merging the two would make the counters unable to
        answer the only question that matters here — was that a reconnect or
        a drop? The position is dropped because it belongs to the previous
        namespace; the state is "awaiting snapshot" because a new generation
        must be re-based by a snapshot before any delta can be ordered.
        """
        if generation <= self.generation:
            raise SubscriptionError(
                f"sid {self.sid}: refusing to begin generation {generation} "
                f"from {self.generation}; epochs are monotonic and a "
                "backwards one would re-base the stream onto dead state")
        self.generation = generation
        self.last_seq = None
        self.healthy = False
        self.state_reason = SUB_AWAITING_SNAPSHOT
        self.stats["generation_advances"] += 1

    def supersede(self, *, market_tickers=None, generation=None) -> int:
        """Start a new generation, e.g. after a full resubscription.

        The generation is what makes a straggler from the old stream
        identifiable: its sequence numbers are from a different namespace, and
        without this they would look like an ordinary gap.

        `generation` may be given explicitly so the collector's session-wide
        epoch — the number it also stamps onto every archived envelope — is the
        SAME number this object validates against. Two counters merely intended
        to agree is how the tape ended up with a null column in the first
        place. It may never move backwards.
        """
        # `coerce_generation`, not `int()`: `int(True)` is 1, and an epoch
        # that came from a corrupt value must not be indistinguishable from a
        # real first generation.
        target = (self.generation + 1 if generation is None
                  else coerce_generation(generation))
        if target <= self.generation:
            raise SubscriptionError(
                f"sid {self.sid}: supersede to generation {target} from "
                f"{self.generation} would move the epoch backwards")
        self.generation = target
        if market_tickers is not None:
            self.subscribed_market_tickers = tuple(market_tickers)
        self.stats["generation_advances"] += 1
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
        # The epoch this router's books are positioned in. Compared against the
        # subscription's on every accepted message rather than inferred from
        # who called what: the generation advances by TWO different routes —
        # `supersede()` in the live lane, where the collector knows a
        # resubscription happened, and the record's own stamp in the replay
        # lane, where only the tape knows. One reconciliation point covers both.
        self._books_generation = subscription.generation

    def book_for(self, ticker: str) -> "OrderBook":
        book = self.books.get(ticker)
        if book is None:
            book = self.books[ticker] = OrderBook(ticker, grid=self.grid)
            # Born into the epoch the books are positioned in, so a book
            # created after a reconnect is not silently attributed to
            # generation 0 — and is not re-based a second time by `_rebase_all`.
            book.subscription_generation = self._books_generation
        return book

    def _unpublish_all(self, reason: str) -> None:
        for book in self.books.values():
            book._halt(f"subscription {self.subscription.sid}: {reason}")

    def _rebase_all(self) -> None:
        """A new subscription generation began — rebase, do NOT halt.

        The counterpart to `_unpublish_all`, and the distinction is the point
        of KALSHI-TAPE-GENERATION: a lost message means every book on the
        subscription is suspect, while a reconnect means every book's POSITION
        is stale and nothing else. Collapsing the two made an ordinary
        reconnect look exactly like data loss.

        Rebasing does take every book out of publishable state — each one now
        awaits its OWN snapshot for the new epoch — but it does so through the
        typed `awaiting_snapshot_for_generation` state rather than through
        `_halt`, so the two remain telling different stories. Nothing here
        touches `integrity_reason`, and a test spies on `_halt` to keep it that
        way.
        """
        generation = self.subscription.generation
        if generation == self._books_generation:
            return
        for book in self.books.values():
            book.begin_subscription_generation(generation)
        self._books_generation = generation

    def _accept(self, record: dict, *, is_snapshot: bool = False,
                needs_base: bool = True) -> str:
        """`subscription.accept` plus the generation-boundary bookkeeping.

        The rebase has to land BETWEEN accepting the message and applying it:
        the snapshot that opens a new generation is the very message whose
        `seq` is from the new namespace.
        """
        try:
            return self.subscription.accept(
                sid=record.get("sid"), seq=record.get("seq"),
                generation=subscription_generation_of(record),
                is_snapshot=is_snapshot, needs_base=needs_base)
        finally:
            # `finally`, not `else`: a generation can advance and THEN fail
            # (a new generation whose first message is a delta, which genuinely
            # cannot be ordered). The books are unpublished by the caller for
            # that reason — but their positions still belong to the epoch that
            # ended, and leaving them would make the next snapshot look like a
            # rewind.
            self._rebase_all()

    def dispatch(self, record: dict) -> dict:
        """Apply one archived/live envelope. Ordering, then routing, then apply.

        A subscription-level failure unpublishes EVERY book on this
        subscription, not just the one the failing message happened to name.
        The lost message could have belonged to any of them.

        A GENERATION BOUNDARY is not such a failure. It rebases the books'
        positions and halts none of them, because the record itself says a new
        subscription began — see `SubscriptionState.accept`. Each book stays
        non-publishable, under its own typed reason, until ITS OWN snapshot for
        the new generation arrives.
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
            #
            # `needs_base=False`: whatever this frame is, it is not a delta
            # being applied to a book, so it must not be refused for want of a
            # snapshot. That refusal is what made every `trade` frame a fault
            # (2026-08-17 wire evidence, `SubscriptionState`). A frame with no
            # `seq` at all — every `ticker` frame on the DEMO wire, 2,071 of
            # 2,071 — is not ordered by anything and is passed over here rather
            # than being counted as a fault for a number the venue never sent.
            if record.get("seq") is not None:
                try:
                    self._accept(record, needs_base=False)
                except SubscriptionError:
                    # A gap on a sid that carries books means a message that
                    # could have belonged to any of them was lost, so they are
                    # all suspect. On a sid that carries none — trade, ticker —
                    # this is a no-op, which is the correct amount of damage.
                    self._unpublish_all(
                        self.subscription.state_reason or "sequence fault")
                    raise
            return {"action": "ignored", "event_type": etype}
        msg = (record.get("raw") or {}).get("msg") or {}
        ticker = record.get("market_ticker") or msg.get("market_ticker")
        is_snapshot = etype == "orderbook_snapshot"
        # Recorded BEFORE the accept, so a subscription whose very first
        # orderbook frame faults is still known to be an orderbook subscription
        # and still gets the recovery that applies to it.
        self.subscription.note_orderbook_frame()

        try:
            status = self._accept(record, is_snapshot=is_snapshot)
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

    def publication_states(self) -> dict:
        """Per-market typed publication state. The only place they are composed.

        THE INVARIANT: within generation `g`, a market is not publishable until
        THAT MARKET itself has received its snapshot for `g`. The per-book half
        lives in `OrderBook.publication_state`; the subscription half is added
        here, and it is a CONJUNCTION rather than a replacement — a
        subscription awaiting its own base can publish nothing, and a based
        subscription still says nothing about a market that has not been
        re-snapshotted.

        The per-market reason is reported in preference to the subscription's
        because it is the more specific true statement about that market. The
        defect this replaces did the opposite: it took `subscription.healthy`,
        a SINGLE flag for sixty markets, and let the first snapshot of a new
        generation republish all sixty.
        """
        healthy = self.subscription.healthy
        states = {}
        for ticker, book in sorted(self.books.items()):
            state = book.publication_state
            if state.publishable and not healthy:
                state = PublicationState(
                    False, PUB_SUBSCRIPTION_UNHEALTHY,
                    (f"subscription {self.subscription.sid}: "
                     f"{self.subscription.state_reason or SUB_AWAITING_SNAPSHOT}"),
                    state.subscription_generation, state.based_generation)
            states[ticker] = state
        return states

    def publishable_books(self) -> dict:
        """The boolean view, derived from the typed one so they cannot diverge."""
        return {t: s.publishable for t, s in self.publication_states().items()}
