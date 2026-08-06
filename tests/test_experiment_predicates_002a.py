"""PROSPECTIVE-EXPERIMENT-REGISTRY-002A — typed population predicates.

REGISTRY-001 guarded membership with a prose blocklist and a review broke it in
one line. These tests assert the replacement is structural: a forbidden
predicate must be *inexpressible*, not merely differently spelled.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db import Base
from app.models import (
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketResearchPacket,
)
from app.services import experiment_population as ep
from app.services import experiment_predicates as pr

REG = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def pop(*alls, none=(), version=1, window_end="unbounded"):
    return {"schema_version": version, "all": list(alls), "none": list(none),
            "window_end": window_end}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def mk_forecast(db, ticker, *, created, domain="sports_baseball",
                family="baseball_evidence", ver="v1", packet=True):
    pid = None
    if packet:
        p = MarketResearchPacket(
            market_ticker=ticker, collector_name="c", domain=domain,
            research_completeness_score=0.9, research_risk="low", created_at=created)
        db.add(p)
        db.flush()
        pid = p.id
    f = MarketForecastRecord(
        market_ticker=ticker, estimated_probability=0.6, forecaster_name=family,
        forecaster_version=ver, confidence=0.8, evidence_depth="shallow",
        forecast_risk="low", research_packet_id=pid, created_at=created)
    db.add(f)
    db.commit()
    return f


class TestSchema:
    def test_valid_document(self):
        c = pr.canonicalize_population(
            pop({"field": "domain", "operator": "eq", "value": "sports_baseball"}))
        assert c["schema_version"] == 1
        assert c["all"][0]["field"] == "domain"

    def test_missing_schema_version_rejected(self):
        with pytest.raises(pr.PredicateError, match="schema_version is required"):
            pr.canonicalize_population({"all": [], "none": [],
                                        "window_end": "unbounded"})

    def test_unknown_schema_version_rejected(self):
        with pytest.raises(pr.PredicateError, match="not supported"):
            pr.canonicalize_population(pop(version=99))

    def test_unknown_population_keys_rejected(self):
        with pytest.raises(pr.PredicateError, match="unsupported population keys"):
            pr.canonicalize_population({"schema_version": 1, "all": [],
                                        "window_end": "unbounded", "sql": "x"})

    def test_unknown_predicate_keys_rejected(self):
        with pytest.raises(pr.PredicateError, match="unsupported keys"):
            pr.canonicalize_population(pop(
                {"field": "domain", "operator": "eq", "value": "x", "raw": "1=1"}))


class TestForbiddenFields:
    @pytest.mark.parametrize("bad", [
        "outcome", "outcome_status", "winner", "winning_side", "resolved_probability",
        "settlement_price", "score", "score_status", "scored_current",
        "was_resolved", "brier_score", "log_loss", "calibration_result",
        "beat_baseline", "closing_price", "post_forecast_performance",
        "future_market_status", "market_status", "profit", "return", "pnl",
    ])
    def test_forbidden_field_rejected_by_identity(self, bad):
        with pytest.raises(pr.PredicateError) as exc:
            pr.canonicalize_population(pop(
                {"field": bad, "operator": "eq", "value": "x"}))
        assert "forbidden" in str(exc.value) or "not in the field registry" in str(exc.value)

    def test_paraphrase_cannot_bypass(self):
        """The whole point. Prose paraphrase is irrelevant because prose is not
        the authority — an unregistered field name is simply unknown."""
        for name in ("rows_that_beat_the_benchmark", "cohort_that_won",
                     "realised_frequency", "final_state", "settled_yes"):
            with pytest.raises(pr.PredicateError, match="not in the field registry"):
                pr.canonicalize_population(pop(
                    {"field": name, "operator": "eq", "value": True}))

    def test_no_registry_field_is_post_forecast(self):
        for spec in pr.FIELD_REGISTRY.values():
            assert spec.available_at == "forecast_creation", spec.name
            assert spec.safe_at_forecast_creation is True, spec.name
            assert spec.immutable_after_forecast is True, spec.name


class TestOperatorsAndTypes:
    def test_unknown_operator_rejected(self):
        with pytest.raises(pr.PredicateError, match="closed operator set"):
            pr.canonicalize_population(pop(
                {"field": "domain", "operator": "regex", "value": ".*"}))

    def test_operator_not_supported_for_field(self):
        with pytest.raises(pr.PredicateError, match="not supported for"):
            pr.canonicalize_population(pop(
                {"field": "domain", "operator": "lt", "value": "x"}))

    def test_type_mismatch_rejected(self):
        with pytest.raises(pr.PredicateError, match="expects a number"):
            pr.canonicalize_population(pop(
                {"field": "research_completeness", "operator": "gte", "value": "hi"}))
        with pytest.raises(pr.PredicateError, match="expects a string"):
            pr.canonicalize_population(pop(
                {"field": "domain", "operator": "eq", "value": 5}))

    @pytest.mark.parametrize("payload", [
        "'; DROP TABLE market_forecasts; --",
        "1=1 OR TRUE",
        "__import__('os').system('ls')",
        "lambda x: True",
        "^(a|b)+$",
        "$(rm -rf /)",
    ])
    def test_injection_payloads_are_inert_values_not_code(self, payload):
        """They are refused for length/type where applicable, and where a short
        one is accepted it is stored as an opaque string compared with `==`.
        Nothing in this module interprets a value."""
        doc = pop({"field": "market_ticker", "operator": "eq", "value": payload})
        if len(payload) <= pr.MAX_VALUE_CHARS:
            c = pr.canonicalize_population(doc)
            assert c["all"][0]["value"] == payload.strip()
        src = Path("app/services/experiment_predicates.py").read_text()
        for banned in ("eval(", "exec(", "__import__", "subprocess", "os.system"):
            assert banned not in src

    def test_oversized_value_rejected(self):
        with pytest.raises(pr.PredicateError, match="exceeds"):
            pr.canonicalize_population(pop(
                {"field": "market_ticker", "operator": "eq", "value": "x" * 500}))

    def test_valueless_operator_rejects_a_value(self):
        with pytest.raises(pr.PredicateError, match="takes no value"):
            pr.canonicalize_population(pop(
                {"field": "forecast_created_at",
                 "operator": "gte_registration_time", "value": "2026-01-01"}))

    def test_naive_timestamp_rejected(self):
        with pytest.raises(pr.PredicateError, match="timezone-naive"):
            pr.canonicalize_population(pop(
                {"field": "forecast_created_at", "operator": "gte",
                 "value": "2026-08-06T00:00:00"}))

    def test_timestamps_canonicalize_to_utc(self):
        a = pr.canonicalize_population(pop(
            {"field": "forecast_created_at", "operator": "gte",
             "value": "2026-08-06T12:00:00+00:00"}))
        b = pr.canonicalize_population(pop(
            {"field": "forecast_created_at", "operator": "gte",
             "value": "2026-08-06T14:00:00+02:00"}))
        assert pr.population_digest(a) == pr.population_digest(b)


class TestCanonicalization:
    def test_order_insensitive(self):
        p1 = {"field": "domain", "operator": "eq", "value": "sports_soccer"}
        p2 = {"field": "forecaster", "operator": "eq", "value": "soccer_evidence"}
        assert pr.population_digest(pr.canonicalize_population(pop(p1, p2))) == \
            pr.population_digest(pr.canonicalize_population(pop(p2, p1)))

    def test_duplicate_predicates_collapse(self):
        p = {"field": "domain", "operator": "eq", "value": "sports_tennis"}
        c = pr.canonicalize_population(pop(p, dict(p)))
        assert len(c["all"]) == 1

    def test_set_members_are_sorted_and_deduplicated(self):
        a = pr.canonicalize_population(pop(
            {"field": "domain", "operator": "in", "value": ["b", "a", "b"]}))
        b = pr.canonicalize_population(pop(
            {"field": "domain", "operator": "in", "value": ["a", "b"]}))
        assert pr.population_digest(a) == pr.population_digest(b)

    def test_numeric_normalization(self):
        a = pr.canonicalize_population(pop(
            {"field": "research_completeness", "operator": "gte", "value": 1}))
        b = pr.canonicalize_population(pop(
            {"field": "research_completeness", "operator": "gte", "value": 1.0}))
        assert pr.population_digest(a) == pr.population_digest(b)

    def test_rationale_does_not_change_the_digest(self):
        base = pop({"field": "domain", "operator": "eq", "value": "x"})
        with_r = dict(base, rationale=["prose that must not be authority"])
        assert pr.population_digest(pr.canonicalize_population(base)) == \
            pr.population_digest(pr.canonicalize_population(with_r))

    @pytest.mark.parametrize("doc", [
        pop({"field": "domain", "operator": "eq", "value": "a"},
            {"field": "domain", "operator": "not_eq", "value": "a"}),
        pop({"field": "domain", "operator": "exists"},
            {"field": "domain", "operator": "not_exists"}),
        pop({"field": "domain", "operator": "eq", "value": "a"},
            {"field": "domain", "operator": "eq", "value": "b"}),
    ])
    def test_decidable_contradictions_rejected(self, doc):
        with pytest.raises(pr.PredicateError, match="contradictory"):
            pr.canonicalize_population(doc)


class TestRegistrationFloor:
    def test_floor_is_injected_when_omitted(self):
        c = pr.ensure_registration_floor(pr.canonicalize_population(
            pop({"field": "domain", "operator": "eq", "value": "x"})))
        assert pr.has_registration_floor(c)

    def test_declaring_the_floor_is_identical_to_omitting_it(self):
        """Omission must not be a way to disable prospectivity, and declaring it
        must not produce a different experiment."""
        omitted = pr.ensure_registration_floor(pr.canonicalize_population(
            pop({"field": "domain", "operator": "eq", "value": "x"})))
        declared = pr.ensure_registration_floor(pr.canonicalize_population(
            pop({"field": "domain", "operator": "eq", "value": "x"},
                {"field": "forecast_created_at",
                 "operator": "gte_registration_time"})))
        assert pr.population_digest(omitted) == pr.population_digest(declared)


class TestPopulationReconstruction:
    def _canon(self, domain="sports_baseball"):
        return pop({"field": "domain", "operator": "eq", "value": domain})

    def test_pre_registration_forecasts_excluded(self, db):
        mk_forecast(db, "BEFORE", created=REG - timedelta(hours=1))
        mk_forecast(db, "AFTER", created=REG + timedelta(hours=1))
        r = ep.reconstruct_population(db, experiment_id="e", population=self._canon(),
                                      registered_at=REG)
        assert r.eligible_count == 1
        assert r.pre_registration_excluded == 1

    def test_boundary_forecast_at_registration_is_included(self, db):
        mk_forecast(db, "EXACT", created=REG)
        r = ep.reconstruct_population(db, experiment_id="e", population=self._canon(),
                                      registered_at=REG)
        assert r.eligible_count == 1, "gte means the boundary instant is a member"

    def test_post_end_excluded_using_the_pinned_window(self, db):
        """The ceiling comes from the REGISTERED document, not a caller.

        It used to be a `declared_end` argument absent from the digest, so
        whoever ran the evaluation picked the cohort's end date — after seeing
        results — with nothing registered to contradict them. That is optional
        stopping, and the exact mirror of the start_time bug at the other end.
        """
        mk_forecast(db, "IN", created=REG + timedelta(hours=1))
        mk_forecast(db, "LATE", created=REG + timedelta(days=10))
        pinned = pop({"field": "domain", "operator": "eq",
                      "value": "sports_baseball"},
                     window_end=(REG + timedelta(days=5)).isoformat())
        r = ep.reconstruct_population(db, experiment_id="e", population=pinned,
                                      registered_at=REG)
        assert r.eligible_count == 1
        assert r.post_end_excluded == 1
        assert r.declared_end is not None

    def test_reconstruction_takes_no_caller_supplied_end(self):
        import inspect

        params = inspect.signature(ep.reconstruct_population).parameters
        assert "declared_end" not in params, (
            "an evaluator must not be able to choose the window's end")

    def test_membership_is_deterministic_and_digested(self, db):
        for i in range(4):
            mk_forecast(db, f"D-{i}", created=REG + timedelta(hours=i + 1))
        a = ep.reconstruct_population(db, experiment_id="e",
                                      population=self._canon(), registered_at=REG)
        b = ep.reconstruct_population(db, experiment_id="e",
                                      population=self._canon(), registered_at=REG)
        assert a.membership_digest == b.membership_digest
        assert a.eligible_count == b.eligible_count == 4

    def test_membership_ignores_outcomes_and_scores(self, db):
        """The load-bearing property: adding outcomes must not move membership."""
        for i in range(3):
            mk_forecast(db, f"S-{i}", created=REG + timedelta(hours=i + 1))
        before = ep.reconstruct_population(db, experiment_id="e",
                                           population=self._canon(),
                                           registered_at=REG)
        db.add(MarketOutcomeRecord(market_ticker="S-0", outcome_status="settled",
                                   winning_side="yes", resolved_probability=1.0,
                                   source="kalshi_rest"))
        db.commit()
        after = ep.reconstruct_population(db, experiment_id="e",
                                          population=self._canon(),
                                          registered_at=REG)
        assert before.membership_digest == after.membership_digest

    def test_reconstruction_writes_nothing_and_calls_nothing(self, db):
        mk_forecast(db, "W", created=REG + timedelta(hours=1))
        n_before = len(db.execute(
            __import__("sqlalchemy").select(MarketForecastRecord)).scalars().all())
        r = ep.reconstruct_population(db, experiment_id="e",
                                      population=self._canon(), registered_at=REG)
        n_after = len(db.execute(
            __import__("sqlalchemy").select(MarketForecastRecord)).scalars().all())
        assert n_before == n_after
        assert r.external_calls == 0 and r.persisted is False

    def test_nullable_field_does_not_widen_the_population(self, db):
        mk_forecast(db, "NOPKT", created=REG + timedelta(hours=1), packet=False)
        r = ep.reconstruct_population(db, experiment_id="e",
                                      population=self._canon(), registered_at=REG)
        assert r.eligible_count == 0
        assert r.unexpected_missing_fields.get("domain") == 1

    def test_forecaster_version_pin(self, db):
        mk_forecast(db, "V1", created=REG + timedelta(hours=1), ver="v1")
        mk_forecast(db, "V2", created=REG + timedelta(hours=2), ver="v2")
        r = ep.reconstruct_population(
            db, experiment_id="e",
            population=pop({"field": "forecaster_version", "operator": "eq",
                            "value": "v1"}), registered_at=REG)
        assert r.eligible_count == 1


class TestDrift:
    def test_missing_references_are_unknown_not_clean(self):
        for recorded in (None, {}, {"predicate_schema_version": 1,
                                    "field_registry_digest":
                                        pr.field_registry_digest()}):
            d = ep.classify_population_drift(recorded)
            assert d["classification"] in (ep.DRIFT_UNKNOWN,)
            assert d["material"] is True

    def test_clean_when_references_match(self):
        d = ep.classify_population_drift(ep.population_reference_snapshot())
        assert d["classification"] == ep.DRIFT_NONE
        assert d["material"] is False

    def test_predicate_schema_drift_detected(self):
        snap = dict(ep.population_reference_snapshot())
        snap["predicate_schema_version"] = 99
        d = ep.classify_population_drift(snap)
        assert d["classification"] == ep.DRIFT_PREDICATE_SCHEMA
        assert d["material"] is True

    def test_field_registry_drift_detected(self):
        snap = dict(ep.population_reference_snapshot())
        snap["field_registry_digest"] = "0" * 64
        d = ep.classify_population_drift(snap)
        assert d["classification"] == ep.DRIFT_FIELD_REGISTRY

    def test_population_logic_drift_detected(self):
        snap = dict(ep.population_reference_snapshot())
        snap["population_logic_digests"] = dict(snap["population_logic_digests"])
        snap["population_logic_digests"][
            "app/services/experiment_predicates.py"] = "0" * 64
        d = ep.classify_population_drift(snap)
        assert d["classification"] == ep.DRIFT_POPULATION_LOGIC
        assert d["material"] is True


class TestDraftManifests:
    @pytest.mark.parametrize("name", [
        "baseball-prospective-calibration-stability",
        "soccer-prospective-reliability",
        "tennis-base-rate-falsification",
    ])
    def test_draft_validates_and_uses_typed_predicates(self, name):
        from app.services.experiment_registry import validate_manifest

        m = json.loads(Path(f"manifests/{name}.json").read_text())
        v = validate_manifest(m)
        assert v.ok, v.errors
        assert "inclusion_rules" not in m and "exclusion_rules" not in m
        canon = pr.ensure_registration_floor(
            pr.canonicalize_population(m["population"]))
        assert pr.has_registration_floor(canon)

    def test_tennis_membership_is_independent_of_scoring(self):
        """The specific repair. The draft previously excluded rows that were
        'not scored_current at evaluation', which made the cohort a function of
        the outcome pipeline's progress."""
        m = json.loads(
            Path("manifests/tennis-base-rate-falsification.json").read_text())
        text = json.dumps(m["population"]).lower()
        for banned in ("scored_current", "score", "outcome", "brier"):
            assert banned not in json.dumps(
                {k: v for k, v in m["population"].items()
                 if k != "rationale"}).lower(), banned
        fields = {p["field"] for p in m["population"]["all"]
                  + m["population"]["none"]}
        for f in fields:
            assert pr.FIELD_REGISTRY[f].available_at == "forecast_creation"
        # the repair is explained in rationale, which is prose and inert
        assert "scored_current" in text

    def test_no_draft_declares_a_registration_timestamp(self):
        for f in Path("manifests").glob("*.json"):
            m = json.loads(f.read_text())
            assert m.get("start_time") is None, f.name
            assert m.get("registered_at") is None, f.name


