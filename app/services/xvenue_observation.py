"""XVENUE-OBS-001: read-only cross-venue observation-window report.

Answers one operator question after a targeted Polymarket scan + match pass:
*did this window produce clean comparable markets between Kalshi and Polymarket,
and if not, why not?* It composes ALREADY-PERSISTED rows — the latest Polymarket
scan run (POLY-COVERAGE-001 provenance) and the latest cross-venue observation
run (POLY-002/POLY-PRECISION-001 candidates) — into a single windowed view for
human review. Derived on demand; nothing persisted, **no external call**, no
timer, no new match label.

A "clean" comparable is a `comparable_market_candidate` row that carries no
review flag (`large_observed_difference_requires_review`) — i.e. the matcher
found the same proposition on both venues and the measured gap is not itself
evidence that the match is wrong. The overlap assessment says whether the window
supplied enough same-proposition markets to be worth re-observing after the next
scan — an observation-coverage verdict for humans, NEVER a signal.

Hard boundary (docs/SAFETY_BOUNDARIES.md): observation reporting only. No EV, no
arbitrage/arb label, no trade recommendation, no paper trading, no sizing, no
orders, no wallets/private keys, no signing, no swaps, no execution, no autonomy.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CrossVenueMarketCandidate,
    CrossVenueObservationRun,
    PolymarketScoutRun,
)
from app.services.cross_venue import LABEL_COMPARABLE, LABEL_UNRESOLVED, REASON_LARGE_DIFFERENCE

logger = logging.getLogger(__name__)

OBSERVATION_NOTE = (
    "Read-only cross-venue observation-window report. It says whether a scan "
    "window produced CLEAN comparable markets (same proposition on both venues, "
    "no review flag) and why candidates fell short — coverage intelligence for "
    "human review. NOT arbitrage, NOT EV, NOT a trade candidate, NOT a "
    "recommendation, NOT a side/size/action. No orders, wallets, keys, swaps, "
    "signing, or execution."
)

# Overlap assessments (observation-coverage verdicts; never signals):
ASSESS_NO_SCAN = "no_scan_data"                       # no Polymarket scan persisted
ASSESS_NO_MATCH_RUN = "no_match_run"                  # scan exists, matcher not run
ASSESS_INSUFFICIENT = "insufficient_overlap"          # few/no candidates at all
ASSESS_NO_CLEAN_COMPARABLE = "overlap_no_clean_comparable"  # candidates, but no unflagged comparable
ASSESS_CLEAN_COMPARABLE = "clean_comparable_present"  # >=1 unflagged comparable

# below this many candidates the venues barely met at all in this window
MIN_CANDIDATES_FOR_OVERLAP = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _has_flag(candidate: CrossVenueMarketCandidate, flag: str) -> bool:
    return any(flag in str(r) for r in (candidate.mismatch_reasons or []))


def _candidate_row(c: CrossVenueMarketCandidate) -> dict:
    return {
        "kalshi_ticker": c.kalshi_ticker,
        "polymarket_market_id": c.polymarket_market_id,
        "domain": c.domain,
        "match_label": c.match_label,
        "match_confidence": c.match_confidence,
        "kalshi_midpoint": c.kalshi_midpoint,
        "polymarket_midpoint": c.polymarket_midpoint,
        "observed_difference": c.observed_difference,
        "title": (c.event_title_normalized or "")[:100],
        "mismatch_reasons": list(c.mismatch_reasons or []),
    }


@dataclass
class XVenueObservationReport:
    note: str
    # scan window (latest completed Polymarket scan)
    scan_run_id: int | None = None
    scan_started_at: str | None = None
    scan_finished_at: str | None = None
    scan_mode: str | None = None
    scan_queries: list = field(default_factory=list)
    scan_markets_seen: int = 0
    # match pass (latest completed cross-venue run)
    match_run_id: int | None = None
    match_started_at: str | None = None
    match_ran_after_scan: bool = False
    kalshi_considered: int = 0
    polymarket_considered: int = 0
    candidates: int = 0
    by_label: dict = field(default_factory=dict)
    by_domain: dict = field(default_factory=dict)
    mismatch_reasons: dict = field(default_factory=dict)
    # comparability quality
    comparable_total: int = 0
    comparable_clean: int = 0
    comparable_flagged: int = 0
    side_uncertain: int = 0
    unresolved: int = 0
    clean_candidates: list = field(default_factory=list)
    flagged_candidates: list = field(default_factory=list)
    # window verdict (observation coverage, never a signal)
    overlap_assessment: str = ASSESS_NO_SCAN
    assessment_detail: str = ""


class XVenueObservationReportService:
    """Composes the latest scan + match runs into one window view. Read-only."""

    def build(self, session: Session, top: int = 10) -> XVenueObservationReport:
        r = XVenueObservationReport(note=OBSERVATION_NOTE)

        scan = session.execute(
            select(PolymarketScoutRun)
            .where(PolymarketScoutRun.status == "ok")
            .order_by(PolymarketScoutRun.id.desc())
        ).scalars().first()
        if scan is not None:
            r.scan_run_id = scan.id
            r.scan_started_at = scan.started_at.isoformat() if scan.started_at else None
            r.scan_finished_at = scan.finished_at.isoformat() if scan.finished_at else None
            r.scan_mode = scan.scan_mode
            r.scan_queries = list(scan.queries_used or [])
            r.scan_markets_seen = scan.markets_seen or 0

        match = session.execute(
            select(CrossVenueObservationRun)
            .where(CrossVenueObservationRun.status == "ok")
            .order_by(CrossVenueObservationRun.id.desc())
        ).scalars().first()
        if match is not None:
            r.match_run_id = match.id
            r.match_started_at = match.started_at.isoformat() if match.started_at else None
            r.kalshi_considered = match.kalshi_markets_considered or 0
            r.polymarket_considered = match.polymarket_markets_considered or 0
            r.candidates = match.candidates_created or 0
            if scan is not None and scan.started_at and match.started_at:
                r.match_ran_after_scan = match.started_at >= scan.started_at

            cands = session.execute(
                select(CrossVenueMarketCandidate)
                .where(CrossVenueMarketCandidate.run_id == match.id)
            ).scalars().all()
            for c in cands:
                r.by_label[c.match_label] = r.by_label.get(c.match_label, 0) + 1
                dom = c.domain or "other"
                r.by_domain[dom] = r.by_domain.get(dom, 0) + 1
                for reason in (c.mismatch_reasons or []):
                    key = str(reason).split("=")[0]
                    r.mismatch_reasons[key] = r.mismatch_reasons.get(key, 0) + 1
                if _has_flag(c, "side_uncertain"):
                    r.side_uncertain += 1

            r.unresolved = r.by_label.get(LABEL_UNRESOLVED, 0)
            comparables = [c for c in cands if c.match_label == LABEL_COMPARABLE]
            clean = [c for c in comparables if not _has_flag(c, REASON_LARGE_DIFFERENCE)]
            flagged = [c for c in comparables if _has_flag(c, REASON_LARGE_DIFFERENCE)]
            r.comparable_total = len(comparables)
            r.comparable_clean = len(clean)
            r.comparable_flagged = len(flagged)
            r.clean_candidates = [
                _candidate_row(c) for c in
                sorted(clean, key=lambda c: -(c.match_confidence or 0))[:top]
            ]
            r.flagged_candidates = [
                _candidate_row(c) for c in
                sorted(flagged, key=lambda c: -(c.match_confidence or 0))[:top]
            ]
            r.mismatch_reasons = dict(
                sorted(r.mismatch_reasons.items(), key=lambda kv: -kv[1])
            )

        r.overlap_assessment, r.assessment_detail = self._assess(r, scan, match)
        return r

    @staticmethod
    def _assess(r: XVenueObservationReport, scan, match) -> tuple[str, str]:
        """Observation-coverage verdict for the window. Language is deliberately
        about COVERAGE (was there enough same-proposition supply to observe?) —
        never about value, action, or opportunity."""
        if scan is None:
            return ASSESS_NO_SCAN, (
                "No completed Polymarket scan persisted — run polymarket-scan-once "
                "--targeted first."
            )
        if match is None:
            return ASSESS_NO_MATCH_RUN, (
                "A scan exists but no completed cross-venue match run — run "
                "cross-venue-match-once."
            )
        detail_suffix = (
            "" if r.match_ran_after_scan
            else " NOTE: the latest match run PRECEDES the latest scan — rerun "
                 "cross-venue-match-once to match against the new sample."
        )
        if r.comparable_clean > 0:
            return ASSESS_CLEAN_COMPARABLE, (
                f"{r.comparable_clean} clean comparable market(s) (no review flag) — "
                f"this window has same-proposition supply worth re-observing after "
                f"the next scan." + detail_suffix
            )
        if r.candidates >= MIN_CANDIDATES_FOR_OVERLAP:
            return ASSESS_NO_CLEAN_COMPARABLE, (
                f"{r.candidates} candidates but no clean comparable "
                f"({r.comparable_flagged} comparable row(s) carry the "
                f"large-difference review flag; {r.side_uncertain} side-uncertain). "
                f"The venues meet in this window but list different market types "
                f"or unalignable sides — see mismatch_reasons." + detail_suffix
            )
        return ASSESS_INSUFFICIENT, (
            f"Only {r.candidates} candidate(s) — the venues barely met in this "
            f"window. Widen the scan (more --limit, --targeted, or a resolution "
            f"window matching the slate) before drawing coverage conclusions."
            + detail_suffix
        )
