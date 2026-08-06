"""PROSPECTIVE-EXPERIMENT-REGISTRY-001.

Nearly every test here asserts a REFUSAL. The registry's value is entirely in
what it will not let a researcher do after seeing results — widen an inclusion
rule, swap a primary metric, quietly delete a failed experiment. A registry that
accepts everything is worse than none, because it launders exploratory work as
confirmatory.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import experiment_registry as er

FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def base_manifest(**over):
    m = {
        "experiment_id": "baseball-calibration-stability",
        "experiment_version": 1,
        "title": "Baseball prospective calibration stability",
        "research_question": "Does baseball calibration hold prospectively?",
        "hypothesis": "Brier skill vs base rate remains positive.",
        "null_hypothesis": "Brier skill vs base rate is <= 0.",
        "exploratory_or_confirmatory": er.CONFIRMATORY,
        "experiment_class": er.CLASS_PROSPECTIVE_CALIBRATION,
        "owner": "eric",
        "domain": "sports_baseball",
        "market_population": "Kalshi baseball markets scanned by the standard scan",
        "forecast_family": "baseball_evidence",
        "forecast_version": "v1",
        "feature_definitions": {"probability": "forecast estimated_probability"},
        "signal_definitions": {"none": "no signal gating"},
        "data_sources": ["kalshi_rest"],
        "provider_policy": "no new provider; existing read-only detail path only",
        "population": {
            "schema_version": 1,
            "all": [
                {"field": "domain", "operator": "eq", "value": "sports_baseball"},
                {"field": "forecast_created_at",
                 "operator": "gte_registration_time"},
            ],
            "none": [],
            "window_end": "unbounded",
            "rationale": ["prose is rationale only; membership is typed"],
        },
        "start_condition": "first forecast created after registration",
        "end_condition": "sample floor reached and all members matured",
        "end_time": None,
        "evaluation_horizons": ["market close + 1h settlement grace"],
        "primary_metric": {"name": "brier_skill_vs_base_rate",
                           "definition": "1 - model_brier/base_rate_brier"},
        "secondary_metrics": [{"name": "ece"}],
        "declared_baselines": {"base_rate_brier": "prevalence-based Brier"},
        "sample_floor": 500,
        "domain_sample_floors": {"sports_baseball": 500},
        "minimum_matured_fraction": 0.9,
        "missing_data_policy": "pending members excluded and counted",
        "canceled_void_policy": "excluded and counted; never scored",
        "conflict_policy": "excluded and preserved; never repaired",
        "stale_score_policy": "excluded; only scored_current enters",
        "multiple_testing_policy": "single primary metric; secondaries descriptive",
        "stopping_rule": "evaluate once at sample floor; no interim looks",
        "invalidating_conditions": ["forecast version changes",
                                    "coverage repair disabled"],
        "known_limitations": ["baseball dominates the population"],
        "safety_boundary": "measurement only; no execution or capital behavior",
    }
    m.update(over)
    return m


class TestValidation:
    def test_valid_manifest(self):
        v = er.validate_manifest(base_manifest())
        assert v.ok, v.errors
        assert v.digest and len(v.digest) == 64

    @pytest.mark.parametrize("field", [
        "primary_metric", "sample_floor", "stopping_rule", "declared_baselines",
        "hypothesis", "null_hypothesis", "population",
        "evaluation_horizons", "safety_boundary",
    ])
    def test_missing_required_field_rejected(self, field):
        m = base_manifest()
        m.pop(field)
        v = er.validate_manifest(m)
        assert not v.ok
        assert any(field in e for e in v.errors)

    def test_multiple_primary_metrics_rejected(self):
        v = er.validate_manifest(base_manifest(
            primary_metric=[{"name": "brier"}, {"name": "ece"}]))
        assert not v.ok
        assert any("multiple primaries" in e for e in v.errors)

    def test_confirmatory_multi_hypothesis_needs_a_multiple_testing_policy(self):
        v = er.validate_manifest(base_manifest(
            secondary_metrics=[{"name": "ece"}, {"name": "mce"}, {"name": "murphy"}],
            multiple_testing_policy="none"))
        assert not v.ok
        assert any("multiple_testing_policy" in e for e in v.errors)

    def test_exploratory_is_not_held_to_the_confirmatory_bar(self):
        v = er.validate_manifest(base_manifest(
            exploratory_or_confirmatory=er.EXPLORATORY,
            secondary_metrics=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
            multiple_testing_policy="none"))
        assert v.ok, v.errors

    def test_legacy_prose_rules_are_no_longer_accepted(self):
        """REGISTRY-002A demotes prose. Leaving the old keys in place would give
        an author two sources of truth, one of which is unenforceable."""
        for legacy in ("inclusion_rules", "exclusion_rules"):
            v = er.validate_manifest(base_manifest(**{legacy: ["anything"]}))
            assert not v.ok
            assert any("no longer executable authority" in e for e in v.errors)

    def test_outcome_derived_membership_is_now_inexpressible(self):
        """The review's bypass — "include forecasts in the cohort that beat the
        benchmark" — cannot be written at all: there is no such field."""
        for bad_field in ("outcome", "scored_current", "brier_score",
                          "beat_baseline", "rows_that_beat_the_benchmark"):
            v = er.validate_manifest(base_manifest(population={
                "schema_version": 1,
                "all": [{"field": bad_field, "operator": "eq", "value": "x"}],
                "none": [], "window_end": "unbounded"}))
            assert not v.ok, bad_field
            assert any("population:" in e for e in v.errors)

    def test_population_requires_the_registration_floor_structurally(self):
        """Omitting it must not disable prospectivity — it is injected."""
        from app.services.experiment_predicates import (
            canonicalize_population, ensure_registration_floor,
            has_registration_floor)

        v = er.validate_manifest(base_manifest(population={
            "schema_version": 1,
            "all": [{"field": "domain", "operator": "eq", "value": "x"}],
            "none": [], "window_end": "unbounded"}))
        assert v.ok, v.errors
        canon = ensure_registration_floor(canonicalize_population({
            "schema_version": 1,
            "all": [{"field": "domain", "operator": "eq", "value": "x"}],
            "none": [], "window_end": "unbounded"}))
        assert has_registration_floor(canon)

    def test_future_information_feature_rejected(self):
        v = er.validate_manifest(base_manifest(
            feature_definitions={"x": "settlement price at resolution"}))
        assert not v.ok
        assert any("available at forecast time" in e for e in v.errors)

    def test_boundary_fields_may_name_the_forbidden_capabilities(self):
        """`safety_boundary` must be able to say "no execution or capital
        behavior". A substring scan cannot tell an assertion from its negation,
        so a narrow, explicit exemption is correct — and the exemption list is
        itself asserted here so it cannot quietly grow."""
        assert er.VOCABULARY_EXEMPT_FIELDS == (
            "safety_boundary", "known_limitations", "invalidating_conditions",
            "provider_policy")
        v = er.validate_manifest(base_manifest(
            safety_boundary="measurement only; no execution, order or portfolio "
                            "behavior; calibration is not an edge claim"))
        assert v.ok, v.errors

    def test_exempt_fields_are_still_scanned_for_secrets(self):
        """Vocabulary-exempt does not mean secret-exempt."""
        v = er.validate_manifest(base_manifest(
            safety_boundary="measurement only; sk-" + "A" * 36))
        assert not v.ok
        assert any("credential" in e for e in v.errors)

    def test_trading_vocabulary_rejected(self):
        for text in ("this identifies a profitable edge",
                     "tradeable opportunity", "compute expected value"):
            v = er.validate_manifest(base_manifest(hypothesis=text))
            assert not v.ok, text
            assert any("trading vocabulary" in e for e in v.errors)

    def test_secret_bearing_field_rejected(self):
        v = er.validate_manifest(base_manifest(api_key="abcd1234"))
        assert not v.ok
        assert any("secret-bearing" in e for e in v.errors)

    def test_credential_shaped_value_rejected(self):
        v = er.validate_manifest(base_manifest(
            owner="sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
        assert not v.ok

    def test_author_supplied_start_time_rejected(self):
        """H1/H2: authors used to write a future timestamp, which rotted between
        authoring and registering — all three of this milestone's manifests went
        invalid within an hour. The registry stamps it now, so prospectivity is
        true by construction rather than by promise."""
        for value in (PAST, FUTURE):
            v = er.validate_manifest(base_manifest(start_time=value))
            assert not v.ok
            assert any("assigned by the registry" in e for e in v.errors)
        assert "start_time" in er.REGISTRY_ASSIGNED_FIELDS

    def test_registry_stamps_start_time_at_registration(self, tmp_path):
        before = datetime.now(timezone.utc)
        er.register(base_manifest(), base=tmp_path, confirm=True, commit="c1")
        d = er.experiment_dir("baseball-calibration-stability", tmp_path)
        stored = json.loads((d / er.MANIFEST_FILENAME).read_text())
        stamped = datetime.fromisoformat(stored["start_time"])
        assert stamped >= before
        assert stored["start_time"] == stored["registered_at"]

    def test_thin_sample_floor_is_flagged_not_rejected(self):
        v = er.validate_manifest(base_manifest(sample_floor=12,
                                               domain_sample_floors={"d": 12}))
        assert v.ok
        assert any("very thin" in w for w in v.warnings)

    def test_thin_domain_is_flagged_not_rejected(self):
        v = er.validate_manifest(base_manifest(domain="sports_tennis"))
        assert v.ok
        assert any("tennis" in w for w in v.warnings)

    def test_unknown_experiment_class_rejected(self):
        v = er.validate_manifest(base_manifest(experiment_class="paper_trading"))
        assert not v.ok

    def test_no_trading_experiment_class_exists(self):
        for banned in ("execution", "pnl", "order", "trading", "capital", "portfolio"):
            assert not any(banned in c for c in er.EXPERIMENT_CLASSES)


class TestDigest:
    def test_digest_is_deterministic_and_key_order_independent(self):
        m = base_manifest()
        shuffled = dict(reversed(list(m.items())))
        assert er.compute_digest(m) == er.compute_digest(shuffled)

    def test_digest_changes_when_a_hypothesis_field_changes(self):
        a = er.compute_digest(base_manifest())
        b = er.compute_digest(base_manifest(hypothesis="something else"))
        assert a != b

    def test_digest_excludes_registry_assigned_fields(self):
        """The digest a reviewer verifies before registration must equal the one
        stored after it, or showing it for confirmation is theatre."""
        m = base_manifest()
        before = er.compute_digest(m)
        m.update({"registered_at": "2026-08-05T00:00:00+00:00",
                  "registration_commit": "deadbeef", "state": er.REGISTERED,
                  "manifest_digest": before})
        assert er.compute_digest(m) == before


class TestRegistration:
    def test_dry_run_writes_nothing(self, tmp_path):
        out = er.register(base_manifest(), base=tmp_path, confirm=False)
        assert out["persisted"] is False
        assert out["mode"] == "dry_run"
        assert not (tmp_path / er.REGISTRY_DIRNAME).exists()

    def test_confirmed_registration(self, tmp_path):
        out = er.register(base_manifest(), base=tmp_path, confirm=True, commit="abc123")
        assert out["persisted"] is True
        d = er.experiment_dir("baseball-calibration-stability", tmp_path)
        stored = json.loads((d / er.MANIFEST_FILENAME).read_text())
        assert stored["state"] == er.REGISTERED
        assert stored["registration_commit"] == "abc123"
        assert stored["manifest_digest"] == out["manifest_digest"]
        assert er.current_state("baseball-calibration-stability", tmp_path) == \
            er.REGISTERED

    def test_duplicate_experiment_id_rejected(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit='c1')
        with pytest.raises(er.ManifestError, match="already registered"):
            er.register(base_manifest(), base=tmp_path, confirm=True, commit='c1')

    def test_invalid_manifest_never_registers(self, tmp_path):
        with pytest.raises(er.ManifestError):
            er.register(base_manifest(primary_metric=None), base=tmp_path,
                        confirm=True, commit='c1')
        assert not (tmp_path / er.REGISTRY_DIRNAME).exists()

    def test_path_traversal_rejected(self, tmp_path):
        for bad in ("../escape", "a/b", "/abs", "..", "x" * 200, "UPPER"):
            with pytest.raises(er.ManifestError):
                er.experiment_dir(bad, tmp_path)


class TestImmutability:
    def test_registered_manifest_edit_is_detected(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit='c1')
        eid = "baseball-calibration-stability"
        assert er.verify_immutability(eid, tmp_path)["intact"]
        d = er.experiment_dir(eid, tmp_path)
        m = json.loads((d / er.MANIFEST_FILENAME).read_text())
        m["hypothesis"] = "a more convenient hypothesis"      # post-result edit
        (d / er.MANIFEST_FILENAME).write_text(json.dumps(m, indent=2))
        assert er.verify_immutability(eid, tmp_path)["intact"] is False

    def test_a_tampered_experiment_cannot_advance(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit='c1')
        eid = "baseball-calibration-stability"
        d = er.experiment_dir(eid, tmp_path)
        m = json.loads((d / er.MANIFEST_FILENAME).read_text())
        m["sample_floor"] = 5
        (d / er.MANIFEST_FILENAME).write_text(json.dumps(m, indent=2))
        with pytest.raises(er.ManifestError, match="digest mismatch"):
            er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)

    def test_hypothesis_fields_are_declared(self):
        for f in ("primary_metric", "sample_floor", "population", "domain",
                  "forecast_version", "stopping_rule", "start_time"):
            assert f in er.HYPOTHESIS_FIELDS

    def test_a_draft_file_may_be_edited_freely(self, tmp_path):
        """Before registration there is nothing to protect."""
        p = tmp_path / "draft.json"
        p.write_text(json.dumps(base_manifest()))
        m = json.loads(p.read_text())
        m["hypothesis"] = "revised while still a draft"
        p.write_text(json.dumps(m))
        assert er.validate_manifest(json.loads(p.read_text())).ok


class TestStateMachine:
    def _reg(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit='c1')
        return "baseball-calibration-stability"

    def test_append_only_progression(self, tmp_path):
        eid = self._reg(tmp_path)
        for to in (er.COLLECTING, er.MATURED, er.EVALUATED, er.RETIRED):
            er.transition(eid, to, base=tmp_path, confirm=True)
            assert er.current_state(eid, tmp_path) == to

    def test_registered_can_never_return_to_draft(self, tmp_path):
        eid = self._reg(tmp_path)
        with pytest.raises(er.ManifestError, match="not permitted"):
            er.transition(eid, er.DRAFT, base=tmp_path, confirm=True)
        assert er.DRAFT not in {t for ts in er.TRANSITIONS.values() for t in ts}

    def test_invalid_transition_rejected(self, tmp_path):
        eid = self._reg(tmp_path)
        with pytest.raises(er.ManifestError, match="not permitted"):
            er.transition(eid, er.EVALUATED, base=tmp_path, confirm=True)

    def test_transition_dry_run_writes_nothing(self, tmp_path):
        eid = self._reg(tmp_path)
        before = len(er.read_events(eid, tmp_path))
        out = er.transition(eid, er.COLLECTING, base=tmp_path, confirm=False)
        assert out["persisted"] is False
        assert len(er.read_events(eid, tmp_path)) == before

    def test_state_is_reconstructed_from_events_not_the_manifest(self, tmp_path):
        eid = self._reg(tmp_path)
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        d = er.experiment_dir(eid, tmp_path)
        m = json.loads((d / er.MANIFEST_FILENAME).read_text())
        assert m["state"] == er.REGISTERED          # convenience field, stale
        assert er.current_state(eid, tmp_path) == er.COLLECTING

    def test_result_cannot_precede_maturity(self, tmp_path):
        eid = self._reg(tmp_path)
        assert er.status(eid, tmp_path)["evaluation_permitted"] is False
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        assert er.status(eid, tmp_path)["evaluation_permitted"] is False
        er.transition(eid, er.MATURED, base=tmp_path, confirm=True)
        assert er.status(eid, tmp_path)["evaluation_permitted"] is True

    def test_failed_experiment_is_preserved_not_deleted(self, tmp_path):
        eid = self._reg(tmp_path)
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        er.transition(eid, er.INVALIDATED, base=tmp_path, confirm=True,
                      note="protocol deviation")
        er.transition(eid, er.RETIRED, base=tmp_path, confirm=True)
        events = er.read_events(eid, tmp_path)
        assert any(e.get("to") == er.INVALIDATED for e in events)
        assert er.experiment_dir(eid, tmp_path).exists()
        assert er.current_state(eid, tmp_path) == er.RETIRED

    def test_verdict_vocabulary_has_no_trading_terms(self):
        for v in er.VERDICTS:
            for banned in ("profit", "trade", "buy", "sell", "edge", "opportunity"):
                assert banned not in v


class TestCli:
    def test_validate_and_register_roundtrip(self, tmp_path, capsys):
        from app import cli

        mf = tmp_path / "m.json"
        mf.write_text(json.dumps(base_manifest()))
        assert cli.experiment_registry_validate(str(mf)) == 0
        assert "VALID" in capsys.readouterr().out

        assert cli.experiment_registry_register(str(mf), confirm=False,
                                                base=str(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "DRY_RUN" in out and "nothing written" in out
        assert not (tmp_path / er.REGISTRY_DIRNAME).exists()

        assert cli.experiment_registry_register(str(mf), confirm=True,
                                                base=str(tmp_path)) == 0
        assert "persisted         true" in capsys.readouterr().out

        assert cli.experiment_registry_list(base=str(tmp_path)) == 0
        assert "baseball-calibration-stability" in capsys.readouterr().out

    def test_validate_rejects_and_exits_nonzero(self, tmp_path, capsys):
        from app import cli

        mf = tmp_path / "bad.json"
        mf.write_text(json.dumps(base_manifest(sample_floor=None)))
        assert cli.experiment_registry_validate(str(mf)) == 1
        assert "REJECTED" in capsys.readouterr().out

    def test_text_json_parity(self, tmp_path, capsys):
        from app import cli

        mf = tmp_path / "m.json"
        mf.write_text(json.dumps(base_manifest()))
        cli.experiment_registry_register(str(mf), confirm=True, base=str(tmp_path))
        capsys.readouterr()
        cli.experiment_registry_status("baseball-calibration-stability",
                                       base=str(tmp_path), fmt="json")
        payload = json.loads(capsys.readouterr().out)
        cli.experiment_registry_status("baseball-calibration-stability",
                                       base=str(tmp_path), fmt="text")
        text = capsys.readouterr().out
        assert payload["state"] in text
        assert str(payload["sample_floor"]) in text
        assert payload["primary_metric"] in text

    def test_dry_run_beats_confirm_in_dispatch(self):
        src = Path("app/cli.py").read_text()
        assert src.count("confirm=args.confirm and not args.dry_run") >= 3


class TestSafetySurface:
    FILE = "app/services/experiment_registry.py"

    def test_no_trading_or_capital_identifiers(self):
        banned = ("expected_value", "kelly", "position_size", "paper_trade",
                  "place_order", "wallet", "private_key", "execute_trade",
                  "sign_transaction", "portfolio", "pnl")
        tree = ast.parse(Path(self.FILE).read_text())
        idents = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                idents.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                idents.add(node.attr.lower())
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                idents.add(node.name.lower())
            elif isinstance(node, ast.arg):
                idents.add(node.arg.lower())
        for i in idents:
            for b in banned:
                assert b not in i, f"{self.FILE} defines {i!r}"

    def test_no_provider_or_network_imports(self):
        tree = ast.parse(Path(self.FILE).read_text())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        for banned in ("httpx", "requests", "urllib", "socket", "aiohttp"):
            assert banned not in mods

    def test_no_database_or_marketops_coupling(self):
        src = Path(self.FILE).read_text()
        for banned in ("sqlalchemy", "get_sessionmaker", "MarketOps", "marketops",
                       "Session", "run_migrations"):
            assert banned not in src, f"registry must not couple to {banned}"

    def test_no_migration_added(self):
        versions = sorted(p.name for p in Path("alembic/versions").glob("0*.py"))
        assert versions[-1].startswith("0027")

    def test_no_timer_or_daemon_added(self):
        src = Path(self.FILE).read_text()
        for banned in ("systemd", "crontab", "Timer", "daemon", "schedule"):
            assert banned not in src


class TestImmutableReferences:
    """B3: the manifest text alone cannot reconstruct an experiment. The same
    words evaluated by different scoring code produce different numbers."""

    def test_references_are_captured_at_registration(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit="cafe123")
        d = er.experiment_dir("baseball-calibration-stability", tmp_path)
        refs = json.loads((d / er.MANIFEST_FILENAME).read_text())["immutable_references"]
        for key in ("population_definition_digest", "population_predicate_digest",
                    "population_references", "feature_configuration_digest",
                    "signal_configuration_digest", "baseline_definition_digest",
                    "primary_metric_digest", "forecast_family", "forecast_version",
                    "repository_commit", "evaluation_code_digests", "canon_digests"):
            assert key in refs, key
        assert refs["repository_commit"] == "cafe123"
        assert refs["forecast_version"] == "v1"
        assert refs["evaluation_code_digests"]["app/services/calibration.py"]

    def test_definition_digests_are_content_sensitive(self):
        a = er.capture_immutable_references(base_manifest())
        wider = base_manifest()
        wider["population"] = dict(wider["population"])
        wider["population"]["all"] = list(wider["population"]["all"]) + [
            {"field": "forecast_risk", "operator": "eq", "value": "low"}]
        b = er.capture_immutable_references(wider)
        assert a["population_predicate_digest"] != b["population_predicate_digest"]
        assert a["feature_configuration_digest"] == b["feature_configuration_digest"]

    def test_references_do_not_affect_the_manifest_digest(self, tmp_path):
        """The digest a reviewer verifies before registration must survive the
        registry adding its own bookkeeping afterwards."""
        m = base_manifest()
        before = er.compute_digest(m)
        er.register(m, base=tmp_path, confirm=True, commit='c1')
        d = er.experiment_dir("baseball-calibration-stability", tmp_path)
        stored = json.loads((d / er.MANIFEST_FILENAME).read_text())
        assert stored["manifest_digest"] == before
        assert er.verify_immutability("baseball-calibration-stability",
                                      tmp_path)["intact"]

    def test_evaluation_code_drift_is_detected_and_is_not_an_error(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit='c1')
        eid = "baseball-calibration-stability"
        assert er.status(eid, tmp_path)["evaluation_code_drift"]["drifted"] is False
        d = er.experiment_dir(eid, tmp_path)
        m = json.loads((d / er.MANIFEST_FILENAME).read_text())
        m["immutable_references"]["evaluation_code_digests"][
            "app/services/calibration.py"] = "0" * 64
        (d / er.MANIFEST_FILENAME).write_text(json.dumps(m, indent=2, sort_keys=True))
        st = er.status(eid, tmp_path)
        assert st["evaluation_code_drift"]["drifted"] is True
        assert "app/services/calibration.py" in \
            st["evaluation_code_drift"]["changed_files"]
        # drift is disclosure, not tampering: the hypothesis digest still holds
        assert st["integrity"]["intact"] is True

    def test_no_production_data_is_snapshotted(self):
        src = Path("app/services/experiment_registry.py").read_text()
        for banned in ("SELECT", "market_forecasts", "forecast_scores",
                       "market_outcomes", ".db"):
            assert banned not in src


class TestSecretDetectionPrecision:
    """A check that fires on ordinary correct input does not get tightened —
    it gets deleted. The first draft rejected the experiment IDs themselves."""

    @pytest.mark.parametrize("benign", [
        "baseball-prospective-calibration-stability",
        "ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR is disabled during collection",
        "forecast-reliability-decomposition-report baselines block",
        "Brier of a constant forecast equal to the observed prevalence",
        "FORECAST-RELIABILITY-DIRECTIONAL-FIX-001 made the residual explicit",
    ])
    def test_no_false_positive_on_project_vocabulary(self, benign):
        assert not er.SECRET_VALUE_PATTERN.search(benign), benign

    @pytest.mark.parametrize("secret", [
        "sk-" + "A" * 36,
        "Bearer abcdef0123456789abcdef",
        "ghp_" + "A" * 30,
        "AKIAIOSFODNN7EXAMPLE",
        "aB3dEfGh1JkLmN0pQrStUvWxYz012345678",
    ])
    def test_real_credential_shapes_are_caught(self, secret):
        assert er.SECRET_VALUE_PATTERN.search(secret), secret

    def test_field_name_detection_is_the_primary_guard(self):
        """Even a short, innocuous-looking value is rejected under a secret
        field name — shape detection is the backstop, not the frontline."""
        v = er.validate_manifest(base_manifest(auth_token="x"))
        assert not v.ok
        assert any("secret-bearing" in e for e in v.errors)


class TestReviewFindings:
    """Findings from the independent governance review. The registry's three
    loudest claims — immutability, leakage prevention, reproducible pinning —
    were docstrings rather than properties of the code."""

    def test_every_shipped_manifest_validates(self):
        """The single highest-value missing test. All three deliverable
        manifests were invalid at review time because their hand-written
        start_time had already passed, and nothing in the suite noticed."""
        files = sorted(Path("manifests").glob("*.json"))
        assert files, "no manifests to validate"
        for f in files:
            v = er.validate_manifest(json.loads(f.read_text()))
            assert v.ok, f"{f.name}: {v.errors}"

    def test_coordinated_manifest_and_event_rewrite_is_detected(self, tmp_path):
        """H3, and be precise about the limit. Recomputing the digest and
        rewriting BOTH files used to pass unconditionally. The hash chain now
        catches it as soon as the log has more than one entry, because rewriting
        the registration event invalidates every `prev` after it.

        It does NOT catch a rewrite of a single-event log — hashing alone cannot,
        since the forger controls every input. That case is caught only by the
        git diff, which is the honest statement of what this architecture buys.
        """
        er.register(base_manifest(), base=tmp_path, confirm=True, commit="c1")
        eid = "baseball-calibration-stability"
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        d = er.experiment_dir(eid, tmp_path)
        m = json.loads((d / er.MANIFEST_FILENAME).read_text())
        m["sample_floor"] = 25
        m["manifest_digest"] = er.compute_digest(m)
        (d / er.MANIFEST_FILENAME).write_text(json.dumps(m, indent=2, sort_keys=True))
        events = er.read_events(eid, tmp_path)
        events[0]["manifest_digest"] = m["manifest_digest"]
        (d / er.EVENTS_FILENAME).write_text(
            "\n".join(er.canonical_json(e) for e in events) + "\n")
        assert er.verify_immutability(eid, tmp_path)["intact"] is False

    def test_event_log_truncation_is_detected(self, tmp_path):
        """H3. Dropping the last line rolled `invalidated` back to `matured`
        and let the experiment advance again."""
        er.register(base_manifest(), base=tmp_path, confirm=True, commit="c1")
        eid = "baseball-calibration-stability"
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        er.transition(eid, er.MATURED, base=tmp_path, confirm=True)
        d = er.experiment_dir(eid, tmp_path)
        lines = (d / er.EVENTS_FILENAME).read_text().splitlines()
        (d / er.EVENTS_FILENAME).write_text("\n".join(lines[:-1]) + "\n")
        # the chain still verifies (a prefix is a valid chain) but re-appending
        # after truncation breaks it, which is what a forger must do
        er.append_event(eid, {"event": "transition", "from": er.COLLECTING,
                              "to": er.MATURED, "at": "x"}, tmp_path)
        lines2 = (d / er.EVENTS_FILENAME).read_text().splitlines()
        forged = [json.loads(x) for x in lines2]
        forged.insert(1, {"event": "transition", "to": er.EVALUATED, "seq": 99,
                          "prev": None})
        (d / er.EVENTS_FILENAME).write_text(
            "\n".join(er.canonical_json(e) for e in forged) + "\n")
        assert er.verify_event_chain(eid, tmp_path)["intact"] is False
        assert er.verify_immutability(eid, tmp_path)["intact"] is False

    def test_events_are_hash_chained(self, tmp_path):
        er.register(base_manifest(), base=tmp_path, confirm=True, commit="c1")
        eid = "baseball-calibration-stability"
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        events = er.read_events(eid, tmp_path)
        assert events[0]["prev"] is None and events[0]["seq"] == 0
        assert events[1]["seq"] == 1 and events[1]["prev"] is not None
        assert er.verify_event_chain(eid, tmp_path)["intact"] is True

    def test_registration_without_a_commit_is_refused(self, tmp_path):
        """H4. `repository_commit: null` used to be a valid registration."""
        with pytest.raises(er.ManifestError, match="repository commit"):
            er.register(base_manifest(), base=tmp_path, confirm=True, commit=None)

    def test_pinning_failure_refuses_registration(self, tmp_path):
        """H4. Missing evaluation code used to record null digests, after which
        drift detection reported a permanent clean bill of health."""
        with pytest.raises(er.ManifestError, match="cannot pin evaluation code"):
            er.capture_immutable_references(base_manifest(), repo_root=tmp_path)

    def test_repo_root_is_derived_from_the_module_not_cwd(self):
        root = er.repo_root_default()
        assert (root / "app" / "services" / "experiment_registry.py").exists()
        for f in er.EVALUATION_CODE_FILES + er.CANON_FILES:
            assert (root / f).exists(), f

    def test_ordinary_research_prose_is_not_a_trading_claim(self):
        """M2. Substring matching rejected all of these."""
        for text in ("we collect 200 members in order to reach the sample floor",
                     "acknowledged limitation of the design",
                     "alphabetical listing of domains",
                     "the recorded ordering of events is preserved",
                     "a non-profit reference dataset"):
            v = er.validate_manifest(base_manifest(research_question=text))
            assert v.ok, f"{text!r}: {v.errors}"

    def test_paraphrased_trading_claims_are_still_caught(self):
        for text in ("identifies mispricing we can act on",
                     "quantifies exploitable divergence for capital deployment",
                     "this is monetisable signal"):
            v = er.validate_manifest(base_manifest(research_question=text))
            assert not v.ok, text

    def test_directional_shares_sum_to_exactly_one_after_rounding(self):
        """L1. Three independent 4dp roundings of 1/3 gave 0.9999."""
        from app.services.forecast_reliability import compute_bins, directional_summary

        spec = [(0.9, 0.0, 40), (0.55, 1.0, 40), (0.5, 1.0, 20), (0.5, 0.0, 20)]
        points = []
        from tests.test_forecast_reliability_directional_fix_001 import pts
        d = directional_summary(compute_bins(pts(spec), [i / 10 for i in range(11)]),
                                pts(spec))
        total = (d["overprediction_weighted_share"]
                 + d["underprediction_weighted_share"]
                 + d["approximately_calibrated_weighted_share"]
                 + d["unclassified_weighted_share"])
        assert total == 1.0, total

    def test_cli_survives_a_corrupt_registry(self, tmp_path, capsys):
        """M7. Corrupt files are exactly when a clean diagnostic matters."""
        from app import cli

        d = er.registry_root(tmp_path) / "broken-experiment"
        d.mkdir(parents=True)
        (d / er.MANIFEST_FILENAME).write_text("{not json")
        assert cli.experiment_registry_list(base=str(tmp_path)) == 2
        assert "unreadable" in capsys.readouterr().out
        assert cli.experiment_registry_status("broken-experiment",
                                              base=str(tmp_path)) == 2
