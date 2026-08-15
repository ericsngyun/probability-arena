# KALSHI-LIVE-TAPE-COLLECTOR-001 — live venue -> canonical archive bridge

STATUS: DESIGN ONLY. No production code in this branch.

## 1. Objective

Connect an authenticated, read-only Kalshi market-data websocket to the already-reviewed
canonical synchronous archive, and use the first real sessions to REPLACE the guessed
production rate that `segment.py`'s rotation defaults were derived from with a measured one.

The archive exists. The replay/integrity lane exists. The signer exists. **Nothing joins
them to the venue** — `app/realtime/kalshi.py:366` declares a `Transport` interface whose
only implementation is `FixtureTransport` (`app/realtime/kalshi.py:379`), and no file in
`app/` reads `KALSHI_OBSERVER_API_KEY_ID` / `KALSHI_OBSERVER_CREDENTIAL_PATH`. This
milestone builds exactly that bridge and measures it.

## 2. What this unlocks

### New capability

A **live tape** of Kalshi market data: real venue frames, sequence-validated, normalized,
and written into a genesis-anchored, chained, manifest-committed archive that the existing
`kalshi-realtime-replay` lane can reconstruct deterministically. Today the replay lane can
only replay fixtures and a 4-record demo capture.

### The dependency that disappears

From `MEMORY.md`, the Probability lane's chain to a first trustworthy prospective paper P&L is:

    archive -> [MISSING: LIVE TAPE WRITER] -> replay -> quotes -> ... -> paper fill -> P&L

`KALSHI-LIVE-TAPE-COLLECTOR-001` is that missing step, and only that step. When it lands:

1. **"We have no prospectively-recorded Kalshi quote stream" stops being true.** Every
   downstream measurement in the Probability lane — executable entry quote, exit quote,
   spread at decision time — currently has no prospective source. The archive is the
   source; the collector is what puts anything in it.
2. **The archive's deployment/load gate stops being a guess.** `app/realtime/segment.py:200-212`
   derives `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` against "the ~500 events/s assumed peak
   this milestone's performance gate used" and notes "the one real measured Kalshi rate
   (4 records over ~2 minutes, DEMO)". A measured rate turns three shipped constants from
   assumptions into calibrated values — or refutes them.

### What it explicitly does NOT unlock

It does not produce a quote, a fill, a P&L, an opportunity, or an entry. It produces
evidence on disk plus a measurement report. Every step after "replay" in the chain above
remains a separate, separately-accepted milestone.

## 3. Scope and non-goals

### In scope

- A real websocket `Transport` implementation satisfying `app/realtime/kalshi.py:366`,
  using the already-vendored `websockets>=12.0` (`requirements.txt:9`; precedent:
  `app/services/tennis_livefeed.py:135-138`).
- Credential wiring: the one permitted path from `KALSHI_OBSERVER_API_KEY_ID` /
  `KALSHI_OBSERVER_CREDENTIAL_PATH` to `ReadOnlyRequestSigner.from_path`
  (`app/realtime/auth.py:402`) and `headers_for(purpose=WEBSOCKET_HANDSHAKE)`
  (`app/realtime/auth.py:510`). **Today nothing in `app/` reads those variables at all.**
- A collector session loop: connect -> handshake -> subscribe -> read -> normalize -> archive,
  bounded by an explicit `--max-seconds` and an explicit market-ticker list.
- Reconnect and subscription-generation handling, driving the EXISTING
  `SubscriptionState.supersede` / `begin_recovery` (`app/realtime/book.py:689-707`).
- A measurement lane (section 7) that is structurally off the archive's critical path.
- A `kalshi-collect-once` CLI, default-off flag, dry-run, and NO systemd unit installed
  by this milestone.

### Non-goals (explicitly deferred)

- **No timer, no daemon, no MarketOps hook, no systemd unit installed.** Per
  `docs/SAFETY_BOUNDARIES.md:72-74`: "No timer, no service, no daemon and no MarketOps
  hook exists for the observer, and installing one is a separate approved milestone
  (KALSHI-REALTIME-OBSERVATION-001B)." This milestone is the collector; scheduling it is
  001B and stays 001B.
- **No SQLite writes.** The collector writes to the archive filesystem tree and to the
  JSONL measurement sink. It opens no database session. This keeps it entirely outside
  the SQLITE-LOCK-TELEMETRY contention story and the backup-coordination lane.
- **No book publication into any consumer.** `SubscriptionRouter` may be run in-process to
  validate sequence integrity, but no book state is exported, persisted to the DB, or
  read by any forecast/signal/MarketOps path.
- **No REST reconciliation loop.** `reconcile_with_rest` (`app/realtime/archive.py:1091`)
  stays a replay-time function.
- **No rotation-default change in this milestone.** Retuning `DEFAULT_MAX_SEGMENT_*` from
  the measurement is a follow-up whose input is this milestone's output. Shipping the
  measurement and the retune together would mean the retune was never validated against
  an independent number.
- **Production environment is not the default and is gated separately** (open question Q1).

## 4. Capability boundary

### The governing text, quoted

`docs/SAFETY_BOUNDARIES.md:21-30` (Amendment KALSHI-READONLY-AUTH-001):

> **Permitted, narrowly:** RSA private-key loading solely to sign authenticated
> **read-scoped Kalshi market-data** requests under capability mode
> `OBSERVE_ONLY`.
>
> **Still forbidden, with no implementation surface:** wallets; custody; key
> generation; transaction signing; order signing; blockchain signing; order
> creation, cancellation or amendment; API-key creation, rotation or deletion;
> write-scoped credentials; and any general-purpose `sign(method, path)` API.

