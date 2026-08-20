"""SOCIAL-TAPE-001 — the X filtered-stream collector.

Written entirely against `app.social.transport.SocialStreamTransport`. It has
no HTTP client, no credential loader, and no way to reach the network: the only
transports in this repository are a fixture replayer and a null transport that
refuses.

Order of operations at start-up, and the order matters
------------------------------------------------------
1. **Cost guard.** ``assert_startable()`` before anything else. A collector
   that is over budget must not reach the point of opening a stream.
2. **Source universe.** Refuse an empty universe. A collector matching nothing
   reports zero items and is indistinguishable from a healthy quiet stream.
3. **Rule reconciliation.** Bring the platform's rule set toward ours, and
   record the reconciliation on the tape as a ``stream_event`` — because a rule
   change alters the denominator of every statistic computed across the
   boundary, and a boundary nobody recorded is one nobody can condition on.
4. Only then consume frames.

Per-frame, and again the order matters
--------------------------------------
1. **Stamp receipt first.** ``capture_receipt`` is called before parsing, so
   ``our_received_at`` is not inflated by our own deserialisation.
2. **Reserve budget second.** Before the post is consumed, never after. A crash
   between reserve and use over-counts; the reverse under-counts, and only
   under-counting can produce an unauthorized bill.
3. **Classify** against the propagation ledger.
4. **Write** — artifact, or redelivery record. Never drop silently.

CONTAINS NO SIGNAL. The collector decides what to KEEP, never what is
important.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.social.artifact import (
    INGESTION_VERSION,
    Deferred,
    DeliveryMode,
    MediaRef,
    ParentRef,
    Platform,
    PropagationKind,
    SocialArtifact,
    raw_content_digest,
)
from app.social.cost_guard import (
    BudgetExhaustedError,
    CostGuardError,
    MonthlyReadCostGuard,
)
from app.social.dedupe import DedupeVerdict, PropagationLedger
from app.social.resolution import EntityResolver, NullResolver
from app.social.sources import SourceUniverse, SourceUniverseError
from app.social.tape import SocialTapeWriter
from app.social.timebase import (
    ProcessEpoch,
    ReceiptClock,
    SourceCreatedAt,
    SourceTimeFidelity,
    SystemReceiptClock,
    capture_receipt,
    delivery_offset,
)
from app.social.transport import (
    FrameKind,
    RuleSyncResult,
    SocialStreamTransport,
    StreamFrame,
    TransportRule,
)

__all__ = [
    "CollectorError",
    "CollectorNotStartableError",
    "CollectorReport",
    "XFilteredStreamCollector",
    "parse_x_payload",
    "XPayloadError",
]


class CollectorError(RuntimeError):
    """Base class for collector faults."""


class CollectorNotStartableError(CollectorError):
    """A precondition for starting was not met."""


class XPayloadError(CollectorError):
    """A data frame could not be understood."""


@dataclass
class CollectorReport:
    """What the run did. Counters only — no judgement about content."""

    frames_seen: int = 0
    data_frames: int = 0
    keepalives: int = 0
    errors: int = 0
    reconnects: int = 0
    backfill_frames: int = 0
    artifacts_written: int = 0
    redeliveries_written: int = 0
    propagation_events: int = 0
    revisions: int = 0
    unparseable_frames: int = 0
    reads_reserved: int = 0
    subscription_generation: int = 0
    stopped_reason: str = "stream_ended"
    #: Ledger eviction counters, surfaced rather than hidden — a fall in
    #: propagation events must be checkable against "the ledger forgot".
    ledger_counters: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "frames_seen": self.frames_seen,
            "data_frames": self.data_frames,
            "keepalives": self.keepalives,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "backfill_frames": self.backfill_frames,
            "artifacts_written": self.artifacts_written,
            "redeliveries_written": self.redeliveries_written,
            "propagation_events": self.propagation_events,
            "revisions": self.revisions,
            "unparseable_frames": self.unparseable_frames,
            "reads_reserved": self.reads_reserved,
            "subscription_generation": self.subscription_generation,
            "stopped_reason": self.stopped_reason,
            "ledger_counters": dict(self.ledger_counters),
        }


def parse_x_payload(raw: bytes) -> dict[str, Any]:
    """Decode one X data frame. Raises rather than guessing.

    Deliberately thin: the tape stores ``raw`` verbatim, so the authoritative
    copy is always the bytes, and this parse can be redone and audited later
    against ``raw_content_hash``.
    """

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise XPayloadError(f"frame is not readable JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise XPayloadError("frame is not a JSON object")
    return dict(payload)


def _parse_platform_datetime(text: str) -> datetime:
    """Parse a platform timestamp WITHOUT ever defaulting to now().

    There is no fallback branch here on purpose. A creation time we could not
    parse must not silently become our receipt time — which would make the
    delivery offset exactly zero and look like a perfectly fast pipeline.
    """

    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        raise XPayloadError(
            "platform timestamp has no timezone; reading it as local time "
            "would make it host-dependent"
        )
    return parsed.astimezone(timezone.utc)


class XFilteredStreamCollector:
    """Collect X filtered-stream frames onto an immutable tape."""

    #: Which platform field carries the creation time. Named, per §5.4: the
    #: field that supplied a value is itself an observation. Its semantics are
    #: UNVERIFIED until someone re-reads it across a known interval.
    SOURCE_TIME_FIELD = "created_at"
    SOURCE_TIME_FIDELITY = SourceTimeFidelity.UNVERIFIED

    def __init__(
        self,
        *,
        transport: SocialStreamTransport,
        tape: SocialTapeWriter,
        universe: SourceUniverse,
        cost_guard: MonthlyReadCostGuard,
        process_epoch: ProcessEpoch,
        ledger: PropagationLedger | None = None,
        resolver: EntityResolver | None = None,
        receipt_clock: ReceiptClock = SystemReceiptClock,
    ) -> None:
        if cost_guard is None:
            raise CollectorNotStartableError(
                "a cost guard is mandatory; there is no unmetered mode"
            )
        self._transport = transport
        self._tape = tape
        self._universe = universe
        self._cost_guard = cost_guard
        self._epoch = process_epoch
        self._ledger = ledger if ledger is not None else PropagationLedger()
        self._resolver = resolver if resolver is not None else NullResolver()
        self._receipt_clock = receipt_clock
        self._generation = 0

    # -- start-up -----------------------------------------------------------

    def assert_startable(self) -> None:
        """Every precondition, checked before a stream is touched."""

        # 1. Cost first: an over-budget collector must not reach the stream.
        self._cost_guard.assert_startable()

        # 2. A universe that names nothing looks exactly like a quiet market.
        if self._universe.is_empty:
            raise CollectorNotStartableError(
                "the source universe is empty; a collector with no rules "
                "cannot be distinguished from a working one"
            )

    async def reconcile_rules(self) -> RuleSyncResult:
        """Bring the platform's rule set toward our universe, and record it."""

        remote = {r.tag: r for r in await self._transport.list_rules()}
        desired = {
            rule.rule_id: TransportRule(
                remote_id=f"local:{rule.rule_id}",
                tag=rule.rule_id,
                value=rule.selector,
            )
            for rule in self._universe.for_platform(Platform.X)
            if rule.active_until is None
        }

        add = [r for tag, r in desired.items() if tag not in remote]
        stale = [
            remote[tag].remote_id
            for tag in remote
            if tag in desired and remote[tag].value != desired[tag].value
        ]
        # Rules the platform holds that our universe does not name are NOT
        # deleted. They may belong to another user of the same credential, and
        # deleting them would be a cross-tenant mutation performed by a
        # read-only collector.
        foreign = tuple(tag for tag in remote if tag not in desired)

        result = await self._transport.apply_rules(add, stale)
        result = RuleSyncResult(
            added=result.added,
            deleted=result.deleted,
            unchanged=tuple(t for t in desired if t in remote and t not in stale),
            foreign=foreign,
        )

        self._tape.append_stream_event(
            "rule_reconciliation",
            {
                "added": list(result.added),
                "deleted": list(result.deleted),
                "unchanged": list(result.unchanged),
                "foreign_rules_left_alone": list(result.foreign),
                "universe": self._universe.digest_fields(),
            },
        )
        return result

    # -- the loop -----------------------------------------------------------

    async def run(self, *, max_frames: int | None = None) -> CollectorReport:
        """Consume frames until the stream ends, the budget stops us, or
        ``max_frames`` is reached."""

        self.assert_startable()
        report = CollectorReport()
        await self.reconcile_rules()

        async for frame in self._transport.frames():
            report.frames_seen += 1

            if frame.kind is FrameKind.KEEPALIVE:
                report.keepalives += 1
            elif frame.kind is FrameKind.RECONNECT:
                self._generation += 1
                report.reconnects += 1
                report.subscription_generation = self._generation
                # A gap is recorded as typed absence, never left implicit.
                self._tape.append_absence(
                    "stream_disconnected",
                    {
                        "new_subscription_generation": self._generation,
                        "note": (
                            "records between the disconnect and this point "
                            "were not observed by us; any item recovered for "
                            "this window is DeliveryMode.BACKFILL and its "
                            "our_received_at is not live delivery timing"
                        ),
                    },
                )
            elif frame.kind is FrameKind.ERROR:
                report.errors += 1
                self._tape.append_stream_event(
                    "platform_error",
                    {"raw_len": len(frame.raw), "generation": self._generation},
                )
            elif frame.kind in (FrameKind.DATA, FrameKind.BACKFILL):
                if frame.kind is FrameKind.BACKFILL:
                    report.backfill_frames += 1
                report.data_frames += 1
                stop = await self._handle_data_frame(frame, report)
                if stop:
                    return self._finish(report)

            if max_frames is not None and report.frames_seen >= max_frames:
                report.stopped_reason = "max_frames"
                return self._finish(report)

        report.stopped_reason = "stream_ended"
        return self._finish(report)

    def _finish(self, report: CollectorReport) -> CollectorReport:
        report.ledger_counters = self._ledger.counters()
        self._tape.append_stream_event("collector_stopped", report.to_json())
        return report

    async def _handle_data_frame(
        self, frame: StreamFrame, report: CollectorReport
    ) -> bool:
        """Process one data frame. Returns True if the run must stop."""

        # 1. RECEIPT FIRST. Before parsing, before budget, before anything.
        #    This stamp is the perishable quantity; everything else can be
        #    redone from the bytes.
        received = capture_receipt(self._epoch, clock=self._receipt_clock)

        # 2. Budget BEFORE consumption. Fail closed on every guard fault, not
        #    only on exhaustion: if we cannot say what has been spent, we stop.
        try:
            self._cost_guard.reserve(1)
        except BudgetExhaustedError as exc:
            self._tape.append_absence(
                "cost_budget_exhausted",
                {"detail": str(exc), "generation": self._generation},
            )
            report.stopped_reason = "cost_budget_exhausted"
            return True
        except CostGuardError as exc:
            self._tape.append_absence(
                "cost_guard_unavailable",
                {"detail": str(exc), "generation": self._generation},
            )
            report.stopped_reason = "cost_guard_unavailable"
            return True
        report.reads_reserved += 1

        # 3. Parse. An unparseable frame is RECORDED, not dropped: a silent
        #    drop is indistinguishable from a stream that went quiet.
        try:
            payload = parse_x_payload(frame.raw)
            artifact = self._build_artifact(frame, payload, received)
        except (XPayloadError, SourceUniverseError, ValueError, KeyError) as exc:
            report.unparseable_frames += 1
            self._tape.append_stream_event(
                "unparseable_frame",
                {
                    "reason": str(exc),
                    "raw_content_hash": raw_content_digest(frame.raw),
                    "our_received_at": received.to_json(),
                    "generation": self._generation,
                },
            )
            return False

        # 4. Identity: transport duplicate, or the world spreading?
        decision = self._ledger.record(artifact)
        if decision.is_transport_duplicate:
            self._tape.append_redelivery(artifact, verdict=decision.verdict.value)
            report.redeliveries_written += 1
            return False

        if decision.verdict is DedupeVerdict.PROPAGATION:
            report.propagation_events += 1
        elif decision.verdict is DedupeVerdict.REVISION:
            report.revisions += 1

        self._tape.append_artifact(artifact)
        report.artifacts_written += 1
        return False

    # -- construction -------------------------------------------------------

    def _build_artifact(
        self,
        frame: StreamFrame,
        payload: Mapping[str, Any],
        received,
    ) -> SocialArtifact:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise XPayloadError("frame has no `data` object")

        message_id = str(data.get("id") or "")
        author_id = str(data.get("author_id") or "")
        if not message_id:
            raise XPayloadError("frame has no message id")

        raw_created = data.get(self.SOURCE_TIME_FIELD)
        if not isinstance(raw_created, str) or not raw_created.strip():
            # NO FALLBACK. An item whose creation time we cannot read is
            # refused rather than stamped with our own clock.
            raise XPayloadError(
                f"frame has no usable `{self.SOURCE_TIME_FIELD}`; refusing to "
                "substitute our receipt time for the platform's claim"
            )
        source_created_at = SourceCreatedAt.from_platform(
            raw_created,
            source_field=self.SOURCE_TIME_FIELD,
            parsed=_parse_platform_datetime(raw_created),
            fidelity=self.SOURCE_TIME_FIDELITY,
        )

        # The authored text, extracted once and stored, because content
        # identity must be over what was SAID, not over the envelope it
        # arrived in — see `SocialArtifact.content_text`.
        text = str(data.get("text") or "")

        parent = _parent_from_payload(data)
        media = _media_from_payload(payload)

        matched = frame.matched_rule_ids or ()
        if not matched:
            raise XPayloadError(
                "frame names no matching rule; an unattributed item cannot "
                "have its rule's value measured"
            )
        for rule_id in matched:
            # Refuse an item attributed to a rule we do not hold: it would
            # silently widen the universe past what was configured.
            self._universe.by_id(rule_id)

        delivery_mode = (
            DeliveryMode.BACKFILL
            if frame.kind is FrameKind.BACKFILL
            else DeliveryMode.LIVE
        )

        artifact = SocialArtifact(
            platform=Platform.X,
            source_id=str(data.get("author_id") or matched[0]),
            message_id=message_id,
            author_id=author_id,
            source_created_at=source_created_at,
            our_received_at=received,
            raw_content=frame.raw,
            raw_content_hash=raw_content_digest(frame.raw),
            content_text=text,
            matching_rule=",".join(matched),
            parent=parent,
            delivery_mode=delivery_mode,
            media=media,
            entity_resolution=self._resolver.resolve(text),
            # Explicitly ABSENT. Later milestones fill these; nothing here does.
            first_onchain_reaction=Deferred(),
            first_price_reaction=Deferred(),
            delivery_sequence=frame.delivery_sequence,
            subscription_generation=(
                frame.subscription_generation or self._generation
            ),
            delivery_offset=delivery_offset(source_created_at, received),
            ingestion_version=INGESTION_VERSION,
        )
        return artifact


