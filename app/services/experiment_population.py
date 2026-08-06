"""PROSPECTIVE-EXPERIMENT-REGISTRY-002A — population reconstruction and drift.

Reconstructs an experiment's membership from its registered typed predicates and
local forecast data. Provider-free, write-free, and — the property that matters —
**it cannot see an outcome or a score**, because the only thing a predicate is
ever handed is a `ForecastFacts` struct built from forecast-time columns.

`scored_current` is deliberately not a membership input. Whether a forecast has
been scored yet is an *evaluation status*; letting it decide membership makes the
population a function of the outcome pipeline's progress, which is how a cohort
quietly becomes "the rows that happened to work out".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.models import (
    MarketForecastRecord,
    MarketResearchPacket,
    MarketResolutionAssessment,
)
from app.services.experiment_predicates import (
    FIELD_REGISTRY,
    FIELD_REGISTRY_VERSION,
    PREDICATE_SCHEMA_VERSION,
    ForecastFacts,
    canonicalize_population,
    declared_window_end,
    ensure_registration_floor,
    evaluate,
    field_registry_digest,
    population_digest,
)

DISCLAIMER = (
    "Population reconstruction only — membership is decided from forecast-time "
    "fields. Never advice, EV, a side, a size, an order, a recommendation, a "
    "wallet, or a trade action."
)

# --- drift classes --------------------------------------------------------------
DRIFT_NONE = "none"
DRIFT_DOC = "non_material_documentation"
DRIFT_PREDICATE_SCHEMA = "material_predicate_schema"
DRIFT_FIELD_REGISTRY = "material_field_registry"
DRIFT_POPULATION_LOGIC = "material_population_logic"
DRIFT_UNKNOWN = "unknown"

MATERIAL_DRIFT = (DRIFT_PREDICATE_SCHEMA, DRIFT_FIELD_REGISTRY,
                  DRIFT_POPULATION_LOGIC, DRIFT_UNKNOWN)

POPULATION_LOGIC_FILES = (
    "app/services/experiment_predicates.py",
    "app/services/experiment_population.py",
)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _file_digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def population_reference_snapshot(repo_root=None) -> dict:
    """References pinned at draft validation and at registration."""
    from pathlib import Path

    from app.services.experiment_registry import repo_root_default

    root = Path(repo_root) if repo_root else repo_root_default()
    missing = [f for f in POPULATION_LOGIC_FILES if not (root / f).exists()]
    if missing:
        from app.services.experiment_registry import ManifestError

        raise ManifestError(
            f"cannot pin population logic: missing {missing}. Refusing to record "
            "null references, which would make drift permanently undetectable.")
    return {
        "predicate_schema_version": PREDICATE_SCHEMA_VERSION,
        "field_registry_version": FIELD_REGISTRY_VERSION,
        "field_registry_digest": field_registry_digest(),
        "population_logic_digests": {
            f: _file_digest(root / f) for f in POPULATION_LOGIC_FILES},
        "forecast_model_reference": "MarketForecastRecord@alembic_0027",
    }


def classify_population_drift(recorded: dict | None, repo_root=None) -> dict:
    """Compare pinned references against the current code.

    An absent or unreadable reference is `unknown`, never `none`. Reporting a
    clean bill of health from missing evidence is the specific failure this
    milestone series has already hit once, in the B3 pinning that silently
    recorded nulls.
    """
    if not recorded:
        return {"classification": DRIFT_UNKNOWN, "material": True,
                "changed": [], "detail": "no population references recorded"}
    try:
        current = population_reference_snapshot(repo_root)
    except Exception as exc:
        return {"classification": DRIFT_UNKNOWN, "material": True, "changed": [],
                "detail": f"current references unresolvable: {type(exc).__name__}"}

    if recorded.get("predicate_schema_version") != current["predicate_schema_version"]:
        return {"classification": DRIFT_PREDICATE_SCHEMA, "material": True,
                "changed": ["predicate_schema_version"],
                "detail": "the predicate language itself changed"}
    if recorded.get("field_registry_digest") != current["field_registry_digest"]:
        return {"classification": DRIFT_FIELD_REGISTRY, "material": True,
                "changed": ["field_registry_digest"],
                "detail": "an allowed field, operator or type changed"}
    rec_logic = recorded.get("population_logic_digests") or {}
    if not rec_logic:
        return {"classification": DRIFT_UNKNOWN, "material": True, "changed": [],
                "detail": "no population-logic digests recorded"}
    changed = [f for f, d in rec_logic.items()
               if current["population_logic_digests"].get(f) != d]
    if changed:
        return {"classification": DRIFT_POPULATION_LOGIC, "material": True,
                "changed": changed,
                "detail": "membership could be computed differently now"}
    return {"classification": DRIFT_NONE, "material": False, "changed": [],
            "detail": "population references match"}


@dataclass
class PopulationResult:
    experiment_id: str
    manifest_digest: str | None
    predicate_schema_version: int
    predicate_digest: str
    registered_at: str | None
    declared_end: str | None
    population_count: int = 0
    eligible_count: int = 0
    pre_registration_excluded: int = 0
    post_end_excluded: int = 0
    declared_rule_exclusions: dict = field(default_factory=dict)
    unexpected_missing_fields: dict = field(default_factory=dict)
    invalid_field_values: list = field(default_factory=list)
    membership_digest: str = ""
    examples: list = field(default_factory=list)
    external_calls: int = 0
    persisted: bool = False
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict:
        return asdict(self)


def _load_facts(session: Session) -> list:
    """Bulk-load the forecast-time projection. Reads no outcome and no score."""
    forecasts = list(session.execute(
        select(MarketForecastRecord).options(load_only(
            MarketForecastRecord.market_ticker,
            MarketForecastRecord.forecaster_name,
            MarketForecastRecord.forecaster_version,
            MarketForecastRecord.evidence_depth,
            MarketForecastRecord.forecast_risk,
            MarketForecastRecord.research_packet_id,
            MarketForecastRecord.resolution_assessment_id,
            MarketForecastRecord.created_at,
        )).order_by(MarketForecastRecord.id)
    ).scalars().all())

    packet_ids = {f.research_packet_id for f in forecasts if f.research_packet_id}
    res_ids = {f.resolution_assessment_id for f in forecasts
               if f.resolution_assessment_id}
    packets = {
        pid: (dom, comp, risk) for pid, dom, comp, risk in session.execute(
            select(MarketResearchPacket.id, MarketResearchPacket.domain,
                   MarketResearchPacket.research_completeness_score,
                   MarketResearchPacket.research_risk)
            .where(MarketResearchPacket.id.in_(packet_ids or {0}))).all()
    }
    assessments = {
        rid: (rrisk, trade) for rid, rrisk, trade in session.execute(
            select(MarketResolutionAssessment.id,
                   MarketResolutionAssessment.resolution_risk,
                   MarketResolutionAssessment.tradeability)
            .where(MarketResolutionAssessment.id.in_(res_ids or {0}))).all()
    }

    out = []
    for f in forecasts:
        dom, comp, rrisk = packets.get(f.research_packet_id, (None, None, None))
        resrisk, trade = assessments.get(f.resolution_assessment_id, (None, None))
        out.append(ForecastFacts(forecast_id=f.id, values={
            "domain": dom,
            "forecast_created_at": _aware(f.created_at),
            "forecaster": f.forecaster_name,
            "forecaster_version": f.forecaster_version,
            "evidence_depth": f.evidence_depth,
            "forecast_risk": f.forecast_risk,
            "research_completeness": comp,
            "research_risk": rrisk,
            "resolution_risk": resrisk,
            "tradeability_at_forecast_time": trade,
            "market_ticker": f.market_ticker,
            "has_research_packet": f.research_packet_id is not None,
            "has_resolution_assessment": f.resolution_assessment_id is not None,
        }))
    return out


def reconstruct_population(
    session: Session, *, experiment_id: str, population: dict,
    registered_at: datetime, manifest_digest: str | None = None,
    examples: int = 5,
) -> PopulationResult:
    """Deterministic membership from typed predicates. Writes nothing.

    There is deliberately NO `declared_end` argument. It used to be a caller
    parameter absent from the digest, which meant whoever ran the evaluation
    chose the cohort's end date — after seeing results — with nothing registered
    to contradict them. That is optional stopping, and it is the exact mirror of
    the REGISTRY-001 optional-`start_time` bug. The ceiling now comes from the
    registered `population.window_end`, which is inside the canonical digest.
    """
    canon = ensure_registration_floor(canonicalize_population(population))
    registered_at = _aware(registered_at)
    declared_end = declared_window_end(canon)

    facts = _load_facts(session)
    members: list[int] = []
    pre_reg = post_end = 0
    rule_exclusions: dict[str, int] = {}
    missing_fields: dict[str, int] = {}

    referenced = {p["field"] for p in canon.get("all", []) + canon.get("none", [])}
    for row in facts:
        for fname in referenced:
            if fname in FIELD_REGISTRY and row.values.get(fname) is None:
                missing_fields[fname] = missing_fields.get(fname, 0) + 1

        created = row.values.get("forecast_created_at")
        if created is None or created < registered_at:
            pre_reg += 1
            continue
        if declared_end is not None and created >= declared_end:
            post_end += 1
            continue

        ok, reasons = evaluate(canon, row, registered_at=registered_at,
                               declared_end=declared_end)
        if ok:
            members.append(row.forecast_id)
        else:
            for r in reasons:
                rule_exclusions[r] = rule_exclusions.get(r, 0) + 1

    members.sort()
    digest = hashlib.sha256(json.dumps(
        {"experiment_id": experiment_id, "members": members},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    return PopulationResult(
        experiment_id=experiment_id, manifest_digest=manifest_digest,
        predicate_schema_version=PREDICATE_SCHEMA_VERSION,
        predicate_digest=population_digest(canon),
        registered_at=registered_at.isoformat(),
        declared_end=declared_end.isoformat() if declared_end else None,
        population_count=len(facts), eligible_count=len(members),
        pre_registration_excluded=pre_reg, post_end_excluded=post_end,
        declared_rule_exclusions=dict(sorted(rule_exclusions.items())),
        unexpected_missing_fields=dict(sorted(missing_fields.items())),
        membership_digest=digest, examples=members[:max(0, examples)],
    )
