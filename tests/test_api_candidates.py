"""End-to-end tests of GET /markets/candidates via TestClient.

Lifespan is intentionally not entered (no context manager), so no migrations
run; the DB dependency is overridden with an in-memory SQLite session and the
Kalshi adapter is faked.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import MarketEligibilityAssessment
from app.services import cache
from app.services import scanner as scanner_module
from tests.conftest import make_market

GOOD = make_market(
    ticker="GOOD-MKT",
    title="Healthy two-sided market",
    close_time=datetime.now(timezone.utc) + timedelta(days=7),
    rules_primary=(
        "Resolves YES if the final score exceeds 100 points "
        "according to the official MLB box score at mlb.com."
    ),
)
PARLAY = make_market(
    ticker="KXMVECROSSCATEGORY-S2026-XYZ",
    title="yes Spain advances,yes Croatia advances,yes Argentina advances",
    yes_bid=None,
    yes_ask=None,
    liquidity=0,
    volume_24h=0,
    close_time=datetime.now(timezone.utc) + timedelta(days=18),
)


class FakeAdapter:
    def __init__(self, markets):
        self.markets = markets

    async def fetch_active_markets(self, max_markets=None):
        return self.markets[: max_markets or len(self.markets)]


@pytest.fixture
def client(monkeypatch):
    # TestClient serves requests on another thread; share the one in-memory connection
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    def override_get_db():
        yield session

    from app.services import enrichment as enrichment_module
    from app.services import outcomes as outcomes_module
    from tests.test_enrichment import FakeDetailAdapter

    class SettledYesOutcomeAdapter:
        async def get_market_detail(self, ticker):
            return {"ticker": ticker, "status": "settled", "result": "yes"}

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(scanner_module, "KalshiRestAdapter", lambda: FakeAdapter([GOOD, PARLAY]))
    monkeypatch.setattr(enrichment_module, "KalshiRestAdapter", FakeDetailAdapter)
    monkeypatch.setattr(outcomes_module, "KalshiRestAdapter", SettledYesOutcomeAdapter)
    monkeypatch.setattr(cache, "get_cached", lambda key: None)
    monkeypatch.setattr(cache, "set_cached", lambda key, value, ttl: None)

    yield TestClient(app), session

    app.dependency_overrides.clear()
    session.close()


def test_rejected_markets_do_not_appear_by_default(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates").json()

    tickers = [c["ticker"] for c in body["candidates"]]
    assert tickers == ["GOOD-MKT"]
    assert body["rejected"] == []
    assert body["markets_assessed"] == 2
    assert body["eligible_count"] == 1
    assert body["rejected_count"] == 1


def test_eligible_candidate_ranks_normally_with_eligibility_status(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates").json()

    candidate = body["candidates"][0]
    assert candidate["is_eligible"] is True
    assert candidate["score"] > 0.0
    assert candidate["components"]["spread"] > 0.0


def test_include_rejected_exposes_reasons(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates?include_rejected=true").json()

    assert [c["ticker"] for c in body["candidates"]] == ["GOOD-MKT"]
    assert len(body["rejected"]) == 1
    rejected = body["rejected"][0]
    assert rejected["ticker"] == "KXMVECROSSCATEGORY-S2026-XYZ"
    assert rejected["is_eligible"] is False
    assert rejected["score"] == 0.0
    assert "no_quotes" in rejected["rejection_reasons"]
    assert rejected["market_type_flags"]["multivariate"] is True
    assert "parlay_like_market" in rejected["warnings"]


def test_every_rejected_market_has_reasons(client):
    test_client, _ = client
    body = test_client.get("/markets/candidates?include_rejected=true").json()
    assert all(r["rejection_reasons"] for r in body["rejected"])


def test_post_resolution_assessment_persists_and_returns_result(client):
    test_client, session = client
    test_client.get("/markets/candidates")  # scan stores market metadata

    response = test_client.post("/markets/GOOD-MKT/resolution-assessment")
    assert response.status_code == 201
    body = response.json()
    assert body["market_ticker"] == "GOOD-MKT"
    assert body["model_name"] == "rule-based"
    assert body["prompt_version"] == "v1"
    assert body["scanner_run_id"] is None
    assert body["tradeability"] == "researchable"
    assert 0.0 <= body["clarity_score"] <= 1.0
    assert "raw_response" not in body

    from app.models import MarketResolutionAssessment

    rows = session.execute(select(MarketResolutionAssessment)).scalars().all()
    assert len(rows) == 1
    assert rows[0].raw_response is not None


def test_post_resolution_assessment_unknown_ticker_404(client):
    test_client, _ = client
    response = test_client.post("/markets/NOPE-MKT/resolution-assessment")
    assert response.status_code == 404


def test_candidates_omit_resolution_by_default(client):
    test_client, _ = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/resolution-assessment")

    body = test_client.get("/markets/candidates").json()
    assert body["candidates"][0]["resolution"] is None


def test_include_resolution_attaches_latest_assessment(client):
    test_client, _ = client
    test_client.get("/markets/candidates")

    # No assessment yet -> resolution stays null even when requested
    body = test_client.get("/markets/candidates?include_resolution=true").json()
    assert body["candidates"][0]["resolution"] is None

    test_client.post("/markets/GOOD-MKT/resolution-assessment")
    test_client.post("/markets/GOOD-MKT/resolution-assessment")  # newer row wins

    body = test_client.get("/markets/candidates?include_resolution=true").json()
    resolution = body["candidates"][0]["resolution"]
    assert resolution is not None
    assert resolution["market_ticker"] == "GOOD-MKT"
    assert resolution["model_name"] == "rule-based"
    assert resolution["tradeability"] == "researchable"
    assert resolution["settlement_source"]


def test_post_enrich_details_persists_and_excludes_raw_payloads(client):
    test_client, session = client
    test_client.get("/markets/candidates")  # scan stores market metadata

    response = test_client.post("/markets/GOOD-MKT/enrich-details")
    assert response.status_code == 201
    body = response.json()
    assert body["market_ticker"] == "GOOD-MKT"
    assert body["series_ticker"] == "KXMLBHRR"
    assert body["settlement_source"].startswith("ESPN")
    assert body["scanner_run_id"] is None
    assert not any(key.startswith("raw_") for key in body)

    from app.models import MarketDetailEnrichment

    row = session.execute(select(MarketDetailEnrichment)).scalar_one()
    assert row.raw_market_detail is not None
    assert row.raw_series_detail is not None


def test_post_enrich_details_unknown_ticker_404(client):
    test_client, _ = client
    assert test_client.post("/markets/NOPE-MKT/enrich-details").status_code == 404


def test_resolution_assessment_uses_persisted_enrichment(client):
    test_client, _ = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/enrich-details")

    body = test_client.post("/markets/GOOD-MKT/resolution-assessment").json()
    assert body["settlement_source"].startswith("ESPN")
    assert "unclear_settlement_source" not in body["ambiguity_flags"]


def test_post_research_packet_creates_and_persists(client):
    test_client, session = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/enrich-details")
    test_client.post("/markets/GOOD-MKT/resolution-assessment")

    response = test_client.post("/markets/GOOD-MKT/research-packet")
    assert response.status_code == 201
    body = response.json()
    assert body["market_ticker"] == "GOOD-MKT"
    assert body["collector_name"] == "template"
    assert body["domain"] == "sports_baseball"  # via enriched KXMLBHRR series metadata
    assert body["enrichment_id"] is not None
    assert body["resolution_assessment_id"] is not None
    assert any("settles via" in f["fact"].lower() for f in body["key_facts"])
    assert body["research_risk"] in ("low", "medium")
    assert "raw_response" not in body

    from app.models import MarketResearchPacket

    row = session.execute(select(MarketResearchPacket)).scalar_one()
    assert row.raw_response is not None


def test_get_research_packets_returns_recent_without_raw(client):
    test_client, _ = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/research-packet")
    test_client.post("/markets/GOOD-MKT/research-packet")

    response = test_client.get("/markets/GOOD-MKT/research-packets?limit=1")
    assert response.status_code == 200
    packets = response.json()
    assert len(packets) == 1
    assert packets[0]["market_ticker"] == "GOOD-MKT"
    assert "raw_response" not in packets[0]

    assert len(test_client.get("/markets/GOOD-MKT/research-packets").json()) == 2


def test_research_packet_endpoints_unknown_ticker_404(client):
    test_client, _ = client
    assert test_client.post("/markets/NOPE-MKT/research-packet").status_code == 404
    assert test_client.get("/markets/NOPE-MKT/research-packets").status_code == 404


def test_post_forecast_requires_research_packet(client):
    test_client, _ = client
    test_client.get("/markets/candidates")

    response = test_client.post("/markets/GOOD-MKT/forecast")
    assert response.status_code == 409
    assert "research packet" in response.json()["detail"]


def test_post_forecast_with_prepare_creates_packet_and_forecast(client):
    test_client, session = client
    test_client.get("/markets/candidates")

    response = test_client.post("/markets/GOOD-MKT/forecast?prepare=true")
    assert response.status_code == 201
    body = response.json()
    assert body["market_ticker"] == "GOOD-MKT"
    assert body["forecaster_name"] == "template_baseline"
    assert body["research_packet_id"] is not None
    assert 0.0 <= body["estimated_probability"] <= 1.0
    assert body["evidence_depth"] == "template_only"
    assert body["confidence"] <= 0.55
    assert body["bull_case"]["points"] and body["bear_case"]["points"]
    assert "raw_response" not in body

    from app.models import MarketForecastRecord, MarketResearchPacket

    assert session.execute(select(MarketResearchPacket)).scalar_one() is not None
    assert session.execute(select(MarketForecastRecord)).scalar_one().raw_response is not None


def test_post_forecast_after_full_pipeline(client):
    test_client, _ = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/enrich-details")
    test_client.post("/markets/GOOD-MKT/resolution-assessment")
    test_client.post("/markets/GOOD-MKT/research-packet")

    body = test_client.post("/markets/GOOD-MKT/forecast").json()
    assert body["resolution_assessment_id"] is not None
    assert body["forecast_risk"] in ("medium", "high")  # template_only stays cautious


def test_get_forecasts_newest_first_without_raw(client):
    test_client, _ = client
    test_client.get("/markets/candidates")
    first = test_client.post("/markets/GOOD-MKT/forecast?prepare=true").json()
    second = test_client.post("/markets/GOOD-MKT/forecast").json()

    response = test_client.get("/markets/GOOD-MKT/forecasts")
    assert response.status_code == 200
    forecasts = response.json()
    assert [f["id"] for f in forecasts] == [second["id"], first["id"]]
    assert all("raw_response" not in f for f in forecasts)

    assert len(test_client.get("/markets/GOOD-MKT/forecasts?limit=1").json()) == 1


def test_forecast_endpoints_unknown_ticker_404(client):
    test_client, _ = client
    assert test_client.post("/markets/NOPE-MKT/forecast").status_code == 404
    assert test_client.get("/markets/NOPE-MKT/forecasts").status_code == 404


def test_include_forecast_attaches_latest_without_creating(client):
    test_client, session = client
    test_client.get("/markets/candidates")

    # No forecast yet -> stays null even when requested
    body = test_client.get("/markets/candidates?include_forecast=true").json()
    assert body["candidates"][0]["forecast"] is None

    created = test_client.post("/markets/GOOD-MKT/forecast?prepare=true").json()

    from app.models import MarketForecastRecord

    count_before = len(session.execute(select(MarketForecastRecord)).scalars().all())
    body = test_client.get("/markets/candidates?include_forecast=true").json()
    attached = body["candidates"][0]["forecast"]
    assert attached is not None
    assert attached["id"] == created["id"]
    assert "raw_response" not in attached
    # GET must never create forecasts
    count_after = len(session.execute(select(MarketForecastRecord)).scalars().all())
    assert count_after == count_before

    # And the default response omits forecasts entirely
    assert test_client.get("/markets/candidates").json()["candidates"][0]["forecast"] is None


def test_outcome_endpoints_sync_then_get(client):
    test_client, session = client
    test_client.get("/markets/candidates")

    # Not synced yet -> 404
    assert test_client.get("/markets/GOOD-MKT/outcome").status_code == 404

    created = test_client.post("/markets/GOOD-MKT/sync-outcome")
    assert created.status_code == 201
    body = created.json()
    assert body["market_ticker"] == "GOOD-MKT"
    assert body["outcome_status"] == "settled"
    assert body["winning_side"] == "yes"
    assert body["resolved_probability"] == 1.0
    assert "raw_payload" not in body

    fetched = test_client.get("/markets/GOOD-MKT/outcome").json()
    assert fetched["id"] == body["id"]

    from app.models import MarketOutcomeRecord

    row = session.execute(select(MarketOutcomeRecord)).scalar_one()
    assert row.raw_payload is not None

    # Unknown ticker -> 404 on both
    assert test_client.get("/markets/NOPE-MKT/outcome").status_code == 404
    assert test_client.post("/markets/NOPE-MKT/sync-outcome").status_code == 404


def test_forecast_scores_endpoint_with_filters(client):
    test_client, session = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/forecast?prepare=true")
    test_client.post("/markets/GOOD-MKT/sync-outcome")

    from app.services.calibration import CalibrationService

    CalibrationService().score_unscored(session)

    scores = test_client.get("/forecasts/scores").json()
    assert len(scores) == 1
    assert scores[0]["score_status"] == "scored"
    assert scores[0]["market_ticker"] == "GOOD-MKT"
    assert scores[0]["brier_score"] is not None

    assert test_client.get("/forecasts/scores?score_status=scored").json()
    assert test_client.get("/forecasts/scores?score_status=pending_outcome").json() == []
    assert test_client.get("/forecasts/scores?market_ticker=GOOD-MKT").json()
    assert test_client.get("/forecasts/scores?market_ticker=OTHER").json() == []
    assert test_client.get("/forecasts/scores?forecaster_name=template_baseline").json()
    assert test_client.get("/forecasts/scores?forecaster_name=nope").json() == []
    assert test_client.get("/forecasts/scores?evidence_depth=template_only").json()
    assert test_client.get("/forecasts/scores?evidence_depth=source_backed").json() == []


def test_calibration_summary_endpoint(client):
    test_client, session = client
    test_client.get("/markets/candidates")
    test_client.post("/markets/GOOD-MKT/forecast?prepare=true")
    test_client.post("/markets/GOOD-MKT/sync-outcome")

    from app.services.calibration import CalibrationService

    CalibrationService().score_unscored(session)

    body = test_client.get("/calibration/summary").json()
    assert body["total_scores"] == 1
    assert body["resolved"] == 1
    assert body["overall"]["count"] == 1
    assert body["overall"]["mean_brier"] is not None
    assert "template_only" in body["by_evidence_depth"]
    assert "template_baseline" in body["by_forecaster"]


def test_pipeline_runs_endpoints(client):
    test_client, session = client
    # No runs yet
    assert test_client.get("/pipeline/runs").json() == []
    assert test_client.get("/pipeline/runs/999").status_code == 404

    import asyncio

    from tests.test_pipeline import CFG, make_runner

    run = asyncio.run(make_runner().run_baseline_pipeline(session, CFG))

    runs = test_client.get("/pipeline/runs").json()
    assert len(runs) == 1
    assert runs[0]["id"] == run.id
    assert runs[0]["status"] == "completed"
    assert "stages" not in runs[0]

    detail = test_client.get(f"/pipeline/runs/{run.id}").json()
    assert detail["id"] == run.id
    assert len(detail["stages"]) == 8
    assert detail["stages"][0]["stage_name"] == "scan"
    assert all(s["status"] == "completed" for s in detail["stages"])

    assert test_client.get("/pipeline/runs?status=failed").json() == []


def _seed_signal(session, **overrides):
    from datetime import datetime, timezone

    from app.models import OpportunitySignal

    now = datetime.now(timezone.utc)
    fields = dict(
        market_ticker="GOOD-MKT",
        signal_type="price_move_threshold",
        signal_status="new",
        observed_at=now,
        old_midpoint=0.5,
        new_midpoint=0.6,
        price_change=0.1,
        reason="Midpoint moved +0.10",
        evidence={"threshold": 0.07},
        raw_payload={"kept": "db-only"},
        created_at=now,
    )
    fields.update(overrides)
    row = OpportunitySignal(**fields)
    session.add(row)
    session.commit()
    return row


def test_signals_list_detail_and_filters(client):
    test_client, session = client
    assert test_client.get("/signals").json() == []

    first = _seed_signal(session)
    second = _seed_signal(session, signal_type="spread_tightened", market_ticker="OTHER-MKT")

    signals = test_client.get("/signals").json()
    assert [s["id"] for s in signals] == [second.id, first.id]  # newest first
    assert all("raw_payload" not in s for s in signals)

    assert len(test_client.get("/signals?signal_type=price_move_threshold").json()) == 1
    assert len(test_client.get("/signals?market_ticker=OTHER-MKT").json()) == 1
    assert len(test_client.get("/signals?signal_status=new").json()) == 2
    assert test_client.get("/signals?signal_status=bogus").status_code == 422

    detail = test_client.get(f"/signals/{first.id}").json()
    assert detail["reason"] == "Midpoint moved +0.10"
    assert detail["evidence"] == {"threshold": 0.07}
    assert test_client.get("/signals/9999").status_code == 404


def test_signal_status_update_workflow(client):
    test_client, session = client
    signal = _seed_signal(session)

    updated = test_client.patch(
        f"/signals/{signal.id}/status", json={"signal_status": "promoted_to_research"}
    )
    assert updated.status_code == 200
    assert updated.json()["signal_status"] == "promoted_to_research"
    assert test_client.get(f"/signals/{signal.id}").json()["signal_status"] == "promoted_to_research"

    invalid = test_client.patch(f"/signals/{signal.id}/status", json={"signal_status": "executed"})
    assert invalid.status_code == 422
    assert test_client.patch("/signals/9999/status", json={"signal_status": "reviewed"}).status_code == 404


def test_signal_workflow_api_promote_and_process(client, monkeypatch):
    test_client, session = client
    from app.services import enrichment as enrichment_module
    from tests.test_enrichment import FakeDetailAdapter

    # processing builds its own enrichment service; adapter already patched in
    # the fixture, but be explicit for this flow
    monkeypatch.setattr(enrichment_module, "KalshiRestAdapter", FakeDetailAdapter)

    test_client.get("/markets/candidates")  # store GOOD-MKT metadata
    signal = _seed_signal(session)  # GOOD-MKT, price_move_threshold, new

    # GET /signals/recent
    recent = test_client.get("/signals/recent").json()
    assert [s["id"] for s in recent] == [signal.id]
    assert recent[0]["signal_status"] == "new"

    # POST /signals/{id}/promote
    promoted = test_client.post(f"/signals/{signal.id}/promote")
    assert promoted.status_code == 200
    assert promoted.json()["signal_status"] == "promoted_to_research"
    assert promoted.json()["promoted_at"] is not None
    # idempotent
    assert test_client.post(f"/signals/{signal.id}/promote").status_code == 200
    assert test_client.post("/signals/9999/promote").status_code == 404

    # dismissed signal -> 409
    dismissed = _seed_signal(session, signal_status="dismissed", market_ticker="OTHER-MKT")
    assert test_client.post(f"/signals/{dismissed.id}/promote").status_code == 409

    # POST /signals/process-promoted
    processed = test_client.post("/signals/process-promoted?limit=5")
    assert processed.status_code == 200
    body = processed.json()
    assert len(body) == 1
    assert body[0]["signal_status"] == "forecast_refreshed"
    assert body[0]["refreshed_research_packet_id"] is not None
    assert body[0]["refreshed_forecast_id"] is not None
    assert body[0]["processing_error_type"] is None

    # GET /signals/report
    report = test_client.get("/signals/report").json()
    assert report["total"] == 2
    assert report["by_status"]["forecast_refreshed"] == 1
    assert report["promoted_awaiting_processing"] == 0
    assert len(report["recent_refreshed"]) == 1
    assert report["recent_refreshed"][0]["market_ticker"] == "GOOD-MKT"


def test_signal_status_patch_accepts_new_workflow_statuses(client):
    test_client, session = client
    signal = _seed_signal(session)
    response = test_client.patch(
        f"/signals/{signal.id}/status", json={"signal_status": "paper_candidate_pending"}
    )
    assert response.status_code == 200
    assert response.json()["signal_status"] == "paper_candidate_pending"


def test_champion_challenger_endpoint(client):
    test_client, session = client
    from tests.test_champion_challenger import seed_pair

    seed_pair(session, "CC-API-1", base_brier=0.30, chal_brier=0.10)

    body = test_client.get("/calibration/champion-challenger").json()
    assert body["baseline_forecaster"] == "template_baseline"
    assert body["challenger_forecaster"] == "baseball_evidence_v1"
    assert body["comparison_basis"] == "unpaired"
    assert body["baseline"]["scored"]["count_scored"] == 1
    assert body["challenger"]["scored"]["count_scored"] == 1
    assert body["delta_brier"] == pytest.approx(-0.20)
    assert body["paired"]["pair_count"] == 1
    assert body["sample_label"] == "insufficient_sample"
    assert "do NOT infer edge" in body["warning"]
    assert body["by_market_type"]

    # filters
    filtered = test_client.get(
        "/calibration/champion-challenger?domain=sports_tennis"
    ).json()
    assert filtered["challenger"]["scored"]["count_scored"] == 0
    paired_only = test_client.get(
        "/calibration/champion-challenger?paired_only=true"
    ).json()
    assert paired_only["comparison_basis"] == "paired"
    custom = test_client.get(
        "/calibration/champion-challenger?challenger_forecaster=baseball_evidence"
    ).json()
    assert custom["challenger"]["scored"]["count_scored"] == 1  # bare-name match


def test_scan_persists_eligibility_assessments_linked_to_run(client):
    test_client, session = client
    body = test_client.get("/markets/candidates").json()

    rows = session.execute(select(MarketEligibilityAssessment)).scalars().all()
    assert len(rows) == 2
    by_ticker = {row.market_ticker: row for row in rows}
    assert by_ticker["GOOD-MKT"].is_eligible is True
    assert by_ticker["GOOD-MKT"].rejection_reasons == []

    parlay = by_ticker["KXMVECROSSCATEGORY-S2026-XYZ"]
    assert parlay.is_eligible is False
    assert "no_quotes" in parlay.rejection_reasons
    assert parlay.has_two_sided_quote is False
    assert parlay.market_type_flags["multivariate"] is True
    assert all(row.scanner_run_id == body["scanner_run_id"] for row in rows)
