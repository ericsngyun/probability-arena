"""The frozen prospective sampling contract, as a pure decision core.

`MARKET-MICROSTRUCTURE-EDGE-001` Amendment 2 retired the static 40-market
universe. The unit of eligibility is now **market x event-time block**, decided
at a 300 s clock from information available at or before the decision instant.
This module is that rule and nothing else -- no model, no score, no feature.

Deliberately pure. It consumes typed observations and market metadata and
returns typed decisions, so the anti-lookahead property can be proven by
construction (`decide_panel` cannot see a frame it was not given) and every
boundary can be tested without a live socket.

**Sequence cleanliness is per-SID, not per-market** (L22). A gap on the
order-book subscription contaminates every market riding it, and this module
models that rather than pretending faults are per-market.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

from app.realtime.book import (
    PUB_AWAITING_GENERATION_SNAPSHOT,
    PUB_BOOK_HALTED,
    PUB_PUBLISHABLE,
    PUB_SUBSCRIPTION_UNHEALTHY,
)

# ---------------------------------------------------------------------------
# Frozen constants. Amendment 2 SS.B/SS.D and the capture plan SS.1.
# Changing any of these changes the preregistered rule and requires a new
# amendment -- they are not tuning knobs.
# ---------------------------------------------------------------------------
LOOKBACK_S = 300
WARMUP_S = 300
DECISION_TICK_S = 300

#: 0.10 events/s over a 300 s lookback IS exactly 30 events. The rule is
#: evaluated as an INTEGER COUNT so no float rounding can move the boundary:
#: 29 -> ineligible, 30 -> eligible.
MIN_ACTIVITY_EVENTS_PER_S = 0.10
MIN_OB_EVENTS_IN_LOOKBACK = 30

PANEL_K = 12
NEVER_EXCEED_CONCURRENCY = 24

#: Label feasibility depends on how much SESSION remains, not on where the
#: market sits in its event lifecycle. Strictly greater: max horizon 300 s +
#: 300 s embargo.
#:
#: This replaced a `TTE > 600 s` gate (Amendment 4). That rule used
#: event-relative time as a proxy for future-label computability, which made
#: two preregistered strata structurally unreachable -- `late_resolution`
#: (TTE < 0) could never produce a single row, and `live_event` admitted only
#: a 300 s sliver. The two concepts are independent: TTE is a stratification
#: variable, session-remaining is the feasibility constraint, and horizon
#: availability is enforced independently by the labeler.
MIN_SESSION_REMAINING_S = 600

#: Order-book message types. `ticker` is absent BY CONTRACT (Amendment 2 SS.B):
#: both PROD-ACTIVITY-PROFILE-001 SS7 controls emitted zero ticker frames while
#: producing real order-book deltas.
ORDERBOOK_MESSAGE_TYPES = frozenset({"orderbook_delta", "orderbook_snapshot"})

TTE_FAR = "far"
TTE_APPROACHING = "approaching"
TTE_NEAR_EVENT = "near_event"
TTE_LIVE_EVENT = "live_event"
TTE_LATE_RESOLUTION = "late_resolution"

#: Frozen before any M0/M1 output exists (Amendment 2 SS.C). Boundaries are
#: closed at the top so every second lands in exactly one bin.
TTE_BIN_EDGES_S = ((TTE_FAR, 21_600), (TTE_APPROACHING, 7_200),
                   (TTE_NEAR_EVENT, 900), (TTE_LIVE_EVENT, 0))

# Reasons a market was not selected. Closed vocabulary, so the audit can be
# grouped without string archaeology.
NOT_SELECTED_WARMUP = "warmup"
NOT_SELECTED_LOW_ACTIVITY = "activity_below_floor"
NOT_SELECTED_BOOK_UNPUBLISHABLE = "book_not_publishable"
NOT_SELECTED_SEQUENCE_FAULT = "sequence_fault_in_lookback"
NOT_SELECTED_SESSION_TOO_SHORT = "session_remaining_at_or_below_600s"
NOT_SELECTED_RANK = "eligible_but_outranked"


class DatasetRole:
    """Typed, not conventional. The evaluator rejects on THIS, never on a path."""
    PROFILE = "PROFILE"
    VALIDATION = "VALIDATION"
    CONFIRMATION = "CONFIRMATION"
    ALL = (PROFILE, VALIDATION, CONFIRMATION)


def tte_bin(tte_seconds: float) -> str:
    """Which frozen event-lifecycle bin a time-to-event falls in."""
    if tte_seconds < 0:
        return TTE_LATE_RESOLUTION
    for name, lower in TTE_BIN_EDGES_S:
        if tte_seconds > lower:
            return name
    return TTE_LIVE_EVENT


@dataclass(frozen=True)
class MarketMeta:
    ticker: str
    series: str
    occurrence_datetime: datetime


@dataclass(frozen=True)
class Observation:
    """One archived frame, reduced to what the eligibility rule may look at.

    Nothing here carries price, size, volume or any model output. If a field
    the rule must not see is ever needed, it does not belong in this type.
    """
    market_ticker: str | None
    received_at_utc: datetime
    message_type: str
    sid: int | None = None
    seq: int | None = None
    subscription_generation: int | None = None


@dataclass(frozen=True)
class BookState:
    """The typed publication answer, reusing the collector's own vocabulary."""
    publishable: bool
    state: str
    subscription_generation: int | None
    based_generation: int | None


