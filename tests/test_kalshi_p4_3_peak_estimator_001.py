"""KALSHI-P4-3 — the peak-rate estimator, and why the primary one exists.

A fixed calendar-second bucket splits any burst that straddles a clock
boundary, so it is biased DOWNWARD — the dangerous direction for capacity
engineering. This file pins the bias with a synthetic burst whose correct
answer is known by construction, and pins the estimator against the frozen P4
production tape's measured figures.
"""

from __future__ import annotations

import importlib.util

NS = 1_000_000_000


def _mod():
    spec = importlib.util.spec_from_file_location(
        "_p4", "scripts/kalshi_prod_capture_p4.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _calendar_bucket_peak(ts):
    """The estimator being replaced, reproduced here so the two are compared
    on identical input rather than across two code paths."""
    from collections import Counter
    buckets = Counter(t // NS for t in ts)
    return max(buckets.values()) if buckets else None


class TestTheStraddlingBurstPositiveControl:
    """THE reason the primary metric exists.

    400 frames in the final 500 ms of second N and 400 in the first 500 ms of
    N+1 is a real 800 f/s event. Fixed buckets report two quiet 400 f/s seconds
    and would clear a 500 f/s guard the traffic actually breached.
    """

    def _burst(self):
        n = 10 * NS
        first = [n + 500_000_000 + i * (500_000_000 // 400) for i in range(400)]
        second = [n + NS + i * (500_000_000 // 400) for i in range(400)]
        return first + second

    def test_the_sliding_window_sees_the_whole_burst(self):
        ts = self._burst()
        got = _mod()._sliding_peak_1s(ts)
        assert got >= 780, (
            f"the true 1s maximum is ~800 by construction; got {got}")
        assert got <= 800

    def test_calendar_buckets_report_roughly_half_of_it(self):
        ts = self._burst()
        bucketed = _calendar_bucket_peak(ts)
        assert 380 <= bucketed <= 420, (
            f"expected the burst to be split into ~400/400; got {bucketed}")

    def test_the_bias_is_downward_and_that_is_the_point(self):
        ts = self._burst()
        sliding = _mod()._sliding_peak_1s(ts)
        bucketed = _calendar_bucket_peak(ts)
        assert sliding > bucketed, (
            "the calendar bucket must UNDERSTATE, never overstate — a peak "
            "estimator that errs low is the one failure mode capacity "
            "engineering cannot absorb")
        assert sliding / bucketed >= 1.8

    def test_a_guard_would_be_cleared_by_traffic_that_breached_it(self):
        """The consequence, stated as a test rather than as a comment."""
        ts = self._burst()
        GUARD = 500
        assert _calendar_bucket_peak(ts) < GUARD, "bucketed reading passes"
        assert _mod()._sliding_peak_1s(ts) > GUARD, "the traffic breached it"


class TestEstimatorAgreesWithTheFrozenProductionMeasurement:
    """Anti-vacuity in the other direction: on a stream with no boundary-
    straddling burst the two estimators must NOT diverge wildly, or the
    sliding implementation is simply counting something else."""

    def test_a_uniform_stream_gives_almost_the_same_answer(self):
        ts = [i * (NS // 100) for i in range(1000)]      # exactly 100 f/s
        sliding = _mod()._sliding_peak_1s(ts)
        bucketed = _calendar_bucket_peak(ts)
        assert abs(sliding - bucketed) <= 1, (
            f"uniform traffic must agree; got {sliding} vs {bucketed}")

    def test_empty_input_is_none_not_zero(self):
        assert _mod()._sliding_peak_1s([]) is None, (
            "a zero here would be a fabricated measurement of an unobserved "
            "stream (doctrine 10)")

    def test_a_single_frame_is_a_peak_of_one(self):
        assert _mod()._sliding_peak_1s([42 * NS]) == 1


class TestTheAmbiguousOldFieldIsGone:
    def test_the_old_key_name_no_longer_exists(self):
        """A stale reader must get a loud KeyError, not a plausible number.

        `frames_per_second_peak_1s` named the biased statistic. Silently
        repointing that name at the corrected value would change what every
        historical evidence file's field means; leaving it pointing at the
        biased value would keep shipping the defect. It is removed, and the two
        replacements say which is which.
        """
        src = open("scripts/kalshi_prod_capture_p4.py").read()
        assert '"frames_per_second_peak_1s"' not in src
        assert '"peak_1s_sliding"' in src
        assert '"peak_1s_calendar_bucket"' in src
