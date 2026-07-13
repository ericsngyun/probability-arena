"""CRYPTO-HORIZON-OBS-002 tests: pair selection + liquidity extraction +
report reconciliation + outcome-transition proof.

Covers: multiple pairs where the first lacks liquidity but a later one has it,
highest-liquidity selection, stale-high-liquidity vs active-lower-liquidity,
base/quote token identity, zero/null/missing liquidity classification, honest
no_liquidity_state when no usable pair, retry-in-place of failed observations,
no-duplicate observation, the explicit due/attempt/completion denominators,
the unknown->known outcome transition, dry-run zero calls/zero persistence,
bounded real-call behaviour via a fake adapter, and no forbidden capability.
In-memory SQLite; fake adapter — no network.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app import cli
from app.adapters.dexscreener import PairData
from app.models import (
    CryptoHorizonObservation,
    CryptoPriceTick,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_horizon import (
    LIQ_ABSENT,
    LIQ_NULL,
    LIQ_PRESENT,
    LIQ_ZERO,
    OBS_NO_LIQUIDITY_STATE,
    OBS_OBSERVED,
    POLICY_MAX_LIQ,
    POLICY_QUALITY,
    active_pair_quality_score,
    build_observation_report,
    build_outcome_reconciliation_report,
    build_pair_selection_report,
    describe_pair,
    liquidity_field_state,
    pair_is_eligible,
    select_pair,
)
from tests.test_crypto_horizon_obs_001 import (
    NOW,
    FakeAdapter,
    add_birth,
    make_cohort,
    service,
    session,  # fixture reuse
)
from tests.test_crypto_tape_001 import add_tick

REPO = Path(__file__).resolve().parents[1]


def mkpair(token, *, addr=None, liq=10_000.0, price=0.002, vol1h=5_000.0,
           vol24=8_000.0, dex="raydium", txns_h1=20, created=None, base=None):
    raw = {"txns": {"h1": {"buys": txns_h1, "sells": 0}}}
    if liq is not None:
        raw["liquidity"] = {"usd": liq}
    return PairData(
        chain="solana", pair_address=addr or (token + "p"),
        base_token_address=base or token, quote_token_address="SOL",
        price_usd=price, liquidity_usd=liq, volume_1h_usd=vol1h,
        volume_24h_usd=vol24, market_cap=90_000.0, fdv=90_000.0, dex_id=dex,
        pair_created_at=created, raw=raw,
    )


# --- pair eligibility + liquidity-field classification --------------------------


class TestPairDiagnostics:
    def test_eligibility_requires_price_and_positive_liquidity(self):
        t = "t" * 20
        assert pair_is_eligible(mkpair(t, liq=5_000.0)) is True
        assert pair_is_eligible(mkpair(t, liq=None)) is False
        assert pair_is_eligible(mkpair(t, liq=0.0)) is False
        assert pair_is_eligible(mkpair(t, price=None)) is False

    def test_liquidity_field_states(self):
        t = "t" * 20
        assert liquidity_field_state(mkpair(t, liq=12_000.0)) == LIQ_PRESENT
        assert liquidity_field_state(mkpair(t, liq=0.0)) == LIQ_ZERO
        p_null = PairData(chain="solana", pair_address="p", base_token_address=t,
                          price_usd=0.001, liquidity_usd=None,
                          raw={"liquidity": {"usd": None}})
        assert liquidity_field_state(p_null) == LIQ_NULL
        p_absent = PairData(chain="solana", pair_address="p", base_token_address=t,
                            price_usd=0.001, liquidity_usd=None, raw={})
        assert liquidity_field_state(p_absent) == LIQ_ABSENT

    def test_describe_pair_marks_base_and_score(self):
        t = "t" * 20
        d = describe_pair(mkpair(t, liq=5_000.0), t)
        assert d["is_base_token"] is True
        assert d["eligible"] is True
        assert d["quality_score"] is not None


# --- selection policies ---------------------------------------------------------


class TestSelection:
    def test_first_lacks_liquidity_second_has_it(self):
        t = "t" * 20
        pairs = [mkpair(t, addr="A", liq=None), mkpair(t, addr="B", liq=6_000.0)]
        chosen, basis = select_pair(pairs, t, policy=POLICY_QUALITY)
        assert chosen.pair_address == "B"       # skips the null-liquidity first pair
        assert basis["eligible_count"] == 1

    def test_highest_liquidity_among_active(self):
        t = "t" * 20
        pairs = [mkpair(t, addr="A", liq=10_000.0), mkpair(t, addr="B", liq=50_000.0)]
        chosen, _ = select_pair(pairs, t, policy=POLICY_QUALITY)
        assert chosen.pair_address == "B"

    def test_stale_high_liquidity_loses_to_active_lower(self):
        t = "t" * 20
        stale = mkpair(t, addr="STALE", liq=50_000.0, txns_h1=0, vol1h=0, vol24=0,
                       created=NOW - timedelta(hours=48))
        active = mkpair(t, addr="ACTIVE", liq=10_000.0, txns_h1=40, vol1h=9_000.0)
        assert active_pair_quality_score(stale, t) < active_pair_quality_score(active, t)
        chosen, _ = select_pair([stale, active], t, policy=POLICY_QUALITY)
        assert chosen.pair_address == "ACTIVE"

    def test_max_liquidity_policy_ignores_activity(self):
        t = "t" * 20
        stale = mkpair(t, addr="STALE", liq=50_000.0, txns_h1=0, vol1h=0)
        active = mkpair(t, addr="ACTIVE", liq=10_000.0, txns_h1=40)
        chosen, _ = select_pair([stale, active], t, policy=POLICY_MAX_LIQ)
        assert chosen.pair_address == "STALE"    # policy contrast is intentional

    def test_base_token_identity_preferred(self):
        t = "t" * 20
        as_base = mkpair(t, addr="BASE", liq=10_000.0, base=t)
        as_quote = mkpair(t, addr="QUOTE", liq=10_000.0, base="other")
        assert active_pair_quality_score(as_base, t) > active_pair_quality_score(as_quote, t)

    def test_no_eligible_pair_returns_none(self):
        t = "t" * 20
        chosen, basis = select_pair(
            [mkpair(t, liq=None), mkpair(t, addr="z", liq=0.0)], t, policy=POLICY_QUALITY
        )
        assert chosen is None
        assert basis["eligible_count"] == 0


# --- observe pass behaviour -----------------------------------------------------


class TestObservePass:
    async def test_selects_liquid_pair_when_first_is_null(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        pairs = [mkpair("a" * 25, addr="NULL", liq=None),
                 mkpair("a" * 25, addr="GOOD", liq=22_000.0)]
        adapter = FakeAdapter({"a" * 25: pairs})
        await service(adapter).observe_once(session, cid)
        obs = session.query(CryptoHorizonObservation).filter_by(horizon="24h").one()
        assert obs.status == OBS_OBSERVED
        assert obs.liquidity_usd == 22_000.0
        assert obs.pair_address == "GOOD"
        assert obs.raw_payload["candidate_count"] == 2

    async def test_no_usable_pair_is_honest_and_writes_no_tick(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [mkpair("a" * 25, liq=None)]})
        await service(adapter).observe_once(session, cid)
        obs = session.query(CryptoHorizonObservation).filter_by(horizon="24h").one()
        assert obs.status == OBS_NO_LIQUIDITY_STATE
        assert obs.tick_id is None
        assert session.query(CryptoPriceTick).count() == 0

    async def test_failed_observation_retried_in_place_no_duplicate(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        # first pass: no liquidity -> failed row
        await service(FakeAdapter({"a" * 25: [mkpair("a" * 25, liq=None)]})).observe_once(
            session, cid
        )
        assert session.query(CryptoHorizonObservation).filter_by(horizon="24h").count() == 1
        # second pass: now liquidity is available -> SAME row updated to observed
        await service(FakeAdapter({"a" * 25: [mkpair("a" * 25, liq=30_000.0)]})).observe_once(
            session, cid
        )
        rows = session.query(CryptoHorizonObservation).filter_by(horizon="24h").all()
        assert len(rows) == 1                        # retried in place, not duplicated
        assert rows[0].status == OBS_OBSERVED
        assert rows[0].liquidity_usd == 30_000.0

    async def test_observed_row_frozen_not_re_fetched(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [mkpair("a" * 25, liq=15_000.0)]})
        await service(adapter).observe_once(session, cid)
        first = adapter.calls
        await service(adapter).observe_once(session, cid)  # 24h already observed
        assert adapter.calls == first                     # not re-fetched
        assert session.query(CryptoHorizonObservation).filter_by(horizon="24h").count() == 1

    async def test_dry_run_zero_calls_zero_persistence(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [mkpair("a" * 25)]})
        r = await service(adapter).observe_once(session, cid, dry_run=True)
        assert r["external_calls"] == 0 and adapter.calls == 0
        assert session.query(CryptoHorizonObservation).count() == 0

    async def test_bounded_calls(self, session):
        cid = make_cohort(session, [(f"t{i}" + "A" * 22, 24) for i in range(6)])
        adapter = FakeAdapter({f"t{i}" + "A" * 22: [mkpair(f"t{i}" + "A" * 22)]
                               for i in range(6)})
        await service(adapter).observe_once(session, cid, limit=3)
        assert adapter.calls == 3


# --- report reconciliation ------------------------------------------------------


class TestReconciliation:
    async def test_explicit_denominators(self, session):
        # one observed (24h due), one young (24h not_due)
        add_birth(session, "obs" + "A" * 22, born_h=24)
        add_birth(session, "young" + "Y" * 19, born_h=0.05)
        cid = service().create_cohort(session, limit=10, hours=48)["cohort_id"]
        await service(FakeAdapter({"obs" + "A" * 22: [mkpair("obs" + "A" * 22, liq=20_000.0)]})).observe_once(
            session, cid
        )
        h = build_observation_report(session, cid)["by_horizon"]["24h"]
        assert h["observed"] == 1
        assert h["attempted"] == 1
        assert h["skipped_not_due"] == 1           # the young token
        assert h["completion_rate_of_attempts"] == 1.0
        assert h["completion_denominator"] == "attempted"
        assert h["coverage_denominator"] == "horizon_due_total"

    async def test_pair_selection_report_flags_avoidable(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        # a failed observation whose captured candidates include an eligible pair
        pairs = [mkpair("a" * 25, addr="NULL", liq=None),
                 mkpair("a" * 25, addr="GOOD", liq=9_000.0)]
        # force the failure by selecting the null one? No — observe would pick GOOD.
        # Instead seed a failed row directly with candidates to test the report.
        from app.services.crypto_horizon import CryptoHorizonService
        svc = CryptoHorizonService()
        member = session.query(
            __import__("app.models", fromlist=["CryptoHorizonCohortMember"]).CryptoHorizonCohortMember
        ).first()
        cands = [describe_pair(p, "a" * 25) for p in pairs]
        session.add(CryptoHorizonObservation(
            cohort_id=cid, member_id=member.id, chain="solana",
            token_address="a" * 25, horizon="6h", status=OBS_NO_LIQUIDITY_STATE,
            missing_cause=OBS_NO_LIQUIDITY_STATE,
            raw_payload={"selected_pair_basis": {"selected_pair": None},
                         "candidate_count": 2, "candidates": cands},
            observed_at=NOW, created_at=NOW,
        ))
        session.flush()
        r = build_pair_selection_report(session, cid)
        assert r["failed_no_liquidity"] == 1
        assert r["avoidable_failures"] == 1
        ex = r["examples"][0]
        assert ex["eligible_pair_count"] == 1
        assert ex["shadow_policy_selection"][POLICY_QUALITY] == "GOOD"


# --- outcome-transition proof ---------------------------------------------------


class TestOutcomeTransition:
    async def test_observation_flips_unknown_to_known(self, session):
        # token 24h old, no ticks yet -> 24h unknown; observe adds an in-window
        # liquidity tick -> 24h becomes known ONLY because of that tick
        birth = add_birth(session, "a" * 25, born_h=24)
        session.add(CryptoTokenSurvivalOutcome(
            birth_event_id=birth.id, chain="solana", token_address="a" * 25,
            created_at=NOW,
        ))
        session.flush()
        cid = service().create_cohort(session, limit=10, hours=48)["cohort_id"]
        await service(FakeAdapter({"a" * 25: [mkpair("a" * 25, liq=20_000.0)]})).observe_once(
            session, cid
        )
        r = build_outcome_reconciliation_report(session, cid)
        assert r["observed_with_tick"] == 1
        assert r["transitioned_unknown_to_known"] == 1
        row = r["reconciliation"][0]
        assert row["outcome_before"] is None            # without the tick: unknown
        assert row["outcome_after"] is True             # with the tick: known
        assert row["transitioned_unknown_to_known"] is True


# --- CLI + safety ---------------------------------------------------------------


class TestCLIAndSafety:
    async def test_new_report_clis_render(self, session, capsys):
        cid = make_cohort(session, [("a" * 25, 24)])
        await service(FakeAdapter({"a" * 25: [mkpair("a" * 25, liq=20_000.0)]})).observe_once(
            session, cid
        )
        await cli.crypto_horizon_pair_selection_report(cohort_id=cid, session=session)
        out = capsys.readouterr().out
        assert "pair-selection report" in out
        await cli.crypto_horizon_outcome_reconciliation_report(cohort_id=cid, session=session)
        out = capsys.readouterr().out
        assert "outcome reconciliation" in out

    def test_main_wires_new_commands(self):
        import argparse

        holder = {}
        original = argparse.ArgumentParser.parse_args

        def fake_parse(self, *a, **k):
            holder["parser"] = self
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = fake_parse
        try:
            with pytest.raises(SystemExit):
                cli.main([])
        finally:
            argparse.ArgumentParser.parse_args = original
        actions = holder["parser"]._subparsers._group_actions[0].choices
        assert {"crypto-horizon-pair-selection-report",
                "crypto-horizon-outcome-reconciliation-report"} <= set(actions)

    def test_no_forbidden_vocab(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "crypto_horizon.py").read_text()
        toks = [t.string.lower()
                for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "recommend",
                    "execute_trade", "sign_transaction", "pnl", "profit",
                    "sell", "arbitrage", "entry_price", "stop_loss"):
            assert bad not in code, bad
