"""The Solana identity gates must have NO path into the Kalshi confirmation lane.

Two experiments are running side by side. One is a frozen, locked, mid-flight
preregistered capture; the other is active engineering. The only way that stays
safe is if the second cannot reach the first -- not by convention, but because
the import graph forbids it.

Checked in both directions, transitively, over real module dependencies rather
than over source text.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Everything that defines or executes the Kalshi confirmation experiment.
KALSHI_CONFIRMATION = {
    "app.microstructure.evaluate",
    "app.microstructure.tte_heterogeneity",
    "app.microstructure.rows",
    "app.microstructure.features",
    "app.microstructure.labels",
    "app.microstructure.panel",
    "app.microstructure.coverage",
    "app.microstructure.authorization",
    "app.microstructure.linalg",
}

#: The Solana identity gates.
SOLANA_GATES = {
    "app.seam.chain_identity",
    "app.seam.corroboration",
}


def _imports_of(module_name: str) -> set[str]:
    mod = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(mod))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def _transitive(seed: set[str], prefix: str = "app.") -> set[str]:
    seen, stack = set(), list(seed)
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        try:
            for dep in _imports_of(m):
                if dep.startswith(prefix) and dep not in seen:
                    stack.append(dep)
        except Exception:
            continue
    return seen


def test_solana_gates_cannot_reach_the_kalshi_confirmation_lane():
    reach = _transitive(SOLANA_GATES)
    leaked = reach & KALSHI_CONFIRMATION
    assert not leaked, (
        f"Solana identity gates transitively import Kalshi confirmation "
        f"modules: {sorted(leaked)}")


def test_kalshi_confirmation_lane_cannot_reach_the_solana_gates():
    reach = _transitive(KALSHI_CONFIRMATION)
    leaked = reach & SOLANA_GATES
    assert not leaked, (
        f"Kalshi confirmation modules transitively import Solana gates: "
        f"{sorted(leaked)}")


def test_the_two_lanes_share_no_mutable_state_module():
    """Overlap in leaf utilities is fine; overlap in anything holding
    experiment state is not."""
    solana = _transitive(SOLANA_GATES)
    kalshi = _transitive(KALSHI_CONFIRMATION)
    shared = solana & kalshi
    for m in shared:
        assert not m.startswith("app.microstructure"), (
            f"{m} is shared and belongs to the Kalshi experiment")
        assert m not in SOLANA_GATES


def test_the_capture_runner_does_not_import_the_solana_gates():
    """The script that writes confirmation tape, checked directly."""
    src = (REPO / "scripts" / "kalshi_microstructure_capture_runner.py").read_text()
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    assert not any("seam" in m or "social" in m for m in mods), mods


def test_gate2_creates_no_trading_or_alpha_path():
    """Gate 2 decides identity. It must not be able to express a prediction."""
    from app.seam import corroboration as C
    src = inspect.getsource(C)
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # `ProvenanceScope.QUOTED` is quoted CONTENT -- a retweet -- not a market
    # quote, so a bare "quote" substring flags a correct module. The domain
    # term is allowed by name; the market concept is not.
    allowed = {"quoted"}
    for banned in ("swap", "sign", "wallet", "order", "position", "pnl",
                   "predict", "forecast", "bid", "ask", "midprice",
                   "orderbook", "liquidity", "slippage"):
        hits = {n for n in names if banned in n.lower()} - allowed
        assert not hits, f"gate 2 references {hits}"
    # and the market sense of "quote" specifically
    for n in names:
        low = n.lower()
        if "quote" in low and low not in allowed:
            assert low.startswith("quoted"), (
                f"gate 2 references a market quote: {n}")


def test_gate1_performs_no_network_io_of_its_own():
    """`AccountReader` is injected; the module must not open a socket itself."""
    from app.seam import chain_identity as CI
    mods = _imports_of("app.seam.chain_identity")
    for banned in ("requests", "httpx", "urllib", "socket", "aiohttp", "http"):
        assert not any(banned in m for m in mods), f"gate 1 imports {banned}"
