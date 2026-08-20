# KALSHI-DEMO-READONLY-VALIDATION-001 — 2026-08-07

**Verdict: `KALSHI DEMO READ-ONLY VALIDATION BLOCKED`**

Gate 2 halted. **No demo credential is installed on either host**, so Gates 5–15
— every gate that requires wire evidence — could not be attempted. Nothing in
this document claims a wire observation.

What *could* be closed without a key was closed: the Gate 3 typed-purpose
architecture, the Gate 4 signing-parity and negative-capability tests, and the
demo host constants.

---

## 1. Baseline (Gate 1)

Captured 2026-08-07 10:19:55 UTC / 03:19:55 PDT, before any Kalshi call.

| | |
|---|---|
| Mac / origin / EVO-X2 | `170a428` — all three equal, as reported |
| Alembic | 0027 |
| tracked changes | none, all three |
| MarketOps | runs 8303–8305 all `ok`, 38–42 s |
| watcher | `active`; tick cadence **2.50/s** (1500 ticks/600 s) |
| backup | `backup-20260807T014026Z.db.gz` |
| database | 4,550,623,232 bytes |
| disk | root 88 G free (62 % used), /mnt/data 710 G free |
| memory | 92 G total, 32 G available |
| telemetry | 5,347 lines |
| **`DEMO_LOCK_BASELINE`** | **6** |
| timers | 7 | 
| failed units | 0 |
| observer processes | **0** |

The observer-process count initially read 3. That was the audit's own SSH
command line matching its own `pgrep` pattern — the third time this
false positive has occurred in this workstream. A pattern that can match the
command containing it is not a measurement.

### Stop conditions for the (unrun) session

`current_lock_events > 6`, any exhausted lock retry, any non-`ok` MarketOps
cycle, MarketOps > 90 s, tick cadence materially below 2.50/s, archive
integrity failure, unrecoverable sequence integrity, or memory/disk pressure.
A new lock event stops the session and triggers attribution review — the
observer is not assumed to be the cause.

## 2. Demo credential confinement (Gate 2) — **HALT**

```
KALSHI_OBSERVER_API_KEY_ID        ABSENT from EVO .env
KALSHI_OBSERVER_CREDENTIAL_PATH   ABSENT from EVO .env
observer_credential_configured    False
```

No candidate credential file exists at any of the searched paths on either
host, and no `*kalshi*` file exists outside the repository. Nothing was
created — provisioning is a human action.

The confinement checks the milestone lists are implemented and tested
(`O_NOFOLLOW` open, `fstat` on the descriptor, absolute + fully-resolved path,
regular file, single link, owner, mode ≤ 0600, confined parent, no
group/world-writable non-sticky ancestor, outside the repository). They have
never been run against a real credential because none exists.

## 3. Independent scope verification (Gate 3) — architecture built, unrun

`app/realtime/credential_audit.py`. It is **not part of the collector**: a
separate entry point, one request, then exit.

```
AuthPurpose.WEBSOCKET_HANDSHAKE  -> ("GET", "/trade-api/ws/v2")
AuthPurpose.API_KEY_METADATA     -> ("GET", "/trade-api/v2/api_keys")
```

No caller supplies a method, a path, or a URL. The caller names an **intent**;
the route is looked up in a closed constant table. The set of signable requests
is therefore fixed at import time and is exactly as long as that enum, and
adding a member is a reviewable diff rather than a runtime argument.

Purposes are granted at construction and never widened. The continuous observer
is built with `WEBSOCKET_HANDSHAKE` only, so it **cannot reach a REST route at
all** — even though the same credential would be accepted there. A test asserts
that an observer signer raises when asked for metadata.

`READ_ONLY_PATH_ALLOWLIST` is now *derived* from the route table rather than
maintained beside it: two lists that must agree is one list too many.

`audit_scopes` takes a `fetch` callable, so no transport lives inside the
security boundary and every verdict path is testable without a key or a socket.
It halts on: any scope set other than exactly `["read"]`, missing scopes,
empty, duplicated, unknown, the key id absent from the response, the key id
appearing twice, malformed metadata, and request failure — the last because
"we could not check" must never read the same as "we checked and it was fine".
Failure messages repeat only the exception *type*; the original can carry the
signed URL.

