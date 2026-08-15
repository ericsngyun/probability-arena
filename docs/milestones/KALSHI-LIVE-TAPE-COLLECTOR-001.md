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

- `EventEnvelope` (`app/realtime/book.py:54-86`) — raw + normalized + full time lineage;
  `data_age_us` is INTEGER microseconds by canonical-representability requirement
  (`:76-80`).
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
                     max_queue: int, read_timeout_s: float) -> None
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
- **`send()`** is the new structural control described in section 4: `cmd` must be in
  `("subscribe", "unsubscribe", "update_subscription")`, any `params["channels"]` is
  re-run through `assert_channels_allowed`, and any other shape raises `CapabilityError`.
  No `send_text`/`send_bytes` exists. The underlying connection object is private and no
  collector code touches it.
- **`__aiter__`** yields *parsed dicts*. A frame that is not JSON, or not a dict, is
  counted as `malformed_frames` and skipped — it is not an envelope and must never be
  passed to `make_envelope`, which would archive a nonsense record.
- **Backpressure is made explicit at the library boundary.** `websockets` buffers
  incoming frames in an internal queue whose bound is `max_queue`. This is the single
  most important knob in the whole design and section 8 is about it. **ASSUMPTION TO
  VERIFY:** that the installed `websockets` version exposes `max_queue` on the client
  connect API and that exceeding it applies TCP backpressure rather than dropping
  frames. `docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md:213` records `websockets 16.0`
  as available; the checkpoint that introduces the transport must confirm the parameter's
  name and semantics against the installed version before anything is built on it.

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
2. `envelope = make_envelope(...)` — existing code, unchanged.
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
  `subscription.supersede(market_tickers=...)` (`book.py:696-707`), because the new
  stream's `seq` numbers are in a different namespace. The new generation number rides
  into every subsequent envelope so the archive record carries
  `subscription_generation` (`archive.py:633`) and replay can tell a straggler from a gap.
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
| **archive close latency** | Wall time of the CLOSER thread's work per segment, obtained via a callback on `_on_rotation_closed`, plus `wait_for_rotations` duration at session end | Close is already OFF the producer thread (`archive.py:496-512`). Measuring it must not put it back on: the timing is taken on the closer thread |
| **dropped events** | Three separate counters that must never be merged: `events_rejected` (archive said no, typed), `frames_malformed` (unparseable), and `transport_dropped` (the library's own drop counter, if it exposes one) | A single "dropped" number would hide which layer failed. **Assumption to verify:** whether the installed `websockets` exposes a drop/overflow counter at all; if it does not, that is a measurement GAP and must be reported as one, not zeroed |
| **backpressure / lag** | `reader_lag_frames_max` from the library's inbound queue depth if exposed; ALWAYS also `reader_stall_ms_max` — the longest wall gap between consecutive successful reads — which needs no library support | Stall time is the property that actually matters and is measurable regardless. **If queue depth is not exposed, say so; do not report zero lag** |

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
| 2 | sustained arrival above append throughput; buffer reaches `max_queue` | library stops reading the socket; TCP receive window closes | `reader_stall_ms_max` rises sharply |
| 3 | venue reacts to the stalled consumer | connection closes (**assumption to verify**: Kalshi may instead buffer, or disconnect with a specific close code — this has never been observed by this repo) | `disconnects` +1, `disconnected_at` recorded |
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
3. Kalshi disconnects a stalled consumer rather than buffering indefinitely, and the
   close code is observable. Never observed by this repo. (CP8 / first overload.)
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
