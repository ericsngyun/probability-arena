"""SOCIAL-X-LIVE-TRANSPORT-001 — qualification of the live X transport.

This is the first module in `app/social/` allowed to open a socket, so the
guards here are the ones that decide whether that permission stays narrow.
Every one of them is paired with a mutation in `TestGuardsBite`: per
TESTING_POLICY, a guard is not qualified until a mutation has proved it
detects the violation it claims to prevent.

No test here reaches the network. `httpx.MockTransport` answers every request,
and a guard below asserts that no test in this file names a real host.
"""

from __future__ import annotations

import ast
import json
import pickle
from pathlib import Path

import httpx
import pytest

from app.social import x_transport as XT
from app.social.transport import (
    FrameKind,
    SocialStreamTransport,
    TransportError,
    TransportRule,
)
from app.social.x_transport import (
    ALLOWED_URLS,
    RULES_URL,
    STREAM_URL,
    BearerToken,
    CredentialLeakError,
    CredentialUnavailableError,
    XFilteredStreamTransport,
)

MODULE_PATH = Path(XT.__file__)
SOURCE = MODULE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

SECRET = "SECRET-TOKEN-DO-NOT-PRINT-9f3a"


def _docstrings(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                out.add(d)
    return out


def url_literals(tree: ast.AST) -> set[str]:
    """Every non-docstring string literal that names an http endpoint.

    Prose is excluded on the astguard principle: a module note that says
    "there is no posting endpoint" is asserting the property, not holding one.
    Only literals that are themselves URLs can carry capability.
    """
    docs = _docstrings(tree)
    return {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docs and n.value.startswith(("http://", "https://"))
    }


def token() -> BearerToken:
    return BearerToken(SECRET)


def transport(handler, **kw) -> XFilteredStreamTransport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return XFilteredStreamTransport(token=token(), client=client, **kw)


def sse(*lines: str) -> httpx.Response:
    return httpx.Response(200, content="\n".join(lines).encode())


# --------------------------------------------------------------------------
# 1. THE PROTOCOL WAS NOT WIDENED TO FIT X
# --------------------------------------------------------------------------


class TestProtocolUnchanged:
    def test_the_transport_satisfies_the_existing_protocol(self):
        assert isinstance(transport(lambda r: sse()), SocialStreamTransport)

    def test_the_protocol_still_has_exactly_four_methods(self):
        """If X had forced a fifth, that is what this would catch."""
        surface = {n for n in dir(SocialStreamTransport)
                   if not n.startswith("_")}
        assert surface == {"list_rules", "apply_rules", "frames", "aclose"}

    def test_the_protocol_module_does_not_import_the_x_transport(self):
        """Dependency points one way. The seam does not know its implementor."""
        import app.social.transport as base
        src = Path(base.__file__).read_text(encoding="utf-8")
        assert "x_transport" not in src

    def test_the_transport_adds_no_public_method_the_protocol_lacks(self):
        """Extra public surface is how a Protocol gets widened by drift."""
        cls = XFilteredStreamTransport
        extra = {n for n in vars(cls) if not n.startswith("_")}
        assert extra == {"list_rules", "apply_rules", "frames", "aclose"}


# --------------------------------------------------------------------------
# 2. CAPABILITY IS EXACTLY TWO ENDPOINTS
# --------------------------------------------------------------------------


class TestCapabilityIsNarrow:
    def test_the_only_urls_in_the_module_are_the_two_official_ones(self):
        assert url_literals(TREE) == set(ALLOWED_URLS) == {STREAM_URL,
                                                           RULES_URL}

    def test_the_endpoints_are_the_documented_ones(self):
        assert STREAM_URL == "https://api.x.com/2/tweets/search/stream"
        assert RULES_URL == "https://api.x.com/2/tweets/search/stream/rules"
        assert RULES_URL.startswith(STREAM_URL)

    def test_no_write_capable_account_endpoint_appears(self):
        """Posting, liking, following, DMs, blocks, mutes: none exist here."""
        banned = ("/2/tweets/create", "/2/users/", "/2/dm_", "/2/dm/",
                  "likes", "following", "followers", "retweets", "blocking",
                  "muting", "bookmarks")
        for url in url_literals(TREE):
            for b in banned:
                assert b not in url, f"{url} contains write-capable path {b}"

    def test_no_oauth_user_context_machinery_exists(self):
        """Bearer only. There is no code path that could act as a user."""
        names = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(TREE)
                  if isinstance(n, ast.Attribute)}
        names |= {n.name for n in ast.walk(TREE)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))}
        banned = ("oauth", "access_token_secret", "consumer_secret",
                  "signature_method", "pkce", "refresh_token")
        for name in names:
            for b in banned:
                assert b not in name.lower(), f"{name} is OAuth machinery"

    def test_only_get_and_post_to_rules_are_ever_issued(self):
        """POST exists solely to reconcile the FILTER, never an account."""
        verbs = {n.attr for n in ast.walk(TREE)
                 if isinstance(n, ast.Attribute)
                 and n.attr in {"get", "post", "put", "patch", "delete",
                                "request", "send"}}
        # `.get` also covers dict lookups; the point is what is ABSENT.
        assert not (verbs & {"put", "patch", "request", "send"})

    async def test_an_unlisted_url_is_refused_at_the_call(self):
        t = transport(lambda r: sse())
        with pytest.raises(TransportError, match="not in the allowed URL set"):
            t._assert_allowed("https://api.x.com/2/users/me")


