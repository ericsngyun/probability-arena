"""KALSHI-REALTIME-OBSERVATION-001A.

Most of these assert a refusal. An observer that can be talked into placing an
order, or that quietly continues across a sequence gap, is worse than no
observer: it produces confident evidence that is wrong.
"""

from __future__ import annotations

import ast
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import book as bk
from app.realtime import fixedpoint as fp
from app.realtime import kalshi as kx


def _init_archive(root, environment="demo"):
    """Archives are brought into existence EXPLICITLY, exactly as an operator does.

    The collector cannot do this, and that is the point: "the head is missing,
    therefore this is a new archive" was the inference that let a rebuilt
    history certify its own deletions. Tests initialize on purpose.
    """
    from app.realtime import archive_head as _ah
    try:
        _ah.initialize_archive(Path(root), environment,
                               archive_identity="kalshi-realtime")
    except _ah.ArchiveHeadError:
        pass                       # already initialized in this test
    return root


def _arch(root, **kw):
    from app.realtime import archive as _ar
    _init_archive(root, kw.get("environment", "demo"))
    return _ar.EventArchive(root, **kw)


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
PKG = Path("app/realtime")


def env(msg, *, etype, seq=None, sid=1, environment="demo", when=None,
        channel="orderbook_delta"):
    when = when or NOW
    return bk.make_envelope(
        venue="kalshi", environment=environment, channel=channel,
        message={"type": etype, "sid": sid, "seq": seq, "msg": msg},
        receive_time=when, receive_mono=bk.monotonic_ns())


def snapshot_msg(yes=None, no=None, ticker="KXTEST"):
    return {"market_ticker": ticker, "market_id": "mid-1",
            "yes_dollars_fp": yes or [], "no_dollars_fp": no or []}


# --- 9-19: fixed point ----------------------------------------------------------


class TestFixedPoint:
    def test_price_parsing_is_exact(self):
        assert fp.parse_price_units("0.6150") == 6150
        assert fp.parse_price_units("0.0001") == 1
        assert fp.parse_price_units("1.0000") == 10_000
        assert fp.parse_price_units("0") == 0

    def test_quantity_parsing_is_exact(self):
        assert fp.parse_contract_units("12.50") == 1250
        assert fp.parse_contract_units("0.01") == 1
        assert fp.parse_contract_units("100") == 10_000

    def test_scales_are_the_authorized_ones(self):
        assert (fp.PRICE_SCALE, fp.CONTRACT_SCALE, fp.NOTIONAL_SCALE) == \
            (10_000, 100, 1_000_000)
        assert fp.PRICE_SCALE * fp.CONTRACT_SCALE == fp.NOTIONAL_SCALE

    def test_notional_carries_six_dollar_decimals(self):
        n = fp.notional_units(fp.parse_price_units("0.6150"),
                              fp.parse_contract_units("12.50"))
        assert n == 6150 * 1250
        assert n / fp.NOTIONAL_SCALE == pytest.approx(0.61500 * 12.5, rel=1e-9)

    def test_more_than_four_price_decimals_rejected(self):
        with pytest.raises(fp.FixedPointError, match="decimal places"):
            fp.parse_price_units("0.61505")

    def test_more_than_two_count_decimals_rejected(self):
        with pytest.raises(fp.FixedPointError, match="decimal places"):
            fp.parse_contract_units("1.005")

    def test_negative_delta_accepted(self):
        assert fp.parse_contract_units("-5.00", allow_negative=True) == -500

    def test_negative_snapshot_quantity_rejected(self):
        with pytest.raises(fp.FixedPointError, match="negative"):
            fp.parse_contract_units("-5.00", allow_negative=False)

    @pytest.mark.parametrize("bad", ["NaN", "nan", "Infinity", "-inf"])
    def test_nan_and_infinity_rejected(self, bad):
        with pytest.raises(fp.FixedPointError, match="not a finite|not a plain"):
            fp.parse_price_units(bad)

    @pytest.mark.parametrize("bad", ["1E-4", "1e4", "0,5", "  ", "0.5.1", "+.5"])
    def test_malformed_and_exponent_rejected(self, bad):
        with pytest.raises(fp.FixedPointError):
            fp.parse_price_units(bad)

    def test_float_input_refused_outright(self):
        """By the time a float exists the precision loss has happened;
        converting it would launder the error."""
        with pytest.raises(fp.FixedPointError, match="received a float"):
            fp.parse_price_units(0.615)

    def test_price_above_one_dollar_rejected(self):
        with pytest.raises(fp.FixedPointError, match="outside"):
            fp.parse_price_units("1.0001")

    def test_json_numbers_never_become_float(self):
        parsed = fp.loads_exact('{"a": 0.615, "b": 3}')
        assert not isinstance(parsed["a"], float)
        assert isinstance(parsed["b"], int)
        with pytest.raises(fp.FixedPointError):
            fp.loads_exact('{"a": NaN}')

    def test_round_trip_is_exact(self):
        for s in ("0.0001", "0.5000", "0.6150", "1.0000"):
            assert fp.format_price_units(fp.parse_price_units(s)) == s


