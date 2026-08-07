# KALSHI-READONLY-AUTH-AND-VALIDATION-001

**Verdict: `KALSHI OBSERVER IMPLEMENTED — AUTHENTICATED VALIDATION INCOMPLETE`**

Gate 2 halted: no credential exists in either environment. Gates 4–9 ran in
full. The eight adversarial reviews found substantially more than expected, and
what they found changes the standing of the 001A code that was merged before
them.

---

## 1. Baseline (Gate 1)

Verified 2026-08-07 03:31 UTC / 2026-08-06 20:31 PDT.

| | |
|---|---|
| Mac / origin / EVO-X2 | `221549d` — all three equal, as reported |
| Alembic | 0027 |
| tracked changes | none, all three |
| observer runtime | no process, 0 services, 7 timers (unchanged) |
| MarketOps | runs 8234–8236 all `ok`, 40–50 s |
| backup | `backup-20260807T014026Z` — fresh |
| lock events | 5 (unchanged) |
| disk | root 88 G free, /mnt/data 710 G free |

## 2. Credential inventory (Gate 2) — **HALT**

EVO's `.env` declares `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`; **both
are empty**. No candidate credential path exists on either host. Nothing was
created — provisioning is a human action.

Gates 3, 10, 11, 12, 13 and 14 are unreachable and were not attempted. No demo
session, no production session, no live REST reconciliation, no resource
measurement under load. **Nothing in this document claims live evidence.**

### A correction to the 001A record

001A stated that private-key handling had no implementation surface in the
repository. The doc said that; the code did not. `app/services/ws_snapshots.py`
has carried a working RSA-PSS Kalshi signer since long before this work,
allowlisted in the canonical safety audit, dormant only because those two env
vars are empty — and it is *broader* than the new one: it accepts an arbitrary
URL and performs no file-confinement checks at all. The boundary described as
untouched had already been crossed.

## 3. Safety-boundary amendment (Gate 5)

`docs/SAFETY_BOUNDARIES.md` now separates **custody** keys from **request
authentication** keys, which had been sharing one row. RSA loading is permitted
solely for read-scoped Kalshi market-data requests under `OBSERVE_ONLY`, in one
named file, with an explicit list of what would require amending it again.
Wallets, custody, transaction/order/blockchain signing, order lifecycle, key
management, write-scoped credentials and general-purpose signing APIs remain
forbidden with no implementation surface.

Three rows of the first draft overstated what the code enforced. They were
corrected rather than defended — in particular the containment row now says it
bounds *accidental* egress, since any in-process code can still call the signing
closure and Python offers no defence against that.

## 4. Signing implementation (Gate 4) and private-key confinement (Gate 6)

`timestamp_ms + "GET" + "/trade-api/ws/v2"`, RSA-PSS/SHA-256/MGF1(SHA-256),
digest salt length, base64. No `sign(method, path)`; `websocket_headers` takes
neither a method nor a path.

Loaded once at startup from a confined file. No `from_pem_bytes`, no `from_env`
— both would route key material through a shell, a process listing or
`docker inspect`.

## 5. Eight adversarial reviews (Gate 9)

All eight ran. **All eight requested changes.** Findings addressed in
`16f4cc9` (credential surface) and `dda8936` (data path); each is now an
executable regression test that reproduces the original attack, rather than an
assertion about the shape of the fix.

### Fixed — credential surface

| finding | what it was |
|---|---|
| audit allowlist | **Repo-wide.** The audit asked whether *any* allowlisted fragment appeared anywhere in an identifier, exempting the whole **file**. In a file allowlisted for `private_key`, `wallet_private_key`, `private_key_place_order` and `sign_transaction_with_private_key` all passed. Predates this branch; applied equally to `ws_snapshots.py` and `config.py` |
| symlinked parent | `lstat` inspects only the final component, so the anti-symlink, parent-mode and repo-containment checks all looked at a different directory than the one read |
| `..` segment | made an in-repository key look external to `relative_to` |
| `_key` attribute | `signer._key.sign(arbitrary_bytes)` bypassed the method and path locks in one attribute access. The key now lives in a closure; there is no attribute to reach |
| `str` subclass path | a subclass with a lying `__eq__` signed `/trade-api/v2/portfolio/orders` in the review. Exact-`str` check now |
| public constructor | required no verified facts, so it *was* the `from_env` the boundary disclaims, and skipped the key-size and environment checks |
| `verify_scopes` unwired | it had **zero production callers**. `from_path` now requires `reported_scopes` with no default and fails closed through it |
| scope spoofing | a `list` subclass lying in `__iter__`, and an object whose `__str__` returns `"read"`, both passed |
| owner check | opt-in and nobody opted in, while the doc listed it as unconditional. Defaults to this process's uid |
| TOCTOU, hardlinks, loose ancestors | closed together by `O_NOFOLLOW` + `fstat` on the descriptor |

