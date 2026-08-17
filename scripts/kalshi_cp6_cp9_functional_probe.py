"""KALSHI-CP6-CP9-FUNCTIONAL — the live sessions for CP6, CP7 and CP8.

**Scope: FUNCTIONAL ONLY.** Per the §8 amendment to
`docs/experiments/KALSHI-CP6-CP9-QUALIFICATION-PREREGISTRATION.md`, the
100,000-frame floor is withdrawn for DEMO and **no rate, latency-tail,
throughput or capacity claim may be made from anything this script produces.**
DEMO is an engineering sandbox: 98.3% of the frames its eligible population
emits come from 194 venue test instruments. Those instruments are used here
*deliberately and legitimately* — a functional proof needs frames that exercise
the code paths, not frames that resemble production microstructure — and every
artifact this script writes says so in `scope_note`.

Three modes, each a separate session, because they cannot share one tape:

* `observe`   — the CP6 semantics session and the CP8 conservation/replay tape.
                No perturbation of any kind. This is also the **negative
                control** for the two below: zero forced closes, zero dropped
                frames, and the fault counter must stay at zero.
* `reconnect` — CP7. Forces genuine socket teardowns by closing the real
                websocket underneath the collector, then records the per-market
                publishability transition timeline across the boundary.
* `drop`      — CP7 anti-vacuity. Withholds exactly one sequenced orderbook
                frame from the collector *within* a generation, which is what a
                real venue drop looks like from the collector's side. The
                P0 fix separated ordering from basing; this proves it did not
                blind the drop detector on live data.

**Read-only.** Market-data channels only (`assert_channels_allowed` refuses
anything else at `CollectorConfig` construction). The only frames that reach a
socket are the `subscribe` the collector builds and, on a real orderbook fault,
its own `get_snapshot`. No order, position, portfolio, wallet or key-management
surface is reached. No key material is printed: the credential audit records a
key-id fingerprint and nothing else.

The collector runs **exactly as shipped**. The taps are delegating wrappers
around the transport, and `ObservedSession` overrides one method to *read*
state after each frame — it never writes collector state, never suppresses an
exception and never alters what is archived (except in `drop` mode, where
withholding the frame IS the experiment and the tape is correct to lack it).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# `python scripts/x.py` puts `scripts/` on the path and NOT the repo root, so
# both are made explicit rather than left to the caller's PYTHONPATH — a probe
# that only runs from one working directory is a probe that will be run from
# the other one at 3am.
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Reused VERBATIM from the P0 probe, on purpose: the per-sid ordering census and
# the separated ladder buckets are the instrument that produced the wire
# evidence CP6 is checked against, so CP6 must not answer the same questions
# with a differently-behaved recorder.
from kalshi_collector_p0_wire_probe import (  # noqa: E402
    ENVIRONMENT,
    WireRecorder,
    _plain,
    prove_read_only,
)

from app.realtime.collector import CollectorConfig, _Session  # noqa: E402
from app.realtime.collector_metrics import CollectorMetrics  # noqa: E402
from app.realtime.ws_transport import (  # noqa: E402
    KalshiWebsocketTransport,
    TransportError,
)

SCOPE_NOTE = (
    "FUNCTIONAL PROOF ON KALSHI DEMO. Venue test instruments are used "
    "deliberately: they are dense enough to exercise every code path a "
    "functional proof needs. NO rate, latency-tail, throughput, capacity or "
    "microstructure-realism claim may be derived from this artifact. Those "
    "belong to KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001."
)


# =================================================================================
# taps — delegating, credential-free, and each one perturbs exactly one thing
# =================================================================================


class _BaseTap:
    """The P0 tap. Cannot drop, reorder, retry or synthesise a frame."""

    def __init__(self, inner, recorder: WireRecorder, journal: list):
        self._inner = inner
        self._recorder = recorder
        self._journal = journal

    # -- pass-through surface the collector reads ---------------------------------
    @property
    def counters(self):
        return self._inner.counters

    @property
    def queue_depth(self):
        return self._inner.queue_depth

    @property
    def backpressure_active(self):
        return self._inner.backpressure_active

    @property
    def last_close(self):
        return self._inner.last_close

    @property
    def connected(self) -> bool:
        return self._inner.connected

    async def connect(self) -> None:
        await self._inner.connect()

    async def send(self, message) -> None:
        self._recorder.on_command(message)
        await self._inner.send(message)

    async def close(self) -> None:
        await self._inner.close()

    def __aiter__(self):
        return self._tap()

    async def _tap(self):
        async for message in self._inner:
            self._recorder.observe(message)
            yield message

    def _note(self, event: str, **fields) -> None:
        self._journal.append(
            {"event": event, "at": datetime.now(timezone.utc).isoformat(),
             **fields})


class ForceCloseTap(_BaseTap):
    """CP7. Closes the REAL socket underneath the collector, mid-stream.

    Not an injected exception: `close()` tears down the actual websocket, so the
    collector's own read raises its own `TransportError`, walks its own
    reconnect ladder, opens a new socket, re-handshakes and re-subscribes. The
    generation boundary under test is produced by the same code path a venue
    disconnect would drive, which is what makes this a positive control rather
    than a mock of one.

    Bounded by construction: it fires at most `budget` times across the whole
    session, and the budget is shared by every connection through a one-element
    list, so a reconnect cannot silently re-arm it.
    """

    def __init__(self, inner, recorder, journal, *, after_frames: int, budget: list):
        super().__init__(inner, recorder, journal)
        self._after_frames = after_frames
        self._budget = budget
        self._seen = 0

    async def _tap(self):
        async for message in self._inner:
            self._recorder.observe(message)
            self._seen += 1
            yield message
            if self._seen >= self._after_frames and self._budget[0] > 0:
                self._budget[0] -= 1
                self._note("forced_socket_close",
                           frames_on_this_connection=self._seen,
                           remaining_budget=self._budget[0])
                await self._inner.close()
                # The iterator is exhausted or will raise on the next read; the
                # collector treats either as the disconnect it is.
                raise TransportError("forced close (CP7 positive control)")


class DropTap(_BaseTap):
    """CP7 anti-vacuity. Withholds exactly one sequenced orderbook frame.

    A venue drop, from the collector's side, is precisely "a frame with the next
    `seq` never arrives". Withholding one here reproduces that exactly — and
    unlike a synthetic gap it happens on a live stream, inside a real
    generation, on a book that a real snapshot has already based.

    The frame is chosen by OBSERVATION, not by assumption: the tap waits until
    it has seen `arm_after` orderbook frames carrying a `seq` on one sid, then
    drops the next one on that same sid. It records which sid and which `seq`
    it removed, so the fault the collector reports can be checked against the
    exact hole that was made rather than against "a fault happened".

    It drops ONCE. A second drop would make the re-anchor behaviour and the
    fault count ambiguous.
    """

    def __init__(self, inner, recorder, journal, *, arm_after: int, budget: list):
        super().__init__(inner, recorder, journal)
        self._arm_after = arm_after
        self._budget = budget
        self._orderbook_seen = 0
        self._orderbook_sid = None

    async def _tap(self):
        async for message in self._inner:
            if type(message) is dict:
                etype = message.get("type")
                sid = message.get("sid")
                seq = message.get("seq")
                is_book = etype in ("orderbook_snapshot", "orderbook_delta")
                sequenced = isinstance(seq, int) and not isinstance(seq, bool)
                if is_book and sequenced:
                    if self._orderbook_sid is None:
                        self._orderbook_sid = sid
                    if sid == self._orderbook_sid:
                        self._orderbook_seen += 1
                        if (self._orderbook_seen > self._arm_after
                                and self._budget[0] > 0
                                and etype == "orderbook_delta"):
                            self._budget[0] -= 1
                            self._note("dropped_frame", sid=sid, seq=seq,
                                       event_type=etype,
                                       market_ticker=(message.get("msg") or {}).get(
                                           "market_ticker"),
                                       orderbook_frames_before=self._orderbook_seen)
                            # NOT observed by the recorder either: the wire
                            # census must agree with what the collector saw, or
                            # the conservation arithmetic would be checked
                            # against a frame that never entered the system.
                            continue
            self._recorder.observe(message)
            yield message


# =================================================================================
# state capture — the live-derived terminal state CP8 compares against
# =================================================================================


def _book_state(book, state) -> dict:
    """One market's terminal state.

    `checksum` only when publishable, mirroring `archive.replay` exactly: a
    consumer comparing checksums without also reading `publishable` would
    otherwise accept a torn book, and a comparison that used a different rule
    on each side would not be comparing the same quantity.

    `state` is a `PublicationState`, not a boolean. The CP7 run of 2026-08-17
    recorded only the boolean, and the failure it measured then had to be
    RECONSTRUCTED from the frame stream afterwards to learn *why* each book was
    unpublishable. Doctrine 10 applies to the artifact as much as to the code:
    `publishable: false` is an absence, and the three ways of arriving at it —
    a real integrity fault, an un-re-snapshotted epoch, an unbased subscription
    — are not the same observation. They are written down here so that no
    future reader has to infer which one occurred.
    """
    publishable = state.publishable
    return {
        "publishable": publishable,
        # The typed reason, its human detail, and BOTH epoch numbers.
        "publication_state": state.state,
        "publication_reason": state.reason,
        "publication_subscription_generation": _plain(
            state.subscription_generation),
        "publication_based_generation": _plain(state.based_generation),
        # Read off the book itself as well as off the state, so the artifact
        # records the invariant's two inputs and not only its verdict.
        "based_generation": _plain(book.based_generation),
        "based_for_current_generation": book.based_for_current_generation,
        "checksum": (book.checksum() if publishable else None),
        "synced": book.synced,
        "integrity_reason": book.integrity_reason,
        "book_generation": book.generation,
        "subscription_generation": _plain(book.subscription_generation),
        "last_seq": book.last_seq,
        "levels_yes": len(book.yes),
        "levels_no": len(book.no),
        # Doctrine 10: absence is typed, never a level count of zero.
        "ladder_presence": {str(k): str(v) for k, v in book.ladder_presence.items()},
        "stats": dict(book.stats),
    }


def capture_state(session) -> dict:
    """The collector's own terminal view, read from the real routers."""
    out = {}
    for sid, router in sorted(session._routers.items(), key=lambda kv: str(kv[0])):
        states = router.publication_states()
        sub = router.subscription
        out[str(sid)] = {
            "sid": sub.sid,
            "generation": sub.generation,
            "healthy": sub.healthy,
            "state_reason": sub.state_reason,
            "last_seq": sub.last_seq,
            "carries_orderbook": sub.carries_orderbook,
            "stats": dict(sub.stats),
            # `publication_states()` is keyed by exactly `router.books`, so the
            # lookup is total; it is written as a direct index rather than a
            # defaulted `.get` because a missing key would be a real disagreement
            # between the two and must raise rather than be recorded as `False`.
            "books": {t: _book_state(b, states[t])
                      for t, b in sorted(router.books.items())},
        }
    return out


