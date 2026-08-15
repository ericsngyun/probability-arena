"""KALSHI-LIVE-TAPE-COLLECTOR-001 CP2 — credential loading, offline.

Zero network, zero live session, zero real credentials. Every key here is
generated inside the test in a temporary directory and never leaves it — if
this suite ever needs the DEMO key on the EVO host, the confinement design has
failed.

What CP2 has to prove, and what each class below proves:

* the loader refuses cleanly and typed when either documented variable is
  absent, and when the file is present but not confined (`TestRefusals`,
  `TestFileConfinement`);
* nothing it raises can carry the contents of the credential file or the key id
  (`TestRefusalsCarryNoSecrets`);
* no code path through it can yield an `UnsignedTransportSigner`, argued
  structurally rather than by convention (`TestNoUnsignedFallback`);
* `app/realtime/auth.py` is still the only file in the repository that holds key
  material, with the new module included in the scan (`TestKeyMaterialStaysInAuth`).
"""

from __future__ import annotations

import ast
import os
import traceback
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.realtime import auth as ka
from app.realtime import collector as kc
from app.realtime import kalshi as kx

PKG = Path(kc.__file__).parent
REPO = PKG.parent.parent
COLLECTOR_SRC = ast.parse((PKG / "collector.py").read_text())

# Written INTO the credential file so that any leak of its bytes into an
# exception, its chain, or a formatted traceback is detectable by one search.
FILE_MARKER = "MARKER-c0ffee-file-contents-must-never-be-quoted"
KEY_ID_MARKER = "MARKER-cafe-key-id-must-never-be-quoted"


