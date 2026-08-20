"""SOCIAL-TAPE-001 — schema, timebase, tape and dedupe.

Fixture-driven. No network, no credentials, no live transport exists.
"""

from __future__ import annotations

import ast
import datetime as dt
import gzip
import json
from pathlib import Path

import pytest

from app.social.artifact import (
    INGESTION_VERSION,
    ArtifactSchemaError,
    Deferred,
    DeferredState,
    DeliveryMode,
    EntityResolution,
    MediaRef,
    ParentRef,
    Platform,
    PropagationKind,
    ResolutionConfidence,
    SocialArtifact,
    raw_content_digest,
)
from app.social.dedupe import DedupeVerdict, PropagationLedger
from app.social.resolution import ConservativeAddressResolver, NullResolver
from app.social.sources import (
    EMPTY_SOURCE_UNIVERSE,
    RuleKind,
    SourceRule,
    SourceUniverse,
    SourceUniverseError,
    load_source_universe,
)
from app.social.tape import (
    RecordKind,
    SocialTapeWriter,
    TapeImmutabilityError,
    TapeIntegrityError,
    fold_stream_digest,
    genesis_digest,
    read_segment_records,
    replay,
    verify_chain,
    verify_segment,
)
from app.social.timebase import (
    ClockConfusionError,
    CrossEpochIntervalError,
    OurReceivedAt,
    SourceCreatedAt,
    SourceTimeFidelity,
    capture_receipt,
    delivery_offset,
    pipeline_interval_us,
    process_epoch,
)

SOCIAL_PACKAGE = Path(__file__).resolve().parents[1] / "app" / "social"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_source_created_at(
    text: str = "2026-08-20T12:00:00.000Z",
) -> SourceCreatedAt:
    cleaned = text[:-1] + "+00:00" if text.endswith("Z") else text
    return SourceCreatedAt.from_platform(
        text,
        source_field="created_at",
        parsed=dt.datetime.fromisoformat(cleaned),
    )


def make_artifact(
    epoch,
    *,
    message_id: str = "m1",
    author_id: str = "a1",
    body: bytes = b'{"text":"hello"}',
    delivery_sequence: int = 0,
    subscription_generation: int = 0,
    delivery_mode: DeliveryMode = DeliveryMode.LIVE,
    parent: ParentRef | None = None,
    clock=None,
) -> SocialArtifact:
    received = capture_receipt(epoch, clock=clock) if clock else capture_receipt(epoch)
    return SocialArtifact(
        platform=Platform.X,
        source_id="acct:1",
        message_id=message_id,
        author_id=author_id,
        source_created_at=make_source_created_at(),
        our_received_at=received,
        raw_content=body,
        raw_content_hash=raw_content_digest(body),
        matching_rule="caller.example-account-01",
        parent=parent or ParentRef(kind=PropagationKind.ORIGINAL),
        delivery_mode=delivery_mode,
        delivery_sequence=delivery_sequence,
        subscription_generation=subscription_generation,
    )


# --------------------------------------------------------------------------
# 1. TIMESTAMP SEMANTICS — the point of the milestone
# --------------------------------------------------------------------------


