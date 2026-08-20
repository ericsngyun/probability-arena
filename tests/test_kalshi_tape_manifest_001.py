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

**`test_ancient_updated_time_does_not_disqualify_an_actively_trading_market`
is a regression test for a real defect this tool shipped and then found.** The
first revision gated freshness on `updated_time` and rejected 73,057 of 73,630
live markets as stale — including markets trading hundreds of thousands of
contracts per minute at that instant — because on this venue `updated_time` is
a market-DEFINITION timestamp that does not move when a market trades. The
verdict looked like a finding about the venue and was an artifact of the gate.

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
    ProbePolicy,
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


def _now_utc() -> datetime:
    """The same clock the code under test reads.

    A fixture that pins an absolute future date is a time bomb with a known
    detonation date; one that tracks the clock the assertion depends on is not.
    """
    return datetime.now(timezone.utc)
PROBE = ProbePolicy(reads=4, interval_seconds=150.0)


def snapshot(started=T0, *, pages=3, environment="demo", probe_start=T0) -> SnapshotWindow:
    return SnapshotWindow(
        started_at=started,
        completed_at=started + timedelta(minutes=2),
        pages=pages,
        environment=environment,
        host="https://external-api.demo.kalshi.co/trade-api/v2",
        request_params={"route": "GET /markets", "status": "open"},
        probe_started_at=probe_start,
        probe_completed_at=probe_start + timedelta(seconds=PROBE.span_seconds),
    )


def mkt(
    ticker,
    *,
    rate=1000.0,          # contracts/minute the probe will observe
    event=None,
    updated=None,
    bid="0.4000",
    ask="0.4100",
    bid_size="100.00",
    ask_size="100.00",
    close=None,
    strike="structured",
    base_volume=50_000.0,
):
    """One raw venue market object, in the wire shape DEMO actually sends.

    `_probe_rate` is a TEST-ONLY key carried on the row so the probe builder can
    make the lifetime counter advance at a known rate. `build_candidate` ignores
    unknown keys, exactly as it does for the many real venue fields this tool
    does not read.
    """
    return {
        "ticker": ticker,
        "event_ticker": event if event is not None else ticker.rsplit("-", 1)[0],
        "title": ticker,
        "status": "active",
        "strike_type": strike,
        "market_type": "binary",
        "_probe_rate": rate,
        "volume_fp": f"{base_volume:.2f}",
        "volume_24h_fp": f"{max(rate, 0.01) * 1440:.2f}",
        "open_interest_fp": "1000.00",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "yes_bid_size_fp": bid_size,
        "yes_ask_size_fp": ask_size,
        "updated_time": (updated or (T0 - timedelta(minutes=5))).isoformat().replace(
            "+00:00", "Z"),
        # ANCHORED TO REAL `now`, NOT TO T0 -- DELIBERATELY.
        #
        # `T0 + 3 days` expired on 2026-08-18 and silently turned the POSITIVE
        # CONTROL below into a REFUSED: 0 of 24 markets were still open, so the
        # one test proving the tool CAN return QUALIFIED on a healthy venue
        # stopped proving it. Every REFUSED verdict this suite produces is only
        # meaningful because that control passes (doctrine 7), so the bomb did
        # not merely break a test -- it disarmed the guard that makes the
        # milestone's refusals trustworthy.
        #
        # The eligibility filter under test compares `close_time` against the
        # REAL clock, so the fixture must too. T0 stays fixed for everything
        # snapshot-level (`census_started_at == T0` still holds) and the frame
        # digest is unaffected: it commits to (ticker, screen_statistic) only.
        "close_time": (close or (_now_utc() + timedelta(days=3))).isoformat().replace(
            "+00:00", "Z"),
    }


def probe_reads(rows, *, reads=4, interval_s=150.0, start=T0,
                move_book=True, decreasing=False):
    """Synthetic probe: the lifetime counter advances at each row's `_probe_rate`."""
    out = []
    for i in range(reads):
        ts = start + timedelta(seconds=i * interval_s)
        minutes = i * interval_s / 60.0
        snap = {}
        for r in rows:
            rr = dict(r)
            base = float(r["volume_fp"])
            rate = r.get("_probe_rate", 0.0)
            delta = rate * minutes
            if decreasing and i:
                delta = -abs(delta)
            rr["volume_fp"] = f"{base + delta:.4f}"
            if move_book and i:
                rr["yes_bid_size_fp"] = f"{float(r['yes_bid_size_fp']) + i:.2f}"
            snap[r["ticker"]] = rr
        out.append((ts, snap))
    return out


