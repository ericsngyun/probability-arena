"""RAW-PAYLOAD-STORAGE-001 — explicit capture policy for raw provider bodies.

The one failure mode that matters here is silent: suppressing a payload that
something actually reads. These tests attack that from both directions — the
policy physically cannot suppress a pinned column, and normalized data is proven
byte-identical across every capture mode.

Nothing here touches production data. No historical row is modified anywhere in
this milestone.
"""

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import Settings
from app.db import Base
from app.models import (
    MarketDetailEnrichment,
    MarketPriceTick,
    MarketSnapshot,
    OpportunitySignal,
)
from app.services import raw_payload_policy as rp

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 4, 2, 0, 0, tzinfo=timezone.utc)

BODY = {
    "ticker": "GEN-MKT-1",
    "yes_bid": 41,
    "yes_ask": 44,
    "volume_24h": 1234,
    "rules_primary": "x" * 800,
    "unmapped_field": "kept for debugging",
}


def settings_with(mode):
    return Settings(database_url="sqlite:///unused.db", raw_payload_capture_mode=mode)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rp.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# --------------------------------------------------------------------------
# 1-2: default and unknown-mode handling
# --------------------------------------------------------------------------

class TestModeResolution:
    def test_default_is_full(self):
        assert rp.DEFAULT_CAPTURE_MODE == rp.CAPTURE_MODE_FULL
        assert Settings(
            database_url="sqlite:///x.db"
        ).raw_payload_capture_mode == "full"
        assert rp.resolve_capture_mode(Settings(database_url="sqlite:///x.db")) == "full"

    @pytest.mark.parametrize("mode", ["full", "errors_only", "none"])
    def test_valid_modes_resolve(self, mode):
        assert rp.resolve_capture_mode(settings_with(mode)) == mode

    @pytest.mark.parametrize("mode", [
        "nonsense", "off", "disabled", "0", "true", "", "  ",
        "non", "None;none", "full;none",
    ])
    def test_unknown_mode_fails_closed_to_full_never_none(self, mode):
        """A typo in a host .env must never be read as 'discard the bodies'."""
        resolved = rp.resolve_capture_mode(settings_with(mode))
        assert resolved == rp.CAPTURE_MODE_FULL
        assert resolved != rp.CAPTURE_MODE_NONE

    def test_case_and_whitespace_are_normalized(self):
        for mode in ("NONE", " none ", "None", "Errors_Only"):
            assert rp.resolve_capture_mode(settings_with(mode)) in rp.VALID_CAPTURE_MODES

    def test_env_var_name_is_the_documented_one(self, monkeypatch):
        monkeypatch.setenv("RAW_PAYLOAD_CAPTURE_MODE", "none")
        assert Settings(database_url="sqlite:///x.db").raw_payload_capture_mode == "none"


# --------------------------------------------------------------------------
# 3-6: capture semantics
# --------------------------------------------------------------------------