Verdict string on failure: `HALT — DEMO OBSERVER CREDENTIAL IS NOT PROVEN
READ-ONLY`.

## 4. Signing parity (Gate 4) — verified locally

Both purposes verified against a generated key: RSA-PSS, MGF1-SHA256, SHA-256,
digest salt length, base64, over `timestamp_ms + "GET" + path`. Query strings
are stripped and no route in the table contains one. PSS randomisation
confirmed (two signatures differ, both verify).

Proven unreachable rather than merely rejected: `POST`, `PUT`, `PATCH`,
`DELETE`, `HEAD`; `/trade-api/v2/portfolio/{orders,balance,positions,fills}`;
and any arbitrary path. There is no parameter through which any of them could
arrive — `_signature`, `headers_for` and `websocket_headers` take neither a
method nor a path nor a URL.

A `str` subclass with a lying `__eq__` previously satisfied the allowlist
membership test and got `/trade-api/v2/portfolio/orders` signed. Typed purposes
remove the injection point rather than guarding it.

## 5. Demo hosts (Gate 5 / Gate 13) — corrected, unverified

```
WS   demo: wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2   (was demo-api.kalshi.co)
REST demo: https://external-api.demo.kalshi.co/trade-api/v2       (new)
```

Both taken from the milestone. **Neither has been reached.** The production
hosts are unchanged and were not contacted.

## 6–13. Wire evidence — **none**

Channel entitlement, the multi-market SID experiment, `use_yes_price`
semantics, schema parity, `get_snapshot` recovery, the bounded demo archive,
replay against captured data, REST reconciliation, latency and resource
measurement **all require the credential**. None was attempted.

The SID-global sequencing model deployed in
KALSHI-OBSERVER-PREAUTH-HARDENING-001 therefore **remains an inference from
`docs/LOW_LATENCY_ARCHITECTURE_001.md`, not a wire observation.** Gate 7 is the
experiment that confirms or refutes it, and it is still owed.

## 14. Out-of-scope defect found and fixed

`tests/test_meme_news.py::test_velocity_scoring_from_previous_snapshot` bound
`NOW` at module import while the service computes `boost_velocity` against the
real clock, so the elapsed term grew by however long the suite took to reach
that test. `rel=1e-2` allows about 72 seconds of that; a full-suite run exceeds
it. It failed three times across two branches under load and passed in
isolation every time, which is exactly how this presents. Anchored to the clock
at test time. Unrelated to Kalshi, but it was corrupting the suite gate this
milestone depends on.

## 14b. Live DEMO REST evidence — 2026-08-07 (credential-free)

Kalshi's demo market endpoints are public, so this much could be collected
without a key. One bounded read-only GET to
`https://external-api.demo.kalshi.co/trade-api/v2/markets` (HTTP 200, 413 ms).

### A real defect it found

```
price_ranges: [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]
```

`PriceGrid` expected `start_dollars` / `end_dollars` / `tick_dollars`. Those are
not names the venue sends, so **every real market raised a bare `KeyError` and
the price-grid guard had never once executed against live data** — the control
that decides whether a price is admissible at all. The `_dollars` suffix *is*
real on scalar price fields (`yes_bid_dollars`), which is presumably where the
guess came from; inside `price_ranges` it does not appear.

Fixed in `aa3baa1`: real names accepted, missing fields raise `FixedPointError`
rather than `KeyError`, and overlapping ranges are refused. The captured payload
is pinned as a fixture.

### Three assumptions it confirmed

| assumption | evidence |
|---|---|
| `PRICE_SCALE = 10_000` | every `*_dollars` field carries exactly 4 decimals (`"0.0000"`, `"1.0000"`) |
| `CONTRACT_SCALE = 100` | every `*_fp` field carries exactly 2 (`"0.00"`) |
| `price_level_structure` is a display label | value is `"linear_cent"`; the `0.0100` step in `price_ranges` is what actually constrains the grid |

