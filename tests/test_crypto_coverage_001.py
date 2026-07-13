"""CRYPTO-COVERAGE-001 tests: read-only tape-coverage forensics.

Covers: every gap-cause classification, due/not-due arithmetic, nearest-tick
tolerance logic, token-revisit detection, the coverage funnel math, the
selection-starvation signal, the shadow selection comparison (due-first beats
recent-first when old cohorts are starved), no persistence, no external calls,
and no forbidden capability. In-memory SQLite; no network anywhere.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app import cli
from app.models import (
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenBirthEvent,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenRiskAssessment,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_coverage import (
    CAUSE_INACTIVE,
    CAUSE_JOIN_FAILED,
    CAUSE_NOT_DUE,
    CAUSE_NOT_REVISITED,
    CAUSE_NO_LIQ,
    CAUSE_NO_TICK,
    CAUSE_OUTSIDE_TOL,
    STATUS_DUE,
    STATUS_NOT_YET_DUE,
    STATUS_OVERDUE,
    CryptoCoverageService,
    _nearest,
    build_coverage_report,
    coverage_funnel,
    selection_analysis,
    shadow_selection,
)
from tests.test_crypto_tape_001 import NOW, session  # fixture reuse

REPO = Path(__file__).resolve().parents[1]


def seed_token(
    session, addr, *, born_h, ticks_min=(), snaps_min=(), stored=None,
    liq=10_000.0, symbol="T",
):
    """Seed a taped token: CryptoToken + birth + later ticks + revisit
    snapshots + (optionally) a stored survival outcome."""
    anchor = NOW - timedelta(hours=born_h)
    session.add(CryptoToken(
        chain="solana", token_address=addr, symbol=symbol,
        first_seen_at=anchor, last_seen_at=NOW, created_at=anchor,
    ))
    birth = CryptoTokenBirthEvent(
        chain="solana", token_address=addr, symbol=symbol, observed_at=anchor,
        first_evidence_at=anchor, initial_liquidity_usd=liq,
        first_dex_id="raydium", created_at=anchor,
    )
    session.add(birth)
    session.flush()
    for off in ticks_min:
        at = anchor + timedelta(minutes=off)
        session.add(CryptoPriceTick(
            chain="solana", token_address=addr, pair_address=addr + "p",
            observed_at=at, liquidity_usd=liq * 0.9, volume_24h_usd=5_000.0,
            created_at=at,
        ))
    for off in snaps_min:
        at = anchor + timedelta(minutes=off)
        session.add(CryptoTokenLifecycleSnapshot(
            chain="solana", token_address=addr, observed_at=at, run_id=1,
            birth_event_id=birth.id, created_at=at,
        ))
    if stored is not None:
        outcome = CryptoTokenSurvivalOutcome(
            birth_event_id=birth.id, chain="solana", token_address=addr,
            computed_at=NOW, created_at=NOW,
        )
        for key, value in stored.items():
            setattr(outcome, key, value)
        session.add(outcome)
    session.flush()
    return birth


def coverages(session, hours=200):
    return CryptoCoverageService().token_coverages(session, hours=hours)


# --- gap-cause classification (pure) --------------------------------------------


class TestClassify:
    TOL = timedelta(minutes=30)

    def c(self, **kw):
        base = dict(
            status=STATUS_DUE, stored_known=False, fresh_measurable=False,
            later=[], in_window=[], nearest_after_dist=None, tol=self.TOL,
            revisited_after_due=False, stored=None,
        )
        base.update(kw)
        return CryptoCoverageService._classify(**base)

    def test_stored_known_is_matured_not_a_gap(self):
        assert self.c(stored_known=True) is None

    def test_not_due(self):
        assert self.c(status=STATUS_NOT_YET_DUE) == CAUSE_NOT_DUE

    def test_join_failed_when_measurable_and_revisited(self):
        assert self.c(
            fresh_measurable=True, revisited_after_due=True, later=[object()],
        ) == CAUSE_JOIN_FAILED

    def test_not_revisited_when_measurable_but_never_recomputed(self):
        assert self.c(
            fresh_measurable=True, revisited_after_due=False, later=[object()],
        ) == CAUSE_NOT_REVISITED

    def test_inactive_when_no_later_ticks(self):
        assert self.c(later=[]) == CAUSE_INACTIVE

    def test_no_liquidity_when_tick_in_window_but_unmeasurable(self):
        assert self.c(later=[object()], in_window=[object()]) == CAUSE_NO_LIQ

    def test_outside_tolerance_when_tick_just_past_window(self):
        assert self.c(
            later=[object()], in_window=[],
            nearest_after_dist=self.TOL.total_seconds() * 1.5,
        ) == CAUSE_OUTSIDE_TOL

    def test_no_tick_when_nearest_far_outside(self):
        assert self.c(
            later=[object()], in_window=[],
            nearest_after_dist=self.TOL.total_seconds() * 10,
        ) == CAUSE_NO_TICK

    def test_overdue_is_treated_like_due(self):
        assert self.c(status=STATUS_OVERDUE, later=[]) == CAUSE_INACTIVE


# --- nearest / tolerance --------------------------------------------------------


class TestNearest:
    def test_before_and_after_split_on_target(self):
        target = NOW
        rows = [
            type("R", (), {"at": NOW - timedelta(minutes=10)})(),
            type("R", (), {"at": NOW - timedelta(minutes=2)})(),
            type("R", (), {"at": NOW + timedelta(minutes=5)})(),
        ]
        before, after = _nearest(rows, target, lambda r: r.at)
        assert before.at == NOW - timedelta(minutes=2)
        assert after.at == NOW + timedelta(minutes=5)


# --- due arithmetic + revisit detection (DB) ------------------------------------


class TestHorizonStatus:
    def test_young_token_all_horizons_not_due(self, session):
        seed_token(session, "young" + "A" * 20, born_h=0.05)  # 3 min old
        [tc] = coverages(session)
        for label in ("15m", "1h", "6h", "24h"):
            assert tc.horizons[label].status == STATUS_NOT_YET_DUE
            assert tc.horizons[label].cause == CAUSE_NOT_DUE

    def test_old_token_long_horizons_overdue(self, session):
        # 24h window is target ± 12h (50% tolerance): a 26h-old token is still
        # DUE; overdue needs birth > 24h + 12h = 36h ago.
        seed_token(session, "mid" + "A" * 22, born_h=26, ticks_min=[])
        seed_token(session, "old" + "A" * 22, born_h=40, ticks_min=[])
        by = {tc.token_address: tc for tc in coverages(session)}
        assert by["mid" + "A" * 22].horizons["24h"].status == STATUS_DUE
        assert by["old" + "A" * 22].horizons["24h"].status == STATUS_OVERDUE
        assert by["old" + "A" * 22].horizons["15m"].status == STATUS_OVERDUE

    def test_not_revisited_vs_join_failed(self, session):
        # A: measurable 24h data exists, never re-observed after due -> not_revisited
        seed_token(session, "aaa" + "A" * 22, born_h=26, ticks_min=[1440],
                   snaps_min=[0])
        # D: same, but a snapshot after due exists -> join_failed
        seed_token(session, "ddd" + "D" * 22, born_h=26, ticks_min=[1440],
                   snaps_min=[0, 1500])
        by_addr = {tc.token_address: tc for tc in coverages(session)}
        a = by_addr["aaa" + "A" * 22].horizons["24h"]
        d = by_addr["ddd" + "D" * 22].horizons["24h"]
        assert a.fresh_measurable and not a.stored_known
        assert a.revisited_after_due is False
        assert a.cause == CAUSE_NOT_REVISITED
        assert d.revisited_after_due is True
        assert d.cause == CAUSE_JOIN_FAILED

    def test_inactive_when_no_later_ticks(self, session):
        seed_token(session, "dead" + "A" * 21, born_h=26, ticks_min=[])
        [tc] = coverages(session)
        assert tc.horizons["24h"].cause == CAUSE_INACTIVE
        assert tc.horizons["24h"].later_rows_exist is False

    def test_matured_outcome_is_not_a_gap(self, session):
        seed_token(session, "mat" + "A" * 22, born_h=26, ticks_min=[1440],
                   stored={"survived_24h": True})
        [tc] = coverages(session)
        assert tc.horizons["24h"].stored_known is True
        assert tc.horizons["24h"].cause is None


# --- funnel ---------------------------------------------------------------------


class TestFunnel:
    def test_funnel_counts(self, session):
        seed_token(session, "A" * 25, born_h=26, ticks_min=[1440], snaps_min=[0])
        seed_token(session, "B" * 25, born_h=26, ticks_min=[])          # inactive
        seed_token(session, "C" * 25, born_h=0.05)                      # not due
        f = coverage_funnel(coverages(session), "24h")
        assert f["tokens_born"] == 3
        assert f["horizon_due"] == 2                # A, B (C not due)
        assert f["raw_market_data_available"] == 1  # only A has later ticks
        assert f["tick_within_tolerance"] == 1      # A's 1440 tick in window
        assert f["outcome_measurable"] == 0         # neither stored known
        assert f["provider_gap"] == 2


# --- selection starvation + shadow ----------------------------------------------


class TestSelection:
    def test_recent_first_starves_due_old_cohorts(self, session):
        seed_token(session, "young" + "Y" * 20, born_h=0.05)      # rank 0
        seed_token(session, "oldA" + "A" * 21, born_h=26, ticks_min=[1440])
        seed_token(session, "oldB" + "B" * 21, born_h=27, ticks_min=[1440])
        s = selection_analysis(coverages(session), limit=1)
        # both old tokens are due for 24h but rank >= 1 (below the limit)
        assert s["due_tokens_omitted_from_limit"]["24h"] == 2
        assert s["due_token_omission_rate"]["24h"] == 1.0
        assert s["recent_first_starves_old_cohorts"] is True

    def test_shadow_due_first_beats_recent_first(self, session):
        # young non-maturable token is newest; old maturable token is starved
        seed_token(session, "young" + "Y" * 20, born_h=0.05)
        seed_token(session, "oldA" + "A" * 21, born_h=26, ticks_min=[1440],
                   snaps_min=[0])
        sh = shadow_selection(coverages(session), limit=1)
        assert sh["total_maturable_available"]["24h"] == 1
        cur = sh["policies"]["current_recent_selection"]
        due = sh["policies"]["due_horizon_first"]
        assert cur["expected_new_matures_total"] == 0   # picks the young token
        assert due["expected_new_matures_total"] == 1   # picks the old maturable one


# --- report + no persistence ----------------------------------------------------


class TestReport:
    def test_report_structure_and_verdict(self, session):
        seed_token(session, "A" * 25, born_h=26, ticks_min=[1440], snaps_min=[0])
        seed_token(session, "B" * 25, born_h=26, ticks_min=[])   # inactive/upstream
        r = build_coverage_report(session, hours=200, top=5, limit=1)
        assert r["tokens_analyzed"] == 2
        assert set(r["coverage_funnel"]) == {"15m", "1h", "6h", "24h"}
        assert "24h" in r["bottleneck_verdict"]
        assert r["shadow_selection"]["limit"] == 1
        assert "never advice" in r["disclaimer"]

    def test_empty_report(self, session):
        r = build_coverage_report(session)
        assert r["tokens_analyzed"] == 0

    def test_persists_nothing(self, session):
        seed_token(session, "A" * 25, born_h=26, ticks_min=[1440])
        session.flush()
        before = {
            m.__tablename__: session.query(m).count()
            for m in (CryptoToken, CryptoTokenBirthEvent, CryptoPriceTick,
                      CryptoTokenSurvivalOutcome, CryptoTokenLifecycleSnapshot,
                      CryptoTokenRiskAssessment)
        }
        build_coverage_report(session, hours=200)
        assert not session.new
        assert not session.dirty
        after = {
            m.__tablename__: session.query(m).count()
            for m in (CryptoToken, CryptoTokenBirthEvent, CryptoPriceTick,
                      CryptoTokenSurvivalOutcome, CryptoTokenLifecycleSnapshot,
                      CryptoTokenRiskAssessment)
        }
        assert before == after


# --- CLI ------------------------------------------------------------------------


class TestCLI:
    async def test_cli_renders(self, session, capsys):
        seed_token(session, "A" * 25, born_h=26, ticks_min=[1440], snaps_min=[0])
        n = await cli.crypto_tape_coverage_report(session=session, hours=200)
        out = capsys.readouterr().out
        assert n == 1
        assert "coverage funnel" in out
        assert "gap causes by horizon" in out
        assert "bottleneck verdict" in out
        assert "shadow selection" in out
        assert "diagnostic only, never advice" in out

    def test_main_wires_command(self):
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
        assert "crypto-tape-coverage-report" in actions


# --- safety ---------------------------------------------------------------------


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "crypto_coverage.py").read_text()
        toks = [
            t.string.lower()
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "submit_order", "create_order", "wallet",
                    "private_key", "recommend", "execute_trade", "swap",
                    "jupiter", "sign_transaction", "pnl", "profit", "buy",
                    "sell", "arbitrage", "entry_price", "stop_loss"):
            assert bad not in code, bad

    def test_no_direct_network_imports(self):
        src = (REPO / "app" / "services" / "crypto_coverage.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket",
                    "adapters"):
            assert net not in src

    async def test_no_network_even_with_broken_httpx(self, session, monkeypatch):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("coverage must not construct an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        seed_token(session, "A" * 25, born_h=26, ticks_min=[1440])
        r = build_coverage_report(session, hours=200)
        assert r["tokens_analyzed"] == 1
