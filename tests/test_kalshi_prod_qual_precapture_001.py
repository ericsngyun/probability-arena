"""KALSHI-PROD-QUAL-PRECAPTURE — the pre-capture guards, and their red states.

`KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001`'s capture phase is BLOCKED on an
operator (no production credential), so nothing here connects, captures, or
reads a credential. What it proves is that when the credential arrives the
capture is a short, well-guarded step.

Three deliverables, and the second half of each is the part that matters:

* the **structural order-API guard** is clean on this tree — AND goes red on
  each of seven injected violations, in a scratch copy of the closure. A guard
  whose red state has never been observed is a guard that has only been
  observed passing (AGENTS.md doctrine 7).
* the **session-root rule** refuses a second session against one archive root —
  AND a fresh root works, so the refusal is a rule and not a breakage.
* the **endpoint disagreement** is recorded and reported, never silently
  resolved.

The scratch-copy technique is the anti-vacuity instrument throughout: the real
closure is copied into `tmp_path`, one line is added, and the guard is asked
the same question about the mutated tree. `app/` is never mutated — the
KALSHI-ARCHIVE lesson about `meta_mutation/campaign.py` corrupting the live
tree is recent enough to be worth respecting explicitly.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from app.realtime.kalshi import ENV_PRODUCTION, WS_HOSTS  # noqa: E402
from app.realtime.session_root import (  # noqa: E402
    SESSION_CLAIM_FIELDS,
    SESSION_CLAIM_FILENAME,
    SessionRootConflict,
    SessionRootCorrupt,
    SessionRootError,
    claim_session_root,
    new_session_id,
    open_session_root,
    read_session_claim,
    session_claim_path,
)


def _load_script(name: str):
    """Import a `scripts/` module by path — `scripts/` is not a package.

    `sys.modules` is populated BEFORE `exec_module`, because `@dataclass` reads
    `sys.modules[cls.__module__]` while it processes the class and raises an
    unhelpful `AttributeError` on `None` otherwise.
    """
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GUARD = _load_script("kalshi_prod_observation_guard")
PREFLIGHT = _load_script("kalshi_prod_precapture_preflight")


# ---------------------------------------------------------------------------
# scratch copies of the closure
# ---------------------------------------------------------------------------


def _copy_closure(tmp_path: Path) -> Path:
    """A minimal tree containing exactly the audited closure, and nothing else.

    Only the closure's own files are copied, so an injected import of a module
    that does not exist stays unresolvable — which is itself a finding, and is
    the honest reading: a dependency the audit cannot see is a dependency the
    audit cannot clear.
    """
    root = tmp_path / "scratch"
    for module in sorted(GUARD.EXPECTED_CLOSURE):
        source = GUARD._module_file(REPO, module)
        assert source is not None, module
        relative = source.relative_to(REPO)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
    # package markers the closure walk needs but does not itself audit
    for package in ("app", "app/telemetry"):
        marker = root / package / "__init__.py"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text("")
    return root


def _mutate(root: Path, module: str, addition: str) -> None:
    path = GUARD._module_file(root, module)
    assert path is not None, module
    path.write_text(path.read_text() + "\n" + addition + "\n")


def _arms(report) -> set:
    return {finding.arm for finding in report.findings}


# ---------------------------------------------------------------------------
# DELIVERABLE 1 — the structural order-API guard
# ---------------------------------------------------------------------------


class TestStructuralOrderApiGuard:
    """The guard is green on this tree, and every arm is looking at real data."""

    def test_the_real_tree_is_clean(self):
        report = GUARD.audit(REPO)
        assert report.clean, [f.to_dict() for f in report.findings]

    def test_the_closure_is_the_reviewed_one_and_is_an_equality(self):
        report = GUARD.audit(REPO)
        assert set(report.closure) == set(GUARD.EXPECTED_CLOSURE)
        # anti-vacuity: the closure contains the modules the audit is ABOUT.
        for required in ("app.realtime.collector", "app.realtime.ws_transport",
                         "app.realtime.auth", "app.realtime.kalshi",
                         "app.realtime.archive"):
            assert required in report.closure

    def test_the_arms_read_real_values_and_not_defaults(self):
        """Doctrine 7's failure class is a plausible benign value from a broken
        path. Every arm's input is asserted here to be the value actually in
        the source, so a parse that silently produced nothing cannot be the
        reason the audit is clean."""
        _, sources, _ = GUARD.walk_closure(REPO)
        kalshi = sources["app.realtime.kalshi"]
        transport = sources["app.realtime.ws_transport"]
        assert GUARD._extract_signing_routes(kalshi) == GUARD.EXPECTED_SIGNING_ROUTES
        assert GUARD._assigned_literal(kalshi, "IMPLEMENTED_MODES") == ("OBSERVE_ONLY",)
        assert GUARD._assigned_literal(kalshi, "ALLOWED_HTTP_METHODS") == ("GET",)
        assert set(GUARD._assigned_literal(kalshi, "ALLOWED_CHANNELS")) == \
            set(GUARD.EXPECTED_ALLOWED_CHANNELS)
        assert set(GUARD._assigned_literal(transport, "SENDABLE_COMMANDS")) == \
            set(GUARD.EXPECTED_SENDABLE_COMMANDS)
        report = GUARD.audit(REPO)
        assert report.modules_parsed == len(GUARD.EXPECTED_CLOSURE)
        assert report.identifiers_scanned > 2000

    def test_no_forbidden_module_family_is_reachable(self):
        report = GUARD.audit(REPO)
        for module in report.closure:
            for banned in GUARD.FORBIDDEN_MODULE_PREFIXES:
                assert not (module == banned or module.startswith(banned + ".")), module

    def test_identifier_words_split_the_way_the_guard_claims(self):
        """The word split is what lets the tripwire target order SURFACES
        without firing on `OrderBook`, `_read_order` or `_TRUNCATION_ORDER` —
        all of which mean *ordering*, and two of which are in the closure."""
        assert GUARD.identifier_words("OrderBook") == ["order", "book"]
        assert GUARD.identifier_words("orderbook_delta") == ["orderbook", "delta"]
        assert GUARD.identifier_words("_TRUNCATION_ORDER") == ["truncation", "order"]
        assert GUARD.identifier_words("place_order") == ["place", "order"]
        # `order` alone is deliberately NOT a forbidden word — three legitimate
        # identifiers in the closure contain it. The forbidden thing is the PAIR.
        assert "order" not in GUARD.FORBIDDEN_WORDS
        assert ("place", "order") in GUARD.FORBIDDEN_BIGRAMS
        assert ("order", "id") in GUARD.FORBIDDEN_BIGRAMS
        assert ("order", "book") not in GUARD.FORBIDDEN_BIGRAMS
        # and `fill` IS a forbidden word: the private channel has no
        # market-data homonym in this closure.
        assert "fill" in GUARD.FORBIDDEN_WORDS

    def test_a_docstring_naming_a_forbidden_route_does_not_trip_the_guard(self,
                                                                         tmp_path):
        """CP2's lesson: the module's own docstring discusses the seam, so the
        guard must read structure and not text. Here the WORST case — a
        docstring that names an order route in as many ways as possible."""
        root = _copy_closure(tmp_path)
        path = GUARD._module_file(root, "app.realtime.collector")
        source = path.read_text()
        prose = ('"""This lane must never POST /trade-api/v2/portfolio/orders, '
                 'never place_order, never cancel_order, and holds no wallet '
                 'or position. Forbidden: fills, portfolio, expected_value."""')
        path.write_text(source + "\n\ndef _boundary_statement():\n    " + prose
                        + "\n    return None\n")
        report = GUARD.audit(root)
        assert report.clean, [f.to_dict() for f in report.findings]

    def test_the_RUNTIME_closure_matches_the_static_one(self):
        """The static walk's one blind spot, closed by measurement.

        An `importlib.import_module` or `__import__` inside the closure would
        not appear in the AST import set. So a FRESH interpreter imports the
        production-observation entry point and reports what actually landed in
        `sys.modules`. The runtime set may legitimately be a SUBSET (some
        imports are function-local), but it must never contain an `app.*`
        module the static equality does not know about.

        `app` and `app.telemetry` are implicit parent packages — Python creates
        them to hold a submodule and nothing imports them by name — so they are
        named here rather than added to the reviewed closure, which would have
        weakened the equality for two empty `__init__.py` files.
        """
        probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "import app.realtime.collector\n"
            "import json\n"
            "print(json.dumps({\n"
            "  'app': sorted(m for m in sys.modules"
            "         if m == 'app' or m.startswith('app.')),\n"
            "  'roots': sorted({m.split('.')[0] for m in sys.modules}),\n"
            "}))\n" % str(REPO))
        proc = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO),
                              capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        implicit_parents = {"app", "app.telemetry"}
        runtime = set(payload["app"])
        unexpected = runtime - set(GUARD.EXPECTED_CLOSURE) - implicit_parents
        assert unexpected == set(), unexpected
        # anti-vacuity: the entry point really was imported, and the permitted
        # network dependency really did load.
        assert "app.realtime.collector" in runtime
        assert "websockets" in payload["roots"]
        # No outbound HTTP client, no database, no chain library, at RUNTIME.
        # Stdlib `urllib` is deliberately NOT asserted here: any third-party
        # dependency may pull it in, so it is checked where it belongs — in the
        # closure's own declared imports, by the `http` arm.
        for banned in ("requests", "httpx", "aiohttp", "urllib3", "sqlalchemy",
                       "psycopg2", "solana", "solders", "web3", "eth_account"):
            assert banned not in payload["roots"], banned

    def test_the_guard_cli_exits_zero_on_this_tree(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "kalshi_prod_observation_guard.py"),
             "--repo-root", str(REPO), "--json"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["clean"] is True
        assert payload["findings"] == []
        assert len(payload["closure"]) == len(GUARD.EXPECTED_CLOSURE)


