"""KALSHI-PROD-QUAL-PRECAPTURE — one immutable archive root per collection session.

**This module closes B4 of `KALSHI-TAPE-MEASUREMENT-CONTRACT-001`, without
touching the record schema.**

B4, measured: `RECORD_FIELDS` has no session field, and none is derivable.
`subscription_generation` and `connection_generation` both restart at 1 in the
next session, and `segment_id` is `venue.YYYY-MM-DDTHH[.rNNNN]` — a wall-clock
partition plus a rotation counter — so a new session starting in a new hour is
indistinguishable from a rotation inside one. The measured consequence of
appending two sessions to one archive root is not a merge, it is a silent
halt: every record after the boundary is rejected as `stale_generation`,
`publishable` goes False, and the identical first session ALONE replays clean.

The contract's remedy is a run rule — *one archive root per collection
session* — and it says so explicitly:

> If P4 wants a single multi-session archive, the record envelope needs a
> session identity — a `RECORD_SCHEMA_VERSION` bump, which is a schema decision
> outside this contract's authority and must not be made silently.

So this module does **not** bump `RECORD_SCHEMA_VERSION` and does not add a
field to any record. It makes the run rule *enforceable* instead of
*remembered*, with a sidecar claim beside the genesis:

* a session id is **generated and persisted BEFORE the socket is opened** —
  `claim_session_root()` returns only after the claim is fsynced to disk, and
  the pre-capture preflight builds no transport factory until it has;
* the claim is **immutable**: it is published with `os.link`, which fails with
  EEXIST rather than overwriting, and this module exposes no function that
  rewrites or removes one. Immutability is a property of the filesystem here,
  not of anyone's discipline;
* a **second session pointed at a claimed root is refused, loudly and typed** —
  `SessionRootConflict`, naming both session ids and the rule it is enforcing.

**Why the claim sits at the ENVIRONMENT root and not the top-level root.**
`archive.read_verified()` returns every record for an *environment* in
committed order, and that is what the replay CLI reads — so the environment
directory is the unit the B4 boundary actually lives in. Claiming there is
strictly tighter than claiming at the top: a root holding `demo/` and
`production/` would pass a top-level claim while still mixing two sessions
inside one environment, which is the failure B4 describes.

**Re-entry is not a second session.** A crash-restart of the *same* session id
against its own root is idempotent and returns the durable claim unchanged.
That is deliberate: the reconnect ladder is inside one session, and a rule that
refused its own session would push operators toward deleting claims, which is
the one behaviour that would make the guarantee meaningless.

This module is NOT imported by the collector. It is a preflight, and keeping it
out of the collector's import closure is what lets the structural
order-API guard (`scripts/kalshi_prod_observation_guard.py`) keep asserting
that closure as an equality.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    canonical_bytes,
    canonical_datetime,
    digest_hex,
    parse_canonical,
)
from app.realtime import evidence_fs

SESSION_CLAIM_FILENAME = "collection-session.json"
SESSION_CLAIM_SCHEMA_VERSION = 1
TEMP_SUFFIX = ".tmp"

# Every field carries a stated purpose. Nothing is here because later code would
# find it convenient.
SESSION_CLAIM_FIELDS = (
    "schema_version",            # envelope version for this artifact
    "canonical_schema_version",  # digests only compare within one encoding
    "session_id",                # THE identity B4 says the record cannot carry
    "environment",               # demo evidence must never become production
    "archive_root",              # the root this session owns, as configured
    "claimed_at",                # persisted BEFORE the socket opens, by rule
    "claim_digest",              # so a truncated or edited claim is detectable
)

# A bounded read: the claim is a few hundred bytes and anything larger is not a
# claim. Stated as a constant so "the file was enormous" is a refusal with a
# number in it rather than an unbounded allocation.
MAX_CLAIM_BYTES = 64 * 1024


class SessionRootError(RuntimeError):
    """A session/archive-root pairing that must not be used."""


class SessionRootConflict(SessionRootError):
    """The root already belongs to a DIFFERENT collection session.

    Typed, and not a bool return: a caller can forget to check a bool, and the
    thing it would forget to check is whether the tape it is about to write can
    ever be replayed past its own first boundary.
    """


class SessionRootCorrupt(SessionRootError):
    """A claim exists but cannot be read as one.

    Deliberately NOT the same as "no claim". An unreadable claim means an
    unknown session may own this root, and re-claiming it would be exactly the
    silent mixing this module exists to prevent. Absence is a state; garbage is
    a halt.
    """


def new_session_id(*, now: datetime | None = None) -> str:
    """`s-YYYYMMDDTHHMMSSZ-<12 hex>` — sortable, and unique without a registry.

    The timestamp half is for humans reading a directory listing; the random
    half is the identity. Two sessions started in the same second on the same
    host must not collide, and a counter would need somewhere durable to live —
    which is the problem this module is solving, so it cannot also be its
    dependency.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"s-{moment.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"