`docs/SAFETY_BOUNDARIES.md:233` (Always true, phase-independent):

> All external interaction is read-only (GETs); Kalshi credentials are not required and
> not stored; the optional WS client sends channel subscriptions only.

`docs/SAFETY_BOUNDARIES.md:65` (the exact signable input the amendment opened):

> signable input | `timestamp_ms + "GET" + "/trade-api/ws/v2"`. There is no method
> parameter and no path parameter on the public surface

`docs/SAFETY_BOUNDARIES.md:72-74`:

> `ENABLE_*` flags do not gate this: nothing runs it. No timer, no service, no
> daemon and no MarketOps hook exists for the observer, and installing one is a
> separate approved milestone (KALSHI-REALTIME-OBSERVATION-001B).

This milestone **runs it, manually and boundedly**. It does not install a timer, a
service, a daemon, or a MarketOps hook — that half of the sentence stays true and 001B
stays required.

### Channels: in scope and banned

The allowlist and the banlist are already CODE, not prose — `app/realtime/kalshi.py:99-107`:

    ALLOWED_CHANNELS  = ("orderbook_delta", "ticker", "trade", "market_lifecycle_v2")
    FORBIDDEN_CHANNELS = ("fill", "market_positions", "user_orders", "communications",
                          "order_group_updates")

**In scope for this milestone:** `orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2`
— all four are market data broadcast to any read-scoped subscriber; none is
account-scoped. The default subscription SHOULD be `orderbook_delta` + `ticker` only
(see Q3); `trade` and `market_lifecycle_v2` require an explicit `--channels` argument.

**Banned:** every channel in `FORBIDDEN_CHANNELS` — the private/authenticated-user
streams — plus every channel not in `ALLOWED_CHANNELS`, including any channel a future
venue release adds. The allowlist is closed, so a new private channel is refused by
default rather than after someone remembers to ban it.

### Why misconfiguration cannot reach a private channel

Four independent structural reasons, all of which already exist and none of which this
milestone may weaken:

1. **`build_subscribe` is the only way to construct a subscribe frame**
   (`app/realtime/kalshi.py:298`), and its first act is
   `assert_channels_allowed(channels)` (`:144`), which raises `CapabilityError` on a
   `FORBIDDEN_CHANNELS` member and again on anything outside `ALLOWED_CHANNELS`.
   A config string, an env var, or a CLI argument naming `fill` therefore fails at frame
   construction, before a socket write.
   *Design rule for the collector:* the transport's `send()` must accept ONLY dicts
   produced by `build_subscribe` / `build_get_snapshot` / `build_unsubscribe` /
   `build_resubscribe`. No raw-dict send path may exist.
2. **The signer cannot sign anything else.** `AuthPurpose` is a closed two-member enum
   (`app/realtime/kalshi.py:77-78`) mapped to constant `(method, path)` pairs at
   `:82-85`; `route_for_purpose` refuses a non-`AuthPurpose` argument (`:88-93`). There
   is no path parameter to misconfigure. A private channel over a DIFFERENT route is
   unreachable because the route is not an input.
3. **The credential is scope-verified before use and fails closed on silence.**
   `verify_scopes` (`app/realtime/kalshi.py:190`) treats an ABSENT scopes field as a
   HALT, rejects `list` subclasses and non-`str` members, and halts on `write`.
   A mis-provisioned order-capable key is refused at startup, not at first order.
4. **Only `GET` exists.** `ALLOWED_HTTP_METHODS = ("GET",)` (`:109`) and
   `assert_method_allowed` (`:136`) gate the canonical signing string. There is no
   POST/PUT/PATCH/DELETE code path in the package.

### The one new hole this milestone must close

A real transport introduces a genuinely new capability the fixture transport never had:
**an open socket that can send arbitrary bytes.** Points 1-4 above constrain what the
repo can CONSTRUCT; they do not constrain what a socket can WRITE. The collector must
therefore add one structural control of its own:

- `KalshiWebsocketTransport.send()` accepts a dict, asserts `cmd` is in a closed set
  `("subscribe", "unsubscribe", "update_subscription")`, re-runs
  `assert_channels_allowed` on any `params["channels"]` present, and refuses anything
  else. It exposes no `send_text` / `send_bytes` / `send_raw`, and the underlying
  `websockets` connection object is held in a closure or a private attribute that no
  collector code reads (the same containment reasoning `auth.py` applies to the key, per
  `docs/SAFETY_BOUNDARIES.md:70`).
- The safety grep in `AGENTS.md` must stay clean; a new test asserts that no
  `FORBIDDEN_CHANNELS` string is reachable from any collector configuration surface.

**Reversibility tiers.** Connect + subscribe + append against **demo**: Tier 1
(autonomous — read-only, bounded, no venue state change, evidence is append-only and a
bad session is a discardable segment). First **production** connection: Tier 2 (single
confirm — same read-only semantics, but production evidence and a production credential).
Any change to `ALLOWED_CHANNELS`, `AuthPurpose`, `ALLOWED_HTTP_METHODS`, or the
`SAFETY_BOUNDARIES` amendment: Tier 3 (dual confirm, boundary amendment required).
Installing a systemd unit/timer: out of scope entirely — that is 001B.

## 5. Survey of what already exists

TBD

## 6. Component design

TBD

## 7. Measurement plan

TBD

## 8. Failure and backpressure handling

TBD

## 9. Implementation checkpoints

TBD

## 10. Validation plan

TBD

## 11. Open questions for Eric

TBD
