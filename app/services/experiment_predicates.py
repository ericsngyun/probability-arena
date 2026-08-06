"""PROSPECTIVE-EXPERIMENT-REGISTRY-002A — typed population predicates.

REGISTRY-001 let a manifest define its population in prose and guarded it with a
blocklist of suspicious words. An independent review broke that in one line:
`"include forecasts in the cohort that beat the benchmark"` passed cleanly —
exactly the post-hoc selection the registry exists to prevent — and the repo's
own tennis draft shipped a score-dependent exclusion that a differently-worded
test asserted was rejected.

The lesson is that prose cannot be an enforcement surface. A blocklist rejects
spellings; membership needs a decision procedure. So prose is demoted to
rationale and the executable authority becomes a **closed, typed, versioned**
predicate document over an **allowlisted set of forecast-time fields**.

The security property is structural rather than lexical: a predicate referencing
an outcome, a score or any post-forecast fact cannot be *expressed*, because
those fields are not in the registry and the schema admits no free-text escape —
no SQL, no regex, no expression strings, no callables, no `eval`.

Provider-free, database-read-only, and computes nothing about results. No EV,
price, side, size, order, wallet, execution or capital surface exists here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PREDICATE_SCHEMA_VERSION = 1
FIELD_REGISTRY_VERSION = 1

# --- operators ------------------------------------------------------------------
OP_EQ = "eq"
OP_NOT_EQ = "not_eq"
OP_IN = "in"
OP_NOT_IN = "not_in"
OP_LT = "lt"
OP_LTE = "lte"
OP_GT = "gt"
OP_GTE = "gte"
OP_EXISTS = "exists"
OP_NOT_EXISTS = "not_exists"
OP_GTE_REGISTRATION_TIME = "gte_registration_time"
OP_BEFORE_DECLARED_END = "before_declared_end"

ALL_OPERATORS = (
    OP_EQ, OP_NOT_EQ, OP_IN, OP_NOT_IN, OP_LT, OP_LTE, OP_GT, OP_GTE,
    OP_EXISTS, OP_NOT_EXISTS, OP_GTE_REGISTRATION_TIME, OP_BEFORE_DECLARED_END,
)

# Operators that take no `value` — supplying one is a schema error, not a
# harmless extra, because it usually means the author expected it to matter.
VALUELESS_OPERATORS = (
    OP_EXISTS, OP_NOT_EXISTS, OP_GTE_REGISTRATION_TIME, OP_BEFORE_DECLARED_END,
)
SET_OPERATORS = (OP_IN, OP_NOT_IN)
ORDERING_OPERATORS = (OP_LT, OP_LTE, OP_GT, OP_GTE)

TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_TIMESTAMP = "timestamp"
TYPE_BOOL = "bool"

# Values above this length are refused outright. A predicate value is an enum
# member, a ticker or a number; anything longer is either a mistake or an
# attempt to smuggle something through a field that will never interpret it.
MAX_VALUE_CHARS = 128
MAX_SET_MEMBERS = 64
MAX_PREDICATES = 32
MAX_RATIONALE_CHARS = 4000
# Numeric magnitude bound. Without it `float(10**400)` raises OverflowError out
# of a function documented as pure, escaping `validate_manifest`'s PredicateError
# handler and tracebacking the CLI.
MAX_ABS_NUMBER = 1e15


@dataclass(frozen=True)
class FieldSpec:
    """One allowlisted field, and the evidence that it is safe to filter on.

    `available_at` is the load-bearing attribute. A field is admissible only if
    its value is fixed at the moment the forecast is made; anything observed
    later can encode the outcome, however indirectly.
    """
    name: str
    source_model: str
    source_attribute: str
    value_type: str
    operators: tuple
    available_at: str
    immutable_after_forecast: bool
    nullable: bool
    safe_at_forecast_creation: bool
    note: str

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


_COMMON_ENUM_OPS = (OP_EQ, OP_NOT_EQ, OP_IN, OP_NOT_IN, OP_EXISTS, OP_NOT_EXISTS)

FIELD_REGISTRY: dict[str, FieldSpec] = {
    f.name: f for f in (
        FieldSpec(
            name="domain", source_model="MarketResearchPacket",
            source_attribute="domain", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True,
            note="research packet domain, assigned before the forecast exists"),
        FieldSpec(
            name="forecast_created_at", source_model="MarketForecastRecord",
            source_attribute="created_at", value_type=TYPE_TIMESTAMP,
            operators=(OP_LT, OP_LTE, OP_GT, OP_GTE, OP_EXISTS,
                       OP_GTE_REGISTRATION_TIME, OP_BEFORE_DECLARED_END),
            available_at="forecast_creation", immutable_after_forecast=True,
            nullable=False, safe_at_forecast_creation=True,
            note="the prospectivity anchor; compared UTC-aware"),
        FieldSpec(
            name="forecaster", source_model="MarketForecastRecord",
            source_attribute="forecaster_name", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=False,
            safe_at_forecast_creation=True, note="forecaster family"),
        FieldSpec(
            name="forecaster_version", source_model="MarketForecastRecord",
            source_attribute="forecaster_version", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True, note="pins the model generation"),
        FieldSpec(
            name="evidence_depth", source_model="MarketForecastRecord",
            source_attribute="evidence_depth", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True,
            note="self-declared at forecast time; a property of the process"),
        FieldSpec(
            name="forecast_risk", source_model="MarketForecastRecord",
            source_attribute="forecast_risk", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True,
            note="self-declared risk band, not an observed result"),
        FieldSpec(
            name="research_completeness", source_model="MarketResearchPacket",
            source_attribute="research_completeness_score",
            value_type=TYPE_NUMBER,
            operators=(OP_LT, OP_LTE, OP_GT, OP_GTE, OP_EQ, OP_NOT_EQ,
                       OP_EXISTS, OP_NOT_EXISTS),
            available_at="forecast_creation", immutable_after_forecast=True,
            nullable=True, safe_at_forecast_creation=True,
            note="0..1 completeness of the research packet"),
        FieldSpec(
            name="research_risk", source_model="MarketResearchPacket",
            source_attribute="research_risk", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True, note="research risk band"),
        FieldSpec(
            name="resolution_risk", source_model="MarketResolutionAssessment",
            source_attribute="resolution_risk", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True,
            note="PRE-resolution clarity assessment, not the resolution itself"),
        FieldSpec(
            name="tradeability_at_forecast_time",
            source_model="MarketResolutionAssessment",
            source_attribute="tradeability", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=True,
            safe_at_forecast_creation=True,
            note=("a researchability label recorded before the forecast; "
                  "carries no price, side, size or execution meaning")),
        FieldSpec(
            name="market_ticker", source_model="MarketForecastRecord",
            source_attribute="market_ticker", value_type=TYPE_STRING,
            operators=_COMMON_ENUM_OPS, available_at="forecast_creation",
            immutable_after_forecast=True, nullable=False,
            safe_at_forecast_creation=True, note="market identity"),
        FieldSpec(
            name="has_research_packet", source_model="MarketForecastRecord",
            source_attribute="research_packet_id", value_type=TYPE_BOOL,
            operators=(OP_EQ, OP_NOT_EQ, OP_EXISTS, OP_NOT_EXISTS),
            available_at="forecast_creation", immutable_after_forecast=True,
            nullable=False, safe_at_forecast_creation=True,
            note="declared feature-presence flag"),
        FieldSpec(
            name="has_resolution_assessment",
            source_model="MarketForecastRecord",
            source_attribute="resolution_assessment_id", value_type=TYPE_BOOL,
            operators=(OP_EQ, OP_NOT_EQ, OP_EXISTS, OP_NOT_EXISTS),
            available_at="forecast_creation", immutable_after_forecast=True,
            nullable=False, safe_at_forecast_creation=True,
            note="declared feature-presence flag"),
    )
}

# Named for the error message, not for filtering. A field is forbidden by being
# absent from FIELD_REGISTRY; this list only lets the validator explain WHY a
# familiar-looking name was refused, instead of a bare "unknown field".
KNOWN_FORBIDDEN_FIELDS = {
    "outcome": "the outcome is the thing being predicted",
    "outcome_status": "post-resolution state",
    "winner": "post-resolution state",
    "winning_side": "post-resolution state",
    "resolution_result": "post-resolution state",
    "market_result": "post-resolution state",
    "resolved_probability": "post-resolution state",
    "settlement_price": "post-resolution state",
    "score": "derived from the outcome",
    "score_status": "derived from the outcome",
    "scored_current": "derived from the outcome, and an evaluation-time status",
    "was_resolved": "derived from the outcome",
    "brier_score": "derived from the outcome",
    "log_loss": "derived from the outcome",
    "absolute_error": "derived from the outcome",
    "calibration_result": "derived from the outcome",
    "beat_baseline": "derived from the outcome; this is cohort-picking",
    "post_close_price": "observed after the forecast",
    "closing_price": "observed after the forecast",
    "final_price": "observed after the forecast",
    "post_forecast_performance": "observed after the forecast",
    "post_forecast_liquidity": "observed after the forecast",
    "future_market_status": "observed after the forecast",
    "market_status": ("market status is mutable and observed later; use "
                      "forecast-time fields instead"),
    "future_evidence": "observed after the forecast",
    "profit": "not a research quantity in this repository",
    "return": "not a research quantity in this repository",
    "execution_result": "no execution surface exists",
    "pnl": "no capital surface exists",
}


class PredicateError(ValueError):
    """A predicate document that must not define a population."""


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: Any, *, where: str) -> datetime:
    """UTC-aware only. A naive timestamp is refused rather than assumed.

    Guessing a timezone here would silently shift a prospectivity boundary by
    up to a day, which is precisely the kind of quiet error that makes a
    "prospective" experiment retrospective.
    """
    if not isinstance(value, str):
        raise PredicateError(f"{where}: timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PredicateError(f"{where}: {value!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PredicateError(
            f"{where}: {value!r} is timezone-naive; supply an explicit UTC "
            "offset so the boundary cannot shift silently")
    return parsed.astimezone(timezone.utc)


def _check_scalar(value: Any, spec: FieldSpec, where: str) -> None:
    if isinstance(value, bool):
        if spec.value_type != TYPE_BOOL:
            raise PredicateError(f"{where}: {spec.name} is {spec.value_type}, got bool")
        return
    if spec.value_type == TYPE_BOOL:
        raise PredicateError(f"{where}: {spec.name} expects a bool")
    if spec.value_type == TYPE_NUMBER:
        if not isinstance(value, (int, float)):
            raise PredicateError(f"{where}: {spec.name} expects a number")
        # NaN/Infinity survive json.loads, compare False against everything, and
        # serialize as bare NaN/Infinity — which is not valid JSON. A digest
        # containing one cannot be recomputed by any non-Python implementation,
        # silently falsifying the reproducibility property.
        if isinstance(value, float) and (value != value
                                         or value in (float("inf"), float("-inf"))):
            raise PredicateError(f"{where}: {spec.name} must be a finite number")
        if abs(value) > MAX_ABS_NUMBER:
            raise PredicateError(
                f"{where}: {spec.name} magnitude exceeds {MAX_ABS_NUMBER:g}")
        return
    if spec.value_type == TYPE_TIMESTAMP:
        _parse_timestamp(value, where=where)
        return
    if not isinstance(value, str):
        raise PredicateError(f"{where}: {spec.name} expects a string")
    if len(value) > MAX_VALUE_CHARS:
        raise PredicateError(
            f"{where}: value exceeds {MAX_VALUE_CHARS} characters; predicate "
            "values are enum members, tickers or numbers")


def validate_predicate(pred: Any, where: str) -> dict:
    """One typed predicate. Returns its canonical form."""
    if not isinstance(pred, dict):
        raise PredicateError(f"{where}: predicate must be an object")
    unknown_keys = set(pred) - {"field", "operator", "value"}
    if unknown_keys:
        raise PredicateError(
            f"{where}: unsupported keys {sorted(unknown_keys)}; the schema is "
            "closed and admits no free-form escape")

    fname = pred.get("field")
    if not isinstance(fname, str):
        raise PredicateError(f"{where}: 'field' must be a string")
    spec = FIELD_REGISTRY.get(fname)
    if spec is None:
        reason = KNOWN_FORBIDDEN_FIELDS.get(fname)
        if reason:
            raise PredicateError(
                f"{where}: field {fname!r} is forbidden — {reason}. Population "
                "membership must be decidable at forecast time.")
        raise PredicateError(
            f"{where}: field {fname!r} is not in the field registry. Allowed: "
            f"{sorted(FIELD_REGISTRY)}")

    op = pred.get("operator")
    if op not in ALL_OPERATORS:
        raise PredicateError(
            f"{where}: operator {op!r} is not in the closed operator set "
            f"{list(ALL_OPERATORS)}")
    if op not in spec.operators:
        raise PredicateError(
            f"{where}: operator {op!r} is not supported for {fname!r} "
            f"({spec.value_type}); supported: {list(spec.operators)}")

    has_value = "value" in pred
    if op in VALUELESS_OPERATORS:
        if has_value:
            raise PredicateError(
                f"{where}: operator {op!r} takes no value; supplying one usually "
                "means the author expected it to matter")
        return {"field": fname, "operator": op}

    if not has_value:
        raise PredicateError(f"{where}: operator {op!r} requires a value")
    value = pred["value"]

    if op in SET_OPERATORS:
        if not isinstance(value, list) or not value:
            raise PredicateError(f"{where}: {op!r} requires a non-empty list")
        if len(value) > MAX_SET_MEMBERS:
            raise PredicateError(f"{where}: at most {MAX_SET_MEMBERS} members")
        for v in value:
            _check_scalar(v, spec, where)
        # Canonical: sorted + deduplicated. Set membership is order-free, so
        # two authors writing the same set must digest identically.
        canon = sorted({_canon_scalar(v, spec) for v in value},
                       key=lambda x: (str(type(x)), str(x)))
        return {"field": fname, "operator": op, "value": list(canon)}

    if op in ORDERING_OPERATORS and spec.value_type == TYPE_BOOL:
        raise PredicateError(f"{where}: ordering operators are meaningless for bool")
    _check_scalar(value, spec, where)
    return {"field": fname, "operator": op, "value": _canon_scalar(value, spec)}


def _canon_scalar(value: Any, spec: FieldSpec) -> Any:
    if spec.value_type == TYPE_TIMESTAMP:
        return _parse_timestamp(value, where=spec.name).isoformat()
    if spec.value_type == TYPE_NUMBER and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        # 1 and 1.0 are the same threshold and must not digest differently.
        return float(value)
    if spec.value_type == TYPE_STRING and isinstance(value, str):
        return value.strip()
    return value


def _predicate_sort_key(p: dict) -> tuple:
    return (p["field"], p["operator"], json.dumps(p.get("value"), sort_keys=True,
                                                  default=str))


# Gate 4 — fields that name individual markets. A static set is legitimate for a
# predefined universe and is also the cleanest way to hand-pick a cohort, so it
# is gated by experiment kind rather than banned outright.
IDENTIFIER_FIELDS = ("market_ticker",)


def check_identifier_cohort(canon: dict, *, kind: str,
                            universe: dict | None = None) -> list:
    """Errors for identifier predicates that could encode a hand-picked cohort.

    Confirmatory experiments may not enumerate markets unless the set comes
    from a separately committed pre-registration universe artifact with its own
    digest. Exploratory experiments may, because they cannot make a
    confirmatory claim — but the set is still digested and its concentration
    must be reported.
    """
    errors = []
    used = [p for p in canon.get("all", []) + canon.get("none", [])
            if p["field"] in IDENTIFIER_FIELDS]
    if not used:
        return errors
    if kind == CONFIRMATORY_KIND:
        if not universe:
            errors.append(
                "confirmatory experiments may not enumerate market_ticker "
                "without a committed pre-registration universe artifact: an "
                "identifier set chosen by hand IS a cohort, and choosing it "
                "after seeing results is undetectable from the manifest alone")
        else:
            for required in ("universe_id", "digest", "created_at",
                             "selection_method", "member_count"):
                if not universe.get(required):
                    errors.append(f"population.universe.{required} is required")
            method = str(universe.get("selection_method", "")).lower()
            for banned in ("performance", "best", "top", "winning", "beat",
                           "highest", "lowest", "brier", "score"):
                if banned in method:
                    errors.append(
                        f"universe.selection_method references {banned!r}: the "
                        "universe must not be result-derived")
    for p in used:
        if p["operator"] not in (OP_EQ, OP_IN, OP_NOT_EQ, OP_NOT_IN):
            errors.append(
                f"identifier field {p['field']!r} supports only exact set "
                "membership; no wildcard, substring, regex or dynamic selection")
    return errors


CONFIRMATORY_KIND = "confirmatory"


def canonicalize_population(document: Any) -> dict:
    """Validate and canonicalize a whole population document.

    Normalization boundary, stated plainly because overclaiming here would be
    worse than the prose it replaces: entries within `all` and within `none`
    are sorted and de-duplicated, values are type-normalized (numbers to float,
    timestamps to UTC ISO, sets sorted), and exact duplicates collapse. This is
    **syntactic** canonicalization. It does NOT solve Boolean equivalence:
    `lt 5` and `lte 4.999` are semantically identical for many populations and
    will canonicalize differently, and no attempt is made to detect that.
    """
    if not isinstance(document, dict):
        raise PredicateError("population must be an object")
    unknown = set(document) - {"schema_version", "all", "none", "rationale",
                               "window_end", "universe"}
    if unknown:
        raise PredicateError(f"unsupported population keys {sorted(unknown)}")

    version = document.get("schema_version")
    if version is None:
        raise PredicateError("population.schema_version is required")
    if version != PREDICATE_SCHEMA_VERSION:
        raise PredicateError(
            f"population.schema_version {version!r} is not supported "
            f"(this registry implements {PREDICATE_SCHEMA_VERSION})")

    out: dict = {"schema_version": PREDICATE_SCHEMA_VERSION}
    total = 0
    for clause in ("all", "none"):
        entries = document.get(clause, [])
        if not isinstance(entries, list):
            raise PredicateError(f"population.{clause} must be a list")
        total += len(entries)
        if total > MAX_PREDICATES:
            raise PredicateError(f"at most {MAX_PREDICATES} predicates")
        canon = [validate_predicate(p, f"population.{clause}[{i}]")
                 for i, p in enumerate(entries)]
        seen, deduped = set(), []
        for p in canon:
            key = _predicate_sort_key(p)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        out[clause] = sorted(deduped, key=_predicate_sort_key)

    # H1 — the CEILING must be pinned, not supplied by whoever runs the
    # evaluation. The floor was structural while the window's end was a caller
    # argument absent from the digest, which left optional stopping fully
    # available: an evaluator could choose the end date after seeing results and
    # nothing registered would contradict it. That is the exact mirror of the
    # REGISTRY-001 optional-`start_time` bug this module fixed at the other end.
    if "window_end" not in document:
        raise PredicateError(
            'population.window_end is required: either an explicit UTC '
            'timestamp or the literal "unbounded". Leaving the end of the '
            'collection window undeclared permits optional stopping.')
    end = document["window_end"]
    out["window_end"] = ("unbounded" if end == "unbounded"
                         else _parse_timestamp(
                             end, where="population.window_end").isoformat())

    _reject_contradictions(out)
    if "universe" in document:
        u = document["universe"]
        if not isinstance(u, dict):
            raise PredicateError("population.universe must be an object")
        allowed = {"universe_id", "digest", "created_at", "selection_method",
                   "member_count", "source"}
        extra = set(u) - allowed
        if extra:
            raise PredicateError(f"population.universe extra keys {sorted(extra)}")
        out["universe"] = {k: u[k] for k in sorted(u)}
    if "rationale" in document:
        rationale = document["rationale"]
        if not isinstance(rationale, (str, list)):
            raise PredicateError("population.rationale must be text")
        if len(json.dumps(rationale, default=str)) > MAX_RATIONALE_CHARS:
            raise PredicateError(
                f"population.rationale exceeds {MAX_RATIONALE_CHARS} characters")
        # Carried for humans, excluded from the canonical digest below.
        out["rationale"] = rationale
    return out


def _reject_contradictions(canon: dict) -> None:
    """Catch the contradictions that are decidable cheaply.

    Deliberately narrow: an `eq` and a `not_eq` on the same field/value, or the
    same field required both to exist and not exist. Anything requiring real
    constraint solving is out of scope and is not claimed.
    """
    # H2 — `before_declared_end` returns True when no end is pinned: a harmless
    # no-op inside `all`, and a silent emptying of the cohort inside `none`.
    for p in canon.get("none", []):
        if p["operator"] == OP_BEFORE_DECLARED_END:
            raise PredicateError(
                "before_declared_end may not appear in `none`: with an "
                "unbounded window it would exclude every forecast silently")
    # L3 — this check previously inspected only `all`, so a self-contradicting
    # `none` clause excluded everything without comment.
    for p in canon.get("none", []):
        if any(q["field"] == p["field"] and q.get("value") == p.get("value")
               and {q["operator"], p["operator"]} == {OP_EQ, OP_NOT_EQ}
               for q in canon.get("none", [])):
            raise PredicateError(
                f"contradictory `none` predicates on {p['field']!r}: eq and "
                "not_eq of the same value would exclude every forecast")

    eqs = {(p["field"], json.dumps(p.get("value"), sort_keys=True, default=str))
           for p in canon["all"] if p["operator"] == OP_EQ}
    for p in canon["all"]:
        if p["operator"] == OP_NOT_EQ:
            key = (p["field"], json.dumps(p.get("value"), sort_keys=True,
                                          default=str))
            if key in eqs:
                raise PredicateError(
                    f"contradictory predicates on {p['field']!r}: eq and not_eq "
                    "of the same value can never both hold")
    for p in canon["all"]:
        if p["operator"] == OP_EXISTS:
            if any(q["field"] == p["field"] and q["operator"] == OP_NOT_EXISTS
                   for q in canon["all"]):
                raise PredicateError(
                    f"contradictory predicates on {p['field']!r}: exists and "
                    "not_exists can never both hold")
    for p in canon["all"]:
        if any(q["field"] == p["field"] and q["operator"] == p["operator"]
               and q.get("value") != p.get("value")
               and p["operator"] == OP_EQ for q in canon["all"]):
            raise PredicateError(
                f"contradictory predicates on {p['field']!r}: two different eq "
                "values can never both hold")


def canonical_population_json(canon: dict) -> str:
    """Digest input. Rationale is prose and must not change the digest."""
    payload = {k: v for k, v in canon.items() if k != "rationale"}
    # allow_nan=False: a digest a strict JSON parser cannot recompute is not a
    # reproducibility guarantee.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def population_digest(canon: dict) -> str:
    return hashlib.sha256(
        canonical_population_json(canon).encode("utf-8")).hexdigest()


def has_registration_floor(canon: dict) -> bool:
    return any(p["field"] == "forecast_created_at"
               and p["operator"] == OP_GTE_REGISTRATION_TIME
               for p in canon.get("all", []))


def ensure_registration_floor(canon: dict) -> dict:
    """The prospectivity floor is structural: injected if absent.

    Omitting it must not be a way to disable prospectivity. REGISTRY-001 made
    exactly that mistake with an optional `start_time`, where leaving the field
    out skipped the only check that made an experiment prospective. Here the
    floor is added deterministically and appears in the canonical digest, so a
    manifest that omits it and one that declares it are the same document.
    """
    if has_registration_floor(canon):
        return canon
    out = dict(canon)
    out["all"] = sorted(
        list(canon.get("all", [])) + [{"field": "forecast_created_at",
                                       "operator": OP_GTE_REGISTRATION_TIME}],
        key=_predicate_sort_key)
    return out


def declared_window_end(canon: dict) -> datetime | None:
    """The pinned ceiling, read from the canonical document — never a caller.

    `None` means the author explicitly declared an unbounded window: a recorded
    choice rather than an omission.
    """
    end = canon.get("window_end")
    if end in (None, "unbounded"):
        return None
    return _parse_timestamp(end, where="population.window_end")


def field_registry_snapshot() -> dict:
    return {
        "field_registry_version": FIELD_REGISTRY_VERSION,
        "predicate_schema_version": PREDICATE_SCHEMA_VERSION,
        "fields": {n: s.to_dict() for n, s in sorted(FIELD_REGISTRY.items())},
        "operators": list(ALL_OPERATORS),
        "forbidden_examples": dict(sorted(KNOWN_FORBIDDEN_FIELDS.items())),
    }


def field_registry_digest() -> str:
    return hashlib.sha256(json.dumps(
        field_registry_snapshot(), sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


# --- evaluation against a forecast row -------------------------------------------


@dataclass
class ForecastFacts:
    """The forecast-time projection a predicate may see.

    Deliberately a narrow struct rather than the ORM row: if the evaluator
    cannot reach an outcome or a score, no predicate can accidentally depend on
    one, whatever a future edit does.
    """
    forecast_id: int
    values: dict = field(default_factory=dict)


# Three-valued logic. `UNKNOWN` is returned when the source field is NULL and
# the operator cannot decide, and it is NEVER coerced to True.
TRUE, FALSE, UNKNOWN = True, False, None


def _compare(spec: FieldSpec, op: str, actual: Any, expected: Any,
             registered_at: datetime, declared_end: datetime | None):
    """Returns True / False / UNKNOWN(None).

    **Canonical NULL policy (Gate 3): a predicate matches only when its truth
    value is explicitly TRUE.** Unknown never satisfies anything, positive or
    negative. The subtle case is negation: `forecaster_version not_eq "v2"`
    against a NULL version is UNKNOWN, not True — otherwise every row with a
    missing field silently joins the cohort through a rule the author wrote to
    *narrow* it. An author who wants missing values included must say so with
    an explicit `not_exists`, which is a declaration rather than an accident.

    Consequence, stated because it breaks a reflex: De Morgan does NOT hold
    across unknowns. `none: [x eq "a"]` is not the same population as
    `all: [x not_eq "a"]` when x can be NULL.
    """
    if op == OP_EXISTS:
        return actual is not None
    if op == OP_NOT_EXISTS:
        return actual is None
    if op == OP_GTE_REGISTRATION_TIME:
        return actual is not None and _aware(actual) >= registered_at
    if op == OP_BEFORE_DECLARED_END:
        if declared_end is None:
            return True          # no declared end -> unbounded, not excluded
        return actual is not None and _aware(actual) < declared_end
    if actual is None:
        # Every remaining operator compares a value; with no value the truth is
        # UNKNOWN, including for the negative forms.
        return UNKNOWN
    if spec.value_type == TYPE_TIMESTAMP:
        actual_cmp: Any = _aware(actual)
        expected_cmp: Any = (
            [_parse_timestamp(v, where=spec.name) for v in expected]
            if isinstance(expected, list)
            else _parse_timestamp(expected, where=spec.name))
    else:
        actual_cmp, expected_cmp = actual, expected
    if op == OP_EQ:
        return actual_cmp == expected_cmp
    if op == OP_NOT_EQ:
        return actual_cmp != expected_cmp
    if op == OP_IN:
        return actual_cmp in expected_cmp
    if op == OP_NOT_IN:
        return actual_cmp not in expected_cmp
    if op == OP_LT:
        return actual_cmp < expected_cmp
    if op == OP_LTE:
        return actual_cmp <= expected_cmp
    if op == OP_GT:
        return actual_cmp > expected_cmp
    if op == OP_GTE:
        return actual_cmp >= expected_cmp
    raise PredicateError(f"unreachable operator {op!r}")


def evaluate(canon: dict, facts: ForecastFacts, *, registered_at: datetime,
             declared_end: datetime | None = None) -> tuple[bool, list]:
    """(is_member, reasons_for_exclusion). Pure; reads only `facts`."""
    registered_at = _aware(registered_at)
    declared_end = _aware(declared_end) if declared_end else None
    reasons: list[str] = []
    for p in canon.get("all", []):
        spec = FIELD_REGISTRY[p["field"]]
        truth = _compare(spec, p["operator"], facts.values.get(p["field"]),
                         p.get("value"), registered_at, declared_end)
        # `all` requires explicit TRUE. UNKNOWN excludes.
        if truth is not TRUE:
            reasons.append(f"all:{p['field']}:{p['operator']}"
                           + (":unknown" if truth is UNKNOWN else ""))
    for p in canon.get("none", []):
        spec = FIELD_REGISTRY[p["field"]]
        truth = _compare(spec, p["operator"], facts.values.get(p["field"]),
                         p.get("value"), registered_at, declared_end)
        # `none` excludes on explicit TRUE only. UNKNOWN does NOT exclude —
        # and that asymmetry with `all` is deliberate and documented: `all` is
        # a requirement (unproven means not met) while `none` is a veto
        # (unproven means not vetoed). Both refuse to act on unknowns.
        if truth is TRUE:
            reasons.append(f"none:{p['field']}:{p['operator']}")
    return (not reasons), reasons
