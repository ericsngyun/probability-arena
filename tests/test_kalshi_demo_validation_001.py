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

    def test_the_only_transport_in_the_observer_is_the_one_ws_file(self):
        """Amended by KALSHI-LIVE-TAPE-COLLECTOR-001 CP1, deliberately.

        Formerly `test_still_no_transport_anywhere_in_the_observer`. That claim
        was true and worth pinning while the observer had no way to reach a
        venue; CP1 gives it one, under an approved milestone, in exactly one
        file. The audit is narrowed rather than dropped, so it now catches the
        regression that actually matters: a SECOND socket appearing in the
        package. The three HTTP clients stay banned everywhere, including in the
        websocket file — the collector has no REST path (§3 non-goals).
        """
        socket_holder = "ws_transport.py"
        holders = []
        for path in sorted(PKG.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module}
                else:
                    continue
                for banned in ("httpx", "requests", "aiohttp"):
                    assert not any(n == banned or n.startswith(banned + ".")
                                   for n in names), (path, banned)
                if any(n == "websockets" or n.startswith("websockets.")
                       for n in names):
                    holders.append(path.name)
        assert set(holders) == {socket_holder}, holders


# --- live DEMO REST wire evidence, 2026-08-07 --------------------------------------
# Captured from https://external-api.demo.kalshi.co/trade-api/v2/markets.
# Pinned as a fixture so the assumptions it settles cannot silently regress.
# Values are venue market data, not credentials.
LIVE_DEMO_MARKET = {
    "ticker": "KXPLATINUMH-26AUG0716-T1759.49",
    "status": "active",
    "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000",
    "no_bid_dollars": "1.0000", "no_ask_dollars": "1.0000",
    "last_price_dollars": "0.0000",
    "yes_bid_size_fp": "0.00", "yes_ask_size_fp": "0.00",
    "volume_fp": "0.00", "open_interest_fp": "0.00",
    "liquidity_dollars": "0.0000",
    "price_level_structure": "linear_cent",
    "price_ranges": [{"end": "1.0000", "start": "0.0000", "step": "0.0100"}],
}


class TestLiveDemoRestEvidence:
    def test_price_ranges_uses_start_end_step_not_dollar_suffixed_names(self):
        """The defect this evidence found.

        `PriceGrid` expected `start_dollars`/`end_dollars`/`tick_dollars`. The
        venue sends `start`/`end`/`step`, so every real market raised a bare
        KeyError and the grid guard had never once run against live data. The
        `_dollars` suffix is real on scalar price fields, which is presumably
        where the guess came from; inside `price_ranges` it does not appear.
        """
        from app.realtime import fixedpoint as fp

        assert set(LIVE_DEMO_MARKET["price_ranges"][0]) == {"start", "end", "step"}
        grid = fp.PriceGrid(LIVE_DEMO_MARKET["price_ranges"],
                            structure_name=LIVE_DEMO_MARKET["price_level_structure"])
        assert not grid.unconstrained
        # linear_cent: whole cents on grid, sub-cent off it.
        grid.validate(fp.parse_price_units("0.5000"), field="probe")
        grid.validate(fp.parse_price_units("0.0100"), field="probe")
        with pytest.raises(fp.FixedPointError):
            grid.validate(fp.parse_price_units("0.6153"), field="probe")

    def test_missing_range_field_raises_a_typed_error_not_keyerror(self):
        from app.realtime import fixedpoint as fp

        with pytest.raises(fp.FixedPointError, match="no 'step'"):
            fp.PriceGrid([{"start": "0.0000", "end": "1.0000"}])

    def test_overlapping_ranges_are_refused(self):
        from app.realtime import fixedpoint as fp

        with pytest.raises(fp.FixedPointError, match="overlap"):
            fp.PriceGrid([{"start": "0.0000", "end": "0.5000", "step": "0.0100"},
                          {"start": "0.4000", "end": "1.0000", "step": "0.0100"}])

    def test_live_scales_confirm_the_fixed_point_contract(self):
        """`_dollars` fields carry 4 decimals and `_fp` fields 2 — which is
        exactly PRICE_SCALE=10_000 and CONTRACT_SCALE=100, now confirmed on the
        wire rather than assumed."""
        from app.realtime import fixedpoint as fp

        assert fp.PRICE_SCALE == 10_000 and fp.CONTRACT_SCALE == 100
        for k, v in LIVE_DEMO_MARKET.items():
            if k.endswith("_dollars"):
                assert len(v.split(".")[1]) == 4, (k, v)
                fp.parse_price_units(v, field=k)
            elif k.endswith("_fp"):
                assert len(v.split(".")[1]) == 2, (k, v)
                fp.parse_contract_units(v, field=k)

    def test_rest_field_names_match_what_reconciliation_reads(self):
        from app.realtime import archive as ar
        from app.realtime import book as bk

        b = bk.OrderBook(LIVE_DEMO_MARKET["ticker"])
        b.apply_snapshot({"market_ticker": LIVE_DEMO_MARKET["ticker"],
                          "yes_dollars_fp": [], "no_dollars_fp": []}, seq=1, sid=1)
        out = ar.reconcile_with_rest(b, LIVE_DEMO_MARKET)
        # Identity resolves via `ticker`, and `status` is read — both confirmed
        # present on the live payload.
        assert out["classification"] != "identity_mismatch"
        assert out["rest_status"] == "active"

    def test_price_level_structure_is_a_label_and_keys_no_arithmetic(self):
        from app.realtime import fixedpoint as fp

        grid = fp.PriceGrid(LIVE_DEMO_MARKET["price_ranges"],
                            structure_name="linear_cent")
        relabelled = fp.PriceGrid(LIVE_DEMO_MARKET["price_ranges"],
                                  structure_name="something_else_entirely")
        assert grid.ranges == relabelled.ranges