class TestStructuralGuardRedStates:
    """**The mandatory half.** Each test introduces one order/execution surface
    into a scratch copy and proves the guard goes red. A guard that has never
    been seen red is a guard nobody has tested."""

    def test_the_scratch_copy_itself_is_clean(self, tmp_path):
        """The control. Without it, every red below could be an artifact of
        copying rather than of the violation."""
        root = _copy_closure(tmp_path)
        report = GUARD.audit(root)
        assert report.clean, [f.to_dict() for f in report.findings]

    def test_red_on_an_order_module_dependency(self, tmp_path):
        root = _copy_closure(tmp_path)
        _mutate(root, "app.realtime.collector",
                "import app.services.order_execution")
        report = GUARD.audit(root)
        assert not report.clean
        assert "closure" in _arms(report)
        assert any("app.services.order_execution" in f.module
                   for f in report.findings)

    def test_red_on_an_http_client_reaching_the_closure(self, tmp_path):
        """The arm that makes 'cannot reach an order API' true by construction:
        with no HTTP client there is no REST route to call, whatever it is
        named. Adding one must be red even though the import is innocent on
        its face."""
        root = _copy_closure(tmp_path)
        _mutate(root, "app.realtime.collector", "import httpx")
        report = GUARD.audit(root)
        assert not report.clean
        assert "http" in _arms(report)

    def test_red_on_a_third_signing_route(self, tmp_path):
        """A renamed enum member changes nothing; a new ROUTE is immediate."""
        root = _copy_closure(tmp_path)
        path = GUARD._module_file(root, "app.realtime.kalshi")
        source = path.read_text()
        source = source.replace(
            'AuthPurpose.API_KEY_METADATA: ("GET", API_KEYS_PATH),',
            'AuthPurpose.API_KEY_METADATA: ("GET", API_KEYS_PATH),\n'
            '    "third": ("POST", "/trade-api/v2/portfolio/orders"),')
        path.write_text(source)
        report = GUARD.audit(root)
        assert not report.clean
        assert "signing_routes" in _arms(report)
        assert any("only GET may be" in f.detail for f in report.findings)

    def test_red_on_a_fourth_outbound_command(self, tmp_path):
        root = _copy_closure(tmp_path)
        path = GUARD._module_file(root, "app.realtime.ws_transport")
        path.write_text(path.read_text().replace(
            'SENDABLE_COMMANDS = ("subscribe", "unsubscribe", "update_subscription")',
            'SENDABLE_COMMANDS = ("subscribe", "unsubscribe", '
            '"update_subscription", "create_order")'))
        report = GUARD.audit(root)
        assert not report.clean
        assert "sendable" in _arms(report)

    def test_red_on_a_send_site_that_does_not_pass_a_builder_frame(self, tmp_path):
        """The strongest arm. `assert_sendable` would refuse this at runtime —
        but 'refused at runtime' and 'cannot be written' are different claims,
        and P4 is being asked for the second."""
        root = _copy_closure(tmp_path)
        _mutate(root, "app.realtime.collector",
                "async def _extra(transport, frame):\n"
                "    await transport.send(frame)")
        report = GUARD.audit(root)
        assert not report.clean
        assert "sendable" in _arms(report)
        assert any("closed builders" in f.detail for f in report.findings)

    def test_red_on_a_raw_connection_write_outside_the_governed_writer(self, tmp_path):
        root = _copy_closure(tmp_path)
        _mutate(root, "app.realtime.ws_transport",
                "async def _bypass(conn, text):\n"
                "    await conn.send(text)")
        report = GUARD.audit(root)
        assert not report.clean
        assert any("governed writer" in f.detail for f in report.findings)

    def test_red_on_a_forbidden_identifier_however_it_is_spelled(self, tmp_path):
        root = _copy_closure(tmp_path)
        _mutate(root, "app.realtime.collector",
                "def resting_order_id(self):\n    return None")
        report = GUARD.audit(root)
        assert not report.clean
        assert "identifiers" in _arms(report)

    def test_red_on_a_private_channel_leaving_the_forbidden_list(self, tmp_path):
        root = _copy_closure(tmp_path)
        path = GUARD._module_file(root, "app.realtime.kalshi")
        path.write_text(path.read_text().replace(
            'FORBIDDEN_CHANNELS = ("fill", "market_positions", "user_orders",',
            'FORBIDDEN_CHANNELS = ("market_positions", "user_orders",'))
        report = GUARD.audit(root)
        assert not report.clean
        assert "channels" in _arms(report)

    def test_red_on_a_capability_mode_beyond_observe_only(self, tmp_path):
        root = _copy_closure(tmp_path)
        path = GUARD._module_file(root, "app.realtime.kalshi")
        path.write_text(path.read_text().replace(
            "IMPLEMENTED_MODES = (OBSERVE_ONLY,)",
            "IMPLEMENTED_MODES = (OBSERVE_ONLY, DEMO_EXECUTION)"))
        report = GUARD.audit(root)
        assert not report.clean
        assert "modes" in _arms(report)

    def test_red_on_an_order_route_in_a_non_docstring_literal(self, tmp_path):
        root = _copy_closure(tmp_path)
        _mutate(root, "app.realtime.collector",
                'ROUTE = "/trade-api/v2/portfolio/orders"')
        report = GUARD.audit(root)
        assert not report.clean
        assert "routes_in_strings" in _arms(report)

    def test_red_on_a_socket_used_for_anything_but_the_host_name(self, tmp_path):
        """The attribute-level exemption is the narrowest one in the guard, so
        it gets its own red state: `gethostname` is permitted, connecting is
        not."""
        root = _copy_closure(tmp_path)
        _mutate(root, "app.telemetry.schema",
                "def _reach(host):\n    return socket.create_connection(host)")
        report = GUARD.audit(root)
        assert not report.clean
        assert "http" in _arms(report)

    def test_an_empty_tree_does_not_look_clean(self, tmp_path):
        """Doctrine 4: a guard satisfied by a repository in which nothing works
        is not a guard. Every ban below is trivially satisfied here."""
        root = tmp_path / "empty"
        (root / "app" / "realtime").mkdir(parents=True)
        for marker in ("app/__init__.py", "app/realtime/__init__.py",
                       "app/realtime/collector.py"):
            (root / marker).write_text("")
        report = GUARD.audit(root)
        assert not report.clean
        assert "anti_vacuity" in _arms(report)