REST field names that reconciliation depends on — `ticker`, `status`,
`yes_bid_dollars`, `yes_ask_dollars` — are the names the venue sends.

### What it did NOT settle

Nothing authenticated, and nothing about the WebSocket. `use_yes_price`, the SID
sequencing question, snapshot/delta wire shapes, entitlement and recovery all
need the socket. Also worth recording for later: the sampled demo markets are
entirely illiquid (all bids/asks/sizes `0.0000`/`0.00`), so demo may not produce
a meaningful order book even once connected — the SID experiment may need
markets chosen for activity rather than the first page of results.

## 14c. Host preparation completed on EVO-X2

- `<REMOTE_HOME>/.config/pa-secrets` created, mode `0700`, owner
  `<REMOTE_USER>`, outside the repository, not a symlink, fully resolved.
- `~/.config` tightened **0775 → 0750**. It was group-writable with no sticky
  bit, which the hardened loader refuses: a writable ancestor lets the
  credential directory be renamed out from under the permission check. Group
  `<REMOTE_USER>` has no other members and `~` is already `0750`, so nothing
  lost access. **Rollback: `chmod 775 ~/.config`.**
- `~/.config/pa-secrets/install-demo-credential.sh` (mode `0700`) written: it
  validates PEM confinement, parses the key to confirm RSA without displaying
  it, prompts for the key ID locally, backs up `.env` outside git, writes only
  the key ID and the PATH, and reports before/after hashes plus a key-name-only
  diff. It refuses to run when no PEM is present.

## 14d. Transport gap

`app/realtime/` still contains **no transport at all** — deliberately, and
asserted by tests. `websockets 16.0` and `httpx 0.28.1` are available on the
host. A bounded demo session runner therefore still has to be written before
Phases 6-16 can execute; it should live in its own module so the observer core
stays inert and the existing guard narrows rather than being deleted.

## 16. AUTHENTICATED DEMO SESSION — 2026-08-08

Credential installed and confined (mode 0600, owner `<REMOTE_USER>`, parent
0700, outside the repo, fully resolved). `DEMO_LOCK_BASELINE = 6`.

### A. Scope — PROVEN `["read"]`

`GET /trade-api/v2/api_keys` → HTTP 200, one call. The installed key appears
exactly once and reports exactly `["read"]`. Not taken on trust from how it was
labelled at creation.

Running this exposed a bootstrap cycle: `from_path` required proven scopes and
the audit is what proves them. `for_scope_audit` breaks it — it grants
`API_KEY_METADATA` only, so a bootstrap signer cannot open a socket, and records
an `UNVERIFIED` sentinel so it cannot be mistaken for an audited credential.

### B. Entitlement — YES

An exactly-`["read"]` key received `subscribed` acks for all four channels, each
on its own sid: `ticker` (1), `market_lifecycle_v2` (2), `trade` (3),
`orderbook_delta` (4). **No credential broadening was needed or attempted.**

### C. Sequencing — `SUBSCRIPTION_GLOBAL_SEQUENCE`. Deployed model CORRECT.

One sid carried both markets. seq ran 1..9 contiguously across the
*subscription*, while each market's own view had holes:

```
seq=1 snapshot M2 | seq=2 snapshot M1 | seq=3 delta M2 | seq=4 ERROR
seq=5..8 delta M1 | seq=9 snapshot M1
per-market: M2=[1,3]   M1=[2,5,6,7,8,9]     ← neither contiguous
```

**But a sharp edge the model missed: non-orderbook frames consume a sequence
number.** The `error` frame took seq 4. Ignoring it without advancing the
position made the next delta read as a gap, which would have unpublished every
book on the subscription within seconds of connecting.

### D. `use_yes_price=true` — NO levels arrive ALREADY YES-SCALED

The most serious defect found. Ground truth from a `ticker` frame:

```
ticker : yes_bid 0.4700 size 5.00 | yes_ask 0.5100 size 206.00
book   : yes_dollars_fp [["0.4700","5.00"]]
         no_dollars_fp  [["0.5100","5.00"]] + delta +201.00 -> 206.00
```

The NO price **is** the YES ask. The code complemented it to `0.4900` —
uncrossed, plausible, two cents wrong, on every ask. Retaining `raw_price_units`
beside `normalized_yes_price_units` is what made this a re-read of the evidence
rather than a re-collection.

### E. `get_snapshot` — requires `market_tickers`

The sids-only form returned `{"code":14,"msg":"Market Ticker required"}`, and
that error consumed a sequence slot. With tickers supplied, recovery returned
two snapshots cleanly on a fresh connection.

### Other wire findings

- An **empty book snapshot omits both ladder keys** (seq 9, after deltas emptied
  the book). The previous fail-closed guard rejected a valid snapshot.
- **Venue timestamps are not uniform:** `ts` is an ISO string on
  `orderbook_delta` and epoch *seconds* on `ticker`; `ts_ms` is unambiguous on
  both and is now read first.

### F. Replay — DETERMINISTIC

Network primitives blocked at import. Two replays produced identical digests
(`4358f98b…`), identical checksums, identical top-of-book, identical
subscription state. `external_calls=0`, `persisted=False`.

### Fresh-connection re-validation, post-fix

`records=4 faults=0`, subscription healthy, 0 gaps, both books publishable, and
`KXMLBHIT` reconstructed to **bid 0.4700/5.00, ask 0.5100/5.00** — matching
ticker truth exactly.

### REST reconciliation

| market | verdict |
|---|---|
| `KXMLBHIT-…` | **`agreement`** — book `0.4700/0.5100` == REST `0.4700/0.5100` |
| `KXQUICKSETTLE-…` | **`unknown`** — settled to `determined` between the WS capture (01:15:52Z) and the REST read (01:18:43Z) |

`price_ranges` parsed cleanly with the corrected `start/end/step` schema.

### G. EVO-X2 impact — NONE MEASURABLE

| | baseline | after |
|---|---|---|
| SQLite lock count | 6 | **6** |
| MarketOps | ok, 37–43 s | ok, 43–49 s |
| tick cadence | 2.25–2.50/s | 2.50/s |
| RAM available | 32 G | 32 G |
| disk free | 88 G | 88 G |

`receive→parse` p50 9.7 µs; dispatch p50 43.6 µs, max 143.4 µs. Sample sizes are
tiny — descriptive only, not a resource envelope.

## 17. What still blocks production provisioning

1. **The `unknown` REST discrepancy is unresolved.** Its cause is explainable —
   the market settled mid-window — but `reconcile_with_rest` cannot distinguish
   a lifecycle transition from corruption, and the milestone's own rule is that
   an unresolved `unknown` blocks production.
2. **The eight evidence-driven adversarial reviews have not been run** against
   this new wire evidence.
3. **The bounded capture was 4 records over ~2 minutes**, not the authorized
   ≤10 min / ≤5 markets. Demo liquidity is extremely thin, so a longer window
   and activity-selected markets are needed before the archive proves anything
   about sustained operation.

## 15. Production credential decision (Gate 17)

`MORE DEMO VALIDATION REQUIRED`

No production credential was provisioned, configured, inspected or used.

## 16. Remaining human action

1. Provision a **demo** key with scopes exactly `["read"]`. Production stays out
   of scope until demo passes.
2. PEM at a service-user-confined path, mode `0600`, **fully resolved** — the
   loader refuses `..` and symlinked ancestors by design.
3. Set `KALSHI_OBSERVER_API_KEY_ID` and **`KALSHI_OBSERVER_CREDENTIAL_PATH`**
   (not `..._PRIVATE_KEY_PATH`).
4. Then Gates 3 → 15 run in order, starting with the one-shot scope audit.
   Gate 7 (is `seq` per-SID?) is the load-bearing experiment: if it comes back
   per-market, the sequencing architecture is revised before anything else
   proceeds.

**KALSHI DEMO READ-ONLY VALIDATION BLOCKED**
