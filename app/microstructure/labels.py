"""Future-mid labels. Nothing else.

**This module must never import `features`, and `features` must never import
this one.** That mutual exclusion is the structural reason a future price
cannot reach an M0/M1 column: the two live in disjoint halves of the import
graph and are joined only by `rows`, which owns the time discipline. A test
asserts the graph.

A label is `Δmid(t, t+h) = mid(t+h) − mid(t)` in probability units. It is
horizon-specific and it is either **available** or **`UNAVAILABLE`** — never
zero. A missing future price is not a zero return, and collapsing the two
would silently label every truncated window as "no move".
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

#: The frozen horizons (MARKET-MICROSTRUCTURE-EDGE-001 §3). 30 s is primary.
HORIZONS_S = (1, 5, 30, 300)
PRIMARY_HORIZON_S = 30

#: How far from the exact endpoint a mid may be taken. Frozen BEFORE any
#: confirmation data exists, so it can never be widened to rescue coverage.
#: Samples are on a 1 Hz grid, so a half-grid tolerance keeps the match
#: unambiguous -- at most one sample can satisfy it.
ENDPOINT_TOLERANCE_MS = 500

UNAVAILABLE = "UNAVAILABLE"

REASON_NO_BASE_MID = "no_mid_at_t"
REASON_NO_ENDPOINT = "no_publishable_mid_at_endpoint"
REASON_PAST_SESSION_END = "endpoint_past_session_end"


@dataclass(frozen=True)
class Label:
    horizon_s: int
    value: float | None
    available: bool
    reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _mid_at(mid_grid: dict, target_ms: float, tol_ms: int):
    """The published mid at `target_ms`, within tolerance. No forward-fill.

    Deliberately NOT "the last mid before the endpoint". Forward-filling a
    stale midpoint across an unpublishable interval changes the target from
    "the price moved" to "the price last seen moved", which is a different
    experiment.
    """
    exact = mid_grid.get(round(target_ms))
    if exact is not None:
        return exact
    best, best_gap = None, None
    for ms, mid in mid_grid.items():
        gap = abs(ms - target_ms)
        if gap <= tol_ms and (best_gap is None or gap < best_gap):
            best, best_gap = mid, gap
    return best


def compute_labels(*, t_ms: float, mid_at_t: float | None, mid_grid: dict,
                   session_end_ms: float,
                   tolerance_ms: int = ENDPOINT_TOLERANCE_MS) -> dict:
    """One `Label` per frozen horizon.

    `mid_grid` holds ONLY published mids, keyed by integer sample millisecond.
    A horizon whose endpoint has no published mid is `UNAVAILABLE` with a
    reason; the row survives, because the preregistration nowhere says a row
    must be discarded when one of four horizons cannot be computed.
    """
    labels = {}
    for h in HORIZONS_S:
        end_ms = t_ms + h * 1000
        if mid_at_t is None:
            labels[h] = Label(h, None, False, REASON_NO_BASE_MID)
            continue
        if end_ms > session_end_ms:
            labels[h] = Label(h, None, False, REASON_PAST_SESSION_END)
            continue
        endpoint = _mid_at(mid_grid, end_ms, tolerance_ms)
        if endpoint is None:
            labels[h] = Label(h, None, False, REASON_NO_ENDPOINT)
            continue
        labels[h] = Label(h, endpoint - mid_at_t, True, None)
    return labels


def coverage(rows) -> dict:
    """Per-horizon label availability across a set of built rows."""
    out = {}
    for h in HORIZONS_S:
        total = len(rows)
        got = sum(1 for r in rows if r["labels"][str(h)]["available"])
        out[h] = {"rows": total, "available": got,
                  "coverage": round(got / total, 4) if total else None}
    return out
