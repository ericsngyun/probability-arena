"""RAW-PAYLOAD-STORAGE-001 — explicit capture policy for raw provider bodies.

Several tables store the *complete* provider response alongside the normalized
columns that were extracted from it. Measured on the 2026-08-04 backup snapshot,
that is **1,582.9 MiB — roughly a third of the database** — and the two largest
contributors average ~2 KiB per row, which is 95-96% of the whole row.

This module is the one place that decides whether a full body is persisted. It
changes storage only: no provider call, no normalized field, no row count, no
timing anchor, no transaction boundary.

Three things this deliberately does NOT do:

1. **It cannot suppress a payload production reads.** `PINNED_FULL` lists the
   columns with a proven runtime reader; they ignore the capture mode entirely.
   Getting the reader audit wrong is the one failure mode that would break the
   system silently, so the design removes the possibility rather than relying on
   the audit being complete.
2. **It never writes NULL.** NULL already means "the provider gave us nothing"
   (`tests/test_watcher.py` asserts exactly that). Overloading it with "we chose
   not to store it" would destroy a real distinction and make suppression
   invisible. Suppressed columns get a bounded provenance envelope instead,
   which also keeps non-nullable columns valid without a migration.
3. **It touches no historical row.** Suppression is prospective only; existing
   payloads are left exactly as they are. Reclamation is RAW-PAYLOAD-RECLAMATION-001
   and compaction is SQLITE-COMPACT-COPY-001.
"""

import hashlib
import json
import logging

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# capture() runs on every persisted row — ~226,000/day on EVO-X2. A warning
# emitted per call would put ~25 MiB/day of journald on a shared host, in the
# milestone whose whole purpose is reducing disk pressure.
_WARNED: set = set()


_WARN_ONCE_CAP = 64


def _warn_once(template: str, *args) -> None:
    key = (template, args)
    if key in _WARNED or len(_WARNED) >= _WARN_ONCE_CAP:
        return
    _WARNED.add(key)
    logger.warning(template, *args)

CAPTURE_MODE_FULL = "full"
CAPTURE_MODE_NONE = "none"

# TWO modes, not three. An `errors_only` mode was implemented first, and review
# showed it was provably dead configuration: an error body is the payload class
# most likely to echo the request URL back, and this repo sends a provider key
# in a query string (tennis_providers.py, params={"APIkey": ...}), so no writer
# could be allowed to keep one without a redaction pass. With the allowlist
# necessarily empty, `errors_only` behaved identically to `none` for every
# column, unconditionally — an env value that cannot behave differently from
# another is a misconfiguration trap, not a feature. The security reasoning is
# preserved in MAX_ERROR_PAYLOAD_BYTES' successor note below and in the docs;
# the mode is not shipped. A future writer with a proven-clean error path can
# reintroduce it deliberately.
VALID_CAPTURE_MODES = (CAPTURE_MODE_FULL, CAPTURE_MODE_NONE)

# The default preserves today's behaviour byte-for-byte. Deploying this code
# changes nothing until a host explicitly opts in.
DEFAULT_CAPTURE_MODE = CAPTURE_MODE_FULL

# Truncated SHA-256 (first 16 hex chars = 64 bits). Enough to prove two rows saw
# the same body, or to match a stored body against a replayed provider response,
# without spending 64 bytes per row on a hash of data we deliberately discarded.
DIGEST_HEX_CHARS = 16

# NOTE for any future error-capture mode: a provider ERROR body is the payload
# class most likely to echo the request URL back, and tennis_providers.py sends
# `params={"APIkey": ...}`. Reintroducing error-body capture therefore requires
# a redaction pass or a per-writer proven-clean error shape, plus a hard size
# cap — an HTML error page or a stack-trace blob is unbounded.

# Columns with a PROVEN production reader. These ignore the capture mode. Each
# entry names the reader so the pin can be re-audited rather than trusted.
PINNED_FULL = {
    "crypto_token_risk_assessments.raw_payload": (
        "crypto_tape.extract_creator_address / extract_cohort_counts read "
        "creator_address and snipers/insiders/bundlers counts; "
        "crypto_provider_health, crypto_risk_engine and frontier_eval read "
        "provider_errors; provider_budget runs a SQL LIKE over the column to "
        "derive SolanaTracker request accounting."
    ),
    "market_research_packets.raw_response": (
        "baseball_forecasting, soccer_forecasting and tennis_forecasting all "
        "read packet.raw_response to extract the evidence they forecast from."
    ),
    "crypto_price_ticks.raw_payload": (
        "crypto_scout reads boosts_active to compute the boost delta; "
        "crypto_risk_engine reads boosts_active; crypto_tape reads dex_id."
    ),
    "crypto_horizon_observations.raw_payload": (
        "crypto_horizon reads the observation audit dict back on retry/report."
    ),
}

