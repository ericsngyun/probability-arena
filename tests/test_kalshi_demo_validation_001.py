"""KALSHI-DEMO-READONLY-VALIDATION-001 — Gates 3, 4 (credential-independent).

No credential, no socket, no provider call. Keys are generated per test.

Gates 5-15 need a demo credential and are NOT covered here: this file is the
part of the milestone that could be closed before a key exists.
"""

from __future__ import annotations

import ast
import base64
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.realtime import auth as ka
from app.realtime import credential_audit as ca
from app.realtime import kalshi as kx

PKG = Path(ka.__file__).parent
REPO = PKG.parent.parent
TS = 1_754_500_000_000
ALL_PURPOSES = frozenset(kx.AuthPurpose)


def _params(fn) -> tuple:
    """Parameter names only.

    `co_varnames` also lists local variables, so a function that *derives*
    `method` and `path` from a purpose appears to accept them. That would make
    the assertion pass or fail for the wrong reason.
    """
    code = fn.__code__
    return code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]



def _gen(bits: int = 2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _install(tmp_path: Path, key) -> Path:
    d = tmp_path / "cred"
    d.mkdir(parents=True)
    os.chmod(d, 0o700)
    p = d / "demo.pem"
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    os.chmod(p, 0o600)
    return p


_UNSET = object()


def _signer(tmp_path, *, purposes=_UNSET, key_id="demo-key-1", key=None):
    key = key or _gen()
    p = _install(tmp_path, key)
    # `is _UNSET`, not `or`: an empty frozenset is falsy, and `purposes or
    # default` silently substituted the default for exactly the input the
    # empty-set test was trying to exercise.
    if purposes is _UNSET:
        purposes = frozenset({kx.AuthPurpose.WEBSOCKET_HANDSHAKE})
    s = ka.ReadOnlyRequestSigner.from_path(
        key_id=key_id, credential_path=p, environment=kx.ENV_DEMO,
        reported_scopes=["read"], purposes=purposes)
    return s, key


def _verify(pub, sig_b64, message: bytes):
    pub.verify(base64.b64decode(sig_b64), message,
               padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                           salt_length=padding.PSS.DIGEST_LENGTH),
               hashes.SHA256())