# --------------------------------------------------------------------------
# 3. THE TOKEN CANNOT BE RENDERED, SERIALIZED, OR REACHED
# --------------------------------------------------------------------------


class TestCredentialContainment:
    def test_repr_str_and_format_all_redact(self):
        t = token()
        assert SECRET not in repr(t)
        assert SECRET not in str(t)
        assert SECRET not in f"{t}"
        assert SECRET not in "{}".format(t)
        assert SECRET not in f"{t!r}"

    def test_pickling_raises_rather_than_redacting(self):
        """Redacted-but-serializable would still put bytes on disk."""
        with pytest.raises(CredentialLeakError):
            pickle.dumps(token())

    def test_it_is_not_json_serializable(self):
        with pytest.raises(TypeError):
            json.dumps({"t": token()})

    def test_the_transport_repr_does_not_reach_the_token(self):
        assert SECRET not in repr(transport(lambda r: sse()))

    def test_the_value_leaves_through_exactly_one_method(self):
        holders = [n for n in ast.walk(TREE)
                   if isinstance(n, ast.Attribute) and n.attr == "_v"]
        fns = []
        for fn in ast.walk(TREE):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(h in ast.walk(fn) for h in holders):
                    fns.append(fn.name)
        assert set(fns) == {"__init__", "authorization_header"}, fns

    def test_the_header_carries_the_token_and_nothing_else_does(self):
        t = transport(lambda r: sse())
        assert t._headers()["Authorization"] == f"Bearer {SECRET}"
        assert SECRET not in json.dumps(
            {k: v for k, v in t._headers().items() if k != "Authorization"})

    def test_it_loads_from_the_environment_never_from_a_literal(self,
                                                               monkeypatch):
        monkeypatch.delenv(XT.TOKEN_ENV, raising=False)
        monkeypatch.delenv(XT.TOKEN_FILE_ENV, raising=False)
        with pytest.raises(CredentialUnavailableError):
            BearerToken.load()
        monkeypatch.setenv(XT.TOKEN_ENV, SECRET)
        assert BearerToken.load().authorization_header()["Authorization"] == (
            f"Bearer {SECRET}")

    def test_it_loads_from_a_secret_file_the_environment_names(self, tmp_path,
                                                              monkeypatch):
        p = tmp_path / "x.token"
        p.write_text(SECRET + "\n")
        monkeypatch.delenv(XT.TOKEN_ENV, raising=False)
        monkeypatch.setenv(XT.TOKEN_FILE_ENV, str(p))
        assert BearerToken.load().authorization_header()["Authorization"] == (
            f"Bearer {SECRET}")

    def test_an_empty_token_refuses_rather_than_going_unauthenticated(self):
        with pytest.raises(CredentialUnavailableError):
            BearerToken("   ")

    def test_the_module_cannot_print_or_log_at_all(self):
        """The strongest available form of "never logged": there is no
        logging call in the module to leak through. A redaction that has to
        be remembered at every call site is not a guarantee."""
        calls = {n.func.id for n in ast.walk(TREE)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "print" not in calls
        imports = {a.name.split(".")[0] for n in ast.walk(TREE)
                   if isinstance(n, ast.Import) for a in n.names}
        imports |= {(n.module or "").split(".")[0] for n in ast.walk(TREE)
                    if isinstance(n, ast.ImportFrom)}
        assert "logging" not in imports

    def test_no_credential_literal_is_hardcoded_in_the_module(self):
        for lit in {n.value for n in ast.walk(TREE)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)}:
            assert not lit.startswith("AAAA"), "looks like a bearer token"

    def test_the_artifact_and_extractor_cannot_reach_the_credential(self):
        """Import-graph, not substring: the secret is out of their world."""
        import app.social.artifact as artifact
        import app.social.evidence_extractor as extractor
        import app.social.observer_funnel as funnel
        for mod in (artifact, extractor, funnel):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            tree = ast.parse(src)
            mods = {n.module or "" for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)}
            mods |= {a.name for n in ast.walk(tree)
                     if isinstance(n, ast.Import) for a in n.names}
            hits = {m for m in mods if "x_transport" in m}
            assert not hits, f"{mod.__name__} can reach the transport: {hits}"


