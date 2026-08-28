"""The consumer cannot bypass the adapter's read-only surface.

Asserting the adapter's own surface is not enough. If anything reachable from
the observer can obtain a wider RPC capability -- by importing urllib directly,
by holding a raw endpoint, by reaching a second client -- then "read-only" is
again a property of discipline rather than of the code.
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from tests.astguard import imported_modules

#: Everything the social observer path is allowed to reach.
OBSERVER_MODULES = (
    "app.social.observer_funnel",
    "app.social.evidence_extractor",
    "app.social.source_authority",
    "app.seam.corroboration",
    "app.seam.fill_seam",
    "app.seam.chain_identity",
)

#: The single sanctioned door to the chain.
ADAPTER = "app.seam.solana_rpc"

NETWORK_MODULES = ("urllib", "requests", "httpx", "aiohttp", "socket",
                   "websockets", "http.client")


def _transitive(seed, prefix="app."):
    seen, stack = set(), list(seed)
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        try:
            mod = importlib.import_module(m)
            for dep in imported_modules(mod):
                if dep.startswith(prefix) and dep not in seen:
                    stack.append(dep)
        except Exception:
            continue
    return seen


#: `socket` is not automatically a transport. `app.seam.clock` imports it for
#: `gethostname()` — deriving the host identity that the boot/host comparability
#: key depends on — and never opens a connection. Banning the import would
#: condemn a module that does exactly what it should, so the guard checks the
#: CALLS instead.
CONNECTING_CALLS = ("connect", "create_connection", "urlopen", "request",
                    "get", "post", "send", "recv", "sendall", "socket")


def _root_name(node) -> str | None:
    """Walk an attribute chain to its root: `urllib.request.urlopen` -> urllib.

    The naive version read `.id` off `node.func.value`, which is a Name only
    for single-level calls like `socket.connect`. For `urllib.request.urlopen`
    the base is itself an Attribute, so `.id` was absent and the call escaped
    the guard entirely -- a mutation injecting exactly that PASSED.
    """
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _socket_calls(mod) -> set[str]:
    tree = ast.parse(inspect.getsource(mod))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if _root_name(n.func.value) in ("socket", "urllib", "requests",
                                            "httpx", "aiohttp", "websockets"):
                out.add(n.func.attr)
    return out


def test_no_observer_module_opens_the_network_itself():
    """Only the adapter may touch a transport.

    Checked on CALLS, not imports: importing `socket` to read a hostname is not
    opening a connection.
    """
    for name in _transitive(OBSERVER_MODULES):
        if name == ADAPTER:
            continue
        mod = importlib.import_module(name)
        for net in ("requests", "httpx", "aiohttp", "websockets"):
            assert not any(net in m for m in imported_modules(mod)), (
                f"{name} imports {net}; the adapter is the only door")
        bad = _socket_calls(mod) & set(CONNECTING_CALLS)
        assert not bad, f"{name} makes network calls {sorted(bad)}"


def test_the_clock_uses_socket_only_for_identity():
    """The one sanctioned `socket` import, pinned to its purpose."""
    from app.seam import clock as C
    calls = _socket_calls(C)
    assert calls <= {"gethostname"}, f"clock calls socket.{sorted(calls)}"


def test_the_adapter_is_the_only_module_holding_an_endpoint():
    from app.seam import solana_rpc as RPC
    assert hasattr(RPC, "DEFAULT_ENDPOINT")
    for name in _transitive(OBSERVER_MODULES):
        if name == ADAPTER:
            continue
        mod = importlib.import_module(name)
        src = inspect.getsource(mod)
        assert "http://" not in src and "https://" not in src, (
            f"{name} contains a URL; endpoints belong to the adapter")


def test_gate1_receives_a_reader_and_never_constructs_one():
    """Dependency INJECTION is what keeps the capability auditable."""
    from app.seam import chain_identity as CI
    assert ADAPTER not in imported_modules(CI), (
        "chain_identity must not import the adapter; it takes an AccountReader")
    sig = inspect.signature(CI.verify_chain_existence)
    assert "reader" in sig.parameters


def test_widening_the_adapter_would_be_visible_in_one_file():
    """The whitelist is the audit surface."""
    from app.seam import solana_rpc as RPC
    assert len(RPC.ALLOWED_METHODS) == 5
    tree = ast.parse(inspect.getsource(RPC))
    # every method string sent must be a member of the whitelist
    posted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "_post" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant):
                posted.add(a.value)
    assert posted <= RPC.ALLOWED_METHODS, f"unwhitelisted sends: {posted}"
    assert posted == RPC.ALLOWED_METHODS, (
        "every whitelisted method should have exactly one typed caller")


def test_the_observer_cannot_reach_the_kalshi_confirmation_lane():
    reach = _transitive(OBSERVER_MODULES)
    assert not any(m.startswith("app.microstructure") for m in reach), reach
