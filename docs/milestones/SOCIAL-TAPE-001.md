# SOCIAL-TAPE-001 — social collection infrastructure

**Status: BUILT, DEPLOYED NOWHERE, ACTIVATED NOWHERE.**
Branch `SOCIAL-TAPE-001`, not merged. Nothing in this milestone has opened a
connection to X, Telegram, or Discord — not once, not in a test. No credential
was requested, read, printed, or stored.

**This is a tape, not a strategy.** Nothing here ranks, scores, weights, or
predicts. Every module carries a `CONTAINS NO SIGNAL` line, and that is a
boundary, not a decoration: the moment collection starts selecting for what
looks interesting, the tape stops being a record of what happened and becomes
an artefact of what we already believed.

---

## §0 — Activation preconditions

Activation requires **all three**, jointly:

1. **A named source universe.** `app.social.sources.EMPTY_SOURCE_UNIVERSE` is
   empty and `load_source_universe()` refuses an empty file. 100–300 named
   rules, each with a stated rationale and an `active_from`.
2. **A configured monthly cost cap.** There is no default and no unlimited
   mode; `CostBudget.from_config(None)` raises.
3. **Separate authorization**, recorded as an amendment to this document.
   Telegram/Discord additionally require a per-channel `AuthorizationGrant`.

Absent any one of them, the collector refuses to start. That refusal is tested
(`TestStartupPreconditions`), including the positive control that it *does*
start when all three are supplied.

---

## §1 — What is collected

One `SocialArtifact` per item (`app/social/artifact.py`), frozen and JSON
round-trippable:

| field | provenance | notes |
|---|---|---|
| `platform` | COLLECTOR_FACT | closed enum; a new platform is a schema change |
| `source_id` | VENUE_FACT | the configured source it came from |
| `message_id` | VENUE_FACT | the platform's id for this item |
| `author_id` | VENUE_FACT | |
| `source_created_at` | VENUE_FACT | **their clock** — see §2 |
| `our_received_at` | COLLECTOR_FACT, LIVE_ONLY | **our clock** — see §2 |
| `raw_content` | VENUE_FACT | the exact bytes, verbatim |
| `raw_content_hash` | DERIVED_STATE | sha256 over those bytes, before decode |
| `content_text` | DERIVED_STATE | extracted authored text; what content identity hashes |
| `matching_rule` | COLLECTOR_FACT | which configured rule admitted it |
| `parent` | VENUE_FACT | reply / quote / rebroadcast relation — see §5 |
| `delivery_mode` | COLLECTOR_FACT | LIVE / BACKFILL / PULLED / UNKNOWN — see §2.4 |
| `media` | VENUE_FACT | references only; `retrieved` is always False |
| `entity_resolution` | DERIVED_STATE | confidence + `resolved_mint` + `first_entity_resolution_at` |
| `first_onchain_reaction` | — | **typed ABSENT** |
| `first_price_reaction` | — | **typed ABSENT** |
| `delivery_sequence`, `subscription_generation` | COLLECTOR_FACT | stream identity |
| `delivery_offset` | DERIVED_STATE | contaminated cross-clock offset — see §2.3 |
| `ingestion_version` | COLLECTOR_FACT | which parser produced this record |

### Typed absence

`first_onchain_reaction` and `first_price_reaction` are structurally present
and `DeferredState.ABSENT` from ingestion. They cannot be constructed with a
value while ABSENT, and the enum distinguishes four states that a `None` would
collapse:

| state | means |
|---|---|
| `ABSENT` | nobody has looked |
| `OBSERVED` | we looked and found a reaction |
| `OBSERVED_NONE` | we looked and there was none |
| `NOT_APPLICABLE` | the question cannot apply here |

This is AGENTS.md doctrine 10 at field level: an unobserved price reaction
recorded as `0` reads as "the market did not move", which is fabricated market
state, and every model built on it inherits the fabrication.

---

## §2 — TIMESTAMP SEMANTICS (the point of this milestone)

`app/social/timebase.py`. The two quantities are **different types**, so
conflating them is a construction error rather than a plausible number.

### 2.1 What each one is

**`source_created_at`** — the platform's claim about when the item was created.
A foreign clock we do not control, do not synchronise against, and cannot
audit.

*Trustworthy for:* coarse ordering of items within one platform; joining back
to the platform's own API; detecting back-dating when compared against our
receipt.

