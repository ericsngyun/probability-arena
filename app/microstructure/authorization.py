"""The lock that keeps confirmation data unreadable while the evaluator is built.

`MARKET-MICROSTRUCTURE-EDGE-001` is a preregistered experiment whose value
depends entirely on the evaluator being fixed before its outcomes are seen. The
discipline is therefore enforced in code rather than remembered: any attempt to
load `dataset_role=CONFIRMATION` rows raises unless an explicit, dated,
operator-signed authorization exists on disk.

This is deliberately awkward. Writing the authorization file is a decision, and
it should feel like one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.microstructure.panel import DatasetRole

REPO = Path(__file__).resolve().parents[2]

#: Every module whose contents define the statistical test. A change to any of
#: them changes the fingerprint and invalidates an existing authorization.
#: This is the point: a "harmless refactor" during the two weeks of capture
#: must not be able to alter the test before the corpus is opened.
FROZEN_EVALUATOR_FILES = (
    # the lock itself is fingerprinted: a weakened guard must invalidate an
    # authorization, exactly as a changed statistical test does
    "app/microstructure/authorization.py",
    "app/microstructure/evaluate.py",
    "app/microstructure/features.py",
    "app/microstructure/labels.py",
    "app/microstructure/rows.py",
    "app/microstructure/panel.py",
    "app/microstructure/linalg.py",
)

#: The documents the test is preregistered in.
FROZEN_PREREGISTRATION_FILES = (
    "docs/experiments/MARKET-MICROSTRUCTURE-EDGE-001.md",
    "docs/experiments/MARKET-MICROSTRUCTURE-EDGE-001-CAPTURE-PLAN.md",
    "docs/milestones/MARKET-STATE-FABRIC-v1.md",
)


def _fingerprint(paths) -> str:
    """sha256 over (relative path, bytes) for each file, in fixed order."""
    h = hashlib.sha256()
    for rel in paths:
        f = REPO / rel
        if not f.exists():
            raise ConfirmationDataLocked(
                f"frozen file {rel} is missing; the evaluator cannot be "
                f"fingerprinted and confirmation data stays locked")
        h.update(rel.encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def evaluator_fingerprint() -> str:
    return _fingerprint(FROZEN_EVALUATOR_FILES)


def preregistration_fingerprint() -> str:
    return _fingerprint(FROZEN_PREREGISTRATION_FILES)

#: Where the authorization must live. Overridable ONLY for tests -- production
#: callers never pass a path, so a stray env var cannot silently unlock a real
#: evaluation on someone else's machine.
AUTH_ENV = "PROBABILITY_ARENA_EDGE001_AUTHORIZATION"
DEFAULT_AUTH_PATH = Path.home() / ".probability-arena" / "EDGE001-FINAL-EVALUATION.json"

REQUIRED_FIELDS = ("milestone", "operator", "authorized_at_utc",
                   "sessions_complete", "statement",
                   "evaluator_fingerprint", "preregistration_fingerprint",
                   "expected_row_schema", "expected_label_schema")

EXPECTED_MILESTONE = "MARKET-MICROSTRUCTURE-EDGE-001"
REQUIRED_SESSIONS = 20

#: The operator must type this exactly. A checkbox is not a decision.
REQUIRED_STATEMENT = (
    "I authorize the final frozen evaluation of MARKET-MICROSTRUCTURE-EDGE-001. "
    "The evaluator is frozen and no confirmation outcome has been inspected.")


class ConfirmationDataLocked(RuntimeError):
    """Raised when confirmation rows are touched without authorization."""


@dataclass(frozen=True)
class Authorization:
    operator: str
    authorized_at_utc: str
    sessions_complete: int
    statement: str
    evaluator_fingerprint: str
    preregistration_fingerprint: str


def _auth_path() -> Path:
    override = os.environ.get(AUTH_ENV)
    return Path(override) if override else DEFAULT_AUTH_PATH


def load_authorization() -> Authorization | None:
    """The authorization, or None. Malformed is treated as absent-and-loud."""
    p = _auth_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception as exc:
        raise ConfirmationDataLocked(
            f"authorization at {p} is unreadable ({exc}); refusing to guess") from exc
    missing = [f for f in REQUIRED_FIELDS if f not in d]
    if missing:
        raise ConfirmationDataLocked(
            f"authorization at {p} is missing {missing}; refusing to proceed")
    if d["milestone"] != EXPECTED_MILESTONE:
        raise ConfirmationDataLocked(
            f"authorization names {d['milestone']!r}, not {EXPECTED_MILESTONE!r}")
    if d["statement"] != REQUIRED_STATEMENT:
        raise ConfirmationDataLocked(
            "authorization statement does not match the required text verbatim; "
            "the operator must type it, not approximate it")
    if int(d["sessions_complete"]) < REQUIRED_SESSIONS:
        raise ConfirmationDataLocked(
            f"authorization claims {d['sessions_complete']} complete sessions, "
            f"but the tranche requires {REQUIRED_SESSIONS}")

    # VERSION BINDING. An authorization is for one exact evaluator against one
    # exact preregistration. If either has changed since it was signed, the
    # test being run is not the test that was authorised.
    actual_ev, actual_pre = evaluator_fingerprint(), preregistration_fingerprint()
    if d["evaluator_fingerprint"] != actual_ev:
        raise ConfirmationDataLocked(
            "the running evaluator does not match the authorised one.\n"
            f"  authorised: {d['evaluator_fingerprint']}\n"
            f"  running:    {actual_ev}\n"
            "One of the frozen modules changed after the authorisation was "
            "signed. Re-authorise deliberately, or restore the frozen code.")
    if d["preregistration_fingerprint"] != actual_pre:
        raise ConfirmationDataLocked(
            "the preregistration does not match the authorised one.\n"
            f"  authorised: {d['preregistration_fingerprint']}\n"
            f"  running:    {actual_pre}\n"
            "The documents defining this test changed after authorisation.")
    from app.microstructure.rows import (LABEL_SCHEMA_VERSION,
                                         ROW_SCHEMA_VERSION)
    if (d["expected_row_schema"] != ROW_SCHEMA_VERSION
            or d["expected_label_schema"] != LABEL_SCHEMA_VERSION):
        raise ConfirmationDataLocked(
            f"authorization expects schema "
            f"{d['expected_row_schema']}/{d['expected_label_schema']}, "
            f"running {ROW_SCHEMA_VERSION}/{LABEL_SCHEMA_VERSION}")
    return Authorization(str(d["operator"]), str(d["authorized_at_utc"]),
                         int(d["sessions_complete"]), str(d["statement"]),
                         str(d["evaluator_fingerprint"]),
                         str(d["preregistration_fingerprint"]))


def require_readable(dataset_role: str, *, context: str = "") -> None:
    """Gate every read of research rows. Confirmation needs authorization.

    `PROFILE` and `VALIDATION` are always readable -- they exist to be looked
    at. `CONFIRMATION` is readable only under a valid authorization, and the
    error says exactly what to do rather than merely refusing.
    """
    if dataset_role not in DatasetRole.ALL:
        raise ValueError(f"unknown dataset_role {dataset_role!r}")
    if dataset_role != DatasetRole.CONFIRMATION:
        return
    if load_authorization() is None:
        raise ConfirmationDataLocked(
            f"CONFIRMATION rows are locked{' for ' + context if context else ''}.\n"
            f"The evaluator is developed against synthetic and VALIDATION data "
            f"only, so that it is frozen before any outcome is seen.\n"
            f"To unlock, write {_auth_path()} containing "
            f"{list(REQUIRED_FIELDS)} with the statement typed verbatim.")


def authorization_banner() -> str:
    a = load_authorization()
    if a is None:
        return "CONFIRMATION DATA: LOCKED (evaluator development mode)"
    return (f"CONFIRMATION DATA: UNLOCKED by {a.operator} at "
            f"{a.authorized_at_utc} ({a.sessions_complete} sessions)")


# ---------------------------------------------------------------------------
# TTE-HETEROGENEITY-001 — an INDEPENDENT lock for the secondary family.
#
# Deliberately separate constants rather than a parameterised shared lock:
# unlocking the secondary analysis must not, by any code path, unlock the
# primary one. Two locks that share nothing but a hashing helper cannot be
# opened with one key by accident.
# ---------------------------------------------------------------------------
TTE_AUTH_ENV = "PROBABILITY_ARENA_TTE001_AUTHORIZATION"
TTE_DEFAULT_AUTH_PATH = (Path.home() / ".probability-arena"
                         / "TTE001-FINAL-EVALUATION.json")
TTE_MILESTONE = "MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001"

TTE_FROZEN_FILES = (
    "app/microstructure/authorization.py",
    "app/microstructure/tte_heterogeneity.py",
    "app/microstructure/features.py",
    "app/microstructure/labels.py",
    "app/microstructure/rows.py",
    "app/microstructure/panel.py",
    "app/microstructure/linalg.py",
)
TTE_PREREGISTRATION_FILES = (
    "docs/experiments/MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001.md",
)
TTE_REQUIRED_STATEMENT = (
    "I authorize the final frozen evaluation of "
    "MARKET-MICROSTRUCTURE-TTE-HETEROGENEITY-001. This is a SECONDARY analysis "
    "that cannot rescue a failed EDGE-001 primary result.")


def tte_evaluator_fingerprint() -> str:
    return _fingerprint(TTE_FROZEN_FILES)


def tte_preregistration_fingerprint() -> str:
    return _fingerprint(TTE_PREREGISTRATION_FILES)


def _tte_auth_path() -> Path:
    override = os.environ.get(TTE_AUTH_ENV)
    return Path(override) if override else TTE_DEFAULT_AUTH_PATH


def load_tte_authorization() -> Authorization | None:
    p = _tte_auth_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception as exc:
        raise ConfirmationDataLocked(
            f"TTE authorization at {p} is unreadable ({exc})") from exc
    missing = [f for f in REQUIRED_FIELDS if f not in d]
    if missing:
        raise ConfirmationDataLocked(
            f"TTE authorization at {p} is missing {missing}")
    if d["milestone"] != TTE_MILESTONE:
        raise ConfirmationDataLocked(
            f"TTE authorization names {d['milestone']!r}, not {TTE_MILESTONE!r}; "
            "an EDGE-001 authorization can never unlock the secondary family")
    if d["statement"] != TTE_REQUIRED_STATEMENT:
        raise ConfirmationDataLocked(
            "TTE authorization statement does not match the required text "
            "verbatim; it must acknowledge the analysis is secondary")
    if int(d["sessions_complete"]) < REQUIRED_SESSIONS:
        raise ConfirmationDataLocked(
            f"TTE authorization claims {d['sessions_complete']} sessions, "
            f"requires {REQUIRED_SESSIONS}")
    ev, pre = tte_evaluator_fingerprint(), tte_preregistration_fingerprint()
    if d["evaluator_fingerprint"] != ev:
        raise ConfirmationDataLocked(
            f"the running TTE evaluator does not match the authorised one.\n"
            f"  authorised: {d['evaluator_fingerprint']}\n  running:    {ev}")
    if d["preregistration_fingerprint"] != pre:
        raise ConfirmationDataLocked(
            f"the TTE preregistration does not match the authorised one.\n"
            f"  authorised: {d['preregistration_fingerprint']}\n  running:    {pre}")
    from app.microstructure.rows import LABEL_SCHEMA_VERSION, ROW_SCHEMA_VERSION
    if (d["expected_row_schema"] != ROW_SCHEMA_VERSION
            or d["expected_label_schema"] != LABEL_SCHEMA_VERSION):
        raise ConfirmationDataLocked("TTE authorization expects a foreign schema")
    return Authorization(str(d["operator"]), str(d["authorized_at_utc"]),
                         int(d["sessions_complete"]), str(d["statement"]),
                         str(d["evaluator_fingerprint"]),
                         str(d["preregistration_fingerprint"]))


def require_tte_readable(dataset_role: str, *, context: str = "") -> None:
    """Gate for the SECONDARY family. Independent of the primary lock."""
    if dataset_role not in DatasetRole.ALL:
        raise ValueError(f"unknown dataset_role {dataset_role!r}")
    if dataset_role != DatasetRole.CONFIRMATION:
        return
    if load_tte_authorization() is None:
        raise ConfirmationDataLocked(
            f"CONFIRMATION rows are locked for the TTE-HETEROGENEITY secondary "
            f"family{' (' + context + ')' if context else ''}.\n"
            f"An EDGE-001 authorization does NOT unlock this analysis; write "
            f"{_tte_auth_path()} with the secondary statement typed verbatim.")
