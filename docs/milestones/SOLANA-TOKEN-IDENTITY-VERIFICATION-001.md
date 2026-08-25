# SOLANA-TOKEN-IDENTITY-VERIFICATION-001

**Status: BUILT, NOT DEPLOYED.** On branch
`solana-token-semantic-corroboration-001`, deliberately not merged and not on
EVO while a Kalshi confirmation session is running.

Makes `CANONICALLY_VERIFIED` **reachable**. Until now `default_ladder()`
provably could not produce one, so the primary alpha cohort was empty by
construction and the whole social→fill lane was blocked behind it.

## Two gates, and the boundary between them

> **Gate 1 — is this a real Solana mint?**
> **Gate 2 — does this social artifact actually refer to that mint canonically?**

Keeping them separate is the design, not tidiness. **Every one of the six
threats in `token.THREATS` involves an address that is perfectly real.** Gate 1
passes them all. A system that conflated the two would verify a scam post that
quotes a competitor's genuine contract address.

```text
TEXT_CANDIDATE ──gate 1──> CHAIN_VERIFIED ──gate 2──> CANONICALLY_VERIFIED
       │                        │                            (joinable)
       │                        ├──> CORROBORATION_PENDING   (looked, found nothing)
       │                        └──> CONFLICTING_EVIDENCE    (authoritative contradiction)
       ├──> CHAIN_INVALID · NOT_FOUND · WRONG_ACCOUNT_TYPE
       │    UNKNOWN_TOKEN_PROGRAM · UNINITIALIZED_MINT · UNAVAILABLE
       └──> AMBIGUOUS · REJECTED
```

## Threat closure

| threat (`token.THREATS`) | closed by | how |
|---|---|---|
| decoy address in a scam post | **gate 2** | the poster is not an official project surface |
| old mint quoted in a stale post | **gate 2** | the official surface now names a different mint → `CONFLICTING_EVIDENCE` |
| address copied from an unrelated source | **gate 1 + 2** | a wallet fails the account-type gate; a real foreign mint has no authoritative binding |
| competitor token mentioned in passing | **gate 2** | the publication's subject is a different mint |
| screenshot containing an unrelated contract | **gate 2** | no authoritative surface publishes it |
| quote-posted scam inside a legitimate post | **gate 2** | `ProvenanceScope.QUOTED` can neither bind nor conflict |

**Gate 1 closes exactly one of six on its own**, and a test asserts that so the
boundary cannot quietly erode.

## The acceptance rule

> `CANONICALLY_VERIFIED` ⟺ `CHAIN_VERIFIED` ∧ authoritative binding ∧ ¬authoritative conflict

A **binding** requires all four, each answering a named threat: the evidence
must be a `PUBLISHED_MINT` (not a mention, not a ticker), from an
`OFFICIAL_PROJECT_SURFACE` (not a third party, however many), **not** in
`QUOTED` scope, and about **the candidate mint itself**.

A **conflict** is an authoritative disavowal of the candidate, or an
authoritative publication naming a different mint. **A conflict is never
outvoted by bindings** — if an official surface both published and disavowed a
mint, the honest state is that we do not know.

### What is structurally impossible, not merely discouraged

* **No confidence threshold.** `CorroborationEvidence` carries no score,
  weight or probability field, so there is nothing to compare against a cutoff.
  A model may extract and normalize evidence; only `decide_corroboration` can
  emit a status.
* **No ticker identity.** Every scam copies a name. Fifty official-surface
  `TICKER_ONLY` items still verify nothing.
* **No transitivity.** "An official account mentioned this mint" is not "this
  account owns this mint".
* **No strength in numbers.** 1,000 third-party publications of a real mint sum
  to `CORROBORATION_PENDING`, with all 1,000 recorded as seen and ignored.

## Evidence

**Gate 1 — 18 tests, 10 mutations, 10 killed.** Adversarial cases are the
suite: a real wallet, a token *account* (someone's balance) versus the mint, an
unknown program owner, an uninitialized mint, a truncated buffer, a corrupt
COption tag. An RPC failure is `UNAVAILABLE` and explicitly never `NOT_FOUND` —
an unreachable node must not be cached as "this token does not exist".

**Gate 2 — 16 tests covering all eight adversarial cases, 10 mutations, 10
killed**, including: quoted content binding, third parties binding, a mention
counting as publication, a ticker resolving identity, the subject mint going
unchecked, a conflict losing to bindings, a disavowal ignored, a migration
conflict ignored, the gate-1 short-circuit removed, and a majority vote
introduced.

**Falsifier 7 re-run** (§9 of `SOCIAL-FILL-MEASUREMENT-SEAM-001` requires it
after any change to `app/seam`): **363 seam tests green**, and the controls
still have teeth — widening `JOINABLE_STATUSES` fails 7, and making
`Measurement.__bool__` return instead of raise fails 1. Control 5 is now a
*stronger* test than before, because the three new states are also states that
must never become joinable.

## Two defects the work exposed

* The base58 encoder appended a spurious sentinel when the numeric part was
  empty, so an all-zero pubkey — the system program — encoded to **33
  characters instead of 32**. Caught by round-tripping a real constant.
* A mutation deleting the token-*account* branch passed 17/17: a 165-byte
  account also fails the generic length check, and both messages contained the
  word the test matched on. "Someone's balance" and "wrong length" are
  different mistakes and an operator needs to know which happened.

## What this does NOT do

* It does not wire a live RPC. `AccountReader` is a Protocol; the production
  reader and its authorization are a separate decision.
* It does not build the extractor. Deciding *that a surface is official* is
  itself evidence and belongs to the model layer, recorded with a hash.
* It does not touch `delivery_mode`, the clock contract, or the join.
* **It is not a social experiment.** Nothing here evaluates whether social
  information predicts anything. The next milestone is seam qualification —
  `CANONICALLY_VERIFIED` + `delivery_mode=LIVE` + a clock-safe interval →
  on-chain reaction and quote linkage — **not** model training.
