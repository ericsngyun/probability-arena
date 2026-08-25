# SOCIAL-EVIDENCE-EXTRACTOR-001

**Status: BUILT, NOT MERGED.** On branch `social-evidence-extractor-001`, held
off `main` and off EVO while S04 is armed against a pinned commit.

The semantic layer, and only that.

```text
model:                raw artifact  ->  typed evidence claims
deterministic policy: typed claims  ->  Gate 2 verdict
```

The model may say *"this span appears to be a direct publication of mint X by
source Y"*. It may not conclude *"therefore X is canonical"*.
`decide_corroboration` remains the only place that conclusion exists, and this
module has **no field through which such a conclusion could travel**.

## One artifact yields many claims

*"Old contract X is fake, official is Y"* is not a single mint guess. It is two
facts:

```text
X -> DISAVOWED_MINT
Y -> PUBLISHED_MINT
```

An extractor that returned one "best" mint would have made the decision it is
forbidden to make. A test asserts every claim the model returns survives to the
gate, and a mutation that keeps only the first fails **6** tests.

## The claim schema

`artifact_id` · `candidate_mint` · `subject_entity` · `relationship` ·
`source_identity` · `source_surface` · `span_origin` · `evidence_span_hash` ·
`extractor_model` · `extractor_version`

`relationship` is the load-bearing field: **"a mint appears somewhere in the
post" is not enough**. The same string can be published, mentioned in passing,
disavowed as fake, or named as superseded — four different facts.

`source_surface` records a **claim**, not a finding: whether a surface really is
official is itself evidence. `CLAIMED_OFFICIAL_*` becomes
`OFFICIAL_PROJECT_SURFACE` only in the adapter, and a third-party account
describing itself as an official partner does **not** cross that line.

### Fields that cannot exist

`verified` · `canonical` · `confidence_to_accept` · `score` · `trade_signal` ·
`recommendation` — refused at construction, so a future model or prompt cannot
smuggle a decision through an extra key. Removing that check fails **6** tests.

## The adversarial corpus (12 cases, all passing)

official publication · "X is fake, Y is official" · migration A→B · official
account quoting scam content · official discussing a competitor mint · multiple
mints in one post · quoted vs source-authored sections of the same post ·
ticker-only · screenshot-derived · impersonator copying an official
announcement · third party claiming to represent a project · forwarded content.

## Three defects the corpus and mutations exposed

1. **`MIGRATED_MINT` did not conflict.** It mapped to Gate 2's
   `NAMES_DIFFERENT_MINT`, whose contract is that `subject_mint` is the *other*
   mint — but a migration claim's subject is the mint being migrated **away
   from**. So `subject == candidate`, Gate 2 correctly saw no conflict, and a
   superseded mint silently failed to conflict. It now maps to `DISAVOWAL`: for
   *identity*, "we migrated away from X" and "X is not ours" are one claim. The
   fake-vs-superseded distinction survives in `detail`.

2. **A missing field became the strongest possible claim.** Defaulting an
   absent `relationship` to `PUBLISHED_MINT` passed **26/26**, because the suite
   tested an *invalid value* and never an *absent key*. Silence from the model
   is not assent. Now every missing field is refused.

3. **The boundary leaked a bare `KeyError`** when a claim named no mint,
   reaching callers as a generic failure rather than as "the model did not name
   a mint".

## Mutations

Ten applied, ten killed after the fixes above: decision fields allowed through ·
third party becomes official · forwarded becomes source-authored · quoted
becomes source-authored · mention becomes publication · migration stops
conflicting · only the first claim kept · span hash no longer required ·
unknown enum silently defaulted · unknown surface becomes official.

## Not done

* **No live model call.** `ClaimExtractor` is a Protocol; wiring one is a
  separate authorization, as is the live RPC for Gate 1.
* **No real-artifact corpus.** Every case is a constructed fixture.
* Nothing here evaluates whether social information predicts anything. The next
  milestone is seam qualification — `CANONICALLY_VERIFIED` +
  `delivery_mode=LIVE` + a computable clock interval → joinable event — **not**
  `SOCIAL-LEAD-LAG-001`.
