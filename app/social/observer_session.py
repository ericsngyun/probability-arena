"""SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001 — the observer session driver.

Consumes typed transport events and drives the deterministic stream state
machine. This is the ONLY place where a wire fact becomes a statement about
observation coverage.

Three things live here and deliberately not in the transport:

* **the event → state mapping**, so reconnect handling cannot be edited
  without the accounting change being visible in this file;
* **the Post-read budget**, because a cost cap is session policy and a
  transport that knew its own budget could stop for a reason the accounting
  never saw;
* **the keepalive stall deadline**, because "wedged" is a judgement about
  elapsed time, not a wire fact.

The driver is fed events. It never calls the transport, never opens anything,
and holds no credential -- so the entire lifecycle, including every failure
path, replays from a list in a test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from app.social.x_events import (
    EventKind,
    TransportEvent,
)
from app.social.x_stream_state import (
    KEEPALIVE_STALL_S,
    CoverageReport,
    StreamState,
    StreamStateMachine,
    TransitionReason,
)

__all__ = [
    "ObserverSessionDriver",
    "SessionOutcome",
    "BudgetPolicy",
]


@dataclass(frozen=True)
class BudgetPolicy:
    """The frozen cost envelope, as policy the driver enforces.

    `max_posts` is the natural Post-read cap from the preregistration.
    CONTROL artifacts are injected rather than read and consume none of it,
    which is why this counts only what the transport reports as a Post.
    """

    max_posts: int = 3_000
    max_wall_s: float = 8 * 3600.0
    min_wall_s: float = 4 * 3600.0
    min_natural_artifacts: int = 250
    keepalive_stall_s: float = KEEPALIVE_STALL_S


@dataclass(frozen=True)
class SessionOutcome:
    coverage: CoverageReport
    reached_minimum_duration: bool
    reached_minimum_artifacts: bool

    @property
    def qualification_complete(self) -> bool:
        """Both floors met. Reported, never used to relabel a run."""
        return self.reached_minimum_duration and self.reached_minimum_artifacts


class ObserverSessionDriver:
    """Maps transport events onto stream states. Deterministic and offline."""

    def __init__(self, *, budget: BudgetPolicy | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget or BudgetPolicy()
        self._clock = clock
        self._machine = StreamStateMachine(clock=clock)
        self._started_at = clock()
        self.foreign_rules_seen = 0

    @property
    def state(self) -> StreamState:
        return self._machine.state

    @property
    def posts(self) -> int:
        return self._machine.posts_received

    # -- the mapping -------------------------------------------------------
    def consume(self, event: TransportEvent) -> None:
        """One wire fact in, at most one state change out."""
        m, k = self._machine, event.kind

        if k is EventKind.AUTHENTICATION_ACCEPTED:
            self._to(StreamState.AUTHENTICATED,
                     TransitionReason.AUTHENTICATED_OK)

        elif k is EventKind.RULES_RECONCILED:
            self.foreign_rules_seen += getattr(event, "foreign", 0)
            self._to(StreamState.RULES_RECONCILED,
                     TransitionReason.RULES_APPLIED)

        elif k is EventKind.STREAM_OPENED:
            self._to(StreamState.STREAM_CONNECTED, TransitionReason.CONNECT_OK)

        elif k is EventKind.FRAME_OBSERVED:
            m.note_frame(is_post=getattr(event, "is_post", False))
            # A frame of ANY kind proves liveness, so a keepalive is what
            # lifts a connection into RECEIVING. That is the whole reason
            # keepalives are frames rather than noise.
            if m.state is StreamState.STREAM_CONNECTED:
                self._to(StreamState.RECEIVING, TransitionReason.FIRST_FRAME)
            elif m.state is StreamState.DEGRADED:
                self._to(StreamState.RECEIVING, TransitionReason.RECOVERED)
            if self.posts >= self.budget.max_posts:
                self._to(StreamState.BUDGET_EXHAUSTED,
                         TransitionReason.POST_BUDGET_REACHED)

        elif k is EventKind.PLATFORM_ERROR_OBSERVED:
            self._to(StreamState.DEGRADED,
                     TransitionReason.PLATFORM_ERROR_FRAME)

        elif k is EventKind.RATE_LIMITED:
            self._to(StreamState.DEGRADED, TransitionReason.RATE_LIMITED)

        elif k is EventKind.HTTP_ERROR_OBSERVED:
            self._to(StreamState.RECONNECTING, TransitionReason.HTTP_ERROR)

        elif k is EventKind.CONNECTION_ENDED:
            clean = getattr(event, "clean", True)
            self._to(StreamState.RECONNECTING,
                     TransitionReason.CLEAN_EOF if clean
                     else TransitionReason.CONNECTION_DROPPED)

        elif k is EventKind.RETRY_BUDGET_EXHAUSTED:
            self._to(StreamState.STOPPED,
                     TransitionReason.RETRY_BUDGET_EXHAUSTED)

    def tick(self) -> None:
        """Time-based judgements the wire cannot report.

        A wedged connection emits nothing at all -- including no event saying
        so -- which is precisely why the deadline has to be evaluated here
        rather than waited for.
        """
        # The wall cap is evaluated FIRST and unconditionally. It was once an
        # `elif` after the stall check, which meant a wedged stream masked the
        # absolute session bound: past the stall window both conditions are
        # true, the stall branch won, and the 8-hour cap never fired. A hard
        # stop that only applies when nothing else is wrong is not a cap.
        if self._clock() - self._started_at >= self.budget.max_wall_s:
            self.stop(TransitionReason.TIME_CAP_REACHED)
            return
        if (self._machine.state is StreamState.RECEIVING
                and self._machine.keepalive_stalled(
                    stall_s=self.budget.keepalive_stall_s)):
            self._to(StreamState.DEGRADED,
                     TransitionReason.KEEPALIVE_STALLED)

    def stop(self, reason: TransitionReason = TransitionReason.OPERATOR_STOP
             ) -> None:
        if self._machine.state is not StreamState.STOPPED:
            self._to(StreamState.STOPPED, reason)

    def drive(self, events: Iterable[TransportEvent]) -> "SessionOutcome":
        for event in events:
            self.consume(event)
        return self.outcome()

    def outcome(self) -> SessionOutcome:
        report = self._machine.report()
        return SessionOutcome(
            coverage=report,
            reached_minimum_duration=(
                report.observed_s >= self.budget.min_wall_s),
            reached_minimum_artifacts=(
                report.posts_received >= self.budget.min_natural_artifacts),
        )

    # -- internals ---------------------------------------------------------
    def _to(self, state: StreamState, reason: TransitionReason) -> None:
        """Ignore a transition the machine forbids, rather than crashing a run.

        The machine still refuses it -- the illegal history never gets
        recorded -- but a live observation session should not die because the
        platform sent a rate-limit frame after we had already stopped. The
        refusal is the machine's; tolerating it is the driver's.
        """
        from app.social.x_stream_state import IllegalTransitionError
        if state is self._machine.state:
            return
        try:
            self._machine.transition(state, reason)
        except IllegalTransitionError:
            return