# --- complement properties ------------------------------------------------------


class TestComplement:
    @pytest.mark.parametrize("price", ["0.0000", "0.0001", "0.0100", "0.3300",
                                       "0.5000", "0.9900", "1.0000"])
    def test_complement_sums_to_one_dollar_exactly(self, price):
        p = fp.parse_price_units(price)
        assert p + fp.complement_price_units(p) == fp.ONE_DOLLAR_UNITS

    def test_complement_is_an_involution(self):
        for cents in range(0, 10_001, 137):
            assert fp.complement_price_units(
                fp.complement_price_units(cents)) == cents

    def test_sub_cent_grid_accepted(self):
        grid = fp.PriceGrid([{"start": "0.0001", "end": "0.9999",
                              "step": "0.0001"}])
        assert grid.contains(fp.parse_price_units("0.6153"))

    def test_whole_cent_grid_accepted_and_off_grid_rejected(self):
        grid = fp.PriceGrid([{"start": "0.0100", "end": "0.9900",
                              "step": "0.0100"}])
        assert grid.contains(fp.parse_price_units("0.6100"))
        with pytest.raises(fp.FixedPointError, match="off the market"):
            grid.validate(fp.parse_price_units("0.6153"))

    def test_complement_is_not_assumed_to_be_on_grid(self):
        """A complement is arithmetically exact but need not be a valid ORDER
        price; the grid decides that separately."""
        grid = fp.PriceGrid([{"start": "0.0000", "end": "0.5000",
                              "step": "0.0100"}])
        p = fp.parse_price_units("0.4900")
        assert grid.contains(p)
        assert not grid.contains(fp.complement_price_units(p))

    def test_grid_does_not_key_off_the_structure_name(self):
        grid = fp.PriceGrid([{"start": "0.0001", "end": "0.9999",
                              "step": "0.0001"}],
                            structure_name="legacy_whole_cent")
        assert grid.contains(fp.parse_price_units("0.6153")), (
            "numerical behaviour must come from price_ranges, not the label")


# --- 20-22: YES-scale normalization ---------------------------------------------


class TestYesNormalization:
    def test_subscribe_always_sets_use_yes_price(self):
        cmd = kx.build_subscribe(1, ["orderbook_delta"], ["KXTEST"])
        assert cmd["params"]["use_yes_price"] is True
        assert cmd["cmd"] == "subscribe"

    def test_yes_side_is_a_bid_and_no_side_is_a_derived_offer(self):
        b = bk.OrderBook("KXTEST")
        b.apply_snapshot(snapshot_msg(
            yes=[["0.6000", "10.00"], ["0.5900", "5.00"]],
            no=[["0.6500", "8.00"]]), seq=1)
        assert b.best_yes_bid_units == 6000
        # best NO bid 0.35 -> YES offer at 0.65
        assert b.best_yes_ask_units == 6500
        assert b.top_of_book()["spread_units"] == 500

    def test_ladder_preserves_raw_side_and_price(self):
        b = bk.OrderBook("KXTEST")
        b.apply_snapshot(snapshot_msg(yes=[["0.6000", "10.00"]],
                                      no=[["0.6500", "8.00"]]), seq=1)
        ladder = b.yes_scale_ladder()
        assert ladder["bids"][0]["raw_side"] == "yes"
        ask = ladder["asks"][0]
        assert ask["raw_side"] == "no"
        assert ask["raw_price_units"] == 6500   # NO price IS the YES ask
        assert ask["price_units"] == 6500, "normalized to the YES scale"

    def test_normalization_is_not_destructive(self):
        e = env(snapshot_msg(yes=[["0.6000", "10.00"]]), etype="orderbook_snapshot",
                seq=1)
        assert e.raw["msg"]["yes_dollars_fp"] == [["0.6000", "10.00"]]


# --- 23-27: sequence integrity --------------------------------------------------


