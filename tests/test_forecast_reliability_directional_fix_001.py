"""FORECAST-RELIABILITY-DIRECTIONAL-FIX-001.

The reported "anomaly" — over_share and under_share both 0.0 while the signed
aggregate gap is nonzero — is NOT a defect. Every populated production bin sat
inside CALIB_TOLERANCE, so every bin was `approximately_calibrated` and belonged
to neither share. What was genuinely wrong is that the residual holding all that
mass was invisible, so a reader could not distinguish "excellently calibrated"
from "no data at all". These tests pin the partition and the distinction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.forecast_reliability import (
    APPROX_CALIBRATED,
    CALIB_TOLERANCE,
    OVERCONF_POS,
    UNDERCONF_POS,
    compute_bins,
    direction,
    directional_summary,
)
from app.services.forecast_reliability import _Point  # noqa: PLC2701

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)
EDGES = [i / 10 for i in range(11)]


def pts(spec):
    """spec: list of (probability, outcome, repeat)."""
    out = []
    for p, y, k in spec:
        for _ in range(k):
            out.append(_Point(p=p, y=y, created_at=NOW, domain="d",
                              forecaster="f:v1", evidence_depth="e",
                              forecast_risk="r", research_completeness_bucket="0.90-1.00",
                              research_risk="low", resolution_risk="low",
                              tradeability="researchable"))
    return out


def summary(points):
    return directional_summary(compute_bins(points, EDGES), points)


class TestPartition:
    """The three shares must partition 1.0 exactly. This is the A2 contract."""

    @pytest.mark.parametrize("spec", [
        [(0.9, 0.0, 50), (0.9, 1.0, 10)],                       # heavy over
        [(0.2, 1.0, 40), (0.2, 0.0, 10)],                       # heavy under
        [(0.5, 1.0, 25), (0.5, 0.0, 25)],                       # calibrated
        [(0.9, 0.0, 30), (0.2, 1.0, 30), (0.5, 1.0, 15), (0.5, 0.0, 15)],
        [(0.05, 0.0, 100), (0.95, 1.0, 100)],
    ])
    def test_shares_are_non_negative_and_sum_to_one(self, spec):
        d = summary(pts(spec))
        over = d["overprediction_weighted_share"]
        under = d["underprediction_weighted_share"]
        calib = d["approximately_calibrated_weighted_share"]
        unc = d["unclassified_weighted_share"]
        assert over >= 0 and under >= 0 and calib >= 0 and unc >= 0
        assert over + under <= 1.0 + 1e-9
        assert over + under + calib + unc == pytest.approx(1.0, abs=1e-3)

    def test_weight_basis_is_declared_and_is_forecast_count(self):
        # 30 overconfident + 10 underconfident. NOTE: a low probability that
        # resolves YES is overconfident_NEGATIVE, not underconfident — the
        # naive pairing (0.9,no)+(0.1,yes) is two OVER bins, not a mix.
        d = summary(pts([(0.9, 0.0, 30), (0.55, 1.0, 10)]))
        assert d["directional_weight_basis"] == "forecast_count"
        assert d["overprediction_weighted_share"] == pytest.approx(0.75, abs=1e-3)
        assert d["underprediction_weighted_share"] == pytest.approx(0.25, abs=1e-3)


class TestDirections:
    def test_pure_overprediction(self):
        d = summary(pts([(0.9, 0.0, 60)]))
        assert d["overprediction_weighted_share"] == pytest.approx(1.0)
        assert d["underprediction_weighted_share"] == 0.0
        assert d["approximately_calibrated_weighted_share"] == 0.0

    def test_pure_underprediction(self):
        d = summary(pts([(0.55, 1.0, 60)]))
        assert d["underprediction_weighted_share"] == pytest.approx(1.0)
        assert d["overprediction_weighted_share"] == 0.0

    def test_perfectly_calibrated_bins_land_in_the_residual(self):
        """The production case. Both shares 0.0 is CORRECT, and the residual
        must say so rather than leaving the reader to guess."""
        d = summary(pts([(0.5, 1.0, 50), (0.5, 0.0, 50)]))
        assert d["overprediction_weighted_share"] == 0.0
        assert d["underprediction_weighted_share"] == 0.0
        assert d["approximately_calibrated_weighted_share"] == pytest.approx(1.0)

    def test_mixed_direction(self):
        d = summary(pts([(0.9, 0.0, 40), (0.55, 1.0, 40)]))
        assert d["overprediction_weighted_share"] > 0
        assert d["underprediction_weighted_share"] > 0
        assert d["overprediction_weighted_share"] + \
            d["underprediction_weighted_share"] == pytest.approx(1.0, abs=1e-3)

    def test_gap_exactly_at_tolerance_is_calibrated(self):
        # Literals, not 0.5 + CALIB_TOLERANCE: that sum is 0.55000000000000004
        # in binary floating point, which is OUTSIDE tolerance and would make
        # this test assert the opposite of what it reads as.
        assert direction(0.55, 0.50) == APPROX_CALIBRATED
        assert direction(0.56, 0.50) == OVERCONF_POS
        assert direction(0.50, 0.56) == UNDERCONF_POS


class TestAggregateGapIsADifferentQuantity:
    def test_signed_gap_can_be_nonzero_while_both_shares_are_zero(self):
        """The exact production configuration, reproduced synthetically.

        Every bin leans the same way but stays inside tolerance, so the shares
        are 0.0 while the aggregate gap is not. These are different statistics
        and must never be reconciled against each other.
        """
        spec = []
        for lo in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95):
            # observed rate 0.04 below the forecast in every bin -> inside tol
            yes = int(round((lo - 0.04) * 100))
            spec.append((lo, 1.0, yes))
            spec.append((lo, 0.0, 100 - yes))
        d = summary(pts(spec))
        assert d["overprediction_weighted_share"] == 0.0
        assert d["underprediction_weighted_share"] == 0.0
        assert d["approximately_calibrated_weighted_share"] == pytest.approx(1.0)
        assert d["signed_calibration_gap"] > 0.0, (
            "aggregate gap must survive even though every bin is calibrated")


class TestEdgeCases:
    def test_zero_scored_rows(self):
        d = directional_summary(compute_bins([], EDGES), [])
        assert d["signed_calibration_gap"] is None
        assert d["overprediction_weighted_share"] == 0.0
        assert d["underprediction_weighted_share"] == 0.0
        assert d["approximately_calibrated_weighted_share"] == 0.0

    def test_thin_bins_still_carry_their_weight(self):
        """Thin bins are labelled for reporting but must not vanish from the
        partition — dropping them would silently break the sum."""
        d = summary(pts([(0.95, 0.0, 2), (0.5, 1.0, 49), (0.5, 0.0, 49)]))
        total = (d["overprediction_weighted_share"]
                 + d["underprediction_weighted_share"]
                 + d["approximately_calibrated_weighted_share"]
                 + d["unclassified_weighted_share"])
        assert total == pytest.approx(1.0, abs=1e-3)

    def test_rounding_does_not_break_the_bound(self):
        d = summary(pts([(0.9, 0.0, 333), (0.2, 1.0, 333), (0.5, 1.0, 334)]))
        assert d["overprediction_weighted_share"] + \
            d["underprediction_weighted_share"] <= 1.0


class TestReportParity:
    def test_service_and_both_renderings_expose_the_partition(self, capsys):
        """Parity is asserted on a populated report; an empty database returns
        an INSUFFICIENT_DATA status rather than a full document, which would
        make this test pass for the wrong reason."""
        import json as _json

        d = summary(pts([(0.9, 0.0, 40), (0.55, 1.0, 40), (0.5, 1.0, 30),
                         (0.5, 0.0, 30)]))
        for key in ("overprediction_weighted_share",
                    "underprediction_weighted_share",
                    "approximately_calibrated_weighted_share",
                    "unclassified_weighted_share",
                    "directional_weight_basis", "calibration_tolerance"):
            assert key in d, key
        assert d["directional_weight_basis"] == "forecast_count"
        assert d["calibration_tolerance"] == CALIB_TOLERANCE
        # JSON-serializable without loss
        assert _json.loads(_json.dumps(
            {k: v for k, v in d.items() if not k.startswith("_")})) == \
            {k: v for k, v in d.items() if not k.startswith("_")}

    def test_text_renderer_states_the_basis_and_the_disclaimer(self):
        src = Path("app/cli.py").read_text()
        assert "directional_weight_basis" in src
        assert "approx_calibrated_share" in src
        assert "NOT the complement" in src


class TestProductionRegression:
    def test_the_reported_production_shape_is_not_a_defect(self):
        """Regression fixture built from the real 2026-08-05 bin table: 10
        populated bins, max |gap| 0.045156, CALIB_TOLERANCE 0.05."""
        real = [(0.055, 0.039, 948), (0.1509, 0.1243, 1030), (0.254, 0.2088, 1494),
                (0.3518, 0.3316, 1903), (0.4504, 0.4125, 2439), (0.5455, 0.5576, 2432),
                (0.6444, 0.6791, 1097), (0.7416, 0.7299, 585), (0.8508, 0.8103, 448),
                (0.9515, 0.9415, 513)]
        for mean_p, obs, _ in real:
            assert abs(mean_p - obs) <= CALIB_TOLERANCE
            assert direction(mean_p, obs) == APPROX_CALIBRATED
        spec = []
        for mean_p, obs, n in real:
            yes = int(round(obs * n))
            spec.append((mean_p, 1.0, yes))
            spec.append((mean_p, 0.0, n - yes))
        d = summary(pts(spec))
        assert d["overprediction_weighted_share"] == 0.0
        assert d["underprediction_weighted_share"] == 0.0
        assert d["approximately_calibrated_weighted_share"] == pytest.approx(1.0)
        assert d["signed_calibration_gap"] != 0.0
