"""RAW-PAYLOAD-RECLAMATION-001 — bounded historical raw-payload reclamation.

This is the one irreversible step in the raw-payload sequence: a reclaimed body
is gone, and the only recovery is a backup restore. These tests are written
against that asymmetry — most of them assert that something is *refused*, not
that something works.

Everything runs against disposable SQLite databases populated with
representative multi-KiB JSON payloads. No production row is read or written.
"""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import Settings
from app.db import Base
from app.models import (
    MarketDetailEnrichment,
    MarketOpsRun,
    MarketPriceTick,
    MarketSnapshot,
    OpportunitySignal,
    WatcherRun,
)
from app.services import raw_payload_policy as rp
from app.services import raw_payload_reclamation as rr

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 4, 5, 0, 0, tzinfo=timezone.utc)
BACKUP_AT = (NOW - timedelta(hours=3)).isoformat()

BODY = {
    "ticker": "GEN-MKT-1", "yes_bid": 41, "yes_ask": 44, "volume_24h": 1234,
    "rules_primary": "x" * 1800, "unmapped_field": "kept for debugging",
    "nested": {"a": [1, 2, 3], "b": "y" * 200},
}


def healthy_backup(monkeypatch, ok=True, age=3600.0):
    """Pin the backup precondition without touching a real backup root."""
    monkeypatch.setattr(
        rr, "backup_precondition",
        lambda *a, **k: (ok, "pinned test backup", BACKUP_AT if ok else None),
    )


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reclaim.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed_snapshots(session, *, old=40, recent=5, suppressed=3, null=2, tiny=2,
                   mid=0):
    """A representative mix: old full bodies (eligible), recent full bodies
    (preserved), already-suppressed, genuine NULLs, and bodies too small to be
    worth suppressing."""
    def add(captured_at, payload):
        session.add(MarketSnapshot(
            market_id=1, yes_bid=41, yes_ask=44, score=0.5,
            captured_at=captured_at, raw_payload=payload,
        ))
    for i in range(old):
        add(NOW - timedelta(days=30 + i), dict(BODY, seq=i))
    for i in range(mid):
        # between the 7-day age floor and a 20-day backup floor
        add(NOW - timedelta(days=10), dict(BODY, seq=500 + i))
    for i in range(recent):
        add(NOW - timedelta(hours=i), dict(BODY, seq=1000 + i))
    for i in range(suppressed):
        add(NOW - timedelta(days=30), rp.provenance_envelope(
            BODY, source="kalshi_rest", mode="none"))
    for _ in range(null):
        add(NOW - timedelta(days=30), None)
    for i in range(tiny):
        add(NOW - timedelta(days=30), {"k": i})
    session.commit()


def snapshot_state(session):
    rows = session.execute(
        select(MarketSnapshot).order_by(MarketSnapshot.id)
    ).scalars().all()
    return [
        (r.id, r.market_id, r.yes_bid, r.yes_ask, r.score, r.captured_at,
         rp.is_suppressed(r.raw_payload), r.raw_payload is None)
        for r in rows
    ]


# --------------------------------------------------------------------------
# 3-6: only reviewed targets are selectable
# --------------------------------------------------------------------------

