"""PROSPECTIVE-EXPERIMENT-REGISTRY-002B — result enforcement.

The registry computes the result; the caller supplies an id and prose. Almost
every test here asserts that some route to a flattering answer is closed.
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
from app.services import experiment_registry as er
from app.services import experiment_results as rs
from app.services.calibration import brier_score

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def manifest(*, eid="exp-one", floor=3, frac=0.5, primary="mean_brier",
             rule="primary_metric_delta_gt_zero", domain="sports_baseball"):
    return {
        "experiment_id": eid, "experiment_version": 1, "title": "t",
        "research_question": "q", "hypothesis": "h", "null_hypothesis": "n",
        "exploratory_or_confirmatory": "confirmatory",
        "experiment_class": "prospective_calibration", "owner": "eric",
        "domain": domain, "market_population": "mp",
        "forecast_family": "baseball_evidence", "forecast_version": "v1",
        "feature_definitions": {"p": "estimated_probability"},
        "signal_definitions": {"none": "none"}, "data_sources": ["kalshi_rest"],
        "provider_policy": "existing read-only path only",
        "population": {"schema_version": 1, "window_end": "unbounded",
                       "all": [{"field": "domain", "operator": "eq",
                                "value": domain}], "none": []},
        "start_condition": "first forecast after registration",
        "end_condition": "floor and matured fraction",
        "evaluation_horizons": ["close + 1h"],
        "primary_metric": {"name": primary, "definition": "d"},
        "secondary_metrics": [{"name": "ece"}],
        "declared_baselines": {"base_rate_brier": "prevalence Brier"},
        "sample_floor": floor, "domain_sample_floors": {domain: floor},
        "minimum_matured_fraction": frac,
        "missing_data_policy": "pending counted", "canceled_void_policy": "excluded",
        "conflict_policy": "preserved", "stale_score_policy": "excluded",
        "multiple_testing_policy": "one primary",
        "stopping_rule": "fixed sample and end",
        "invalidating_conditions": ["version change"],
        "known_limitations": ["thin"],
        "safety_boundary": "measurement only; no execution or capital behavior",
        "result_protocol": {
            "baseline": "base_rate_brier", "decision_rule": rule,
            "confidence_interval_policy": "cluster_bootstrap_by_market_v1",
            "stopping_rule": {"kind": "fixed_sample_and_end",
                              "minimum_sample": floor,
                              "minimum_matured_fraction": frac,
                              "not_before": "2026-08-06T00:00:00+00:00",
                              "not_after": "2027-08-06T00:00:00+00:00"},
        },
    }


def seed(db, n, *, after, domain="sports_baseball", p=0.9, wins=None,
         settle=True, ticker_prefix="T", discriminating=False):
    """n forecasts, each on its own market.

    `discriminating=True` gives winners a high probability and losers a low one.
    A CONSTANT forecast can never beat the base rate — the base rate is the
    optimal constant — so skill requires discrimination, not confidence.
    """
    wins = n if wins is None else wins
    for i in range(n):
        t = f"{ticker_prefix}-{i:03d}"
        pk = MarketResearchPacket(market_ticker=t, collector_name="c",
                                  domain=domain, research_completeness_score=0.9,
                                  research_risk="low", created_at=after)
        db.add(pk); db.flush()
        prob = p if not discriminating else (0.92 if i < wins else 0.08)
        db.add(MarketForecastRecord(
            market_ticker=t, estimated_probability=prob,
            forecaster_name="baseball_evidence", forecaster_version="v1",
            confidence=0.8, evidence_depth="shallow", forecast_risk="low",
            research_packet_id=pk.id, created_at=after + timedelta(minutes=i)))
        if settle:
            side = "yes" if i < wins else "no"
            db.add(MarketOutcomeRecord(
                market_ticker=t, outcome_status="settled", winning_side=side,
                resolved_probability=1.0 if side == "yes" else 0.0,
                source="kalshi_rest"))
    db.commit()


def register(tmp_path, m=None):
    m = m or manifest()
    er.register(m, base=tmp_path, confirm=True, commit="c1")
    return m["experiment_id"]


class TestEventHeadIntegrity:
    def _reg(self, tmp_path):
        eid = register(tmp_path)
        er.transition(eid, er.COLLECTING, base=tmp_path, confirm=True)
        er.transition(eid, er.MATURED, base=tmp_path, confirm=True)
        return eid

    def test_clean_chain_and_head(self, tmp_path):
        eid = self._reg(tmp_path)
        assert er.verify_event_chain(eid, tmp_path)["intact"] is True

    def test_suffix_truncation_detected(self, tmp_path):
        """A prefix of a valid hash chain is a valid hash chain — which is why
        the head must pin the count and terminal hash."""
        eid = self._reg(tmp_path)
        f = er.experiment_dir(eid, tmp_path) / er.EVENTS_FILENAME
        lines = f.read_text().splitlines()
        f.write_text("\n".join(lines[:-1]) + "\n")
        r = er.verify_event_chain(eid, tmp_path)
        assert r["intact"] is False
        assert "truncated" in r["reason"]

    def test_middle_deletion_detected(self, tmp_path):
        eid = self._reg(tmp_path)
        f = er.experiment_dir(eid, tmp_path) / er.EVENTS_FILENAME
        lines = f.read_text().splitlines()
        f.write_text("\n".join([lines[0], lines[2]]) + "\n")
        assert er.verify_event_chain(eid, tmp_path)["intact"] is False

    def test_append_without_head_update_detected(self, tmp_path):
        eid = self._reg(tmp_path)
        d = er.experiment_dir(eid, tmp_path)
        events = er.read_events(eid, tmp_path)
        forged = dict(events[-1]); forged["seq"] = len(events)
        forged["prev"] = er._event_hash(events[-1])
        with (d / er.EVENTS_FILENAME).open("a") as fh:
            fh.write(er.canonical_json(forged) + "\n")
        r = er.verify_event_chain(eid, tmp_path)
        assert r["intact"] is False
        assert "appended without updating the head" in r["reason"]

    def test_missing_head_fails_closed(self, tmp_path):
        eid = self._reg(tmp_path)
        (er.experiment_dir(eid, tmp_path) / er.HEAD_FILENAME).unlink()
        r = er.verify_event_chain(eid, tmp_path)
        assert r["intact"] is False
        assert "head missing" in r["reason"]

    def test_zero_result_state_is_explicit(self, tmp_path):
        eid = register(tmp_path)
        r = rs.verify_result_chain(eid, tmp_path)
        assert r["intact"] is True and r["empty"] is True


class TestOrderingAndPopulation:
    def test_membership_is_frozen_before_outcomes_are_read(self, db, tmp_path):
        """The ordering that makes this a control: a cohort cannot be shaped by
        what it would score."""
        eid = register(tmp_path)
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 6, after=reg_at + timedelta(hours=1), wins=4)
        first = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        # flip every outcome; membership must not move
        for o in db.query(MarketOutcomeRecord).all():
            o.winning_side = "no"; o.resolved_probability = 0.0
        db.commit()
        second = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        assert first["record"]["membership_digest"] == \
            second["record"]["membership_digest"]
        assert first["record"]["primary_metric_value"] != \
            second["record"]["primary_metric_value"]

    def test_pre_registration_forecasts_excluded(self, db, tmp_path):
        eid = register(tmp_path)
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 3, after=reg_at - timedelta(days=2), ticker_prefix="OLD")
        seed(db, 4, after=reg_at + timedelta(hours=1), ticker_prefix="NEW")
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        r = out["record"]
        assert r["actual_population_count"] == 4
        assert r["pre_registration_excluded"] == 3

    def test_caller_cannot_supply_population_metric_or_verdict(self):
        import inspect

        params = set(inspect.signature(rs.evaluate_experiment).parameters)
        for banned in ("population", "members", "metric", "metric_value",
                       "verdict", "sample_count", "declared_end", "end_time",
                       "primary_metric"):
            assert banned not in params, f"caller may supply {banned}"
        assert params <= {"session", "experiment_id", "base", "confirm", "now",
                          "operator_notes", "reevaluation_reason", "commit"}


class TestMetricParity:
    def test_brier_parity_with_canonical_implementation(self, db, tmp_path):
        eid = register(tmp_path)
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 5, after=reg_at + timedelta(hours=1), p=0.8, wins=3)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        expected = round(sum(brier_score(0.8, y) for y in
                             [1.0, 1.0, 1.0, 0.0, 0.0]) / 5, 6)
        assert out["record"]["primary_metric_value"] == expected

    def test_base_rate_baseline_parity(self, db, tmp_path):
        eid = register(tmp_path)
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 4, after=reg_at + timedelta(hours=1), p=0.7, wins=2)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        prev = 0.5
        expected = round(sum(brier_score(prev, y) for y in
                             [1.0, 1.0, 0.0, 0.0]) / 4, 6)
        assert out["record"]["declared_baseline_value"] == expected

    def test_brier_skill_parity(self, db, tmp_path):
        eid = register(tmp_path, manifest(
            eid="exp-skill", primary="brier_skill_vs_base_rate", floor=2))
        m = er.load_manifest(er.experiment_dir("exp-skill", tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 4, after=reg_at + timedelta(hours=1), p=0.9, wins=2)
        out = rs.evaluate_experiment(db, "exp-skill", base=tmp_path,
                                     now=NOW + timedelta(days=1))
        r = out["record"]
        mb = round(sum(brier_score(0.9, y) for y in [1.0, 1.0, 0.0, 0.0]) / 4, 6)
        bb = round(sum(brier_score(0.5, y) for y in [1.0, 1.0, 0.0, 0.0]) / 4, 6)
        assert r["primary_metric_value"] == round(1.0 - mb / bb, 6)

    def test_confidence_interval_is_deterministic(self, db, tmp_path):
        eid = register(tmp_path)
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 6, after=reg_at + timedelta(hours=1), wins=4)
        a = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        b = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        assert a["record"]["confidence_interval"] == b["record"]["confidence_interval"]
        assert a["record"]["confidence_interval"]["seed"] == rs.CI_SEED

    def test_ci_clusters_by_market_not_forecast(self, db, tmp_path):
        eid = register(tmp_path)
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 5, after=reg_at + timedelta(hours=1), wins=3)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        assert out["record"]["confidence_interval"]["clusters"] == 5


class TestEnforcement:
    def _prep(self, db, tmp_path, n, wins, discriminating=False, **kw):
        eid = register(tmp_path, manifest(**kw))
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, n, after=reg_at + timedelta(hours=1), wins=wins,
             discriminating=discriminating)
        return eid

    def test_below_sample_floor_cannot_support(self, db, tmp_path):
        eid = self._prep(db, tmp_path, 2, 2, floor=100)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        assert out["record"]["verdict"] in (rs.STILL_COLLECTING,
                                            rs.INCONCLUSIVE_FLOOR)
        assert out["record"]["verdict"] not in rs.TERMINAL_FAVOURABLE

    def test_stopping_rule_unmet_returns_collecting(self, db, tmp_path):
        eid = self._prep(db, tmp_path, 2, 2, floor=50)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        assert out["record"]["verdict"] == rs.STILL_COLLECTING
        assert out["record"]["stopping_rule_met"] is False

    def test_matured_fraction_unmet_returns_collecting(self, db, tmp_path):
        eid = register(tmp_path, manifest(eid="exp-frac", floor=2, frac=0.9))
        m = er.load_manifest(er.experiment_dir("exp-frac", tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 2, after=reg_at + timedelta(hours=1), wins=2)
        seed(db, 6, after=reg_at + timedelta(hours=2), settle=False,
             ticker_prefix="P")
        out = rs.evaluate_experiment(db, "exp-frac", base=tmp_path,
                                     now=NOW + timedelta(days=1))
        r = out["record"]
        assert r["pending_count"] == 6
        assert r["verdict"] == rs.STILL_COLLECTING

    def test_registry_derives_a_favorable_verdict_only_when_earned(self, db, tmp_path):
        # 4 of 6 win with DISCRIMINATING forecasts. A constant forecast can
        # never beat the base rate, so a fixture with one probability for every
        # member can only ever produce does_not_support.
        eid = self._prep(db, tmp_path, 6, 4, floor=3, frac=0.5,
                         discriminating=True)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        r = out["record"]
        assert r["sample_floor_met"] and r["stopping_rule_met"]
        assert r["verdict"] == rs.SUPPORTS
        assert r["metric_delta"] > 0

    def test_unsupported_metric_is_rejected_at_REGISTRATION(self, db, tmp_path):
        """Stricter than the review asked: an unsupported metric can no longer
        even be registered, so it never reaches evaluation."""
        with pytest.raises(er.ManifestError, match="not registry-supported"):
            er.register(manifest(eid="exp-bad", primary="ece"), base=tmp_path,
                        confirm=True, commit="c1")

    def test_stopping_rule_without_an_end_is_rejected_at_registration(self, tmp_path):
        """H5 — `fixed_sample_and_end` with no end made the terminal moment
        'whenever someone chooses to look'."""
        m = manifest(eid="exp-noend")
        m["result_protocol"]["stopping_rule"].pop("not_after")
        with pytest.raises(er.ManifestError, match="requires not_after"):
            er.register(m, base=tmp_path, confirm=True, commit="c1")

    def test_pending_and_unscorable_are_reported_not_dropped(self, db, tmp_path):
        eid = register(tmp_path, manifest(eid="exp-rep", floor=1, frac=0.1))
        m = er.load_manifest(er.experiment_dir("exp-rep", tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 2, after=reg_at + timedelta(hours=1), wins=2)
        seed(db, 3, after=reg_at + timedelta(hours=2), settle=False,
             ticker_prefix="Q")
        out = rs.evaluate_experiment(db, "exp-rep", base=tmp_path,
                                     now=NOW + timedelta(days=1))
        r = out["record"]
        assert r["actual_population_count"] == 5
        assert r["matured_count"] == 2 and r["pending_count"] == 3
        assert r["missingness"] == {"pending": 3, "unscorable": 0}

    def test_broken_event_chain_blocks_confirmation(self, db, tmp_path):
        eid = self._prep(db, tmp_path, 6, 6, floor=3, frac=0.5)
        f = er.experiment_dir(eid, tmp_path) / er.EVENTS_FILENAME
        f.write_text("")
        with pytest.raises(er.ManifestError):
            rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)

    def test_material_drift_invalidates(self, db, tmp_path):
        eid = self._prep(db, tmp_path, 6, 6, floor=3, frac=0.5)
        d = er.experiment_dir(eid, tmp_path)
        man = json.loads((d / "manifest.json").read_text())
        man["immutable_references"]["metric_references"][
            "metric_code_digests"]["app/services/calibration.py"] = "0" * 64
        (d / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
        out = rs.evaluate_experiment(db, eid, base=tmp_path,
                                     now=NOW + timedelta(days=1))
        assert out["record"]["verdict"] == rs.INVALID_PROTOCOL

    def test_degenerate_base_rate_invalidates(self, db, tmp_path):
        """Everything wins -> prevalence 1.0 -> base-rate Brier 0 -> the
        baseline is unbeatable. This is exactly the artifact that produced
        soccer's 0.0033 on 34 members at 2.9% prevalence."""
        eid = self._prep(db, tmp_path, 6, 6, eid="exp-degen", floor=3, frac=0.5)
        out = rs.evaluate_experiment(db, "exp-degen", base=tmp_path,
                                     now=NOW + timedelta(days=1))
        r = out["record"]
        assert r["verdict"] == rs.INVALID_DATA
        assert any("degenerate base rate" in e for e in r["invalidating_events"])

    def test_unregistered_experiment_refused(self, db, tmp_path):
        with pytest.raises(er.ManifestError, match="not registered"):
            rs.evaluate_experiment(db, "no-such", base=tmp_path)


