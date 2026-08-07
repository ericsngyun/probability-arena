"""KALSHI-REALTIME-OBSERVATION-001A — observe-only Kalshi transport.

Defence in depth. The API key is scoped `["read"]` by human provisioning, and
this module independently refuses to be capable of trading **even if a
mis-scoped credential is installed**:

* capability mode is `OBSERVE_ONLY` and is checked before every request;
* only `GET` is reachable — there is no code path that emits POST/PUT/PATCH/DELETE;
* only the four market-data channels are subscribable;
* no order, portfolio, cancel, amend, or key-management route exists as a string
  in this package, so none can be requested even by accident.

**This module holds no key material.** Signing lives in the sibling
`auth.py` under the KALSHI-READONLY-AUTH-001 boundary amendment, which confines
RSA private-key loading to authenticated read-scoped market-data requests. This
module keeps the key-free half — scope verification, capability enforcement, the
canonical signing string, subscription construction — so that the parts every
transport path depends on can be reasoned about without key material anywhere in
view. Credential *metadata* (path, owner, mode) is inspected here; contents
never are.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --- capability modes -----------------------------------------------------------
OBSERVE_ONLY = "OBSERVE_ONLY"
SHADOW_ONLY = "SHADOW_ONLY"
DEMO_EXECUTION = "DEMO_EXECUTION"
LIVE_BOUNDED = "LIVE_BOUNDED"
ALL_MODES = (OBSERVE_ONLY, SHADOW_ONLY, DEMO_EXECUTION, LIVE_BOUNDED)

# This milestone implements exactly one.
IMPLEMENTED_MODES = (OBSERVE_ONLY,)

# --- environments (physically and logically separate) ---------------------------
ENV_DEMO = "demo"
ENV_PRODUCTION = "production"
ENVIRONMENTS = (ENV_DEMO, ENV_PRODUCTION)

WS_PATH = "/trade-api/ws/v2"
WS_HOSTS = {
    ENV_PRODUCTION: "wss://external-api-ws.kalshi.com" + WS_PATH,
    ENV_DEMO: "wss://demo-api.kalshi.co" + WS_PATH,
}

# --- channel allowlist ----------------------------------------------------------
# `ticker`, not `ticker_v2`. The lifecycle channel is versioned and the ticker
# channel is not; assuming the version suffix generalises would subscribe to a
# channel that does not exist, which fails as silence rather than as an error.
ALLOWED_CHANNELS = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")

# Market open/close timing is NOT promised as an explicit lifecycle message.
# A book must not infer that a market is still open from the absence of a
# close event — absence of a message is not evidence of state, and this is the
# same reasoning `verify_scopes` applies to an absent scopes field.
LIFECYCLE_GUARANTEES_EXPLICIT_OPEN_CLOSE = False
FORBIDDEN_CHANNELS = ("fill", "market_positions", "user_orders", "communications",
                      "order_group_updates")

ALLOWED_HTTP_METHODS = ("GET",)

# Required scope set. Anything else halts.
REQUIRED_SCOPES = ("read",)


class CapabilityError(RuntimeError):
    """A requested action exceeds the active capability mode."""


class CredentialError(RuntimeError):
    """A credential that must not be used."""


def require_mode(active: str, needed: str) -> None:
    """Fail closed. The comparison is explicit rather than ordinal so a new mode
    cannot accidentally become 'greater than' observe-only."""
    if active not in ALL_MODES:
        raise CapabilityError(f"unknown capability mode {active!r}")
    if needed not in ALL_MODES:
        raise CapabilityError(f"unknown required capability {needed!r}")
    if active != needed:
        raise CapabilityError(
            f"active mode {active} does not grant {needed}; this build "
            f"implements {IMPLEMENTED_MODES} only")


def assert_method_allowed(method: str) -> str:
    if method.upper() not in ALLOWED_HTTP_METHODS:
        raise CapabilityError(
            f"HTTP {method} is not permitted in OBSERVE_ONLY; only "
            f"{ALLOWED_HTTP_METHODS} exist in this package")
    return method.upper()


def assert_channels_allowed(channels) -> list:
    out = []
    for ch in channels:
        if ch in FORBIDDEN_CHANNELS:
            raise CapabilityError(
                f"channel {ch!r} is a private user stream and is forbidden in "
                "OBSERVE_ONLY")
        if ch not in ALLOWED_CHANNELS:
            raise CapabilityError(
                f"channel {ch!r} is not in the observe-only allowlist "
                f"{ALLOWED_CHANNELS}")
        out.append(ch)
    if not out:
        raise CapabilityError("at least one channel is required")
    return out


# --- credential -----------------------------------------------------------------


@dataclass(frozen=True)
class CredentialDescriptor:
    """Everything about a credential that may be recorded. Never the key."""
    environment: str
    key_id_fingerprint: str
    scopes: tuple
    path: str
    owner_uid: int
    mode_octal: str
    verified_at: str

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def fingerprint_key_id(key_id: str) -> str:
    """A bounded identifier for the record. Not reversible, not the key."""
    import hashlib

    if not key_id:
        raise CredentialError("empty key id")
    digest = hashlib.sha256(key_id.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def verify_scopes(reported: object, *, environment: str) -> tuple:
    """Enforce `scopes == ["read"]`, halting on anything else.

    Kalshi keys default to broader access when scopes are omitted, so an ABSENT
    scopes field is treated as a failure rather than a default — the whole point
    of the check is that silence must not read as permission.
    """
    if reported is None:
        raise CredentialError(
            "HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED: the key "
            "metadata reports no scopes field. Omission is not 'read'; Kalshi "
            "keys default to broader access when scopes are omitted.")
    # `type(...) in` rather than isinstance: a list SUBCLASS can lie in
    # __iter__, and a string is itself iterable — `"read"` would otherwise
    # decompose into four single-character "scopes".
    if type(reported) not in (list, tuple):
        raise CredentialError(
            "HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED: scopes "
            f"is {type(reported).__name__}, not a list")
    # No str() coercion: an object whose __str__ returns "read" is not a scope
    # the venue granted.
    if any(type(s) is not str for s in reported):
        raise CredentialError(
            "HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED: scopes "
            "contains a non-string entry")
    scopes = tuple(reported)
    unknown = [s for s in scopes if s not in ("read", "write")]
    if unknown:
        raise CredentialError(
            "HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED: "
            f"unrecognized scope(s) {unknown}")
    if "write" in scopes:
        raise CredentialError(
            "HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED: the key "
            f"reports write scope ({list(scopes)}). The observer must never "
            "hold an order-capable credential.")
    if tuple(sorted(scopes)) != tuple(sorted(REQUIRED_SCOPES)):
        raise CredentialError(
            "HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED: scopes "
            f"{list(scopes)} != {list(REQUIRED_SCOPES)}")
    return scopes


def describe_credential(*, environment: str, key_id: str, scopes: object,
                        key_path: str | os.PathLike) -> CredentialDescriptor:
    """Validate a credential and produce the recordable descriptor.

    Reads the key file's METADATA only — owner and mode. The contents are never
    opened here, so nothing in this function can leak them.
    """
    if environment not in ENVIRONMENTS:
        raise CredentialError(f"unknown environment {environment!r}")
    verified = verify_scopes(scopes, environment=environment)
    p = Path(key_path)
    if not p.exists():
        raise CredentialError(f"credential file not found at {p}")
    st = p.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise CredentialError(
            f"credential file {p} is mode {oct(mode)}; it must not be readable "
            "or writable by group or other")
    return CredentialDescriptor(
        environment=environment, key_id_fingerprint=fingerprint_key_id(key_id),
        scopes=verified, path=str(p), owner_uid=st.st_uid,
        mode_octal=oct(mode), verified_at=datetime.now(timezone.utc).isoformat())


class RequestSigner:
    """Signing SEAM. This base class holds no key and signs nothing.

    The venue signs with RSA-PSS-SHA256 over `timestamp_ms + METHOD + path`,
    base64, sent as KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE. The concrete
    implementation is `app.realtime.auth.ReadOnlyRequestSigner`, which is
    reachable only through a confined credential path and exposes no
    `sign(method, path)`.

    The seam remains because the fixture, replay and archive paths must run with
    no credential at all, and the honest way to express that is an object that
    refuses rather than one that quietly returns empty headers.
    """

    def headers(self, *, method: str, path: str,
                timestamp_ms: int | None = None) -> dict:  # pragma: no cover
        raise NotImplementedError(
            "no signer is installed; live authenticated validation is pending "
            "the human-installed read-only key")


def canonical_signing_string(*, method: str, path: str,
                             timestamp_ms: int) -> str:
    """`timestamp_ms + METHOD + path`, query stripped. Key-free and testable."""
    method = assert_method_allowed(method)
    return f"{timestamp_ms}{method}{path.split('?', 1)[0]}"


class UnsignedTransportSigner(RequestSigner):
    """Explicit no-op for fixture and replay paths."""

    def headers(self, *, method: str, path: str,
                timestamp_ms: int | None = None) -> dict:
        assert_method_allowed(method)
        return {}


# --- subscription ---------------------------------------------------------------


def build_subscribe(command_id: int, channels, market_tickers) -> dict:
    """The subscribe command, with `use_yes_price` always set.

    Never relies on the server default: the default is subject to migration, and
    a silent flip would reinterpret every NO level without changing a single
    line of our code.
    """
    params = {
        "channels": assert_channels_allowed(channels),
        "use_yes_price": True,
    }
    tickers = [str(t) for t in market_tickers or []]
    if not tickers:
        raise CapabilityError("at least one market ticker is required")
    params["market_tickers"] = tickers
    return {"id": int(command_id), "cmd": "subscribe", "params": params}


# Documented recovery action. Named as a constant because the exact spelling is
# a venue fact that has not been confirmed on the wire, and a demo session must
# settle it before recovery is relied on — see the Gate 10 checklist.
RECOVERY_ACTION_GET_SNAPSHOT = "get_snapshot"


def build_get_snapshot(command_id: int, sid: int) -> dict:
    """Recovery path 1: ask the venue to re-send the snapshot for a subscription.

    Cheaper than resubscribing and keeps the sid, so books do not have to be
    torn down. Unconfirmed on the wire; `build_resubscribe` is the fallback
    that uses only commands we already send.
    """
    return {"id": int(command_id), "cmd": "update_subscription",
            "params": {"sids": [int(sid)],
                       "action": RECOVERY_ACTION_GET_SNAPSHOT}}


def build_unsubscribe(command_id: int, sid: int) -> dict:
    return {"id": int(command_id), "cmd": "unsubscribe",
            "params": {"sids": [int(sid)]}}


def build_resubscribe(command_id: int, sid: int, channels, market_tickers) -> list:
    """Recovery path 2: unsubscribe then subscribe.

    Uses only commands whose shapes are already exercised, so it is the path
    that works even if `get_snapshot` turns out not to be a valid action. The
    caller must supersede the subscription generation, because the new stream's
    sequence numbers are in a different namespace from the old one's.
    """
    return [build_unsubscribe(command_id, sid),
            build_subscribe(command_id + 1, channels, market_tickers)]


def build_resubscribe_snapshot(command_id: int, sid: int) -> dict:
    """Deprecated alias for `build_get_snapshot`, kept for existing callers."""
    return build_get_snapshot(command_id, sid)


class Transport:
    """Interface. A fixture transport satisfies it without a network or a key."""

    async def connect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def send(self, message: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    def __aiter__(self):  # pragma: no cover
        raise NotImplementedError


class FixtureTransport(Transport):
    """Replays recorded frames. No socket, no credential, no venue."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def __aiter__(self):  # pragma: no cover - exercised via helper
        for frame in self.frames:
            yield frame

    def iter_frames(self):
        return iter(self.frames)
