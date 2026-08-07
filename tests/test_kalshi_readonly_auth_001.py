"""KALSHI-READONLY-AUTH-001 — Gate 8 signing and credential-confinement tests.

Every key here is generated inside the test, in a temporary directory, and never
leaves it. No production or demo credential is referenced, read, or required —
if this suite ever needs a real key, the confinement design has failed.
"""

from __future__ import annotations

import ast
import base64
import copy
import os
import pickle
import stat
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from app.realtime import auth as ka
from app.realtime import kalshi as kx

PKG = Path(ka.__file__).parent


# --- helpers ---------------------------------------------------------------------
def _gen(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _confined_dir(tmp_path: Path, name: str = "cred") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    os.chmod(d, 0o700)
    return d


def _install(tmp_path: Path, data: bytes, *, mode: int = 0o600,
             name: str = "kalshi.pem") -> Path:
    d = _confined_dir(tmp_path)
    p = d / name
    p.write_bytes(data)
    os.chmod(p, mode)
    return p


def _signer(tmp_path: Path, *, key=None, key_id: str = "aaaa-bbbb-cccc",
            environment: str = kx.ENV_DEMO) -> tuple[ka.ReadOnlyRequestSigner, object]:
    key = key or _gen()
    p = _install(tmp_path, _pem(key))
    s = ka.ReadOnlyRequestSigner.from_path(
        key_id=key_id, credential_path=p, environment=environment)
    return s, key


def _verify(pubkey, signature_b64: str, message: bytes) -> None:
    pubkey.verify(
        base64.b64decode(signature_b64),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


TS = 1_754_500_000_000


# --- 1-4: the signing string itself ----------------------------------------------
class TestSigningString:
    def test_1_deterministic_fixture(self):
        """The signing string is fully determined by (timestamp, method, path).

        Pinned as a literal rather than recomputed from the same helper the
        implementation uses — a test that rebuilds the value with the code under
        test only proves the code is self-consistent.
        """
        assert kx.canonical_signing_string(
            method="GET", path="/trade-api/ws/v2", timestamp_ms=TS
        ) == "1754500000000GET/trade-api/ws/v2"

    def test_2_exact_bytes(self):
        msg = kx.canonical_signing_string(
            method="GET", path=ka.WS_PATH, timestamp_ms=TS).encode("utf-8")
        assert msg == b"1754500000000GET/trade-api/ws/v2"
        # Concatenation order matters: timestamp, then METHOD, then path.
        assert msg.startswith(str(TS).encode())
        assert msg[len(str(TS)):len(str(TS)) + 3] == b"GET"

    def test_3_query_string_is_excluded(self):
        assert kx.canonical_signing_string(
            method="GET", path="/trade-api/ws/v2?ticker=KXMLB&x=1",
            timestamp_ms=TS) == "1754500000000GET/trade-api/ws/v2"

    def test_4_timestamp_must_be_integer_milliseconds(self, tmp_path):
        s, _ = _signer(tmp_path)
        for bad in ("1754500000000", 1754500000000.0, None, True):
            with pytest.raises(kx.CredentialError, match="timestamp_ms"):
                s.websocket_headers(timestamp_ms=bad)
        with pytest.raises(kx.CredentialError, match="positive"):
            s.websocket_headers(timestamp_ms=0)


# --- 5-10: method and path confinement -------------------------------------------
class TestMethodAndPathConfinement:
    def test_5_get_is_accepted(self, tmp_path):
        s, _ = _signer(tmp_path)
        h = s.websocket_headers(timestamp_ms=TS)
        assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                          "KALSHI-ACCESS-SIGNATURE"}

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_6_to_9_write_methods_rejected(self, method, tmp_path):
        """Tests 6-9. Two independent refusals, and both are asserted.

        `canonical_signing_string` refuses the method, and the signer has no
        parameter through which a caller could supply one in the first place.
        """
        with pytest.raises(kx.CapabilityError):
            kx.canonical_signing_string(method=method, path=ka.WS_PATH,
                                        timestamp_ms=TS)
        s, _ = _signer(tmp_path)
        with pytest.raises(kx.CapabilityError):
            s._signature(method=method, path=ka.WS_PATH, timestamp_ms=TS)
        assert "method" not in ka.ReadOnlyRequestSigner.websocket_headers.__code__.co_varnames

    def test_10_wrong_path_rejected(self, tmp_path):
        s, _ = _signer(tmp_path)
        for bad in ("/trade-api/v2/portfolio/orders", "/trade-api/ws/v1",
                    "/trade-api/ws/v2/", "", "/"):
            with pytest.raises(kx.CredentialError, match="allowlist"):
                s.websocket_headers(timestamp_ms=TS, path=bad)
        assert ka.READ_ONLY_PATH_ALLOWLIST == frozenset({"/trade-api/ws/v2"})

    def test_no_general_purpose_public_signer_exists(self, tmp_path):
        s, _ = _signer(tmp_path)
        assert not hasattr(s, "sign")
        public = [n for n in dir(s) if not n.startswith("_")]
        assert "websocket_headers" in public
        assert not any("sign" in n for n in public if n != "websocket_headers")


# --- 11-14: signature correctness -------------------------------------------------
class TestSignatureCorrectness:
    def test_11_signature_is_valid_base64_and_verifies(self, tmp_path):
        s, key = _signer(tmp_path)
        sig = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        base64.b64decode(sig, validate=True)
        _verify(key.public_key(), sig, b"1754500000000GET/trade-api/ws/v2")

    def test_12_wrong_private_key_fails_verification(self, tmp_path):
        s, _ = _signer(tmp_path)
        sig = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        with pytest.raises(InvalidSignature):
            _verify(_gen().public_key(), sig,
                    b"1754500000000GET/trade-api/ws/v2")

    def test_13_modified_timestamp_fails_verification(self, tmp_path):
        s, key = _signer(tmp_path)
        sig = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        with pytest.raises(InvalidSignature):
            _verify(key.public_key(), sig,
                    b"1754500000001GET/trade-api/ws/v2")

    def test_14_modified_path_fails_verification(self, tmp_path):
        s, key = _signer(tmp_path)
        sig = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        with pytest.raises(InvalidSignature):
            _verify(key.public_key(), sig,
                    b"1754500000000GET/trade-api/v2/portfolio")

    def test_pss_is_randomised_so_repeat_signatures_differ(self, tmp_path):
        """PSS is probabilistic. Two signatures over identical input must differ,
        and both must verify — a deterministic result would mean the salt is
        broken."""
        s, key = _signer(tmp_path)
        a = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        b = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        assert a != b
        for sig in (a, b):
            _verify(key.public_key(), sig, b"1754500000000GET/trade-api/ws/v2")

    def test_headers_carry_the_full_key_id_and_matching_timestamp(self, tmp_path):
        s, _ = _signer(tmp_path, key_id="kid-1234")
        h = s.websocket_headers(timestamp_ms=TS)
        assert h["KALSHI-ACCESS-KEY"] == "kid-1234"
        assert h["KALSHI-ACCESS-TIMESTAMP"] == str(TS)


# --- 15, 21: secret containment ---------------------------------------------------
class TestSecretContainment:
    def test_15_pem_never_appears_in_any_output(self, tmp_path, caplog):
        key = _gen()
        pem = _pem(key).decode()
        secret_lines = [ln for ln in pem.splitlines()
                        if ln and not ln.startswith("-----")]
        assert secret_lines
        p = _install(tmp_path, _pem(key))

        with caplog.at_level("DEBUG"):
            s = ka.ReadOnlyRequestSigner.from_path(
                key_id="kid", credential_path=p, environment=kx.ENV_DEMO)
            h = s.websocket_headers(timestamp_ms=TS)

        blobs = [repr(s), str(s), repr(s.credential_facts), caplog.text,
                 repr(h), s.public_key_pem()]
        for blob in blobs:
            for line in secret_lines:
                assert line not in blob
        # And the fingerprint is bounded, not the key id repeated in full.
        assert s.key_id_fingerprint.startswith("sha256:")
        assert "kid" not in s.key_id_fingerprint

    def test_21_exception_messages_are_secret_free(self, tmp_path):
        """A malformed PEM is the dangerous case: the underlying library is
        entitled to quote the offending bytes, and that exception text lands in
        logs and issue reports."""
        marker = "SUPERSECRETKEYMATERIAL"
        body = f"-----BEGIN PRIVATE KEY-----\n{marker}\n-----END PRIVATE KEY-----\n"
        p = _install(tmp_path, body.encode())
        with pytest.raises(ka.CredentialConfinementError) as ei:
            ka.ReadOnlyRequestSigner.from_path(
                key_id="kid", credential_path=p, environment=kx.ENV_DEMO)
        text = f"{ei.value}{ei.value.__cause__}{ei.value.__context__}"
        assert marker not in text
        assert ei.value.__cause__ is None  # `from None`: no chained original

    def test_signer_cannot_be_pickled_copied_or_serialised(self, tmp_path):
        s, _ = _signer(tmp_path)
        with pytest.raises(TypeError):
            pickle.dumps(s)
        with pytest.raises(TypeError):
            copy.copy(s)
        with pytest.raises(TypeError):
            copy.deepcopy(s)

    def test_signer_has_no_dict_to_leak_through(self, tmp_path):
        s, _ = _signer(tmp_path)
        assert not hasattr(s, "__dict__")  # __slots__


# --- 16-20: loader confinement ----------------------------------------------------
class TestLoaderConfinement:
    def test_16_symlink_rejected(self, tmp_path):
        real = _install(tmp_path, _pem(_gen()))
        link = real.parent / "link.pem"
        link.symlink_to(real)
        with pytest.raises(ka.CredentialConfinementError, match="symlink"):
            ka.inspect_credential_file(link)

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604, 0o666, 0o700])
    def test_17_permissive_mode_rejected(self, mode, tmp_path):
        p = _install(tmp_path, _pem(_gen()), mode=mode)
        with pytest.raises(ka.CredentialConfinementError, match="mode"):
            ka.inspect_credential_file(p)

    def test_17b_tighter_than_required_is_accepted(self, tmp_path):
        p = _install(tmp_path, _pem(_gen()), mode=0o400)
        assert ka.inspect_credential_file(p).mode_octal == "0o400"

    def test_17c_permissive_parent_directory_rejected(self, tmp_path):
        p = _install(tmp_path, _pem(_gen()))
        os.chmod(p.parent, 0o755)
        try:
            with pytest.raises(ka.CredentialConfinementError, match="directory"):
                ka.inspect_credential_file(p)
        finally:
            os.chmod(p.parent, 0o700)

    def test_18_wrong_owner_rejected(self, tmp_path):
        p = _install(tmp_path, _pem(_gen()))
        with pytest.raises(ka.CredentialConfinementError, match="owned by uid"):
            ka.inspect_credential_file(p, expected_owner_uid=os.getuid() + 4242)
        assert ka.inspect_credential_file(
            p, expected_owner_uid=os.getuid()).owner_uid == os.getuid()

    def test_19_malformed_key_rejected(self, tmp_path):
        for body in (b"", b"not a pem at all\n",
                     b"-----BEGIN PRIVATE KEY-----\nzzzz\n-----END PRIVATE KEY-----\n",
                     _pem(_gen())[:-40]):
            p = _install(tmp_path / os.urandom(4).hex(), body)
            with pytest.raises(ka.CredentialConfinementError):
                ka.ReadOnlyRequestSigner.from_path(
                    key_id="kid", credential_path=p, environment=kx.ENV_DEMO)

    def test_20_unsupported_key_types_rejected(self, tmp_path):
        ecp = _install(tmp_path / "ec", _pem(ec.generate_private_key(ec.SECP256R1())))
        with pytest.raises(ka.CredentialConfinementError, match="requires an RSA"):
            ka.ReadOnlyRequestSigner.from_path(
                key_id="kid", credential_path=ecp, environment=kx.ENV_DEMO)

    def test_20b_weak_rsa_key_rejected(self, tmp_path):
        p = _install(tmp_path / "weak", _pem(_gen(1024)))
        with pytest.raises(ka.CredentialConfinementError, match="minimum accepted"):
            ka.ReadOnlyRequestSigner.from_path(
                key_id="kid", credential_path=p, environment=kx.ENV_DEMO)
        assert ka.MIN_RSA_KEY_BITS == 2048

    def test_20c_encrypted_pem_rejected_before_any_prompt(self, tmp_path):
        enc = _gen().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"pw"),
        )
        p = _install(tmp_path / "enc", enc)
        with pytest.raises(ka.CredentialConfinementError, match="encrypted"):
            ka.ReadOnlyRequestSigner.from_path(
                key_id="kid", credential_path=p, environment=kx.ENV_DEMO)

    def test_20d_multiple_pem_blocks_rejected(self, tmp_path):
        p = _install(tmp_path / "multi", _pem(_gen()) + _pem(_gen()))
        with pytest.raises(ka.CredentialConfinementError, match="multiple PEM"):
            ka.ReadOnlyRequestSigner.from_path(
                key_id="kid", credential_path=p, environment=kx.ENV_DEMO)

    def test_relative_path_and_missing_file_rejected(self, tmp_path):
        with pytest.raises(ka.CredentialConfinementError, match="relative"):
            ka.inspect_credential_file("kalshi.pem")
        with pytest.raises(ka.CredentialConfinementError, match="not found"):
            ka.inspect_credential_file(tmp_path / "nope.pem")

    def test_directory_rejected(self, tmp_path):
        d = _confined_dir(tmp_path, "adir")
        with pytest.raises(ka.CredentialConfinementError, match="regular file"):
            ka.inspect_credential_file(d)

    def test_credential_inside_the_repository_is_rejected(self, tmp_path):
        """A key under the repo root is one `git add -A` from being published."""
        p = _install(tmp_path, _pem(_gen()))
        with pytest.raises(ka.CredentialConfinementError, match="inside the repository"):
            ka.inspect_credential_file(p, forbid_repo_root=tmp_path)