class TestSafetySurface:
    FILES = ("app/services/experiment_predicates.py",
             "app/services/experiment_population.py")

    def test_no_eval_or_dynamic_execution(self):
        for rel in self.FILES:
            tree = ast.parse(Path(rel).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in ("eval", "exec", "compile",
                                                "__import__"), rel

    def test_no_provider_or_network_imports(self):
        for rel in self.FILES:
            tree = ast.parse(Path(rel).read_text())
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module.split(".")[0])
            for banned in ("httpx", "requests", "urllib", "socket", "aiohttp",
                           "subprocess"):
                assert banned not in mods, f"{rel} imports {banned}"

    def test_no_trading_or_capital_identifiers(self):
        banned = ("expected_value", "kelly", "position_size", "place_order",
                  "wallet", "private_key", "execute_trade", "portfolio", "pnl",
                  "paper_trade")
        for rel in self.FILES:
            tree = ast.parse(Path(rel).read_text())
            idents = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    idents.add(node.id.lower())
                elif isinstance(node, ast.Attribute):
                    idents.add(node.attr.lower())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    idents.add(node.name.lower())
            for i in idents:
                for b in banned:
                    assert b not in i, f"{rel}: {i}"

    def test_population_never_reads_outcome_or_score_models(self):
        """Structural, not textual: the module's own docstring explains why it
        avoids `scored_current`, so a substring scan fails on correct code —
        the same trap that has already bitten two checks in this series."""
        tree = ast.parse(Path("app/services/experiment_population.py").read_text())
        imported, attrs, names = set(), set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Attribute):
                attrs.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        for banned in ("MarketOutcomeRecord", "ForecastScoreRecord"):
            assert banned not in imported, f"imports {banned}"
            assert banned not in names, f"references {banned}"
        for banned in ("scored_current", "brier_score", "winning_side",
                       "outcome_status", "resolved_probability", "score_status"):
            assert banned not in attrs, f"reads attribute {banned}"

    def test_no_migration_added(self):
        versions = sorted(p.name for p in Path("alembic/versions").glob("0*.py"))
        assert versions[-1].startswith("0027")

    def test_no_timer_daemon_or_marketops_hook(self):
        for rel in self.FILES:
            src = Path(rel).read_text()
            for banned in ("systemd", "crontab", "daemon", "marketops",
                           "MarketOps"):
                assert banned not in src, f"{rel}: {banned}"


