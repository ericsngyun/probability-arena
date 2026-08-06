# KALSHI-REALTIME-OBSERVATION-001A — observe-only real-time foundation

**Status:** implemented, suite green, safety audit clean. **Live authenticated
validation pending the human-installed read-only key.**

---

## 1. Credential-scope verification

`verify_scopes` enforces `scopes == ["read"]` and halts with
`HALT — OBSERVE-ONLY CREDENTIAL REQUIREMENT NOT SATISFIED` on write scope, both
scopes, an unrecognized scope, or an **absent** scopes field. Omission is
treated as failure, not a default, because Kalshi keys default to broader access
when scopes are omitted — silence must not read as permission.

No key is created, rotated or revoked here. Provisioning is a human action.

### Defence in depth, independent of the key

| control | enforcement |
|---|---|
| capability mode | `OBSERVE_ONLY`; `require_mode` fails closed and is not ordinal |
| HTTP methods | `ALLOWED_HTTP_METHODS == ("GET",)`; nothing else exists |
| channels | `orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2` only |
| private user streams | `fill`, `market_positions`, `user_orders`, `communications`, `order_group_updates` rejected by name |
| routes | no order/portfolio/cancel/amend/api-key literal exists in the package |

Static tests parse the AST and check **operative** string literals, excluding
docstrings — a raw substring scan matches the module's own prose describing what
it refuses to do, which has bitten three checks in this repository already.

### The private key: removed, not excluded

A concrete PEM-backed signer was written, and the canonical safety audit
correctly flagged `load_pem_private_key` as private-key handling —
`SAFETY_BOUNDARIES.md` records that as having no implementation surface
(ADR-002). The human decision authorizes a read-scoped key, so that boundary
will move, but it should move in **its own reviewed step at the moment a key
exists**, not as a side effect of a collector milestone.

Signing is therefore a seam. `canonical_signing_string` implements
`timestamp_ms + METHOD + path` (query stripped) and is fully tested without key
material. `RequestSigner.headers` raises until a key-bearing implementation is
installed. The observer is *structurally incapable* of touching a private key,
which is a stronger statement than a promise not to, and a test asserts it.

## 2. Fixed-point contract

`PRICE_SCALE = 10_000` · `CONTRACT_SCALE = 100` · `NOTIONAL_SCALE = 1_000_000`.

Rejected: exponent notation, >4 price decimals, >2 count decimals, NaN/Infinity,
negative snapshot quantities, off-grid prices, out-of-bound magnitudes,
locale formatting. Negative `delta_fp` is valid — that is how a level
decrements. `json` is parsed with `parse_float=Decimal`.

**A `float` input is refused outright rather than converted.** By the time a
float exists the precision loss has already happened, and converting it launders
the error into something that looks deliberate.

`price_ranges` is authoritative for the grid. `price_level_structure` is
retained for reporting and never keys arithmetic — a display label is not a
contract, and a market quietly moving to a sub-cent grid would otherwise start
silently rejecting valid prices.

## 3. YES-scale normalization

`use_yes_price: true` is set on every subscription and never inherited from the
server default: a default migration would reinterpret every NO level without
changing a line of our code.

```
yes-side level  ->  resting BID for YES at p
no-side level   ->  resting economic OFFER of YES at p
```

`best_yes_ask` is **derived** from the NO ladder, not assumed to exist on the
YES one. Raw side and raw price survive into the envelope: normalization adds an
interpretation, it never replaces the venue's own words.

Complement is exact on the integer grid (`yes + no == 1.0000`), and a test
asserts that an exact complement is **not** therefore a valid order price — the
grid decides that separately.

## 4. Sequence and book integrity

Snapshot required before any delta; pre-snapshot deltas are **rejected, not
buffered** (buffering means guessing how far back the snapshot reaches, and a
wrong guess double-applies). Duplicate ignored, gap and regression halt and
unpublish, a delta driving a level negative halts. A non-publishable book raises
from its derived views rather than returning a stale number. Resync bumps a
generation so a consumer knows its view was discontinuous, not merely old.

## 5. Archive, replay, latency

Append-only gzip-JSONL, `env=/venue=/date=/hour=`, per-record digests,
tail-tolerant reads (an interrupted write loses at most the last record).
**Demo events cannot be written into a production archive** — replaying demo as
production evidence would be a fabricated observation.

Replay is pure and deterministic and is proven to reproduce a live book's
checksum exactly. Latency is decomposed into hops with p50/p95/p99 — never one
number. Nothing here touches SQLite.

CLI: `kalshi-realtime-replay --archive <path> --environment demo|production`.

## 6. Demo validation

**Not yet run** — steps 3–4 of the demo-first sequence require the demo key.
Steps 1–2 (fixture replay, synthetic snapshot/delta) are complete and green.

## 7. Production read-only validation

**Not yet run.** Requires the human-installed read-scoped key. Scope
verification will run first and halt on anything but `["read"]`.

## 8. REST reconciliation

`reconcile_with_rest` compares reconstructed best YES bid/ask against a REST
market snapshot and returns `action: resynchronise` on any discrepancy. REST is
an independent check, never a fallback — taking whichever source looks better
would paper over exactly the gaps the collector exists to detect. Tested
synthetically; live comparison awaits the key.

## 9. Manual action record — human to complete

Recorded here after installation. **Never the private key.**

| field | demo | production |
|---|---|---|
| credential environment | _pending_ | _pending_ |
| key ID fingerprint | _pending_ | _pending_ |
| reported scopes | _pending_ | _pending_ |
| credential file path | _pending_ | _pending_ |
| file owner | _pending_ | _pending_ |
| file mode | _pending_ | _pending_ |
| verification timestamp | _pending_ | _pending_ |

Store the PEM at a service-user-confined path with mode `0600`; supply the
**path** to the process. `describe_credential` refuses any file readable by
group or other and reads metadata only — it never opens the contents. Future
systemd deployment should use `LoadCredential=`. **No service is installed by
this milestone.**

## 10. Scope

No orders, no fills, no execution credentials, no MarketOps hook, no timer, no
service, no provider call, no SQLite write, no migration (Alembic 0027).