class TestCaptureSemantics:
    def test_full_preserves_the_payload_identically(self):
        out = rp.capture(BODY, source="kalshi_rest", column="market_price_ticks.raw_payload", mode="full")
        assert out is BODY

    def test_none_suppresses_the_body(self):
        out = rp.capture(BODY, source="kalshi_rest", column="market_price_ticks.raw_payload", mode="none")
        assert rp.is_suppressed(out)
        assert "rules_primary" not in json.dumps(out)
        assert "unmapped_field" not in json.dumps(out)
        assert out["source"] == "kalshi_rest"
        assert out["bytes"] == rp.payload_bytes(BODY)
        assert out["digest"] == rp.payload_digest(BODY)

    def test_errors_only_does_not_persist_an_unallowlisted_error_body(self):
        """An error body is the payload class most likely to echo the request
        URL back, and this repo sends a provider key in a query string. A writer
        must be explicitly allowlisted before its error bodies are kept."""
        assert rp.ERROR_BODY_CAPTURE_ALLOWLIST == frozenset()
        out = rp.capture(
            BODY, source="kalshi_rest",
            column="market_price_ticks.raw_payload",
            mode="errors_only", is_error=True,
        )
        assert rp.is_suppressed(out)

    def test_errors_only_keeps_a_failure_body_once_allowlisted(self, monkeypatch):
        monkeypatch.setattr(
            rp, "ERROR_BODY_CAPTURE_ALLOWLIST",
            frozenset({"market_price_ticks.raw_payload"}),
        )
        out = rp.capture(
            BODY, source="kalshi_rest",
            column="market_price_ticks.raw_payload",
            mode="errors_only", is_error=True,
        )
        assert out is BODY

    def test_errors_only_suppresses_a_successful_body(self):
        out = rp.capture(BODY, source="kalshi_rest", column="market_price_ticks.raw_payload", mode="errors_only", is_error=False)
        assert rp.is_suppressed(out)

    def test_errors_only_caps_an_oversized_failure_body(self, monkeypatch):
        monkeypatch.setattr(
            rp, "ERROR_BODY_CAPTURE_ALLOWLIST",
            frozenset({"market_price_ticks.raw_payload"}),
        )
        huge = {"html": "x" * (rp.MAX_ERROR_PAYLOAD_BYTES + 5000)}
        out = rp.capture(huge, source="p", column="market_price_ticks.raw_payload", mode="errors_only", is_error=True)
        assert rp.is_suppressed(out)
        assert out["error_payload_dropped"] == "exceeds_max_error_payload_bytes"
        assert out["max_error_payload_bytes"] == rp.MAX_ERROR_PAYLOAD_BYTES
        assert "x" * 100 not in json.dumps(out)

    @pytest.mark.parametrize("mode", ["full", "errors_only", "none"])
    def test_none_payload_stays_none_in_every_mode(self, mode):
        """NULL means 'the provider gave us nothing'. Suppression must not
        overload that — the two are genuinely different facts."""
        assert rp.capture(None, source="k", column="market_price_ticks.raw_payload", mode=mode) is None
        assert rp.capture(None, source="k", column="market_price_ticks.raw_payload", mode=mode, is_error=True) is None

    def test_envelope_is_small(self):
        out = rp.capture(BODY, source="kalshi_rest", column="market_price_ticks.raw_payload", mode="none")
        assert len(json.dumps(out)) < 200
        assert len(json.dumps(out)) < rp.payload_bytes(BODY) / 5

    def test_digest_is_stable_and_key_order_independent(self):
        a = {"x": 1, "y": [1, 2], "z": "q"}
        b = {"z": "q", "y": [1, 2], "x": 1}
        assert rp.payload_digest(a) == rp.payload_digest(b)
        assert len(rp.payload_digest(a)) == rp.DIGEST_HEX_CHARS
        assert rp.payload_digest({"x": 2}) != rp.payload_digest(a)

    def test_byte_count_matches_a_real_serialization(self):
        assert rp.payload_bytes(BODY) == len(
            json.dumps(BODY, separators=(",", ":"), default=str)
        )

    def test_unserializable_payload_degrades_without_raising(self):
        class Odd:
            pass

        assert rp.payload_bytes({"o": Odd()}) > 0   # default=str handles it
        assert rp.capture({"o": Odd()}, source="k", column="market_price_ticks.raw_payload", mode="none") is not None


# --------------------------------------------------------------------------
# The central safety property: pinned columns cannot be suppressed
# --------------------------------------------------------------------------

