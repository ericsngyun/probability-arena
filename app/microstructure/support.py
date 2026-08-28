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

import hashlib
import json
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


def support_ledger(rows: list[dict], *,
                   expected_sessions: list[str] | None = None) -> dict:
    """Per session x horizon evaluable support. Counts only.

    `expected_sessions` makes a ZERO-ROW session visible. Without it, a session
    that produced no rows simply does not appear -- indistinguishable from one
    that was never processed. S04 vanished from this ledger exactly that way,
    and the header then read "5 sessions" for a six-session corpus.
    """
    masks = [presence_of(r) for r in rows]        # values discarded here
    seen = {m.session_id for m in masks}
    sessions = sorted(seen | set(expected_sessions or ()))
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
        "sessions_expected": len(sessions),
        "sessions_with_zero_rows": sorted(set(sessions) - seen),
        "sessions": out,
        "tranche_totals_by_horizon": totals,
    }


# ---------------------------------------------------------------------------
# Metadata provenance (repair, 2026-08-25)
#
# A demonstrated measurement defect, not a hypothetical one. Pairing S01 with
# another session's candidate-metadata file produced **zero rows and no error**:
# the market keys simply did not match the tape, every row was skipped, and the
# ledger reported two sessions where three were expected. Nothing intrinsically
# said which session had been mis-paired.
#
# That is the doctrine-7 shape -- a silent empty result that looks like a valid
# measurement of nothing. The fix binds the metadata to the session it came
# from and REFUSES on a mismatch.
# ---------------------------------------------------------------------------


class MetadataProvenanceMismatch(RuntimeError):
    """The supplied metadata does not belong to this session."""


def candidate_universe_hash(tickers) -> str:
    """Order-independent digest of the subscribed set."""
    payload = "\n".join(sorted(tickers))
    return hashlib.sha256(payload.encode()).hexdigest()


def candidate_metadata_hash(events: dict) -> str:
    """Digest of the full metadata mapping, including series and event times."""
    canon = json.dumps(
        {t: {"series": events[t]["series"],
             "occurrence_datetime": events[t]["occurrence_datetime"]}
         for t in sorted(events)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def assert_metadata_matches_session(session: dict, events: dict) -> dict:
    """Bind metadata to a capture. Raises rather than yielding nothing.

    Checked on the SUBSCRIBED SET, which the session records at capture time,
    so a plausible file from a different session of the same shape -- 24
    markets, same series, same day -- is still refused.
    """
    subscribed = list(session.get("subscribed") or [])
    if not subscribed:
        raise MetadataProvenanceMismatch(
            f"session {session.get('session_id')!r} records no subscribed set; "
            "provenance cannot be established and the ledger refuses to guess")
    want, got = set(subscribed), set(events)
    if want != got:
        missing, extra = sorted(want - got)[:3], sorted(got - want)[:3]
        raise MetadataProvenanceMismatch(
            f"METADATA_PROVENANCE_MISMATCH for session "
            f"{session.get('session_id')!r}: the supplied metadata describes "
            f"{len(got)} markets, the capture subscribed {len(want)}. "
            f"missing from metadata: {missing}; not subscribed: {extra}. "
            "This is refused rather than producing zero rows, which is what "
            "the mismatch previously did.")
    return {
        "session_id": session.get("session_id"),
        "candidate_universe_hash": candidate_universe_hash(subscribed),
        "candidate_metadata_hash": candidate_metadata_hash(events),
        "capture_commit": session.get("capture_commit"),
        "markets": len(subscribed),
    }
