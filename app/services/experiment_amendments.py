"""PROSPECTIVE-EXPERIMENT-REGISTRY-002C — governed re-pinning of references.

002B pinned `experiment_results.py` as one of its own metric references, so any
edit to the evaluator made drift material for every registered experiment and
`_derive_verdict` returned `invalidated_protocol_deviation` forever. Fail-closed
was right. **Permanently** closed, with no way back, was not: the next bug fix —
including fixing anything found in a review — would silently destroy every
experiment in flight, and the only escape would be editing a registered manifest,
which is the one thing the registry exists to prevent.

So references are never overwritten. An amendment is an append-only record
carrying the old digest, the new digest, a **typed** reason, the experiments it
covers, a review approval, and an explicit judgement about whether collection
before and after remains comparable.

The hard rule is at the bottom of `apply_amendment`: an amendment may only
declare `collection_comparable=True` for reasons that cannot change a number.
A semantic change to how a metric is computed does not get to be waved through
as a refactor — it forces a new experiment version, because the observations
either side of it are not measuring the same thing.

Provider-free, no database, no EV/price/order/execution surface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

AMENDMENTS_FILENAME = "amendments.jsonl"
AMENDMENT_HEAD_FILENAME = "amendment-head.json"
MAX_AMENDMENTS = 200

# --- typed amendment reasons ----------------------------------------------------
# The split that matters is not "big change / small change" but "could this move
# a number?". Only the first group can leave prior collection comparable.
REASON_DOCUMENTATION = "documentation_only"
REASON_COMMENT_OR_TYPING = "comment_or_typing_only"
REASON_NONSEMANTIC_REFACTOR = "nonsemantic_refactor"
REASON_DEFECT_FIX_SEMANTIC = "defect_fix_semantic"
REASON_METRIC_DEFINITION_CHANGE = "metric_definition_change"
REASON_BASELINE_DEFINITION_CHANGE = "baseline_definition_change"
REASON_POPULATION_LOGIC_CHANGE = "population_logic_change"
REASON_CI_POLICY_CHANGE = "ci_policy_change"

ALL_REASONS = (
    REASON_DOCUMENTATION, REASON_COMMENT_OR_TYPING, REASON_NONSEMANTIC_REFACTOR,
    REASON_DEFECT_FIX_SEMANTIC, REASON_METRIC_DEFINITION_CHANGE,
    REASON_BASELINE_DEFINITION_CHANGE, REASON_POPULATION_LOGIC_CHANGE,
    REASON_CI_POLICY_CHANGE,
)

# Reasons that CANNOT move a number, and therefore may declare prior collection
# comparable. Everything else forces a new experiment version.
NON_SEMANTIC_REASONS = (
    REASON_DOCUMENTATION, REASON_COMMENT_OR_TYPING, REASON_NONSEMANTIC_REFACTOR,
)


class AmendmentError(ValueError):
    """An amendment that must not be recorded."""


@dataclass
class Amendment:
    amendment_id: int
    at: str
    reason: str
    detail: str
    reviewer: str
    review_reference: str
    reference_kind: str            # population_references | metric_references
    changed_files: dict = field(default_factory=dict)   # file -> {old, new}
    old_snapshot_digest: str | None = None
    new_snapshot_digest: str | None = None
    affected_experiments: list = field(default_factory=list)
    collection_comparable: bool = False
    requires_new_experiment_version: bool = True
    prev: str | None = None
    seq: int = 0

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _hash(obj) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def amendments_dir(base: Path | None = None) -> Path:
    return (base or Path.cwd()) / "experiments"


def _paths(base: Path | None = None) -> tuple:
    d = amendments_dir(base)
    return d / AMENDMENTS_FILENAME, d / AMENDMENT_HEAD_FILENAME


def read_amendments(base: Path | None = None) -> list:
    log, _ = _paths(base)
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]


def read_amendment_head(base: Path | None = None) -> dict | None:
    _, head = _paths(base)
    if not head.exists():
        return None
    return json.loads(head.read_text())


def verify_amendment_chain(base: Path | None = None) -> dict:
    """Same hash-chain-plus-pinned-head discipline as events and results."""
    entries = read_amendments(base)
    head = read_amendment_head(base)
    if not entries and head is None:
        return {"intact": True, "length": 0, "reason": None, "empty": True}
    prev = None
    for i, e in enumerate(entries):
        if e.get("seq") != i or e.get("prev") != prev:
            return {"intact": False, "length": len(entries),
                    "reason": f"amendment chain broken at {i}"}
        prev = _hash(e)
    if head is None:
        return {"intact": False, "length": len(entries),
                "reason": "amendment head missing"}
    if head.get("amendment_count") != len(entries) or \
            head.get("terminal_amendment_hash") != prev:
        return {"intact": False, "length": len(entries),
                "reason": "amendment head does not match the log"}
    return {"intact": True, "length": len(entries), "reason": None,
            "empty": False}


def classify_change(reason: str) -> dict:
    """Whether a reason may leave prior collection comparable."""
    if reason not in ALL_REASONS:
        raise AmendmentError(
            f"reason {reason!r} is not one of the typed reasons {ALL_REASONS}. "
            "Free-form justification is how a semantic change gets waved "
            "through as a refactor.")
    non_semantic = reason in NON_SEMANTIC_REASONS
    return {
        "reason": reason,
        "semantic": not non_semantic,
        "may_declare_comparable": non_semantic,
        "requires_new_experiment_version": not non_semantic,
    }


def apply_amendment(
    *, reason: str, detail: str, reviewer: str, review_reference: str,
    reference_kind: str, old_snapshot: dict, new_snapshot: dict,
    affected_experiments: list, collection_comparable: bool = False,
    base: Path | None = None, confirm: bool = False,
    now: datetime | None = None,
) -> dict:
    """Record one append-only amendment. Never overwrites a reference.

    Registered manifests are NOT touched. The amendment sits beside them and an
    evaluator consults it, so the immutable declaration stays immutable and the
    reason the references moved is on the record rather than inferred from a
    diff.
    """
    now = now or datetime.now(timezone.utc)
    cls = classify_change(reason)

    if not detail or len(detail) > 2000:
        raise AmendmentError("detail is required and must be under 2000 characters")
    if not reviewer or not review_reference:
        raise AmendmentError(
            "reviewer and review_reference are required: an amendment that "
            "nobody approved is an unreviewed change to what a result means")
    if reference_kind not in ("population_references", "metric_references"):
        raise AmendmentError(f"unknown reference_kind {reference_kind!r}")
    if not affected_experiments:
        raise AmendmentError("affected_experiments must list what this covers")

    if collection_comparable and not cls["may_declare_comparable"]:
        raise AmendmentError(
            f"reason {reason!r} is a semantic change, so observations collected "
            "before and after it are not measuring the same thing. It cannot "
            "declare collection comparable; it requires a new experiment "
            "version.")

    old_files = (old_snapshot or {}).get(
        "metric_code_digests" if reference_kind == "metric_references"
        else "population_logic_digests") or {}
    new_files = (new_snapshot or {}).get(
        "metric_code_digests" if reference_kind == "metric_references"
        else "population_logic_digests") or {}
    changed = {f: {"old": old_files.get(f), "new": new_files.get(f)}
               for f in sorted(set(old_files) | set(new_files))
               if old_files.get(f) != new_files.get(f)}
    if not changed:
        raise AmendmentError(
            "no reference digests actually changed; an amendment records a real "
            "movement, not an intention")

    entries = read_amendments(base)
    if len(entries) >= MAX_AMENDMENTS:
        raise AmendmentError(f"amendment log exceeds {MAX_AMENDMENTS}")
    prev = _hash(entries[-1]) if entries else None

    record = Amendment(
        amendment_id=len(entries) + 1, at=now.isoformat(), reason=reason,
        detail=detail, reviewer=reviewer, review_reference=review_reference,
        reference_kind=reference_kind, changed_files=changed,
        old_snapshot_digest=_hash(old_snapshot or {}),
        new_snapshot_digest=_hash(new_snapshot or {}),
        affected_experiments=sorted(affected_experiments),
        collection_comparable=bool(collection_comparable),
        requires_new_experiment_version=cls["requires_new_experiment_version"],
        prev=prev, seq=len(entries),
    ).to_dict()

    out = {"mode": "confirmed" if confirm else "dry_run", "persisted": False,
           "amendment": record, "classification": cls}
    if not confirm:
        return out

    d = amendments_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    log, head_path = _paths(base)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(_canon(record) + "\n")
    head_path.write_text(json.dumps({
        "amendment_count": len(entries) + 1,
        "terminal_amendment_hash": _hash(record),
        "updated_at": now.isoformat(),
    }, indent=2, sort_keys=True) + "\n")
    out["persisted"] = True
    return out


def amendment_for(experiment_id: str, reference_kind: str,
                  new_snapshot_digest: str, base: Path | None = None) -> dict | None:
    """Is this experiment's reference movement covered by an amendment?

    Returns the amendment if one covers the experiment AND lands on exactly the
    current snapshot. A blanket amendment cannot pre-authorize an arbitrary
    future change: it names the digest it moved to.
    """
    for e in reversed(read_amendments(base)):
        if e.get("reference_kind") != reference_kind:
            continue
        if experiment_id not in (e.get("affected_experiments") or []):
            continue
        if e.get("new_snapshot_digest") == new_snapshot_digest:
            return e
    return None