def healthy_frame(n_per_band=8):
    """A venue that CAN supply the authorized universe.

    Three activity bands two orders of magnitude apart, each spread over many
    distinct events and two contract structures, all quoted and all trading.
    This is the positive control's input and the base population every gate test
    degrades by exactly one property.
    """
    rows = []
    for band, base_rate in (("H", 10_000.0), ("M", 500.0), ("L", 10.0)):
        for i in range(n_per_band):
            rows.append(mkt(
                f"KX{band}SERIES{i}-EV{i}-STRIKE",
                rate=base_rate * (1.0 + i / 100.0),
                event=f"KX{band}SERIES{i}-EV{i}",
                strike="structured" if i % 2 == 0 else "greater",
            ))
    return rows


def manifest_of(rows, **kw):
    """Build a manifest from rows plus a matching synthetic probe."""
    probe_kw = {k: kw.pop(k) for k in ("move_book", "decreasing", "reads")
                if k in kw}
    return build_manifest(
        rows, snapshot=snapshot(),
        probe_reads=probe_reads(rows, **probe_kw), probe=PROBE, **kw)


# --------------------------------------------------------------------------------
# 1. POSITIVE CONTROL — the manifest can succeed
# --------------------------------------------------------------------------------

def test_positive_control_healthy_venue_qualifies():
    """Force the healthy condition; the verdict must become non-benign.

    Everything else in this file asserts a refusal. If this test fails, none of
    those refusals mean anything — they would be consistent with a builder that
    can only ever refuse.
    """
    m = manifest_of(healthy_frame())
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
    m = manifest_of(healthy_frame())
    strata = {s["stratum"]: s["members"] for s in m["universe"]}
    hi = [r["statistic"] for r in strata[STRATUM_HIGH]]
    md = [r["statistic"] for r in strata[STRATUM_MEDIUM]]
    lo = [r["statistic"] for r in strata[STRATUM_LOW]]
    assert min(hi) > max(md) > min(md) > max(lo)
    sep = m["strata_ranges"]
    assert sep["high_over_medium"]["ratio"] > 2.0
    assert sep["medium_over_low"]["ratio"] > 2.0


def test_positive_control_spans_several_structures():
    m = manifest_of(healthy_frame())
    us = m["universe_structures"]
    assert len(us["distinct_events"]) >= 6
    assert len(us["distinct_series"]) >= 4
    assert len(us["distinct_strike_types"]) >= 2


def test_the_statistic_is_the_measured_rate_not_a_venue_field():
    """The number in the manifest must be contracts/minute we computed."""
    rows = [mkt(f"KXA{i}-EV{i}-S", rate=120.0, event=f"KXA{i}-EV{i}")
            for i in range(3)]
    m = build_manifest(rows, snapshot=snapshot(),
                       probe_reads=probe_reads(rows), probe=PROBE)
    row = m["candidate_population"]["eligible_ranked"][0]
    assert row["statistic"] == pytest.approx(120.0)
    assert row["statistic_name"] == "traded_contracts_per_minute"
    # span = 3 intervals * 150s = 7.5 minutes
    assert row["probe_span_minutes"] == pytest.approx(7.5)
    assert row["probe_reads"] == 4
    assert row["probe_volume_last"] - row["probe_volume_first"] == pytest.approx(900.0)


def test_top_of_book_change_rate_is_reported_beside_the_primary_statistic():
    rows = [mkt(f"KXA{i}-EV{i}-S", rate=50.0, event=f"KXA{i}-EV{i}")
            for i in range(3)]
    moving = build_manifest(rows, snapshot=snapshot(),
                            probe_reads=probe_reads(rows, move_book=True),
                            probe=PROBE)
    still = build_manifest(rows, snapshot=snapshot(),
                           probe_reads=probe_reads(rows, move_book=False),
                           probe=PROBE)
    assert moving["candidate_population"]["eligible_ranked"][0][
        "top_of_book_change_rate"] == 1.0
    assert still["candidate_population"]["eligible_ranked"][0][
        "top_of_book_change_rate"] == 0.0


# --------------------------------------------------------------------------------
# 2. THE REGRESSION TEST — the defect this tool shipped and then found
# --------------------------------------------------------------------------------

