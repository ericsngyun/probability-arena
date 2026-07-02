import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import MarketResolutionAssessment
from app.schemas import ResolutionAssessment
from app.services.resolution import (
    FLAG_MISSING_RULES,
    FLAG_MULTI_CONDITION,
    FLAG_RULES_TOO_SHORT,
    FLAG_SUBJECTIVE_WORDING,
    FLAG_UNCLEAR_SETTLEMENT_SOURCE,
    REASON_CLARITY_BELOW_MIN,
    MockResolutionJudge,
    RuleBasedResolutionJudge,
    detect_settlement_source,
    get_judge,
    persist_assessment,
)
from tests.conftest import make_market

CLEAR_RULES = (
    "Resolves YES if the upper bound of the federal funds target range exceeds 4.00% "
    "according to the Federal Reserve's official press release at federalreserve.gov."
)
VAGUE_RULES = (
    "Resolves YES if there is a significant and widely reported major policy shift, "
    "as generally expected by observers."
)

JUDGE = RuleBasedResolutionJudge(min_clarity_score=0.70, prompt_version="v1")


def clear_market(**overrides):
    return make_market(ticker="CLEAR-MKT", rules_primary=CLEAR_RULES, **overrides)


def vague_market(**overrides):
    return make_market(ticker="VAGUE-MKT", rules_primary=VAGUE_RULES, **overrides)


class TestRuleBasedJudge:
    async def test_clear_market_scores_higher_than_vague(self):
        clear = await JUDGE.assess(clear_market())
        vague = await JUDGE.assess(vague_market())
        assert clear.clarity_score > vague.clarity_score
        assert clear.tradeability == "researchable"
        assert clear.resolution_risk == "low"
        assert clear.rejection_reasons == []

    async def test_missing_rules_text_flags_and_rejects(self):
        assessment = await JUDGE.assess(make_market(rules_primary=None))
        assert assessment.clarity_score == 0.0
        assert assessment.resolution_risk == "unknown"
        assert assessment.tradeability == "needs_manual_review"
        assert FLAG_MISSING_RULES in assessment.ambiguity_flags
        assert FLAG_MISSING_RULES in assessment.rejection_reasons
        assert REASON_CLARITY_BELOW_MIN in assessment.rejection_reasons

    async def test_subjective_wording_is_penalized_and_flagged(self):
        neutral = await JUDGE.assess(
            make_market(rules_primary="Resolves YES if the score exceeds 100 according to ESPN.com.")
        )
        subjective = await JUDGE.assess(
            make_market(
                rules_primary="Resolves YES if a significant score increase is likely according to ESPN.com."
            )
        )
        assert subjective.clarity_score < neutral.clarity_score
        assert any(
            flag.startswith(FLAG_SUBJECTIVE_WORDING) for flag in subjective.ambiguity_flags
        )

    async def test_unclear_settlement_source_is_penalized(self):
        sourced = await JUDGE.assess(
            make_market(rules_primary="Resolves YES if X happens according to the Associated Press.")
        )
        unsourced = await JUDGE.assess(
            make_market(rules_primary="Resolves YES if X happens before the deadline passes.")
        )
        assert unsourced.clarity_score < sourced.clarity_score
        assert FLAG_UNCLEAR_SETTLEMENT_SOURCE in unsourced.ambiguity_flags
        assert sourced.settlement_source == "the Associated Press"

    async def test_multi_condition_phrasing_is_penalized(self):
        single = await JUDGE.assess(clear_market())
        multi = await JUDGE.assess(
            make_market(
                rules_primary=(
                    "Resolves YES if all of the following occur according to federalreserve.gov: "
                    "a rate cut and a policy statement and a press conference."
                )
            )
        )
        assert multi.clarity_score < single.clarity_score
        assert FLAG_MULTI_CONDITION in multi.ambiguity_flags

    async def test_parlay_ticker_counts_as_multi_condition(self):
        assessment = await JUDGE.assess(
            make_market(ticker="KXMVE-COMBO-1", rules_primary=CLEAR_RULES)
        )
        assert FLAG_MULTI_CONDITION in assessment.ambiguity_flags

    async def test_short_rules_text_is_penalized(self):
        assessment = await JUDGE.assess(make_market(rules_primary="Resolves YES."))
        assert FLAG_RULES_TOO_SHORT in assessment.ambiguity_flags

    async def test_below_min_clarity_is_not_researchable(self):
        assessment = await JUDGE.assess(vague_market())
        assert assessment.clarity_score < 0.70
        assert assessment.tradeability in ("needs_manual_review", "avoid")
        assert REASON_CLARITY_BELOW_MIN in assessment.rejection_reasons

    async def test_assessment_is_deterministic(self):
        first = await JUDGE.assess(vague_market())
        second = await JUDGE.assess(vague_market())
        assert first.model_dump() == second.model_dump()

    async def test_min_clarity_threshold_is_configurable(self):
        lenient = RuleBasedResolutionJudge(min_clarity_score=0.10)
        strict = RuleBasedResolutionJudge(min_clarity_score=0.99)
        market = vague_market()
        assert (await lenient.assess(market)).tradeability == "researchable"
        assert (await strict.assess(market)).tradeability != "researchable"


def test_detect_settlement_source_patterns():
    assert detect_settlement_source("as published by the Bureau of Labor Statistics") is not None
    assert detect_settlement_source("data from federalreserve.gov applies") is not None
    assert detect_settlement_source("the official MLB box score decides") is not None
    assert detect_settlement_source("whatever seems right at the time") is None


def test_get_judge_defaults_to_rule_based():
    judge = get_judge()
    assert isinstance(judge, RuleBasedResolutionJudge)
    assert judge.model_name == "rule-based"


class TestPersistence:
    @pytest.fixture
    def session(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            yield session

    async def test_assessment_persists_with_audit_fields(self, session):
        assessment = await JUDGE.assess(clear_market())
        row = persist_assessment(session, "CLEAR-MKT", assessment, JUDGE, scanner_run_id=None)

        loaded = session.execute(select(MarketResolutionAssessment)).scalar_one()
        assert loaded.id == row.id
        assert loaded.market_ticker == "CLEAR-MKT"
        assert loaded.scanner_run_id is None
        assert loaded.model_name == "rule-based"
        assert loaded.prompt_version == "v1"
        assert loaded.clarity_score == assessment.clarity_score
        assert loaded.tradeability == "researchable"
        assert loaded.raw_response["clarity_score"] == assessment.clarity_score
        assert loaded.created_at is not None

    async def test_mock_judge_persists_canned_result(self, session):
        judge = MockResolutionJudge()
        assessment = await judge.assess(clear_market())
        persist_assessment(session, "CLEAR-MKT", assessment, judge)

        loaded = session.execute(select(MarketResolutionAssessment)).scalar_one()
        assert loaded.model_name == "mock"
        assert loaded.clarity_score == 0.9
        assert judge.assessed_tickers == ["CLEAR-MKT"]


def test_raw_response_is_excluded_from_serialization():
    assessment = ResolutionAssessment(
        clarity_score=0.5,
        resolution_risk="medium",
        tradeability="needs_manual_review",
        raw_response={"secret": "internals"},
    )
    assert "raw_response" not in assessment.model_dump()
