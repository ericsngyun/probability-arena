"""PROSPECTIVE-EXPERIMENT-REGISTRY-001 — immutable pre-registration for research.

**Architecture: Git-backed manifests, no database.** Option 1 from the milestone,
chosen over the SQLite and hybrid options for one reason that outweighs the
convenience of runtime querying: the threat model here is *us*. The failure this
registry exists to prevent is a well-intentioned researcher quietly widening an
inclusion rule or swapping a primary metric after seeing results. A row in a
database the same process already writes to every six minutes is a weak barrier
against that. A committed file is a strong one — it cannot change without a diff,
an author and a timestamp, and the repository already treats history as
append-only.

The hybrid was rejected on the milestone's own terms: it is preferred *only* when
the projection is demonstrably non-authoritative and rebuildable, and there is
currently nothing to query that `list`/`status` cannot answer by reading a few
hundred small files. Adding a production-database coupling to buy an index we do
not need would trade real immutability for convenience. If querying ever becomes
the bottleneck, a rebuildable projection can be added then, with the manifests
still the source of truth.

Research governance only. No EV, price, side, size, order, wallet, execution or
capital surface exists here, and experiment classes that would imply one are
rejected by construction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DISCLAIMER = (
    "Research governance only — pre-registration of measurement hypotheses. "
    "Never advice, EV, a side, a size, an order, a recommendation, a wallet, or "
    "a trade action. Calibration is not executable edge."
)

REGISTRY_DIRNAME = "experiments"
MANIFEST_FILENAME = "manifest.json"
EVENTS_FILENAME = "events.jsonl"
RESULTS_DIRNAME = "results"

# --- states ---------------------------------------------------------------------
DRAFT = "draft"
REGISTERED = "registered"
COLLECTING = "collecting"
MATURED = "matured"
EVALUATED = "evaluated"
INVALIDATED = "invalidated"
RETIRED = "retired"

ALL_STATES = (DRAFT, REGISTERED, COLLECTING, MATURED, EVALUATED, INVALIDATED, RETIRED)

# Append-only: no edge returns to draft, and `registered` is the point of no
# return for every hypothesis-defining field.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    DRAFT: (REGISTERED,),
    REGISTERED: (COLLECTING, INVALIDATED),
    COLLECTING: (MATURED, INVALIDATED),
    MATURED: (EVALUATED, INVALIDATED),
    EVALUATED: (RETIRED,),
    INVALIDATED: (RETIRED,),
    RETIRED: (),
}

# --- experiment classes ---------------------------------------------------------
CLASS_PROSPECTIVE_CALIBRATION = "prospective_calibration"
CLASS_SIGNAL_SEPARATION = "prospective_signal_separation"
CLASS_FAMILY_COMPARISON = "forecast_family_comparison"
CLASS_DOMAIN_RELIABILITY = "domain_reliability"
CLASS_FEATURE_ABLATION = "feature_ablation"
CLASS_PROVIDER_EVIDENCE_ABLATION = "provider_evidence_ablation"
CLASS_LATENCY_OBSERVATION = "latency_observation"

EXPERIMENT_CLASSES = (
    CLASS_PROSPECTIVE_CALIBRATION, CLASS_SIGNAL_SEPARATION, CLASS_FAMILY_COMPARISON,
    CLASS_DOMAIN_RELIABILITY, CLASS_FEATURE_ABLATION,
    CLASS_PROVIDER_EVIDENCE_ABLATION, CLASS_LATENCY_OBSERVATION,
)

EXPLORATORY = "exploratory"
CONFIRMATORY = "confirmatory"

# --- verdicts -------------------------------------------------------------------
VERDICTS = (
    "supports_hypothesis", "does_not_support_hypothesis", "inconclusive_sample_floor",
    "invalidated_protocol_deviation", "invalidated_data_quality", "still_collecting",
)

# Vocabulary that would turn a measurement into a trading claim. Rejected in any
# free-text manifest field, not merely discouraged: the registry is the place a
# research programme decides what it is allowed to say about itself.
FORBIDDEN_VOCABULARY = (
    "profitable", "profit", "tradeable", "tradable", "buy", "sell", "edge",
    "opportunity", "alpha", "expected value", "ev ", "kelly", "position size",
    "portfolio", "order", "wallet", "execute", "execution", "pnl", "p&l",
)

# Fields whose PURPOSE is to name the forbidden capabilities. A substring scan
# cannot distinguish "we will compute expected value" from "no expected value is
# computed", and `safety_boundary` is required to contain exactly the second
# sentence. Exempting them is narrow and deliberate; exempting anything else
# would hollow out the check. Note these are still scanned for secrets.
VOCABULARY_EXEMPT_FIELDS = ("safety_boundary", "known_limitations",
                            "invalidating_conditions", "provider_policy")

# Field names that must never carry a value, so a manifest cannot smuggle a
# credential into a committed file.
SECRET_FIELD_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|credential|bearer|private[_-]?key)",
    re.IGNORECASE,
)
# Credential-shaped VALUES. Deliberately narrow. The first draft used
# `[A-Za-z0-9_-]{32,}`, which matches every long identifier in the project —
# it rejected `baseball-prospective-calibration-stability`,
# `ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR` and
# `forecast-reliability-decomposition-report` as credentials. A check that fires
# on ordinary correct input does not get tightened, it gets deleted, so it is
# scoped here to shapes real secrets actually have: known prefixes, or a long
# run of base64/hex-ish characters with NO word separators and mixed case plus
# digits. Field-NAME detection (SECRET_FIELD_PATTERN) remains the primary guard.
SECRET_VALUE_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{20,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|(?<![A-Za-z0-9])(?=[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9+/=]))"
    r"(?=[^\s]*[a-z])(?=[^\s]*[A-Z])(?=[^\s]*[0-9])[A-Za-z0-9+/=]{32,})"
)

EXPERIMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,63}$")

# --- required manifest fields ---------------------------------------------------
REQUIRED_FIELDS = (
    "experiment_id", "experiment_version", "title", "research_question",
    "hypothesis", "null_hypothesis", "exploratory_or_confirmatory",
    "experiment_class", "owner", "domain", "market_population", "forecast_family",
    "forecast_version", "feature_definitions", "signal_definitions", "data_sources",
    "provider_policy", "inclusion_rules", "exclusion_rules", "start_condition",
    "end_condition", "evaluation_horizons", "primary_metric", "secondary_metrics",
    "declared_baselines", "sample_floor", "domain_sample_floors",
    "minimum_matured_fraction", "missing_data_policy", "canceled_void_policy",
    "conflict_policy", "stale_score_policy", "multiple_testing_policy",
    "stopping_rule", "invalidating_conditions", "known_limitations",
    "safety_boundary",
)

# Set at registration by the registry, never by the author.
REGISTRY_ASSIGNED_FIELDS = (
    "registered_at", "registration_commit", "manifest_digest", "state",
    "immutable_references",
)

# Changing any of these after registration changes the hypothesis. Immutable.
HYPOTHESIS_FIELDS = (
    "experiment_id", "title", "research_question", "hypothesis", "null_hypothesis",
    "exploratory_or_confirmatory", "experiment_class", "domain", "market_population",
    "forecast_family", "forecast_version", "feature_definitions",
    "signal_definitions", "inclusion_rules", "exclusion_rules", "start_condition",
    "start_time", "end_condition", "evaluation_horizons", "primary_metric",
    "declared_baselines", "sample_floor", "domain_sample_floors",
    "minimum_matured_fraction", "multiple_testing_policy", "stopping_rule",
)

# Inclusion/exclusion language that would leak the outcome into membership.
OUTCOME_DERIVED_TOKENS = (
    "outcome", "settled", "winning_side", "resolved_probability", "brier",
    "score_status", "was_resolved", "result", "won", "lost", "correct",
    "best_performing", "best-performing", "top_performing", "highest_skill",
    "lowest_brier", "profitable",
)

# Feature language that would use information unavailable at forecast time.
FUTURE_INFORMATION_TOKENS = (
    "settlement", "settled_time", "final_price", "closing_price", "outcome",
    "post_close", "after_close", "resolution_value", "future_",
)


class ManifestError(ValueError):
    """A manifest that must not be registered."""


@dataclass
class ValidationResult:
    ok: bool
    experiment_id: str | None
    digest: str | None
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def canonical_json(manifest: dict) -> str:
    """Byte-stable serialization. Digest inputs must not depend on key order."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def compute_digest(manifest: dict) -> str:
    """SHA-256 over the canonical form, EXCLUDING registry-assigned fields.

    The digest identifies the *author's* declaration. Including
    `registered_at`/`registration_commit` would make it unverifiable from the
    manifest a reviewer reads before registration, which defeats the point of
    showing a digest for confirmation.
    """
    payload = {k: v for k, v in manifest.items()
               if k not in ("manifest_digest", "registered_at",
                            "registration_commit", "state",
                            "immutable_references")}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _walk_strings(value, path="") -> list:
    out = []
    if isinstance(value, str):
        out.append((path, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            out.extend(_walk_strings(v, f"{path}[{i}]"))
    return out


def _contains_token(text: str, tokens) -> str | None:
    low = text.lower()
    for t in tokens:
        if t in low:
            return t
    return None


def validate_manifest(manifest: dict, *, strict: bool = True) -> ValidationResult:
    """Structural + anti-p-hacking validation. Pure; touches no filesystem."""
    errors: list[str] = []
    warnings: list[str] = []
    eid = manifest.get("experiment_id")

    for f in REQUIRED_FIELDS:
        if f not in manifest or manifest[f] in (None, "", [], {}):
            errors.append(f"missing required field: {f}")

    if eid and not EXPERIMENT_ID_RE.match(str(eid)):
        errors.append(
            "experiment_id must be lowercase alphanumeric/hyphen, 3-64 chars "
            "(it becomes a directory name)")

    kind = manifest.get("exploratory_or_confirmatory")
    if kind not in (EXPLORATORY, CONFIRMATORY):
        errors.append(f"exploratory_or_confirmatory must be one of "
                      f"{EXPLORATORY!r}/{CONFIRMATORY!r}")

    cls = manifest.get("experiment_class")
    if cls is not None and cls not in EXPERIMENT_CLASSES:
        errors.append(f"experiment_class {cls!r} is not one of {EXPERIMENT_CLASSES}")

    # --- primary metric: exactly one, explicitly named -------------------------
    primary = manifest.get("primary_metric")
    if isinstance(primary, (list, tuple)):
        errors.append(
            "primary_metric must be a single named metric; multiple primaries are "
            "how a null result becomes a positive one. Move the rest to "
            "secondary_metrics and declare a multiple_testing_policy.")
    elif primary is not None and not isinstance(primary, dict):
        errors.append("primary_metric must be an object with name/definition")
    elif isinstance(primary, dict) and not primary.get("name"):
        errors.append("primary_metric.name is required")

    # --- sample floor / stopping rule / baseline -------------------------------
    floor = manifest.get("sample_floor")
    if isinstance(floor, bool) or not isinstance(floor, int):
        if floor is not None:
            errors.append("sample_floor must be an integer count")
    elif floor <= 0:
        errors.append("sample_floor must be positive")
    elif floor < 30:
        warnings.append(
            f"sample_floor {floor} is very thin; a result at this size will "
            "usually be inconclusive_sample_floor")

    if not manifest.get("stopping_rule"):
        errors.append("stopping_rule is required — an experiment with no declared "
                      "stop can always be stopped at its most favourable moment")
    if not manifest.get("declared_baselines"):
        errors.append("declared_baselines is required — a metric with no baseline "
                      "cannot support or fail to support anything")

    if kind == CONFIRMATORY:
        secondary = manifest.get("secondary_metrics") or []
        mtp = manifest.get("multiple_testing_policy")
        if len(secondary) > 1 and (not mtp or str(mtp).strip().lower() in
                                   ("", "none", "n/a")):
            errors.append(
                "confirmatory experiments testing multiple hypotheses require an "
                "explicit multiple_testing_policy")
        if not manifest.get("minimum_matured_fraction"):
            errors.append("confirmatory experiments require minimum_matured_fraction")

    # --- leakage: outcome-derived inclusion, future-information features --------
    for key in ("inclusion_rules", "exclusion_rules"):
        for path, text in _walk_strings(manifest.get(key)):
            hit = _contains_token(text, OUTCOME_DERIVED_TOKENS)
            if hit:
                errors.append(
                    f"{key}{('.' + path) if path else ''} references {hit!r}: "
                    "membership must not depend on the outcome or on which cohort "
                    "performed best")
    for path, text in _walk_strings(manifest.get("feature_definitions")):
        hit = _contains_token(text, FUTURE_INFORMATION_TOKENS)
        if hit:
            errors.append(
                f"feature_definitions.{path} references {hit!r}: features must be "
                "computable from information available at forecast time")

    # --- vocabulary and secrets ------------------------------------------------
    for path, text in _walk_strings(manifest):
        root_field = path.split(".")[0].split("[")[0]
        hit = (None if root_field in VOCABULARY_EXEMPT_FIELDS
               else _contains_token(text, FORBIDDEN_VOCABULARY))
        if hit:
            errors.append(
                f"{path} uses trading vocabulary {hit!r}; this registry governs "
                "measurement only and may not describe results as executable")
        if SECRET_VALUE_PATTERN.search(text):
            errors.append(f"{path} looks like a credential; manifests are committed")
    for path, _ in _walk_strings(manifest):
        leaf = path.split(".")[-1].split("[")[0]
        if SECRET_FIELD_PATTERN.search(leaf):
            errors.append(f"{path} is a secret-bearing field name")

    # --- start condition must be future-facing ---------------------------------
    if not manifest.get("start_condition"):
        errors.append("start_condition is required and must be prospective")
    st = manifest.get("start_time")
    if st:
        try:
            parsed = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if strict and parsed < datetime.now(timezone.utc):
                errors.append(
                    f"start_time {st} is in the past; a prospective experiment "
                    "cannot admit forecasts that already exist")
        except ValueError:
            errors.append(f"start_time {st!r} is not ISO-8601")

    # --- non-blocking risk flags ----------------------------------------------
    dom = manifest.get("domain")
    if isinstance(dom, str) and dom.lower() in ("sports_tennis", "politics"):
        warnings.append(
            f"domain {dom} has a small arrival rate and, for tennis, the worst "
            "recorded calibration; expect a long collection window")
    if manifest.get("depends_on_unresolved_provider_coverage"):
        warnings.append("declares dependency on unresolved provider coverage")

    digest = compute_digest(manifest) if not errors else None
    return ValidationResult(ok=not errors, experiment_id=eid, digest=digest,
                            errors=errors, warnings=warnings)


# --- filesystem layer -----------------------------------------------------------


# Code whose behaviour defines what a metric MEANS. If any of these change, a
# later evaluation is not measuring the same thing the manifest declared, even
# though every manifest field is byte-identical. Hashing them is what makes
# "reconstruct membership and metrics later" a checkable claim rather than an
# aspiration.
EVALUATION_CODE_FILES = (
    "app/services/forecast_reliability.py",
    "app/services/calibration.py",
    "app/services/forecast_scorability.py",
    "app/services/outcome_coverage.py",
)
CANON_FILES = (
    "docs/PROJECT_CANON.md",
    "docs/SAFETY_BOUNDARIES.md",
)


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def capture_immutable_references(
    manifest: dict, *, repo_root: Path | None = None, commit: str | None = None
) -> dict:
    """Deterministic references pinning what the declaration MEANS.

    Manifest text alone is not enough to reconstruct an experiment: the same
    words evaluated against different scoring code produce different numbers.
    This records content hashes of the definition sub-objects AND of the code
    that will evaluate them, so a later reader can prove whether the ground
    moved underneath a result.

    Deliberately hashes definitions and code only — never production data. The
    point is to preserve the RULES needed to rebuild membership, not a frozen
    copy of the rows, which would be both enormous and immediately stale.
    """
    root = repo_root or Path.cwd()

    def sub(name):
        value = manifest.get(name)
        return hashlib.sha256(
            canonical_json({name: value}).encode("utf-8")).hexdigest()

    return {
        "population_definition_digest": sub("market_population"),
        "inclusion_logic_digest": sub("inclusion_rules"),
        "exclusion_logic_digest": sub("exclusion_rules"),
        "feature_configuration_digest": sub("feature_definitions"),
        "signal_configuration_digest": sub("signal_definitions"),
        "baseline_definition_digest": sub("declared_baselines"),
        "primary_metric_digest": sub("primary_metric"),
        "forecast_family": manifest.get("forecast_family"),
        "forecast_version": manifest.get("forecast_version"),
        "repository_commit": commit,
        "evaluation_code_digests": {
            f: _file_digest(root / f) for f in EVALUATION_CODE_FILES},
        "canon_digests": {f: _file_digest(root / f) for f in CANON_FILES},
    }


def registry_root(base: Path | None = None) -> Path:
    return (base or Path.cwd()) / REGISTRY_DIRNAME


def experiment_dir(experiment_id: str, base: Path | None = None) -> Path:
    """Path confinement. `experiment_id` is validated to a strict charset that
    cannot express `..`, a separator or an absolute path, and the result is
    re-checked against the root — a two-layer guard, because a path escape here
    writes attacker-chosen content into a committed tree."""
    if not EXPERIMENT_ID_RE.match(str(experiment_id)):
        raise ManifestError(f"invalid experiment_id: {experiment_id!r}")
    root = registry_root(base).resolve()
    target = (root / str(experiment_id)).resolve()
    if target.parent != root:
        raise ManifestError(f"path escape rejected: {experiment_id!r}")
    return target


def load_manifest(path: Path) -> dict:
    text = Path(path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    return data


def read_events(experiment_id: str, base: Path | None = None) -> list:
    path = experiment_dir(experiment_id, base) / EVENTS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def current_state(experiment_id: str, base: Path | None = None) -> str | None:
    """Reconstructed from the event log, never read from a mutable field.

    The manifest carries a `state` for convenience; the events are the truth. If
    they ever disagree, the events win, because they are append-only.
    """
    events = read_events(experiment_id, base)
    state = None
    for e in events:
        if e.get("event") == "registered":
            state = REGISTERED
        elif e.get("event") == "transition":
            state = e.get("to")
    return state


def append_event(experiment_id: str, event: dict, base: Path | None = None) -> None:
    d = experiment_dir(experiment_id, base)
    d.mkdir(parents=True, exist_ok=True)
    with (d / EVENTS_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(canonical_json(event) + "\n")


def register(
    manifest: dict, *, base: Path | None = None, confirm: bool = False,
    commit: str | None = None, now: datetime | None = None,
) -> dict:
    """Validate and (with `confirm`) write the immutable manifest + first event."""
    now = now or datetime.now(timezone.utc)
    result = validate_manifest(manifest)
    if not result.ok:
        raise ManifestError("; ".join(result.errors))

    eid = manifest["experiment_id"]
    d = experiment_dir(eid, base)
    already = (d / MANIFEST_FILENAME).exists()
    if already and confirm:
        raise ManifestError(
            f"experiment_id {eid!r} is already registered; identities are never "
            "reused and a registered manifest is never overwritten")

    out = {
        "mode": "confirmed" if confirm else "dry_run",
        "experiment_id": eid,
        "manifest_digest": result.digest,
        "state": REGISTERED if confirm else DRAFT,
        "registered_at": now.isoformat() if confirm else None,
        "registration_commit": commit if confirm else None,
        "persisted": False,
        "warnings": result.warnings,
        "disclaimer": DISCLAIMER,
    }
    if not confirm:
        out["would_write"] = [str(d / MANIFEST_FILENAME), str(d / EVENTS_FILENAME)]
        return out

    stored = dict(manifest)
    stored["immutable_references"] = capture_immutable_references(
        manifest, commit=commit)
    stored["manifest_digest"] = result.digest
    stored["registered_at"] = now.isoformat()
    stored["registration_commit"] = commit
    stored["state"] = REGISTERED
    d.mkdir(parents=True, exist_ok=True)
    (d / MANIFEST_FILENAME).write_text(
        json.dumps(stored, indent=2, sort_keys=True) + "\n")
    (d / RESULTS_DIRNAME).mkdir(exist_ok=True)
    append_event(eid, {
        "event": "registered", "at": now.isoformat(), "state": REGISTERED,
        "manifest_digest": result.digest, "registration_commit": commit,
    }, base)
    out["persisted"] = True
    return out


def verify_immutability(experiment_id: str, base: Path | None = None) -> dict:
    """Does the stored manifest still match the digest recorded at registration?

    This is the whole governance guarantee reduced to one comparison. It catches
    a hypothesis field edited in place after results appeared — which is the
    single most damaging thing anyone could do to this registry, and the easiest
    to do by accident.
    """
    d = experiment_dir(experiment_id, base)
    manifest = load_manifest(d / MANIFEST_FILENAME)
    recorded = manifest.get("manifest_digest")
    recomputed = compute_digest(manifest)
    events = read_events(experiment_id, base)
    registered = next((e for e in events if e.get("event") == "registered"), None)
    at_registration = registered.get("manifest_digest") if registered else None
    return {
        "experiment_id": experiment_id,
        "digest_at_registration": at_registration,
        "digest_recorded_in_manifest": recorded,
        "digest_recomputed_now": recomputed,
        "intact": bool(at_registration)
        and at_registration == recorded == recomputed,
    }


def transition(
    experiment_id: str, to_state: str, *, base: Path | None = None,
    confirm: bool = False, now: datetime | None = None, note: str | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    if to_state not in ALL_STATES:
        raise ManifestError(f"unknown state {to_state!r}")
    state = current_state(experiment_id, base)
    if state is None:
        raise ManifestError(f"{experiment_id!r} is not registered")
    allowed = TRANSITIONS.get(state, ())
    if to_state not in allowed:
        raise ManifestError(
            f"{state!r} -> {to_state!r} is not permitted (allowed: {allowed or 'none'})")

    integrity = verify_immutability(experiment_id, base)
    if not integrity["intact"]:
        raise ManifestError(
            f"manifest digest mismatch for {experiment_id!r}; refusing to advance "
            "an experiment whose declaration has changed since registration")

    out = {
        "mode": "confirmed" if confirm else "dry_run",
        "experiment_id": experiment_id, "from": state, "to": to_state,
        "persisted": False, "integrity": integrity, "disclaimer": DISCLAIMER,
    }
    if confirm:
        append_event(experiment_id, {
            "event": "transition", "at": now.isoformat(), "from": state,
            "to": to_state, "note": note,
        }, base)
        out["persisted"] = True
    return out


def list_experiments(base: Path | None = None) -> list:
    root = registry_root(base)
    if not root.exists():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        mf = d / MANIFEST_FILENAME
        if not mf.exists():
            continue
        m = load_manifest(mf)
        out.append({
            "experiment_id": m.get("experiment_id", d.name),
            "title": m.get("title"),
            "class": m.get("experiment_class"),
            "kind": m.get("exploratory_or_confirmatory"),
            "domain": m.get("domain"),
            "state": current_state(d.name, base),
            "registered_at": m.get("registered_at"),
            "digest": m.get("manifest_digest"),
            "intact": verify_immutability(d.name, base)["intact"],
        })
    return out


def _evaluation_code_drift(manifest: dict, repo_root: Path | None = None) -> dict:
    """Has the evaluation code changed since registration?

    Not an error — code improves, and the directional fix in this very milestone
    is an example of a change everyone wants. But a result computed after a
    scoring change is not directly comparable with the declaration that preceded
    it, and that has to be visible rather than discovered later.
    """
    refs = manifest.get("immutable_references") or {}
    recorded = refs.get("evaluation_code_digests") or {}
    root = repo_root or Path.cwd()
    changed = []
    for f, digest in recorded.items():
        now = _file_digest(root / f)
        if digest is not None and now is not None and now != digest:
            changed.append(f)
    return {"changed_files": changed, "drifted": bool(changed)}


def status(experiment_id: str, base: Path | None = None) -> dict:
    d = experiment_dir(experiment_id, base)
    manifest = load_manifest(d / MANIFEST_FILENAME)
    state = current_state(experiment_id, base)
    results = sorted(p.name for p in (d / RESULTS_DIRNAME).glob("*.json")) \
        if (d / RESULTS_DIRNAME).exists() else []
    return {
        "experiment_id": experiment_id,
        "state": state,
        "allowed_transitions": list(TRANSITIONS.get(state or DRAFT, ())),
        "integrity": verify_immutability(experiment_id, base),
        "registered_at": manifest.get("registered_at"),
        "registration_commit": manifest.get("registration_commit"),
        "kind": manifest.get("exploratory_or_confirmatory"),
        "class": manifest.get("experiment_class"),
        "domain": manifest.get("domain"),
        "primary_metric": (manifest.get("primary_metric") or {}).get("name"),
        "sample_floor": manifest.get("sample_floor"),
        "start_time": manifest.get("start_time"),
        "events": len(read_events(experiment_id, base)),
        "results": results,
        "immutable_references": manifest.get("immutable_references"),
        "evaluation_code_drift": _evaluation_code_drift(manifest),
        "evaluation_permitted": state in (MATURED, EVALUATED),
        "disclaimer": DISCLAIMER,
    }