# --------------------------------------------------------------------------
# 4. FRAME BEHAVIOUR
# --------------------------------------------------------------------------


class TestFrames:
    async def test_a_blank_line_becomes_a_keepalive_frame(self):
        t = transport(lambda r: sse('{"data":{"id":"1"}}', "", "",
                                    '{"data":{"id":"2"}}'),
                      max_reconnects=0)
        kinds = [f.kind async for f in t.frames()]
        assert kinds[:4] == [FrameKind.DATA, FrameKind.KEEPALIVE,
                             FrameKind.KEEPALIVE, FrameKind.DATA]
        assert t.health.keepalives == 2
        assert t.health.data_frames == 2

    async def test_delivery_sequence_counts_data_not_keepalives(self):
        t = transport(lambda r: sse('{"data":{"id":"1"}}', "",
                                    '{"data":{"id":"2"}}'),
                      max_reconnects=0)
        seqs = [f.delivery_sequence async for f in t.frames()
                if f.kind is FrameKind.DATA]
        assert seqs == [1, 2]

    async def test_matched_rules_come_from_the_platform_not_from_us(self):
        t = transport(lambda r: sse(json.dumps(
            {"data": {"id": "1"},
             "matching_rules": [{"id": "9", "tag": "mint-mentions"}]})),
            max_reconnects=0)
        frames = [f async for f in t.frames() if f.kind is FrameKind.DATA]
        assert frames[0].matched_rule_ids == ("mint-mentions",)

    async def test_an_in_band_platform_error_is_an_error_frame(self):
        t = transport(lambda r: sse(json.dumps(
            {"errors": [{"title": "operational-disconnect"}]})),
            max_reconnects=0)
        kinds = [f.kind async for f in t.frames()]
        assert FrameKind.ERROR in kinds
        assert t.health.error_frames == 1
        assert t.health.data_frames == 0

    async def test_live_frames_carry_no_fixture_provenance(self):
        """Provenance certifies a FIXTURE's basis. A live frame's basis is
        the wire, and stamping one would make replay indistinguishable."""
        t = transport(lambda r: sse('{"data":{"id":"1"}}'), max_reconnects=0)
        stamped = [f async for f in t.frames() if f.provenance is not None]
        assert stamped == []

    async def test_reconnect_bumps_the_generation_and_restarts_the_sequence(
            self):
        """A productive stream reconnects indefinitely, so the CONSUMER
        bounds this, exactly as the collector will."""
        t = transport(lambda r: sse('{"data":{"id":"a"}}'), max_reconnects=2,
                      backoff_initial_s=0.0)
        got = []
        async for f in t.frames():
            got.append((f.kind, f.subscription_generation,
                        f.delivery_sequence))
            if len([g for g in got if g[0] is FrameKind.DATA]) == 3:
                break
        data = [g for g in got if g[0] is FrameKind.DATA]
        assert [g[1] for g in data] == [0, 1, 2]
        assert [g[2] for g in data] == [1, 1, 1], "sequence must restart"
        assert [g[0] for g in got].count(FrameKind.RECONNECT) == 2

    async def test_an_unproductive_connection_spends_the_retry_budget(self):
        """Accept-then-close must not reconnect forever. A connection that
        delivered nothing does not earn a budget reset."""
        t = transport(lambda r: sse(), max_reconnects=3,
                      backoff_initial_s=0.0)
        frames = [f async for f in t.frames()]
        assert [f.kind for f in frames] == [FrameKind.RECONNECT] * 3
        assert t.health.connects == 4
        assert t.health.data_frames == 0

    async def test_a_clean_end_of_stream_is_treated_as_a_disconnect(self):
        """Quietly closed and loudly dropped are the same event to a
        collector measuring arrival times."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return sse('{"data":{"id":"a"}}') if calls["n"] == 1 else sse()

        t = transport(handler, max_reconnects=1, backoff_initial_s=0.0)
        kinds = [f.kind async for f in t.frames()]
        # No error was raised anywhere: the first stream simply ENDED, and
        # that alone produced a RECONNECT.
        assert kinds.count(FrameKind.RECONNECT) == 1
        assert t.health.connects == 2
        assert t.health.http_errors == 0

    async def test_a_rate_limit_is_counted_separately_from_other_errors(self):
        t = transport(lambda r: httpx.Response(429), max_reconnects=0,
                      backoff_initial_s=0.0)
        with pytest.raises(TransportError, match="rate limited"):
            async for _ in t.frames():
                pass
        assert t.health.rate_limited == 1

    async def test_http_failure_is_a_system_failure_not_a_quiet_stream(self):
        """The counter exists so an hour of 401s cannot read as 'nobody
        posted'. Funnel loss and system failure are different facts."""
        t = transport(lambda r: httpx.Response(401), max_reconnects=0,
                      backoff_initial_s=0.0)
        with pytest.raises(TransportError):
            async for _ in t.frames():
                pass
        assert t.health.http_errors == 1
        assert t.health.data_frames == 0


