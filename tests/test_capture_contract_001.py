"""Capture-contract authorization: executable content, not repository SHA."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.microstructure import capture_contract as CC


@pytest.fixture
def freeze_dir(tmp_path):
    sched = {"scheduled_target_bin": "live_event",
             "anchor_occurrence_datetime": "2026-09-20T20:00:00+00:00",
             "scheduled_session_start": "2026-09-20T19:40:00+00:00",
             "session_seconds": 1800}
    events = {"T1": {"series": "KXNFLGAME",
                     "occurrence_datetime": "2026-09-20T20:00:00Z"}}
    markets = "T1\n"
    sp = tmp_path / "schedule.json"
    ep = tmp_path / "events.json"
    mp = tmp_path / "markets.txt"
    sp.write_text(json.dumps(sched, indent=2))
    ep.write_text(json.dumps(events, indent=2))
    mp.write_text(markets)
    return sp, ep, mp


@pytest.fixture
def clean_tree(monkeypatch):
    monkeypatch.setattr(CC, "working_tree_clean", lambda *, root=None: (True, ""))
    monkeypatch.setattr(CC, "repo_tip", lambda *, root=None: "0250cc2f95d3e16da628a9656da9dd52919a2388")


def test_docs_tip_may_differ_from_capture_contract_when_fingerprints_match(
        freeze_dir, clean_tree):
    sp, ep, mp = freeze_dir
    code_fp = CC.capture_code_fingerprint()
    freeze_fp = CC.freeze_artifact_fingerprint(sp, ep, mp)
    tip = CC.repo_tip()
    genesis = {
        "code_commit": "8b448f687dc00ac0d34332f98d26f86d1a77f5f8",
        "capture_code_fingerprint": code_fp,
        "freeze_fingerprint": freeze_fp,
    }
    auth = CC.authorize_capture(
        genesis=genesis, schedule_path=sp, events_path=ep, markets_path=mp)
    assert auth.authorized is True
    assert auth.result == "AUTHORIZED"
    assert auth.repo_tip == tip
    assert auth.authorized_capture_contract.startswith("8b448f6")


def test_capture_code_drift_refuses(freeze_dir, clean_tree):
    sp, ep, mp = freeze_dir
    genesis = {
        "code_commit": "8b448f6",
        "capture_code_fingerprint": "0" * 64,
        "freeze_fingerprint": CC.freeze_artifact_fingerprint(sp, ep, mp),
    }
    auth = CC.authorize_capture(
        genesis=genesis, schedule_path=sp, events_path=ep, markets_path=mp)
    assert auth.authorized is False
    assert auth.result == "REFUSED_CAPTURE_CODE_DRIFT"


def test_freeze_artifact_drift_refuses(freeze_dir, clean_tree):
    sp, ep, mp = freeze_dir
    genesis = {
        "code_commit": "8b448f6",
        "capture_code_fingerprint": CC.capture_code_fingerprint(),
        "freeze_fingerprint": "0" * 64,
    }
    auth = CC.authorize_capture(
        genesis=genesis, schedule_path=sp, events_path=ep, markets_path=mp)
    assert auth.authorized is False
    assert auth.result == "REFUSED_FREEZE_ARTIFACT_DRIFT"


def test_literal_head_equality_is_not_required(freeze_dir, clean_tree):
    """Mutation guard: authorizing solely by HEAD==code_commit must fail here.

    At any tip after the Amendment-002 docs commits, HEAD != 8b448f6, yet
    identical capture bytes must still authorize.
    """
    sp, ep, mp = freeze_dir
    tip = CC.repo_tip()
    contract = "8b448f687dc00ac0d34332f98d26f86d1a77f5f8"
    assert not (tip.startswith(contract) or contract.startswith(tip[:12]))
    genesis = {
        "code_commit": contract,
        "capture_code_fingerprint": CC.capture_code_fingerprint(),
        "freeze_fingerprint": CC.freeze_artifact_fingerprint(sp, ep, mp),
    }
    auth = CC.authorize_capture(
        genesis=genesis, schedule_path=sp, events_path=ep, markets_path=mp)
    assert auth.authorized is True


def test_capture_contract_files_include_the_lock_itself():
    assert "app/microstructure/capture_contract.py" in CC.CAPTURE_CONTRACT_FILES
    assert "scripts/kalshi_microstructure_arm_session.py" in CC.CAPTURE_CONTRACT_FILES


def test_dirty_tree_still_refuses(freeze_dir, monkeypatch):
    monkeypatch.setattr(CC, "working_tree_clean",
                        lambda *, root=None: (False, " M app/x.py"))
    sp, ep, mp = freeze_dir
    genesis = {
        "code_commit": "8b448f6",
        "capture_code_fingerprint": CC.capture_code_fingerprint(),
        "freeze_fingerprint": CC.freeze_artifact_fingerprint(sp, ep, mp),
    }
    auth = CC.authorize_capture(
        genesis=genesis, schedule_path=sp, events_path=ep, markets_path=mp)
    assert auth.result == "REFUSED_DIRTY_TREE"