@dataclass(frozen=True)
class EligibilityAudit:
    """Experimental provenance, not a model feature.

    For any (market, tick) this answers exactly why it entered, remained,
    dropped, or lost a top-K competition.
    """
    market: str
    series: str
    tick_t: str
    activity_lookback_count: int
    activity_rate: float
    book_state: str
    sequence_clean: bool
    tte_seconds: float
    tte_bin: str
    eligible: bool
    rank: int | None
    selected: bool
    reason_if_not_selected: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PanelDecision:
    tick_t: str
    is_warmup: bool
    panel: tuple[str, ...]
    audit: tuple[EligibilityAudit, ...]

    def to_dict(self) -> dict:
        return {"tick_t": self.tick_t, "is_warmup": self.is_warmup,
                "panel": list(self.panel),
                "audit": [a.to_dict() for a in self.audit]}


class SubscriptionSequenceTracker:
    """Per-SID sequence health. L22: `seq` is per-subscription, not per-market.

    A gap therefore contaminates every market on that subscription for the
    lookback, and this class says so rather than attributing the fault to
    whichever market happened to carry the frame.
    """

    def __init__(self) -> None:
        self._last: dict[int, int] = {}
        self.fault_times: list[datetime] = []

    def observe(self, obs: Observation) -> None:
        if obs.sid is None or obs.seq is None:
            return
        prev = self._last.get(obs.sid)
        if prev is not None and obs.seq != prev + 1:
            self.fault_times.append(obs.received_at_utc)
        self._last[obs.sid] = obs.seq

    def clean_over(self, start: datetime, end: datetime) -> bool:
        """No fault in the half-open lookback `(start, end]`."""
        return not any(start < t <= end for t in self.fault_times)


class BookStateTracker:
    """Current-generation publication state per market, from the tape.

    Mirrors `OrderBook.publication_state`: a book is publishable only if it has
    received its OWN snapshot for the generation the subscription is currently
    in. A stale ladder from generation N-1 is NOT a valid book in generation N,
    which is the CP7 finding this reuses rather than collapsing to
    "a book exists".
    """

    def __init__(self) -> None:
        self.subscription_generation: int = 1
        self._based: dict[str, int] = {}
        self._halted: dict[str, str] = {}

    def observe(self, obs: Observation) -> None:
        if obs.subscription_generation is not None:
            self.subscription_generation = obs.subscription_generation
        if obs.market_ticker is None:
            return
        if obs.message_type == "orderbook_snapshot":
            self._based[obs.market_ticker] = self.subscription_generation

    def halt(self, market: str, reason: str) -> None:
        self._halted[market] = reason

    def state_of(self, market: str) -> BookState:
        gen = self.subscription_generation
        if market in self._halted:
            return BookState(False, PUB_BOOK_HALTED, gen, self._based.get(market))
        based = self._based.get(market)
        if based is None:
            return BookState(False, PUB_SUBSCRIPTION_UNHEALTHY, gen, None)
        if based != gen:
            return BookState(False, PUB_AWAITING_GENERATION_SNAPSHOT, gen, based)
        return BookState(True, PUB_PUBLISHABLE, gen, based)