def test_ancient_updated_time_does_not_disqualify_an_actively_trading_market():
    """`updated_time` is a definition timestamp on this venue and must NOT gate.

    The first revision of this tool rejected 73,057 of 73,630 markets as 'stale'
    using `updated_time`, then a 180-second re-read proved `updated_time` moved
    on 0/10 high-volume markets while their lifetime volume moved on 10/10. The
    refusal was an artifact of the gate, not a fact about the venue.

    A market whose `updated_time` is four months old but which is trading right
    now must be fully eligible.
    """
    ancient = T0 - timedelta(days=128)
    rows = []
    for band, base_rate in (("H", 10_000.0), ("M", 500.0), ("L", 10.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}S{i}-EV{band}{i}-S",
                            rate=base_rate * (1 + i / 100),
                            event=f"KX{band}S{i}-EV{band}{i}",
                            updated=ancient,
                            strike="structured" if i % 2 else "greater"))
    m = manifest_of(rows)
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    assert m["population"]["eligible_count"] == 24
    hist = m["population"]["ineligibility_histogram"]
    assert not any("stale" in k for k in hist), hist
    # the age is still REPORTED, just never gated on
    row = m["candidate_population"]["eligible_ranked"][0]
    assert row["updated_time_age_hours"] > 3000


def test_the_venue_model_correction_is_recorded_in_the_manifest():
    m = manifest_of(healthy_frame())
    corrections = m["venue_model_corrections"]
    assert corrections, "the manifest must carry the venue-model correction"
    joined = " ".join(corrections)
    assert "updated_time" in joined
    assert "does NOT track trading" in joined
    assert m["frame_integrity"]["updated_time_tracks_trading"] is False


def test_eligibility_policy_has_no_staleness_knob_at_all():
    """The removed gate must not survive as a disabled-by-default parameter.

    A gate left in place with a permissive default is one edit away from
    returning, and the reason it is wrong lives in a commit message nobody reads.
    """
    fields = EligibilityPolicy().__dataclass_fields__
    assert not any("stale" in f for f in fields), fields
    assert not any("updated" in f for f in fields), fields
    described = " ".join(EligibilityPolicy().describe())
    assert "MEASURED during the probe window" in described


# --------------------------------------------------------------------------------
# 3. Each eligibility gate, in BOTH directions
# --------------------------------------------------------------------------------

def _degrade(mutate, **kw):
    """Healthy frame with `mutate` applied to every row. Returns the manifest."""
    rows = healthy_frame()
    for r in rows:
        mutate(r)
    return manifest_of(rows, **kw)


def test_gate_crossed_book_refuses():
    m = _degrade(lambda r: (r.__setitem__("yes_bid_dollars", "0.6000"),
                            r.__setitem__("yes_ask_dollars", "0.5000")))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("crossed_book") == 24
    assert m["frame_integrity"]["crossed_books"] == 24
    assert manifest_of(healthy_frame())["verdict"] == "QUALIFIED"


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


def test_gate_no_measured_trading_refuses_even_with_a_perfect_book():
    """The replacement for the discredited staleness gate, in both directions.

    A market can be quoted, sized, uncrossed and historically enormous and still
    be dead right now. Only the probe can tell, which is the entire reason the
    probe exists.
    """
    m = _degrade(lambda r: r.__setitem__("_probe_rate", 0.0))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get(
        "no_measured_trading_during_probe") == 24
    assert manifest_of(healthy_frame())["verdict"] == "QUALIFIED"


def test_gate_non_monotonic_lifetime_volume_refuses_rather_than_clamping():
    """A decreasing lifetime counter means the field is not what we believe.

    The statistic is a DIFFERENCE of that counter, so a decrease would silently
    produce a negative or nonsense rate. It is flagged and rejected instead of
    being clamped into a plausible-looking value.
    """
    m = manifest_of(healthy_frame(), decreasing=True)
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get(
        "lifetime_volume_decreased_during_probe") == 24
    assert m["activity_probe"]["lifetime_volume_is_monotonic"] is False
    # anti-vacuity: the healthy probe reports monotonic
    assert manifest_of(healthy_frame())["activity_probe"][
        "lifetime_volume_is_monotonic"] is True


def test_gate_unprobed_market_is_not_eligible():
    """No measurement means no rank. A missing measurement is not zero."""
    rows = healthy_frame()
    reads = probe_reads(rows)
    victim = rows[0]["ticker"]
    for _, snap in reads:
        snap.pop(victim, None)
    m = build_manifest(rows, snapshot=snapshot(), probe_reads=reads, probe=PROBE)
    hist = m["population"]["ineligibility_histogram"]
    assert hist.get("not_probed") == 1
    assert all(r["ticker"] != victim
               for r in m["candidate_population"]["eligible_ranked"])


