"""KALSHI-LIVE-TAPE-COLLECTOR-001 — the session orchestrator.

**CP2 scope: credential wiring.** Section 6.5 of the milestone says the path
from the two documented observer environment variables to
`ReadOnlyRequestSigner.from_path` must exist in exactly one place; that is
`load_observer_signer`, below.

**CP3 scope: the session loop and the observation record.** Everything after
the credential section: the frame-by-frame orchestrator (connect, subscribe,
read, envelope, append, dispatch, close, result) and the normalization that
decides what a tape record says. The measurement lane
(`collector_metrics.py`, CP4) is NOT here; this module talks to it through one
optional, no-op-by-default hook (`NullCollectorMetrics`).

Two properties of the CP3 half are load-bearing and are argued at their
definitions:

**Trade direction comes from the venue or it is absent — it is never
inferred.** `_normalize_trade` reads Kalshi's own `taker_side` field and
nothing else. It takes the venue message as its only argument, so it has no
book, no quote and no previous frame to compare a print against; a Lee-Ready
or tick-rule classifier is not merely absent, it has no input to run on. A
frame without the field produces a typed absence, never a guess.

**Normalization adds a reading; it never replaces the venue's own words.**
Every normalized number sits beside the exact value the venue sent and the
name of the field it came from, following the pattern `book.py:503-518`
established for order-book levels, for the reason stated there: a convention
that turns out to be wrong must be reinterpretable from the archive rather
than force a re-collection.

Before this module the signer was orphaned — `from_path` had no caller anywhere
in `app/`, and the DEMO session recorded in
`docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md` was therefore opened by an
out-of-repo script. A credential loader that lives outside the repository is a
credential loader nobody reviews.

Three properties of the CREDENTIAL half are load-bearing and are asserted
structurally in `tests/test_kalshi_live_tape_cp2_001.py`:

**This module never touches key material.** It passes a key id and a filesystem
path to `app.realtime.auth`, which is the only file in the repository permitted
to open a credential file, and gets back an object whose private key lives in a
closure. There is no `open`, no `read_bytes`, no PEM parsing and no signing
here; adding any would make `auth.py` the *first* holder of key material rather
than the *only* one.

**There is no unsigned fallback, and there cannot be one.** The function has a
single `return`, whose value is what `ReadOnlyRequestSigner.from_path` returned
and which is type-checked before it is handed back; every other exit is a
`raise`. `UnsignedTransportSigner` is not imported, not named, and not
reachable from this module's namespace, and no `except` clause exists inside
the loader that could turn a refusal into a degraded success. Silently
downgrading an authenticated read-only session to an unauthenticated one would
report success while collecting a tape nobody authenticated — the failure would
be invisible in exactly the artifact meant to detect it.

**Refusals name variables, never values.** Every message this module builds is
made of environment-variable NAMES and reasons. A stack trace is a document
that gets pasted into issues and logs; anything it can carry must be assumed
public. The same rule the auth module states for the key file applies here to
the key id.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.realtime.archive import ArchiveError, EventArchive
from app.realtime.archive_head import ArchiveHeadError
from app.realtime.auth import ReadOnlyRequestSigner
from app.realtime.book import (
    SubscriptionError,
    SubscriptionRouter,
    SubscriptionState,
    make_envelope,
    monotonic_ns,
    utcnow,
)
from app.realtime.canonical import canonical_datetime
from app.realtime.fixedpoint import (
    FixedPointError,
    parse_contract_units,
    parse_price_units,
)
from app.realtime.kalshi import (
    ALLOWED_CHANNELS,
    ENVIRONMENTS,
    CapabilityError,
    CredentialError,
    assert_channels_allowed,
    build_get_snapshot,
    build_subscribe,
)
from app.realtime.ws_transport import TransportError

# The status the collector reports when it refuses for want of a credential.
# Named here rather than spelled inline at the call site so the CLI, the
# session result and this refusal cannot drift apart.
REFUSED_NO_CREDENTIAL = "refused_no_credential"

# The two documented variables, quoted exactly as the operator sets them
# (`docs/KALSHI_OBSERVER_PREAUTH_HARDENING_001.md`, `docs/SAFETY_BOUNDARIES.md`).
# They are read through `app.config.Settings`, which is the repository's single
# environment surface and also honours the `.env` file the EVO host uses — a
# second, direct `os.environ` read here would refuse on a host where the
# credential is in fact configured, which is the worst possible way to be safe.
OBSERVER_KEY_ID_VAR = "KALSHI_OBSERVER_API_KEY_ID"
OBSERVER_CREDENTIAL_PATH_VAR = "KALSHI_OBSERVER_CREDENTIAL_PATH"


class ObserverCredentialUnavailable(CredentialError):
    """No observer credential is configured, so no session may be opened.

    A subclass of `CredentialError`, so a caller that already handles credential
    failures handles this one too, and typed rather than a `None` return: a
    sentinel is something a caller can forget to check, and the thing it would
    forget to check is whether the session it just opened was authenticated.
    """

    status = REFUSED_NO_CREDENTIAL


def load_observer_signer(*, environment: str,
                         reported_scopes) -> ReadOnlyRequestSigner:
    """Build the observer's signer, or refuse. Never returns anything else.

    `reported_scopes` has no default here for the same reason it has none in
    `from_path`: it must come from a real `/trade-api/v2/api_keys` response via
    `credential_audit.audit_scopes`, and a hard-coded `["read"]` would defeat
    `verify_scopes` entirely — whatever the default said is what nobody would
    ever pass.

    Raises `ObserverCredentialUnavailable` when either variable is unset, and
    `CredentialConfinementError` (also a `CredentialError`) when the file exists
    but is not confined the way `auth.py` requires. Both are refusals; neither
    is a fallback.
    """
    # Checked before the credential is looked at, so a mistyped environment
    # cannot cause a key file to be opened and read on the way to being
    # rejected. `from_path` also rejects it, but only after loading the key.
    if environment not in ENVIRONMENTS:
        raise ObserverCredentialUnavailable(
            f"{REFUSED_NO_CREDENTIAL}: unknown environment {environment!r}; "
            f"expected one of {list(ENVIRONMENTS)}")

    settings = get_settings()
    key_id = (settings.kalshi_observer_api_key_id or "").strip()
    credential_path = (settings.kalshi_observer_credential_path or "").strip()

    # Half a credential is not a credential, and the refusal says WHICH half is
    # missing by name — an operator debugging a refusal needs the variable name,
    # and needs it without the value being echoed anywhere.
    absent = [name for name, value in (
        (OBSERVER_KEY_ID_VAR, key_id),
        (OBSERVER_CREDENTIAL_PATH_VAR, credential_path)) if not value]
    if absent:
        raise ObserverCredentialUnavailable(
            f"{REFUSED_NO_CREDENTIAL}: {' and '.join(absent)} "
            f"{'is' if len(absent) == 1 else 'are'} not set. The observer opens "
            "no unauthenticated session and has no credential-free mode on a "
            "live host.")

    # The only call. `purposes` is left at its default — the WebSocket handshake
    # alone — so the collector's signer cannot reach the key-metadata route that
    # `for_scope_audit` exists to sign. That audit is a separate one-shot entry
    # point and never runs inside a session.
    signer = ReadOnlyRequestSigner.from_path(
        key_id=key_id,
        credential_path=credential_path,
        environment=environment,
        reported_scopes=reported_scopes,
    )
    # Structurally redundant — `from_path` is a classmethod that can only return
    # its own class — and kept anyway, because this is the exact boundary where
    # an unsigned or otherwise degraded object would have to appear in order to
    # reach a live socket. A control that costs one comparison and closes the
    # whole class of substitution is worth its redundancy.
    if not isinstance(signer, ReadOnlyRequestSigner):
        raise CredentialError(
            f"{type(signer).__name__} is not a ReadOnlyRequestSigner; the "
            "observer refuses any signer it did not load from a confined "
            "credential file, and never degrades to an unsigned one.")
    return signer


# =================================================================================
# CP3 — the observation record
# =================================================================================
#
# What a tape record has to make recomputable, and where each part comes from:
#
#   timestamp             the venue's own stamp (`make_envelope` reads `ts_ms`
#                         first; the frame's raw stamps stay in `raw`)
#   sequence / update id  `seq`/`sid` from the frame, plus any venue-supplied
#                         per-print id (`trade_id`)
#   market                `market_ticker`
#   bid levels            the YES ladder (resting bids), exact integer units
#   ask levels            the NO ladder (already YES-scaled under
#                         `use_yes_price=true`; NO complement is applied — see
#                         `book.py:186-205`)
#   trade price           the venue's `yes_price`, exactly parsed
#   trade quantity        the venue's `count`, exactly parsed
#   trade direction       the venue's `taker_side`, AND NOTHING ELSE
#   spread                derived WITHIN one frame, never across frames
#   market state          the venue's lifecycle/status words
#   time to resolution    the venue's close/expiration stamp minus receive time
#
# Two rules shape every function below.
#
# **Nothing here is stateful.** Every normalizer takes one venue message (plus,
# where a duration is involved, the receive time) and returns a dict. It cannot
# see the previous frame, the book, or the top of book, so no field can be
# derived by comparing a frame to state — which is the mechanism by which trade
# direction would get inferred, and the mechanism by which the same fixture
# could normalize two different ways on two runs.
#
# **A quantity is either observed or explicitly absent.** `coverage` carries one
# entry per required quantity, always, from the closed vocabulary below. A
# missing field is a recorded absence with a reason, never a default, a zero, or
# an inference.

OBSERVATION_SCHEMA_VERSION = 1

# The label an envelope carries for a frame that belongs to no subscribable
# channel (`ok`, `subscribed`, `error`). Deliberately not a member of
# `ALLOWED_CHANNELS`: it names a session frame, and it is never sent anywhere.
CHANNEL_CONTROL = "control"

# event `type` -> the channel that carries it. Closed: an unrecognised type is
# still archived, because an unrecognised frame is evidence about the venue and
# discarding it would lose exactly the surprise worth keeping.
CHANNEL_FOR_EVENT_TYPE = {
    "orderbook_snapshot": "orderbook_delta",
    "orderbook_delta": "orderbook_delta",
    "ticker": "ticker",
    "trade": "trade",
    "market_lifecycle_v2": "market_lifecycle_v2",
}

EVENT_TYPE_UNKNOWN = "unknown"

# --- the required quantities, named once ------------------------------------------
OBS_TIMESTAMP = "timestamp"
OBS_SEQUENCE = "sequence"
OBS_MARKET = "market"
OBS_BID_LEVELS = "bid_levels"
OBS_ASK_LEVELS = "ask_levels"
OBS_TRADE_PRICE = "trade_price"
OBS_TRADE_QUANTITY = "trade_quantity"
OBS_TRADE_DIRECTION = "trade_direction"
OBS_SPREAD = "spread"
OBS_MARKET_STATE = "market_state"
OBS_TIME_TO_RESOLUTION = "time_to_resolution"

REQUIRED_OBSERVATIONS = (
    OBS_TIMESTAMP, OBS_SEQUENCE, OBS_MARKET, OBS_BID_LEVELS, OBS_ASK_LEVELS,
    OBS_TRADE_PRICE, OBS_TRADE_QUANTITY, OBS_TRADE_DIRECTION, OBS_SPREAD,
    OBS_MARKET_STATE, OBS_TIME_TO_RESOLUTION,
)

# --- the closed absence vocabulary -------------------------------------------------
PRESENT = "present"
# The venue could have sent this on this kind of frame and did not.
ABSENT_NOT_SUPPLIED = "absent:not_supplied_by_venue"
# This kind of frame does not carry this quantity at all (a trade print has no
# ask ladder). Distinct from the above because "the venue omitted it" and "it
# was never on offer" are different facts about the venue.
ABSENT_NOT_APPLICABLE = "absent:not_applicable_to_this_frame"
# Present but unusable: it did not parse under the strict fixed-point contract.
# The raw value is retained regardless, so the decision is revisitable.
ABSENT_UNPARSEABLE = "absent:venue_value_unparseable"
# Derivable, but only from more than one frame — and this normalizer refuses to
# hold state. A ladder delta cannot know the other side of the book.
ABSENT_NOT_DERIVABLE = "absent:not_derivable_from_this_frame_alone"
# A ladder larger than `MAX_NORMALIZED_LEVELS`. Raw is still archived whole.
ABSENT_OVER_BOUND = "absent:exceeds_normalization_bound"

ABSENCE_REASONS = (ABSENT_NOT_SUPPLIED, ABSENT_NOT_APPLICABLE,
                   ABSENT_UNPARSEABLE, ABSENT_NOT_DERIVABLE, ABSENT_OVER_BOUND)
COVERAGE_VALUES = (PRESENT,) + ABSENCE_REASONS

# A ladder beyond this is not normalized level-by-level. On the cent grid a
# side holds at most 99 levels and on the 4-decimal grid `fixedpoint.py` admits
# at most 9,999, so this is ~4x the arithmetic worst case: high enough that no
# legitimate book is ever refused, low enough that a hostile frame cannot make
# one record eat the canonical encoder's work budget.
MAX_NORMALIZED_LEVELS = 40_000

# --- venue field names -------------------------------------------------------------
# Aliases exist because two field sets are in play and they disagree: the names
# this repository has pinned as fixtures (`tests/test_kalshi_canonical_001.py`,
# taken from the DEMO wire and the venue's REST payloads) and the names the
# QDK-001 survey recorded from a third-party mirror of the venue docs, which is
# marked UNVERIFIED-on-our-wire for `trade` and `market_lifecycle_v2`
# (`docs/research/QDK-001-clob-microstructure-execution.md:1356-1357`).
#
# Trying each in order is not guessing at a VALUE — the value is whatever the
# venue sent, verbatim — it is tolerating an unsettled NAME. Which name actually
# supplied each number is recorded in the record as `venue_field`, so the first
# real session settles the question from the tape instead of from a doc.
TRADE_ID_FIELDS = ("trade_id", "id")
TRADE_YES_PRICE_FIELDS = ("yes_price_dollars", "yes_price")
TRADE_NO_PRICE_FIELDS = ("no_price_dollars", "no_price")
TRADE_COUNT_FIELDS = ("count_fp", "count")

# ONE field, and it is the venue's own. There is no second candidate and no
# fallback: the alternative to reading this field is recording its absence.
TAKER_SIDE_FIELD = "taker_side"

TICKER_BID_FIELDS = ("yes_bid_dollars", "yes_bid")
TICKER_ASK_FIELDS = ("yes_ask_dollars", "yes_ask")
TICKER_BID_SIZE_FIELDS = ("yes_bid_size_fp", "yes_bid_size")
TICKER_ASK_SIZE_FIELDS = ("yes_ask_size_fp", "yes_ask_size")
TICKER_LAST_PRICE_FIELDS = ("price_dollars", "price", "last_price_dollars")

MARKET_STATE_FIELDS = ("market_status", "status", "state", "lifecycle_state")
RESOLUTION_TIME_FIELDS = ("close_ts", "close_time", "expiration_ts",
                          "expiration_time", "expected_expiration_ts")

# Values we recognise. An unrecognised state is retained verbatim and reported
# as unparseable rather than mapped onto the nearest familiar word — a venue
# that introduces a new state must not be silently read as an old one.
KNOWN_MARKET_STATES = ("open", "closed", "settled", "determined", "active",
                       "paused", "finalized", "initialized")

# Epoch-second sanity bounds. A venue that switched to milliseconds would
# otherwise silently produce a resolution date ~50,000 years out; refusing the
# value is the honest response to a unit we cannot confirm.
MIN_EPOCH_SECONDS = 1_000_000_000      # 2001-09-09
MAX_EPOCH_SECONDS = 4_000_000_000      # 2096-10-02

# The one true statement about trade direction, carried in every trade record so
# that a downstream reader never has to consult a docstring to learn where the
# sign came from.
TRADE_DIRECTION_SOURCE = "venue_field:taker_side"
TRADE_DIRECTION_NOT_INFERRED = (
    "authoritative venue field only; no Lee-Ready, tick-rule or quote-rule "
    "classification exists anywhere in this package")
VENUE_TAKER_SIDES = ("yes", "no")


def _first_field(msg: dict, names) -> tuple:
    """The first of `names` the venue actually supplied, with its name.

    `None` counts as not supplied: a null in JSON is the venue declining to
    answer, and treating it as a value would put a typed absence's reason in the
    one place nobody looks.
    """
    for name in names:
        if name in msg:
            value = msg[name]
            if value is not None:
                return name, value
    return None, None


def _price_block(venue_field, raw_value, *, yes_scaled: bool):
    """The four-field pattern of `book.py:503-518`, for one scalar price.

    `raw_value` is stored exactly as the venue sent it — string, integer or
    `Decimal` — beside the integer units it parsed to. Which field name carried
    it is stored too, because with two candidate name sets in play (see above)
    "0.5100 came from `yes_price`" and "0.5100 came from `yes_price_dollars`"
    are different observations about the venue.
    """
    try:
        units = parse_price_units(raw_value, field=str(venue_field))
    except FixedPointError as exc:
        return {"venue_field": venue_field, "raw_value": raw_value,
                "raw_price_units": None,
                "normalized_yes_price_units": None,
                "yes_scale_normalization": None,
                "parse_refusal": type(exc).__name__}, ABSENT_UNPARSEABLE
    return {"venue_field": venue_field, "raw_value": raw_value,
            "raw_price_units": units,
            # Identity under `use_yes_price=true`, and named rather than
            # implied: the one defect demo validation found was a complement
            # applied where none belongs (`book.py:186-205`).
            "normalized_yes_price_units": units if yes_scaled else None,
            "yes_scale_normalization": ("identity_yes_scaled" if yes_scaled
                                        else "none_applied"),
            "parse_refusal": None}, PRESENT


def _size_block(venue_field, raw_value, *, allow_negative: bool = False):
    try:
        units = parse_contract_units(raw_value, field=str(venue_field),
                                     allow_negative=allow_negative)
    except FixedPointError as exc:
        return {"venue_field": venue_field, "raw_value": raw_value,
                "contract_units": None,
                "parse_refusal": type(exc).__name__}, ABSENT_UNPARSEABLE
    return {"venue_field": venue_field, "raw_value": raw_value,
            "contract_units": units, "parse_refusal": None}, PRESENT


def _normalize_ladder(raw_levels, *, venue_side: str, venue_field: str):
    """One ladder as exact integer levels, with the venue's own words kept.

    Returns `(levels, reason)`. `reason` is `PRESENT` even for an empty ladder:
    an empty book legitimately omits both ladder keys (confirmed on the DEMO
    wire, `book.py:291-298`), and "the venue said there is nothing here" is an
    observation, not a gap.
    """
    if not isinstance(raw_levels, (list, tuple)):
        return [], ABSENT_UNPARSEABLE
    if len(raw_levels) > MAX_NORMALIZED_LEVELS:
        return [], ABSENT_OVER_BOUND
    out = []
    for entry in raw_levels:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return [], ABSENT_UNPARSEABLE
        price, reason = _price_block(f"{venue_field}[price]", entry[0],
                                     yes_scaled=True)
        size, size_reason = _size_block(f"{venue_field}[size]", entry[1])
        if reason != PRESENT or size_reason != PRESENT:
            return [], ABSENT_UNPARSEABLE
        out.append({
            "venue_side": venue_side,
            "venue_field": venue_field,
            "raw_price_value": price["raw_value"],
            "raw_price_units": price["raw_price_units"],
            "normalized_yes_price_units": price["normalized_yes_price_units"],
            "raw_size_value": size["raw_value"],
            "contract_units": size["contract_units"],
        })
    return out, PRESENT


def _best_units(levels, *, highest: bool):
    if not levels:
        return None
    prices = [lvl["normalized_yes_price_units"] for lvl in levels]
    return max(prices) if highest else min(prices)


def _resolution_block(msg: dict, receive_time: datetime):
    """Time to resolution, from THIS frame's own close stamp or not at all.

    A close time seen on an earlier lifecycle frame is deliberately not carried
    forward: caching it would make a record's contents depend on what the
    collector happened to have seen already, so the same fixture could normalize
    two ways. The lifecycle frames are on the tape; the join belongs to the
    reader, who can do it with the whole session in hand.
    """
    venue_field, raw_value = _first_field(msg, RESOLUTION_TIME_FIELDS)
    if venue_field is None:
        return None, ABSENT_NOT_SUPPLIED
    when = None
    if isinstance(raw_value, int) and not isinstance(raw_value, bool):
        if MIN_EPOCH_SECONDS <= raw_value <= MAX_EPOCH_SECONDS:
            when = datetime.fromtimestamp(raw_value, tz=timezone.utc)
    elif isinstance(raw_value, str):
        try:
            when = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    if when is None:
        return {"venue_field": venue_field, "raw_value": raw_value,
                "resolution_time": None,
                "time_to_resolution_us": None}, ABSENT_UNPARSEABLE
    # INTEGER microseconds, for the reason `EventEnvelope.data_age_us` gives at
    # `book.py:76-80`: a float is not canonically representable. Negative means
    # the market's close is already behind us, which is evidence and is kept.
    delta_us = round((when - receive_time).total_seconds() * 1_000_000)
    return {"venue_field": venue_field, "raw_value": raw_value,
            "resolution_time": canonical_datetime(when),
            "time_to_resolution_us": delta_us}, PRESENT


def _market_state_block(msg: dict):
    venue_field, raw_value = _first_field(msg, MARKET_STATE_FIELDS)
    deactivated = msg.get("is_deactivated")
    if venue_field is None and not isinstance(deactivated, bool):
        return None, ABSENT_NOT_SUPPLIED
    normalized = None
    reason = PRESENT
    if type(raw_value) is str and raw_value.strip().lower() in KNOWN_MARKET_STATES:
        normalized = raw_value.strip().lower()
    elif venue_field is not None:
        reason = ABSENT_UNPARSEABLE
    return {"venue_field": venue_field, "raw_value": raw_value,
            "normalized_state": normalized,
            "is_deactivated": (deactivated if isinstance(deactivated, bool)
                               else None),
            "known_vocabulary": list(KNOWN_MARKET_STATES)}, reason


def _normalize_trade(msg: dict) -> dict:
    """One trade print. The ONLY argument is the venue's own message.

    That signature is the control, not a convenience. Published work on
    prediction-market microstructure puts naive direction inference from a
    public book feed at roughly 59-62% accuracy — enough to flip the SIGN of
    effective spread and of Kyle's lambda, which is worse than not computing
    them at all. Kalshi hands us the answer in `taker_side`, so the correct
    design is to read it and to make the alternative unreachable: with no book,
    no quote and no previous print in scope, a Lee-Ready or tick-rule classifier
    has nothing to classify against. There is no midpoint here to compare a
    price to.

    When the field is absent, `normalized_taker_side` is `None`, `source` names
    the absence, and `coverage[trade_direction]` records it. A downstream reader
    that needs signed flow must therefore either find the field or drop the
    print; what it cannot do is receive a direction nobody observed.
    """
    coverage = {}

    raw_side = msg.get(TAKER_SIDE_FIELD)
    normalized_side = None
    if raw_side is None:
        direction_reason = ABSENT_NOT_SUPPLIED
    elif type(raw_side) is str and raw_side.strip().lower() in VENUE_TAKER_SIDES:
        normalized_side = raw_side.strip().lower()
        direction_reason = PRESENT
    else:
        # A value the venue's own vocabulary does not contain. Kept verbatim,
        # refused as a direction. Mapping it onto the nearest familiar word is
        # the inference this whole function exists to prevent.
        direction_reason = ABSENT_UNPARSEABLE
    coverage[OBS_TRADE_DIRECTION] = direction_reason

    yes_field, yes_raw = _first_field(msg, TRADE_YES_PRICE_FIELDS)
    if yes_field is None:
        price_block, price_reason = None, ABSENT_NOT_SUPPLIED
    else:
        price_block, price_reason = _price_block(yes_field, yes_raw,
                                                 yes_scaled=True)
    coverage[OBS_TRADE_PRICE] = price_reason

    no_field, no_raw = _first_field(msg, TRADE_NO_PRICE_FIELDS)
    if no_field is None:
        no_block = None
    else:
        # No complement is applied. The YES price is the authoritative YES-scale
        # number and is present in the same frame, so converting the NO price
        # would produce a second, redundant reading whose only possible effect
        # is to disagree with the first.
        no_block, _ = _price_block(no_field, no_raw, yes_scaled=False)

    count_field, count_raw = _first_field(msg, TRADE_COUNT_FIELDS)
    if count_field is None:
        size_block, size_reason = None, ABSENT_NOT_SUPPLIED
    else:
        size_block, size_reason = _size_block(count_field, count_raw)
    coverage[OBS_TRADE_QUANTITY] = size_reason

    id_field, id_raw = _first_field(msg, TRADE_ID_FIELDS)

    # An arithmetic observation, not an assumption: if the venue's two prices
    # are complements this is one dollar in price units. Recorded rather than
    # asserted, so the convention can be checked from the tape.
    sum_units = None
    if (price_block and price_block["raw_price_units"] is not None
            and no_block and no_block["raw_price_units"] is not None):
        sum_units = price_block["raw_price_units"] + no_block["raw_price_units"]

    block = {
        "trade_id": {"venue_field": id_field, "raw_value": id_raw},
        "direction": {
            "venue_field": TAKER_SIDE_FIELD if raw_side is not None else None,
            "raw_taker_side": raw_side,
            "normalized_taker_side": normalized_side,
            "source": (TRADE_DIRECTION_SOURCE if direction_reason == PRESENT
                       else direction_reason),
            "venue_vocabulary": list(VENUE_TAKER_SIDES),
            "inference_policy": TRADE_DIRECTION_NOT_INFERRED,
        },
        "yes_price": price_block,
        "no_price": no_block,
        "quantity": size_block,
        "yes_plus_no_price_units": sum_units,
    }
    return {"block": block, "coverage": coverage}


def _normalize_orderbook(msg: dict, *, is_snapshot: bool) -> dict:
    coverage = {}
    block = {"is_snapshot": is_snapshot,
             "use_yes_price_requested": True,
             "no_side_normalization": "identity_yes_scaled"}

    if is_snapshot:
        bids, bid_reason = _normalize_ladder(msg.get("yes_dollars_fp") or [],
                                             venue_side="yes",
                                             venue_field="yes_dollars_fp")
        asks, ask_reason = _normalize_ladder(msg.get("no_dollars_fp") or [],
                                             venue_side="no",
                                             venue_field="no_dollars_fp")
        block["bid_levels"] = bids
        block["ask_levels"] = asks
        block["venue_omitted_bid_ladder"] = "yes_dollars_fp" not in msg
        block["venue_omitted_ask_ladder"] = "no_dollars_fp" not in msg
        block["depth"] = "full_ladder"
        coverage[OBS_BID_LEVELS] = bid_reason
        coverage[OBS_ASK_LEVELS] = ask_reason

        best_bid = _best_units(bids, highest=True) if bid_reason == PRESENT else None
        best_ask = _best_units(asks, highest=False) if ask_reason == PRESENT else None
        block["best_yes_bid_units"] = best_bid
        block["best_yes_ask_units"] = best_ask
        if best_bid is not None and best_ask is not None:
            block["spread_units"] = best_ask - best_bid
            coverage[OBS_SPREAD] = PRESENT
        else:
            block["spread_units"] = None
            # One-sided or empty. NOT zero: a zero spread is a tradable market,
            # which is the opposite of what an empty side means.
            coverage[OBS_SPREAD] = (ABSENT_NOT_SUPPLIED
                                    if bid_reason == ask_reason == PRESENT
                                    else ABSENT_UNPARSEABLE)
        return {"block": block, "coverage": coverage}

    # A delta is one signed net change at one price level on one side.
    side = msg.get("side")
    side_name = side.strip().lower() if type(side) is str else None
    price_field = "price_dollars"
    level_reason = ABSENT_UNPARSEABLE
    level = None
    if side_name in ("yes", "no") and price_field in msg and "delta_fp" in msg:
        price, price_reason = _price_block(price_field, msg[price_field],
                                           yes_scaled=True)
        change, change_reason = _size_block("delta_fp", msg["delta_fp"],
                                            allow_negative=True)
        level = {
            "venue_side": side_name,
            "venue_field": price_field,
            "raw_price_value": price["raw_value"],
            "raw_price_units": price["raw_price_units"],
            "normalized_yes_price_units": price["normalized_yes_price_units"],
            "raw_delta_value": change["raw_value"],
            "delta_contract_units": change["contract_units"],
        }
        if price_reason == PRESENT and change_reason == PRESENT:
            level_reason = PRESENT
    elif side_name is None:
        level_reason = ABSENT_NOT_SUPPLIED
    block["changed_level"] = level
    block["depth"] = "single_level_change"
    block["bid_levels"] = [level] if (level_reason == PRESENT
                                      and side_name == "yes") else []
    block["ask_levels"] = [level] if (level_reason == PRESENT
                                      and side_name == "no") else []
    coverage[OBS_BID_LEVELS] = (level_reason if side_name == "yes"
                                else ABSENT_NOT_APPLICABLE)
    coverage[OBS_ASK_LEVELS] = (level_reason if side_name == "no"
                                else ABSENT_NOT_APPLICABLE)
    # The other side of the book is not in this frame, and this normalizer holds
    # no book. Replaying the deltas is how a reader gets the spread back.
    block["spread_units"] = None
    coverage[OBS_SPREAD] = ABSENT_NOT_DERIVABLE
    return {"block": block, "coverage": coverage}


def _normalize_ticker(msg: dict) -> dict:
    coverage = {}
    block = {"depth": "top_of_book_only"}

    bid_field, bid_raw = _first_field(msg, TICKER_BID_FIELDS)
    ask_field, ask_raw = _first_field(msg, TICKER_ASK_FIELDS)
    bid_size_field, bid_size_raw = _first_field(msg, TICKER_BID_SIZE_FIELDS)
    ask_size_field, ask_size_raw = _first_field(msg, TICKER_ASK_SIZE_FIELDS)

    if bid_field is None:
        bid, bid_reason = None, ABSENT_NOT_SUPPLIED
    else:
        bid, bid_reason = _price_block(bid_field, bid_raw, yes_scaled=True)
    if ask_field is None:
        ask, ask_reason = None, ABSENT_NOT_SUPPLIED
    else:
        # The `ticker` channel quotes the ask on the YES scale directly
        # (`yes_ask`), so this is an identity like every other YES-scale price
        # here — not a complemented NO level.
        ask, ask_reason = _price_block(ask_field, ask_raw, yes_scaled=True)

    bid_size = (_size_block(bid_size_field, bid_size_raw)[0]
                if bid_size_field is not None else None)
    ask_size = (_size_block(ask_size_field, ask_size_raw)[0]
                if ask_size_field is not None else None)

    block["bid_levels"] = ([{**bid, "venue_side": "yes", "size": bid_size}]
                           if bid_reason == PRESENT else [])
    block["ask_levels"] = ([{**ask, "venue_side": "yes", "size": ask_size}]
                           if ask_reason == PRESENT else [])
    last_field, last_raw = _first_field(msg, TICKER_LAST_PRICE_FIELDS)
    block["last_price"] = (None if last_field is None else
                           _price_block(last_field, last_raw,
                                        yes_scaled=True)[0])
    coverage[OBS_BID_LEVELS] = bid_reason
    coverage[OBS_ASK_LEVELS] = ask_reason

    if bid_reason == PRESENT and ask_reason == PRESENT:
        block["best_yes_bid_units"] = bid["raw_price_units"]
        block["best_yes_ask_units"] = ask["raw_price_units"]
        block["spread_units"] = ask["raw_price_units"] - bid["raw_price_units"]
        coverage[OBS_SPREAD] = PRESENT
    else:
        block["best_yes_bid_units"] = None
        block["best_yes_ask_units"] = None
        block["spread_units"] = None
        coverage[OBS_SPREAD] = (ABSENT_NOT_SUPPLIED
                                if ABSENT_NOT_SUPPLIED in (bid_reason, ask_reason)
                                else ABSENT_UNPARSEABLE)
    return {"block": block, "coverage": coverage}


def normalize_frame(*, message: dict, receive_time: datetime) -> dict:
    """One venue frame -> the normalized half of its archive record.

    Pure and total: same frame in, same dict out, for every frame including the
    ones this code has never seen. An unrecognised `type` still produces a
    record with a full `coverage` map, because the archive's job is to be able
    to answer a question that has not been asked yet.
    """
    event_type = str(message.get("type") or EVENT_TYPE_UNKNOWN)
    raw_msg = message.get("msg")
    msg = raw_msg if isinstance(raw_msg, dict) else {}

    coverage = {name: ABSENT_NOT_APPLICABLE for name in REQUIRED_OBSERVATIONS}
    observation = {
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "event_type": event_type,
        "channel": CHANNEL_FOR_EVENT_TYPE.get(event_type, CHANNEL_CONTROL),
        "msg_is_object": isinstance(raw_msg, dict),
    }

    # -- identity and ordering ------------------------------------------------
    ticker = msg.get("market_ticker")
    observation["market_ticker"] = ticker if type(ticker) is str else None
    coverage[OBS_MARKET] = (PRESENT if type(ticker) is str
                            else ABSENT_NOT_SUPPLIED)

    seq, sid = message.get("seq"), message.get("sid")
    observation["sequence"] = {
        "sid": sid if isinstance(sid, int) and not isinstance(sid, bool) else None,
        "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        "command_id": message.get("id"),
    }
    coverage[OBS_SEQUENCE] = (PRESENT
                              if observation["sequence"]["seq"] is not None
                              else ABSENT_NOT_SUPPLIED)

    # -- venue timestamp ------------------------------------------------------
    # `make_envelope` owns the interpretation (`ts_ms` first — the venue is not
    # uniform across channels, `book.py:97-106`). What is recorded here is only
    # WHICH stamps the venue supplied, so the coverage question is answerable
    # without re-deriving the parse.
    stamped = [k for k in ("ts_ms", "time", "ts", "timestamp")
               if msg.get(k) is not None]
    observation["venue_timestamp_fields"] = stamped
    coverage[OBS_TIMESTAMP] = PRESENT if stamped else ABSENT_NOT_SUPPLIED

    # -- per-channel content --------------------------------------------------
    if event_type in ("orderbook_snapshot", "orderbook_delta"):
        part = _normalize_orderbook(
            msg, is_snapshot=event_type == "orderbook_snapshot")
        observation["book"] = part["block"]
    elif event_type == "ticker":
        part = _normalize_ticker(msg)
        observation["quote"] = part["block"]
    elif event_type == "trade":
        part = _normalize_trade(msg)
        observation["trade"] = part["block"]
    else:
        part = {"coverage": {}}
    coverage.update(part["coverage"])

    # -- state and resolution, which any frame may carry ----------------------
    state, state_reason = _market_state_block(msg)
    observation["market_state"] = state
    coverage[OBS_MARKET_STATE] = state_reason

    resolution, resolution_reason = _resolution_block(msg, receive_time)
    observation["resolution"] = resolution
    coverage[OBS_TIME_TO_RESOLUTION] = resolution_reason

    observation["coverage"] = coverage
    return observation


# =================================================================================
# CP3 — the session orchestrator
# =================================================================================

VENUE = "kalshi"

# Section 6.3 / open question Q3: two channels, and `trade` /
# `market_lifecycle_v2` only when an operator names them. They are inside the
# safety allowlist and are legitimate market data; they are off by default
# because they add volume without a downstream reader today, and volume is the
# scarce resource in section 8.
DEFAULT_CHANNELS = ("orderbook_delta", "ticker")

STATUS_OK = "ok"
STATUS_CAPPED_TIME = "capped_time"
STATUS_CAPPED_EVENTS = "capped_events"
STATUS_CAPPED_RECONNECTS = "capped_reconnects"
STATUS_STOPPED = "stopped"
STATUS_ARCHIVE_ERROR = "archive_error"

# Section 6.3's draft list also carried a `transport_error` status. There is no
# such outcome and there should not be one: EVERY socket failure enters the
# bounded reconnect ladder, so it either recovers or ends as
# `capped_reconnects`, which additionally says how many attempts were spent. A
# status nothing can ever produce reads as a measured possibility, which is the
# same defect `latency_envelope` removed when it deleted a permanently empty hop.
SESSION_STATUSES = (STATUS_OK, STATUS_CAPPED_TIME, STATUS_CAPPED_EVENTS,
                    STATUS_CAPPED_RECONNECTS, STATUS_STOPPED,
                    STATUS_ARCHIVE_ERROR, REFUSED_NO_CREDENTIAL)

# Carried on every result, including refusals — the TAPE_NOTE pattern of
# `docs/SAFETY_BOUNDARIES.md:241`. A boundary statement that lives only in a
# docstring is not on the artifact.
BOUNDARY_NOTE = (
    "OBSERVE_ONLY: authenticated read-only Kalshi market-data observation. "
    "The session sends channel subscriptions only, over a closed allowlist "
    f"{ALLOWED_CHANNELS}; no order, position, wallet, key-management or "
    "write-scoped surface exists in this package. Evidence is appended to a "
    "filesystem archive; no database session is opened.")

# `archive.append` raises one `ArchiveError` for a rejected RECORD and another
# for a broken PARTITION, and they must not be handled the same way: a
# pathological payload must not end a session (section 8.6), while an
# unwritable partition must, because continuing would produce a tape with an
# unrecorded hole. The two are told apart by the prefix `archive.py:648` puts
# on the per-record case. That is a coupling to a message string and it is
# pinned by a test that drives a REAL rejection through a REAL archive, so a
# reworded message fails loudly here rather than silently reclassifying every
# rejection as a fatal error.
RECORD_REJECTION_PREFIX = "event rejected by the writer:"

# The closed set of counter names the collector will ever hand the metrics
# lane. A name outside this set is a schema change, not a log line.
METRIC_EVENTS = ("frames_malformed", "events_rejected", "sequence_fault",
                 "recovery_requested", "reconnect", "subscription_superseded",
                 "archive_rotation")


class NullCollectorMetrics:
    """The default metrics seam: three methods, all of which do nothing.

    CP4 owns `app/realtime/collector_metrics.py` and may supply any object with
    this interface. The contract, so the two halves can be built independently:

    * ``observe_frame(*, event_type: str, archived: bool, append_ns: int | None,
      rotations: int) -> None`` — once per received frame, AFTER the append, so
      nothing here can delay a record reaching the writer. ``append_ns`` is
      `None` on a dry run and on a rejected record.
    * ``observe_event(name: str) -> None`` — one counter increment, `name` from
      `METRIC_EVENTS`.
    * ``close() -> None`` — once, at session end.

    **No market ticker is ever passed.** A ticker is high-cardinality market
    identity and section 7.2 keeps it out of the telemetry directory entirely;
    the interval record carries a COUNT of subscribed markets, never a list. So
    the seam is shaped such that the collector cannot leak one even by mistake.

    A hook that raises is counted in `metrics_errors` and the session continues.
    The measurement lane is off the archive's critical path in both directions:
    it may not slow the tape down, and it may not take the tape down either.
    """

    def observe_frame(self, *, event_type: str, archived: bool,
                      append_ns: int | None, rotations: int) -> None:
        return None

    def observe_event(self, name: str) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class CollectorConfig:
    """Everything a session is allowed to decide, decided before it starts.

    Every bound is explicit and there is no unbounded session: `max_seconds`,
    `max_events` and `max_reconnects` are three independent hard caps, and the
    market list is enumerated rather than "all markets" — the subscription is
    the tape's sample frame, so it has to be a deliberate choice.

    `channels` runs through `assert_channels_allowed` HERE, at construction, so
    a `fill` in a config file, an environment variable or a CLI argument is a
    `CapabilityError` before any object exists that could open a socket.
    """

    environment: str
    archive_root: Path | None = None
    market_tickers: tuple = ()
    channels: tuple = DEFAULT_CHANNELS
    max_seconds: int = 300
    max_events: int = 100_000
    max_reconnects: int = 3
    dry_run: bool = False
    validate_sequence: bool = True
    reconnect_backoff_base_s: float = 0.5
    reconnect_backoff_max_s: float = 8.0

    def __post_init__(self):
        if self.environment not in ENVIRONMENTS:
            raise CapabilityError(
                f"unknown environment {self.environment!r}; expected one of "
                f"{list(ENVIRONMENTS)}")
        # Raises `CapabilityError` on a forbidden channel and on anything
        # outside the allowlist, including a channel a future venue release
        # adds. The allowlist is closed, so new is refused by default.
        assert_channels_allowed(self.channels)
        if type(self.market_tickers) is not tuple or not self.market_tickers:
            raise CapabilityError(
                "market_tickers must be a non-empty tuple; a session that does "
                "not name its markets has no defined sample frame")
        if any(type(t) is not str or not t for t in self.market_tickers):
            raise CapabilityError("every market ticker must be a non-empty string")
        for name in ("max_seconds", "max_events", "max_reconnects"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise CapabilityError(f"{name} must be a non-negative int")
        if self.max_seconds < 1 or self.max_events < 1:
            raise CapabilityError(
                "max_seconds and max_events are hard caps and must be at least "
                "1; there is no unbounded session")
        if not self.dry_run and self.archive_root is None:
            raise CapabilityError(
                "archive_root is required unless dry_run is set; a session that "
                "archives nowhere must say so explicitly")

    @property
    def markets_subscribed(self) -> int:
        return len(self.market_tickers)


@dataclass(frozen=True)
class CollectorResult:
    """What the session did, in numbers that never merge two failure classes.

    `events_rejected` (the writer said no, typed) and `frames_malformed` (not a
    venue message at all) stay separate for the reason section 7.4 gives: a
    single "dropped" number hides which layer failed. There is no
    `transport_dropped` — CP0 12.4 established the installed library has no drop
    path and no drop counter, so a zero would be a fabricated measurement.
    """

    status: str
    environment: str
    events_received: int = 0
    events_archived: int = 0
    events_rejected: int = 0
    frames_malformed: int = 0
    reconnects: int = 0
    subscription_generations: int = 0
    sequence_faults: int = 0
    recoveries_requested: int = 0
    segments_committed: int = 0
    rotation_failures: int = 0
    metrics_errors: int = 0
    markets_subscribed: int = 0
    channels: tuple = ()
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    archive_root: str | None = None
    measurement_path: str | None = None
    rejection_reasons: tuple = ()
    detail: str = ""
    boundary_note: str = BOUNDARY_NOTE

    def to_dict(self) -> dict:
        return asdict(self)


class _Outcome:
    """Why one connection's read loop ended. Internal to the session."""

    __slots__ = ("kind", "status", "detail")

    def __init__(self, kind: str, *, status: str = "", detail: str = ""):
        self.kind = kind          # stream_ended | cap | transport | archive
        self.status = status
        self.detail = detail


