# EVIDENCE-JOIN-CONTRACT-001

**Status: CONTRACT, NOT IMPLEMENTED.** Written 2026-08-21 by reading
`app/social/` and `app/fills/` against each other. Defines how a future
experiment joins

> social event → on-chain state → quote → realized fill → markout

and records the mismatches that exist **today**, so a later experiment does not
reconcile them ad hoc — which is how two honest datasets produce one dishonest
number.

No model, no signal, no alpha. Join keys and provenance only.

---

## 1. The join keys

| link | key | status |
|---|---|---|
| social artifact → entity | `EntityResolution.resolved_mint` + `ResolutionConfidence` | present, **unverified against chain** (§4) |
| entity → on-chain state | mint (base58) | present both sides |
| quote → fill | `tx_signature` | present, authoritative |
| fill → markout | `slot` + pool identity | **pool identity is `NOT_RECONSTRUCTABLE`** |
| everything → time | see §3 — **three clock domains, only one pair protected** |

## 2. Two absence vocabularies that do not compose

This is the first thing a join would silently break.

| | `app/fills` `AbsenceReason` | `app/social` `DeferredState` |
|---|---|---|
| | `NOT_PROVIDED` | `ABSENT` |
| | `NOT_APPLICABLE` | `NOT_APPLICABLE` |
| | `NOT_RECONSTRUCTABLE` | `OBSERVED_NONE` |
| | `NOT_YET_OBSERVED` | `OBSERVED` |
| | `NOT_AUTHORIZED` | — |

They agree on exactly one member. The distinctions each side considered worth
encoding are **different distinctions**:

* `app/fills` separates *why we cannot have it* — never provided, cannot be
  rebuilt, not yet seen, or **not authorized** (waiting will never produce it,
  which is `ε_fill`'s state today).
* `app/social` separates *what looking achieved* — nobody looked (`ABSENT`),
  looked and found nothing (`OBSERVED_NONE`), looked and found something.

**BINDING: the join must NOT map one onto the other.** Collapsing
`OBSERVED_NONE` into `NOT_PROVIDED` would turn "we watched for an on-chain
reaction and there was none" — a real measurement, and the negative case any
lead-lag study needs — into "we have no data". That is doctrine 10 committed at
the seam rather than in a field.

**Required before the join:** one **explicit, lossless** mapping declared as a
table, where any pair that does not correspond is an error rather than a
best-effort match. A joined row carries both vocabularies or neither.

## 3. Three clock domains, and only one pair is protected

| domain | who owns it | where |
|---|---|---|
| **platform** | X/Telegram/Discord | `SourceCreatedAt`, fidelity `UNVERIFIED` |
| **ours** | this host | `OurReceivedAt` (wall + `monotonic_ns` + `epoch_id`); `t_quote`, `t_submit` |
| **chain** | Solana | `slot`, `t_confirmed` |

**The asymmetry that matters.** `app/social` protects our-clock intervals
*structurally*: `pipeline_interval_us()` accepts only two `OurReceivedAt` values
**in one process epoch**, and refuses across epochs. `app/fills` carries bare
`datetime` for `t_quote` / `t_submit` — **no monotonic anchor, no epoch id**.

So the most interesting interval in the whole programme —

> *social artifact received → quote observed*

— subtracts a bare wall-clock `datetime` from a monotonic-anchored stamp. That
computation **bypasses the social side's epoch guard entirely**, is exposed to
any NTP step between the two readings, and would produce a plausible number with
no way to detect it was wrong. It is the exact shape of defect this repository
keeps finding.

**BINDING:**

1. **`app/fills` timestamps that we generate (`t_quote`, `t_submit`) must adopt
   the `OurReceivedAt` type** — wall + `monotonic_ns` + `epoch_id` — before any
   social→quote interval is computed. Until then, that interval is
   `NOT_COMPUTABLE`, not "approximately fine".
2. **No interval may cross a domain boundary without being typed as
   cross-domain.** A social→confirmation figure spans platform → ours → chain
   and inherits the worst fidelity in the chain, which is `UNVERIFIED`.
3. **Chain time is not our time.** `t_confirmed` is a cluster-derived stamp;
   `slot` is the ordering primitive and is the one to prefer for chain-side
   ordering.

## 4. The mint join is weaker than it looks

`app/social` produces `resolved_mint` by extracting **plausible-length base58
strings from free text**, with a typed confidence and an `UNRESOLVED` state.
`app/fills` produces a mint **read off a confirmed transaction**.

These are not the same kind of object, and joining on string equality treats
them as if they were. A base58 string of the right length that never existed
on-chain will simply fail to match — harmless. The hazard is the opposite: a
string that matches a **real but wrong** mint (a lookalike, a stale address in
an old post, a decoy in a scam post) joins successfully and attributes on-chain
activity to a social event that never referenced it.

**BINDING:** a social-side mint may enter a join only after it has been
**confirmed to exist on-chain**, and the joined row carries
`ResolutionConfidence` **and** the confirmation. Confidence must never be
dropped at the seam — a low-confidence resolution that survives into an
experiment as a bare mint string is an unrecorded assumption.

## 5. Provenance travels, or the join is unauditable

Every joined row carries, per source: the artifact's `raw_content_hash` and
`ingestion_version`; the fill's `tx_signature` and fixture/decoder version; and
the **`delivery_mode`** of the social artifact.

`delivery_mode` is not optional at the seam. A backfilled artifact has an
honest `our_received_at` that is **not live delivery timing**, so pooling
backfilled and live artifacts puts a long right tail on any latency figure that
is an artefact of our own downtime. `SOCIAL-TAPE-001` records that nothing
forces a consumer to condition on it — **this contract is where that obligation
lands.** No pooled delivery-timing figure may be reported without a
`delivery_mode` breakdown beside it.

## 6. What must exist before the first joined experiment

1. The explicit absence mapping table (§2).
2. `OurReceivedAt` adopted for our-generated fill timestamps (§3).
3. On-chain confirmation of social-resolved mints (§4).
4. A pool-identity decision — it is `NOT_RECONSTRUCTABLE` today, so markouts
   must name the price source they used and its limits.
5. `ε_fill` remains `NOT_AUTHORIZED`. **The join is constructible without it**;
   what is *not* constructible without it is any claim about executable edge.

## 7. What this contract cannot do

It cannot make the three clocks comparable — it can only stop them being
silently mixed. It cannot validate a social→on-chain causal claim; cross-domain
ordering by receive timestamps is **correlational**, and doctrine 12 puts the
causal question to an agent screen and the economic question to a deterministic
evaluator, never to the join itself.
