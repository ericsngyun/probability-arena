"""KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001 — the structural order-API guard.

**What this proves, and how.** `KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001`
points the *unchanged* collector at the production venue. The lane must be
strictly observational: no order creation, no cancellation, no portfolio
mutation, no private fill/order channels, no capital, **no venue write except
the protocol-required subscription/control messages**, and no dependency on any
trading or execution module. This file proves that **structurally**, by walking
the real import closure of the production-observation entry point
(`app.realtime.collector`) and constraining what that closure can *reach* —
not by grepping for scary words.

**Why closure arms and not greps.** A grep is defeated by a rename and tripped
by a docstring. Six of the eight arms below are rename-proof because they
constrain a *capability set* rather than a spelling:

| arm | what it closes |
|---|---|
| `closure` | the exact set of `app.*` modules reachable from the entry point — an EQUALITY, so a new dependency is red even if it is innocent |
| `http` | no HTTP client (`requests`/`httpx`/`aiohttp`/`urllib`/`http.client`) and no raw socket is reachable, so **no REST route exists to call** — order routes included, whatever they are named |
| `signing_routes` | the closed `AuthPurpose -> (method, path)` map is exactly two **GET** routes (the WS handshake and key metadata). A credential in this lane cannot sign anything else, so a mis-scoped key still cannot address an order route |
| `sendable` | the outbound command set that can reach the socket is exactly `subscribe` / `unsubscribe` / `update_subscription`, and every `send` call site in the closure sits inside the one governed writer |
| `channels` | the channel allowlist is an equality and the private user streams are named as forbidden |
| `modes` | only `OBSERVE_ONLY` is implemented; the ordinal-free `require_mode` refuses the rest |
| `identifiers` | a word-level tripwire over identifiers ONLY (docstrings and comments are structurally invisible to it) |
| `routes_in_strings` | non-docstring string literals may not spell an order/portfolio venue route |

The last two are tripwires, not the proof. They exist because a *new* order
surface is usually written before it is wired, and a tripwire catches it at the
moment it is typed rather than at the moment it is reachable.

**Anti-vacuity is part of the contract** (AGENTS.md doctrine 7). Every arm also
asserts that the PERMITTED thing exists: the closure is non-empty and contains
the collector, the transport and the signer; `websockets` really is reachable
(from exactly one module); the two GET routes really are present; the three
subscription commands really are present. An empty or unparsed tree fails here
instead of passing every ban silently.

**Runnable as a pre-capture gate:**

    python scripts/kalshi_prod_observation_guard.py            # exit 1 on any finding
    python scripts/kalshi_prod_observation_guard.py --json

Stdlib only, and it imports nothing from `app/` — the guard must be able to
audit a tree it is not itself running inside (that is how its own positive
controls work).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[1]

# --- what the production observation lane starts from ---------------------------
#
# The collector IS the entry point: `collect_once` / `run_session` are what an
# operator command calls, and `CollectorConfig.__post_init__` is where the
# channel allowlist runs. Anything the production capture can execute is
# reachable from here or is not executed at all.
ENTRY_POINTS = ("app.realtime.collector",)

# The EXACT app-level closure. An equality, never a subset: a new dependency —
# even an innocent one — must be reviewed against this file, because "the
# collector now imports one more thing" is precisely how an order surface would
# arrive.
EXPECTED_CLOSURE = frozenset({
    "app.config",
    "app.realtime",
    "app.realtime.archive",
    "app.realtime.archive_head",
    "app.realtime.auth",
    "app.realtime.book",
    "app.realtime.canonical",
    "app.realtime.collector",
    "app.realtime.collector_metrics",
    "app.realtime.evidence_fs",
    "app.realtime.fixedpoint",
    "app.realtime.kalshi",
    "app.realtime.segment",
    "app.realtime.ws_transport",
    "app.telemetry.schema",
    "app.telemetry.sink",
})

# Modules whose PRESENCE in the closure is itself the violation. Prefixes, so a
# submodule cannot slip in under a permitted parent.
FORBIDDEN_MODULE_PREFIXES = (
    "app.services", "app.crud", "app.db", "app.models", "app.routers",
    "app.adapters",
    # third-party execution/wallet surfaces
    "solana", "solders", "web3", "eth_account", "jupiter",
)

# No HTTP client and no raw socket. This is the arm that makes "cannot reach an
# order API" true by construction rather than by naming: a REST order route is
# unreachable when nothing in the closure can make an HTTP request at all.
FORBIDDEN_NETWORK_MODULES = (
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "http.client", "http",
    "ftplib", "telnetlib", "smtplib", "xmlrpc", "ssl",
)
# `socket` is a special case and is handled at ATTRIBUTE level rather than at
# import level. `app/telemetry/schema.py` imports it for `gethostname()` — a
# lookup of our own host name, not an outbound capability. Banning the import
# would have been a false positive; permitting the import unconditionally would
# have been a hole wide enough to drive a REST client through. So the import is
# permitted in exactly one module and only these attributes may be touched.
SOCKET_HOLDER = "app.telemetry.schema"
SOCKET_PERMITTED_ATTRS = frozenset({"gethostname"})
# The one permitted network dependency, and the one module allowed to hold it.
PERMITTED_NETWORK_MODULE = "websockets"
PERMITTED_NETWORK_HOLDER = "app.realtime.ws_transport"
# The local name the live connection object is bound to inside
# `KalshiWebsocketTransport.connect`. The raw write is permitted only through it
# and only inside the governed writer.
RAW_CONNECTION_NAME = "conn"

# --- word-level identifier tripwire ----------------------------------------------
#
# Identifiers are split into WORDS (snake_case and camelCase), so the channel
# `orderbook_delta` is the word "orderbook" and never the word "order", and a
# substring match — which would have hit it — is not what happens here.
#
# `order` is deliberately NOT a forbidden word. Three identifiers in the real
# closure contain it (`OrderBook`, `_read_order`, `_TRUNCATION_ORDER`) and all
# three mean *ordering*. Banning it would make the guard fire on the permitted
# thing, so the order surface is caught by BIGRAM instead: `place_order`,
# `order_id`, `user_orders`. Words that have no market-data homonym in this
# closure — `fill`, `position`, `portfolio`, `wallet` — are banned outright,
# and each was verified to have zero occurrences before it was added.
FORBIDDEN_WORDS = frozenset({
    "portfolio", "wallet", "kelly",
    "fill", "fills", "filled",
    "position", "positions",
    "cancel", "cancelled", "cancellation",
    "amend", "amendment",
    "buy", "sell", "bought", "sold",
    "collateral", "margin", "capital", "deposit", "withdrawal",
})
# Word BIGRAMS: the second half of the tripwire, for words that are innocent
# alone and forbidden together (`order` + `id`, `expected` + `value`).
FORBIDDEN_BIGRAMS = frozenset({
    ("place", "order"), ("submit", "order"), ("create", "order"),
    ("cancel", "order"), ("amend", "order"), ("decrease", "order"),
    ("batch", "order"), ("batch", "orders"), ("resting", "order"),
    ("order", "id"), ("order", "group"), ("user", "orders"), ("my", "orders"),
    ("expected", "value"), ("paper", "trade"), ("paper", "trading"),
    ("execute", "trade"), ("trade", "recommendation"),
    ("position", "size"),
})
# NOT forbidden, and deliberately so: `("order", "book")`. `OrderBook` is the
# market-data structure this whole lane exists to build. Banning it would make
# the guard fire on the permitted thing, which is the failure mode a guard is
# supposed to prevent, not commit.

# Reviewed exemptions, asserted as an EQUALITY below so the list cannot grow
# quietly. Each is a market-data or credential identifier that happens to
# collide with the tripwire vocabulary.
#
# Empty today, and that is the finding worth recording: the closure contains no
# identifier that needed excusing. If this list ever needs an entry, the entry
# is a review event, not a formality.
IDENTIFIER_EXEMPTIONS: frozenset = frozenset()

# --- route-shaped string literals -------------------------------------------------
#
# Docstrings are excluded (module/class/function leading Constant), so the
# collector's own boundary prose — which names these routes on purpose — does
# not trip the guard. Comments are not in the AST at all.
FORBIDDEN_ROUTE_RE = re.compile(
    r"/(orders|portfolio|positions|fills|balance|batched?_orders|order_groups)\b"
    r"|/trade-api/v2/(portfolio|orders)"
    r"|\b(POST|PUT|PATCH|DELETE)\b",
    re.IGNORECASE)

# --- the closed capability sets the arms compare against --------------------------
EXPECTED_SENDABLE_COMMANDS = frozenset({"subscribe", "unsubscribe",
                                        "update_subscription"})
EXPECTED_SIGNING_ROUTES = frozenset({("GET", "/trade-api/ws/v2"),
                                     ("GET", "/trade-api/v2/api_keys")})
EXPECTED_ALLOWED_CHANNELS = frozenset({"orderbook_delta", "ticker", "trade",
                                       "market_lifecycle_v2"})
EXPECTED_FORBIDDEN_CHANNELS = frozenset({"fill", "market_positions",
                                         "user_orders", "communications",
                                         "order_group_updates"})
EXPECTED_HTTP_METHODS = ("GET",)
EXPECTED_IMPLEMENTED_MODES = ("OBSERVE_ONLY",)

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")


def identifier_words(name: str) -> list:
    """`ask_size_raw` -> [ask, size, raw]; `OrderBook` -> [order, book].

    `OrderBook` deliberately yields two words. The channel name
    `orderbook_delta` yields `orderbook`, one word, because the venue spells it
    as one. That asymmetry is the point: a class that models the book is not an
    order surface, and neither is the channel.
    """
    out = []
    for part in re.split(r"_+", name):
        out += [w.lower() for w in _WORD_RE.findall(part)]
    return out


@dataclass
class Finding:
    arm: str
    module: str
    detail: str

    def to_dict(self) -> dict:
        return {"arm": self.arm, "module": self.module, "detail": self.detail}


@dataclass
class GuardReport:
    findings: list = field(default_factory=list)
    closure: frozenset = frozenset()
    identifiers_scanned: int = 0
    modules_parsed: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def add(self, arm: str, module: str, detail: str) -> None:
        self.findings.append(Finding(arm, module, detail))

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "closure": sorted(self.closure),
            "modules_parsed": self.modules_parsed,
            "identifiers_scanned": self.identifiers_scanned,
            "findings": [f.to_dict() for f in self.findings],
        }


# --- module resolution -------------------------------------------------------------


def _module_file(root: Path, module: str) -> Path | None:
    flat = root / (module.replace(".", "/") + ".py")
    if flat.is_file():
        return flat
    pkg = root / module.replace(".", "/") / "__init__.py"
    return pkg if pkg.is_file() else None


def _imported_modules(tree: ast.AST, root: Path) -> set:
    """Every module name this source imports, absolute only.

    `from app.realtime import evidence_fs` names a MODULE; `from
    app.realtime.kalshi import ALLOWED_CHANNELS` names a SYMBOL. The two are
    told apart by resolution, never by convention — and an `import x.y.z`
    target is always a module, resolvable or not, so an import of something
    that does not exist still lands in the closure and still fails the equality.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative; not used in this closure
                continue
            if not node.module:
                continue
            out.add(node.module)
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                if _module_file(root, candidate) is not None:
                    out.add(candidate)
    return out