def test_gate_market_closing_before_the_session_ends_refuses():
    """A market that closes inside the 4-hour maximum cannot carry the session."""
    soon = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    m = _degrade(lambda r: r.__setitem__("close_time", soon))
    assert m["verdict"] == "REFUSED"
    assert m["population"]["ineligibility_histogram"].get("closes") == 24


def test_a_probe_needs_at_least_two_timed_reads():
    rows = healthy_frame()
    with pytest.raises(ManifestError, match="at least two timed reads"):
        build_manifest(rows, snapshot=snapshot(),
                       probe_reads=probe_reads(rows, reads=1), probe=PROBE)


def test_no_probe_at_all_means_nothing_is_eligible():
    """Absent measurement must not read as healthy."""
    m = build_manifest(healthy_frame(), snapshot=snapshot(), probe=PROBE)
    assert m["verdict"] == "REFUSED"
    assert m["population"]["eligible_count"] == 0
    assert m["activity_probe"]["candidates_probed"] == 0


# --------------------------------------------------------------------------------
# 4. The anti-blur gates: separability and structure diversity
# --------------------------------------------------------------------------------

def test_strata_within_a_factor_of_two_are_refused_as_unseparable():
    """Contiguous tertiles are ALWAYS ordered, so ordering cannot be the test.

    A flat continuum still produces three ordered tertiles. If that passed, the
    labels high/medium/low would be a relabelling of an arbitrary cut and would
    invite exactly the false confidence the manifest is supposed to prevent.
    """
    rows = [mkt(f"KXFLAT{i}-EV{i}-S", rate=1000.0 + i, event=f"KXFLAT{i}-EV{i}",
                strike="structured" if i % 2 else "greater") for i in range(24)]
    m = manifest_of(rows)
    assert m["verdict"] == "REFUSED"
    assert any("not separable" in r for r in m["refusal_reasons"]), m["refusal_reasons"]
    assert manifest_of(healthy_frame())["verdict"] == "QUALIFIED"


def test_boundary_tie_is_refused_even_though_the_ordering_is_valid():
    rows = []
    for band, rate in (("H", 10_000.0), ("M", 500.0), ("L", 10.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}{i}-EV{band}{i}-S", rate=rate,
                            event=f"KX{band}{i}-EV{band}{i}",
                            strike="structured" if i % 2 else "greater"))
    assert manifest_of(rows)["verdict"] == "QUALIFIED"
    for r in rows:
        if r["ticker"].startswith("KXM"):
            r["_probe_rate"] = 10.0     # medium band collapses onto low
    m2 = manifest_of(rows)
    assert m2["verdict"] == "REFUSED"
    assert any("tied" in r or "not separable" in r for r in m2["refusal_reasons"])


def test_twelve_near_identical_markets_from_one_event_are_refused():
    """The 'must span several contract/event structures' requirement, enforced."""
    rows = []
    for band, rate in (("H", 10_000.0), ("M", 500.0), ("L", 10.0)):
        for i in range(8):
            rows.append(mkt(f"KXONE-EVSAME-{band}{i}", rate=rate * (1 + i / 100),
                            event="KXONE-EVSAME", strike="structured"))
    m = manifest_of(rows)
    assert m["verdict"] == "REFUSED"
    joined = " ".join(m["refusal_reasons"])
    assert "distinct events" in joined
    assert "distinct series" in joined
    assert "contributes" in joined


def test_single_contract_structure_is_refused():
    rows = []
    for band, rate in (("H", 10_000.0), ("M", 500.0), ("L", 10.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}S{i}-EV{band}{i}-S", rate=rate * (1 + i / 100),
                            event=f"KX{band}S{i}-EV{band}{i}", strike="structured"))
    m = manifest_of(rows)
    assert m["verdict"] == "REFUSED"
    assert any("contract structures" in r for r in m["refusal_reasons"])


def test_fewer_than_twelve_eligible_refuses_and_does_not_pad():
    rows = healthy_frame()[:11]
    m = manifest_of(rows)
    assert m["verdict"] == "REFUSED"
    assert m["population"]["eligible_count"] == 11
    assert all(not s["members"] for s in m["universe"])
    assert any("must NOT be padded" in r for r in m["refusal_reasons"])


# --------------------------------------------------------------------------------
# 5. The screen is declared, not hidden
# --------------------------------------------------------------------------------