# Columns this policy governs, with the writer that produces them. Everything
# here was classified no_reader_found or test_only by the Gate 3 audit.
GOVERNED_COLUMNS = {
    "market_price_ticks.raw_payload": "watcher / tennis_watcher",
    "market_snapshots.raw_payload": "scanner",
    "crypto_token_discovery_events.raw_payload": "crypto_scout",
    "opportunity_signals.raw_payload": "watcher",
    "market_detail_enrichments.raw_market_detail": "enrichment",
    "market_detail_enrichments.raw_event_detail": "enrichment",
    "market_detail_enrichments.raw_series_detail": "enrichment",
}

# ONE governed column is read — and the independent audit caught it after the
# first pass called it unread. `crypto_tape.build_birth_event` copies
# `crypto_token_discovery_events.raw_payload` verbatim into
# `crypto_token_birth_events.raw_payload` (crypto_tape.py:342). That sink has no
# reader of its own, so suppressing the source is safe in effect — but it is a
# propagation, not an absence, and it is recorded here rather than left to be
# rediscovered. Under suppression the birth event receives the ENVELOPE, which
# `is_suppressed()` identifies; a test pins that the propagation stays coherent.
PROPAGATED_COLUMNS = {
    "crypto_token_discovery_events.raw_payload":
        "crypto_tape.py:342 -> crypto_token_birth_events.raw_payload (no reader)",
}

# Deliberately NOT governed, and why — so the coverage decision is auditable
# rather than an accident of where the work stopped.
UNGOVERNED_BY_DESIGN = {
    "crypto_opportunity_signals.raw_payload":
        "3.0 MiB total at 44 B/row — the body is already tiny. (It is copied "
        "from the PINNED crypto_price_ticks.raw_payload at crypto_scout.py:173; "
        "suppressing the sink could not affect the pinned source either way.)",
    "edge_precheck_snapshots.raw_context":
        "already a bounded 174-byte thresholds dict, not a provider body.",
    "cross_venue_market_candidates.raw_context":
        "already a bounded 67-byte dict; the lane is not running.",
    "tennis_tape_score_snapshots.raw_payload":
        "already bounded by _strip_bulk() at tennis_tape.py:401.",
    "market_outcomes.raw_payload":
        "4.0 MiB total, ~60 rows/day — below the risk/benefit line for a writer change.",
    "market_forecasts.raw_response":
        "12.3 MiB; the LLM flags are off, so rows carry small structured "
        "fallbacks rather than provider bodies.",
    "market_resolution_assessments.raw_response":
        "5.8 MiB; same reasoning as market_forecasts.raw_response.",
    "crypto_token_birth_events.raw_payload":
        "3.4 MiB; the propagated SINK of a governed column (see "
        "PROPAGATED_COLUMNS) — it receives whatever crypto_tape copies in, so "
        "governing it separately would double-capture.",
}

# Every raw-body column found by the Gate 2 inventory must land in exactly one
# bucket. A column that is inventoried but unclassified is an audit hole, so
# this union is asserted against the inventory by test.
ALL_CLASSIFIED_COLUMNS = (
    frozenset(GOVERNED_COLUMNS)
    | frozenset(PINNED_FULL)
    | frozenset(UNGOVERNED_BY_DESIGN)
    | frozenset({"crypto_token_birth_events.raw_payload"})   # propagated sink
)

# The envelope serializes to ~110 bytes as stored. A body at or below this is
# cheaper to keep than to describe.
MIN_SUPPRESSIBLE_BYTES = 160

SUPPRESSED_MARKER = "raw_payload_suppressed"


def resolve_capture_mode(settings: Settings | None = None) -> str:
    """The effective mode, failing CLOSED to `full` on anything unrecognised.

    An unknown value must never be interpreted as `none` — a typo in a host
    `.env` would then silently start discarding provider bodies. `full` is the
    only safe reading of "I don't understand this setting".
    """
    settings = settings or get_settings()
    raw = (getattr(settings, "raw_payload_capture_mode", None) or "").strip().lower()
    if raw in VALID_CAPTURE_MODES:
        return raw
    if raw:
        _warn_once(
            "unrecognised RAW_PAYLOAD_CAPTURE_MODE %r — failing closed to %r",
            raw[:64], CAPTURE_MODE_FULL,
        )
    return DEFAULT_CAPTURE_MODE


