"""KALSHI-CP7-LIVE-RERUN — the live verdict, pinned as executable claims.

CP7 FAILED on the live venue on 2026-08-17 and was fixed by
`KALSHI-REPLAY-GENERATION-CONSISTENCY-001`, whose proofs were offline. This
module checks the **live re-run's** artifacts, and — the part that matters —
checks that the verifier which produced the passing verdict is still capable of
producing a failing one.

**The control is not synthetic.** `docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-
RUNS/s2-reconnect-session.json` is the venue's own recording of the session
where the defect occurred. Pointing the same verifier at it must FAIL, with the
CP7 shape as the reason. That is the whole of doctrine 7 applied to a
verification script: a checker that cannot fail is not a check, and the
strongest available evidence that this one can is the real defect.

Doctrine 9 — provenance. Both artifacts are live DEMO captures written by
`scripts/kalshi_cp6_cp9_functional_probe.py`; the assertions below re-read them
from disk rather than restating their numbers, so if an artifact is edited or
replaced these tests move with it instead of certifying a memory of it.

**Scope.** 60 VENUE TEST INSTRUMENTS, functional proof only. Nothing here
supports a rate, latency, throughput, capacity or microstructure claim.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RERUN = REPO / "docs/experiments/KALSHI-CP7-LIVE-RERUN-RUNS"
FAILED_RUN = (REPO / "docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-RUNS"
              / "s2-reconnect-session.json")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = _load_module(REPO / "scripts/kalshi_cp7_live_rerun_verify.py",
                      "kalshi_cp7_live_rerun_verify")


@pytest.fixture(scope="module")
def rerun():
    return {name: json.loads((RERUN / f"{name}-session.json").read_text())
            for name in ("s1-observe", "s2-reconnect", "s3-drop")}


@pytest.fixture(scope="module")
def failed_run():
    return json.loads(FAILED_RUN.read_text())


class TestTheArtifactsAreWhatTheyClaim:
    """Doctrine 9. A fixture that cannot identify its empirical basis is
    synthetic test data, and these are the basis of a qualification verdict."""

    def test_every_session_is_a_live_demo_capture_of_the_frozen_universe(
            self, rerun):
        universe = json.loads((RERUN / "universe.json").read_text())
        assert universe["universe_size"] == 60
        for name, session in rerun.items():
            assert session["environment"] == "demo", name
            assert session["config"]["market_tickers"] == universe["universe"], (
                f"{name} did not run on the frozen universe")
            # Frozen BEFORE the sockets opened, or the selection could have
            # seen the sessions' own telemetry.
            assert universe["frozen_at"] < session["started_at"], name
            assert session["session_result"]["events_received"] > 0, name

    def test_the_universe_is_declared_as_venue_test_instruments(self, rerun):
        """§8 rescope. Every artifact must say what these markets are."""
        universe = json.loads((RERUN / "universe.json").read_text())
        for blob in [universe] + list(rerun.values()):
            note = blob["scope_note"].upper()
            assert "TEST INSTRUMENT" in note
            assert "FUNCTIONAL" in note
            for forbidden in ("RATE", "LATENCY", "THROUGHPUT", "CAPACITY"):
                assert forbidden in note, (
                    f"{forbidden} is not disclaimed in a scope note")

    def test_only_market_data_channels_were_subscribed(self, rerun):
        """Read-only. No order, portfolio or private surface."""
        for name, session in rerun.items():
            assert set(session["config"]["channels"]) <= {
                "orderbook_delta", "ticker", "trade"}, name


class TestTheThreePropertiesHoldLive:
    """The preregistered CP7 properties, computed from the live artifacts."""

    def test_property_1_each_boundary_advances_the_generation(self, rerun):
        out = verify.property_1(rerun["s2-reconnect"], rerun["s1-observe"])
        assert out["verdict"] == "PROVEN"
        assert out["forced_closes"] == 2
        assert out["epoch_sequence_observed"] == [1, 2, 3]
        # The paired control, without which the counter proves nothing.
        assert out["control_epoch_final"] == 1
        assert out["control_reconnects"] == 0

    def test_property_2_every_market_re_acquires_on_its_OWN_snapshot(self, rerun):
        out = verify.property_2(rerun["s2-reconnect"])
        assert out["verdict"] == "PROVEN"
        assert out["boundary_epochs"] == [2, 3]
        for epoch in (2, 3):
            per = out["per_epoch"][epoch]
            # THE SHAPE. 60 entries of one acquisition each — never one entry
            # carrying 60, which is exactly what CP7 measured.
            assert per["acquisitions"] == 60
            assert per["entries_carrying_an_acquisition"] == 60
            assert per["max_acquisitions_in_one_entry"] == 1
            assert per["markets_left_awaiting_their_own_snapshot"] == 59
            # "No book may silently survive across a generation boundary as if
            # nothing happened" — the preregistration's own words. All 60.
            assert per["markets_unpublished_at_the_boundary"] == 60
            assert per["boundary_states"] == {
                "awaiting_snapshot_for_generation": 59,
                # The one market that re-acquired on the rebasing frame itself;
                # its last unpublished state in the window is the ack's.
                "subscription_unhealthy": 1}

    def test_property_3_a_real_gap_still_faults_and_is_typed_as_a_fault(
            self, rerun):
        out = verify.property_3(rerun["s3-drop"], rerun["s1-observe"])
        assert out["verdict"] == "PROVEN"
        assert out["typed_state"] == "book_halted"
        assert out["books_halted"] == 60
        # The new benign state must not have absorbed the real fault.
        assert out["reported_as_benign_boundary_state"] == 0
        assert out["control_halts"] == 0
        assert out["control_faults"] == 0


class TestTheVerifierCanStillFail:
    """The positive control, on the venue's own recording of the defect.

    Every assertion here would pass just as happily if `property_2` had been
    written to return PROVEN unconditionally. This is what rules that out.
    """

    def test_property_2_FAILS_on_the_session_that_measured_the_defect(
            self, failed_run):
        with pytest.raises(verify.Failed) as exc:
            verify.property_2(failed_run)
        # And it fails for the RIGHT reason: the CP7 shape, not a missing
        # field, not a count, not an exception from reading an old artifact.
        assert "CP7 failure shape" in str(exc.value)
        assert "republished 60 markets at once" in str(exc.value)

    def test_the_defect_is_actually_present_in_that_artifact(self, failed_run):
        """The other half: the control artifact must really contain the defect,
        or `raises` above could be passing on a corrupt file."""
        acq = verify.acquisitions(failed_run["publishability_timeline"], epoch=2)
        assert len(acq) == 60
        # ONE entry carried all sixty, each caused by a SIBLING's snapshot.
        assert {a["acquisitions_in_this_entry"] for a in acq} == {60}
        assert len({a["frame_ordinal"] for a in acq}) == 1
        foreign = [a for a in acq if a["caused_by"] != a["market"]]
        assert len(foreign) == 59, (
            "59 of 60 markets must have been republished on a sibling's "
            "snapshot; that is the measured CP7 defect")

    def test_the_old_artifact_carried_no_typed_state_which_is_why_this_exists(
            self, failed_run, rerun):
        """The reason the probe was changed before the re-run ran."""
        old = verify.acquisitions(failed_run["publishability_timeline"], epoch=2)
        assert not any(a["typed_state_recorded"] for a in old)
        new = verify.acquisitions(
            rerun["s2-reconnect"]["publishability_timeline"], epoch=2)
        assert all(a["typed_state_recorded"] for a in new)


class TestTheDeltaRefusalPathIsReportedHonestly:
    """CP7 could only say the serious case "did not happen to occur"."""

    def test_the_refusal_path_was_not_exercised_and_says_so(self, rerun):
        out = verify.property_4(rerun["s2-reconnect"])
        assert out["observed_refusals"] == 0
        assert out["rejected_pre_generation_snapshot_total"] == 0
        # The verdict must not read as a pass. Luck is not proof.
        assert out["verdict"].startswith("NOT EXERCISED")
        assert "not the same as proven" in out["verdict"].lower()

    def test_the_reporter_would_have_said_EXERCISED_if_it_had_fired(self, rerun):
        """Anti-vacuity for the reporter itself: "NOT EXERCISED" must be a
        measurement, not the only thing this function can say."""
        forged = json.loads(json.dumps(rerun["s2-reconnect"]))
        forged["generation_delta_refusals"] = [
            {"frame_ordinal": 605, "market_ticker": "X",
             "rejected_pre_generation_snapshot_total": 1}]
        out = verify.property_4(forged)
        assert out["verdict"] == "EXERCISED"
        assert out["observed_refusals"] == 1

    def test_an_artifact_without_the_observer_is_NOT_MEASURED_not_zero(
            self, failed_run):
        """Doctrine 10 at the artifact boundary: the 2026-08-17 run had no
        refusal observer, and that absence must not be reported as a zero."""
        out = verify.property_4(failed_run)
        assert out["verdict"] == "NOT MEASURED"