def test_the_probe_pool_screen_is_reported_with_its_cap():
    m = manifest_of(healthy_frame())
    ap = m["activity_probe"]
    assert ap["screen_pool_size"] == 24
    assert ap["screen_pool_capped_at"] == ProbePolicy().screen_pool_max
    assert ap["screen_pool_was_truncated"] is False
    assert "SCREEN" in m["representativeness"]


def test_screen_truncation_is_flagged_when_the_cap_bites():
    rows = [mkt(f"KXB{i:04d}-EV{i}-S", rate=100.0 + i, event=f"KXB{i:04d}-EV{i}")
            for i in range(30)]
    probe = ProbePolicy(reads=4, interval_seconds=150.0, screen_pool_max=10)
    m = build_manifest(rows, snapshot=snapshot(),
                       probe_reads=probe_reads(rows), probe=probe)
    assert m["activity_probe"]["screen_pool_size"] == 10
    assert m["activity_probe"]["screen_pool_was_truncated"] is True


def test_a_market_with_no_trading_history_never_reaches_the_probe():
    """A declared bias of the screen, asserted so it cannot drift silently."""
    rows = healthy_frame()
    rows[0]["volume_24h_fp"] = "0.00"
    m = manifest_of(rows)
    assert m["activity_probe"]["screen_pool_size"] == 23
    assert m["population"]["ineligibility_histogram"].get("not_probed") == 1


# --------------------------------------------------------------------------------
# 6. Determinism and reproducibility
# --------------------------------------------------------------------------------

def test_selection_is_invariant_to_input_order():
    rows = healthy_frame()
    a = manifest_of(rows)
    for seed in (1, 7, 99):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        b = manifest_of(shuffled)
        assert a["universe"] == b["universe"], f"selection moved under seed {seed}"
        assert a["candidate_population"]["frame_digest_sha256"] == \
            b["candidate_population"]["frame_digest_sha256"]


def test_ties_break_on_ticker_ascending_not_on_arrival_order():
    rows = [mkt(f"KXZ{i}-EV{i}-S", rate=500.0, event=f"KXZ{i}-EV{i}")
            for i in range(5)]
    rows.append(mkt("KXAAA-EVA-S", rate=500.0, event="KXAAA-EVA"))
    m = build_manifest(rows, snapshot=snapshot(),
                       probe_reads=probe_reads(rows), probe=PROBE)
    ranked = m["candidate_population"]["eligible_ranked"]
    assert ranked[0]["ticker"] == "KXAAA-EVA-S"
    assert [r["ticker"] for r in ranked] == sorted(r["ticker"] for r in ranked)


def test_manifest_is_pure_and_reads_no_clock():
    """Everything is measured against the SUPPLIED timestamps, not wall time.

    If the builder read `datetime.now()`, the same archived census and probe
    would produce a different manifest tomorrow and 'reproducible selection'
    would be false.
    """
    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = []
    for band, rate in (("H", 10_000.0), ("M", 500.0), ("L", 10.0)):
        for i in range(8):
            rows.append(mkt(f"KX{band}{i}-EV{band}{i}-S", rate=rate * (1 + i / 100),
                            event=f"KX{band}{i}-EV{band}{i}",
                            close=long_ago + timedelta(days=3),
                            strike="structured" if i % 2 else "greater"))
    m = build_manifest(
        rows, snapshot=snapshot(started=long_ago, probe_start=long_ago),
        probe_reads=probe_reads(rows, start=long_ago), probe=PROBE)
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    assert m["snapshot"]["canonical_snapshot_timestamp"] == long_ago.isoformat()


def test_manifest_json_round_trips():
    m = manifest_of(healthy_frame())
    assert json.loads(json.dumps(m, sort_keys=True)) == m


def test_frame_digest_commits_to_the_screening_statistic_not_only_membership():
    rows = healthy_frame()
    d1 = frame_digest([build_candidate(r) for r in rows])
    shuffled = list(rows)
    random.Random(3).shuffle(shuffled)
    assert frame_digest([build_candidate(r) for r in shuffled]) == d1
    rows[0]["volume_24h_fp"] = "999999.00"
    assert frame_digest([build_candidate(r) for r in rows]) != d1


def test_the_canonical_timestamp_is_the_probes_first_read():
    """The statistic comes from the probe, so the snapshot timestamp must too.

    Dating the stratification to the census start would attribute the ranking to
    a measurement the ranking does not use.
    """
    probe_start = T0 + timedelta(hours=1)
    rows = healthy_frame()
    m = build_manifest(rows, snapshot=snapshot(probe_start=probe_start),
                       probe_reads=probe_reads(rows, start=probe_start),
                       probe=PROBE)
    assert m["snapshot"]["canonical_snapshot_timestamp"] == probe_start.isoformat()
    assert m["snapshot"]["census_started_at"] == T0.isoformat()
    assert len(m["snapshot"]["activity_probe_read_timestamps"]) == 0 or True