*Not trustworthy for:* any interval involving our system; any sub-second claim;
any cross-platform ordering; any statement about how fast something reached us.

**`our_received_at`** — when *our* process first held the bytes. Carries three
things, not one: wall-clock UTC (for joining to other systems), `monotonic_ns`
(for intervals), and `epoch_id` (which process run read the monotonic clock).

**This is the perishable quantity.** Prices, follower counts, post bodies and
on-chain state can all be re-fetched later. The instant a byte arrived cannot.
If `our_received_at` is wrong, no later work repairs it, and every lead-lag
result built on the tape is wrong in a way that will look fine.

### 2.2 Why `source_created_at` cannot support a latency claim about our pipeline

A pipeline latency is an interval between two events *we* observed, on *one*
clock, in *one* process. `source_created_at` is none of those:

* it is stamped by a machine whose offset from ours is uncharacterised;
* on several platforms it is truncated or rounded, so its resolution is coarser
  than the interval being claimed;
* on several platforms it is assigned at *request admission*, not at fan-out —
  so it excludes the platform's own internal queueing, which is precisely the
  component a delivery-latency claim is trying to capture;
* it can be back-dated, edited, or re-issued, and nothing on the wire
  distinguishes a re-issue from an original.

Enforced structurally: `pipeline_interval_us()` accepts only two
`OurReceivedAt` values, refuses cross-epoch pairs, and cannot receive a
`SourceCreatedAt` — that type has no `monotonic_ns` to offer.

### 2.3 The delivery-offset distribution, and what it means for lead-lag

`delivery_offset()` returns `offset_contaminated_us`, named at length after the
house precedent `venue_to_receive_offset_contaminated_ms`:

```
offset = true_delivery_lag
       + (our_clock_offset − their_clock_offset)
       + (their_stamp_semantics − actual_creation_instant)
```

Two of three terms are uncharacterised, so this is **evidence, not a latency**,
and the record carries `host_clock_offset_characterised: false` to say so.
Negative samples are kept, never dropped: on a cross-clock hop, negatives *are*
the offset evidence.

For a future lead-lag claim:

* Its **spread**, not its centre, is the usable part. A constant offset cancels
  out of any within-platform comparison; a heavy right tail does not, and the
  tail is what decides whether "post preceded price move by 400 ms" survives.
* Its width is a **floor on the resolution of any claim made against
  `source_created_at`**. If the offset's IQR is 3 s, a 400 ms lead measured
  that way is unmeasurable, whatever the p-value says.
