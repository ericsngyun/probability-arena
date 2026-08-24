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

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.microstructure.panel import DatasetRole

#: Where the authorization must live. Overridable ONLY for tests -- production
#: callers never pass a path, so a stray env var cannot silently unlock a real
#: evaluation on someone else's machine.
AUTH_ENV = "PROBABILITY_ARENA_EDGE001_AUTHORIZATION"
DEFAULT_AUTH_PATH = Path.home() / ".probability-arena" / "EDGE001-FINAL-EVALUATION.json"

REQUIRED_FIELDS = ("milestone", "operator", "authorized_at_utc",
                   "sessions_complete", "statement")

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
    return Authorization(str(d["operator"]), str(d["authorized_at_utc"]),
                         int(d["sessions_complete"]), str(d["statement"]))


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