class TestAppendOnlyResults:
    def _terminal(self, db, tmp_path):
        eid = register(tmp_path, manifest(floor=3, frac=0.5))
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 6, after=reg_at + timedelta(hours=1), wins=4)
        return eid

    def test_dry_run_writes_nothing(self, db, tmp_path):
        eid = self._terminal(db, tmp_path)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, now=NOW + timedelta(days=1))
        assert out["persisted"] is False
        d = er.experiment_dir(eid, tmp_path)
        assert not (d / rs.RESULT_EVENTS_FILENAME).exists()
        assert not list((d / rs.RESULTS_DIRNAME).glob("*.json")) \
            if (d / rs.RESULTS_DIRNAME).exists() else True

    def test_confirm_appends_and_is_chained(self, db, tmp_path):
        eid = self._terminal(db, tmp_path)
        out = rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)
        assert out["persisted"] is True
        assert rs.verify_result_chain(eid, tmp_path)["intact"] is True
        assert len(rs.read_result_events(eid, tmp_path)) == 1

    def test_reevaluation_requires_a_reason_and_links_the_prior(self, db, tmp_path):
        eid = self._terminal(db, tmp_path)
        first = rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)
        with pytest.raises(er.ManifestError, match="requires an explicit reason"):
            rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)
        second = rs.evaluate_experiment(
            db, eid, base=tmp_path, confirm=True,
            reevaluation_reason="outcome corrected upstream")
        assert second["record"]["previous_result_digest"] == \
            first["record"]["result_digest"]
        assert len(rs.read_result_events(eid, tmp_path)) == 2

    def test_first_terminal_result_is_preserved(self, db, tmp_path):
        eid = self._terminal(db, tmp_path)
        first = rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)
        for o in db.query(MarketOutcomeRecord).all():
            o.winning_side = "no"; o.resolved_probability = 0.0
        db.commit()
        rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True,
                               reevaluation_reason="flip")
        files = sorted((er.experiment_dir(eid, tmp_path) /
                        rs.RESULTS_DIRNAME).glob("*.json"))
        assert len(files) == 2
        original = json.loads(files[0].read_text()) if \
            json.loads(files[0].read_text())["result_digest"] == \
            first["record"]["result_digest"] else json.loads(files[1].read_text())
        assert original["verdict"] == first["record"]["verdict"]

    def test_result_suffix_truncation_detected(self, db, tmp_path):
        eid = self._terminal(db, tmp_path)
        rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)
        f = er.experiment_dir(eid, tmp_path) / rs.RESULT_EVENTS_FILENAME
        f.write_text("")
        r = rs.verify_result_chain(eid, tmp_path)
        assert r["intact"] is False

    def test_content_digest_is_deterministic_and_result_digest_is_unique(
            self, db, tmp_path):
        """Reproducibility means the FINDING reproduces, not the timestamp.

        Two evaluations of identical data are different records — in an
        append-only log they must be — so result_digest differs while
        content_digest does not.
        """
        eid = self._terminal(db, tmp_path)
        a = rs.evaluate_experiment(db, eid, base=tmp_path)
        b = rs.evaluate_experiment(db, eid, base=tmp_path)
        assert a["record"]["content_digest"] == b["record"]["content_digest"]
        assert a["record"]["result_digest"] != b["record"]["result_digest"]


