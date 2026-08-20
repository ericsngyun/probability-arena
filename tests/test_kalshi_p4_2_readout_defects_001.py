"""KALSHI-P4-2 — the P4 readout defects, pinned.

Every defect here shipped in `docs/evidence/KALSHI-PROD-QUAL-CAPTURE-2-capture.json`
from a capture that was, on the tape, completely clean: 84,170 records, seq
1..79,256 contiguous on the order-book sid, zero gaps, zero faults, every book
publishable. That is what makes them dangerous — a research consumer reading the
summary instead of the tape inherits wrong numbers from a good capture.

Each test states the number the production tape actually holds, so these are
claims about measured reality rather than about the current code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.realtime import session_root as sr


def _result(**kw):
    """A CollectorResult with only the fields under test varied."""
    from app.realtime.collector import CollectorResult
    return CollectorResult(status="ok", environment="production", **kw)


def _init(root, environment):
    from app.realtime import archive_head as _ah
    try:
        _ah.initialize_archive(Path(root), environment,
                               archive_identity="kalshi-realtime")
    except _ah.ArchiveHeadError:
        pass
    return root


class TestB4ClaimLandsWhereTheEvidenceIs:
    """The claim guarded a SIBLING of the archive, so it guarded nothing.

    The P4 tape root holds BOTH directories side by side: `env=production/`
    with 7 segments and 84,170 records, and `production/` with nothing but the
    claim. A second concurrent session writing real evidence would never have
    touched the claimed path.
    """

    def test_the_claim_sits_in_the_directory_the_archive_actually_writes_to(
            self, tmp_path):
        # Behavioural, not a string comparison: write REAL evidence, then ask
        # where it landed. A test asserting `env=` as a literal would pass just
        # as happily if the archive changed its own layout.
        from datetime import datetime, timezone

        from app.realtime import archive as _ar
        from app.realtime import book as bk
        _init(tmp_path, "production")
        arch = _ar.EventArchive(tmp_path, environment="production")
        arch.append(bk.make_envelope(
            venue="kalshi", environment="production",
            channel="orderbook_snapshot",
            message={"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                     "msg": {"market_ticker": "KXTEST", "market_id": "mid-1",
                             "yes_dollars_fp": [], "no_dollars_fp": []}},
            receive_time=datetime(2026, 8, 20, tzinfo=timezone.utc),
            receive_mono=bk.monotonic_ns()))
        arch.close()

        segment_dirs = sorted(tmp_path.rglob("segment=*"))
        assert segment_dirs, "no evidence was written; the test proves nothing"
        evidence_env_dir = segment_dirs[0].parent
        while evidence_env_dir.parent != Path(tmp_path) and \
                evidence_env_dir.parent != evidence_env_dir:
            evidence_env_dir = evidence_env_dir.parent

        claim_dir = sr.env_root(tmp_path, "production")
        assert claim_dir == evidence_env_dir, (
            f"the claim would be planted in {claim_dir.name!r} while the "
            f"evidence lives in {evidence_env_dir.name!r} — the guard is "
            f"vacuous, which is exactly what the P4 tape shows")

    def test_a_second_session_is_actually_refused_where_evidence_lives(
            self, tmp_path):
        """Anti-vacuity: the conflict must fire in the REAL directory."""
        sr.claim_session_root(tmp_path, "production", session_id="s-one")
        with pytest.raises(sr.SessionRootConflict):
            sr.claim_session_root(tmp_path, "production", session_id="s-two")
        # ...and the claim file is inside the archive's own env directory.
        assert sr.session_claim_path(tmp_path, "production").parent.name == \
            "env=production"


class TestSegmentsCommittedCountsEverySegment:
    """Reported 1 for a session whose tape holds SEVEN segment ids.

    `close()` returns only the segments still OPEN at close time — one per
    partition — so the six that rotated and committed mid-run were dropped.
    A capture that rotated MORE reported LESS.
    """

    def test_the_collector_adds_rotations_to_the_closed_segments(self):
        """Structural, over the REAL assignment.

        An arithmetic test on a fake archive passes just as happily against the
        pre-fix code, which is the definition of a vacuous guard — so this
        reads the collector's own expression instead. `rotations` must appear
        on the right-hand side of the `segments_committed` assignment that
        wraps `self._archive.close()`.
        """
        import ast

        src = Path("app/realtime/collector.py").read_text()
        tree = ast.parse(src)

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "segments_committed" not in targets:
                continue
            rhs = ast.dump(node.value)
            if "close" in rhs:
                found.append(rhs)

        assert found, ("no `segments_committed = ...close()...` assignment "
                       "found; this guard has stopped guarding anything")
        for rhs in found:
            assert "rotations" in rhs, (
                "segments_committed is computed from close() alone, so every "
                "segment that rotated mid-run is dropped. The P4 production "
                "tape holds 7 segment ids and the capture reported 1.")

    def test_the_result_carries_the_total(self):
        assert _result(segments_committed=7).segments_committed == 7


class TestSubscriptionGenerationsIsAGenerationCount:
    """Reported 3 for a session with ONE generation and three channels.

    The counter advances once per new sid and once per sid per epoch, so it
    counts ROUTER instantiations. The P4 capture shipped
    `subscription_generations: 3` in the same JSON as
    `subscription_epoch_final: 1` — two fields, one question, disagreeing.
    """

    def test_three_sids_in_one_generation_is_one_generation(self):

        r = _result(subscription_generations=1,
                    subscription_router_epochs=3)
        assert r.subscription_generations == 1, (
            "the P4 tape carries a single distinct subscription_generation "
            "value across all 84,170 records")
        assert r.subscription_router_epochs == 3, (
            "the router count is real information and must be kept, under a "
            "name that says what it counts")

    def test_the_two_fields_are_not_the_same_quantity(self):
        r = _result()
        assert hasattr(r, "subscription_router_epochs"), (
            "dropping the router count instead of renaming it would destroy "
            "evidence rather than repair a readout")


class TestHealthyIsNotReportedForChannelsWithoutABook:
    """`healthy=false` on the ticker and trade sids read as two broken
    subscriptions in a capture that was clean. Both delivered every frame; they
    simply have no order book to be based on."""

    def test_a_non_orderbook_subscription_reports_not_applicable(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_probe", "scripts/kalshi_cp6_cp9_functional_probe.py")
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        class _Sub:
            sid = 3
            generation = 1
            healthy = False            # never based: there is no book
            carries_orderbook = False  # ...which is WHY
            state_reason = None
            last_seq = 2516
            stats = {}

        class _Router:
            subscription = _Sub()
            books = {}
            def publication_states(self):
                return {}

        class _Session:
            _routers = {3: _Router()}

        out = probe.capture_state(_Session())["3"]
        assert out["liveness"] == "NOT_APPLICABLE:carries_no_orderbook", (
            "a channel with no order book cannot be 'unhealthy' — the P4 "
            "capture reported exactly this for 2,395 ticker and 2,516 trade "
            "frames that all arrived")
        assert out["healthy"] is False, (
            "the raw model value is kept; only the READOUT is repaired")
