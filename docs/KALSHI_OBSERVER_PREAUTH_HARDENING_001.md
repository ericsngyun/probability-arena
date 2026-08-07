# KALSHI-OBSERVER-PREAUTH-HARDENING-001

Zero credential, zero provider call, zero network. Closes what could be closed
statically before a key exists.

---

## 1. Kalshi authentication-surface inventory (Gate 1)

Full repository sweep for key-id readers, key-path readers, PEM loaders, RSA
loaders, `.sign()` calls, PSS implementations, auth-header builders, and
authenticated REST/WS adapters.

**Before:**

```
app/config.py
  kalshi_api_key_id, kalshi_private_key_path      generic, any subsystem
  Settings.ws_enabled ──────────────────────────┐
                                                 │
app/main.py  lifespan ─→ WsSnapshotService ──────┤
                                                 ▼
app/services/ws_snapshots.py
  sign_ws_auth(private_key_path, key_id, ws_url)      ← arbitrary URL
    load_pem_private_key → private_key.sign(PSS/SHA256)
    KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP
  WsSnapshotService ─→ OrderbookSnapshot (SQLite write)

app/realtime/auth.py
  ReadOnlyRequestSigner.from_path → _load_key_material → load_pem_private_key
    → closure → PSS/SHA256 over timestamp+GET+/trade-api/ws/v2
```

**Two signers, different contracts.** The legacy one took any URL, loaded the
PEM on *every call*, and performed no ownership, mode, symlink or containment
check. `app/adapters/kalshi.py` is unauthenticated and reads no credential.

**After:** one path, and the generic credential no longer exists.

```
app/config.py
  kalshi_observer_api_key_id
  kalshi_observer_credential_path        ← read by app/realtime/auth.py only
        │
        ▼
app/realtime/auth.py  ReadOnlyRequestSigner.from_path
  verify_scopes(["read"]) → O_NOFOLLOW open → fstat checks → PEM → closure
  → PSS/SHA256 over  timestamp_ms + "GET" + "/trade-api/ws/v2"
```

## 2. Credential-isolation architecture (Gate 2)

`KALSHI_OBSERVER_API_KEY_ID` and `KALSHI_OBSERVER_CREDENTIAL_PATH`.

**The path field deviates from the milestone's suggested
`KALSHI_OBSERVER_PRIVATE_KEY_PATH`, deliberately.** That name puts the fragment
`private_key` into `config.py` and so needs a safety-audit allowlist entry — for
a field that holds a *path* and never key material. Avoiding the name keeps
"exactly one private-key surface in the repository" literally true with **zero**
allowlist exemptions outside `auth.py`, which is the stronger form of the same
guarantee. **Set `KALSHI_OBSERVER_CREDENTIAL_PATH`, not `..._PRIVATE_KEY_PATH`.**

Proven statically (tests 11–15): only `config.py`, `main.py` and
`realtime/auth.py` name these fields; the adapter, scanner, watcher and research
paths do not. The old generic fields are **gone**, so setting them configures
nothing — `extra="ignore"` means a stale `.env` is inert rather than an error.
Half a credential is not a credential.

The config holds a key id and a path. PEM contents must never enter the
environment: env vars are readable from `/proc`, surface in `docker inspect`,
and persist in shell history. A test asserts no settings field is named for a
key or a PEM.

## 3. Legacy-signer disposition (Gate 3) — **deleted**

Option 1, on evidence:

| check | result |
|---|---|
| systemd units starting the FastAPI app | **none** — all 8 units are CLI commands |
| `uvicorn` processes on EVO for this repo | none (the three running belong to other projects) |
| `orderbook_snapshots` rows | **0**, ever |
| readers of `OrderbookSnapshot` | none outside the deleted writer and `test_models.py` |

Deleted: `app/services/ws_snapshots.py`, the `main.py` lifespan hook, the
generic credential fields, `Settings.ws_enabled`, and both allowlist entries.
The empty `orderbook_snapshots` table is left in place — dropping it needs a
migration, and this milestone adds none.

The observer signer is unchanged: `GET` only, `/trade-api/ws/v2` only,
read-scoped credential only, no general-purpose signing API.

## 4. Subscription-sequence correction (Gate 4)

The defect four reviews converged on. `seq` counts messages across a whole
**subscription**, and one subscription carries many tickers — so a per-market
view of it has a hole at every sibling message.

Measured on the old model, two markets, one sid, interleaved:

```
applied=0  rejected=4   publishable A=False B=False
  KXA: sequence gap: expected 2, got 3
  KXB: sequence gap: expected 3, got 4
```

Invisible at one market, total at two. Now:

```
SubscriptionState(sid, generation, last_seq, subscribed_market_tickers, healthy)
        │  1 validate sid   2 validate generation   3 validate seq
        ▼
SubscriptionRouter  ── 4 validate type  5 route by market_ticker ──▶ OrderBook[ticker]
        │                                                              raw YES / raw NO
        └─ 7 publish only if subscription healthy AND book publishable  normalized views
```

A book no longer compares sequence numbers at all. `replay()` groups by sid for
the same reason. Both are pinned by a test that asserts the *old* model rejects
all four interleaved deltas and the new one accepts them, so the discrimination
is visible in the file rather than inferred.

## 5. Gap recovery (Gate 5)

A sid-level failure unpublishes **every** book on that subscription. Nothing in
the hole says which market the lost message belonged to, so repairing only the
market named in the next message would leave the others silently wrong.

