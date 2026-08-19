# KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001

**Status: AUTHORIZED, NOT STARTED.** Scheduled **after** the DEMO qualification
(CP6–CP9), the generation-aware `publishable_books()` fix, and the tape schema
freeze — and **BEFORE** `MARKET-MICROSTRUCTURE-EDGE-001`.

**Read-only production tape. Same collector. No orders, no portfolio channels,
no venue writes, no capital.**

---

## Why this exists

DEMO is increasingly looking like a **functional sandbox, not a realistic
microstructure environment**. `KALSHI-TAPE-MANIFEST-001` measured its activity
distribution as **four hyperactive markets → a 98.3× cliff → a broad quasi-flat
plateau**, with 30.9% of eligible markets inside a single 15–17 c/min band.
That is not a market distribution; it is what simulated flow looks like.

**Without this milestone, `MARKET-MICROSTRUCTURE-EDGE-001` risks building a
sophisticated alpha study over sandbox-generated flow.** Every feature family it
proposes — OFI, queue imbalance, microprice, cancellation intensity, liquidity
regimes, adverse selection — is a claim about *how real order flow behaves*. A
model fitted to synthetic uniform flow would be internally consistent, well
validated, and about nothing.

## What DEMO qualification can and cannot establish

A successful CP6–CP9 run on DEMO **may** establish:

- transport stability
- sequence correctness
- reconnect / generation behaviour
- raw-frame conservation
- replay equality
- archive correctness
- bounded collector lag **under DEMO load**
- segment rotation / close behaviour
- metrics correctness

It **may NOT** establish:

- production message-rate capacity
- production latency tails
- production liquidity regimes
- representative order-flow statistics
- expected production microstructure
- production sizing / capacity

**Those are what this milestone is for.** CP9 must state the distinction on its
face rather than leaving a reader to infer it.

## Scope

Same collector, unchanged, pointed at production market-data channels:

- read-only; market-data channels only, **no private/portfolio channel**
- a frozen manifest with the same reproducibility requirements: stratification
  snapshot timestamp, ranking statistic **with its limitations stated**, full
  candidate population
- the production universe stratified on **empirically measured** regimes, not on
  the regime structure DEMO happened to show
- **field semantics verified before use** (doctrine 8) — production may differ
  from DEMO in exactly the way `updated_time` differed from its name
- the same four-way verdict as CP9: qualified / conditionally qualified /
  underpowered / failed

## Prerequisites

1. CP6–CP9 complete on DEMO (functional qualification)
2. `KALSHI-REPLAY-GENERATION-CONSISTENCY-001` merged
3. Tape schema frozen and reviewed as a **measurement contract**
4. A production read-scoped credential whose scopes are verified against the
   live key-metadata route, and a **confirmed** production WS host —
   `kalshi.py:52-55` currently records it as **UNVERIFIED**

## The comparison that matters

Report explicitly whether **the DEMO rate distribution predicted the production
one**. CP10 was already written to check this; the manifest finding makes it the
central question rather than a footnote. If DEMO does not predict production,
say so plainly — it bears on every rotation constant tuned against DEMO, and on
whether DEMO is useful for anything beyond functional correctness.

---

## PRE-CAPTURE STATUS — 2026-08-17 (`KALSHI-PROD-QUAL-PRECAPTURE`)

**CAPTURE IS BLOCKED, and nothing here attempted one.** No production
connection, no capture, no credential read. The blocker is operational and
belongs to an operator: `KALSHI_PRIVATE_KEY_PATH=` is set-but-empty on EVO and
the only key present is the DEMO observer credential, so the authenticated
production handshake cannot be attempted at all.

What now exists, so that capture is a short, well-guarded step when the
credential arrives:

| | artifact | state |
|---|---|---|
| structural order-API guard | `scripts/kalshi_prod_observation_guard.py` | CLEAN on `main`'s closure; red state demonstrated 13 ways |
| one archive root per session (§11 B4) | `app/realtime/session_root.py` | refusal proven; `RECORD_SCHEMA_VERSION` untouched |
| the pre-capture gate | `scripts/kalshi_prod_precapture_preflight.py` | guard → endpoint → session root, in that order |
| the endpoint disagreement | `docs/KALSHI_PRODUCTION_ENDPOINT_001.md` | **RESOLVED** — the AsyncAPI spec names `external-api-ws.kalshi.com` and the collector already dials it; `settings.kalshi_ws_url` has no reader, so the `.env` value cannot mis-route a capture |
| tests | `tests/test_kalshi_prod_qual_precapture_001.py` | **46 passed** (measured on `main`) |