# ---------------------------------------------------------------------------
# DELIVERABLE 2 — one immutable archive root per collection session
# ---------------------------------------------------------------------------


class TestSessionRoot:

    def test_a_fresh_root_is_claimed_and_the_claim_is_durable(self, tmp_path):
        session = new_session_id()
        claim = claim_session_root(tmp_path, ENV_PRODUCTION, session_id=session)
        assert claim.session_id == session
        assert claim.already_existed is False
        path = session_claim_path(tmp_path, ENV_PRODUCTION)
        assert path.is_file()
        assert path.name == SESSION_CLAIM_FILENAME
        # persisted, not merely returned
        durable = read_session_claim(tmp_path, ENV_PRODUCTION)
        assert durable.session_id == session
        for field in SESSION_CLAIM_FIELDS:
            assert field in json.loads(path.read_text())
        claim.assert_durable()

    def test_a_second_session_on_one_root_is_REFUSED(self, tmp_path):
        """The positive control the milestone asks for. Forcing the condition
        must make the refusal fire — testing only the healthy state proves only
        the healthy state."""
        first = new_session_id()
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=first)
        second = new_session_id()
        assert second != first
        with pytest.raises(SessionRootConflict) as excinfo:
            claim_session_root(tmp_path, ENV_PRODUCTION, session_id=second)
        message = str(excinfo.value)
        assert "ONE ARCHIVE ROOT PER COLLECTION SESSION" in message
        assert first in message and second in message
        assert "B4" in message
        # and the first session still owns it — a refused claim writes nothing
        assert read_session_claim(tmp_path, ENV_PRODUCTION).session_id == first

    def test_a_fresh_root_works_for_the_refused_session(self, tmp_path):
        """The other half of the control: the rule must be a rule, not a
        breakage. The session that was refused above succeeds on its own root."""
        first = new_session_id()
        claim_session_root(tmp_path / "a", ENV_PRODUCTION, session_id=first)
        second = new_session_id()
        claim = claim_session_root(tmp_path / "b", ENV_PRODUCTION,
                                   session_id=second)
        assert claim.session_id == second
        assert read_session_claim(tmp_path / "a", ENV_PRODUCTION).session_id == first

    def test_reentry_with_the_same_session_is_idempotent(self, tmp_path):
        """A reconnect is inside one session. A rule that refused its own
        session would push operators toward deleting claims."""
        session = new_session_id()
        first = claim_session_root(tmp_path, ENV_PRODUCTION, session_id=session)
        again = claim_session_root(tmp_path, ENV_PRODUCTION, session_id=session)
        assert again.session_id == session
        assert again.already_existed is True
        assert again.claim_digest == first.claim_digest

    def test_the_two_environments_of_one_root_are_claimed_separately(self, tmp_path):
        """Why the claim sits at the environment root: mixing is per
        environment, because `read_verified()` reads per environment."""
        demo = claim_session_root(tmp_path, "demo", session_id=new_session_id())
        prod = claim_session_root(tmp_path, ENV_PRODUCTION,
                                  session_id=new_session_id())
        assert demo.session_id != prod.session_id
        with pytest.raises(SessionRootConflict):
            claim_session_root(tmp_path, ENV_PRODUCTION,
                               session_id=new_session_id())

    def test_the_claim_is_immutable_on_the_filesystem(self, tmp_path):
        """`os.link` publishing, plus the absence of any rewrite function. The
        second half is asserted structurally: an update path that exists is an
        update path that will eventually be called."""
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=new_session_id())
        import app.realtime.session_root as module
        exported = {name for name in dir(module) if not name.startswith("_")}
        for forbidden in ("update_session_claim", "rewrite_session_claim",
                          "clear_session_claim", "release_session_root",
                          "delete_session_claim", "force_claim_session_root"):
            assert forbidden not in exported

    def test_an_edited_claim_is_CORRUPT_and_not_UNCLAIMED(self, tmp_path):
        """`None` means unclaimed. Garbage means an unknown session may own
        this root — and re-claiming it would be the silent mixing the module
        exists to prevent. Conflating them is the whole failure class."""
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=new_session_id())
        path = session_claim_path(tmp_path, ENV_PRODUCTION)
        payload = json.loads(path.read_text())
        payload["session_id"] = "s-forged-000000000000"
        path.unlink()
        path.write_text(json.dumps(payload))
        with pytest.raises(SessionRootCorrupt):
            read_session_claim(tmp_path, ENV_PRODUCTION)
        with pytest.raises(SessionRootCorrupt):
            claim_session_root(tmp_path, ENV_PRODUCTION,
                               session_id=new_session_id())

    def test_a_CANONICALLY_VALID_forgery_is_still_corrupt(self, tmp_path):
        """The test above could be passing on the parser rather than on the
        digest — a forged claim re-encoded canonically parses cleanly. This one
        isolates the digest check, and is the reason `claim_digest` is a field
        rather than a decoration."""
        from app.realtime.canonical import canonical_bytes, parse_canonical
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=new_session_id())
        path = session_claim_path(tmp_path, ENV_PRODUCTION)
        forged = parse_canonical(path.read_bytes())
        forged["session_id"] = "s-forged-000000000000"
        path.unlink()
        path.write_bytes(canonical_bytes(forged))
        with pytest.raises(SessionRootCorrupt) as excinfo:
            read_session_claim(tmp_path, ENV_PRODUCTION)
        assert "digest" in str(excinfo.value)

    def test_a_truncated_claim_is_corrupt(self, tmp_path):
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=new_session_id())
        path = session_claim_path(tmp_path, ENV_PRODUCTION)
        path.unlink()
        path.write_text('{"schema_version": 1}')
        with pytest.raises(SessionRootCorrupt):
            read_session_claim(tmp_path, ENV_PRODUCTION)

    def test_an_unclaimed_root_reads_as_None(self, tmp_path):
        assert read_session_claim(tmp_path, ENV_PRODUCTION) is None

    def test_an_empty_session_id_is_refused(self, tmp_path):
        for bad in ("", "   ", None, 7):
            with pytest.raises(SessionRootError):
                claim_session_root(tmp_path, ENV_PRODUCTION, session_id=bad)
        assert read_session_claim(tmp_path, ENV_PRODUCTION) is None

    def test_session_ids_are_unique(self):
        assert len({new_session_id() for _ in range(500)}) == 500

    def test_no_record_schema_version_was_bumped(self):
        """The contract is explicit that a session field on the record envelope
        is a schema decision outside its authority. This closes B4 by run rule
        and by sidecar — so the record schema must be untouched, and that is
        asserted rather than asserted-in-prose."""
        from app.realtime import segment
        source = (REPO / "app" / "realtime" / "segment.py").read_text()
        assert "session_id" not in source
        assert segment.RECORD_SCHEMA_VERSION == 1, segment.RECORD_SCHEMA_VERSION
        assert not any("session" in field for field in segment.RECORD_FIELDS)
        # anti-vacuity: the schema this asserts is untouched must be real.
        assert len(segment.RECORD_FIELDS) >= 10