class TestTargetRegistry:
    def test_only_registered_targets_resolve(self):
        for name in rr.RECLAMATION_TARGETS:
            assert rr.resolve_target(name).qualified == name

    @pytest.mark.parametrize("name", [
        "market_price_ticks.raw_payload",           # excluded: self-expiring
        "crypto_token_risk_assessments.raw_payload",  # pinned
        "market_research_packets.raw_response",       # pinned
        "crypto_price_ticks.raw_payload",             # pinned
        "crypto_horizon_observations.raw_payload",    # pinned
        "market_outcomes.raw_payload",                # ungoverned
        "markets.title", "totally_unknown.column", "", "market_snapshots",
    ])
    def test_everything_else_is_rejected(self, name):
        with pytest.raises(LookupError):
            rr.resolve_target(name)

    def test_no_pinned_column_is_a_target(self):
        assert not (set(rr.RECLAMATION_TARGETS) & set(rp.PINNED_FULL))

    def test_every_target_is_a_governed_column(self):
        """Reclaiming a column the live writer does not suppress would leave the
        table permanently mixed — old rows enveloped, new rows full bodies."""
        for name in rr.RECLAMATION_TARGETS:
            assert name in rp.GOVERNED_COLUMNS, name

    def test_self_expiring_column_is_excluded_with_a_reason(self):
        assert "market_price_ticks.raw_payload" in rr.EXCLUDED_FROM_RECLAMATION
        why = rr.EXCLUDED_FROM_RECLAMATION["market_price_ticks.raw_payload"]
        assert "2-day" in why or "2 day" in why
        assert "retention" in why

    def test_every_target_resolves_against_the_real_schema(self):
        for name, target in rr.RECLAMATION_TARGETS.items():
            table = Base.metadata.tables.get(target.table)
            assert table is not None, name
            assert target.column in table.c, name
            assert target.timestamp_column in table.c, name

    def test_there_is_no_arbitrary_table_interface(self):
        """The CLI takes a registry NAME, never a table/column pair."""
        from app.cli import build_parser

        args = build_parser().parse_args(
            ["raw-payload-reclaim", "market_snapshots.raw_payload"])
        assert args.target == "market_snapshots.raw_payload"
        for flag in ("--table", "--column", "--where", "--sql", "--all"):
            with pytest.raises(SystemExit):
                build_parser().parse_args(
                    ["raw-payload-reclaim", "x", flag, "y"])

    def test_every_target_carries_a_rationale(self):
        for name, target in rr.RECLAMATION_TARGETS.items():
            assert len(target.rationale) > 60, name
            assert target.preservation_days > 0, name


# --------------------------------------------------------------------------
# 7-9: preservation floors
# --------------------------------------------------------------------------

class TestPreservation:
    def test_recent_rows_are_never_eligible(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        plan = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=BACKUP_AT)
        assert plan.eligible_rows == 40
        assert plan.preserved_recent_rows == 5

    def test_the_backup_floor_wins_when_it_is_earlier(self, db, monkeypatch):
        """A row written after the newest verified backup has NO recovery path.
        That floor is not negotiable per-table."""
        healthy_backup(monkeypatch)
        seed_snapshots(db, mid=6)
        old_backup = (NOW - timedelta(days=20)).isoformat()
        # with a fresh backup the 6 mid-aged rows ARE eligible
        fresh = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=BACKUP_AT)
        assert fresh.eligible_rows == 46
        # with a 20-day-old backup they are NOT: nothing newer than the newest
        # backup may be reclaimed, because it has no recovery path
        plan = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=old_backup)
        assert plan.effective_cutoff == old_backup
        assert plan.eligible_rows == 40

    def test_the_age_floor_wins_when_it_is_earlier(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        plan = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=BACKUP_AT)
        assert plan.effective_cutoff == plan.cutoff  # the 7-day age floor
        assert plan.effective_cutoff < plan.backup_cutoff

    def test_already_suppressed_rows_are_excluded(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        plan = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=BACKUP_AT)
        assert plan.already_suppressed_rows == 3
        assert plan.eligible_rows == 40  # the 3 are not counted

    def test_null_payloads_are_excluded(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        plan = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=BACKUP_AT)
        # both SQL NULL and the JSON literal `null` count as absent
        assert plan.null_rows == 2

    def test_bodies_below_the_envelope_size_are_excluded(self, db, monkeypatch):
        """Replacing a 20-byte body with a 118-byte envelope makes the row
        bigger. The policy is monotone; so is reclamation."""
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        plan = rr.plan_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, backup_created_at=BACKUP_AT)
        assert plan.below_min_size_rows == 2

    def test_preservation_days_differ_per_table(self):
        """A single global cutoff for implementation convenience is exactly what
        the design forbids."""
        days = {t.qualified: t.preservation_days
                for t in rr.RECLAMATION_TARGETS.values()}
        assert len(set(days.values())) > 1
        assert days["crypto_token_discovery_events.raw_payload"] == 14