* Comparing it **across platforms is meaningless** until each platform's stamp
  semantics are separately verified (doctrine 8: re-read the field across a
  known interval and observe what moves it — `SourceTimeFidelity` is
  `UNVERIFIED` for X's `created_at` today, and saying so is the honest state).
* A lead-lag claim measured against **`our_received_at`** instead is sound in
  our frame, and is a statement about *when we could have known* — which is the
  tradeable question anyway.

### 2.4 `delivery_mode`, and why the schema needs it

A backfilled item has an honest `our_received_at` and an honest
`source_created_at`, and pooling it with live items produces a delivery-latency
distribution with a fabricated tail. `DeliveryMode` is a **required field with
no silent default**; `UNKNOWN` is never optimistically read as `LIVE`.

### 2.5 The structural guarantees, and their tests

| guarantee | mechanism | test |
|---|---|---|
| `our_received_at` is only ever read from a clock | `capture_receipt()` takes no timestamp parameter; `ReceiptClock` is `() -> (datetime, int)` | `test_receipt_is_read_from_the_clock_not_from_the_payload` |
| nothing routes platform time into the receipt position | AST walk of every keyword arg and assignment in `app/social/` | `test_no_source_module_populates_our_received_at_from_source_time` (+ positive control) |
| an unreadable creation time is refused, not substituted | `_build_artifact` raises; no fallback branch exists | `test_a_post_with_no_creation_time_is_refused_not_stamped_with_ours` |
| naive timestamps are refused on both sides | `SourceCreatedAt.from_platform`, `capture_receipt` | `test_naive_platform_timestamps_are_refused` |
| our pipeline cannot be measured with their clock | type signature of `pipeline_interval_us` | `test_pipeline_interval_refuses_platform_time` |

The refusal in row 3 is the sharp one: had a missing `created_at` silently
become our receipt time, `delivery_offset` would be exactly zero and the
pipeline would look flawless.

---

## §3 — The raw tape

`app/social/tape.py`, deliberately the same shape as `app/realtime/segment.py`
(**which is frozen and untouched** — PROD-ACTIVITY-PROFILE-001 is capturing
live Kalshi windows against it). Reused ideas, not reused code paths:

* **Explicit digest field list.** `TAPE_RECORD_FIELDS` is enumerated, so a
  field added later cannot silently fall outside the digest.
* **Identity-derived genesis.** `genesis_digest(segment_id, environment)` — a
  constant sentinel would let record #1 of one segment splice into another.
* **Order folded into the chain.** `ordered_stream_digest` makes a reorder
  detectable even though every self-digest still verifies.
* **Atomic manifest commit.** temp file → fsync → `os.replace` → directory
  fsync. A half-written manifest is never observable.
* **Segment chaining.** each manifest names `previous_segment_digest`.
* **Verbatim payload.** raw bytes preserved and hashed before any decode, so a
  later re-parse is auditable against what actually arrived.
* **Synchronous writer**, per KALSHI-ARCHIVE-SYNCHRONOUS-SIMPLIFICATION: the
  queue that was removed there had made Ctrl-C durability strictly worse.

Four record kinds, because a gap nobody recorded is indistinguishable from a
quiet market:

| kind | written when |
|---|---|
| `artifact` | an item was collected |
| `redelivery` | the stream handed us something we already had |
| `stream_event` | connect / rule reconciliation / platform error / unparseable frame / run report |
| `absence` | a typed statement that we know we were NOT collecting |

`replay()` refuses to yield anything from a segment that does not verify: a
replay that tolerates a broken chain is a guess.

The manifest **pins the source universe and the process epoch**. Without the
universe pinned, a later reader cannot tell whether a quiet segment means
"nothing happened" or "the rule set changed under us".

---

## §4 — The cost guard

`app/social/cost_guard.py`. X post reads are priced per read against a monthly
cap, so the counter is a hard, persisted, pre-incremented gate, not advice.

**Four refusal conditions, all fail-closed:**

1. **No budget configured** → refuse to start. A default budget is a budget
   nobody chose.
2. **Budget exhausted** → stop at the cap, and stay stopped.
3. **Counter unreadable** → missing digest, corrupt JSON, truncated file,
   failed integrity check, unknown version, negative count, permission error.
   "We cannot say what has been spent" is treated as "we may have spent it
   all". A guard that opens when it cannot see is not a guard.
4. **Counter unwritable** → the read it would have paid for does not happen.
   We would rather lose a post than lose the count.

**Pre-increment ordering.** The counter is incremented and fsynced *before* the
read it pays for. A crash in between over-counts by the in-flight reservation;
the opposite ordering under-counts, and under-counting is the only error
direction that can produce a bill nobody authorized.

**Rollover asymmetry.** A month strictly *after* the stored one rolls over and
carries the closed period's final count forward. A month *before* it is a
refusal (`PeriodRegressionError`), because a clock moving backwards — skew, a
restored backup, a mis-set host — must not hand back a budget already spent.

**Integrity.** The ledger is `{body, digest}`; editing `consumed` downward
fails the digest and stops spending rather than buying more reads.

In the collector, every guard fault stops the run and writes an `absence`
record naming the reason — so the tape says *why* it went quiet.

---

## §5 — Deduplication and propagation identity

`app/social/dedupe.py`. Getting this wrong destroys any future lead-lag
measurement, and it does so invisibly: the tape simply contains fewer records,
all of them plausible.

Three identities:

```
delivery_identity   (platform, source_id, message_id, generation, sequence)
message_identity    (platform, source_id, message_id)
content_identity    sha256 of whitespace-normalised content_text
```

Five verdicts:

| verdict | condition | meaning |
|---|---|---|
| `NOVEL` | everything new | |
| `REDELIVERY` | same delivery identity | transport noise, same connection |
| `RESTREAM` | same message, new delivery | transport noise, e.g. after reconnect |
| `REVISION` | same message id, different content | the platform edited it — record both |
| `PROPAGATION` | new message, same content | **the world spreading. An EVENT.** |

A retweet is a new observation about the world: someone with a different
audience amplified something at a different instant. Discarding it as a
duplicate deletes exactly the diffusion curve a lead-lag study is trying to
measure. Transport duplicates are **written to the tape** as `redelivery`
records rather than dropped, because an unrecorded redelivery rate cannot later
be told apart from a delivery gap.

`content_identity` hashes the **extracted text**, not the raw frame. Hashing
the frame was the first implementation and it was wrong: a retweet's envelope
carries a different id, author, and reference block, so identical text hashed
differently and no propagation was ever detected. The test suite caught it, and
`test_content_identity_ignores_the_transport_envelope` now pins it.

Normalisation is deliberately conservative (whitespace collapse only).
Aggressive normalisation — lowercasing, stripping URLs and mentions — merges
genuinely different posts and manufactures propagation events that never
happened.

The ledger is bounded (default 200,000 keys) and its **eviction counters are
surfaced, not hidden**, so "propagation events fell" can be checked against
"the ledger forgot". Doctrine 7 positive control included.

---

## §6 — Source universe

`app/social/sources.py`. Configuration, never code. Design target: 100–300
named rules whose individual value is being measured. Explicitly not "ingest
Crypto Twitter" — an unnamed firehose has no denominator, so no rule's
contribution can be attributed, defended, or retired, and per-read pricing makes
it an unbounded bill.

Each `SourceRule` requires `rule_id` (stable, never reused), `platform`,
`kind`, `selector`, a **`rationale` you could defend when asked to retire it**,
and an **`active_from`** every longitudinal statistic must be conditioned on.
Rules are retired via `active_until`, never deleted, so older tape stays
interpretable.

Worked example set: `docs/social/source-universe.example.json` — exchange
listing accounts, launchpad accounts, ecosystem accounts, historically-early
callers (named individually so each can be retired on its own evidence),
contract-address patterns, listing/migration keywords, exploit/rug keywords.
Every selector is an `EXAMPLE_*` placeholder. **Copying that file does not
configure a universe.**

Rule reconciliation writes a `stream_event` to the tape, because a rule change
alters the denominator across the boundary. Rules the platform holds that our
universe does not name are **left alone**, never deleted: they may belong to
another user of the same credential, and deleting one would be a cross-tenant
mutation performed by a read-only collector.

---

## §7 — Entity/mint resolution

`app/social/resolution.py`. An interface (`EntityResolver`) plus a deliberately
dull default (`ConservativeAddressResolver`) and a `NullResolver`.

`ResolutionConfidence` is ordinal and coarse — `UNRESOLVED` / `CANDIDATE` /
`CONFIRMED` / `AMBIGUOUS` — not a float. A float invites thresholding,
thresholding invites tuning, and tuning a collector is how a tape stops being a
record of what happened.

The default extracts length-bounded base58 strings and reports `CANDIDATE`. Its
ceiling is `CANDIDATE`: `CONFIRMED` requires an authoritative registry, which
is a network call, which is a cost, which is a separate milestone. Two distinct
candidates yield `AMBIGUOUS`, never "the first one" — and the schema *refuses*
an `AMBIGUOUS` resolution carrying a `resolved_mint`.

Because raw bytes are preserved verbatim, **resolution can always be redone**
from the tape by a better resolver, and `with_entity_resolution()` returns a new
artifact rather than mutating one.

---

## §8 — Telegram / Discord: boundary only

`app/social/connectors.py` defines the interface and the authorization
boundary. **No implementation. No connection. No client.**

X's filtered stream is a public firehose under a commercial contract. A
Telegram group or Discord server is not: reading one means joining under a
community's rules and receiving messages from people who did not publish them
to the world. That is not a technical problem, so it is not solved by writing a
client. It is solved by recording, before a single byte is read, **who** granted
access, **what** they granted, **when**, and under **which mechanism**.

`AuthorizationGrant` requires all of that plus an `expires_at` (an unexpiring
grant is one nobody revisits) and a `retention_note` attached to the consent
that permitted it. Wildcard scopes are refused: they cannot be revoked
partially or audited at all. `GrantMechanism.TELEGRAM_USER_SESSION` is refused
even *with* a grant record, because operating a personal session reads as a
human and carries that human's obligations.

`NO_GRANTS` is empty. `assert_connector_authorized()` fails closed.

---

## §9 — What is deliberately NOT built

* **No signal, score, ranking, weighting, priority, or prediction.** Not a
  "virality" number, not an author reputation, not an influence metric. That is
  a later, separately preregistered milestone.
* **No live transport of any kind.** No HTTP client, no websocket client, no
  socket import anywhere in `app/social/` — asserted by an AST scan with its own
  positive control. `NullTransport` is the default and refuses everything.
* **No credential surface.** No `api_key`, `bearer_token`, `access_token`, or
  `client_secret` identifier exists in the package; asserted by test.
* **No Telegram or Discord implementation.**
* **No media retrieval.** References are recorded; `retrieved` is always False.
* **No clever resolver**, no registry lookup, no name→mint mapping.
* **No CLI command, no config entries, no systemd unit, no flag.** Adding a
  flag is part of the activation decision, not of building the tape.
* **No database tables and no migration.** The tape is files.
* **No reaction measurement.** `first_onchain_reaction` and
  `first_price_reaction` are structurally present and ABSENT; nothing here
  fills them.
* **`app/realtime/` untouched.** The Kalshi collector is frozen.
* **`AGENTS.md` untouched.** The coordinator owns it.

---

## §10 — The single most likely way this tape records something misleading

**A backfilled or restreamed item pooled with live ones, producing a delivery
distribution that is real in every record and wrong in aggregate.**

The mechanism: X's filtered stream re-delivers, and after a disconnect it can
replay a window. Each such item has a genuine `source_created_at` and a genuine
`our_received_at` — neither field is corrupt, neither record is wrong. But
`our_received_at` for a recovered item is *when we caught up*, not *when we
could have known*. Mixed into one distribution, a few percent of recovered items
put a long right tail on delivery latency, and the honest-looking conclusion —
"we learn about posts minutes late" — is an artefact of our own downtime.

It is the repo's recurring failure class exactly: **a plausible benign value
emitted by a broken path.** Nothing crashes, nothing alerts, and the dataset
looks clean.

Three defences are built, and none of them is sufficient alone:

1. `delivery_mode` is a required field with no silent default, and `UNKNOWN` is
   never read as `LIVE`.
2. A disconnect writes an `absence` record naming the gap and the new
   subscription generation, so the affected window is identifiable in the tape
   rather than inferred from missing rows.
3. `subscription_generation` and `delivery_sequence` are on every record, so a
   restream across a reconnect is distinguishable from a within-connection
   duplicate.

**What is still not defended:** nothing in this milestone *forces* a downstream
consumer to condition on `delivery_mode`. A future analysis that selects
`WHERE ...` without it will get a clean-looking, wrong answer. The
countermeasure belongs to the analysis milestone — every delivery-timing
statistic must report its `delivery_mode` breakdown beside its result, and
refuse to emit a pooled figure. Recording that here so the obligation is
inherited rather than rediscovered.

Two runners-up, recorded for the same reason:

* **`source_created_at`'s semantics are `UNVERIFIED`.** Doctrine 8 says a field
  name is not evidence of its semantics, and nobody has re-read X's `created_at`
  across a known interval to see what moves it. Until someone does, every
  statistic conditioned on it inherits `SourceTimeFidelity.UNVERIFIED`.
* **The propagation ledger is bounded**, so a long-running collector will
  eventually evict content keys and under-report propagation. The eviction
  counters are surfaced on every run report specifically so that fall is
  checkable rather than believable.

---

## §11 — Validation

| suite | tests |
|---|---|
| `tests/test_social_tape_001.py` | 72 |
| `tests/test_social_cost_guard_001.py` | 29 |
| `tests/test_social_x_collector_001.py` | 46 |
| **total** | **147** |

All fixture-driven. No network, no credentials, no live transport exists.
Fixture frames carry provenance (doctrine 9) and are honestly marked
`SYNTHETIC` — no wire capture exists, because nothing has been connected. A
later milestone that captures real frames replaces the basis field, and only
then do these tests certify venue truth rather than our own imagination.

**Doctrine 5 (reachability), stated honestly.** `TestSeam` drives the real tape
writer, the real cost guard and the real ledger and asserts state OUTSIDE the
collector — bytes on disk, a counter re-read from disk through a fresh guard,
ledger membership. But **nothing in `app/` imports `app.social`**: there is no
CLI command, no flag, and no scheduled caller, because building one is part of
the activation decision. `test_no_module_in_app_imports_app_social` states that
gap as an assertion, so if it ever fails, a production caller has appeared and
that requires an explicit decision.

Positive controls included, per doctrine 7: the AST guard can fail, the import
scan can fail, a forced reconnect moves `subscription_generation`, a forced
eviction moves the ledger counters, and every cost-guard refusal is paired with
a control proving the read is permitted when the condition is absent.