def _parent_from_payload(data: Mapping[str, Any]) -> ParentRef:
    """Map X's referenced_tweets into typed propagation.

    A missing ``referenced_tweets`` is ``NOT_PROVIDED``, not ``ORIGINAL``: X
    omits the field unless expansions were requested, so "no field" genuinely
    means "we did not ask", which is a different claim from "there is no
    parent".
    """

    refs = data.get("referenced_tweets")
    if refs is None:
        return ParentRef(kind=PropagationKind.NOT_PROVIDED)
    if not isinstance(refs, list):
        return ParentRef(
            kind=PropagationKind.UNKNOWN_PARENT, source_field="referenced_tweets"
        )
    if not refs:
        return ParentRef(
            kind=PropagationKind.ORIGINAL, source_field="referenced_tweets"
        )

    mapping = {
        "retweeted": PropagationKind.REBROADCAST,
        "quoted": PropagationKind.QUOTE,
        "replied_to": PropagationKind.REPLY,
    }
    # Prefer the strongest propagation relation present.
    for key in ("retweeted", "quoted", "replied_to"):
        for ref in refs:
            if isinstance(ref, Mapping) and ref.get("type") == key:
                parent_id = ref.get("id")
                if not parent_id:
                    return ParentRef(
                        kind=PropagationKind.UNKNOWN_PARENT,
                        source_field="referenced_tweets",
                    )
                return ParentRef(
                    kind=mapping[key],
                    parent_message_id=str(parent_id),
                    parent_author_id=(
                        str(ref["author_id"]) if ref.get("author_id") else None
                    ),
                    source_field="referenced_tweets",
                )
    return ParentRef(
        kind=PropagationKind.UNKNOWN_PARENT, source_field="referenced_tweets"
    )


def _media_from_payload(payload: Mapping[str, Any]) -> tuple[MediaRef, ...]:
    """Record media REFERENCES. Nothing is fetched."""

    includes = payload.get("includes")
    if not isinstance(includes, Mapping):
        return ()
    media = includes.get("media")
    if not isinstance(media, list):
        return ()
    out: list[MediaRef] = []
    for item in media:
        if not isinstance(item, Mapping):
            continue
        key = item.get("media_key")
        if not key:
            continue
        out.append(
            MediaRef(
                media_key=str(key),
                media_type=str(item.get("type") or "unknown"),
                url=str(item["url"]) if item.get("url") else None,
                retrieved=False,
            )
        )
    return tuple(out)
