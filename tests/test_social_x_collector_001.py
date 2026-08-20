"""SOCIAL-TAPE-001 — the X collector, driven entirely by fixtures.

NO NETWORK. The only transports that exist in this repository are
`FixtureTransport` (replays frames) and `NullTransport` (refuses). There is no
live HTTP implementation to point at anything, which is asserted structurally
below.

Every fixture frame carries provenance (doctrine 9) and is honestly marked
SYNTHETIC: no wire capture exists, because nothing has been connected.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path

import pytest

from app.social.artifact import (
    INGESTION_VERSION,
    DeferredState,
    DeliveryMode,
    Platform,
    PropagationKind,
    ResolutionConfidence,
)
from app.social.cost_guard import (
    CostBudget,
    CounterUnreadableError,
    MonthlyReadCostGuard,
)
from app.social.dedupe import PropagationLedger
from app.social.resolution import ConservativeAddressResolver
from app.social.sources import RuleKind, SourceRule, SourceUniverse
from app.social.tape import RecordKind, SocialTapeWriter, read_segment_records, replay, verify_segment
from app.social.timebase import process_epoch
from app.social.transport import (
    FixtureBasis,
    FixtureProvenanceError,
    FixtureTransport,
    FrameKind,
    FrameProvenance,
    LiveTransportUnavailableError,
    NullTransport,
    SocialStreamTransport,
    StreamFrame,
    TransportRule,
)
from app.social.x_collector import (
    CollectorNotStartableError,
    XFilteredStreamCollector,
    XPayloadError,
    parse_x_payload,
)

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
SOCIAL_PACKAGE = APP_ROOT / "social"

RULE_ID = "caller.example-account-01"
OTHER_RULE_ID = "exchange.listing.example-venue"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def universe() -> SourceUniverse:
    return SourceUniverse(
        universe_id="test-universe",
        rules=(
            SourceRule(
                rule_id=RULE_ID,
                platform=Platform.X,
                kind=RuleKind.ACCOUNT,
                selector="from:EXAMPLE_EARLY_CALLER_01",
                rationale="named individually so it can be measured and retired",
                active_from="2026-09-01",
            ),
            SourceRule(
                rule_id=OTHER_RULE_ID,
                platform=Platform.X,
                kind=RuleKind.ACCOUNT,
                selector="from:EXAMPLE_VENUE_ANNOUNCEMENTS",
                rationale="venue listing announcements are attributable events",
                active_from="2026-09-01",
            ),
        ),
    )


def post_bytes(
    message_id: str = "1001",
    author_id: str = "author-1",
    text: str = "gm",
    created_at: str = "2026-08-20T12:00:00.000Z",
    referenced=None,
) -> bytes:
    data = {
        "id": message_id,
        "author_id": author_id,
        "text": text,
        "created_at": created_at,
    }
    if referenced is not None:
        data["referenced_tweets"] = referenced
    return json.dumps({"data": data}, sort_keys=True).encode("utf-8")


def frame(
    raw: bytes,
    *,
    kind: FrameKind = FrameKind.DATA,
    seq: int = 0,
    generation: int = 0,
    rules: tuple[str, ...] = (RULE_ID,),
    capture_id: str = "synthetic-x-frame",
) -> StreamFrame:
    return StreamFrame(
        kind=kind,
        raw=raw,
        delivery_sequence=seq,
        subscription_generation=generation,
        matched_rule_ids=rules,
        provenance=FrameProvenance.synthetic(capture_id, raw),
    )


def control_frame(kind: FrameKind, capture_id: str = "synthetic-control") -> StreamFrame:
    return StreamFrame(
        kind=kind, raw=b"", provenance=FrameProvenance.synthetic(capture_id, b"")
    )


class Harness:
    """Wires a collector against fixtures. Nothing here can reach a network."""

    def __init__(self, tmp_path: Path, *, budget: int = 100, frames=(), rules=None):
        self.epoch = process_epoch()
        self.tape = SocialTapeWriter(
            tmp_path / "tape",
            environment="test",
            segment_id="seg-1",
            universe_fields=universe().digest_fields(),
            process_epoch=self.epoch,
            ingestion_version=INGESTION_VERSION,
        )
        self.guard = MonthlyReadCostGuard(
            tmp_path / "cost.json", CostBudget(max_reads_per_month=budget)
        )
        self.transport = FixtureTransport(frames, remote_rules=rules or ())
        self.ledger = PropagationLedger()
        self.collector = XFilteredStreamCollector(
            transport=self.transport,
            tape=self.tape,
            universe=universe(),
            cost_guard=self.guard,
            process_epoch=self.epoch,
            ledger=self.ledger,
            resolver=ConservativeAddressResolver(),
        )

    def records(self):
        return read_segment_records(self.tape.directory / "events.jsonl.gz")


# --------------------------------------------------------------------------
# 1. NOTHING CAN OPEN A SOCKET
# --------------------------------------------------------------------------


class TestNoNetworkExists:
    def test_the_social_package_imports_no_http_client(self):
        forbidden = {
            "httpx", "requests", "aiohttp", "urllib", "urllib3", "http",
            "socket", "websockets", "websocket", "ssl", "tweepy",
        }
        offenders: list[str] = []
        for path in sorted(SOCIAL_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name in forbidden:
                        offenders.append(f"{path.name}: {name}")
        assert offenders == [], f"network capability in app/social/: {offenders}"

    def test_positive_control_the_import_scan_can_fail(self):
        tree = ast.parse("import httpx\n")
        found = [
            a.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for a in node.names
        ]
        assert found == ["httpx"]

    async def test_the_null_transport_refuses_everything(self):
        transport = NullTransport()
        with pytest.raises(LiveTransportUnavailableError):
            await transport.list_rules()
        with pytest.raises(LiveTransportUnavailableError):
            await transport.apply_rules([], [])
        with pytest.raises(LiveTransportUnavailableError):
            async for _ in transport.frames():
                pass

    async def test_a_collector_on_the_null_transport_cannot_run(self, tmp_path):
        harness = Harness(tmp_path)
        harness.collector._transport = NullTransport()
        with pytest.raises(LiveTransportUnavailableError):
            await harness.collector.run()

    def test_both_transports_satisfy_the_protocol(self):
        assert isinstance(FixtureTransport([]), SocialStreamTransport)
        assert isinstance(NullTransport(), SocialStreamTransport)

    def test_no_credential_surface_in_the_package(self):
        banned = ("api_key", "bearer_token", "access_token", "client_secret")
        offenders = []
        for path in sorted(SOCIAL_PACKAGE.glob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            for token in banned:
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == [], f"credential surface present: {offenders}"


# --------------------------------------------------------------------------
# 2. FIXTURE PROVENANCE
# --------------------------------------------------------------------------


class TestFixtureProvenance:
    def test_a_frame_without_provenance_is_refused(self):
        with pytest.raises(FixtureProvenanceError):
            FixtureTransport([StreamFrame(kind=FrameKind.DATA, raw=b"{}")])

    def test_provenance_requires_a_capture_id(self):
        with pytest.raises(FixtureProvenanceError):
            FrameProvenance(
                capture_id="",
                captured_at="1970-01-01T00:00:00Z",
                platform="X",
                schema_version="v1",
                basis=FixtureBasis.SYNTHETIC,
                sanitized_frame_hash="0" * 64,
            )

    def test_provenance_requires_a_real_frame_hash(self):
        with pytest.raises(FixtureProvenanceError):
            FrameProvenance(
                capture_id="c",
                captured_at="1970-01-01T00:00:00Z",
                platform="X",
                schema_version="v1",
                basis=FixtureBasis.SYNTHETIC,
                sanitized_frame_hash="short",
            )

    def test_our_fixtures_are_honestly_marked_synthetic(self):
        f = frame(post_bytes())
        assert f.provenance is not None
        assert f.provenance.basis is FixtureBasis.SYNTHETIC
        assert f.provenance.basis is not FixtureBasis.WIRE_CAPTURE


# --------------------------------------------------------------------------
# 3. START-UP PRECONDITIONS
# --------------------------------------------------------------------------


class TestStartupPreconditions:
    async def test_an_empty_universe_refuses_to_start(self, tmp_path):
        harness = Harness(tmp_path, frames=[frame(post_bytes())])
        harness.collector._universe = SourceUniverse(universe_id="empty")
        with pytest.raises(CollectorNotStartableError):
            await harness.collector.run()
        assert harness.transport.frames_yielded == 0

    async def test_an_exhausted_budget_refuses_to_start(self, tmp_path):
        harness = Harness(tmp_path, budget=1, frames=[frame(post_bytes())])
        harness.guard.reserve(1)
        from app.social.cost_guard import BudgetExhaustedError

        with pytest.raises(BudgetExhaustedError):
            await harness.collector.run()
        assert harness.transport.frames_yielded == 0

    def test_a_collector_cannot_be_built_without_a_cost_guard(self, tmp_path):
        harness = Harness(tmp_path)
        with pytest.raises(CollectorNotStartableError):
            XFilteredStreamCollector(
                transport=harness.transport,
                tape=harness.tape,
                universe=universe(),
                cost_guard=None,  # type: ignore[arg-type]
                process_epoch=harness.epoch,
            )

    async def test_positive_control_a_configured_collector_runs(self, tmp_path):
        harness = Harness(tmp_path, frames=[frame(post_bytes())])
        report = await harness.collector.run()
        assert report.artifacts_written == 1


# --------------------------------------------------------------------------
# 4. THE COST GUARD STOPS THE COLLECTOR
# --------------------------------------------------------------------------


class TestCollectorCostGuard:
    async def test_the_collector_stops_when_the_budget_is_exhausted(self, tmp_path):
        """THE test: prove it stops mid-stream, and records why."""

        frames = [
            frame(post_bytes(str(1000 + i), text=f"post {i}"), seq=i)
            for i in range(10)
        ]
        harness = Harness(tmp_path, budget=3, frames=frames)
        report = await harness.collector.run()

        assert report.stopped_reason == "cost_budget_exhausted"
        assert report.reads_reserved == 3
        assert report.artifacts_written == 3
        assert harness.guard.read_state().consumed == 3

        harness.tape.close()
        kinds = [r["record_kind"] for r in harness.records()]
        assert RecordKind.ABSENCE.value in kinds
        absences = [
            r for r in harness.records()
            if r["record_kind"] == RecordKind.ABSENCE.value
        ]
        assert any(
            r["payload"]["reason"] == "cost_budget_exhausted" for r in absences
        )

    async def test_start_up_refuses_when_the_counter_is_unreadable(self, tmp_path):
        harness = Harness(
            tmp_path, budget=100, frames=[frame(post_bytes("1001"))]
        )
        harness.guard.reserve(1)
        (tmp_path / "cost.json").write_bytes(b"{corrupt")
        with pytest.raises(CounterUnreadableError):
            await harness.collector.run()
        assert harness.transport.frames_yielded == 0

    async def test_the_collector_stops_mid_stream_when_the_counter_breaks(
        self, tmp_path
    ):
        """Corruption discovered AFTER the run began must also stop it."""

        ledger_path = tmp_path / "cost.json"
        frames = [frame(post_bytes(str(1000 + i)), seq=i) for i in range(5)]
        harness = Harness(tmp_path, budget=100, frames=frames)

        class CorruptingTransport:
            """Breaks the ledger after the first frame is delivered."""

            def __init__(self, inner):
                self._inner = inner
                self.frames_yielded = 0

            async def list_rules(self):
                return await self._inner.list_rules()

            async def apply_rules(self, add, delete):
                return await self._inner.apply_rules(add, delete)

            async def frames(self):
                async for item in self._inner.frames():
                    self.frames_yielded += 1
                    yield item
                    if self.frames_yielded == 1:
                        ledger_path.write_bytes(b"{corrupt")

            async def aclose(self):
                await self._inner.aclose()

        harness.collector._transport = CorruptingTransport(harness.transport)
        report = await harness.collector.run()

        assert report.stopped_reason == "cost_guard_unavailable"
        assert report.artifacts_written == 1
        harness.tape.close()
        absences = [
            r["payload"]["reason"]
            for r in harness.records()
            if r["record_kind"] == RecordKind.ABSENCE.value
        ]
        assert "cost_guard_unavailable" in absences

    async def test_a_redelivery_still_costs_a_read(self, tmp_path):
        raw = post_bytes("1001")
        harness = Harness(
            tmp_path,
            budget=10,
            frames=[frame(raw, seq=0), frame(raw, seq=0)],
        )
        report = await harness.collector.run()
        assert report.reads_reserved == 2
        assert report.artifacts_written == 1
        assert report.redeliveries_written == 1

    async def test_budget_is_reserved_before_the_post_is_used(self, tmp_path):
        """An unparseable frame still consumed a read, and the count says so."""

        harness = Harness(tmp_path, budget=10, frames=[frame(b"not json")])
        report = await harness.collector.run()
        assert report.reads_reserved == 1
        assert report.unparseable_frames == 1
        assert harness.guard.read_state().consumed == 1


# --------------------------------------------------------------------------
# 5. TIMESTAMPS THROUGH THE COLLECTOR
# --------------------------------------------------------------------------


class TestCollectorTimestamps:
    async def test_our_received_at_comes_from_our_clock_not_the_payload(
        self, tmp_path
    ):
        harness = Harness(
            tmp_path,
            frames=[frame(post_bytes(created_at="2026-08-20T12:00:00.000Z"))],
        )
        fixed = dt.datetime(2026, 8, 20, 12, 0, 5, tzinfo=dt.timezone.utc)
        harness.collector._receipt_clock = lambda: (fixed, 99)

        await harness.collector.run()
        harness.tape.close()
        (artifact,) = list(replay(harness.tape.directory))

        assert artifact.source_created_at.value == "2026-08-20T12:00:00.000000Z"
        assert artifact.our_received_at.value == "2026-08-20T12:00:05.000000Z"
        assert artifact.our_received_at.value != artifact.source_created_at.value
        assert artifact.our_received_at.epoch_id == harness.epoch.epoch_id
        assert artifact.delivery_offset is not None
        assert artifact.delivery_offset.offset_contaminated_us == 5_000_000
        assert artifact.delivery_offset.host_clock_offset_characterised is False

    async def test_a_post_with_no_creation_time_is_refused_not_stamped_with_ours(
        self, tmp_path
    ):
        """The failure this milestone exists to prevent.

        If a missing `created_at` silently became our receipt time, the delivery
        offset would be exactly zero and the pipeline would look flawless.
        """

        raw = json.dumps({"data": {"id": "1", "author_id": "a", "text": "x"}}).encode()
        harness = Harness(tmp_path, frames=[frame(raw)])
        report = await harness.collector.run()

        assert report.artifacts_written == 0
        assert report.unparseable_frames == 1
        harness.tape.close()
        events = [
            r for r in harness.records()
            if r["record_kind"] == RecordKind.STREAM_EVENT.value
            and r["payload"].get("event_type") == "unparseable_frame"
        ]
        assert len(events) == 1
        # The receipt time IS recorded for the refused frame — we still know
        # exactly when the bytes arrived, we simply refuse to invent the rest.
        detail = events[0]["payload"]["detail"]
        assert detail["our_received_at"]["clock_owner"] == "COLLECTOR"
        assert "raw_content_hash" in detail

    async def test_a_naive_creation_time_is_refused(self, tmp_path):
        harness = Harness(
            tmp_path, frames=[frame(post_bytes(created_at="2026-08-20T12:00:00"))]
        )
        report = await harness.collector.run()
        assert report.unparseable_frames == 1
        assert report.artifacts_written == 0

    async def test_backfilled_items_are_not_marked_live(self, tmp_path):
        harness = Harness(
            tmp_path,
            frames=[
                frame(post_bytes("1001"), kind=FrameKind.DATA, seq=0),
                frame(post_bytes("1002"), kind=FrameKind.BACKFILL, seq=1),
            ],
        )
        report = await harness.collector.run()
        assert report.backfill_frames == 1
        harness.tape.close()
        live, backfilled = list(replay(harness.tape.directory))
        assert live.delivery_mode is DeliveryMode.LIVE
        assert live.is_live_delivery
        assert backfilled.delivery_mode is DeliveryMode.BACKFILL
        assert not backfilled.is_live_delivery


# --------------------------------------------------------------------------
# 6. STREAM LIFECYCLE
# --------------------------------------------------------------------------


class TestStreamLifecycle:
    async def test_positive_control_a_reconnect_moves_the_generation(self, tmp_path):
        """Doctrine 7: force a reconnect, the generation MUST change."""

        harness = Harness(
            tmp_path,
            frames=[
                frame(post_bytes("1001"), seq=0, generation=0),
                control_frame(FrameKind.RECONNECT),
                frame(post_bytes("1002"), seq=0, generation=1),
            ],
        )
        assert harness.collector._generation == 0
        report = await harness.collector.run()
        assert report.reconnects == 1
        assert report.subscription_generation == 1

    async def test_a_disconnect_writes_a_typed_absence(self, tmp_path):
        harness = Harness(
            tmp_path, frames=[control_frame(FrameKind.RECONNECT)]
        )
        await harness.collector.run()
        harness.tape.close()
        absences = [
            r for r in harness.records()
            if r["record_kind"] == RecordKind.ABSENCE.value
        ]
        assert len(absences) == 1
        assert absences[0]["payload"]["reason"] == "stream_disconnected"
        assert absences[0]["payload"]["detail"]["new_subscription_generation"] == 1

    async def test_the_same_post_across_a_reconnect_is_a_restream_not_a_drop(
        self, tmp_path
    ):
        raw = post_bytes("1001")
        harness = Harness(
            tmp_path,
            frames=[
                frame(raw, seq=0, generation=0),
                control_frame(FrameKind.RECONNECT),
                frame(raw, seq=0, generation=1),
            ],
        )
        report = await harness.collector.run()
        assert report.artifacts_written == 1
        assert report.redeliveries_written == 1
        harness.tape.close()
        kinds = [r["record_kind"] for r in harness.records()]
        assert kinds.count(RecordKind.REDELIVERY.value) == 1

    async def test_keepalives_are_counted_not_treated_as_data(self, tmp_path):
        harness = Harness(
            tmp_path,
            frames=[control_frame(FrameKind.KEEPALIVE), frame(post_bytes())],
        )
        report = await harness.collector.run()
        assert report.keepalives == 1
        assert report.data_frames == 1
        assert report.reads_reserved == 1

    async def test_platform_errors_are_recorded(self, tmp_path):
        harness = Harness(tmp_path, frames=[control_frame(FrameKind.ERROR)])
        report = await harness.collector.run()
        assert report.errors == 1
        harness.tape.close()
        events = [
            r["payload"].get("event_type") for r in harness.records()
            if r["record_kind"] == RecordKind.STREAM_EVENT.value
        ]
        assert "platform_error" in events

    async def test_max_frames_stops_the_run(self, tmp_path):
        frames = [frame(post_bytes(str(1000 + i)), seq=i) for i in range(10)]
        harness = Harness(tmp_path, frames=frames)
        report = await harness.collector.run(max_frames=4)
        assert report.stopped_reason == "max_frames"
        assert report.frames_seen == 4

    async def test_the_run_report_is_written_to_the_tape(self, tmp_path):
        harness = Harness(tmp_path, frames=[frame(post_bytes())])
        await harness.collector.run()
        harness.tape.close()
        events = [
            r for r in harness.records()
            if r["payload"].get("event_type") == "collector_stopped"
        ]
        assert len(events) == 1
        assert events[0]["payload"]["detail"]["artifacts_written"] == 1
        assert "ledger_counters" in events[0]["payload"]["detail"]


# --------------------------------------------------------------------------
# 7. RULE MANAGEMENT
# --------------------------------------------------------------------------


class TestRuleManagement:
    async def test_rules_are_reconciled_and_the_change_is_recorded(self, tmp_path):
        harness = Harness(tmp_path, frames=[])
        result = await harness.collector.reconcile_rules()
        assert set(result.added) == {RULE_ID, OTHER_RULE_ID}
        harness.tape.close()
        events = [
            r for r in harness.records()
            if r["payload"].get("event_type") == "rule_reconciliation"
        ]
        assert len(events) == 1
        assert events[0]["payload"]["detail"]["universe"]["rule_count"] == 2

    async def test_foreign_rules_are_left_alone(self, tmp_path):
        """A rule we do not name may belong to another user of the credential.

        Deleting it would be a cross-tenant mutation performed by a read-only
        collector.
        """

        foreign = TransportRule(
            remote_id="remote:99", tag="someone.elses.rule", value="from:THEM"
        )
        harness = Harness(tmp_path, frames=[], rules=[foreign])
        result = await harness.collector.reconcile_rules()
        assert result.foreign == ("someone.elses.rule",)
        assert "someone.elses.rule" not in result.deleted
        remaining = {r.tag for r in await harness.transport.list_rules()}
        assert "someone.elses.rule" in remaining

    async def test_an_item_attributed_to_an_unknown_rule_is_refused(self, tmp_path):
        harness = Harness(
            tmp_path,
            frames=[frame(post_bytes(), rules=("not.in.our.universe",))],
        )
        report = await harness.collector.run()
        assert report.artifacts_written == 0
        assert report.unparseable_frames == 1

    async def test_an_item_with_no_rule_attribution_is_refused(self, tmp_path):
        harness = Harness(tmp_path, frames=[frame(post_bytes(), rules=())])
        report = await harness.collector.run()
        assert report.artifacts_written == 0
        assert report.unparseable_frames == 1

    async def test_retired_rules_are_not_pushed_to_the_platform(self, tmp_path):
        retired = SourceUniverse(
            universe_id="u",
            rules=(
                SourceRule(
                    rule_id=RULE_ID,
                    platform=Platform.X,
                    kind=RuleKind.ACCOUNT,
                    selector="from:EXAMPLE",
                    rationale="a defensible reason to collect this",
                    active_from="2026-09-01",
                    active_until="2026-09-30",
                ),
            ),
        )
        harness = Harness(tmp_path, frames=[])
        harness.collector._universe = retired
        result = await harness.collector.reconcile_rules()
        assert result.added == ()


# --------------------------------------------------------------------------
# 8. END TO END
# --------------------------------------------------------------------------


class TestEndToEnd:
    async def test_a_full_session_verifies_and_replays(self, tmp_path):
        mint = "So11111111111111111111111111111111111111112"
        original = post_bytes("1001", author_id="a1", text=f"gm {mint}")
        retweet = post_bytes(
            "1002",
            author_id="a2",
            text=f"gm {mint}",
            referenced=[{"type": "retweeted", "id": "1001", "author_id": "a1"}],
        )
        harness = Harness(
            tmp_path,
            budget=50,
            frames=[
                frame(original, seq=0),
                frame(original, seq=0),  # redelivery
                control_frame(FrameKind.KEEPALIVE),
                frame(retweet, seq=1),  # propagation
                control_frame(FrameKind.RECONNECT),
                frame(post_bytes("1003", text="unrelated"), seq=0, generation=1),
            ],
        )
        report = await harness.collector.run()
        manifest = harness.tape.close()

        assert report.frames_seen == 6
        assert report.artifacts_written == 3
        assert report.redeliveries_written == 1
        assert report.propagation_events == 1
        assert report.reconnects == 1
        assert report.reads_reserved == 4

        verdict = verify_segment(harness.tape.directory)
        assert verdict.ok, verdict.reason
        assert verdict.artifact_count == 3
        assert manifest["universe"]["universe_id"] == "test-universe"

        artifacts = list(replay(harness.tape.directory))
        assert [a.message_id for a in artifacts] == ["1001", "1002", "1003"]

        # Propagation preserved as a relation, not collapsed into a dupe.
        assert artifacts[1].parent.kind is PropagationKind.REBROADCAST
        assert artifacts[1].parent.parent_message_id == "1001"
        assert artifacts[0].content_identity == artifacts[1].content_identity
        assert artifacts[0].message_identity != artifacts[1].message_identity

        # Resolution ran, conservatively.
        assert artifacts[0].entity_resolution.resolved_mint == mint
        assert (
            artifacts[0].entity_resolution.confidence
            is ResolutionConfidence.CANDIDATE
        )
        assert (
            artifacts[2].entity_resolution.confidence
            is ResolutionConfidence.UNRESOLVED
        )

        # Later-milestone fields are ABSENT on every record.
        for artifact in artifacts:
            assert artifact.first_onchain_reaction.state is DeferredState.ABSENT
            assert artifact.first_price_reaction.state is DeferredState.ABSENT

    async def test_reply_and_quote_are_distinguished_from_rebroadcast(self, tmp_path):
        harness = Harness(
            tmp_path,
            frames=[
                frame(
                    post_bytes(
                        "2001",
                        referenced=[{"type": "quoted", "id": "1001"}],
                    ),
                    seq=0,
                ),
                frame(
                    post_bytes(
                        "2002",
                        text="different",
                        referenced=[{"type": "replied_to", "id": "1001"}],
                    ),
                    seq=1,
                ),
            ],
        )
        await harness.collector.run()
        harness.tape.close()
        quote, reply = list(replay(harness.tape.directory))
        assert quote.parent.kind is PropagationKind.QUOTE
        assert quote.is_propagation
        assert reply.parent.kind is PropagationKind.REPLY
        assert not reply.is_propagation

    async def test_missing_referenced_tweets_is_not_provided_not_original(
        self, tmp_path
    ):
        harness = Harness(tmp_path, frames=[frame(post_bytes("3001"))])
        await harness.collector.run()
        harness.tape.close()
        (artifact,) = list(replay(harness.tape.directory))
        assert artifact.parent.kind is PropagationKind.NOT_PROVIDED

    async def test_media_is_referenced_never_fetched(self, tmp_path):
        raw = json.dumps(
            {
                "data": {
                    "id": "4001",
                    "author_id": "a1",
                    "text": "look",
                    "created_at": "2026-08-20T12:00:00.000Z",
                },
                "includes": {
                    "media": [
                        {"media_key": "k1", "type": "photo", "url": "https://x/y"}
                    ]
                },
            }
        ).encode()
        harness = Harness(tmp_path, frames=[frame(raw)])
        await harness.collector.run()
        harness.tape.close()
        (artifact,) = list(replay(harness.tape.directory))
        assert len(artifact.media) == 1
        assert artifact.media[0].media_key == "k1"
        assert artifact.media[0].retrieved is False


class TestSeam:
    """Doctrine 5: prove observable state changes through the REAL collaborators.

    A unit suite cannot catch an unreachable module, because from inside the
    module everything works. These assertions instantiate the real tape writer,
    the real cost guard and the real ledger, drive the real collector path, and
    check state OUTSIDE the collector: bytes on disk, a persisted counter, and
    ledger membership.

    Known and deliberate gap, recorded rather than hidden: nothing in `app/`
    imports `app.social` — there is no CLI command, no flag, and no scheduled
    caller, because building one is part of the activation decision and this
    milestone activates nothing. The seam proven here is collector-to-
    collaborators, NOT collector-to-production.
    """

    async def test_the_tape_file_actually_grows_on_disk(self, tmp_path):
        harness = Harness(tmp_path, frames=[frame(post_bytes("1001"))])
        events = harness.tape.directory / "events.jsonl.gz"
        before = events.stat().st_size
        await harness.collector.run()
        harness.tape.close()
        assert events.stat().st_size > before
        assert (harness.tape.directory / "manifest.json").exists()

    async def test_the_persisted_cost_counter_actually_moves(self, tmp_path):
        ledger_path = tmp_path / "cost.json"
        harness = Harness(
            tmp_path,
            frames=[frame(post_bytes("1001"), seq=0), frame(post_bytes("1002"), seq=1)],
        )
        assert harness.guard.read_state().consumed == 0
        await harness.collector.run()
        # Re-read from disk through a NEW guard: the count is durable, not
        # merely in-memory.
        reborn = MonthlyReadCostGuard(ledger_path, CostBudget(max_reads_per_month=100))
        assert reborn.read_state().consumed == 2

    async def test_the_shared_ledger_instance_is_the_one_that_is_updated(
        self, tmp_path
    ):
        harness = Harness(tmp_path, frames=[frame(post_bytes("1001"))])
        assert harness.ledger.counters()["tracked_messages"] == 0
        await harness.collector.run()
        assert harness.ledger.counters()["tracked_messages"] == 1

    def test_no_module_in_app_imports_app_social(self):
        """States the gap as a fact so a future milestone must change it.

        If this ever fails, `app.social` has gained a production caller — which
        is exactly the activation event this milestone forbids, so the failure
        is the alarm, not a nuisance.
        """

        importers: list[str] = []
        for path in APP_ROOT.rglob("*.py"):
            if path.is_relative_to(SOCIAL_PACKAGE):
                continue
            if "app.social" in path.read_text(encoding="utf-8"):
                importers.append(str(path.relative_to(APP_ROOT)))
        assert importers == [], (
            "app/social is now reachable from production code; SOCIAL-TAPE-001 "
            f"activates nothing, so this needs an explicit decision: {importers}"
        )


class TestPayloadParsing:
    def test_non_json_is_refused(self):
        with pytest.raises(XPayloadError):
            parse_x_payload(b"<html>")

    def test_non_object_is_refused(self):
        with pytest.raises(XPayloadError):
            parse_x_payload(b"[1,2,3]")

    def test_valid_payload_parses(self):
        assert parse_x_payload(post_bytes())["data"]["id"] == "1001"