class TestSequenceIntegrity:
    def _synced(self):
        b = bk.OrderBook("KXTEST")
        b.apply_snapshot(snapshot_msg(yes=[["0.6000", "10.00"]]), seq=10)
        return b

    def test_delta_before_snapshot_is_rejected_not_buffered(self):
        b = bk.OrderBook("KXTEST")
        with pytest.raises(bk.BookIntegrityError, match="before any snapshot"):
            b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                           "delta_fp": "1.00"}, seq=1)
        assert b.stats["rejected_pre_snapshot"] == 1

    def test_ordered_delta_applies(self):
        b = self._synced()
        out = b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                             "delta_fp": "5.00"}, seq=11)
        assert out["level_units"] == 1500
        assert b.publishable

    def test_duplicate_sequence_is_ignored(self):
        b = self._synced()
        b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                       "delta_fp": "5.00"}, seq=11)
        before = b.checksum()
        out = b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                            "delta_fp": "5.00"}, seq=11)
        assert out["action"] == "duplicate_ignored"
        assert b.checksum() == before
        assert b.stats["duplicates"] == 1

    def test_gap_halts_and_unpublishes(self):
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="gap"):
            b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                           "delta_fp": "1.00"}, seq=15)
        assert b.publishable is False
        assert b.stats["gaps"] == 1
        with pytest.raises(bk.BookIntegrityError):
            _ = b.best_yes_bid_units

    def test_regression_detected(self):
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="regression"):
            b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                           "delta_fp": "1.00"}, seq=5)
        assert b.stats["regressions"] == 1

    def test_snapshot_resynchronises_and_bumps_generation(self):
        b = self._synced()
        gen = b.generation
        with pytest.raises(bk.BookIntegrityError):
            b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                           "delta_fp": "1.00"}, seq=99)
        b.mark_resynchronised()
        b.apply_snapshot(snapshot_msg(yes=[["0.6100", "2.00"]]), seq=100)
        assert b.publishable and b.generation == gen + 1
        assert b.best_yes_bid_units == 6100
        assert b.stats["resyncs"] == 1

    def test_resync_command_shape(self):
        cmd = kx.build_resubscribe_snapshot(7, sid=3, market_tickers=["KXTEST"])
        assert cmd["cmd"] == "update_subscription"
        assert cmd["params"]["sids"] == [3]

    def test_delta_driving_a_level_negative_halts(self):
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="negative"):
            b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                           "delta_fp": "-50.00"}, seq=11)
        assert b.publishable is False

    def test_level_deletion_on_zero(self):
        b = self._synced()
        b.apply_delta({"side": "yes", "price_dollars": "0.6000",
                       "delta_fp": "-10.00"}, seq=11)
        assert b.yes == {}


# --- 1-8: credential and capability ---------------------------------------------


class TestCredentialAndCapability:
    def test_read_only_scope_accepted(self):
        assert kx.verify_scopes(["read"], environment="production") == ("read",)

    @pytest.mark.parametrize("bad", [["write"], ["read", "write"], []])
    def test_write_or_wrong_scope_rejected(self, bad):
        with pytest.raises(kx.CredentialError,
                           match="OBSERVE-ONLY CREDENTIAL REQUIREMENT"):
            kx.verify_scopes(bad, environment="production")

    def test_missing_scope_field_rejected(self):
        """Omission is not 'read' — Kalshi keys default to broader access."""
        with pytest.raises(kx.CredentialError, match="no scopes field"):
            kx.verify_scopes(None, environment="production")

    def test_unknown_scope_rejected(self):
        with pytest.raises(kx.CredentialError, match="unrecognized scope"):
            kx.verify_scopes(["read", "trade"], environment="production")

    def test_only_get_is_permitted(self):
        assert kx.assert_method_allowed("get") == "GET"
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(kx.CapabilityError, match="not permitted"):
                kx.assert_method_allowed(m)

    def test_capability_mode_fails_closed(self):
        kx.require_mode(kx.OBSERVE_ONLY, kx.OBSERVE_ONLY)
        for needed in (kx.SHADOW_ONLY, kx.DEMO_EXECUTION, kx.LIVE_BOUNDED):
            with pytest.raises(kx.CapabilityError):
                kx.require_mode(kx.OBSERVE_ONLY, needed)
        assert kx.IMPLEMENTED_MODES == (kx.OBSERVE_ONLY,)

    @pytest.mark.parametrize("ch", ["fill", "market_positions", "user_orders",
                                    "communications", "order_group_updates"])
    def test_private_user_channels_rejected(self, ch):
        with pytest.raises(kx.CapabilityError):
            kx.assert_channels_allowed([ch])

    def test_channel_allowlist_is_exactly_the_authorized_four(self):
        assert set(kx.ALLOWED_CHANNELS) == {
            "orderbook_delta", "ticker", "trade", "market_lifecycle_v2"}

    def test_credential_descriptor_never_carries_the_key(self, tmp_path):
        p = tmp_path / "key.pem"
        p.write_text("-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----")
        p.chmod(0o600)
        d = kx.describe_credential(environment="production", key_id="abc-123",
                                   scopes=["read"], key_path=p)
        blob = json.dumps(d.to_dict())
        assert "SECRET" not in blob and "BEGIN PRIVATE KEY" not in blob
        assert d.key_id_fingerprint.startswith("sha256:")
        assert "abc-123" not in blob, "the raw key id is fingerprinted, not stored"
        assert d.mode_octal == "0o600"

    def test_world_readable_credential_refused(self, tmp_path):
        p = tmp_path / "key.pem"
        p.write_text("x")
        p.chmod(0o644)
        with pytest.raises(kx.CredentialError, match="group or other"):
            kx.describe_credential(environment="production", key_id="k",
                                   scopes=["read"], key_path=p)

    def test_key_handling_is_confined_to_the_auth_module(self):
        """Key material may exist in exactly one file.

        KALSHI-READONLY-AUTH-001 amended the boundary to permit RSA loading for
        read-scoped market-data requests, and the amendment is only meaningful
        if the surface it opened stays one file wide. A second module quietly
        growing a `sign` call is precisely the drift this catches.
        """
        import ast as _ast

        for path in sorted(PKG.rglob("*.py")):
            if path.name == "auth.py":
                continue
            tree = _ast.parse(path.read_text())
            names = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ImportFrom):
                    names |= {a.name for a in node.names}
                elif isinstance(node, (_ast.Name, )):
                    names.add(node.id)
                elif isinstance(node, _ast.Attribute):
                    names.add(node.attr)
            for banned in ("load_pem_private_key", "load_der_private_key",
                           "private_bytes", "sign"):
                assert banned not in names, f"{path}: {banned}"

    def test_signing_string_is_canonical_and_key_free(self):
        assert kx.canonical_signing_string(
            method="GET", path="/trade-api/ws/v2?x=1",
            timestamp_ms=1234567890000) == "1234567890000GET/trade-api/ws/v2"
        with pytest.raises(kx.CapabilityError):
            kx.canonical_signing_string(method="POST", path="/x",
                                        timestamp_ms=1)

    def test_signer_seam_refuses_without_an_installed_key(self):
        with pytest.raises(NotImplementedError, match="pending"):
            kx.RequestSigner().headers(method="GET", path="/trade-api/ws/v2")
        assert kx.UnsignedTransportSigner().headers(
            method="GET", path="/x") == {}

    def test_ws_hosts_are_environment_separated(self):
        assert kx.WS_HOSTS[kx.ENV_PRODUCTION].endswith(kx.WS_PATH)
        assert kx.WS_HOSTS[kx.ENV_DEMO] != kx.WS_HOSTS[kx.ENV_PRODUCTION]