class _Session:
    """One bounded collection session. Single producer, single archive.

    Threading, stated once: there is exactly one task reading exactly one
    socket and calling `archive.append()` on its own stack. `append()` is
    synchronous, caller-threaded and lock-serialised (`segment.py:1499-1528`),
    and section 8.1 keeps it inline deliberately — a queue in front of the
    archive is the architecture KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1 removed
    after eleven review rounds, because it can tell a producer ACCEPTED while
    the event has already left the only place that made it recoverable. So
    append latency IS reader stall here, on purpose: overload becomes a
    timestamped disconnect rather than a silent gap.
    """

    def __init__(self, config: CollectorConfig, *, transport_factory,
                 metrics=None, stop_requested=None, sleep=None):
        self.config = config
        self._transport_factory = transport_factory
        self._metrics = metrics if metrics is not None else NullCollectorMetrics()
        self._stop_requested = stop_requested
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._archive = None
        self._routers: dict = {}
        self._recovery_pending: dict = {}
        self._command_id = 0
        self._started_ns = 0
        self._rotations_seen = 0

        self.events_received = 0
        self.events_archived = 0
        self.events_rejected = 0
        self.frames_malformed = 0
        self.reconnects = 0
        self.subscription_generations = 0
        self.sequence_faults = 0
        self.recoveries_requested = 0
        self.metrics_errors = 0
        self.rejection_reasons: list = []

    # -- the metrics seam, which may never take the session down --------------
    def _metric_frame(self, **kwargs) -> None:
        try:
            self._metrics.observe_frame(**kwargs)
        except Exception:                     # noqa: BLE001 - counted, not silent
            self.metrics_errors += 1

    def _metric_event(self, name: str) -> None:
        try:
            self._metrics.observe_event(name)
        except Exception:                     # noqa: BLE001 - counted, not silent
            self.metrics_errors += 1

    # -- ordering -------------------------------------------------------------
    def _router_for(self, sid: int) -> SubscriptionRouter:
        """One router per SID, exactly as `archive.replay` groups on read.

        Kalshi assigns `seq` per subscription and a multi-channel session holds
        several sids at once (DEMO wire: ticker 1, lifecycle 2, trade 3,
        orderbook 4). Sharing one sequence space across them would manufacture a
        gap on every frame. Grouping the same way replay does means live
        validation and replay cannot disagree about what a fault is.
        """
        router = self._routers.get(sid)
        if router is None:
            router = self._routers[sid] = SubscriptionRouter(
                SubscriptionState(sid, market_tickers=self.config.market_tickers))
            self.subscription_generations += 1
        return router

    def _supersede_all(self) -> None:
        """A new stream's sequence numbers are in a different namespace.

        `supersede` is the existing model (`book.py:696-707`) and is driven, not
        re-invented: it advances the generation and marks every subscription as
        awaiting a fresh snapshot, so a straggler from the old stream is
        identifiable rather than looking like an ordinary gap.
        """
        for router in self._routers.values():
            router.subscription.supersede(
                market_tickers=self.config.market_tickers)
            self.subscription_generations += 1
            self._metric_event("subscription_superseded")
        self._recovery_pending.clear()

    async def _request_recovery(self, transport, sid: int) -> None:
        """Recovery path 1: ask the venue to re-send snapshots for this sid.

        With `market_tickers`, per the wire-confirmed code-14 error
        (`kalshi.py:326-331`) — and once per fault, not once per frame: after a
        gap every subsequent delta faults too, and re-sending on each of them
        would answer a stalled subscription with a command storm.
        """
        if self._recovery_pending.get(sid):
            return
        self._recovery_pending[sid] = True
        self._command_id += 1
        await transport.send(build_get_snapshot(
            self._command_id, sid, list(self.config.market_tickers)))
        self.recoveries_requested += 1
        self._metric_event("recovery_requested")

    # -- per-frame ------------------------------------------------------------
    async def _handle_frame(self, message, transport):
        """One frame, in the order section 6.3 fixes. Returns an `_Outcome` or None."""
        # 1. Both stamps BEFORE any processing, so `data_age_us` means what
        #    `book.py` says it means and is not inflated by our own work.
        receive_time = utcnow()
        receive_mono = monotonic_ns()
        self.events_received += 1

        if type(message) is not dict:
            # Never handed to `make_envelope`: it would archive a record whose
            # `raw` is not a venue message. The live transport already filters
            # these; a fixture transport does not, and the collector must not
            # depend on which transport it holds.
            self.frames_malformed += 1
            self._metric_event("frames_malformed")
            return self._cap_check()

        # 2. Normalization: pure, stateless, and never destructive — `raw` goes
        #    into the envelope verbatim alongside it.
        observation = normalize_frame(message=message, receive_time=receive_time)
        envelope = make_envelope(
            venue=VENUE, environment=self.config.environment,
            channel=observation["channel"], message=message,
            receive_time=receive_time, receive_mono=receive_mono,
            normalized=observation)

        # 3. ARCHIVE FIRST, INTERPRET SECOND. A frame we could not order is
        #    still evidence, and a dispatch exception must never cost us the
        #    record — which is the whole reason `raw` is stored verbatim.
        append_ns = None
        archived = False
        if self._archive is not None:
            before = monotonic_ns()
            try:
                self._archive.append(envelope)
            except ArchiveError as exc:
                text = str(exc)
                if not text.startswith(RECORD_REJECTION_PREFIX):
                    # Partition-level: ENOSPC, an unwritable env dir, every
                    # candidate segment id held. Continuing here would produce a
                    # tape with a hole nothing records.
                    return _Outcome("archive", status=STATUS_ARCHIVE_ERROR,
                                    detail=f"{type(exc).__name__}: {text}")
                self.events_rejected += 1
                if len(self.rejection_reasons) < 32:
                    self.rejection_reasons.append(text)
                self._metric_event("events_rejected")
            else:
                append_ns = monotonic_ns() - before
                archived = True
                self.events_archived += 1
            rotations = self._archive.rotations
            if rotations > self._rotations_seen:
                self._rotations_seen = rotations
                self._metric_event("archive_rotation")

        # 4. Sequence validation, AFTER the append. A `SubscriptionError` is
        #    recorded and triggers recovery; it does not end the session and it
        #    cannot un-archive anything.
        if self.config.validate_sequence:
            await self._validate(envelope, transport)

        # 5. Measurement, last and off the critical path.
        self._metric_frame(event_type=envelope.event_type, archived=archived,
                           append_ns=append_ns, rotations=self._rotations_seen)
        return self._cap_check()

    async def _validate(self, envelope, transport) -> None:
        sid = envelope.sid
        if not isinstance(sid, int) or isinstance(sid, bool):
            # Nothing to order against: `seq` is per subscription and this frame
            # names none. Counted as a fault rather than silently ignored.
            if envelope.seq is not None:
                self.sequence_faults += 1
                self._metric_event("sequence_fault")
            return
        router = self._router_for(sid)
        try:
            router.dispatch(envelope.to_dict())
        except SubscriptionError:
            self.sequence_faults += 1
            self._metric_event("sequence_fault")
            router.subscription.begin_recovery()
            await self._request_recovery(transport, sid)
        except Exception:                     # noqa: BLE001 - see below
            # A book-level refusal (`BookIntegrityError`, `FixedPointError`) is
            # a statement about ONE market's reconstruction, not about the tape.
            # The record is already durable; losing the session over it would
            # trade evidence for tidiness.
            self.sequence_faults += 1
            self._metric_event("sequence_fault")
        else:
            if router.subscription.healthy:
                self._recovery_pending.pop(sid, None)

    def _cap_check(self):
        if self.events_received >= self.config.max_events:
            return _Outcome("cap", status=STATUS_CAPPED_EVENTS,
                            detail=f"max_events={self.config.max_events} reached")
        elapsed_ns = monotonic_ns() - self._started_ns
        if elapsed_ns >= self.config.max_seconds * 1_000_000_000:
            return _Outcome("cap", status=STATUS_CAPPED_TIME,
                            detail=f"max_seconds={self.config.max_seconds} reached")
        if self._stop_requested is not None and self._stop_requested():
            return _Outcome("cap", status=STATUS_STOPPED,
                            detail="stop requested by the caller")
        return None

    # -- one connection -------------------------------------------------------
    async def _one_connection(self, transport):
        try:
            await transport.connect()
            self._command_id += 1
            # The ONLY frame shape that reaches a socket comes from a builder;
            # the transport re-validates it and refuses anything else.
            await transport.send(build_subscribe(
                self._command_id, list(self.config.channels),
                list(self.config.market_tickers)))
            async for message in transport:
                outcome = await self._handle_frame(message, transport)
                if outcome is not None:
                    return outcome
        except TransportError as exc:
            # Type name only: a URI and signed headers must never reach a log
            # line through an exception message (SAFETY_BOUNDARIES:259).
            return _Outcome("transport", detail=type(exc).__name__)
        finally:
            closer = getattr(transport, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:             # noqa: BLE001 - close is best effort
                    pass
        return _Outcome("stream_ended")

    async def _backoff(self) -> None:
        base = self.config.reconnect_backoff_base_s
        if base <= 0:
            return
        delay = min(base * (2 ** max(0, self.reconnects - 1)),
                    self.config.reconnect_backoff_max_s)
        # Jittered and CAPPED, and the loop that calls it is bounded by
        # `max_reconnects`. There is no infinite retry anywhere in this module
        # (the TENNIS-LIVE-FEED-002 precedent), and CP0 12.6 row 5 is why the
        # library's own `async for ws in connect(...)` form is never used: it
        # carries its own unbounded `while True`.
        await self._sleep(delay * (0.5 + random.random() / 2))

    # -- the session ----------------------------------------------------------
    async def run(self) -> CollectorResult:
        started_at = utcnow()
        self._started_ns = monotonic_ns()
        status, detail = STATUS_OK, ""
        segments_committed = 0
        rotation_failures = 0

        if not self.config.dry_run:
            try:
                # The archive must ALREADY exist: `read_genesis` refuses an
                # uninitialized root, and a collector can never bring one into
                # existence (`archive.py:398-401`). Operator step:
                # `archive-init --confirm`.
                self._archive = EventArchive(self.config.archive_root,
                                             environment=self.config.environment)
            # `ArchiveHeadError` is the uninitialized-root HALT and is NOT an
            # `ArchiveError`; catching only the latter turned the one refusal
            # this path exists to report into an untyped crash.
            except (ArchiveError, ArchiveHeadError, OSError) as exc:
                return self._result(STATUS_ARCHIVE_ERROR, started_at,
                                    f"{type(exc).__name__}: {exc}", 0, 0)
        try:
            while True:
                outcome = await self._one_connection(self._transport_factory())
                if outcome.kind == "stream_ended":
                    status, detail = STATUS_OK, "the stream ended"
                    break
                if outcome.kind in ("cap", "archive"):
                    status, detail = outcome.status, outcome.detail
                    break
                # A socket failure. Reconnecting is bounded, and exhausting the
                # bound ENDS the session and says so: a collector that cannot
                # keep up stops rather than degrading into a partial tape whose
                # gaps are invisible (section 8.2 rung 5).
                if self.reconnects >= self.config.max_reconnects:
                    status = STATUS_CAPPED_RECONNECTS
                    detail = (f"{outcome.detail}; max_reconnects="
                              f"{self.config.max_reconnects} exhausted")
                    break
                self.reconnects += 1
                self._metric_event("reconnect")
                self._supersede_all()
                await self._backoff()
        finally:
            if self._archive is not None:
                try:
                    # THE COMMIT POINT. Until it runs there is no authoritative
                    # record count and an unclosed segment is explicitly not
                    # evidence (`archive.py:653-657`), so it runs in a `finally`.
                    segments_committed = len(self._archive.close())
                except ArchiveError as exc:
                    status = STATUS_ARCHIVE_ERROR
                    detail = f"close failed: {type(exc).__name__}: {exc}"
                rotation_failures = len(self._archive.rotation_failures)
            try:
                self._metrics.close()
            except Exception:                 # noqa: BLE001 - counted, not silent
                self.metrics_errors += 1
        if rotation_failures and status == STATUS_OK:
            # An uncommitted segment is not evidence, so a rotation failure is a
            # session-level fault even when every frame was accepted.
            status = STATUS_ARCHIVE_ERROR
            detail = f"{rotation_failures} rotation failure(s)"
        return self._result(status, started_at, detail, segments_committed,
                            rotation_failures)

    def _result(self, status: str, started_at, detail: str,
                segments_committed: int, rotation_failures: int) -> CollectorResult:
        finished_at = utcnow()
        return CollectorResult(
            status=status, environment=self.config.environment,
            events_received=self.events_received,
            events_archived=self.events_archived,
            events_rejected=self.events_rejected,
            frames_malformed=self.frames_malformed,
            reconnects=self.reconnects,
            subscription_generations=self.subscription_generations,
            sequence_faults=self.sequence_faults,
            recoveries_requested=self.recoveries_requested,
            segments_committed=segments_committed,
            rotation_failures=rotation_failures,
            metrics_errors=self.metrics_errors,
            markets_subscribed=self.config.markets_subscribed,
            channels=tuple(self.config.channels),
            dry_run=self.config.dry_run,
            started_at=canonical_datetime(started_at),
            finished_at=canonical_datetime(finished_at),
            duration_ms=(monotonic_ns() - self._started_ns) // 1_000_000,
            archive_root=(None if self.config.archive_root is None
                          else str(self.config.archive_root)),
            measurement_path=None,
            rejection_reasons=tuple(self.rejection_reasons),
            detail=detail)


async def run_session(config: CollectorConfig, *, transport_factory,
                      metrics=None, stop_requested=None,
                      sleep=None) -> CollectorResult:
    """Run one bounded session against whatever transport the factory returns.

    `transport_factory` is a callable, not a transport, because reconnecting
    means building a NEW one: the previous connection is gone and its ops are
    not reusable. It is also the seam that keeps this function credential-free
    and socket-free — CP3's tests pass a `FixtureTransport` factory and open no
    socket at all, while the live path passes a factory that builds a
    `KalshiWebsocketTransport` around a signer from `load_observer_signer`.
    """
    return await _Session(config, transport_factory=transport_factory,
                          metrics=metrics, stop_requested=stop_requested,
                          sleep=sleep).run()


def collect_once(config: CollectorConfig, *, transport_factory, metrics=None,
                 stop_requested=None) -> CollectorResult:
    """Synchronous entry point for an operator command. One session, then stop."""
    return asyncio.run(run_session(config, transport_factory=transport_factory,
                                   metrics=metrics,
                                   stop_requested=stop_requested))
