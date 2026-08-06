"""PROSPECTIVE-EXPERIMENT-REGISTRY-002C — identifier-universe authority.

002B let a confirmatory experiment enumerate `market_ticker` if the manifest
carried a `universe` block with five truthy keys. A review defeated that in one
line: a fabricated block declaring `selection_method: "hand picked after looking
at results"` passed, because "results" was not in the banned substring list and
the digest was never resolved to anything.

That is the prose-blocklist mistake again, one layer down. Presence of a field is
not authority. A universe that authorizes a confirmatory cohort has to be a real,
separately committed artifact whose digest resolves, whose creation predates
registration, and whose selection method is a **typed enum** rather than a
sentence — because a sentence can always be written to sound innocent.

Provider-free, read-only, no database. No EV, price, order, wallet, execution or
capital surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

UNIVERSE_DIRNAME = "universes"
UNIVERSE_SCHEMA_VERSION = 1
MAX_UNIVERSE_MEMBERS = 5000

# Typed selection methods. A closed enum, not prose, and every member is a rule
# that can be checked against the world rather than a claim about intent. Adding
# one is a deliberate act with a review attached; writing a new sentence is not.
SELECTION_METHODS = {
    "exhaustive_series": (
        "every market in a named Kalshi series, taken whole. The series name is "
        "the rule; nothing is chosen market by market."),
    "exhaustive_event": (
        "every market in a named Kalshi event, taken whole."),
    "scheduled_fixtures": (
        "markets corresponding to a fixture list published by an external body "
        "before the universe was created."),
    "random_sample_seeded": (
        "a seeded random draw from a stated frame; the seed and frame are "
        "recorded so the draw can be reproduced."),
}

# Methods whose members could have been picked one at a time by a human eye.
# None are currently allowed for confirmatory work; the constant exists so the
# distinction is explicit rather than implied by omission.
HAND_SELECTED_METHODS: tuple = ()

_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


class UniverseError(ValueError):
    """A universe artifact that must not authorize a cohort."""


@dataclass
class UniverseValidation:
    ok: bool
    universe_id: str | None
    digest: str | None
    member_count: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def canonical_universe_json(doc: dict) -> str:
    payload = {k: v for k, v in doc.items() if k != "digest"}
    if "members" in payload:
        # Order must not change identity: the same set of markets is the same
        # universe however the author happened to list them.
        payload["members"] = sorted(set(payload["members"]))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def universe_digest(doc: dict) -> str:
    return hashlib.sha256(
        canonical_universe_json(doc).encode("utf-8")).hexdigest()


def universe_path(universe_id: str, base: Path | None = None) -> Path:
    """Path confinement, same two-layer guard the registry uses."""
    if not re.match(r"^[a-z0-9][a-z0-9\-]{2,63}$", str(universe_id)):
        raise UniverseError(f"invalid universe_id: {universe_id!r}")
    root = ((base or Path.cwd()) / UNIVERSE_DIRNAME).resolve()
    target = (root / f"{universe_id}.json").resolve()
    if target.parent != root:
        raise UniverseError(f"path escape rejected: {universe_id!r}")
    return target


def validate_universe(doc: dict) -> UniverseValidation:
    """Structural validation of a universe artifact. Pure."""
    errors: list[str] = []
    warnings: list[str] = []
    uid = doc.get("universe_id")

    if doc.get("schema_version") != UNIVERSE_SCHEMA_VERSION:
        errors.append(
            f"universe.schema_version must be {UNIVERSE_SCHEMA_VERSION}")
    if not uid or not re.match(r"^[a-z0-9][a-z0-9\-]{2,63}$", str(uid)):
        errors.append("universe_id must be lowercase alphanumeric/hyphen, 3-64")

    method = doc.get("selection_method")
    if method not in SELECTION_METHODS:
        errors.append(
            f"selection_method {method!r} is not one of the typed methods "
            f"{sorted(SELECTION_METHODS)}. Free-form prose cannot authorize a "
            "confirmatory cohort — a sentence can always be written to sound "
            "innocent, which is exactly how the previous check was defeated.")
    if method in HAND_SELECTED_METHODS:
        errors.append(
            f"selection_method {method!r} permits market-by-market choice and "
            "may not authorize a confirmatory cohort")

    src = doc.get("selection_source")
    if not isinstance(src, dict) or not src.get("kind"):
        errors.append(
            "selection_source is required and must record WHERE the members "
            "came from (e.g. {'kind': 'kalshi_series', 'series_ticker': 'KXMLB'})")
    elif method == "random_sample_seeded" and not (
            src.get("seed") is not None and src.get("frame")):
        errors.append("random_sample_seeded requires selection_source.seed and .frame")

    created = doc.get("created_at")
    if not created:
        errors.append("created_at is required")
    else:
        try:
            parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                errors.append("created_at must carry an explicit UTC offset")
        except ValueError:
            errors.append(f"created_at {created!r} is not ISO-8601")

    members = doc.get("members")
    if not isinstance(members, list) or not members:
        errors.append("members must be a non-empty list of market identifiers")
    else:
        if len(members) > MAX_UNIVERSE_MEMBERS:
            errors.append(f"at most {MAX_UNIVERSE_MEMBERS} members")
        bad = [m for m in members if not isinstance(m, str)
               or not _TICKER_RE.match(m)]
        if bad:
            errors.append(f"{len(bad)} member(s) are not valid identifiers")
        if len(set(members)) != len(members):
            warnings.append("members contains duplicates; they are de-duplicated "
                            "for the digest")
        declared = doc.get("member_count")
        if declared is not None and declared != len(set(members)):
            errors.append(
                f"member_count {declared} disagrees with {len(set(members))} "
                "distinct members — the count is not an independent assertion")

    if doc.get("rationale") and len(json.dumps(doc["rationale"],
                                               default=str)) > 4000:
        errors.append("rationale exceeds 4000 characters")

    digest = universe_digest(doc) if not errors else None
    return UniverseValidation(
        ok=not errors, universe_id=uid, digest=digest,
        member_count=len(set(members)) if isinstance(members, list) else 0,
        errors=errors, warnings=warnings)


def resolve_universe(reference: dict, *, base: Path | None = None,
                     registered_at: datetime | None = None) -> dict:
    """Resolve a manifest's universe reference to the committed artifact.

    This is the part 002B did not do. The reference names a file and a digest;
    both must exist, the digest must recompute over the artifact's real content,
    and the artifact must predate registration. A reference that merely *looks*
    complete authorizes nothing.
    """
    uid = reference.get("universe_id")
    path = universe_path(uid, base)
    if not path.exists():
        raise UniverseError(
            f"universe {uid!r} is referenced but no committed artifact exists at "
            f"{UNIVERSE_DIRNAME}/{uid}.json")
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise UniverseError(f"universe {uid!r} is not valid JSON: {exc}") from exc

    v = validate_universe(doc)
    if not v.ok:
        raise UniverseError(f"universe {uid!r} is invalid: {'; '.join(v.errors)}")

    if reference.get("digest") != v.digest:
        raise UniverseError(
            f"universe {uid!r} digest mismatch: the manifest references "
            f"{reference.get('digest')} but the committed artifact hashes to "
            f"{v.digest}. The universe changed, or was never the one referenced.")
    if reference.get("member_count") is not None and \
            reference["member_count"] != v.member_count:
        raise UniverseError(
            f"universe {uid!r} member_count mismatch: manifest says "
            f"{reference['member_count']}, artifact has {v.member_count}")

    created = datetime.fromisoformat(
        str(doc["created_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    if registered_at is not None:
        reg = (registered_at if registered_at.tzinfo
               else registered_at.replace(tzinfo=timezone.utc))
        if created > reg:
            raise UniverseError(
                f"universe {uid!r} was created at {created.isoformat()}, AFTER "
                f"registration at {reg.isoformat()} — a cohort defined after the "
                "experiment began is not a pre-registered cohort")

    return {"universe_id": uid, "digest": v.digest,
            "member_count": v.member_count,
            "selection_method": doc["selection_method"],
            "selection_source": doc["selection_source"],
            "created_at": doc["created_at"], "members": sorted(set(doc["members"]))}


def check_universe_covers(members_used: list, resolved: dict) -> list:
    """Every enumerated ticker must be in the resolved universe.

    Otherwise the universe is decoration: an author could reference a legitimate
    artifact and then enumerate a hand-picked subset beside it.
    """
    allowed = set(resolved["members"])
    outside = sorted(t for t in members_used if t not in allowed)
    if outside:
        return [f"predicate enumerates {len(outside)} ticker(s) outside the "
                f"resolved universe {resolved['universe_id']!r}: {outside[:5]}"]
    return []
