"""KALSHI-READONLY-AUTH-001 — confined RSA-PSS signing for the read-only observer.

This module is the **only** place in `app/realtime/` that touches key material,
and it is deliberately narrow.

Scope of the amended safety boundary (see `docs/SAFETY_BOUNDARIES.md`): RSA
private-key loading solely for authenticated **read-scoped Kalshi market-data**
requests under `OBSERVE_ONLY`. Wallets, transaction/order/blockchain signing,
order creation/cancellation/amendment, API-key management, write-scoped
credentials and general-purpose signing APIs remain prohibited.

Two design choices are load-bearing:

**There is no `sign(method, path)`.** The public surface is
`websocket_headers()`, which takes no method at all and accepts only the single
reviewed WebSocket path. A general-purpose signer would let any future caller
sign anything the key is capable of authorising — the credential's scope would
become the only control left, and one mis-provisioned key would then be the
whole failure. Narrowing the *caller's* reach keeps a second, independent lock.

**The key is loaded once, at construction, and never leaves this object.** No
serialization, no `repr`, no pickling, no copy into a temporary file. Exception
messages carry the path and the reason, never bytes from the file: a stack trace
is a document that gets pasted into issues and logs, so anything it can carry
must be assumed public.
"""

from __future__ import annotations

import base64
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.realtime.kalshi import (
    OBSERVE_ONLY,
    WS_PATH,
    CredentialError,
    assert_method_allowed,
    canonical_signing_string,
    fingerprint_key_id,
    require_mode,
)

# The single reviewed read-only path. Adding an entry here is a boundary change
# and needs its own review — that is exactly why it is a named constant and not
# an argument default somewhere.
READ_ONLY_PATH_ALLOWLIST: frozenset[str] = frozenset({WS_PATH})

SIGNING_METHOD = "GET"
MIN_RSA_KEY_BITS = 2048

# `0o600` is the required mode. `0o400` is accepted because it is *strictly
# tighter*, and a check that rejects a safer permission teaches people to loosen
# it to pass the check.
ACCEPTED_CREDENTIAL_MODES = (0o600, 0o400)

_PEM_BEGIN = b"-----BEGIN"
_PEM_ENCRYPTED_MARKERS = (b"ENCRYPTED PRIVATE KEY", b"Proc-Type: 4,ENCRYPTED")

# Repo root: app/realtime/auth.py -> realtime -> app -> repo
_REPO_ROOT = Path(__file__).resolve().parents[2]


class CredentialConfinementError(CredentialError):
    """The credential file is not confined the way the boundary requires.

    Raised *before* the file is opened, so no key byte has been read when this
    surfaces.
    """


@dataclass(frozen=True)
class CredentialFileFacts:
    """Metadata-only record. Deliberately has nowhere to put key contents."""

    path: str
    owner_uid: int
    owner: str
    group: str
    mode_octal: str
    parent_mode_octal: str
    inspected_at: str


def _owner_names(st: os.stat_result) -> tuple[str, str]:
    """Resolve uid/gid to names, degrading to numeric strings off-host."""
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(st.st_uid).pw_name
    except (ImportError, KeyError):
        return (str(st.st_uid), str(st.st_gid))
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return (owner, group)


