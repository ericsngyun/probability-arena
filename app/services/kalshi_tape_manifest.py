"""KALSHI-TAPE-MANIFEST — freeze the CP6-CP9 qualification session's universe.

This module builds the **session manifest** for the first live DEMO
qualification session of `KALSHI-LIVE-TAPE-COLLECTOR-001`, and it exists to be
run **before any capture happens**. Its whole purpose is to make the
subscription a *preregistered* object rather than something an analyst picks
after seeing which tickers behaved.

Read-only. This module issues no venue write of any kind: the only route it can
reach is `GET /markets` on the public market-data host, it holds no credential,
and it constructs no order, portfolio, or private-channel request.

## Why the selection rule is frozen in code rather than in prose

Eric's constraint, quoted:

> Replacements must NOT be chosen based on which tickers produce cleaner
> telemetry. That is cherry-picking one level down, and it is how the
> EDGE-SELECTION lane died.

Every gate below is therefore **structural** — it asks whether a market is
*capable* of emitting book messages at all (is it quoted? is there resting
size? is the book uncrossed?) — and never *how good the resulting tape looks*.
No gate in this file can be evaluated from a capture, which is the property
that makes it immune to the failure mode above. A gate that needed a tape to
evaluate would be exactly the cherry-pick.

## The sampling frame is NOT the venue

The manifest records the complete candidate population, the statistic, and the
snapshot timestamp so that a future analyst cannot mistake the twelve chosen
markets for a representative sample of Kalshi. They are not. They are the
twelve highest-activity survivors of a five-gate eligibility funnel applied to
one paginated snapshot of one environment at one instant, and the manifest says
so on its face (`REPRESENTATIVENESS`).

## Refusal is a first-class outcome

If the venue cannot supply twelve eligible markets, or the three strata are not
cleanly separated, or the twelve do not span enough distinct contract/event
structures, this module emits a **REFUSED** manifest carrying the evidence
rather than padding the universe or blurring the strata to reach twelve. A
refusal is a finding about the venue and is the honest result; a manifest that
always succeeds would be measuring nothing.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- the statistic --------------------------------------------------------------

STATISTIC_NAME = "volume_24h_contracts"
STATISTIC_SOURCE_FIELDS = ("volume_24h", "volume_24h_fp")

STATISTIC_DEFINITION = (
    "volume_24h_contracts — the venue-reported trailing-24-hour traded contract "
    "count for a market, read from a single snapshot of "
    "GET /trade-api/v2/markets. The venue delivers it as the fixed-point string "
    "field `volume_24h_fp` (legacy integer field `volume_24h` is accepted when "
    "present); both are contract counts, not currency."
)

STATISTIC_RATIONALE = (
    "It is the only venue-reported activity measure available for EVERY market "
    "in one read-only snapshot, and it is monotone in participant activity: a "
    "market with more contracts traded had more participants acting on it, and "
    "participants who act also quote, cancel and replace. Since the tape's "
    "message volume is dominated by orderbook_delta, and orderbook_delta is "
    "emitted by the same population whose trading this field counts, it is a "
    "defensible ORDERING variable for 'more active' vs 'less active'."
)

# Stated on the manifest itself, not in a commit message. A stratification that
# cannot be justified is worse than an unstratified universe, because it invites
# false confidence.
STATISTIC_LIMITATIONS = (
    "It counts TRADES, not MESSAGES. The tape's volume is dominated by "
    "orderbook_delta, which fires on every quote revision — including cancels "
    "and replaces that never trade. A market quoted by a churning maker can "
    "emit a high message rate at near-zero volume, and a market that trades in "
    "occasional blocks can emit low message rates at high volume. The proxy is "
    "monotone-at-best and its rank correlation with message rate is UNMEASURED.",

    "It is a trailing-24-hour AGGREGATE, not an instantaneous rate. A market "
    "that was busy twenty hours ago and is dead now ranks high. The session "
    "measures the latter and the statistic describes the former.",

    "It is read at snapshot time; the session runs later. Event-driven markets "
    "(sports especially) reprice around scheduled events, so a ranking can "
    "invert between the freeze and the capture. The manifest is frozen "
    "deliberately anyway — a universe rechosen at capture time is not a "
    "preregistration.",

    "It says nothing about the DEPTH of a book, and orderbook_delta cost scales "
    "with the number of price levels that move, not with contracts traded.",

    "Cross-environment transfer is unestablished. A DEMO ranking is evidence "
    "about DEMO. Nothing here licenses treating it as a production ranking.",
)

# The honest alternative, named so that nobody has to rediscover it.
STATISTIC_STRONGER_ALTERNATIVE = (
    "The only statistic that is NOT a proxy is the message rate itself, "
    "obtained by subscribing to each candidate and counting frames. That was "
    "deliberately NOT done: it requires opening the socket and capturing, which "
    "is the very thing this manifest must precede, and choosing a universe by "
    "observed frame behaviour is the cherry-pick the selection rule exists to "
    "prevent. The gap is recorded rather than closed."
)

# --- strata ---------------------------------------------------------------------

STRATUM_HIGH = "high"
STRATUM_MEDIUM = "medium"
STRATUM_LOW = "low"
STRATA = (STRATUM_HIGH, STRATUM_MEDIUM, STRATUM_LOW)

PER_STRATUM = 4
UNIVERSE_SIZE = PER_STRATUM * len(STRATA)  # 12, frozen by Eric

# --- the authorized session parameters, frozen by Eric --------------------------
# Reproduced here so the manifest carries them and drift is visible in a diff.

SESSION_MIN_SECONDS = 2 * 60 * 60          # 2 hours
SESSION_MIN_ARCHIVED_FRAMES = 100_000
SESSION_MAX_SECONDS = 4 * 60 * 60          # 4 hours, first qualification run
SESSION_STOP_RULE = (
    "Run until BOTH the 2-hour minimum AND the 100,000-archived-live-frame "
    "minimum are satisfied — whichever occurs LATER — and stop unconditionally "
    "at the 4-hour maximum even if the frame minimum has not been reached. "
    "Reaching the 4-hour cap short of 100,000 frames is a FINDING about DEMO "
    "message rates, not a reason to extend the session."
)


class ManifestError(RuntimeError):
    """The manifest could not be built from the data supplied."""


# --- numeric helpers ------------------------------------------------------------

def _num(raw: dict, *names: str) -> float:
    """Read the first present field among `names` as a float, else 0.0.

    The venue sends activity and size fields as fixed-point STRINGS
    (`"100.00"`) since the fp migration, and as integers before it. Returning
    0.0 for an absent field is correct here — every caller treats 0 as
    'no evidence of activity' — but returning 0.0 for an UNPARSEABLE field
    would silently launder corruption into a benign value, so that case raises.
    """
    for name in names:
        if raw.get(name) in (None, ""):
            continue
        try:
            value = float(raw[name])
        except (TypeError, ValueError) as exc:
            raise ManifestError(
                f"market {raw.get('ticker')!r} field {name!r} is {raw[name]!r}, "
                f"which is not a number"
            ) from exc
        if math.isnan(value) or math.isinf(value):
            raise ManifestError(
                f"market {raw.get('ticker')!r} field {name!r} is non-finite: "
                f"{raw[name]!r}"
            )
        return value
    return 0.0


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def series_of(ticker: str) -> str:
    """Kalshi tickers are `SERIES-EVENTSUFFIX-STRIKE`; the series is the head.

    Used only for the structure-diversity gate. It is a naming convention, not
    a promised contract, which is why the gate ALSO requires distinct
    `event_ticker` values — a field the venue actually sends.
    """
    return (ticker or "").split("-")[0]


# --- candidate ------------------------------------------------------------------

@dataclass
class Candidate:
    """One market in the candidate population, with everything the gates read."""

    ticker: str
    event_ticker: str | None
    series: str
    title: str
    status: str
    strike_type: str | None
    market_type: str | None
    statistic: float
    lifetime_volume: float
    open_interest: float
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float
    updated_time: datetime | None
    close_time: datetime | None
    # filled by the funnel
    ineligible_reasons: list[str] = field(default_factory=list)
    rank: int | None = None
    stratum: str | None = None
    selected: bool = False

    @property
    def eligible(self) -> bool:
        return not self.ineligible_reasons

    def staleness_hours(self, now: datetime) -> float | None:
        if self.updated_time is None:
            return None
        return (now - self.updated_time).total_seconds() / 3600.0

    def to_row(self, now: datetime) -> dict:
        stale = self.staleness_hours(now)
        return {
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "series": self.series,
            "title": self.title,
            "strike_type": self.strike_type,
            "statistic": self.statistic,
            "lifetime_volume": self.lifetime_volume,
            "open_interest": self.open_interest,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "yes_bid_size": self.yes_bid_size,
            "yes_ask_size": self.yes_ask_size,
            "updated_time": self.updated_time.isoformat() if self.updated_time else None,
            "staleness_hours": None if stale is None else round(stale, 3),
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "eligible": self.eligible,
            "ineligible_reasons": list(self.ineligible_reasons),
            "rank": self.rank,
            "stratum": self.stratum,
            "selected": self.selected,
        }


def build_candidate(raw: dict) -> Candidate:
    ticker = raw.get("ticker")
    if not ticker:
        raise ManifestError(f"market object has no ticker: {sorted(raw)}")
    return Candidate(
        ticker=ticker,
        event_ticker=raw.get("event_ticker"),
        series=series_of(ticker),
        title=raw.get("title") or "",
        status=(raw.get("status") or "unknown"),
        strike_type=raw.get("strike_type"),
        market_type=raw.get("market_type"),
        statistic=_num(raw, *STATISTIC_SOURCE_FIELDS),
        lifetime_volume=_num(raw, "volume", "volume_fp"),
        open_interest=_num(raw, "open_interest", "open_interest_fp"),
        yes_bid=_num(raw, "yes_bid_dollars", "yes_bid"),
        yes_ask=_num(raw, "yes_ask_dollars", "yes_ask"),
        yes_bid_size=_num(raw, "yes_bid_size_fp", "yes_bid_size"),
        yes_ask_size=_num(raw, "yes_ask_size_fp", "yes_ask_size"),
        updated_time=_parse_time(raw.get("updated_time")),
        close_time=_parse_time(raw.get("close_time")),
    )


# --- eligibility ----------------------------------------------------------------
#
# Every gate answers "can this market emit book messages at all?". None of them
# can be evaluated from a capture. That is the property that keeps the funnel
# from becoming a telemetry-quality filter.

@dataclass(frozen=True)
class EligibilityPolicy:
    max_staleness_hours: float = 24.0
    require_two_sided_quote: bool = True
    require_uncrossed: bool = True
    require_resting_size: bool = True
    require_nonzero_statistic: bool = True
    min_seconds_to_close: float = 6 * 60 * 60

    def describe(self) -> list[str]:
        out = [
            f"updated_time is present and within {self.max_staleness_hours}h of "
            f"the snapshot (a market the venue has not touched cannot be "
            f"emitting a live tape)",
        ]
        if self.require_two_sided_quote:
            out.append("yes_bid > 0 AND yes_ask > 0 (a one-sided or empty book "
                       "has no resting orders to delta)")
        if self.require_uncrossed:
            out.append("yes_bid < yes_ask (a crossed book cannot be the output "
                       "of a live matching engine; it is corrupt or synthetic)")
        if self.require_resting_size:
            out.append("yes_bid_size > 0 AND yes_ask_size > 0, both non-negative "
                       "(a negative resting size is physically impossible)")
        if self.require_nonzero_statistic:
            out.append(f"{STATISTIC_NAME} > 0 (a market with no trailing volume "
                       f"cannot be ranked by it)")
        out.append(f"close_time is at least {self.min_seconds_to_close / 3600:.1f}h "
                   f"after the snapshot, so the market survives the session's "
                   f"4-hour maximum")
        return out


def apply_eligibility(
    candidates: list[Candidate], *, now: datetime, policy: EligibilityPolicy
) -> None:
    """Annotate each candidate with its ineligibility reasons, in place."""
    for c in candidates:
        reasons: list[str] = []

        stale = c.staleness_hours(now)
        if stale is None:
            reasons.append("no_updated_time")
        elif stale > policy.max_staleness_hours:
            reasons.append(f"stale_{stale:.1f}h")
        elif stale < 0:
            # A future updated_time is a clock/venue anomaly, not freshness.
            reasons.append(f"updated_time_in_future_{-stale:.1f}h")

        if policy.require_two_sided_quote and not (c.yes_bid > 0 and c.yes_ask > 0):
            reasons.append("no_two_sided_quote")
        if policy.require_uncrossed and c.yes_bid > 0 and c.yes_ask > 0 \
                and c.yes_bid >= c.yes_ask:
            reasons.append("crossed_book")
        if policy.require_resting_size and not (c.yes_bid_size > 0 and c.yes_ask_size > 0):
            reasons.append("no_resting_size")
        if c.yes_bid_size < 0 or c.yes_ask_size < 0:
            reasons.append("negative_resting_size")
        if policy.require_nonzero_statistic and not c.statistic > 0:
            reasons.append(f"zero_{STATISTIC_NAME}")
        if c.close_time is not None:
            remaining = (c.close_time - now).total_seconds()
            if remaining < policy.min_seconds_to_close:
                reasons.append(f"closes_in_{remaining / 3600:.1f}h")

        c.ineligible_reasons = reasons


# --- integrity audit of the frame ------------------------------------------------

def audit_frame(candidates: list[Candidate], *, now: datetime) -> dict:
    """Venue-health counts over the WHOLE frame, reported whatever the verdict.

    These are the numbers that decide whether the activity statistic means
    anything, so they are computed over every candidate rather than over the
    survivors — a funnel that only reports its output cannot tell you that its
    input was corrupt.
    """
    n = len(candidates)
    crossed = [c for c in candidates
               if c.yes_bid > 0 and c.yes_ask > 0 and c.yes_bid >= c.yes_ask]
    negative = [c for c in candidates if c.yes_bid_size < 0 or c.yes_ask_size < 0]
    with_stat = [c for c in candidates if c.statistic > 0]
    fresh = [c for c in candidates
             if (c.staleness_hours(now) or float("inf")) <= 24.0]
    # THE contradiction test: a field that claims trailing-24h volume on a market
    # the venue has not touched in over 24h is not a trailing-24h field.
    contradictory = [c for c in with_stat
                     if (c.staleness_hours(now) or float("inf")) > 24.0]
    return {
        "frame_size": n,
        "markets_with_nonzero_statistic": len(with_stat),
        "markets_updated_within_24h": len(fresh),
        "markets_two_sided_quote": sum(
            1 for c in candidates if c.yes_bid > 0 and c.yes_ask > 0),
        "markets_with_resting_size": sum(
            1 for c in candidates if c.yes_bid_size > 0 and c.yes_ask_size > 0),
        "crossed_books": len(crossed),
        "negative_resting_sizes": len(negative),
        "nonzero_statistic_but_not_updated_in_24h": len(contradictory),
        "statistic_contradiction_rate": (
            round(len(contradictory) / len(with_stat), 6) if with_stat else None),
        "statistic_is_internally_consistent": (
            len(contradictory) == 0 if with_stat else None),
    }


# --- ranking and stratification --------------------------------------------------

def rank_candidates(eligible: list[Candidate]) -> list[Candidate]:
    """Descending by statistic, ties broken by ticker ASCENDING.

    The tie-break is lexicographic on an identifier the venue assigned before
    any of this existed. It is deterministic, reproducible from the manifest
    alone, and — the point — carries no information about how a market behaves,
    so a tie cannot be resolved in favour of the market that later looks better.
    """
    ordered = sorted(eligible, key=lambda c: (-c.statistic, c.ticker))
    for i, c in enumerate(ordered, start=1):
        c.rank = i
    return ordered


def assign_strata(ordered: list[Candidate]) -> dict[str, list[Candidate]]:
    """Contiguous tertiles of the ranked eligible set.

    Tertiles of the ELIGIBLE population, not of the venue: `low` means 'least
    active among markets that can emit a tape at all', never 'a typical Kalshi
    market'. The manifest repeats this where a reader will see it.
    """
    n = len(ordered)
    if n < UNIVERSE_SIZE:
        raise ManifestError(
            f"cannot stratify {n} eligible markets into {len(STRATA)} strata of "
            f"{PER_STRATUM}; {UNIVERSE_SIZE} are required")
    cut_a = n // 3
    cut_b = (2 * n) // 3
    groups = {
        STRATUM_HIGH: ordered[:cut_a],
        STRATUM_MEDIUM: ordered[cut_a:cut_b],
        STRATUM_LOW: ordered[cut_b:],
    }
    for name, members in groups.items():
        for c in members:
            c.stratum = name
    return groups


def select_from_stratum(
    members: list[Candidate], *, already_used_events: set, count: int = PER_STRATUM
) -> list[Candidate]:
    """Take `count` from a stratum, spreading across event structures.

    Two deterministic passes in rank order: the first accepts only markets
    whose `event_ticker` is not already claimed anywhere in the selection, the
    second fills any shortfall from what remains. Rank order is the only
    ordering used, so the outcome is a pure function of the frozen ranking —
    there is no discretionary step where a market could be swapped for one that
    behaves better.
    """
    picked: list[Candidate] = []
    used = set(already_used_events)
    for c in members:
        if len(picked) == count:
            break
        key = c.event_ticker or c.ticker
        if key not in used:
            picked.append(c)
            used.add(key)
    if len(picked) < count:
        for c in members:
            if len(picked) == count:
                break
            if c not in picked:
                picked.append(c)
    return picked[:count]


# --- gates on the finished selection ---------------------------------------------

@dataclass(frozen=True)
class SelectionPolicy:
    """Thresholds that decide QUALIFIED vs REFUSED.

    `min_separation_ratio` is the anti-blur gate. Contiguous tertiles are
    always ordered, so 'separable' cannot mean 'ordered' — it has to mean the
    bands are far enough apart that calling them high/medium/low is a claim and
    not a relabelling of an arbitrary cut through a continuum.
    """
    min_separation_ratio: float = 2.0
    min_distinct_events: int = 6
    min_distinct_series: int = 4
    max_per_event: int = 3
    min_distinct_strike_types: int = 2


def evaluate_separation(groups: dict[str, list[Candidate]]) -> dict:
    """Boundary statistics between adjacent strata, on the SELECTED members."""
    def lo(name):
        return min(c.statistic for c in groups[name])

    def hi(name):
        return max(c.statistic for c in groups[name])

    out = {}
    for upper, lower in ((STRATUM_HIGH, STRATUM_MEDIUM),
                         (STRATUM_MEDIUM, STRATUM_LOW)):
        u_min, l_max = lo(upper), hi(lower)
        out[f"{upper}_over_{lower}"] = {
            "upper_stratum_min": u_min,
            "lower_stratum_max": l_max,
            "ratio": (u_min / l_max) if l_max > 0 else None,
            "boundary_tie": u_min == l_max,
        }
    for name in STRATA:
        out[f"{name}_range"] = {"min": lo(name), "max": hi(name),
                                "n": len(groups[name])}
    return out


def check_gates(
    selected: dict[str, list[Candidate]], *, policy: SelectionPolicy
) -> list[str]:
    """Return the list of gate FAILURES. Empty means qualified."""
    failures: list[str] = []
    flat = [c for name in STRATA for c in selected.get(name, [])]

    for name in STRATA:
        got = len(selected.get(name, []))
        if got != PER_STRATUM:
            failures.append(
                f"stratum {name!r} has {got} markets, requires {PER_STRATUM}")
    if len(flat) != UNIVERSE_SIZE:
        failures.append(f"universe has {len(flat)} markets, requires {UNIVERSE_SIZE}")
    if not flat:
        return failures

    if len({c.ticker for c in flat}) != len(flat):
        failures.append("duplicate tickers in the universe")

    sep = evaluate_separation(selected)
    for key in (f"{STRATUM_HIGH}_over_{STRATUM_MEDIUM}",
                f"{STRATUM_MEDIUM}_over_{STRATUM_LOW}"):
        s = sep[key]
        if s["boundary_tie"]:
            failures.append(
                f"strata not separable at {key}: boundary values are tied at "
                f"{s['upper_stratum_min']}")
        elif s["ratio"] is None:
            failures.append(
                f"strata not separable at {key}: lower stratum max is 0, so the "
                f"separation ratio is undefined")
        elif s["ratio"] < policy.min_separation_ratio:
            failures.append(
                f"strata not separable at {key}: ratio {s['ratio']:.3f} < "
                f"required {policy.min_separation_ratio}")

    events = {c.event_ticker or c.ticker for c in flat}
    if len(events) < policy.min_distinct_events:
        failures.append(
            f"universe spans {len(events)} distinct events, requires at least "
            f"{policy.min_distinct_events} — twelve near-identical markets is "
            f"the outcome this gate exists to prevent")
    series = {c.series for c in flat}
    if len(series) < policy.min_distinct_series:
        failures.append(
            f"universe spans {len(series)} distinct series, requires at least "
            f"{policy.min_distinct_series}")
    strikes = {c.strike_type for c in flat}
    if len(strikes) < policy.min_distinct_strike_types:
        failures.append(
            f"universe spans {len(strikes)} distinct contract structures "
            f"(strike_type), requires at least {policy.min_distinct_strike_types}")
    for ev in events:
        n = sum(1 for c in flat if (c.event_ticker or c.ticker) == ev)
        if n > policy.max_per_event:
            failures.append(
                f"event {ev!r} contributes {n} of {UNIVERSE_SIZE} markets, "
                f"maximum is {policy.max_per_event}")
    return failures


# --- the manifest ----------------------------------------------------------------

@dataclass
class SnapshotWindow:
    """When the activity snapshot was taken, to the second.

    Two timestamps, not one: a 73k-market enumeration takes minutes, so a
    single 'snapshot time' would be a fiction. `started_at` is the instant the
    first page was requested and is the canonical reference for staleness;
    `completed_at` bounds how much drift the frame can contain, and the gap
    between them is reported rather than hidden.
    """
    started_at: datetime
    completed_at: datetime
    pages: int
    environment: str
    host: str
    request_params: dict

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()

    def to_dict(self) -> dict:
        return {
            "activity_snapshot_started_at": self.started_at.isoformat(),
            "activity_snapshot_completed_at": self.completed_at.isoformat(),
            "activity_snapshot_duration_seconds": round(self.duration_seconds, 3),
            "canonical_snapshot_timestamp": self.started_at.isoformat(),
            "pages_fetched": self.pages,
            "environment": self.environment,
            "host": self.host,
            "request_params": dict(self.request_params),
        }


def build_manifest(
    raw_markets: list[dict],
    *,
    snapshot: SnapshotWindow,
    eligibility: EligibilityPolicy | None = None,
    selection: SelectionPolicy | None = None,
    top_ineligible: int = 250,
) -> dict:
    """The whole decision, as one pure function of the snapshot.

    Pure by construction: no clock read, no network, no filesystem. `now` is
    `snapshot.started_at`, so re-running this on the archived frame reproduces
    the manifest byte for byte — which is what 'reproducible selection' has to
    mean if it is to mean anything.
    """
    eligibility = eligibility or EligibilityPolicy()
    selection = selection or SelectionPolicy()
    now = snapshot.started_at

    candidates = [build_candidate(raw) for raw in raw_markets]
    if len({c.ticker for c in candidates}) != len(candidates):
        raise ManifestError(
            "the snapshot contains duplicate tickers; pagination overlapped and "
            "the frame is not a valid population")

    apply_eligibility(candidates, now=now, policy=eligibility)
    integrity = audit_frame(candidates, now=now)

    eligible = [c for c in candidates if c.eligible]
    refusals: list[str] = []
    groups: dict[str, list[Candidate]] = {}
    selected: dict[str, list[Candidate]] = {}
    separation: dict = {}

    if len(eligible) < UNIVERSE_SIZE:
        refusals.append(
            f"only {len(eligible)} of {len(candidates)} markets in the frame are "
            f"eligible; {UNIVERSE_SIZE} are required. The universe must NOT be "
            f"padded to reach {UNIVERSE_SIZE}.")
    else:
        ordered = rank_candidates(eligible)
        groups = assign_strata(ordered)
        used_events: set = set()
        for name in STRATA:
            picked = select_from_stratum(groups[name], already_used_events=used_events)
            selected[name] = picked
            for c in picked:
                c.selected = True
                used_events.add(c.event_ticker or c.ticker)
        separation = evaluate_separation(selected)
        refusals.extend(check_gates(selected, policy=selection))

    qualified = not refusals
    flat = [c for name in STRATA for c in selected.get(name, [])]

    eligible_events = {c.event_ticker or c.ticker for c in eligible}
    eligible_series = {c.series for c in eligible}

    return {
        "manifest_id": "KALSHI-TAPE-MANIFEST-001",
        "milestone": "KALSHI-LIVE-TAPE-COLLECTOR-001",
        "purpose": (
            "Freeze the universe and selection rule for the first live DEMO "
            "qualification session (CP6-CP9) BEFORE any capture happens."),
        "verdict": "QUALIFIED" if qualified else "REFUSED",
        "refusal_reasons": refusals,

        "snapshot": snapshot.to_dict(),

        "statistic": {
            "name": STATISTIC_NAME,
            "source_fields": list(STATISTIC_SOURCE_FIELDS),
            "definition": STATISTIC_DEFINITION,
            "why_it_is_a_reasonable_proxy": STATISTIC_RATIONALE,
            "limitations": list(STATISTIC_LIMITATIONS),
            "stronger_alternative_not_used": STATISTIC_STRONGER_ALTERNATIVE,
        },

        "selection_rule": {
            "frame": (
                "Every market returned by GET /markets with status=open and "
                "mve_filter=exclude, paginated to exhaustion. Multivariate-event "
                "(MVE) combinatorial shards are excluded because they are "
                "generated near-identical permutations of the same underlying "
                "legs — precisely the 'twelve near-identical markets' the "
                "universe requirement forbids."),
            "eligibility_gates": eligibility.describe(),
            "gate_design_note": (
                "Every gate is STRUCTURAL — it asks whether a market can emit "
                "book messages at all. None can be evaluated from a capture, so "
                "no gate can be tuned toward tickers that produce cleaner "
                "telemetry."),
            "ranking": (
                f"Eligible markets sorted by {STATISTIC_NAME} DESCENDING, ties "
                f"broken by ticker ASCENDING (lexicographic). The tie-break "
                f"carries no behavioural information."),
            "stratification": (
                f"Contiguous tertiles of the ranked ELIGIBLE set: ranks "
                f"[1, n/3) = high, [n/3, 2n/3) = medium, [2n/3, n] = low. "
                f"{PER_STRATUM} markets taken from each."),
            "within_stratum_pick": (
                "Two deterministic passes in rank order: first accept only "
                "markets whose event_ticker is unclaimed anywhere in the "
                "selection, then fill any shortfall from the remainder."),
            "replacement_rule": (
                "If a selected market becomes unusable before the session, it is "
                "replaced by the NEXT market in frozen rank order within the same "
                "stratum whose event_ticker is unclaimed. Replacements must NOT "
                "be chosen by observed telemetry quality, message volume, or how "
                "clean the resulting tape looks — that is cherry-picking one "
                "level down. Any replacement is recorded as an amendment to this "
                "manifest, with its reason, BEFORE the session starts."),
            "thresholds": {
                "min_separation_ratio": selection.min_separation_ratio,
                "min_distinct_events": selection.min_distinct_events,
                "min_distinct_series": selection.min_distinct_series,
                "min_distinct_strike_types": selection.min_distinct_strike_types,
                "max_per_event": selection.max_per_event,
                "max_staleness_hours": eligibility.max_staleness_hours,
                "min_seconds_to_close": eligibility.min_seconds_to_close,
            },
        },

        "session_parameters": {
            "min_seconds": SESSION_MIN_SECONDS,
            "min_archived_live_frames": SESSION_MIN_ARCHIVED_FRAMES,
            "max_seconds": SESSION_MAX_SECONDS,
            "stop_rule": SESSION_STOP_RULE,
            "universe_size": UNIVERSE_SIZE,
            "per_stratum": PER_STRATUM,
            "authorized_by": "Eric",
            "frozen_before_capture": True,
        },

        "population": {
            "frame_size": len(candidates),
            "eligible_count": len(eligible),
            "eligible_distinct_events": len(eligible_events),
            "eligible_distinct_series": len(eligible_series),
            "ineligible_count": len(candidates) - len(eligible),
            "ineligibility_histogram": _reason_histogram(candidates),
        },

        "frame_integrity": integrity,

        "strata_ranges": separation,

        "universe": [
            {"stratum": name, "members": [c.to_row(now) for c in selected.get(name, [])]}
            for name in STRATA
        ],

        "universe_structures": {
            "distinct_events": sorted({c.event_ticker or c.ticker for c in flat}),
            "distinct_series": sorted({c.series for c in flat}),
            "distinct_strike_types": sorted(
                {str(c.strike_type) for c in flat}),
        },

        "representativeness": (
            "THESE TWELVE MARKETS ARE NOT A REPRESENTATIVE SAMPLE OF THE VENUE. "
            f"They are the survivors of a multi-gate eligibility funnel applied "
            f"to ONE paginated snapshot of the {snapshot.environment} "
            f"environment taken at {snapshot.started_at.isoformat()}, ranked by "
            f"a trade-volume proxy for message rate whose rank correlation with "
            f"actual message rate is UNMEASURED. 'low' means least active AMONG "
            f"ELIGIBLE MARKETS, not typical of the venue: the frame contained "
            f"{len(candidates)} open markets and only {len(eligible)} were "
            f"eligible at all. Any statistic computed from the resulting tape "
            f"describes this universe and must not be generalised to Kalshi."),

        "candidate_population": _population_section(
            candidates, eligible, now=now, top_ineligible=top_ineligible),
    }


def frame_digest(candidates: list[Candidate]) -> str:
    """SHA-256 over the whole frame's (ticker, statistic) pairs, ticker-sorted.

    The frame is ~70k markets on this venue, which is far too large to commit
    and far too valuable to drop: without it, 'the full candidate population'
    would be an unverifiable claim. The digest is the compromise that keeps the
    claim checkable — re-enumerate, recompute, compare. It commits to the
    statistic as well as the membership, so a frame with the same tickers and
    different activity values does not collide.
    """
    h = hashlib.sha256()
    for c in sorted(candidates, key=lambda c: c.ticker):
        h.update(f"{c.ticker}\x1f{c.statistic!r}\x1e".encode())
    return h.hexdigest()


def _population_section(
    candidates: list[Candidate],
    eligible: list[Candidate],
    *,
    now: datetime,
    top_ineligible: int,
) -> dict:
    """Eric's requirement 3, as much of it as can honestly be committed.

    The requirement is that the sampling frame be explicit and reproducible so
    that a future analyst cannot mistake the twelve for the venue. What that
    needs is (a) every member of the pool the twelve were actually drawn from,
    complete and ranked, (b) enough of the rejected population to see WHY it was
    rejected, and (c) a commitment to the rest that can be checked. Shipping
    70,000 rows of `statistic = 0.0` would satisfy the letter and bury all
    three. The truncation is declared in the payload rather than being silent.
    """
    ineligible = sorted(
        (c for c in candidates if not c.eligible),
        key=lambda c: (-c.statistic, c.ticker))
    shown = ineligible[:top_ineligible]
    return {
        "note": (
            "`eligible_ranked` is COMPLETE: it is the entire pool the twelve "
            "were drawn from, every member carrying its statistic value, rank "
            "and stratum. The full enumerated frame is larger than is sensible "
            "to commit, so it is represented by `frame_digest_sha256` plus the "
            "highest-statistic rejected markets, and can be reproduced exactly "
            "by re-running the enumeration and comparing the digest."),
        "frame_size": len(candidates),
        "frame_digest_sha256": frame_digest(candidates),
        "frame_digest_covers": "sha256 of ticker\\x1f repr(statistic) \\x1e, ticker-sorted, whole frame",
        "eligible_ranked_is_complete": True,
        "eligible_ranked": [
            c.to_row(now) for c in sorted(
                eligible, key=lambda c: (c.rank if c.rank is not None else 0,
                                         -c.statistic, c.ticker))],
        "ineligible_shown": len(shown),
        "ineligible_total": len(ineligible),
        "ineligible_truncated": len(ineligible) > len(shown),
        "top_ineligible_by_statistic": [c.to_row(now) for c in shown],
    }


def _reason_histogram(candidates: list[Candidate]) -> dict:
    hist: dict = {}
    for c in candidates:
        for r in c.ineligible_reasons:
            # Collapse the numeric suffix so the histogram has bounded keys.
            key = r.split("_")[0] if r.startswith(("stale", "closes")) else r
            hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- rendering -------------------------------------------------------------------

def render_markdown(manifest: dict) -> str:
    """The frozen artifact a human reads. Same facts as the JSON, no new ones."""
    m = manifest
    snap = m["snapshot"]
    out: list[str] = []
    w = out.append

    w(f"# {m['manifest_id']} — frozen session manifest")
    w("")
    w(f"**Milestone:** {m['milestone']} (CP6-CP9 live DEMO qualification session)  ")
    w(f"**Verdict:** **{m['verdict']}**  ")
    w(f"**Activity snapshot (canonical timestamp):** "
      f"`{snap['canonical_snapshot_timestamp']}`  ")
    w(f"**Environment:** `{snap['environment']}` — `{snap['host']}`")
    w("")
    w("> Frozen BEFORE any capture. This document fixes the universe and the "
      "selection rule so that neither can be chosen after seeing how the tape "
      "behaved.")
    w("")

    if m["verdict"] == "REFUSED":
        w("## VERDICT: REFUSED — the session must not run as specified")
        w("")
        w("The authorized universe (12 live tickers, stratified 4 high / 4 medium "
          "/ 4 low by message rate, spanning several contract/event structures) "
          "**cannot be constructed from this venue at this snapshot**. Padding "
          "the universe or blurring the strata to reach 12 was explicitly "
          "forbidden, so it was not done. The reasons:")
        w("")
        for r in m["refusal_reasons"]:
            w(f"- {r}")
        w("")

    w("## 1. The activity snapshot")
    w("")
    w("| field | value |")
    w("|---|---|")
    w(f"| canonical timestamp (first page requested) | `{snap['canonical_snapshot_timestamp']}` |")
    w(f"| enumeration started | `{snap['activity_snapshot_started_at']}` |")
    w(f"| enumeration completed | `{snap['activity_snapshot_completed_at']}` |")
    w(f"| enumeration duration | {snap['activity_snapshot_duration_seconds']} s |")
    w(f"| pages fetched | {snap['pages_fetched']} |")
    w(f"| request | `{snap['request_params']}` |")
    w("")
    w("Two timestamps, not one: the enumeration takes minutes, so a single "
      "'snapshot time' would be a fiction. The canonical reference for every "
      "staleness computation is the START; the completion time bounds how much "
      "drift the frame can contain.")
    w("")

    st = m["statistic"]
    w("## 2. The ranking statistic")
    w("")
    w(f"**`{st['name']}`** — sourced from `{st['source_fields']}`.")
    w("")
    w(st["definition"])
    w("")
    w("**Why it is a reasonable proxy for message rate.** " + st["why_it_is_a_reasonable_proxy"])
    w("")
    w("**Limitations — read these before using any number derived from this tape.**")
    w("")
    for lim in st["limitations"]:
        w(f"- {lim}")
    w("")
    w("**The stronger statistic that was deliberately not used.** "
      + st["stronger_alternative_not_used"])
    w("")

    sr = m["selection_rule"]
    w("## 3. The selection rule (frozen)")
    w("")
    w(f"**Frame.** {sr['frame']}")
    w("")
    w("**Eligibility gates.** A market is a candidate only if all hold:")
    w("")
    for g in sr["eligibility_gates"]:
        w(f"- {g}")
    w("")
    w(f"> {sr['gate_design_note']}")
    w("")
    w(f"**Ranking.** {sr['ranking']}")
    w("")
    w(f"**Stratification.** {sr['stratification']}")
    w("")
    w(f"**Within-stratum pick.** {sr['within_stratum_pick']}")
    w("")
    w(f"**Replacement rule.** {sr['replacement_rule']}")
    w("")
    w("**Thresholds.**")
    w("")
    w("| threshold | value |")
    w("|---|---|")
    for k, v in sr["thresholds"].items():
        w(f"| `{k}` | {v} |")
    w("")

    sp = m["session_parameters"]
    w("## 4. Authorized session parameters (frozen by Eric, not alterable here)")
    w("")
    w("| parameter | value |")
    w("|---|---|")
    w(f"| minimum duration | {sp['min_seconds'] // 3600} h |")
    w(f"| minimum archived live frames | {sp['min_archived_live_frames']:,} |")
    w(f"| maximum duration | {sp['max_seconds'] // 3600} h |")
    w(f"| universe size | {sp['universe_size']} ({sp['per_stratum']} per stratum) |")
    w("")
    w(f"**Stop rule.** {sp['stop_rule']}")
    w("")

    pop = m["population"]
    w("## 5. The candidate population")
    w("")
    w("| quantity | value |")
    w("|---|---|")
    w(f"| open markets enumerated (the frame) | {pop['frame_size']:,} |")
    w(f"| eligible after the gates | {pop['eligible_count']:,} |")
    w(f"| ineligible | {pop['ineligible_count']:,} |")
    w(f"| distinct events among eligible | {pop['eligible_distinct_events']} |")
    w(f"| distinct series among eligible | {pop['eligible_distinct_series']} |")
    w("")
    w("**Why each rejected market was rejected** (a market can fail several gates):")
    w("")
    w("| gate failed | markets |")
    w("|---|---|")
    for k, v in pop["ineligibility_histogram"].items():
        w(f"| `{k}` | {v:,} |")
    w("")

    fi = m["frame_integrity"]
    w("## 6. Frame integrity — does the statistic mean anything?")
    w("")
    w("Computed over the WHOLE frame, not over the survivors. A funnel that only "
      "reports its output cannot tell you its input was corrupt.")
    w("")
    w("| check | value |")
    w("|---|---|")
    for k, v in fi.items():
        w(f"| `{k}` | {v} |")
    w("")

    cp = m["candidate_population"]
    w("## 7. The eligible population, complete and ranked")
    w("")
    w(cp["note"])
    w("")
    w(f"**Frame digest (SHA-256):** `{cp['frame_digest_sha256']}`  ")
    w(f"*covers:* {cp['frame_digest_covers']}")
    w("")
    if cp["eligible_ranked"]:
        w("| rank | stratum | ticker | event | series | statistic | staleness (h) | bid | ask | selected |")
        w("|---:|---|---|---|---|---:|---:|---:|---:|---|")
        for r in cp["eligible_ranked"]:
            w(f"| {r['rank']} | {r['stratum'] or '—'} | `{r['ticker']}` | "
              f"`{r['event_ticker']}` | `{r['series']}` | {r['statistic']:,.2f} | "
              f"{r['staleness_hours']} | {r['yes_bid']} | {r['yes_ask']} | "
              f"{'**YES**' if r['selected'] else 'no'} |")
    else:
        w("*(empty — no market in the frame passed the eligibility gates)*")
    w("")

    if cp["top_ineligible_by_statistic"]:
        n = cp["ineligible_shown"]
        w(f"### 7b. The {n} highest-statistic REJECTED markets")
        w("")
        w("These are the markets a naive 'take the top 12 by volume' rule would "
          "have selected. Their rejection reasons are the finding.")
        w("")
        w("| ticker | event | statistic | staleness (h) | bid | ask | rejected because |")
        w("|---|---|---:|---:|---:|---:|---|")
        for r in cp["top_ineligible_by_statistic"][:60]:
            w(f"| `{r['ticker']}` | `{r['event_ticker']}` | {r['statistic']:,.2f} | "
              f"{r['staleness_hours']} | {r['yes_bid']} | {r['yes_ask']} | "
              f"{', '.join(r['ineligible_reasons'])} |")
        if cp["ineligible_shown"] > 60:
            w("")
            w(f"*(showing 60 of {cp['ineligible_shown']} recorded; "
              f"{cp['ineligible_total']:,} rejected in total — the full list is "
              f"in the JSON manifest)*")
        w("")

    w("## 8. The selected universe")
    w("")
    any_selected = any(s["members"] for s in m["universe"])
    if any_selected:
        w("| stratum | ticker | event | series | structure | statistic | rank |")
        w("|---|---|---|---|---|---:|---:|")
        for s in m["universe"]:
            for r in s["members"]:
                w(f"| **{s['stratum']}** | `{r['ticker']}` | `{r['event_ticker']}` | "
                  f"`{r['series']}` | {r['strike_type']} | {r['statistic']:,.2f} | "
                  f"{r['rank']} |")
        w("")
        us = m["universe_structures"]
        w(f"**Distinct events spanned:** {len(us['distinct_events'])} — "
          f"{', '.join('`%s`' % e for e in us['distinct_events'])}")
        w("")
        w(f"**Distinct series spanned:** {len(us['distinct_series'])} — "
          f"{', '.join('`%s`' % e for e in us['distinct_series'])}")
        w("")
        w(f"**Distinct contract structures (`strike_type`):** "
          f"{len(us['distinct_strike_types'])} — "
          f"{', '.join('`%s`' % e for e in us['distinct_strike_types'])}")
        w("")
        w("### Stratum boundaries")
        w("")
        w("| boundary | upper stratum min | lower stratum max | ratio |")
        w("|---|---:|---:|---:|")
        for key, v in m["strata_ranges"].items():
            if key.endswith("_range"):
                continue
            ratio = v["ratio"]
            w(f"| {key} | {v['upper_stratum_min']:,.2f} | {v['lower_stratum_max']:,.2f} | "
              f"{'—' if ratio is None else f'{ratio:.2f}x'} |")
        w("")
    else:
        w("**NO UNIVERSE WAS SELECTED.** See the refusal reasons above. This "
          "manifest authorizes no capture session.")
        w("")

    w("## 9. Representativeness — read this before generalising anything")
    w("")
    w(f"> {m['representativeness']}")
    w("")
    return "\n".join(out) + "\n"


# --- the venue snapshot ----------------------------------------------------------

async def snapshot_and_build(
    *,
    environment: str = "demo",
    mve_filter: str | None = "exclude",
    page_delay_seconds: float = 0.3,
    eligibility: EligibilityPolicy | None = None,
    selection: SelectionPolicy | None = None,
    progress=None,
) -> tuple[dict, list[dict]]:
    """Take the activity snapshot and build the manifest. Returns (manifest, frame).

    The credential is deliberately NOT loaded. Kalshi's market-data routes are
    public on both environments (confirmed credential-free on 2026-08-07,
    `docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md` section 14b), so this tool
    needs no key to do its job — which makes 'never copy the credential off the
    host, never print it, never write it to a manifest' true by construction
    rather than by discipline. There is no code path from here to
    `app/realtime/auth.py`.
    """
    # Imported here rather than at module scope so the pure decision logic above
    # stays importable — and unit-testable — without httpx or venue constants.
    from app.adapters.kalshi import KalshiRestAdapter
    from app.realtime.kalshi import ENVIRONMENTS, REST_HOSTS

    if environment not in ENVIRONMENTS:
        raise ManifestError(
            f"{environment!r} is not a known environment; expected one of "
            f"{list(ENVIRONMENTS)}")
    host = REST_HOSTS[environment]

    started_at = utc_now()
    adapter = KalshiRestAdapter(base_url=host)
    frame, pages = await adapter.fetch_open_markets_raw(
        mve_filter=mve_filter,
        page_delay_seconds=page_delay_seconds,
        progress=progress,
    )
    completed_at = utc_now()

    snapshot = SnapshotWindow(
        started_at=started_at,
        completed_at=completed_at,
        pages=pages,
        environment=environment,
        host=host,
        request_params={
            "route": "GET /markets",
            "status": "open",
            "limit": 200,
            "mve_filter": mve_filter,
            "paginated_to_exhaustion": True,
        },
    )
    manifest = build_manifest(
        frame, snapshot=snapshot, eligibility=eligibility, selection=selection)
    return manifest, frame