class TestSafetySurface:
    FILE = "app/services/experiment_results.py"

    def test_no_ev_price_or_capital_surface(self):
        banned = ("expected_value", "kelly", "position_size", "place_order",
                  "wallet", "private_key", "execute_trade", "paper_trade",
                  "market_price", "midpoint", "yes_bid", "yes_ask", "pnl",
                  "portfolio", "returns")
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
        for i in idents:
            for b in banned:
                assert b not in i, f"{self.FILE}: {i}"

    def test_no_provider_or_network_imports(self):
        tree = ast.parse(Path(self.FILE).read_text())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        for banned in ("httpx", "requests", "urllib", "socket", "aiohttp",
                       "subprocess"):
            assert banned not in mods

    def test_metrics_reuse_canonical_implementations(self):
        """A second implementation is a second answer."""
        src = Path(self.FILE).read_text()
        assert "from app.services.calibration import" in src
        assert "brier_score" in src
        # the formula itself must not be restated
        assert "** 2" not in src and "**2" not in src

    def test_no_migration_hook_or_timer(self):
        versions = sorted(p.name for p in Path("alembic/versions").glob("0*.py"))
        assert versions[-1].startswith("0027")
        src = Path(self.FILE).read_text()
        for banned in ("systemd", "crontab", "daemon", "MarketOps", "marketops"):
            assert banned not in src

    def test_verdict_vocabulary_is_closed_and_research_only(self):
        assert len(rs.ALL_VERDICTS) == 6
        for v in rs.ALL_VERDICTS:
            for banned in ("profit", "trade", "buy", "sell", "edge",
                           "opportunity", "alpha"):
                assert banned not in v


