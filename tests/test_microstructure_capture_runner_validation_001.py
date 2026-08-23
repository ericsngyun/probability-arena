"""MARKET-MICROSTRUCTURE-CAPTURE-RUNNER-VALIDATION-001 — the twelve invariants.

This suite does not produce research data. Its only job is to demonstrate that
the prospective sampling contract frozen in MARKET-MICROSTRUCTURE-EDGE-001
Amendment 2 and its capture plan is implemented EXACTLY as preregistered.

Each test names the invariant it proves. A failure here blocks the tranche.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.microstructure import panel as P

T0 = datetime(2026, 8, 23, 18, 0, 0, tzinfo=timezone.utc)
EVENT = T0 + timedelta(hours=3)


def meta(ticker: str, series: str = "KXMLBGAME", event: datetime = EVENT):
    return P.MarketMeta(ticker=ticker, series=series, occurrence_datetime=event)


def ob(ticker: str, at: datetime, *, sid: int = 1, seq: int | None = None,
       kind: str = "orderbook_delta", gen: int | None = None):
    return P.Observation(market_ticker=ticker, received_at_utc=at,
                         message_type=kind, sid=sid, seq=seq,
                         subscription_generation=gen)


def session(markets: dict, *, open_at: datetime = T0, k: int = P.PANEL_K):
    return P.PanelSession(session_open=open_at, markets=markets, k=k)


def base(s: P.PanelSession, ticker: str, at: datetime, gen: int = 1):
    """Give a market a valid current-generation book."""
    s.observe(ob(ticker, at, kind="orderbook_snapshot", gen=gen))


def feed(s: P.PanelSession, ticker: str, n: int, *, end: datetime,
         step_s: float = 1.0):
    """`n` order-book deltas landing strictly inside the lookback ending at `end`."""
    for i in range(n):
        s.observe(ob(ticker, end - timedelta(seconds=step_s * i)))


# ---------------------------------------------------------------------------
# 1. Warmup is real
# ---------------------------------------------------------------------------

def test_invariant_01_warmup_emits_no_rows_even_while_frames_arrive():
    s = session({"A": meta("A")})
    base(s, "A", T0)
    feed(s, "A", 500, end=T0 + timedelta(seconds=299))

    for offset in (0, 1, 150, 299):
        t = T0 + timedelta(seconds=offset)
        d = s.decide_panel(t)
        assert d.is_warmup is True
        assert d.panel == (), f"row emitted during warmup at +{offset}s"
        assert all(a.selected is False for a in d.audit)
        assert all(a.reason_if_not_selected == P.NOT_SELECTED_WARMUP
                   for a in d.audit)
    # activity counters ARE accumulating during warmup -- that is the point
    assert s.activity_count("A", T0 + timedelta(seconds=299)) > 0


def test_invariant_01_first_decision_is_at_or_after_open_plus_300s():
    s = session({"A": meta("A")})
    ticks = s.decision_ticks(T0 + timedelta(seconds=1800))
    assert ticks[0] == T0 + timedelta(seconds=P.WARMUP_S)
    assert all(t >= T0 + timedelta(seconds=300) for t in ticks)
    assert s.is_warmup(T0 + timedelta(seconds=299)) is True
    assert s.is_warmup(T0 + timedelta(seconds=300)) is False


# ---------------------------------------------------------------------------
# 2. Eligibility uses only the trailing interval (metamorphic anti-lookahead)
# ---------------------------------------------------------------------------

def test_invariant_02_metamorphic_future_activity_cannot_change_panel_at_t():
    t = T0 + timedelta(seconds=600)
    markets = {c: meta(c) for c in "ABCDE"}

    def build():
        s = session(markets)
        for i, c in enumerate("ABCDE"):
            base(s, c, T0)
            feed(s, c, 40 + i * 10, end=t - timedelta(seconds=1))
        return s

    before = build()
    panel_before = before.decide_panel(t)

    after = build()
    # An enormous burst STRICTLY after t, on every market, in any order.
    for c in "ABCDE":
        for j in range(5000):
            after.observe(ob(c, t + timedelta(seconds=1 + j * 0.01)))
    panel_after = after.decide_panel(t)

    assert panel_before.panel == panel_after.panel
    assert [a.activity_lookback_count for a in panel_before.audit] == \
           [a.activity_lookback_count for a in panel_after.audit]


def test_invariant_02_frame_exactly_at_t_counts_and_one_past_t_does_not():
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A")})
    base(s, "A", T0)
    feed(s, "A", 29, end=t - timedelta(seconds=1))
    assert s.activity_count("A", t) == 29
    s.observe(ob("A", t))                      # exactly t -> inside (t-300, t]
    assert s.activity_count("A", t) == 30
    s.observe(ob("A", t + timedelta(microseconds=1)))   # past t -> excluded
    assert s.activity_count("A", t) == 30


def test_invariant_02_frame_at_lookback_lower_edge_is_excluded():
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A")})
    s.observe(ob("A", t - timedelta(seconds=P.LOOKBACK_S)))   # exactly t-300
    assert s.activity_count("A", t) == 0, "lookback is half-open (t-300, t]"


# ---------------------------------------------------------------------------
# 3. The activity threshold, exercised on both sides, as an integer
# ---------------------------------------------------------------------------

def test_invariant_03_threshold_is_29_ineligible_30_eligible():
    t = T0 + timedelta(seconds=600)
    for n, expect in ((29, False), (30, True)):
        s = session({"A": meta("A")})
        base(s, "A", T0)
        feed(s, "A", n, end=t - timedelta(seconds=1))
        row = s.decide_panel(t).audit[0]
        assert row.activity_lookback_count == n
        assert row.eligible is expect, f"{n} events should be eligible={expect}"
        if not expect:
            assert row.reason_if_not_selected == P.NOT_SELECTED_LOW_ACTIVITY


def test_invariant_03_rule_is_an_integer_count_not_a_float_rate():
    src = inspect.getsource(P.PanelSession.decide_panel)
    assert "MIN_OB_EVENTS_IN_LOOKBACK" in src
    assert "MIN_ACTIVITY_EVENTS_PER_S" not in src, (
        "eligibility must compare integer counts; comparing the float rate "
        "reintroduces rounding at the boundary")
    assert P.MIN_OB_EVENTS_IN_LOOKBACK == int(
        P.MIN_ACTIVITY_EVENTS_PER_S * P.LOOKBACK_S)


# ---------------------------------------------------------------------------
# 4. K means K, with no backfill and a frozen tie-break
# ---------------------------------------------------------------------------

def test_invariant_04_panel_never_exceeds_k_when_many_qualify():
    t = T0 + timedelta(seconds=600)
    markets = {f"M{i:02d}": meta(f"M{i:02d}") for i in range(30)}
    s = session(markets)
    for i, tk in enumerate(markets):
        base(s, tk, T0)
        feed(s, tk, 40 + i, end=t - timedelta(seconds=1))
    d = s.decide_panel(t)
    assert len(d.panel) == P.PANEL_K == 12
    assert sum(1 for a in d.audit if a.selected) == 12
    outranked = [a for a in d.audit if a.eligible and not a.selected]
    assert outranked and all(
        a.reason_if_not_selected == P.NOT_SELECTED_RANK for a in outranked)


def test_invariant_04_seven_qualify_means_seven_selected_no_backfill():
    t = T0 + timedelta(seconds=600)
    markets = {f"M{i:02d}": meta(f"M{i:02d}") for i in range(20)}
    s = session(markets)
    for i, tk in enumerate(markets):
        base(s, tk, T0)
        feed(s, tk, 40 if i < 7 else 5, end=t - timedelta(seconds=1))
    d = s.decide_panel(t)
    assert len(d.panel) == 7
    assert all(a.eligible for a in d.audit if a.selected)


def test_invariant_04_tie_break_is_activity_desc_then_ticker_ascending():
    t = T0 + timedelta(seconds=600)
    # Deliberate exact tie across five markets, inserted in scrambled order.
    names = ["MZZ", "MAA", "MMM", "MBB", "MYY"]
    markets = {n: meta(n) for n in names}
    s = session(markets, k=3)
    for tk in names:
        base(s, tk, T0)
        feed(s, tk, 50, end=t - timedelta(seconds=1))
    d = s.decide_panel(t)
    assert d.panel == ("MAA", "MBB", "MMM"), "ties must break ticker-ascending"
    # and the winner of a non-tie is activity, not alphabet
    s2 = session(markets, k=1)
    for tk in names:
        base(s2, tk, T0)
        feed(s2, tk, 50, end=t - timedelta(seconds=1))
    feed(s2, "MZZ", 40, end=t - timedelta(seconds=100))
    assert s2.decide_panel(t).panel == ("MZZ",)


def test_invariant_04_k_above_never_exceed_ceiling_is_refused():
    with pytest.raises(ValueError, match="never-exceed"):
        session({"A": meta("A")}, k=P.NEVER_EXCEED_CONCURRENCY + 1)


# ---------------------------------------------------------------------------
# 5. Ticker / volume / REST rankings cannot reach eligibility (source guard)
# ---------------------------------------------------------------------------

FORBIDDEN_IN_ELIGIBILITY = (
    "ticker_frames", "volume", "open_interest", "contracts", "notional",
    "rest", "p4_activity", "score", "prediction", "forecast", "alpha",
)


def test_invariant_05_eligibility_source_contains_no_forbidden_signal():
    """AST guard, not merely behavioural.

    A refactor must not be able to quietly reintroduce ticker activity, traded
    volume, REST rankings or a model score into the eligibility path.
    """
    for fn in (P.PanelSession.decide_panel, P.PanelSession.activity_count,
               P.PanelSession.observe):
        tree = ast.parse(inspect.getsource(fn).lstrip())
        names = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr.lower() for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
        for bad in FORBIDDEN_IN_ELIGIBILITY:
            hits = {n for n in names if bad in n}
            assert not hits, f"{fn.__name__} references forbidden {hits}"


def test_invariant_05_ticker_frames_do_not_count_as_activity():
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A")})
    base(s, "A", T0)
    for i in range(500):
        s.observe(P.Observation("A", t - timedelta(seconds=i * 0.5), "ticker",
                                sid=2, seq=None))
    assert s.activity_count("A", t) == 0
    row = s.decide_panel(t).audit[0]
    assert row.eligible is False
    assert row.reason_if_not_selected == P.NOT_SELECTED_LOW_ACTIVITY


def test_invariant_05_observation_type_carries_no_price_or_size_field():
    fields = set(P.Observation.__dataclass_fields__)
    for banned in ("price", "size", "volume", "yes_price", "no_price",
                   "contracts", "notional"):
        assert banned not in fields


# ---------------------------------------------------------------------------
# 6. Sequence contamination excludes, and then recovers
# ---------------------------------------------------------------------------

def test_invariant_06_sequence_fault_in_lookback_excludes_despite_huge_activity():
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A")})
    base(s, "A", T0)
    # A real gap on the order-book subscription: seq 10 -> 12.
    s.observe(ob("A", t - timedelta(seconds=200), seq=10))
    s.observe(ob("A", t - timedelta(seconds=199), seq=12))
    feed(s, "A", 400, end=t - timedelta(seconds=1), step_s=0.1)
    row = s.decide_panel(t).audit[0]
    assert row.activity_lookback_count >= 400
    assert row.sequence_clean is False
    assert row.eligible is False
    assert row.reason_if_not_selected == P.NOT_SELECTED_SEQUENCE_FAULT


def test_invariant_06_market_recovers_once_the_fault_ages_out_of_the_lookback():
    fault_at = T0 + timedelta(seconds=400)
    s = session({"A": meta("A"), }, )
    base(s, "A", T0)
    s.observe(ob("A", fault_at, seq=10))
    s.observe(ob("A", fault_at + timedelta(seconds=1), seq=12))

    t_contaminated = fault_at + timedelta(seconds=100)
    feed(s, "A", 60, end=t_contaminated - timedelta(seconds=1))
    assert s.decide_panel(t_contaminated).audit[0].eligible is False

    # Well past the fault: it has aged completely out of (t-300, t].
    t_clean = fault_at + timedelta(seconds=P.LOOKBACK_S + 60)
    feed(s, "A", 60, end=t_clean - timedelta(seconds=1))
    row = s.decide_panel(t_clean).audit[0]
    assert row.sequence_clean is True
    assert row.eligible is True, "must recover once the fault ages out"


def test_invariant_06_fault_is_per_subscription_and_contaminates_siblings():
    """L22: `seq` is per-SID. A gap contaminates every market on it."""
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A"), "B": meta("B")})
    for tk in ("A", "B"):
        base(s, tk, T0)
        feed(s, tk, 60, end=t - timedelta(seconds=2))
    s.observe(ob("A", t - timedelta(seconds=100), seq=10))
    s.observe(ob("A", t - timedelta(seconds=99), seq=12))
    d = s.decide_panel(t)
    assert all(a.sequence_clean is False for a in d.audit)
    assert d.panel == ()


# ---------------------------------------------------------------------------
# 7. Generation validity is CURRENT-generation validity
# ---------------------------------------------------------------------------

def test_invariant_07_stale_generation_book_is_not_publishable():
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A")})
    base(s, "A", T0, gen=1)
    feed(s, "A", 60, end=t - timedelta(seconds=2))
    assert s.decide_panel(t).audit[0].eligible is True

    # The subscription rolls to generation 2; A never re-snapshots.
    s.observe(ob("A", t - timedelta(seconds=1), gen=2))
    row = s.decide_panel(t).audit[0]
    assert row.book_state == P.PUB_AWAITING_GENERATION_SNAPSHOT
    assert row.eligible is False
    assert row.reason_if_not_selected == P.NOT_SELECTED_BOOK_UNPUBLISHABLE

    # It becomes publishable again only on its OWN snapshot for generation 2.
    base(s, "A", t, gen=2)
    assert s.decide_panel(t).audit[0].book_state == P.PUB_PUBLISHABLE


def test_invariant_07_never_based_and_halted_books_are_refused():
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A"), "B": meta("B")})
    feed(s, "A", 60, end=t - timedelta(seconds=2))          # no snapshot ever
    base(s, "B", T0)
    feed(s, "B", 60, end=t - timedelta(seconds=2))
    s.halt_book("B", "integrity fault")
    by = {a.market: a for a in s.decide_panel(t).audit}
    assert by["A"].book_state == P.PUB_SUBSCRIPTION_UNHEALTHY
    assert by["B"].book_state == P.PUB_BOOK_HALTED
    assert not by["A"].eligible and not by["B"].eligible


def test_invariant_07_reuses_the_collectors_own_vocabulary():
    from app.realtime import book as rb
    for const in (P.PUB_PUBLISHABLE, P.PUB_BOOK_HALTED,
                  P.PUB_AWAITING_GENERATION_SNAPSHOT,
                  P.PUB_SUBSCRIPTION_UNHEALTHY):
        assert const in (rb.PUB_PUBLISHABLE, rb.PUB_BOOK_HALTED,
                         rb.PUB_AWAITING_GENERATION_SNAPSHOT,
                         rb.PUB_SUBSCRIPTION_UNHEALTHY)


# ---------------------------------------------------------------------------
# 8. TTE boundaries, and every frozen bin edge
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tte,expect", [(601, True), (600, False), (599, False)])
def test_invariant_08_tte_boundary_is_strictly_greater_than_600s(tte, expect):
    t = T0 + timedelta(seconds=600)
    s = session({"A": meta("A", event=t + timedelta(seconds=tte))})
    base(s, "A", T0)
    feed(s, "A", 60, end=t - timedelta(seconds=2))
    row = s.decide_panel(t).audit[0]
    assert row.eligible is expect
    if not expect:
        assert row.reason_if_not_selected == P.NOT_SELECTED_TTE_TOO_SHORT


@pytest.mark.parametrize("tte,expect", [
    (21_601, P.TTE_FAR), (21_600, P.TTE_APPROACHING),
    (7_201, P.TTE_APPROACHING), (7_200, P.TTE_NEAR_EVENT),
    (901, P.TTE_NEAR_EVENT), (900, P.TTE_LIVE_EVENT),
    (1, P.TTE_LIVE_EVENT), (0, P.TTE_LIVE_EVENT),
    (-1, P.TTE_LATE_RESOLUTION), (-10_000, P.TTE_LATE_RESOLUTION),
])
def test_invariant_08_every_frozen_bin_edge(tte, expect):
    assert P.tte_bin(tte) == expect


def test_invariant_08_bins_are_total_and_disjoint():
    seen = {P.tte_bin(v) for v in range(-5, 30_000, 7)}
    assert seen == {P.TTE_FAR, P.TTE_APPROACHING, P.TTE_NEAR_EVENT,
                    P.TTE_LIVE_EVENT, P.TTE_LATE_RESOLUTION}


# ---------------------------------------------------------------------------
# 9. Panel changes only on the 300 s decision clock
# ---------------------------------------------------------------------------

def test_invariant_09_ticks_are_exactly_every_300s_from_open_plus_warmup():
    s = session({"A": meta("A")})
    ticks = s.decision_ticks(T0 + timedelta(seconds=1500))
    assert ticks == [T0 + timedelta(seconds=x)
                     for x in (300, 600, 900, 1200, 1500)]
    for a, b in zip(ticks, ticks[1:]):
        assert (b - a).total_seconds() == P.DECISION_TICK_S


def test_invariant_09_midtick_surge_does_not_enter_until_the_next_tick():
    markets = {"HOT": meta("HOT"), "OLD": meta("OLD")}
    s = session(markets, k=1)
    for tk in markets:
        base(s, tk, T0)
    feed(s, "OLD", 60, end=T0 + timedelta(seconds=598))
    t1 = T0 + timedelta(seconds=600)
    assert s.decide_panel(t1).panel == ("OLD",)

    # HOT erupts at t1+17s -- between scheduled ticks.
    feed(s, "HOT", 900, end=t1 + timedelta(seconds=17), step_s=0.01)
    # The contract evaluates only ON ticks; the next one is t1+300.
    ticks = [t for t in s.decision_ticks(T0 + timedelta(seconds=1200))
             if t > t1]
    assert ticks[0] == t1 + timedelta(seconds=300)
    assert s.decide_panel(ticks[0]).panel == ("HOT",)


# ---------------------------------------------------------------------------
# 10. The event cap is structurally unreachable before the safety stop
# ---------------------------------------------------------------------------

def test_invariant_10_capacity_relationship_holds_for_the_planned_session():
    P.assert_capacity_relationship(40_000_000, P.HARD_STOP_FPS, 10_800)


def test_invariant_10_startup_refused_when_the_inequality_breaks():
    # exactly equal is NOT enough
    with pytest.raises(ValueError, match="must exceed"):
        P.assert_capacity_relationship(3500 * 10_800, P.HARD_STOP_FPS, 10_800)
    # the profile's default, which actually censored day 2 slot C
    with pytest.raises(ValueError, match="right-censor"):
        P.assert_capacity_relationship(1_000_000, P.HARD_STOP_FPS, 10_800)
    # lengthening the session while leaving 40M "obviously large"
    with pytest.raises(ValueError):
        P.assert_capacity_relationship(40_000_000, P.HARD_STOP_FPS, 20_000)


# ---------------------------------------------------------------------------
# 11. The safety gate can actually stop the runner (positive control)
# ---------------------------------------------------------------------------

def test_invariant_11_synthetic_breach_halts_and_marks_the_session():
    at = T0 + timedelta(seconds=900)
    v = P.evaluate_safety_stop(P.HARD_STOP_FPS + 1, at=at)
    assert v.breached is True
    assert v.status == P.SESSION_SAFETY_HALT
    assert v.halt_at == at.isoformat()


def test_invariant_11_threshold_is_strict_and_clean_below_it():
    assert P.evaluate_safety_stop(P.HARD_STOP_FPS).breached is False
    assert P.evaluate_safety_stop(P.HARD_STOP_FPS).status == P.SESSION_OK
    assert P.evaluate_safety_stop(2704).breached is False   # profile's peak


def test_invariant_11_halted_session_can_never_be_confirmation_data():
    prov = P.RowProvenance(
        session_id="s-1", panel_tick=T0.isoformat(), market="A",
        subscription_generation=1, feature_schema_version="v1",
        capture_commit="deadbeef", preregistration_version="Amendment 2",
        dataset_role=P.DatasetRole.CONFIRMATION,
        session_status=P.SESSION_SAFETY_HALT)
    assert prov.usable_as_confirmation() is False


# ---------------------------------------------------------------------------
# 12. Provenance is typed, and the evaluator rejects on it
# ---------------------------------------------------------------------------

def test_invariant_12_dataset_role_is_typed_not_conventional():
    with pytest.raises(ValueError, match="dataset_role"):
        P.RowProvenance(
            session_id="s", panel_tick=T0.isoformat(), market="A",
            subscription_generation=1, feature_schema_version="v1",
            capture_commit="c", preregistration_version="Amendment 2",
            dataset_role="probably_fine", session_status=P.SESSION_OK)


@pytest.mark.parametrize("role,ok", [
    (P.DatasetRole.CONFIRMATION, True),
    (P.DatasetRole.VALIDATION, False),
    (P.DatasetRole.PROFILE, False),
])
def test_invariant_12_only_confirmation_rows_are_usable(role, ok):
    prov = P.RowProvenance(
        session_id="s", panel_tick=T0.isoformat(), market="A",
        subscription_generation=1, feature_schema_version="v1",
        capture_commit="c", preregistration_version="Amendment 2",
        dataset_role=role, session_status=P.SESSION_OK)
    assert prov.usable_as_confirmation() is ok


def test_invariant_12_every_required_provenance_field_is_present():
    required = {"session_id", "panel_tick", "market", "subscription_generation",
                "feature_schema_version", "capture_commit",
                "preregistration_version", "dataset_role", "session_status"}
    assert required <= set(P.RowProvenance.__dataclass_fields__)


# ---------------------------------------------------------------------------
# The eligibility-transition audit itself
# ---------------------------------------------------------------------------

def test_audit_explains_every_market_at_every_tick():
    t = T0 + timedelta(seconds=600)
    markets = {f"M{i:02d}": meta(f"M{i:02d}") for i in range(20)}
    s = session(markets)
    for i, tk in enumerate(markets):
        base(s, tk, T0)
        feed(s, tk, 40 + i if i % 3 else 2, end=t - timedelta(seconds=1))
    d = s.decide_panel(t)
    assert len(d.audit) == len(markets), "every market must be accounted for"
    for a in d.audit:
        assert (a.selected and a.reason_if_not_selected is None) or \
               (not a.selected and a.reason_if_not_selected is not None)
        assert a.tte_bin in {P.TTE_FAR, P.TTE_APPROACHING, P.TTE_NEAR_EVENT,
                             P.TTE_LIVE_EVENT, P.TTE_LATE_RESOLUTION}
    reasons = {a.reason_if_not_selected for a in d.audit if not a.selected}
    assert P.NOT_SELECTED_LOW_ACTIVITY in reasons