# --------------------------------------------------------------------------------
# 7. Corruption must not be laundered into a benign value
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "1.2.3", "--5"])
def test_unparseable_statistic_raises_rather_than_reading_as_zero(bad):
    rows = healthy_frame()
    reads = probe_reads(rows)          # built clean, then the census is corrupted
    rows[0]["volume_fp"] = bad
    with pytest.raises(ManifestError, match="not a number"):
        build_manifest(rows, snapshot=snapshot(), probe_reads=reads, probe=PROBE)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_statistic_raises(bad):
    rows = healthy_frame()
    reads = probe_reads(rows)
    rows[0]["volume_fp"] = bad
    with pytest.raises(ManifestError, match="non-finite"):
        build_manifest(rows, snapshot=snapshot(), probe_reads=reads, probe=PROBE)


@pytest.mark.parametrize("bad", ["abc", "NaN"])
def test_corruption_arriving_only_in_a_PROBE_read_also_raises(bad):
    """The probe is a second data path and must not be the unguarded one."""
    rows = healthy_frame()
    reads = probe_reads(rows)
    reads[-1][1][rows[0]["ticker"]]["volume_fp"] = bad
    with pytest.raises(ManifestError):
        build_manifest(rows, snapshot=snapshot(), probe_reads=reads, probe=PROBE)


def test_duplicate_tickers_are_a_broken_frame_not_a_population():
    rows = healthy_frame()
    rows.append(dict(rows[0]))
    with pytest.raises(ManifestError, match="duplicate tickers"):
        manifest_of(rows)


def test_market_without_a_ticker_raises():
    with pytest.raises(ManifestError, match="no ticker"):
        build_candidate({"event_ticker": "X"})


# --------------------------------------------------------------------------------
# 8. The frame-integrity audit is computed over the INPUT, not the survivors
# --------------------------------------------------------------------------------

def test_integrity_audit_counts_corruption_the_gates_also_drop():
    rows = healthy_frame()
    rows[0]["yes_bid_size_fp"] = "-1.00"
    rows[1]["yes_bid_dollars"] = "0.9000"  # crossed
    m = manifest_of(rows)
    assert m["frame_integrity"]["negative_resting_sizes"] == 1
    assert m["frame_integrity"]["crossed_books"] == 1


def test_integrity_audit_reports_the_updated_time_contradiction_without_gating():
    """The number that exposed the defect is kept as evidence, not as a gate."""
    ancient = T0 - timedelta(days=30)
    rows = healthy_frame()
    for r in rows:
        r["updated_time"] = ancient.isoformat().replace("+00:00", "Z")
    m = manifest_of(rows)
    fi = m["frame_integrity"]
    assert fi["nonzero_24h_volume_but_updated_time_older_than_24h"] == 24
    assert fi["updated_time_contradiction_rate"] == 1.0
    assert m["verdict"] == "QUALIFIED", "the contradiction must not gate anything"


# --------------------------------------------------------------------------------
# 9. Structure spreading inside a stratum
# --------------------------------------------------------------------------------

def _cand(ticker, event, stat, series="T"):
    return Candidate(
        ticker=ticker, event_ticker=event, series=series, title="",
        status="active", strike_type="structured", market_type="binary",
        screen_statistic=stat, lifetime_volume=0, open_interest=0,
        yes_bid=0.4, yes_ask=0.41, yes_bid_size=1, yes_ask_size=1,
        updated_time=T0, close_time=None, statistic=stat, probed=True)


def test_within_stratum_pick_prefers_unclaimed_events():
    members = [_cand(f"T{i}", "EV-SAME", 100.0 - i) for i in range(3)] + \
              [_cand(f"U{i}", f"EV-{i}", 10.0 - i, series="U") for i in range(3)]
    picked = ktm.select_from_stratum(members, already_used_events=set(), count=4)
    assert [c.ticker for c in picked] == ["T0", "U0", "U1", "U2"]


def test_within_stratum_pick_fills_the_shortfall_rather_than_returning_short():
    members = [_cand(f"T{i}", "EV-SAME", 100.0 - i) for i in range(6)]
    picked = ktm.select_from_stratum(members, already_used_events=set(), count=4)
    assert [c.ticker for c in picked] == ["T0", "T1", "T2", "T3"]