class TestReviewFindings002A:
    """Findings from the independent population-boundary review."""

    def test_window_end_is_required(self):
        with pytest.raises(pr.PredicateError, match="window_end is required"):
            pr.canonicalize_population({
                "schema_version": 1,
                "all": [{"field": "domain", "operator": "eq", "value": "x"}],
                "none": []})

    def test_window_end_is_inside_the_digest(self):
        a = pr.canonicalize_population(pop(
            {"field": "domain", "operator": "eq", "value": "x"},
            window_end="unbounded"))
        b = pr.canonicalize_population(pop(
            {"field": "domain", "operator": "eq", "value": "x"},
            window_end="2026-12-01T00:00:00+00:00"))
        assert pr.population_digest(a) != pr.population_digest(b)

    def test_naive_window_end_rejected(self):
        with pytest.raises(pr.PredicateError, match="timezone-naive"):
            pr.canonicalize_population(pop(
                {"field": "domain", "operator": "eq", "value": "x"},
                window_end="2026-12-01T00:00:00"))

    def test_before_declared_end_forbidden_in_none(self):
        """With an unbounded window it returns True for every row, so in `none`
        it silently excludes the entire cohort."""
        with pytest.raises(pr.PredicateError, match="may not appear in .none."):
            pr.canonicalize_population(pop(
                none=[{"field": "forecast_created_at",
                       "operator": "before_declared_end"}]))

    def test_contradictory_none_clause_rejected(self):
        with pytest.raises(pr.PredicateError, match="contradictory .none."):
            pr.canonicalize_population(pop(
                none=[{"field": "domain", "operator": "eq", "value": "a"},
                      {"field": "domain", "operator": "not_eq", "value": "a"}]))

    @pytest.mark.parametrize("bad", [10 ** 400, float("nan"), float("inf"),
                                     float("-inf"), 1e20])
    def test_non_finite_and_oversized_numbers_rejected(self, bad):
        """A huge int raised OverflowError out of a function documented as pure,
        escaping validate_manifest's handler; NaN/Infinity serialize as invalid
        JSON, so any non-Python implementation could not recompute the digest."""
        with pytest.raises(pr.PredicateError):
            pr.canonicalize_population(pop(
                {"field": "research_completeness", "operator": "gte",
                 "value": bad}))

    def test_canonical_json_is_strict_json(self):
        """The reproducibility property in one assertion: a strict parser must
        be able to recompute what we digested."""
        canon = pr.canonicalize_population(pop(
            {"field": "research_completeness", "operator": "gte", "value": 0.5}))
        text = pr.canonical_population_json(canon)
        assert json.loads(text)
        assert "NaN" not in text and "Infinity" not in text

    def test_oversized_rationale_rejected(self):
        with pytest.raises(pr.PredicateError, match="rationale exceeds"):
            pr.canonicalize_population(pop(
                {"field": "domain", "operator": "eq", "value": "x"})
                | {"rationale": "x" * 9000})

    def test_validate_manifest_never_raises_a_non_predicate_error(self):
        from app.services.experiment_registry import validate_manifest
        from tests.test_prospective_experiment_registry_001 import base_manifest

        for bad in (10 ** 400, float("nan"), {"$ne": 1}, [1, 2]):
            m = base_manifest()
            m["population"] = {
                "schema_version": 1,
                "all": [{"field": "research_completeness", "operator": "gte",
                         "value": bad}],
                "none": [], "window_end": "unbounded"}
            v = validate_manifest(m)          # must not raise
            assert not v.ok