@dataclass
class PanelSession:
    """Applies the frozen rule to one session's observations.

    `decide_panel(t)` may only be called with observations already fed in. The
    core cannot see a future frame because it is never handed one -- the
    anti-lookahead property is structural, and the metamorphic test in
    VALIDATION-001 proves the wiring honours it.
    """
    session_open: datetime
    markets: dict[str, MarketMeta]
    #: The SCHEDULED end, known before the socket opens. Deliberately not the
    #: observed last-frame time: eligibility at `t` may not consult how long
    #: the venue happened to keep publishing afterwards.
    session_end: datetime | None = None
    k: int = PANEL_K
    _ob_events: dict[str, list[datetime]] = field(default_factory=dict)
    _seq: SubscriptionSequenceTracker = field(
        default_factory=SubscriptionSequenceTracker)
    _books: BookStateTracker = field(default_factory=BookStateTracker)

    def __post_init__(self) -> None:
        if self.k > NEVER_EXCEED_CONCURRENCY:
            raise ValueError(
                f"panel K={self.k} exceeds the never-exceed concurrency "
                f"ceiling {NEVER_EXCEED_CONCURRENCY} frozen in Amendment 2")

    # -- ingestion ---------------------------------------------------------
    def observe(self, obs: Observation) -> None:
        self._seq.observe(obs)
        self._books.observe(obs)
        if (obs.message_type in ORDERBOOK_MESSAGE_TYPES
                and obs.market_ticker is not None):
            self._ob_events.setdefault(obs.market_ticker, []).append(
                obs.received_at_utc)

    def halt_book(self, market: str, reason: str) -> None:
        self._books.halt(market, reason)

    # -- the rule ----------------------------------------------------------
    def activity_count(self, market: str, t: datetime) -> int:
        """Order-book frames in the half-open trailing lookback `(t-300, t]`."""
        lo = t - timedelta(seconds=LOOKBACK_S)
        return sum(1 for ts in self._ob_events.get(market, ()) if lo < ts <= t)

    def is_warmup(self, t: datetime) -> bool:
        return t < self.session_open + timedelta(seconds=WARMUP_S)

    def decide_panel(self, t: datetime) -> PanelDecision:
        lo = t - timedelta(seconds=LOOKBACK_S)
        warmup = self.is_warmup(t)
        rows, ranked = [], []

        for ticker, meta in sorted(self.markets.items()):
            count = self.activity_count(ticker, t)
            rate = count / LOOKBACK_S
            book = self._books.state_of(ticker)
            clean = self._seq.clean_over(lo, t)
            tte = (meta.occurrence_datetime - t).total_seconds()
            remaining = ((self.session_end - t).total_seconds()
                         if self.session_end is not None else float("inf"))

            # INTEGER comparison -- see MIN_OB_EVENTS_IN_LOOKBACK.
            reason = None
            if warmup:
                reason = NOT_SELECTED_WARMUP
            elif count < MIN_OB_EVENTS_IN_LOOKBACK:
                reason = NOT_SELECTED_LOW_ACTIVITY
            elif not book.publishable:
                reason = NOT_SELECTED_BOOK_UNPUBLISHABLE
            elif not clean:
                reason = NOT_SELECTED_SEQUENCE_FAULT
            elif remaining <= MIN_SESSION_REMAINING_S:
                # NOT a TTE test. A market deep in `late_resolution` is
                # eligible; a market hours from its event is not, if the
                # session is about to end.
                reason = NOT_SELECTED_SESSION_TOO_SHORT

            eligible = reason is None
            rows.append({
                "market": ticker, "series": meta.series,
                "activity_lookback_count": count, "activity_rate": rate,
                "book_state": book.state, "sequence_clean": clean,
                "tte_seconds": tte, "tte_bin": tte_bin(tte),
                "eligible": eligible, "reason": reason,
            })
            if eligible:
                ranked.append((-count, ticker))

        # Frozen ordering: activity DESCENDING, ties by ticker ASCENDING.
        ranked.sort()
        chosen = {tk: i + 1 for i, (_c, tk) in enumerate(ranked[:self.k])}

        audit = []
        for r in rows:
            tk = r["market"]
            rank = chosen.get(tk)
            selected = rank is not None
            reason = r["reason"]
            if r["eligible"] and not selected:
                reason = NOT_SELECTED_RANK
            audit.append(EligibilityAudit(
                market=tk, series=r["series"], tick_t=t.isoformat(),
                activity_lookback_count=r["activity_lookback_count"],
                activity_rate=round(r["activity_rate"], 6),
                book_state=r["book_state"], sequence_clean=r["sequence_clean"],
                tte_seconds=round(r["tte_seconds"], 3), tte_bin=r["tte_bin"],
                eligible=r["eligible"], rank=rank, selected=selected,
                reason_if_not_selected=reason))

        panel = tuple(tk for tk, _ in sorted(chosen.items(), key=lambda kv: kv[1]))
        assert len(panel) <= self.k, "panel exceeded K -- contract violated"
        return PanelDecision(tick_t=t.isoformat(), is_warmup=warmup,
                             panel=panel, audit=tuple(audit))

    def decision_ticks(self, session_end: datetime | None = None) -> list[datetime]:
        """Panel changes happen ONLY on this clock (invariant 9).

        The first decision is at open + 300 s; nothing before it may emit a
        research row, and activity arriving at open+317 s cannot enter the
        panel until the next scheduled tick.
        """
        end = session_end if session_end is not None else self.session_end
        ticks, t = [], self.session_open + timedelta(seconds=WARMUP_S)
        while t <= end:
            ticks.append(t)
            t += timedelta(seconds=DECISION_TICK_S)
        return ticks


