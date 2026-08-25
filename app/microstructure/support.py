"""Blind evaluable-support ledger. Missingness only — never a value.

`operationally observable` does not imply `statistically evaluable`. S03 emitted
9,331 rows of which only ~25% carried a defined midpoint, because post-event
baseball books went one-sided. That is a power/completeness fact, and it is
safe to watch during a blind tranche because it answers exactly one question:

    will the frozen evaluator have enough mechanically usable observations?

and cannot answer:

    do those observations predict anything?

**The blindness is structural, not promised.** Every row is projected to a
PRESENCE MASK on arrival — a set of booleans saying which columns are defined —
and the numbers are discarded before any counting happens. Nothing downstream
holds a feature value or a label value, so no statistic of them can be formed
here even by mistake. A test asserts the module never compares, sums or returns
a value, and a mutation that tries to smuggle one out fails.

This adds a diagnostic. It changes **no floor**: the frozen ≥4,000 market-block
and ≥150 cluster thresholds are untouched, and the evaluator's own
`UNDERPOWERED` rule remains the only thing that decides power.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.microstructure import features as F
from app.microstructure.labels import HORIZONS_S


@dataclass(frozen=True)
class PresenceMask:
    """What was DEFINED on one row. Carries no magnitudes, by construction."""
    session_id: str
    cluster: tuple
    m0_complete: bool
    m1_complete: bool
    label_available: dict          # horizon -> bool

    def to_dict(self) -> dict:
        return asdict(self)


def presence_of(row: dict) -> PresenceMask:
    """Project a row to booleans. The ONLY place a row is touched.

    Values are read solely through `is None`; none is retained, compared or
    arithmetically combined.
    """
    block = {**row["m0"], **row["m1_flow"]}
    m0 = all(block.get(n) is not None for n in F.M0_FEATURES)
    m1 = m0 and all(block.get(n) is not None for n in F.M1_FLOW_FEATURES)
    labels = {h: bool(row["labels"][str(h)]["available"]) for h in HORIZONS_S}
    return PresenceMask(
        session_id=row["session_id"],
        cluster=(row["event_id"], row["ticker"]),
        m0_complete=m0, m1_complete=m1, label_available=labels)


def support_ledger(rows: list[dict]) -> dict:
    """Per session x horizon evaluable support. Counts only."""
    masks = [presence_of(r) for r in rows]        # values discarded here
    sessions = sorted({m.session_id for m in masks})
    out = {}
    for sid in sessions:
        sm = [m for m in masks if m.session_id == sid]
        per_h = {}
        for h in HORIZONS_S:
            lab = [m for m in sm if m.label_available[h]]
            j0 = [m for m in lab if m.m0_complete]
            j1 = [m for m in lab if m.m1_complete]
            per_h[str(h)] = {
                "label_available_rows": len(lab),
                "joint_M0_evaluable_rows": len(j0),
                "joint_M1_evaluable_rows": len(j1),
                "clusters_with_an_evaluable_M0_row": len({m.cluster for m in j0}),
                "clusters_with_an_evaluable_M1_row": len({m.cluster for m in j1}),
            }
        out[sid] = {
            "rows_emitted": len(sm),
            "M0_complete_rows": sum(1 for m in sm if m.m0_complete),
            "M1_complete_rows": sum(1 for m in sm if m.m1_complete),
            "clusters": len({m.cluster for m in sm}),
            "by_horizon": per_h,
        }
    totals = {}
    for h in HORIZONS_S:
        totals[str(h)] = {
            k: sum(out[s]["by_horizon"][str(h)][k] for s in sessions)
            for k in ("label_available_rows", "joint_M0_evaluable_rows",
                      "joint_M1_evaluable_rows")
        }
        totals[str(h)]["clusters_with_an_evaluable_M1_row"] = len({
            m.cluster for m in masks
            if m.label_available[h] and m.m1_complete})
    return {
        "note": "missingness only; no feature or label VALUE is read here",
        "changes_no_floor": True,
        "sessions": out,
        "tranche_totals_by_horizon": totals,
    }
