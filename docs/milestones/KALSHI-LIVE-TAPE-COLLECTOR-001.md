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