def walk_closure(root: Path, entry_points=ENTRY_POINTS):
    """Transitive `app.*` closure, plus the raw import set of each member."""
    sources: dict = {}
    raw_imports: dict = {}
    pending = list(entry_points)
    closure = set()
    while pending:
        module = pending.pop()
        if module in closure:
            continue
        closure.add(module)
        path = _module_file(root, module)
        if path is None:
            continue                  # unresolvable, but still IN the closure
        tree = ast.parse(path.read_text(), filename=str(path))
        sources[module] = tree
        raw_imports[module] = _imported_modules(tree, root)
        for name in raw_imports[module]:
            if name.startswith("app.") and name not in closure:
                pending.append(name)
    return frozenset(closure), sources, raw_imports


# --- AST collectors ------------------------------------------------------------------


def collect_identifiers(tree: ast.AST) -> set:
    """Identifiers only. String constants are NOT identifiers, which is exactly
    why a docstring that names a forbidden route cannot trip this arm."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[-1])
    return out


def _docstring_nodes(tree: ast.AST) -> set:
    """The id() of every leading string Constant — module, class and function."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def collect_non_docstring_strings(tree: ast.AST) -> list:
    skip = _docstring_nodes(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip]


_UNRESOLVED = object()


def _module_assignments(tree: ast.AST) -> dict:
    """name -> value expression, for module-level assignments only."""
    out = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = node.value
    return out


