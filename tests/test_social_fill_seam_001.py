"""SOCIAL-FILL-SEAM-QUALIFICATION-001 — the join contract."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.seam import fill_seam as S
from app.seam.clock import (HostBootId, capture_observation, new_process_epoch_id)
from app.seam.fill_seam import SeamRefusal as RF
from app.seam.token import TokenResolutionStatus as T

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER = "So11111111111111111111111111111111111111112"
BOOT = HostBootId.from_json({"status": "PRESENT", "value": "b" * 36})
BOOT2 = HostBootId.from_json({"status": "PRESENT", "value": "c" * 36})


def stamp(offset_us=0, boot=BOOT, host="h1", epoch=None):
    import app.seam.clock as C
    from datetime import datetime, timezone
    base_wall = datetime(2026, 8, 26, 20, 0, 0, tzinfo=timezone.utc)
    base_mono = 1_000_000_000_000
    return C.capture_observation(
        process_epoch_id=epoch or "e1", boot_id=boot, host=host,
        clock=lambda: (base_wall + timedelta(microseconds=offset_us),
                       base_mono + offset_us * 1000))


def q(**over):
    kw = dict(token_status=T.CANONICALLY_VERIFIED, social_mint=MINT,
              market_mint=MINT, delivery_mode="LIVE",
              social_received=stamp(0), quote_observed=stamp(750_000),
              source_can_bind=True, source_created_us=None,
              chain_observed=True)
    kw.update(over)
    return S.qualify(**kw)


# --- the happy path, once ----------------------------------------------------

def test_all_four_conditions_hold():
    r = q()
    assert r.joinable is True
    assert r.verdict is S.SeamVerdict.SOCIAL_FILL_JOINABLE
    assert r.refusals == ()
    assert r.latencies.pipeline_us == 750_000


# --- each condition, refused with its own reason -----------------------------

@pytest.mark.parametrize("status", [T.CHAIN_VERIFIED, T.CORROBORATION_PENDING,
                                    T.CONFLICTING_EVIDENCE, T.TEXT_CANDIDATE,
                                    T.RESOLVED_FROM_PROJECT, T.AMBIGUOUS])
def test_a_non_canonical_token_is_refused(status):
    r = q(token_status=status)
    assert RF.TOKEN_NOT_CANONICAL in r.refusals and r.joinable is False


def test_a_non_authoritative_source_is_refused():
    r = q(source_can_bind=False)
    assert RF.SOURCE_NOT_AUTHORITATIVE in r.refusals


@pytest.mark.parametrize("mode", ["BACKFILL", "REPLAY", "", "live"])
def test_non_live_delivery_is_refused(mode):
    r = q(delivery_mode=mode)
    assert RF.DELIVERY_NOT_LIVE in r.refusals, "only exact LIVE qualifies"


def test_a_mint_mismatch_is_refused():
    assert RF.MINT_MISMATCH in q(market_mint=OTHER).refusals


def test_an_absent_mint_is_not_agreement():
    assert RF.MINT_MISMATCH in q(social_mint=None).refusals
    assert RF.MINT_MISMATCH in q(market_mint=None).refusals
    assert RF.MINT_MISMATCH in q(social_mint=None, market_mint=None).refusals


def test_a_missing_chain_observation_is_refused():
    assert RF.CHAIN_OBSERVATION_MISSING in q(chain_observed=False).refusals


def test_a_missing_stamp_is_clock_not_computable():
    assert RF.CLOCK_NOT_COMPUTABLE in q(quote_observed=None).refusals
    assert RF.CLOCK_NOT_COMPUTABLE in q(social_received=None).refusals


def test_a_different_host_is_refused():
    r = q(quote_observed=stamp(750_000, host="h2"))
    assert RF.HOST_MISMATCH in r.refusals


def test_a_different_boot_epoch_is_refused():
    r = q(quote_observed=stamp(750_000, boot=BOOT2))
    assert RF.BOOT_EPOCH_MISMATCH in r.refusals


def test_a_quote_preceding_the_social_receipt_is_refused():
    """A reaction cannot precede what it reacts to."""
    r = q(social_received=stamp(500_000), quote_observed=stamp(0))
    assert RF.QUOTE_PRECEDES_SOCIAL_RECEIPT in r.refusals
    assert r.joinable is False


# --- every refusal is collected, not short-circuited -------------------------

def test_all_refusals_are_reported_together():
    r = q(token_status=T.TEXT_CANDIDATE, delivery_mode="BACKFILL",
          market_mint=OTHER, source_can_bind=False, chain_observed=False)
    assert {RF.TOKEN_NOT_CANONICAL, RF.DELIVERY_NOT_LIVE, RF.MINT_MISMATCH,
            RF.SOURCE_NOT_AUTHORITATIVE,
            RF.CHAIN_OBSERVATION_MISSING} <= set(r.refusals)
    assert len(r.refusals) >= 5, "a five-way failure is not a one-way failure"


# --- the two latencies stay separate -----------------------------------------

def test_delivery_and_pipeline_are_separate_fields():
    fields = set(S.Latencies.__dataclass_fields__)
    assert {"delivery_us", "pipeline_us"} <= fields
    assert not any("total" in f or "latency_us" == f for f in fields)


def test_delivery_latency_is_marked_contaminated():
    r = q(source_created_us=int(stamp(0).wall_datetime.timestamp() * 1e6) - 2_000_000)
    assert r.latencies.delivery_us == 2_000_000
    assert r.latencies.delivery_is_contaminated is True, (
        "t_source_created comes from a platform clock we cannot audit")


def test_absent_creation_time_gives_no_delivery_latency_not_zero():
    r = q(source_created_us=None)
    assert r.latencies.delivery_us is None
    assert r.latencies.delivery_us != 0


def test_the_seam_never_sums_the_two():
    import ast, inspect
    tree = ast.parse(inspect.getsource(S.qualify))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not any("total" in n.lower() for n in names)
    r = q(source_created_us=int(stamp(0).wall_datetime.timestamp() * 1e6) - 1_000_000)
    d = r.to_dict()["latencies"]
    assert d["delivery_us"] != d["pipeline_us"]
    assert "total_us" not in d


# --- it reads no market outcome ---------------------------------------------

def test_the_seam_reads_no_price_return_or_markout():
    import ast, inspect
    tree = ast.parse(inspect.getsource(S))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("price", "return", "markout", "pnl", "profit", "signal",
                   "predict", "alpha"):
        hits = {n for n in names if banned in n.lower()}
        assert not hits, f"seam references {hits}"


def test_joinable_is_the_only_sanctioned_read():
    assert q().joinable is True
    assert q(delivery_mode="BACKFILL").joinable is False