def _publication_state_map(session) -> dict:
    """Per-market typed publication state, keyed by `(sid, ticker)`.

    The value is the identity a transition is detected on: the boolean, the
    typed reason, and both epoch numbers. Keying on the boolean alone — what
    the 2026-08-17 run did — cannot see a market move from
    `awaiting_snapshot_for_generation` to `book_halted`, which is precisely the
    distinction the anti-vacuity control has to make.

    `reason` is deliberately NOT part of the key. It is free text derived from
    the other four fields, so including it would add no transition that the key
    does not already carry, while making the log sensitive to wording.
    """
    out = {}
    for sid, router in session._routers.items():
        for ticker, state in router.publication_states().items():
            out[(sid, ticker)] = {
                "publishable": state.publishable,
                "state": state.state,
                "subscription_generation": _plain(state.subscription_generation),
                "based_generation": _plain(state.based_generation),
            }
    return out


def _refusal_map(session) -> dict:
    """Per-market cumulative `rejected_pre_generation_snapshot` counts.

    The fix refuses a new-generation delta that lands on a book which has not
    been re-snapshotted into that generation. CP7 could previously only report
    that this "did not happen to occur"; this makes the refusal an OBSERVABLE
    rather than an inference, so the artifact can distinguish "the guard fired"
    from "the venue's ordering happened to be favourable again".

    Read from `OrderBook.stats`, which is cumulative and is not reset by a
    later snapshot, so a refusal cannot be erased by the recovery that follows
    it.
    """
    out = {}
    for sid, router in session._routers.items():
        for ticker, book in router.books.items():
            out[(sid, ticker)] = book.stats.get(
                "rejected_pre_generation_snapshot", 0)
    return out