class TestSessionRootIsReachableFromOutside:
    """Doctrine 5: reachability is asserted from OUTSIDE, by driving the real
    caller and proving observable state changes. `session_root.py` with a
    hundred green tests and no caller would be CP4 again."""

    def test_the_preflight_persists_the_session_before_any_transport_is_built(
            self, tmp_path):
        """The ORDERING requirement, proven rather than documented: the
        factory builder records whether the claim was already on disk at the
        moment it was invoked."""
        observed = {}

        def builder():
            observed["claim_on_disk"] = session_claim_path(
                tmp_path, ENV_PRODUCTION).is_file()
            return "a-transport-factory"

        result = PREFLIGHT.preflight(archive_root=tmp_path, repo_root=REPO,
                                     transport_factory_builder=builder)
        assert result["passed"] is True
        assert observed["claim_on_disk"] is True
        assert result["transport_factory"] == "a-transport-factory"
        assert result["socket_opened"] is False
        assert result["credential_read"] is False
        assert result["capture_attempted"] is False

    def test_the_preflight_builds_no_transport_when_the_root_is_taken(self,
                                                                     tmp_path):
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=new_session_id())
        called = []
        result = PREFLIGHT.preflight(
            archive_root=tmp_path, repo_root=REPO,
            session_id=new_session_id(),
            transport_factory_builder=lambda: called.append(1))
        assert result["passed"] is False
        assert called == []
        assert result["transport_factory"] is None
        session_gate = [g for g in result["gates"] if g["gate"] == "session_root"][0]
        assert "SessionRootConflict" in session_gate["error"]

    def test_the_preflight_builds_no_transport_when_the_guard_is_red(self,
                                                                     tmp_path):
        """A failing structural guard must stop everything downstream —
        nothing later can make an unsafe closure safe."""
        scratch = _copy_closure(tmp_path)
        _mutate(scratch, "app.realtime.collector", "import httpx")
        called = []
        result = PREFLIGHT.preflight(
            archive_root=tmp_path / "root", repo_root=scratch,
            transport_factory_builder=lambda: called.append(1))
        assert result["passed"] is False
        assert called == []
        assert result["gates"][0]["gate"] == "structural_guard"
        assert result["gates"][0]["passed"] is False
        # and no session was claimed, because the gate never ran
        assert read_session_claim(tmp_path / "root", ENV_PRODUCTION) is None

    def test_open_session_root_mints_and_verifies_in_one_call(self, tmp_path):
        claim = open_session_root(tmp_path, ENV_PRODUCTION)
        assert claim.session_id.startswith("s-")
        assert claim.already_existed is False
        assert session_claim_path(tmp_path, ENV_PRODUCTION).is_file()

    def test_the_preflight_cli_exits_nonzero_on_a_taken_root(self, tmp_path):
        claim_session_root(tmp_path, ENV_PRODUCTION, session_id=new_session_id())
        proc = subprocess.run(
            [sys.executable,
             str(REPO / "scripts" / "kalshi_prod_precapture_preflight.py"),
             "--archive-root", str(tmp_path)],
            capture_output=True, text=True, timeout=180)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "ONE ARCHIVE ROOT PER COLLECTION SESSION" in proc.stdout


