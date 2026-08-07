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
        grid = fp.PriceGrid([{"start_dollars": "0.0001", "end_dollars": "0.9999",
                              "tick_dollars": "0.0001"}])
        assert grid.contains(fp.parse_price_units("0.6153"))

    def test_whole_cent_grid_accepted_and_off_grid_rejected(self):
        grid = fp.PriceGrid([{"start_dollars": "0.0100", "end_dollars": "0.9900",
                              "tick_dollars": "0.0100"}])
        assert grid.contains(fp.parse_price_units("0.6100"))
        with pytest.raises(fp.FixedPointError, match="off the market"):
            grid.validate(fp.parse_price_units("0.6153"))

    def test_complement_is_not_assumed_to_be_on_grid(self):
        """A complement is arithmetically exact but need not be a valid ORDER
        price; the grid decides that separately."""
        grid = fp.PriceGrid([{"start_dollars": "0.0000", "end_dollars": "0.5000",
                              "tick_dollars": "0.0100"}])
        p = fp.parse_price_units("0.4900")
        assert grid.contains(p)
        assert not grid.contains(fp.complement_price_units(p))

    def test_grid_does_not_key_off_the_structure_name(self):
        grid = fp.PriceGrid([{"start_dollars": "0.0001", "end_dollars": "0.9999",
                              "tick_dollars": "0.0001"}],
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
            no=[["0.3500", "8.00"]]), seq=1)
        assert b.best_yes_bid_units == 6000
        # best NO bid 0.35 -> YES offer at 0.65
        assert b.best_yes_ask_units == 6500
        assert b.top_of_book()["spread_units"] == 500

    def test_ladder_preserves_raw_side_and_price(self):
        b = bk.OrderBook("KXTEST")
        b.apply_snapshot(snapshot_msg(yes=[["0.6000", "10.00"]],
                                      no=[["0.3500", "8.00"]]), seq=1)
        ladder = b.yes_scale_ladder()
        assert ladder["bids"][0]["raw_side"] == "yes"
        ask = ladder["asks"][0]
        assert ask["raw_side"] == "no"
        assert ask["raw_price_units"] == 3500
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
        cmd = kx.build_resubscribe_snapshot(7, sid=3)
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

        for path in sorted(PKG.glob("*.py")):
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
    FILES = sorted(PKG.glob("*.py"))

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
        out = [env(snapshot_msg(yes=[["0.6000", "10.00"]], no=[["0.3500", "8.00"]]),
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
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        assert a.written == 4
        v = a.verify()
        assert v["intact"] and v["records"] == 4

    def test_demo_events_cannot_enter_a_production_archive(self, tmp_path):
        prod = ar.EventArchive(tmp_path, environment="production")
        with pytest.raises(ar.ArchiveError, match="never become"):
            prod.append(self._stream(environment="demo")[0])

    def test_archives_are_physically_separated(self, tmp_path):
        demo = ar.EventArchive(tmp_path, environment="demo")
        prod = ar.EventArchive(tmp_path, environment="production")
        demo.append(self._stream("demo")[0])
        prod.append(self._stream("production")[0])
        assert demo.read_all() and prod.read_all()
        assert len(demo.read_all()) == 1 and len(prod.read_all()) == 1
        assert "env=demo" in str(demo.partition(NOW))
        assert "env=production" in str(prod.partition(NOW))

    def test_replay_is_deterministic(self, tmp_path):
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        records = a.read_all()
        first, second = ar.replay(records), ar.replay(records)
        assert first["checksums"] == second["checksums"]
        assert first["events_applied"] == 4 and first["events_rejected"] == 0
        assert first["publishable"]["KXTEST"] is True
        assert first["external_calls"] == 0 and first["persisted"] is False

    def test_replay_reproduces_the_live_book_exactly(self, tmp_path):
        live = bk.OrderBook("KXTEST")
        stream = self._stream()
        live.apply_snapshot(stream[0].raw["msg"], seq=1)
        for e in stream[1:]:
            live.apply_delta(e.raw["msg"], seq=e.seq)
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in stream:
            a.append(e)
        assert ar.replay(a.read_all())["checksums"]["KXTEST"] == live.checksum()

    def test_replay_surfaces_a_gap_rather_than_absorbing_it(self, tmp_path):
        stream = self._stream()
        stream[2].seq = 99          # punch a hole
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in stream:
            a.append(e)
        out = ar.replay(a.read_all())
        assert out["events_rejected"] >= 1
        assert any("gap" in f["error"] for f in out["faults"])
        assert out["publishable"]["KXTEST"] is False

    def test_malformed_tail_loses_one_record_not_the_file(self, tmp_path):
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        path = next(tmp_path.rglob("events.jsonl.gz"))
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write('{"truncated": \n')
        assert len(a.read_all()) == 4
        assert a.truncated_records == 1

    def test_latency_is_decomposed_not_a_single_number(self, tmp_path):
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        env_ = ar.latency_envelope(a.read_all()).to_dict()
        assert set(env_) >= {"venue_to_receive_ms", "receive_to_normalize_us",
                             "normalize_to_book_us"}
        assert set(env_["receive_to_normalize_us"]) >= {"p50", "p95", "p99", "max"}

    def test_rest_reconciliation_reports_and_resyncs(self):
        b = bk.OrderBook("KXTEST")
        b.apply_snapshot(snapshot_msg(yes=[["0.6000", "10.00"]],
                                      no=[["0.3500", "8.00"]]), seq=1)
        ok = ar.reconcile_with_rest(b, {"yes_bid_dollars": "0.6000",
                                        "yes_ask_dollars": "0.6500"})
        assert ok["agrees"] and ok["action"] == "none"
        bad = ar.reconcile_with_rest(b, {"yes_bid_dollars": "0.5900",
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
        a = ar.EventArchive(tmp_path, environment="demo")
        for e in self._stream():
            a.append(e)
        blob = json.dumps(a.read_all()).lower()
        for needle in ("private key", "begin rsa", "kalshi-access-signature",
                       "api_key", "secret", "password"):
            assert needle not in blob


class TestFixtureTransport:
    def test_fixture_transport_needs_no_credential_or_network(self):
        t = kx.FixtureTransport([{"type": "subscribed"}])
        assert list(t.iter_frames()) == [{"type": "subscribed"}]
        assert t.connected is False
