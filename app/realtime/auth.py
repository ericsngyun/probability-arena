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
    ENVIRONMENTS,
    OBSERVE_ONLY,
    WS_PATH,
    CredentialError,
    assert_method_allowed,
    canonical_signing_string,
    fingerprint_key_id,
    require_mode,
    verify_scopes,
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

# 2001-09-09 .. 2286-11-20 in ms. Wide enough never to reject a real clock,
# narrow enough that seconds-as-milliseconds is caught locally.
_MIN_TIMESTAMP_MS = 1_000_000_000_000
_MAX_TIMESTAMP_MS = 9_999_999_999_999


class _RedactingHeaders(dict):
    """Headers that do not print their own contents.

    The signature and the key id are both credential-adjacent, and headers get
    logged. `repr` is where that happens by accident.
    """

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<KalshiAuthHeaders {sorted(self)} redacted>"

    __str__ = __repr__


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


def _check_ancestors(resolved: Path) -> str:
    """Reject a confined directory reachable through a loose ancestor.

    A 0700 credential directory inside a group-writable parent is not confined:
    whoever can write the parent can rename it and substitute their own
    directory. Sticky directories (/tmp) are exempt — the sticky bit is exactly
    the mechanism that makes a shared-writable directory safe against rename.
    """
    parent = resolved.parent
    parent_mode = stat.S_IMODE(os.stat(parent).st_mode)
    if parent_mode & 0o077:
        raise CredentialConfinementError(
            f"credential directory {parent} is mode {oct(parent_mode)}; it must "
            "not be readable, writable or traversable by group or other")
    for ancestor in list(parent.parents):
        amode = stat.S_IMODE(os.stat(ancestor).st_mode)
        if amode & 0o022 and not amode & stat.S_ISVTX:
            raise CredentialConfinementError(
                f"ancestor directory {ancestor} is mode {oct(amode)}; a "
                "group- or world-writable ancestor without the sticky bit lets "
                "the credential directory be renamed out from under the check")
    return oct(parent_mode)


def _open_confined(
    path: str | os.PathLike,
    *,
    expected_owner_uid: int | None,
    forbid_repo_root: Path | None,
) -> tuple[int, CredentialFileFacts]:
    """Open the credential with O_NOFOLLOW and validate the DESCRIPTOR.

    Checking a path and then reading it are two syscalls, and between them the
    path can be repointed. So the path is opened first, refusing to traverse a
    final symlink, and every subsequent check runs against `fstat` on the open
    descriptor — the object actually being read, which cannot be swapped.

    The path must already be fully resolved. Accepting `..` or a symlinked
    parent would mean the string we validate and the file we open describe
    different places, which is how the repository-containment check gets
    walked around.
    """
    p = Path(path)
    if not p.is_absolute():
        raise CredentialConfinementError(
            f"credential path {p} is relative; supply an absolute path so the "
            "file the process reads cannot depend on its working directory")
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise CredentialConfinementError(f"credential file not found at {p}") from None
    except OSError as exc:
        raise CredentialConfinementError(
            f"credential path {p} could not be resolved "
            f"({type(exc).__name__})") from None
    if resolved != p:
        raise CredentialConfinementError(
            f"credential path {p} is not fully resolved (it resolves to "
            f"{resolved}); supply the real path so that the path checked and "
            "the file opened cannot describe different places")

    if forbid_repo_root is not None:
        try:
            resolved.relative_to(forbid_repo_root)
        except ValueError:
            pass
        else:
            raise CredentialConfinementError(
                f"credential file {resolved} is inside the repository at "
                f"{forbid_repo_root}; a key under version control is one "
                "`git add -A` away from being published")

    parent_mode_octal = _check_ancestors(resolved)

    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise CredentialConfinementError(
            f"credential file {resolved} could not be opened without following "
            f"a symlink ({type(exc).__name__})") from None

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CredentialConfinementError(
                f"credential path {resolved} is not a regular file")
        if st.st_nlink != 1:
            raise CredentialConfinementError(
                f"credential file {resolved} has {st.st_nlink} hard links; a "
                "second name for the same inode is a second set of permissions")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            raise CredentialConfinementError(
                f"credential file {resolved} is mode {oct(mode)}; it must not "
                "be readable or writable by group or other")
        if mode not in ACCEPTED_CREDENTIAL_MODES:
            raise CredentialConfinementError(
                f"credential file {resolved} is mode {oct(mode)}; expected 0o600")
        expected = os.getuid() if expected_owner_uid is None else expected_owner_uid
        if st.st_uid != expected:
            raise CredentialConfinementError(
                f"credential file {resolved} is owned by uid {st.st_uid}, "
                f"expected uid {expected}")
        owner, group = _owner_names(st)
        facts = CredentialFileFacts(
            path=str(resolved), owner_uid=st.st_uid, owner=owner, group=group,
            mode_octal=oct(mode), parent_mode_octal=parent_mode_octal,
            inspected_at=datetime.now(timezone.utc).isoformat())
    except BaseException:
        os.close(fd)
        raise
    return fd, facts