# --------------------------------------------------------------------------
# 1-2, 34-35: the dry run
# --------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_writes_nothing(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        before = snapshot_state(db)
        rr.build_report(db, now=NOW)
        rr.build_report(db, now=NOW)
        assert snapshot_state(db) == before

    def test_dry_run_makes_no_provider_call(self, db, monkeypatch):
        import httpx

        healthy_backup(monkeypatch)
        seed_snapshots(db)
        for client in (httpx.AsyncClient, httpx.Client):
            for attr in ("get", "post", "request"):
                monkeypatch.setattr(
                    client, attr,
                    lambda *a, **k: pytest.fail("provider call from a dry run"))
        report = rr.build_report(db, now=NOW)
        assert report.external_calls == 0
        assert report.persisted is False
        assert report.rows_changed == 0

    def test_dry_run_exposes_the_required_fields(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        data = rr.build_report(db, now=NOW).to_dict()
        plan = next(t for t in data["targets"]
                    if t["target"] == "market_snapshots.raw_payload")
        for key in ("table", "column", "effective_cutoff", "eligible_rows",
                    "eligible_bytes", "estimated_replacement_bytes",
                    "estimated_reclaimed_bytes", "preserved_recent_rows",
                    "already_suppressed_rows", "rationale"):
            assert key in plan, key
        assert data["physical_file_shrinks"] is False
        assert data["prints_payload_contents"] is False
        assert data["pinned"] == sorted(rp.PINNED_FULL)
        assert "market_price_ticks.raw_payload" in data["excluded"]

    def test_text_and_json_parity(self, db, monkeypatch, capsys):
        from app import cli

        healthy_backup(monkeypatch)
        seed_snapshots(db)
        text = cli.raw_payload_reclamation_report(fmt="text", session=db)
        capsys.readouterr()
        data = cli.raw_payload_reclamation_report(fmt="json", session=db)
        out = capsys.readouterr().out
        # cutoffs are computed from now() per call, so they differ by
        # microseconds between the two invocations; everything else must match
        def stable(d):
            out = {k: v for k, v in d.items() if k != "generated_at"}
            out["targets"] = [
                {k: v for k, v in t.items()
                 if k not in ("cutoff", "effective_cutoff")}
                for t in out["targets"]
            ]
            return out

        assert stable(text) == stable(data)
        assert len(json.loads(out)["targets"]) == len(data["targets"])

    def test_output_never_prints_a_payload(self, db, monkeypatch, capsys):
        from app import cli

        healthy_backup(monkeypatch)
        seed_snapshots(db)
        cli.raw_payload_reclamation_report(fmt="text", session=db)
        text_out = capsys.readouterr().out
        cli.raw_payload_reclamation_report(fmt="json", session=db)
        json_out = capsys.readouterr().out
        for out in (text_out, json_out):
            assert "unmapped_field" not in out
            assert "kept for debugging" not in out
            assert "rules_primary" not in out
            assert "x" * 50 not in out

    def test_output_is_secret_free(self, db, monkeypatch, capsys):
        from app import cli

        healthy_backup(monkeypatch)
        seed_snapshots(db)
        cli.raw_payload_reclamation_report(fmt="json", session=db)
        out = capsys.readouterr().out.lower()
        for banned in ("password", "secret", "api_key", "apikey", "bearer",
                       "authorization", "postgresql://", "sk-", "token="):
            assert banned not in out

    def test_output_discloses_the_physical_limitation(self, db, monkeypatch, capsys):
        from app import cli

        healthy_backup(monkeypatch)
        seed_snapshots(db)
        cli.raw_payload_reclamation_report(fmt="text", session=db)
        out = capsys.readouterr().out
        assert "does NOT" in out and "shrink" in out
        assert "SQLITE-COMPACT-COPY-001" in out
        assert "irreversible" in out
        assert "BACKUP PREREQUISITE" in out

    def test_reclaim_command_without_confirm_is_a_dry_run(self, db, monkeypatch, capsys):
        from app import cli

        healthy_backup(monkeypatch)
        seed_snapshots(db)
        before = snapshot_state(db)
        data = cli.raw_payload_reclaim(
            "market_snapshots.raw_payload", confirm=False, session=db)
        capsys.readouterr()
        assert data["mode"] == "dry_run"
        assert snapshot_state(db) == before

    def test_dry_run_flag_overrides_confirm(self):
        from app.cli import build_parser

        args = build_parser().parse_args(
            ["raw-payload-reclaim", "market_snapshots.raw_payload",
             "--confirm", "--dry-run"])
        assert args.confirm and args.dry_run
        assert (args.confirm and not args.dry_run) is False


# --------------------------------------------------------------------------
# 10-13: the canonical replacement contract
# --------------------------------------------------------------------------

class TestReplacementContract:
    def test_uses_the_same_envelope_as_the_live_writer(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=5, recent=0, suppressed=0, null=0, tiny=0)
        rr.reclaim_target(db, rr.resolve_target("market_snapshots.raw_payload"),
                          now=NOW)
        row = db.execute(
            select(MarketSnapshot).order_by(MarketSnapshot.id)).scalars().first()
        assert rp.is_suppressed(row.raw_payload)
        assert set(row.raw_payload) == {
            "raw_payload_suppressed", "mode", "source", "bytes", "digest"}
        assert row.raw_payload["mode"] == "none"
        assert row.raw_payload["source"] == "kalshi_rest"

    def test_original_byte_count_and_digest_are_preserved(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        payload = dict(BODY, seq=7)
        db.add(MarketSnapshot(market_id=1, yes_bid=1, yes_ask=2, score=0.1,
                              captured_at=NOW - timedelta(days=30),
                              raw_payload=payload))
        db.commit()
        rr.reclaim_target(db, rr.resolve_target("market_snapshots.raw_payload"),
                          now=NOW)
        row = db.execute(select(MarketSnapshot)).scalars().one()
        assert row.raw_payload["bytes"] == rp.payload_bytes(payload)
        assert row.raw_payload["digest"] == rp.payload_digest(payload)

    def test_no_second_historical_only_representation(self):
        """The envelope must come from the policy module, not be rebuilt here."""
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text()
        assert "provenance_envelope" in source
        assert "raw_payload_suppressed\":" not in source
        assert source.count("SUPPRESSED_MARKER") >= 1

    def test_normalized_fields_are_unchanged(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        before = {
            r[0]: r[1:6] for r in snapshot_state(db)
        }
        rr.reclaim_target(db, rr.resolve_target("market_snapshots.raw_payload"),
                          now=NOW)
        after = {r[0]: r[1:6] for r in snapshot_state(db)}
        assert before == after

    def test_no_row_is_deleted(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        before = db.execute(select(func.count()).select_from(MarketSnapshot)).scalar()
        rr.reclaim_target(db, rr.resolve_target("market_snapshots.raw_payload"),
                          now=NOW)
        after = db.execute(select(func.count()).select_from(MarketSnapshot)).scalar()
        assert before == after

    def test_other_tables_are_untouched(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        db.add(MarketPriceTick(market_ticker="T-1", observed_at=NOW,
                               created_at=NOW - timedelta(days=60),
                               yes_bid=1, yes_ask=2, raw_payload=dict(BODY)))
        db.add(OpportunitySignal(market_ticker="T-1",
                                 signal_type="price_move_threshold",
                                 signal_status="new", observed_at=NOW,
                                 created_at=NOW - timedelta(days=60),
                                 raw_payload=dict(BODY)))
        db.commit()
        rr.reclaim_target(db, rr.resolve_target("market_snapshots.raw_payload"),
                          now=NOW)
        tick = db.execute(select(MarketPriceTick)).scalars().one()
        signal = db.execute(select(OpportunitySignal)).scalars().one()
        assert not rp.is_suppressed(tick.raw_payload)
        assert not rp.is_suppressed(signal.raw_payload)

    def test_non_nullable_column_stays_valid(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        for i in range(4):
            db.add(MarketDetailEnrichment(
                market_ticker=f"E-{i}", created_at=NOW - timedelta(days=30),
                raw_market_detail=dict(BODY, seq=i),
                raw_event_detail=None, raw_series_detail=dict(BODY, s=i)))
        db.commit()
        rr.reclaim_target(
            db, rr.resolve_target("market_detail_enrichments.raw_market_detail"),
            now=NOW)
        rows = db.execute(select(MarketDetailEnrichment)).scalars().all()
        for row in rows:
            assert rp.is_suppressed(row.raw_market_detail)
            assert row.raw_event_detail is None       # genuine absence preserved
            assert not rp.is_suppressed(row.raw_series_detail)  # not this target


# --------------------------------------------------------------------------
# 14-23: batching, stop conditions, idempotency
# --------------------------------------------------------------------------

class TestBatchingAndStops:
    def test_batch_row_limit_is_enforced(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=25, recent=0, suppressed=0, null=0, tiny=0)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=10)
        assert result.rows_changed == 25
        assert result.batches_committed >= 3
        assert all(b["rows_selected"] <= 10 for b in result.batches)

    def test_batch_byte_limit_is_enforced(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=20, recent=0, suppressed=0, null=0, tiny=0)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=100, bytes_per_batch=6000)
        assert result.rows_changed == 20
        assert result.batches_committed >= 4
        for b in result.batches:
            assert b["bytes_before"] <= 6000 or b["rows_selected"] == 1

    def test_max_rows_stops_the_run(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=30, recent=0, suppressed=0, null=0, tiny=0)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=5, max_rows=12)
        assert result.rows_changed == 12
        assert result.stop_reason == "max_rows"

    def test_total_runtime_bound_stops_the_run(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=30, recent=0, suppressed=0, null=0, tiny=0)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=1, max_total_seconds=0.0)
        assert result.stop_reason == "max_total_seconds"
        assert result.rows_changed == 0

    def test_batch_duration_bound_stops_after_committing(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=30, recent=0, suppressed=0, null=0, tiny=0)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=5, max_batch_seconds=0.0)
        assert result.stop_reason == "max_batch_seconds"
        assert result.batches_committed == 1
        assert result.rows_changed == 5

    def test_repeated_run_is_idempotent(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db)
        first = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        state = snapshot_state(db)
        second = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        assert first.rows_changed == 40
        assert second.rows_changed == 0
        assert second.stop_reason == "completed"
        assert snapshot_state(db) == state

    def test_partial_run_is_resumable(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=30, recent=0, suppressed=0, null=0, tiny=0)
        first = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=5, max_rows=10)
        assert first.rows_changed == 10
        second = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        assert second.rows_changed == 20
        assert second.stop_reason == "completed"
        third = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        assert third.rows_changed == 0

    def test_lock_retry_exhaustion_stops_safely(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=10, recent=0, suppressed=0, null=0, tiny=0)
        before = snapshot_state(db)

        real_commit = type(db).commit
        monkeypatch.setattr(
            type(db), "commit",
            lambda self, *a, **k: (_ for _ in ()).throw(
                RuntimeError("database is locked")))
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=5)
        monkeypatch.setattr(type(db), "commit", real_commit)

        assert result.stop_reason == "lock_retry_exhausted"
        assert result.lock_retries > rr.LOCK_RETRY_ATTEMPTS
        assert result.rows_changed == 0
        assert snapshot_state(db) == before

    def test_batch_rollback_changes_nothing(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=10, recent=0, suppressed=0, null=0, tiny=0)
        before = snapshot_state(db)

        real_commit = type(db).commit
        monkeypatch.setattr(
            type(db), "commit",
            lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        monkeypatch.setattr(type(db), "commit", real_commit)

        assert result.stop_reason.startswith("batch_failed")
        assert snapshot_state(db) == before

    def test_verification_failure_halts(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=20, recent=0, suppressed=0, null=0, tiny=0)
        real_execute = type(db).execute
        calls = {"n": 0}

        def sabotage(self, stmt, *a, **k):
            # make the verification count report a row still holding a body
            rendered = str(stmt)
            if "count" in rendered.lower() and "IN" in rendered:
                calls["n"] += 1
                if calls["n"] == 1:
                    class R:
                        def scalar(self_inner):
                            return 3
                    return R()
            return real_execute(self, stmt, *a, **k)

        monkeypatch.setattr(type(db), "execute", sabotage)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=5)
        monkeypatch.setattr(type(db), "execute", real_execute)
        assert result.stop_reason == "verification_failed"
        assert result.verified is False

    def test_health_degradation_stops_between_batches(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=30, recent=0, suppressed=0, null=0, tiny=0)

        class FailingHealth(rr.HealthCheck):
            def __init__(self):
                self.calls = 0

            def check(self, session):
                self.calls += 1
                return None if self.calls <= 2 else "marketops_run_error: run 9"

        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=5, health=FailingHealth())
        assert result.stop_reason.startswith("marketops_run_error")
        assert 0 < result.rows_changed < 30

    def test_health_check_sees_marketops_and_watcher_errors(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        db.add(MarketOpsRun(status="error", started_at=NOW, created_at=NOW))
        db.commit()
        assert rr.HealthCheck().check(db).startswith("marketops_run_error")

        db.query(MarketOpsRun).delete()
        db.add(WatcherRun(status="error", started_at=NOW, created_at=NOW))
        db.commit()
        assert rr.HealthCheck().check(db).startswith("watcher_run_error")

    def test_a_row_that_changed_since_selection_is_not_overwritten(
        self, db, monkeypatch
    ):
        """The UPDATE re-checks that the row still holds a full body."""
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=3, recent=0, suppressed=0, null=0, tiny=0)
        target = rr.resolve_target("market_snapshots.raw_payload")
        # pre-suppress one row as if a writer had got there first
        row = db.execute(select(MarketSnapshot).order_by(
            MarketSnapshot.id)).scalars().first()
        row.raw_payload = rp.provenance_envelope(
            BODY, source="other", mode="none")
        db.commit()
        marker = row.raw_payload["source"]
        rr.reclaim_target(db, target, now=NOW)
        db.expire_all()
        reloaded = db.get(MarketSnapshot, row.id)
        assert reloaded.raw_payload["source"] == marker  # untouched


