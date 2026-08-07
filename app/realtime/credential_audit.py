"""KALSHI-DEMO-READONLY-VALIDATION-001 — one-shot credential scope audit.

A human statement that a key was created read-only is not evidence. This asks
the venue.

**This module is not part of the collector.** It is a separate entry point that
makes exactly one request, holds the `API_KEY_METADATA` purpose that the
observer runtime is never granted, and exits. Keeping it separate is the whole
design: the moment the continuous observer can also reach a REST route, the
number of things that must stay correct forever goes up by one.

No transport is implemented here either. `audit_scopes` takes a `fetch`
callable, so the network layer stays outside the security boundary and the
whole verdict path is testable without a key or a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.realtime.kalshi import (
    AuthPurpose,
    CredentialError,
    fingerprint_key_id,
    route_for_purpose,
    verify_scopes,
)

HALT_NOT_PROVEN = "HALT — DEMO OBSERVER CREDENTIAL IS NOT PROVEN READ-ONLY"


@dataclass(frozen=True)
class ScopeAudit:
    """Recordable result. Carries a fingerprint, never the key id in full."""

    environment: str
    key_id_fingerprint: str
    scopes: tuple
    proven_read_only: bool
    verified_at: str
    detail: str | None = None


def _halt(detail: str) -> None:
    raise CredentialError(f"{HALT_NOT_PROVEN}: {detail}")


def audit_scopes(*, signer, key_id: str, environment: str, fetch,
                 timestamp_ms: int) -> ScopeAudit:
    """Ask the venue what this key can do, and refuse anything but `["read"]`.

    `fetch(url, headers)` must perform one GET and return the decoded body.
    Every failure mode below halts rather than degrading: a metadata lookup
    that errored tells us nothing about the key, and "we could not check" must
    never read the same as "we checked and it was fine".
    """
    if AuthPurpose.API_KEY_METADATA not in signer.granted_purposes:
        _halt("the signer was not granted the API_KEY_METADATA purpose")
    method, path = route_for_purpose(AuthPurpose.API_KEY_METADATA)
    headers = signer.headers_for(purpose=AuthPurpose.API_KEY_METADATA,
                                 timestamp_ms=timestamp_ms)
    try:
        body = fetch(path, headers)
    except Exception as exc:
        # Only the type: the exception can carry the signed URL.
        _halt(f"metadata request failed ({type(exc).__name__})")

    if not isinstance(body, dict):
        _halt(f"metadata response is {type(body).__name__}, not an object")
    entries = body.get("api_keys")
    if not isinstance(entries, list):
        _halt("metadata response has no `api_keys` list")

    fingerprint = fingerprint_key_id(key_id)
    matches = [e for e in entries
               if isinstance(e, dict) and e.get("api_key_id") == key_id]
    if not matches:
        # The key we are about to use is not in the list of keys this account
        # has. Either the wrong credential is installed or the wrong account
        # answered; neither is safe to proceed from.
        _halt("the installed key id does not appear in the account's api_keys")
    if len(matches) > 1:
        _halt("the installed key id appears more than once in api_keys")

    entry = matches[0]
    if "scopes" not in entry:
        _halt("the key's metadata has no `scopes` field; omission is not "
              "'read', and Kalshi keys default to broader access when scopes "
              "are omitted")
    reported = entry["scopes"]
    if isinstance(reported, (list, tuple)) and len(set(map(str, reported))) != len(reported):
        _halt(f"the key reports duplicate scopes {list(reported)}")
    try:
        scopes = verify_scopes(reported, environment=environment)
    except CredentialError as exc:
        _halt(str(exc).split(": ", 1)[-1])

    return ScopeAudit(
        environment=environment, key_id_fingerprint=fingerprint,
        scopes=scopes, proven_read_only=True,
        verified_at=datetime.now(timezone.utc).isoformat(),
        detail=f"verified against {method} {path}")
