"""PROSPECTIVE-EXPERIMENT-REGISTRY-002B — registry-owned result evaluation.

The registry computes the result. The caller supplies an experiment id and,
optionally, non-authoritative prose. It cannot supply a sample count, a metric
value, a population, an end time or a verdict — because every one of those is a
place where a null result becomes a positive one, and a registry that accepts
them from the person who wants the answer is a filing cabinet, not a control.

Ordering is fixed and testable, and the order is the point:

    reconstruct membership   (forecast-time fields ONLY)
    → freeze membership digest
    → attach outcome/score state
    → compute maturity
    → compute metrics
    → derive verdict
    → append an immutable, hash-chained result

Outcomes are unreachable until membership is frozen, so a cohort cannot be
shaped by what it would score.

Metrics reuse the deployed canonical implementations — `calibration.brier_score`
and the reliability baselines — rather than restating the formulas here. A
second implementation is a second answer.

Research-only. No EV, market price, return, P&L, order, wallet, execution or
capital surface exists here, and none can be added without failing a test.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.models import ForecastScoreRecord, MarketForecastRecord, MarketOutcomeRecord
from app.services.calibration import STATUS_SCORED, _score_target, brier_score
from app.services.experiment_population import (
    classify_population_drift,
    population_reference_snapshot,
    reconstruct_population,
)
from app.services.experiment_predicates import (
    canonicalize_population,
    ensure_registration_floor,
    population_digest,
)
from app.services.experiment_registry import (
    EVALUATED,
    MATURED,
    ManifestError,
    canonical_json,
    experiment_dir,
    load_manifest,
    read_events,
    verify_event_chain,
    verify_immutability,
)

DISCLAIMER = (
    "Registry-owned research evaluation — measurement only. Never advice, EV, a "
    "price, a side, a size, an order, a recommendation, a wallet, or a trade "
    "action. A supportive verdict is not evidence of executable edge."
)

RESULTS_DIRNAME = "results"
RESULT_EVENTS_FILENAME = "result-events.jsonl"
RESULT_HEAD_FILENAME = "result-head.json"
MAX_RESULTS = 200

# --- verdicts (closed) ----------------------------------------------------------
SUPPORTS = "supports_hypothesis"
DOES_NOT_SUPPORT = "does_not_support_hypothesis"
INCONCLUSIVE_FLOOR = "inconclusive_sample_floor"
INVALID_PROTOCOL = "invalidated_protocol_deviation"
INVALID_DATA = "invalidated_data_quality"
STILL_COLLECTING = "still_collecting"

ALL_VERDICTS = (SUPPORTS, DOES_NOT_SUPPORT, INCONCLUSIVE_FLOOR,
                INVALID_PROTOCOL, INVALID_DATA, STILL_COLLECTING)
TERMINAL_FAVOURABLE = (SUPPORTS,)

# --- supported metrics (closed) -------------------------------------------------
METRIC_MEAN_BRIER = "mean_brier"
METRIC_BRIER_SKILL = "brier_skill_vs_base_rate"
SUPPORTED_PRIMARY_METRICS = (METRIC_MEAN_BRIER, METRIC_BRIER_SKILL)

BASELINE_BASE_RATE_BRIER = "base_rate_brier"
SUPPORTED_BASELINES = (BASELINE_BASE_RATE_BRIER,)

SECONDARY_ECE = "ece"
SECONDARY_MURPHY = "murphy_components"
SUPPORTED_SECONDARY = (SECONDARY_ECE, SECONDARY_MURPHY)

# --- stopping rules (closed) ----------------------------------------------------
STOP_FIXED_SAMPLE_AND_END = "fixed_sample_and_end"
SUPPORTED_STOPPING_RULES = (STOP_FIXED_SAMPLE_AND_END,)

# --- decision rules (closed, typed) ---------------------------------------------
DECISION_DELTA_GT_ZERO = "primary_metric_delta_gt_zero"
DECISION_DELTA_LT_ZERO = "primary_metric_delta_lt_zero"
DECISION_CI_LOWER_GT_ZERO = "ci_lower_bound_gt_zero"
# M8 — a negative POINT estimate cannot confirm persistent underperformance.
# `ci_upper_bound_lt_zero` is the falsification counterpart of
# `ci_lower_bound_gt_zero`: the whole interval must sit below zero.
DECISION_CI_UPPER_LT_ZERO = "ci_upper_bound_lt_zero"
SUPPORTED_DECISION_RULES = (DECISION_DELTA_GT_ZERO, DECISION_DELTA_LT_ZERO,
                            DECISION_CI_LOWER_GT_ZERO, DECISION_CI_UPPER_LT_ZERO)

# --- confidence interval policy (versioned, deterministic) ----------------------
CI_POLICY_VERSION = 1
CI_CLUSTER_BOOTSTRAP = "cluster_bootstrap_by_market_v1"
SUPPORTED_CI_POLICIES = (CI_CLUSTER_BOOTSTRAP,)
CI_BOOTSTRAP_SAMPLES = 2000
CI_SEED = 20260806          # fixed: an evaluator must not be able to reroll
CI_ALPHA = 0.05

# H3 — `now` exists for tests. A confirmed evaluation may not run on a clock the
# caller chose: with a `not_before` rule, passing a future `now` turned
# still_collecting into supports_hypothesis and persisted it under the spoofed
# timestamp. Confirmed runs must be near the real clock, and every record also
# carries a non-overridable `recorded_at`.
MAX_CLOCK_SKEW_SECONDS = 300

# Prevalence at or beyond this edge makes the base rate near-unbeatable and the
# skill metric uninformative. 5% covers the soccer artifact (2.9%).
DEGENERATE_PREVALENCE = 0.05

# M5 — operator prose is a NON-AUTHORITATIVE annotation. It cannot affect
# membership, metrics, stopping or the verdict, and nothing here reads it back.
# It is bounded and secret-scanned because it lands in a committed artifact —
# but bounding prose is hygiene, not semantic control, and pretending otherwise
# is how the old vocabulary blocklist earned its reputation.
MAX_NOTE_CHARS = 2000

# --- drift classes added here ---------------------------------------------------
DRIFT_METRIC_CODE = "material_metric_code"
DRIFT_BASELINE = "material_baseline"
DRIFT_FORECASTER = "material_forecaster"
DRIFT_STOPPING_RULE = "material_stopping_rule"

METRIC_CODE_FILES = (
    "app/services/calibration.py",
    "app/services/forecast_reliability.py",
    "app/services/experiment_results.py",
)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metric_reference_snapshot(repo_root=None) -> dict:
    from app.services.experiment_registry import repo_root_default

    root = Path(repo_root) if repo_root else repo_root_default()
    missing = [f for f in METRIC_CODE_FILES if not (root / f).exists()]
    if missing:
        raise ManifestError(
            f"cannot pin metric code: missing {missing}. Refusing to record null "
            "digests, which would make metric drift permanently undetectable.")
    return {
        "metric_code_digests": {f: _file_digest(root / f)
                                for f in METRIC_CODE_FILES},
        "baseline_definition_version": 1,
        "ci_policy_version": CI_POLICY_VERSION,
    }


def classify_metric_drift(recorded: dict | None, repo_root=None) -> dict:
    """Missing references are `unknown`, never `none`."""
    if not recorded or not recorded.get("metric_code_digests"):
        return {"classification": "unknown", "material": True, "changed": [],
                "detail": "no metric references recorded"}
    try:
        current = metric_reference_snapshot(repo_root)
    except Exception as exc:
        return {"classification": "unknown", "material": True, "changed": [],
                "detail": f"unresolvable: {type(exc).__name__}"}
    if recorded.get("baseline_definition_version") != \
            current["baseline_definition_version"]:
        return {"classification": DRIFT_BASELINE, "material": True,
                "changed": ["baseline_definition_version"],
                "detail": "the baseline definition changed"}
    if recorded.get("ci_policy_version") != current["ci_policy_version"]:
        return {"classification": DRIFT_METRIC_CODE, "material": True,
                "changed": ["ci_policy_version"],
                "detail": "the confidence-interval policy changed"}
    changed = [f for f, d in recorded["metric_code_digests"].items()
               if current["metric_code_digests"].get(f) != d]
    if changed:
        return {"classification": DRIFT_METRIC_CODE, "material": True,
                "changed": changed,
                "detail": "the metric would be computed differently now"}
    return {"classification": "none", "material": False, "changed": [],
            "detail": "metric references match"}


# --- metrics -------------------------------------------------------------------


@dataclass
class _Member:
    forecast_id: int
    market_ticker: str
    probability: float
    forecaster_version: str | None
    domain: str | None
    y: float | None = None          # 1.0/0.0 once scored; None while pending
    state: str = "pending"          # pending | scored | unscorable


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def compute_metrics(scored: list) -> dict:
    """Canonical Brier, base-rate baseline and skill.

    `brier_score` is imported from `calibration`, not reimplemented: a second
    implementation is a second answer, and the whole point of registering a
    metric is that it means one thing.
    """
    if not scored:
        return {"n": 0, "prevalence": None, "mean_brier": None,
                "base_rate_brier": None, "brier_skill_vs_base_rate": None}
    ys = [m.y for m in scored]
    prevalence = _mean(ys)
    mean_brier = _mean([brier_score(m.probability, m.y) for m in scored])
    base_rate_brier = _mean([brier_score(prevalence, y) for y in ys])
    skill = (None if not base_rate_brier
             else round(1.0 - (mean_brier / base_rate_brier), 6))
    return {"n": len(scored), "prevalence": round(prevalence, 6),
            "mean_brier": round(mean_brier, 6),
            "base_rate_brier": round(base_rate_brier, 6),
            "brier_skill_vs_base_rate": skill}


def compute_secondary(scored: list) -> dict:
    """ECE and Murphy components via the deployed reliability implementation."""
    if not scored:
        return {}
    from app.services.forecast_reliability import (
        _Point,
        calibration_error,
        compute_bins,
        murphy_decomposition,
    )

    now = datetime.now(timezone.utc)
    points = [_Point(p=m.probability, y=m.y, created_at=now,
                     domain=m.domain or "unknown",
                     forecaster=m.forecaster_version or "unknown",
                     evidence_depth="", forecast_risk="",
                     research_completeness_bucket="", research_risk="",
                     resolution_risk="", tradeability="") for m in scored]
    edges = [i / 10 for i in range(11)]
    bins = compute_bins(points, edges)
    ce = calibration_error(bins, len(points))
    murphy = murphy_decomposition(points, edges)
    return {
        SECONDARY_ECE: ce.get("ece"),
        SECONDARY_MURPHY: {k: murphy.get(k) for k in
                           ("reliability", "resolution", "uncertainty")},
    }


def _delta_for(metrics: dict, primary: str):
    """Positive always means "better than baseline", for both metrics."""
    if primary == METRIC_BRIER_SKILL:
        return metrics.get(METRIC_BRIER_SKILL)
    mb, bb = metrics.get(METRIC_MEAN_BRIER), metrics.get(BASELINE_BASE_RATE_BRIER)
    if mb is None or bb is None:
        return None
    # Lower Brier is better, so "better than baseline" is baseline - model.
    return round(bb - mb, 6)


def _cluster_bootstrap_ci(scored: list, metric_name: str) -> dict:
    """Deterministic cluster bootstrap, resampling MARKETS not forecasts.

    Forecasts on the same market share an outcome, so treating them as
    independent draws understates the interval — often badly, because this
    repository's population is dominated by one domain with many forecasts per
    market. Clustering by market is the conservative choice and it is fixed
    before registration so nobody can pick the flattering one later.

    Seed and sample count are constants. An evaluator who could reroll could
    keep rolling until an interval cleared zero.
    """
    import random

    if len(scored) < 2:
        return {"policy": CI_CLUSTER_BOOTSTRAP, "version": CI_POLICY_VERSION,
                "lower": None, "upper": None, "alpha": CI_ALPHA,
                "note": "insufficient sample"}
    clusters: dict[str, list] = {}
    for m in scored:
        clusters.setdefault(m.market_ticker, []).append(m)
    keys = sorted(clusters)
    rng = random.Random(CI_SEED)
    stats: list[float] = []
    for _ in range(CI_BOOTSTRAP_SAMPLES):
        drawn = []
        for _ in range(len(keys)):
            drawn.extend(clusters[keys[rng.randrange(len(keys))]])
        # H4 — bootstrap the DELTA, not the raw metric. `mean_brier` is a
        # squared error, so its lower bound exceeds 0 for any non-perfect
        # forecaster: `ci_lower_bound_gt_zero` paired with `mean_brier` was an
        # UNCONDITIONAL supports_hypothesis. A deliberately worthless p=0.5
        # forecaster at 50% prevalence produced delta 0.0 and CI [0.25, 0.25]
        # and "passed". The interval must be about the comparison the decision
        # rule tests.
        v = _delta_for(compute_metrics(drawn), metric_name)
        if v is not None and math.isfinite(v):
            stats.append(v)
    if not stats:
        return {"policy": CI_CLUSTER_BOOTSTRAP, "version": CI_POLICY_VERSION,
                "lower": None, "upper": None, "alpha": CI_ALPHA,
                "note": "no finite bootstrap statistics"}
    stats.sort()
    lo = stats[int((CI_ALPHA / 2) * len(stats))]
    hi = stats[min(len(stats) - 1, int((1 - CI_ALPHA / 2) * len(stats)))]
    return {"policy": CI_CLUSTER_BOOTSTRAP, "version": CI_POLICY_VERSION,
            "lower": round(lo, 6), "upper": round(hi, 6), "alpha": CI_ALPHA,
            "clusters": len(keys), "samples": CI_BOOTSTRAP_SAMPLES,
            "seed": CI_SEED}


# --- result record ---------------------------------------------------------------


@dataclass
class ResultRecord:
    experiment_id: str
    experiment_version: int | None
    manifest_digest: str | None
    registration_event_hash: str | None
    registry_head_hash: str | None
    predicate_schema_version: int | None
    predicate_digest: str | None
    membership_digest: str | None
    evaluation_commit: str | None
    evaluation_code_digest: dict = field(default_factory=dict)
    baseline_definition_version: int | None = None
    ci_policy: dict = field(default_factory=dict)
    evaluated_at: str = ""
    recorded_at: str = ""          # real clock; never caller-supplied
    collection_started_at: str | None = None
    collection_ended_at: str | None = None
    scanned_forecast_count: int = 0     # every forecast row scanned, not a cohort
    eligible_count: int = 0             # what reconstruction found eligible
    actual_population_count: int = 0
    pre_registration_excluded: int = 0
    post_end_excluded: int = 0
    declared_rule_exclusions: dict = field(default_factory=dict)
    unexpected_exclusions: dict = field(default_factory=dict)
    matured_count: int = 0
    pending_count: int = 0
    unscorable_count: int = 0
    matured_fraction: float | None = None
    primary_metric_name: str | None = None
    primary_metric_value: float | None = None
    declared_baseline_name: str | None = None
    declared_baseline_value: float | None = None
    metric_delta: float | None = None
    confidence_interval: dict = field(default_factory=dict)
    sample_floor: int | None = None
    sample_floor_met: bool = False
    stopping_rule: dict = field(default_factory=dict)
    stopping_rule_met: bool = False
    decision_rule: str | None = None
    secondary_metrics: dict = field(default_factory=dict)
    domain_composition: dict = field(default_factory=dict)
    forecaster_version_composition: dict = field(default_factory=dict)
    missingness: dict = field(default_factory=dict)
    protocol_deviations: list = field(default_factory=list)
    invalidating_events: list = field(default_factory=list)
    drift_classification: dict = field(default_factory=dict)
    operator_notes: str | None = None
    reevaluation_reason: str | None = None
    verdict: str = STILL_COLLECTING
    previous_result_digest: str | None = None
    content_digest: str = ""       # the finding, independent of when/who
    result_digest: str = ""        # this evaluation, timestamps included
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict:
        return asdict(self)


# Timestamps and operator prose identify WHEN and WHO, not WHAT was found.
_NON_CONTENT_FIELDS = ("result_digest", "content_digest", "evaluated_at",
                       "recorded_at", "operator_notes", "reevaluation_reason",
                       "previous_result_digest", "evaluation_commit",
                       "superseded_by_protocol", "terminal_verdict_of_record")


def result_digest(record: dict) -> str:
    """Identity of THIS evaluation, timestamps included.

    Two evaluations of the same data at different moments are different
    records, and in an append-only log they must be, or the second could not be
    appended alongside the first.
    """
    payload = {k: v for k, v in record.items() if k != "result_digest"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def content_digest(record: dict) -> str:
    """Identity of the FINDING — what reproducibility actually means.

    Excludes when it was computed, by whom, and the append-chain links. Two
    independent reconstructions of the same experiment over the same data must
    produce the same content digest even though their result digests differ.
    """
    payload = {k: v for k, v in record.items() if k not in _NON_CONTENT_FIELDS}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def read_result_events(experiment_id: str, base: Path | None = None) -> list:
    path = experiment_dir(experiment_id, base) / RESULT_EVENTS_FILENAME
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def read_result_head(experiment_id: str, base: Path | None = None) -> dict | None:
    path = experiment_dir(experiment_id, base) / RESULT_HEAD_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text())


def verify_result_chain(experiment_id: str, base: Path | None = None) -> dict:
    events = read_result_events(experiment_id, base)
    head = read_result_head(experiment_id, base)
    if not events and head is None:
        # Explicit zero-result state: valid, and distinguishable from a deleted
        # history because a deleted history leaves a head behind.
        return {"intact": True, "length": 0, "reason": None, "empty": True}
    prev = None
    for i, e in enumerate(events):
        if e.get("seq") != i or e.get("prev") != prev:
            return {"intact": False, "length": len(events),
                    "reason": f"result chain broken at {i}"}
        prev = hashlib.sha256(canonical_json(e).encode("utf-8")).hexdigest()
    if head is None:
        return {"intact": False, "length": len(events),
                "reason": "result head missing"}
    if head.get("result_count") != len(events) or \
            head.get("terminal_result_hash") != prev:
        return {"intact": False, "length": len(events),
                "reason": "result head does not match the log — truncated or "
                          "appended without updating the head"}
    if len(events) > MAX_RESULTS:
        return {"intact": False, "length": len(events),
                "reason": f"result count exceeds {MAX_RESULTS}"}

    # H2 — the events carry `file` and `result_digest`, and nothing checked
    # them: a result JSON could be deleted, or its verdict rewritten to
    # supports_hypothesis, and the chain still verified. The evidence existed
    # and was simply never used.
    rdir = experiment_dir(experiment_id, base) / RESULTS_DIRNAME
    for e in events:
        f = rdir / str(e.get("file", ""))
        if not f.exists():
            return {"intact": False, "length": len(events),
                    "reason": f"result file {e.get('file')!r} is missing"}
        try:
            stored = json.loads(f.read_text())
        except Exception:
            return {"intact": False, "length": len(events),
                    "reason": f"result file {e.get('file')!r} is unreadable"}
        if result_digest(stored) != e.get("result_digest") or \
                stored.get("result_digest") != e.get("result_digest"):
            return {"intact": False, "length": len(events),
                    "reason": f"result file {e.get('file')!r} does not match "
                              "its recorded digest"}
    return {"intact": True, "length": len(events), "reason": None,
            "empty": False,
            "terminal_result_digest": head.get("terminal_result_digest"),
            "terminal_verdict": head.get("terminal_verdict")}


def _append_result(experiment_id: str, record: dict, base: Path | None = None) -> dict:
    """Append-only, hash-chained, head-pinned. Never overwrites."""
    d = experiment_dir(experiment_id, base)
    (d / RESULTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    prior = read_result_events(experiment_id, base)
    if len(prior) >= MAX_RESULTS:
        raise ManifestError(f"result log exceeds {MAX_RESULTS} entries")
    prev_hash = (hashlib.sha256(canonical_json(prior[-1]).encode("utf-8")).hexdigest()
                 if prior else None)

    digest = record["result_digest"]
    stamp = record["evaluated_at"].replace(":", "").replace("-", "")[:15]
    path = d / RESULTS_DIRNAME / f"{stamp}-{digest[:12]}.json"
    if path.exists():
        raise ManifestError(
            f"a result file already exists at {path.name}; results are "
            "append-only and are never overwritten")
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    entry = {"event": "result", "seq": len(prior), "prev": prev_hash,
             "at": record["evaluated_at"], "verdict": record["verdict"],
             "result_digest": digest, "file": path.name,
             "previous_result_digest": record.get("previous_result_digest")}
    with (d / RESULT_EVENTS_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(canonical_json(entry) + "\n")
    prior_head = read_result_head(experiment_id, base) or {}
    # H1 — the head used to track the NEWEST result, so peeking repeatedly and
    # citing the flattering one was fully available. The FIRST terminal verdict
    # is now pinned and never moves; later records are marked superseded so a
    # reader cannot mistake one for the binding result.
    terminal_digest = prior_head.get("terminal_result_digest")
    terminal_verdict = prior_head.get("terminal_verdict")
    if terminal_digest is None and record["verdict"] != STILL_COLLECTING:
        terminal_digest = digest
        terminal_verdict = record["verdict"]
    head = {"experiment_id": experiment_id, "result_count": len(prior) + 1,
            "terminal_result_hash": hashlib.sha256(
                canonical_json(entry).encode("utf-8")).hexdigest(),
            "terminal_result_digest": terminal_digest,
            "terminal_verdict": terminal_verdict,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    (d / RESULT_HEAD_FILENAME).write_text(
        json.dumps(head, indent=2, sort_keys=True) + "\n")
    return {"file": str(path), "head": head}


def _load_member_state(session: Session, member_ids: list) -> list:
    """Attach outcome/score state to an ALREADY FROZEN membership.

    Called only after `reconstruct_population` has returned and its digest has
    been recorded. Nothing here can change who is a member.
    """
    if not member_ids:
        return []
    forecasts = {f.id: f for f in session.execute(
        select(MarketForecastRecord).options(load_only(
            MarketForecastRecord.market_ticker,
            MarketForecastRecord.estimated_probability,
            MarketForecastRecord.forecaster_version,
            MarketForecastRecord.research_packet_id,
        )).where(MarketForecastRecord.id.in_(member_ids))
    ).scalars()}
    tickers = {f.market_ticker for f in forecasts.values()}
    outcomes = {o.market_ticker: o for o in session.execute(
        select(MarketOutcomeRecord).where(
            MarketOutcomeRecord.market_ticker.in_(tickers or {""}))
    ).scalars()}
    latest_score: dict[int, ForecastScoreRecord] = {}
    for row in session.execute(
        select(ForecastScoreRecord).where(
            ForecastScoreRecord.forecast_id.in_(member_ids))
        .order_by(ForecastScoreRecord.id)
    ).scalars():
        latest_score[row.forecast_id] = row

    from app.models import MarketResearchPacket
    pids = {f.research_packet_id for f in forecasts.values() if f.research_packet_id}
    domains = {pid: dom for pid, dom in session.execute(
        select(MarketResearchPacket.id, MarketResearchPacket.domain)
        .where(MarketResearchPacket.id.in_(pids or {0}))).all()}

    out = []
    for fid in sorted(member_ids):
        f = forecasts.get(fid)
        if f is None:
            continue
        m = _Member(forecast_id=fid, market_ticker=f.market_ticker,
                    probability=f.estimated_probability,
                    forecaster_version=f.forecaster_version,
                    domain=domains.get(f.research_packet_id))
        outcome = outcomes.get(f.market_ticker)
        status, y, _ = _score_target(outcome)
        score = latest_score.get(fid)
        if status == STATUS_SCORED and y is not None:
            m.y, m.state = y, "scored"
        elif outcome is not None and status == "unscorable":
            m.state = "unscorable"
        else:
            m.state = "pending"
        # A member whose score row disagrees with the current outcome is a
        # data-quality signal, not something to quietly re-derive.
        if score is not None and score.score_status == STATUS_SCORED \
                and status != STATUS_SCORED:
            m.state = "unscorable"
        out.append(m)
    return out


def validate_result_protocol(manifest: dict) -> list:
    """Protocol errors that must block REGISTRATION, not merely evaluation."""
    errors = []
    proto = manifest.get("result_protocol") or {}
    pm = manifest.get("primary_metric")
    # A list here is already rejected upstream as "multiple primaries"; guard so
    # this validator reports its own findings instead of raising.
    primary = pm.get("name") if isinstance(pm, dict) else None
    rule = proto.get("decision_rule")
    stopping = proto.get("stopping_rule") or {}

    if primary not in SUPPORTED_PRIMARY_METRICS:
        errors.append(f"primary metric {primary!r} is not registry-supported")
    if proto.get("baseline") not in SUPPORTED_BASELINES:
        errors.append(f"baseline {proto.get('baseline')!r} is not supported")
    if rule not in SUPPORTED_DECISION_RULES:
        errors.append(f"decision rule {rule!r} is not supported")
    if proto.get("confidence_interval_policy") not in SUPPORTED_CI_POLICIES:
        errors.append("confidence-interval policy is not supported")

    if stopping.get("kind") not in SUPPORTED_STOPPING_RULES:
        errors.append(f"stopping rule {stopping.get('kind')!r} is not supported")
    elif stopping.get("kind") == STOP_FIXED_SAMPLE_AND_END:
        # H5 — the "and_end" half was unenforced, so the terminal moment was
        # "the first evaluation after the count clears the floor". That is the
        # optional-stopping surface this milestone exists to remove.
        if not stopping.get("not_after"):
            errors.append(
                "fixed_sample_and_end requires not_after: without a declared "
                "end the terminal moment is whenever someone chooses to look")
        if not stopping.get("minimum_sample"):
            errors.append("fixed_sample_and_end requires minimum_sample")
    return errors


def _check_stopping_rule(rule: dict, *, now: datetime, scored: int,
                         matured_fraction: float | None,
                         registered_at: datetime) -> tuple:
    """(met, reasons). Closed vocabulary; prose is never executable."""
    kind = rule.get("kind")
    if kind not in SUPPORTED_STOPPING_RULES:
        return False, [f"unsupported stopping rule {kind!r}"]
    reasons = []
    minimum = rule.get("minimum_sample")
    if minimum is not None and scored < minimum:
        reasons.append(f"minimum_sample {minimum} not reached ({scored})")
    frac = rule.get("minimum_matured_fraction")
    if frac is not None and (matured_fraction is None or matured_fraction < frac):
        reasons.append(
            f"minimum_matured_fraction {frac} not reached ({matured_fraction})")
    not_before = rule.get("not_before")
    if not_before:
        nb = _aware(datetime.fromisoformat(str(not_before).replace("Z", "+00:00")))
        if now < nb:
            reasons.append(f"not_before {not_before} has not passed")
    not_after = rule.get("not_after")
    if not_after:
        na = _aware(datetime.fromisoformat(str(not_after).replace("Z", "+00:00")))
        if now > na:
            reasons.append(f"not_after {not_after} has passed")
    return (not reasons), reasons


def _derive_verdict(*, integrity_ok: bool, drift_material: bool,
                    data_quality_bad: bool, stopping_met: bool,
                    floor_met: bool, delta, decision_rule: str,
                    ci: dict) -> tuple:
    """Registry-derived verdict with fixed precedence. The caller has no say.

    Precedence exists so a favourable metric cannot outrank a broken protocol.
    Every branch above the last one returns without ever looking at the number.
    """
    if not integrity_ok:
        return INVALID_PROTOCOL, ["registry or event integrity is broken"]
    if drift_material:
        return INVALID_PROTOCOL, ["material unresolved drift"]
    if data_quality_bad:
        return INVALID_DATA, ["data-quality invalidation"]
    if not stopping_met:
        return STILL_COLLECTING, ["stopping rule not satisfied"]
    if not floor_met:
        return INCONCLUSIVE_FLOOR, ["sample floor not met at a valid terminal end"]
    if delta is None:
        return INCONCLUSIVE_FLOOR, ["primary metric could not be computed"]
    if decision_rule == DECISION_DELTA_GT_ZERO:
        return (SUPPORTS if delta > 0 else DOES_NOT_SUPPORT), [
            f"delta {delta} vs 0 under {decision_rule}"]
    if decision_rule == DECISION_DELTA_LT_ZERO:
        return (SUPPORTS if delta < 0 else DOES_NOT_SUPPORT), [
            f"delta {delta} vs 0 under {decision_rule}"]
    if decision_rule == DECISION_CI_LOWER_GT_ZERO:
        lower = ci.get("lower")
        if lower is None:
            return INCONCLUSIVE_FLOOR, ["confidence interval unavailable"]
        return (SUPPORTS if lower > 0 else DOES_NOT_SUPPORT), [
            f"ci lower {lower} vs 0 under {decision_rule}"]
    if decision_rule == DECISION_CI_UPPER_LT_ZERO:
        upper = ci.get("upper")
        if upper is None:
            return INCONCLUSIVE_FLOOR, ["confidence interval unavailable"]
        # A point estimate below zero is not evidence of persistent
        # underperformance — the interval has to exclude zero. Otherwise
        # "continues to fail" is confirmed by noise.
        return (SUPPORTS if upper < 0 else DOES_NOT_SUPPORT), [
            f"ci upper {upper} vs 0 under {decision_rule}"]
    return INVALID_PROTOCOL, [f"unsupported decision rule {decision_rule!r}"]


def evaluate_experiment(
    session: Session, experiment_id: str, *, base: Path | None = None,
    confirm: bool = False, now: datetime | None = None,
    operator_notes: str | None = None, reevaluation_reason: str | None = None,
    commit: str | None = None,
) -> dict:
    """The whole contract, in the order that makes it a control.

    The caller supplies an id and prose. Everything that could change the answer
    — population, metric, baseline, floor, stopping rule, verdict — comes from
    the registered manifest.
    """
    real_now = datetime.now(timezone.utc)
    for label, text in (("operator_notes", operator_notes),
                        ("reevaluation_reason", reevaluation_reason)):
        if text is None:
            continue
        if not isinstance(text, str) or len(text) > MAX_NOTE_CHARS:
            raise ManifestError(
                f"{label} must be text under {MAX_NOTE_CHARS} characters")
        from app.services.experiment_registry import (
            SECRET_FIELD_PATTERN,
            SECRET_VALUE_PATTERN,
        )

        if SECRET_VALUE_PATTERN.search(text) or SECRET_FIELD_PATTERN.search(text):
            raise ManifestError(
                f"{label} looks like it carries a credential; it is committed")
    now = _aware(now) or real_now
    if confirm and abs((now - real_now).total_seconds()) > MAX_CLOCK_SKEW_SECONDS:
        raise ManifestError(
            f"refusing to confirm an evaluation dated {now.isoformat()} when the "
            f"real clock reads {real_now.isoformat()}: the stopping rule is "
            "evaluated against this timestamp, so a caller-chosen clock is a "
            "caller-chosen verdict")
    d = experiment_dir(experiment_id, base)
    if not (d / "manifest.json").exists():
        raise ManifestError(f"{experiment_id!r} is not registered")
    manifest = load_manifest(d / "manifest.json")

    from app.services.experiment_registry import current_state

    state = current_state(experiment_id, base)
    integrity = verify_immutability(experiment_id, base)
    chain = verify_event_chain(experiment_id, base)
    result_chain = verify_result_chain(experiment_id, base)
    integrity_ok = bool(integrity["intact"]) and chain["intact"] \
        and result_chain["intact"]

    refs = manifest.get("immutable_references") or {}
    pop_drift = classify_population_drift(refs.get("population_references"))
    metric_drift = classify_metric_drift(refs.get("metric_references"))

    # M6 — governed re-pinning. Pinning `experiment_results.py` as its own
    # reference meant any edit — including fixing anything a review found —
    # made drift material for every registered experiment, permanently, with no
    # way back except editing a registered manifest. Fail-closed was right;
    # permanently closed was not. An append-only amendment can cover a specific
    # movement, but ONLY a non-semantic one: a change that could move a number
    # forces a new experiment version instead.
    from app.services.experiment_amendments import amendment_for
    from app.services.experiment_population import population_reference_snapshot

    def _covered(kind, drift_result, current_snapshot):
        if not drift_result["material"]:
            return drift_result, None
        try:
            digest = hashlib.sha256(canonical_json(
                current_snapshot).encode("utf-8")).hexdigest()
        except Exception:
            return drift_result, None
        am = amendment_for(experiment_id, kind, digest, base)
        if am and am.get("collection_comparable"):
            out = dict(drift_result)
            out["material"] = False
            out["amended_by"] = am["amendment_id"]
            out["detail"] = (f"covered by amendment {am['amendment_id']} "
                             f"({am['reason']}); prior collection declared "
                             "comparable by review")
            return out, am
        return drift_result, am

    try:
        cur_metric = metric_reference_snapshot()
    except Exception:
        cur_metric = {}
    try:
        cur_pop = population_reference_snapshot()
    except Exception:
        cur_pop = {}
    metric_drift, metric_amend = _covered(
        "metric_references", metric_drift, cur_metric)
    pop_drift, pop_amend = _covered(
        "population_references", pop_drift, cur_pop)

    drift = {"population": pop_drift, "metric": metric_drift,
             "amendments": [a["amendment_id"] for a in (metric_amend, pop_amend)
                            if a]}
    drift_material = bool(pop_drift["material"] or metric_drift["material"])

    protocol = manifest.get("result_protocol") or {}
    primary = (manifest.get("primary_metric") or {}).get("name")
    baseline = protocol.get("baseline")
    decision_rule = protocol.get("decision_rule")
    stopping = protocol.get("stopping_rule") or {}
    floor = manifest.get("sample_floor")
    min_frac = manifest.get("minimum_matured_fraction")

    deviations = list(validate_result_protocol(manifest))
    if False and primary not in SUPPORTED_PRIMARY_METRICS:
        deviations.append(f"primary metric {primary!r} is not registry-supported")
    if baseline not in SUPPORTED_BASELINES:
        deviations.append(f"baseline {baseline!r} is not registry-supported")
    if decision_rule not in SUPPORTED_DECISION_RULES:
        deviations.append(f"decision rule {decision_rule!r} is not supported")
    if protocol.get("confidence_interval_policy") not in SUPPORTED_CI_POLICIES:
        deviations.append("confidence-interval policy is not supported")
    if state not in (MATURED, EVALUATED, "collecting", "registered"):
        deviations.append(f"state {state!r} does not permit evaluation")

    # --- 1. membership, from forecast-time fields ONLY -------------------------
    registered_at = _aware(datetime.fromisoformat(
        str(manifest["registered_at"]).replace("Z", "+00:00")))
    canon = ensure_registration_floor(
        canonicalize_population(manifest["population"]))
    pinned_predicate = refs.get("population_predicate_digest")
    if pinned_predicate and pinned_predicate != population_digest(canon):
        deviations.append("population predicate digest differs from registration")

    pop = reconstruct_population(
        session, experiment_id=experiment_id, population=manifest["population"],
        registered_at=registered_at,
        manifest_digest=integrity["digest_recomputed_now"])

    # --- 2. freeze, then attach outcome/score state ----------------------------
    frozen_digest = pop.membership_digest
    member_ids = _member_ids(session, pop, canon, registered_at)
    # M2 — `_member_ids` re-derives the list rather than reusing the frozen
    # digest's members, so agreement must be proven, not assumed.
    recomputed = hashlib.sha256(json.dumps(
        {"experiment_id": experiment_id, "members": member_ids},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if recomputed != pop.membership_digest:
        raise ManifestError(
            "membership digest disagrees with the evaluated cohort; refusing to "
            "evaluate a population that cannot be reproduced")
    members = _load_member_state(session, member_ids)
    scored = [m for m in members if m.state == "scored"]
    pending = [m for m in members if m.state == "pending"]
    unscorable = [m for m in members if m.state == "unscorable"]
    matured_fraction = (round(len(scored) / len(members), 6) if members else None)

    # --- 3. metrics ------------------------------------------------------------
    metrics = compute_metrics(scored)
    secondary = compute_secondary(scored) if scored else {}
    primary_value = metrics.get(primary)
    baseline_value = metrics.get(BASELINE_BASE_RATE_BRIER)
    if primary == METRIC_MEAN_BRIER and primary_value is not None \
            and baseline_value is not None:
        # Lower Brier is better, so "better than baseline" is a NEGATIVE
        # difference. Reported as baseline - model so a positive delta always
        # means "better", which is what the decision rules assume.
        delta = round(baseline_value - primary_value, 6)
    elif primary == METRIC_BRIER_SKILL:
        delta = primary_value          # skill is already relative to baseline
    else:
        delta = None
    ci = _cluster_bootstrap_ci(scored, primary) if scored else {}

    stopping_met, stop_reasons = _check_stopping_rule(
        stopping, now=now, scored=len(scored), matured_fraction=matured_fraction,
        registered_at=registered_at)
    floor_met = bool(floor is not None and len(scored) >= floor)
    if min_frac is not None and (matured_fraction is None
                                 or matured_fraction < min_frac):
        stopping_met = False
        stop_reasons.append(
            f"matured fraction {matured_fraction} below required {min_frac}")

    # Data-quality invalidation: members the pipeline says are scored while the
    # current outcome disagrees. Not "some field was null" — that is ordinary.
    data_quality_bad = bool(unscorable) and len(unscorable) > len(members) * 0.5
    # A degenerate base rate makes the baseline unbeatable and the skill metric
    # meaningless. This is the exact artifact that produced soccer's Brier of
    # 0.0033 on 34 members at 2.9% prevalence — reported as a domain result when
    # it was a property of the sample. Caught here rather than rediscovered.
    prevalence = metrics.get("prevalence")
    # Threshold set to the regime the comment cites: soccer was 2.9%. A 1%
    # bound would not have caught it, which made the guard false assurance.
    degenerate_base_rate = (
        baseline_value is not None and baseline_value < 1e-9) or (
        prevalence is not None and (prevalence <= DEGENERATE_PREVALENCE
                                    or prevalence >= 1 - DEGENERATE_PREVALENCE))
    # Only at a terminal state. Mid-collection a lopsided prevalence is an
    # ordinary small-sample artifact that the next observations will move; it
    # only becomes a data-quality finding once this is the final sample.
    if scored and degenerate_base_rate and stopping_met:
        data_quality_bad = True
    invalidating = []
    if scored and degenerate_base_rate and stopping_met:
        invalidating.append(
            f"degenerate base rate: prevalence {prevalence}, baseline Brier "
            f"{baseline_value} — the baseline is unbeatable and the metric "
            "carries no information about forecast quality")
    if pop.pre_registration_excluded and pop.eligible_count == 0 and members:
        invalidating.append("membership disagrees with reconstruction")
    if deviations:
        invalidating.extend(deviations)

    verdict, why = _derive_verdict(
        integrity_ok=integrity_ok, drift_material=drift_material,
        data_quality_bad=data_quality_bad, stopping_met=stopping_met,
        floor_met=floor_met, delta=delta,
        decision_rule=decision_rule or "", ci=ci)
    if deviations and verdict in TERMINAL_FAVOURABLE:
        verdict = INVALID_PROTOCOL
        why = why + ["protocol deviations present"]

    prior_events = read_result_events(experiment_id, base)
    previous_digest = prior_events[-1]["result_digest"] if prior_events else None
    prior_head = read_result_head(experiment_id, base) or {}
    locked_verdict = prior_head.get("terminal_verdict")
    locked_digest = prior_head.get("terminal_result_digest")
    if prior_events and confirm and not reevaluation_reason:
        raise ManifestError(
            "re-evaluation requires an explicit reason; the first terminal "
            "result is preserved and this will be appended, not substituted")

    record = ResultRecord(
        experiment_id=experiment_id,
        experiment_version=manifest.get("experiment_version"),
        manifest_digest=integrity["digest_recomputed_now"],
        registration_event_hash=_registration_event_hash(experiment_id, base),
        registry_head_hash=(chain.get("head") or {}).get("terminal_event_hash"),
        predicate_schema_version=pop.predicate_schema_version,
        predicate_digest=pop.predicate_digest,
        membership_digest=frozen_digest,
        evaluation_commit=commit,
        evaluation_code_digest=metric_reference_snapshot().get(
            "metric_code_digests", {}),
        baseline_definition_version=1,
        ci_policy={"policy": protocol.get("confidence_interval_policy"),
                   "version": CI_POLICY_VERSION},
        evaluated_at=now.isoformat(),
        recorded_at=real_now.isoformat(),
        collection_started_at=registered_at.isoformat(),
        collection_ended_at=pop.declared_end,
        scanned_forecast_count=pop.population_count,
        eligible_count=pop.eligible_count,
        actual_population_count=len(members),
        pre_registration_excluded=pop.pre_registration_excluded,
        post_end_excluded=pop.post_end_excluded,
        declared_rule_exclusions=pop.declared_rule_exclusions,
        unexpected_exclusions=pop.unexpected_missing_fields,
        matured_count=len(scored), pending_count=len(pending),
        unscorable_count=len(unscorable), matured_fraction=matured_fraction,
        primary_metric_name=primary, primary_metric_value=primary_value,
        declared_baseline_name=baseline, declared_baseline_value=baseline_value,
        metric_delta=delta, confidence_interval=ci,
        sample_floor=floor, sample_floor_met=floor_met,
        stopping_rule=stopping, stopping_rule_met=stopping_met,
        decision_rule=decision_rule,
        secondary_metrics=secondary,
        domain_composition=_count(m.domain for m in members),
        forecaster_version_composition=_count(
            m.forecaster_version for m in members),
        missingness={"pending": len(pending), "unscorable": len(unscorable)},
        protocol_deviations=deviations + stop_reasons,
        invalidating_events=invalidating,
        drift_classification=drift,
        operator_notes=operator_notes, reevaluation_reason=reevaluation_reason,
        verdict=verdict, previous_result_digest=previous_digest,
    ).to_dict()
    # H1 — once a terminal verdict is locked, later evaluations are recorded but
    # explicitly superseded. Peeking is not forbidden (an outcome really can be
    # corrected upstream); citing a later peek as THE result is.
    record["superseded_by_protocol"] = bool(
        locked_digest and record["verdict"] != STILL_COLLECTING)
    record["terminal_verdict_of_record"] = locked_verdict or (
        record["verdict"] if record["verdict"] != STILL_COLLECTING else None)
    record["verdict_reasons"] = why
    record["content_digest"] = content_digest(record)
    record["result_digest"] = result_digest(record)

    out = {"mode": "confirmed" if confirm else "dry_run",
           "persisted": False, "record": record,
           "integrity": {"manifest": integrity["intact"], "events": chain,
                         "results": result_chain},
           "external_calls": 0, "disclaimer": DISCLAIMER}
    if not confirm:
        return out
    if not integrity_ok:
        raise ManifestError(
            "refusing to record a result for an experiment whose registry "
            "integrity is broken")
    out.update(_append_result(experiment_id, record, base))
    out["persisted"] = True
    return out


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        key = str(v)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _member_ids(session: Session, pop, canon, registered_at) -> list:
    """Re-derive the full member id list (the report caps `examples`)."""
    from app.services.experiment_population import _load_facts
    from app.services.experiment_predicates import declared_window_end, evaluate

    end = declared_window_end(canon)
    ids = []
    for row in _load_facts(session):
        created = row.values.get("forecast_created_at")
        if created is None or created < registered_at:
            continue
        if end is not None and created >= end:
            continue
        ok, _ = evaluate(canon, row, registered_at=registered_at,
                         declared_end=end)
        if ok:
            ids.append(row.forecast_id)
    return sorted(ids)


def _registration_event_hash(experiment_id: str, base: Path | None = None):
    """Hash of the `registered` event — the anchor the result ties back to."""
    for e in read_events(experiment_id, base):
        if e.get("event") == "registered":
            return hashlib.sha256(
                canonical_json(e).encode("utf-8")).hexdigest()
    return None