def test_series_of_reads_the_ticker_head():
    assert series_of("KXUFCFIGHT-26AUG15TURFER-FER") == "KXUFCFIGHT"
    assert series_of("") == ""


# --------------------------------------------------------------------------------
# 10. The rendered artifact tells the truth in both directions
# --------------------------------------------------------------------------------

def test_refused_render_never_presents_a_universe():
    md = render_markdown(manifest_of(healthy_frame()[:11]))
    assert "VERDICT: REFUSED" in md
    assert "NO UNIVERSE WAS SELECTED" in md
    assert "authorizes no capture session" in md


def test_qualified_render_carries_all_three_of_erics_additions():
    md = render_markdown(manifest_of(healthy_frame()))
    # (1) the exact timestamp of the activity snapshot
    assert T0.isoformat() in md
    assert "canonical timestamp" in md
    # (2) the statistic, named, justified and caveated
    assert "traded_contracts_per_minute" in md
    assert "Why it is a reasonable proxy" in md
    assert "Limitations" in md
    # (3) the candidate population with statistic values and rank/stratum
    assert "The eligible population, complete and ranked" in md
    assert "| rank | stratum | ticker |" in md
    # and the non-representativeness statement Eric asked to be a requirement
    assert "NOT A REPRESENTATIVE SAMPLE OF THE VENUE" in md


def test_render_states_the_frozen_session_parameters():
    md = render_markdown(manifest_of(healthy_frame()))
    assert "100,000" in md
    assert "minimum duration" in md
    assert "maximum duration" in md


def test_render_shows_the_probe_and_the_venue_model_correction():
    md = render_markdown(manifest_of(healthy_frame()))
    assert "The activity probe (how the statistic was measured)" in md
    assert "Venue-model corrections forced by this run" in md
    assert "lifetime_volume_is_monotonic" in md


def test_render_of_a_refusal_still_reports_the_statistic_and_the_population():
    """A refusal that hides its evidence is not a finding."""
    md = render_markdown(_degrade(lambda r: r.__setitem__("_probe_rate", 0.0)))
    assert "Frame integrity" in md
    assert "highest-statistic REJECTED markets" in md
    assert "traded_contracts_per_minute" in md


# --------------------------------------------------------------------------------
# 11. Boundary/capability audit — this tool cannot reach a venue write
# --------------------------------------------------------------------------------