class TestPinnedColumnsCannotBeSuppressed:
    @pytest.mark.parametrize("column", sorted(rp.PINNED_FULL))
    @pytest.mark.parametrize("mode", ["none", "errors_only"])
    def test_pinned_column_ignores_the_mode(self, column, mode):
        out = rp.capture(BODY, source="p", column=column, mode=mode)
        assert out is BODY, f"{column} must never be suppressed"

    @pytest.mark.parametrize("typo", [
        "crypto_token_risk_assessments.raw_payloads",   # trailing s
        "crypto_token_risk_assessment.raw_payload",     # missing s
        "market_research_packets.raw_responses",
        "crypto_price_tick.raw_payload",
        "CRYPTO_PRICE_TICKS.RAW_PAYLOAD",
        "totally_unknown.column",
        "",
    ])
    @pytest.mark.parametrize("mode", ["none", "errors_only"])
    def test_an_unrecognised_column_fails_CLOSED_not_open(self, typo, mode):
        """The security review found this failing OPEN: checking only
        `column in PINNED_FULL` means a typo'd pin silently drops out of the
        pin list and starts suppressing data a production reader needs. An
        unrecognised identifier must keep the full body."""
        out = rp.capture(BODY, source="p", column=typo, mode=mode)
        assert out is BODY, f"{typo!r} was suppressed — fail-open regression"

    def test_column_none_also_fails_closed(self):
        assert rp.capture(BODY, source="p", column=None, mode="none") is BODY

    def test_only_governed_columns_can_ever_be_suppressed(self):
        for column in rp.GOVERNED_COLUMNS:
            assert rp.is_suppressed(
                rp.capture(BODY, source="p", column=column, mode="none")
            ), column

    def test_unknown_mode_warning_is_emitted_once_not_per_row(self, caplog):
        """capture() runs ~226,000 times a day; a per-call warning would put
        ~25 MiB/day of journald on a shared host."""
        import logging

        rp._WARNED.clear()
        bogus = settings_with("definitely-not-a-mode")
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            for _ in range(50):
                rp.resolve_capture_mode(bogus)
        assert len([r for r in caplog.records if "RAW_PAYLOAD_CAPTURE_MODE" in
                    r.getMessage()]) == 1

    def test_unknown_column_warning_is_emitted_once(self, caplog):
        import logging

        rp._WARNED.clear()
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            for _ in range(50):
                rp.capture(BODY, source="p", column="bogus.col", mode="none")
        assert len([r for r in caplog.records if "unrecognised raw-payload column"
                    in r.getMessage()]) == 1

    def test_warning_does_not_echo_an_unbounded_env_value(self, caplog):
        import logging

        rp._WARNED.clear()
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            rp.resolve_capture_mode(settings_with("z" * 5000))
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "z" * 200 not in joined

    def test_every_pin_names_its_reader(self):
        for column, reason in rp.PINNED_FULL.items():
            assert len(reason) > 40, column
            assert "." in column

    def test_pinned_and_governed_sets_are_disjoint(self):
        assert not (set(rp.PINNED_FULL) & set(rp.GOVERNED_COLUMNS))

    def test_the_four_known_production_readers_are_pinned(self):
        """Regression guard: these were traced to real runtime readers."""
        assert set(rp.PINNED_FULL) == {
            "crypto_token_risk_assessments.raw_payload",
            "market_research_packets.raw_response",
            "crypto_price_ticks.raw_payload",
            "crypto_horizon_observations.raw_payload",
        }

    def test_propagation_is_recorded(self):
        """crypto_tape copies the discovery payload into the birth event. It is
        a governed column WITH a consumer, so the propagation is documented
        rather than left to be rediscovered."""
        assert "crypto_token_discovery_events.raw_payload" in rp.PROPAGATED_COLUMNS
        assert "crypto_token_birth_events" in \
            rp.PROPAGATED_COLUMNS["crypto_token_discovery_events.raw_payload"]

    def test_propagated_envelope_stays_identifiable(self):
        """Under suppression the birth event receives the envelope; downstream
        must still be able to tell that apart from a real body or a NULL."""
        envelope = rp.capture(BODY, source="dexscreener",
                              column="crypto_token_discovery_events.raw_payload",
                              mode="none")
        assert rp.is_suppressed(envelope)
        assert not rp.is_suppressed(BODY)
        assert not rp.is_suppressed(None)

    def test_ungoverned_columns_are_justified(self):
        for column, reason in rp.UNGOVERNED_BY_DESIGN.items():
            assert len(reason) > 30, column
            assert column not in rp.GOVERNED_COLUMNS


# --------------------------------------------------------------------------
# 7-11: normalized-data parity across modes
# --------------------------------------------------------------------------

NORMALIZED_FIELDS = {
    MarketPriceTick: ("market_ticker", "yes_bid", "yes_ask", "midpoint", "spread",
                      "volume_24h", "liquidity_proxy", "observed_at", "created_at"),
    MarketSnapshot: ("market_id", "yes_bid", "yes_ask", "score", "captured_at"),
}


