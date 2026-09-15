"""Capture-contract authorization for confirmation sessions.

The arm/launch guard must bind to *executable capture content*, not the
entire repository SHA. A README or freeze-evidence commit must neither
authorize changed collector code nor invalidate identical collector code.

Invariant:

    authorized executable content  ==  frozen executable content

`code_commit` on a genesis record names the historical tip at which the
capture contract was established (e.g. Amendment-002 at 8b448f6). The
live check is the fingerprint of CAPTURE_CONTRACT_FILES, recorded on the
genesis and re-verified at arm and at launch.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Every path whose bytes can change what a confirmation capture *does*.
#: Docs-only and ledger-only files are deliberately absent.
CAPTURE_CONTRACT_FILES = (
    "app/microstructure/capture_contract.py",  # this lock is fingerprinted
    "app/microstructure/lifecycle_compatibility.py",
    "app/microstructure/panel.py",
    "app/microstructure/rows.py",
    "app/microstructure/labels.py",
    "app/microstructure/features.py",
    "scripts/kalshi_microstructure_arm_session.py",
    "scripts/kalshi_microstructure_capture_runner.py",
)

#: Freeze artifacts whose identity defines *this* session (not the code).
#: Changing markets/anchor/schedule without a new freeze must refuse.
FREEZE_ARTIFACT_BASENAMES = (
    "schedule.json",
    "events.json",
    "markets.txt",
)


def _fingerprint(paths: tuple[str, ...], *, root: Path = REPO) -> str:
    h = hashlib.sha256()
    for rel in paths:
        f = root / rel
        if not f.exists():
            raise FileNotFoundError(
                f"capture-contract file missing: {rel}")
        h.update(rel.encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def capture_code_fingerprint(*, root: Path = REPO) -> str:
    return _fingerprint(CAPTURE_CONTRACT_FILES, root=root)


def freeze_artifact_fingerprint(*paths: Path) -> str:
    """sha256 over (basename, bytes) for the frozen schedule/events/markets."""
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: x.name):
        h.update(p.name.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def repo_tip(*, root: Path = REPO) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def working_tree_clean(*, root: Path = REPO) -> tuple[bool, str]:
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        return False, dirty
    return True, ""


@dataclass(frozen=True)
class CaptureAuthorization:
    authorized: bool
    result: str
    repo_tip: str
    authorized_capture_contract: str
    capture_code_fingerprint: str
    expected_capture_code_fingerprint: str
    freeze_fingerprint: str
    expected_freeze_fingerprint: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def authorize_capture(
        *,
        genesis: dict,
        schedule_path: Path,
        events_path: Path,
        markets_path: Path,
        root: Path = REPO,
) -> CaptureAuthorization:
    """No-socket authorization decision for a frozen confirmation session."""
    tip = repo_tip(root=root)
    clean, dirty = working_tree_clean(root=root)
    code_fp = capture_code_fingerprint(root=root)
    freeze_fp = freeze_artifact_fingerprint(
        schedule_path, events_path, markets_path)

    expected_contract = str(genesis.get("code_commit")
                            or genesis.get("authorized_capture_contract")
                            or "")
    expected_code = str(genesis.get("capture_code_fingerprint") or "")
    expected_freeze = str(genesis.get("freeze_fingerprint") or "")

    if not clean:
        return CaptureAuthorization(
            False, "REFUSED_DIRTY_TREE", tip, expected_contract,
            code_fp, expected_code, freeze_fp, expected_freeze,
            f"working tree is dirty:\n{dirty[:400]}")
    if not expected_code:
        return CaptureAuthorization(
            False, "REFUSED_MISSING_CODE_FINGERPRINT", tip, expected_contract,
            code_fp, expected_code, freeze_fp, expected_freeze,
            "genesis lacks capture_code_fingerprint; re-freeze under "
            "capture-contract authorization")
    if code_fp != expected_code:
        return CaptureAuthorization(
            False, "REFUSED_CAPTURE_CODE_DRIFT", tip, expected_contract,
            code_fp, expected_code, freeze_fp, expected_freeze,
            "capture-code fingerprint drifted from the frozen contract")
    if not expected_freeze:
        return CaptureAuthorization(
            False, "REFUSED_MISSING_FREEZE_FINGERPRINT", tip, expected_contract,
            code_fp, expected_code, freeze_fp, expected_freeze,
            "genesis lacks freeze_fingerprint")
    if freeze_fp != expected_freeze:
        return CaptureAuthorization(
            False, "REFUSED_FREEZE_ARTIFACT_DRIFT", tip, expected_contract,
            code_fp, expected_code, freeze_fp, expected_freeze,
            "frozen schedule/events/markets bytes drifted")

    # Repo tip may advance (docs/ledger). Record it; do not require equality
    # with authorized_capture_contract.
    return CaptureAuthorization(
        True, "AUTHORIZED", tip, expected_contract,
        code_fp, expected_code, freeze_fp, expected_freeze,
        "capture-code and freeze fingerprints match; tree clean; "
        f"repo_tip={tip[:12]} may differ from capture contract "
        f"{expected_contract[:12]}")


def write_validation_report(auth: CaptureAuthorization, path: Path) -> None:
    path.write_text(json.dumps(auth.to_dict(), indent=2) + "\n")
