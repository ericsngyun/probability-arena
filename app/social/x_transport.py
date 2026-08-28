"""SOCIAL-X-LIVE-TRANSPORT-001 — the live X Filtered Stream transport.

Implements the **existing** `SocialStreamTransport` Protocol. The Protocol is
not modified to accommodate X: if a platform cannot be expressed through
`list_rules` / `apply_rules` / `frames` / `aclose`, that is a fact about the
platform worth discovering, not a reason to widen the surface every other
transport is held to.

## Capability

Two X operations, and nothing else:

* `GET  /2/tweets/search/stream`        — consume the public filtered stream
* `GET/POST /2/tweets/search/stream/rules` — manage the stream's rule set

The rule endpoint is a write **to the stream's own filter**, not to an account.
There is no posting, liking, following, DM, block, mute, or any other
account-affecting call, and no OAuth user context exists to authorize one:
authentication is **Bearer Token only**, which cannot act on behalf of a user.

## The token

Held in `BearerToken`, which:

* is loaded from an environment variable or a secret file, never a literal;
* refuses `repr`, `str`, `format`, pickling and JSON;
* exposes the value through exactly one method, used only to build the
  `Authorization` header;
* is never placed on a frame, an artifact, a tape record or a log line.

A credential that can be printed will eventually be printed — into a traceback,
a debug dump, a serialized config. Making it unprintable is cheaper than
auditing every path that might print it.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Sequence

import httpx

from app.social.transport import (
    FrameKind, RuleSyncResult, StreamFrame, TransportError, TransportRule,
)

STREAM_URL = "https://api.x.com/2/tweets/search/stream"
RULES_URL = "https://api.x.com/2/tweets/search/stream/rules"

#: The complete set of URLs this transport may contact. Enumerated so that
#: widening it is a visible diff rather than a matter of caller discipline --
#: the same reason the Solana adapter has no generic `call(method, params)`.
ALLOWED_URLS = frozenset({STREAM_URL, RULES_URL})

TOKEN_ENV = "X_BEARER_TOKEN"
TOKEN_FILE_ENV = "X_BEARER_TOKEN_FILE"

#: A keepalive on X's filtered stream is a bare newline. It is a FRAME, not
#: noise: a quiet stream and a dead stream are different states and this is the
#: only thing that distinguishes them.
KEEPALIVE_BYTES = frozenset({b"", b"\r"})


class CredentialUnavailableError(TransportError):
    """No bearer token is configured. Never falls back to unauthenticated."""


class CredentialLeakError(RuntimeError):
    """Something tried to render or serialize the token."""


class BearerToken:
    """A token that refuses to be printed, formatted or serialized."""

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        v = (value or "").strip()
        if not v:
            raise CredentialUnavailableError("bearer token is empty")
        self._v = v

    # Every rendering path is closed, not merely the obvious one.
    def __repr__(self) -> str:
        return "<BearerToken redacted>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<BearerToken redacted>"

    def __reduce__(self):
        raise CredentialLeakError("a bearer token must not be pickled")

    def __getstate__(self):
        raise CredentialLeakError("a bearer token must not be serialized")

    def authorization_header(self) -> dict[str, str]:
        """The ONLY way the value leaves this object."""
        return {"Authorization": f"Bearer {self._v}"}

    @classmethod
    def load(cls) -> "BearerToken":
        """From the environment, or a file the environment names."""
        path = os.environ.get(TOKEN_FILE_ENV)
        if path:
            p = Path(path)
            if not p.exists():
                raise CredentialUnavailableError(
                    f"{TOKEN_FILE_ENV} points at {p}, which does not exist")
            return cls(p.read_text())
        raw = os.environ.get(TOKEN_ENV)
        if not raw:
            raise CredentialUnavailableError(
                f"no bearer token: set {TOKEN_ENV} or {TOKEN_FILE_ENV}. "
                "There is no unauthenticated mode.")
        return cls(raw)


@dataclass(frozen=True)
class StreamHealth:
    """System-failure counters, kept apart from funnel loss.

    A stream that dropped for an hour must not read as 'the market was quiet'.
    """
    connects: int = 0
    reconnects: int = 0
    keepalives: int = 0
    data_frames: int = 0
    error_frames: int = 0
    http_errors: int = 0
    rate_limited: int = 0


class XFilteredStreamTransport:
    """Live X filtered stream, behind the unmodified Protocol."""

    def __init__(self, *, token: BearerToken | None = None,
                 client: httpx.AsyncClient | None = None,
                 max_reconnects: int = 8,
                 backoff_initial_s: float = 1.0,
                 backoff_max_s: float = 60.0) -> None:
        self._token = token if token is not None else BearerToken.load()
        self._client = client
        self._owns_client = client is None
        self._generation = 0
        self._max_reconnects = max_reconnects
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self.health = StreamHealth()

    def __repr__(self) -> str:
        # never renders the token, even indirectly
        return (f"<XFilteredStreamTransport generation={self._generation} "
                f"data={self.health.data_frames}>")

    # -- internals ---------------------------------------------------------
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(
                connect=10.0, read=None, write=10.0, pool=10.0))
        return self._client

    def _headers(self) -> dict[str, str]:
        return {**self._token.authorization_header(),
                "User-Agent": "probability-arena/social-tape-001"}

    def _bump(self, **kw) -> None:
        cur = {f: getattr(self.health, f) for f in StreamHealth.__dataclass_fields__}
        for k, v in kw.items():
            cur[k] = cur[k] + v
        self.health = StreamHealth(**cur)

    @staticmethod
    def _assert_allowed(url: str) -> None:
        if url not in ALLOWED_URLS:
            raise TransportError(
                f"{url} is not in the allowed URL set {sorted(ALLOWED_URLS)}; "
                "this transport reaches the filtered stream and its rules, "
                "and nothing else")

    # -- Protocol: list_rules ---------------------------------------------
    async def list_rules(self) -> Sequence[TransportRule]:
        self._assert_allowed(RULES_URL)
        r = await self._ensure_client().get(RULES_URL, headers=self._headers())
        self._raise_for_status(r)
        body = r.json()
        return tuple(
            TransportRule(remote_id=str(item.get("id", "")),
                          tag=str(item.get("tag", "")),
                          value=str(item.get("value", "")))
            for item in (body.get("data") or ()))

    # -- Protocol: apply_rules --------------------------------------------
    async def apply_rules(self, add: Sequence[TransportRule],
                          delete: Sequence[str]) -> RuleSyncResult:
        """Reconcile the FILTER, never an account.

        The remote set is read first, which buys two things. Rules already
        present with the same value are reported `unchanged` rather than
        re-sent (X rejects duplicate values, and a rejected batch would
        otherwise take the whole sync down with it). Rules the platform holds
        that this call names neither to add nor to delete are reported
        `foreign` and left alone: the same credential may be shared, and
        silently removing another tenant's rule would be a cross-tenant
        mutation this transport has no mandate to perform.
        """
        self._assert_allowed(RULES_URL)
        client = self._ensure_client()

        remote = await self.list_rules()
        remote_by_value = {r.value: r for r in remote}
        delete_ids = set(delete)

        wanted, unchanged = [], []
        for rule in add:
            present = remote_by_value.get(rule.value)
            if present is not None and present.remote_id not in delete_ids:
                unchanged.append(present.tag or rule.tag)
            else:
                wanted.append(rule)

        named = {r.value for r in add}
        foreign = tuple(r.tag or r.remote_id for r in remote
                        if r.value not in named and r.remote_id not in delete_ids)

        deleted: list[str] = []
        if delete:
            r = await client.post(RULES_URL, headers=self._headers(),
                                  json={"delete": {"ids": list(delete)}})
            self._raise_for_status(r)
            deleted = list(delete)

        added: list[str] = []
        if wanted:
            payload = {"add": [{"value": x.value, "tag": x.tag}
                               for x in wanted]}
            r = await client.post(RULES_URL, headers=self._headers(),
                                  json=payload)
            self._raise_for_status(r)
            body = r.json()
            added = [str(i.get("tag", "")) for i in (body.get("data") or ())]
            errs = body.get("errors") or []
            if errs and not added:
                raise TransportError(
                    f"rule add rejected: {json.dumps(errs)[:300]}")

        return RuleSyncResult(added=tuple(added), deleted=tuple(deleted),
                              unchanged=tuple(unchanged), foreign=foreign)

    # -- Protocol: frames --------------------------------------------------
    async def frames(self) -> AsyncIterator[StreamFrame]:
        """Consume the stream, reconnecting with backoff.

        `subscription_generation` increments on every reconnect and
        `delivery_sequence` restarts within each connection -- the collector
        needs both to tell a re-established stream from a continuing one.

        A clean end-of-stream is treated exactly like a dropped one. The
        platform closing the connection quietly and the connection failing
        loudly are the same event to a collector measuring arrival times, and
        the difference is not worth a second code path that only one of them
        exercises.
        """
        self._assert_allowed(STREAM_URL)
        client = self._ensure_client()
        attempt, backoff = 0, self._backoff_initial_s

        while True:
            if self._generation > 0:
                # A reconnect is itself an observation the collector must see:
                # frames on either side of it are not a contiguous tape.
                yield StreamFrame(kind=FrameKind.RECONNECT,
                                  subscription_generation=self._generation)
            seq, produced = 0, 0
            fatal: BaseException | None = None
            try:
                async with client.stream("GET", STREAM_URL,
                                         headers=self._headers()) as resp:
                    if resp.status_code == 429:
                        self._bump(rate_limited=1, http_errors=1)
                        raise TransportError("rate limited by the platform")
                    if resp.status_code >= 400:
                        self._bump(http_errors=1)
                        raise TransportError(
                            f"stream returned HTTP {resp.status_code}")
                    self._bump(connects=1)
                    async for line in resp.aiter_lines():
                        raw = line.encode()
                        produced += 1
                        if raw.strip() in KEEPALIVE_BYTES:
                            self._bump(keepalives=1)
                            yield StreamFrame(
                                kind=FrameKind.KEEPALIVE,
                                subscription_generation=self._generation)
                            continue
                        seq += 1
                        yield self._to_frame(raw, seq)
            except (httpx.HTTPError, TransportError) as exc:
                fatal = exc

            # Either path lands here: the connection is over. The retry
            # budget resets only for a connection that actually DELIVERED
            # something. Resetting on connect alone would mean an endpoint
            # that accepts and immediately closes reconnects forever at the
            # floor backoff -- a hot loop wearing the costume of resilience.
            if produced:
                attempt, backoff = 0, self._backoff_initial_s
            attempt += 1
            self._generation += 1
            self._bump(reconnects=1)
            if attempt > self._max_reconnects:
                if fatal is not None:
                    raise fatal
                return
            await asyncio.sleep(min(backoff, self._backoff_max_s))
            backoff = min(backoff * 2, self._backoff_max_s)

    def _to_frame(self, raw: bytes, seq: int) -> StreamFrame:
        matched: tuple[str, ...] = ()
        kind = FrameKind.DATA
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if payload.get("errors"):
                kind = FrameKind.ERROR
                self._bump(error_frames=1)
            matched = tuple(str(m.get("tag", ""))
                            for m in (payload.get("matching_rules") or ()))
        if kind is FrameKind.DATA:
            self._bump(data_frames=1)
        # `provenance` stays None: it certifies a FIXTURE's basis, and a live
        # frame's basis is the wire itself. Inventing one here would make live
        # frames indistinguishable from replayed ones.
        return StreamFrame(kind=kind, raw=raw, delivery_sequence=seq,
                           subscription_generation=self._generation,
                           matched_rule_ids=matched)

    # -- Protocol: aclose --------------------------------------------------
    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    @staticmethod
    def _raise_for_status(r: httpx.Response) -> None:
        if r.status_code >= 400:
            # The body may echo the request; never include headers.
            raise TransportError(
                f"X API returned HTTP {r.status_code}: {r.text[:200]}")