# --- static: the observer must be incapable of trading --------------------------


class TestNoTradingSurface:
    FILES = sorted(PKG.rglob("*.py"))

    def _sources(self):
        return [(p, p.read_text()) for p in self.FILES]

    def test_no_write_http_method_is_reachable(self):
        """Structural: no operative literal equals a mutating method, and the
        allowlist itself contains only GET."""
        assert kx.ALLOWED_HTTP_METHODS == ("GET",)
        for p, src in self._sources():
            for literal in self._operative_strings(src):
                assert literal.strip().upper() not in (
                    "POST", "PUT", "PATCH", "DELETE"), f"{p}: {literal!r}"

    @staticmethod
    def _operative_strings(src):
        """String literals that could become a request, excluding docstrings.

        A raw substring scan matches the module's own prose describing what it
        refuses to do — the same trap that has bitten three checks in this
        repository. Scan the literals that could actually be sent.
        """
        tree = ast.parse(src)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                d = ast.get_docstring(node, clean=False)
                if d:
                    docstrings.add(d)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value not in docstrings:
                    out.append(node.value)
        return out

    @pytest.mark.parametrize("route", [
        "/portfolio", "/orders", "/order", "cancel", "amend", "/api-keys",
        "subaccount", "/positions", "/fills",
    ])
    def test_no_trading_or_account_route_present(self, route):
        for p, src in self._sources():
            for literal in self._operative_strings(src):
                assert route not in literal.lower(), (
                    f"{p} has an operative literal containing {route!r}: "
                    f"{literal!r}")

    def test_no_order_or_portfolio_identifiers(self):
        banned = ("place_order", "create_order", "cancel_order", "amend_order",
                  "get_positions", "get_balance", "portfolio", "wallet",
                  "private_key_bytes")
        for p, src in self._sources():
            tree = ast.parse(src)
            for node in ast.walk(tree):
                name = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    name = node.name
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                if name:
                    for b in banned:
                        assert b not in name.lower(), f"{p}: {name}"

    def test_no_network_client_outside_the_transport_seam(self):
        for p, src in self._sources():
            tree = ast.parse(src)
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module.split(".")[0])
            for banned in ("httpx", "requests", "aiohttp", "urllib"):
                assert banned not in mods, f"{p} imports {banned}"

    def test_no_sqlite_or_orm_coupling(self):
        for p, src in self._sources():
            tree = ast.parse(src)
            mods, names = set(), set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module.split(".")[0])
                elif isinstance(node, ast.Name):
                    names.add(node.id)
            for banned in ("sqlalchemy", "sqlite3"):
                assert banned not in mods, f"{p} imports {banned}"
            for banned in ("get_sessionmaker", "run_migrations"):
                assert banned not in names, f"{p} calls {banned}"

    def test_no_marketops_timer_or_daemon(self):
        for p, src in self._sources():
            tree = ast.parse(src)
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module)
                elif isinstance(node, ast.Import):
                    mods |= {a.name for a in node.names}
            for m in mods:
                assert "marketops" not in m.lower(), f"{p} imports {m}"
            for literal in self._operative_strings(src):
                for banned in ("systemd", "crontab"):
                    assert banned not in literal.lower(), f"{p}: {literal!r}"

    def test_no_ev_or_capital_surface(self):
        banned = ("expected_value", "kelly", "position_size", "pnl",
                  "paper_trade", "execute_trade", "sign_transaction")
        for p, src in self._sources():
            tree = ast.parse(src)
            idents = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    idents.add(node.id.lower())
                elif isinstance(node, ast.Attribute):
                    idents.add(node.attr.lower())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    idents.add(node.name.lower())
            for i in idents:
                for b in banned:
                    assert b not in i, f"{p}: {i}"

    def test_no_migration_added(self):
        versions = sorted(x.name for x in Path("alembic/versions").glob("0*.py"))
        assert versions[-1].startswith("0027")


