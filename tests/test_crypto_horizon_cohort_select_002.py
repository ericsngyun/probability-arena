"""CRYPTO-HORIZON-COHORT-SELECT-002: explicit-token cohort selection.

Freeze EXACTLY the requested canonical token ids — no freshest-first fallback,
no substitution, atomic all-or-nothing, zero provider calls. Everything local.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoTokenBirthEvent,
)
from app.services.crypto_horizon import CryptoHorizonService, _valid_token_id

A = "A" * 44
B = "B" * 44
C = "C" * 44
D = "D" * 44


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_birth(session, addr, *, mins_ago, symbol="T", liq=5000.0, pair="pair", price=1e-5):
    anchor = datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    b = CryptoTokenBirthEvent(
        chain="solana", token_address=addr, symbol=symbol,
        observed_at=anchor, first_evidence_at=anchor,
        first_pair_address=pair, initial_price_usd=price,
        initial_liquidity_usd=liq, first_dex_id="pumpfun", created_at=anchor,
    )
    session.add(b)
    session.flush()
    return b


def svc():
    return CryptoHorizonService()


def create(session, tokens, **kw):
    return svc().create_cohort(session, tokens=tokens, **kw)


def cohort_count(session):
    return session.execute(select(func.count()).select_from(CryptoHorizonCohort)).scalar()


# --- exact selection --------------------------------------------------------


def test_exact_two_token_selection_order_preserved(session):
    add_birth(session, A, mins_ago=6, symbol="AA")
    add_birth(session, B, mins_ago=6, symbol="BB")
    r = create(session, [A, B], require_complete=True, confirm=True)
    assert r["status"] == "ok" and r["persisted"] is True
    members = session.execute(
        select(CryptoHorizonCohortMember)
        .where(CryptoHorizonCohortMember.cohort_id == r["cohort_id"])
        .order_by(CryptoHorizonCohortMember.id)
    ).scalars().all()
    assert [m.token_address for m in members] == [A, B]  # input order


def test_exact_one_token_selection(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["status"] == "ok" and r["members_selected"] == 1


def test_duplicate_token_rejected(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A, A], require_complete=True, confirm=True)
    assert r["status"] == "rejected"
    assert any(x["reason"] == "duplicate" for x in r["rejections"])
    assert cohort_count(session) == 0


def test_malformed_identifier_rejected(session):
    r = create(session, ["not-a-valid-id!"], confirm=True)
    assert r["status"] == "rejected"
    assert r["token_reports"][0]["reason"] == "malformed_identifier"
    assert cohort_count(session) == 0


def test_unknown_token_rejected(session):
    r = create(session, [A], confirm=True)  # A not seeded
    assert r["status"] == "rejected"
    assert r["token_reports"][0]["reason"] == "unknown_token_no_local_evidence"
    assert cohort_count(session) == 0


def test_no_symbol_matching(session):
    add_birth(session, A, mins_ago=6, symbol="MEME")
    r = create(session, ["MEME"], confirm=True)  # symbol, not id
    assert r["status"] == "rejected"
    assert r["token_reports"][0]["reason"] == "malformed_identifier"
    assert cohort_count(session) == 0


def test_no_display_name_matching(session):
    add_birth(session, A, mins_ago=6, symbol="Meme Coin")
    r = create(session, ["Meme Coin"], confirm=True)
    assert r["status"] == "rejected"
    assert cohort_count(session) == 0


def test_no_partial_address_matching(session):
    add_birth(session, A, mins_ago=6)  # A = "A"*44
    r = create(session, ["A" * 33], confirm=True)  # valid-length prefix, not exact
    assert r["status"] == "rejected"
    assert r["token_reports"][0]["reason"] == "unknown_token_no_local_evidence"
    assert cohort_count(session) == 0


def test_no_freshest_first_fallback(session):
    # a fresher null token exists; explicit request of an unknown id must NOT
    # fall back to selecting the fresher token.
    add_birth(session, B, mins_ago=1, symbol="FRESH", liq=None)
    r = create(session, [A], confirm=True)  # A unknown
    assert r["status"] == "rejected"
    assert r["resulting_members"] == []
    assert cohort_count(session) == 0


def test_mixed_valid_and_invalid_creates_nothing(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A, B], require_complete=True, confirm=True)  # B unknown
    assert r["status"] == "rejected"
    assert cohort_count(session) == 0
    assert session.execute(
        select(func.count()).select_from(CryptoHorizonCohortMember)
    ).scalar() == 0


# --- completeness validation ------------------------------------------------


def test_complete_state_validation_succeeds(session):
    add_birth(session, A, mins_ago=6, liq=8000.0)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["status"] == "ok"


def test_null_initial_liquidity_rejected(session):
    add_birth(session, A, mins_ago=6, liq=None)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["status"] == "rejected"
    assert r["token_reports"][0]["reason"] == "liquidity_or_initial_state_missing"
    assert cohort_count(session) == 0


def test_missing_initial_price_rejected(session):
    add_birth(session, A, mins_ago=6, price=None)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["token_reports"][0]["reason"] == "missing_initial_price"


def test_invalid_pair_rejected(session):
    add_birth(session, A, mins_ago=6, pair=None)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["token_reports"][0]["reason"] == "invalid_pair"


def test_no_liquidity_state_rejected(session):
    add_birth(session, A, mins_ago=6, liq=None)  # no liquidity state at birth
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["status"] == "rejected"


def test_zero_liquidity_rejected(session):
    add_birth(session, A, mins_ago=6, liq=0.0)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["token_reports"][0]["reason"] == "null_initial_liquidity"


def test_expired_15m_rejected_when_full_lifecycle_required(session):
    add_birth(session, A, mins_ago=30, liq=8000.0)  # 15m window closed
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["status"] == "rejected"
    assert r["token_reports"][0]["reason"] == "horizon_infeasible"


# --- shared-window validation -----------------------------------------------


def test_shared_intersection_calculated_for_identical_birth(session):
    add_birth(session, A, mins_ago=6)
    add_birth(session, B, mins_ago=6)  # identical birth
    r = create(session, [A, B], require_complete=True, dry_run=True)
    sp = r["shared_pass"]
    assert sp["shared_pass_eligible"] is True
    assert sp["per_horizon"]["15m"]["nonempty"] is True
    assert r["persisted"] is False


def test_non_overlapping_rejected_with_require_shared(session):
    add_birth(session, A, mins_ago=6, liq=8000.0)    # 15m [now+1.5, now+16.5]
    add_birth(session, B, mins_ago=30, liq=8000.0)   # 15m [now-22.5, now-7.5] disjoint
    r = create(session, [A, B], require_shared_horizon_windows=True, confirm=True)
    assert r["status"] == "rejected"
    assert any(x["reason"] == "no_shared_horizon_windows" for x in r["rejections"])
    assert r["shared_pass"]["per_horizon"]["15m"]["nonempty"] is False
    assert cohort_count(session) == 0


def test_identical_birth_passes_shared_window(session):
    add_birth(session, A, mins_ago=6)
    add_birth(session, B, mins_ago=6)
    r = create(session, [A, B], require_complete=True,
               require_shared_horizon_windows=True, confirm=True)
    assert r["status"] == "ok" and r["shared_pass_eligible"] is True


def test_closely_overlapping_passes_when_grace_fits(session):
    add_birth(session, A, mins_ago=6)
    add_birth(session, B, mins_ago=3)  # 3 min apart -> ~12 min overlap >> 45s grace
    r = create(session, [A, B], require_complete=True,
               require_shared_horizon_windows=True, dry_run=True)
    assert r["shared_pass_eligible"] is True
    assert r["shared_pass"]["activation_grace_fits_shared_window"] is True


def test_grace_not_fitting_causes_rejection(session):
    # 15m intersection narrower than the 45s activation grace
    add_birth(session, A, mins_ago=19.5)
    add_birth(session, B, mins_ago=5.0)
    r = create(session, [A, B], require_shared_horizon_windows=True, confirm=True)
    assert r["shared_pass_eligible"] is False
    assert r["status"] == "rejected"
    assert cohort_count(session) == 0


# --- dry-run / confirm / purity ---------------------------------------------


def test_dry_run_writes_nothing(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A], require_complete=True, dry_run=True)
    assert r["status"] == "dry_run" and r["persisted"] is False
    assert cohort_count(session) == 0
    assert session.execute(
        select(func.count()).select_from(CryptoHorizonObservation)
    ).scalar() == 0


def test_dry_run_zero_provider_calls(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A], require_complete=True, dry_run=True)
    assert r["external_calls"] == 0


def test_confirm_creates_exactly_one_cohort_atomically(session):
    add_birth(session, A, mins_ago=6)
    add_birth(session, B, mins_ago=6)
    r = create(session, [A, B], require_complete=True, confirm=True)
    assert cohort_count(session) == 1 and r["members_selected"] == 2


def test_confirm_zero_provider_calls(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A], require_complete=True, confirm=True)
    assert r["external_calls"] == 0


def test_no_confirm_no_dry_run_defaults_to_preview(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A], require_complete=True)  # neither flag
    assert r["status"] == "dry_run" and r["persisted"] is False
    assert cohort_count(session) == 0


def test_no_observation_or_unit_created_on_confirm(session):
    add_birth(session, A, mins_ago=6)
    r = create(session, [A], require_complete=True, confirm=True)
    assert session.execute(
        select(func.count()).select_from(CryptoHorizonObservation)
        .where(CryptoHorizonObservation.cohort_id == r["cohort_id"])
    ).scalar() == 0


# --- backward compatibility -------------------------------------------------


def test_freshest_first_unchanged_without_token(session):
    add_birth(session, A, mins_ago=1, symbol="fresh1")
    add_birth(session, B, mins_ago=3, symbol="fresh2")
    r = svc().create_cohort(session, limit=2, hours=240, dry_run=True)  # no tokens
    syms = [e["symbol"] for e in r["preview"]]
    assert syms == ["fresh1", "fresh2"]  # freshest-first preserved
    assert r.get("mode") != "explicit_token"


def test_require_complete_unchanged_without_token(session):
    add_birth(session, A, mins_ago=1, symbol="nullfresh", liq=None)
    add_birth(session, B, mins_ago=3, symbol="complete", liq=5000.0)
    r = svc().create_cohort(session, limit=2, hours=240, dry_run=True, require_complete=True)
    assert [e["symbol"] for e in r["preview"]] == ["complete"]


# --- CANARY-004 regression + safety -----------------------------------------


def test_canary004_regression_selects_intended_two_not_fresher(session):
    # two complete tokens with overlapping 15m windows + a fresher unrelated
    # complete token; explicitly selecting the intended two creates exactly those.
    add_birth(session, A, mins_ago=8, symbol="INTENDED_A", liq=9000.0)
    add_birth(session, B, mins_ago=8, symbol="INTENDED_B", liq=9000.0)  # same birth as A
    add_birth(session, C, mins_ago=1, symbol="FRESHER_UNRELATED", liq=7000.0)
    r = create(session, [A, B], require_complete=True,
               require_shared_horizon_windows=True, confirm=True)
    assert r["status"] == "ok"
    members = session.execute(
        select(CryptoHorizonCohortMember.token_address)
        .where(CryptoHorizonCohortMember.cohort_id == r["cohort_id"])
    ).scalars().all()
    assert set(members) == {A, B}
    assert C not in members  # fresher unrelated NOT included
    assert r["shared_pass_eligible"] is True


def test_no_trading_capability_in_explicit_selector():
    source = (Path(__file__).resolve().parents[1] / "app/services/crypto_horizon.py").read_text()
    assert "explicit_token_selection" in source
    for banned in ("expected_value", "place_order", "submit_order", "position_siz",
                   "while True", "while 1"):
        assert banned not in source


def test_valid_token_id_helper():
    assert _valid_token_id(A) and _valid_token_id("DC4sure5XanGz2Te2UNPAKR2iAkshzYqvsv3coH4pump")
    assert not _valid_token_id("22M")
    assert not _valid_token_id("has space here aaaaaaaaaaaaaaaaaaaaaaa")
    assert not _valid_token_id("short")
    assert not _valid_token_id("0" * 44)  # 0 not in base58
