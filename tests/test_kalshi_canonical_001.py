"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 Gate 1 — canonical serialization invariants.

The fixpoint property is the whole contract:

    canonical_bytes(x) == canonical_bytes(parse(canonical_bytes(x)))

Once it holds, a digest taken before a write necessarily equals the digest
recomputed after a read, because both are over the same bytes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.realtime import canonical as cn

UTC = timezone.utc

# Realistic frames, verbatim shapes from the DEMO wire.
FIXTURES = {
    "subscribed": {"type": "subscribed", "id": 4,
                   "msg": {"channel": "orderbook_delta", "sid": 4}},
    "orderbook_snapshot": {
        "type": "orderbook_snapshot", "sid": 4, "seq": 1,
        "msg": {"market_ticker": "KXA", "market_id": "mid-1",
                "yes_dollars_fp": [["0.4700", "5.00"]],
                "no_dollars_fp": [["0.5100", "5.00"]]}},
    "orderbook_delta": {
        "type": "orderbook_delta", "sid": 4, "seq": 3,
        "msg": {"market_ticker": "KXA", "market_id": "mid-1",
                "price_dollars": "0.5100", "delta_fp": "201.00", "side": "no",
                "ts": "2026-08-08T00:49:08.065758Z", "ts_ms": 1786150148065}},
    "ticker": {
        "type": "ticker", "sid": 1,
        "msg": {"market_ticker": "KXA", "price_dollars": "0.5000",
                "yes_bid_dollars": "0.4700", "yes_ask_dollars": "0.5100",
                "volume_fp": "2.00", "dollar_volume": 1,
                "yes_ask_size_fp": "206.00", "ts": 1786150148,
                "ts_ms": 1786150148065, "time": "2026-08-08T00:49:08.065758Z"}},
    "trade": {"type": "trade", "sid": 3, "seq": 5,
              "msg": {"market_ticker": "KXA", "count_fp": "1.00",
                      "yes_price_dollars": "0.5000", "ts_ms": 1786150148065}},
    "market_lifecycle_v2": {
        "type": "market_lifecycle_v2", "sid": 2, "seq": 7,
        "msg": {"market_ticker": "KXA", "open_ts": 1786100000,
                "close_ts": 1786200000, "is_deactivated": False}},
    "error": {"type": "error", "sid": 4, "seq": 4,
              "msg": {"code": 14, "msg": "Market Ticker required"}},
}


class TestFixpoint:
    @pytest.mark.parametrize("name", sorted(FIXTURES))
    def test_every_frame_shape_is_a_fixpoint(self, name):
        cn.assert_fixpoint(FIXTURES[name])

    @pytest.mark.parametrize("name", sorted(FIXTURES))
    def test_digest_survives_the_round_trip(self, name):
        frame = FIXTURES[name]
        assert cn.digest_hex(frame) == cn.digest_hex(
            cn.parse_canonical(cn.canonical_bytes(frame)))

    def test_ts_ms_stays_an_integer(self):
        """The venue's millisecond stamp must never become a Decimal or a float
        on the way through — an integer is exactly self-inverse."""
        frame = FIXTURES["orderbook_delta"]
        back = cn.parse_canonical(cn.canonical_bytes(frame))
        assert back["msg"]["ts_ms"] == 1786150148065
        assert isinstance(back["msg"]["ts_ms"], int)
        assert not isinstance(back["msg"]["ts_ms"], bool)

    def test_decimal_round_trips_to_identical_bytes(self):
        for raw in ("0.5100", "201.00", "0", "-3.25", "1E+2", "100.00"):
            v = {"x": Decimal(raw)}
            assert cn.assert_fixpoint(v)

    def test_numerically_equal_decimals_share_one_digest(self):
        """Otherwise two equal values would produce two digests."""
        assert cn.digest_hex({"x": Decimal("1E+2")}) == cn.digest_hex({"x": Decimal("100")})
        assert cn.digest_hex({"x": Decimal("100.00")}) == cn.digest_hex({"x": Decimal("100")})

    def test_datetime_round_trips_at_fixed_precision(self):
        for us in (0, 1, 65758, 999999):
            dt = datetime(2026, 8, 8, 0, 49, 8, us, tzinfo=UTC)
            text = cn.canonical_datetime(dt)
            assert text.endswith("Z") and len(text.split(".")[1]) == 7  # 6 digits + Z
            assert cn.parse_canonical_datetime(text) == dt
            assert cn.assert_fixpoint({"t": dt})

    def test_a_zero_microsecond_timestamp_keeps_its_fraction(self):
        """`isoformat()` drops the fraction at exactly zero, so two timestamps a
        microsecond apart would serialise to different shapes."""
        dt = datetime(2026, 8, 8, 0, 0, 0, 0, tzinfo=UTC)
        assert cn.canonical_datetime(dt) == "2026-08-08T00:00:00.000000Z"

    def test_non_utc_input_is_normalised_not_rejected(self):
        east = timezone(timedelta(hours=-4))
        a = datetime(2026, 8, 8, 0, 49, 8, 65758, tzinfo=UTC)
        b = a.astimezone(east)
        assert cn.canonical_datetime(a) == cn.canonical_datetime(b)