def inspect_credential_file(
    path: str | os.PathLike,
    *,
    expected_owner_uid: int | None = None,
    forbid_repo_root: Path | None = _REPO_ROOT,
) -> CredentialFileFacts:
    """Verify confinement and return metadata. Never opens the file.

    Every check here runs before any read, so a failure means the process has
    not seen a single byte of the key.
    """
    p = Path(path)
    if not p.is_absolute():
        raise CredentialConfinementError(
            f"credential path {p} is relative; supply an absolute path so the "
            "file the process reads cannot depend on its working directory")

    try:
        lst = os.lstat(p)
    except FileNotFoundError:
        raise CredentialConfinementError(f"credential file not found at {p}") from None
    except PermissionError:
        raise CredentialConfinementError(
            f"credential file at {p} is not readable by this process") from None

    if stat.S_ISLNK(lst.st_mode):
        raise CredentialConfinementError(
            f"credential path {p} is a symlink; the target can be swapped "
            "between the permission check and the read, so only a regular file "
            "is accepted")
    if not stat.S_ISREG(lst.st_mode):
        raise CredentialConfinementError(
            f"credential path {p} is not a regular file")

    mode = stat.S_IMODE(lst.st_mode)
    if mode & 0o077:
        raise CredentialConfinementError(
            f"credential file {p} is mode {oct(mode)}; it must not be readable "
            "or writable by group or other")
    if mode not in ACCEPTED_CREDENTIAL_MODES:
        raise CredentialConfinementError(
            f"credential file {p} is mode {oct(mode)}; expected 0o600")

    if expected_owner_uid is not None and lst.st_uid != expected_owner_uid:
        raise CredentialConfinementError(
            f"credential file {p} is owned by uid {lst.st_uid}, expected "
            f"uid {expected_owner_uid}")

    parent = p.parent
    parent_mode = stat.S_IMODE(os.stat(parent).st_mode)
    if parent_mode & 0o077:
        raise CredentialConfinementError(
            f"credential directory {parent} is mode {oct(parent_mode)}; it must "
            "not be readable, writable or traversable by group or other")

    if forbid_repo_root is not None:
        try:
            p.relative_to(forbid_repo_root)
        except ValueError:
            pass
        else:
            raise CredentialConfinementError(
                f"credential file {p} is inside the repository at "
                f"{forbid_repo_root}; a key under version control is one "
                "`git add -A` away from being published")

    owner, group = _owner_names(lst)
    return CredentialFileFacts(
        path=str(p),
        owner_uid=lst.st_uid,
        owner=owner,
        group=group,
        mode_octal=oct(mode),
        parent_mode_octal=oct(parent_mode),
        inspected_at=datetime.now(timezone.utc).isoformat(),
    )