### Fixed — data path

Fail-closed held for three faults (gap, regression, negative level) and nothing
else. Now: every rejection unpublishes; a snapshot may not rewind the book (at
least-once redelivery on reconnect silently discarded applied deltas and
reported a clean result); a missing `seq` is a fault, not permission; a book
refuses another market's data and a superseded subscription's deltas; duplicate
levels raise; a crossed book is refused. `checksum()` is gated like every other
derived view and now covers generation/seq/sid.

Archive: an interrupted write lost the **entire hour**, not the last record, and
`verify()` called the empty result `intact`. Digests are verified on read, not
in an opt-in `verify()` nobody called. A copied demo file could be read as
production evidence. Records sort by instant, not timestamp text. One malformed
record no longer aborts the whole replay.

Latency: `int(p*n)` made `p99` identical to `max` for every n ≤ 100 and
overstated the tail ~27%. The venue hop is renamed `offset_contaminated`; the
permanently-empty book hop is deleted; negatives are counted; and the report
states that observation gaps and clock offset are **not measured**.

### NOT fixed — blocking, and honestly so

**Venue semantics cannot be settled without the demo socket.** Four reviews
independently flagged assumptions that no amount of local reasoning resolves:

1. **`seq` is per-subscription, not per-market.** `docs/LOW_LATENCY_ARCHITECTURE_001.md:138`
   says so, and `build_subscribe` multiplexes many tickers onto one `sid`. With
   two tickers, every book halts on its second message. The `sid` guard added
   here refuses the wrong thing loudly rather than absorbing it, but the real
   fix is a per-`sid` sequence tracker that all books on that subscription
   share — a structural change that should be made against observed frames, not
   guessed.
2. **`use_yes_price` is asserted, never read, and self-contradictory.** The
   docstring says both ladders arrive on the YES scale; the arithmetic
   complements the NO ladder, which is the un-flagged convention. If the flag
   does what the docstring says, every ask is double-converted. The crossed-book
   check now catches the symptom; the cause needs one demo frame.
3. **Snapshot/delta field names are unverified** (`yes_dollars_fp`,
   `no_dollars_fp`, `delta_fp`, `price_dollars`).
4. **`update_subscription` / `request_snapshot`** is documented as "official"
   without evidence, and the production WS host and the `ticker` channel version
   are unconfirmed.

**Operational resilience does not exist yet** — no reconnect, no backoff, no
retention, no staleness bound, no collector epoch, no streaming replay. That is
001B's scope, and the review produced a concrete spec list for it. Two numbers
worth carrying forward: the archive costs **3.5×** the disk it needs (one gzip
member per record), and `read_all()` materialises everything — a day's archive
at 500 markets is ~17 GB resident on a host that also runs a 65 GB model.

## 6–8. Demo, production, REST validation

**Not run.** All three require the credential. `reconcile_with_rest` now checks
market identity and status and returns a Gate-11 classification, but its
verdict is `unknown` by default rather than guessing `timing_difference` —
this function can see a difference and not its cause.

## 9. Resource and MarketOps isolation

Verified unchanged throughout: MarketOps `ok`, lock events 5, timers 7, Alembic
0027, no observer process. Nothing in `app/realtime/` imports SQLite or the ORM,
and no timer, service, daemon or MarketOps hook was added. The guard tests were
non-recursive (`glob` not `rglob`) and would not have caught a 001B collector in
a subpackage; they now recurse.

## 10. Human action remaining

1. Provision a **demo** key and a **production** key, scopes exactly `["read"]`.
2. PEM at a service-user-confined path, mode `0600`, **fully resolved** — the
   loader refuses `..` and symlinked ancestors by design. Supply the path.
3. Fill the record table in `docs/KALSHI_REALTIME_OBSERVATION_001A.md` §9.
   Never the key.
4. Decide whether the venue-semantic questions above are settled by a
   short authenticated demo session (they can be) before or after 001B.

## 11. Activation decision (Gate 15)

`MORE DEMO VALIDATION REQUIRED`

Not because demo validation was attempted and fell short — it could not be
attempted. The reviews independently established that four venue semantics are
unverified and that one of them (`seq` namespace) makes the collector
non-functional past a single market. Those are demo questions, and no local
work substitutes for them.

**KALSHI OBSERVER IMPLEMENTED — AUTHENTICATED VALIDATION INCOMPLETE**
