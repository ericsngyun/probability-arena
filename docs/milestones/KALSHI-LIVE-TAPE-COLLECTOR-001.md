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

Surveyed at `HEAD = 3b513ef`. **Do not rebuild any of this.**

### 5.1 Kalshi websocket / client code

| Surface | file:line | State |
|---|---|---|
| Capability modes, channel allowlist, method allowlist | `app/realtime/kalshi.py:33-112` | **Production-ready.** Closed enums; `IMPLEMENTED_MODES = (OBSERVE_ONLY,)` |
| `AuthPurpose` + `route_for_purpose` | `app/realtime/kalshi.py:64-93` | Production-ready |
| WS hosts (demo + production) | `app/realtime/kalshi.py:50-56` | Demo host **verified on the wire**; production host **UNVERIFIED** (comment at `:52-55`) |
| `verify_scopes`, `describe_credential` | `app/realtime/kalshi.py:190-255` | Production-ready |
| `build_subscribe` (`use_yes_price` always True) | `app/realtime/kalshi.py:298-313` | Production-ready; wire-confirmed |
| `build_get_snapshot` / `build_unsubscribe` / `build_resubscribe` | `app/realtime/kalshi.py:322-363` | `get_snapshot`'s **required `market_tickers`** confirmed on the demo wire (error code 14, `:326-331`) |
| `Transport` interface | `app/realtime/kalshi.py:366-376` | **Interface only — three `NotImplementedError`s** |
| `FixtureTransport` | `app/realtime/kalshi.py:379-398` | **DEMO/TEST ONLY.** Replays a list of dicts. No socket |
| `ReadOnlyRequestSigner` | `app/realtime/auth.py:335-554` | Production-ready, confined; `from_path` at `:402`, `headers_for` at `:510` |
| `RequestSigner` base / `UnsignedTransportSigner` | `app/realtime/kalshi.py:258-292` | Seam for credential-free paths |
| `credential_audit.audit_scopes` | `app/realtime/credential_audit.py:49` | Ready; needs a `fetch` callable supplied by the caller |
| `KalshiRestAdapter` | `app/adapters/kalshi.py` | Pre-existing public read-only REST scanner; **unrelated lane**, do not touch |

**FINDING (gap 1): there is no real transport.** `websockets>=12.0` is already a
dependency (`requirements.txt:9`) and `app/services/tennis_livefeed.py:135-138` is the
in-repo precedent for a bounded `websockets.connect(...)` session.

**FINDING (gap 2): the signer is orphaned.** A repo-wide grep for
`KALSHI_OBSERVER_API_KEY_ID` / `KALSHI_OBSERVER_CREDENTIAL_PATH` returns hits in
`docs/` ONLY — no `.py` file reads them. `ReadOnlyRequestSigner.from_path` has no caller
in `app/`. The DEMO validation recorded in `docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md`
was therefore performed by an out-of-repo script; nothing in the repository can open an
authenticated session today. Wiring those two variables to `from_path` is new code and
must land in exactly one place.

**FINDING (gap 3): no session loop, no reconnect driver.** `SubscriptionState.supersede`
and `begin_recovery` (`app/realtime/book.py:689-707`) exist and are tested, but nothing
calls them from a live context; there is no code that decides WHEN to supersede.

### 5.2 The canonical archive's append API and its durability/ordering contract

Entry point: `EventArchive.append(envelope) -> Path` (`app/realtime/archive.py:620-651`).

The contract, verbatim where it matters (`app/realtime/segment.py:1499-1528`):

 > `submit()` canonicalises and appends the record on the CALLER'S thread, serialised
 > against every other `submit()`/`close()` by one lock (`self._lock`), and returns only
 > after the record has been durably handed to the writer -- never before.
 >
 > One consequence worth naming explicitly: **a caller is never told ACCEPTED before the
 > canonical writer owns the event.** There is no interval, of any size, in which
 > `submit()` has returned `None` but the record is not yet durably part of this
 > segment's gzip stream.
 >
 > Durability is explicit: records are flushed on a cadence, `fsync` happens at close, and
 > the manifest is written to a temp file, fsynced, atomically renamed, and the directory
 > fsynced after. `close()` is not the durability contract - rename-after-fsync is.

Properties the collector must not weaken:

1. **Synchronous, caller-thread, lock-serialised.** `append()` blocks the caller. There
   is no queue. Reintroducing one in the collector would recreate exactly the
   ownership-gap class the archive milestone spent eleven review rounds removing
   (`segment.py:1506-1517`).
2. **Ordering.** One `SegmentWriter` per partition, chained records
   (`fold_stream_digest`, `segment.py:845`), and a per-segment `_lock`. Concurrent
   producers are safe but their interleaving is arbitrary — **so the collector must be
   single-producer per subscription if archive order is to mean wire order.**
3. **Flush cadence, not per-record fsync.** `flush_every: int = 256`
   (`segment.py:1534`), and `EventArchive` never overrides it. The loss window on
   SIGKILL is the unflushed tail plus the whole uncommitted segment
   (`segment.py:1829-1838`).
4. **Environment isolation is enforced at append.** `archive.py:622-626` refuses to
   write a `demo` envelope into a `production` archive.
5. **The archive must already exist.** `EventArchive.__init__` calls `read_genesis`
   (`archive.py:402`); an uninitialized root is a HALT — "A collector consumes an
   initialized archive and can never bring one into existence" (`archive.py:398-401`).
   Operator step: `archive-init --confirm` (`app/cli.py:726`).
6. **Rotation closes OFF the producer thread** (`archive.py:496-512`, `_RotationCloser`
   at `archive.py:119`). Measured producer stall before that change: "~3-5 s at the old
   bound on an idle machine, 14.8 s under contention" (`archive.py:500-501`).
7. **`close()` is the commit point** (`archive.py:653-657`): "Until it runs there is no
   authoritative record count, and an unclosed segment is explicitly not evidence."
8. **Rejection is typed and loud.** `append` raises `ArchiveError` when
   `writer.submit()` returns a `RejectReason` (`archive.py:648-649`). The collector must
   never swallow it.

Rotation defaults (`segment.py:225-227`), and the guess this milestone kills
(`segment.py:200-212`):

    DEFAULT_MAX_SEGMENT_RECORDS = 13_000
    DEFAULT_MAX_SEGMENT_AGE_S   = 900.0             # 15 minutes
    DEFAULT_MAX_SEGMENT_BYTES   = 32 * 1024 * 1024  # 32 MiB compressed

 > At the ~500 events/s assumed peak this milestone's performance gate used that is a
 > rotation every ~26 s; at the one real measured Kalshi rate (4 records over ~2 minutes,
 > DEMO) it is unreachable and `DEFAULT_MAX_SEGMENT_AGE_S` is what rotates.

Measured close cost, from the committed benchmark
`tests/benchmarks/segment_close_cost.py` (`segment.py:187-198`): ~145 ms CPU per 1,000
records at `commit_to_head=True`; 13,000 records = 2.134 s CPU / 2.497 s wall; 20,000
records = 14.8 s WALL under CPU contention. Measured synchronous append throughput
(`segment.py:1514-1517`): **3,440 events/s on `SegmentWriter`, sustained 2,500-5,000/s,
bursts draining ~7,000/s.**

### 5.3 The envelope and the normalization path

- `EventEnvelope` (`app/realtime/book.py`) — raw + normalized + full time lineage;
  `data_age_us` is INTEGER microseconds by canonical-representability requirement.
  Since KALSHI-TAPE-GENERATION it also carries the two capture-time epochs,
  `connection_generation` and `subscription_generation` (§6.4.1); before that it
  carried neither, and both of the archive's pinned generation columns were
  permanently null.
- `make_envelope(...)` (`app/realtime/book.py:93-155`) — already handles Kalshi's
  non-uniform timestamping (`ts_ms` first; `ticker` sends epoch SECONDS in `ts` while
  `orderbook_delta` sends an ISO string — wire-confirmed at `:97-106`).
- `SubscriptionState` (`app/realtime/book.py:594-707`) — **`seq` is subscription-global**;
  ordering lives here, not on books. Non-orderbook frames (including `error`) consume a
  sequence number (`book.py:742-750`, wire-confirmed).
- `SubscriptionRouter.dispatch` (`app/realtime/book.py:733-795`) — ordering, then
  routing, then apply; a sequence fault unpublishes EVERY book on the subscription.
- Canonical encoding and bounded work budget: `app/realtime/canonical.py`
  (`CapabilityLimits` at `:66`, `canonical_bytes` at `:414`).

`EventArchive.append` maps envelope fields onto record fields at `archive.py:630-647`,
including dropping the duplicated `raw` from the normalized copy per RAW-PAYLOAD-STORAGE-001.

### 5.4 Telemetry sink and `writer_name`

- Sink: `app/telemetry/sink.py:77-216`. Append-only JSONL under the telemetry directory
  (`SQLITE_TELEMETRY_DIR` override, `sink.py:70-74`), one unbuffered `os.write()` per
  event on an `O_APPEND|O_WRONLY|O_NOFOLLOW` fd, never raises into the writer, no fsync
  per event, 4096-byte line cap.
- Schema: `app/telemetry/schema.py`. `WRITER_NAMES` is a closed `frozenset`
  (`:66-74`) — **no Kalshi/collector name exists in it**. `WRITER_CLASSES` (`:59-62`)
  DOES already contain `continuous_daemon`, which no current writer uses.
  `ALLOWED_FIELDS` (`:162-228`) is a closed set and "an event carrying ANY other key is
  rejected".
- Registration path for a new writer: add the name to `WRITER_NAMES`, then call
  `app.telemetry.writer_pass.emit_writer_pass(...)` (`:268`) once per pass, AFTER the
  pass's own commits.

**FINDING (telemetry fit — reuse the transport, not the envelope).** The 001A sink is the
right MECHANISM and this milestone should use it, but four properties make the
`sqlite-writes.jsonl` ENVELOPE unsuitable as the home for the collector's measurements:

1. **The field set is closed and SQLite-shaped.** There is no field for events/sec,
   event bytes, append latency percentiles, dropped events, or lag. Adding ~12 such
   fields contradicts the schema's own rule (`schema.py:22-24`): "this envelope is the
   safety boundary for five writers and must not grow a label describing one lane's
   bookkeeping."
2. **The model is one event per PASS.** A collector session is hours long. One
   session-summary event is the right granularity for THAT file, and far too coarse to
   answer "what is the p99 burst rate".
3. **`emit_writer_pass` derives `writer_class` from `run_source` only**
   (`writer_pass.py:317-320`), yielding `scheduled_oneshot` or `manual_command` — it can
   never emit `continuous_daemon`, so a long-running collector would be mislabeled.
4. The file has no rotation/retention until 001E (`sink.py:25-35`) and `read_events`
   slurps it whole (420 MB peak heap on a 60 MB file, `sink.py:236-240`).

Design consequence (section 7): reuse `TelemetrySink`'s exact append technique in a
SEPARATE file with its own bounded schema, and additionally file ONE `emit_writer_pass`
session record in the shared file so the collector is visible to existing operator
tooling. That requires adding exactly one name — `kalshi_live_tape` — to `WRITER_NAMES`,
and nothing else in the shared schema changes.

### 5.5 What already exists that must not be rebuilt

