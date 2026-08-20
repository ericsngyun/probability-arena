"""REALIZED-FILL-CORPUS-001 — reachability seam and the capital boundary.

Two guards that stay in the suite permanently.

**Seam (doctrine 5).** CP4 shipped 1,186 lines and 81 passing tests that
nothing in `app/` could call, because from inside a module everything works.
So reachability is asserted from OUTSIDE: these tests drive the real CLI entry
point with the real fixtures and prove observable output changes. A parameter
nothing ever passes is a comment, not an interface, so the test proves the
CALLER exists rather than that the handler works.

**Boundary.** The forbidden-capability surface is checked mechanically rather
than promised in a docstring. The RPC adapter's allowlist is the boundary, so
it is tested as one.
"""

from __future__ import annotations

import ast
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

APP_FILLS = Path(__file__).resolve().parents[1] / "app" / "fills"
ADAPTER = (
    Path(__file__).resolve().parents[1] / "app" / "adapters" / "solana_rpc.py"
)


# ---------------------------------------------------------------------------
# seam: the production path is reachable from outside the package
# ---------------------------------------------------------------------------


def test_the_cli_verb_exists_and_drives_the_real_decoder():
    """Instantiate the REAL collaborators through the REAL entry point and
    prove observable state. A unit suite cannot catch an unreachable module."""
    from app.cli import main

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["realized-fill-corpus-report"])
    out = buffer.getvalue()

    assert code == 0
    assert "REALIZED-FILL-CORPUS-001" in out
    assert "fixtures           : 6" in out
    assert "provenance problems: 0" in out
    # the decoder actually ran: statuses came from real transactions
    assert "'confirmed': 4" in out
    assert "'failed': 2" in out


def test_the_cli_verb_emits_absence_reasons_not_blanks():
    """The report must show WHY a field is missing. A coverage table of blanks
    is how "no markout" becomes indistinguishable from "markout of zero"."""
    from app.cli import main

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["realized-fill-corpus-report", "--format", "json"])
    assert code == 0
    payload = json.loads(buffer.getvalue())

    coverage = payload["summary"]["coverage"]
    assert coverage["actual_output"]["absent_reasons"] == {"transaction_failed": 2}
    markouts = payload["summary"]["markout_coverage"]
    for horizon in ("markout_1s", "markout_5s", "markout_30s", "markout_300s"):
        assert markouts[horizon]["present"] == 0
        assert markouts[horizon]["absent_reasons"] == {"not_provided": 6}


def test_the_corpus_reports_zero_eps_fill_rows_as_not_authorized():
    """The headline honesty property, asserted at the seam.

    `eps_fill` is the quantity three specs are blocked on, and the corpus must
    report that it has NONE of it — and that the reason is a missing
    authorization, not a missing collection run. `NOT_YET_OBSERVED` would
    imply waiting fixes it."""
    from app.cli import main

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        main(["realized-fill-corpus-report", "--format", "json"])
    summary = json.loads(buffer.getvalue())["summary"]

    assert summary["eps_fill_rows"] == 0
    assert "NOT authorized" in summary["eps_fill_note"]
    assert "NOT_AUTHORIZED, not not-yet-collected" in summary["eps_fill_note"]


def test_the_cli_verb_fails_loudly_on_a_provenance_violation(tmp_path):
    """POSITIVE CONTROL for the seam's own error path: corrupt a fixture and
    require a non-zero exit. A reporter that always exits 0 is a reporter that
    cannot report."""
    import shutil

    from app.cli import main

    src = Path(__file__).parent / "fixtures" / "solana_fills"
    dest = tmp_path / "solana_fills"
    shutil.copytree(src, dest)

    manifest = json.loads((dest / "MANIFEST.json").read_text())
    manifest["fixtures"][0]["content_sha256"] = "0" * 64
    (dest / "MANIFEST.json").write_text(json.dumps(manifest))

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(
            ["realized-fill-corpus-report", "--fixtures-dir", str(dest)]
        )
    assert code == 1
    assert "VIOLATION" in buffer.getvalue()


