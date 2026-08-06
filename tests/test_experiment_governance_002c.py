"""PROSPECTIVE-EXPERIMENT-REGISTRY-002C — governance edge cases.

M4 universe authority, M6 governed re-pinning, M8 uncertainty-aware
falsification, M9 NULL accounting, M5 non-authoritative prose.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import experiment_amendments as am
from app.services import experiment_predicates as pr
from app.services import experiment_results as rs
from app.services import experiment_universe as un

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def universe_doc(**over):
    d = {
        "schema_version": 1,
        "universe_id": "kxmlb-2026-season",
        "selection_method": "exhaustive_series",
        "selection_source": {"kind": "kalshi_series", "series_ticker": "KXMLB"},
        "created_at": "2026-08-01T00:00:00+00:00",
        "members": ["KXMLB-A", "KXMLB-B", "KXMLB-C"],
        "member_count": 3,
    }
    d.update(over)
    return d


def write_universe(tmp_path, doc=None):
    doc = doc or universe_doc()
    d = tmp_path / un.UNIVERSE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc['universe_id']}.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return doc, un.universe_digest(doc)


class TestUniverseAuthority:
    """M4 — presence of a field is not authority."""

    def test_valid_universe(self):
        v = un.validate_universe(universe_doc())
        assert v.ok, v.errors
        assert v.member_count == 3

    def test_free_form_selection_method_rejected(self):
        """The exact bypass the review used: prose that avoids a substring list."""
        v = un.validate_universe(universe_doc(
            selection_method="hand picked after looking at results"))
        assert not v.ok
        assert any("typed methods" in e for e in v.errors)

    def test_every_selection_method_is_a_checkable_rule(self):
        for name, desc in un.SELECTION_METHODS.items():
            assert desc and isinstance(desc, str)
        assert un.HAND_SELECTED_METHODS == ()

    def test_member_count_is_not_an_independent_assertion(self):
        v = un.validate_universe(universe_doc(member_count=99))
        assert not v.ok
        assert any("disagrees" in e for e in v.errors)

    def test_digest_is_order_insensitive(self):
        a = universe_doc(members=["KXMLB-C", "KXMLB-A", "KXMLB-B"])
        b = universe_doc(members=["KXMLB-A", "KXMLB-B", "KXMLB-C"])
        assert un.universe_digest(a) == un.universe_digest(b)

    def test_missing_artifact_is_refused(self, tmp_path):
        with pytest.raises(un.UniverseError, match="no committed artifact"):
            un.resolve_universe({"universe_id": "kxmlb-2026-season",
                                 "digest": "x"}, base=tmp_path)

    def test_digest_mismatch_is_refused(self, tmp_path):
        doc, digest = write_universe(tmp_path)
        with pytest.raises(un.UniverseError, match="digest mismatch"):
            un.resolve_universe({"universe_id": doc["universe_id"],
                                 "digest": "0" * 64}, base=tmp_path)

    def test_universe_created_after_registration_is_refused(self, tmp_path):
        doc, digest = write_universe(
            tmp_path, universe_doc(created_at="2026-09-01T00:00:00+00:00"))
        with pytest.raises(un.UniverseError, match="AFTER"):
            un.resolve_universe({"universe_id": doc["universe_id"],
                                 "digest": digest}, base=tmp_path,
                                registered_at=NOW)

    def test_resolution_succeeds_when_everything_lines_up(self, tmp_path):
        doc, digest = write_universe(tmp_path)
        out = un.resolve_universe(
            {"universe_id": doc["universe_id"], "digest": digest,
             "member_count": 3}, base=tmp_path, registered_at=NOW)
        assert out["members"] == ["KXMLB-A", "KXMLB-B", "KXMLB-C"]

    def test_enumerating_outside_the_universe_is_refused(self, tmp_path):
        doc, digest = write_universe(tmp_path)
        resolved = un.resolve_universe(
            {"universe_id": doc["universe_id"], "digest": digest},
            base=tmp_path, registered_at=NOW)
        errs = un.check_universe_covers(["KXMLB-A", "SNEAKY-Z"], resolved)
        assert errs and "outside the resolved universe" in errs[0]

    def test_path_escape_rejected(self, tmp_path):
        for bad in ("../escape", "a/b", "/abs", ".."):
            with pytest.raises(un.UniverseError):
                un.universe_path(bad, tmp_path)

    def test_confirmatory_manifest_with_a_fabricated_universe_is_rejected(
            self, tmp_path):
        canon = pr.canonicalize_population({
            "schema_version": 1, "window_end": "unbounded",
            "all": [{"field": "market_ticker", "operator": "in",
                     "value": ["KXMLB-A", "KXMLB-B"]}], "none": []})
        errs = pr.check_identifier_cohort(
            canon, kind="confirmatory",
            universe={"universe_id": "kxmlb-2026-season", "digest": "deadbeef",
                      "created_at": "2026-08-01T00:00:00+00:00",
                      "selection_method": "hand picked after looking at results",
                      "member_count": 3},
            universe_base=tmp_path, registered_at=NOW)
        assert errs and any("universe:" in e for e in errs)


class TestGovernedRepinning:
    """M6 — a bug fix must not permanently invalidate every experiment."""

    def _snap(self, digest):
        return {"metric_code_digests": {"app/services/calibration.py": digest},
                "baseline_definition_version": 1, "ci_policy_version": 1}

    def test_free_form_reason_rejected(self):
        with pytest.raises(am.AmendmentError, match="typed reasons"):
            am.classify_change("we fixed a thing")

    def test_semantic_change_cannot_declare_collection_comparable(self, tmp_path):
        """The hard rule: a change that could move a number does not get waved
        through as a refactor."""
        with pytest.raises(am.AmendmentError, match="semantic change"):
            am.apply_amendment(
                reason=am.REASON_METRIC_DEFINITION_CHANGE, detail="d",
                reviewer="r", review_reference="ref",
                reference_kind="metric_references",
                old_snapshot=self._snap("a" * 64), new_snapshot=self._snap("b" * 64),
                affected_experiments=["e1"], collection_comparable=True,
                base=tmp_path, confirm=False)

    def test_non_semantic_change_may_declare_comparable(self, tmp_path):
        out = am.apply_amendment(
            reason=am.REASON_COMMENT_OR_TYPING, detail="typo in a docstring",
            reviewer="eric", review_reference="review-002c",
            reference_kind="metric_references",
            old_snapshot=self._snap("a" * 64), new_snapshot=self._snap("b" * 64),
            affected_experiments=["e1"], collection_comparable=True,
            base=tmp_path, confirm=True)
        assert out["persisted"] is True
        assert out["amendment"]["requires_new_experiment_version"] is False

    def test_semantic_change_requires_a_new_experiment_version(self):
        cls = am.classify_change(am.REASON_DEFECT_FIX_SEMANTIC)
        assert cls["requires_new_experiment_version"] is True
        assert cls["may_declare_comparable"] is False

    def test_amendment_requires_a_reviewer_and_a_real_movement(self, tmp_path):
        with pytest.raises(am.AmendmentError, match="reviewer"):
            am.apply_amendment(
                reason=am.REASON_DOCUMENTATION, detail="d", reviewer="",
                review_reference="", reference_kind="metric_references",
                old_snapshot=self._snap("a" * 64), new_snapshot=self._snap("b" * 64),
                affected_experiments=["e1"], base=tmp_path)
        with pytest.raises(am.AmendmentError, match="actually changed"):
            am.apply_amendment(
                reason=am.REASON_DOCUMENTATION, detail="d", reviewer="e",
                review_reference="r", reference_kind="metric_references",
                old_snapshot=self._snap("a" * 64), new_snapshot=self._snap("a" * 64),
                affected_experiments=["e1"], base=tmp_path)

    def test_amendments_are_hash_chained_and_head_pinned(self, tmp_path):
        for i in range(2):
            am.apply_amendment(
                reason=am.REASON_DOCUMENTATION, detail=f"d{i}", reviewer="e",
                review_reference="r", reference_kind="metric_references",
                old_snapshot=self._snap(chr(97 + i) * 64),
                new_snapshot=self._snap(chr(98 + i) * 64),
                affected_experiments=["e1"], base=tmp_path, confirm=True)
        assert am.verify_amendment_chain(tmp_path)["intact"] is True
        log = tmp_path / "experiments" / am.AMENDMENTS_FILENAME
        log.write_text(log.read_text().splitlines()[0] + "\n")
        assert am.verify_amendment_chain(tmp_path)["intact"] is False

    def test_amendment_names_the_digest_it_moved_to(self, tmp_path):
        """A blanket amendment cannot pre-authorize an arbitrary future change."""
        new = self._snap("b" * 64)
        am.apply_amendment(
            reason=am.REASON_DOCUMENTATION, detail="d", reviewer="e",
            review_reference="r", reference_kind="metric_references",
            old_snapshot=self._snap("a" * 64), new_snapshot=new,
            affected_experiments=["e1"], collection_comparable=True,
            base=tmp_path, confirm=True)
        good = am._hash(new)
        assert am.amendment_for("e1", "metric_references", good, tmp_path)
        assert am.amendment_for("e1", "metric_references", "0" * 64, tmp_path) is None
        assert am.amendment_for("other", "metric_references", good, tmp_path) is None


class TestFalsificationRule:
    """M8 — a negative point estimate cannot confirm a negative hypothesis."""

    def test_ci_upper_rule_is_supported(self):
        assert rs.DECISION_CI_UPPER_LT_ZERO in rs.SUPPORTED_DECISION_RULES

    def test_interval_must_exclude_zero(self):
        supports, _ = rs._derive_verdict(
            integrity_ok=True, drift_material=False, data_quality_bad=False,
            stopping_met=True, floor_met=True, delta=-0.04,
            decision_rule=rs.DECISION_CI_UPPER_LT_ZERO,
            ci={"lower": -0.09, "upper": -0.01})
        assert supports == rs.SUPPORTS
        # same negative point estimate, interval straddling zero -> not confirmed
        straddle, _ = rs._derive_verdict(
            integrity_ok=True, drift_material=False, data_quality_bad=False,
            stopping_met=True, floor_met=True, delta=-0.04,
            decision_rule=rs.DECISION_CI_UPPER_LT_ZERO,
            ci={"lower": -0.12, "upper": 0.05})
        assert straddle == rs.DOES_NOT_SUPPORT

    def test_tennis_draft_uses_the_uncertainty_aware_rule(self):
        m = json.loads(
            Path("manifests/tennis-base-rate-falsification.json").read_text())
        assert m["result_protocol"]["decision_rule"] == rs.DECISION_CI_UPPER_LT_ZERO
        assert any("point estimate" in k.lower() for k in m["known_limitations"])


class TestNullAccounting:
    """M9 — unknown exclusions and retentions must be visible."""

    def test_unknown_exclusions_are_separated_from_rule_exclusions(self):
        canon = pr.canonicalize_population({
            "schema_version": 1, "window_end": "unbounded",
            "all": [{"field": "forecaster_version", "operator": "not_eq",
                     "value": "v2"}], "none": []})
        facts = pr.ForecastFacts(forecast_id=1, values={
            "forecast_created_at": NOW, "forecaster_version": None})
        ok, reasons = pr.evaluate(canon, facts, registered_at=NOW)
        assert ok is False
        assert reasons == ["all:forecaster_version:not_eq:unknown"]

    def test_none_clause_unknown_retention_is_collected(self):
        canon = pr.canonicalize_population({
            "schema_version": 1, "window_end": "unbounded", "all": [],
            "none": [{"field": "domain", "operator": "eq", "value": "crypto"}]})
        facts = pr.ForecastFacts(forecast_id=1, values={
            "forecast_created_at": NOW, "domain": None})
        retained: list = []
        ok, reasons = pr.evaluate(canon, facts, registered_at=NOW,
                                  collect_unknown=retained)
        assert ok is True, "an unknown veto does not exclude"
        assert retained == ["none:domain:eq:unknown"], (
            "but the retention must be visible — it used to be counted nowhere")

    def test_de_morgan_divergence_is_real_and_both_sides_are_reported(self):
        facts = pr.ForecastFacts(forecast_id=1, values={
            "forecast_created_at": NOW, "domain": None})
        neg = pr.canonicalize_population({
            "schema_version": 1, "window_end": "unbounded",
            "all": [{"field": "domain", "operator": "not_eq", "value": "crypto"}],
            "none": []})
        veto = pr.canonicalize_population({
            "schema_version": 1, "window_end": "unbounded", "all": [],
            "none": [{"field": "domain", "operator": "eq", "value": "crypto"}]})
        assert pr.evaluate(neg, facts, registered_at=NOW)[0] is False
        assert pr.evaluate(veto, facts, registered_at=NOW)[0] is True


class TestOperatorProse:
    """M5 — annotations, bounded and scanned, never semantic control."""

    def test_note_length_is_bounded(self):
        assert rs.MAX_NOTE_CHARS == 2000

    def test_prose_cannot_reach_membership_metrics_or_verdict(self):
        """The real control is that nothing reads it back."""
        import ast

        src = Path("app/services/experiment_results.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for side in [node.left] + list(node.comparators):
                    if isinstance(side, ast.Name) and side.id in (
                            "operator_notes", "reevaluation_reason"):
                        raise AssertionError("prose is being branched on")
        # it appears only as a stored field and in its own validation
        assert "operator_notes=operator_notes" in src
