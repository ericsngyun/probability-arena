"""The confirmation-data lock: preregistration discipline enforced in code.

The evaluator's value depends on being frozen before any outcome is seen, so
the rule is not "remember not to look" -- it is that looking raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.microstructure import authorization as A
from app.microstructure.panel import DatasetRole


@pytest.fixture
def auth_path(tmp_path, monkeypatch):
    p = tmp_path / "EDGE001-FINAL-EVALUATION.json"
    monkeypatch.setenv(A.AUTH_ENV, str(p))
    return p


def _valid(**over):
    d = {"milestone": A.EXPECTED_MILESTONE, "operator": "eric",
         "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
         "sessions_complete": 20, "statement": A.REQUIRED_STATEMENT}
    d.update(over)
    return d


# --- the default posture is LOCKED -----------------------------------------

def test_confirmation_is_locked_when_no_authorization_exists(auth_path):
    assert not auth_path.exists()
    with pytest.raises(A.ConfirmationDataLocked, match="locked"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_profile_and_validation_are_always_readable(auth_path):
    A.require_readable(DatasetRole.PROFILE)
    A.require_readable(DatasetRole.VALIDATION)


def test_unknown_role_is_refused_rather_than_waved_through(auth_path):
    with pytest.raises(ValueError, match="unknown dataset_role"):
        A.require_readable("probably_fine")


# --- what a valid authorization must contain --------------------------------

def test_a_complete_authorization_unlocks(auth_path):
    auth_path.write_text(json.dumps(_valid()))
    A.require_readable(DatasetRole.CONFIRMATION)
    assert "UNLOCKED by eric" in A.authorization_banner()


@pytest.mark.parametrize("field", A.REQUIRED_FIELDS)
def test_every_required_field_is_actually_required(auth_path, field):
    d = _valid(); d.pop(field)
    auth_path.write_text(json.dumps(d))
    with pytest.raises(A.ConfirmationDataLocked, match="missing"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_statement_must_be_typed_verbatim(auth_path):
    auth_path.write_text(json.dumps(_valid(statement="yeah go ahead")))
    with pytest.raises(A.ConfirmationDataLocked, match="verbatim"):
        A.require_readable(DatasetRole.CONFIRMATION)
    # even a near-miss fails -- a checkbox is not a decision
    auth_path.write_text(json.dumps(
        _valid(statement=A.REQUIRED_STATEMENT.replace("frozen", "Frozen"))))
    with pytest.raises(A.ConfirmationDataLocked, match="verbatim"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_wrong_milestone_does_not_unlock_this_one(auth_path):
    auth_path.write_text(json.dumps(_valid(milestone="SOME-OTHER-001")))
    with pytest.raises(A.ConfirmationDataLocked, match="names"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_an_incomplete_tranche_cannot_authorize(auth_path):
    auth_path.write_text(json.dumps(_valid(sessions_complete=19)))
    with pytest.raises(A.ConfirmationDataLocked, match="requires"):
        A.require_readable(DatasetRole.CONFIRMATION)
    auth_path.write_text(json.dumps(_valid(sessions_complete=20)))
    A.require_readable(DatasetRole.CONFIRMATION)


def test_malformed_authorization_fails_loud_not_open(auth_path):
    """A corrupt file must not read as 'absent, therefore locked' silently,
    nor as 'present, therefore fine'."""
    auth_path.write_text("{not json")
    with pytest.raises(A.ConfirmationDataLocked, match="unreadable"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_banner_states_the_posture(auth_path):
    assert "LOCKED" in A.authorization_banner()
    auth_path.write_text(json.dumps(_valid()))
    assert "UNLOCKED" in A.authorization_banner()