def _resolve(node, assignments: dict, depth: int = 0):
    """Evaluate a module-level constant expression WITHOUT importing anything.

    `ast.literal_eval` alone is not enough here and the difference matters:
    `IMPLEMENTED_MODES = (OBSERVE_ONLY,)` is a tuple of a NAME, and a guard that
    read it as "not a literal" would have reported the observe-only build as
    unreadable — a benign-looking value from a broken path, which is the exact
    failure class doctrine 7 exists to catch. So module-level names are
    substituted, one hop at a time, with a depth bound instead of a cycle
    detector because these are constants and not a language.
    """
    if depth > 8:
        return _UNRESOLVED
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in assignments:
            return _resolve(assignments[node.id], assignments, depth + 1)
        return _UNRESOLVED
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_resolve(e, assignments, depth + 1) for e in node.elts]
        if any(i is _UNRESOLVED for i in items):
            return _UNRESOLVED
        return tuple(items)
    if isinstance(node, ast.Dict):
        pairs = []
        for key, value in zip(node.keys, node.values):
            resolved_key = (_resolve(key, assignments, depth + 1)
                            if key is not None else _UNRESOLVED)
            resolved_value = _resolve(value, assignments, depth + 1)
            if resolved_value is _UNRESOLVED:
                return _UNRESOLVED
            pairs.append((resolved_key, resolved_value))
        return tuple(pairs)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve(node.left, assignments, depth + 1)
        right = _resolve(node.right, assignments, depth + 1)
        if left is _UNRESOLVED or right is _UNRESOLVED:
            return _UNRESOLVED
        try:
            return left + right
        except TypeError:
            return _UNRESOLVED
    if isinstance(node, ast.Attribute):
        # `AuthPurpose.WEBSOCKET_HANDSHAKE` — an enum member used as a dict key.
        # Its VALUE is irrelevant to every arm here; only the route it maps to
        # is. Represented by its dotted spelling so the pair still resolves.
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return _UNRESOLVED