class TestNormalizedParity:
    def _tick(self, mode):
        return MarketPriceTick(
            market_ticker="GEN-MKT-1", observed_at=NOW, created_at=NOW,
            yes_bid=41, yes_ask=44, midpoint=42.5, spread=3,
            volume_24h=1234, liquidity_proxy=99.5,
            raw_payload=rp.capture(
                BODY, source="kalshi_rest",
                column="market_price_ticks.raw_payload", mode=mode,
            ),
        )

    @pytest.mark.parametrize("mode", ["full", "errors_only", "none"])
    def test_every_normalized_column_is_identical_across_modes(self, db, mode):
        baseline = self._tick("full")
        candidate = self._tick(mode)
        db.add_all([baseline, candidate])
        db.commit()
        for field in NORMALIZED_FIELDS[MarketPriceTick]:
            assert getattr(baseline, field) == getattr(candidate, field), field

    @pytest.mark.parametrize("mode", ["full", "errors_only", "none"])
    def test_row_count_is_identical_across_modes(self, db, mode):
        for _ in range(5):
            db.add(self._tick(mode))
        db.commit()
        assert db.execute(
            select(MarketPriceTick)
        ).scalars().all().__len__() == 5

    def test_suppression_actually_reduces_stored_bytes(self, db):
        full = self._tick("full")
        none = self._tick("none")
        db.add_all([full, none])
        db.commit()
        assert len(json.dumps(full.raw_payload)) > \
            5 * len(json.dumps(none.raw_payload))

    def test_non_nullable_column_stays_valid_without_a_migration(self, db):
        """market_detail_enrichments.raw_market_detail is NOT NULL — the
        envelope keeps it valid, which is why suppression never writes NULL."""
        row = MarketDetailEnrichment(
            market_ticker="GEN-MKT-1",
            raw_market_detail=rp.capture(
                BODY, source="kalshi_rest",
                column="market_detail_enrichments.raw_market_detail", mode="none",
            ),
            raw_event_detail=None,
            raw_series_detail=None,
            created_at=NOW,
        )
        db.add(row)
        db.commit()
        assert rp.is_suppressed(row.raw_market_detail)
        assert row.raw_event_detail is None   # genuine absence preserved

    def test_signal_row_survives_suppression(self, db):
        signal = OpportunitySignal(
            market_ticker="GEN-MKT-1", signal_type="price_move_threshold",
            signal_status="new", observed_at=NOW, created_at=NOW,
            raw_payload=rp.capture(
                BODY, source="kalshi_rest",
                column="opportunity_signals.raw_payload", mode="none",
            ),
        )
        db.add(signal)
        db.commit()
        assert signal.market_ticker == "GEN-MKT-1"
        assert rp.is_suppressed(signal.raw_payload)


# --------------------------------------------------------------------------
# 14: secret safety
# --------------------------------------------------------------------------

