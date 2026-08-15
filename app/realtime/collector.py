"""KALSHI-LIVE-TAPE-COLLECTOR-001 — the session orchestrator.

**CP2 scope: credential wiring only.** The transport, the read loop, the
reconnect driver and the measurement lane arrive in later checkpoints. What is
here is the one thing section 6.5 of the milestone says must exist in exactly
one place: the path from the two documented observer environment variables to
`ReadOnlyRequestSigner.from_path`.

Before this module the signer was orphaned — `from_path` had no caller anywhere
in `app/`, and the DEMO session recorded in
`docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md` was therefore opened by an
out-of-repo script. A credential loader that lives outside the repository is a
credential loader nobody reviews.

Three properties are load-bearing and are asserted structurally in
`tests/test_kalshi_live_tape_cp2_001.py`:

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

from app.config import get_settings
from app.realtime.auth import ReadOnlyRequestSigner
from app.realtime.kalshi import ENVIRONMENTS, CredentialError

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