class ObservedSession(_Session):
    """The shipped session, with a read-only observer after each frame.

    It records only TRANSITIONS in per-market publication state, plus the frame
    that caused each one. A per-frame dump would be a rate measurement in
    disguise and this milestone makes no rate claims; a transition log is the
    exact evidence CP7 needs — `old book -> nonpublishable -> its own new
    snapshot -> publishable`, independently per market.

    The state is the TYPED one, not the boolean. A transition is detected on
    `(publishable, state, subscription_generation, based_generation)`, so a
    book that moves from `awaiting_snapshot_for_generation` to `book_halted`
    without changing its boolean is visible — which is the whole of the
    anti-vacuity question, and is exactly what the 2026-08-17 artifacts could
    not answer without reconstruction.

    It cannot change what the collector does: it runs AFTER `_handle_frame` has
    returned, it mutates nothing the collector reads, and it returns the
    collector's own outcome unmodified.
    """

    def __init__(self, *args, timeline: list, refusals: list | None = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self._timeline = timeline
        # Optional so a caller that only wants the transition log keeps working;
        # the live `run()` always passes one, because a session that cannot say
        # whether the guard fired cannot answer CP7's fourth question.
        self._refusals = [] if refusals is None else refusals
        self._last_pub: dict = {}
        self._last_refusals: dict = {}
        self._frame_ordinal = 0

    @staticmethod
    def _cause(message) -> dict:
        msg = message if type(message) is dict else {}
        raw_msg = msg.get("msg")
        return {
            "event_type": msg.get("type"),
            "sid": msg.get("sid"),
            "seq": msg.get("seq"),
            "market_ticker": (raw_msg or {}).get("market_ticker")
            if isinstance(raw_msg, dict) else None,
        }

    async def _handle_frame(self, message, transport):
        outcome = await super()._handle_frame(message, transport)
        self._frame_ordinal += 1
        current = _publication_state_map(self)
        changes = []
        for key in sorted(set(current) | set(self._last_pub), key=lambda k: str(k)):
            before = self._last_pub.get(key)
            after = current.get(key)
            if before != after:
                changes.append({
                    "sid": key[0], "market_ticker": key[1],
                    # The booleans keep their exact former meaning and name, so
                    # every existing consumer of this artifact still reads the
                    # same quantity.
                    "from": None if before is None else before["publishable"],
                    "to": None if after is None else after["publishable"],
                    # The typed reason beside it. A market can move between two
                    # unpublishable states — `awaiting_snapshot_for_generation`
                    # to `book_halted` is the one that matters — and the
                    # booleans alone cannot see that at all.
                    "from_state": before,
                    "to_state": after,
                })
        if changes:
            self._timeline.append({
                "frame_ordinal": self._frame_ordinal,
                "at": datetime.now(timezone.utc).isoformat(),
                "cause": self._cause(message),
                "subscription_epoch": self.subscription_epoch,
                "connection_generation": self.connection_generation,
                "sequence_faults_so_far": self.sequence_faults,
                "changes": changes,
            })
        self._last_pub = current

        # The delta-refusal guard, observed per frame. Recorded separately from
        # the publishability timeline because a refusal changes NO publication
        # state — the book was already `awaiting_snapshot_for_generation` — so
        # the transition log cannot see it, and "it did not happen" and "it
        # happened and was invisible" would otherwise look identical.
        refusals = _refusal_map(self)
        for key in sorted(set(refusals) | set(self._last_refusals),
                          key=lambda k: str(k)):
            before = self._last_refusals.get(key, 0)
            after = refusals.get(key, 0)
            if after > before:
                self._refusals.append({
                    "frame_ordinal": self._frame_ordinal,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "sid": key[0],
                    "market_ticker": key[1],
                    "cause": self._cause(message),
                    "subscription_epoch": self.subscription_epoch,
                    "rejected_pre_generation_snapshot_delta": after - before,
                    "rejected_pre_generation_snapshot_total": after,
                    # A refusal reaches the collector as a `BookIntegrityError`
                    # and is counted in `sequence_faults` by the generic
                    # book-level handler. That is NOT a lost message, and this
                    # pairing is what lets a reader subtract it out rather than
                    # read a clean reconnect as a faulting one.
                    "session_sequence_faults_so_far": self.sequence_faults,
                })
        self._last_refusals = refusals
        return outcome


# =================================================================================
# the session
# =================================================================================


def _metrics_snapshot(metrics) -> dict:
    """The metrics lane's own numbers, so doctrine 7 can be checked on them.

    A metric that never moves is indistinguishable from a metric that is not
    wired up, so the forced conditions below are checked against THESE values,
    not only against the session result's.
    """
    return {
        "events_received": metrics.events_received,
        "events_archived": metrics.events_archived,
        "events_rejected": metrics.events_rejected,
        "frames_malformed": metrics.frames_malformed,
        "append_calls": metrics.append_calls,
        "rotations_started": metrics.rotations_started,
        "segments_closed": metrics.segments_closed,
        "disconnects": metrics.disconnects,
        "reconnects": metrics.reconnects,
        "sequence_gaps": metrics.sequence_gaps,
        "sequence_regressions": metrics.sequence_regressions,
        "sequence_duplicates": metrics.sequence_duplicates,
        "subscription_generation": metrics.subscription_generation,
        "observe_errors": metrics.observe_errors,
        "event_bytes_total": metrics.event_bytes_total,
    }


def run(*, mode, tickers, channels, max_seconds, max_events, max_reconnects,
        archive_root, samples_per_type, label, out_path, read_timeout_s,
        force_close_after, force_close_budget, drop_arm_after):
    import asyncio

    signer, audit = prove_read_only()
    recorder = WireRecorder(samples_per_type=samples_per_type)
    journal: list = []
    timeline: list = []
    refusals: list = []
    transports: list = []
    close_budget = [force_close_budget if mode == "reconnect" else 0]
    drop_budget = [1 if mode == "drop" else 0]

    def transport_factory():
        inner = KalshiWebsocketTransport(environment=ENVIRONMENT, signer=signer,
                                         read_timeout_s=read_timeout_s)
        transports.append(inner)
        if mode == "reconnect":
            return ForceCloseTap(inner, recorder, journal,
                                 after_frames=force_close_after,
                                 budget=close_budget)
        if mode == "drop":
            return DropTap(inner, recorder, journal,
                           arm_after=drop_arm_after, budget=drop_budget)
        return _BaseTap(inner, recorder, journal)

    config = CollectorConfig(
        environment=ENVIRONMENT,
        archive_root=Path(archive_root),
        market_tickers=tuple(tickers),
        channels=tuple(channels),
        max_seconds=max_seconds,
        max_events=max_events,
        max_reconnects=max_reconnects,
        dry_run=False,
        validate_sequence=True,
    )
    metrics = CollectorMetrics(environment=ENVIRONMENT,
                               markets_subscribed=len(tickers))

    started = datetime.now(timezone.utc).isoformat()
    holder: dict = {}

    async def _go():
        session = ObservedSession(config, transport_factory=transport_factory,
                                  metrics=metrics, timeline=timeline,
                                  refusals=refusals)
        holder["session"] = session
        return await session.run()

    result = asyncio.run(_go())
    finished = datetime.now(timezone.utc).isoformat()
    session = holder["session"]

    payload = {
        "milestone": "KALSHI-CP6-CP9-FUNCTIONAL",
        "scope_note": SCOPE_NOTE,
        "mode": mode,
        "run_label": label,
        "environment": ENVIRONMENT,
        "credential_audit": audit,
        "started_at": started,
        "finished_at": finished,
        "config": {
            "market_tickers_count": len(tickers),
            "market_tickers": list(tickers),
            "channels": list(channels),
            "max_seconds": max_seconds,
            "max_events": max_events,
            "max_reconnects": max_reconnects,
            "archive_root": str(archive_root),
            "read_timeout_s": read_timeout_s,
            "force_close_after_frames": force_close_after if mode == "reconnect" else None,
            "force_close_budget": force_close_budget if mode == "reconnect" else 0,
            "drop_arm_after_orderbook_frames": drop_arm_after if mode == "drop" else None,
        },
        "session_result": result.to_dict(),
        "subscription_epoch_final": session.subscription_epoch,
        "connection_generation_final": session.connection_generation,
        "metrics": _metrics_snapshot(metrics),
        "wire_counters_per_connection": [t.counters.snapshot() for t in transports],
        "wire": recorder.to_dict(),
        "perturbation_journal": journal,
        "publishability_timeline": timeline,
        # Empty means the guard did not fire in this session — which is a
        # statement about the venue's frame ordering, NOT a proof that the
        # guard works. Read it with §3.3 of the CP9 report.
        "generation_delta_refusals": refusals,
        "live_terminal_state": capture_state(session),
    }
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True,
                                         default=str) + "\n")
    print(json.dumps({
        "mode": mode,
        "run_label": label,
        "status": result.status,
        "events_received": result.events_received,
        "events_archived": result.events_archived,
        "events_rejected": result.events_rejected,
        "frames_malformed": result.frames_malformed,
        "reconnects": result.reconnects,
        "subscription_epoch_final": session.subscription_epoch,
        "sequence_faults": result.sequence_faults,
        "recoveries_requested": result.recoveries_requested,
        "metrics_errors": result.metrics_errors,
        "segments_committed": result.segments_committed,
        "by_type": dict(recorder.by_type),
        "perturbations": len(journal),
        "publishability_transitions": len(timeline),
        "generation_delta_refusals": len(refusals),
        "out": str(out_path),
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True,
                        choices=("observe", "reconnect", "drop"))
    parser.add_argument("--tickers", default="")
    parser.add_argument("--tickers-file", default="")
    parser.add_argument("--channels", default="orderbook_delta,ticker,trade")
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--max-events", type=int, default=200_000)
    parser.add_argument("--max-reconnects", type=int, default=4)
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--samples-per-type", type=int, default=8)
    parser.add_argument("--read-timeout-s", type=float, default=None)
    parser.add_argument("--force-close-after", type=int, default=400)
    parser.add_argument("--force-close-budget", type=int, default=2)
    parser.add_argument("--drop-arm-after", type=int, default=300)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.tickers_file:
        raw = Path(args.tickers_file).read_text()
        tickers = [t for t in (x.strip() for x in raw.split(",")) if t]
    else:
        tickers = [t for t in args.tickers.split(",") if t]
    if not tickers:
        raise SystemExit("refused: no market tickers; a session with no sample "
                         "frame measures nothing")

    run(mode=args.mode,
        tickers=tickers,
        channels=[c for c in args.channels.split(",") if c],
        max_seconds=args.max_seconds,
        max_events=args.max_events,
        max_reconnects=args.max_reconnects,
        archive_root=args.archive_root,
        samples_per_type=args.samples_per_type,
        label=args.label,
        out_path=args.out,
        read_timeout_s=(args.read_timeout_s if args.read_timeout_s is not None
                        else float(args.max_seconds + 120)),
        force_close_after=args.force_close_after,
        force_close_budget=args.force_close_budget,
        drop_arm_after=args.drop_arm_after)


if __name__ == "__main__":
    main()
