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

### Test attribution — 2 failures, and they are a fixture time bomb on `main`

The kalshi selection ran **1,799 passed / 2 failed / 5 skipped** against a
stated baseline of 1,801 passed / 0 failed. Attributed by measurement, not by
argument:

| | |
|---|---|
| failing | `test_kalshi_tape_manifest_001.py::test_no_credential_is_read_when_the_snapshot_runs`, `::test_cli_command_is_reachable_and_writes_both_artifacts` |
| on this branch | **2 failed** |
| on pristine `main` (`975d1b3`, detached worktree) | **2 failed, identically** |
| `git diff main -- app/ tests/` | **empty** — the code under test and the tests are byte-identical to `main` |

**Root cause: a hardcoded fixture epoch that has expired.** The fake venue pins
`T0 = 2026-08-15T12:00:00Z` (`tests/test_kalshi_tape_manifest_001.py:58`) and
gives every synthetic market `close_time = T0 + 3 days`, i.e.
**2026-08-18T12:00:00Z**. Today is 2026-08-19. So all 24 synthetic markets have
already closed, the eligibility funnel correctly rejects all 24 under `closes`,
the manifest correctly returns REFUSED, and the two tests that assert *"a
healthy venue must exit 0 (QUALIFIED)"* correctly fail.

**Every component behaved correctly. The fixture is wrong about what time it
is.** This is doctrine 9 turned on the suite itself: a fixture is an executable
claim about external reality, and this one claims a date.

**This is NOT the known wall-clock flake class** (contract L14, rotating
membership, baseline 5,195/8). It is worse in one specific way and better in
another: it is **deterministic and permanent** — it became due at
2026-08-18T12:00Z, it will fail on every run from now on, and it will never
recover on its own — but for the same reason it cannot mislead intermittently.

**Not fixed here.** It is outside this run's authority (capture phase, no
repairs), and the fix is a design choice rather than a typo: pinning `T0`
relative to `now()` makes the fixture self-maintaining but makes its arithmetic
non-reproducible, while re-pinning the constant re-arms the same bomb on a
later date. That choice deserves its own small change with its own review.
Recorded here so the next reader does not spend the time re-deriving it.

### The comparison that matters — answered at the REST layer, not the wire

The credential stop blocks the WebSocket capture. It does **not** block the one
production measurement that needs no credential: the same read-only
`GET /markets` census, with the same measured statistic, that produced the DEMO
finding this milestone was written around. So it was run.

`kalshi-tape-manifest --environment production`, 2026-08-19T23:30:26Z, 488
pages, 207.6 s census, then a 7.6-minute timed activity probe.
Artifact: `docs/evidence/KALSHI-PROD-QUAL-CAPTURE-production-activity-distribution.json`.

| | DEMO (`KALSHI-TAPE-MANIFEST-001`) | **PRODUCTION (measured here)** |
|---|---|---|
| verdict | **REFUSED** — the middle was empty | **QUALIFIED** |
| frame | — | **97,408** open markets |
| eligible | could not reach 12 | **618**, across **296 events / 141 series** |
| shape | 4 hyperactive → **98.3× cliff** → broad quasi-flat plateau | **no cliff anywhere** |
| largest adjacent ratio in the ranked population | **98.3×**, after rank 4 | **2.53×**, and it occurs at **rank 617** — the very tail |
| plateau | 30.9% of eligible inside a single **15–17 c/min** band | no comparable band; the body is continuous |
| median | — | **68.17 c/min** |
| decile spread | — | p10 1,428 · p25 324 · p50 68.2 · p75 11.1 · p90 2.35 |
| max / dynamic range | — | 48,939.55 c/min · **2.48 million ×** min-to-max |

**The DEMO rate distribution did NOT predict the production one, and the
difference is categorical rather than a matter of scale.** DEMO was four
hyperactive instruments separated from a flat plateau by a two-order-of-magnitude
discontinuity. Production is a smooth, continuous distribution spanning roughly
six orders of magnitude in which **the single largest step between adjacent
ranked markets is 2.53×** — there is no cliff, no plateau, and no separated
tier. DEMO's entire plateau band (15–17 c/min) lands near production's *lowest*
selected stratum (18.6–19.2 c/min): the whole of DEMO's "broad middle"
corresponds to the bottom of production's range, with two further tiers above
it that DEMO had no analogue for at all.

**A trap in the artifact, flagged before someone falls into it.**
`strata_ranges.high_over_medium.ratio` reads **94.77×** — arrestingly close to
DEMO's 98.3×, and it means something completely different. That ratio is a gap
between *selected strata*, and the selection rule requires separated strata
(`min_separation_ratio = 2.0`); it is manufactured by the procedure, not
observed in the venue. The comparable statistic is the largest adjacent ratio in
the **ranked population**, and that is **2.53×**. Reading 94.77 as "production
looks like DEMO after all" would invert this entire finding.

**What this does and does not answer.** It is the **traded-contracts-per-minute**
distribution measured over REST. It is **not** the WebSocket frame-rate
distribution, which remains **NOT MEASURED** and needs the capture. Frame rate
is driven by quote updates as much as by trades, and nothing here observes a
quote update. So this settles the shape question at the layer it was originally
raised on, and leaves the message-rate question — the one that bears on
`DEFAULT_MAX_SEGMENT_RECORDS`, rotation constants and the append ceiling
(contract L9, L16) — open.

**Stated limitations, because the funnel is narrow.** The 618 eligible are not
618 of 97,408. They are the survivors of a three-stage funnel: census (97,408) →
screen to the top **1,200** by 24-hour volume → timed probe (618 eligible).
**96,208 markets were never probed at all.** So `low` means *least active among
the eligible*, not *inactive on Kalshi*, and the distribution above describes
the actively-traded head of the venue rather than the venue. The artifact says
so on its face under `representativeness`, and `lifetime_volume_is_monotonic`
was re-verified **True** in production — the doctrine-8 precondition for the
statistic holds there too, and was not assumed from DEMO.

**This is a measurement, NOT the frozen session manifest for a capture.** It
must not be reused as one: the manifest's value is its proximity to the session
it governs, and one frozen today would be stale and authoritative-looking by the
time attempt 2 runs. Attempt 2 freezes its own. What this run establishes is
that a production manifest **can** qualify — which DEMO's could not.