Two paths: `build_get_snapshot` (`update_subscription` /
`action: get_snapshot`) and `build_resubscribe` (unsubscribe + subscribe, using
only command shapes we already send). A resubscription **supersedes the
generation**, which is what makes a straggler from the old stream identifiable —
its sequence numbers are from a different namespace and would otherwise read as
an ordinary gap. Mid-recovery, nothing publishes, and re-snapshotting one market
does not republish its siblings.

`get_snapshot` is an unconfirmed wire fact; `build_resubscribe` is the fallback
that does not depend on it.

## 6. YES-price semantics (Gate 6)

`use_yes_price: true` is structurally present on every subscribe and
resubscribe. Every ladder level carries all four canonical fields —
`venue_side`, `raw_price_string`, `raw_price_units`,
`normalized_yes_price_units` — plus `use_yes_price_requested` and
`no_side_normalization` on the ladder itself.

**The flag's wire effect remains unverified.** The class docstring previously
asserted that both ladders arrive YES-scaled while the arithmetic complemented
the NO ladder — the un-flagged convention. That contradiction is now stated
plainly rather than asserted away. Keeping the raw pair means that if the
convention differs, archived levels can be **reinterpreted** rather than
re-collected. The crossed-book check refuses the symptom meanwhile.

## 7. Current wire schema (Gate 7)

Fixtures carry exactly the documented fields — snapshot: `sid`, `seq`,
`market_ticker`, `market_id`, `yes_dollars_fp`, `no_dollars_fp`; delta: `sid`,
`seq`, `market_ticker`, `market_id`, `price_dollars`, `delta_fp`, `side`, and
`ts_ms` when present. No invented fields. `ts_ms` is optional and its absence
does not change acceptance.

## 8. Channel contract (Gate 8)

`orderbook_delta`, `ticker`, `trade`, `market_lifecycle_v2`. **`ticker`, not
`ticker_v2`** — asserted by an AST scan over every string literal in the
package, and `build_subscribe("ticker_v2")` raises. The lifecycle channel is
versioned and the ticker channel is not; assuming the suffix generalises would
subscribe to a channel that does not exist, which fails as *silence*.

`LIFECYCLE_GUARANTEES_EXPLICIT_OPEN_CLOSE = False`: market open/close timing is
not promised as an explicit message, so absence of a close event is not evidence
that a market is open.

## 9. Demo-validation checklist (Gate 10) — **do not run yet**

Run in order; stop at the first failure. `KALSHI_OBSERVER_CREDENTIAL_PATH`, demo
key, ≤10 min, ≤5 markets, at least **two** markets on one subscription.

| # | Question | Pass criterion | If it fails |
|---|---|---|---|
| 1 | Handshake with scopes exactly `["read"]` | 101 Switching Protocols | scope/signing/host wrong — check `WS_HOSTS[demo]` first |
| 2 | Subscribe to `orderbook_delta` | `subscribed` frame with a `sid` | inspect the `error` frame; `params` shape is unconfirmed |
| 3 | Does one sid multiplex many tickers? | one `sid` for N tickers | if per-ticker sids, `SubscriptionState` becomes per (sid) still — harmless, but note it |
| 4 | **Is `seq` monotonic across the sid, not per market?** | interleaved A/B/A increments by 1 across the sid | **if per-market, revert §4** — this is the load-bearing assumption |
| 5 | Does `use_yes_price=true` change the NO wire price? | compare a NO level with the flag on vs off | if NO arrives YES-scaled, delete the complement in `book.py`; archived `raw_price_units` makes this a re-read, not a re-collect |
| 6 | Snapshot/delta field names identical to spec? | fixtures parse a live frame unchanged | correct fixtures **and** `_parse_levels` keys together |
| 7 | Does `get_snapshot` behave as documented? | a fresh `orderbook_snapshot` on the same sid | fall back to `build_resubscribe` and supersede the generation |
| 8 | ticker/trade/lifecycle schemas current? | frames parse; note whether any carries a venue `ts` | if orderbook frames carry no `ts`, the venue latency hop stays `n=0` — say so rather than reporting it |
| 9 | Replay reproduces the final book exactly? | archive checksum == live checksum, both markets | a mismatch is a data-path bug, not a venue question |
| 10 | REST reconciliation agrees? | `classification: agreement`, or a timing gap you can explain | `identity_mismatch` means the payload is for another market |

Also capture, because 001B needs them: per-market delta rate (decides whether
the archive is a 2 GB/day or 22 GB/day problem), disconnect frequency, and
whether snapshot frames carry a venue timestamp.

## 10. Deployment

Inert. No credential, no connection, no service, no timer, no provider call, no
MarketOps hook, no migration (Alembic 0027).

## 11. Remaining human action

1. Provision demo and production keys, scopes **exactly** `["read"]`.
2. PEM at a service-user-confined path, mode `0600`, **fully resolved** — the
   loader refuses `..` and symlinked ancestors.
3. Set `KALSHI_OBSERVER_API_KEY_ID` and **`KALSHI_OBSERVER_CREDENTIAL_PATH`**.
   The old `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` are dead and can be
   removed from `.env`; leaving them configures nothing.
4. Then run §9 against demo.

**KALSHI PRE-AUTH HARDENING COMPLETE — DEMO CREDENTIAL REQUIRED**