class TestScopeAuditBootstrap:
    """`from_path` requires proven scopes and the audit is what proves them.

    Requiring the proof in order to run the proof is circular; this constructor
    breaks the cycle, and these tests pin the direction it breaks it in.
    """

    def test_bootstrap_signer_can_only_reach_the_metadata_route(self, tmp_path):
        p = _install(tmp_path, _gen())
        s = ka.ReadOnlyRequestSigner.for_scope_audit(
            key_id="demo-key-1", credential_path=p, environment=kx.ENV_DEMO)
        assert s.granted_purposes == frozenset({kx.AuthPurpose.API_KEY_METADATA})
        s.headers_for(purpose=kx.AuthPurpose.API_KEY_METADATA, timestamp_ms=TS)
        # It cannot open a socket even though the credential would be accepted.
        with pytest.raises(kx.CredentialError, match="not granted"):
            s.websocket_headers(timestamp_ms=TS)

    def test_bootstrap_scopes_are_a_sentinel_not_a_plausible_value(self, tmp_path):
        """A bootstrap credential that reads like an audited one is how an
        unverified key ends up trusted."""
        p = _install(tmp_path, _gen())
        s = ka.ReadOnlyRequestSigner.for_scope_audit(
            key_id="demo-key-1", credential_path=p, environment=kx.ENV_DEMO)
        assert s.scopes == ka.UNVERIFIED_SCOPES
        assert s.scopes != ("read",)
        assert s.scopes_are_verified is False
        audited, _ = _signer(tmp_path / "b")
        assert audited.scopes_are_verified is True

    def test_bootstrap_keeps_the_full_confinement_contract(self, tmp_path):
        d = tmp_path / "loose"
        d.mkdir(parents=True)
        os.chmod(d, 0o700)
        bad = d / "k.pem"
        bad.write_bytes(_gen().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
        os.chmod(bad, 0o644)          # too permissive
        with pytest.raises(ka.CredentialConfinementError, match="mode"):
            ka.ReadOnlyRequestSigner.for_scope_audit(
                key_id="k", credential_path=bad, environment=kx.ENV_DEMO)

    def test_the_audit_accepts_a_bootstrap_signer(self, tmp_path):
        p = _install(tmp_path, _gen())
        s = ka.ReadOnlyRequestSigner.for_scope_audit(
            key_id="demo-key-1", credential_path=p, environment=kx.ENV_DEMO)
        out = ca.audit_scopes(
            signer=s, key_id="demo-key-1", environment=kx.ENV_DEMO,
            fetch=lambda path, headers: {
                "api_keys": [{"api_key_id": "demo-key-1", "scopes": ["read"]}]},
            timestamp_ms=TS)
        assert out.proven_read_only is True and out.scopes == ("read",)


# --- LIVE DEMO WEBSOCKET EVIDENCE, 2026-08-08 --------------------------------------
# Frames captured verbatim from wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2
# on one bounded read-only session. Venue market data, no credential material.
# Pinned because each one settled a question the implementation had guessed at.
WIRE_TICKER = {
    "type": "ticker", "sid": 1,
    "msg": {"market_id": "29c0608a-3169-4cf1-9e08-32f241927c20",
            "market_ticker": "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1",
            "price_dollars": "0.5000", "yes_bid_dollars": "0.4700",
            "yes_ask_dollars": "0.5100", "volume_fp": "2.00",
            "open_interest_fp": "2.00", "dollar_volume": 1,
            "dollar_open_interest": 1, "yes_bid_size_fp": "5.00",
            "yes_ask_size_fp": "206.00", "last_trade_size_fp": "1.00",
            "ts": 1786150148, "ts_ms": 1786150148065,
            "time": "2026-08-08T00:49:08.065758Z"}}
WIRE_SNAPSHOT = {
    "type": "orderbook_snapshot", "sid": 4, "seq": 1,
    "msg": {"market_ticker": "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1",
            "market_id": "29c0608a-3169-4cf1-9e08-32f241927c20",
            "yes_dollars_fp": [["0.4700", "5.00"]],
            "no_dollars_fp": [["0.5100", "5.00"]]}}
WIRE_DELTA = {
    "type": "orderbook_delta", "sid": 4, "seq": 3,
    "msg": {"market_ticker": "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1",
            "market_id": "29c0608a-3169-4cf1-9e08-32f241927c20",
            "price_dollars": "0.5100", "delta_fp": "201.00", "side": "no",
            "ts": "2026-08-08T00:49:08.065758Z", "ts_ms": 1786150148065}}
WIRE_ERROR = {"type": "error", "sid": 4, "seq": 4,
              "msg": {"code": 14, "msg": "Market Ticker required"}}
WIRE_EMPTY_SNAPSHOT = {
    "type": "orderbook_snapshot", "sid": 4, "seq": 9,
    "msg": {"market_ticker": "KXQUICKSETTLE-07AUG26H2050-2",
            "market_id": "0526f569-9efc-45c4-8b79-84828984f48a"}}
MKT = "KXMLBHIT-26AUG071845CINWSH-WSHNNUEZ26-1"


def _rec(frame, gen=1):
    return {"event_type": frame["type"], "sid": frame.get("sid"),
            "seq": frame.get("seq"),
            "market_ticker": (frame.get("msg") or {}).get("market_ticker"),
            "subscription_generation": gen, "raw": {"msg": frame["msg"]}}


class TestLiveWebSocketEvidence:
    def test_use_yes_price_emits_no_levels_already_on_the_yes_scale(self):
        """Question D, settled by the venue rather than by reasoning.

        The ticker frame is ground truth: yes_ask 0.5100 size 206.00. The book's
        NO ladder holds 0.5100, and a +201.00 delta takes 5.00 to 206.00. So the
        NO price IS the YES ask. The previous code complemented it to 0.4900 —
        uncrossed, plausible, and two cents wrong.
        """
        from app.realtime import book as bk

        b = bk.OrderBook(MKT)
        b.apply_snapshot(WIRE_SNAPSHOT["msg"], sid=4, seq=1)
        b.apply_delta(WIRE_DELTA["msg"], seq=3, sid=4, ordered_externally=True)
        top = b.top_of_book()
        t = WIRE_TICKER["msg"]
        assert top["best_yes_bid"] == t["yes_bid_dollars"] == "0.4700"
        assert top["best_yes_ask"] == t["yes_ask_dollars"] == "0.5100"
        assert top["best_yes_bid_size"] == t["yes_bid_size_fp"] == "5.00"
        assert top["best_yes_ask_size"] == t["yes_ask_size_fp"] == "206.00"

    def test_a_non_orderbook_frame_consumes_a_sequence_number(self):
        """Question C's sharp edge. The error frame arrived at seq 4, between
        deltas at 3 and 5. Skipping it without advancing the position made the
        next delta look like a gap and would have unpublished every book on the
        subscription within seconds of connecting."""
        from app.realtime import book as bk

        # Both markets, in the real receive order: seq 2 was the sibling's
        # snapshot, which is precisely the traffic a per-market view mistakes
        # for a hole.
        other = "KXQUICKSETTLE-07AUG26H2050-2"
        sub = bk.SubscriptionState(4, market_tickers=(MKT, other))
        r = bk.SubscriptionRouter(sub)
        r.dispatch(_rec(WIRE_SNAPSHOT))                       # seq 1
        sib = {"type": "orderbook_snapshot", "sid": 4, "seq": 2,
               "msg": {"market_ticker": other,
                       "yes_dollars_fp": [["0.3000", "100.00"]],
                       "no_dollars_fp": [["0.7000", "100.00"]]}}
        r.dispatch(_rec(sib))                                 # seq 2
        r.dispatch(_rec(WIRE_DELTA))                          # seq 3
        assert sub.last_seq == 3
        out = r.dispatch(_rec(WIRE_ERROR))
        assert out["action"] == "ignored"
        assert sub.last_seq == 4, "the error frame must advance the position"
        assert sub.healthy is True
        nxt = dict(_rec(WIRE_DELTA)); nxt["seq"] = 5
        r.dispatch(nxt)                      # must NOT read as a gap
        assert sub.healthy is True
        assert r.publishable_books() == {MKT: True, other: True}

    def test_seq_is_subscription_global_not_per_market(self):
        """Question C. One sid carried both markets; seq ran 1..9 across the
        subscription while each market's own view had holes."""
        observed = [(1, "orderbook_snapshot", "M2"), (2, "orderbook_snapshot", "M1"),
                    (3, "orderbook_delta", "M2"), (4, "error", None),
                    (5, "orderbook_delta", "M1"), (6, "orderbook_delta", "M1"),
                    (7, "orderbook_delta", "M1"), (8, "orderbook_delta", "M1"),
                    (9, "orderbook_snapshot", "M1")]
        seqs = [s for s, _, _ in observed]
        assert seqs == list(range(1, 10)), "contiguous across the SID"
        per = {}
        for s, _, m in observed:
            if m:
                per.setdefault(m, []).append(s)
        assert per["M2"] == [1, 3] and per["M1"] == [2, 5, 6, 7, 8, 9]
        for m, ss in per.items():
            assert not all(b - a == 1 for a, b in zip(ss, ss[1:])), m

    def test_an_empty_book_snapshot_omits_both_ladder_keys(self):
        """Confirmed at seq 9 after deltas emptied the book. Requiring a ladder
        key rejected a valid snapshot."""
        from app.realtime import book as bk

        b = bk.OrderBook("KXQUICKSETTLE-07AUG26H2050-2")
        out = b.apply_snapshot(WIRE_EMPTY_SNAPSHOT["msg"], sid=4, seq=9)
        assert out["yes_levels"] == 0 and out["no_levels"] == 0
        assert b.publishable is True
        assert b.top_of_book()["best_yes_bid"] is None

    def test_venue_timestamp_fields_are_not_uniform_across_channels(self):
        """`ts` means different things per channel: an ISO string on
        orderbook_delta, epoch SECONDS on ticker. `ts_ms` is unambiguous
        wherever it appears, so it is read first."""
        from datetime import datetime, timezone

        from app.realtime import book as bk

        recv = datetime(2026, 8, 8, 0, 49, 9, tzinfo=timezone.utc)
        for frame in (WIRE_DELTA, WIRE_TICKER):
            env = bk.make_envelope(
                venue="kalshi", environment="demo", channel="c",
                message=frame, receive_time=recv, receive_mono=1)
            assert env.venue_time is not None, frame["type"]
            # 1786150148065 ms -> 2026-08-08T00:49:08.065Z; receive ~935 ms later.
            # Microseconds now, and an integer — a float is not canonically
            # representable and was what made archived records unreadable.
            assert isinstance(env.data_age_us, int)
            assert 0 < env.data_age_us < 2_000_000, (frame["type"], env.data_age_us)

    def test_get_snapshot_requires_market_tickers(self):
        """The sids-only form was rejected with code 14, and that error consumed
        a sequence slot."""
        from app.realtime import kalshi as kx

        cmd = kx.build_get_snapshot(9, 4, [MKT])
        assert cmd["params"]["market_tickers"] == [MKT]
        assert cmd["params"]["action"] == "get_snapshot"
        with pytest.raises(kx.CapabilityError, match="market ticker"):
            kx.build_get_snapshot(9, 4, [])
        assert WIRE_ERROR["msg"]["code"] == 14

    def test_read_scope_entitles_all_four_market_data_channels(self):
        """Question B. A key whose metadata is exactly ["read"] received
        `subscribed` acks for every channel in the allowlist."""
        acks = {"ticker": 1, "market_lifecycle_v2": 2, "trade": 3,
                "orderbook_delta": 4}
        from app.realtime import kalshi as kx

        assert set(acks) == set(kx.ALLOWED_CHANNELS)
        assert len(set(acks.values())) == 4, "each channel got its own sid"
