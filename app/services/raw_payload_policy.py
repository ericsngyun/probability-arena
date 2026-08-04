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


def _warn_once(template: str, *args) -> None:
    key = (template, args)
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(template, *args)

CAPTURE_MODE_FULL = "full"
CAPTURE_MODE_ERRORS_ONLY = "errors_only"
CAPTURE_MODE_NONE = "none"

VALID_CAPTURE_MODES = (CAPTURE_MODE_FULL, CAPTURE_MODE_ERRORS_ONLY, CAPTURE_MODE_NONE)

# The default preserves today's behaviour byte-for-byte. Deploying this code
# changes nothing until a host explicitly opts in.
DEFAULT_CAPTURE_MODE = CAPTURE_MODE_FULL

# Truncated SHA-256 (first 16 hex chars = 64 bits). Enough to prove two rows saw
# the same body, or to match a stored body against a replayed provider response,
# without spending 64 bytes per row on a hash of data we deliberately discarded.
DIGEST_HEX_CHARS = 16

# Upper bound on a body kept under `errors_only`. Failure payloads are exactly
# where an unbounded provider body (an HTML error page, a stack-trace blob) is
# most likely, so the error path is the one that must be capped.
MAX_ERROR_PAYLOAD_BYTES = 16 * 1024

# `errors_only` persists a provider ERROR body verbatim, and an error body is
# the payload class most likely to echo the request URL back — and this repo
# does send a provider key in a query string (tennis_providers.py: params
# {"APIkey": ...}). So a writer may only opt into error-body capture after its
# error path has been shown not to carry request detail. No writer is on this
# list today, which is why `errors_only` currently behaves exactly like `none`
# everywhere. Adding one requires a redaction pass or a proven-clean error shape.
ERROR_BODY_CAPTURE_ALLOWLIST: frozenset = frozenset()

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
        "crypto_scout.py:173 copies the PINNED crypto_price_ticks.raw_payload; "
        "3.0 MiB total, and governing it would desynchronise a pinned source.",
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
}

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


def payload_bytes(payload) -> int:
    """Serialized size of a payload, or 0 when it is absent/unserializable."""
    if payload is None:
        return 0
    try:
        return len(json.dumps(payload, separators=(",", ":"), default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def payload_digest(payload) -> str | None:
    """Truncated SHA-256 over the canonical serialization, or None."""
    if payload is None:
        return None
    try:
        blob = json.dumps(
            payload, separators=(",", ":"), sort_keys=True, default=str
        ).encode("utf-8")
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return hashlib.sha256(blob).hexdigest()[:DIGEST_HEX_CHARS]


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

    `full`        -> the payload, unchanged.
    `errors_only` -> the payload when `is_error`, capped at
                     MAX_ERROR_PAYLOAD_BYTES; otherwise the envelope.
    `none`        -> the envelope.

    A `column` listed in PINNED_FULL always returns the payload unchanged,
    whatever the mode says.

    `payload is None` is returned unchanged in every mode: the provider genuinely
    gave nothing, and that is a fact worth keeping distinguishable from
    suppression.

    Writers with no failure concept (a tick row only exists on success) pass
    `is_error=False` always, so for them `errors_only` behaves exactly like
    `none`. That is intended and tested, not an oversight.
    """
    if payload is None:
        return None
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
    if (
        effective == CAPTURE_MODE_ERRORS_ONLY
        and is_error
        and column in ERROR_BODY_CAPTURE_ALLOWLIST
    ):
        if payload_bytes(payload) <= MAX_ERROR_PAYLOAD_BYTES:
            return payload
        # Oversized failure body: keep the envelope plus why it was dropped,
        # rather than persisting an unbounded HTML/stack-trace blob.
        envelope = provenance_envelope(payload, source=source, mode=effective)
        envelope["error_payload_dropped"] = "exceeds_max_error_payload_bytes"
        envelope["max_error_payload_bytes"] = MAX_ERROR_PAYLOAD_BYTES
        return envelope
    return provenance_envelope(payload, source=source, mode=effective)