class TestTimestampSemantics:
    def test_our_received_at_cannot_be_built_from_a_platform_timestamp(self):
        """The structural guarantee: no factory converts one into the other."""

        source = make_source_created_at()
        # SourceCreatedAt carries no monotonic stamp and no epoch, so there is
        # nothing to build an OurReceivedAt from.
        assert not hasattr(source, "monotonic_ns")
        assert not hasattr(source, "epoch_id")
        with pytest.raises((TypeError, ClockConfusionError)):
            OurReceivedAt(value=source.value)  # type: ignore[call-arg]

    def test_our_received_at_requires_an_epoch(self):
        with pytest.raises(ClockConfusionError):
            OurReceivedAt(
                value="2026-08-20T12:00:00.000000Z", monotonic_ns=1, epoch_id=""
            )

    def test_receipt_is_read_from_the_clock_not_from_the_payload(self):
        """A platform time far in the past must not become our receipt time."""

        epoch = process_epoch()
        fixed = dt.datetime(2026, 8, 20, 18, 0, 0, tzinfo=dt.timezone.utc)
        received = capture_receipt(epoch, clock=lambda: (fixed, 12_345))
        source = make_source_created_at("2026-08-20T12:00:00.000Z")

        assert received.value != source.value
        assert received.value.startswith("2026-08-20T18:00:00")
        assert received.epoch_id == epoch.epoch_id

    def test_no_source_module_populates_our_received_at_from_source_time(self):
        """AST guard: nothing in app/social/ assigns platform time into the
        collector-receipt position.

        A grep would be fooled by formatting; this walks every keyword argument
        and every attribute assignment named `our_received_at`/`received` and
        asserts the value expression never mentions `source_created_at`.
        """

        offenders: list[str] = []
        for path in sorted(SOCIAL_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[ast.AST] = []
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg in {"our_received_at", "received"}:
                            targets.append(kw.value)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    names = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                    for name in names:
                        text = ast.dump(name)
                        if "our_received_at" in text and node.value is not None:
                            targets.append(node.value)
                for value in targets:
                    dumped = ast.dump(value)
                    if "source_created_at" in dumped or "SourceCreatedAt" in dumped:
                        offenders.append(f"{path.name}: {ast.dump(value)[:120]}")
        assert offenders == [], (
            "platform creation time is being routed into our receipt time: "
            f"{offenders}"
        )

    def test_positive_control_the_ast_guard_can_fail(self):
        """Doctrine 7: prove the guard becomes non-benign when the condition
        it screens for actually occurs."""

        bad = ast.parse(
            "SocialArtifact(our_received_at=source_created_at, x=1)\n"
        )
        hits = [
            kw
            for node in ast.walk(bad)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "our_received_at"
            and "source_created_at" in ast.dump(kw.value)
        ]
        assert len(hits) == 1

    def test_pipeline_interval_refuses_platform_time(self):
        epoch = process_epoch()
        received = capture_receipt(epoch)
        with pytest.raises(ClockConfusionError):
            pipeline_interval_us(make_source_created_at(), received)  # type: ignore[arg-type]

    def test_pipeline_interval_refuses_cross_epoch(self):
        a = capture_receipt(process_epoch())
        b = capture_receipt(process_epoch())
        with pytest.raises(CrossEpochIntervalError):
            pipeline_interval_us(a, b)

    def test_pipeline_interval_works_within_one_epoch(self):
        epoch = process_epoch()
        a = capture_receipt(epoch, clock=lambda: (dt.datetime.now(dt.timezone.utc), 1_000_000))
        b = capture_receipt(epoch, clock=lambda: (dt.datetime.now(dt.timezone.utc), 3_000_000))
        assert pipeline_interval_us(a, b) == 2_000

    def test_delivery_offset_is_integer_microseconds_and_may_be_negative(self):
        epoch = process_epoch()
        early = dt.datetime(2026, 8, 20, 11, 59, 59, tzinfo=dt.timezone.utc)
        received = capture_receipt(epoch, clock=lambda: (early, 1))
        offset = delivery_offset(make_source_created_at(), received)
        assert isinstance(offset.offset_contaminated_us, int)
        assert offset.offset_contaminated_us == -1_000_000
        # A negative sample is EVIDENCE of clock offset, and is kept.
        assert offset.host_clock_offset_characterised is False

    def test_offset_carries_the_unverified_fidelity_of_its_source_field(self):
        epoch = process_epoch()
        received = capture_receipt(epoch)
        offset = delivery_offset(make_source_created_at(), received)
        assert offset.source_time_fidelity is SourceTimeFidelity.UNVERIFIED

    def test_naive_platform_timestamps_are_refused(self):
        with pytest.raises(ClockConfusionError):
            SourceCreatedAt.from_platform(
                "2026-08-20T12:00:00",
                source_field="created_at",
                parsed=dt.datetime(2026, 8, 20, 12, 0, 0),
            )

    def test_source_created_at_preserves_the_raw_platform_bytes(self):
        source = make_source_created_at("2026-08-20T12:00:00.000Z")
        assert source.raw_value == "2026-08-20T12:00:00.000Z"
        assert source.value == "2026-08-20T12:00:00.000000Z"
        assert source.source_field == "created_at"


# --------------------------------------------------------------------------
# 2. SCHEMA
# --------------------------------------------------------------------------


class TestArtifactSchema:
    def test_round_trip(self):
        epoch = process_epoch()
        artifact = make_artifact(epoch)
        assert SocialArtifact.from_json(artifact.to_json()) == artifact

    def test_deferred_fields_are_structurally_present_and_absent(self):
        artifact = make_artifact(process_epoch())
        for name in ("first_onchain_reaction", "first_price_reaction"):
            value = getattr(artifact, name)
            assert isinstance(value, Deferred)
            assert value.state is DeferredState.ABSENT
            assert value.is_absent
            assert artifact.to_json()[name] == {
                "state": "ABSENT",
                "observed_at": None,
                "detail": None,
            }

    def test_absent_is_not_zero_and_cannot_carry_a_value(self):
        with pytest.raises(ArtifactSchemaError):
            Deferred(state=DeferredState.ABSENT, observed_at="2026-08-20T00:00:00Z")
        with pytest.raises(ArtifactSchemaError):
            Deferred(state=DeferredState.ABSENT, detail={"price_move": 0})

    def test_observed_none_is_distinguishable_from_absent(self):
        observed_none = Deferred(
            state=DeferredState.OBSERVED_NONE,
            observed_at="2026-08-20T00:00:00.000000Z",
        )
        assert not observed_none.is_absent
        assert observed_none.state is not DeferredState.ABSENT

    def test_observed_requires_a_time_and_a_payload(self):
        with pytest.raises(ArtifactSchemaError):
            Deferred(state=DeferredState.OBSERVED, detail={"x": 1})
        with pytest.raises(ArtifactSchemaError):
            Deferred(
                state=DeferredState.OBSERVED,
                observed_at="2026-08-20T00:00:00.000000Z",
            )

    def test_raw_content_hash_must_match_the_bytes(self):
        epoch = process_epoch()
        with pytest.raises(ArtifactSchemaError):
            SocialArtifact(
                platform=Platform.X,
                source_id="s",
                message_id="m",
                author_id="a",
                source_created_at=make_source_created_at(),
                our_received_at=capture_receipt(epoch),
                raw_content=b"one",
                raw_content_hash=raw_content_digest(b"two"),
                matching_rule="r",
                parent=ParentRef(kind=PropagationKind.ORIGINAL),
                delivery_mode=DeliveryMode.LIVE,
            )

    def test_raw_content_hash_is_over_bytes_not_text(self):
        with pytest.raises(ArtifactSchemaError):
            raw_content_digest("not bytes")  # type: ignore[arg-type]

    def test_every_artifact_names_its_matching_rule(self):
        epoch = process_epoch()
        with pytest.raises(ArtifactSchemaError):
            SocialArtifact(
                platform=Platform.X,
                source_id="s",
                message_id="m",
                author_id="a",
                source_created_at=make_source_created_at(),
                our_received_at=capture_receipt(epoch),
                raw_content=b"x",
                raw_content_hash=raw_content_digest(b"x"),
                matching_rule="",
                parent=ParentRef(kind=PropagationKind.ORIGINAL),
                delivery_mode=DeliveryMode.LIVE,
            )

    def test_unknown_delivery_mode_is_not_treated_as_live(self):
        artifact = make_artifact(
            process_epoch(), delivery_mode=DeliveryMode.UNKNOWN
        )
        assert artifact.is_live_delivery is False

    def test_backfill_is_not_live(self):
        artifact = make_artifact(
            process_epoch(), delivery_mode=DeliveryMode.BACKFILL
        )
        assert artifact.is_live_delivery is False

    def test_parent_claiming_a_relation_must_name_a_parent(self):
        with pytest.raises(ArtifactSchemaError):
            ParentRef(kind=PropagationKind.REBROADCAST)
        with pytest.raises(ArtifactSchemaError):
            ParentRef(kind=PropagationKind.ORIGINAL, parent_message_id="x")

    def test_not_provided_is_distinct_from_original(self):
        assert (
            ParentRef(kind=PropagationKind.NOT_PROVIDED).kind
            is not PropagationKind.ORIGINAL
        )

    def test_media_references_are_never_marked_retrieved(self):
        ref = MediaRef(media_key="k", media_type="photo", url="https://x/y")
        assert ref.retrieved is False

    def test_ambiguous_resolution_cannot_collapse_to_one_mint(self):
        with pytest.raises(ArtifactSchemaError):
            EntityResolution(
                confidence=ResolutionConfidence.AMBIGUOUS, resolved_mint="A"
            )

    def test_unresolved_carries_no_mint(self):
        with pytest.raises(ArtifactSchemaError):
            EntityResolution(
                confidence=ResolutionConfidence.UNRESOLVED, resolved_mint="A"
            )

    def test_candidate_requires_a_mint(self):
        with pytest.raises(ArtifactSchemaError):
            EntityResolution(confidence=ResolutionConfidence.CANDIDATE)

    def test_unknown_ingestion_version_is_refused(self):
        epoch = process_epoch()
        payload = make_artifact(epoch).to_json()
        payload["ingestion_version"] = "some-future-writer.v9"
        with pytest.raises(ArtifactSchemaError):
            SocialArtifact.from_json(payload)

    def test_ingestion_version_is_on_every_record(self):
        assert make_artifact(process_epoch()).ingestion_version == INGESTION_VERSION


# --------------------------------------------------------------------------
# 3. PROPAGATION IDENTITY
# --------------------------------------------------------------------------


class TestPropagationIdentity:
    def test_redelivery_of_the_same_delivery_is_transport_noise(self):
        epoch = process_epoch()
        ledger = PropagationLedger()
        artifact = make_artifact(epoch, message_id="m1", delivery_sequence=7)
        assert ledger.record(artifact).verdict is DedupeVerdict.NOVEL
        again = ledger.record(artifact)
        assert again.verdict is DedupeVerdict.REDELIVERY
        assert again.is_transport_duplicate
        assert not again.is_spread

    def test_same_post_on_a_later_delivery_is_a_restream(self):
        epoch = process_epoch()
        ledger = PropagationLedger()
        ledger.record(make_artifact(epoch, message_id="m1", delivery_sequence=1))
        decision = ledger.record(
            make_artifact(
                epoch, message_id="m1", delivery_sequence=2, subscription_generation=1
            )
        )
        assert decision.verdict is DedupeVerdict.RESTREAM
        assert decision.is_transport_duplicate

    def test_same_content_from_a_new_message_is_PROPAGATION_not_duplicate(self):
        """The measurement this whole module exists to protect."""

        epoch = process_epoch()
        ledger = PropagationLedger()
        first = make_artifact(epoch, message_id="m1", author_id="a1")
        ledger.record(first)

        spread = make_artifact(
            epoch,
            message_id="m2",
            author_id="a2",
            delivery_sequence=1,
            parent=ParentRef(
                kind=PropagationKind.REBROADCAST, parent_message_id="m1"
            ),
        )
        decision = ledger.record(spread)

        assert decision.verdict is DedupeVerdict.PROPAGATION
        assert decision.is_spread
        assert not decision.is_transport_duplicate
        assert decision.first_seen_message_identity == first.message_identity
        assert decision.first_seen_our_received_at == first.our_received_at.value
        assert decision.prior_distinct_messages == 1

    def test_edited_post_under_a_stable_id_is_a_revision(self):
        epoch = process_epoch()
        ledger = PropagationLedger()
        ledger.record(make_artifact(epoch, message_id="m1", body=b'{"text":"a"}'))
        decision = ledger.record(
            make_artifact(
                epoch, message_id="m1", body=b'{"text":"b"}', delivery_sequence=1
            )
        )
        assert decision.verdict is DedupeVerdict.REVISION

    def test_three_identity_levels_are_distinct(self):
        epoch = process_epoch()
        a = make_artifact(epoch, message_id="m1", delivery_sequence=1)
        b = make_artifact(epoch, message_id="m1", delivery_sequence=2)
        c = make_artifact(epoch, message_id="m2", delivery_sequence=3)

        assert a.message_identity == b.message_identity
        assert a.delivery_identity != b.delivery_identity
        assert a.content_identity == c.content_identity
        assert a.message_identity != c.message_identity

    def test_content_identity_normalisation_is_conservative(self):
        epoch = process_epoch()
        a = make_artifact(epoch, message_id="m1", body=b'{"text": "hello"}')
        b = make_artifact(epoch, message_id="m2", body=b'{"text":  "hello"}')
        # Whitespace collapses...
        assert a.content_identity == b.content_identity
        c = make_artifact(epoch, message_id="m3", body=b'{"text":"HELLO"}')
        # ...but case does NOT, so genuinely different posts stay different.
        assert a.content_identity != c.content_identity

    def test_positive_control_eviction_counter_becomes_non_benign(self):
        """Force the condition; the metric must move (doctrine 7)."""

        epoch = process_epoch()
        ledger = PropagationLedger(capacity=2)
        assert ledger.counters()["evicted_content_keys"] == 0
        for i in range(6):
            ledger.record(
                make_artifact(
                    epoch,
                    message_id=f"m{i}",
                    body=f'{{"text":"post-{i}"}}'.encode(),
                    delivery_sequence=i,
                )
            )
        assert ledger.counters()["evicted_content_keys"] > 0

    def test_ledger_can_be_primed_from_tape(self):
        epoch = process_epoch()
        ledger = PropagationLedger()
        artifacts = [
            make_artifact(epoch, message_id=f"m{i}", delivery_sequence=i)
            for i in range(3)
        ]
        ledger.prime(artifacts)
        assert ledger.counters()["tracked_messages"] == 3


# --------------------------------------------------------------------------
# 4. THE TAPE
# --------------------------------------------------------------------------


class TestTape:
    def _writer(self, tmp_path: Path, epoch, segment_id="seg-1", **kw):
        return SocialTapeWriter(
            tmp_path / "tape",
            environment="test",
            segment_id=segment_id,
            universe_fields={"universe_id": "u1", "rule_count": 1},
            process_epoch=epoch,
            ingestion_version=INGESTION_VERSION,
            **kw,
        )

    def test_write_verify_replay_round_trip(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        a = make_artifact(epoch, message_id="m1")
        b = make_artifact(epoch, message_id="m2", delivery_sequence=1)
        writer.append_artifact(a)
        writer.append_stream_event("reconnect", {"generation": 1})
        writer.append_artifact(b)
        manifest = writer.close()

        assert manifest["record_count"] == 3
        assert manifest["artifact_count"] == 2
        assert manifest["close_status"] == "clean"

        verdict = verify_segment(writer.directory)
        assert verdict.ok, verdict.reason
        assert verdict.record_count == 3
        assert verdict.artifact_count == 2

        replayed = list(replay(writer.directory))
        assert replayed == [a, b]

    def test_raw_bytes_survive_verbatim(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        body = '{"text":"caf\\u00e9 \\ud83d\\ude80 unicode"}'.encode("utf-8")
        artifact = make_artifact(epoch, body=body)
        writer.append_artifact(artifact)
        writer.close()
        (replayed,) = list(replay(writer.directory))
        assert replayed.raw_content == body
        assert replayed.raw_content_hash == raw_content_digest(body)

    def test_genesis_digest_is_bound_to_segment_identity(self):
        a = genesis_digest(segment_id="seg-1", environment="test")
        b = genesis_digest(segment_id="seg-2", environment="test")
        c = genesis_digest(segment_id="seg-1", environment="demo")
        assert a != b != c and a != c

    def test_a_record_spliced_from_another_segment_breaks_the_chain(self, tmp_path):
        epoch = process_epoch()
        w1 = self._writer(tmp_path, epoch, segment_id="seg-1")
        r1 = w1.append_artifact(make_artifact(epoch, message_id="m1"))
        w1.close()
        verdict = verify_chain([r1], segment_id="seg-2", environment="test")
        assert not verdict.ok
        assert verdict.broken_at == 0

    def test_reordering_records_is_detected_even_though_self_digests_hold(
        self, tmp_path
    ):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        r0 = writer.append_artifact(make_artifact(epoch, message_id="m1"))
        r1 = writer.append_artifact(
            make_artifact(epoch, message_id="m2", delivery_sequence=1)
        )
        writer.close()

        from app.social.tape import verify_record_self_digest

        assert verify_record_self_digest(r0) and verify_record_self_digest(r1)
        assert not verify_chain(
            [r1, r0], segment_id="seg-1", environment="test"
        ).ok

    def test_ordered_stream_digest_folds_position(self):
        assert fold_stream_digest("a", "b") != fold_stream_digest("b", "a")

    def test_tampered_payload_fails_verification(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        writer.append_artifact(make_artifact(epoch, message_id="m1"))
        writer.close()

        events = writer.directory / "events.jsonl.gz"
        raw = gzip.open(events, "rb").read()
        tampered = raw.replace(b'"author_id":"a1"', b'"author_id":"a9"')
        assert tampered != raw
        with gzip.open(events, "wb") as handle:
            handle.write(tampered)

        verdict = verify_segment(writer.directory)
        assert not verdict.ok
        with pytest.raises(TapeIntegrityError):
            list(replay(writer.directory))

    def test_manifest_tamper_is_detected(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        writer.append_artifact(make_artifact(epoch, message_id="m1"))
        writer.close()

        manifest_path = writer.directory / "manifest.json"
        body = json.loads(manifest_path.read_text())
        body["record_count"] = 99
        manifest_path.write_text(json.dumps(body))
        verdict = verify_segment(writer.directory)
        assert not verdict.ok
        assert "self-digest" in (verdict.reason or "")

    def test_a_closed_segment_refuses_further_records(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        writer.append_artifact(make_artifact(epoch))
        writer.close()
        with pytest.raises(TapeImmutabilityError):
            writer.append_stream_event("late", {})
        with pytest.raises(TapeImmutabilityError):
            writer.close()

    def test_a_committed_segment_cannot_be_reopened(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        writer.append_artifact(make_artifact(epoch))
        writer.close()
        with pytest.raises(TapeImmutabilityError):
            self._writer(tmp_path, epoch, segment_id="seg-1")

    def test_segments_chain_to_their_predecessor(self, tmp_path):
        epoch = process_epoch()
        w1 = self._writer(tmp_path, epoch, segment_id="seg-1")
        w1.append_artifact(make_artifact(epoch))
        m1 = w1.close()
        w2 = self._writer(
            tmp_path,
            epoch,
            segment_id="seg-2",
            previous_segment_digest=m1["manifest_digest"],
        )
        w2.append_artifact(make_artifact(epoch, message_id="m2"))
        m2 = w2.close()
        assert m2["previous_segment_digest"] == m1["manifest_digest"]
        assert verify_segment(w2.directory).ok

    def test_rotation_is_reported_at_the_bound(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch, max_records_per_segment=2)
        assert not writer.rotation_due()
        writer.append_artifact(make_artifact(epoch, message_id="m1"))
        writer.append_artifact(
            make_artifact(epoch, message_id="m2", delivery_sequence=1)
        )
        assert writer.rotation_due()
        writer.close()

    def test_absence_is_recorded_as_a_typed_record(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        writer.append_absence("stream_disconnected", {"generation": 2})
        writer.close()
        records = read_segment_records(writer.directory / "events.jsonl.gz")
        assert records[0]["record_kind"] == RecordKind.ABSENCE.value
        assert records[0]["payload"]["reason"] == "stream_disconnected"

    def test_redelivery_is_written_not_dropped(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        artifact = make_artifact(epoch)
        writer.append_redelivery(artifact, verdict="REDELIVERY")
        manifest = writer.close()
        assert manifest["record_count"] == 1
        assert manifest["artifact_count"] == 0
        records = read_segment_records(writer.directory / "events.jsonl.gz")
        assert records[0]["record_kind"] == RecordKind.REDELIVERY.value

    def test_universe_is_pinned_into_the_manifest(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        manifest = writer.close()
        assert manifest["universe"]["universe_id"] == "u1"
        assert len(manifest["universe_digest"]) == 64

    def test_process_epoch_is_pinned_into_the_manifest(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        manifest = writer.close()
        assert manifest["process_epoch"]["epoch_id"] == epoch.epoch_id

    def test_uncommitted_segment_does_not_verify(self, tmp_path):
        epoch = process_epoch()
        writer = self._writer(tmp_path, epoch)
        writer.append_artifact(make_artifact(epoch))
        verdict = verify_segment(writer.directory)
        assert not verdict.ok
        assert "manifest" in (verdict.reason or "")

    def test_context_manager_marks_an_aborted_close(self, tmp_path):
        epoch = process_epoch()
        with pytest.raises(RuntimeError):
            with self._writer(tmp_path, epoch) as writer:
                writer.append_artifact(make_artifact(epoch))
                raise RuntimeError("boom")
        manifest = json.loads((writer.directory / "manifest.json").read_text())
        assert manifest["close_status"] == "aborted"
        assert verify_segment(writer.directory).ok


# --------------------------------------------------------------------------
# 5. SOURCE UNIVERSE
# --------------------------------------------------------------------------


class TestSourceUniverse:
    def test_the_shipped_universe_is_empty(self):
        assert EMPTY_SOURCE_UNIVERSE.is_empty
        assert len(EMPTY_SOURCE_UNIVERSE) == 0

    def test_loading_an_empty_universe_is_refused(self, tmp_path):
        path = tmp_path / "u.json"
        path.write_text(json.dumps({"universe_id": "u", "rules": []}))
        with pytest.raises(SourceUniverseError):
            load_source_universe(path)
        assert load_source_universe(path, allow_empty=True).is_empty

    def test_the_documented_example_set_loads_and_is_enumerable(self):
        example = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "social"
            / "source-universe.example.json"
        )
        universe = load_source_universe(example)
        assert 1 <= len(universe) <= 300
        kinds = {r.kind for r in universe.rules}
        assert RuleKind.ACCOUNT in kinds
        assert RuleKind.KEYWORD in kinds
        assert RuleKind.ADDRESS_PATTERN in kinds
        # Every example selector is a placeholder, never a live handle.
        assert all("EXAMPLE" in r.selector for r in universe.rules)

    def test_a_rule_without_a_rationale_is_refused(self):
        with pytest.raises(SourceUniverseError):
            SourceRule(
                rule_id="a.b",
                platform=Platform.X,
                kind=RuleKind.ACCOUNT,
                selector="from:x",
                rationale="short",
                active_from="2026-09-01",
            )

    def test_duplicate_rule_ids_are_refused(self):
        rule = SourceRule(
            rule_id="a.b",
            platform=Platform.X,
            kind=RuleKind.ACCOUNT,
            selector="from:x",
            rationale="a defensible reason to collect",
            active_from="2026-09-01",
        )
        with pytest.raises(SourceUniverseError):
            SourceUniverse(universe_id="u", rules=(rule, rule))

    def test_universe_larger_than_the_bound_is_refused(self):
        rules = tuple(
            SourceRule(
                rule_id=f"a.b{i}",
                platform=Platform.X,
                kind=RuleKind.ACCOUNT,
                selector=f"from:x{i}",
                rationale="a defensible reason to collect",
                active_from="2026-09-01",
            )
            for i in range(5)
        )
        with pytest.raises(SourceUniverseError):
            SourceUniverse(universe_id="u", rules=rules, max_rules=4)

    def test_retired_rules_are_kept_not_deleted(self):
        rule = SourceRule(
            rule_id="a.b",
            platform=Platform.X,
            kind=RuleKind.ACCOUNT,
            selector="from:x",
            rationale="a defensible reason to collect",
            active_from="2026-09-01",
            active_until="2026-10-01",
        )
        universe = SourceUniverse(universe_id="u", rules=(rule,))
        assert universe.by_id("a.b").active_until == "2026-10-01"


# --------------------------------------------------------------------------
# 6. ENTITY RESOLUTION
# --------------------------------------------------------------------------


class TestResolution:
    def test_no_address_is_unresolved(self):
        result = ConservativeAddressResolver().resolve("just talking about a coin")
        assert result.confidence is ResolutionConfidence.UNRESOLVED
        assert result.resolved_mint is None
        assert result.first_entity_resolution_at is not None

    def test_one_address_is_a_candidate_never_confirmed(self):
        mint = "So11111111111111111111111111111111111111112"
        result = ConservativeAddressResolver().resolve(f"look at {mint} now")
        assert result.confidence is ResolutionConfidence.CANDIDATE
        assert result.resolved_mint == mint

    def test_two_addresses_are_ambiguous_not_first_wins(self):
        a = "So11111111111111111111111111111111111111112"
        b = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        result = ConservativeAddressResolver().resolve(f"{a} vs {b}")
        assert result.confidence is ResolutionConfidence.AMBIGUOUS
        assert result.resolved_mint is None
        assert result.candidates == (a, b)

    def test_the_system_program_is_not_a_mint(self):
        result = ConservativeAddressResolver().resolve(
            "11111111111111111111111111111111"
        )
        assert result.confidence is ResolutionConfidence.UNRESOLVED

    def test_the_resolver_never_reaches_confirmed(self):
        mint = "So11111111111111111111111111111111111111112"
        result = ConservativeAddressResolver().resolve(mint)
        assert result.confidence is not ResolutionConfidence.CONFIRMED

    def test_null_resolver_resolves_nothing(self):
        result = NullResolver().resolve("So11111111111111111111111111111111111111112")
        assert result.confidence is ResolutionConfidence.UNRESOLVED

    def test_resolver_id_is_recorded(self):
        result = ConservativeAddressResolver().resolve("nothing")
        assert result.resolver_id == "conservative-base58.v1"

    def test_resolution_is_reversible_because_raw_bytes_are_kept(self, tmp_path):
        """A better resolver can redo this later from the tape."""

        epoch = process_epoch()
        mint = "So11111111111111111111111111111111111111112"
        body = json.dumps({"text": f"gm {mint}"}).encode()
        artifact = make_artifact(epoch, body=body)
        assert (
            artifact.entity_resolution.confidence is ResolutionConfidence.UNRESOLVED
        )
        redone = artifact.with_entity_resolution(
            ConservativeAddressResolver().resolve(artifact.raw_text)
        )
        assert redone.entity_resolution.resolved_mint == mint
        # The original is untouched: records are never mutated in place.
        assert (
            artifact.entity_resolution.confidence is ResolutionConfidence.UNRESOLVED
        )