# --- 28-35: archive, replay, reconciliation -------------------------------------


class TestArchiveAndReplay:
    def _stream(self, environment="demo"):
        out = [env(snapshot_msg(yes=[["0.6000", "10.00"]], no=[["0.6500", "8.00"]]),
                   etype="orderbook_snapshot", seq=1, environment=environment,
                   when=NOW, channel="orderbook_delta")]
        for i, (side, price, delta) in enumerate((
                ("yes", "0.5900", "4.00"), ("no", "0.3600", "2.00"),
                ("yes", "0.6000", "-3.00")), start=2):
            out.append(env({"market_ticker": "KXTEST", "side": side,
                            "price_dollars": price, "delta_fp": delta},
                           etype="orderbook_delta", seq=i,
                           environment=environment,
                           when=NOW + timedelta(milliseconds=i)))
        return out

    def test_archive_round_trips_and_verifies(self, tmp_path):
        a = _arch(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        a.close()
        assert a.written == 4
        v = a.verify()
        assert v["intact"] and v["records"] == 4

    def test_demo_events_cannot_enter_a_production_archive(self, tmp_path):
        prod = _arch(tmp_path, environment="production")
        with pytest.raises(ar.ArchiveError, match="never become"):
            prod.append(self._stream(environment="demo")[0])

    def test_archives_are_physically_separated(self, tmp_path):
        """Demo and production evidence never share a tree.

        The `env=/venue=/date=/hour=` path assertion retires with that layout;
        the property it protected is asserted directly instead — each
        environment's segments live under its own root, and neither reads the
        other's records.
        """
        demo = _arch(tmp_path, environment="demo")
        prod = _arch(tmp_path, environment="production")
        for e in self._stream(environment="demo"):
            demo.append(e)
        demo.close()
        demo_dirs = {str(x["directory"]) for x in demo.segment_paths()}
        prod_dirs = {str(x["directory"]) for x in prod.segment_paths()}
        assert demo_dirs and not (demo_dirs & prod_dirs)
        assert all("env=demo" in d for d in demo_dirs)
        assert prod.read_unverified_diagnostic() == []

    def test_replay_is_deterministic(self, tmp_path):
        a = _arch(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        a.close()
        records = a.read_unverified_diagnostic()
        first, second = ar.replay(records), ar.replay(records)
        assert first["checksums"] == second["checksums"]
        assert first["events_applied"] == 4 and first["events_rejected"] == 0
        assert first["publishable"]["KXTEST"] is True
        assert first["external_calls"] == 0 and first["persisted"] is False

    def test_replay_reproduces_the_live_book_exactly(self, tmp_path):
        live = bk.OrderBook("KXTEST")
        stream = self._stream()
        # sid is threaded through both paths because the checksum now covers
        # it: two books with identical ladders at different stream positions
        # are not the same observation, and replay determinism is asserted on
        # exactly that equality.
        live.apply_snapshot(stream[0].raw["msg"], seq=1, sid=stream[0].sid)
        for e in stream[1:]:
            live.apply_delta(e.raw["msg"], seq=e.seq, sid=e.sid)
        a = _arch(tmp_path, environment="demo")
        for e in stream:
            a.append(e)
        a.close()
        assert ar.replay(a.read_unverified_diagnostic())["checksums"]["KXTEST"] == live.checksum()

    def test_replay_surfaces_a_gap_rather_than_absorbing_it(self, tmp_path):
        stream = self._stream()
        stream[2].seq = 99          # punch a hole
        a = _arch(tmp_path, environment="demo")
        for e in stream:
            a.append(e)
        a.close()
        out = ar.replay(a.read_unverified_diagnostic())
        assert out["events_rejected"] >= 1
        assert any("gap" in f["error"] for f in out["faults"])
        assert out["publishable"]["KXTEST"] is False

    def test_malformed_tail_loses_one_record_not_the_file(self, tmp_path):
        a = _arch(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        a.close()
        path = a.segment_paths()[0]["events_path"]
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write('{"truncated": \n')
        assert len(a.read_unverified_diagnostic()) == 4
        assert a.truncated_records == 1

    def test_latency_is_decomposed_not_a_single_number(self, tmp_path):
        a = _arch(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        a.close()
        env_ = ar.latency_envelope(a.read_unverified_diagnostic()).to_dict()
        assert set(env_) >= {"venue_to_receive_offset_contaminated_ms",
                             "receive_to_normalize_us", "coverage"}
        assert "normalize_to_book_us" not in env_
        assert set(env_["receive_to_normalize_us"]) >= {"p50", "p95", "p99", "max"}

    def test_rest_reconciliation_reports_and_resyncs(self):
        b = bk.OrderBook("KXTEST")
        b.apply_snapshot(snapshot_msg(yes=[["0.6000", "10.00"]],
                                      no=[["0.6500", "8.00"]]), seq=1)
        # The ticker is required: without it this reconciled one market's book
        # against another market's payload and returned a confident verdict.
        assert ar.reconcile_with_rest(b, {"yes_bid_dollars": "0.6000"})[
            "classification"] == "identity_mismatch"
        ok = ar.reconcile_with_rest(b, {"ticker": "KXTEST",
                                        "yes_bid_dollars": "0.6000",
                                        "yes_ask_dollars": "0.6500"})
        assert ok["agrees"] and ok["action"] == "none"
        bad = ar.reconcile_with_rest(b, {"ticker": "KXTEST",
                                         "yes_bid_dollars": "0.5900",
                                         "yes_ask_dollars": "0.6500"})
        assert not bad["agrees"] and bad["action"] == "resynchronise"
        assert bad["discrepancies"][0]["field"] == "best_yes_bid"

    def test_replay_makes_no_provider_or_trading_call(self):
        tree = ast.parse((PKG / "archive.py").read_text())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        for banned in ("httpx", "requests", "websockets", "aiohttp", "socket"):
            assert banned not in mods

    def test_archive_output_is_secret_free(self, tmp_path):
        a = _arch(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        a.close()
        blob = json.dumps(a.read_unverified_diagnostic()).lower()
        for needle in ("private key", "begin rsa", "kalshi-access-signature",
                       "api_key", "secret", "password"):
            assert needle not in blob


class TestFixtureTransport:
    def test_fixture_transport_needs_no_credential_or_network(self):
        t = kx.FixtureTransport([{"type": "subscribed"}])
        assert list(t.iter_frames()) == [{"type": "subscribed"}]
        assert t.connected is False


# --- adversarial-review regressions (Gate 9) --------------------------------------
class TestFailClosedRegressions:
    """Each test reproduces an attack a review actually ran and observed pass.

    The systematic gap the reviews found was that the suite asserted refusals
    only for paths that already refused, and never asserted refusal for a path
    that accepted. These are the second kind.
    """

    def _synced(self, **kw):
        b = bk.OrderBook("KXTEST", **kw)
        b.apply_snapshot(snapshot_msg(yes=[["0.6000", "10.00"]],
                                      no=[["0.6500", "8.00"]]), seq=10, sid=1)
        return b

    def test_any_rejection_leaves_the_book_unpublishable(self):
        """The invariant that catches this whole class: if apply_* raised, the
        book must not still be advertising itself as clean."""
        cases = [
            ("delta", {"market_ticker": "KXTEST", "side": "sideways",
                       "price_dollars": "0.6000", "delta_fp": "1.00"}, 11),
            ("delta", {"market_ticker": "KXTEST", "side": "yes",
                       "delta_fp": "1.00"}, 11),                    # missing price
            ("delta", {"market_ticker": "KXTEST", "side": "yes",
                       "price_dollars": "0.6000"}, 11),             # missing delta
            ("delta", {"market_ticker": "OTHER", "side": "yes",
                       "price_dollars": "0.6000", "delta_fp": "1.00"}, 11),
        ]
        for kind, msg, seq in cases:
            b = self._synced()
            with pytest.raises((bk.BookIntegrityError, fp.FixedPointError)):
                b.apply_delta(msg, seq=seq, sid=1)
            assert b.publishable is False, (kind, msg)
            assert b.integrity_reason
            with pytest.raises(bk.BookIntegrityError):
                b.top_of_book()

    def test_rejected_snapshot_does_not_leave_a_stale_book_publishable(self):
        """A snapshot with NO ladder keys is a legitimately EMPTY book, not a
        rejection — confirmed on the DEMO wire (seq 9 arrived with only
        market_ticker and market_id after deltas emptied the book). The
        fail-closed case is a snapshot that identifies a DIFFERENT market."""
        b = self._synced()
        out = b.apply_snapshot({"market_ticker": "KXTEST"}, seq=11)
        assert out["yes_levels"] == 0 and out["no_levels"] == 0
        assert b.publishable is True
        b2 = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="another market"):
            b2.apply_snapshot({"market_ticker": "SOMEONE-ELSE"}, seq=11)
        assert b2.publishable is False

    def test_snapshot_cannot_rewind_the_book(self):
        """At-least-once redelivery on reconnect is ordinary. Older state
        arriving with a higher generation and publishing as current is the one
        thing generation numbering exists to prevent."""
        b = self._synced()
        b.apply_delta({"market_ticker": "KXTEST", "side": "yes",
                       "price_dollars": "0.6000", "delta_fp": "3.00"},
                      seq=11, sid=1)
        with pytest.raises(bk.BookIntegrityError, match="behind the applied"):
            b.apply_snapshot(snapshot_msg(yes=[["0.5000", "1.00"]]), seq=2, sid=1)
        assert b.publishable is False

    def test_missing_sequence_is_a_fault_not_permission(self):
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="no sequence number"):
            b.apply_delta({"market_ticker": "KXTEST", "side": "yes",
                           "price_dollars": "0.6000", "delta_fp": "1.00"},
                          seq=None, sid=1)
        assert b.publishable is False
        # And a snapshot with no seq does not silently blind the gap check.
        b2 = bk.OrderBook("KXTEST")
        b2.apply_snapshot(snapshot_msg(yes=[["0.6000", "1.00"]]), seq=None)
        assert b2.publishable is False

    def test_a_book_never_absorbs_another_markets_data(self):
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="another market"):
            b.apply_snapshot(snapshot_msg(yes=[["0.6000", "1.00"]],
                                          ticker="SOME-OTHER-MARKET"), seq=11)
        assert b.publishable is False

    def test_delta_from_a_superseded_subscription_is_refused(self):
        """Kalshi's seq is per SUBSCRIPTION, so a straggler from an old sid
        carries a sequence from a different namespace entirely."""
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError, match="superseded|subscription"):
            b.apply_delta({"market_ticker": "KXTEST", "side": "yes",
                           "price_dollars": "0.6000", "delta_fp": "1.00"},
                          seq=11, sid=2)
        assert b.publishable is False

    def test_crossed_book_is_refused(self):
        b = bk.OrderBook("KXTEST")
        with pytest.raises(bk.BookIntegrityError, match="crossed"):
            b.apply_snapshot(snapshot_msg(yes=[["0.8000", "10.00"]],
                                          no=[["0.7000", "5.00"]]), seq=1)
        assert b.publishable is False

    def test_duplicate_price_levels_in_a_snapshot_are_refused(self):
        b = bk.OrderBook("KXTEST")
        with pytest.raises(fp.FixedPointError, match="twice"):
            b.apply_snapshot(snapshot_msg(
                yes=[["0.6000", "10.00"], ["0.6000", "3.00"]]), seq=1)

    def test_checksum_is_withheld_from_an_unpublishable_book(self):
        b = self._synced()
        with pytest.raises(bk.BookIntegrityError):
            b.apply_delta({"market_ticker": "KXTEST", "side": "yes",
                           "price_dollars": "0.6000", "delta_fp": "1.00"},
                          seq=99, sid=1)                      # gap
        assert b.publishable is False
        with pytest.raises(bk.BookIntegrityError):
            b.checksum()

    def test_checksum_distinguishes_stream_position(self):
        """Two books with identical ladders at different positions are not the
        same observation, and checksum equality is what replay determinism is
        asserted on."""
        a, c = self._synced(), self._synced()
        c.generation, c.last_seq, c.sid = 6, 782, 99
        assert a.checksum() != c.checksum()


class TestArchiveIntegrityRegressions:
    def _archive(self, tmp_path, environment="demo"):
        return _arch(tmp_path, environment=environment)

    def test_interrupted_write_loses_the_last_record_not_the_file(self, tmp_path):
        """`fh.read()` buffers the whole member and then raises on a truncated
        trailer, discarding everything already decoded — so an interrupted
        write lost the entire hour while the docstring promised the opposite,
        and `verify()` called the empty result intact."""
        a = self._archive(tmp_path)
        for e in TestArchiveAndReplay()._stream():
            a.append(e)
        a.close()
        path = a.segment_paths()[0]["events_path"]
        raw = path.read_bytes()
        path.write_bytes(raw[:-5])
        records = a.read_unverified_diagnostic()
        assert len(records) >= 3, "everything before the torn record must survive"
        assert a.verify()["intact"] is False

    def test_corrupt_gzip_does_not_raise_out_of_read_all(self, tmp_path):
        a = self._archive(tmp_path)
        for e in TestArchiveAndReplay()._stream():
            a.append(e)
        a.close()
        path = a.segment_paths()[0]["events_path"]
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        path.write_bytes(bytes(raw))
        a.read_unverified_diagnostic()                        # zlib.error must not escape
        assert a.verify()["intact"] is False

    def test_tampered_record_is_rejected_on_read_not_only_in_verify(self, tmp_path):
        """`verify()` caught tampering and nothing called it, so `replay()`
        rebuilt a book from a rewritten record with no complaint."""
        import gzip as _gzip

        a = self._archive(tmp_path)
        for e in TestArchiveAndReplay()._stream():
            a.append(e)
        a.close()
        path = a.segment_paths()[0]["events_path"]
        with _gzip.open(path, "rt") as fh:
            lines = fh.read().splitlines()
        lines[0] = lines[0].replace('"0.6000"', '"0.9900"')
        with _gzip.open(path, "wt") as fh:
            fh.write("\n".join(lines) + "\n")
        # STRONGER than the legacy behaviour, and deliberately so. Tampering
        # breaks the chain, and a broken chain means nothing after the break can
        # be trusted either — the old reader dropped only the edited record and
        # returned the rest. The semantic assertion is unchanged: a tampered
        # record never reaches a caller, and verification fails.
        returned = a.read_unverified_diagnostic()
        assert all(r.get("raw", {}).get("msg", {}).get("yes_dollars_fp")
                   != [["0.9900", "5.00"]] for r in returned)
        assert len(returned) < 4
        assert a.verify()["intact"] is False

    def test_a_misplaced_demo_file_is_not_read_as_production(self, tmp_path):
        """The write-side guard is in-process only; a copy, an rsync or a
        restore defeats it, and replaying demo events as production evidence
        is a fabricated observation."""
        demo = _arch(tmp_path / "d", environment="demo")
        for e in TestArchiveAndReplay()._stream():
            demo.append(e)
        src = demo.segment_paths()[0]["events_path"]
        dst = (tmp_path / "p" / "env=production" / "venue=kalshi"
               / "date=2026-08-06" / "hour=12")
        dst.mkdir(parents=True)
        dst.joinpath("events.jsonl.gz").write_bytes(src.read_bytes())
        prod = _arch(tmp_path / "p", environment="production")
        # The property is unchanged — demo records never become production
        # evidence. Under the segment layout a transplanted file also carries a
        # segment id and manifest that do not belong to it, so it is rejected
        # earlier than the record-level environment check and never counted as
        # a "foreign record" that was read.
        assert prod.read_unverified_diagnostic() == []
        assert prod.read_verified() == []
        # `intact` is no longer the carrier of this property. An initialized
        # archive with no committed segments is genuinely intact — that used to
        # report INVALID only because an archive with no head was invalid by
        # construction, which is a different fact that happened to coincide.
        # What matters is that the transplanted bytes are not evidence: they sit
        # in a path the archive does not recognise, so no segment claims them
        # and no reader returns them.
        from app.realtime import segment as _sg
        out = _sg.verify_archive(tmp_path / "p", environment="production")
        assert out["records_read"] == 0 and out["segments"] == 0
        assert out["orphaned_committed_segments"] == []

    def test_venue_cannot_escape_its_path_component(self, tmp_path):
        with pytest.raises(ar.ArchiveError, match="safe path component"):
            _arch(tmp_path, environment="demo",
                            venue="../../env=production/venue=kalshi")

    def test_naive_receive_time_is_refused(self, tmp_path):
        """`astimezone()` reads a naive datetime as LOCAL time, so identical
        events landed in different hour partitions on different hosts."""
        a = self._archive(tmp_path)
        with pytest.raises(ar.ArchiveError, match="timezone-aware"):
            a.partition(datetime(2026, 8, 6, 12, 0, 0))

    def test_records_are_ordered_by_instant_not_by_timestamp_text(self, tmp_path):
        """`13:00-04:00` sorts before `12:00+00:00` as text and is five hours
        later as an instant."""
        earlier = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        later = earlier + timedelta(hours=5)
        recs = [{"collector_receive_time": later.astimezone(
                    timezone(timedelta(hours=-4))).isoformat(), "seq": 2},
                {"collector_receive_time": earlier.isoformat(), "seq": 1}]
        assert [r["seq"] for r in sorted(recs, key=ar._read_order)] == [1, 2]

    def test_string_seq_does_not_take_down_the_whole_read(self, tmp_path):
        recs = [{"collector_receive_time": "2026-08-06T12:00:00+00:00", "seq": "2"},
                {"collector_receive_time": "2026-08-06T12:00:00+00:00", "seq": 1}]
        assert [r["seq"] for r in sorted(recs, key=ar._read_order)] == [1, "2"]

    def test_one_malformed_record_does_not_abort_the_replay(self, tmp_path):
        stream = TestArchiveAndReplay()._stream()
        recs = [e.to_record() if hasattr(e, "to_record") else e for e in stream]
        a = self._archive(tmp_path)
        for e in stream:
            a.append(e)
        a.close()
        records = a.read_unverified_diagnostic()
        records[2]["raw"]["msg"].pop("price_dollars")
        out = ar.replay(records)            # KeyError must not escape
        assert out["events_rejected"] >= 1 and out["faults"]


class TestLatencyArithmetic:
    def test_percentiles_match_a_reference_implementation(self):
        """`int(p*n)` is a floor, which returned the (floor(p*n)+1)-th order
        statistic — making p99 identical to max for every n <= 100."""
        import math as _math

        values = [float(i) for i in range(1, 101)]
        q = ar._quantiles(values)
        for name, p in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
            expected = sorted(values)[_math.ceil(p * len(values)) - 1]
            assert q[name] == expected, name
        assert q["p99"] != q["max"] or len(values) < 100

    def test_percentiles_are_withheld_when_n_cannot_support_them(self):
        assert ar._quantiles([7.0])["p50"] is None
        assert ar._quantiles([1.0, 3.0])["p50"] is None
        q = ar._quantiles([float(i) for i in range(5)])
        assert q["p50"] is not None and q["p95"] is None and q["p99"] is None

    def test_empty_and_populated_hops_have_the_same_keys(self):
        assert set(ar._quantiles([])) == set(ar._quantiles([1.0, 2.0, 3.0]))

    def test_negative_samples_are_counted_never_dropped(self):
        recs = [{"receive_monotonic_ns": 100, "normalize_monotonic_ns": 50},
                {"receive_monotonic_ns": 100, "normalize_monotonic_ns": 200}]
        env_ = ar.latency_envelope(recs).to_dict()
        assert env_["receive_to_normalize_us"]["negative"] == 1

    def test_the_report_states_what_it_does_not_measure(self):
        cov = ar.latency_envelope([]).to_dict()["coverage"]
        assert cov["observation_gaps_measured"] is False
        assert cov["host_clock_offset_characterised"] is False