def assert_capacity_relationship(max_events: int, hard_stop_fps: int,
                                 max_seconds: int) -> None:
    """`max_events` must be structurally unreachable before the safety stop.

    The 1,000,000 default right-censored PROD-ACTIVITY-PROFILE-001 day 2 slot C
    at 1,472.3 s of a 1,500 s budget. Pinning the inequality here means nobody
    can later lengthen a session or lower the stop while leaving a constant
    that merely LOOKS large.
    """
    required = hard_stop_fps * max_seconds
    if max_events <= required:
        raise ValueError(
            f"max_events={max_events:,} must exceed hard_stop_fps x "
            f"max_seconds = {hard_stop_fps:,} x {max_seconds:,} = "
            f"{required:,}, or the frame cap can bind before the safety stop "
            f"and silently right-censor the session")


HARD_STOP_FPS = 3500

SESSION_OK = "ok"
SESSION_SAFETY_HALT = "safety_halt"


@dataclass(frozen=True)
class SafetyVerdict:
    breached: bool
    observed_peak_fps: int
    threshold_fps: int
    status: str
    halt_at: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_safety_stop(peak_1s_sliding: int, *, at: datetime | None = None,
                         threshold_fps: int = HARD_STOP_FPS) -> SafetyVerdict:
    """The 3,500 f/s stop, unchanged from PROD-ACTIVITY-PROFILE-001 SS6.

    A stopping rule, not a judgement call. On breach the session halts, no
    further panel tick may run, and the session can never be treated as
    confirmation data.
    """
    breached = peak_1s_sliding > threshold_fps
    return SafetyVerdict(
        breached=breached, observed_peak_fps=peak_1s_sliding,
        threshold_fps=threshold_fps,
        status=SESSION_SAFETY_HALT if breached else SESSION_OK,
        halt_at=(at.isoformat() if (breached and at is not None) else None))


@dataclass(frozen=True)
class RowProvenance:
    """Every research row is rejectable on THIS, never on a filename.

    A future M0/M1 evaluator must be able to exclude profile windows,
    validation sessions, wrong code versions and halted sessions without
    guessing from paths.
    """
    session_id: str
    panel_tick: str
    market: str
    subscription_generation: int | None
    feature_schema_version: str
    capture_commit: str
    preregistration_version: str
    dataset_role: str
    session_status: str

    def __post_init__(self) -> None:
        if self.dataset_role not in DatasetRole.ALL:
            raise ValueError(
                f"dataset_role must be one of {DatasetRole.ALL}, "
                f"got {self.dataset_role!r} -- this field is typed precisely "
                f"so an evaluator never has to infer role from a path")

    def usable_as_confirmation(self) -> bool:
        return (self.dataset_role == DatasetRole.CONFIRMATION
                and self.session_status == SESSION_OK)

    def to_dict(self) -> dict:
        return asdict(self)