- `kalshi-realtime-replay` CLI (`app/cli.py:651-723`) — deterministic replay, integrity,
  and the decomposed latency envelope (`latency_envelope`, `archive.py:978`) with its
  honest `MIN_SAMPLES_FOR = {"p50": 3, "p95": 20, "p99": 100}` gate (`archive.py:947`)
  and its explicit NOT-MEASURED disclosures for reconnect gaps and host clock offset
  (`cli.py:717-722`). **The collector must not compute its own venue-latency number.**
- `archive-init` / `archive-recover-head` / `archive-adopt` / `archive-migrate-legacy`
  (`app/cli.py:726`, `:792`, `:860`, `:968`) — the full operator lifecycle.
- `verify_segment` / `verify_archive` (`segment.py:3187`, `:3641`), residue
  classification (`segment.py:2886-2946`), salvage (`segment.py:2657`).
- `tests/benchmarks/segment_close_cost.py` — the committed close-cost benchmark. Extend
  it; do not write a second one.
- Roughly 30 `tests/test_kalshi_*` suites already pin the archive/replay/canonical
  contracts.

## 6. Component design

### 6.1 Dependency direction

    CLI (app/cli.py: kalshi-collect-once)
      -> app/realtime/collector.py        [NEW - the session orchestrator]
           -> app/realtime/ws_transport.py [NEW - the only socket in the package]
           -> app/realtime/auth.py         [EXISTING - signer, unchanged]
           -> app/realtime/kalshi.py       [EXISTING - allowlists, frame builders, unchanged]
           -> app/realtime/book.py         [EXISTING - make_envelope, SubscriptionState, Router]
           -> app/realtime/archive.py      [EXISTING - EventArchive.append/close, unchanged]
           -> app/realtime/collector_metrics.py [NEW - measurement, off the critical path]

Strictly downward. Nothing existing imports anything new. `archive.py`, `segment.py`,
`canonical.py`, `book.py` and `auth.py` are **not modified by this milestone** (one
exception, stated in 6.6: a single name added to `app/telemetry/schema.py:WRITER_NAMES`).
That is deliberate: those files carry the reviewed guarantees, and a bridge milestone
that edits them has already lost the argument that the guarantees are unchanged.

### 6.2 `ws_transport.py` — the only socket

    class KalshiWebsocketTransport(Transport):
        def __init__(self, *, environment: str, signer: RequestSigner,
                     open_timeout_s: float, ping_interval_s: float | None,
                     ping_timeout_s: float | None,
                     max_queue: int, max_size: int,
                     read_timeout_s: float | None) -> None
        async def connect(self) -> None       # handshake with signed headers
        async def send(self, message: dict) -> None   # GOVERNED, see below
        def __aiter__(self)                   # yields parsed dict frames

Contract and invariants:

- **`connect()`** resolves the URL from `WS_HOSTS[environment]`
  (`app/realtime/kalshi.py:50-56`) — never from a caller-supplied string — and obtains
  headers from `signer.headers_for(purpose=AuthPurpose.WEBSOCKET_HANDSHAKE)`. There is
  no `url` parameter. The connect URI and the headers are never logged (the
  TENNIS-LIVE-FEED-002 precedent, `docs/SAFETY_BOUNDARIES.md:259`: failures reported by
  exception type name only).
  **The signed headers are passed as `additional_headers=`, not `extra_headers=`**
  (12.6 row 2): the v12 spelling does not exist on the installed `websockets` 16.0 and
  raises `TypeError` at connect, on the signed handshake. `connect` is also driven
  directly (`await connect(...)`); the `async for ws in connect(...)` form is forbidden
  here because it carries its own unbounded `while True` retry loop (12.6 row 5), which
  would silently defeat §6.4's `max_reconnects`.
  A signer without `headers_for` — i.e. `UnsignedTransportSigner` — is refused at this
  point rather than tolerated, per §6.5.
- **`send()`** is the new structural control described in section 4: `cmd` must be in
  `("subscribe", "unsubscribe", "update_subscription")`, any `params["channels"]` is
  re-run through `assert_channels_allowed`, and any other shape raises `CapabilityError`.
  No `send_text`/`send_bytes` exists. The underlying connection object is private and no
  collector code touches it.
- **`__aiter__`** yields *parsed dicts*. A frame that is not JSON, or not a dict, is
  counted as `malformed_frames` and skipped — it is not an envelope and must never be
  passed to `make_envelope`, which would archive a nonsense record.
- **Backpressure is made explicit at the library boundary.** `websockets` buffers
  incoming frames in an internal queue, and `max_queue` is a **high-water mark, not a
  hard cap**: the queue is an unbounded `deque` and the library pauses reading when depth
  exceeds `max_queue`, resuming when it falls back to the derived low mark (`max_queue // 4`
  for a bare int). Overflow therefore applies TCP backpressure and **drops nothing** —
  see 12.1 and 12.2, which settle this against the installed source rather than leaving it
  as an assumption. This is the single most important knob in the whole design and
  section 8 is about it. `max_queue=None` disables flow control entirely and must never be
  passed (12.2); the transport refuses it at construction.
- **`max_size` is a constructor parameter and a deliberate choice** (12.1). It bounds a
  single message; exceeding it raises `PayloadTooBig`, which **fails the connection**
  rather than dropping one message, so too tight a value manufactures observation gaps.
  CP1 sets it to **8 MiB** — 8x the library default, roughly 20x the arithmetic worst case
  for a full two-sided book on the 4-decimal grid `fixedpoint.py` admits, and still
  finite, so a runaway frame cannot exhaust memory. When it is hit, the transport raises a
  named `FrameTooLargeError` carrying the configured limit and the remedy, and records a
  close cause of `oversize_frame`, so the symptom is never an unexplained disconnect.

### 6.3 `collector.py` — the session orchestrator

    @dataclass(frozen=True)
    class CollectorConfig:
        environment: str            # "demo" | "production"; production is separately gated
        archive_root: Path          # MUST already be initialized (read_genesis)
        market_tickers: tuple[str, ...]   # explicit, never "all markets"
        channels: tuple[str, ...]         # default ("orderbook_delta", "ticker")
        max_seconds: int            # HARD CAP. No unbounded session exists.
        max_events: int             # second independent hard cap
        max_reconnects: int         # third; exhausting it ends the session cleanly
        dry_run: bool               # connect + read + measure, NEVER append

    @dataclass(frozen=True)
    class CollectorResult:
        status: str                 # ok | capped_time | capped_events | capped_reconnects
                                    # | refused_* | archive_error | transport_error
        events_received: int
        events_archived: int
        events_rejected: int        # typed archive rejections, never silent
        frames_malformed: int
        reconnects: int
        subscription_generations: int
        segments_committed: int
        started_at: str
        finished_at: str
        measurement_path: str | None
        boundary_note: str          # the OBSERVE_ONLY statement, carried in every result

The loop, in order, per frame:

1. `receive_time = utcnow()`, `receive_mono = monotonic_ns()` — captured BEFORE any
   processing, so `data_age_us` means what `book.py` says it means.
2. `envelope = make_envelope(...)` — existing code, plus the two capture-time
   epochs the session holds (§6.4.1). They are stamped HERE because nothing
   downstream can reconstruct which connection or which subscription
   generation a frame arrived under.
3. `router.dispatch(record)` — OPTIONAL, controlled by `--validate-sequence`
   (default on). A `SubscriptionError` is recorded and triggers recovery (6.4); it does
   **not** stop the archive append. **Archive first, interpret second**: a frame we could
   not order is still evidence, and the whole reason the archive stores `raw` verbatim
   is that a later question must be answerable from the archive.
   *Ordering decision:* `archive.append()` is called BEFORE `dispatch()`, so a dispatch
   exception can never cost us the record.
4. `archive.append(envelope)` — synchronous, blocking, on this thread.
5. `metrics.observe(...)` — in-memory counters and pre-sized histograms only. No I/O.

Threading: **one producer thread/task per subscription** (5.2 invariant 2). If more than
one subscription is ever needed, each gets its own `EventArchive`-visible partition and
its own `SubscriptionState`, never a shared `seq` space.

### 6.4 Reconnect and subscription generations

The existing model is already correct and must be driven, not re-invented:

- On a sequence GAP or REGRESSION, `SubscriptionRouter.dispatch` raises
  `SubscriptionError` and has already unpublished every book
  (`book.py:770-772`). The collector then attempts recovery path 1:
  `build_get_snapshot(cmd_id, sid, market_tickers)` — with the tickers, per the
  wire-confirmed code-14 error (`kalshi.py:326-331`) — and calls
  `subscription.begin_recovery()`.
- On a socket close/error, the collector reconnects and calls
  `subscription.supersede(market_tickers=...)` (`book.py`), because the new
  stream's `seq` numbers are in a different namespace. The new generation number rides
  into every subsequent envelope so the archive record carries
  `subscription_generation` and replay can tell a straggler from a gap.

#### 6.4.1 KALSHI-TAPE-GENERATION — the epoch this section assumed and never had

**The paragraph above asserted something that was not true.** `EventEnvelope`
defined neither `connection_id` nor `subscription_generation`, while
`archive.append` wrote both through `raw.get(...)` on an `asdict()` of that
envelope, so **both pinned columns were permanently `None`** — and
`SubscriptionRouter.dispatch` read that null straight back into
`SubscriptionState.accept(generation=...)`. Nothing ever rode into an envelope.

The consequence is exactly the one the tape exists to prevent: after a
reconnect the venue's `seq` restarts, the generation change is invisible,
`accept()` sees a discontinuity, and `_unpublish_all("sequence fault")` drops
every book. A sequence hole is the **only** drop detector available (CP0: the
installed library never silently drops and exposes no drop counter), so a
routine reconnect was indistinguishable from data loss, and **CP8's verify line
could not have passed as written.**

Scope amendment: `book.py` and `archive.py` were previously off-limits for this
milestone and were opened for this fix. `kalshi.py` remains unmodified (§6.1).
This is required data semantics, not feature creep: a reconnect and a sequence
discontinuity are different phenomena and must remain distinguishable in the
durable tape.

What now exists:

- **Two monotonic capture-time epochs**, stamped by the collector onto every
  envelope and written into the record's existing pinned columns:
  `connection_generation` advances when a socket comes up (`connect()` has
  returned); `subscription_generation` advances **only when a new successful
  subscription generation begins** — `_begin_subscription_epoch()`, called once
  per accepted subscribe, never per connect attempt and never per frame. A
  reconnect whose `connect()` fails consumes no subscription epoch.
- **One number, not two that agree.** `_begin_subscription_epoch` moves the
  session's stamp *and* every `SubscriptionState` onto the same value
  (`supersede(generation=...)`), and `_router_for` births a new sid's state at
  the current epoch. The value replay validates is the value the collector
  held.
- **Sequence continuity is evaluated WITHIN a generation.** In
  `SubscriptionState.accept`: a **greater** generation is a BOUNDARY — it
  re-bases the stream and each book's position, is counted as
  `generation_advances`, and does **not** unpublish anything; a **lower**
  generation is still a straggler fault; a gap **inside** a generation still
  raises. The drop detector is not blinded, which is the property test class
  `TestAGenuineGapStillFaults` exists to hold.
- **The venue's `seq` is not overloaded.** It stays the venue's number; the
  epoch is ours and is carried beside it.
- **Nothing is inferred at read time.** An epoch that is not on the record is
  not reconstructed from one.
