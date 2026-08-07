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