# ---------------------------------------------------------------------------
# DELIVERABLE 3 — the endpoint, and the disagreement
# ---------------------------------------------------------------------------


class TestProductionEndpointIsRecordedNotResolved:

    def test_the_collector_would_use_the_documented_production_host(self):
        assert WS_HOSTS[ENV_PRODUCTION] == \
            "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

    def test_the_env_file_host_is_the_other_documented_host(self):
        assert PREFLIGHT.ENV_FILE_WS_URL == \
            "wss://api.elections.kalshi.com/trade-api/ws/v2"

    def test_the_disagreement_is_reported_and_not_silently_resolved(self):
        gate = PREFLIGHT.gate_endpoint()
        assert gate["hosts_agree"] is False
        assert gate["collector_would_connect_to"] == WS_HOSTS[ENV_PRODUCTION]
        assert gate["env_file_records"] == PREFLIGHT.ENV_FILE_WS_URL
        # An UNVERIFIED host must never read as verified. Nothing has connected.
        assert gate["verified_on_the_wire"] is False
        assert "RECORDED, NOT RESOLVED" in gate["note"]

    def test_the_env_setting_still_has_no_reader(self):
        """The finding that makes the disagreement non-operative:
        `settings.kalshi_ws_url` is read by nothing. The transport uses
        `WS_HOSTS[environment]`. If that ever changes, this test is the one
        that says so."""
        from app.config import Settings
        assert "kalshi_ws_url" in Settings.model_fields
        readers = subprocess.run(
            ["grep", "-rn", "--include=*.py", "kalshi_ws_url", str(REPO / "app")],
            capture_output=True, text=True)
        lines = [line for line in readers.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, lines
        assert lines[0].endswith(
            'kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"')

    def test_the_endpoint_note_exists_and_states_both_hosts(self):
        note = REPO / PREFLIGHT.ENDPOINT_NOTE_PATH
        assert note.is_file()
        text = note.read_text()
        assert "wss://external-api-ws.kalshi.com/trade-api/ws/v2" in text
        assert "wss://api.elections.kalshi.com/trade-api/ws/v2" in text
        assert "UNVERIFIED" in text


class TestNothingHereTouchesProduction:
    """The constraint this whole milestone runs under, asserted rather than
    promised."""

    def test_no_module_added_here_can_open_a_socket(self):
        import ast
        for name in ("app/realtime/session_root.py",
                     "scripts/kalshi_prod_observation_guard.py",
                     "scripts/kalshi_prod_precapture_preflight.py"):
            tree = ast.parse((REPO / name).read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            for banned in ("websockets", "socket", "requests", "httpx",
                           "aiohttp", "urllib", "ssl"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in imported), (name, banned)
            # anti-vacuity: the file really was parsed and has content.
            assert len([n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]) > 3

    def test_the_preflight_reads_no_credential(self):
        import ast
        tree = ast.parse(
            (REPO / "scripts" / "kalshi_prod_precapture_preflight.py").read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("load_observer_signer", "ReadOnlyRequestSigner",
                       "from_path", "KalshiWebsocketTransport", "connect",
                       "collect_once", "run_session"):
            assert banned not in names, banned