# --------------------------------------------------------------------------
# 24-25: backup prerequisite
# --------------------------------------------------------------------------

class TestBackupPrerequisite:
    def test_unhealthy_backup_blocks_the_run(self, db, monkeypatch):
        seed_snapshots(db)
        before = snapshot_state(db)
        monkeypatch.setattr(
            rr, "backup_precondition",
            lambda *a, **k: (False, "backup not healthy: no_committed_backup", None))
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        assert result.stop_reason.startswith("backup_precondition_failed")
        assert result.rows_changed == 0
        assert result.persisted is False
        assert snapshot_state(db) == before

    def test_stale_backup_blocks_the_run(self, monkeypatch):
        from app.services import backup_freshness as bf

        class R:
            healthy = True
            reason = "healthy"
            age_seconds = 13 * 3600
            newest_verified_at = BACKUP_AT
            newest_backup_filename = "b.db.gz"
            manifest_alembic_revision = "0027"

        monkeypatch.setattr(bf, "evaluate_backup_freshness", lambda *a, **k: R())
        ok, detail, _ = rr.backup_precondition(
            Settings(database_url="sqlite:///x.db"))
        assert ok is False
        assert "older than" in detail

    def test_fresh_verified_backup_passes(self, monkeypatch):
        from app.services import backup_freshness as bf

        class R:
            healthy = True
            reason = "healthy"
            age_seconds = 3600.0
            newest_verified_at = BACKUP_AT
            newest_backup_filename = "b.db.gz"
            manifest_alembic_revision = "0027"

        monkeypatch.setattr(bf, "evaluate_backup_freshness", lambda *a, **k: R())
        ok, detail, at = rr.backup_precondition(
            Settings(database_url="sqlite:///x.db"))
        assert ok is True and at == BACKUP_AT
        assert "0027" in detail

    def test_backup_requirement_is_12_hours(self):
        assert rr.REQUIRED_BACKUP_MAX_AGE_SECONDS == 12 * 60 * 60

    def test_health_check_rechecks_the_backup_between_batches(self, db, monkeypatch):
        monkeypatch.setattr(
            rr, "backup_precondition",
            lambda *a, **k: (False, "went stale mid-run", None))
        assert rr.HealthCheck().check(db).startswith("backup_precondition_failed")


