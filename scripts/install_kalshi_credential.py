#!/usr/bin/env python3
"""Install a Kalshi credential on this host WITHOUT it passing through a
terminal echo, a shell history, an argv, or a process list.

The secret arrives on **stdin only**. Nothing secret is ever printed: the
confirmations below are length, mode, and a SHA-256 fingerprint — never the
value. Run this ON the host that will hold the credential.

    # the private key (multi-line PEM), from the clipboard:
    pbpaste | ssh <host> 'cd ~/projects/probability-arena && python3 scripts/install_kalshi_credential.py --field key --env production'

    # the API key id (single line), from the clipboard:
    pbpaste | ssh <host> 'cd ~/projects/probability-arena && python3 scripts/install_kalshi_credential.py --field id --env production'

Why stdin: an argv value is visible in `ps` to every user on the box for the
lifetime of the process, and a typed value lands in shell history. Neither is
recoverable after the fact, so the safe shape has to be the only shape offered.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import sys

SECRETS_DIR = pathlib.Path.home() / ".config" / "pa-secrets"
ENV_FILE = pathlib.Path.home() / "projects" / "probability-arena" / ".env"
ID_VAR = "KALSHI_OBSERVER_API_KEY_ID"
PATH_VAR = "KALSHI_OBSERVER_CREDENTIAL_PATH"


def fingerprint(data: bytes) -> str:
    """A stable identifier for a secret that is not the secret."""
    return hashlib.sha256(data).hexdigest()[:16]


def install_key(env: str) -> int:
    pem = sys.stdin.buffer.read()
    if not pem.strip():
        print("REFUSED: stdin was empty. Nothing written.", file=sys.stderr)
        return 2
    # Validate BEFORE writing. A truncated paste otherwise surfaces later as a
    # mysterious handshake failure, which is expensive to diagnose and easy to
    # blame on the venue.
    text = pem.decode("utf-8", errors="replace")
    if "-----BEGIN" not in text or "-----END" not in text:
        print("REFUSED: that does not look like a PEM (no BEGIN/END armour). "
              "Nothing written.", file=sys.stderr)
        return 2
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(pem, password=None)
        bits = getattr(key, "key_size", None)
    except ImportError:
        bits = None            # validation unavailable, not a failure
    except Exception as exc:   # noqa: BLE001 - any parse failure is a refusal
        print(f"REFUSED: PEM did not parse as a private key "
              f"({type(exc).__name__}). Nothing written.", file=sys.stderr)
        return 2

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)
    dest = SECRETS_DIR / f"kalshi-{env}.pem"
    # Create 0600 from the outset rather than writing then chmod'ing, so the
    # bytes are never on disk world-readable even briefly.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    os.chmod(dest, 0o600)

    st = dest.stat()
    print(f"installed  : {dest}")
    print(f"bytes      : {st.st_size}")
    print(f"mode       : {oct(st.st_mode & 0o777)}")
    print(f"fingerprint: sha256:{fingerprint(pem)}   (not the key)")
    if bits:
        print(f"parsed     : private key OK, {bits}-bit")
    else:
        print("parsed     : armour OK (cryptography unavailable for deep check)")
    print("\nNEXT: install the api key id, then repoint .env with --field id")
    return 0


def install_id(env: str) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("REFUSED: stdin was empty. Nothing written.", file=sys.stderr)
        return 2
    if "\n" in raw or len(raw.split()) != 1:
        print("REFUSED: the api key id must be a single token. Nothing written.",
              file=sys.stderr)
        return 2
    dest = SECRETS_DIR / f"kalshi-{env}.pem"
    if not dest.exists():
        print(f"REFUSED: {dest} does not exist yet — install the key first, so "
              "the two halves are never out of step.", file=sys.stderr)
        return 2
    if not ENV_FILE.exists():
        print(f"REFUSED: {ENV_FILE} not found.", file=sys.stderr)
        return 2

    s = ENV_FILE.read_text()
    before = s
    s = re.sub(rf"^{ID_VAR}=.*$", f"{ID_VAR}={raw}", s, flags=re.M)
    s = re.sub(rf"^{PATH_VAR}=.*$", f"{PATH_VAR}={dest}", s, flags=re.M)
    if s == before:
        print(f"REFUSED: neither {ID_VAR} nor {PATH_VAR} was found in .env.",
              file=sys.stderr)
        return 2
    # Preserve .env's own mode; it holds secrets.
    mode = ENV_FILE.stat().st_mode & 0o777
    ENV_FILE.write_text(s)
    os.chmod(ENV_FILE, mode)

    print(f"{ID_VAR}    : set, length {len(raw)}, "
          f"fingerprint sha256:{fingerprint(raw.encode())}   (not the id)")
    print(f"{PATH_VAR}: {dest}")
    print(f".env mode  : {oct(mode)} (unchanged)")
    print("\nThe DEMO credential was NOT deleted; reverting is a two-line .env edit.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", required=True, choices=("key", "id"))
    ap.add_argument("--env", required=True, choices=("production", "demo"))
    a = ap.parse_args()
    if sys.stdin.isatty():
        print("REFUSED: refusing to read a secret from a terminal — pipe it in, "
              "so it never reaches your shell history.", file=sys.stderr)
        return 2
    return install_key(a.env) if a.field == "key" else install_id(a.env)


if __name__ == "__main__":
    raise SystemExit(main())