class TestDraftCompatibility:
    @pytest.mark.parametrize("name", [
        "baseball-prospective-calibration-stability",
        "soccer-prospective-reliability",
        "tennis-base-rate-falsification",
    ])
    def test_draft_declares_a_supported_result_contract(self, name):
        m = json.loads(Path(f"manifests/{name}.json").read_text())
        proto = m["result_protocol"]
        assert m["primary_metric"]["name"] in rs.SUPPORTED_PRIMARY_METRICS
        assert proto["baseline"] in rs.SUPPORTED_BASELINES
        assert proto["decision_rule"] in rs.SUPPORTED_DECISION_RULES
        assert proto["confidence_interval_policy"] in rs.SUPPORTED_CI_POLICIES
        assert proto["stopping_rule"]["kind"] in rs.SUPPORTED_STOPPING_RULES
        assert isinstance(m["sample_floor"], int) and m["sample_floor"] > 0
        assert 0 < m["minimum_matured_fraction"] <= 1
        assert proto["data_quality_invalidation_policy"]
        assert proto["protocol_deviation_policy"]

    def test_tennis_remains_a_falsification(self):
        """Negative tennis performance must not be quietly converted into a
        promotion objective."""
        m = json.loads(
            Path("manifests/tennis-base-rate-falsification.json").read_text())
        assert m["result_protocol"]["decision_rule"] == \
            rs.DECISION_DELTA_LT_ZERO
        assert "at or below zero" in m["hypothesis"].lower() \
            or "not" in m["hypothesis"].lower()


