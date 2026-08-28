"""The funnel measures plumbing. It must be incapable of measuring alpha."""

from __future__ import annotations

import pytest

from app.social.observer_funnel import (
    ArtifactOutcome, Refusal as RF, Stage as S, STAGE_ORDER, funnel_report,
)
from tests.astguard import assert_never_references, imported_modules


def out(aid, reached, refusal=None, **kw):
    return ArtifactOutcome(artifact_id=aid, source_id=kw.pop("src", "s1"),
                           reached=reached, refusal=refusal, **kw)


# --- silence is not a stage --------------------------------------------------

def test_an_artifact_must_terminate_in_a_stage_or_a_typed_refusal():
    """An artifact that simply stops being mentioned is indistinguishable from
    one never received."""
    with pytest.raises(ValueError, match="silence is not a stage"):
        ArtifactOutcome("a", "s", S.CHAIN_VERIFIED, None)


def test_joinable_and_refused_is_incoherent():
    with pytest.raises(ValueError, match="joinable AND refused"):
        ArtifactOutcome("a", "s", S.SOCIAL_FILL_JOINABLE, RF.NOT_CANONICAL)


def test_a_joinable_artifact_needs_no_refusal():
    o = out("a", S.SOCIAL_FILL_JOINABLE)
    assert o.refusal is None


# --- the funnel is monotone ---------------------------------------------------

def test_reaching_a_stage_implies_reaching_every_earlier_one():
    r = funnel_report([out("a", S.QUOTE_OBSERVATION, RF.SEAM_REFUSED)])
    for s in STAGE_ORDER[:STAGE_ORDER.index(S.QUOTE_OBSERVATION) + 1]:
        assert r["reached_at_least"][s.value] == 1
    assert r["reached_at_least"][S.SOCIAL_FILL_JOINABLE.value] == 0


def test_counts_are_non_increasing_down_the_funnel():
    outs = ([out(f"a{i}", S.RECEIVED_SOCIAL, RF.NO_MINT_CANDIDATE) for i in range(60)]
            + [out(f"b{i}", S.CHAIN_VERIFIED, RF.NOT_CANONICAL) for i in range(30)]
            + [out(f"c{i}", S.SOCIAL_FILL_JOINABLE) for i in range(10)])
    r = funnel_report(outs)
    counts = [r["reached_at_least"][s.value] for s in STAGE_ORDER]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 100 and counts[-1] == 10


def test_the_largest_loss_points_at_the_next_milestone():
    """The shape is the deliverable: where the funnel dies is the finding."""
    outs = ([out(f"a{i}", S.CHAIN_VERIFIED, RF.AUTHORITY_UNVERIFIED) for i in range(27)]
            + [out(f"b{i}", S.SOCIAL_FILL_JOINABLE) for i in range(3)])
    r = funnel_report(outs)
    assert r["largest_loss"]["from"] == S.CHAIN_VERIFIED.value
    assert r["largest_loss"]["lost"] == 27
    assert r["refusals"]["AUTHORITY_UNVERIFIED"] == 27


# --- the two latencies -------------------------------------------------------

def test_both_latencies_are_reported_and_never_summed():
    outs = [out("a", S.SOCIAL_FILL_JOINABLE, delivery_us=2_000_000,
                pipeline_us=750_000)]
    r = funnel_report(outs)
    assert r["delivery_latency_us"]["median"] == 2_000_000
    assert r["pipeline_latency_us"]["median"] == 750_000
    assert r["delivery_latency_us"]["contaminated"] is True
    assert r["pipeline_latency_us"]["contaminated"] is False
    flat = str(r)
    assert "total_latency" not in flat and "2750000" not in flat


def test_an_absent_latency_is_omitted_not_zeroed():
    r = funnel_report([out("a", S.SOCIAL_FILL_JOINABLE)])
    assert r["delivery_latency_us"]["n"] == 0
    assert r["delivery_latency_us"]["median"] is None


# --- it cannot express alpha --------------------------------------------------

def test_the_funnel_module_references_no_outcome_quantity():
    from app.social import observer_funnel as F
    assert_never_references(F, (
        "return", "markout", "pnl", "profit", "price", "score", "rank",
        "winrate", "win_rate", "alpha", "predict", "forecast", "signal",
        "m0_", "m1_"))


def test_the_outcome_type_has_no_result_field():
    fields = set(ArtifactOutcome.__dataclass_fields__)
    assert fields == {"artifact_id", "source_id", "reached", "refusal",
                      "delivery_us", "pipeline_us", "detail"}


def test_the_report_contains_only_counts_and_latencies():
    r = funnel_report([out("a", S.SOCIAL_FILL_JOINABLE, pipeline_us=1)])
    for k in ("reached_at_least", "stopped_at", "refusals"):
        assert all(isinstance(v, int) for v in r[k].values())
    assert "note" in r


def test_the_funnel_imports_nothing_that_could_price_anything():
    from app.social import observer_funnel as F
    mods = imported_modules(F)
    for banned in ("evaluate", "features", "labels", "rows", "linalg"):
        assert not any(banned in m for m in mods), f"funnel imports {banned}"