class TestReportCli:
    """The command that claims to fail closed had zero tests."""

    def _register(self, tmp_path):
        from app.services.experiment_registry import register
        from tests.test_prospective_experiment_registry_001 import base_manifest

        register(base_manifest(), base=tmp_path, confirm=True, commit="c1")
        return "baseball-calibration-stability"

    def test_clean_report_exits_zero_and_has_no_false_errors(self, tmp_path, capsys):
        from app import cli

        eid = self._register(tmp_path)
        assert cli.experiment_registry_report(eid, base=str(tmp_path),
                                              fmt="json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["manifest_intact"] is True
        assert payload["fail_closed_ok"] is True
        assert payload["validation_errors"] == [], (
            "re-validating the stored manifest produced a permanent false error "
            "on every registered experiment")
        assert payload["population_predicate_digest_matches"] is True

    def test_report_fails_closed_on_a_tampered_manifest(self, tmp_path, capsys):
        from app import cli
        from app.services.experiment_registry import experiment_dir

        eid = self._register(tmp_path)
        d = experiment_dir(eid, tmp_path)
        m = json.loads((d / "manifest.json").read_text())
        m["sample_floor"] = 5
        (d / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True))
        assert cli.experiment_registry_report(eid, base=str(tmp_path)) == 1
        assert "FAILED CLOSED" in capsys.readouterr().out

    def test_report_fails_closed_on_material_drift(self, tmp_path, capsys):
        from app import cli
        from app.services.experiment_registry import experiment_dir

        eid = self._register(tmp_path)
        d = experiment_dir(eid, tmp_path)
        m = json.loads((d / "manifest.json").read_text())
        m["immutable_references"]["population_references"][
            "field_registry_digest"] = "0" * 64
        (d / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True))
        # digest now mismatches too, but the drift itself must also be fatal
        assert cli.experiment_registry_report(eid, base=str(tmp_path)) == 1

    def test_report_writes_nothing(self, tmp_path):
        from app import cli
        from app.services.experiment_registry import experiment_dir

        eid = self._register(tmp_path)
        d = experiment_dir(eid, tmp_path)
        before = {p.name: p.read_bytes() for p in d.rglob("*") if p.is_file()}
        cli.experiment_registry_report(eid, base=str(tmp_path), fmt="json")
        after = {p.name: p.read_bytes() for p in d.rglob("*") if p.is_file()}
        assert before == after

    def test_report_text_json_parity(self, tmp_path, capsys):
        from app import cli

        eid = self._register(tmp_path)
        cli.experiment_registry_report(eid, base=str(tmp_path), fmt="json")
        payload = json.loads(capsys.readouterr().out)
        cli.experiment_registry_report(eid, base=str(tmp_path), fmt="text")
        text = capsys.readouterr().out
        assert payload["registry_state"] in text
        assert payload["population_predicate_digest"] in text
        assert str(payload["event_count"]) in text

    def test_report_on_a_missing_experiment_exits_two(self, tmp_path, capsys):
        from app import cli

        assert cli.experiment_registry_report("no-such-experiment",
                                              base=str(tmp_path)) == 2
        assert "error:" in capsys.readouterr().out

    def test_report_is_secret_free(self, tmp_path, capsys):
        from app import cli

        eid = self._register(tmp_path)
        cli.experiment_registry_report(eid, base=str(tmp_path), fmt="json")
        out = capsys.readouterr().out.lower()
        for needle in ("api_key", "secret", "password", "bearer", "token="):
            assert needle not in out