def env_root(root, environment: str) -> Path:
    return Path(root) / environment


def session_claim_path(root, environment: str) -> Path:
    return env_root(root, environment) / SESSION_CLAIM_FILENAME


def claim_digest_of(claim: dict) -> str:
    payload = {k: v for k, v in claim.items() if k != "claim_digest"}
    return digest_hex(payload)


@dataclass(frozen=True)
class SessionClaim:
    """The durable fact that one session owns one archive root."""

    session_id: str
    environment: str
    archive_root: str
    claimed_at: str
    path: str
    schema_version: int = SESSION_CLAIM_SCHEMA_VERSION
    canonical_schema_version: int = CANONICAL_SCHEMA_VERSION
    claim_digest: str = ""
    already_existed: bool = False

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "canonical_schema_version": self.canonical_schema_version,
            "session_id": self.session_id,
            "environment": self.environment,
            "archive_root": self.archive_root,
            "claimed_at": self.claimed_at,
            "claim_digest": self.claim_digest,
        }

    def assert_durable(self) -> "SessionClaim":
        """Re-read the claim from disk and prove it is this one.

        Called by the preflight immediately before it is willing to build a
        transport factory. An in-memory object is not evidence that anything
        was persisted, and "the session id existed in a variable before the
        socket opened" is not the rule — the rule is that it was on disk.
        """
        durable = read_session_claim(Path(self.archive_root), self.environment)
        if durable is None:
            raise SessionRootError(
                f"the claim for session {self.session_id} is not on disk at "
                f"{self.path}; no socket may be opened for a session whose "
                "identity was never persisted")
        if durable.session_id != self.session_id:
            raise SessionRootConflict(
                f"{self.path} now names session {durable.session_id!r}, not "
                f"{self.session_id!r}")
        if self.claim_digest and durable.claim_digest != self.claim_digest:
            raise SessionRootConflict(
                f"{self.path} has a different claim digest than the claim this "
                "process published")
        # `self`, not `durable`: the durable read always reports
        # `already_existed=True` (it read a file), and returning it would erase
        # the one fact the caller may need — whether THIS call minted the claim
        # or adopted one. A verifier must not overwrite what it verified.
        return self


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage(directory: Path, payload: bytes) -> Path:
    tmp = directory / (f"{SESSION_CLAIM_FILENAME}.{os.getpid()}."
                       f"{uuid.uuid4().hex[:8]}{TEMP_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    staged = tmp.read_bytes()
    if staged != payload:
        tmp.unlink(missing_ok=True)
        raise SessionRootError(
            f"staged session claim is {len(staged)} bytes, expected "
            f"{len(payload)}; refusing to publish bytes that are not the ones "
            "we meant to commit")
    return tmp


def read_session_claim(root, environment: str) -> SessionClaim | None:
    """The durable claim, `None` if the root is unclaimed, or a typed halt.

    Never a bare `None` for a claim it merely failed to parse: `None` means
    *unclaimed*, and conflating "nobody owns this" with "we could not tell who
    owns this" is how two sessions would end up in one root.
    """
    path = session_claim_path(root, environment)
    if not os.path.lexists(path):
        return None
    if not evidence_fs.is_regular_file(path):
        raise SessionRootCorrupt(
            f"{path} exists but is not a regular file; an archive root whose "
            "session claim is a symlink, device or directory must not be "
            "written to")
    data, reason = evidence_fs.bounded_read(path, max_bytes=MAX_CLAIM_BYTES)
    if data is None:
        raise SessionRootCorrupt(f"{path} could not be read: {reason}")
    try:
        claim = parse_canonical(data)
    except Exception as exc:
        raise SessionRootCorrupt(
            f"{path} is not a readable session claim: "
            f"{type(exc).__name__}") from None
    if type(claim) is not dict:
        raise SessionRootCorrupt(f"{path} is not a session-claim object")
    missing = [f for f in SESSION_CLAIM_FIELDS if f not in claim]
    if missing:
        raise SessionRootCorrupt(f"{path} is missing field(s) {missing}")
    if claim["claim_digest"] != claim_digest_of(claim):
        raise SessionRootCorrupt(
            f"{path} does not match its own digest; the claim has been edited "
            "or truncated and the owning session is therefore unknown")
    if claim["environment"] != environment:
        raise SessionRootCorrupt(
            f"{path} claims environment {claim['environment']!r}, but was read "
            f"as {environment!r}")
    return SessionClaim(
        session_id=claim["session_id"], environment=claim["environment"],
        archive_root=claim["archive_root"], claimed_at=claim["claimed_at"],
        path=str(path), schema_version=claim["schema_version"],
        canonical_schema_version=claim["canonical_schema_version"],
        claim_digest=claim["claim_digest"], already_existed=True)


def claim_session_root(root, environment: str, *, session_id: str,
                       now: datetime | None = None) -> SessionClaim:
    """Bind one archive root to one collection session, durably and once.

    Returns only after the claim is fsynced, so the caller may treat the return
    as "this session's identity exists on disk" — which is the whole ordering
    requirement: **generated and persisted BEFORE the WebSocket is opened.**

    Raises `SessionRootConflict` if a DIFFERENT session already owns the root,
    and `SessionRootCorrupt` if a claim exists but cannot be read. Re-claiming
    with the same session id is idempotent.
    """
    if type(session_id) is not str or not session_id.strip():
        raise SessionRootError("session_id must be a non-empty string")
    if type(environment) is not str or not environment.strip():
        raise SessionRootError("environment must be a non-empty string")
    root = Path(root)
    directory = env_root(root, environment)
    directory.mkdir(parents=True, exist_ok=True)

    existing = read_session_claim(root, environment)
    if existing is not None:
        if existing.session_id == session_id:
            return existing
        raise SessionRootConflict(
            f"HALT — ONE ARCHIVE ROOT PER COLLECTION SESSION. {directory} "
            f"already belongs to session {existing.session_id!r} (claimed "
            f"{existing.claimed_at}); session {session_id!r} must write to a "
            "different root. Appending a second session here produces a tape "
            "that cannot be replayed past the first boundary: the durable "
            "record carries no session identity, so every record after it is "
            "rejected as a stale generation "
            "(KALSHI-TAPE-MEASUREMENT-CONTRACT-001 §11 B4).")

    claim = {
        "schema_version": SESSION_CLAIM_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "session_id": session_id,
        "environment": environment,
        "archive_root": str(root),
        "claimed_at": canonical_datetime(now or datetime.now(timezone.utc)),
    }
    claim["claim_digest"] = claim_digest_of(claim)
    payload = canonical_bytes(claim)

    final = session_claim_path(root, environment)
    tmp = _stage(directory, payload)
    try:
        # `os.link` fails with EEXIST if `final` exists, which is what makes the
        # claim immutable in the filesystem rather than by convention. A rename
        # would silently overwrite — and overwriting a session claim is exactly
        # the mixing this module refuses.
        os.link(tmp, final)
    except FileExistsError:
        # Someone claimed it between our read and our publish. Re-read and let
        # the same rule decide, so a race produces the same typed refusal as a
        # sequential attempt rather than a different one.
        tmp.unlink(missing_ok=True)
        durable = read_session_claim(root, environment)
        if durable is not None and durable.session_id == session_id:
            return durable
        owner = "an unreadable claim" if durable is None else repr(durable.session_id)
        raise SessionRootConflict(
            f"HALT — ONE ARCHIVE ROOT PER COLLECTION SESSION. {directory} was "
            f"claimed by {owner} while session {session_id!r} was claiming it; "
            "two collectors are pointed at one root") from None
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_directory(directory)

    return SessionClaim(
        session_id=session_id, environment=environment,
        archive_root=str(root), claimed_at=claim["claimed_at"],
        path=str(final), claim_digest=claim["claim_digest"],
        already_existed=False)


def open_session_root(root, environment: str, *, session_id: str | None = None,
                      now: datetime | None = None) -> SessionClaim:
    """Generate (if needed) and persist the session identity. The one entry point.

    A caller that wants a fresh session passes no `session_id` and gets one
    minted here — so the ordering rule ("generated and persisted before the
    socket opens") is satisfied by construction rather than by a caller
    remembering to do the two steps in the right order.
    """
    session_id = session_id or new_session_id(now=now)
    return claim_session_root(root, environment, session_id=session_id,
                              now=now).assert_durable()