def _canonical(payload) -> str | None:
    """One canonical serialization, reused for BOTH the byte count and the
    digest. Sorting keys does not change the length, so a single dumps feeds
    both and halves the only CPU this module adds on the hot path."""
    if payload is None:
        return None
    try:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True,
                          default=str)
    except Exception:
        # Deliberately broad. json.dumps raises RecursionError (a RuntimeError)
        # on deeply nested input, and `default=str` re-raises whatever a hostile
        # __str__ raises — neither is a TypeError/ValueError. This runs inside a
        # scan's write transaction, so an escape would abort the whole scan, and
        # it would do so ONLY under suppression: `full` returns before ever
        # touching json. An asymmetric abort surface that exists only in the
        # mode being activated is exactly the wrong risk to carry.
        return None


def payload_bytes(payload) -> int:
    """Serialized size of a payload, or 0 when it is absent/unserializable."""
    blob = _canonical(payload)
    return len(blob.encode("utf-8")) if blob is not None else 0


def payload_digest(payload) -> str | None:
    """Truncated SHA-256 over the canonical serialization, or None."""
    blob = _canonical(payload)
    if blob is None:
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:DIGEST_HEX_CHARS]


def provenance_envelope(payload, *, source: str | None, mode: str) -> dict:
    """The bounded evidence that replaces a suppressed body.

    Deliberately small — this is written on every high-volume row, so a field
    that is merely nice to have costs tens of MiB a day. It carries only what
    the row's own normalized columns cannot: that suppression happened, under
    which policy, from which source, how large the discarded body was, and a
    digest that can still prove identity against a replayed response.

    Contains no credential, header, key, environment value, exception text, or
    any fragment of the payload itself — not even its keys.
    """
    return {
        SUPPRESSED_MARKER: True,
        "mode": mode,
        "source": source,
        "bytes": payload_bytes(payload),
        "digest": payload_digest(payload),
    }


def is_suppressed(value) -> bool:
    """True when a stored column holds a provenance envelope rather than a body."""
    return isinstance(value, dict) and value.get(SUPPRESSED_MARKER) is True


def capture(
    payload,
    *,
    source: str | None = None,
    column: str | None = None,
    is_error: bool = False,
    settings: Settings | None = None,
    mode: str | None = None,
):
    """Decide what actually gets stored for one raw-payload column.

    `full` -> the payload, unchanged.
    `none` -> the bounded provenance envelope.

    A `column` listed in PINNED_FULL always returns the payload unchanged,
    whatever the mode says.

    `payload is None` is returned unchanged in every mode: the provider genuinely
    gave nothing, and that is a fact worth keeping distinguishable from
    suppression.

    An already-suppressed payload is returned unchanged, so re-capturing is a
    no-op. RAW-PAYLOAD-RECLAMATION-001 is precisely the caller that would
    otherwise produce an envelope-of-an-envelope whose bytes/digest describe the
    envelope and destroy the original provenance.
    """
    if payload is None:
        return None
    if is_suppressed(payload):
        # Idempotent: never describe an envelope with another envelope.
        return payload
    if payload_bytes(payload) <= MIN_SUPPRESSIBLE_BYTES:
        # The envelope costs ~110 B. Suppressing a body smaller than that would
        # make the row BIGGER — e.g. crypto_scout's EVENT_PAIR_SEEN records a
        # 66-byte {"dex_id":..., "url":...}. Keeping small bodies makes the
        # policy monotone: it can only ever reduce storage.
        return payload
    # Fail CLOSED on anything not explicitly governed. Checking only
    # `column in PINNED_FULL` would fail OPEN: a typo in a pin name, a table
    # rename, or a new writer copy-pasting a pin with one wrong character would
    # silently drop out of the pin list and start suppressing data a production
    # reader needs — exactly the silent failure this module claims to have
    # engineered away. An unrecognised identifier is therefore treated as
    # "not known to be safe" and keeps the full body.
    if column not in GOVERNED_COLUMNS:
        if column is not None and column not in PINNED_FULL:
            _warn_once(
                "unrecognised raw-payload column %r — keeping the full body "
                "(fail-closed); add it to GOVERNED_COLUMNS or PINNED_FULL",
                column,
            )
        return payload

    effective = mode if mode in VALID_CAPTURE_MODES else resolve_capture_mode(settings)
    if effective == CAPTURE_MODE_FULL:
        return payload
    try:
        return provenance_envelope(payload, source=source, mode=effective)
    except Exception:  # pragma: no cover - defensive
        # Fail closed: keeping a body costs storage, dropping one that a future
        # reader needs is unrecoverable, and raising aborts the caller's scan.
        _warn_once("raw-payload envelope failed for %r — keeping the body", column)
        return payload