class TestRefusals:
    def test_float_is_refused_outright(self):
        """The defect that made the archive write-only."""
        with pytest.raises(cn.CanonicalError, match="float"):
            cn.canonical_bytes({"data_age_ms": 30.0})

    def test_naive_datetime_is_refused(self):
        with pytest.raises(cn.CanonicalError, match="timezone-aware"):
            cn.canonical_datetime(datetime(2026, 8, 8, 0, 0, 0))

    def test_nan_and_infinity_are_refused(self):
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(cn.CanonicalError):
                cn.canonical_bytes({"x": bad})

    def test_bool_is_distinguished_from_int(self):
        """`isinstance(True, int)` is True; writing 1 for True would collapse
        two distinct values onto one digest."""
        assert cn.digest_hex({"x": True}) != cn.digest_hex({"x": 1})
        assert cn.canonical_bytes({"x": True}) == b'{"x":true}'

    def test_non_string_mapping_keys_are_refused(self):
        with pytest.raises(cn.CanonicalError, match="keys"):
            cn.canonical_bytes({1: "a"})

    def test_unknown_types_do_not_fall_through_to_repr(self):
        class Thing:
            def __repr__(self):
                return "<Thing>"

        with pytest.raises(cn.CanonicalError, match="no canonical representation"):
            cn.canonical_bytes({"x": Thing()})

    def test_lone_surrogate_value_is_a_typed_canonical_error_not_a_crash(self):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect C: a lone UTF-16
        surrogate (`"\\ud800"`) is legal JSON and round-trips through
        `json.loads` as an ordinary `str`, but `str.encode("utf-8")` refuses
        it with `UnicodeEncodeError` -- a `ValueError`, NOT a
        `CanonicalError`. Before this fix, that error surfaced raw at
        `canonical_bytes`'s final `.encode("utf-8")`, so `verify_chain`
        (which catches only `CanonicalError`) crashed on any record
        containing one rather than reporting a typed refusal."""
        with pytest.raises(cn.CanonicalError, match="surrogate"):
            cn.canonical_bytes("\ud800")
        with pytest.raises(cn.CanonicalError, match="surrogate"):
            cn.canonical_bytes({"x": "\ud800"})

    def test_lone_surrogate_mapping_key_is_a_typed_canonical_error_not_a_crash(self):
        with pytest.raises(cn.CanonicalError, match="surrogate"):
            cn.canonical_bytes({"\ud800": "v"})


class TestDeterminism:
    def test_key_order_does_not_affect_bytes(self):
        assert (cn.canonical_bytes({"b": 1, "a": 2})
                == cn.canonical_bytes({"a": 2, "b": 1}))

    def test_no_insignificant_whitespace(self):
        assert b" " not in cn.canonical_bytes({"a": 1, "b": [1, 2]})

    def test_utf8_is_preserved_not_escaped(self):
        raw = cn.canonical_bytes({"t": "café"})
        assert "café".encode("utf-8") in raw

    def test_bytes_are_stable_across_processes(self):
        """Guards against any hash-order or interpreter-state dependence."""
        import json
        import subprocess
        import sys

        payload = json.dumps({"z": 1, "a": [1, 2, 3], "m": {"k": "v"}})
        outs = set()
        for seed in ("0", "1", "12345"):
            r = subprocess.run(
                [sys.executable, "-c",
                 "import json,sys;from app.realtime import canonical as c;"
                 "print(c.canonical_bytes(json.loads(sys.argv[1])).decode())",
                 payload],
                capture_output=True, text=True,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                     "PYTHONPATH": "."})
            assert r.returncode == 0, r.stderr
            outs.add(r.stdout.strip())
        assert len(outs) == 1, outs