class TestSecretSafety:
    def test_envelope_carries_no_payload_fragment(self):
        secretish = {
            "authorization": "Bearer sk-live-abc123",
            "api_key": "topsecret",
            "headers": {"X-API-KEY": "nope"},
            "password": "hunter2",
            "body": "y" * 500,
        }
        out = rp.capture(secretish, source="p", column="market_price_ticks.raw_payload", mode="none")
        blob = json.dumps(out).lower()
        for banned in ("bearer", "sk-live", "topsecret", "hunter2", "x-api-key",
                       "authorization", "api_key", "password", "yyyy"):
            assert banned not in blob
        assert set(out) == {"raw_payload_suppressed", "mode", "source", "bytes",
                            "digest"}

    def test_envelope_keys_are_a_fixed_closed_set(self):
        out = rp.capture(BODY, source="k", column="market_price_ticks.raw_payload", mode="none")
        assert set(out) == {"raw_payload_suppressed", "mode", "source", "bytes",
                            "digest"}

    def test_module_never_logs_a_payload(self):
        source = (REPO / "app/services/raw_payload_policy.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"info", "debug", "warning", "error", "exception"}:
                    rendered = ast.get_source_segment(source, node) or ""
                    assert "payload" not in rendered or "RAW_PAYLOAD_CAPTURE_MODE" \
                        in rendered, rendered

    def test_no_provider_or_capital_surface(self):
        source = (REPO / "app/services/raw_payload_policy.py").read_text().lower()
        for banned in ("httpx", "requests.", "aiohttp", "urllib", "socket",
                       "expected_value", "kelly", "position_siz", "paper_trad",
                       "place_order", "wallet", "trade_recommend", "execute_trade"):
            assert banned not in source, banned


# --------------------------------------------------------------------------
# 19-22: the measurement report
# --------------------------------------------------------------------------

class TestReport:
    def _seed(self, db):
        for i in range(4):
            db.add(MarketPriceTick(
                market_ticker=f"T-{i}", observed_at=NOW, created_at=NOW,
                yes_bid=1, yes_ask=2,
                raw_payload=BODY if i < 2 else rp.capture(
                    BODY, source="kalshi_rest",
                    column="market_price_ticks.raw_payload", mode="none"),
            ))
        db.commit()

    def test_text_and_json_parity(self, db, capsys):
        from app import cli

        self._seed(db)
        text = cli.raw_payload_storage_report(fmt="text", session=db)
        capsys.readouterr()
        data = cli.raw_payload_storage_report(fmt="json", session=db)
        out = capsys.readouterr().out
        volatile = {"generated_at"}
        assert {k: v for k, v in text.items() if k not in volatile} == \
               {k: v for k, v in data.items() if k not in volatile}
        assert json.loads(out)["columns"] == data["columns"]

    def test_report_counts_capture_state_in_the_bounded_window(self, db, capsys):
        from app import cli

        self._seed(db)
        data = cli.raw_payload_storage_report(fmt="json", session=db)
        capsys.readouterr()
        ticks = next(c for c in data["columns"]
                     if c["column"] == "market_price_ticks.raw_payload")
        assert data["full_scan"] is False
        assert ticks["rows_total"] == 4
        assert ticks["window_rows"] == 4
        assert ticks["window_rows_full_payload"] == 2
        assert ticks["window_rows_suppressed"] == 2
        assert ticks["pinned_full"] is False
        # whole-table aggregates are deliberately not computed by default
        assert ticks["rows_full_payload"] is None
        assert ticks["stored_bytes"] is None

    def test_full_scan_computes_whole_table_totals(self, db, capsys):
        from app import cli

        self._seed(db)
        data = cli.raw_payload_storage_report(
            fmt="json", full_scan=True, session=db
        )
        capsys.readouterr()
        ticks = next(c for c in data["columns"]
                     if c["column"] == "market_price_ticks.raw_payload")
        assert data["full_scan"] is True
        assert ticks["rows_full_payload"] == 2
        assert ticks["rows_suppressed"] == 2
        assert ticks["stored_bytes"] > 0

    def test_default_avoids_the_whole_table_scan(self, db, capsys, monkeypatch):
        """A full LIKE/length scan over a multi-hundred-MiB JSON column holds a
        read lock long enough to block the writer under journal_mode=delete."""
        from app import cli

        self._seed(db)
        seen = []
        real_execute = type(db).execute

        def spy(self, stmt, *a, **k):
            seen.append(str(stmt))
            return real_execute(self, stmt, *a, **k)

        monkeypatch.setattr(type(db), "execute", spy)
        cli.raw_payload_storage_report(fmt="json", session=db)
        capsys.readouterr()
        unbounded_like = [
            q for q in seen
            if "LIKE" in q.upper() and "created_at" not in q and "observed_at" not in q
            and "captured_at" not in q
        ]
        assert unbounded_like == [], unbounded_like

    def test_report_writes_nothing(self, db, tmp_path, capsys):
        from app import cli

        self._seed(db)
        before = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
        rows_before = db.execute(select(MarketPriceTick)).scalars().all()
        cli.raw_payload_storage_report(fmt="json", session=db)
        capsys.readouterr()
        after = sorted((p.name, p.stat().st_size) for p in tmp_path.iterdir())
        assert before == after
        assert len(db.execute(select(MarketPriceTick)).scalars().all()) == \
            len(rows_before)

    def test_report_never_prints_payload_contents(self, db, capsys):
        from app import cli

        self._seed(db)
        cli.raw_payload_storage_report(fmt="text", session=db)
        text_out = capsys.readouterr().out
        cli.raw_payload_storage_report(fmt="json", session=db)
        json_out = capsys.readouterr().out
        for out in (text_out, json_out):
            assert "unmapped_field" not in out
            assert "kept for debugging" not in out
            assert "x" * 50 not in out
            assert "rules_primary" not in out

    def test_report_declares_zero_provider_calls(self, db, capsys):
        from app import cli

        self._seed(db)
        data = cli.raw_payload_storage_report(fmt="json", session=db)
        capsys.readouterr()
        assert data["external_calls"] == 0
        assert data["persisted"] is False
        assert data["prints_payload_contents"] is False

    def test_report_marks_pinned_columns(self, db, capsys):
        from app import cli

        self._seed(db)
        data = cli.raw_payload_storage_report(fmt="json", session=db)
        capsys.readouterr()
        pinned = {c["column"] for c in data["columns"] if c["pinned_full"]}
        assert pinned == set(rp.PINNED_FULL) & {
            c["column"] for c in data["columns"]
        }

    def test_report_states_the_logical_vs_physical_boundary(self, db, capsys):
        from app import cli

        self._seed(db)
        cli.raw_payload_storage_report(fmt="text", session=db)
        out = capsys.readouterr().out
        assert "does not delete history" in out
        assert "shrink the SQLite file" in out
        assert "RAW-PAYLOAD-RECLAMATION-001" in out
        assert "SQLITE-COMPACT-COPY-001" in out

    def test_cli_registered(self):
        from app.cli import build_parser

        args = build_parser().parse_args(
            ["raw-payload-storage-report", "--format", "json", "--hours", "6"]
        )
        assert args.command == "raw-payload-storage-report"
        assert args.fmt == "json" and args.hours == 6
        for flag in ("--confirm", "--apply", "--delete", "--purge"):
            with pytest.raises(SystemExit):
                build_parser().parse_args(["raw-payload-storage-report", flag])


# --------------------------------------------------------------------------
# 23-27: scope and boundaries
# --------------------------------------------------------------------------

class TestScopeAndBoundaries:
    def test_no_migration_added(self):
        for path in (REPO / "alembic/versions").glob("*.py"):
            text = path.read_text().lower()
            assert "raw_payload_capture" not in text
            assert "capture_mode" not in text

    def test_no_historical_mutation_anywhere_in_the_milestone(self):
        """The policy is prospective only — no UPDATE/DELETE of stored payloads."""
        source = (REPO / "app/services/raw_payload_policy.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else None)
                if name == "add":
                    # module-level warn-once set, not a session write
                    rendered = ast.get_source_segment(source, node) or ""
                    assert "_WARNED.add" in rendered, rendered
                    continue
                assert name not in {"delete", "update", "execute", "commit",
                                    "flush"}, name
        for banned in ("UPDATE ", "DELETE ", "VACUUM", "session"):
            assert banned not in source, banned

    def test_no_pragma_or_transaction_change(self):
        for path in ("app/services/scanner.py", "app/services/watcher.py",
                     "app/services/enrichment.py", "app/services/crypto_scout.py",
                     "app/services/tennis_watcher.py"):
            source = (REPO / path).read_text()
            for banned in ("PRAGMA", "journal_mode", "busy_timeout",
                           "isolation_level", "begin_nested"):
                assert banned not in source, f"{path}: {banned}"

    def test_writers_only_gained_a_capture_call(self):
        """Each governed writer changed by wrapping the payload — nothing else."""
        for path in ("app/services/scanner.py", "app/services/watcher.py",
                     "app/services/enrichment.py", "app/services/crypto_scout.py",
                     "app/services/tennis_watcher.py"):
            source = (REPO / path).read_text()
            assert "raw_payload_policy import capture as _capture_raw" in source

    def test_every_governed_column_has_a_live_call_site(self):
        sources = {
            path.name: path.read_text()
            for path in (REPO / "app/services").glob("*.py")
        }
        blob = "\n".join(sources.values())
        for column in rp.GOVERNED_COLUMNS:
            assert f'column="{column}"' in blob, column

    def test_no_pinned_column_has_a_capture_call_site(self):
        """A pinned column must not even be routed through the policy."""
        blob = "\n".join(
            path.read_text() for path in (REPO / "app/services").glob("*.py")
        )
        for column in rp.PINNED_FULL:
            assert f'column="{column}"' not in blob, column

    def test_no_backup_or_retention_behaviour_change(self):
        for path in ("app/services/backup.py", "app/services/backup_freshness.py",
                     "app/services/retention.py"):
            assert "raw_payload_policy" not in (REPO / path).read_text()

    def test_documentation_exists(self):
        doc = REPO / "docs/RAW_PAYLOAD_STORAGE_001.md"
        text = doc.read_text().lower()
        for required in ("inventory", "provenance", "capture mode", "rollback",
                         "raw-payload-reclamation-001", "sqlite-compact-copy-001",
                         "pinned"):
            assert required in text, required


# --------------------------------------------------------------------------
# 30: disposable-database growth comparison
# --------------------------------------------------------------------------

class TestGrowthComparison:
    """Write the SAME workload into two disposable databases under different
    capture modes and compare the resulting FILE sizes. This is the milestone's
    central claim, measured rather than asserted."""

    def _write_workload(self, tmp_path, name, mode, rows=400):
        engine = create_engine(f"sqlite:///{tmp_path / name}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        session = factory()
        try:
            for i in range(rows):
                session.add(MarketPriceTick(
                    market_ticker=f"GEN-MKT-{i % 25}",
                    observed_at=NOW, created_at=NOW,
                    yes_bid=40 + (i % 5), yes_ask=44 + (i % 5),
                    midpoint=42.0, spread=4, volume_24h=1000 + i,
                    liquidity_proxy=500.0,
                    raw_payload=rp.capture(
                        dict(BODY, seq=i), source="kalshi_rest",
                        column="market_price_ticks.raw_payload", mode=mode,
                    ),
                ))
            session.commit()
            normalized = [
                (r.market_ticker, r.yes_bid, r.yes_ask, r.midpoint, r.spread,
                 r.volume_24h, r.liquidity_proxy, r.observed_at)
                for r in session.execute(
                    select(MarketPriceTick).order_by(MarketPriceTick.id)
                ).scalars().all()
            ]
            count = len(normalized)
            from sqlalchemy import func as _f

            column_bytes = session.execute(
                select(_f.coalesce(_f.sum(_f.length(MarketPriceTick.raw_payload)), 0))
            ).scalar() or 0
        finally:
            session.close()
            engine.dispose()
        return (tmp_path / name).stat().st_size, count, normalized, column_bytes

    def test_none_writes_materially_less_than_full(self, tmp_path):
        full_bytes, full_rows, full_norm, full_col = self._write_workload(
            tmp_path, "full.db", "full")
        none_bytes, none_rows, none_norm, none_col = self._write_workload(
            tmp_path, "none.db", "none")

        # identical rows, identical normalized data — only storage differs
        assert full_rows == none_rows == 400
        assert full_norm == none_norm

        # The direct claim: the payload COLUMN collapses to the envelope. The
        # fixture body is ~950 B, so the ratio here is lower than production's
        # (~2,050 B bodies against the same ~117 B envelope = ~94%).
        column_saved = 1 - (none_col / full_col)
        assert column_saved > 0.85, (
            f"payload column only shrank {column_saved:.1%} "
            f"({full_col} -> {none_col} bytes)"
        )
        # The file also shrinks, but by less: a disposable database carries the
        # full schema and every index as fixed overhead, which the 400-row
        # workload cannot dilute. Production dilutes it; this floor is honest
        # about what a small fixture can actually demonstrate.
        file_saved = 1 - (none_bytes / full_bytes)
        assert file_saved > 0.25, (
            f"file only shrank {file_saved:.1%} "
            f"({full_bytes} -> {none_bytes} bytes)"
        )

    def test_errors_only_matches_none_for_a_writer_with_no_error_path(
        self, tmp_path
    ):
        """Documented honestly rather than implied: the governed writers only
        create a row on success, so errors_only cannot differ from none there."""
        none_bytes, _, none_norm, _ = self._write_workload(
            tmp_path, "n.db", "none", rows=200)
        eo_bytes, _, eo_norm, _ = self._write_workload(
            tmp_path, "e.db", "errors_only", rows=200)
        assert none_norm == eo_norm
        assert abs(none_bytes - eo_bytes) < 8192

    def test_full_mode_is_unchanged_from_no_policy_at_all(self, tmp_path):
        """Default `full` must be byte-equivalent to the pre-milestone writer."""
        engine = create_engine(f"sqlite:///{tmp_path / 'raw.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        session = factory()
        try:
            for i in range(200):
                session.add(MarketPriceTick(
                    market_ticker=f"GEN-MKT-{i % 25}",
                    observed_at=NOW, created_at=NOW,
                    yes_bid=40 + (i % 5), yes_ask=44 + (i % 5),
                    midpoint=42.0, spread=4, volume_24h=1000 + i,
                    liquidity_proxy=500.0,
                    raw_payload=dict(BODY, seq=i),      # no policy at all
                ))
            session.commit()
        finally:
            session.close()
            engine.dispose()
        unpoliced = (tmp_path / "raw.db").stat().st_size

        policed, _, _, _ = self._write_workload(
            tmp_path, "policed.db", "full", rows=200)
        assert policed == unpoliced