# --------------------------------------------------------------------------
# 26-36: scope and safety
# --------------------------------------------------------------------------

class TestScopeAndSafety:
    def test_no_deletion_no_compaction_no_pragma(self):
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else fn.id if isinstance(fn, ast.Name) else None)
                assert name not in {"delete", "drop", "truncate", "remove",
                                    "rmtree", "unlink"}, name
        # code only — the module docstring necessarily names VACUUM and PRAGMA
        # in order to say it does neither
        code = source.split('"""', 2)[-1]
        code = "\n".join(
            line for line in code.split("\n") if not line.lstrip().startswith("#")
        )
        for banned in ("VACUUM", "PRAGMA", "journal_mode", "busy_timeout",
                       "isolation_level", "DELETE FROM", "DROP "):
            assert banned not in code, banned

    def test_only_the_target_column_is_written(self):
        """Every UPDATE must set exactly one column, and it must be the
        target's own."""
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text()
        assert ".values({target.column: envelope})" in source
        assert source.count(".values(") == 1

    def test_no_migration_added(self):
        for path in (REPO / "alembic/versions").glob("*.py"):
            text = path.read_text().lower()
            assert "reclamation" not in text
            assert "reclaim" not in text

    def test_no_provider_or_capital_surface(self):
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text().lower()
        for banned in ("httpx", "requests.", "aiohttp", "urllib", "socket",
                       "dexscreener_adapter", "expected_value", "kelly",
                       "position_siz", "paper_trad", "place_order", "wallet",
                       "trade_recommend", "execute_trade"):
            assert banned not in source, banned

    def test_no_schedule_or_timer_change(self):
        for path in (REPO / "infra/systemd/user").iterdir():
            assert "reclaim" not in path.name
            assert "reclaim" not in path.read_text().lower()
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text().lower()
        for banned in ("systemd", "systemctl", "crontab", "while true", "daemon"):
            assert banned not in source, banned

    def test_capture_mode_is_not_changed(self):
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text()
        assert "raw_payload_capture_mode" not in source
        assert "CAPTURE_MODE_NONE" in source  # only READ, to label the envelope

    def test_backup_and_retention_untouched(self):
        for path in ("app/services/backup.py", "app/services/backup_freshness.py",
                     "app/services/retention.py"):
            assert "raw_payload_reclamation" not in (REPO / path).read_text()

    def test_reclamation_never_logs_a_payload(self):
        source = (REPO / "app/services/raw_payload_reclamation.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"info", "debug", "warning", "error", "exception"}:
                    rendered = ast.get_source_segment(source, node) or ""
                    assert "payload" not in rendered or "plan failed" in rendered, rendered

    def test_documentation_exists(self):
        doc = REPO / "docs/RAW_PAYLOAD_RECLAMATION_001.md"
        text = doc.read_text().lower()
        for required in ("preservation", "backup", "batch", "stop condition",
                         "idempot", "sqlite-compact-copy-001", "does not shrink",
                         "pinned"):
            assert required in text, required


# --------------------------------------------------------------------------
# representative-scale behaviour
# --------------------------------------------------------------------------

class TestAtScale:
    def test_a_realistic_batch_run_reclaims_and_verifies(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=500, recent=10, suppressed=0, null=0, tiny=0)
        before_rows = db.execute(
            select(func.count()).select_from(MarketSnapshot)).scalar()
        before_bytes = db.execute(
            select(func.sum(func.length(MarketSnapshot.raw_payload)))).scalar()

        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=100)

        after_rows = db.execute(
            select(func.count()).select_from(MarketSnapshot)).scalar()
        after_bytes = db.execute(
            select(func.sum(func.length(MarketSnapshot.raw_payload)))).scalar()

        assert result.rows_changed == 500
        assert result.stop_reason == "completed"
        assert result.verified is True
        assert all(b["verified"] for b in result.batches)
        assert after_rows == before_rows           # nothing deleted
        assert after_bytes < before_bytes / 5      # materially smaller
        # the 10 recent rows kept their bodies
        kept = db.execute(
            select(func.count()).select_from(MarketSnapshot)
            .where(MarketSnapshot.raw_payload.not_like(rr.SUPPRESSED_LIKE))
        ).scalar()
        assert kept == 10