def _load_key_material(p: Path) -> rsa.RSAPrivateKey:
    """The one bounded loader. Reads, parses, and drops the buffer.

    Every failure below is re-raised with a message built from the *path and the
    reason*, never from the file's bytes.
    """
    buf = bytearray(p.read_bytes())
    try:
        if buf.count(_PEM_BEGIN) == 0:
            raise CredentialConfinementError(
                f"credential file {p} does not contain a PEM block")
        if buf.count(_PEM_BEGIN) > 1:
            raise CredentialConfinementError(
                f"credential file {p} contains multiple PEM blocks; which key "
                "authenticates the session must not be positional luck")
        if any(marker in buf for marker in _PEM_ENCRYPTED_MARKERS):
            raise CredentialConfinementError(
                f"credential file {p} is an encrypted PEM; no reviewed password "
                "mechanism exists, and prompting or hardcoding one is not in "
                "scope for this milestone")
        try:
            key = serialization.load_pem_private_key(bytes(buf), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            # `exc` can quote the offending bytes; only its type is repeated.
            raise CredentialConfinementError(
                f"credential file {p} could not be parsed as an unencrypted PEM "
                f"private key ({type(exc).__name__})") from None
    finally:
        # Best effort: CPython may already hold copies, but leaving the buffer
        # live in a long-running process is a needless second chance for it to
        # end up in a core dump.
        for i in range(len(buf)):
            buf[i] = 0

    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialConfinementError(
            f"credential file {p} holds a {type(key).__name__}; Kalshi request "
            "signing requires an RSA private key")
    if key.key_size < MIN_RSA_KEY_BITS:
        raise CredentialConfinementError(
            f"credential file {p} holds a {key.key_size}-bit RSA key; the "
            f"minimum accepted is {MIN_RSA_KEY_BITS}")
    return key


class ReadOnlyRequestSigner:
    """Signs exactly one thing: the read-only Kalshi WebSocket handshake.

    There is no method parameter and no free-form path parameter, so a caller
    cannot reach a write route through this object even with a key that would
    permit one.
    """

    __slots__ = ("_key", "_key_id", "_key_id_fingerprint", "_environment", "_facts")

    def __init__(
        self,
        *,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        environment: str,
        capability_mode: str = OBSERVE_ONLY,
        facts: CredentialFileFacts | None = None,
    ) -> None:
        require_mode(capability_mode, OBSERVE_ONLY)
        if not isinstance(key_id, str) or not key_id.strip():
            raise CredentialError("key id must be a non-empty string")
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise CredentialError("private key must be an RSA key")
        self._key = private_key
        self._key_id = key_id
        self._key_id_fingerprint = fingerprint_key_id(key_id)
        self._environment = environment
        self._facts = facts

    # --- construction ---------------------------------------------------------
    @classmethod
    def from_path(
        cls,
        *,
        key_id: str,
        credential_path: str | os.PathLike,
        environment: str,
        expected_owner_uid: int | None = None,
        capability_mode: str = OBSERVE_ONLY,
    ) -> "ReadOnlyRequestSigner":
        """Load at startup from a confined path. The only supported entry point.

        There is no `from_pem_bytes` and no `from_env`: an in-memory or
        environment-variable constructor would make it possible to route the key
        through a shell, a process listing, or a container inspect output, and
        no caller in this milestone needs that.
        """
        facts = inspect_credential_file(
            credential_path, expected_owner_uid=expected_owner_uid)
        key = _load_key_material(Path(facts.path))
        return cls(key_id=key_id, private_key=key, environment=environment,
                   capability_mode=capability_mode, facts=facts)

    # --- signing --------------------------------------------------------------
    def _signature(self, *, method: str, path: str, timestamp_ms: int) -> str:
        method = assert_method_allowed(method)
        if method != SIGNING_METHOD:
            raise CredentialError(
                f"{method} is not signable by the read-only signer")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
            raise CredentialError(
                "timestamp_ms must be an integer number of milliseconds; a "
                f"{type(timestamp_ms).__name__} was given")
        if timestamp_ms <= 0:
            raise CredentialError("timestamp_ms must be positive")
        if path not in READ_ONLY_PATH_ALLOWLIST:
            raise CredentialError(
                f"path {path!r} is not in the reviewed read-only allowlist "
                f"{sorted(READ_ONLY_PATH_ALLOWLIST)}")
        message = canonical_signing_string(
            method=method, path=path, timestamp_ms=timestamp_ms).encode("utf-8")
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def websocket_headers(
        self, *, timestamp_ms: int, path: str = WS_PATH,
    ) -> Mapping[str, str]:
        """Headers for the observe-only WebSocket handshake.

        `path` is accepted only so a caller can be explicit; the allowlist is
        what decides, and it currently holds one entry.
        """
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": self._signature(
                method=SIGNING_METHOD, path=path, timestamp_ms=timestamp_ms),
        }

    # --- introspection: metadata only ----------------------------------------
    @property
    def environment(self) -> str:
        return self._environment

    @property
    def key_id_fingerprint(self) -> str:
        return self._key_id_fingerprint

    @property
    def credential_facts(self) -> CredentialFileFacts | None:
        return self._facts

    def public_key_pem(self) -> str:
        """The PUBLIC half, for offline signature verification in tests/ops.

        Named unambiguously so nobody reaches for it expecting the other one.
        """
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def __repr__(self) -> str:
        return (f"<ReadOnlyRequestSigner env={self._environment} "
                f"key={self._key_id_fingerprint} mode={OBSERVE_ONLY}>")

    __str__ = __repr__

    def __getstate__(self):
        raise TypeError(
            "ReadOnlyRequestSigner is not serializable; pickling it would write "
            "the private key wherever the pickle goes")

    def __reduce__(self):
        raise TypeError("ReadOnlyRequestSigner is not serializable")

    def __copy__(self):
        raise TypeError("ReadOnlyRequestSigner is not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("ReadOnlyRequestSigner is not copyable")
