"""CRYPTO-HORIZON-COHORT-SELECT-001: complete-lifecycle-anchor cohort filter.

`--require-complete` restricts cohort selection to births with a valid pair,
a positive initial liquidity, and an initial price — so a canary cohort can
deterministically exclude null-liquidity fresh tokens that otherwise sort first.
Read-only DB selection; no external call, no migration.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoHorizonCohort, CryptoHorizonCohortMember, CryptoTokenBirthEvent
from app.services.crypto_horizon import CryptoHorizonService

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_birth(session, addr, *, mins_ago, symbol, liq, pair="p", price=1e-5):
    anchor = NOW - timedelta(minutes=mins_ago)
    b = CryptoTokenBirthEvent(
        chain="solana", token_address=addr, symbol=symbol,
        observed_at=anchor, first_evidence_at=anchor,
        first_pair_address=pair, initial_price_usd=price,
        initial_liquidity_usd=liq, first_dex_id="pumpfun", created_at=anchor,
    )
    session.add(b)
    session.flush()
    return b


def seed_mixed(session):
    # freshest-first: nullA(1m), nullB(3m), COMPLETE1(5m), lowliq(8m), COMPLETE2(10m)
    add_birth(session, "n" * 40, mins_ago=1, symbol="nullA", liq=None, pair=None)
    add_birth(session, "m" * 40, mins_ago=3, symbol="nullB", liq=None)
    add_birth(session, "c" * 40, mins_ago=5, symbol="COMPLETE1", liq=5000.0)
    add_birth(session, "l" * 40, mins_ago=8, symbol="lowliq", liq=200.0)
    add_birth(session, "d" * 40, mins_ago=10, symbol="COMPLETE2", liq=9000.0)


def svc():
    return CryptoHorizonService()


def test_default_selection_includes_null_liquidity_freshest(session):
    seed_mixed(session)
    r = svc().create_cohort(session, limit=2, hours=240, dry_run=True)
    syms = [e["symbol"] for e in r["preview"]]
    assert syms == ["nullA", "nullB"]  # freshest-first, unfiltered
    assert r["require_complete"] is False


def test_require_complete_excludes_null_and_picks_freshest_complete(session):
    seed_mixed(session)
    r = svc().create_cohort(session, limit=2, hours=240, dry_run=True, require_complete=True)
    syms = [e["symbol"] for e in r["preview"]]
    assert syms == ["COMPLETE1", "lowliq"]  # freshest COMPLETE, null tokens skipped
    assert r["require_complete"] is True and r["members_selected"] == 2


def test_min_liquidity_threshold_filters_below(session):
    seed_mixed(session)
    r = svc().create_cohort(
        session, limit=5, hours=240, dry_run=True,
        require_complete=True, min_liquidity=1000.0,
    )
    syms = [e["symbol"] for e in r["preview"]]
    assert syms == ["COMPLETE1", "COMPLETE2"]  # lowliq (200) excluded
    assert r["min_liquidity"] == 1000.0


def test_require_complete_real_create_persists_only_complete(session):
    seed_mixed(session)
    r = svc().create_cohort(session, limit=2, hours=240, require_complete=True)
    assert r["status"] == "ok" and r.get("cohort_id")
    members = session.execute(
        select(CryptoHorizonCohortMember).where(
            CryptoHorizonCohortMember.cohort_id == r["cohort_id"]
        )
    ).scalars().all()
    assert {m.symbol for m in members} == {"COMPLETE1", "lowliq"}
    cohort = session.get(CryptoHorizonCohort, r["cohort_id"])
    assert cohort.provenance["require_complete"] is True


def test_require_complete_no_complete_births_selects_none(session):
    add_birth(session, "n" * 40, mins_ago=1, symbol="nullA", liq=None, pair=None)
    add_birth(session, "m" * 40, mins_ago=3, symbol="nullB", liq=None)
    r = svc().create_cohort(session, limit=5, hours=240, require_complete=True)
    assert r["members_selected"] == 0 and r["status"] == "no_births"
    assert session.execute(
        select(func.count()).select_from(CryptoHorizonCohort)
    ).scalar() == 0


def test_require_complete_is_zero_call_and_dry_run_persists_nothing(session):
    seed_mixed(session)
    before = session.execute(
        select(func.count()).select_from(CryptoHorizonCohort)
    ).scalar()
    r = svc().create_cohort(session, limit=2, hours=240, dry_run=True, require_complete=True)
    assert r["external_calls"] == 0
    after = session.execute(
        select(func.count()).select_from(CryptoHorizonCohort)
    ).scalar()
    assert before == after == 0