**Run this before capture:**

```bash
python scripts/kalshi_prod_precapture_preflight.py --archive-root <ROOT>
```

It exits non-zero unless the order-API guard is clean and the archive root is
free for this session. It opens no socket and reads no credential; the capture
command is a separate, separately-authorized step.

**Prerequisite 4 is still open, and is still the only thing blocking.** The
production WS host remains **UNVERIFIED**: the official Kalshi AsyncAPI spec
names `wss://external-api-ws.kalshi.com/trade-api/ws/v2` and the collector
already uses it, but documentation is not a handshake. §11 B1 closes on the
first successful production connection and not before — the preflight reports
`verified_on_the_wire: false` for exactly that reason.

**§11 B4 is closed by run rule, not by schema.** The contract's text is
unchanged and its characterization test still pins the record schema; what
changed is that the run rule is now enforced by a typed refusal instead of
remembered. A P4 run that wants a single multi-session archive still needs a
`RECORD_SCHEMA_VERSION` bump, and that decision is still outside this
milestone's authority.

---

## CAPTURE ATTEMPT 1 — 2026-08-19 (`KALSHI-PROD-QUAL-CAPTURE`)

```
Production semantics     STOPPED-ON-FINDING
Capture integrity        NOT ESTABLISHED — no capture was attempted
Observed production load NOT MEASURED — the number DEMO could not give is still not given
Replay equality          NOT QUALIFIED — B3
```

**No socket was opened to the production WebSocket host. No frame was received,
archived or labelled `production`. No session root was claimed.** The run
stopped at the credential gate, one link before the handshake.

### The finding

**The production observer credential installed on EVO is not read-scoped. The
venue reports its scopes as `["read", "write"]`.**

This is not an inference from a file, a name or a provisioning note. It is the
venue's own answer on the live key-metadata route, and the audit halted on it:

| | measured |
|---|---|
| route | `GET /trade-api/v2/api_keys` |
| host that answered | **`api.elections.kalshi.com`** — the production REST host |
| HTTP status | **200** |
| the installed key id in that account's `api_keys` | **found, exactly once** |
| its reported scopes | **`["read", "write"]`** |
| verdict | `CredentialError` HALT — *"the key reports write scope. The observer must never hold an order-capable credential."* |

Read the top two rows and the bottom row together, because they are two
different findings wearing one halt:

1. **The credential IS a production credential.** A 200 from the production
   identity store on an RSA-PSS signature it verified, with the key id present
   in that account, is exactly the evidence prerequisite 4 asks for on the
   identity question. A demo key does not authenticate here. **This is the
   strongest production evidence this milestone has ever held**, and it arrived
   in the same response that stopped the run.
2. **The credential carries `write`, and the boundary refuses it.** The
   observer must never hold such a key — that is the boundary, not a
   preference, and it is the reason `audit_scopes` exists as a separate
   one-shot entry point the collector runtime can never reach.

   **Doctrine 8 applies to `write` too, and is stated rather than assumed.**
   The measured fact is the scope *string* the venue returned. What `write`
   actually enables at Kalshi was **not** verified, will **not** be verified,
   and must not be: the only experiment that would establish it is addressing
   a write route, which is the thing forbidden. So the refusal deliberately
   does not depend on knowing — `verify_scopes` refuses any key carrying
   `write` whatever it turns out to permit. That is the correct direction of
   the unknown: assuming `write` is harmless could authorize an order-capable
   session, whereas assuming it is not cannot hide anything.

**Prerequisite 4 is therefore still open, and for a new reason.** It was
written as *"a production read-scoped credential whose scopes are verified
against the live key-metadata route"*. The scopes were verified against the
live route, exactly as instructed, and they came back write-capable. The
prerequisite has moved from *unverified* to **verified and failing**, which is
a strictly better place to be: the check worked.