# --- Gate 4: signing parity --------------------------------------------------------
class TestSigningParity:
    def test_websocket_handshake_signing_string(self, tmp_path):
        s, key = _signer(tmp_path)
        sig = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        _verify(key.public_key(), sig, b"1754500000000GET/trade-api/ws/v2")

    def test_api_key_metadata_signing_string(self, tmp_path):
        s, key = _signer(tmp_path, purposes=ALL_PURPOSES)
        sig = s.headers_for(purpose=kx.AuthPurpose.API_KEY_METADATA,
                            timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        _verify(key.public_key(), sig, b"1754500000000GET/trade-api/v2/api_keys")

    def test_no_query_parameters_enter_the_signed_path(self):
        for _method, path in kx.AUTH_PURPOSE_ROUTES.values():
            assert "?" not in path
        assert kx.canonical_signing_string(
            method="GET", path="/trade-api/v2/api_keys?limit=100",
            timestamp_ms=TS) == "1754500000000GET/trade-api/v2/api_keys"

    def test_signature_is_randomised_and_both_verify(self, tmp_path):
        s, key = _signer(tmp_path)
        a = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        b = s.websocket_headers(timestamp_ms=TS)["KALSHI-ACCESS-SIGNATURE"]
        assert a != b                      # PSS salt
        for sig in (a, b):
            _verify(key.public_key(), sig, b"1754500000000GET/trade-api/ws/v2")


# --- Gate 4: the typed design cannot sign anything else -----------------------------
class TestTypedPurposeCannotSignArbitraryRequests:
    def test_there_is_no_method_or_path_parameter_anywhere_public(self, tmp_path):
        s, _ = _signer(tmp_path)
        for name in ("websocket_headers", "headers_for", "_signature"):
            names = _params(getattr(ka.ReadOnlyRequestSigner, name))
            assert "method" not in names, name
            assert "path" not in names, name
            assert "url" not in names, name
        assert not hasattr(s, "sign")
        # `from_path` is the one constructor; nothing else builds a signer.
        assert [n for n in dir(s) if n.startswith("from_")] == ["from_path"]

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def test_no_write_method_is_reachable(self, method):
        # Not merely rejected — absent. Every route in the closed table is GET.
        assert all(m == "GET" for m, _p in kx.AUTH_PURPOSE_ROUTES.values())
        assert kx.ALLOWED_HTTP_METHODS == ("GET",)
        with pytest.raises(kx.CapabilityError):
            kx.canonical_signing_string(method=method, path=kx.WS_PATH,
                                        timestamp_ms=TS)

    @pytest.mark.parametrize("route", [
        "/trade-api/v2/portfolio/orders",
        "/trade-api/v2/portfolio/balance",
        "/trade-api/v2/portfolio/positions",
        "/trade-api/v2/portfolio/fills",
        "/trade-api/v2/exchange/schedule",
        "/",
        "",
    ])
    def test_no_arbitrary_path_is_reachable(self, route):
        """There is no parameter through which any of these could arrive."""
        assert route not in ka.READ_ONLY_PATH_ALLOWLIST
        assert route not in {p for _m, p in kx.AUTH_PURPOSE_ROUTES.values()}

    def test_a_purpose_cannot_be_supplied_as_a_string(self, tmp_path):
        s, _ = _signer(tmp_path, purposes=ALL_PURPOSES)
        for bogus in ("api_key_metadata", "websocket_handshake", 0, None):
            with pytest.raises(kx.CapabilityError):
                kx.route_for_purpose(bogus)
            with pytest.raises((kx.CapabilityError, kx.CredentialError)):
                s.headers_for(purpose=bogus, timestamp_ms=TS)

    def test_the_observer_signer_cannot_sign_metadata(self, tmp_path):
        """The load-bearing separation: the continuous observer holds the
        handshake purpose only, so it cannot reach a REST route even though the
        same credential would be accepted there."""
        s, _ = _signer(tmp_path)
        assert s.granted_purposes == frozenset(
            {kx.AuthPurpose.WEBSOCKET_HANDSHAKE})
        with pytest.raises(kx.CredentialError, match="not granted"):
            s.headers_for(purpose=kx.AuthPurpose.API_KEY_METADATA,
                          timestamp_ms=TS)

    def test_purposes_cannot_be_widened_after_construction(self, tmp_path):
        s, _ = _signer(tmp_path)
        assert isinstance(s.granted_purposes, frozenset)
        with pytest.raises(AttributeError):
            s.granted_purposes = ALL_PURPOSES
        with pytest.raises((AttributeError, TypeError)):
            s._purposes.add(kx.AuthPurpose.API_KEY_METADATA)

    def test_an_empty_or_bogus_purpose_set_is_refused(self, tmp_path):
        for bad in (frozenset(), frozenset({"websocket_handshake"})):
            with pytest.raises(kx.CredentialError, match="purposes"):
                _signer(tmp_path / os.urandom(4).hex(), purposes=bad)

    def test_the_route_table_is_closed_and_pinned(self):
        """Adding a signable route is a boundary change, visible in a diff."""
        assert kx.AUTH_PURPOSE_ROUTES == {
            kx.AuthPurpose.WEBSOCKET_HANDSHAKE: ("GET", "/trade-api/ws/v2"),
            kx.AuthPurpose.API_KEY_METADATA: ("GET", "/trade-api/v2/api_keys"),
        }
        # The allowlist is DERIVED from the table, so the two cannot drift.
        assert ka.READ_ONLY_PATH_ALLOWLIST == frozenset(
            {"/trade-api/ws/v2", "/trade-api/v2/api_keys"})


# --- Gate 3: scope audit verdicts ---------------------------------------------------
class TestScopeAudit:
    def _fetch(self, body):
        def fetch(path, headers):
            assert path == "/trade-api/v2/api_keys"
            assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                                    "KALSHI-ACCESS-SIGNATURE"}
            return body
        return fetch

    def _audit(self, tmp_path, body, key_id="demo-key-1"):
        s, _ = _signer(tmp_path, purposes=ALL_PURPOSES, key_id=key_id)
        return ca.audit_scopes(signer=s, key_id=key_id, environment=kx.ENV_DEMO,
                               fetch=self._fetch(body), timestamp_ms=TS)

    def test_exactly_read_is_proven(self, tmp_path):
        out = self._audit(tmp_path, {"api_keys": [
            {"api_key_id": "demo-key-1", "scopes": ["read"]}]})
        assert out.proven_read_only is True
        assert out.scopes == ("read",)
        assert out.environment == "demo"
        assert out.key_id_fingerprint.startswith("sha256:")
        assert "demo-key-1" not in repr(out)      # fingerprint, not the id

    @pytest.mark.parametrize("scopes", [
        ["write"], ["read", "write"], [], ["READ"], ["read", "read"],
        ["read", "trade"], "read", None, {"read": True},
    ])
    def test_anything_but_exactly_read_halts(self, scopes, tmp_path):
        with pytest.raises(kx.CredentialError, match=ca.HALT_NOT_PROVEN):
            self._audit(tmp_path, {"api_keys": [
                {"api_key_id": "demo-key-1", "scopes": scopes}]})

    def test_missing_scopes_field_halts(self, tmp_path):
        """Omission is not 'read'. Kalshi keys default to broader access when
        scopes are omitted, so silence must not read as permission."""
        with pytest.raises(kx.CredentialError, match="omission is not"):
            self._audit(tmp_path, {"api_keys": [{"api_key_id": "demo-key-1"}]})

    def test_key_absent_from_the_response_halts(self, tmp_path):
        """Either the wrong credential is installed or the wrong account
        answered. Neither is safe to proceed from."""
        with pytest.raises(kx.CredentialError, match="does not appear"):
            self._audit(tmp_path, {"api_keys": [
                {"api_key_id": "some-other-key", "scopes": ["read"]}]})

    @pytest.mark.parametrize("body", [
        {}, {"api_keys": "nope"}, [], "not-json", None, {"api_keys": [None]},
    ])
    def test_malformed_metadata_halts(self, body, tmp_path):
        with pytest.raises(kx.CredentialError, match=ca.HALT_NOT_PROVEN):
            self._audit(tmp_path, body)

    def test_request_failure_halts_and_leaks_nothing(self, tmp_path):
        s, _ = _signer(tmp_path, purposes=ALL_PURPOSES)

        def boom(path, headers):
            raise RuntimeError(f"connection to {path} with {headers} failed")

        with pytest.raises(kx.CredentialError) as ei:
            ca.audit_scopes(signer=s, key_id="demo-key-1",
                            environment=kx.ENV_DEMO, fetch=boom, timestamp_ms=TS)
        # Only the exception TYPE is repeated: the original can carry the URL.
        assert "RuntimeError" in str(ei.value)
        assert "KALSHI-ACCESS" not in str(ei.value)

    def test_a_handshake_only_signer_cannot_run_the_audit(self, tmp_path):
        s, _ = _signer(tmp_path)
        with pytest.raises(kx.CredentialError, match="not granted"):
            ca.audit_scopes(signer=s, key_id="demo-key-1",
                            environment=kx.ENV_DEMO,
                            fetch=self._fetch({}), timestamp_ms=TS)


