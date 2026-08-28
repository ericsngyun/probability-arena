# SOCIAL-X-LIVE-TRANSPORT-001

**Status: BUILT AND QUALIFIED, NOT MERGED, NOT WIRED.** Branch
`social-x-live-transport-001`, held off `main` while S07 is armed against a
pinned commit. Nothing in `app/` imports it: the only importers are its own
tests. It is a capability that exists, not a capability that runs.

The first module in this repository permitted to open a socket to a social
platform. SOCIAL-TAPE-001 shipped deliberately unable to connect; this
milestone spends exactly one unit of that safety and pays for it in guards.

## The Protocol was not widened

`XFilteredStreamTransport` implements the existing `SocialStreamTransport`
verbatim — `list_rules` · `apply_rules` · `frames` · `aclose`. Four tests hold
that line: the Protocol still has exactly four methods, the transport adds no
fifth public method, `transport.py` does not import `x_transport` (the seam
does not know its implementor), and the class satisfies the runtime Protocol.

If X had required a fifth method, that would have been a fact about X worth
discovering, not a reason to loosen the interface every other transport is
held to.

## Capability is two endpoints

```
GET      https://api.x.com/2/tweets/search/stream
GET/POST https://api.x.com/2/tweets/search/stream/rules
```

`ALLOWED_URLS` is those two and the test asserts **set equality** against every
URL literal in the module — tighter than a denylist, because it fails on a URL
nobody thought to ban. `_assert_allowed` re-checks at each call site.

The POST exists solely to reconcile the stream's own filter. It is a write to
a query, not to an account. Structurally absent and asserted: any `/2/users/`,
`/2/dm_`, likes, following, followers, retweets, blocking, muting or bookmarks
path; any `put`/`patch`/`request`/`send` verb; and any identifier containing
`oauth`, `pkce`, `refresh_token`, `consumer_secret` or `access_token_secret`.
Bearer authentication cannot act on behalf of a user, so no user-context code
path exists to audit.

## The token

`BearerToken` loads from `X_BEARER_TOKEN` or the file named by
`X_BEARER_TOKEN_FILE`, and from nowhere else — no literal, no default, no
unauthenticated fallback. An empty value raises rather than degrading.

Every rendering path is closed, not merely the obvious one: `__repr__`,
`__str__`, `__format__` redact; `__reduce__` and `__getstate__` raise
`CredentialLeakError`, so it is unpicklable and not JSON-serializable. The
value leaves through exactly one method, `authorization_header()`, and a test
walks the AST to assert the set of functions touching `_v` is exactly
`{__init__, authorization_header}`.

The module contains **no `print` and no `logging` import**. "Never logged" as a
structural property beats "never logged" as a rule every future call site has
to remember.

Reachability, checked on the import graph rather than by substring:
`artifact.py`, `evidence_extractor.py` and `observer_funnel.py` cannot reach
`x_transport` at all. The secret is outside the world the artifacts live in.

## Two guards were relaxed, deliberately and in the open

SOCIAL-TAPE-001's blanket bans in `test_social_x_collector_001.py` said *no
module in `app/social/` may import an HTTP client* and *no module may carry
credential surface*. Both were correct while nothing was connected. Both would
now be false.

They were not deleted and not weakened into "except files that look live".
Each became an **equality against a named set**:

* network-capable modules `== {"x_transport.py"}`
* credential-bearing modules `== NETWORK_CAPABLE_MODULES`

Equality fails in both directions. A second connector fails. A module that
acquires a token without a socket fails. `x_transport` quietly losing its
client also fails. The credential scan additionally moved from lowercased raw
file text to an AST scan excluding docstrings and prose, because the old form
condemned any module that merely *named* a credential in a sentence promising
not to hold one — the recurring false-positive class in this repo.

## Two behaviours worth naming

**A clean end-of-stream is a disconnect.** The platform closing quietly and the
connection failing loudly are the same event to a collector measuring arrival
times, and a second code path exercised by only one of them is a liability.
Both emit a `RECONNECT` frame, bump `subscription_generation`, and restart
`delivery_sequence`.

**The retry budget resets only on a productive connection.** Resetting on
connect alone means an endpoint that accepts and immediately closes reconnects
forever at the floor backoff — a hot loop wearing the costume of resilience.
A connection that delivered nothing spends a retry.

Live frames carry `provenance=None`. `FrameProvenance` certifies a *fixture's*
basis; a live frame's basis is the wire. Stamping one would make replayed and
live frames indistinguishable, which is the one thing the field exists to
prevent.

## System failure is separated from funnel loss

`StreamHealth` counts connects, reconnects, keepalives, data frames, error
frames, HTTP errors and rate limits separately. An hour of 401s must never be
readable as "nobody posted". Keepalives are first-class frames for the same
reason: quiet-because-nothing-happened and quiet-because-dead are different
states.

## Qualification

48 tests. Ten are mutations, per TESTING_POLICY doctrine 4 — a guard is not
qualified until a mutation proves it detects the violation:

| mutation | caught by |
|---|---|
| a third URL literal | URL set equality |
| a `/2/dm_conversations` endpoint | write-endpoint scan |
| an `_oauth_sign` method | OAuth identifier scan |
| a leaking `__repr__` | redaction test |
| a `__reduce__` returning the value | pickle test |
| a third reader of `_v` | AST reader-set test |
| a `print` of the auth header | print/logging test |
| a second network-capable module | allowlist equality |
| **auto-deleting foreign rules** | cross-tenant assertion |
| prose naming a forbidden endpoint | **must NOT fire** (false-positive control) |

The last row is the control the substring guards in this repo have failed six
times before.

No test opens a socket: `httpx.MockTransport` answers everything, and a guard
asserts no test in the file names a host outside `api.x.com/2/`.

Full suite green.

## What this does NOT do

No collector is wired to it. No rules are registered. No token is installed.
No cost is incurred. Activation still requires the source universe and cost
envelope frozen in SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001, and merging still
waits for the S07 session boundary.