def _assigned_literal(tree: ast.AST, name: str):
    """The value assigned to a module-level constant, or None if unreadable.

    Read from the SOURCE rather than by importing, so the guard can audit a tree
    it is not running inside — which is the only way its own positive controls
    can inject a violation without contaminating the live process.
    """
    assignments = _module_assignments(tree)
    if name not in assignments:
        return None
    value = _resolve(assignments[name], assignments)
    return None if value is _UNRESOLVED else value


def _enclosing_function(tree: ast.AST, target: ast.AST):
    """The innermost function containing `target`, by parent walk."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    cur = target
    while id(cur) in parents:
        cur = parents[id(cur)]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


# --- the arms --------------------------------------------------------------------------


def audit(root: Path = REPO_DEFAULT, *, entry_points=ENTRY_POINTS,
          expected_closure=EXPECTED_CLOSURE) -> GuardReport:
    report = GuardReport()
    closure, sources, raw_imports = walk_closure(root, entry_points)
    report.closure = closure
    report.modules_parsed = len(sources)

    # --- arm 1: the closure is an EQUALITY ------------------------------------
    if set(closure) != set(expected_closure):
        for extra in sorted(set(closure) - set(expected_closure)):
            report.add("closure", extra,
                       "module is reachable from the production-observation "
                       "entry point and is not on the reviewed list")
        for missing in sorted(set(expected_closure) - set(closure)):
            report.add("closure", missing,
                       "reviewed dependency has DISAPPEARED from the closure; "
                       "the audit may be running against a gutted tree")
    for module in sorted(closure):
        for bad in FORBIDDEN_MODULE_PREFIXES:
            if module == bad or module.startswith(bad + "."):
                report.add("closure", module,
                           f"forbidden dependency family {bad!r} is reachable")
        if _module_file(root, module) is None:
            report.add("closure", module,
                       "module is imported by the closure but does not resolve "
                       "to a file")

    # anti-vacuity: the permitted things must EXIST.
    for required in ("app.realtime.collector", "app.realtime.ws_transport",
                     "app.realtime.auth", "app.realtime.kalshi"):
        if required not in sources:
            report.add("anti_vacuity", required,
                       "the module the whole audit is about was not parsed")

    # --- arm 2: no HTTP client, no raw socket ---------------------------------
    holder_has_websockets = False
    for module, imports in raw_imports.items():
        for name in sorted(imports):
            head = name.split(".")[0]
            for bad in FORBIDDEN_NETWORK_MODULES:
                if name == bad or name.startswith(bad + ".") or head == bad:
                    report.add("http", module,
                               f"imports {name!r}: an HTTP/socket client makes "
                               "a REST order route reachable")
                    break
            if head == PERMITTED_NETWORK_MODULE:
                if module != PERMITTED_NETWORK_HOLDER:
                    report.add("http", module,
                               f"imports {name!r}; the venue socket belongs to "
                               f"{PERMITTED_NETWORK_HOLDER} alone")
                else:
                    holder_has_websockets = True
    if not holder_has_websockets:
        report.add("anti_vacuity", PERMITTED_NETWORK_HOLDER,
                   "the PERMITTED network dependency is absent — this arm "
                   "would pass over a transport that cannot connect at all")
    # `socket`, at attribute level. One holder, one attribute.
    for module, imports in raw_imports.items():
        if not any(name == "socket" or name.startswith("socket.")
                   for name in imports):
            continue
        if module != SOCKET_HOLDER:
            report.add("http", module,
                       "imports `socket`; only "
                       f"{SOCKET_HOLDER} may, and only for "
                       f"{sorted(SOCKET_PERMITTED_ATTRS)}")
            continue
        used = {n.attr for n in ast.walk(sources[module])
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "socket"}
        stray = sorted(used - set(SOCKET_PERMITTED_ATTRS))
        if stray:
            report.add("http", module,
                       f"uses socket.{stray}; only "
                       f"{sorted(SOCKET_PERMITTED_ATTRS)} is a host-name "
                       "lookup rather than an outbound capability")
        if not used:
            report.add("anti_vacuity", module,
                       "`socket` is imported but never used; the attribute-level "
                       "exemption is excusing nothing")

    # --- arm 3: the signing-route closure -------------------------------------
    kalshi = sources.get("app.realtime.kalshi")
    if kalshi is None:
        report.add("anti_vacuity", "app.realtime.kalshi", "not parsed")
    else:
        routes = _extract_signing_routes(kalshi)
        if routes is None:
            report.add("signing_routes", "app.realtime.kalshi",
                       "AUTH_PURPOSE_ROUTES could not be read statically; a "
                       "signing map that is computed is a signing map that is "
                       "not closed")
        else:
            if set(routes) != set(EXPECTED_SIGNING_ROUTES):
                report.add("signing_routes", "app.realtime.kalshi",
                           f"signable routes {sorted(routes)} != the reviewed "
                           f"set {sorted(EXPECTED_SIGNING_ROUTES)}")
            for method, path in sorted(routes):
                if method != "GET":
                    report.add("signing_routes", "app.realtime.kalshi",
                               f"{method} {path} is signable; only GET may be")
            # anti-vacuity: signing must still be POSSIBLE for the handshake.
            if ("GET", "/trade-api/ws/v2") not in routes:
                report.add("anti_vacuity", "app.realtime.kalshi",
                           "the websocket handshake route is missing — the "
                           "closed set is closed around nothing")
        methods = _assigned_literal(kalshi, "ALLOWED_HTTP_METHODS")
        if tuple(methods or ()) != EXPECTED_HTTP_METHODS:
            report.add("signing_routes", "app.realtime.kalshi",
                       f"ALLOWED_HTTP_METHODS is {methods!r}, not "
                       f"{EXPECTED_HTTP_METHODS!r}")
        modes = _assigned_literal(kalshi, "IMPLEMENTED_MODES")
        if tuple(modes or ()) != EXPECTED_IMPLEMENTED_MODES:
            report.add("modes", "app.realtime.kalshi",
                       f"IMPLEMENTED_MODES is {modes!r}; this build implements "
                       f"{EXPECTED_IMPLEMENTED_MODES!r} only")
        allowed = _assigned_literal(kalshi, "ALLOWED_CHANNELS")
        if set(allowed or ()) != set(EXPECTED_ALLOWED_CHANNELS):
            report.add("channels", "app.realtime.kalshi",
                       f"ALLOWED_CHANNELS is {allowed!r}, not the reviewed "
                       f"market-data set")
        forbidden = _assigned_literal(kalshi, "FORBIDDEN_CHANNELS")
        missing_private = set(EXPECTED_FORBIDDEN_CHANNELS) - set(forbidden or ())
        if missing_private:
            report.add("channels", "app.realtime.kalshi",
                       f"private user streams {sorted(missing_private)} are no "
                       "longer named as forbidden")

    # --- arm 4: the outbound venue-write closure ------------------------------
    transport = sources.get("app.realtime.ws_transport")
    if transport is None:
        report.add("anti_vacuity", "app.realtime.ws_transport", "not parsed")
    else:
        sendable = _assigned_literal(transport, "SENDABLE_COMMANDS")
        if sendable is None:
            sendable = _assigned_literal(kalshi, "SENDABLE_COMMANDS") if kalshi else None
        if sendable is None:
            report.add("sendable", "app.realtime.ws_transport",
                       "SENDABLE_COMMANDS is not a module-level literal; the "
                       "outbound command set is not statically closed")
        elif set(sendable) != set(EXPECTED_SENDABLE_COMMANDS):
            report.add("sendable", "app.realtime.ws_transport",
                       f"outbound commands {sorted(sendable)} != the "
                       "protocol-required subscription/control set "
                       f"{sorted(EXPECTED_SENDABLE_COMMANDS)}")
        report.findings.extend(_audit_send_sites(sources))

    # --- arms 5 and 6: the tripwires ------------------------------------------
    exempt_seen = set()
    for module in sorted(sources):
        idents = collect_identifiers(sources[module])
        report.identifiers_scanned += len(idents)
        for ident in sorted(idents):
            if ident in IDENTIFIER_EXEMPTIONS:
                exempt_seen.add(ident)
                continue
            words = identifier_words(ident)
            hit = sorted(set(words) & FORBIDDEN_WORDS)
            if hit:
                report.add("identifiers", module,
                           f"{ident!r} contains the forbidden word(s) {hit}")
            for i in range(len(words) - 1):
                pair = (words[i], words[i + 1])
                if pair in FORBIDDEN_BIGRAMS:
                    report.add("identifiers", module,
                               f"{ident!r} contains the forbidden pair {pair}")
        for text in collect_non_docstring_strings(sources[module]):
            match = FORBIDDEN_ROUTE_RE.search(text)
            if match:
                report.add("routes_in_strings", module,
                           f"non-docstring literal matches {match.group(0)!r}")
    stale = set(IDENTIFIER_EXEMPTIONS) - exempt_seen
    if stale:
        report.add("anti_vacuity", "IDENTIFIER_EXEMPTIONS",
                   f"exemption(s) {sorted(stale)} match nothing in the closure; "
                   "an exemption list that excuses nothing is a list nobody "
                   "re-reads")

    # anti-vacuity: a tree that parsed nothing must not look clean.
    if report.identifiers_scanned < 500:
        report.add("anti_vacuity", "-",
                   f"only {report.identifiers_scanned} identifiers were "
                   "scanned; the audit is not looking at a real closure")
    return report


def _extract_signing_routes(kalshi_tree: ast.AST):
    """`AUTH_PURPOSE_ROUTES` read as a set of `(method, path)` pairs.

    The mapping's VALUES are what matter — the keys are enum members and the
    enum is only a name for the route. Reading the values means renaming
    `WEBSOCKET_HANDSHAKE` changes nothing here, while adding a third route is
    immediately red.
    """
    pairs = _assigned_literal(kalshi_tree, "AUTH_PURPOSE_ROUTES")
    if pairs is None:
        return None
    routes = set()
    for entry in pairs:
        # `_resolve` returns a dict as a tuple of (key, value) pairs.
        if not (isinstance(entry, tuple) and len(entry) == 2):
            return None
        value = entry[1]
        if not (isinstance(value, tuple) and len(value) == 2):
            return None
        if not all(isinstance(half, str) for half in value):
            return None
        routes.add((value[0], value[1]))
    return frozenset(routes)


# The one function permitted to write to the raw connection, and the closed set
# of builders whose output is the only thing any other site may hand to a
# transport. Named here so each check is about a SITE and an ARGUMENT, never
# about a spelling.
GOVERNED_WRITER = "_send_governed"
GOVERNANCE_CALL = "assert_sendable"
PERMITTED_BUILDERS = frozenset({
    "build_subscribe", "build_unsubscribe", "build_get_snapshot",
    "build_resubscribe", "build_resubscribe_snapshot",
})


def _audit_send_sites(sources) -> list:
    """Two rules, and together they close the outbound path.

    1. **Inside the transport**, the raw-connection write (`conn.send(...)`) may
       occur only inside `_send_governed`, and that function must call
       `assert_sendable`. This is CP1's property, re-asserted from outside the
       module that owns it.
    2. **Everywhere else in the closure**, a `.send(...)` call may pass exactly
       one argument and that argument must be a direct call to one of the closed
       `kalshi.build_*` builders. A dict literal, a variable, a `**kwargs`
       splat or a helper's return value is a finding.

    Rule 2 is the one that matters for this milestone. `assert_sendable`
    already rebuilds and compares the frame at runtime, so a hand-rolled order
    command would be refused — but "refused at runtime" and "cannot be written"
    are different claims, and P4 is being asked for the second one. With rule 2
    the collector has no expressible way to name a frame that is not a
    subscription or control message; the argument is the builder call itself.
    """
    out = []
    for module in sorted(sources):
        tree = sources[module]
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            if not isinstance(node, ast.Call) or not isinstance(func, ast.Attribute):
                continue
            if func.attr != "send":
                continue
            enclosing = _enclosing_function(tree, node)
            where = enclosing.name if enclosing is not None else "<module level>"
            receiver = func.value.id if isinstance(func.value, ast.Name) else "<expr>"
            if module == PERMITTED_NETWORK_HOLDER and receiver == RAW_CONNECTION_NAME:
                if where != GOVERNED_WRITER:
                    out.append(Finding(
                        "sendable", module,
                        f"a raw `{RAW_CONNECTION_NAME}.send(...)` write sits in "
                        f"{where!r}, not in the one governed writer "
                        f"{GOVERNED_WRITER!r}"))
                continue
            if node.keywords or len(node.args) != 1:
                out.append(Finding(
                    "sendable", module,
                    f"the `.send(...)` call in {where!r} does not pass exactly "
                    "one positional builder frame"))
                continue
            argument = node.args[0]
            if not (isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Name)
                    and argument.func.id in PERMITTED_BUILDERS):
                shape = type(argument).__name__
                named = (argument.func.id
                         if isinstance(argument, ast.Call)
                         and isinstance(argument.func, ast.Name) else shape)
                out.append(Finding(
                    "sendable", module,
                    f"the `.send(...)` call in {where!r} passes {named!r}, "
                    f"which is not one of the closed builders "
                    f"{sorted(PERMITTED_BUILDERS)}"))

    # anti-vacuity: the governed writer must exist, must govern, and the
    # builders must actually be used — an arm that never matched a real send
    # site would pass over a collector that cannot subscribe at all.
    transport = sources.get(PERMITTED_NETWORK_HOLDER)
    if transport is not None:
        writers = [n for n in ast.walk(transport)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == GOVERNED_WRITER]
        if not writers:
            out.append(Finding("anti_vacuity", PERMITTED_NETWORK_HOLDER,
                               f"{GOVERNED_WRITER!r} does not exist; the send "
                               "arm is guarding a path that is not there"))
        else:
            called = {n.func.id for n in ast.walk(writers[0])
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if GOVERNANCE_CALL not in called:
                out.append(Finding(
                    "sendable", PERMITTED_NETWORK_HOLDER,
                    f"{GOVERNED_WRITER!r} does not call {GOVERNANCE_CALL!r}; "
                    "the one write path is ungoverned"))
    builder_sites = 0
    for tree in sources.values():
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)
                    and func.attr == "send" and node.args
                    and isinstance(node.args[0], ast.Call)
                    and isinstance(node.args[0].func, ast.Name)
                    and node.args[0].func.id in PERMITTED_BUILDERS):
                builder_sites += 1
    if builder_sites < 2:
        out.append(Finding(
            "anti_vacuity", "-",
            f"only {builder_sites} builder-argument send site(s) found; the "
            "subscribe and recovery paths must both be present"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=str(REPO_DEFAULT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(Path(args.repo_root))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print("KALSHI production-observation structural guard")
        print(f"  entry points      : {', '.join(ENTRY_POINTS)}")
        print(f"  modules in closure: {len(report.closure)}")
        print(f"  identifiers scanned: {report.identifiers_scanned}")
        if report.clean:
            print("  VERDICT: CLEAN — no order/portfolio/execution surface is "
                  "reachable from the production-observation entry point.")
        else:
            print(f"  VERDICT: {len(report.findings)} FINDING(S)")
            for finding in report.findings:
                print(f"    [{finding.arm}] {finding.module}: {finding.detail}")
    return 0 if report.clean else 1


if __name__ == "__main__":      # pragma: no cover - operator entry point
    sys.exit(main())