def inspect_credential_file(
    path: str | os.PathLike,
    *,
    expected_owner_uid: int | None = None,
    forbid_repo_root: Path | None = _REPO_ROOT,
) -> CredentialFileFacts:
    """Verify confinement and return metadata. Reads no key bytes.

    The descriptor is opened (that is the only way to check the file rather
    than the name) and closed immediately without being read.
    """
    fd, facts = _open_confined(path, expected_owner_uid=expected_owner_uid,
                               forbid_repo_root=forbid_repo_root)
    os.close(fd)
    return facts


def _load_key_material(fd: int, label: str) -> rsa.RSAPrivateKey:
    """The one bounded loader. Reads the open descriptor and drops the buffer.

    Every failure is re-raised with a message built from the *path and the
    reason*, never from the file's bytes.
    """
    with os.fdopen(fd, "rb", closefd=True) as fh:
        buf = bytearray(fh.read())
    try:
        if buf.count(_PEM_BEGIN) == 0:
            raise CredentialConfinementError(
                f"credential file {label} does not contain a PEM block")
        if buf.count(_PEM_BEGIN) > 1:
            raise CredentialConfinementError(
                f"credential file {label} contains multiple PEM blocks; which "
                "key authenticates the session must not be positional luck")
        if any(marker in buf for marker in _PEM_ENCRYPTED_MARKERS):
            raise CredentialConfinementError(
                f"credential file {label} is an encrypted PEM; no reviewed "
                "password mechanism exists, and prompting or hardcoding one is "
                "not in scope for this milestone")
        try:
            key = serialization.load_pem_private_key(bytes(buf), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            # `exc` can quote the offending bytes; only its type is repeated,
            # and the context is cleared so a reporter that walks __context__
            # regardless of suppression still finds nothing.
            err = CredentialConfinementError(
                f"credential file {label} could not be parsed as an unencrypted "
                f"PEM private key ({type(exc).__name__})")
            err.__suppress_context__ = True
            raise err from None
    finally:
        # Best effort: CPython may already hold copies, but leaving the buffer
        # live in a long-running process is a needless second chance for it to
        # end up in a core dump or a --showlocals traceback.
        for i in range(len(buf)):
            buf[i] = 0

    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialConfinementError(
            f"credential file {label} holds a {type(key).__name__}; Kalshi "
            "request signing requires an RSA private key")
    _require_key_strength(key, label)
    return key


def _require_key_strength(key: rsa.RSAPrivateKey, label: str) -> None:
    if key.key_size < MIN_RSA_KEY_BITS:
        raise CredentialConfinementError(
            f"credential {label} holds a {key.key_size}-bit RSA key; the "
            f"minimum accepted is {MIN_RSA_KEY_BITS}")


class ReadOnlyRequestSigner:
    """Signs exactly one thing: the read-only Kalshi WebSocket handshake.

    There is no method parameter and no free-form path parameter, so a caller
    cannot reach a write route through this object even with a key that would
    permit one.
    """

    __slots__ = ("_sign_bytes", "_public_key", "_key_id", "_key_id_fingerprint",
                 "_environment", "_facts", "_scopes")

    def __init__(
        self,
        *,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        environment: str,
        facts: CredentialFileFacts,
        scopes: tuple,
        capability_mode: str = OBSERVE_ONLY,
    ) -> None:
        """Not a public entry point. Use `from_path`.

        `facts` is required rather than optional so that this constructor
        cannot be used as the `from_pem_bytes`/`from_env` the boundary
        disclaims: producing a `CredentialFileFacts` means a confined file was
        opened and validated. The key-strength and environment checks are
        repeated here rather than trusted from the loader, because a
        constructor that trusts its caller is not a control.
        """
        require_mode(capability_mode, OBSERVE_ONLY)
        if not isinstance(key_id, str) or not key_id.strip():
            raise CredentialError("key id must be a non-empty string")
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise CredentialError("private key must be an RSA key")
        if not isinstance(facts, CredentialFileFacts):
            raise CredentialError(
                "a verified CredentialFileFacts is required; construct through "
                "ReadOnlyRequestSigner.from_path so the credential file is "
                "actually confined")
        if environment not in ENVIRONMENTS:
            raise CredentialError(
                f"unknown environment {environment!r}; expected one of "
                f"{list(ENVIRONMENTS)}")
        _require_key_strength(private_key, facts.path)

        # The key is captured in a closure and never stored as an attribute.
        # `signer._key.sign(anything)` would otherwise bypass the method and
        # path locks in one attribute access, which makes the narrowing a
        # convention rather than a control.
        def _sign(message: bytes) -> bytes:
            return private_key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256())

        self._sign_bytes = _sign
        self._public_key = private_key.public_key()
        self._key_id = key_id
        self._key_id_fingerprint = fingerprint_key_id(key_id)
        self._environment = environment
        self._facts = facts
        self._scopes = scopes

    # --- construction ---------------------------------------------------------
    @classmethod
    def from_path(
        cls,
        *,
        key_id: str,
        credential_path: str | os.PathLike,
        environment: str,
        reported_scopes: object,
        expected_owner_uid: int | None = None,
        capability_mode: str = OBSERVE_ONLY,
    ) -> "ReadOnlyRequestSigner":
        """Load at startup from a confined path. The only supported entry point.

        There is no `from_pem_bytes` and no `from_env`: an in-memory or
        environment-variable constructor would make it possible to route the key
        through a shell, a process listing, or a container inspect output, and
        no caller in this milestone needs that.

        `reported_scopes` is required and has no default. The venue's key
        metadata must say `["read"]` before a key is loaded at all — and an
        absent scopes field halts, because Kalshi keys default to broader access
        when scopes are omitted. A default here would be the whole control:
        whatever it defaulted to would be what nobody ever passed.
        """
        scopes = verify_scopes(reported_scopes, environment=environment)
        fd, facts = _open_confined(
            credential_path, expected_owner_uid=expected_owner_uid,
            forbid_repo_root=_REPO_ROOT)
        key = _load_key_material(fd, facts.path)
        return cls(key_id=key_id, private_key=key, environment=environment,
                   capability_mode=capability_mode, facts=facts, scopes=scopes)

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
        if not _MIN_TIMESTAMP_MS <= timestamp_ms <= _MAX_TIMESTAMP_MS:
            raise CredentialError(
                f"timestamp_ms {timestamp_ms} is outside the plausible "
                "millisecond range; seconds passed as milliseconds produce an "
                "opaque handshake rejection at the venue instead of a local error")
        # `type(path) is not str` rather than isinstance: a str SUBCLASS can
        # define __eq__/__hash__ that make `in` succeed while carrying a
        # completely different route, and the signature would then authenticate
        # that route.
        if type(path) is not str or path not in READ_ONLY_PATH_ALLOWLIST:
            raise CredentialError(
                f"path {str(path)!r} is not in the reviewed read-only allowlist "
                f"{sorted(READ_ONLY_PATH_ALLOWLIST)}")
        message = canonical_signing_string(
            method=method, path=path, timestamp_ms=timestamp_ms).encode("utf-8")
        return base64.b64encode(self._sign_bytes(message)).decode("ascii")

    def websocket_headers(self, *, timestamp_ms: int) -> Mapping[str, str]:
        """Headers for the observe-only WebSocket handshake.

        There is no `path` parameter. There is exactly one signable path, so
        offering the choice would only create a way to get it wrong.
        """
        return _RedactingHeaders({
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": self._signature(
                method=SIGNING_METHOD, path=WS_PATH, timestamp_ms=timestamp_ms),
        })

    # --- introspection: metadata only ----------------------------------------
    @property
    def environment(self) -> str:
        return self._environment

    @property
    def key_id_fingerprint(self) -> str:
        return self._key_id_fingerprint

    @property
    def credential_facts(self) -> CredentialFileFacts:
        return self._facts

    @property
    def scopes(self) -> tuple:
        """The venue-reported scopes this credential was admitted under."""
        return self._scopes

    def public_key_pem(self) -> str:
        """The PUBLIC half, for offline signature verification in tests/ops.

        Named unambiguously so nobody reaches for it expecting the other one.
        """
        return self._public_key.public_bytes(
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
