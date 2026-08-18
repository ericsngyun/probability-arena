"""KALSHI-PROD-QUAL-PRECAPTURE — the gate that runs immediately before capture.

**This script opens no socket, reads no credential and captures nothing.** It
is the ordered list of things that must be true before
`KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001` may connect, and it is the caller
that makes the two pre-capture deliverables *reachable* rather than merely
present (AGENTS.md doctrine 5 — a checkpoint is complete when its intended
production path is demonstrably reachable, asserted from OUTSIDE the module).

Four gates, in this order, because the order is the guarantee:

1. **STRUCTURAL GUARD.** `scripts/kalshi_prod_observation_guard.py` walks the
   real import closure of the production-observation entry point and proves no
   order, portfolio or execution surface is reachable from it. A finding here
   stops everything; nothing downstream can make an unsafe closure safe.
2. **ENDPOINT.** The production WS host the collector would use is printed
   beside the host recorded in `.env`, and any disagreement is REPORTED, never
   resolved silently (doctrine 8: an endpoint is a claim about the venue).
3. **SESSION ROOT.** A session id is generated and persisted, and the archive
   root is bound to it — **before** step 4 is even reachable. This is the
   run rule that closes §11 B4 of the measurement contract.
4. **TRANSPORT FACTORY.** Only now may a factory be built, and this script
   still does not build one: it prints the fact that the gate is open. The
   capture command is a separate, separately-authorized step.

Nothing here is a substitute for the credential. B1 of the measurement
contract — an unverified production host and a production read-scoped
credential — is closable only by an operator, and this script says so on its
face rather than implying readiness it cannot establish.

    python scripts/kalshi_prod_precapture_preflight.py --archive-root /path/to/root
    python scripts/kalshi_prod_precapture_preflight.py --archive-root ... --json

Exit code 0 only when every gate passes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.realtime.kalshi import ENV_PRODUCTION, WS_HOSTS  # noqa: E402
from app.realtime.session_root import (  # noqa: E402
    SessionRootError,
    open_session_root,
)

# The host EVO's `.env` carries today. Recorded as a constant so the comparison
# is against a written-down value and not against whatever happens to be in the
# environment of whoever runs this.
ENV_FILE_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
ENDPOINT_NOTE_PATH = "docs/KALSHI_PRODUCTION_ENDPOINT_001.md"


def _load_guard():
    """Import the guard by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "kalshi_prod_observation_guard",
        REPO / "scripts" / "kalshi_prod_observation_guard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def gate_structural_guard(repo_root: Path) -> dict:
    report = _load_guard().audit(repo_root)
    return {
        "gate": "structural_guard",
        "passed": report.clean,
        "modules_in_closure": len(report.closure),
        "identifiers_scanned": report.identifiers_scanned,
        "findings": [f.to_dict() for f in report.findings],
    }


def gate_endpoint() -> dict:
    """Report both hosts. NEVER pick one here.

    The collector's host and the `.env` host disagree. Both are documented by
    Kalshi as valid production WebSocket hosts, so this is a disagreement to be
    stated, not a defect to be patched — and the resolution belongs to an
    operator with the credential in hand, on the first successful handshake.
    """
    collector_host = WS_HOSTS[ENV_PRODUCTION]
    agrees = collector_host == ENV_FILE_WS_URL
    return {
        "gate": "endpoint",
        # A disagreement does not fail the preflight; an UNREPORTED one would.
        "passed": True,
        "collector_would_connect_to": collector_host,
        "env_file_records": ENV_FILE_WS_URL,
        "hosts_agree": agrees,
        "note": ("RECORDED, NOT RESOLVED — see " + ENDPOINT_NOTE_PATH
                 if not agrees else "hosts agree"),
        "verified_on_the_wire": False,
    }


def gate_session_root(archive_root: Path, environment: str,
                      session_id: str | None) -> dict:
    try:
        claim = open_session_root(archive_root, environment,
                                 session_id=session_id)
    except SessionRootError as exc:
        return {"gate": "session_root", "passed": False,
                "error": f"{type(exc).__name__}: {exc}"}
    return {
        "gate": "session_root",
        "passed": True,
        "session_id": claim.session_id,
        "claim_path": claim.path,
        "claimed_at": claim.claimed_at,
        "reused_existing_claim": claim.already_existed,
        "record_schema_version_bumped": False,
    }


def preflight(*, archive_root: Path, environment: str = ENV_PRODUCTION,
              session_id: str | None = None, repo_root: Path = REPO,
              transport_factory_builder=None) -> dict:
    """Run the gates in order and stop at the first failure.

    `transport_factory_builder` exists so the ORDER can be proven from outside:
    a test passes a builder that records whether the session claim was already
    on disk when it was called. It is never called if any earlier gate failed,
    which is the property that makes "persisted before the socket is opened"
    a structural fact rather than a sentence in a runbook.
    """
    gates = [gate_structural_guard(repo_root)]
    if gates[-1]["passed"]:
        gates.append(gate_endpoint())
    if gates[-1]["passed"]:
        gates.append(gate_session_root(archive_root, environment, session_id))
    factory = None
    if all(g["passed"] for g in gates) and transport_factory_builder is not None:
        factory = transport_factory_builder()
    return {
        "milestone": "KALSHI-PROD-QUAL-PRECAPTURE",
        "capture_attempted": False,
        "credential_read": False,
        "socket_opened": False,
        "gates": gates,
        "passed": all(g["passed"] for g in gates),
        "transport_factory": factory,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--environment", default=ENV_PRODUCTION)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = preflight(archive_root=Path(args.archive_root),
                       environment=args.environment,
                       session_id=args.session_id)
    if args.json:
        print(json.dumps({k: v for k, v in result.items()
                          if k != "transport_factory"},
                         indent=2, sort_keys=True))
    else:
        print("KALSHI production-observation PRE-CAPTURE preflight")
        print("  no socket opened, no credential read, no capture attempted")
        for gate in result["gates"]:
            status = "PASS" if gate["passed"] else "FAIL"
            print(f"  [{status}] {gate['gate']}")
            for key, value in gate.items():
                if key in ("gate", "passed"):
                    continue
                print(f"           {key}: {value}")
        print(f"  VERDICT: {'GATE OPEN' if result['passed'] else 'BLOCKED'}")
        print("  NOTE: a production READ-SCOPED CREDENTIAL is still required "
              "and is not something this preflight can establish (§11 B1).")
    return 0 if result["passed"] else 1


if __name__ == "__main__":      # pragma: no cover - operator entry point
    sys.exit(main())
