"""KALSHI-TAPE-MANIFEST — the frozen qualification-session universe.

This suite exists because the manifest's job is to REFUSE when the venue
cannot supply the authorized universe, and a refusal is only evidence if the
same code demonstrably ACCEPTS a venue that can.

AGENTS.md research doctrine 7 in its exact shape: every important verdict gets
a positive control. `test_positive_control_*` forces the healthy condition and
proves the manifest becomes QUALIFIED with a full 4/4/4 universe. Without it,
`REFUSED` on the real DEMO frame would be indistinguishable from a manifest
builder that refuses everything — a plausible benign value emitted by a broken
path, which is the failure class this repo keeps rediscovering.

Every gate below is likewise tested in BOTH directions: the condition present
(refuses) and the condition absent (qualifies), against the same base
population. A one-directional gate test is satisfied by a repository in which
nothing works.

No socket is opened anywhere in this file.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.services import kalshi_tape_manifest as ktm
from app.services.kalshi_tape_manifest import (
    Candidate,
    EligibilityPolicy,
    ManifestError,
    SelectionPolicy,
    SnapshotWindow,
    STRATA,
    STRATUM_HIGH,
    STRATUM_LOW,
    STRATUM_MEDIUM,
    UNIVERSE_SIZE,
    build_candidate,
    build_manifest,
    frame_digest,
    render_markdown,
    series_of,
)

T0 = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def snapshot(started=T0, *, pages=3, environment="demo") -> SnapshotWindow:
    return SnapshotWindow(
        started_at=started,
        completed_at=started + timedelta(minutes=2),
        pages=pages,
        environment=environment,
        host="https://external-api.demo.kalshi.co/trade-api/v2",
        request_params={"route": "GET /markets", "status": "open"},
    )


def mkt(
    ticker,
    *,
    stat=1000.0,
    event=None,
    updated=None,
    bid="0.4000",
    ask="0.4100",
    bid_size="100.00",
    ask_size="100.00",
    close=None,
    strike="structured",
):
    """One raw venue market object, in the wire shape DEMO actually sends."""
    return {
        "ticker": ticker,
        "event_ticker": event if event is not None else ticker.rsplit("-", 1)[0],
        "title": ticker,
        "status": "active",
        "strike_type": strike,
        "market_type": "binary",
        "volume_24h_fp": f"{stat:.2f}",
        "volume_fp": f"{stat * 2:.2f}",
        "open_interest_fp": f"{stat:.2f}",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "yes_bid_size_fp": bid_size,
        "yes_ask_size_fp": ask_size,
        "updated_time": (updated or (T0 - timedelta(minutes=5))).isoformat().replace(
            "+00:00", "Z"),
        "close_time": (close or (T0 + timedelta(days=3))).isoformat().replace(
            "+00:00", "Z"),
    }


def healthy_frame(n_per_band=8):
    """A venue that CAN supply the authorized universe.

    Three activity bands two orders of magnitude apart, each spread over many
    distinct events and two contract structures, all freshly updated and
    two-sided. This is the positive control's input and the base population
    every gate test degrades by exactly one property.
    """
    rows = []
    for band, base in (("H", 1_000_000.0), ("M", 10_000.0), ("L", 100.0)):
        for i in range(n_per_band):
            rows.append(mkt(
                f"KX{band}SERIES{i}-EV{i}-STRIKE",
                stat=base * (1.0 + i / 100.0),
                event=f"KX{band}SERIES{i}-EV{i}",
                strike="structured" if i % 2 == 0 else "greater",
            ))
    return rows


# --------------------------------------------------------------------------------
# 1. POSITIVE CONTROL — the manifest can succeed
# --------------------------------------------------------------------------------

def test_positive_control_healthy_venue_qualifies():
    """Force the healthy condition; the verdict must become non-benign.

    Everything else in this file asserts a refusal. If this test fails, none of
    those refusals mean anything — they would be consistent with a builder that
    can only ever refuse.
    """
    m = build_manifest(healthy_frame(), snapshot=snapshot())
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    assert m["refusal_reasons"] == []

    strata = {s["stratum"]: s["members"] for s in m["universe"]}
    assert sorted(strata) == sorted(STRATA)
    for name in STRATA:
        assert len(strata[name]) == 4, f"stratum {name} has {len(strata[name])}"
    flat = [r for name in STRATA for r in strata[name]]
    assert len(flat) == UNIVERSE_SIZE
    assert len({r["ticker"] for r in flat}) == UNIVERSE_SIZE


def test_positive_control_strata_are_actually_ordered_and_separated():
    m = build_manifest(healthy_frame(), snapshot=snapshot())
    strata = {s["stratum"]: s["members"] for s in m["universe"]}
    hi = [r["statistic"] for r in strata[STRATUM_HIGH]]
    md = [r["statistic"] for r in strata[STRATUM_MEDIUM]]
    lo = [r["statistic"] for r in strata[STRATUM_LOW]]
    assert min(hi) > max(md) > min(md) > max(lo)
    sep = m["strata_ranges"]
    assert sep["high_over_medium"]["ratio"] > 2.0
    assert sep["medium_over_low"]["ratio"] > 2.0


def test_positive_control_spans_several_structures():
    m = build_manifest(healthy_frame(), snapshot=snapshot())
    us = m["universe_structures"]
    assert len(us["distinct_events"]) >= 6
    assert len(us["distinct_series"]) >= 4
    assert len(us["distinct_strike_types"]) >= 2


# --------------------------------------------------------------------------------
# 2. Each eligibility gate, in BOTH directions
# --------------------------------------------------------------------------------

def _degrade(mutate):
    """Healthy frame with `mutate` applied to every row. Returns the manifest."""
    rows = healthy_frame()
    for r in rows:
        mutate(r)
    return build_manifest(rows, snapshot=snapshot())


def test_gate_staleness_refuses_and_the_control_qualifies():
    old = (T0 - timedelta(days=40)).isoformat().replace("+00:00", "Z")
    m = _degrade(lambda r: r.__setitem__("updated_time", old))
    assert m["verdict"] == "REFUSED"
    assert any("eligible" in r for r in m["refusal_reasons"])
    assert m["population"]["eligible_count"] == 0
    # anti-vacuity: the identical frame with fresh timestamps qualifies
    assert build_manifest(healthy_frame(), snapshot=snapshot())["verdict"] == "QUALIFIED"


def test_gate_crossed_book_refuses():
    m = _degrade(lambda r: (r.__setitem__("yes_bid_dollars", "0.6000"),
                            r.__setitem__("yes_ask_dollars", "0.5000")))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("crossed_book") == 24
    assert m["frame_integrity"]["crossed_books"] == 24


def test_gate_negative_resting_size_refuses():
    m = _degrade(lambda r: r.__setitem__("yes_bid_size_fp", "-500.00"))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("negative_resting_size") == 24
    assert m["frame_integrity"]["negative_resting_sizes"] == 24


def test_gate_one_sided_book_refuses():
    m = _degrade(lambda r: r.__setitem__("yes_ask_dollars", "0.0000"))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("no_two_sided_quote") == 24


def test_gate_no_resting_size_refuses():
    m = _degrade(lambda r: (r.__setitem__("yes_bid_size_fp", "0.00"),
                            r.__setitem__("yes_ask_size_fp", "0.00")))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("no_resting_size") == 24


def test_gate_zero_statistic_refuses():
    m = _degrade(lambda r: r.__setitem__("volume_24h_fp", "0.00"))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get(
        f"zero_{ktm.STATISTIC_NAME}") == 24


def test_gate_market_closing_before_the_session_ends_refuses():
    """A market that closes inside the 4-hour maximum cannot carry the session."""
    soon = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    m = _degrade(lambda r: r.__setitem__("close_time", soon))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("closes") == 24


def test_gate_missing_updated_time_refuses():
    m = _degrade(lambda r: r.__setitem__("updated_time", None))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("no_updated_time") == 24


def test_future_updated_time_is_an_anomaly_not_freshness():
    future = (T0 + timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    m = _degrade(lambda r: r.__setitem__("updated_time", future))
    assert m["verdict"] == "REFUSED"
    assert any(k.startswith("updated_time_in_future")
               for k in m["population"]["ineligibility_histogram"])


# --------------------------------------------------------------------------------
# 3. The anti-blur gates: separability and structure diversity
# --------------------------------------------------------------------------------

def test_strata_within_a_factor_of_two_are_refused_as_unseparable():
    """Contiguous tertiles are ALWAYS ordered, so ordering cannot be the test.

    A flat continuum still produces three ordered tertiles. If that passed, the
    labels high/medium/low would be a relabelling of an arbitrary cut and would
    invite exactly the false confidence the manifest is supposed to prevent.
    """
    rows = []
    for i in range(24):
        rows.append(mkt(f"KXFLAT{i}-EV{i}-S", stat=1000.0 + i,
                        event=f"KXFLAT{i}-EV{i}",
                        strike="structured" if i % 2 else "greater"))
    m = build_manifest(rows, snapshot=snapshot())
    assert m["verdict"] == "REFUSED"
    assert any("not separable" in r for r in m["refusal_reasons"]), m["refusal_reasons"]
    # anti-vacuity: widen the bands and the SAME code qualifies
    assert build_manifest(healthy_frame(), snapshot=snapshot())["verdict"] == "QUALIFIED"


def test_boundary_tie_is_refused_even_though_the_ordering_is_valid():
    rows = []
    for band, base in (("H", 1_000_000.0), ("M", 10_000.0), ("L", 100.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}{i}-EV{band}{i}-S", stat=base,
                            event=f"KX{band}{i}-EV{band}{i}",
                            strike="structured" if i % 2 else "greater"))
    m = build_manifest(rows, snapshot=snapshot())
    # every member of a stratum shares one value, so no boundary tie yet:
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    # now make the medium band equal the low band -> boundary tie
    for r in rows:
        if r["ticker"].startswith("KXM"):
            r["volume_24h_fp"] = "100.00"
    m2 = build_manifest(rows, snapshot=snapshot())
    assert m2["verdict"] == "REFUSED"
    assert any("tied" in r or "not separable" in r for r in m2["refusal_reasons"])


def test_twelve_near_identical_markets_from_one_event_are_refused():
    """The 'must span several contract/event structures' requirement, enforced."""
    rows = []
    for band, base in (("H", 1_000_000.0), ("M", 10_000.0), ("L", 100.0)):
        for i in range(8):
            rows.append(mkt(f"KXONE-EVSAME-{band}{i}", stat=base * (1 + i / 100),
                            event="KXONE-EVSAME", strike="structured"))
    m = build_manifest(rows, snapshot=snapshot())
    assert m["verdict"] == "REFUSED"
    joined = " ".join(m["refusal_reasons"])
    assert "distinct events" in joined
    assert "distinct series" in joined
    assert "contributes" in joined  # max_per_event


def test_single_contract_structure_is_refused():
    rows = []
    for band, base in (("H", 1_000_000.0), ("M", 10_000.0), ("L", 100.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}S{i}-EV{band}{i}-S", stat=base * (1 + i / 100),
                            event=f"KX{band}S{i}-EV{band}{i}", strike="structured"))
    m = build_manifest(rows, snapshot=snapshot())
    assert m["verdict"] == "REFUSED"
    assert any("contract structures" in r for r in m["refusal_reasons"])


def test_fewer_than_twelve_eligible_refuses_and_does_not_pad():
    rows = healthy_frame()[:11]
    m = build_manifest(rows, snapshot=snapshot())
    assert m["verdict"] == "REFUSED"
    assert m["population"]["eligible_count"] == 11
    assert all(not s["members"] for s in m["universe"])
    assert any("must NOT be padded" in r for r in m["refusal_reasons"])


# --------------------------------------------------------------------------------
# 4. Determinism and reproducibility
# --------------------------------------------------------------------------------

def test_selection_is_invariant_to_input_order():
    rows = healthy_frame()
    a = build_manifest(rows, snapshot=snapshot())
    for seed in (1, 7, 99):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        b = build_manifest(shuffled, snapshot=snapshot())
        assert a["universe"] == b["universe"], f"selection moved under seed {seed}"
        assert a["candidate_population"]["frame_digest_sha256"] == \
            b["candidate_population"]["frame_digest_sha256"]


def test_ties_break_on_ticker_ascending_not_on_arrival_order():
    rows = [mkt(f"KXZ{i}-EV{i}-S", stat=500.0, event=f"KXZ{i}-EV{i}") for i in range(5)]
    rows += [mkt("KXAAA-EVA-S", stat=500.0, event="KXAAA-EVA")]
    cands = [build_candidate(r) for r in rows]
    ktm.apply_eligibility(cands, now=T0, policy=EligibilityPolicy())
    ordered = ktm.rank_candidates([c for c in cands if c.eligible])
    assert ordered[0].ticker == "KXAAA-EVA-S"
    assert [c.ticker for c in ordered] == sorted(c.ticker for c in ordered)


def test_manifest_is_pure_and_reads_no_clock():
    """Staleness is measured against the SNAPSHOT, not against wall time.

    If the builder read `datetime.now()`, the same archived frame would produce
    a different manifest tomorrow and 'reproducible selection' would be false.
    """
    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = []
    for band, base in (("H", 1_000_000.0), ("M", 10_000.0), ("L", 100.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}{i}-EV{band}{i}-S", stat=base * (1 + i / 100),
                            event=f"KX{band}{i}-EV{band}{i}",
                            updated=long_ago - timedelta(minutes=5),
                            close=long_ago + timedelta(days=3),
                            strike="structured" if i % 2 else "greater"))
    m = build_manifest(rows, snapshot=snapshot(started=long_ago))
    # Six years stale by wall clock, five minutes stale by its own snapshot.
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    row = m["candidate_population"]["eligible_ranked"][0]
    assert 0 < row["staleness_hours"] < 1


def test_manifest_json_round_trips():
    m = build_manifest(healthy_frame(), snapshot=snapshot())
    assert json.loads(json.dumps(m, sort_keys=True)) == m


def test_frame_digest_commits_to_the_statistic_not_only_membership():
    rows = healthy_frame()
    d1 = frame_digest([build_candidate(r) for r in rows])
    shuffled = list(rows)
    random.Random(3).shuffle(shuffled)
    assert frame_digest([build_candidate(r) for r in shuffled]) == d1
    rows[0]["volume_24h_fp"] = "999999.00"
    assert frame_digest([build_candidate(r) for r in rows]) != d1


# --------------------------------------------------------------------------------
# 5. Corruption must not be laundered into a benign value
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "1.2.3", "--5"])
def test_unparseable_statistic_raises_rather_than_reading_as_zero(bad):
    rows = healthy_frame()
    rows[0]["volume_24h_fp"] = bad
    with pytest.raises(ManifestError, match="not a number"):
        build_manifest(rows, snapshot=snapshot())


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_statistic_raises(bad):
    rows = healthy_frame()
    rows[0]["volume_24h_fp"] = bad
    with pytest.raises(ManifestError, match="non-finite"):
        build_manifest(rows, snapshot=snapshot())


def test_absent_field_is_zero_but_present_garbage_is_not():
    """Absent means 'no evidence of activity'; garbage means the frame is bad."""
    rows = healthy_frame()
    del rows[0]["volume_24h_fp"]
    m = build_manifest(rows, snapshot=snapshot())
    assert m["verdict"] in ("QUALIFIED", "REFUSED")  # it simply drops out
    assert all(r["ticker"] != rows[0]["ticker"]
               for s in m["universe"] for r in s["members"])


def test_duplicate_tickers_are_a_broken_frame_not_a_population():
    rows = healthy_frame()
    rows.append(dict(rows[0]))
    with pytest.raises(ManifestError, match="duplicate tickers"):
        build_manifest(rows, snapshot=snapshot())


def test_market_without_a_ticker_raises():
    with pytest.raises(ManifestError, match="no ticker"):
        build_candidate({"event_ticker": "X"})


# --------------------------------------------------------------------------------
# 6. The frame-integrity audit is computed over the INPUT, not the survivors
# --------------------------------------------------------------------------------

def test_integrity_audit_detects_a_statistic_that_is_not_a_24h_quantity():
    """The DEMO defect, in miniature.

    A market claiming trailing-24h volume that the venue has not touched in over
    24 hours is not reporting a trailing-24h quantity. The audit must say so
    even though every such market is ALSO dropped by the freshness gate — a
    funnel that only reports its output cannot tell you its input was corrupt.
    """
    old = (T0 - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    rows = healthy_frame()
    for r in rows:
        r["updated_time"] = old
    m = build_manifest(rows, snapshot=snapshot())
    fi = m["frame_integrity"]
    assert fi["markets_with_nonzero_statistic"] == 24
    assert fi["nonzero_statistic_but_not_updated_in_24h"] == 24
    assert fi["statistic_contradiction_rate"] == 1.0
    assert fi["statistic_is_internally_consistent"] is False
    # anti-vacuity: the same audit reports consistency on a fresh frame
    fi2 = build_manifest(healthy_frame(), snapshot=snapshot())["frame_integrity"]
    assert fi2["nonzero_statistic_but_not_updated_in_24h"] == 0
    assert fi2["statistic_is_internally_consistent"] is True


def test_integrity_audit_counts_corruption_the_gates_also_drop():
    rows = healthy_frame()
    rows[0]["yes_bid_size_fp"] = "-1.00"
    rows[1]["yes_bid_dollars"] = "0.9000"  # crossed
    m = build_manifest(rows, snapshot=snapshot())
    assert m["frame_integrity"]["negative_resting_sizes"] == 1
    assert m["frame_integrity"]["crossed_books"] == 1


# --------------------------------------------------------------------------------
# 7. Structure spreading inside a stratum
# --------------------------------------------------------------------------------

def test_within_stratum_pick_prefers_unclaimed_events():
    members = [
        Candidate(ticker=f"T{i}", event_ticker="EV-SAME", series="T", title="",
                  status="active", strike_type="structured", market_type="binary",
                  statistic=100.0 - i, lifetime_volume=0, open_interest=0,
                  yes_bid=0.4, yes_ask=0.41, yes_bid_size=1, yes_ask_size=1,
                  updated_time=T0, close_time=None)
        for i in range(3)
    ] + [
        Candidate(ticker=f"U{i}", event_ticker=f"EV-{i}", series="U", title="",
                  status="active", strike_type="structured", market_type="binary",
                  statistic=10.0 - i, lifetime_volume=0, open_interest=0,
                  yes_bid=0.4, yes_ask=0.41, yes_bid_size=1, yes_ask_size=1,
                  updated_time=T0, close_time=None)
        for i in range(3)
    ]
    picked = ktm.select_from_stratum(members, already_used_events=set(), count=4)
    assert [c.ticker for c in picked] == ["T0", "U0", "U1", "U2"]
    assert len(picked) == 4


def test_within_stratum_pick_fills_the_shortfall_rather_than_returning_short():
    members = [
        Candidate(ticker=f"T{i}", event_ticker="EV-SAME", series="T", title="",
                  status="active", strike_type="structured", market_type="binary",
                  statistic=100.0 - i, lifetime_volume=0, open_interest=0,
                  yes_bid=0.4, yes_ask=0.41, yes_bid_size=1, yes_ask_size=1,
                  updated_time=T0, close_time=None)
        for i in range(6)
    ]
    picked = ktm.select_from_stratum(members, already_used_events=set(), count=4)
    assert len(picked) == 4
    assert [c.ticker for c in picked] == ["T0", "T1", "T2", "T3"]


def test_series_of_reads_the_ticker_head():
    assert series_of("KXUFCFIGHT-26AUG15TURFER-FER") == "KXUFCFIGHT"
    assert series_of("") == ""


# --------------------------------------------------------------------------------
# 8. The rendered artifact tells the truth in both directions
# --------------------------------------------------------------------------------

def test_refused_render_never_presents_a_universe():
    rows = healthy_frame()[:11]
    md = render_markdown(build_manifest(rows, snapshot=snapshot()))
    assert "VERDICT: REFUSED" in md
    assert "NO UNIVERSE WAS SELECTED" in md
    assert "authorizes no capture session" in md


def test_qualified_render_carries_all_three_of_erics_additions():
    md = render_markdown(build_manifest(healthy_frame(), snapshot=snapshot()))
    # (1) the exact snapshot timestamp
    assert T0.isoformat() in md
    # (2) the statistic, named, justified and caveated
    assert ktm.STATISTIC_NAME in md
    assert "Why it is a reasonable proxy" in md
    assert "Limitations" in md
    # (3) the candidate population with statistic values and rank/stratum
    assert "The eligible population, complete and ranked" in md
    assert "| rank | stratum | ticker |" in md
    # and the non-representativeness statement Eric asked to be a requirement
    assert "NOT A REPRESENTATIVE SAMPLE OF THE VENUE" in md


def test_render_states_the_frozen_session_parameters():
    md = render_markdown(build_manifest(healthy_frame(), snapshot=snapshot()))
    assert "100,000" in md
    assert "minimum duration" in md
    assert "maximum duration" in md


def test_render_of_a_refusal_still_reports_the_statistic_and_the_population():
    """A refusal that hides its evidence is not a finding."""
    old = (T0 - timedelta(days=40)).isoformat().replace("+00:00", "Z")
    rows = healthy_frame()
    for r in rows:
        r["updated_time"] = old
    md = render_markdown(build_manifest(rows, snapshot=snapshot()))
    assert "Frame integrity" in md or "frame_integrity" in md
    assert "highest-statistic REJECTED markets" in md
    assert ktm.STATISTIC_NAME in md


# --------------------------------------------------------------------------------
# 9. Boundary/capability audit — this tool cannot reach a venue write
# --------------------------------------------------------------------------------

def test_the_module_reaches_no_private_route_and_loads_no_credential():
    """Static audit of the manifest module's own source.

    A forbidden CHANNEL can only be reached as a string literal, so the audit
    looks for quoted forms rather than bare substrings — `"fill"` is a
    subscription, `fill any shortfall` is prose, and an audit that cannot tell
    them apart gets deleted the first time it cries wolf.

    Anti-vacuity guard: the permitted route must be PRESENT. A guard that only
    asserts absence is satisfied by an empty file, and by a repository in which
    this module does nothing at all.
    """
    import inspect as _inspect
    import re

    from app.realtime.kalshi import FORBIDDEN_CHANNELS

    src = _inspect.getsource(ktm)

    assert FORBIDDEN_CHANNELS, "the forbidden-channel list is empty; audit is vacuous"
    for channel in FORBIDDEN_CHANNELS:
        for literal in (f'"{channel}"', f"'{channel}'"):
            assert literal not in src, \
                f"forbidden channel literal {literal} in kalshi_tape_manifest.py"

    # No subscription can be constructed here at all.
    for token in ("subscribe", '"channel"', "'channel'", "websocket", "wss://"):
        assert token not in src, f"{token!r} appears in kalshi_tape_manifest.py"

    # No credential surface.
    for token in ("api_key_id", "private_key", "CREDENTIAL_PATH", "signer",
                  "signature", "load_observer"):
        assert token not in src, f"{token!r} appears in kalshi_tape_manifest.py"

    # Only GET is reachable; no mutating verb appears as a word.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{method}\b", src), \
            f"{method} appears in kalshi_tape_manifest.py"

    # The permitted things EXIST — without these the assertions above are free.
    assert "GET /markets" in src
    assert "REST_HOSTS" in src


def test_no_credential_is_read_when_the_snapshot_runs(monkeypatch):
    """The credential loader must never be called on this path.

    `snapshot_and_build` is the only function here that touches the network, and
    it must reach the public market-data route without a key. This drives the
    real function with a stubbed adapter and fails if anything tries to sign.
    """
    import app.realtime.auth as auth

    called = []
    if hasattr(auth, "load_observer_signer"):
        monkeypatch.setattr(
            auth, "load_observer_signer",
            lambda *a, **k: called.append("signer") or pytest.fail(
                "the manifest tool loaded a credential"))

    import app.adapters.kalshi as adapters

    async def fake(self, **kwargs):
        return healthy_frame(), 3

    monkeypatch.setattr(adapters.KalshiRestAdapter, "fetch_open_markets_raw", fake)

    import asyncio
    m, frame = asyncio.run(ktm.snapshot_and_build(environment="demo"))
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    assert m["snapshot"]["environment"] == "demo"
    assert "demo" in m["snapshot"]["host"]
    assert called == []
    assert len(frame) == 24


def test_unknown_environment_is_refused():
    import asyncio
    with pytest.raises(ManifestError, match="not a known environment"):
        asyncio.run(ktm.snapshot_and_build(environment="mainnet"))


def test_truncated_census_raises_rather_than_looking_complete():
    """A frame that hit the page cap is biased and must not become a population."""
    import asyncio

    import httpx

    from app.adapters.kalshi import KalshiRestAdapter

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"markets": [mkt("KXA-EV-S")], "cursor": "more"}

        @staticmethod
        def raise_for_status():
            return None

    async def fake_get(self, client, path, params):
        return _Resp()

    original = KalshiRestAdapter._get_with_retry
    KalshiRestAdapter._get_with_retry = fake_get
    try:
        adapter = KalshiRestAdapter(base_url="https://example.invalid")
        with pytest.raises(RuntimeError, match="truncated"):
            asyncio.run(adapter.fetch_open_markets_raw(
                max_pages=3, page_delay_seconds=0))
    finally:
        KalshiRestAdapter._get_with_retry = original
    assert httpx is not None  # import is load-bearing for the adapter under test


# --------------------------------------------------------------------------------
# 10. The frozen session parameters must not drift
# --------------------------------------------------------------------------------

def test_authorized_session_parameters_are_exactly_what_eric_froze():
    assert ktm.SESSION_MIN_SECONDS == 2 * 3600
    assert ktm.SESSION_MAX_SECONDS == 4 * 3600
    assert ktm.SESSION_MIN_ARCHIVED_FRAMES == 100_000
    assert ktm.UNIVERSE_SIZE == 12
    assert ktm.PER_STRATUM == 4
    assert ktm.STRATA == ("high", "medium", "low")
    m = build_manifest(healthy_frame(), snapshot=snapshot())
    sp = m["session_parameters"]
    assert sp["min_seconds"] == 7200
    assert sp["max_seconds"] == 14400
    assert sp["min_archived_live_frames"] == 100_000
    assert sp["frozen_before_capture"] is True
    assert "whichever occurs LATER" in sp["stop_rule"]


def test_cli_command_is_reachable_and_writes_both_artifacts(monkeypatch, tmp_path):
    """Doctrine 5: a checkpoint is complete when its production path is reachable.

    The unit tests above all call `build_manifest` directly. From inside the
    module everything works — so reachability is asserted from OUTSIDE, through
    the real argparse dispatch, with only the network stubbed.
    """
    import app.adapters.kalshi as adapters
    from app.cli import main as cli_main

    async def fake(self, **kwargs):
        return healthy_frame(), 3

    monkeypatch.setattr(adapters.KalshiRestAdapter, "fetch_open_markets_raw", fake)

    j = tmp_path / "m.json"
    md = tmp_path / "m.md"
    rc = cli_main(["kalshi-tape-manifest", "--environment", "demo",
                   "--out-json", str(j), "--out-markdown", str(md)])
    assert rc == 0, "a healthy venue must exit 0 (QUALIFIED)"
    loaded = json.loads(j.read_text())
    assert loaded["verdict"] == "QUALIFIED"
    assert sum(len(s["members"]) for s in loaded["universe"]) == UNIVERSE_SIZE
    assert "NOT A REPRESENTATIVE SAMPLE" in md.read_text()


def test_cli_exit_code_is_one_when_the_venue_cannot_supply_the_universe(
        monkeypatch, tmp_path):
    """A refusal must be machine-detectable, or an operator script proceeds."""
    import app.adapters.kalshi as adapters
    from app.cli import main as cli_main

    async def fake(self, **kwargs):
        return healthy_frame()[:11], 1

    monkeypatch.setattr(adapters.KalshiRestAdapter, "fetch_open_markets_raw", fake)

    j = tmp_path / "m.json"
    rc = cli_main(["kalshi-tape-manifest", "--out-json", str(j)])
    assert rc == 1
    assert json.loads(j.read_text())["verdict"] == "REFUSED"


def test_replacement_rule_forbids_telemetry_based_substitution():
    m = build_manifest(healthy_frame(), snapshot=snapshot())
    rule = m["selection_rule"]["replacement_rule"]
    assert "must NOT be chosen by observed telemetry quality" in rule
    assert "frozen rank order" in rule