class TestScanEfficiency:
    def test_batches_advance_by_primary_key_rather_than_rescanning(
        self, db, monkeypatch
    ):
        """Without a high-water mark every batch re-scans from the first row,
        skipping everything already reclaimed — O(n^2) over a 150k-row run, and
        each scan re-reads payload pages."""
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=40, recent=0, suppressed=0, null=0, tiny=0)

        seen = []
        real_execute = type(db).execute

        def spy(self, stmt, *a, **k):
            try:
                seen.append(str(stmt.compile(
                    compile_kwargs={"literal_binds": True})))
            except Exception:
                seen.append(str(stmt))
            return real_execute(self, stmt, *a, **k)

        monkeypatch.setattr(type(db), "execute", spy)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"),
            now=NOW, rows_per_batch=10)
        monkeypatch.setattr(type(db), "execute", real_execute)

        assert result.rows_changed == 40
        selects = [q for q in seen
                   if q.lstrip().upper().startswith("SELECT")
                   and "raw_payload" in q and "LIMIT" in q.upper()]
        assert len(selects) >= 4
        # every batch after the first must carry a primary-key lower bound
        bounded = [q for q in selects if "market_snapshots.id >" in q]
        assert len(bounded) >= len(selects) - 1, selects

    def test_default_batch_size_is_conservative(self):
        """4 database_locked events EVER on this host — a first run should be
        boring, not fast."""
        assert rr.MAX_ROWS_PER_BATCH <= 500

    def test_total_seconds_is_actual_elapsed_time(self, db, monkeypatch):
        healthy_backup(monkeypatch)
        seed_snapshots(db, old=5, recent=0, suppressed=0, null=0, tiny=0)
        result = rr.reclaim_target(
            db, rr.resolve_target("market_snapshots.raw_payload"), now=NOW)
        assert 0 <= result.total_seconds < 60