### Why this was not worked around

The halt is one boolean in one pure function (`verify_scopes`,
`kalshi.py:221-225`) and it would have been trivial to relax. Every reason not
to survives inspection:

* **The structural guard does not make a write key safe.** It proves our
  closure cannot address an order route — it says nothing about what the key
  can do in any other process, on this host or another. Defence in depth is
  only depth if the second layer is allowed to refuse.
* **A read-only tape captured with an order-capable key is still a boundary
  breach**, and the tape would carry no record of it. The evidence would look
  identical to a compliant run forever after.
* **`OBSERVE_ONLY` is the only implemented capability mode.** A write-scoped
  credential is not in it.

### Secondary finding — a production halt is labelled DEMO

`credential_audit.HALT_NOT_PROVEN` is the constant string
`"HALT — DEMO OBSERVER CREDENTIAL IS NOT PROVEN READ-ONLY"`
(`app/realtime/credential_audit.py:30`). It is hard-coded, environment-blind,
and left over from `KALSHI-DEMO-READONLY-VALIDATION-001`. So the *production*
credential failure above is reported to an operator as a **DEMO** credential
failure.

Nobody is misled today because this record exists. But this is the repository's
own recurring failure shape aimed at an incident message — the one artifact
that gets read under time pressure, by someone deciding which of two
credentials to look at. **Not patched here**: it is a message, not a semantic,
and this milestone's authority is capture, not remediation. It is recorded so
the fix is a one-line change with a reason attached rather than a rediscovery.

### What was established before the stop

| link | state | evidence |
|---|---|---|
| E1 host constant | **PASS** | `WS_HOSTS[production]` is `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, the host the AsyncAPI spec publishes, and is not the demo constant |
| E2 DNS | **recorded** | production WS resolves to 8 addresses; demo WS to 2, disjoint. Recorded, not asserted — a CDN may legitimately share addresses |
| E3 TLS out of band | **PASS** | production WS presents `CN=*.kalshi.com`, SAN `["*.kalshi.com"]`, issuer *Amazon RSA 2048 M01*; production REST presents `CN=elections.kalshi.com`. The demo host presents `CN=demo.kalshi.co`, SANs `["*.demo.kalshi.co", "demo.kalshi.co"]`. **Cryptographically distinct identities**, verified against the system trust store with hostname checking on |
| E4 credential identity | **PASS** | 200 from `api.elections.kalshi.com`, key found in that account |
| E4 credential scope | **FAIL — the stop** | `["read", "write"]` |
| E5 capture-socket TLS | **NOT REACHED** | no socket was opened |
| E6 universe | **NOT REACHED** | no census, no manifest, no subscription |
| structural order-API guard | **CLEAN** | 16 modules, 3,292 identifiers, 0 findings, on the throwaway clone of `main` |

**§11 B1 remains OPEN.** The production WS host is still `UNVERIFIED` in the
sense the contract means: documentation and a certificate are both stronger
than a name, and neither is a handshake. `app/realtime/kalshi.py:52-55` must
not be edited to claim otherwise.

### Deliberate ordering deviation, and why

The authorized sequence is preflight → handshake → verify production → capture.
The credential and endpoint evidence were gathered **before** the preflight was
run to completion, so **no session root was claimed**. Claiming one mints an
immutable, un-removable claim (`session_root.py` exposes no delete), and a
claim for a session that provably cannot happen is a fabricated evidence
record — the exact artifact class this repo keeps finding. The preflight's
first and only load-bearing gate, the structural guard, was run standalone and
is clean; the remaining two gates are the endpoint report (above) and the root
claim (deliberately not made).

### What is required before attempt 2

1. **A production observer credential whose venue-reported scopes are exactly
   `["read"]`.** Provisioned at Kalshi as read-only; nothing local can fix a
   key's scopes, and nothing local should try.
2. Re-run `scripts/kalshi_prod_capture_p4.py evidence`. It must print
   `"passed": true` with `scopes: ["read"]`. That single command is the whole
   gate.
3. Only then: preflight against a fresh root, then capture.

Everything else is ready and was exercised: the instrument, the evidence chain,
the guard, and the throwaway-clone run procedure. Attempt 2 is a short step.