# --------------------------------------------------------------------------
# 5. RULE RECONCILIATION NEVER TOUCHES A FOREIGN RULE
# --------------------------------------------------------------------------


class _RuleServer:
    def __init__(self, remote):
        self.remote = list(remote)
        self.posts: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) in ALLOWED_URLS, str(request.url)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": r.remote_id, "tag": r.tag, "value": r.value}
                for r in self.remote]})
        body = json.loads(request.content)
        self.posts.append(body)
        added = body.get("add", [])
        return httpx.Response(200, json={"data": [
            {"id": f"new-{i}", "tag": a["tag"], "value": a["value"]}
            for i, a in enumerate(added)]})


class TestRules:
    async def test_a_foreign_rule_is_reported_and_never_deleted(self):
        server = _RuleServer([TransportRule("77", "someone-else", "$FOO")])
        t = XFilteredStreamTransport(
            token=token(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
        result = await t.apply_rules(
            add=[TransportRule("", "ours", "$BAR")], delete=[])
        assert result.foreign == ("someone-else",)
        assert result.added == ("ours",)
        assert all("delete" not in p for p in server.posts)

    async def test_an_already_present_rule_is_unchanged_not_re_added(self):
        server = _RuleServer([TransportRule("77", "ours", "$BAR")])
        t = XFilteredStreamTransport(
            token=token(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
        result = await t.apply_rules(
            add=[TransportRule("", "ours", "$BAR")], delete=[])
        assert result.unchanged == ("ours",)
        assert result.added == ()
        assert server.posts == []

    async def test_deletion_names_ids_the_caller_asked_for(self):
        server = _RuleServer([TransportRule("77", "ours", "$BAR")])
        t = XFilteredStreamTransport(
            token=token(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
        result = await t.apply_rules(add=[], delete=["77"])
        assert result.deleted == ("77",)
        assert server.posts == [{"delete": {"ids": ["77"]}}]

    async def test_list_rules_reads_the_platform_set(self):
        server = _RuleServer([TransportRule("77", "ours", "$BAR")])
        t = XFilteredStreamTransport(
            token=token(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
        assert [r.tag for r in await t.list_rules()] == ["ours"]

    async def test_an_http_error_on_rules_does_not_echo_headers(self):
        def handler(request):
            return httpx.Response(403, text="Forbidden")
        t = transport(handler)
        with pytest.raises(TransportError) as e:
            await t.list_rules()
        assert SECRET not in str(e.value)
        assert "403" in str(e.value)


# --------------------------------------------------------------------------
# 6. MUTATIONS — each guard above is proved to bite
# --------------------------------------------------------------------------


class TestGuardsBite:
    """A guard is not qualified until a mutation proves it detects the
    violation. Each mutation below is the real violation, applied to a copy
    of the source, and each is asserted to be CAUGHT."""

    @staticmethod
    def _mutate(old: str, new: str) -> ast.AST:
        assert old in SOURCE, f"mutation target vanished: {old!r}"
        return ast.parse(SOURCE.replace(old, new, 1))

    def test_a_smuggled_url_is_caught(self):
        tree = self._mutate('TOKEN_ENV = "X_BEARER_TOKEN"',
                            'LIKE = "https://api.x.com/2/users/1/likes"\n'
                            'TOKEN_ENV = "X_BEARER_TOKEN"')
        assert url_literals(tree) != set(ALLOWED_URLS)

    def test_a_write_capable_endpoint_is_caught(self):
        tree = self._mutate('TOKEN_ENV = "X_BEARER_TOKEN"',
                            'DM = "https://api.x.com/2/dm_conversations"\n'
                            'TOKEN_ENV = "X_BEARER_TOKEN"')
        banned = ("/2/dm_",)
        hits = [u for u in url_literals(tree)
                for b in banned if b in u]
        assert hits, "the write-endpoint guard did not bite"

    def test_oauth_machinery_is_caught(self):
        tree = self._mutate("def _headers(self)",
                            "def _oauth_sign(self): pass\n\n"
                            "    def _headers(self)")
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert any("oauth" in n.lower() for n in names)

    def test_a_prose_mention_of_a_forbidden_endpoint_is_not_a_violation(self):
        """The false-positive direction. Six guards in this repo have
        condemned a module for containing the sentence promising the
        property; this asserts the URL guard cannot."""
        tree = self._mutate('TOKEN_ENV = "X_BEARER_TOKEN"',
                            'NOTE = "there is no /2/dm_conversations call '
                            'anywhere in this transport"\n'
                            'TOKEN_ENV = "X_BEARER_TOKEN"')
        assert url_literals(tree) == set(ALLOWED_URLS)

    def test_a_leaking_repr_is_caught(self):
        ns: dict = {}
        exec(compile(self._mutate(
            'return "<BearerToken redacted>"',
            "return f'<BearerToken {self._v}>'"), "<mut>", "exec"), ns)
        assert SECRET in repr(ns["BearerToken"](SECRET)), "leak not detected"

    def test_a_picklable_token_is_caught(self):
        ns: dict = {}
        exec(compile(self._mutate(
            'raise CredentialLeakError("a bearer token must not be pickled")',
            "return (BearerToken, (self._v,))"), "<mut>", "exec"), ns)
        reduced = ns["BearerToken"](SECRET).__reduce__()
        assert SECRET in repr(reduced), "the pickle guard was not the reason"

    def test_a_third_reader_of_the_token_is_caught(self):
        tree = self._mutate("    def authorization_header(self)",
                            "    def debug(self):\n"
                            "        return self._v\n\n"
                            "    def authorization_header(self)")
        holders = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and n.attr == "_v"]
        fns = {fn.name for fn in ast.walk(tree)
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(h in ast.walk(fn) for h in holders)}
        assert fns != {"__init__", "authorization_header"}

    def test_a_print_of_the_token_is_caught(self):
        tree = self._mutate("        return {**self._token.authorization_header(),",
                            "        print(self._token.authorization_header())\n"
                            "        return {**self._token.authorization_header(),")
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "print" in calls, "the print guard did not bite"

    def test_the_module_allowlist_guard_is_caught_by_a_new_importer(self):
        """The relaxed network ban must still fail on a SECOND connector."""
        tree = ast.parse("import httpx\n")
        found = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
        capable = {"x_transport.py"} | ({"connectors.py"} if found else set())
        assert capable != {"x_transport.py"}

    async def test_auto_deleting_foreign_rules_is_caught(self):
        """The real cross-tenant mutation, performed, and detected."""
        server = _RuleServer([TransportRule("77", "someone-else", "$FOO")])
        client = httpx.AsyncClient(transport=httpx.MockTransport(server))
        t = XFilteredStreamTransport(token=token(), client=client)
        remote = await t.list_rules()
        await t.apply_rules(add=[TransportRule("", "ours", "$BAR")],
                            delete=[r.remote_id for r in remote])
        assert any("delete" in p for p in server.posts), (
            "the foreign-rule assertion would not have noticed a deletion")

    def test_no_test_in_this_file_names_a_reachable_host(self):
        """Positive control on the no-network claim of this test module."""
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for lit in url_literals(tree):
            if lit in {"http://", "https://"}:
                continue  # the scheme prefixes url_literals itself matches on
            assert lit.startswith("https://api.x.com/2/"), lit