- **Backward compatibility.** Absent/`None` is the documented sentinel
  `GENERATION_UNKNOWN` — never `0`, which would be a fabricated epoch. A
  pre-milestone record reads as "generation unknown", the generation check is
  skipped, and ordering falls back to pure `seq` continuity exactly as before.
  `ENVELOPE_SCHEMA_VERSION` is bumped **1 → 2** deliberately, so a reader can
  tell "written before the field existed" from "the writer had no epoch". The
  durable record schema (`segment.RECORD_FIELDS`) is **unchanged**: it always
  carried both columns, and this change only stops writing null into them.
- **Durability and ordering are untouched.** `append()` is still synchronous
  and caller-threaded; no close or rotation semantics are involved.

Known limitation, deliberately not fixed here: the subscription epoch is
monotonic **within one collection session**. Two sessions appended to the same
archive restart it, so replaying across a session boundary sees a lower
generation and faults — as it already did before this change, via a `seq`
regression instead. Making it monotonic across sessions requires persisting the
epoch, which is a separate decision.
- **Reconnect gaps are unmeasured by design in the replay lane** (`cli.py:717-719`:
  "NOT MEASURED: reconnect/observation gaps. Every percentile above is conditioned on
  being connected."). The collector closes exactly that gap by recording, in its own
  measurement stream, `disconnected_at` / `reconnected_at` per reconnect. It does not
  change the replay lane's disclosure.
- Reconnect backoff is bounded and jittered, capped by `max_reconnects`. There is no
  infinite retry loop (the TENNIS-LIVE-FEED-002 precedent: "no reconnect loop and no
  timer").

### 6.5 Credential wiring — exactly one new call site

    def load_observer_signer(*, environment: str, reported_scopes) -> ReadOnlyRequestSigner

Lives in `collector.py` and is the ONLY code that reads
`KALSHI_OBSERVER_API_KEY_ID` / `KALSHI_OBSERVER_CREDENTIAL_PATH`. It calls
`ReadOnlyRequestSigner.from_path` (`auth.py:402`) and nothing else. `reported_scopes`
has no default in `from_path` and must come from a real `/trade-api/v2/api_keys`
response via `credential_audit.audit_scopes` (`credential_audit.py:49`) using
`AuthPurpose.API_KEY_METADATA` — **not** from a config value, because a hard-coded
`["read"]` would defeat `verify_scopes` entirely.

If either variable is absent, the collector refuses with
`status="refused_no_credential"` and makes no connection. It never falls back to
`UnsignedTransportSigner` on a live host — that seam exists for fixtures and replay
(`kalshi.py:286-292`) and silently degrading to it would open an unauthenticated
session while reporting success.

### 6.6 The one change to shared code

`app/telemetry/schema.py:66-74` — add `"kalshi_live_tape"` to `WRITER_NAMES`.

That is a one-line change to a closed label set, which is exactly the documented
registration mechanism. **It is flagged here as an architectural change**: it widens a
safety-boundary enum that five other writers share. It carries no new field, no new
enum member elsewhere, and no change to `ALLOWED_FIELDS` or `REQUIRED_FIELDS`.

### 6.7 CLI surface

    python -m app.cli kalshi-collect-once \
      --environment demo \
      --archive <root> \
      --tickers TICKER[,TICKER...] \
      [--channels orderbook_delta,ticker] \
      [--max-seconds 300] [--max-events 100000] [--max-reconnects 3] \
      [--dry-run] [--format text|json]

- `--dry-run` connects, reads, validates sequence, and measures, but performs **zero**
  archive appends. It is the honest smoke test.
- No flag enables a scheduled path, because no scheduled path exists (that is 001B).
  A feature flag `ENABLE_KALSHI_LIVE_TAPE` (default false) gates any FUTURE scheduled
  entry point only; manual invocation is always allowed, matching the repo's existing
  pattern (`docs/SAFETY_BOUNDARIES.md:257`, POLY-001).
- Every result — including refusals — carries the boundary note, matching the TAPE_NOTE
  pattern in `docs/SAFETY_BOUNDARIES.md:241`.

## 7. Measurement plan

**The measurement is the deliverable.** The tape is the by-product. If a session
produces a clean archive and no defensible rate distribution, the milestone has failed.

### 7.1 The one rule: metric writes are never in the archive's critical path

`archive.append()` is synchronous and lock-serialised (5.2). Anything that adds latency
inside the per-frame loop shows up as archive latency, and a measurement that inflates
the thing it measures is worse than no measurement.

Therefore, in the per-frame path (`collector.py`, step 5 of 6.3) the ONLY permitted
operations are:

- integer counter increments;
- `time.monotonic_ns()` deltas (two calls per frame, around `append` only);
- a bucket increment into a PRE-ALLOCATED fixed-width histogram array;
- a byte-length integer added to a pre-allocated histogram.

Explicitly forbidden in that path: any `os.write`, any `json.dumps`, any list append
that grows without bound, any string formatting, any lock other than the archive's own,
any allocation proportional to event count.

Flushing to disk happens on a **separate thread** on a fixed wall-clock cadence
(default 10 s), reading a snapshot of the counters. One `os.write()` per 10 s against a
stream that may be running at thousands of events/s is four orders of magnitude off the
hot path. The flusher uses the exact `TelemetrySink._write_line` technique
(`sink.py:114-141`): `O_APPEND|O_WRONLY|O_CREAT|O_NOFOLLOW`, one unbuffered write, short
write counts as a drop and is never resumed, never raises into the collector.

### 7.2 Two streams, deliberately

| Stream | File | Cadence | Schema |
|---|---|---|---|
| A. Interval metrics | `kalshi-live-tape.jsonl`, same telemetry dir as `sink.telemetry_dir()` | one record / 10 s | NEW, bounded, defined below |
| B. Session record | existing `sqlite-writes.jsonl` | one record / session | EXISTING envelope, `writer_name="kalshi_live_tape"` via `emit_writer_pass` |

Stream B exists so the collector is visible to the operator tooling that already reads
that file (`scripts/sqlite_analyze_maintenance.py::_lock_tally`, per
`writer_pass.py:250-255`). It carries only fields already in `ALLOWED_FIELDS`:
`duration_ms`, `outcome`, `run_status`, `retry_count` (reconnects), `external_calls`,
`rows_attempted` / `rows_committed` / `rows_skipped` (received / archived / rejected). It
carries **no** rate, size, or latency field, because none exists in that envelope and
section 5.4 explains why none should be added.

Stream A is a separate file with a separate schema for the reasons in 5.4. It reuses the
sink's write technique but not its validator. Its own validator is equally strict and
equally closed: a fixed field list, integers and pre-computed bucket labels only, no
market ticker, no raw payload, no exception message. **A ticker is high-cardinality
market identity and must not enter the telemetry directory** — the interval record
carries `markets_subscribed` (a count), never a list. Bucket labels follow the existing
`_HISTOGRAM_KEY_RE` convention (`schema.py:267`) so a later reader can share one parser.

### 7.3 The interval record (field list; bucket labels abbreviated)

    schema_version          1
    session_id              uuid4, minted once per session
    environment             demo | production
    interval_index          monotonic integer
    interval_started_at     ISO-8601 Z
    interval_ended_at       ISO-8601 Z
    interval_wall_ms        integer, actual elapsed (never assumed 10000)

    events_received         integer
    events_archived         integer
    events_rejected         integer   typed archive rejections
    frames_malformed        integer   not JSON, or not a dict

    event_bytes_total       integer
    event_bytes_histogram   bucket-label -> count
    append_us_histogram     bucket-label -> count
    append_us_max           integer
    append_calls            integer

    rotations_started       integer
    rotation_failures       integer
    closer_outstanding_max  integer   peak of EventArchive._closer.outstanding()

    reader_lag_frames_max   integer   see 7.4
    reader_stall_ms_max     integer   longest single gap between two reads
    disconnects             integer
    reconnects              integer
    subscription_generation integer
    sequence_gaps           integer
    sequence_regressions    integer
    sequence_duplicates     integer

    markets_subscribed      integer   a COUNT, never a ticker list
    metric_flush_drops      integer   the measurement's own honesty field

### 7.4 How each required quantity is obtained, and its distortion risk

| Quantity | Method | Distortion control |
|---|---|---|
| **events/sec average** | `events_received / interval_wall_ms` per interval, then over the session | `interval_wall_ms` is measured, not assumed; a stalled flusher cannot inflate a rate |
| **p95 / p99 burst rate** | Percentiles over the per-interval rate SERIES at 10 s, plus a second 1 s-resolution counter ring (600 slots, pre-allocated, integer writes only) for sub-interval bursts | 10 s buckets alone would smooth away the burst that matters. The 1 s ring is a fixed-size integer array; it allocates nothing per event |
| **event sizes** | `len(raw_frame_bytes)` at the transport boundary, before JSON parsing | Measured on the WIRE bytes, so it is comparable to bandwidth. Record size on disk is a different number and is derived separately from segment manifests |
| **archive append latency** | `monotonic_ns()` immediately before and after `archive.append(envelope)`, into a pre-allocated histogram | Two clock reads per event. **Assumption to verify:** that this is a low-tens-of-nanoseconds cost on the target host and therefore negligible against a measured append. Checkpoint 5 measures the instrumentation overhead explicitly by running the same fixture load with instrumentation on and off |
| **rotation frequency** | `EventArchive.rotations` delta per interval, plus `_live_segment_id` transitions | Read from the archive's own counters, not inferred from the filesystem |
| **archive close latency** | Wall time of ONE COMPLETED segment close, measured inside `EventArchive._timed_close` and handed to a typed callback (`on_segment_closed=callable(close_ns: int) -> None`) — see §15 | Close is already OFF the producer thread (`archive.py:496-512`). Measuring it must not put it back on: the timing is taken on the thread that ran the close. The callback is invoked **after** the measured interval has ended, so a slow observer cannot inflate the number it is being handed |
| **dropped events** | Two separate counters that must never be merged: `events_rejected` (archive said no, typed) and `frames_malformed` (unparseable), plus the collector's own receive count. **`transport_dropped` is DELETED, not zeroed** (12.4): the installed library has no drop path and no drop counter, so the number has no source — and needs none, because the library's contribution is provably zero. Loss can enter only across a disconnect (§8.3) or upstream at the venue, and sequence integrity detects the latter | A single "dropped" number would hide which layer failed. A fabricated zero would be worse than either |
| **backpressure / lag** | `reader_lag_frames_max` from `len(conn.recv_messages.frames)`; ALWAYS also `reader_stall_ms_max` — the longest wall gap between consecutive successful reads — which needs no library support | Queue depth **rests on an undocumented attribute chain through a `SimpleQueue` the library calls internal** (12.3). It is read through a single guarded helper returning `None` on `AttributeError`/`TypeError`, is recorded as UNAVAILABLE rather than `0` when the chain breaks, and a test pins the chain so a `websockets` upgrade fails loudly. Stall time is the property that actually matters and is measurable regardless |

### 7.5 The report

`kalshi-tape-measure-report --session <id>` reads stream A (streaming, never
`read_events`-style slurping — see `sink.py:236-240`) and prints:

- the rate distribution with an explicit sample-count gate reusing the existing
  `MIN_SAMPLES_FOR = {"p50": 3, "p95": 20, "p99": 100}` rule (`archive.py:947`) — a p99
  from 12 intervals is not printed, it is refused;
- the three rotation constants re-derived from the measurement, each with the margin
  against the measured close cost, and an explicit VERDICT per constant:
  `consistent_with_measurement` / `too_loose` / `too_tight` / `insufficient_sample`;
- a NOT MEASURED section, in the style of `cli.py:717-722`, naming everything the
  session could not establish (production rates from a demo session; overnight/peak
  behaviour from a daytime session; any quantity whose library support was absent).

## 8. Failure and backpressure handling

This is the central risk of the milestone and it gets the most space.

### 8.1 The decision: `append()` stays INLINE in the reader coroutine

The collector calls `archive.append(envelope)` directly in the coroutine that reads the
socket. It does **not** hand events to a background writer thread and it does **not**
put a queue in front of the archive.

Consequences, stated plainly:

- Append latency IS reader stall. A slow append directly stops us reading the socket.
- Backpressure propagates to TCP, and from there to the venue.
- Overload therefore ends in a **disconnect**, which is loud, timestamped, and produces
  a `seq` discontinuity the existing `SubscriptionState` detects
  (`book.py:681-684`) and the replay lane reports as a fault.

**Alternative rejected: a bounded internal queue plus a writer thread.** That is
precisely the architecture `KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1` removed after eleven
review rounds (`segment.py:1499-1518`), for a correctness reason that applies here
verbatim: a producer could be told an event was ACCEPTED while the event had already
left the only place that made it recoverable. It also measured SLOWER (1,927 vs 3,440
events/s). Re-adding it would convert a detectable disconnect into a silent drop — the
exact trade this codebase has already refused once.

**Alternative rejected: a separate collector process per subscription writing to the same
archive.** `EventArchive` supports concurrent writers safely (flock + candidate-id
advance, `archive.py:466-495`), but archive order would stop meaning wire order
(5.2 invariant 2), and the whole point of the tape is a faithful ordering.

### 8.2 The overload ladder, and what the collector does at each rung

| Rung | Condition | Behaviour | Recorded as |
|---|---|---|---|
| 0 | append p99 well under inter-arrival time | nothing | ordinary intervals |
| 1 | transient burst; library inbound buffer absorbs it | nothing; buffer drains | `reader_lag_frames_max` rises |
| 2 | sustained arrival above append throughput; buffer passes the `max_queue` high-water mark | library stops reading the socket (`transport.pause_reading`); TCP receive window closes. Nothing is dropped (12.2) | `reader_stall_ms_max` rises sharply |
| 3a | **our own keepalive fuse fires first** | `pause_reading` stops PONG frames too, and a blocking `append()` stalls the event loop including the keepalive task — so after ~`ping_timeout` (default 20 s) **this client** closes the connection with code **1011 "keepalive ping timeout"** (12.7). Local, deterministic, bounded by a knob we own | `disconnects` +1, `disconnected_at` recorded, close cause `local_keepalive_timeout` (from `ConnectionClosed.sent`) |
| 3b | venue reacts to the stalled consumer | connection closes (**assumption to verify**: Kalshi may instead buffer, or disconnect with a specific close code — this has never been observed by this repo, and on the installed library rung 3a normally fires before the venue gets a say) | `disconnects` +1, close cause `remote_close` (from `ConnectionClosed.rcvd`) |
| 4 | collector reconnects, `supersede()`, `get_snapshot` | new generation; books rebuilt from a fresh snapshot | `reconnects` +1, `subscription_generation` +1, and an explicit **observation gap** record |
| 5 | reconnects exceed `max_reconnects` | session ENDS, `status="capped_reconnects"` | terminal record, non-zero exit |

Rung 5 is a design commitment: **a collector that cannot keep up stops and says so.**
It never degrades into a partial tape whose gaps are invisible.

### 8.3 The gap is data loss and must be named as such

Between rung 3 and rung 4 the venue continued to publish and we were not connected. Those
events are gone. The collector records `disconnected_at` / `reconnected_at` and the
report prints an `observation_gap_seconds` total. It is never reported as "no events in
that period", and no rate percentile is computed across a gap boundary — a gap splits
the rate series, exactly as `latency_envelope`'s coverage block already refuses to imply
continuity (`cli.py:717-719`).

### 8.4 FINDING — can the append contract keep up with Kalshi burst rates?

**Honest answer: unknown, and nobody in this repository knows.** The only Kalshi rate
ever measured here is "4 records over ~2 minutes, DEMO" (`segment.py:208-209`). The
`~500 events/s assumed peak` (`segment.py:207`) is an assumption with no measurement
behind it. That is the gap this milestone exists to close, so the design must not
pretend to have closed it in advance.

What IS measured, and the arithmetic it implies:

- Synchronous append: **3,440 events/s** on `SegmentWriter`, sustained 2,500-5,000/s,
  bursts draining ~7,000/s (`segment.py:1514-1517`).
- Segment close: **~145 ms CPU per 1,000 records** at `commit_to_head=True`
  (`segment.py:196-198`).

At `DEFAULT_MAX_SEGMENT_RECORDS = 13_000`, a segment closes every `13000 / R` seconds at
rate `R`, and each close costs about **1.9 s of CPU**. The closer thread keeps up only
while `13000 / R > 1.9`, i.e. **R below roughly 6,900 events/s** — and that is the same
order of magnitude as the measured burst-drain ceiling of ~7,000/s. **The rotation
defaults and the append ceiling do not have an order of magnitude between them; the
margin is under 2x.** Any measured Kalshi peak in the low thousands per second puts the
system inside that margin.

Two aggravating factors the arithmetic above does not include:

1. **"Off the producer thread" is not free under the GIL.** The closer thread's work is
   substantially Python-level (`read_segment_records` parses every record;
   `verify_segment` reads the segment a third time), and Python-level work contends with
   the reader coroutine for the GIL. The archive's own measurement already shows the
   shape of this: 2.5 s wall at 13,000 records on an idle machine, **14.8 s wall at
   20,000 under CPU contention** (`segment.py:195-196`, `archive.py:500-501`).
   **Assumption to verify:** what fraction of close cost holds the GIL. Checkpoint 5
   measures reader stall during a close, directly.
2. **Rotation itself costs the producer thread something.** `_writer_for` constructs the
   successor inline (`archive.py:466-490`): `presence()` stat calls per candidate id,
   an flock, a file open. That cost lands on ONE append — the one that triggers the
   rotation. Design refinement: the metrics must keep **two** append histograms,
   `append_us_histogram` and `append_us_rotation_histogram`, or the session maximum is
   unattributable and will be misread as ordinary append latency.

### 8.5 If the finding turns out badly

If the measured venue rate approaches the ceiling in 8.4, the options — in the order
this design prefers them, and NONE of which are in scope for this milestone — are:

1. **Subscribe to fewer markets.** The subscription is explicit; the tape does not have
   to be the whole venue. This is the cheapest lever and it costs no guarantee.
2. **Lower `DEFAULT_MAX_SEGMENT_RECORDS`.** Smaller segments close faster and the closer
   keeps up at a higher rate, at the cost of more manifests and more head commits.
   This is a retune, not a redesign, and it is the follow-up milestone this one feeds.
3. **Drop `market_lifecycle_v2` / `trade` from the default channel set** if they prove to
   be a material share of volume without a downstream reader.
4. **Separate processes per market group**, accepting that cross-group ordering is not
   preserved (and saying so in the archive metadata).

Explicitly NOT on that list: a queue in front of the archive, per-record sampling, or
"drop events when busy". Any of those would make the tape's completeness unverifiable,
which is the one property the whole archive design exists to provide.

### 8.6 Other failure modes and their handling

- **Per-record rejection** (`writer.submit()` returns a `RejectReason`, surfaced as
  `ArchiveError` at `archive.py:648-649`): counted in `events_rejected`, the raw frame's
  reject reason is recorded, and the session CONTINUES. One pathological payload must
  not end a session.
- **Partition-level / filesystem failure** (`ArchiveError` from `_writer_for`,
  `ENOSPC`, an unwritable env dir per `_check_partition_writable`,
  `archive.py:537-563`): the session STOPS with `status="archive_error"`. Continuing
  would produce a tape with an unrecorded hole.
- **Rotation failure**: `EventArchive` deliberately does not wedge — the successor is
  already admitting and the failure lands in `rotation_failures`
  (`archive.py:519-522`). The collector surfaces a non-empty `rotation_failures` as a
  session-level WARNING and a non-zero exit, because an uncommitted segment is not
  evidence.
- **SIGINT / SIGTERM**: one handler sets a stop flag; the loop finishes the current
  frame, exits, and calls `archive.close()` inside a `finally`. `close()` is the commit
  point (`archive.py:653-657`) and it must run. **Note the known hazard from the archive
  work: CPython delivers signals only on the main thread**, so the handler must be
  installed on the main thread and must not do work itself.
- **SIGKILL / host crash**: the loss window is the unflushed tail (`flush_every=256`)
  plus the entire uncommitted segment. This is the archive's documented contract
  (`segment.py:1829-1838`), not something the collector can improve, and the milestone
  report must state the window in events at the MEASURED rate rather than in the
  abstract.
- **Clock skew**: `EventArchive.partition` refuses a naive datetime
  (`archive.py:407-412`). The collector always passes `utcnow()`. Host clock offset stays
  the replay lane's declared NOT MEASURED item (`cli.py:720-722`); the collector does not
  invent a correction.

## 9. Implementation checkpoints

Ten checkpoints. Each is independently verifiable and each is a commit. No checkpoint
depends on a later one being right.

**CP0 — Library capability audit (no product code).**
Establish, against the installed `websockets` version, the answers section 6.2 and 7.4
marked as assumptions: the client connect parameter that bounds the inbound buffer, its
overflow semantics (backpressure vs drop), whether inbound queue depth is readable,
whether a drop counter exists, and how a close code is surfaced. Output: a short findings
note appended to this document. **If overflow silently drops frames rather than applying
backpressure, section 8.1's whole argument changes and the design must be revisited
before CP1.**
*Verify:* the note names a version and a source (installed source, not memory), and each
answer is either confirmed or explicitly recorded as unavailable.

**CP1 — `KalshiWebsocketTransport`, offline.**
The class, its governed `send()`, its frame parsing, its counters. Tested with NO socket:
`send()` refuses a `fill` channel, refuses an unknown `cmd`, refuses a raw dict; the
class exposes no raw-send method; a non-JSON and a non-dict frame both count as
`frames_malformed` and never reach `make_envelope`.
*Verify:* new unit suite green; `FixtureTransport` still satisfies the same interface;
the AGENTS.md safety grep clean.

**CP2 — Credential loading, offline.**
`load_observer_signer` reading the two documented env vars, refusing cleanly when either
is absent, and never falling back to `UnsignedTransportSigner`.
*Verify:* tests for absent id, absent path, bad file mode; a test that asserts
`app/realtime/auth.py` remains the only file holding key material (the existing AST test
still passes).

**CP3 — Collector loop against `FixtureTransport`.**
The whole orchestrator, end to end, with no network: connect, subscribe, read N fixture
frames, envelope, dispatch, append, close, result. Uses a temp archive root initialized
via `archive-init --confirm`.
*Verify:* `kalshi-realtime-replay` on the produced archive reports `records=N`,
`faults=0`, integrity intact; the per-market checksums are deterministic across two runs
of the same fixture.

**CP4 — Measurement lane, offline.**
`collector_metrics.py`, its closed validator, the 10 s flusher thread, the 1 s ring, the
two append histograms, and the interval-record writer.
*Verify:* a fixture run at a synthetic 5,000 events/s produces intervals whose
`events_received` sums to the fixture count; a ticker never appears in the output file;
the flusher's failure paths never raise into the loop (inject an unwritable directory).

**CP5 — Instrumentation-overhead gate.**
Run the SAME fixture load with instrumentation enabled and disabled and compare append
throughput and append latency distribution. Extend `tests/benchmarks/segment_close_cost.py`
rather than writing a second benchmark.
*Verify:* the measured overhead is stated as a number and is a small single-digit
percentage of append cost, or the instrumentation is redesigned. **This checkpoint can
fail the design and is placed before any live connection for that reason.**

**CP6 — First DEMO session, dry-run.**
`--environment demo --dry-run --max-seconds 120`. Real credential, real socket, real
frames, ZERO archive appends.
*Verify:* handshake succeeds; subscription confirmed; frames arrive; no archive
directory is created or modified; `events_archived == 0`; the boundary note is printed.

**CP7 — First DEMO session, archiving.**
`--environment demo --max-seconds 300` into a purpose-initialized demo archive root.
*Verify:* `kalshi-realtime-replay` on the result reports integrity intact and zero
faults; `verify_archive` clean; the interval file parses; `emit_writer_pass` filed one
session record with `writer_name="kalshi_live_tape"`.

**CP8 — Reconnect and recovery, deliberately provoked.**
Kill the connection mid-session (locally, by closing the socket from the test harness or
by a firewall rule on the host) and confirm the recovery path.
*Verify:* `supersede()` ran, generation incremented, a fresh snapshot re-based the books,
the observation gap is recorded with both timestamps, and the replay lane reports the
generation change rather than a spurious sequence fault.

> **Unblocked by KALSHI-TAPE-GENERATION (§6.4.1).** This verify line could not
> have passed before: the generation never reached an envelope, so the replay
> lane had nothing to report a generation change *with*. The offline half is
> now proven in `tests/test_kalshi_tape_generation_001.py` — a real collector
> session over a lost socket, with the venue sequence restarting, replays with
> `faults=[]`, `generation_advances=1`, `gaps=0` and every book still
> publishable, while the same tape with the generation stripped still faults.
> CP8 remains outstanding for the part only a live socket can show: the
> observation gap's two timestamps, and a real venue-side reconnect.

**CP9 — The measurement session and the report.**
The longest DEMO session the operator authorizes (see Q2), followed by
`kalshi-tape-measure-report`.
*Verify:* the report either prints p95/p99 with the sample gate satisfied, or refuses
them and says why; each of the three rotation constants gets a verdict; the NOT MEASURED
section is non-empty and honest.

**CP10 (SEPARATE APPROVAL, Tier 2) — First PRODUCTION session.**
Not part of the merge. Requires: CP0-CP9 all passing, a production read-scoped
credential whose scopes were verified by `audit_scopes` against the live key-metadata
route, and a confirmed production WS host (`kalshi.py:52-55` records it as UNVERIFIED).
Bounded exactly as CP7 was.
*Verify:* same as CP7, in the production archive tree, plus an explicit statement that
the demo rate distribution did NOT predict the production one (or did).

## 10. Validation plan

### 10.1 Per-checkpoint proof

Stated inline in section 9 — each checkpoint carries its own *Verify* line, and none of
them is "it ran without an exception".

### 10.2 Suite-level gates (every checkpoint)

- `.venv/bin/python -m pytest -q` fully green. **Not** `-k kalshi`: the archive lane's
  own history includes a report of "suite 1097" that was actually a `-k kalshi` subset
  against a real 3,893. The number reported must be the whole suite's.
- The AGENTS.md safety grep clean, with the documented allowlist unchanged (the Kalshi
  WS auth entry is pre-existing; this milestone must not add a second allowlist entry).
- A new test asserting that no member of `FORBIDDEN_CHANNELS` is reachable from any
  configuration surface: env var, CLI argument, or config default.
- A new test asserting the collector opens no database session (no `get_sessionmaker`
  import reachable from `app/realtime/collector.py`).

### 10.3 The archive contract is proven by the archive's own tools, not by new ones

Every session's output is validated with `kalshi-realtime-replay` (`cli.py:651`) and
`verify_archive` (`segment.py:3641`). The collector introduces **no new verification
path**. This matters: a bridge milestone that shipped its own verifier would be able to
certify its own output, which is the exact failure class `EventArchive`'s docstring
describes at `archive.py:321-327` — two implementations merely intended to agree.

### 10.4 What "the measurement is trustworthy" means, concretely

The measurement is accepted only if ALL of the following hold:

1. CP5 shows instrumentation overhead is small and stated as a number.
2. The session that produced the distribution had zero `events_rejected` and zero
   `rotation_failures`, or the report explains each one.
3. The sample gate (`MIN_SAMPLES_FOR`) is satisfied for every percentile printed, and
   percentiles that fail it are refused rather than printed with a caveat.
4. No percentile is computed across an observation gap.
5. `metric_flush_drops == 0`, or the drops are reported and the affected intervals are
   excluded rather than interpolated.

If any fails, the correct outcome is "we do not yet have a measured rate", not a softer
number. The whole point of the milestone is to stop guessing; a low-confidence
measurement presented as a measurement would be worse than the current honest guess.

### 10.5 Deployment state at merge

Merged code, nothing running. No systemd unit, no timer, no MarketOps hook, no flag
flipped on EVO-X2. The runbook gains a manual-invocation section; the deployment report
records that the collector exists and is not scheduled.

## 11. Open questions for Eric

**Q1 — Demo only, or is a production session in scope? (Tier 2 decision.)**
The whole point is to kill a guess about PRODUCTION rates, and a demo session measures
demo liquidity, which may be an order of magnitude off. But a production session needs a
production read-scoped credential and touches the production WS host, which
`app/realtime/kalshi.py:52-55` records as never having been reached. Recommendation:
merge CP0-CP9 demo-only, and treat CP10 as a separate single-confirm decision once the
demo numbers exist. **If you want production rates in THIS milestone, say so now** — it
changes the credential provisioning and the risk profile, not the code.

**Q2 — How long may a session run, and how many markets? (Tier 1 or 2.)**
`max_seconds` is a hard cap with no default I am willing to pick for you. A 5-minute
session cannot satisfy the p99 sample gate; a multi-hour session on a shared host is a
different operational question. Related: how many market tickers, and which? The tape is
only as representative as the subscription. Recommendation: one 30-minute session during
a known-active window, on 5-10 liquid tickers chosen by you, as the first real
measurement — then decide about longer.

**Q3 — Default channel set.**
Recommendation: `orderbook_delta` + `ticker` only. `trade` and `market_lifecycle_v2` are
inside the safety allowlist and are legitimate market data, but they add volume without a
downstream reader today, and volume is the scarce resource in section 8. Confirm, or say
you want all four from the start.

**Q4 — Where does the tape live on EVO-X2?**
The archive is a filesystem tree, not SQLite, so it does not interact with the backup
coordination or the SQLite growth alert. But it is not free: at a measured rate it will
produce compressed GB. Candidates: under `/mnt/data` alongside the backups, or the
existing observation directory. This needs your decision because it also decides who
prunes it and under what retention — and this milestone deliberately defines **no
retention policy** for tape segments. **Naming that as an open hole rather than
inventing a retention rule is the honest move; a tape with an unowned growth curve is
how the SQLite growth alert story started.**

**Q5 — Does `kalshi_live_tape` get added to `WRITER_NAMES`? (Architectural change,
flagged.)**
Section 6.6. It is one line in a closed safety-boundary enum shared by five writers. The
alternative is that the collector files nothing in the shared telemetry file and is
invisible to the existing operator tooling. Recommendation: add it.

**Q6 — What happens if CP0 finds that the library drops frames instead of applying
backpressure?**
Section 8.1's argument depends on overload becoming a visible disconnect rather than a
silent drop. If the installed library silently drops, the options are to bound the
inbound buffer at 1 and accept that stall equals TCP backpressure, or to select a
different client. I would want your call before building on either. This is the single
finding most likely to change the design, which is why it is CP0.

**Q7 — Retune the rotation defaults in this milestone or the next?**
This design says next (section 3, non-goals), so that the retune is validated against an
independently produced number. If you would rather land the retune with the measurement
that motivates it, that is a defensible call and changes the checkpoint list, not the
architecture.

### Assumptions to verify (not decisions — flagged so nothing here is mistaken for fact)

1. The installed `websockets` client exposes an inbound-buffer bound, and exceeding it
   applies TCP backpressure rather than dropping frames. (CP0.)
2. The library exposes inbound queue depth and/or a drop counter. If not, `reader_lag`
   is unavailable and must be reported as a gap, not as zero. (CP0.)
3. **Partly resolved by 12.7.** The close code is observable — from
   `ConnectionClosed.rcvd`/`sent`, not the discouraged `close_code` property (12.5) — and
   under sustained overload OUR OWN client closes first, at ~`ping_timeout`, with code
   1011 "keepalive ping timeout" (rung 3a). What remains unverified is only the venue
   half: whether Kalshi disconnects a stalled consumer rather than buffering
   indefinitely, and with what code. Never observed by this repo. (CP8 / first overload.)
4. Two `monotonic_ns()` calls per event are negligible against append cost on the target
   host. (CP5.)
5. A material fraction of segment-close cost holds the GIL and therefore steals from the
   reader. Strongly suggested by the 2.5 s idle vs 14.8 s contended measurement
   (`segment.py:195-196`), but not directly measured. (CP5.)
6. The production WS host string at `app/realtime/kalshi.py:51` is correct. It has never
   been reached. (CP10.)
7. The measured 3,440 events/s append figure was taken on `SegmentWriter` with a
   test-shaped record; a real `orderbook_snapshot` for a deep book may canonicalize more
   slowly. The per-record size histogram in section 7 is what will tell us. (CP7.)

## 12. CP0 FINDINGS — library capability audit

STATUS: **CP0 COMPLETE.** VERDICT: **DESIGN SURVIVES CP0** — with four mandatory
corrections listed in 12.9. The gate condition stated in section 9 ("if overflow silently
drops frames rather than applying backpressure, section 8.1's whole argument changes") is
**NOT triggered**: overflow applies backpressure and drops nothing.

### 12.0 What was audited, and how

- **Installed version: `websockets` 16.0**, confirmed two ways: the distribution directory
  `.venv/lib/python3.12/site-packages/websockets-16.0.dist-info`, and the literal
  `tag = version = commit = "16.0"` at
  `.venv/lib/python3.12/site-packages/websockets/version.py:23`. A runtime probe printed
  `websockets.__version__ == "16.0"`.
- **All file:line citations below refer to the INSTALLED SOURCE** under
  `.venv/lib/python3.12/site-packages/websockets/`, read directly. Paths are given relative
  to that root. Nothing here is quoted from memory or from documentation.
- Method: source reading plus a throwaway introspection probe (`inspect.signature`, plus
  driving `Assembler` in-process with synthetic frames). **No network connection was
  opened.** Nothing was installed. No product code was written.
- Note on requirements drift: `requirements.txt:9` pins `websockets>=12.0`, which is
  satisfied by 16.0 but does not describe it. The design text was written against the v12
  API. `websockets/legacy/` is still present on disk but `legacy/__init__.py:6-11` emits a
  `DeprecationWarning` ("deprecated in 14.0"); **no design element may depend on it.**

### 12.1 Q1 — Which connect parameter bounds the inbound buffer, and its default?

**VERIFIED. The parameter is `max_queue`, and its default is `16`.**

- `asyncio/client.py:321` — `max_queue: int | None | tuple[int | None, int | None] = 16`
  in the `connect` signature. Probe output: `connect.max_queue default = 16`.
- It is passed straight through to the connection at `asyncio/client.py:362`, and stored by
  `asyncio/connection.py:59-69`, which splits it into `self.max_queue_high` /
  `self.max_queue_low`.

**Two facts the design did not have:**

1. **`max_queue` is a HIGH-WATER MARK, not a hard cap.** The library says so in its own
   comment at `asyncio/messages.py:94-97`: *"We cannot put a hard limit on the size of the
   queue because a single call to `Protocol.data_received()` could produce thousands of
   frames, which must be buffered. Instead, we pause reading when the buffer goes above the
   high limit and we resume when it goes under the low limit."* The queue is allowed to
   exceed `max_queue`. Section 6.2's phrase "an internal queue whose bound is `max_queue`"
   is therefore imprecise and must be reworded.
2. **`max_queue` accepts a `(high, low)` tuple.** With a bare int, the low mark is derived
   as `high // 4` (`asyncio/messages.py:98-99`). Probe: `Assembler(16, None) -> high, low =
   16, 4`. So the shipped default is pause at >16 frames, resume at <=4.

**Adjacent limit worth naming, because it is a hard failure and the design never mentions
it:** `max_size` defaults to `2**20` (1 MiB) per message (`asyncio/client.py:320`; probe
confirmed `1048576`). A frame exceeding it raises `PayloadTooBig`, which fails the
connection rather than dropping one message. A deep `orderbook_snapshot` is the realistic
candidate. CP1 must set this deliberately.

### 12.2 Q2 — Overflow semantics: backpressure or silent drop? **(THE LOAD-BEARING ANSWER)**

**VERIFIED: BACKPRESSURE. Frames are NEVER dropped on inbound queue overflow. Section
8.1's argument stands.**

The full code path, traced end to end:

1. A received data frame reaches `Connection.process_event`, which calls
   `self.recv_messages.put(event)` — `asyncio/connection.py:753-754`.
2. `Assembler.put` — `asyncio/messages.py:266-278` — checks only `self.closed` (raising
   `EOFError` if the stream ended), then unconditionally calls `self.frames.put(frame)`
   and `self.maybe_pause()`. **There is no capacity check and no discard branch.**
3. `SimpleQueue.put` — `asyncio/messages.py:37-41` — is three lines:
   `self.queue.append(item)` on an **unbounded `collections.deque`** (declared at
   `asyncio/messages.py:32` with no `maxlen`), then it wakes any waiter. A `deque` without
   `maxlen` cannot evict. **This is the decisive line: there is no path by which an
   accepted inbound frame is discarded.**
4. `Assembler.maybe_pause` — `asyncio/messages.py:280-289` — when
   `len(self.frames) > self.high` and not already paused, sets `self.paused = True` and
   calls `self.pause()`.
5. `self.pause` is bound to the real asyncio transport:
   `asyncio/connection.py:1006-1013` constructs the `Assembler` with
   `pause=transport.pause_reading, resume=transport.resume_reading`.
   `pause_reading()` stops the transport delivering bytes, the kernel receive buffer fills,
   and **the TCP receive window closes — genuine backpressure to the venue.**
6. Recovery: every successful read calls `maybe_resume`
   (`asyncio/messages.py:159`, `:174`, `:238`, `:256`), which at
   `asyncio/messages.py:291-300` calls `transport.resume_reading()` once depth falls to
   `<= low`.

**Empirical confirmation (in-process, no socket).** Ten frames were put into an
`Assembler(high=4, low=1)`. Result: `len(frames) == 10`, `paused == True`, pause callback
fired exactly once. **All ten frames were retained — zero loss at 2.5x the high-water
mark.** Draining to depth 1 fired `resume` exactly once. This is precisely the
"buffer absorbs it, then TCP backpressure" behaviour section 8.2 rungs 1-2 assume.

**Corollary the design must record:** `max_queue=None` **disables flow control entirely**
(`asyncio/messages.py:283-284`, `:292-293` both return early when the mark is `None`). That
yields unbounded memory growth under sustained overload — still no drops, but no
backpressure either. **CP1 must never pass `max_queue=None`.**

**One genuine discard path exists and is NOT queue overflow:** `protocol.py:641-669`
(`Protocol.discard`) drops incoming bytes *after* the connection is already closing or has
failed. It is a shutdown behaviour, not an overload behaviour, and it cannot silently eat
frames on a healthy connection. The comment at `asyncio/connection.py:1163`
("There's no backpressure") refers to the **server-side outbound `broadcast()` helper**,
not to inbound reads; it does not apply to this milestone.

### 12.3 Q3 — Is inbound queue depth readable at runtime?

**VERIFIED: YES, but only through an UNDOCUMENTED attribute chain. This is a real
maintenance liability and must be treated as one.**

- The expression is `len(connection.recv_messages.frames)`.
  - `Connection.recv_messages` is a non-underscored instance attribute declared at
    `asyncio/connection.py:98` under the comment "Assembler turning frames into messages
    and serializing reads". It carries **no docstring**, unlike genuinely public attributes
    beside it (`request`/`response` at `asyncio/connection.py:89-92` have docstrings;
    `latency` at `:109-120` has one).
  - `Assembler.frames` is declared at `asyncio/messages.py:92`.
  - `SimpleQueue.__len__` exists at `asyncio/messages.py:34-35`, so `len()` works.
- Also readable: `connection.recv_messages.paused` (`asyncio/messages.py:110`) — a boolean
  "are we currently applying backpressure right now", and
  `.high` / `.low` (`asyncio/messages.py:107`).
- `Assembler` **is** in `asyncio/messages.py`'s `__all__` (`asyncio/messages.py:14`), but
  `SimpleQueue` is not, and `SimpleQueue` docstrings describe it as a "Simplified version
  of `asyncio.Queue`... only the subset of functionality needed by `Assembler`"
  (`asyncio/messages.py:22-27`) — internal-by-intent.

**Assessment.** Not name-mangled, not underscore-private, so reading it is legal Python and
will not warn. But it is not part of the documented surface and nothing constrains the
library from restructuring it in a minor release — the v12-to-v16 restructuring is the
precedent. **CP1 must read depth defensively** (a single guarded helper, wrapped in
`try/except (AttributeError, TypeError)`, returning `None` on failure), must record
`reader_lag_frames_max` as UNAVAILABLE rather than `0` if the chain breaks, and must pin a
test that fails loudly on a `websockets` upgrade that moves it. Section 7.4's
`reader_lag_frames_max` row is therefore **satisfied but conditionally** — it is available
today at the cost of one private-ish coupling.

### 12.4 Q4 — Does a drop counter exist? Can a drop be detected at all?

**VERIFIED: NO drop counter exists — and it does not need to, because no drop occurs.**

- A case-insensitive grep for `drop|discard|overflow|counter` across every non-legacy
  module (`asyncio/`, `sync/`, `protocol.py`, `client.py`, `frames.py`, `exceptions.py`)
  returns **zero** counter-like symbols. Every hit is either a Python-version comment, the
  `Protocol.discard()` shutdown parser described in 12.2, or the outbound-broadcast note.
- A runtime probe listing every `ClientConnection` attribute matching
  `drop|count|overflow|lag` returned the **empty list**.
- There are no inbound frame/byte/message counters of any kind on the connection object.

**Consequence for section 7.4.** The `transport_dropped` counter proposed in the "dropped
events" row **has no library source and must be deleted, not zeroed** — but this is a good
outcome, not a gap: it has no source because the library has nothing to report. The three
counters that remain (`events_rejected`, `frames_malformed`, plus the collector's own
receive count) are sufficient, because the library's contribution is provably zero.
Assumption 2 in the "Assumptions to verify" list is now resolved: queue depth **is**
exposed (12.3), a drop counter **is not**, and the absence is benign.

**Can a drop be detected at all?** Within a connected session, the question is moot — the
library cannot drop. Loss can therefore enter the tape from exactly two places, and both
are already detectable by design: **(a)** the observation gap across a disconnect, which
section 8.3 already names as data loss and timestamps at both ends, and **(b)** anything
upstream of us at the venue, which surfaces only as a `seq` gap via
`SubscriptionState`. The user's lossless-with-sequence-integrity requirement is thus
achievable: **sequence integrity is the only drop detector we need, and we already have
it.**

### 12.5 Q5 — How is a close code surfaced?

**VERIFIED. Two mechanisms; the exception is the intended one.**

1. **Exception (primary).** `recv()` raises `ConnectionClosed`
   (`asyncio/connection.py:257`, documented at `:262-264`, `:299`), specialized into
   `ConnectionClosedOK` after a normal closure and `ConnectionClosedError` otherwise.
   `ConnectionClosed` is defined at `exceptions.py:77-115` and carries:
   - `rcvd` — a `frames.Close | None`; `rcvd.code` and `rcvd.reason` are the **peer's**
     close code and reason;
   - `sent` — the same for the close frame we sent;
   - `rcvd_then_sent` — `bool | None`, which side closed first from our perspective.
   `exceptions.py:100-115` shows `__str__` rendering all four combinations, so a log line
   is honest without extra work. The underlying cause of an abnormal drop is chained via
   `recv_exc` (`asyncio/connection.py:125-127`).
2. **Attributes (secondary).** `connection.close_code` and `connection.close_reason` are
   real `property` objects (`asyncio/connection.py:190-200` and `:202-212`; probe confirmed
   both are properties). **The library explicitly discourages them**:
   *"This attribute is provided for completeness. Typical applications shouldn't check its
   value. Instead, they should inspect attributes of `ConnectionClosed` exceptions"*
   (`asyncio/connection.py:195-197`).

**Directive for CP1:** record the close code from the `ConnectionClosed` exception's
`rcvd`/`sent`/`rcvd_then_sent`, not from the `close_code` property. Distinguishing
`rcvd` from `sent` is exactly how section 8.2 rung 3 tells "the venue disconnected us" from
"we disconnected ourselves" — see 12.7, which shows this distinction now carries real
weight. Enum names for logging are available from `CloseCode` (`frames.py:56-74`).
No callback mechanism exists; there is nothing else to wire.

### 12.6 Q6 — Modern entrypoint, and breaking changes that affect CP1

**VERIFIED. Entrypoint: `websockets.asyncio.client.connect`, re-exported as the top-level
`websockets.connect`.** `__init__.py` maps `"connect": ".asyncio.client"` in its
`lazy_import` alias table, so **the plain `websockets.connect` name already resolves to the
modern asyncio client** — it is not the legacy one. Importing
`websockets.asyncio.client.connect` explicitly is the unambiguous form and is what CP1
should do.

Breaking changes between the design's v12 assumptions and 16.0 that touch CP1:

| # | Change | Impact on this design |
|---|---|---|
| 1 | **`connect` is a CLASS, not a function** (probe: `inspect.isclass(connect) is True`; `asyncio/client.py:300` is `def __init__`). It is awaitable (`__await__`, `asyncio/client.py:536`) and an async context manager (`__aenter__`, `:589`). | `await connect(...)` and `async with connect(...)` both still work. Section 6.2's `async def connect()` wrapper is unaffected. **Low.** |
| 2 | **`extra_headers` is GONE from the client; the parameter is `additional_headers`** (`asyncio/client.py:311`; probe: `has extra_headers param? False`; the only surviving `extra_headers` in the tree is `legacy/server.py`). | **DIRECT HIT on section 6.2.** The signed Kalshi handshake headers from `signer.headers_for(...)` must be passed as `additional_headers=`. Using the v12 spelling raises `TypeError` at connect time. **Must be fixed in CP1.** |
| 3 | **`max_queue` semantics changed**: high-water mark with a derived low mark and tuple support, not a hard bound (12.1). | Section 6.2's wording is wrong; the behaviour is *better* than assumed. **Reword, no redesign.** |
| 4 | **`websockets.legacy` is deprecated** and warns on import (`legacy/__init__.py:6-11`). | Do not import it. Note `app/services/tennis_livefeed.py:135-140` already uses the modern top-level `websockets.connect` with `open_timeout=15`, so the existing repo precedent is correct and can be followed. **None, if avoided.** |
| 5 | **`async for ws in connect(...)` is an UNBOUNDED auto-reconnect loop** — `asyncio/client.py:602-635` is a `while True` with its own `backoff()` and `process_exception` retry policy. | **CONFLICTS with section 6.4's "no infinite retry loop" and with `max_reconnects`.** CP1 must **not** use the async-iterator form; it must drive reconnects from its own bounded loop. Worth stating explicitly because the iterator form is the ergonomic one a reader would reach for. |
| 6 | `max_size` default `2**20` raises `PayloadTooBig` and fails the connection (12.1). | Not in the design at all. **Set deliberately in CP1.** |

Frame reading itself is unchanged in shape: `recv()` (`asyncio/connection.py:257`),
`recv_streaming()` (`:335`), and `async for message in connection` (`__aiter__`,
`asyncio/connection.py:230`) all exist. **Section 6.2's `__aiter__`-yielding-parsed-dicts
transport wrapper is fully implementable on 16.0.**

### 12.7 Q7 — Keepalive defaults, and the finding that changes section 8.2

**VERIFIED: `ping_interval = 20`, `ping_timeout = 20`** (`asyncio/client.py:317-318`;
probe confirmed both as `20`). Related: `open_timeout = 10` (`:316`),
`close_timeout = 10` (`:319`), `write_limit = 2**15` (`:322`).

Behaviour, from `Connection.keepalive` (`asyncio/connection.py:808-854`): a task started
only when `ping_interval is not None` (`:856-862`) sleeps `ping_interval - latency`
(`:820`), sends a ping (`:827`), and awaits the pong under
`asyncio_timeout(self.ping_timeout)` (`:831-838`). **On timeout it calls
`self.protocol.fail(CloseCode.INTERNAL_ERROR, "keepalive ping timeout")`
(`asyncio/connection.py:844-848`) — closing the connection with code 1011**
(`frames.py:70`). Latency-compensated interval means the effective period is
`ping_interval` measured from pong receipt.

**THE FINDING — section 8.2 rung 3 names the wrong actor.** The two mechanisms interact in
a way the design did not anticipate:

- `transport.pause_reading()` (12.2 step 5) stops the transport delivering **all** bytes,
  which includes **PONG control frames** — control frames are not exempt from transport-level
  pausing, and the pong is only processed in `process_event`
  (`asyncio/connection.py:756-757`), which cannot run while reading is paused.
- Section 8.1 additionally mandates that `archive.append()` is **synchronous and blocking in
  the reader coroutine**. A blocking call in a coroutine blocks the **entire event loop** —
  including the keepalive task and pong processing. Backpressure here arises from the loop
  not running at all, which is still lossless (the kernel buffer fills, the TCP window
  closes), but it stalls keepalive identically.

**Therefore: under sustained overload, the connection is killed by OUR OWN CLIENT after
~`ping_timeout` (default 20 s), with close code 1011 and reason "keepalive ping timeout",
before the venue ever gets a say.** Rung 3's "venue reacts to the stalled consumer" is not
the first thing that happens.

This is **good news for the design's honesty properties and it strengthens section 8.1**:
the overload failure is local, deterministic, bounded by a knob we control, and loudly
labelled — rather than depending on unobserved Kalshi behaviour. It is also exactly why
12.5's `rcvd`-vs-`sent` distinction matters: a self-inflicted 1011 appears in `sent`, a
venue-initiated close appears in `rcvd`. **Section 8.2 rung 3 must be rewritten** to name
the local keepalive fuse as rung 3a and the venue reaction as rung 3b, and assumption 3
in the "Assumptions to verify" list is now only partly about Kalshi.

Note also that `keepalive()` swallows non-timeout exceptions into
`self.logger.error("keepalive ping failed", exc_info=True)` (`asyncio/connection.py:853-854`)
— a logging-only path CP1 should ensure is captured, since it is otherwise invisible.

### 12.8 Answer summary

| Q | Answer | Status | Primary citation (installed source) |
|---|---|---|---|
| 1 | `max_queue`, default `16`; a high-water mark, not a hard cap; accepts `(high, low)`, low derived as `high // 4` | VERIFIED | `asyncio/client.py:321`; `asyncio/messages.py:94-99` |
| 2 | **BACKPRESSURE. No drop path exists**; unbounded `deque` + `transport.pause_reading` | VERIFIED | `asyncio/messages.py:32`, `:37-41`, `:266-289`; `asyncio/connection.py:1006-1013` |
| 3 | Yes: `len(conn.recv_messages.frames)` — **undocumented chain, private-by-intent `SimpleQueue`**; also `.paused` | VERIFIED (with liability) | `asyncio/connection.py:98`; `asyncio/messages.py:34-35`, `:92`, `:110` |
| 4 | **No drop counter anywhere** — and none is needed; no inbound counters at all | VERIFIED | exhaustive grep + attribute probe, both empty |
| 5 | `ConnectionClosed{,OK,Error}` with `rcvd` / `sent` / `rcvd_then_sent`; `close_code` property exists but is explicitly discouraged | VERIFIED | `exceptions.py:77-115`; `asyncio/connection.py:190-212` |
| 6 | `websockets.asyncio.client.connect` (also top-level `websockets.connect`); six breaking changes, two material | VERIFIED | `__init__.py` alias table; `asyncio/client.py:300-329` |
| 7 | `ping_interval=20`, `ping_timeout=20`; on timeout the **client itself** closes with 1011 | VERIFIED | `asyncio/client.py:317-318`; `asyncio/connection.py:808-854` |

Nothing was recorded as UNAVAILABLE. All seven questions were answered from the installed
source.

### 12.9 VERDICT

**DESIGN SURVIVES CP0.** The load-bearing assumption is confirmed: the installed
`websockets` 16.0 applies TCP backpressure on inbound overflow and **has no code path that
discards a received frame**. Section 8.1's decision to keep `append()` inline — and its
rejection of the bounded-queue-plus-writer-thread alternative on the grounds that the
latter "would convert a detectable disconnect into a silent drop" — is **correct on the
installed library, for the reason stated.** The user's lossless requirement is achievable,
and sequence integrity remains the only drop detector required.

Four corrections are mandatory before/within CP1. **None is architectural; all are
factual.** **All four are APPLIED in this document as of CP1** — §6.2 (items 1 and 2),
§8.2 rung 3a/3b and assumption 3 (item 3), and §7.4 (item 4).

1. **§6.2 — wording.** Replace "an internal queue whose bound is `max_queue`" with the
   high-water-mark description (12.1), and strike the "ASSUMPTION TO VERIFY" paragraph,
   replacing it with a pointer to 12.2. Add `max_size` to the transport's constructor
   parameters (12.1).
2. **§6.2 — API.** The signed handshake headers must be passed as `additional_headers=`,
   not `extra_headers=` (12.6 row 2). This is the one change that would fail at runtime.
3. **§8.2 — rung 3 is wrong about who disconnects.** Split into 3a (our own keepalive fires
   at ~`ping_timeout`, close 1011 "keepalive ping timeout", locally controlled and
   deterministic) and 3b (the venue's own reaction, still unobserved). Update assumption 3
   accordingly (12.7).
4. **§7.4 — measurement rows.** Delete `transport_dropped` (no source, and none needed —
   12.4). Keep `reader_lag_frames_max` but mark it as resting on an undocumented attribute
   chain, with the defensive-read and UNAVAILABLE-not-zero rules from 12.3.
   `reader_stall_ms_max` is unaffected and remains the primary overload signal.

One additional guard, not a correction but a trap worth pinning: **§6.4's bounded reconnect
policy forbids the `async for ws in connect(...)` form**, which carries its own unbounded
`while True` retry loop (12.6 row 5). CP1 must drive reconnects from the collector's own
`max_reconnects`-bounded loop, and `max_queue=None` must never be passed (12.2).

---

## 13. CP5 findings — instrumentation-overhead gate

**VERDICT: GATE PASSED**, on the direct estimator, with a stated limit on what
was and was not resolvable. Measured 2026-08-15 via
`tests/benchmarks/segment_close_cost.py --mode cp5 --cp5-records 40000
--cp5-reps 8`. **No measured code was modified** — the benchmark file is the
only change in this checkpoint.

### The number

| estimator | point | 95% CI | noise floor | status |
|---|---|---|---|---|
| **E1 direct** | **+901.0 ns/event** | ±166.1 | 93.8 ±90.8 | **RESOLVED** (~9.6× S/N) |
| E2 throughput | +58,778 ns/event | ±290,670 | 137,034 ±92,705 | UNRESOLVED |
| E3 cpu-time | −2,960 ns/event | ±64,767 | 72,953 ±57,509 | UNRESOLVED |

Denominator: append p50 (null arms) = **329,190 ns**.
**E1 = +0.27% of append p50; upper 95% bound 0.32%** — inside the
small-single-digit-percent requirement.

### What was NOT resolved, and why no re-run fixes it

E2 and E3 are unresolved, and **that is structural, not a host-quality problem.**
E2's noise floor is 137,034 ns/event against a 901 ns signal; since variance
falls as 1/√n, resolving it would need on the order of **22,000× more
repetitions**. Inferring a 0.3% effect from ~27-second throughput runs is not
achievable at any plausible rep count or host quietness. A "clean re-run" was
considered and rejected for this reason rather than for cost.

So the claim this checkpoint supports is precisely:

> The **direct** cost of `on_frame` + `on_append` is 0.27% of append (upper
> bound 0.32%). The **systemic** effect — cache pressure, allocation, flusher
> contention — is unresolved on any rig available, with **no positive evidence
> of a problem** (E3's point estimate is negative, i.e. the instrumented arm was
> nominally faster).

### Rig, and a contamination that was caught

Load during the run: 1m 7.45 → 6.77, 5m 7.57 → 8.21, on **8 CPUs** — an
oversubscribed host. `CPU per event 602,847 ns vs 674,447 ns wall`: the ~10.6%
gap **is** the contention, measured rather than assumed.

Mid-run, an **orphaned `while True: pass` process leaked by the CP1 agent** was
found pinning a full core for ~2 hours (reparented to launchd, so it survived
its agent's exit) and was killed. Two consequences are recorded here rather than
tidied away:

1. **CP4's indicative "~395 ns/event ≈ 0.14%" was measured in that same window
   and should not be relied on.** It was already labelled "not the gate"; it is
   now downgraded further to "measured on a known-contaminated rig".
2. **The two-null-arm design is what caught this.** `null_a` vs `null_b`
   measures the noise floor directly, and it exposed a ~200,000 ns/ev floor
   against a ~900 ns signal. A single-null benchmark would have reported
   scheduler noise as an overhead result and passed the gate for the wrong
   reason.

### An actionable constraint for CP3↔CP4 wiring

```
seam wrap: direct p50 = 125 ns   vs   try/except + **kwargs p50 = 375 ns  (+250 ns)
```

A defensive `try/except` + `**kwargs` wrapper at the metrics seam costs **250 ns
— 28% of the entire 901 ns budget.** When the orchestrator wires
`CollectorMetrics`, it must be a **direct call**, not a defensive wrapper.
`NULL_METRICS` already implements the identical surface as no-ops precisely so
neither arm needs an `if metrics` branch in the hot path.

### Method notes worth keeping

- Arms **interleaved and order-varied** within each rep, so thermal state and
  page-cache warmth are not attributed to instrumentation.
- **Two null arms**, so the noise floor is measured rather than assumed.
- The clock-pair quantum (p50 42 ns) and the `path != path` rotation check
  (p50 167 ns, charged to **both** arms) are reported separately, so neither is
  silently folded into the result.
- Segment rotation is inside the measured window (2–3 rotations per run), so the
  closer thread competes during measurement as it will in production.

### 13.1 CORRECTIONS to §13, and an integration gap CP5 exposed

Two amendments, both from the CP5 agent's continued analysis after §13 was
first written. §13's figures above are superseded by these.

#### 13.1.1 The overhead number was too SMALL — corrected to 0.31%

`NULL_METRICS._noop(*args, **kwargs)` packs a varargs tuple and a kwargs dict on
every call — work a genuine no-instrumentation build would never pay. The null
arm was therefore slightly *too slow*, so the raw `real − null` difference
**understated** the true cost.

Measured correction: **+125 ns/event**.

| | ns/event | % of append p50 (329,190 ns) |
|---|---|---|
| §13 as first written | 901 | 0.27% |
| **corrected** | **≈1,026** | **≈0.31%** |

**GATE STILL PASSES** — 0.31% is well inside the small-single-digit-percent
requirement. Recorded because the correction moved the number in the direction
that flatters the design, which is exactly the direction that must not be left
unstated.

Independent cross-check worth noting: append p50 ≈ **329 µs** on the null arms
reproduces `segment.py`'s documented **3,440 ev/s** append ceiling from a
completely different measurement path.

Rig: Apple M2 (Mac14,15), 8 cores, 8 GB, macOS 26.2, CPython 3.12.3, contended
throughout — 1-minute load ranged **4.77 → 24.29** across runs. 24 runs,
960,000 archived events, 0 rejections, 3 rotations per run with the closer still
draining at loop end.

#### 13.1.2 CP3 AND CP4 DO NOT AGREE ON THE METRICS SEAM — the lane is unwired

**`CollectorMetrics` has no caller anywhere in `app/`.** Verified: the only
matches outside `collector_metrics.py` itself are prose in `collector.py`'s
docstrings. CP4's measurement lane is implemented and tested but is
**unreachable production code**.

The two checkpoints designed different interfaces:

| | CP3 (`collector.py`) | CP4 (`collector_metrics.py`) |
|---|---|---|
| per frame | `observe_frame(*, event_type, archived, append_ns, …)` | `on_frame(received_mono_ns, wire_bytes)` |
| per append | — (folded into `observe_frame`) | `on_append(ns, rotated=)` |
| counters | `observe_event(name)` from a closed set | `on_sequence_fault(...)`, `on_disconnect()`, … |
| call style | inside a `try/except`, failures counted in `metrics_errors` | direct call |

**Cause: an orchestration error, not an agent error.** The two checkpoints were
run in parallel under strict file ownership — CP3 was told to define a hook and
not implement the metrics lane; CP4 was told to define the interface it expected
and not wire it. Both complied exactly as instructed. Nothing reconciled the two
interfaces, because nothing was asked to.

**This is an open work item and P1 is NOT complete without it.** A bridging
checkpoint (CP3.5) must reconcile the seam before CP6, since a live session with
an unwired metrics lane measures nothing — which is the entire point of the
milestone.

Costs to carry into that decision, all measured:

- CP3's `try/except` wrapper: **+208 ns/frame** on top of the 1,026 ns
  (total ≈1,234 ns ≈ **0.37%** of append — still passing).
- The design's only rotation signal, `path != previous_path`: **~125–167
  ns/frame**, charged to **both** arms.

**Recommendation:** keep CP3's `try/except`. CP4 already proves it never raises
(six injected faults, none propagate), so the wrapper is redundant defence — but
at 208 ns it is 0.06% of append, and the failure it guards against is the
metrics lane killing a live capture session. That trade is worth 0.06%.

---

## 14. CP3.5 — the seam, wired and proven

**STATUS: CLOSED.** §13.1.2's open work item is done. `CollectorMetrics` has a
caller in `app/`: `app/realtime/collector.py` calls its typed methods directly.

### What the reconciliation actually was

`observe_frame(**kwargs)` and `observe_event(name)` were **deleted, not
forwarded.** An adapter that maps one shape onto the other at runtime is a
second interface pretending to be a bridge, and the next reader would have had
to hold both to know what a counter means. The surviving interface is CP4's,
because it is the one with a typed method per event class — the shape that
cannot silently widen.

Each call sits in **its own** narrow `try/except` that counts
`CollectorResult.metrics_errors` and suppresses. Not one wrapper around many
calls: a `try` holding two observations lets the first one's failure hide the
second entirely, which is asserted against in test 24.

| collector event | typed call | exactly-once proof |
|---|---|---|
| every frame off the wire | `on_frame(received_mono_ns, wire_bytes)` | tests 7, 9, 15, 16 |
| a frame that is not a dict | `on_frame_malformed()` | test 9 |
| an accepted `archive.append` | `on_append(elapsed_ns, rotated=)` | tests 8, 16 |
| a typed record rejection | `on_append_rejected(elapsed_ns)` | test 8 |
| a sequence fault | `on_sequence_fault("gap"\|"regression"\|"duplicate")` | tests 10, 11, 12 |
| a connection the venue ended | `on_disconnect()` | tests 13, 15 |
| a reconnect attempt | `on_reconnect(subscription_generation=)` | tests 13, 14 |
| a new epoch actually beginning | `on_subscription_generation(generation)` | test 14 |

### Three defects the wiring exposed and fixed

1. **`on_reconnect`'s generation had no channel and was never supplied.** The
   old three-method seam could not carry it, so the interval record's
   `subscription_generation` would have sat at 0 while the tape stamped 1, 2, 3.
   The reconnect now carries the epoch it is LEAVING and
   `on_subscription_generation` reports the one it arrives in — the two move on
   different events, and a session that reconnects would otherwise report a
   gauge one epoch behind its own evidence for the rest of its life. Test 14
   compares the gauge against the generation read back off the tape.
2. **`NULL_METRICS` was a varargs sink.** §13.1.1 measured that at 125 ns/event
   of work a no-instrumentation build never pays; every method is now written
   out with its real parameters, so both arms of the gate are honest.
3. **Rotation was detected from `archive.rotations`**, which the CLOSER thread
   increments when a retired segment finishes closing — so the signal arrived on
   another thread, late, and was attributed to whichever append happened to
   observe it. It now uses the segment `Path` the append returned, which is
   CP4's documented signal, observed on the producer thread, on the append that
   actually paid the cost.

### The cost, re-measured against the wired shape

`tests/benchmarks/segment_close_cost.py` now runs **the shape `collector.py`
contains** in its measured loop, and prices all three proposed shapes side by
side in one loop iteration:

| seam shape | p50 | vs direct |
|---|---|---|
| typed direct call | **83 ns** | — |
| **CP3.5: typed direct inside its own `try/except`** | **83 ns** | **+0 ns** |
| CP3: `try/except` + `**kwargs` wrapper | 292–333 ns | +209 to +250 ns |

**The boundary is free at this resolution** — it sits below the host's 42 ns
clock quantum, and the mean over 300,000 iterations is −0.5 to +1.9 ns across
three runs. §13's +208/+250 ns was the **keyword packing and the extra call
frame**, not the exception handler; CPython 3.11+ makes a non-raising
`try/except` zero-cost. Eric's requirement — cost the wrapper's 208 ns or less
— is met with the whole of it to spare.

Full gate, re-run against the wired seam (`--cp5-records 20000 --cp5-reps 6`,
Apple M2, 8 cores, load 1m 3.06 → 3.66):

| estimator | point | 95% CI | noise floor | status |
|---|---|---|---|---|
| **E1 direct** | **+829.1 ns/event** | ±32.2 | 20.5 ±23.6 | **RESOLVED** |
| E2 throughput | +11,975 ns/event | ±15,599 | 4,551 | UNRESOLVED |
| E3 cpu-time | +7,583 ns/event | ±10,448 | 3,658 | UNRESOLVED |

Denominator: append p50 (null arms) = 264,285 ns. **E1 = +0.31% of append p50,
upper 95% bound 0.33% — GATE PASSED**, and identical as a percentage to §13.1.1's
corrected 0.31% despite a quieter host and a faster append. E2/E3 remain
unresolved for the structural reason §13 gives; nothing about this checkpoint
changes that.

Note the arithmetic: E1 is now measured against a null arm with **typed**
no-ops, so the 829 ns already contains §13.1.1's +125 ns correction. It is not
comparable to §13's uncorrected 901 ns and is not an improvement over the
corrected 1,026 ns — the append got faster on this run, and the ratio, which is
what the gate is about, did not move.

### What is still NOT wired, and why

**CLOSED by KALSHI-TAPE-CLOSE-CALLBACK (§15).** As written at CP3.5:

> `on_segment_closed(elapsed_ns)` — documented as a closer-thread call from an
> `_on_rotation_closed` hook. `EventArchive` exposes no callback seam for one, and
> reaching into `archive._closer` from the collector is exactly the private
> coupling CP4 refused to take. `segments_closed`, `segment_close_ms_histogram`
> and `segment_close_ms_max` therefore stay at zero in production until an owned
> change to `archive.py` adds the hook. Test 6 asserts this method is absent from
> the collector, so the gap stays visible instead of being forgotten.

That owned change to `archive.py` is now made. Test 6's absence assertion moved
into `SEAM_METHODS` rather than being deleted, and gained the seam's arrival
point (the `EventArchive(on_segment_closed=)` keyword) — see §15's audit note.

Sequence faults outside `gap|regression|duplicate` (`wrong_sid`,
`stale_generation`, `missing_sequence`, `awaiting_snapshot`, book-level
refusals) are **deliberately not forwarded**: the interval record's field set is
closed and has no bucket for them, and `on_sequence_fault` counts an unknown
kind as an `observe_error` — a claim that the metrics lane malfunctioned. They
are counted in `CollectorResult.sequence_faults`. Test 12 pins that they reach
neither a wrong bucket nor the error counter. Giving them a bucket is a schema
change and belongs to whoever next opens that schema.

### Audit amended

`tests/test_kalshi_live_tape_cp2_001.py::test_32` (dependency direction) now
admits `app.realtime.collector_metrics` — **an audit that forbids the wiring is
an audit that certifies unreachable code.** Kept net-stronger: the metrics
module's own app-level imports are held to the same equality
(`{app.realtime.kalshi, app.telemetry.sink}`), the downward-only direction is
asserted in the audit that governs it rather than only in CP4's own suite, and
each new arm carries an anti-vacuity assertion.