# --- structure ---------------------------------------------------------------------
class TestAuditModuleIsNotPartOfTheCollector:
    def test_no_observer_module_imports_the_audit(self):
        for path in sorted(PKG.rglob("*.py")):
            if path.name == "credential_audit.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "credential_audit" not in node.module, path

    def test_the_audit_implements_no_transport(self):
        tree = ast.parse((PKG / "credential_audit.py").read_text())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        for banned in ("httpx", "requests", "aiohttp", "urllib", "socket",
                       "websockets", "http"):
            assert not any(m == banned or m.startswith(banned + ".")
                           for m in mods), banned

    def test_demo_and_production_hosts_are_distinct_and_labelled(self):
        assert kx.WS_HOSTS[kx.ENV_DEMO] != kx.WS_HOSTS[kx.ENV_PRODUCTION]
        assert kx.REST_HOSTS[kx.ENV_DEMO] != kx.REST_HOSTS[kx.ENV_PRODUCTION]
        assert "demo" in kx.WS_HOSTS[kx.ENV_DEMO]
        assert "demo" in kx.REST_HOSTS[kx.ENV_DEMO]
        assert "demo" not in kx.WS_HOSTS[kx.ENV_PRODUCTION]
        assert "demo" not in kx.REST_HOSTS[kx.ENV_PRODUCTION]

    def test_still_no_transport_anywhere_in_the_observer(self):
        for path in sorted(PKG.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module}
                else:
                    continue
                for banned in ("httpx", "requests", "aiohttp", "websockets"):
                    assert not any(n == banned or n.startswith(banned + ".")
                                   for n in names), (path, banned)