class TestReviewFindings002B:
    """The five High findings from the result-enforcement review, each of which
    was a live route to a favorable verdict."""

    def _terminal(self, db, tmp_path, eid="exp-rv"):
        register(tmp_path, manifest(eid=eid, floor=3, frac=0.5))
        m = er.load_manifest(er.experiment_dir(eid, tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 6, after=reg_at + timedelta(hours=1), wins=4,
             discriminating=True)
        return eid

    def test_first_terminal_verdict_is_locked_in_the_head(self, db, tmp_path):
        """H1 — the head tracked the NEWEST result, so peeking repeatedly and
        citing the flattering one was fully available."""
        eid = self._terminal(db, tmp_path)
        first = rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True)
        head = rs.read_result_head(eid, tmp_path)
        assert head["terminal_result_digest"] == first["record"]["result_digest"]
        assert head["terminal_verdict"] == first["record"]["verdict"]

        for o in db.query(MarketOutcomeRecord).all():
            o.winning_side = "no"; o.resolved_probability = 0.0
        db.commit()
        second = rs.evaluate_experiment(db, eid, base=tmp_path, confirm=True,
                                        reevaluation_reason="upstream fix")
        head2 = rs.read_result_head(eid, tmp_path)
        # the lock does not move, and the later peek is marked
        assert head2["terminal_result_digest"] == first["record"]["result_digest"]
        assert head2["terminal_verdict"] == first["record"]["verdict"]
        assert second["record"]["superseded_by_protocol"] is True

    def test_deleted_result_file_breaks_the_chain(self, db, tmp_path):
        """H2 — result files sat outside the integrity check entirely."""
        eid = self._terminal(db, tmp_path, eid="exp-del")
        rs.evaluate_experiment(db, "exp-del", base=tmp_path, confirm=True)
        f = next((er.experiment_dir("exp-del", tmp_path) /
                  rs.RESULTS_DIRNAME).glob("*.json"))
        f.unlink()
        r = rs.verify_result_chain("exp-del", tmp_path)
        assert r["intact"] is False and "missing" in r["reason"]

    def test_rewritten_result_verdict_breaks_the_chain(self, db, tmp_path):
        """H2 — a verdict could be rewritten to supports_hypothesis in place."""
        eid = self._terminal(db, tmp_path, eid="exp-rw")
        rs.evaluate_experiment(db, "exp-rw", base=tmp_path, confirm=True)
        f = next((er.experiment_dir("exp-rw", tmp_path) /
                  rs.RESULTS_DIRNAME).glob("*.json"))
        rec = json.loads(f.read_text())
        assert rec["verdict"] != rs.DOES_NOT_SUPPORT
        rec["verdict"] = rs.DOES_NOT_SUPPORT      # any in-place edit at all
        rec["primary_metric_value"] = 0.0
        f.write_text(json.dumps(rec, indent=2, sort_keys=True))
        r = rs.verify_result_chain("exp-rw", tmp_path)
        assert r["intact"] is False and "digest" in r["reason"]

    def test_caller_cannot_choose_the_clock_for_a_confirmed_run(self, db, tmp_path):
        """H3 — passing a future `now` turned still_collecting into
        supports_hypothesis and persisted it under the spoofed timestamp."""
        eid = self._terminal(db, tmp_path, eid="exp-clock")
        with pytest.raises(er.ManifestError, match="caller-chosen clock"):
            rs.evaluate_experiment(db, "exp-clock", base=tmp_path, confirm=True,
                                   now=datetime.now(timezone.utc) + timedelta(days=30))

    def test_record_carries_a_non_overridable_recorded_at(self, db, tmp_path):
        eid = self._terminal(db, tmp_path, eid="exp-rec")
        out = rs.evaluate_experiment(db, "exp-rec", base=tmp_path,
                                     now=NOW - timedelta(days=400))
        assert out["record"]["evaluated_at"] != out["record"]["recorded_at"]

    def test_ci_is_on_the_delta_not_the_raw_metric(self, db, tmp_path):
        """H4 — `ci_lower_bound_gt_zero` + `mean_brier` was an unconditional
        SUPPORTS, because a squared error is always > 0. A worthless p=0.5
        forecaster at 50% prevalence produced delta 0.0 and CI [0.25, 0.25]."""
        register(tmp_path, manifest(eid="exp-ci", floor=3, frac=0.5,
                                    rule="ci_lower_bound_gt_zero"))
        m = er.load_manifest(er.experiment_dir("exp-ci", tmp_path) / "manifest.json")
        reg_at = datetime.fromisoformat(m["registered_at"])
        seed(db, 8, after=reg_at + timedelta(hours=1), p=0.5, wins=4)
        out = rs.evaluate_experiment(db, "exp-ci", base=tmp_path)
        r = out["record"]
        assert r["metric_delta"] == 0.0
        assert r["confidence_interval"]["lower"] <= 0.0, (
            "the interval must be about the comparison the rule tests")
        assert r["verdict"] != rs.SUPPORTS

    def test_degenerate_guard_catches_the_soccer_regime(self, db, tmp_path):
        """M1 — the guard cited soccer's 2.9% prevalence but triggered at 1%."""
        assert rs.DEGENERATE_PREVALENCE >= 0.029

    def test_membership_digest_is_cross_checked(self, db, tmp_path):
        """M2 — `_member_ids` re-derives the cohort; agreement is now proven."""
        eid = self._terminal(db, tmp_path, eid="exp-xchk")
        out = rs.evaluate_experiment(db, "exp-xchk", base=tmp_path)
        assert out["record"]["eligible_count"] == \
            out["record"]["actual_population_count"]

    def test_scanned_count_is_not_called_a_population(self, db, tmp_path):
        """M3 — `registered_population_count` was every forecast row in the
        database, printed under a heading about the cohort."""
        eid = self._terminal(db, tmp_path, eid="exp-cnt")
        seed(db, 9, after=NOW - timedelta(days=400), ticker_prefix="OTHER")
        out = rs.evaluate_experiment(db, "exp-cnt", base=tmp_path)
        r = out["record"]
        assert "registered_population_count" not in r
        assert r["scanned_forecast_count"] > r["actual_population_count"]