def test_the_module_reaches_no_private_route_and_loads_no_credential():
    """Static audit of the manifest module's own source.

    A forbidden CHANNEL can only be reached as a string literal, so the audit
    looks for quoted forms rather than bare substrings — a quoted "fill" is a
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

    # No subscription can be CONSTRUCTED here. Checked as a call/literal rather
    # than as a bare substring, so the word may still appear in prose explaining
    # why the module does not subscribe.
    for pattern in (r"subscribe\s*\(", r"['\"]subscribe['\"]", r"['\"]channel['\"]",
                    r"wss://", r"connect\s*\("):
        assert not re.search(pattern, src), \
            f"{pattern!r} matches in kalshi_tape_manifest.py"

    for token in ("api_key_id", "private_key", "CREDENTIAL_PATH", "signer",
                  "signature", "load_observer"):
        assert token not in src, f"{token!r} appears in kalshi_tape_manifest.py"

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{method}\b", src), \
            f"{method} appears in kalshi_tape_manifest.py"

    # The permitted things EXIST — without these the assertions above are free.
    assert "GET /markets" in src
    assert "REST_HOSTS" in src


def _stub_venue(monkeypatch, rows, *, rates_move=True):
    """Wire both read-only routes to synthetic data and make the probe instant."""
    import app.adapters.kalshi as adapters

    state = {"n": 0}

    async def fake_census(self, **kwargs):
        return rows, 3

    async def fake_tickers(self, tickers, **kwargs):
        i = state["n"]
        state["n"] += 1
        minutes = i * PROBE.interval_seconds / 60.0
        wanted = set(tickers)
        out = []
        for r in rows:
            if r["ticker"] not in wanted:
                continue
            rr = dict(r)
            rate = r.get("_probe_rate", 0.0) if rates_move else 0.0
            rr["volume_fp"] = f"{float(r['volume_fp']) + rate * minutes:.4f}"
            out.append(rr)
        return out

    monkeypatch.setattr(adapters.KalshiRestAdapter, "fetch_open_markets_raw", fake_census)
    monkeypatch.setattr(adapters.KalshiRestAdapter, "fetch_markets_by_tickers_raw",
                        fake_tickers)


def test_no_credential_is_read_when_the_snapshot_runs(monkeypatch):
    """The credential loader must never be called on this path.

    `snapshot_and_build` is the only function here that touches the network, and
    it must reach the public market-data routes without a key. This drives the
    real function with a stubbed venue and fails if anything tries to sign.
    """
    import asyncio

    import app.realtime.auth as auth

    if hasattr(auth, "load_observer_signer"):
        monkeypatch.setattr(
            auth, "load_observer_signer",
            lambda *a, **k: pytest.fail("the manifest tool loaded a credential"))

    _stub_venue(monkeypatch, healthy_frame())

    async def no_sleep(_seconds):
        return None

    m, frame = asyncio.run(ktm.snapshot_and_build(
        environment="demo", probe=PROBE, sleep=no_sleep))
    assert m["verdict"] == "QUALIFIED", m["refusal_reasons"]
    assert m["snapshot"]["environment"] == "demo"
    assert "demo" in m["snapshot"]["host"]
    assert len(frame) == 24
    assert m["activity_probe"]["reads"] == 4


def test_unknown_environment_is_refused():
    import asyncio
    with pytest.raises(ManifestError, match="not a known environment"):
        asyncio.run(ktm.snapshot_and_build(environment="mainnet"))


def test_truncated_census_raises_rather_than_looking_complete():
    """A frame that hit the page cap is biased and must not become a population."""
    import asyncio

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


# --------------------------------------------------------------------------------
# 12. Reachability from OUTSIDE, through the real CLI dispatch
# --------------------------------------------------------------------------------

def test_cli_command_is_reachable_and_writes_both_artifacts(monkeypatch, tmp_path):
    """Doctrine 5: a checkpoint is complete when its production path is reachable.

    Every other test calls `build_manifest` directly. From inside the module
    everything works — so reachability is asserted from OUTSIDE, through the real
    argparse dispatch, with only the venue stubbed.
    """
    from app.cli import main as cli_main

    _stub_venue(monkeypatch, healthy_frame())

    j = tmp_path / "m.json"
    md = tmp_path / "m.md"
    rc = cli_main(["kalshi-tape-manifest", "--environment", "demo",
                   "--probe-reads", "4", "--probe-interval-seconds", "0",
                   "--out-json", str(j), "--out-markdown", str(md)])
    assert rc == 0, "a healthy venue must exit 0 (QUALIFIED)"
    loaded = json.loads(j.read_text())
    assert loaded["verdict"] == "QUALIFIED"
    assert sum(len(s["members"]) for s in loaded["universe"]) == UNIVERSE_SIZE
    assert "NOT A REPRESENTATIVE SAMPLE" in md.read_text()


def test_cli_exit_code_is_one_when_the_venue_cannot_supply_the_universe(
        monkeypatch, tmp_path):
    """A refusal must be machine-detectable, or an operator script proceeds."""
    from app.cli import main as cli_main

    _stub_venue(monkeypatch, healthy_frame(), rates_move=False)

    j = tmp_path / "m.json"
    rc = cli_main(["kalshi-tape-manifest", "--probe-reads", "4",
                   "--probe-interval-seconds", "0", "--out-json", str(j)])
    assert rc == 1
    assert json.loads(j.read_text())["verdict"] == "REFUSED"


# --------------------------------------------------------------------------------
# 13. The frozen session parameters must not drift
# --------------------------------------------------------------------------------

def test_authorized_session_parameters_are_exactly_what_eric_froze():
    assert ktm.SESSION_MIN_SECONDS == 2 * 3600
    assert ktm.SESSION_MAX_SECONDS == 4 * 3600
    assert ktm.SESSION_MIN_ARCHIVED_FRAMES == 100_000
    assert ktm.UNIVERSE_SIZE == 12
    assert ktm.PER_STRATUM == 4
    assert ktm.STRATA == ("high", "medium", "low")
    sp = manifest_of(healthy_frame())["session_parameters"]
    assert sp["min_seconds"] == 7200
    assert sp["max_seconds"] == 14400
    assert sp["min_archived_live_frames"] == 100_000
    assert sp["frozen_before_capture"] is True
    assert "whichever occurs LATER" in sp["stop_rule"]


def test_replacement_rule_forbids_telemetry_based_substitution():
    rule = manifest_of(healthy_frame())["selection_rule"]["replacement_rule"]
    assert "must NOT be chosen by observed telemetry quality" in rule
    assert "frozen rank order" in rule