def test_build_realized_fill_produces_a_record_with_every_contract_field():
    """The record shape is the contract. A field silently dropped from
    `as_json` would be invisible to every downstream consumer."""
    from app.fills.corpus import build_realized_fill
    from app.fills.provenance import load_fixture_set
    from app.fills.schema import Side

    fixtures = load_fixture_set(Path(__file__).parent / "fixtures" / "solana_fills")
    entry = fixtures.by_capture_id("direct_dispose_wrapped_sol_ata_cycle")
    fill = build_realized_fill(
        fixtures.payload(entry), side=Side.DISPOSE, base_mint="test"
    )
    blob = fill.as_json()

    for field in (
        "decision_id", "observation_id", "mint", "side",
        "notional_quote_units", "quote_asset_mint", "route", "quote",
        "t_submit", "signature", "slot", "t_confirmed", "status",
        "actual_input", "actual_output", "costs", "actual_price",
        "realized_slippage", "quote_to_submit_ms", "submit_to_confirm_ms",
        "markouts", "states", "model_version", "decoder_version",
        "reconstructability", "decoder_notes",
    ):
        assert field in blob, field

    for field in (
        "t_quote", "quoted_input", "quoted_output", "quoted_price",
        "quoted_price_impact", "quoted_min_output",
    ):
        assert field in blob["quote"], field

    for field in (
        "network_fee_lamports", "priority_fee_lamports", "tip_lamports",
        "rent_lamports_net", "total_lamports",
    ):
        assert field in blob["costs"], field

    assert [m["horizon_seconds"] for m in blob["markouts"]] == [1, 5, 30, 300]
    for field in ("liquidity_state", "volatility_state", "social_state"):
        assert field in blob["states"], field


# ---------------------------------------------------------------------------
# boundary: the forbidden surface cannot be reached
# ---------------------------------------------------------------------------


def test_the_rpc_adapter_refuses_every_state_changing_method():
    """The allowlist IS the boundary. Every forbidden verb is a real method on
    the very endpoint this adapter talks to, so a permissive client would put
    the hard boundary one string literal away."""
    import asyncio

    from app.adapters.solana_rpc import (
        FORBIDDEN_METHODS,
        PERMITTED_METHODS,
        ForbiddenRpcMethod,
        SolanaRpcAdapter,
    )

    adapter = SolanaRpcAdapter()
    for method in FORBIDDEN_METHODS:
        with pytest.raises(ForbiddenRpcMethod):
            asyncio.run(adapter._call(method, []))

    # and the guard is not vacuous: the permitted set is non-empty and
    # disjoint from the forbidden one (doctrine 4 — assert the permitted
    # thing EXISTS, or the guard is satisfied by an adapter that does nothing)
    assert PERMITTED_METHODS
    assert not (PERMITTED_METHODS & FORBIDDEN_METHODS)
    assert "getTransaction" in PERMITTED_METHODS


def test_the_named_refusals_cover_the_documented_hard_boundary():
    """AGENTS.md forbids simulation, signing, submission and blockhash /
    priority-fee / nonce retrieval by name. Each must be refused by name."""
    from app.adapters.solana_rpc import FORBIDDEN_METHODS

    for method in (
        "sendTransaction",
        "simulateTransaction",
        "getLatestBlockhash",
        "getRecentBlockhash",
        "getFeeForMessage",
        "getRecentPrioritizationFees",
        "requestAirdrop",
    ):
        assert method in FORBIDDEN_METHODS


def test_no_banned_identifier_fragment_appears_anywhere_in_the_package():
    """The EVAL-001 AST safety audit bans these fragments as identifiers
    anywhere in `app/`. This package deliberately uses none of them and asks
    for no allowlist entry; the domain words are route, leg, fee payer, owner
    account, venue program."""
    from app.services.frontier_eval import BANNED_IDENTIFIER_FRAGMENTS

    offenders: list[str] = []
    files = sorted(APP_FILLS.glob("*.py")) + [ADAPTER]
    assert len(files) >= 9, "the package shrank; this guard may be vacuous"

    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            for name in names:
                lowered = name.lower()
                for fragment in BANNED_IDENTIFIER_FRAGMENTS:
                    if fragment in lowered:
                        offenders.append(f"{path.name}:{name} contains {fragment}")
    assert not offenders, "\n".join(sorted(set(offenders)))


def test_the_package_declares_the_capital_boundary_in_its_own_docstring():
    """A boundary nobody can find is not a boundary. `app/fills/__init__.py`
    must state it, and this test is what keeps it there through a refactor."""
    import app.fills as package

    # collapse wrapping so a phrase split across two lines still matches
    doc = " ".join((package.__doc__ or "").lower().split())
    for phrase in (
        "no capital",
        "no order submission",
        "no transaction construction",
        "no simulation",
        "no signing",
        "no broadcasting",
        "read-only",
    ):
        assert phrase in doc, phrase


def test_no_module_in_the_package_imports_a_networking_client_except_the_adapter():
    """The decoder, the calibration maths and the corpus assembly must be
    pure. If any of them could open a socket, the boundary would depend on
    reviewing call sites rather than on structure."""
    for path in sorted(APP_FILLS.glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                root = module.split(".")[0]
                assert root not in {
                    "httpx",
                    "requests",
                    "aiohttp",
                    "socket",
                    "websockets",
                    "urllib",
                }, f"{path.name} imports {module}"