# --- helpers ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Every test starts and ends with a cold settings cache.

    `get_settings` is `lru_cache`d, so a test that sets the observer variables
    would otherwise hand its configuration to whatever ran next — including
    suites in other files.
    """
    monkeypatch.delenv("KALSHI_OBSERVER_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_OBSERVER_CREDENTIAL_PATH", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch, *, key_id: str | None, credential_path: str | None):
    """Set the two documented variables in the real environment.

    Deliberately `setenv` rather than patching `Settings`: the point of CP2 is
    that these exact variable names are what the loader reads, and a test that
    injects a `Settings` object proves only that the injection worked.
    """
    if key_id is None:
        monkeypatch.delenv("KALSHI_OBSERVER_API_KEY_ID", raising=False)
    else:
        monkeypatch.setenv("KALSHI_OBSERVER_API_KEY_ID", key_id)
    if credential_path is None:
        monkeypatch.delenv("KALSHI_OBSERVER_CREDENTIAL_PATH", raising=False)
    else:
        monkeypatch.setenv("KALSHI_OBSERVER_CREDENTIAL_PATH", credential_path)
    get_settings.cache_clear()


def _pem_bytes(bits: int = 2048) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def _install(tmp_path: Path, data: bytes, *, mode: int = 0o600,
             dir_mode: int = 0o700, name: str = "kalshi.pem") -> Path:
    d = tmp_path / "cred"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(data)
    os.chmod(p, mode)
    os.chmod(d, dir_mode)
    return p


def _load(**kwargs):
    kwargs.setdefault("environment", kx.ENV_DEMO)
    kwargs.setdefault("reported_scopes", ["read"])
    return kc.load_observer_signer(**kwargs)


def _loader_ast() -> ast.FunctionDef:
    fns = [n for n in ast.walk(COLLECTOR_SRC)
           if isinstance(n, ast.FunctionDef) and n.name == "load_observer_signer"]
    assert len(fns) == 1, "there must be exactly one loader definition"
    return fns[0]


def _chain_text(exc: BaseException) -> str:
    """Everything a reporter could plausibly print, in one string.

    Not just `str(exc)`: `__cause__`/`__context__` are walked because a
    `raise ... from` (or an un-suppressed context) puts the original library
    exception — which is the one that can quote the offending bytes — into the
    printed traceback.
    """
    parts = ["".join(traceback.format_exception(type(exc), exc, exc.__traceback__))]
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.extend([str(cur), repr(cur), repr(getattr(cur, "args", ()))])
        cur = cur.__cause__ or cur.__context__
    return "\n".join(parts)


# --- 1-6: refusals when the credential is not configured --------------------------
class TestRefusals:
    def test_1_absent_key_id_refuses_typed(self, tmp_path, monkeypatch):
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id=None, credential_path=str(p))
        with pytest.raises(kc.ObserverCredentialUnavailable) as e:
            _load()
        assert kc.OBSERVER_KEY_ID_VAR in str(e.value)
        assert kc.OBSERVER_CREDENTIAL_PATH_VAR not in str(e.value)
        # A refusal is an exception, not a value a caller can ignore.
        assert isinstance(e.value, kx.CredentialError)
        assert e.value.status == kc.REFUSED_NO_CREDENTIAL

    def test_2_absent_credential_path_refuses_typed(self, monkeypatch):
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=None)
        with pytest.raises(kc.ObserverCredentialUnavailable) as e:
            _load()
        assert kc.OBSERVER_CREDENTIAL_PATH_VAR in str(e.value)
        assert kc.OBSERVER_KEY_ID_VAR not in str(e.value)
        assert e.value.status == kc.REFUSED_NO_CREDENTIAL

    def test_3_both_absent_names_both(self, monkeypatch):
        _configure(monkeypatch, key_id=None, credential_path=None)
        with pytest.raises(kc.ObserverCredentialUnavailable) as e:
            _load()
        assert kc.OBSERVER_KEY_ID_VAR in str(e.value)
        assert kc.OBSERVER_CREDENTIAL_PATH_VAR in str(e.value)

    def test_4_blank_and_whitespace_are_absent(self, tmp_path, monkeypatch):
        """`KALSHI_OBSERVER_API_KEY_ID=` in an `.env` file is not a key id, and
        neither is a value that is entirely whitespace — both are the shape a
        half-finished deployment leaves behind."""
        p = _install(tmp_path, _pem_bytes())
        for blank in ("", "   ", "\t\n"):
            _configure(monkeypatch, key_id=blank, credential_path=str(p))
            with pytest.raises(kc.ObserverCredentialUnavailable):
                _load()
            _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=blank)
            with pytest.raises(kc.ObserverCredentialUnavailable):
                _load()

    def test_5_unknown_environment_refuses_before_the_file_is_touched(
            self, tmp_path, monkeypatch):
        """Ordering matters: a mistyped environment must not cause a key file to
        be opened and read on the way to being rejected."""
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))

        def _explode(**kwargs):  # pragma: no cover - must never run
            raise AssertionError("from_path was reached with a bad environment")

        monkeypatch.setattr(ka.ReadOnlyRequestSigner, "from_path", _explode)
        with pytest.raises(kc.ObserverCredentialUnavailable) as e:
            _load(environment="prod")
        assert "unknown environment" in str(e.value)

    def test_6_refusal_is_never_a_none_return(self, monkeypatch):
        """The failure mode this replaces is a loader that returns `None` and a
        caller that forgets to check — where what goes unchecked is whether the
        session it opened was authenticated."""
        _configure(monkeypatch, key_id=None, credential_path=None)
        try:
            result = _load()
        except kc.ObserverCredentialUnavailable:
            result = "raised"
        assert result == "raised"
        assert kc.ObserverCredentialUnavailable.status == "refused_no_credential"
        assert issubclass(kc.ObserverCredentialUnavailable, kx.CredentialError)


# --- 7-11: the confinement contract is auth.py's, unchanged and not weakened ------
class TestFileConfinement:
    def test_7_group_readable_file_mode_refuses(self, tmp_path, monkeypatch):
        p = _install(tmp_path, _pem_bytes(), mode=0o644)
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        with pytest.raises(ka.CredentialConfinementError) as e:
            _load()
        assert "0o644" in str(e.value)
        assert isinstance(e.value, kx.CredentialError)

    def test_8_group_readable_directory_refuses(self, tmp_path, monkeypatch):
        p = _install(tmp_path, _pem_bytes(), dir_mode=0o750)
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        with pytest.raises(ka.CredentialConfinementError):
            _load()

    def test_9_missing_file_refuses(self, tmp_path, monkeypatch):
        d = tmp_path / "cred"
        d.mkdir()
        os.chmod(d, 0o700)
        _configure(monkeypatch, key_id="aaaa-bbbb",
                   credential_path=str(d / "absent.pem"))
        with pytest.raises(ka.CredentialConfinementError) as e:
            _load()
        assert "not found" in str(e.value)

    def test_10_symlinked_path_refuses(self, tmp_path, monkeypatch):
        real = _install(tmp_path, _pem_bytes())
        link = tmp_path / "cred" / "link.pem"
        link.symlink_to(real)
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(link))
        with pytest.raises(ka.CredentialConfinementError):
            _load()

    def test_11_relative_path_refuses(self, monkeypatch):
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path="cred/k.pem")
        with pytest.raises(ka.CredentialConfinementError) as e:
            _load()
        assert "relative" in str(e.value)


# --- 12-15: nothing raised may carry the file's bytes or the key id ---------------
class TestRefusalsCarryNoSecrets:
    def test_12_malformed_credential_never_quotes_the_file(self, tmp_path,
                                                           monkeypatch):
        p = _install(tmp_path, f"not a pem at all {FILE_MARKER}\n".encode())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        with pytest.raises(kx.CredentialError) as e:
            _load()
        assert FILE_MARKER not in _chain_text(e.value)

    def test_13_unparseable_pem_body_never_quotes_the_file(self, tmp_path,
                                                           monkeypatch):
        """The dangerous case: a file that LOOKS like a PEM, so the loader hands
        it to `cryptography`, whose own exceptions can quote the offending
        bytes. The context is suppressed for exactly this reason."""
        body = ("-----BEGIN PRIVATE KEY-----\n"
                f"{FILE_MARKER}\n"
                "-----END PRIVATE KEY-----\n")
        p = _install(tmp_path, body.encode())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        with pytest.raises(kx.CredentialError) as e:
            _load()
        text = _chain_text(e.value)
        assert FILE_MARKER not in text
        # The path and the reason are what a failure is allowed to say.
        assert "could not be parsed" in text

    def test_14_wrong_key_type_never_quotes_the_file(self, tmp_path, monkeypatch):
        """An EC key in the file is a real operator mistake; the refusal must
        name the type, not the material."""
        from cryptography.hazmat.primitives.asymmetric import ec

        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())
        p = _install(tmp_path, pem)
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        with pytest.raises(kx.CredentialError) as e:
            _load()
        text = _chain_text(e.value)
        assert "requires an RSA private key" in text
        for line in pem.decode().splitlines():
            if line and not line.startswith("-----"):
                assert line not in text

    def test_15_the_key_id_never_appears_in_a_refusal(self, tmp_path, monkeypatch):
        """The key id is credential-adjacent: `auth.py` fingerprints it rather
        than printing it, and this loader must not undo that by echoing the
        configured value into a message."""
        p = _install(tmp_path, _pem_bytes(), mode=0o644)
        _configure(monkeypatch, key_id=KEY_ID_MARKER, credential_path=str(p))
        with pytest.raises(kx.CredentialError) as e:
            _load()
        assert KEY_ID_MARKER not in _chain_text(e.value)

        # ...and the same on the scopes-refusal path, which runs first.
        p2 = _install(tmp_path, _pem_bytes(), name="ok.pem")
        _configure(monkeypatch, key_id=KEY_ID_MARKER, credential_path=str(p2))
        with pytest.raises(kx.CredentialError) as e2:
            _load(reported_scopes=["read", "write"])
        assert KEY_ID_MARKER not in _chain_text(e2.value)


# --- 16-22: no unsigned fallback, argued structurally -----------------------------
class TestNoUnsignedFallback:
    def test_16_the_module_never_names_the_unsigned_signer(self):
        """Structural, not a substring scan: this module's docstring EXPLAINS
        why the unsigned seam is not used, and a raw grep would fail on its own
        prose — a false failure this repository has produced before."""
        named = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Name):
                named.add(node.id)
            elif isinstance(node, ast.Attribute):
                named.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                named |= {a.name for a in node.names}
                named |= {a.asname for a in node.names if a.asname}
            elif isinstance(node, ast.Import):
                named |= {a.name for a in node.names}
        for banned in ("UnsignedTransportSigner", "RequestSigner",
                       "for_scope_audit"):
            assert banned not in named, banned
        # `ReadOnlyRequestSigner` is imported by name, so the base-class check
        # above must not have passed merely because nothing is imported.
        assert "ReadOnlyRequestSigner" in named

    def test_17_the_loader_has_exactly_one_return(self):
        returns = [n for n in ast.walk(_loader_ast()) if isinstance(n, ast.Return)]
        assert len(returns) == 1
        assert isinstance(returns[0].value, ast.Name)
        assert returns[0].value.id == "signer"

    def test_18_the_only_returned_value_comes_from_from_path(self):
        """`signer` is bound once, by a call to
        `ReadOnlyRequestSigner.from_path`. Nothing else can reach the return."""
        binds = []
        for node in ast.walk(_loader_ast()):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "signer":
                        binds.append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "signer":
                    binds.append(node.value)
        assert len(binds) == 1
        call = binds[0]
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        assert call.func.attr == "from_path"
        assert isinstance(call.func.value, ast.Name)
        assert call.func.value.id == "ReadOnlyRequestSigner"

    def test_19_the_loader_catches_nothing(self):
        """The mechanism by which a refusal becomes a fallback is an `except`
        that returns something usable. There is none, so a failure to load
        cannot be converted into a session at all."""
        assert not [n for n in ast.walk(_loader_ast())
                    if isinstance(n, (ast.Try, ast.ExceptHandler))]

    def test_20_a_substituted_unsigned_signer_is_rejected_at_runtime(
            self, tmp_path, monkeypatch):
        """The structural argument covers the code as written. This covers the
        boundary itself: whatever `from_path` hands back, an object that is not
        a `ReadOnlyRequestSigner` is refused rather than returned."""
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        monkeypatch.setattr(ka.ReadOnlyRequestSigner, "from_path",
                            staticmethod(lambda **kw: kx.UnsignedTransportSigner()))
        with pytest.raises(kx.CredentialError) as e:
            _load()
        assert "UnsignedTransportSigner" in str(e.value)
        assert not isinstance(e.value, kc.ObserverCredentialUnavailable)

    def test_21_none_from_from_path_is_also_refused(self, tmp_path, monkeypatch):
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        monkeypatch.setattr(ka.ReadOnlyRequestSigner, "from_path",
                            staticmethod(lambda **kw: None))
        with pytest.raises(kx.CredentialError):
            _load()

    def test_22_the_unsigned_seam_could_not_serve_the_handshake_anyway(self):
        """Second, independent lock: the two objects do not share an interface.
        The transport asks for `headers_for(purpose=...)`; the unsigned seam
        implements `headers(method=, path=)` and nothing else, so substituting
        it fails at the call site even if the type check were removed."""
        assert not hasattr(kx.UnsignedTransportSigner, "headers_for")
        assert not hasattr(kx.UnsignedTransportSigner, "websocket_headers")
        assert hasattr(ka.ReadOnlyRequestSigner, "headers_for")


# --- 23-26: the loaded signer is the narrow one -----------------------------------
class TestLoadedSigner:
    def test_23_a_confined_credential_yields_a_real_signer(self, tmp_path,
                                                           monkeypatch):
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id="aaaa-bbbb-cccc", credential_path=str(p))
        signer = _load()
        assert isinstance(signer, ka.ReadOnlyRequestSigner)
        assert signer.environment == kx.ENV_DEMO
        assert signer.scopes == ("read",)
        assert signer.scopes_are_verified is True
        assert signer.credential_facts.mode_octal == "0o600"

    def test_24_the_session_signer_cannot_reach_the_rest_route(self, tmp_path,
                                                               monkeypatch):
        """The collector's signer holds the handshake purpose ONLY. The
        key-metadata route belongs to the one-shot audit, which is a separate
        entry point that never runs inside a session."""
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        signer = _load()
        assert signer.granted_purposes == frozenset(
            {kx.AuthPurpose.WEBSOCKET_HANDSHAKE})
        with pytest.raises(kx.CredentialError):
            signer.headers_for(purpose=kx.AuthPurpose.API_KEY_METADATA,
                               timestamp_ms=1_754_500_000_000)

    def test_25_unverified_scopes_are_refused_by_the_loader_path(self, tmp_path,
                                                                 monkeypatch):
        """`reported_scopes` has no default anywhere on this path, so a session
        cannot be opened with scopes nobody asked the venue about."""
        p = _install(tmp_path, _pem_bytes())
        _configure(monkeypatch, key_id="aaaa-bbbb", credential_path=str(p))
        with pytest.raises(TypeError):
            kc.load_observer_signer(environment=kx.ENV_DEMO)
        for bad in (None, ["read", "write"], ["write"], "read", []):
            with pytest.raises(kx.CredentialError):
                _load(reported_scopes=bad)

    def test_26_the_documented_variable_names_are_the_ones_read(self, tmp_path,
                                                               monkeypatch):
        """Pinned as literals. The variables are documented in
        `docs/SAFETY_BOUNDARIES.md` and set by hand on the host; renaming one
        silently would produce a permanent, quiet refusal."""
        assert kc.OBSERVER_KEY_ID_VAR == "KALSHI_OBSERVER_API_KEY_ID"
        assert kc.OBSERVER_CREDENTIAL_PATH_VAR == "KALSHI_OBSERVER_CREDENTIAL_PATH"
        # And the loader really reads the process environment under those names:
        # test 23 configured them with `setenv` and nothing else.
        p = _install(tmp_path, _pem_bytes())
        monkeypatch.setenv(kc.OBSERVER_KEY_ID_VAR, "aaaa-bbbb")
        monkeypatch.setenv(kc.OBSERVER_CREDENTIAL_PATH_VAR, str(p))
        get_settings.cache_clear()
        assert isinstance(_load(), ka.ReadOnlyRequestSigner)


# --- 27-32: auth.py remains the sole holder of key material -----------------------
def _app_py_files():
    return sorted((REPO / "app").rglob("*.py"))


class TestKeyMaterialStaysInAuth:
    def test_27_exactly_one_pem_loader_still_exists(self):
        """The existing AST control, re-run with the new module in scope."""
        loaders = []
        for path in _app_py_files():
            for node in ast.walk(ast.parse(path.read_text())):
                if ((isinstance(node, ast.Attribute)
                     and node.attr in ("load_pem_private_key", "load_der_private_key"))
                        or (isinstance(node, ast.Name)
                            and node.id in ("load_pem_private_key",
                                            "load_der_private_key"))):
                    loaders.append(str(path.relative_to(REPO)))
        assert sorted(set(loaders)) == ["app/realtime/auth.py"], loaders

    def test_28_exactly_one_signing_surface_still_exists(self):
        signers = []
        for path in _app_py_files():
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "sign"):
                    signers.append(str(path.relative_to(REPO)))
        assert sorted(set(signers)) == ["app/realtime/auth.py"], signers

    def test_29_the_existing_isolation_suite_still_passes(self):
        """Run the pre-existing assertions themselves, not a copy of them.

        A re-implementation can drift from the control it claims to preserve;
        calling the original cannot.
        """
        from tests.test_kalshi_preauth_hardening_001 import TestCredentialIsolation

        suite = TestCredentialIsolation()
        suite.test_11_observer_credential_is_read_by_the_observer_loader_only()
        suite.test_13_exactly_one_pem_loader_exists_in_the_repository()
        suite.test_14_exactly_one_signing_surface_exists()
        suite.test_15_no_general_purpose_signing_api()

    def test_30_the_collector_opens_no_file_and_parses_no_key(self):
        """It passes a PATH to `auth.py` and receives an object back. If it
        could read the file itself, `auth.py` would be the first holder of key
        material rather than the only one."""
        called = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Name):
                called.add(node.id)
            elif isinstance(node, ast.Attribute):
                called.add(node.attr)
        for banned in ("open", "fdopen", "read_bytes", "read_text", "mmap",
                       "load_pem_private_key", "load_der_private_key",
                       "private_bytes", "serialization", "rsa", "padding"):
            assert banned not in called, banned

    def test_31_the_collector_logs_nothing_at_all(self):
        """Nothing here may print, log or warn: the values in scope are a key id
        and a credential path, and the honest way to keep them out of a log is
        to have no logging call in the file."""
        emitters = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Name):
                emitters.add(node.id)
            elif isinstance(node, ast.Attribute):
                emitters.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = ([a.name for a in node.names]
                        + ([node.module] if isinstance(node, ast.ImportFrom)
                           and node.module else []))
                emitters |= {m for m in mods if m}
        for banned in ("print", "logging", "logger", "log", "warn", "warnings",
                       "stdout", "stderr", "echo"):
            assert banned not in emitters, banned

    def test_32_the_collector_stays_inside_the_reviewed_dependency_direction(self):
        """Section 6.1: strictly downward. The credential lane may reach config,
        auth and the allowlists — and nothing else in `app/`."""
        mods = set()
        for node in ast.walk(COLLECTOR_SRC):
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        app_mods = {m for m in mods if m.startswith("app.")}
        assert app_mods == {"app.config", "app.realtime.auth",
                            "app.realtime.kalshi"}, app_mods
        for banned in ("solana", "solders", "web3", "eth_account", "websockets",
                       "requests", "httpx", "aiohttp"):
            assert not any(m.startswith(banned) for m in mods), banned
