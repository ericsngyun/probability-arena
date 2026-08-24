"""MARKET-MICROSTRUCTURE-ROW-BUILDER-001 — the consumer, proven before the tranche.

The sampling contract is already QUALIFIED. This suite proves the OTHER half:
that the transformation from raw tape to statistical dataset is the one the
preregistration expects. Feature arithmetic is checked against hand-derived
truth on tiny fixtures, never only against the engine's own output on live tape.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.microstructure import features as F
from app.microstructure import labels as L
from app.microstructure import rows as R
from app.microstructure.panel import DatasetRole, MarketMeta

T0 = datetime(2026, 8, 23, 18, 0, 0, tzinfo=timezone.utc)
T0_MS = T0.timestamp() * 1000


def view(bids, asks, *, at_ms=T0_MS, last_mod=None):
    return F.BookView("A", bids, asks, last_mod, at_ms)


# ---------------------------------------------------------------------------
# 10. Hand-solvable feature truth
# ---------------------------------------------------------------------------

HAND_BIDS = {4200: 1000, 4100: 500}     # 0.42 x 10.00, 0.41 x 5.00
HAND_ASKS = {4300: 2000, 4400: 300}     # 0.43 x 20.00, 0.44 x 3.00


def test_m0_matches_hand_computed_truth():
    m = F.compute_m0(view(HAND_BIDS, HAND_ASKS), seconds_to_close=100.0)
    assert m["mid"] == pytest.approx(0.4250)              # (4200+4300)/2 /1e4
    assert m["spread"] == pytest.approx(0.0100)           # 100 /1e4
    assert m["depth_bid_l1"] == pytest.approx(10.0)       # 1000 /100
    assert m["depth_ask_l1"] == pytest.approx(20.0)       # 2000 /100
    assert m["imbalance_l1"] == pytest.approx((10 - 20) / 30)
    # mid 4250 +/- 500 -> [3750, 4750] catches every level here
    assert m["depth_bid_5c"] == pytest.approx(15.0)       # (1000+500)/100
    assert m["depth_ask_5c"] == pytest.approx(23.0)       # (2000+300)/100
    assert m["imbalance_5c"] == pytest.approx((1500 - 2300) / 3800)
    assert m["levels_bid"] == 2 and m["levels_ask"] == 2
    # (4200*2000 + 4300*1000) / 3000 = 4233.3333
    assert m["microprice"] == pytest.approx(0.42333333, abs=1e-8)
    assert m["micro_minus_mid"] == pytest.approx(-0.00166667, abs=1e-8)
    assert m["dist_to_bound"] == pytest.approx(0.4250)


def test_five_cent_band_actually_excludes_levels_outside_it():
    bids = {4200: 1000, 3600: 900}      # 3600 is 6.5c below mid -> excluded
    asks = {4300: 2000, 4900: 700}      # 4900 is 6.5c above mid -> excluded
    m = F.compute_m0(view(bids, asks), seconds_to_close=None)
    assert m["depth_bid_5c"] == pytest.approx(10.0)
    assert m["depth_ask_5c"] == pytest.approx(20.0)


def test_dist_to_bound_is_symmetric_and_small_near_the_edges():
    hi = F.compute_m0(view({9700: 100}, {9800: 100}), seconds_to_close=None)
    assert hi["mid"] == pytest.approx(0.975)
    assert hi["dist_to_bound"] == pytest.approx(0.025)
    lo = F.compute_m0(view({200: 100}, {300: 100}), seconds_to_close=None)
    assert lo["dist_to_bound"] == pytest.approx(0.025)


def test_signed_depth_flow_is_hand_computable():
    acc = F.FlowAccumulator()
    acc.add_delta(T0_MS - 500, 300, "yes", 4200)     # +3.00 at bid
    acc.add_delta(T0_MS - 400, -100, "yes", 4200)    # -1.00 at bid
    acc.add_delta(T0_MS - 300, 500, "no", 4300)      # +5.00 at ask
    f = F.compute_m1_flow(acc, T0_MS)
    assert f["delta_count_1s"] == 3
    # (300 - 100 - 500)/100 = -3.00
    assert f["signed_depth_flow_1s"] == pytest.approx(-3.0)


def test_signed_trade_flow_signs_by_taker_side_and_respects_the_lag():
    acc = F.FlowAccumulator()
    # inside the LAGGED window (t-1000-1000, t-1000]
    acc.add_trade(T0_MS - 1500, 4.0)
    acc.add_trade(T0_MS - 1200, -1.0)
    # inside the raw 1 s window but INSIDE the lag -> must be excluded
    acc.add_trade(T0_MS - 100, 99.0)
    f = F.compute_m1_flow(acc, T0_MS, trade_lag_ms=1000)
    assert f["trade_count_1s"] == 2
    assert f["signed_trade_flow_1s"] == pytest.approx(3.0)


def test_absent_sentinel_is_literally_none_and_never_a_number():
    """Asserted against `None` itself, NOT against `F.NOT_PROVIDED`.

    Comparing an absent value to the very constant that defines absence is
    self-referential: redefining `NOT_PROVIDED = 0.0` would keep such a test
    green while every unobserved field silently became a real zero. A mutation
    campaign caught exactly that, so the literal is asserted here.
    """
    assert F.NOT_PROVIDED is None
    assert not isinstance(F.NOT_PROVIDED, (int, float))


def test_one_sided_book_yields_no_mid_rather_than_a_fabricated_zero():
    m = F.compute_m0(view({4200: 1000}, {}), seconds_to_close=None)
    for k in ("mid", "spread", "microprice", "dist_to_bound", "imbalance_5c"):
        assert m[k] is None, f"{k} must be absent, not a fabricated number"
        assert m[k] != 0.0 or m[k] is None
    assert m["levels_bid"] == 1 and m["levels_ask"] == 0
    assert m["depth_bid_l1"] == pytest.approx(10.0)


def test_empty_book_reports_absence_rather_than_zero_depth():
    m = F.compute_m0(view({}, {}), seconds_to_close=None)
    assert m["depth_bid_l1"] is None and m["depth_ask_l1"] is None
    assert m["imbalance_l1"] is None
    # level COUNTS are genuine zeros -- the venue told us the side is empty
    assert m["levels_bid"] == 0 and m["levels_ask"] == 0


def test_realized_vol_is_absent_not_zero_when_too_few_points():
    acc = F.FlowAccumulator()
    acc.add_mid(T0_MS - 500, 0.42)
    assert F.compute_m1_flow(acc, T0_MS)["realized_vol_5s"] is None


# ---------------------------------------------------------------------------
# 9. M0 subset of M1, membership auditable
# ---------------------------------------------------------------------------

def test_m0_is_a_strict_subset_of_m1_and_the_difference_is_the_flow_set():
    assert F.m0_is_subset_of_m1()
    assert set(F.M1_FEATURES) - set(F.M0_FEATURES) == set(F.M1_FLOW_FEATURES)
    # every base name on every window, MINUS the stdev features whose window
    # cannot carry one (Amendment 3)
    expected = (len(F.FLOW_WINDOWS_S) * len(F.M1_FLOW_BASE)
                - (len(F.FLOW_WINDOWS_S) - len(F.STDEV_WINDOWS_S)))
    assert len(F.M1_FLOW_FEATURES) == expected == 17


# ---------------------------------------------------------------------------
# Amendment 3 — a stdev feature may not exist on a window that cannot carry one
# ---------------------------------------------------------------------------

def test_stdev_features_require_at_least_two_returns_in_their_window():
    """`window_seconds * sampling_hz >= 2`, as a standing invariant.

    A sample standard deviation needs two differences, so three samples. This
    turns the `realized_vol_1s` defect -- 100% missing at every sample in every
    session, because a 1 s window on a 1 Hz grid holds one midpoint -- from a
    one-off bug into a feature-definition rule that any future window or
    sampling-rate change must satisfy.
    """
    for w in F.STDEV_WINDOWS_S:
        assert w * F.SAMPLING_HZ >= F.MIN_SAMPLES_FOR_STDEV
    for w in F.FLOW_WINDOWS_S:
        admissible = w * F.SAMPLING_HZ >= F.MIN_SAMPLES_FOR_STDEV
        assert (f"realized_vol_{w}s" in F.M1_FLOW_FEATURES) is admissible


def test_realized_vol_1s_is_gone_and_has_no_replacement():
    assert "realized_vol_1s" not in F.M1_FLOW_FEATURES
    assert "realized_vol_5s" in F.M1_FLOW_FEATURES
    assert "realized_vol_30s" in F.M1_FLOW_FEATURES
    # no substitute snuck in for the 1 s window
    one_s = {f for f in F.M1_FLOW_FEATURES if f.endswith("_1s")}
    assert one_s == {"delta_count_1s", "signed_depth_flow_1s",
                     "quote_reversal_1s", "trade_count_1s",
                     "signed_trade_flow_1s"}
    assert not any(k in f for f in one_s
                   for k in ("vol", "sigma", "stdev", "ewma", "abs"))


def test_no_feature_column_can_be_structurally_always_missing():
    """Every declared flow column must be computable for SOME input."""
    acc = F.FlowAccumulator()
    for i in range(120):
        ms = T0_MS - 30_000 + i * 250
        acc.add_delta(ms, 100, "yes", 4200)
        acc.add_mid(ms, 0.42 + i * 0.0001)
        acc.add_best(ms, 4200 + (i % 3), 4300)
        acc.add_trade(ms, 1.0)
    out = F.compute_m1_flow(acc, T0_MS)
    assert set(out) == set(F.M1_FLOW_FEATURES)
    missing = [k for k, v in out.items() if v is None]
    assert not missing, f"columns that can never be computed: {missing}"


def test_flow_set_is_exactly_the_preregistered_names():
    assert set(F.M1_FLOW_BASE) == {
        "delta_count", "signed_depth_flow", "quote_reversal",
        "realized_vol", "trade_count", "signed_trade_flow"}
    assert F.FLOW_WINDOWS_S == (1, 5, 30)


def test_controls_are_not_predictive_features():
    for c in F.M0_CONTROLS:
        assert c not in F.M0_FEATURES and c not in F.M1_FEATURES


# ---------------------------------------------------------------------------
# 5. Structural separation of features and labels
# ---------------------------------------------------------------------------

def _imports_of(mod):
    tree = ast.parse(inspect.getsource(mod))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            out |= {f"{n.module}.{a.name}" for a in n.names}
    return out


def test_features_never_imports_labels_and_labels_never_imports_features():
    assert not any("labels" in i for i in _imports_of(F))
    assert not any("features" in i for i in _imports_of(L))


def test_no_future_word_reaches_the_feature_module():
    src = inspect.getsource(F)
    for banned in ("mid_grid", "session_end", "compute_labels", "horizon"):
        assert banned not in src, f"feature module references {banned!r}"


# ---------------------------------------------------------------------------
# 4. Metamorphic: no future information in features
# ---------------------------------------------------------------------------

def test_metamorphic_future_events_cannot_change_features_at_t():
    def build(with_future):
        acc = F.FlowAccumulator()
        for i in range(20):
            acc.add_delta(T0_MS - 900 + i * 10, 100, "yes", 4200)
            acc.add_mid(T0_MS - 900 + i * 10, 0.42 + i * 0.0001)
            acc.add_best(T0_MS - 900 + i * 10, 4200, 4300)
        acc.add_trade(T0_MS - 1500, 2.0)
        if with_future:
            for j in range(5000):
                acc.add_delta(T0_MS + 1 + j, 9999, "no", 4300)
                acc.add_trade(T0_MS + 1 + j, -50.0)
                acc.add_best(T0_MS + 1 + j, 100, 9900)
                acc.add_mid(T0_MS + 1 + j, 0.99)
        return F.compute_m1_flow(acc, T0_MS)
    assert build(False) == build(True)


def test_a_frame_exactly_at_t_counts_and_one_microsecond_later_does_not():
    acc = F.FlowAccumulator()
    acc.add_delta(T0_MS, 100, "yes", 4200)
    assert F.compute_m1_flow(acc, T0_MS)["delta_count_1s"] == 1
    acc.add_delta(T0_MS + 0.001, 100, "yes", 4200)
    assert F.compute_m1_flow(acc, T0_MS)["delta_count_1s"] == 1


# ---------------------------------------------------------------------------
# 5/7. Labels: future-only, horizon-specific, never zero when missing
# ---------------------------------------------------------------------------

def test_labels_are_future_differences_and_horizon_specific():
    grid = {round(T0_MS + 1000): 0.43, round(T0_MS + 5000): 0.44,
            round(T0_MS + 30000): 0.40}
    # the session runs well past the 300 s endpoint, so an unavailable 300 s
    # label is about a MISSING MID, not about the session running out
    labs = L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid=grid,
                            session_end_ms=T0_MS + 400_000)
    assert labs[1].value == pytest.approx(0.01) and labs[1].available
    assert labs[5].value == pytest.approx(0.02) and labs[5].available
    assert labs[30].value == pytest.approx(-0.02) and labs[30].available
    # 300 s endpoint has no published mid -> UNAVAILABLE, and NOT zero
    assert labs[300].available is False
    assert labs[300].value is None
    assert labs[300].reason == L.REASON_NO_ENDPOINT


def test_unavailable_is_never_zero_return():
    grid = {}
    labs = L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid=grid,
                            session_end_ms=T0_MS + 1_000_000)
    for h in L.HORIZONS_S:
        assert labs[h].value is None, "a missing endpoint must not become 0.0"
        assert labs[h].available is False


def test_a_row_survives_when_only_some_horizons_are_computable():
    grid = {round(T0_MS + 1000): 0.43}
    labs = L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid=grid,
                            session_end_ms=T0_MS + 100000)
    assert labs[1].available and not labs[30].available
    assert len(labs) == len(L.HORIZONS_S)


def test_endpoint_past_session_end_is_its_own_reason():
    labs = L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid={},
                            session_end_ms=T0_MS + 10_000)
    assert labs[300].reason == L.REASON_PAST_SESSION_END
    assert labs[1].reason == L.REASON_NO_ENDPOINT


# ---------------------------------------------------------------------------
# 6. No silent forward-fill
# ---------------------------------------------------------------------------

def test_no_forward_fill_across_an_unpublishable_endpoint():
    # A mid exists 3 s before the 30 s endpoint but NOT at it.
    grid = {round(T0_MS + 27000): 0.41}
    labs = L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid=grid,
                            session_end_ms=T0_MS + 100000)
    assert labs[30].available is False, "must not reuse the last mid before t+30"
    assert labs[30].value is None


def test_tolerance_is_bounded_and_frozen():
    assert L.ENDPOINT_TOLERANCE_MS == 500
    inside = {round(T0_MS + 30000 - 400): 0.44}
    outside = {round(T0_MS + 30000 - 600): 0.44}
    assert L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid=inside,
                            session_end_ms=T0_MS + 1e6)[30].available is True
    assert L.compute_labels(t_ms=T0_MS, mid_at_t=0.42, mid_grid=outside,
                            session_end_ms=T0_MS + 1e6)[30].available is False


def test_no_base_mid_means_no_label_at_any_horizon():
    grid = {round(T0_MS + 1000): 0.43}
    labs = L.compute_labels(t_ms=T0_MS, mid_at_t=None, mid_grid=grid,
                            session_end_ms=T0_MS + 1e6)
    assert all(not labs[h].available for h in L.HORIZONS_S)
    assert labs[1].reason == L.REASON_NO_BASE_MID


# ---------------------------------------------------------------------------
# 3. Panel membership is binding, at the boundary second
# ---------------------------------------------------------------------------

def test_panel_governs_a_half_open_interval_and_the_boundary_is_explicit():
    sched = R.PanelSchedule(ticks=[
        (1000.0, frozenset({"A"}), "tick0"),
        (4000.0, frozenset({"B"}), "tick1"),
    ])
    assert sched.governing(999.0) is None            # before the first decision
    assert sched.governing(1000.0)[2] == "tick0"     # inclusive lower edge
    assert sched.governing(3999.0)[2] == "tick0"
    assert sched.governing(4000.0)[2] == "tick1"     # next tick takes over AT t_k
    assert sched.governing(9999.0)[2] == "tick1"


# ---------------------------------------------------------------------------
# 1/2. Row identity and cadence constants
# ---------------------------------------------------------------------------

REQUIRED_ROW_FIELDS = {
    "session_id", "panel_tick_id", "ticker", "series", "event_id",
    "subscription_generation", "sample_time", "TTE_seconds",
    "capture_commit", "feature_schema_version", "label_schema_version",
    "preregistration_version", "dataset_role", "session_status",
}


def test_sample_cadence_is_one_hertz():
    assert R.SAMPLE_INTERVAL_S == 1


def test_dataset_role_must_be_typed_in_the_builder():
    with pytest.raises(ValueError, match="typed"):
        R.build_rows(env_dir=None, panel_schedule=R.PanelSchedule([]),
                     markets={}, session_id="s", capture_commit="c",
                     dataset_role="whatever", preregistration_version="v")


# ---------------------------------------------------------------------------
# Integration on a tiny synthetic tape with hand-known expected output
# ---------------------------------------------------------------------------

import gzip as _gzip
import json as _json


def _rec(ms, mtype, ticker, seq, sid, msg):
    iso = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")
    return {"message_type": mtype, "market_ticker": ticker, "seq": seq,
            "subscription_id": sid, "subscription_generation": 1,
            "received_at_utc": iso,
            "normalized_event": {"event_type": mtype, "channel": mtype,
                                 "market_ticker": ticker,
                                 "subscription_generation": 1},
            "raw_event": {"msg": msg, "seq": seq, "sid": sid, "type": mtype}}


def _snapshot_msg(ticker, bid_px, bid_sz, ask_px, ask_sz):
    """The venue's real shape: `yes_dollars_fp` / `no_dollars_fp`, both
    YES-scaled, as confirmed against production tape."""
    return {"market_ticker": ticker,
            "yes_dollars_fp": [[f"{bid_px:.4f}", f"{bid_sz:.2f}"]],
            "no_dollars_fp": [[f"{ask_px:.4f}", f"{ask_sz:.2f}"]]}


def _write_tape(tmp_path, records, opened_ms):
    env = tmp_path / "env=production"
    seg = env / "segment=synthetic.0000"
    seg.mkdir(parents=True)
    (seg / "manifest.json").write_text(_json.dumps({
        "opened_at": datetime.fromtimestamp(
            opened_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")}))
    with _gzip.open(seg / "events.jsonl.gz", "wt") as fh:
        for r in records:
            fh.write(_json.dumps(r) + "\n")
    return env


@pytest.fixture
def tiny_tape(tmp_path):
    """Two markets, 12 s, both publishable from t=0."""
    base = T0_MS
    recs, seq = [], 0
    for i, tk in enumerate(("AAA", "BBB")):
        seq += 1
        recs.append(_rec(base, "orderbook_snapshot", tk, seq, 1,
                         _snapshot_msg(tk, 0.42, 10.0, 0.43, 20.0)))
    for s in range(1, 13):
        for tk in ("AAA", "BBB"):
            seq += 1
            recs.append(_rec(base + s * 1000, "orderbook_delta", tk, seq, 1,
                             {"market_ticker": tk, "side": "yes",
                              "price_dollars": "0.4200", "delta_fp": "1.00",
                              "ts_ms": int(base + s * 1000)}))
    env = _write_tape(tmp_path, recs, base)
    markets = {tk: MarketMeta(tk, "KXTEST",
                              datetime.fromtimestamp(base / 1000, tz=timezone.utc)
                              + timedelta(hours=3))
               for tk in ("AAA", "BBB")}
    return env, markets, base


def _build(env, markets, base, *, panel=("AAA",), role=DatasetRole.VALIDATION):
    sched = R.PanelSchedule(ticks=[(base + 3000, frozenset(panel), "tick0")])
    return R.build_rows(env_dir=env, panel_schedule=sched, markets=markets,
                        session_id="s-test", capture_commit="cafe",
                        dataset_role=role, preregistration_version="Amendment 2")


def test_integration_only_panel_members_emit_rows(tiny_tape):
    env, markets, base = tiny_tape
    out = _build(env, markets, base, panel=("AAA",))
    tickers = {r["ticker"] for r in out["rows"]}
    assert tickers == {"AAA"}, "BBB is on the tape but not in the panel"
    assert out["report"]["skips"][R.SKIP_NOT_IN_PANEL] > 0


def test_integration_no_rows_before_the_first_panel_decision(tiny_tape):
    env, markets, base = tiny_tape
    out = _build(env, markets, base)
    first = min(r["sample_time_ms"] for r in out["rows"])
    assert first >= base + 3000, "a row was emitted before the governing tick"
    assert out["report"]["skips"][R.SKIP_BEFORE_FIRST_PANEL] > 0


def test_integration_cadence_is_exactly_one_row_per_second_per_market(tiny_tape):
    env, markets, base = tiny_tape
    out = _build(env, markets, base)
    stamps = sorted(r["sample_time_ms"] for r in out["rows"])
    assert len(stamps) == len(set(stamps)), "duplicate sample instants"
    gaps = {b - a for a, b in zip(stamps, stamps[1:])}
    assert gaps <= {1000.0}, f"non-1 Hz gaps: {gaps}"


def test_integration_rows_carry_full_provenance(tiny_tape):
    env, markets, base = tiny_tape
    out = _build(env, markets, base)
    assert out["rows"], "expected rows"
    for r in out["rows"]:
        assert REQUIRED_ROW_FIELDS <= set(r)
        assert r["dataset_role"] == DatasetRole.VALIDATION
        assert r["capture_commit"] == "cafe"
        assert r["panel_tick_id"] == "tick0"


def test_integration_features_and_labels_are_both_present_and_typed(tiny_tape):
    env, markets, base = tiny_tape
    out = _build(env, markets, base)
    r = out["rows"][0]
    assert set(r["m0"]) == set(F.M0_FEATURES)
    assert set(r["m1_flow"]) == set(F.M1_FLOW_FEATURES)
    assert set(r["labels"]) == {str(h) for h in L.HORIZONS_S}
    assert r["m0"]["mid"] == pytest.approx(0.425)
    assert r["m0"]["spread"] == pytest.approx(0.01)


def test_integration_validation_rows_are_never_marked_confirmation(tiny_tape):
    env, markets, base = tiny_tape
    out = _build(env, markets, base, role=DatasetRole.VALIDATION)
    assert all(r["dataset_role"] == DatasetRole.VALIDATION for r in out["rows"])
    assert not any(r["dataset_role"] == DatasetRole.CONFIRMATION
                   for r in out["rows"])


def test_ticker_is_not_an_orderbook_source_in_the_builder():
    assert "ticker" not in R.ORDERBOOK_TYPES
    assert set(R.ORDERBOOK_TYPES) == {"orderbook_delta", "orderbook_snapshot"}


def test_segment_order_is_by_opened_at_not_by_path_text(tmp_path):
    """`segment=X.r0001/events...` sorts before `segment=X/events...` because
    `.` < `/`. Ordering by the manifest is the fix."""
    env = tmp_path / "env=production"
    for name, opened in (("segment=s.0000", "2026-08-23T01:00:00Z"),
                         ("segment=s.0000.r0001", "2026-08-23T02:00:00Z")):
        d = env / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(_json.dumps({"opened_at": opened}))
    got = [d.name for d in R.segment_dirs_in_order(env)]
    assert got == ["segment=s.0000", "segment=s.0000.r0001"]


def test_mid_grid_is_observability_not_selection(tiny_tape):
    """A label endpoint must not depend on the market still being selected.

    Rotation and the 300 s horizon are the same length, so restricting the mid
    grid to panel members made 300 s label availability correlate with panel
    persistence -- which correlates with sustained activity. That is a
    selection-dependent target.
    """
    env, markets, base = tiny_tape
    # AAA is in the panel for the first tick only; BBB never is.
    sched = R.PanelSchedule(ticks=[
        (base + 3000, frozenset({"AAA"}), "tick0"),
        (base + 7000, frozenset({"BBB"}), "tick1"),
    ])
    out = R.build_rows(env_dir=env, panel_schedule=sched, markets=markets,
                       session_id="s", capture_commit="c",
                       dataset_role=DatasetRole.VALIDATION,
                       preregistration_version="Amendment 2")
    aaa = [r for r in out["rows"] if r["ticker"] == "AAA"]
    assert aaa, "AAA should have rows from tick0"
    # AAA leaves the panel at tick1, but its 1 s labels must still resolve,
    # because its price stayed observable the whole time.
    late = [r for r in aaa if r["sample_time_ms"] >= base + 5000]
    assert late, "expected AAA rows shortly before it leaves the panel"
    assert any(r["labels"]["1"]["available"] for r in late), (
        "a label failed because the market left the panel, not because the "
        "price was unobservable")


# ---------------------------------------------------------------------------
# The frozen bin-coverage rule used by the tranche verdict
# ---------------------------------------------------------------------------

def test_coverage_requires_the_whole_300s_interval_inside_the_bin():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "verdict", pathlib.Path("scripts/kalshi_microstructure_session_verdict.py"))
    V = importlib.util.module_from_spec(spec); spec.loader.exec_module(V)
    from app.microstructure import panel as PP

    # TTE 3000 -> 2700 : both in near_event (900, 7200]  -> counts
    assert V.interval_wholly_within(3000, PP.TTE_NEAR_EVENT) is True
    # TTE 1000 -> 700 : starts in near_event, ends in live_event -> straddles
    assert V.interval_wholly_within(1000, PP.TTE_NEAR_EVENT) is False
    # a bin grazed for one second never counts
    assert V.interval_wholly_within(901, PP.TTE_NEAR_EVENT) is False
    # late_resolution: TTE already negative and only getting more so
    assert V.interval_wholly_within(-10, PP.TTE_LATE_RESOLUTION) is True
    assert V.interval_wholly_within(100, PP.TTE_LATE_RESOLUTION) is False
    # live_event spans 0..900, so it is THREE 300 s intervals wide: any start
    # from 300 to 900 fits wholly inside it, and 299 spills into
    # late_resolution.
    assert V.interval_wholly_within(900, PP.TTE_LIVE_EVENT) is True
    assert V.interval_wholly_within(899, PP.TTE_LIVE_EVENT) is True
    assert V.interval_wholly_within(300, PP.TTE_LIVE_EVENT) is True
    assert V.interval_wholly_within(299, PP.TTE_LIVE_EVENT) is False


# ---------------------------------------------------------------------------
# The deterministic anchor scheduler
# ---------------------------------------------------------------------------

def _sched():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "sched", pathlib.Path("scripts/kalshi_microstructure_schedule_anchor.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_covering_intervals_counts_only_the_TARGET_bin():
    """Both endpoints must be in the target bin, not merely in the same bin.

    Testing `tte_bin(a) == tte_bin(b)` counts intervals lying wholly inside
    some OTHER stratum, which made a 900 s-wide `live_event` target report 34
    covering intervals for a 3 h session.
    """
    S = _sched()
    from app.microstructure import panel as PP
    ev = datetime(2026, 8, 24, 22, 40, tzinfo=timezone.utc)

    start = S.required_start(ev, PP.TTE_LIVE_EVENT)
    assert (ev - start).total_seconds() == 1200, "start is event - 20 min"
    # live_event spans 900 s -> exactly three complete 300 s intervals
    assert S.covering_intervals(ev, start, 10_800, PP.TTE_LIVE_EVENT) == 3
    # and the same session sits mostly in late_resolution, which must NOT
    # be credited to the live_event target
    assert S.covering_intervals(ev, start, 10_800, PP.TTE_LATE_RESOLUTION) > 3


def test_every_bin_has_a_feasible_anchor_for_a_three_hour_session():
    S = _sched()
    from app.microstructure import panel as PP
    ev = datetime(2026, 8, 24, 22, 40, tzinfo=timezone.utc)
    for b in (PP.TTE_FAR, PP.TTE_APPROACHING, PP.TTE_NEAR_EVENT,
              PP.TTE_LIVE_EVENT, PP.TTE_LATE_RESOLUTION):
        start = S.required_start(ev, b)
        assert S.covering_intervals(ev, start, 10_800, b) > 0, f"{b} infeasible"


def test_far_anchor_keeps_the_whole_session_above_the_edge():
    """Anchoring `far` just above 21,600 descends out of the stratum at once."""
    S = _sched()
    from app.microstructure import panel as PP
    ev = datetime(2026, 8, 24, 22, 40, tzinfo=timezone.utc)
    start = S.required_start(ev, PP.TTE_FAR)
    assert S.covering_intervals(ev, start, 10_800, PP.TTE_FAR) >= 30


def test_scheduler_reads_no_activity_or_price_signal():
    """Scheduling must be blind to everything but timing.

    Checked over identifiers and NON-docstring literals. Two false positives
    make the naive form useless: the module's own docstring says "price" and
    "volume" while asserting it reads neither, and `timedelta` contains
    "delta". A guard that cannot tell an assertion from a violation is not a
    guard.
    """
    import ast, inspect
    tree = ast.parse(inspect.getsource(_sched()))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value not in docstrings}
    used = {u.lower() for u in used} - {"timedelta"}
    for banned in ("volume", "price", "spread", "depth", "liquidity",
                   "open_interest", "ticker_frames", "imbalance", "microprice"):
        hits = {u for u in used if banned in u}
        assert not hits, f"scheduler references {hits}"
    # the only venue fields it may key on are timing and identity
    assert "event_time" in used and "occurrence_datetime" in used
