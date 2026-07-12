"""CRYPTO-TAPE-001 tests: read-only Solana memecoin lifecycle tape.

Covers: migration 0026 round trip, dry-run persists nothing, real runs
persist ONLY lifecycle tape rows (never signals, never MarketOps state),
birth-event field mapping + one-per-token dedupe, snapshot consolidation +
provider coverage + missing_info honesty, actor observations (public
addresses only, honest placeholders), deterministic survival labels per
horizon (mature/immature/gap), provider-gap handling, report rendering, no
external calls (module imports no network client), and no forbidden
trading/execution vocabulary. In-memory SQLite; no network anywhere.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app import cli
from app.db import PROJECT_ROOT, Base, run_migrations
from app.models import (
    CryptoOpportunitySignal,
    CryptoPair,
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenActorObservation,
    CryptoTokenBirthEvent,
    CryptoTokenDiscoveryEvent,
    CryptoTokenLifecycleRun,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenRiskAssessment,
    CryptoTokenSurvivalOutcome,
    MarketOpsRun,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)
from app.services.crypto_tape import (
    DEAD_VOLUME_24H_USD,
    SURVIVAL_LIQUIDITY_FRACTION,
    CryptoLifecycleTapeRecorder,
    CryptoTapeConfig,
    TokenSources,
    build_tape_report,
)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
TOKEN = "So1TapeTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PAIR = "So1TapePairAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def recorder() -> CryptoLifecycleTapeRecorder:
    return CryptoLifecycleTapeRecorder(config=CryptoTapeConfig(chain="solana"))


# --- seed helpers (rows the existing lanes would have persisted) --------------


def add_token(session, address=TOKEN, *, first_seen, symbol="TKN", name="Tape Token",
              metadata=None):
    token = CryptoToken(
        chain="solana", token_address=address, symbol=symbol, name=name,
        token_metadata=metadata or {"description": "d", "profile_url": "u"},
        first_seen_at=first_seen, last_seen_at=first_seen, created_at=first_seen,
    )
    session.add(token)
    session.flush()
    return token


def add_pair(session, address=TOKEN, pair_address=PAIR, *, dex="raydium",
             pair_created_at=None, first_seen=None):
    row = CryptoPair(
        chain="solana", pair_address=pair_address, base_token_address=address,
        dex_id=dex, pair_created_at=pair_created_at,
        first_seen_at=first_seen or NOW, created_at=first_seen or NOW,
    )
    session.add(row)
    session.flush()
    return row


def add_tick(session, address=TOKEN, pair_address=PAIR, *, at, price=0.001,
             liq=10_000.0, vol24=5_000.0, vol5m=100.0, mcap=100_000.0,
             dex="raydium"):
    tick = CryptoPriceTick(
        chain="solana", token_address=address, pair_address=pair_address,
        observed_at=at, price_usd=price, liquidity_usd=liq,
        volume_5m_usd=vol5m, volume_24h_usd=vol24, market_cap=mcap, fdv=mcap,
        raw_payload={"boosts_active": 0, "dex_id": dex}, created_at=at,
    )
    session.add(tick)
    session.flush()
    return tick


def add_event(session, address=TOKEN, *, at, event_type="profile",
              source="dexscreener", raw=None):
    session.add(CryptoTokenDiscoveryEvent(
        chain="solana", token_address=address, source=source,
        event_type=event_type, observed_at=at,
        raw_payload=raw or {"tokenAddress": address}, created_at=at,
    ))
    session.flush()


def add_assessment(session, address=TOKEN, *, at, provider="risk-engine",
                   flags=None, raw=None, provider_names=("goplus",),
                   level="low", composite=0.2, reasons=None):
    row = CryptoTokenRiskAssessment(
        chain="solana", token_address=address, provider=provider,
        risk_score=composite, risk_level=level,
        flags=flags if flags is not None else {
            "top10_holder_pct": 22.0, "sniper_pct": 3.0, "insider_pct": 2.0,
            "bundler_pct": 4.0, "holder_count": 150,
            "mint_authority_enabled": False, "freeze_authority_enabled": False,
        },
        raw_payload=raw, composite_risk_score=composite, composite_risk_level=level,
        risk_reasons=reasons or [], provider_names=list(provider_names),
        created_at=at,
    )
    session.add(row)
    session.flush()
    return row


def add_attention(session, address=TOKEN, *, at, att=0.5, boost=10.0):
    row = MemeAttentionSnapshot(
        chain="solana", token_address=address, symbol="TKN",
        attention_score=att, boost_amount=boost, has_social=True,
        social_links_count=3, observed_at=at, created_at=at,
    )
    session.add(row)
    session.flush()
    return row


def seed_full_token(session, *, first_seen):
    """A token as CRYPTO-001 + MEME-NEWS would have left it in the DB."""
    token = add_token(session, first_seen=first_seen)
    add_pair(session, pair_created_at=first_seen - timedelta(minutes=5),
             first_seen=first_seen)
    add_event(session, at=first_seen)
    add_tick(session, at=first_seen)
    add_assessment(session, at=first_seen + timedelta(minutes=10))
    add_attention(session, at=first_seen + timedelta(minutes=10))
    session.add(MemeCatalystEvent(
        source="dexscreener", subject_type="token", subject_ref=TOKEN,
        catalyst_type="boost", observed_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=1),
    ))
    session.flush()
    return token


def tape_counts(session) -> dict:
    return {
        "runs": session.query(CryptoTokenLifecycleRun).count(),
        "births": session.query(CryptoTokenBirthEvent).count(),
        "snapshots": session.query(CryptoTokenLifecycleSnapshot).count(),
        "actors": session.query(CryptoTokenActorObservation).count(),
        "outcomes": session.query(CryptoTokenSurvivalOutcome).count(),
    }


# --- migration -----------------------------------------------------------------


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


class TestMigration0026:
    def test_up_down_up_round_trip(self, tmp_path):
        url = f"sqlite:///{tmp_path}/tape.db"
        run_migrations(url)
        tape_tables = {
            "crypto_token_lifecycle_runs", "crypto_token_birth_events",
            "crypto_token_lifecycle_snapshots", "crypto_token_actor_observations",
            "crypto_token_survival_outcomes",
        }
        assert tape_tables <= _tables(url)
        command.downgrade(_config(url), "0025")
        assert not (tape_tables & _tables(url))
        command.upgrade(_config(url), "head")
        assert tape_tables <= _tables(url)


# --- dry run persists nothing ----------------------------------------------------


class TestDryRun:
    def test_dry_run_persists_nothing(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = recorder().run_once(session, dry_run=True)
        session.rollback()  # nothing should have been added anyway
        assert r["status"] == "dry_run"
        assert r["tokens_considered"] == 1
        assert r["birth_events_created"] == 1
        assert r["snapshots_created"] == 1
        assert all(count == 0 for count in tape_counts(session).values())

    def test_dry_run_reports_zero_external_calls(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = recorder().run_once(session, dry_run=True)
        assert r["external_calls"] == 0


# --- real run persists tape rows only ---------------------------------------------


class TestPersistence:
    def test_run_persists_all_tape_row_kinds(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = recorder().run_once(session)
        counts = tape_counts(session)
        assert r["status"] == "ok"
        assert counts == {
            "runs": 1, "births": 1, "snapshots": 1, "actors": 1, "outcomes": 1,
        }
        run = session.query(CryptoTokenLifecycleRun).one()
        assert run.status == "ok"
        assert run.tokens_considered == 1
        assert run.birth_events_created == 1
        assert run.snapshots_created == 1
        assert run.actor_observations_created == 1
        assert run.outcomes_updated == 1
        assert r["tape_run_id"] == run.id

    def test_run_touches_no_signal_or_marketops_rows(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        assert session.query(CryptoOpportunitySignal).count() == 0
        assert session.query(MarketOpsRun).count() == 0

    def test_second_run_does_not_duplicate_birth_events(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        recorder().run_once(session)
        counts = tape_counts(session)
        assert counts["births"] == 1          # one birth per token, ever
        assert counts["snapshots"] == 2       # one lifecycle snapshot per run
        assert counts["outcomes"] == 1        # upserted, not duplicated
        second = session.query(CryptoTokenLifecycleRun).order_by(
            CryptoTokenLifecycleRun.id.desc()
        ).first()
        assert second.birth_events_created == 0

    def test_limit_and_window_bound_the_universe(self, session):
        for i in range(3):
            addr = f"tok{i}" + "A" * 30
            add_token(session, addr, first_seen=NOW - timedelta(hours=1 + i))
            add_tick(session, addr, f"pair{i}", at=NOW - timedelta(hours=1 + i))
        add_token(session, "old" + "A" * 30, first_seen=NOW - timedelta(hours=100))
        r = recorder().run_once(session, limit=2, hours=48)
        assert r["tokens_considered"] == 2  # limit honored, old token outside window


# --- birth event ------------------------------------------------------------------


class TestBirthEvent:
    def test_field_mapping_and_provenance(self, session):
        first_seen = NOW - timedelta(hours=2)
        seed_full_token(session, first_seen=first_seen)
        recorder().run_once(session)
        birth = session.query(CryptoTokenBirthEvent).one()
        assert birth.token_address == TOKEN
        assert birth.chain == "solana"
        assert birth.launch_source == "dexscreener:profile"
        assert birth.first_pair_address == PAIR
        assert birth.first_dex_id == "raydium"
        assert birth.bonding_curve_state == "amm_pool"
        assert birth.initial_liquidity_usd == 10_000.0
        assert birth.initial_price_usd == 0.001
        assert birth.mint_authority_enabled is False
        assert birth.freeze_authority_enabled is False
        assert birth.metadata_links["description"] == "d"
        assert birth.provenance["first_tick_id"] is not None
        assert birth.provenance["discovery_event_ids"]
        # creator/deployer is not exposed by current sources — honest absence
        assert birth.creator_address is None
        assert "creator_address" in (birth.missing_info or [])

    def test_launchpad_dex_marks_bonding_curve(self, session):
        first_seen = NOW - timedelta(hours=2)
        add_token(session, first_seen=first_seen)
        add_pair(session, dex="pumpfun", pair_created_at=first_seen,
                 first_seen=first_seen)
        add_tick(session, at=first_seen, dex="pumpfun")
        recorder().run_once(session)
        birth = session.query(CryptoTokenBirthEvent).one()
        assert birth.bonding_curve_state == "launchpad_curve"

    def test_sparse_token_names_gaps_instead_of_guessing(self, session):
        add_token(session, first_seen=NOW - timedelta(hours=2), metadata={})
        recorder().run_once(session)
        birth = session.query(CryptoTokenBirthEvent).one()
        assert birth.launch_source is None
        assert birth.initial_liquidity_usd is None
        missing = set(birth.missing_info or [])
        assert {"launch_source", "pair", "initial_market_state",
                "mint_authority", "freeze_authority"} <= missing


# --- lifecycle snapshot -----------------------------------------------------------


class TestSnapshot:
    def test_consolidates_market_risk_and_social_sources(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        snap = session.query(CryptoTokenLifecycleSnapshot).one()
        assert snap.price_usd == 0.001
        assert snap.liquidity_usd == 10_000.0
        assert snap.top10_holder_pct == 22.0
        assert snap.sniper_pct == 3.0
        assert snap.holder_count == 150
        assert snap.risk_level == "low"
        assert snap.attention_score == 0.5
        assert snap.catalyst_count_24h == 1
        assert snap.pair_count == 1
        assert snap.single_venue is True
        assert snap.volume_to_liquidity_24h == 0.5
        assert snap.source_tick_id is not None
        assert snap.source_risk_assessment_id is not None
        assert snap.source_attention_snapshot_id is not None
        assert "price_tick" in snap.provider_coverage
        assert "risk:risk-engine" in snap.provider_coverage
        assert "risk:goplus" in snap.provider_coverage
        assert "attention" in snap.provider_coverage
        assert snap.birth_event_id is not None

    def test_missing_sources_named_not_fabricated(self, session):
        add_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        snap = session.query(CryptoTokenLifecycleSnapshot).one()
        assert snap.price_usd is None
        assert snap.risk_level is None
        missing = set(snap.missing_info or [])
        assert {"market_state", "risk_assessment", "attention_snapshot",
                "top10_holder_pct"} <= missing


# --- actor observations -----------------------------------------------------------


class TestActorObservations:
    def test_holder_distribution_and_placeholders(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        actor = session.query(CryptoTokenActorObservation).one()
        assert actor.holder_distribution["top10_holder_pct"] == 22.0
        assert actor.holder_distribution["holder_count"] == 150
        # placeholders stay empty until a source legitimately provides them
        assert actor.first_buyer_addresses is None
        assert actor.repeated_cohort_ref is None
        assert actor.known_creator_cluster_ref is None
        assert "first_buyer_addresses" in actor.missing_info
        assert actor.observation_sources == ["crypto_token_risk_assessments"]

    def test_cohort_counts_from_persisted_provider_payload(self, session):
        first_seen = NOW - timedelta(hours=2)
        add_token(session, first_seen=first_seen)
        add_tick(session, at=first_seen)
        add_assessment(
            session, at=first_seen, provider="solana-tracker",
            provider_names=("solana-tracker",),
            raw={"snipers": {"count": 7}, "insiders": {"count": 2},
                 "bundlers": {"count": 4}},
        )
        recorder().run_once(session)
        actor = session.query(CryptoTokenActorObservation).one()
        assert actor.sniper_address_count == 7
        assert actor.insider_address_count == 2
        assert actor.bundler_address_count == 4


# --- survival labels --------------------------------------------------------------


def make_sources(token, ticks=(), assessments=(), pairs=()):
    return TokenSources(
        token=token, pairs=list(pairs), ticks=list(ticks),
        assessments=list(assessments), discovery_events=[], attention=None,
        catalyst_count_24h=0,
    )


class TestSurvivalLabels:
    def _birth(self, anchor, *, liq=10_000.0, dex="raydium"):
        return CryptoTokenBirthEvent(
            chain="solana", token_address=TOKEN, observed_at=anchor,
            first_evidence_at=anchor, initial_liquidity_usd=liq,
            first_dex_id=dex, created_at=anchor,
        )

    def _tick(self, at, liq, vol24=5_000.0):
        return CryptoPriceTick(
            chain="solana", token_address=TOKEN, pair_address=PAIR,
            observed_at=at, liquidity_usd=liq, volume_24h_usd=vol24,
            created_at=at,
        )

    def test_survived_horizons_true_when_liquidity_holds(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        ticks = [
            self._tick(anchor + timedelta(minutes=m), 9_000.0)
            for m in (15, 60, 360, 1440)
        ]
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token, ticks=ticks), NOW
        )
        labels = out["labels"]
        assert labels["survived_15m"] is True
        assert labels["survived_1h"] is True
        assert labels["survived_6h"] is True
        assert labels["survived_24h"] is True
        assert labels["liquidity_removed"] is False
        assert labels["dead_volume"] is False
        assert out["final"] is False  # 24h*1.5 window not yet closed at 26h

    def test_liquidity_collapse_fails_horizons_and_flags_removed(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        threshold = SURVIVAL_LIQUIDITY_FRACTION * 10_000.0
        ticks = [
            self._tick(anchor + timedelta(minutes=15), 9_000.0),
            self._tick(anchor + timedelta(minutes=60), threshold - 1),
        ]
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token, ticks=ticks), NOW
        )
        labels = out["labels"]
        assert labels["survived_15m"] is True
        assert labels["survived_1h"] is False
        assert labels["liquidity_removed"] is True

    def test_immature_horizon_stays_none(self, session):
        anchor = NOW - timedelta(minutes=20)
        token = add_token(session, first_seen=anchor)
        ticks = [self._tick(anchor + timedelta(minutes=15), 9_000.0)]
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token, ticks=ticks), NOW
        )
        labels = out["labels"]
        assert labels["survived_15m"] is True
        assert labels["survived_1h"] is None
        assert out["details"]["horizons"]["1h"] == "not_yet_mature"
        assert out["final"] is False

    def test_no_observation_in_window_is_a_gap_not_a_guess(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        # only one tick shortly after birth: 6h/24h windows have no observation
        ticks = [self._tick(anchor + timedelta(minutes=15), 9_000.0)]
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token, ticks=ticks), NOW
        )
        labels = out["labels"]
        assert labels["survived_6h"] is None
        assert labels["survived_24h"] is None
        assert labels["provider_gap"] is True
        assert "no_tick_at_6h" in out["details"]["gap_reasons"]

    def test_dead_volume_after_six_hours(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        ticks = [
            self._tick(anchor + timedelta(minutes=15), 9_000.0),
            self._tick(anchor + timedelta(hours=7), 9_000.0,
                       vol24=DEAD_VOLUME_24H_USD - 1),
        ]
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token, ticks=ticks), NOW
        )
        assert out["labels"]["dead_volume"] is True

    def test_severe_risk_from_post_birth_assessment(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        severe = CryptoTokenRiskAssessment(
            chain="solana", token_address=TOKEN, provider="risk-engine",
            composite_risk_level="severe", risk_level="severe",
            provider_names=["goplus"], created_at=anchor + timedelta(hours=1),
        )
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token, assessments=[severe]), NOW
        )
        assert out["labels"]["severe_risk"] is True

    def test_graduation_from_launchpad_to_amm(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        pairs = [
            CryptoPair(chain="solana", pair_address="p1",
                       base_token_address=TOKEN, dex_id="pumpfun",
                       first_seen_at=anchor, created_at=anchor),
            CryptoPair(chain="solana", pair_address="p2",
                       base_token_address=TOKEN, dex_id="raydium",
                       first_seen_at=anchor + timedelta(hours=2),
                       created_at=anchor + timedelta(hours=2)),
        ]
        out = recorder().compute_survival(
            self._birth(anchor, dex="pumpfun"), make_sources(token, pairs=pairs), NOW
        )
        assert out["labels"]["graduated_or_migrated"] is True

    def test_amm_born_token_has_no_graduation_label(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        out = recorder().compute_survival(
            self._birth(anchor, dex="raydium"), make_sources(token), NOW
        )
        assert out["labels"]["graduated_or_migrated"] is None

    def test_final_after_last_horizon_window_closes(self, session):
        anchor = NOW - timedelta(hours=37)  # past 24h * (1 + 0.5) = 36h
        token = add_token(session, first_seen=anchor)
        out = recorder().compute_survival(
            self._birth(anchor), make_sources(token), NOW
        )
        assert out["final"] is True

    def test_final_outcome_not_overwritten_by_later_runs(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        outcome = session.query(CryptoTokenSurvivalOutcome).one()
        outcome.final = True
        outcome.survived_24h = True
        session.flush()
        recorder().run_once(session)
        refreshed = session.query(CryptoTokenSurvivalOutcome).one()
        assert refreshed.survived_24h is True  # untouched once final


class TestProviderGap:
    def test_heuristic_only_risk_counts_as_provider_gap(self, session):
        anchor = NOW - timedelta(hours=26)
        token = add_token(session, first_seen=anchor)
        heuristic_only = CryptoTokenRiskAssessment(
            chain="solana", token_address=TOKEN, provider="risk-engine",
            composite_risk_level="low", provider_names=[],
            created_at=anchor + timedelta(hours=1),
        )
        ticks = [
            CryptoPriceTick(
                chain="solana", token_address=TOKEN, pair_address=PAIR,
                observed_at=anchor + timedelta(minutes=m),
                liquidity_usd=9_000.0, volume_24h_usd=5_000.0,
                created_at=anchor + timedelta(minutes=m),
            )
            for m in (15, 60, 360, 1440)
        ]
        out = recorder().compute_survival(
            CryptoTokenBirthEvent(
                chain="solana", token_address=TOKEN, observed_at=anchor,
                first_evidence_at=anchor, initial_liquidity_usd=10_000.0,
                first_dex_id="raydium", created_at=anchor,
            ),
            make_sources(token, ticks=ticks, assessments=[heuristic_only]),
            NOW,
        )
        assert out["labels"]["provider_gap"] is True
        assert "no_provider_backed_risk_read" in out["details"]["gap_reasons"]

    def test_provider_backed_clean_token_has_no_gap(self, session):
        first_seen = NOW - timedelta(hours=26)
        add_token(session, first_seen=first_seen)
        add_pair(session, pair_created_at=first_seen, first_seen=first_seen)
        add_event(session, at=first_seen)
        for minutes in (0, 15, 60, 360, 1440):
            add_tick(session, at=first_seen + timedelta(minutes=minutes))
        add_assessment(session, at=first_seen + timedelta(minutes=5))
        recorder().run_once(session, hours=48)
        outcome = session.query(CryptoTokenSurvivalOutcome).one()
        assert outcome.provider_gap is False
        assert outcome.survived_24h is True


# --- report -----------------------------------------------------------------------


class TestReport:
    def test_report_after_run(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        recorder().run_once(session)
        r = build_tape_report(session, hours=24, top=5)
        assert r["tape_runs"] == 1
        assert r["tokens_observed"] == 1
        assert r["birth_events_in_window"] == 1
        assert r["snapshots_recorded"] == 1
        assert r["actor_observations_recorded"] == 1
        assert r["outcomes_computed"] == 1
        assert r["provider_coverage_mix"]["price_tick"] == 1
        assert r["risk_level_mix"]["low"] == 1
        assert "survived_24h" in r["survival_labels"]
        assert r["actor_pattern_examples"][0]["top10_holder_pct"] == 22.0
        assert "never PnL" in r["note"]
        assert "never advice" in r["disclaimer"]
        assert "no EV" in r["disclaimer"]

    def test_empty_report(self, session):
        r = build_tape_report(session)
        assert r["tape_runs"] == 0
        assert r["snapshots_recorded"] == 0
        assert r["db_impact_rows"] == 0


# --- CLI --------------------------------------------------------------------------


class TestCLI:
    async def test_run_once_cli_renders(self, session, capsys):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        n = await cli.crypto_tape_run_once(session=session, dry_run=True)
        out = capsys.readouterr().out
        assert n == 1
        assert "never advice" in out
        assert "status=dry_run" in out
        assert "external_calls=0" in out

    async def test_report_cli_renders(self, session, capsys):
        n = await cli.crypto_tape_report(session=session)
        out = capsys.readouterr().out
        assert n == 0
        assert "research infrastructure only" in out
        assert "disclaimer:" in out

    def test_main_wires_commands(self):
        import argparse

        parser_holder = {}
        original = argparse.ArgumentParser.parse_args

        def fake_parse(self, *a, **k):
            parser_holder["parser"] = self
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = fake_parse
        try:
            with pytest.raises(SystemExit):
                cli.main([])
        finally:
            argparse.ArgumentParser.parse_args = original
        actions = parser_holder["parser"]._subparsers._group_actions[0].choices
        assert "crypto-tape-run-once" in actions
        assert "crypto-tape-report" in actions


# --- safety -----------------------------------------------------------------------


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "crypto_tape.py").read_text()
        toks = [
            t.string.lower()
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "submit_order", "create_order", "wallet",
                    "private_key", "recommend", "execute_trade", "swap",
                    "jupiter", "sign_transaction", "pnl", "profit",
                    "entry_price", "take_profit", "stop_loss"):
            assert bad not in code, bad

    def test_no_direct_network_imports(self):
        src = (REPO / "app" / "services" / "crypto_tape.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket",
                    "adapters"):
            assert net not in src

    def test_run_makes_no_network_calls_even_with_broken_httpx(self, session, monkeypatch):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("crypto tape must not construct an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = recorder().run_once(session)
        assert r["status"] == "ok"
