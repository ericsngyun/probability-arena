"""CRYPTO-HORIZON-OBS-001 tests: bounded read-only horizon-observation lane.

Covers: migration 0027 round trip, due-time arithmetic, stable cohort
membership, no-duplicate-horizon observation, already-observed skipping,
inactive-token handling, missing-liquidity classification, provider-failure
handling, hard call/limit caps, dry-run persists nothing (and makes no external
call), the shadow estimate, the coverage report + success gates, no network in
tests, and no forbidden trading/execution capability. In-memory SQLite; a fake
adapter — no real network anywhere.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app import cli
from app.adapters.dexscreener import PairData
from app.db import PROJECT_ROOT, Base, run_migrations
from app.models import (
    CryptoHorizonCohort,
    CryptoHorizonCohortMember,
    CryptoHorizonObservation,
    CryptoPriceTick,
    CryptoTokenBirthEvent,
)
from app.services.crypto_horizon import (
    COHORT_MAX,
    OBS_NO_LIQUIDITY_STATE,
    OBS_OBSERVED,
    OBS_PROVIDER_NO_PAIR,
    OBS_REQUEST_FAILED,
    OBS_TOKEN_INACTIVE,
    OBSERVE_MAX_CALLS,
    STATUS_ALREADY_OBSERVED,
    STATUS_DUE_NOW,
    STATUS_INACTIVE,
    STATUS_NOT_DUE,
    STATUS_OVERDUE_UNOBSERVED,
    CryptoHorizonService,
    build_observation_report,
    plan_observations,
    shadow_estimate,
)
from tests.test_crypto_tape_001 import NOW, session  # fixture reuse

REPO = Path(__file__).resolve().parents[1]


# --- fakes ----------------------------------------------------------------------


class FakeAdapter:
    """Returns canned pairs per token; counts fetches; can raise for one token."""

    source_name = "dexscreener"

    def __init__(self, pairs_by_token=None, raise_for=None):
        self.pairs_by_token = pairs_by_token or {}
        self.raise_for = raise_for or set()
        self.calls = 0
        self.fetched: list[str] = []

    async def fetch_pairs_for_token(self, token_address):
        self.calls += 1
        self.fetched.append(token_address)
        if token_address in self.raise_for:
            raise RuntimeError("boom")
        return self.pairs_by_token.get(token_address, [])


def pair(token, *, liq=12_000.0, price=0.002, vol=8_000.0, dex="raydium"):
    return PairData(
        chain="solana", pair_address=token + "p", base_token_address=token,
        price_usd=price, liquidity_usd=liq, volume_24h_usd=vol, market_cap=90_000.0,
        fdv=90_000.0, dex_id=dex,
    )


def add_birth(session, addr, *, born_h, symbol="T"):
    anchor = NOW - timedelta(hours=born_h)
    b = CryptoTokenBirthEvent(
        chain="solana", token_address=addr, symbol=symbol, observed_at=anchor,
        first_evidence_at=anchor, initial_liquidity_usd=10_000.0,
        first_dex_id="raydium", created_at=anchor,
    )
    session.add(b)
    session.flush()
    return b


def service(adapter=None):
    return CryptoHorizonService(adapter=adapter)


# --- migration ------------------------------------------------------------------


def _tables(url):
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _config(url):
    c = Config()
    c.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    c.set_main_option("sqlalchemy.url", url)
    return c


class TestMigration0027:
    def test_up_down_up_round_trip(self, tmp_path):
        url = f"sqlite:///{tmp_path}/h.db"
        run_migrations(url)
        tabs = {"crypto_horizon_cohorts", "crypto_horizon_cohort_members",
                "crypto_horizon_observations"}
        assert tabs <= _tables(url)
        command.downgrade(_config(url), "0026")
        assert not (tabs & _tables(url))
        command.upgrade(_config(url), "head")
        assert tabs <= _tables(url)


# --- due-time arithmetic (pure) -------------------------------------------------


class TestPlanning:
    def _member(self, addr, born_h):
        return type("M", (), {
            "token_address": addr, "symbol": "T", "id": 1,
            "first_evidence_at": NOW - timedelta(hours=born_h),
            "birth_observed_at": NOW - timedelta(hours=born_h),
        })()

    def test_young_token_horizons_not_due(self):
        plan = plan_observations([self._member("t", 0.05)], {}, set(), NOW)
        assert {e.horizon: e.status for e in plan} == {
            "15m": STATUS_NOT_DUE, "1h": STATUS_NOT_DUE,
            "6h": STATUS_NOT_DUE, "24h": STATUS_NOT_DUE,
        }

    def test_due_now_within_window(self):
        # born 24h ago: 24h target is now, within +/-12h window => due_now
        plan = plan_observations([self._member("t", 24)], {}, set(), NOW)
        assert {e.horizon: e.status for e in plan}["24h"] == STATUS_DUE_NOW

    def test_overdue_when_window_closed(self):
        # born 40h ago: 24h window (target-12h..target+12h) fully passed
        plan = plan_observations([self._member("t", 40)], {}, set(), NOW)
        assert {e.horizon: e.status for e in plan}["24h"] == STATUS_OVERDUE_UNOBSERVED

    def test_already_observed_and_inactive(self):
        m = self._member("t", 24)
        existing = {("t", "24h"): OBS_OBSERVED}
        plan = plan_observations([m], existing, {"t"}, NOW)
        by = {e.horizon: e.status for e in plan}
        assert by["24h"] == STATUS_ALREADY_OBSERVED   # observed wins over inactive
        assert by["6h"] == STATUS_INACTIVE            # token flagged inactive


# --- cohort intake --------------------------------------------------------------


class TestCohort:
    def test_dry_run_persists_nothing(self, session):
        add_birth(session, "a" * 25, born_h=1)
        r = service().create_cohort(session, limit=10, dry_run=True)
        assert r["status"] == "dry_run"
        assert r["external_calls"] == 0
        assert session.query(CryptoHorizonCohort).count() == 0
        assert session.query(CryptoHorizonCohortMember).count() == 0

    def test_create_freezes_members(self, session):
        for i in range(3):
            add_birth(session, f"tok{i}" + "A" * 20, born_h=i + 1)
        r = service().create_cohort(session, limit=10, hours=48)
        assert r["status"] == "ok"
        assert r["members_selected"] == 3
        members = session.query(CryptoHorizonCohortMember).all()
        assert len(members) == 3
        assert all(m.cohort_id == r["cohort_id"] for m in members)

    def test_hard_cap(self, session):
        add_birth(session, "a" * 25, born_h=1)
        r = service().create_cohort(session, limit=9999, dry_run=True)
        assert r["requested_limit"] == COHORT_MAX


# --- observe pass ---------------------------------------------------------------


def make_cohort(session, tokens_born):
    """tokens_born: list of (addr, born_h). Returns cohort_id."""
    for addr, born_h in tokens_born:
        add_birth(session, addr, born_h=born_h)
    r = service().create_cohort(session, limit=COHORT_MAX, hours=200)
    return r["cohort_id"]


class TestObserve:
    async def test_dry_run_no_calls_no_persistence(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [pair("a" * 25)]})
        r = await service(adapter).observe_once(session, cid, dry_run=True)
        assert r["status"] == "dry_run"
        assert r["external_calls"] == 0
        assert adapter.calls == 0
        assert session.query(CryptoHorizonObservation).count() == 0
        assert session.query(CryptoPriceTick).count() == 0

    async def test_real_pass_persists_tick_and_observation(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])   # 24h due now
        adapter = FakeAdapter({"a" * 25: [pair("a" * 25, liq=15_000.0)]})
        r = await service(adapter).observe_once(session, cid)
        assert r["status"] == "ok"
        assert r["external_calls"] == 1
        obs = session.query(CryptoHorizonObservation).filter_by(horizon="24h").one()
        assert obs.status == OBS_OBSERVED
        assert obs.liquidity_usd == 15_000.0
        assert obs.tick_id is not None
        # persisted an ordinary tick tagged as a horizon observation
        tick = session.query(CryptoPriceTick).one()
        assert tick.raw_payload["source"] == "crypto-horizon-obs"
        assert tick.liquidity_usd == 15_000.0

    async def test_no_duplicate_horizon_and_already_observed_skip(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [pair("a" * 25)]})
        await service(adapter).observe_once(session, cid)
        first_calls = adapter.calls
        # second pass: the 24h horizon is already observed -> not re-fetched
        r2 = await service(adapter).observe_once(session, cid)
        assert adapter.calls == first_calls           # no new fetch
        assert r2["due_observations"] == 0
        assert session.query(CryptoHorizonObservation).filter_by(horizon="24h").count() == 1

    async def test_missing_liquidity_classified(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [pair("a" * 25, liq=None)]})
        await service(adapter).observe_once(session, cid)
        obs = session.query(CryptoHorizonObservation).filter_by(horizon="24h").one()
        assert obs.status == OBS_NO_LIQUIDITY_STATE
        assert obs.missing_cause == OBS_NO_LIQUIDITY_STATE
        assert obs.price_usd is not None   # price captured, liquidity honestly absent

    async def test_provider_no_pair_vs_inactive_by_age(self, session):
        # young born 8h: 6h horizon due_now, not aged -> provider_no_pair
        # aged born 25h: 24h horizon due_now, aged (>24h) -> token_inactive
        cid = make_cohort(session, [("young" + "Y" * 19, 8), ("aged" + "A" * 20, 25)])
        adapter = FakeAdapter({})   # no pairs for anyone
        await service(adapter).observe_once(session, cid, limit=50)
        obs = {(o.token_address, o.horizon): o
               for o in session.query(CryptoHorizonObservation).all()}
        assert obs[("young" + "Y" * 19, "6h")].status == OBS_PROVIDER_NO_PAIR
        assert obs[("aged" + "A" * 20, "24h")].status == OBS_TOKEN_INACTIVE

    async def test_request_failure_recorded_not_fabricated(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter(raise_for={"a" * 25})
        await service(adapter).observe_once(session, cid)
        obs = session.query(CryptoHorizonObservation).filter_by(horizon="24h").one()
        assert obs.status == OBS_REQUEST_FAILED
        assert obs.tick_id is None
        assert session.query(CryptoPriceTick).count() == 0

    async def test_call_cap_bounds_fetches(self, session):
        cid = make_cohort(session, [(f"t{i}" + "A" * 22, 24) for i in range(5)])
        adapter = FakeAdapter({f"t{i}" + "A" * 22: [pair(f"t{i}" + "A" * 22)]
                               for i in range(5)})
        await service(adapter).observe_once(session, cid, limit=2)
        assert adapter.calls == 2   # capped at limit


# --- shadow + report ------------------------------------------------------------


class TestShadowAndReport:
    def test_shadow_estimate_no_calls(self, session):
        cid = make_cohort(session, [("a" * 25, 24), ("b" * 25, 0.05)])
        r = shadow_estimate(session, cid)
        assert r["external_calls"] == 0
        assert "24h" in r["expected_coverage_gain_by_horizon"]
        assert r["required_calls_per_day_estimate"] == {25: 100, 50: 200, 100: 400}
        assert "SolanaTracker" in r["solana_tracker_usage"] or "DexScreener" in r["solana_tracker_usage"]

    async def test_report_gates_and_liquidity_rate(self, session):
        cid = make_cohort(session, [("a" * 25, 24)])
        adapter = FakeAdapter({"a" * 25: [pair("a" * 25, liq=15_000.0)]})
        await service(adapter).observe_once(session, cid)
        r = build_observation_report(session, cid)
        assert r["cohort_size"] == 1
        assert r["by_horizon"]["24h"]["observed"] == 1
        assert r["by_horizon"]["24h"]["completion_rate"] == 1.0
        assert r["success_gates"]["24h"]["pass"] is True
        assert r["provider_usage"]["solana_tracker_calls"] == 0
        assert "never advice" in r["disclaimer"]

    def test_empty_report(self, session):
        # a young cohort with no due horizons -> nothing due, nothing observed
        cid = make_cohort(session, [("a" * 25, 0.05)])
        r = build_observation_report(session, cid)
        assert r["observations_total"] == 0
        assert r["by_horizon"]["24h"]["completion_rate"] is None
        assert r["by_horizon"]["24h"]["due"] == 0


# --- CLI ------------------------------------------------------------------------


class TestCLI:
    async def test_cohort_and_observe_and_report_cli(self, session, capsys):
        add_birth(session, "a" * 25, born_h=24)
        n = await cli.crypto_horizon_cohort_create(session=session, limit=10)
        out = capsys.readouterr().out
        assert n == 1 and "never advice" in out
        cid = session.query(CryptoHorizonCohort).one().id

        await cli.crypto_horizon_observe_once(cohort_id=cid, session=session, dry_run=True)
        out = capsys.readouterr().out
        assert "external_calls=0" in out and "nothing persisted" in out

        await cli.crypto_horizon_observation_report(cohort_id=cid, session=session, shadow=True)
        out = capsys.readouterr().out
        assert "shadow estimate" in out and "solana_tracker" in out

    def test_main_wires_commands(self):
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
        assert {"crypto-horizon-cohort-create", "crypto-horizon-observe-once",
                "crypto-horizon-observation-report"} <= set(actions)


# --- safety ---------------------------------------------------------------------


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "crypto_horizon.py").read_text()
        toks = [
            t.string.lower()
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "submit_order", "create_order", "wallet",
                    "private_key", "recommend", "execute_trade", "sign_transaction",
                    "pnl", "profit", "sell", "arbitrage", "entry_price", "stop_loss"):
            assert bad not in code, bad

    def test_no_timer_or_loop_vocabulary(self):
        src = (REPO / "app" / "services" / "crypto_horizon.py").read_text()
        for term in ("systemd", "while True", "daemonize", "asyncio.sleep"):
            assert term not in src

    async def test_no_network_in_dry_paths(self, session, monkeypatch):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("horizon lane must not build an HTTP client here")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        add_birth(session, "a" * 25, born_h=24)
        r = service().create_cohort(session, limit=10, dry_run=True)  # no network
        assert r["external_calls"] == 0