# --- capability and boundary enforcement ------------------------------------------
class TestCapabilityBoundary:
    def test_signer_refuses_any_mode_but_observe_only(self, tmp_path):
        p = _install(tmp_path, _pem(_gen()))
        for mode in (kx.SHADOW_ONLY, kx.DEMO_EXECUTION, kx.LIVE_BOUNDED):
            with pytest.raises(kx.CapabilityError):
                ka.ReadOnlyRequestSigner.from_path(
                    key_id="kid", credential_path=p, environment=kx.ENV_DEMO,
                    capability_mode=mode)

    def test_no_bytes_constructor_or_env_constructor_exists(self):
        """The only way in is a confined file path.

        A `from_pem_bytes` or `from_env` would let the key travel through a
        shell command, a process listing, or a `docker inspect` — all of which
        are readable by parties the file mode deliberately excludes.
        """
        ctors = [n for n in dir(ka.ReadOnlyRequestSigner)
                 if n.startswith("from_")]
        assert ctors == ["from_path"]

    def test_auth_module_imports_no_trading_or_wallet_surface(self):
        tree = ast.parse((PKG / "auth.py").read_text())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        for banned in ("solana", "solders", "web3", "eth_account", "anchorpy"):
            assert not any(m.startswith(banned) for m in mods), banned
        # Only the key-free half of the observer, plus cryptography and stdlib.
        assert all(not m.startswith("app.") or m == "app.realtime.kalshi"
                   for m in mods), mods

    def test_no_transaction_or_order_signing_identifier_exists(self):
        """The amended boundary permits request signing only. Transaction,
        order and wallet signing stay prohibited, and the amendment is worth
        nothing if it is not enforced narrowly."""
        tree = ast.parse((PKG / "auth.py").read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                names.add(node.name)
        for banned in ("sign_transaction", "send_transaction", "sign_order",
                       "wallet", "keypair", "place_order", "create_order",
                       "cancel_order", "portfolio"):
            assert banned not in names, banned

    def test_signing_is_confined_to_the_websocket_handshake(self):
        """`_signature` must be reachable only with the allowlisted path.

        Asserted structurally: every literal path in the module is either the
        WebSocket path or not a path at all.
        """
        tree = ast.parse((PKG / "auth.py").read_text())
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        routes = {v for v in literals if v.startswith("/trade-api")}
        assert routes <= {"/trade-api/ws/v2"}, routes


# --- the pre-existing broader signer ----------------------------------------------
class TestPreExistingSignerIsNotTheObserverPath:
    def test_ws_snapshots_signer_is_dormant_and_separate(self):
        """`app/services/ws_snapshots.py` has carried an RSA-PSS signer since
        long before this milestone, gated on `Settings.ws_enabled`.

        It is recorded here rather than refactored: it is out of this
        milestone's scope, but a reader comparing the two must not conclude that
        `auth.py` is the only signing surface in the repository.
        """
        from app.config import Settings

        assert Settings(kalshi_api_key_id="", kalshi_private_key_path="").ws_enabled is False
        assert Settings(kalshi_api_key_id="k", kalshi_private_key_path="").ws_enabled is False

    def test_observer_package_does_not_import_ws_snapshots(self):
        for path in sorted(PKG.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "ws_snapshots" not in node.module, path
