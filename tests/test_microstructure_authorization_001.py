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
    from app.microstructure.rows import (LABEL_SCHEMA_VERSION,
                                         ROW_SCHEMA_VERSION)
    d = {"milestone": A.EXPECTED_MILESTONE, "operator": "eric",
         "authorized_at_utc": datetime.now(timezone.utc).isoformat(),
         "sessions_complete": 20, "statement": A.REQUIRED_STATEMENT,
         "evaluator_fingerprint": A.evaluator_fingerprint(),
         "preregistration_fingerprint": A.preregistration_fingerprint(),
         "expected_row_schema": ROW_SCHEMA_VERSION,
         "expected_label_schema": LABEL_SCHEMA_VERSION}
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


# --- version binding: an authorization is for ONE exact test ----------------

def test_authorization_is_bound_to_the_running_evaluator(auth_path):
    auth_path.write_text(json.dumps(_valid(
        evaluator_fingerprint="0" * 64)))
    with pytest.raises(A.ConfirmationDataLocked,
                       match="running evaluator does not match"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_authorization_is_bound_to_the_preregistration(auth_path):
    auth_path.write_text(json.dumps(_valid(
        preregistration_fingerprint="0" * 64)))
    with pytest.raises(A.ConfirmationDataLocked,
                       match="preregistration does not match"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_authorization_is_bound_to_the_schema(auth_path):
    auth_path.write_text(json.dumps(_valid(expected_row_schema="row-v1")))
    with pytest.raises(A.ConfirmationDataLocked, match="expects schema"):
        A.require_readable(DatasetRole.CONFIRMATION)


def test_editing_a_frozen_module_invalidates_an_existing_authorization(
        auth_path, tmp_path, monkeypatch):
    """The scenario this exists for: a harmless-looking refactor mid-tranche."""
    auth_path.write_text(json.dumps(_valid()))
    A.require_readable(DatasetRole.CONFIRMATION)          # valid right now

    target = A.REPO / "app" / "microstructure" / "evaluate.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# a harmless comment\n")
        with pytest.raises(A.ConfirmationDataLocked,
                           match="running evaluator does not match"):
            A.require_readable(DatasetRole.CONFIRMATION)
    finally:
        target.write_bytes(original)
    A.require_readable(DatasetRole.CONFIRMATION)          # valid again


def test_fingerprints_are_stable_and_order_independent_of_filesystem():
    a, b = A.evaluator_fingerprint(), A.evaluator_fingerprint()
    assert a == b and len(a) == 64
    assert A.evaluator_fingerprint() != A.preregistration_fingerprint()


def test_a_missing_frozen_file_locks_rather_than_skipping_it(monkeypatch):
    monkeypatch.setattr(A, "FROZEN_EVALUATOR_FILES",
                        A.FROZEN_EVALUATOR_FILES + ("app/does_not_exist.py",))
    with pytest.raises(A.ConfirmationDataLocked, match="is missing"):
        A.evaluator_fingerprint()
